"""Dispatch tools, OpenAI-compatible intent parsing and local emergency decisions."""

import re
import unicodedata
from itertools import pairwise
from math import hypot, isfinite

from openai import OpenAI, OpenAIError
from shapely.geometry.polygon import orient

from backend.config import LLMSettings, MISSION_LAYERS
from backend.core.airspace import Airspace, Obstacle
from backend.core.planner import NoRouteError, RoutePlanner
from backend.schemas import (
    AssessedRoute, DispatchIntent, DispatchResult, DynamicHazard, HazardFeature, HazardPolygon, LLMMode,
    MissionType, NoFlyProperties, Position, ReplanRequest, ReplanResult, RouteKPIs, RoutePlan,
    ThoughtStep,
)

PAD_ALIASES = {
    "VP01": ("信息学部食堂", "信息学部学生食堂", "食堂"),
    "VP02": ("菜鸟驿站", "菜鸟"),
    "VP03": ("测国重楼顶", "测国重", "测绘国重", "测绘国重实验室"),
    "VP04": ("图书馆西侧", "图书馆巡检点"),
    "VP05": ("星湖备降坪", "星湖"),
    "VP06": ("操场东侧", "操场备降坪"),
}
AVOID_PATTERN = re.compile(
    r"(?:避开|绕开|绕过|\bavoid\b)\s*([^，。；,;]+?)(?=[，。；,;]|从|由|\bfrom\b|$)", re.IGNORECASE
)
SOURCE_PREFIX = re.compile(r"(?:从|由|\bfrom)\s*$", re.IGNORECASE)
TARGET_PREFIX = re.compile(r"(?:到|至|前往|飞往|抵达|送往|→|\bto)\s*$", re.IGNORECASE)
INSPECTION_PATTERN = re.compile(r"巡检|巡逻|安防|\b(?:inspection|inspect|patrol)\b", re.IGNORECASE)
HIGH_PATTERN = re.compile(r"优先高空|高空|60\s*[-–—~至]\s*90|high.altitude", re.IGNORECASE)
URGENT_PATTERN = re.compile(r"加急|紧急|\burgent\b", re.IGNORECASE)
GENERIC_AVOIDANCE = re.compile(r"(?:所有|全部|已知|静态|周边|附近)*(?:建筑物?|障碍物?|禁飞区|no-fly zones?)")
TOOL_NAME = "parse_dispatch_intent"
FLIGHT_SPEED_MPS = {"delivery": 12.0, "inspection": 15.0}
VERTICAL_SPEED_MPS = 3.0
GROUND_SPEED_MPS = 1.35
GROUND_DETOUR_FACTOR = 1.3


class AgentError(ValueError):
    def __init__(self, message: str, steps: list[ThoughtStep], status: int) -> None:
        super().__init__(message)
        self.steps = steps
        self.status = status


def _add(steps: list[ThoughtStep], role, content: str) -> None:
    steps.append(ThoughtStep(step=len(steps) + 1, role=role, content=content))


def _error(steps: list[ThoughtStep], message: str, status: int) -> AgentError:
    _add(steps, "observation", message)
    return AgentError(message, steps, status)


def _aliases(names: dict[str, str], extras: dict[str, tuple[str, ...]]) -> dict[str, str]:
    return {
        alias.casefold(): identifier
        for identifier, name in names.items()
        for alias in (identifier, name, *extras.get(identifier, ()))
    }


