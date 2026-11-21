"""Regression tests for the prototype's original controlled scenarios."""

import json
from itertools import combinations, pairwise, permutations
from math import dist, hypot
from statistics import mean
from time import perf_counter

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import substring

from backend.config import DATA_DIR, LLMSettings, MISSION_LAYERS, ORIGIN_LON_LAT, STUDY_BOUNDS
from backend.core.airspace import Airspace, LocalENU, Obstacle, ObstacleField
from backend.core.planner import NoRouteError, RoutePlanner
from backend.main import create_app
from backend.schemas import (
    BuildingProperties, DispatchResult, DynamicHazard, NoFlyProperties, Position, ReplanResult,
    RoutePlan, VertiportProperties,
)

DELIVERY_QUERY = "中午高峰配送：将餐食从信息学部食堂送至测国重楼顶，避开图书馆静谧区"
INSPECTION_QUERY = "夜间高空安防巡检：从信息学部起飞，沿 60–90m 巡检层前往图书馆西侧巡检点"
MOCK_DATA = DATA_DIR / "mock_backup"


@pytest.fixture(scope="module")
def airspace():
    return Airspace.from_directory(MOCK_DATA)


@pytest.fixture(scope="module")
def planner(airspace):
    return RoutePlanner(airspace)


def test_geojson_contracts_and_building_footprints():
    study_area = box(*STUDY_BOUNDS)
    layers = (
        ("whu_buildings", BuildingProperties, "Polygon", 25, ("height",)),
        ("vertiports", VertiportProperties, "Point", 6, ("altitude",)),
        ("nofly_zones", NoFlyProperties, "Polygon", 3, ("min_alt", "max_alt")),
    )
    for name, schema, geometry_type, minimum_count, float_fields in layers:
        document = json.loads((MOCK_DATA / f"{name}.geojson").read_text(encoding="utf-8"))
        assert document["type"] == "FeatureCollection"
        assert len(document["features"]) >= minimum_count
        ids, geometries = set(), []
        for feature in document["features"]:
            assert feature["type"] == "Feature"
            values = feature["properties"]
            assert all(isinstance(values[field], float) for field in float_fields)
            properties = schema.model_validate(values)
            assert properties.id not in ids
            ids.add(properties.id)
            geometry = shape(feature["geometry"])
            assert geometry.geom_type == geometry_type and geometry.is_valid
            assert not geometry.is_empty and study_area.covers(geometry)
            if geometry_type == "Polygon":
                ring = feature["geometry"]["coordinates"][0]
                assert ring[0] == ring[-1]
            geometries.append(geometry)
        if name == "whu_buildings":
            assert all(not a.intersects(b) for a, b in combinations(geometries, 2))


def test_wgs84_enu_round_trip():
    frame = LocalENU(*ORIGIN_LON_LAT)
    lon, lat = ORIGIN_LON_LAT
    assert frame.to_local(Position(lon=lon, lat=lat, alt=28)) == (0, 0, 28)
    east = frame.to_local(Position(lon=lon + 0.001, lat=lat, alt=0))
    north = frame.to_local(Position(lon=lon, lat=lat + 0.001, alt=0))
    assert east[:2] == pytest.approx((95.98, 0), abs=0.1)
    assert north[:2] == pytest.approx((0, 110.86), abs=0.1)
    for lon, lat in [(114.349, 30.524), (114.362, 30.533), (114.35454, 30.53069)]:
        pos = Position(lon=lon, lat=lat, alt=57.2)
        result = frame.to_position(frame.to_local(pos))
        assert (result.lon, result.lat) == pytest.approx((pos.lon, pos.lat), abs=1e-10, rel=0)
        assert result.alt == pos.alt


def _footprint_intersection(start, end, obstacle):
    """Clip a segment to a prism's altitude interval with Shapely linear referencing."""
    z0, z1 = start[2], end[2]
    if min(z0, z1) > obstacle.max_alt or max(z0, z1) < obstacle.min_alt:
        return False
    if start[:2] == end[:2]:
        return obstacle.footprint.intersects(Point(start[:2]))
    segment = LineString([start[:2], end[:2]])
    if z0 != z1:
        lower, upper = sorted((z0, z1))
        enter = (max(lower, obstacle.min_alt) - z0) / (z1 - z0)
        leave = (min(upper, obstacle.max_alt) - z0) / (z1 - z0)
        segment = substring(segment, min(enter, leave), max(enter, leave), normalized=True)
    return obstacle.footprint.intersects(segment)


