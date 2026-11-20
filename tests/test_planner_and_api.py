"""Phase 1 acceptance tests for the spatial data and planner."""

import json
from itertools import combinations, pairwise, permutations
from math import dist, hypot
from statistics import mean
from time import perf_counter

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import substring

from backend.config import DATA_DIR, MISSION_LAYERS, ORIGIN_LON_LAT, STUDY_BOUNDS
from backend.core.airspace import Airspace, LocalENU, Obstacle, ObstacleField
from backend.core.planner import NoRouteError, RoutePlanner
from backend.schemas import BuildingProperties, DynamicHazard, NoFlyProperties, Position, VertiportProperties


@pytest.fixture(scope="module")
def airspace():
    return Airspace.from_directory()


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
        document = json.loads((DATA_DIR / f"{name}.geojson").read_text(encoding="utf-8"))
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
    document = json.loads((DATA_DIR / "whu_buildings.geojson").read_text(encoding="utf-8"))
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
