"""Exercise real scene selection, task isolation and source-aware airspace checks."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient

from backend.config import DATA_DIR, LLMSettings
from backend.core.airspace import Airspace
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


def confirm(client, proposed, option=None):
    return client.post("/api/agent/confirm-candidate", json={
        "candidate_token": proposed["candidate_token"],
        "option_id": option or proposed["options"][0]["id"],
        "scene_version": proposed["scene_version"],
    }).json()


def assert_clear(scene, route, hazards=(), avoid=()):
    field, _ = scene.with_hazards(hazards, tuple(avoid))
    coordinates = [scene.frame.to_local(Position.model_validate(point)) for point in route["waypoints"]]
    assert all(field.segment_clear(first, last) for first, last in pairwise(coordinates))


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
