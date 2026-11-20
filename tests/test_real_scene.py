"""Acceptance of the versioned OSM and terrain scene served by the app."""

import json
from collections import Counter
from itertools import pairwise, permutations
from statistics import mean
from time import perf_counter

import pytest
import shapely
from fastapi.testclient import TestClient
from shapely.geometry import Point, box, shape

from backend.config import BUILDING_BUFFER_M, CEILING_M, DATA_DIR, LLMSettings, MISSION_LAYERS, STUDY_BOUNDS
from backend.core.airspace import Airspace
from backend.core.planner import RoutePlanner
from backend.main import create_app
from backend.schemas import DispatchResult, ReplanResult
from test_planner_and_api import (
    DELIVERY_QUERY, INSPECTION_QUERY, _crossing_hazard, _footprint_intersection, _verify_api_route,
)


@pytest.fixture(scope="module")
def scene():
    return Airspace.from_directory()


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(LLMSettings())) as api:
        yield api


def test_real_assets_are_wgs84_and_share_obstacle_footprints(scene, client):
    response = client.get("/api/layers/all")
    assert response.status_code == 200
    layers = response.json()
    assert layers["scene_version"] == scene.scene_version
    assert all(layers[key] == scene.geojson[key] for key in scene.geojson)
    terrain_asset = layers["terrain"]
    assert terrain_asset["crs_horizontal"] == "EPSG:4326"
    assert terrain_asset["source_vertical_datum"] == "EGM96"
    assert box(*terrain_asset["bounds"]).covers(box(*STUDY_BOUNDS))
    assert scene.field.terrain.highest > 0

    buildings = layers["buildings"]["features"]
    assert len(buildings) == 463
    assert Counter(item["properties"]["height_source"] for item in buildings) == {
        "osm_height": 2, "osm_levels_estimate": 50, "footprint_estimate": 411,
    }
    aoi = box(*STUDY_BOUNDS)
    obstacles = {obstacle.id: obstacle for obstacle in scene.field.obstacles}
    for feature in buildings:
        raw = shape(feature["geometry"])
        assert raw.is_valid and aoi.covers(raw)
        properties = feature["properties"]
        obstacle = obstacles[properties["id"]]
        plotted_footprint = shapely.transform(raw, scene.frame.project_xy)
        assert obstacle.footprint.equals(plotted_footprint.buffer(BUILDING_BUFFER_M))
        modeled_roof = properties["base_alt"] + properties["height"]
        assert obstacle.max_alt >= modeled_roof
        if properties["height_source"] == "footprint_estimate":
            assert obstacle.max_alt >= CEILING_M
    assert all(aoi.covers(shape(road["geometry"])) for road in layers["roads"]["features"])


def test_real_pads_are_on_surfaces_and_accessible(scene):
    buildings = [
        (shape(item["geometry"]), item["properties"])
        for item in scene.geojson["buildings"]["features"]
    ]
    for pad in scene.vertiports.values():
        x, y, altitude = scene.frame.to_local(pad.position)
        touching = [props for geom, props in buildings if geom.covers(Point(pad.position.lon, pad.position.lat))]
        if pad.properties.host_building_id:
            assert len(touching) == 1 and pad.roof_obstacle.id == pad.properties.host_building_id
            assert altitude == pytest.approx(touching[0]["base_alt"] + touching[0]["height"], abs=.001)
        else:
            assert not touching
            assert altitude == pytest.approx(scene.field.terrain.elevation(x, y), abs=.06)
        assert scene.field.segment_clear((x, y, altitude), (x, y, 85), roof_access=pad.roof_obstacle)


def test_scene_rejects_pad_buried_below_the_roof(tmp_path):
    for source in DATA_DIR.iterdir():
        if source.suffix in {".json", ".geojson"}:
            (tmp_path / source.name).write_bytes(source.read_bytes())
    target = tmp_path / "vertiports.geojson"
    pads = json.loads(target.read_bytes())
    roof = next(item for item in pads["features"] if item["properties"]["id"] == "VP03")
    roof["properties"]["altitude"] -= 1
    target.write_text(json.dumps(pads), encoding="utf-8")
    with pytest.raises(ValueError, match="VP03 is below an intersecting building roof"):
        Airspace.from_directory(tmp_path)


@pytest.mark.parametrize(("query", "destination", "layer"), [
    (DELIVERY_QUERY, "VP03", "delivery"),
    (INSPECTION_QUERY, "VP04", "inspection"),
])
def test_real_dispatch_routes_and_version(scene, client, query, destination, layer):
    response = client.post("/api/agent/dispatch", json={"query": query, "scene_version": scene.scene_version})
    assert response.status_code == 200, response.text
    plan = DispatchResult.model_validate(response.json())
    assert plan.scene_version == scene.scene_version and plan.corridor == layer
    field, _ = scene.with_hazards((), plan.intent.avoid_zone_ids)
    _verify_api_route(plan, scene, "VP01", destination, field)
    stale = client.post("/api/agent/dispatch", json={"query": query, "scene_version": "stale"})
    assert stale.status_code == 409 and "刷新" in stale.json()["detail"]


@pytest.fixture(scope="module")
def real_detour(scene):
    baseline = RoutePlanner(scene).plan_3d_route("VP04", "VP05", "inspection")
    return baseline, *_crossing_hazard(scene, baseline)


@pytest.mark.parametrize("emergency_type", ["wind_gust", "temp_nofly"])
def test_real_temporary_restriction_and_diversion(scene, client, real_detour, emergency_type):
    baseline, current, hazard = real_detour
    changed, _ = scene.with_hazards((hazard,))
    cylinder = changed.obstacles[-1]
    baseline_pts = [scene.frame.to_local(point) for point in baseline.waypoints]
    assert any(_footprint_intersection(a, b, cylinder) for a, b in pairwise(baseline_pts))
    request = {"current_pos": current.model_dump(), "target_id": "VP05",
               "mission_type": "inspection", "scene_version": scene.scene_version}
    wind = client.post("/api/agent/replan", json={
        **request, "emergency_type": emergency_type, "dynamic_hazard": hazard.model_dump(),
    })
    assert wind.status_code == 200, wind.text
    detour = ReplanResult.model_validate(wind.json())
    assert detour.target_id == "VP05" and detour.temporary_nofly_zone
    assert detour.scene_version == scene.scene_version
    _verify_api_route(detour, scene, current, "VP05", changed)
    battery = client.post("/api/agent/replan", json={
        **request, "target_id": "VP03", "emergency_type": "low_battery",
        "active_hazards": [hazard.model_dump()],
    })
    assert battery.status_code == 200, battery.text
    diverted = ReplanResult.model_validate(battery.json())
    assert scene.vertiports[diverted.target_id].properties.type == "emergency"
    _verify_api_route(diverted, scene, current, diverted.target_id, changed)


def test_real_scene_performance(scene, real_detour):
    planner = RoutePlanner(scene)
    _, current, hazard = real_detour
    cases = [
        (origin, target, corridor, ())
        for origin, target in permutations(scene.vertiports, 2)
        for corridor in MISSION_LAYERS
    ]
    cases += [(current, "VP05", "inspection", (hazard,))] * 5
    timings = []
    for origin, target, layer, hazards in cases:
        start = perf_counter()
        plan = planner.plan_3d_route(origin, target, layer, hazards)
        elapsed = (perf_counter() - start) * 1000
        assert plan.distance_m > 0 and elapsed < 500, (origin, target, layer, elapsed)
        timings.append(elapsed)
    print(f"WHU real-scene A* ({len(timings)} runs): mean {mean(timings):.2f} ms, max {max(timings):.2f} ms")
