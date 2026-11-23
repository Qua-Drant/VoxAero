"""Resolve language to scene geometry and prepare task-scoped ground sites."""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from math import cos, hypot, pi, sin
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING

from openai import OpenAIError
from pydantic import BaseModel
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely import transform

from backend.config import CEILING_M, MISSION_LAYERS, STUDY_BOUNDS
from backend.core.airspace import Obstacle, ObstacleField, Vertiport
from backend.core.intent import LOW_FLIGHT, intersect_limits, parse_constraints
from backend.core.planner import NoRouteError
from backend.schemas import (
    AssessedRoute, ConfirmSpatialRequest, DispatchIntent, DispatchNotice, DispatchResult, DynamicHazard,
    CruiseLimits, GroundPoint, HazardFeature, HazardPolygon, LLMMode, MissionType, NoFlyProperties, Position, RouteReference, RouteVisit,
    SpatialChoices, SpatialOptions, SpatialRouteOption, TaskSite, ThoughtStep, VertiportProperties,
)

if TYPE_CHECKING:
    from backend.core.agent import DispatchAgent

COORDINATES = re.compile(r"[\[(]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[\])]")
VIA_WORD = r"中途(?:经过|经由)|(?<!不)(?<!别)(?<!不要)(?:依次|先|再)?(?:经过|途经|途径|经由|飞越|飞过)|(?:先|再)经"
VIA_MARKER = re.compile(VIA_WORD)
ROLES = re.compile(r"从|由|起点(?:设在|在|为|是|[:：])|" + VIA_WORD
                   + r"|(?:最后|再)?(?:飞往|前往|抵达|送往|去|到|至|返回)|→|终点(?:设在|在|为|是|[:：])")
ORIGINS = ("从", "由", "起点")
EXTRA_CONSTRAINT = re.compile(r"[，,。；;]|\s*(?:绕开|避开|绕过|巡航|飞行时限|对比|比较|提供多条)")
COMPARE = re.compile(r"(?:比较|对比|提供|给我|给出|规划)?\s*(?:多条|多种|备选|替代|[两三二23]条)\s*(?:可行)?(?:航线|路线|方案)|(?:比较|对比)(?:可行)?(?:航线|路线|方案)")
MAX_VISITS = 3
ROOFTOP = re.compile(r"楼顶|屋顶|屋面")
SEARCH = re.compile(r"附近|空地|地面(?:点|起降|位置)|找.{0,8}(?:起飞|降落|起降|地面)|择优|任选|候选")
AIRSPACE_QUERY = re.compile(r"(?:查(?:询|看)?|核验|确认).{0,36}(?:空域|禁飞|围栏)|(?:是否|能否|会不会).{0,8}禁飞|是不是禁飞")
AVOID_CLAUSE = re.compile(r"(?:绕开|避开|绕过)[^，,。；;]*")
TASK_AVOID_REFERENCE = re.compile(r"(?:绕开|避开|绕过)(?:我)?(?:标记|选定|画出)的?(?:区域|范围|避让区)")
DIRECTION = {"东": (1, 0), "西": (-1, 0), "北": (0, 1), "南": (0, -1)}
CONTEXT_LIMIT = 128
CANDIDATE_TTL = 600
TASK_TTL = 4 * 60 * 60


class SpatialExtraction(BaseModel):
    origin_quote: str | None = None
    destination_quote: str | None = None
    mission_type: MissionType | None = None


@dataclass(frozen=True)
class Place:
    name: str
    point: GroundPoint
    pad_id: str | None = None
    kind: str | None = None
    osm_ref: str | None = None
    geometry: BaseGeometry | None = None


@dataclass(frozen=True)
class Reference:
    place: Place
    exact: bool = False
    direction: tuple[int, int] | None = None


@dataclass(frozen=True)
class SiteChoice:
    site: TaskSite
    pad: Vertiport


@dataclass(frozen=True)
class ActiveTask:
    intent: DispatchIntent
    pads: dict[str, Vertiport]
    hazards: tuple[DynamicHazard, ...]


