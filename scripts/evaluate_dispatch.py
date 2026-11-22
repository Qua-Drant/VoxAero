"""Compare the configured live model and local rules on labeled campus requests."""

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from time import perf_counter

from fastapi.testclient import TestClient

from backend.config import LLMSettings
from backend.main import create_app


@dataclass(frozen=True)
class Case:
    name: str
    query: str
    status: str
    expected: dict = field(default_factory=dict)
    reply: str | None = None


def route(name, query, start, end, corridor="delivery", *, reply=None, **expected):
    return Case(name, query, "planned", {
        "start": start, "end": end, "corridor": corridor, "compliant": True,
        "mission_type": corridor, "avoid": [], "cruise": None, "deadline": None,
        "preferred_cruise": None, "preferred_deadline": None, **expected,
    }, reply)


CASES = (
    route("配送基线", "从食堂把午餐送到测国重楼顶，避开图书馆禁飞区", "VP01", "VP03", avoid=["NFZ01"]),
    route("巡检基线", "从信息学部起飞，沿 60–90m 巡检层前往图书馆西侧巡检点", "VP01", "VP04", "inspection", cruise=[60, 90, False, False]),
    route("起降坪编号", "从 VP02 配送到 VP01", "VP02", "VP01"),
    route("倒装一", "到测国重楼顶配送午餐，从食堂出发", "VP01", "VP03"),
    route("倒装二", "目的地是菜鸟驿站，起点是食堂，安排一次配送", "VP01", "VP02"),
    route("倒装三", "终点设在图书馆西侧，星湖备降坪是起点，做一次巡检", "VP05", "VP04", "inspection"),
    route("口语配送", "食堂起飞，麻烦把打包的饭带去测国重楼顶", "VP01", "VP03"),
    route("口语安防", "从图书馆西侧飞往星湖备降坪，上空帮我巡视一圈", "VP04", "VP05", "inspection"),
    route("口语查看", "从菜鸟驿站飞往测国重楼顶，帮我去看一下楼顶的情况", "VP02", "VP03", "inspection"),
    route("口语派单", "派单给无人机，菜鸟出发，包裹要给食堂", "VP02", "VP01"),
    route("全角编号", "从ＶＰ０１配送到ＶＰ０２", "VP01", "VP02"),
    route("指定高航层", "从 VP02 配送到 VP03，优先高空", "VP02", "VP03", "inspection", mission_type="delivery"),
    route("补起点保留限高避让", "把午餐配送到测国重楼顶，巡航不超过 50 米，避开图书馆静谧区",
          "VP01", "VP03", reply="食堂", cruise=[None, 50, False, False], avoid=["NFZ01"]),
    route("补终点保留时限", "从食堂起飞配送，必须在 120 秒内送达", "VP01", "VP02", reply="菜鸟", deadline=120),
    Case("起终点都缺失", "请安排一个配送任务", "needs_clarification"),
    Case("未知终点", "从食堂配送到青松楼楼顶", "needs_clarification"),
    Case("地点缺少任务", "菜鸟驿站", "needs_intent", {"model_called": False}),
    route("中文限高数字", "从食堂配送到测国重楼顶，巡航高度不要超过五十米", "VP01", "VP03", cruise=[None, 50, False, False]),
    route("严格上限", "从食堂配送到测国重楼顶，巡航高度低于五十米", "VP01", "VP03", cruise=[None, 50, False, True]),
    route("软时限", "从食堂配送到测国重楼顶，尽量 10 秒内送达", "VP01", "VP03", preferred_deadline=10),
    route("软限高", "从食堂配送到测国重楼顶，尽量巡航低于 20 米", "VP01", "VP03", preferred_cruise=[None, 20, False, True]),
    Case("不可用高度层", "从食堂配送到测国重楼顶，巡航必须低于二十米", "constraint_conflict"),
    Case("不可达时限", "从食堂配送到测国重楼顶，必须在10秒内送达", "constraint_conflict"),
    Case("矛盾高度", "从 VP01 巡检到 VP04，但不要飞高空", "constraint_conflict"),
    Case("未支持多点", "从食堂配送到菜鸟驿站，经由测国重楼顶", "constraint_conflict", {"model_called": False}),
    Case("同一地点", "从 VP01 配送到 VP01", "constraint_conflict"),
    Case("天气", "查一下天气", "out_of_scope", {"model_called": False}),
    Case("翻译", "翻译‘无人机配送从食堂到菜鸟驿站’", "out_of_scope", {"model_called": False}),
    Case("闲聊", "讲个关于无人机巡检的笑话", "out_of_scope", {"model_called": False}),
    Case("帮助", "怎么用？", "help", {"model_called": False}),
)