class DispatchAgent:
    def __init__(self, airspace: Airspace, settings: LLMSettings, client: OpenAI | None = None) -> None:
        self.airspace = airspace
        self.planner = RoutePlanner(airspace)
        self.settings = settings
        self.client = client
        self.pad_names = {key: pad.properties.name for key, pad in airspace.vertiports.items()}
        self.zone_names = {
            item["properties"]["id"]: item["properties"]["name"]
            for item in airspace.geojson.get("nofly_zones", {}).get("features", [])
        }
        self.pad_aliases = _aliases(self.pad_names, PAD_ALIASES)
        self.zone_aliases = _aliases(self.zone_names, {"NFZ01": ("图书馆", "静谧区")})
        alternatives = [
            re.escape(alias) if not alias.isascii() else rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
            for alias in sorted(self.pad_aliases, key=len, reverse=True)
        ]
        self.pad_pattern = re.compile("|".join(alternatives) or r"(?!)", re.IGNORECASE)

    def parse_dispatch_intent(self, user_query: str) -> DispatchIntent:
        """Deterministic intent tool with ordered, role-marked pad names or IDs."""
        query = unicodedata.normalize("NFKC", user_query)
        avoid_zone_ids = self._avoidance(query)
        locations = AVOID_PATTERN.sub("", query)
        if "VP01" in self.pad_names:
            locations = re.sub(r"从信息学部(?=\s*(?:起飞|出发))", "从VP01", locations)
        mentions = [
            (self.pad_aliases[match[0].casefold()], locations[:match.start()])
            for match in self.pad_pattern.finditer(locations)
        ]
        distinct = list(dict.fromkeys(identifier for identifier, _ in mentions))
        if len(distinct) != 2:
            raise ValueError("请给出两个不同的起降坪名称或编号，例如从 VP01 配送至 VP03。")
        start = next((identifier for identifier, prefix in mentions if SOURCE_PREFIX.search(prefix)), None)
        end = next((identifier for identifier, prefix in mentions if TARGET_PREFIX.search(prefix)), None)
        if start is None and end is not None:
            start = next(identifier for identifier in distinct if identifier != end)
        if end is None and start is not None:
            end = next(identifier for identifier in distinct if identifier != start)
        task_text = self.pad_pattern.sub("", locations)
        return DispatchIntent(
            start_id=start or distinct[0],
            end_id=end or distinct[1],
            mission_type="inspection" if INSPECTION_PATTERN.search(task_text) else "delivery",
            avoid_zone_ids=avoid_zone_ids,
            priority="urgent" if URGENT_PATTERN.search(query) else "normal",
            prefer_high_altitude=bool(HIGH_PATTERN.search(query)),
        )

    def _avoidance(self, query: str) -> tuple[str, ...]:
        ids: list[str] = []
        for match in AVOID_PATTERN.finditer(query):
            text = match[1].casefold().strip()
            found = [
                zone_id for alias, zone_id in self.zone_aliases.items()
                if (re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text)
                    if alias.isascii() else alias in text)
            ]
            if not found and not GENERIC_AVOIDANCE.fullmatch(text):
                raise ValueError("请使用地图中已有的禁飞区名称或编号指定绕飞区域。")
            ids.extend(found)
        return tuple(dict.fromkeys(ids))

    def _model_intent(self, query: str) -> DispatchIntent:
        schema = DispatchIntent.model_json_schema()
        for key in ("start_id", "end_id"):
            schema["properties"][key]["enum"] = list(self.pad_names)
        if self.zone_names:
            schema["properties"]["avoid_zone_ids"]["items"]["enum"] = list(self.zone_names)
        function = {
            "name": TOOL_NAME,
            "description": "Extract departure, destination, mission and explicit flight preferences.",
            "parameters": schema,
        }
        prompt = (
            "Call the intent tool once using valid IDs. Interpret 信息学部起飞 as VP01 when available. "
            f"Vertiports: {self.pad_names}. No-fly zones: {self.zone_names}. "
            "Avoidance applies only to explicitly requested zones. Preserve the requested mission type."
        )
        options = dict(
            model=self.settings.model,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": query}],
            tools=[{"type": "function", "function": function}],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        )
        if self.client is None:
            with OpenAI(
                api_key=self.settings.api_key, base_url=self.settings.base_url, timeout=6.0, max_retries=0
            ) as client:
                completion = client.chat.completions.create(**options)
        else:
            completion = self.client.chat.completions.create(**options)
        choices = getattr(completion, "choices", None)
        calls = getattr(choices[0].message, "tool_calls", None) if choices else None
        if not calls or len(calls) != 1 or calls[0].type != "function":
            raise ValueError("Expected a dispatch intent tool call")
        function_call = calls[0].function
        if function_call is None or function_call.name != TOOL_NAME:
            raise ValueError("Unexpected dispatch intent tool name")
        intent = DispatchIntent.model_validate_json(function_call.arguments)
        self._check_intent(intent)
        zones = tuple(dict.fromkeys((*intent.avoid_zone_ids, *self._avoidance(query))))
        return intent.model_copy(update={"avoid_zone_ids": zones})

    def _check_intent(self, intent: DispatchIntent) -> None:
        if intent.start_id not in self.pad_names or intent.end_id not in self.pad_names:
            raise ValueError("起点或终点不在起降坪清单内。")
        if intent.start_id == intent.end_id:
            raise ValueError("起点和终点必须是不同的起降坪。")
        if any(zone_id not in self.zone_names for zone_id in intent.avoid_zone_ids):
            raise ValueError("指定的禁飞区编号不在空域清单内。")

    def plan_3d_route(
        self,
        start: str | Position,
        end_id: str,
        mission_type: MissionType,
        extra_nofly_zones: tuple[DynamicHazard, ...] = (),
        *,
        avoid_zone_ids: tuple[str, ...] = (),
    ) -> AssessedRoute:
        route = self.planner.plan_3d_route(
            start, end_id, mission_type, extra_nofly_zones, avoid_zone_ids=avoid_zone_ids
        )
        kpis = self._metrics(route, mission_type, start, end_id, extra_nofly_zones, avoid_zone_ids)
        return AssessedRoute(waypoints=route.waypoints, distance_m=route.distance_m, kpis=kpis)

    def dispatch(self, query: str) -> DispatchResult:
        steps: list[ThoughtStep] = []
        _add(steps, "thought", "提取任务类型、起降坪和空域偏好。")
        mode: LLMMode = self.settings.mode
        intent = None
        if mode == "live_llm":
            _add(steps, "action", "parse_dispatch_intent · OpenAI 兼容函数调用")
            try:
                intent = self._model_intent(query)
            except (OpenAIError, ValueError):
                mode = "fallback_mock"
                _add(steps, "observation", "模型调用失败或参数不合规，切换至本地规则。")
        if intent is None:
            _add(steps, "action", "parse_dispatch_intent · 本地关键词与起降坪匹配")
            try:
                intent = self.parse_dispatch_intent(query)
                self._check_intent(intent)
            except ValueError as exc:
                raise _error(steps, str(exc), 422) from exc

        start_name = self.pad_names[intent.start_id]
        end_name = self.pad_names[intent.end_id]
        _add(steps, "observation", f"{intent.start_id} {start_name} → {intent.end_id} {end_name}")
        corridor = "inspection" if intent.prefer_high_altitude else intent.mission_type
        layer = MISSION_LAYERS[corridor]
        _add(steps, "action", f"校验空域 · {layer.min_alt:g}–{layer.max_alt:g} m · {layer.name}")
        zones = "、".join(self.zone_names[zone_id] for zone_id in intent.avoid_zone_ids)
        rule = f"全高度绕开{zones}，并应用其余空域限制。" if zones else "应用建筑缓冲区和全部静态空域限制。"
        _add(steps, "observation", rule)
        _add(steps, "action", "plan_3d_route · 搜索三维体素航线并复核航段")
        try:
            route = self.plan_3d_route(
                intent.start_id, intent.end_id, corridor, avoid_zone_ids=intent.avoid_zone_ids
            )
        except NoRouteError as exc:
            raise _error(steps, "当前空域与高度层下没有合规航线。", 409) from exc
        _add(steps, "observation", f"规划成功，{len(route.waypoints)} 个航点，{route.distance_m:.0f} m。")
        return DispatchResult(
            intent=intent, mode=mode, corridor=corridor, start_name=start_name, end_name=end_name,
            waypoints=route.waypoints, kpis=route.kpis, thought_steps=steps,
        )

    def replan_emergency(self, request: ReplanRequest) -> ReplanResult:
        steps: list[ThoughtStep] = []
        _add(steps, "thought", "保持飞行当前位置，优先选择合规的应急航线。")
        _add(steps, "action", f"replan_emergency · {request.emergency_type}")
        if request.target_id not in self.pad_names:
            raise _error(steps, "原目的地不在起降坪清单内。", 422)
        if any(zone_id not in self.zone_names for zone_id in request.avoid_zone_ids):
            raise _error(steps, "指定的禁飞区编号不在空域清单内。", 422)

        corridor = request.mission_type or ("inspection" if request.current_pos.alt >= 60 else "delivery")
        hazards = request.active_hazards
        feature = None
        if request.dynamic_hazard is not None:
            hazards += (request.dynamic_hazard,)
            obstacle = Obstacle.from_hazard(request.dynamic_hazard, self.airspace.frame, len(hazards))
            ring = [self.airspace.frame.to_position((x, y, 0)) for x, y in orient(obstacle.footprint).exterior.coords]
            feature = HazardFeature(
                properties=NoFlyProperties(
                    id=obstacle.id, name="临时禁飞区", reason=request.emergency_type,
                    min_alt=obstacle.min_alt, max_alt=obstacle.max_alt,
                ),
                geometry=HazardPolygon(coordinates=[[(point.lon, point.lat) for point in ring]]),
            )
            _add(steps, "observation", f"已应用半径 {request.dynamic_hazard.radius_m:g} m 的临时禁飞区。")

        target = request.target_id
        try:
            if request.emergency_type == "low_battery":
                _add(steps, "action", "plan_3d_route · 比较可达应急坪的合规航线")
                route, target, corridor = self._divert(request, corridor, hazards)
                _add(steps, "observation", f"选定最短可达备降航线 · {target} {self.pad_names[target]}。")
            else:
                _add(steps, "action", f"plan_3d_route · 从当前坐标绕飞至 {target}")
                route = self.plan_3d_route(
                    request.current_pos, target, corridor, hazards, avoid_zone_ids=request.avoid_zone_ids
                )
        except NoRouteError as exc:
            raise _error(steps, "当前位置和空域限制下没有合规应急航线。", 409) from exc

        _add(steps, "observation", f"新航线已通过空域校验，剩余航程 {route.distance_m:.0f} m。")
        return ReplanResult(
            emergency_type=request.emergency_type, original_target_id=request.target_id, target_id=target,
            corridor=corridor, waypoints=route.waypoints, kpis=route.kpis, thought_steps=steps,
            temporary_nofly_zone=feature,
        )

    def _divert(
        self, request: ReplanRequest, corridor: MissionType, hazards: tuple[DynamicHazard, ...]
    ) -> tuple[AssessedRoute, str, MissionType]:
        emergency, other = [], []
        for pad_id, pad in self.airspace.vertiports.items():
            (emergency if pad.properties.type == "emergency" else other).append(pad_id)
        corridors = (corridor, "inspection" if corridor == "delivery" else "delivery")
        for pads in (emergency, other):
            candidates = []
            for pad_id in pads:
                for candidate_layer in corridors:
                    try:
                        route = self.plan_3d_route(
                            request.current_pos, pad_id, candidate_layer, hazards,
                            avoid_zone_ids=request.avoid_zone_ids,
                        )
                    except NoRouteError:
                        continue
                    candidates.append((route, pad_id, candidate_layer))
            if candidates:
                return min(candidates, key=lambda candidate: candidate[0].distance_m)
        raise NoRouteError("No reachable diversion pad")

    def _metrics(
        self,
        route: RoutePlan,
        corridor: MissionType,
        start: str | Position,
        end_id: str,
        hazards: tuple[DynamicHazard, ...],
        avoid_zone_ids: tuple[str, ...],
    ) -> RouteKPIs:
        start_roof = self.airspace.vertiports[start].roof_obstacle if isinstance(start, str) else None
        end_roof = self.airspace.vertiports[end_id].roof_obstacle
        positions = [self.airspace.frame.to_local(point) for point in route.waypoints]
        field, _ = self.airspace.with_hazards(hazards, avoid_zone_ids)
        elapsed = 0.0
        clearance = float("inf")
        for (a, b), arrival in zip(pairwise(positions), route.waypoints[1:], strict=True):
            elapsed += max(hypot(b[0] - a[0], b[1] - a[1]) / FLIGHT_SPEED_MPS[corridor],
                           abs(b[2] - a[2]) / VERTICAL_SPEED_MPS)
            roof = start_roof if arrival.phase == "takeoff" else end_roof if arrival.phase == "landing" else None
            clearance = min(clearance, field.segment_clearance(a, b, roof_access=roof))
        first, last = positions[0], positions[-1]
        ground_route = hypot(last[0] - first[0], last[1] - first[1]) * GROUND_DETOUR_FACTOR
        ground_time = ground_route / GROUND_SPEED_MPS
        return RouteKPIs(
            distance_m=round(route.distance_m, 2),
            estimated_seconds=round(elapsed, 2),
            ground_estimated_seconds=round(ground_time, 2),
            time_saving_percent=round((1 - elapsed / ground_time) * 100, 1) if ground_time else None,
            min_clearance_m=int(clearance * 100) / 100 if isfinite(clearance) else None,
            compliant=True,
            compliance_percent=100,
        )