@dataclass(frozen=True)
class CandidatePlan:
    option: SpatialRouteOption
    task: ActiveTask
    fixed_altitude: float | None = None


@dataclass(frozen=True)
class CandidateSet:
    plans: dict[str, CandidatePlan]
    zones: tuple[HazardFeature, ...]
    steps: tuple[ThoughtStep, ...]
    mode: LLMMode
    model_called: bool


def event(steps: list[ThoughtStep], role, content: str) -> None:
    steps.append(ThoughtStep(step=len(steps) + 1, role=role, content=content))


def bounded(point: GroundPoint) -> bool:
    west, south, east, north = STUDY_BOUNDS
    return west <= point.lon <= east and south <= point.lat <= north


def route_texts(query: str) -> tuple[dict[str, str], list[str]]:
    # Keep the comma within a bracketed coordinate distinct from a clause separator.
    pairs = [match.span() for match in COORDINATES.finditer(query)]
    marks = list(ROLES.finditer(query))
    values = {}
    via = []
    for index, mark in enumerate(marks):
        role = "via" if VIA_MARKER.fullmatch(mark[0]) else "start" if mark[0].startswith(ORIGINS) else "end"
        stop = marks[index + 1].start() if index + 1 < len(marks) else len(query)
        for delimiter in EXTRA_CONSTRAINT.finditer(query, mark.end(), stop):
            if not any(first < delimiter.start() < last for first, last in pairs):
                stop = delimiter.start()
                break
        clause = query[mark.end():stop].strip()
        if not clause:
            continue
        if role == "via":
            via.extend(part.strip() for part in re.split(r"、|以及|(?<=.)和(?=.)", clause) if part.strip())
        else:
            values.setdefault(role, clause)
    if "start" not in values and "end" in values and marks[0][0] in {"到", "至", "→"}:
        prefix = query[:marks[0].start()].strip()
        if prefix:
            values["start"] = prefix
    return values, via


def route_visits(references: tuple[RouteReference, ...], route: AssessedRoute) -> tuple[RouteVisit, ...]:
    return tuple(RouteVisit(**reference.model_dump(), waypoint_index=index)
                 for reference, index in zip(references, route.visit_indices, strict=True))