def summarize(body):
    intent = body.get("intent", {})
    constraints = intent.get("constraints", {})

    def bounds(key):
        value = constraints.get(key)
        return [value[name] for name in ("min_alt", "max_alt", "min_exclusive", "max_exclusive")] if value else None

    steps = body.get("thought_steps", [])
    attempted = body.get("model_called", False) or any(
        step["content"] == "parse_dispatch_intent · OpenAI 兼容函数调用" for step in steps
    )
    return {
        "status": body.get("status"), "model_called": attempted, "mode": body.get("mode"),
        "model_fallback": body.get("model_fallback", False) or (attempted and body.get("mode") == "fallback_mock"),
        "start": intent.get("start_id"), "end": intent.get("end_id"),
        "mission_type": intent.get("mission_type"), "corridor": body.get("corridor"), "avoid": intent.get("avoid_zone_ids"),
        "cruise": bounds("cruise"), "deadline": constraints.get("deadline_seconds"),
        "preferred_cruise": bounds("preferred_cruise"), "preferred_deadline": constraints.get("preferred_deadline_seconds"),
        "compliant": body.get("kpis", {}).get("compliant"), "detail": body.get("detail"),
    }


def evaluate(settings):
    cases = []
    with TestClient(create_app(settings)) as client:
        version = client.app.state.agent.airspace.scene_version
        for case in CASES:
            expected = {"status": case.status, **case.expected}
            started = perf_counter()
            response = client.post("/api/agent/dispatch", json={"query": case.query, "scene_version": version})
            body = response.json()
            turns = [summarize(body)]
            if case.reply and body.get("status") == "needs_clarification" and body.get("clarification_token"):
                response = client.post("/api/agent/dispatch", json={
                    "query": case.reply, "scene_version": version, "clarification_token": body["clarification_token"],
                })
                turns.append(summarize(response.json()))
            actual = turns[-1]
            errors = [key for key, value in expected.items() if actual.get(key) != value]
            if case.reply and len(turns) != 2:
                errors.append("expected_clarification")
            cases.append({
                "name": case.name, "query": case.query, "reply": case.reply, "expected": expected,
                "turns": turns, "http": response.status_code, "errors": errors,
                "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            })
            print(f"{settings.mode} {'PASS' if not errors else 'FAIL'} {case.name} {','.join(errors)}", flush=True)
    called = [case for case in cases if any(turn["model_called"] for turn in case["turns"])]
    return {
        "scene_version": version, "model": settings.model or None, "cases": cases,
        "passed": sum(not case["errors"] for case in cases), "with_model_call": len(called),
        "with_fallback": sum(any(turn["model_fallback"] for turn in case["turns"]) for case in cases),
        "mean_model_case_ms": round(mean(case["elapsed_ms"] for case in called), 2) if called else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Path for the JSON report")
    args = parser.parse_args()
    settings = LLMSettings.from_environment()
    if settings.mode != "live_llm":
        parser.error("Configure the live model via the project's LLM environment settings first")

    local = evaluate(LLMSettings())
    live = evaluate(settings)
    comparison = {"live_only": [], "local_only": [], "both_pass": [], "both_fail": []}
    for a, b in zip(local["cases"], live["cases"], strict=True):
        pair = (not a["errors"], not b["errors"])
        key = {(False, True): "live_only", (True, False): "local_only",
               (True, True): "both_pass", (False, False): "both_fail"}[pair]
        comparison[key].append(a["name"])
    args.output.write_text(json.dumps({"local": local, "live": live, "comparison": comparison},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Local {local['passed']}/{len(CASES)}, {settings.model} {live['passed']}/{len(CASES)}, "
          f"model cases {live['with_model_call']}, fallback {live['with_fallback']}, "
          f"mean model case {live['mean_model_case_ms']} ms")


if __name__ == "__main__":
    main()
