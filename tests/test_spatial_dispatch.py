"""Exercise real scene selection, task isolation and source-aware airspace checks."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient

from backend.config import DATA_DIR, LLMSettings
from backend.core import session as session_module
from backend.core.airspace import Airspace
from backend.core.planner import NoRouteError
from backend.core.session import SessionStore
from backend.main import create_app
from backend.schemas import DynamicHazard, Position


SURVEY = "从信息学部附近找一个可起飞的地面点，去图书馆西侧巡检，绕开静谧区。"
TWO_OPEN_SITES = "从信息学部附近找一个可起飞的地面点，去星湖备降坪附近找空地巡检。"


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(LLMSettings())) as api:
        yield api


def suggest(client, query=SURVEY, spatial=None):
    response = client.post("/api/agent/dispatch", json={"query": query, "spatial": spatial})
    assert response.status_code == 200, response.text
    return response.json()


def answer(client, previous, query):
    response = client.post("/api/agent/dispatch", json={
        "query": query, "clarification_token": previous["clarification_token"],
        "scene_version": previous["scene_version"],
    })
    assert response.status_code in {200, 409}, response.text
    return response.json()


@pytest.mark.parametrize("gate", ["珞瑜门", "珞喻门"])
def test_spatial_dialog_disambiguates_gate_and_keeps_youth_building(client, gate):
    first = suggest(client, f"从{gate}到青年楼")
    assert first["status"] == "needs_spatial_location", first
    question = first["clarification"]
    assert question["slot"] == "start"
    assert question["slots"][-1]["resolved_name"] == "青年楼"
    assert [option["label"] for option in question["options"]] == ["珞喻门 · 西侧", "珞喻门 · 东侧"]
    assert question["options"][0]["point"]["lon"] < question["options"][1]["point"]["lon"]
    chosen = answer(client, first, "西边那个")
    assert chosen["status"] == "choose_endpoints", chosen
    route = chosen["options"][0]
    assert route["start"]["reference_name"] == "珞喻门 · 西侧"
    assert route["end"]["reference_name"] == "青年楼"
    assert_clear(client.app.state.agent.airspace, confirm(client, chosen))


@pytest.mark.parametrize("side", ["西", "东"])
def test_dispatch_finds_reachable_landing_near_youth_dormitory(client, side):
    pending = suggest(client, "从珞瑜门到青年宿舍楼")
    offered = answer(client, pending, f"{side}边那个")
    assert offered["status"] == "choose_endpoints", offered
    option = offered["options"][0]
    assert option["start"]["reference_name"] == f"珞喻门 · {side}侧"
    assert option["end"]["reference_name"] == "青年宿舍楼"
    assert 0 < option["end"]["offset_m"] <= 60
    assert option["corridor"] == "delivery"
    assert option["waypoints"][-1]["phase"] == "landing"
    assert all(30 <= point["alt"] <= 60 for point in option["waypoints"] if point["phase"] == "cruise")
    planned = confirm(client, offered)
    assert planned["status"] == "planned"
    assert_clear(client.app.state.agent.airspace, planned)


def test_default_layer_can_recover_but_explicit_layer_stays_locked(client, monkeypatch):
    agent = client.app.state.agent
    plan = agent.plan_3d_route

    def fail_default(start, end, mission_type, *args, **kwargs):
        if mission_type == "delivery":
            raise NoRouteError("Test disconnected delivery layer")
        return plan(start, end, mission_type, *args, **kwargs)

    monkeypatch.setattr(agent, "plan_3d_route", fail_default)
    first = suggest(client, "从珞瑜门到青年宿舍楼")
    offered = answer(client, first, "西边那个")
    assert offered["status"] == "choose_endpoints", offered
    assert offered["options"][0]["corridor"] == "inspection"
    assert "改用 60–90 米航层" in offered["options"][0]["planning_note"]
    planned = confirm(client, offered)
    assert planned["corridor"] == "inspection"
    assert agent.spatial.active_task(planned["mission_token"]).corridor == "inspection"
    assert_clear(agent.airspace, planned)

    first = suggest(client, "从珞瑜门到青年宿舍楼，巡航高度30到60米")
    blocked = answer(client, first, "西边那个")
    assert blocked["status"] == "constraint_conflict"
    assert blocked["clarification"]["expand_search"]


def test_wider_landing_search_requires_confirmation_and_retains_task(client, monkeypatch):
    spatial = client.app.state.agent.spatial
    search = spatial.sites

    def require_wider_radius(reference, role, field, rejected, connects, radius, dense):
        if role == "终点" and radius <= 96:
            return []
        return search(reference, role, field, rejected, connects, radius, dense)

    monkeypatch.setattr(spatial, "sites", require_wider_radius)
    first = suggest(client, "从珞瑜门到青年宿舍楼，巡航高度30到60米，绕开静谧区")
    blocked = answer(client, first, "西边那个")
    assert blocked["status"] == "constraint_conflict"
    assert blocked["clarification"]["expand_search"]
    offered = answer(client, blocked, "扩大搜索范围")
    assert offered["status"] == "choose_endpoints", offered
    assert offered["options"][0]["end"]["reference_name"] == "青年宿舍楼"
    planned = confirm(client, offered)
    assert planned["intent"]["avoid_zone_ids"] == ["NFZ01"]
    cruise = planned["intent"]["constraints"]["cruise"]
    assert (cruise["min_alt"], cruise["max_alt"]) == (30, 60)
    assert_clear(client.app.state.agent.airspace, planned, avoid=("NFZ01",))


def test_spatial_dialog_recovers_typo_and_orders_multiple_flyovers(client):
    first = suggest(client, "从VP07到VP06巡检，依次途经星湖、旧钟楼、青午楼，巡航高度60到80米，绕开静谧区")
    assert first["clarification"]["slot"] == "via-2"
    assert "旧钟楼" in first["detail"]
    assert first["clarification"]["slots"][1]["resolved_name"] == "星湖"
    second = answer(client, first, "第二个途经点改为星湖南路")
    assert second["clarification"]["slot"] == "via-3"
    option = next(item for item in second["clarification"]["options"] if item["label"] == "青年楼")
    blocked = answer(client, second, option["reply"])
    assert blocked["status"] == "constraint_conflict", blocked
    assert blocked["clarification"]["slots"][3]["resolved_name"] == "青年楼"
    chosen = answer(client, blocked, "第三个途经点改为信息学部操场")
    assert chosen["status"] == "choose_endpoints", chosen
    planned = confirm(client, chosen)
    assert [point["name"] for point in planned["route_visits"]] == ["星湖", "星湖南路", "信息学部操场"]
    assert planned["intent"]["avoid_zone_ids"] == ["NFZ01"]
    cruise = planned["intent"]["constraints"]["cruise"]
    assert (cruise["min_alt"], cruise["max_alt"]) == (60.0, 80.0)
    assert_clear(client.app.state.agent.airspace, planned, avoid=["NFZ01"])


@pytest.mark.parametrize("query", [
    "从VP07先到星湖，再去星湖南路，最后到VP06巡检",
    "VP07 → 星湖 → 星湖南路 → VP06 巡检",
])
def test_ordered_spatial_itinerary_uses_final_destination(client, query):
    offered = suggest(client, query)
    assert offered["status"] == "choose_endpoints", offered
    assert offered["options"][0]["end"]["id"] == "VP06"
    assert [point["name"] for point in offered["options"][0]["route_visits"]] == ["星湖", "星湖南路"]


def test_spatial_dialog_suggests_grounded_categories_and_accepts_anchor(client):
    first = suggest(client, "从校门到旧广播站巡检")
    assert first["clarification"]["slot"] == "start"
    assert first["clarification"]["options"]
    second = answer(client, first, first["clarification"]["options"][0]["reply"])
    assert second["clarification"]["slot"] == "end"
    assert "旧广播站" in second["detail"]
    assert second["clarification"]["slots"][0]["resolved_name"]
    chosen = answer(client, second, "就在星空广场旁边")
    assert chosen["status"] == "choose_endpoints", chosen
    assert chosen["options"][0]["end"]["reference_name"] == "星空广场"


def test_spatial_dialog_grounds_nearby_landmark_and_preserves_previous_turn(client):
    first = suggest(client, "从珞喻门到青年楼")
    unresolved = answer(client, first, "都可以")
    assert unresolved["clarification"]["options"] == first["clarification"]["options"]
    chosen = answer(client, unresolved, "靠近友谊广场的那个")
    assert chosen["status"] == "choose_endpoints", chosen
    assert chosen["options"][0]["start"]["reference_name"] == "珞喻门 · 东侧"


def test_spatial_dialog_accepts_picked_coordinate_without_losing_destination(client):
    baseline = suggest(client)
    point = baseline["options"][0]["start"]["position"]
    pending = suggest(client, "从旧广播站到图书馆西侧巡检，绕开静谧区")
    out_of_bounds = answer(client, pending, "[114.5, 30.5]")
    assert "场景范围外" in out_of_bounds["detail"]
    assert out_of_bounds["clarification"]["slots"][-1]["resolved_name"]
    chosen = answer(client, out_of_bounds, f"[{point['lon']}, {point['lat']}]")
    assert chosen["status"] == "choose_endpoints", chosen
    route = chosen["options"][0]
    assert route["start"]["position"]["lon"] == point["lon"]
    assert_clear(client.app.state.agent.airspace, confirm(client, chosen), avoid=["NFZ01"])


def confirm(client, proposed, option=None):
    return client.post("/api/agent/confirm-candidate", json={
        "candidate_token": proposed["candidate_token"],
        "option_id": option or proposed["options"][0]["id"],
        "scene_version": proposed["scene_version"],
    }).json()


def assert_clear(scene, route, hazards=(), avoid=()):
    field, _ = scene.with_hazards(hazards, tuple(avoid))
    waypoints = route["waypoints"]
    coordinates = [scene.frame.to_local(Position.model_validate(point)) for point in waypoints]
    start_id = (route.get("intent") or {}).get("start_id") or (route.get("start") or {}).get("id")
    end_id = (route.get("intent") or {}).get("end_id") or route.get("target_id") or (route.get("end") or {}).get("id")
    start_roof = scene.vertiports[start_id].roof_obstacle if start_id in scene.vertiports else None
    end_roof = scene.vertiports[end_id].roof_obstacle if end_id in scene.vertiports else None
    for index, ((first, last), end_wp) in enumerate(zip(pairwise(coordinates), waypoints[1:], strict=True)):
        roof = start_roof if index == 0 and end_wp["phase"] == "takeoff" else (
            end_roof if index == len(waypoints) - 2 and end_wp["phase"] == "landing" else None
        )
        assert field.segment_clear(first, last, roof_access=roof)


def test_ground_options_require_selection_and_preserve_scene_catalog(client):
    baseline = client.get("/api/layers/all").json()
    proposed = suggest(client)
    assert proposed["status"] == "choose_endpoints", proposed
    assert 2 <= len(proposed["options"]) <= 3
    assert len({p["start"]["id"] for p in proposed["options"]}) == len(proposed["options"])
    assert all(p["start"]["offset_m"] >= 8 and p["start"]["id"].startswith("TMP-") for p in proposed["options"])
    assert all(p["airspace_check"]["status"] == "pending" for p in proposed["options"])
    assert confirm(client, proposed, "invalid-choice")["status"] == "constraint_conflict"
    chosen = proposed["options"][1]
    planned = confirm(client, proposed, chosen["id"])
    assert planned["status"] == "planned", planned
    assert planned["intent"]["start_id"] == chosen["start"]["id"]
    assert planned["intent"]["avoid_zone_ids"] == ["NFZ01"]
    assert planned["task_sites"] == [chosen["start"]]
    assert client.get("/api/layers/all").json()["vertiports"] == baseline["vertiports"]
    assert_clear(client.app.state.agent.airspace, planned, avoid=["NFZ01"])


def test_replan_uses_task_sites_and_task_avoidance(client):
    spatial = {"avoid": [{"lon": 114.3552, "lat": 30.5302, "radius_m": 16}]}
    proposed = suggest(client, TWO_OPEN_SITES, spatial)
    assert proposed["status"] == "choose_endpoints", proposed
    planned = confirm(client, proposed)
    assert len(planned["task_sites"]) == 2
    nofly = DynamicHazard(center_lon=114.3552, center_lat=30.5302, radius_m=16, min_alt=0, max_alt=100)
    scene = client.app.state.agent.airspace
    assert_clear(scene, planned, (nofly,))
    assert planned["task_zones"][0]["properties"]["reason"] == "task_avoidance"
    position = next(point for point in planned["waypoints"] if point["alt"] >= 60)
    event = {"center_lon": 114.3615, "center_lat": 30.5245, "radius_m": 14, "min_alt": 0, "max_alt": 100}
    payload = {
        "current_pos": {key: position[key] for key in ("lon", "lat", "alt")},
        "target_id": planned["intent"]["end_id"], "scene_version": planned["scene_version"],
        "mission_type": planned["corridor"], "emergency_type": "temp_nofly", "dynamic_hazard": event,
    }
    assert client.post("/api/agent/replan", json=payload).status_code == 422
    payload["mission_token"] = planned["mission_token"]
    replanned = client.post("/api/agent/replan", json=payload)
    assert replanned.status_code == 200, replanned.text
    assert replanned.json()["target_id"] == planned["intent"]["end_id"]
    assert_clear(scene, replanned.json(), (nofly, DynamicHazard.model_validate(event)))
    payload.update(emergency_type="low_battery", dynamic_hazard=None)
    diverted = client.post("/api/agent/replan", json=payload)
    assert diverted.status_code == 200, diverted.text
    assert diverted.json()["target_id"] != planned["intent"]["end_id"]
    assert_clear(scene, diverted.json(), (nofly,))


def test_failed_detour_persists_fence_and_diversion_respects_battery(client):
    offered = suggest(client, "从珞瑜门西侧到青年宿舍楼")
    planned = confirm(client, offered)
    departure, cruise = next((start, end) for start, end in pairwise(planned["waypoints"])
                             if end["phase"] == "cruise")
    current = {axis: departure[axis] + .25 * (cruise[axis] - departure[axis]) for axis in ("lon", "lat", "alt")}
    landing = planned["waypoints"][-1]
    fence = DynamicHazard(center_lon=landing["lon"], center_lat=landing["lat"],
                          radius_m=20, min_alt=0, max_alt=100)
    payload = {
        "current_pos": current, "target_id": planned["intent"]["end_id"],
        "mission_token": planned["mission_token"], "scene_version": planned["scene_version"],
        "emergency_type": "temp_nofly", "dynamic_hazard": fence.model_dump(),
    }
    blocked = client.post("/api/agent/replan", json=payload)
    assert blocked.status_code == 409
    assert blocked.json()["temporary_nofly_zone"]["geometry"]["type"] == "Polygon"
    task = client.app.state.agent.spatial.active_task(planned["mission_token"])
    assert task.active_hazards == (fence,)
    payload.update(emergency_type="low_battery", dynamic_hazard=None, battery_percent=1)
    low = client.post("/api/agent/replan", json=payload)
    assert low.status_code == 409 and "电量" in low.json()["detail"]
    payload["battery_percent"] = 14
    response = client.post("/api/agent/replan", json=payload)
    assert response.status_code == 200, response.text
    diversion = response.json()
    assert diversion["waypoints"][0]["lon"] == current["lon"]
    assert diversion["target_id"] != planned["intent"]["end_id"]
    assert diversion["arrival_battery_percent"] >= 2
    assert diversion["arrival_battery_percent"] == pytest.approx(
        14 - diversion["kpis"]["estimated_battery_percent"], abs=.0051)
    assert_clear(client.app.state.agent.airspace, diversion, (fence,))

    restart = client.post("/api/agent/restart-mission", json={
        "mission_token": planned["mission_token"], "scene_version": planned["scene_version"],
    })
    assert restart.status_code == 200, restart.text
    restarted = client.app.state.agent.spatial.active_task(restart.json()["mission_token"])
    assert restarted.intent == task.intent and restarted.active_hazards == ()
    assert client.app.state.agent.spatial.active_task(planned["mission_token"]).active_hazards == (fence,)


@pytest.mark.parametrize(("query", "origin", "destination"), [
    ("星湖南路到星空广场巡检", "星湖南路", "星空广场"),
    ("从珞喻二门到友谊广场巡检", "珞瑜二门", "友谊广场"),
])
def test_osm_references_support_nonbuilding_endpoints(client, query, origin, destination):
    offered = suggest(client, query)
    assert offered["status"] == "choose_endpoints", offered
    chosen = offered["options"][0]
    assert chosen["start"]["reference_name"] == origin
    assert chosen["end"]["reference_name"] == destination
    planned = confirm(client, offered)
    assert planned["status"] == "planned", planned
    assert_clear(client.app.state.agent.airspace, planned)


def test_ordered_osm_flyovers_and_checked_alternatives_survive_replan(client):
    scene = client.app.state.agent.airspace
    query = "从星空广场起飞，依次途经星湖、星湖南路，最后到信息学部操场巡检，比较多条航线，2 秒内完成。"
    offered = suggest(client, query)
    assert offered["choice_type"] == "routes" and len(offered["options"]) >= 2, offered
    assert len({tuple((p["lon"], p["lat"], p["alt"]) for p in choice["waypoints"])
                for choice in offered["options"]}) == len(offered["options"])
    for choice in offered["options"]:
        visits = choice["route_visits"]
        assert [visit["name"] for visit in visits] == ["星湖", "星湖南路"]
        assert visits[0]["osm_ref"] == "way/295111844"
        first, second = (visit["waypoint_index"] for visit in visits)
        assert 0 < first < second < len(choice["waypoints"]) - 1
        for visit in visits:
            point = choice["waypoints"][visit["waypoint_index"]]
            assert point["phase"] == "cruise" and 60 <= point["alt"] <= 90
            assert (point["lon"], point["lat"]) == (visit["lon"], visit["lat"])
        assert choice["kpis"]["estimated_seconds"] > 2
        assert_clear(scene, choice)

    choice = offered["options"][-1]
    planned = confirm(client, offered, choice["id"])
    assert planned["status"] == "planned" and planned["waypoints"] == choice["waypoints"]
    assert [ref["name"] for ref in planned["intent"]["via"]] == ["星湖", "星湖南路"]
    current = planned["waypoints"][planned["route_visits"][0]["waypoint_index"]]
    event = DynamicHazard(center_lon=114.3615, center_lat=30.5245, radius_m=14, min_alt=0, max_alt=100)
    payload = {
        "current_pos": current, "target_id": planned["intent"]["end_id"],
        "scene_version": planned["scene_version"], "mission_type": planned["corridor"],
        "emergency_type": "temp_nofly", "dynamic_hazard": event.model_dump(),
        "mission_token": planned["mission_token"], "completed_visits": 1,
    }
    redirected = client.post("/api/agent/replan", json=payload)
    assert redirected.status_code == 200, redirected.text
    assert [site["name"] for site in redirected.json()["route_visits"]] == ["星湖南路"]
    assert_clear(scene, redirected.json(), (event,))
    payload.update(emergency_type="low_battery", dynamic_hazard=None)
    diverted = client.post("/api/agent/replan", json=payload)
    assert diverted.status_code == 200 and diverted.json()["route_visits"] == []
    assert_clear(scene, diverted.json())


def test_unmatched_flyover_is_not_dropped(client):
    result = suggest(client, "从 VP07 经由不存在的地标到 VP09 巡检")
    assert result["status"] == "needs_spatial_location" and "途经点" in result["detail"]


def test_explicit_point_and_unknown_locations(client):
    result = suggest(client, "从不存在的地标附近找空地去图书馆西侧巡检")
    assert result["status"] == "needs_spatial_location", result
    baseline = suggest(client)
    coordinate = baseline["options"][0]["start"]["position"]
    query = f"从 [{coordinate['lon']}, {coordinate['lat']}] 去图书馆西侧巡检，绕开静谧区。"
    result = suggest(client, query)
    assert result["status"] == "choose_endpoints", result
    assert result["options"][0]["start"]["position"]["lon"] == coordinate["lon"]
    blocked = client.post("/api/agent/dispatch", json={
        "query": query, "spatial": {"start": coordinate, "avoid": [{**coordinate, "radius_m": 15}]},
    })
    assert blocked.status_code == 409
    assert blocked.json()["status"] == "constraint_conflict"
    assert "净空" in blocked.json()["detail"]


def test_spatial_model_can_ground_freeform_roles_without_guessing(client, monkeypatch):
    agent = client.app.state.agent
    monkeypatch.setattr(agent, "settings", LLMSettings(api_key="test", model="spatial-test"))

    def recognized(query, function, prompt):
        assert function["name"] == "extract_spatial_task"
        return json.dumps({"origin_quote": "信息学部附近找空地", "destination_quote": "图书馆西侧",
                           "mission_type": "inspection"})

    monkeypatch.setattr(agent, "request_model", recognized)
    result = suggest(client, "以图书馆西侧作为巡检终点，信息学部附近找空地供无人机起飞，绕开静谧区。")
    assert result["status"] == "choose_endpoints" and result["mode"] == "live_llm"
    monkeypatch.setattr(agent, "request_model", lambda *_: json.dumps({
        "origin_quote": "信息学部附近", "destination_quote": "静谧区", "mission_type": "inspection",
    }))
    ungrounded = suggest(client, "从信息学部附近找一个地面点起飞巡检，绕开静谧区。")
    assert ungrounded["status"] == "needs_spatial_location" and "终点" in ungrounded["detail"]


def test_new_rooftop_is_never_silently_replaced_by_ground(client):
    result = suggest(client, "从信息学部附近找空地去图书馆屋顶找空地巡检")
    assert result["status"] == "needs_spatial_location" and "屋顶" in result["detail"]


def test_airspace_query_distinguishes_scenario_and_task_rules(client):
    report = suggest(client, "核验图书馆静谧区是否禁飞")
    assert report["status"] == "airspace_report"
    assert "场景围栏" in report["detail"]
    assert report["airspace_check"]["status"] == "pending"
    report = suggest(client, "核验这里的空域", {
        "start": {"lon": 114.3552, "lat": 30.5302},
        "avoid": [{"lon": 114.3552, "lat": 30.5302, "radius_m": 16}],
    })
    assert "任务避让" in report["detail"]


def test_external_snapshots_normalize_heights_and_check_validity(tmp_path):
    for filename in ("whu_buildings.geojson", "vertiports.geojson", "nofly_zones.geojson", "whu_terrain.json"):
        shutil.copy(DATA_DIR / filename, tmp_path / filename)
    base = Airspace.from_directory(tmp_path)
    source = {
        "name": "测试空域快照", "url": "https://example.org/rules", "version": "fixture-1",
        "horizontal_datum": "EPSG:4326", "vertical_datum": "EGM96",
        "coverage_bbox": [114.349, 30.524, 114.362, 30.533],
        "valid_from": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    ring = [(114.355, 30.527), (114.356, 30.527), (114.356, 30.528), (114.355, 30.527)]
    reference = base.geojson["terrain"]["reference_elevation_m"]
    raw = {"type": "FeatureCollection", "rule_source": source, "features": [{
        "type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"id": "rule", "name": "导入规则", "min_alt": reference + 42,
                       "max_alt": reference + 88, "reason": "单元测试"},
    }]}
    path = tmp_path / "airspace_rules.geojson"
    path.write_text(json.dumps(raw), encoding="utf-8")
    scene = Airspace.from_directory(tmp_path)
    assert scene.scene_version != base.scene_version
    check = scene.check_airspace([Position(lon=114.3555, lat=30.5272, alt=65)])
    assert check.status == "source_checked" and check.source_version == "fixture-1"
    assert scene.check_airspace([Position(lon=114.37, lat=30.5272, alt=65)]).status == "pending"
    assert scene.check_airspace([Position(lon=114.3555, lat=30.5272, alt=65)], 7200).status == "pending"
    imported = next(item for item in scene.field.obstacles if item.id == "EXT-rule")
    assert (imported.min_alt, imported.max_alt) == (42, 88)
    assert not scene.field.segment_clear(scene.frame.to_local(Position(lon=114.3557, lat=30.5272, alt=58)),
                                         scene.frame.to_local(Position(lon=114.3557, lat=30.5272, alt=61)))
    source["vertical_datum"] = "unknown"
    path.write_text(json.dumps(raw), encoding="utf-8")
    invalid = Airspace.from_directory(tmp_path)
    assert invalid.rule_issue and not invalid.rule_source
    assert all(item.id != "EXT-rule" for item in invalid.field.obstacles)


def test_pad_clarification_bridges_to_spatial_landmark_and_retains_constraints(client):
    initial = client.post("/api/agent/dispatch", json={"query": "把午餐送到测国重楼顶，避开图书馆禁飞区"})
    assert initial.status_code == 422, initial.text
    notice = initial.json()
    assert notice["status"] == "needs_clarification"
    assert not notice["clarification_token"].startswith("geo-")
    assert notice["clarification"]["slot"] == "start"
    assert notice["clarification"]["slots"][1]["resolved_name"] == "测国重楼顶停机坪"

    bridged = answer(client, notice, "珞瑜门")
    assert bridged["status"] == "needs_spatial_location", bridged
    assert bridged["clarification_token"].startswith("geo-")
    assert bridged["clarification"]["slots"][1]["resolved_name"] == "测国重楼顶停机坪"
    assert [option["label"] for option in bridged["clarification"]["options"]] == ["珞喻门 · 西侧", "珞喻门 · 东侧"]
    assert answer(client, notice, "珞瑜门")["status"] == "clarification_closed"

    offered = answer(client, bridged, "西边那个")
    assert offered["status"] == "choose_endpoints", offered
    assert offered["options"][0]["start"]["reference_name"] == "珞喻门 · 西侧"
    assert offered["options"][0]["end"]["id"] == "VP03"
    planned = confirm(client, offered)
    assert planned["status"] == "planned"
    assert planned["intent"]["end_id"] == "VP03"
    assert planned["intent"]["avoid_zone_ids"] == ["NFZ01"]
    assert_clear(client.app.state.agent.airspace, planned, avoid=("NFZ01",))


def test_both_missing_pad_clarification_accepts_landmark_pair(client):
    initial = client.post("/api/agent/dispatch", json={"query": "安排一次巡检，避开图书馆禁飞区"})
    assert initial.status_code == 422, initial.text
    notice = initial.json()
    assert notice["status"] == "needs_clarification"

    offered = answer(client, notice, "从星空广场到信息学部操场")
    assert offered["status"] == "choose_endpoints", offered
    planned = confirm(client, offered)
    assert planned["status"] == "planned"
    assert planned["intent"]["mission_type"] == "inspection"
    assert planned["intent"]["avoid_zone_ids"] == ["NFZ01"]
    assert_clear(client.app.state.agent.airspace, planned, avoid=("NFZ01",))


def test_session_store_prefix_expiry_and_lru_capacity(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(session_module, "monotonic", lambda: clock[0])
    store = SessionStore(ttl_seconds=30.0, capacity=2, prefix="geo-")
    t1 = store.issue({"n": 1})
    t2 = store.issue({"n": 2})
    assert t1.startswith("geo-") and store.get(t1[4:]) is None
    assert store.update(t1, lambda value: {"n": value["n"] + 10}) == {"n": 11}
    t3 = store.issue({"n": 3})
    assert store.get(t1) is None
    assert store.get(t2) == {"n": 2} and store.get(t3) == {"n": 3}
    clock[0] = 131.0
    assert store.get(t2) is None
    assert store.update(t3, lambda value: value) is None


def test_multi_route_comparison_populates_objective_highlights(client):
    offered = suggest(client, "从星空广场起飞，依次途经星湖、星湖南路，最后到信息学部操场巡检，比较多条航线。")
    assert offered["choice_type"] == "routes" and len(offered["options"]) >= 2
    all_highlights = {tag for option in offered["options"] for tag in option["highlights"]}
    assert "最短耗时" in all_highlights
    for option in offered["options"]:
        confirmed = confirm(client, offered, option["id"])
        assert confirmed["status"] == "planned"
        assert confirmed["waypoints"] == option["waypoints"]