def _verify_route(route, airspace, mission, end_id, start, field=None):
    waypoints = route.waypoints
    is_pad = isinstance(start, str)
    origin_pad = airspace.vertiports[start] if is_pad else None
    origin = origin_pad.position if is_pad else start
    destination = airspace.vertiports[end_id]
    assert waypoints[0].model_dump(exclude={"phase"}) == origin.model_dump()
    assert waypoints[-1].model_dump(exclude={"phase"}) == destination.position.model_dump()
    if is_pad:
        assert waypoints[0].phase == "takeoff"
    assert waypoints[-1].phase == "landing"
    positions = [airspace.frame.to_local(point) for point in waypoints]
    layer = MISSION_LAYERS[mission]
    assert any(point.phase == "cruise" for point in waypoints)
    assert route.distance_m == pytest.approx(sum(dist(a, b) for a, b in pairwise(positions)))
    field = field or airspace.field
    for index, ((a, b), waypoint) in enumerate(zip(pairwise(positions), waypoints[1:], strict=True)):
        if field.terrain:
            assert field.terrain.segment_clear(a, b)
        roof = None
        if waypoint.phase == "takeoff":
            assert index == 0 and is_pad and b[2] > a[2]
            roof = origin_pad.roof_obstacle
        if waypoint.phase == "landing":
            assert index == len(waypoints) - 2 and b[2] < a[2]
            roof = destination.roof_obstacle
        if waypoint.phase in {"takeoff", "landing", "transition"}:
            assert a[:2] == b[:2]
        else:
            assert layer.min_alt <= min(a[2], b[2]) <= max(a[2], b[2]) <= layer.max_alt
        for obstacle in field.obstacles:
            if obstacle is roof:
                assert min(a[2], b[2]) >= obstacle.max_alt - 5
                continue
            assert not _footprint_intersection(a, b, obstacle), obstacle.id


@pytest.mark.parametrize(("start", "end", "mission"), [
    ("VP01", "VP03", "delivery"),
    ("VP01", "VP04", "inspection"),
    ("VP03", "VP01", "delivery"),
])
def test_campus_routes_respect_buffers_and_layers(airspace, planner, start, end, mission):
    route = planner.plan_3d_route(start, end, mission)
    _verify_route(route, airspace, mission, end, start)
    if mission == "delivery":
        quiet_zone = next(ob for ob in airspace.field.obstacles if ob.id == "NFZ01")
        start_point = airspace.frame.to_local(airspace.vertiports[start].position)
        end_point = airspace.frame.to_local(airspace.vertiports[end].position)
        assert quiet_zone.footprint.intersects(LineString([start_point[:2], end_point[:2]]))
        assert sum(waypoint.phase == "cruise" for waypoint in route.waypoints) >= 2


def test_building_buffers_are_applied_on_both_axes(airspace):
    roof_pad = airspace.vertiports["VP03"]
    obstacle = roof_pad.roof_obstacle
    assert obstacle.id == "BLD03" and obstacle.max_alt == 48
    document = json.loads((MOCK_DATA / "whu_buildings.geojson").read_text(encoding="utf-8"))
    feature = next(item for item in document["features"] if item["properties"]["id"] == obstacle.id)
    ring = np.array(feature["geometry"]["coordinates"][0])
    footprint = shape({"type": "Polygon", "coordinates": [airspace.frame.project_xy(ring)]})
    assert obstacle.footprint.equals(footprint.buffer(5))


