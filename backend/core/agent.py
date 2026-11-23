"""Dispatch tools, OpenAI-compatible intent parsing and local emergency decisions."""

import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from itertools import pairwise
from math import hypot, isfinite
from secrets import token_urlsafe
from threading import Lock
from time import monotonic

from openai import OpenAI, OpenAIError
from shapely.geometry.polygon import orient

from backend.config import LLMSettings, MISSION_LAYERS
from backend.core.airspace import Airspace, Obstacle, Vertiport
from backend.core.intent import (
    ALT_CONTEXT, CHAT, CLARIFY_INTENT_MESSAGE, DISPATCH_WORDS, HELP, HELP_MESSAGE, LOW_FLIGHT, MEASUREMENT,
    ROUTE_WORDS, SCOPE_MESSAGE, TIME_REQUEST, UNSUPPORTED_STOP, intersect_limits, parse_constraints,
)
from backend.core.planner import NoRouteError, RoutePlanner
from backend.core.spatial_dispatch import SpatialDispatch, route_visits
from backend.schemas import (
    AssessedRoute, CruiseLimits, DispatchCandidate, DispatchConstraints, DispatchIntent, DispatchNotice,
    DispatchNoticeType, DispatchResult, DynamicHazard, HazardFeature, HazardPolygon, LLMMode, MissionType,
    NoFlyProperties, Position, ReplanRequest, ReplanResult, RouteKPIs, RoutePlan, RouteReference, SpatialChoices, SpatialOptions, ThoughtStep,
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
SOURCE_PREFIX = re.compile(r"(?:从|由|\bfrom|(?:起点|出发点)(?:设在|在|是|为|[:：]))\s*$", re.IGNORECASE)
TARGET_PREFIX = re.compile(
    r"(?:到|至|前往|飞往|抵达|送往|去|→|->|\bto|(?:终点|目的地)(?:设在|在|是|为|[:：]))\s*$", re.IGNORECASE,
)
SOURCE_SUFFIX = re.compile(r"\s*(?:起飞|出发|(?:作为|设为|是|为)\s*(?:起点|出发点))")
TARGET_SUFFIX = re.compile(r"\s*(?:作为|设为|是|为)\s*(?:终点|目的地)")
INSPECTION_PATTERN = re.compile(r"巡检|巡逻|安防|\b(?:inspection|inspect|patrol)\b", re.IGNORECASE)
DELIVERY_PATTERN = re.compile(r"配送|送(?:到|往|至|餐|件|货)|投送|\bdeliver\w*\b", re.IGNORECASE)
HIGH_PATTERN = re.compile(r"优先高空|高空|60\s*[-–—~至]\s*90|high.altitude", re.IGNORECASE)
URGENT_PATTERN = re.compile(r"加急|紧急|\burgent\b", re.IGNORECASE)
GENERIC_AVOIDANCE = re.compile(r"(?:所有|全部|已知|静态|周边|附近)*(?:建筑物?|障碍物?|禁飞区|no-fly zones?)")
TOOL_NAME = "parse_dispatch_intent"
FLIGHT_SPEED_MPS = {"delivery": 12.0, "inspection": 15.0}
VERTICAL_SPEED_MPS = 3.0
GROUND_SPEED_MPS = 1.35
GROUND_DETOUR_FACTOR = 1.3
PENDING_TTL_SECONDS = 600
PENDING_LIMIT = 128


class ModelDispatchIntent(DispatchCandidate):
    cruise_requirement: str | None = None


@dataclass(frozen=True)
class PendingDispatch:
    candidate: DispatchCandidate
    constraints: DispatchConstraints
    mode: LLMMode
    model_fallback: bool
    expires_at: float


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
        aliases = PAD_ALIASES | {"VP05": ("星湖备降坪",)} if "places" in airspace.geojson else PAD_ALIASES
        self.pad_aliases = _aliases(self.pad_names, aliases)
        self.zone_aliases = _aliases(self.zone_names, {"NFZ01": ("图书馆", "静谧区")})
        alternatives = [
            re.escape(alias) if not alias.isascii() else rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
            for alias in sorted(self.pad_aliases, key=len, reverse=True)
        ]
        self.pad_pattern = re.compile("|".join(alternatives) or r"(?!)", re.IGNORECASE)
        self.pending: OrderedDict[str, PendingDispatch] = OrderedDict()
        self.pending_lock = Lock()
        self.spatial = SpatialDispatch(self)

    def _locations(self, query: str) -> str:
        locations = AVOID_PATTERN.sub("", query)
        if "VP01" in self.pad_names:
            locations = re.sub(r"从信息学部(?=\s*(?:起飞|出发))", "从VP01", locations)
        return locations

    def _pad_mentions(self, text: str) -> list[str]:
        return list(dict.fromkeys(self.pad_aliases[match[0].casefold()] for match in self.pad_pattern.finditer(text)))

    def parse_dispatch_intent(self, query: str) -> DispatchCandidate:
        """Extract locally grounded pad roles and preferences, including partial intent."""
        locations = self._locations(query)
        distinct = self._pad_mentions(locations)
        starts, ends = set(), set()
        for match in self.pad_pattern.finditer(locations):
            pad = self.pad_aliases[match[0].casefold()]
            prefix = locations[:match.start()]
            suffix = locations[match.end():]
            if SOURCE_PREFIX.search(prefix) or SOURCE_SUFFIX.match(suffix):
                starts.add(pad)
            if TARGET_PREFIX.search(prefix) or TARGET_SUFFIX.match(suffix):
                ends.add(pad)
        start = next(iter(starts)) if len(starts) == 1 else None
        end = next(iter(ends)) if len(ends) == 1 else None
        if len(distinct) == 2 and len(starts) <= 1 and len(ends) <= 1:
            start = start or next(pad for pad in distinct if pad != end)
            end = end or next(pad for pad in distinct if pad != start)
        task_text = self.pad_pattern.sub("", locations)
        return DispatchCandidate(
            start_id=start,
            end_id=end,
            mission_type="inspection" if INSPECTION_PATTERN.search(task_text) else "delivery",
            avoid_zone_ids=self._avoidance(LOW_FLIGHT.sub("", query)),
            priority="urgent" if URGENT_PATTERN.search(query) else "normal",
            prefer_high_altitude=bool(HIGH_PATTERN.search(LOW_FLIGHT.sub("", query))),
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

    def _model_intent(self, query: str) -> ModelDispatchIntent:
        schema = ModelDispatchIntent.model_json_schema()
        for key in ("start_id", "end_id"):
            schema["properties"][key]["enum"] = [*self.pad_names, None]
        if self.zone_names:
            schema["properties"]["avoid_zone_ids"]["items"]["enum"] = list(self.zone_names)
        function = {
            "name": TOOL_NAME,
            "description": "Extract departure, destination, mission and explicit flight preferences.",
            "parameters": schema,
        }
        prompt = (
            "Call the intent tool once using valid IDs. Use null for missing or ambiguous endpoints and never guess. "
            "Interpret 信息学部起飞 as VP01 when available. Negative wording such as 不要飞高空 is not a high-altitude preference. "
            "For cruise_requirement quote only an altitude request. Ignore delivery deadlines and time preferences. "
            "Quote the complete relevant phrase verbatim, or use null when absent. Include comparators, units and any preference or obligation. "
            f"Vertiports: {self.pad_names}. No-fly zones: {self.zone_names}. "
            "Avoidance applies only to explicitly requested zones. Preserve the requested mission type."
        )
        intent = ModelDispatchIntent.model_validate_json(self.request_model(query, function, prompt))
        if any(pad is not None and pad not in self.pad_names for pad in (intent.start_id, intent.end_id)):
            raise ValueError("Unknown vertiport from model")
        if any(zone_id not in self.zone_names for zone_id in intent.avoid_zone_ids):
            raise ValueError("Unknown avoid zone from model")
        return intent

    def request_model(self, query: str, function: dict, prompt: str) -> str:
        options = dict(
            model=self.settings.model,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": query}],
            tools=[{"type": "function", "function": function}],
            tool_choice={"type": "function", "function": {"name": function["name"]}},
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
        if function_call is None or function_call.name != function["name"]:
            raise ValueError("Unexpected dispatch intent tool name")
        return function_call.arguments

    def _ground_model(self, query: str, local: DispatchCandidate, model: ModelDispatchIntent) -> DispatchCandidate:
        locations = self._locations(query)
        mentioned = self._pad_mentions(locations)
        task_text = self.pad_pattern.sub("", locations)
        start = local.start_id or (model.start_id if model.start_id in mentioned and model.start_id != local.end_id else None)
        end = local.end_id or (model.end_id if model.end_id in mentioned and model.end_id != start else None)
        zones = tuple(dict.fromkeys((*local.avoid_zone_ids, *model.avoid_zone_ids)))
        return DispatchCandidate(
            start_id=start, end_id=end, avoid_zone_ids=zones,
            mission_type=local.mission_type if INSPECTION_PATTERN.search(task_text) or DELIVERY_PATTERN.search(task_text)
            else model.mission_type,
            priority="urgent" if local.priority == "urgent" else model.priority,
            prefer_high_altitude=local.prefer_high_altitude or (model.prefer_high_altitude and not LOW_FLIGHT.search(query)),
        )

    def _check_intent(self, intent: DispatchIntent) -> None:
        if intent.start_id not in self.pad_names or intent.end_id not in self.pad_names:
            raise ValueError("起点或终点不在起降坪清单内。")
        if intent.start_id == intent.end_id:
            raise ValueError("起点和终点必须是不同的起降坪。")
        if any(zone_id not in self.zone_names for zone_id in intent.avoid_zone_ids):
            raise ValueError("指定的禁飞区编号不在空域清单内。")

    def plan_3d_route(
        self,
        start: str | Vertiport | Position,
        end_id: str | Vertiport,
        mission_type: MissionType,
        extra_nofly_zones: tuple[DynamicHazard, ...] = (),
        *,
        avoid_zone_ids: tuple[str, ...] = (),
        cruise_limits: CruiseLimits | None = None,
        via: tuple[RouteReference, ...] = (),
    ) -> AssessedRoute:
        if via:
            layer = MISSION_LAYERS[mission_type]
            levels = self.planner.cruise_levels(layer, cruise_limits)
            altitudes = [self.airspace.grid.center((0, 0, level))[2] for level in levels]
            for altitude in sorted(altitudes, key=lambda height: abs(height - layer.midpoint)):
                positions = tuple(Position(lon=site.lon, lat=site.lat, alt=altitude) for site in via)
                limits = CruiseLimits(min_alt=altitude, max_alt=altitude)
                try:
                    route = self.planner.plan_3d_route(start, end_id, mission_type, extra_nofly_zones,
                                                       avoid_zone_ids=avoid_zone_ids, cruise_limits=limits, via=positions)
                    break
                except NoRouteError:
                    continue
            else:
                raise NoRouteError("No route visits all requested locations in order")
        else:
            route = self.planner.plan_3d_route(
                start, end_id, mission_type, extra_nofly_zones, avoid_zone_ids=avoid_zone_ids, cruise_limits=cruise_limits,
            )
        kpis, flight_seconds = self._metrics(route, mission_type, start, end_id, extra_nofly_zones, avoid_zone_ids)
        return AssessedRoute(**route.model_dump(), kpis=kpis, flight_seconds=flight_seconds)

    def dispatch(
        self, query: str, clarification_token: str | None = None, spatial: SpatialOptions | None = None
    ) -> DispatchResult | DispatchNotice | SpatialChoices:
        query = unicodedata.normalize("NFKC", query).strip()
        steps: list[ThoughtStep] = []
        _add(steps, "action", "classify_dispatch · 本地识别使用帮助、调度请求及补答")
        if HELP.fullmatch(query):
            return self._notice(steps, "help", HELP_MESSAGE)
        if CHAT.search(query):
            return self._notice(steps, "out_of_scope", SCOPE_MESSAGE)

        if self.spatial.is_airspace_query(query):
            return self.spatial.inspect_airspace(query, spatial, steps)
        if clarification_token and clarification_token.startswith("geo-"):
            if not self.spatial.is_new_route(query):
                return self.spatial.reply(query, clarification_token[4:], steps)

        locations = self._locations(query)
        mentioned = self._pad_mentions(locations)
        task_text = self.pad_pattern.sub("地点", locations)
        use_spatial = self.spatial.applies(query, spatial, mentioned)
        is_request = bool(DISPATCH_WORDS.search(task_text) or (mentioned or use_spatial) and ROUTE_WORDS.search(task_text))
        if clarification_token and self._is_pad_reply(query, mentioned):
            pending = self._pending_for(clarification_token)
            if len(mentioned) == 1 or pending is None or not (pending.candidate.start_id or pending.candidate.end_id):
                return self._complete_clarification(clarification_token, query, mentioned, steps)
        if not is_request:
            if mentioned or query.casefold().rstrip(".!?。！？ ") in self.zone_aliases:
                return self._notice(steps, "needs_intent", CLARIFY_INTENT_MESSAGE)
            return self._notice(steps, "out_of_scope", SCOPE_MESSAGE)

        _add(steps, "thought", "提取有输入依据的起降坪、任务类型与约束。")
        if UNSUPPORTED_STOP.search(locations):
            return self._notice(steps, "constraint_conflict", "途经点采用空中飞越；如需中途落地，请拆成两次任务。")
        if TIME_REQUEST.search(query):
            _add(steps, "observation", "时限不参与规划，预计耗时仅供参考。")
        if use_spatial:
            return self.spatial.propose(query, spatial or SpatialOptions(), steps)
        try:
            local = self.parse_dispatch_intent(query)
        except ValueError as exc:
            raise _error(steps, str(exc), 422) from exc
        try:
            constraints = parse_constraints(query)
        except ValueError as exc:
            return self._notice(steps, "constraint_conflict", str(exc))

        candidate = local
        model_called = self.settings.mode == "live_llm"
        mode: LLMMode = "fallback_mock"
        if model_called:
            _add(steps, "action", "parse_dispatch_intent · OpenAI 兼容函数调用")
            try:
                model = self._model_intent(query)
                candidate = self._ground_model(query, local, model)
                mode = "live_llm"
            except (OpenAIError, ValueError):
                _add(steps, "observation", "模型调用失败或参数不合规，切换至本地规则。")
            else:
                altitude_quote = model.cruise_requirement
                if altitude_quote and altitude_quote in query and (
                    MEASUREMENT.search(altitude_quote)
                    or (ALT_CONTEXT.search(altitude_quote) and not TIME_REQUEST.search(altitude_quote))
                ) and not (
                    constraints.cruise or constraints.preferred_cruise or LOW_FLIGHT.search(query)
                    or HIGH_PATTERN.search(query)
                ):
                    return self._notice(steps, "constraint_conflict", "请明确巡航高度，例如‘巡航不超过 50 米’。",
                                        mode=mode, model_called=True)
        if mode == "fallback_mock":
            _add(steps, "action", "parse_dispatch_intent · 本地关键词与起降坪匹配")
        if LOW_FLIGHT.search(query) and (candidate.mission_type == "inspection" or candidate.prefer_high_altitude):
            return self._notice(steps, "constraint_conflict", "任务要求避开高空，但指定的巡检或高空航道为 60–90 米。请调整任务或高度要求。",
                                mode=mode, model_called=model_called)
        model_fallback = model_called and mode == "fallback_mock"
        if not (candidate.start_id and candidate.end_id):
            token = token_urlsafe(24)
            with self.pending_lock:
                self.pending[token] = PendingDispatch(candidate, constraints, mode, model_fallback,
                                                      monotonic() + PENDING_TTL_SECONDS)
                if len(self.pending) > PENDING_LIMIT:
                    self.pending.popitem(last=False)
            missing = "起点" if candidate.end_id else "终点" if candidate.start_id else "起点和终点"
            return self._notice(steps, "needs_clarification", f"请补充{missing}的起降点编号或场景地名。",
                                mode=mode, model_called=model_called, token=token)
        return self._plan_dispatch(candidate, constraints, mode, steps, model_called, model_fallback)

    def _is_pad_reply(self, query: str, mentioned: list[str]) -> bool:
        if not 1 <= len(mentioned) <= 2:
            return False
        words = self.pad_pattern.sub(" ", query)
        return re.fullmatch(r"(?:\s|[，,。!！]|从|由|到|至|去|选|是|起点|终点|起飞|出发|降落|吧|即可|→|->)+", words) is not None

    def _pending_for(self, token: str, *, consume: bool = False) -> PendingDispatch | None:
        with self.pending_lock:
            pending = self.pending.pop(token, None) if consume else self.pending.get(token)
            if pending is not None and pending.expires_at > monotonic():
                return pending
            self.pending.pop(token, None)
        return None

    def _complete_clarification(
        self, token: str, query: str, mentioned: list[str], steps: list[ThoughtStep],
    ) -> DispatchResult | DispatchNotice:
        pending = self._pending_for(token, consume=True)
        if pending is None:
            return self._notice(steps, "clarification_closed", "这次补答已失效，请重新提交完整调度指令。")
        _add(steps, "action", "clarify_dispatch · 本地补齐起降坪并沿用原任务约束")
        candidate = pending.candidate
        reply = self.parse_dispatch_intent(query)
        if len(mentioned) == 1 and (candidate.start_id or candidate.end_id):
            if candidate.start_id and reply.start_id is None:
                candidate = candidate.model_copy(update={"end_id": mentioned[0]})
            elif candidate.end_id and reply.end_id is None:
                candidate = candidate.model_copy(update={"start_id": mentioned[0]})
        elif len(mentioned) == 2:
            candidate = candidate.model_copy(update={"start_id": reply.start_id, "end_id": reply.end_id})
        if not candidate.start_id or not candidate.end_id:
            return self._notice(steps, "clarification_closed", "补答仍缺少明确起降坪，请重新提交完整调度指令。")
        return self._plan_dispatch(candidate, pending.constraints, pending.mode, steps, False, pending.model_fallback)

    @staticmethod
    def _notice(
        steps: list[ThoughtStep], status: DispatchNoticeType, detail: str, *, mode: LLMMode | None = None,
        model_called: bool = False, token: str | None = None,
    ) -> DispatchNotice:
        _add(steps, "observation", detail)
        return DispatchNotice(status=status, detail=detail, thought_steps=steps, mode=mode,
                              model_called=model_called, clarification_token=token)

    def _plan_dispatch(
        self, candidate: DispatchCandidate, constraints: DispatchConstraints, mode: LLMMode,
        steps: list[ThoughtStep], model_called: bool, model_fallback: bool,
    ) -> DispatchResult | DispatchNotice:
        intent = DispatchIntent(**candidate.model_dump(), constraints=constraints)
        try:
            self._check_intent(intent)
        except ValueError as exc:
            return self._notice(steps, "constraint_conflict", str(exc), mode=mode, model_called=model_called)

        start_name = self.pad_names[intent.start_id]
        end_name = self.pad_names[intent.end_id]
        _add(steps, "observation", f"{intent.start_id} {start_name} → {intent.end_id} {end_name}")
        corridor = "inspection" if intent.prefer_high_altitude else intent.mission_type
        layer = MISSION_LAYERS[corridor]
        limits = constraints.cruise
        _add(steps, "action", f"校验空域 · {layer.min_alt:g}–{layer.max_alt:g} m · {layer.name}")
        if limits and not self.planner.cruise_levels(layer, limits):
            return self._notice(steps, "constraint_conflict",
                                f"当前巡航高度条件与 {layer.min_alt:g}–{layer.max_alt:g} 米航层内的可用高度不相容。",
                                mode=mode, model_called=model_called)
        zones = "、".join(self.zone_names[zone_id] for zone_id in intent.avoid_zone_ids)
        rule = f"全高度绕开{zones}，并应用其余空域限制。" if zones else "应用建筑缓冲区和全部静态空域限制。"
        _add(steps, "observation", rule)
        _add(steps, "action", "plan_3d_route · 搜索三维体素航线并复核航段")
        preferred = intersect_limits(limits, constraints.preferred_cruise)
        try:
            if constraints.preferred_cruise and preferred != limits:
                try:
                    route = self.plan_3d_route(intent.start_id, intent.end_id, corridor,
                                               avoid_zone_ids=intent.avoid_zone_ids, cruise_limits=preferred)
                except NoRouteError:
                    _add(steps, "observation", "巡航高度偏好下没有合规航线，继续按硬约束规划。")
                else:
                    return self._finish_dispatch(intent, route, corridor, mode, steps, model_called, model_fallback)
            route = self.plan_3d_route(
                intent.start_id, intent.end_id, corridor, avoid_zone_ids=intent.avoid_zone_ids, cruise_limits=limits,
            )
        except NoRouteError as exc:
            if limits:
                return self._notice(steps, "constraint_conflict", "指定巡航高度和空域限制下没有合规航线。",
                                    mode=mode, model_called=model_called)
            raise _error(steps, "当前空域与高度层下没有合规航线。", 409) from exc
        return self._finish_dispatch(intent, route, corridor, mode, steps, model_called, model_fallback)

    def _finish_dispatch(
        self, intent: DispatchIntent, route: AssessedRoute, corridor: MissionType, mode: LLMMode,
        steps: list[ThoughtStep], model_called: bool, model_fallback: bool,
    ) -> DispatchResult | DispatchNotice:
        _add(steps, "observation", f"规划成功，{len(route.waypoints)} 个航点，{route.distance_m:.0f} m。")
        return DispatchResult(
            intent=intent, mode=mode, model_called=model_called, model_fallback=model_fallback,
            corridor=corridor, start_name=self.pad_names[intent.start_id], end_name=self.pad_names[intent.end_id],
            waypoints=route.waypoints, kpis=route.kpis, thought_steps=steps,
            airspace_check=self.airspace.check_airspace(route.waypoints, route.flight_seconds),
        )

    def replan_emergency(self, request: ReplanRequest) -> ReplanResult:
        steps: list[ThoughtStep] = []
        _add(steps, "thought", "保持飞行当前位置，优先选择合规的应急航线。")
        _add(steps, "action", f"replan_emergency · {request.emergency_type}")
        task = self.spatial.active_task(request.mission_token) if request.mission_token else None
        if request.mission_token and task is None:
            raise _error(steps, "本任务的临时起降点会话已失效，请重新规划。", 409)
        pads = self.airspace.vertiports | (task.pads if task else {})
        if task:
            request = request.model_copy(update={
                "avoid_zone_ids": tuple(dict.fromkeys((*task.intent.avoid_zone_ids, *request.avoid_zone_ids))),
                "cruise_limits": intersect_limits(task.intent.constraints.cruise, request.cruise_limits),
            })
        if request.target_id not in pads:
            raise _error(steps, "原目的地不在起降坪清单内。", 422)
        if any(zone_id not in self.zone_names for zone_id in request.avoid_zone_ids):
            raise _error(steps, "指定的禁飞区编号不在空域清单内。", 422)

        if task and request.target_id == task.intent.end_id:
            corridor = "inspection" if task.intent.prefer_high_altitude else task.intent.mission_type
        else:
            corridor = request.mission_type or ("inspection" if request.current_pos.alt >= 60 else "delivery")
        hazards = (task.hazards if task else ()) + request.active_hazards
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
        via = task.intent.via if task and target == task.intent.end_id else ()
        if request.completed_visits > len(via):
            raise _error(steps, "途经进度与当前任务不匹配。", 422)
        remaining = via[request.completed_visits:] if request.emergency_type != "low_battery" else ()
        try:
            if request.emergency_type == "low_battery":
                _add(steps, "action", "plan_3d_route · 比较可达应急坪的合规航线")
                route, target, corridor = self._divert(request, corridor, hazards, pads)
                _add(steps, "observation", f"选定最短可达备降航线 · {target} {pads[target].properties.name}。")
            else:
                _add(steps, "action", f"plan_3d_route · 从当前坐标绕飞至 {target}")
                route = self.plan_3d_route(
                    request.current_pos, pads[target], corridor, hazards, avoid_zone_ids=request.avoid_zone_ids,
                    cruise_limits=request.cruise_limits, via=remaining,
                )
        except NoRouteError as exc:
            raise _error(steps, "当前位置和空域限制下没有合规应急航线。", 409) from exc

        _add(steps, "observation", f"新航线已通过空域校验，剩余航程 {route.distance_m:.0f} m。")
        if remaining:
            _add(steps, "observation", "改航保留剩余途经点：" + " → ".join(site.name for site in remaining) + "。")
        return ReplanResult(
            emergency_type=request.emergency_type, original_target_id=request.target_id, target_id=target,
            corridor=corridor, waypoints=route.waypoints, kpis=route.kpis, thought_steps=steps,
            temporary_nofly_zone=feature, route_visits=route_visits(remaining, route),
            airspace_check=self.airspace.check_airspace(route.waypoints, route.flight_seconds),
        )

    def _divert(
        self, request: ReplanRequest, corridor: MissionType, hazards: tuple[DynamicHazard, ...], pads: dict[str, Vertiport]
    ) -> tuple[AssessedRoute, str, MissionType]:
        emergency, other = [], []
        for pad in pads.values():
            (emergency if pad.properties.type == "emergency" else other).append(pad)
        corridors = (corridor, "inspection" if corridor == "delivery" else "delivery")
        for group in (emergency, other):
            candidates = []
            for pad in group:
                for candidate_layer in corridors:
                    try:
                        route = self.plan_3d_route(
                            request.current_pos, pad, candidate_layer, hazards,
                            avoid_zone_ids=request.avoid_zone_ids, cruise_limits=request.cruise_limits,
                        )
                    except NoRouteError:
                        continue
                    candidates.append((route, pad.properties.id, candidate_layer))
            if candidates:
                return min(candidates, key=lambda candidate: candidate[0].distance_m)
        raise NoRouteError("No reachable diversion pad")

    def _metrics(
        self,
        route: RoutePlan,
        corridor: MissionType,
        start: str | Vertiport | Position,
        end_id: str | Vertiport,
        hazards: tuple[DynamicHazard, ...],
        avoid_zone_ids: tuple[str, ...],
    ) -> tuple[RouteKPIs, float]:
        start_pad = self.airspace.vertiports[start] if isinstance(start, str) else start
        start_roof = start_pad.roof_obstacle if isinstance(start_pad, Vertiport) else None
        end_pad = self.airspace.vertiports[end_id] if isinstance(end_id, str) else end_id
        end_roof = end_pad.roof_obstacle
        positions = [self.airspace.frame.to_local(point) for point in route.waypoints]
        field, _ = self.airspace.with_hazards(hazards, avoid_zone_ids)
        elapsed = 0.0
        clearance = float("inf")
        for (a, b), arrival in zip(pairwise(positions), route.waypoints[1:], strict=True):
            elapsed += max(hypot(b[0] - a[0], b[1] - a[1]) / FLIGHT_SPEED_MPS[corridor],
                           abs(b[2] - a[2]) / VERTICAL_SPEED_MPS)
            roof = start_roof if arrival.phase == "takeoff" else end_roof if arrival.phase == "landing" else None
            clearance = min(clearance, field.segment_clearance(a, b, roof_access=roof))
        checkpoints = (positions[0], *(positions[index] for index in route.visit_indices), positions[-1])
        ground_route = sum(hypot(b[0] - a[0], b[1] - a[1]) for a, b in pairwise(checkpoints)) * GROUND_DETOUR_FACTOR
        ground_time = ground_route / GROUND_SPEED_MPS
        kpis = RouteKPIs(
            distance_m=round(route.distance_m, 2),
            estimated_seconds=round(elapsed, 2),
            ground_estimated_seconds=round(ground_time, 2),
            time_saving_percent=round((1 - elapsed / ground_time) * 100, 1) if ground_time else None,
            min_clearance_m=int(clearance * 100) / 100 if isfinite(clearance) else None,
            compliant=True,
            compliance_percent=100,
        )
        return kpis, elapsed