class SpatialDispatch:
    def __init__(self, agent: DispatchAgent) -> None:
        self.agent = agent
        self.airspace = agent.airspace
        self.places: dict[str, list[Place]] = {}
        for alias, pad_id in agent.pad_aliases.items():
            pad = self.airspace.vertiports[pad_id]
            self.places[alias] = [Place(pad.properties.name, pad.position, pad_id)]

        zones = {}
        for layer in ("buildings", "nofly_zones"):
            for feature in self.airspace.geojson.get(layer, {}).get("features", []):
                props = feature["properties"]
                name = props.get("name", "")
                if len(name) < 2 or re.match(r"(?:周边|校园|未命名)?建筑[ -]", name):
                    continue
                center = shape(feature["geometry"]).representative_point()
                place = Place(name, GroundPoint(lon=center.x, lat=center.y))
                if layer == "nofly_zones":
                    zones[props["id"]] = place
                if not any(item.pad_id for item in self.places.get(name, [])):
                    self.places.setdefault(name.casefold(), []).append(place)

        for alias, identifier in agent.zone_aliases.items():
            if identifier in zones:
                self.places.setdefault(alias, [zones[identifier]])
        self.pads_by_source = {}
        for feature in self.airspace.geojson.get("vertiports", {}).get("features", []):
            props = feature["properties"]
            if props.get("osm_ref"):
                self.pads_by_source.setdefault(props["osm_ref"], []).append(props["id"])
        waters = []
        for feature in self.airspace.geojson.get("places", {}).get("features", []):
            props = feature["properties"]
            geometry = shape(feature["geometry"])
            name, kind = props["name"], props["kind"]
            if kind == "road":
                if geometry.geom_type == "MultiLineString":
                    line = max(geometry.geoms, key=lambda segment: segment.length)
                else:
                    line = geometry
                center = line.interpolate(.5, normalized=True)
            else:
                center = geometry.representative_point()
            place = Place(name, GroundPoint(lon=center.x, lat=center.y), kind=kind,
                          osm_ref=props["osm_ref"], geometry=geometry)
            for alias in dict.fromkeys((name, name.replace("珞瑜", "珞喻"))):
                key = alias.casefold()
                if not any(item.pad_id for item in self.places.get(key, [])):
                    self.places.setdefault(key, []).append(place)
            if kind == "water":
                waters.append(transform(geometry, self.airspace.frame.project_xy).buffer(4))
        roads = [transform(shape(feature["geometry"]), self.airspace.frame.project_xy).buffer(5)
                 for feature in self.airspace.geojson.get("roads", {}).get("features", [])
                 if feature["properties"].get("highway") not in {"footway", "path", "steps", "cycleway"}]
        self.ground_exclusions = unary_union([*waters, *roads])

        pad = self.airspace.vertiports.get("VP01")
        if pad:
            self.places.setdefault("信息学部", [Place("信息学部", pad.position)])
        self.pending: OrderedDict[str, tuple[float, CandidateSet]] = OrderedDict()
        self.active: OrderedDict[str, tuple[float, ActiveTask]] = OrderedDict()
        self.lock = Lock()

    def applies(self, query: str, options: SpatialOptions | None, mentioned: list[str]) -> bool:
        if (options and (options.start or options.end or options.avoid)) or COORDINATES.search(query) or SEARCH.search(query):
            return True
        roles, via = route_texts(AVOID_CLAUSE.sub("", query))
        if via or VIA_MARKER.search(query) or COMPARE.search(query):
            return True
        return any(any(not place.pad_id and (place.kind or len(mentioned) < 2)
                       for place in self.matches(text)) for text in roles.values())

    @staticmethod
    def is_airspace_query(query: str) -> bool:
        return bool(AIRSPACE_QUERY.search(query) and not (len(route_texts(query)[0]) == 2 and re.search(r"配送|巡检|起飞", query)))

    def matches(self, text: str) -> list[Place]:
        matched = []
        spans = []
        for key in sorted(self.places, key=len, reverse=True):
            token = rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])" if key.isascii() else re.escape(key)
            for match in re.finditer(token, text.casefold()):
                if any(start <= match.start() and match.end() <= stop for start, stop in spans):
                    continue
                spans.append(match.span())
                matched.extend(self.places[key])
        return list(dict.fromkeys(matched))

    def reference(self, text: str, picked: GroundPoint | None = None) -> Reference | None:
        if picked:
            if ROOFTOP.search(text):
                raise ValueError("地图点选只用于地面选址，屋顶请指定已登记的起降坪。")
            return Reference(Place("地图点选位置", picked), exact=True)
        coords = list(COORDINATES.finditer(text))
        if len(coords) > 1:
            raise ValueError("一个起终点对应了多个坐标，请明确要使用的位置。")
        if coords:
            if ROOFTOP.search(text):
                raise ValueError("经纬度选址只用于地面，屋顶请指定已登记的起降坪。")
            lon, lat = map(float, coords[0].groups())
            return Reference(Place("指令坐标", GroundPoint(lon=lon, lat=lat)), exact=True)
        matches = self.matches(text)
        if len(matches) == 2 and all(place.kind == "road" for place in matches) and "路口" in text:
            intersection = matches[0].geometry.intersection(matches[1].geometry)
            if intersection.geom_type != "Point" or intersection.is_empty:
                raise ValueError("场景路网中无唯一交点，请补充附近地标或在地图上点选。")
            return Reference(Place("与".join(place.name for place in matches) + "路口",
                                   GroundPoint(lon=intersection.x, lat=intersection.y), kind="road"))
        if len(matches) > 1:
            names = "、".join(dict.fromkeys(place.name for place in matches[:4]))
            raise ValueError(f"地点有多个匹配（{names}），请补充名称或在地图上点选。")
        if not matches:
            return None
        place = matches[0]
        if ROOFTOP.search(text):
            if place.pad_id and self.airspace.vertiports[place.pad_id].roof_obstacle is not None and not SEARCH.search(text):
                return Reference(place, exact=True)
            raise ValueError("当前支持新建地面候选，屋顶请指定已登记的起降坪。")
        if SEARCH.search(text) and place.pad_id:
            place = Place(place.name, place.point)
        elif place.pad_id:
            return Reference(place, exact=True)
        side = re.search(r"([东西南北])侧", text)
        direction = DIRECTION[side[1]] if side and side[0] not in place.name else None
        return Reference(place, direction=direction)

    def extract(self, query: str, steps: list[ThoughtStep]) -> tuple[dict[str, str], list[str], LLMMode, bool, MissionType | None]:
        locations = AVOID_CLAUSE.sub("", query)
        roles, via = route_texts(locations)
        called = self.agent.settings.mode == "live_llm"
        if not called:
            event(steps, "action", "按本地规则识别地名、坐标及任务角色。")
            return roles, via, "fallback_mock", False, None
        event(steps, "action", "模型提取地点和任务类型，地名交由场景数据核对。")
        function = {"name": "extract_spatial_task", "description": "Quote origin and destination phrases",
                    "parameters": SpatialExtraction.model_json_schema()}
        prompt = ("Use exactly one tool call. Quote the user's origin and destination verbatim, including words "
                  "like 西侧 or 附近 that qualify a location. Do not invent coordinates or locations. Use null for "
                  "missing or ambiguous roles. Phrases after 途经/经过/经由 describe fly-over points, not destinations. "
                  "Ignore time requirements. Choose delivery or inspection only when supported by the query.")
        try:
            model = SpatialExtraction.model_validate_json(self.agent.request_model(query, function, prompt))
            grounded = roles.copy()
            for role, quote in (("start", model.origin_quote), ("end", model.destination_quote)):
                if quote is not None:
                    if not quote.strip() or quote not in locations:
                        raise ValueError("The model supplied an ungrounded location")
                    other = roles.get("end" if role == "start" else "start")
                    if other and quote in other and role not in roles:
                        raise ValueError("The model reused the other endpoint")
                    if role not in roles and any(quote in location for location in via):
                        raise ValueError("The model reused a fly-over location")
                    grounded.setdefault(role, quote)
            return grounded, via, "live_llm", True, model.mission_type
        except (OpenAIError, ValueError):
            event(steps, "observation", "模型地点未能核对，继续按本地场景匹配。")
            return roles, via, "fallback_mock", True, None

    def inspect_airspace(self, query: str, options: SpatialOptions | None, steps: list[ThoughtStep]) -> DispatchNotice:
        try:
            reference = self.reference(query, (options.start or options.end) if options else None)
            task_zones = self.task_zones(self.task_hazards(options)) if options else ()
        except ValueError as exc:
            return self.notice("needs_spatial_location", str(exc), steps)
        if not reference or not bounded(reference.place.point):
            return self.notice("needs_spatial_location", "请提供场景内的地名、经纬度或地图点选位置。", steps)
        point = reference.place.point
        location = Point(point.lon, point.lat)
        zones = self.airspace.geojson["nofly_zones"]["features"]
        hits = []
        for feature in (*zones, *(zone.model_dump(mode="json") for zone in task_zones)):
            if shape(feature["geometry"]).covers(location):
                props = feature["properties"]
                source = "task" if props.get("reason") == "task_avoidance" else props.get("source_kind", "scenario")
                kind = {"external": "外部快照", "task": "任务避让"}.get(source, "场景围栏")
                if source == "external":
                    now = datetime.now(timezone.utc)
                    active = datetime.fromisoformat(props["effective_from"]) <= now < datetime.fromisoformat(props["effective_until"])
                    kind += " · " + ("在登记时段内" if active else "登记时段外，仿真保守避让")
                hits.append(f"{kind}：{props['name']}（场景高度 {props['min_alt']:g}–{props['max_alt']:g} 米）")
        check = self.airspace.check_airspace([point])
        detail = f"{reference.place.name}参考位置：" + ("；".join(hits) if hits else "已载入围栏在该参考位置无交叠。")
        event(steps, "observation", detail)
        return DispatchNotice(status="airspace_report", detail=detail, thought_steps=steps, airspace_check=check)

    def propose(self, query: str, options: SpatialOptions, steps: list[ThoughtStep]) -> SpatialChoices | DispatchNotice:
        try:
            task_query = TASK_AVOID_REFERENCE.sub("", query) if options.avoid else query
            parsed = self.agent.parse_dispatch_intent(task_query)
            constraints = parse_constraints(task_query)
            hazards = self.task_hazards(options)
        except ValueError as exc:
            return self.notice("constraint_conflict", str(exc), steps)

        roles, via_texts, mode, called, mission_type = self.extract(task_query, steps)
        kind = mission_type if mission_type and not re.search(r"巡检|巡逻|安防|配送|投送|送餐|送件", query) else parsed.mission_type
        if LOW_FLIGHT.search(query) and (kind == "inspection" or parsed.prefer_high_altitude):
            return self.notice("constraint_conflict", "避开高空与本次 60–90 米巡检航层冲突，请调整任务或航层。", steps)
        try:
            start_text = roles.get("start", parsed.start_id or "")
            end_text = roles.get("end", parsed.end_id or "")
            if start_text in {"信息学部起飞", "信息学部出发"} and parsed.start_id == "VP01":
                start_text = "VP01"
            start = self.reference(start_text, options.start)
            end = self.reference(end_text, options.end)
            via = self.resolve_visits(via_texts, task_query)
        except ValueError as exc:
            return self.notice("needs_spatial_location", str(exc), steps)
        if not start or not end:
            missing = "起点" if end else "终点" if start else "起点和终点"
            return self.notice("needs_spatial_location", f"请补充{missing}的校门、广场、道路、起降点编号或坐标。", steps)

        field, _ = self.airspace.with_hazards(hazards, parsed.avoid_zone_ids)
        rejected: Counter[str] = Counter()
        starts = self.sites(start, "起点", field, rejected)
        ends = self.sites(end, "终点", field, rejected)
        if not starts or not ends:
            cause = "、".join(rejected) or "缺少可用地面位置"
            return self.notice("constraint_conflict", f"未找到可用起降点：{cause}。请换一处地面位置。", steps)

        event(steps, "action", "用场景地形、障碍包络及已载入围栏筛选地面点，逐组校验起降与航线。")
        accepted = []
        for origin, target in sorted(product(starts, ends), key=lambda pair: sum(item.site.offset_m for item in pair))[:8]:
            intent = DispatchIntent(**parsed.model_dump(exclude={"start_id", "end_id", "mission_type"}),
                                    start_id=origin.site.id, end_id=target.site.id, mission_type=kind, constraints=constraints, via=via)
            pads = {item.site.id: item.pad for item in (origin, target)}
            task = ActiveTask(intent, pads, hazards)
            try:
                route, corridor, preferred = self.route(task)
            except NoRouteError:
                rejected["起降或航线不满足约束"] += 1
                continue
            accepted.append((origin.site, target.site, task, route, corridor, preferred, None))
        if not accepted:
            missing = "、".join(place.name for place in via)
            detail = f"无法按顺序飞越 {missing}，请核对途经点、避让范围或巡航高度。" if missing else "候选起降点间没有满足空域和高度条件的航线。"
            return self.notice("constraint_conflict", detail, steps)
        compare = bool(COMPARE.search(query) or via)
        if compare:
            best = min(accepted, key=self.rank)
            accepted.extend(self.alternatives(best))
        ranked = sorted(accepted, key=self.rank)
        plans = {}
        for number, (origin, target, task, route, corridor, _, fixed) in enumerate(ranked[:3], start=1):
            clearance = route.kpis.min_clearance_m
            label = f"{fixed:g} m 巡航" if fixed is not None else "可行航线"
            label = ("推荐" if number == 1 else f"方案 {number}") + " · " + label
            option = SpatialRouteOption(
                id=f"choice-{number}", start=origin, end=target, corridor=corridor,
                waypoints=route.waypoints, kpis=route.kpis, route_label=label,
                route_visits=route_visits(via, route),
                airspace_check=self.airspace.check_airspace(route.waypoints, route.flight_seconds),
                recommendation=f"已校验 {len(via) + 2} 个任务位置 · 航程 {route.kpis.distance_m:.1f} m"
                + (f" · 最小净空 {clearance:.1f} m" if clearance is not None else "")
                + f" · 起终点偏移 {origin.offset_m + target.offset_m:.1f} m。",
            )
            plans[option.id] = CandidatePlan(option, task, fixed)
        if via:
            event(steps, "observation", "依序飞越：" + " → ".join(place.name for place in via) + "。")
        event(steps, "observation", f"完成起降及避障复核，共 {len(plans)} 条可行航线。")
        task_zones = self.task_zones(hazards)
        token = self.store(self.pending, CandidateSet(plans, task_zones, tuple(steps), mode, called), CANDIDATE_TTL)
        return SpatialChoices(
            candidate_token=token, detail="请选择可行航线，核对起降和途经位置后确认。", mode=mode,
            choice_type="routes" if compare else "sites",
            model_called=called, thought_steps=steps, options=[plan.option for plan in plans.values()],
            rejected=dict(rejected), task_zones=task_zones,
        )

    def resolve_visits(self, texts: list[str], query: str) -> tuple[RouteReference, ...]:
        if not texts and VIA_MARKER.search(query):
            raise ValueError("请明确要依序飞越的地点。")
        if len(texts) > MAX_VISITS:
            raise ValueError("每次任务最多支持 3 个途经点，请缩短路线或拆成多次任务。")
        visits = []
        for text in texts:
            reference = self.reference(text)
            if not reference or not bounded(reference.place.point):
                raise ValueError(f"未找到途经点「{text}」，请使用场景地名或经纬度。")
            place = reference.place
            if reference.direction:
                raise ValueError(f"途经点「{text}」需明确位置，可填写经纬度。")
            visits.append(RouteReference(name=place.name, lon=place.point.lon, lat=place.point.lat, osm_ref=place.osm_ref))
        return tuple(visits)

    @staticmethod
    def rank(item):
        origin, target, _, route, _, preferred, _ = item
        clearance = route.kpis.min_clearance_m
        return (not preferred, origin.offset_m + target.offset_m,
                -(clearance if clearance is not None else -1), route.distance_m)

    def alternatives(self, candidate):
        origin, target, task, original, corridor, _, _ = candidate
        levels = self.agent.planner.cruise_levels(MISSION_LAYERS[corridor], task.intent.constraints.cruise)
        signature = lambda route: tuple((p.lon, p.lat, p.alt) for p in route.waypoints)
        seen = {signature(original)}
        alternatives = []
        for level in levels:
            altitude = self.airspace.grid.center((0, 0, level))[2]
            try:
                route, _, preferred = self.route(task, altitude)
            except NoRouteError:
                continue
            path = signature(route)
            if path in seen:
                continue
            seen.add(path)
            alternatives.append((origin, target, task, route, corridor, preferred, altitude))
        return alternatives

    def sites(self, reference: Reference, role: str, field: ObstacleField, rejected: Counter[str]) -> list[SiteChoice]:
        place = reference.place
        if not bounded(place.point):
            rejected["位置超出场景范围"] += 1
            return []
        if place.pad_id:
            pad = self.airspace.vertiports[place.pad_id]
            return [SiteChoice(TaskSite(id=place.pad_id, name=place.name, position=pad.position,
                                        reference_name=place.name, offset_m=0), pad)]

        x, y, _ = self.airspace.frame.to_local(Position(lon=place.point.lon, lat=place.point.lat, alt=0))
        dx, dy = reference.direction or (0, 0)
        base_x, base_y = x + 28 * dx, y + 28 * dy
        offsets = [(0, 0)]
        if not reference.exact:
            offsets.extend((radius * cos(angle * pi / 4), radius * sin(angle * pi / 4))
                           for radius in (12, 18, 24, 36, 48, 60, 84, 96) for angle in range(8))
        candidates = []
        terrain = field.terrain
        registered = [self.airspace.frame.to_local(pad.position)[:2] for pad in self.airspace.vertiports.values()]
        for east_offset, north_offset in offsets:
            east, north = base_x + east_offset, base_y + north_offset
            if reference.direction and (east - x) * dx + (north - y) * dy <= 5:
                continue
            if not reference.exact and any(hypot(east - px, north - py) < 8 for px, py in registered):
                continue
            alt = max(0, float(terrain.elevation(east, north))) if terrain is not None else 0
            position = self.airspace.frame.to_position((east, north, alt))
            if reference.exact:
                position = Position(lon=place.point.lon, lat=place.point.lat, alt=alt)
            if not bounded(position):
                rejected["位置超出场景范围"] += 1
                continue
            point = Point(east, north)
            if self.ground_exclusions.covers(point):
                rejected["位置落在道路或水面"] += 1
                continue
            indexes = field.index.query(point.buffer(5), predicate="intersects")
            if any(field.obstacles[index].min_alt <= alt + 3 and field.obstacles[index].max_alt >= alt
                   for index in indexes):
                rejected["起降净空不足或落在围栏内"] += 1
                continue
            if terrain is not None:
                samples = [terrain.elevation(east + a, north + b) for a, b in ((5, 0), (-5, 0), (0, 5), (0, -5))]
                if max(samples) - min(samples) > 1.5:
                    rejected["地形格网显示起伏较大"] += 1
                    continue
            candidates.append((hypot(east - x, north - y), position))
        chosen: list[SiteChoice] = []
        if not reference.exact and not reference.direction and place.osm_ref:
            for pad_id in self.pads_by_source.get(place.osm_ref, []):
                pad = self.airspace.vertiports[pad_id]
                px, py, _ = self.airspace.frame.to_local(pad.position)
                chosen.append(SiteChoice(TaskSite(id=pad_id, name=pad.properties.name, position=pad.position,
                                                  reference_name=place.name, offset_m=round(hypot(px - x, py - y), 1)), pad))
        for offset, position in sorted(candidates, key=lambda value: value[0]):
            local = self.airspace.frame.to_local(position)
            if any(hypot(*(a - b for a, b in zip(local[:2], self.airspace.frame.to_local(item.pad.position)[:2]))) < 14
                   for item in chosen):
                continue
            if len(chosen) >= 5:
                break
            index = len(chosen) + 1
            identifier = f"TMP-{'S' if role == '起点' else 'E'}{index}"
            name = f"{place.name} · {role}候选 {index}"
            pad = Vertiport(VertiportProperties(id=identifier, name=name, type="logistics", altitude=position.alt),
                           position, None)
            chosen.append(SiteChoice(TaskSite(id=identifier, name=name, position=position, reference_name=place.name,
                                              offset_m=round(offset, 1)), pad))
        return chosen

    def route(self, task: ActiveTask, fixed_altitude: float | None = None) -> tuple[AssessedRoute, MissionType, bool]:
        intent = task.intent
        corridor = "inspection" if intent.prefer_high_altitude else intent.mission_type
        constraints = intent.constraints
        strict = constraints.cruise
        if fixed_altitude is not None:
            strict = intersect_limits(strict, CruiseLimits(min_alt=fixed_altitude, max_alt=fixed_altitude))
        softer = intersect_limits(strict, constraints.preferred_cruise)
        variants = [(softer, True), (strict, False)] if softer != strict else [(strict, True)]
        for limits, preferred in variants:
            try:
                route = self.agent.plan_3d_route(task.pads[intent.start_id], task.pads[intent.end_id],
                                                 corridor, task.hazards, avoid_zone_ids=intent.avoid_zone_ids,
                                                 cruise_limits=limits, via=intent.via)
            except NoRouteError:
                continue
            return route, corridor, preferred
        raise NoRouteError("No compliant candidate route")

    def confirm(self, payload: ConfirmSpatialRequest) -> DispatchResult | DispatchNotice:
        selection = self.get(self.pending, payload.candidate_token)
        if selection is None or payload.option_id not in selection.plans:
            return self.notice("constraint_conflict", "候选已失效或选择不存在，请重新规划。", [])
        plan = selection.plans[payload.option_id]
        steps = list(selection.steps)
        try:
            route, corridor, preferred = self.route(plan.task, plan.fixed_altitude)
        except NoRouteError:
            return self.notice("constraint_conflict", "复核时发现所选路线不可用，请重新规划。", steps)
        event(steps, "observation", "所选起降点及航线已复核。" if preferred else "航线满足必须条件，部分偏好未满足。")
        task = plan.task
        token = self.store(self.active, task, TASK_TTL)
        return DispatchResult(
            mode=selection.mode, model_called=selection.model_called,
            model_fallback=selection.model_called and selection.mode == "fallback_mock",
            intent=task.intent, corridor=corridor, start_name=plan.option.start.name, end_name=plan.option.end.name,
            waypoints=route.waypoints, kpis=route.kpis, thought_steps=steps, mission_token=token,
            route_visits=route_visits(task.intent.via, route), cruise_altitude_m=plan.fixed_altitude,
            task_sites=tuple(site for site in (plan.option.start, plan.option.end) if site.id.startswith("TMP-")),
            task_zones=selection.zones, airspace_check=self.airspace.check_airspace(route.waypoints, route.flight_seconds),
        )

    @staticmethod
    def task_hazards(options: SpatialOptions) -> tuple[DynamicHazard, ...]:
        if any(not bounded(point) for point in options.avoid):
            raise ValueError("避让区中心需要在当前场景范围内。")
        return tuple(DynamicHazard(center_lon=point.lon, center_lat=point.lat, radius_m=point.radius_m,
                                   min_alt=0, max_alt=CEILING_M) for point in options.avoid)

    def task_zones(self, hazards: tuple[DynamicHazard, ...]) -> tuple[HazardFeature, ...]:
        features = []
        for index, hazard in enumerate(hazards, start=1):
            obstacle = Obstacle.from_hazard(hazard, self.airspace.frame, index)
            ring = [self.airspace.frame.to_position((x, y, 0)) for x, y in obstacle.footprint.exterior.coords]
            features.append(HazardFeature(
                properties=NoFlyProperties(id=f"task-avoid-{index}", name=f"任务避让区 {index}", reason="task_avoidance",
                                           min_alt=hazard.min_alt, max_alt=hazard.max_alt),
                geometry=HazardPolygon(coordinates=[[(point.lon, point.lat) for point in ring]]),
            ))
        return tuple(features)

    def store(self, cache, value, ttl: int) -> str:
        token = token_urlsafe(24)
        with self.lock:
            cache[token] = (monotonic() + ttl, value)
            if len(cache) > CONTEXT_LIMIT:
                cache.popitem(last=False)
        return token

    def get(self, cache, token: str):
        with self.lock:
            entry = cache.get(token)
            if entry and entry[0] > monotonic():
                return entry[1]
            cache.pop(token, None)
        return None

    def active_task(self, token: str) -> ActiveTask | None:
        return self.get(self.active, token)

    @staticmethod
    def notice(status, detail: str, steps: list[ThoughtStep]) -> DispatchNotice:
        event(steps, "observation", detail)
        return DispatchNotice(status=status, detail=detail, thought_steps=steps)