def test_astar_changes_altitude_between_stacked_restrictions(airspace):
    start, end = "VP04", "VP05"
    frame = airspace.frame
    departure = frame.to_local(airspace.vertiports[start].position)
    arrival = frame.to_local(airspace.vertiports[end].position)
    west, _, east, _ = airspace.grid.bounds
    span = arrival[1] - departure[1]
    y1 = departure[1] + span / 3
    y2 = departure[1] + span * 2 / 3
    upper_fence = Obstacle("upper", box(west - 1, y1 - 5, east + 1, y1 + 5), 40.1, 100)
    lower_fence = Obstacle("lower", box(west - 1, y2 - 5, east + 1, y2 + 5), 0, 49.9)
    field = ObstacleField((upper_fence, lower_fence))
    restricted = Airspace(frame, airspace.grid.bounds, field, airspace.vertiports)
    route = RoutePlanner(restricted).plan_3d_route(start, end, "delivery")
    _verify_route(route, restricted, "delivery", end, start)
    cruise = [point.alt for point in route.waypoints if point.phase in {"takeoff", "cruise"} and point.alt]
    assert min(cruise) < 40.1 and max(cruise) > 49.9


def _crossing_hazard(airspace, route):
    cruise_segments = [
        (airspace.frame.to_local(a), airspace.frame.to_local(b))
        for a, b in pairwise(route.waypoints)
        if b.phase == "cruise"
    ]
    a, b = max(cruise_segments, key=lambda segment: dist(*segment))
    current = airspace.frame.to_position(tuple(x + (y - x) * 0.15 for x, y in zip(a, b)))
    center = airspace.frame.to_position(tuple((x + y) / 2 for x, y in zip(a, b)))
    return current, DynamicHazard(
        center_lon=center.lon, center_lat=center.lat, radius_m=35, min_alt=0, max_alt=100
    )


def test_temporary_cylinder_detour_from_airborne_position(airspace, planner):
    start, end = "VP04", "VP05"
    original = planner.plan_3d_route(start, end, "inspection")
    current, hazard = _crossing_hazard(airspace, original)
    field, mask = airspace.with_hazards((hazard,))
    cylinder = field.obstacles[-1]
    assert np.count_nonzero(mask) > np.count_nonzero(airspace.static_voxels)
    positions = [airspace.frame.to_local(point) for point in original.waypoints]
    assert any(_footprint_intersection(a, b, cylinder) for a, b in pairwise(positions))

    route = planner.plan_3d_route(current, end, "inspection", (hazard,))
    _verify_route(route, airspace, "inspection", end, current, field)
    center = airspace.frame.to_local(Position(lon=hazard.center_lon, lat=hazard.center_lat, alt=0))
    for a, b in pairwise(route.waypoints):
        p, q = (airspace.frame.to_local(point) for point in (a, b))
        trace = Point(p[:2]) if p[:2] == q[:2] else LineString([p[:2], q[:2]])
        assert trace.distance(Point(center[:2])) > hazard.radius_m
    assert planner.plan_3d_route(start, end, "inspection") == original


def test_rejects_origin_inside_hazard_and_below_cruise_floor(airspace, planner):
    pad = airspace.vertiports["VP04"].position
    origin = Position(lon=pad.lon, lat=pad.lat, alt=70)
    hazard = DynamicHazard(center_lon=pad.lon, center_lat=pad.lat, radius_m=20, min_alt=0, max_alt=100)
    with pytest.raises(NoRouteError, match="No safe connection"):
        planner.plan_3d_route(origin, "VP05", "inspection", (hazard,))
    with pytest.raises(NoRouteError, match="at or above 30"):
        planner.plan_3d_route(pad, "VP05", "inspection")
    with pytest.raises(NoRouteError, match="inside the study area"):
        planner.plan_3d_route(Position(lon=-65, lat=-30, alt=70), "VP05", "inspection")


def test_plan_latency_for_all_campus_pairs_and_dynamic_detour(airspace, planner):
    base = planner.plan_3d_route("VP04", "VP05", "inspection")
    current, hazard = _crossing_hazard(airspace, base)
    cases = [
        (start, end, mission, ())
        for start, end in permutations(airspace.vertiports, 2)
        for mission in MISSION_LAYERS
    ]
    cases.extend([(current, "VP05", "inspection", (hazard,))] * 5)
    timings = []
    for start, end, mission, hazards in cases:
        begin = perf_counter()
        result = planner.plan_3d_route(start, end, mission, hazards)
        elapsed_ms = (perf_counter() - begin) * 1000
        timings.append(elapsed_ms)
        assert elapsed_ms < 500, (start, end, mission, elapsed_ms)
        assert result.distance_m > 0
        field = airspace.with_hazards(hazards)[0] if hazards else airspace.field
        _verify_route(result, airspace, mission, end, start, field)
    print(f"\nRoute planning ({len(timings)} runs): mean {mean(timings):.2f} ms, max {max(timings):.2f} ms")


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(LLMSettings(), directory=MOCK_DATA)) as api:
        yield api


def _verify_api_route(result, airspace, start, target, field=None):
    points = [airspace.frame.to_local(point) for point in result.waypoints]
    length = sum(dist(a, b) for a, b in pairwise(points))
    _verify_route(
        RoutePlan(waypoints=result.waypoints, distance_m=length), airspace,
        result.corridor, target, start, field,
    )
    assert result.kpis.distance_m == pytest.approx(length, abs=0.0051)
    assert result.kpis.estimated_seconds > 0
    assert result.kpis.min_clearance_m is not None
    assert result.kpis.compliant and result.kpis.compliance_percent == 100
    assert [event.step for event in result.thought_steps] == list(range(1, len(result.thought_steps) + 1))
    assert {event.role for event in result.thought_steps} == {"thought", "action", "observation"}


def test_service_metadata_geojson_and_preview_assets(client):
    assert client.get("/api/health").json() == {
        "service": "VoxAero", "status": "ok", "llm_mode": "fallback_mock"
    }
    assert client.get("/openapi.json").json()["info"] == {"title": "VoxAero API", "version": "1.0.0"}
    response = client.get("/api/layers/all")
    assert response.status_code == 200
    data = response.json()
    assert data["bounds"] == list(STUDY_BOUNDS)
    for key, count in [("buildings", 30), ("vertiports", 6), ("nofly_zones", 3)]:
        assert data[key]["type"] == "FeatureCollection"
        assert len(data[key]["features"]) == count
    assert [(layer["min_alt"], layer["max_alt"]) for layer in data["airspace_layers"]] == [
        (0, 30), (30, 60), (60, 90)
    ]
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "VoxAero — 低空三维数字孪生与智能体调度平台" in page.text
    for path in ("/js/app.js", "/js/map3d.js", "/css/style.css"):
        assert client.get(path).status_code == 200
    model = client.get("/assets/voxaero-01.glb")
    assert model.status_code == 200 and model.content.startswith(b"glTF\x02\x00\x00\x00")


@pytest.mark.parametrize(("query", "target", "mission", "corridor", "zones", "priority"), [
    (DELIVERY_QUERY, "VP03", "delivery", "delivery", ("NFZ01",), "normal"),
    (INSPECTION_QUERY, "VP04", "inspection", "inspection", (), "normal"),
    ("从 VP01 加急配送到图书馆西侧巡检点，优先高空", "VP04", "delivery", "inspection", (), "urgent"),
    ("配送至 VP03，由 VP01 起飞", "VP03", "delivery", "delivery", (), "normal"),
])
def test_dispatch_fallback_contract(airspace, client, query, target, mission, corridor, zones, priority):
    response = client.post("/api/agent/dispatch", json={"query": query})
    assert response.status_code == 200, response.text
    result = DispatchResult.model_validate(response.json())
    assert result.mode == "fallback_mock" and result.corridor == corridor
    assert result.intent.start_id == "VP01" and result.intent.end_id == target
    assert result.intent.mission_type == mission
    assert result.intent.avoid_zone_ids == zones and result.intent.priority == priority
    assert result.kpis.time_saving_percent > 0
    _verify_api_route(result, airspace, "VP01", target, airspace.with_hazards((), zones)[0])


def test_explicit_avoidance_applies_over_a_low_altitude_restriction(airspace):
    original = next(zone for zone in airspace.field.obstacles if zone.id == "NFZ03")
    point = (*original.footprint.centroid.coords[0], 80)
    assert airspace.field.segment_clear(point, point)
    field, mask = airspace.with_hazards((), ("NFZ03",))
    assert not field.segment_clear(point, point)
    assert np.count_nonzero(mask) > np.count_nonzero(airspace.static_voxels)


def test_dispatch_input_errors(client):
    assert client.post("/api/agent/dispatch", json={"query": "   "}).status_code == 422
    result = client.post("/api/agent/dispatch", json={"query": "帮忙配送"})
    assert result.status_code == 422
    assert result.json()["thought_steps"][-1]["role"] == "observation"
    assert "起降坪" in result.json()["detail"]
    unknown_zone = client.post("/api/agent/dispatch", json={"query": "从 VP01 配送至 VP03，避开 NFZ010 禁飞区"})
    assert unknown_zone.status_code == 422
    assert "禁飞区" in unknown_zone.json()["detail"]


@pytest.mark.parametrize("emergency", ["wind_gust", "temp_nofly"])
def test_airborne_replan_contract_and_temporary_geometry(airspace, planner, client, emergency):
    original = planner.plan_3d_route("VP04", "VP05", "inspection")
    current, hazard = _crossing_hazard(airspace, original)
    response = client.post("/api/agent/replan", json={
        "current_pos": current.model_dump(), "target_id": "VP05", "emergency_type": emergency,
        "dynamic_hazard": hazard.model_dump(),
    })
    assert response.status_code == 200, response.text
    result = ReplanResult.model_validate(response.json())
    assert result.emergency_type == emergency and result.target_id == result.original_target_id == "VP05"
    feature = result.temporary_nofly_zone
    assert feature.properties.min_alt == hazard.min_alt and feature.properties.max_alt == hazard.max_alt
    assert feature.geometry.coordinates[0][0] == feature.geometry.coordinates[0][-1]
    polygon = shape(feature.geometry.model_dump())
    assert polygon.is_valid and polygon.covers(Point(hazard.center_lon, hazard.center_lat))
    _verify_api_route(result, airspace, current, "VP05", airspace.with_hazards((hazard,))[0])


def test_replan_rejects_invalid_or_unflyable_emergency(airspace, client):
    start = airspace.vertiports["VP04"].position.model_copy(update={"alt": 75})
    params = {"current_pos": start.model_dump(), "target_id": "VP05", "emergency_type": "temp_nofly"}
    assert client.post("/api/agent/replan", json=params).status_code == 422
    params["dynamic_hazard"] = {
        "center_lon": start.lon, "center_lat": start.lat, "radius_m": 35, "min_alt": 0, "max_alt": 100
    }
    response = client.post("/api/agent/replan", json=params)
    assert response.status_code == 409
    assert response.json()["thought_steps"][-1]["role"] == "observation"


def test_low_battery_uses_shortest_reachable_emergency_route(airspace, planner, client):
    origin = airspace.vertiports["VP04"].position.model_copy(update={"alt": 65})
    response = client.post("/api/agent/replan", json={
        "current_pos": origin.model_dump(), "target_id": "VP03", "emergency_type": "low_battery",
    })
    assert response.status_code == 200, response.text
    result = ReplanResult.model_validate(response.json())
    assert result.original_target_id == "VP03" and result.temporary_nofly_zone is None
    candidates = [
        (planner.plan_3d_route(origin, pad_id, layer).distance_m, pad_id, layer)
        for pad_id in ("VP05", "VP06") for layer in MISSION_LAYERS
    ]
    distance, pad, layer = min(candidates)
    assert (result.target_id, result.corridor) == (pad, layer)
    assert result.kpis.distance_m == pytest.approx(distance, abs=0.0051)
    _verify_api_route(result, airspace, origin, pad)


def test_low_battery_can_use_regular_pad_when_emergency_pads_are_closed(airspace, client):
    origin = airspace.vertiports["VP04"].position.model_copy(update={"alt": 65})
    hazards = [
        DynamicHazard(
            center_lon=pad.position.lon, center_lat=pad.position.lat, radius_m=24, min_alt=0, max_alt=100
        )
        for pad in airspace.vertiports.values() if pad.properties.type == "emergency"
    ]
    response = client.post("/api/agent/replan", json={
        "current_pos": origin.model_dump(), "target_id": "VP03", "emergency_type": "low_battery",
        "active_hazards": [hazard.model_dump() for hazard in hazards],
    })
    assert response.status_code == 200, response.text
    result = ReplanResult.model_validate(response.json())
    assert airspace.vertiports[result.target_id].properties.type != "emergency"
    _verify_api_route(result, airspace, origin, result.target_id, airspace.with_hazards(tuple(hazards))[0])


@pytest.mark.parametrize("scenario", ["function_call", "timeout", "invalid_tool", "invalid_payload"])
def test_openai_compatible_intent_and_fallback(scenario):
    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["tools"][0]["function"]["name"] == "parse_dispatch_intent"
        assert body["tool_choice"]["function"]["name"] == "parse_dispatch_intent"
        if scenario == "timeout":
            raise httpx.ReadTimeout("mock provider timeout", request=request)
        if scenario == "invalid_payload":
            return httpx.Response(200, text="<html>Provider gateway</html>")
        arguments = {
            "start_id": "VP01", "end_id": "VP03" if scenario == "function_call" else "unknown",
            "mission_type": "delivery",
        }
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_test", "type": "function",
                    "function": {"name": "parse_dispatch_intent", "arguments": json.dumps(arguments)},
                }],
            }}],
        })

    settings = LLMSettings(api_key="test-key", base_url="https://test.invalid/v1", model="test-model")
    transport = httpx.MockTransport(respond)
    with OpenAI(api_key=settings.api_key, base_url=settings.base_url, max_retries=0,
                http_client=httpx.Client(transport=transport)) as sdk:
        with TestClient(create_app(settings, llm_client=sdk, directory=MOCK_DATA)) as api:
            assert api.get("/api/health").json()["llm_mode"] == "live_llm"
            response = api.post("/api/agent/dispatch", json={"query": DELIVERY_QUERY})
    assert response.status_code == 200, response.text
    result = DispatchResult.model_validate(response.json())
    assert result.mode == ("live_llm" if scenario == "function_call" else "fallback_mock")
    assert result.model_called and result.model_fallback == (scenario != "function_call")
    assert result.intent.avoid_zone_ids == ("NFZ01",) and result.intent.end_id == "VP03"
    if scenario != "function_call":
        assert any("切换至本地规则" in event.content for event in result.thought_steps)


def test_dispatch_classification_and_grounded_one_turn_tool_workflow():
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload["messages"][-1]["content"])
        # The provider guesses a plausible origin that the operator never supplied.
        arguments = {"start_id": "VP01", "end_id": "VP03", "mission_type": "delivery"}
        return httpx.Response(200, json={
            "id": "mock-tool-call", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_test", "type": "function",
                    "function": {"name": "parse_dispatch_intent", "arguments": json.dumps(arguments)},
                }],
            }}],
        })

    settings = LLMSettings(api_key="test-key", base_url="https://test.invalid/v1", model="test-model")
    with OpenAI(api_key=settings.api_key, base_url=settings.base_url, max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
        with TestClient(create_app(settings, llm_client=sdk, directory=MOCK_DATA)) as api:
            for query, status in (("你好！", "help"), ("讲个关于无人机配送的笑话", "out_of_scope"),
                                  ("图书馆西侧巡检点", "needs_intent"), ("什么是无人机巡检？", "out_of_scope")):
                body = api.post("/api/agent/dispatch", json={"query": query}).json()
                assert body["status"] == status and not body["model_called"]
                assert "waypoints" not in body and not calls

            short = api.post("/api/agent/dispatch", json={"query": "食堂 → 测国重"})
            assert short.status_code == 200 and short.json()["status"] == "planned"
            query = "把午餐送到测国重，避开图书馆禁飞区，120 秒内送达"
            response = api.post("/api/agent/dispatch", json={"query": query})
            notice = response.json()
            assert response.status_code == 422 and notice["status"] == "needs_clarification"
            assert "起点" in notice["detail"] and "waypoints" not in notice
            assert notice["mode"] == "live_llm" and notice["model_called"]
            token = notice["clarification_token"]

            chat = api.post("/api/agent/dispatch", json={"query": "讲个笑话", "clarification_token": token})
            assert chat.json()["status"] == "out_of_scope" and len(calls) == 2
            reply = {"query": "食堂", "clarification_token": token}
            response = api.post("/api/agent/dispatch", json=reply)
            assert response.status_code == 200, response.text
            result = DispatchResult.model_validate(response.json())
            assert result.mode == "live_llm" and not result.model_called and len(calls) == 2
            assert result.intent.start_id == "VP01" and result.intent.end_id == "VP03"
            assert result.intent.avoid_zone_ids == ("NFZ01",)
            assert result.intent.constraints.deadline_seconds == 120
            assert api.post("/api/agent/dispatch", json=reply).json()["status"] == "clarification_closed"
