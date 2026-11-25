"""Resolve language to scene geometry and prepare task-scoped ground sites."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from datetime import datetime, timezone
from itertools import product
from math import cos, hypot, pi, sin
from typing import TYPE_CHECKING, Callable

from openai import OpenAIError
from pydantic import BaseModel
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely import transform

from backend.config import CEILING_M, MISSION_LAYERS, STUDY_BOUNDS
from backend.core.airspace import Obstacle, ObstacleField, Vertiport
from backend.core.intent import ALT_CONTEXT, ALT_RANGE, LOW_FLIGHT, MEASUREMENT, UNSUPPORTED_STOP, intersect_limits, parse_constraints
from backend.core.planner import NoRouteError
from backend.core.session import SessionStore, add_step as event, build_notice
from backend.schemas import (
    AssessedRoute, ConfirmSpatialRequest, CruiseLimits, DispatchCandidate, DispatchConstraints, DispatchIntent,
    DispatchNotice, DispatchNoticeType, DispatchResult,
    DynamicHazard, GroundPoint, HazardFeature, LLMMode, LocationClarification, LocationOption,
    LocationSlot, MissionType, Position, RouteReference, RouteVisit,
    SpatialChoices, SpatialOptions, SpatialRouteOption, TaskSite, ThoughtStep, VertiportProperties,
)

if TYPE_CHECKING:
    from backend.core.agent import DispatchAgent, PendingDispatch

COORDINATES = re.compile(r"[\[(]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[\])]")
VIA_WORD = r"中途(?:经过|经由)|(?<!不)(?<!别)(?<!不要)(?:依次|先|再)?(?:经过|途经|途径|经由|飞越|飞过)|(?:先|再)经"
VIA_MARKER = re.compile(VIA_WORD)
ROLES = re.compile(r"从|由|起点(?:设在|在|为|是|[:：])|" + VIA_WORD
                   + r"|(?:最后|然后|接着|先|再)?(?:飞往|前往|抵达|送往|去|到|至|返回)|→|终点(?:设在|在|为|是|[:：])")
ORIGINS = ("从", "由", "起点")
EXTRA_CONSTRAINT = re.compile(r"[，,。；;]|\s*(?:绕开|避开|绕过|巡航|飞行时限|对比|比较|提供多条)")
COMPARE = re.compile(r"(?:比较|对比|提供|给我|给出|规划)?\s*(?:多条|多种|备选|替代|[两三二23]条)\s*(?:可行)?(?:航线|路线|方案)|(?:比较|对比)(?:可行)?(?:航线|路线|方案)")
MAX_VISITS = 3
NEAR_RADIUS_M = 96
EXPANDED_RADIUS_M = 168
SITE_STEP_M = 12
ROOFTOP = re.compile(r"楼顶|屋顶|屋面")
SEARCH = re.compile(r"附近|空地|地面(?:点|起降|位置)|找.{0,8}(?:起飞|降落|起降|地面)|择优|任选|候选")
AIRSPACE_QUERY = re.compile(r"(?:查(?:询|看)?|核验|确认).{0,36}(?:空域|禁飞|围栏)|(?:是否|能否|会不会).{0,8}禁飞|是不是禁飞")
AVOID_CLAUSE = re.compile(r"(?:绕开|避开|绕过)[^，,。；;]*")
TASK_AVOID_REFERENCE = re.compile(r"(?:绕开|避开|绕过)(?:我)?(?:标记|选定|画出)的?(?:区域|范围|避让区)")
DIRECTION = {"东": (1, 0), "西": (-1, 0), "北": (0, 1), "南": (0, -1)}
CANDIDATE_TTL = 600
TASK_TTL = 4 * 60 * 60
SLOT_EDIT = re.compile(r"^(?:把|将)?(起点|终点|第\s*([一二三123])\s*个?途经点)\s*(?:换成|改成|改为|设为|是|在|[:：])\s*(.+)$")
SLOT_LABELS = {"start": "起点", "end": "终点", **{f"via-{i}": f"第 {i} 个途经点" for i in range(1, MAX_VISITS + 1)}}
GENERIC_PLACES = {
    "校门": ("entrance", ("门",)), "门口": ("entrance", ("门",)),
    "操场": ("open_area", ("操场", "运动场")), "运动场": ("open_area", ("操场", "运动场")),
    "广场": ("open_area", ("广场",)), "空地": ("open_area", ()),
}
REPLY_CONTEXT = re.compile(r"^(?:(?:就)?在|靠近|离|挨着)\s*(.+?)(?:旁边|附近|最近|那边|那个)?$")


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
class SpatialDraft:
    slots: dict[str, str]
    selected: dict[str, Reference]
    parsed: DispatchCandidate
    constraints: DispatchConstraints
    hazards: tuple[DynamicHazard, ...]
    mission_type: MissionType
    compare: bool
    mode: LLMMode
    model_called: bool
    allow_layer_fallback: bool = False
    expand_search: bool = False


@dataclass(frozen=True)
class LocationIssue:
    reason: str
    options: tuple[Reference, ...] = ()
    map_pick: bool = True
    expand_search: bool = False


@dataclass(frozen=True)
class LocationTurn:
    draft: SpatialDraft
    resolved: dict[str, Reference]
    slot: str
    issue: LocationIssue


@dataclass(frozen=True)
class SiteChoice:
    site: TaskSite
    pad: Vertiport


@dataclass(frozen=True)
class ActiveTask:
    intent: DispatchIntent
    pads: dict[str, Vertiport]
    hazards: tuple[DynamicHazard, ...]
    corridor: MissionType
    allow_layer_fallback: bool
    active_hazards: tuple[DynamicHazard, ...] = ()


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


class AmbiguousPlace(ValueError):
    def __init__(self, places: list[Place]) -> None:
        names = "、".join(dict.fromkeys(place.name for place in places[:4]))
        super().__init__(f"地点有多个匹配（{names}），请选择具体位置。")
        self.places = places


def bounded(point: GroundPoint) -> bool:
    west, south, east, north = STUDY_BOUNDS
    return west <= point.lon <= east and south <= point.lat <= north


def place_key(text: str) -> str:
    return text.casefold().replace("珞瑜", "珞喻")


def location_phrase(text: str) -> str:
    return re.sub(r"(?:起飞|出发|降落|巡检|巡逻|配送|投送|送餐|送件|吧)+$", "", text.strip(" ，,。！？!?")).strip()


def route_texts(query: str) -> tuple[dict[str, str], list[str]]:
    # Keep the comma within a bracketed coordinate distinct from a clause separator.
    pairs = [match.span() for match in COORDINATES.finditer(query)]
    altitude_spans = [match.span() for match in ALT_RANGE.finditer(query)]
    marks = [mark for mark in ROLES.finditer(query)
             if not any(start <= mark.start() < end for start, end in altitude_spans)]
    destinations = [index for index, mark in enumerate(marks)
                    if not mark[0].startswith(ORIGINS) and not VIA_MARKER.fullmatch(mark[0])]
    ordered = (sum(mark[0] == "→" for mark in marks) > 1
               or any(marks[index][0].startswith(("先", "再", "然后", "接着", "最后")) for index in destinations))
    intermediate = set(destinations[:-1]) if ordered else set()
    values = {}
    via = []
    for index, mark in enumerate(marks):
        role = "via" if index in intermediate or VIA_MARKER.fullmatch(mark[0]) else "start" if mark[0].startswith(ORIGINS) else "end"
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
        if prefix and not re.search(r"^(?:把|请|帮我)|送|运|配送|巡检|巡逻|飞|规划|任务", prefix):
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
            self.places[place_key(alias)] = [Place(pad.properties.name, pad.position, pad_id)]

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
                if not any(item.pad_id for item in self.places.get(place_key(name), [])):
                    self.places.setdefault(place_key(name), []).append(place)

        for alias, identifier in agent.zone_aliases.items():
            if identifier in zones:
                self.places.setdefault(place_key(alias), [zones[identifier]])
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
            key = place_key(name)
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
        self.catalog = tuple(dict.fromkeys(place for group in self.places.values() for place in group))
        self.match_keys = sorted(self.places, key=len, reverse=True)
        self.dialogs: SessionStore[LocationTurn] = SessionStore(CANDIDATE_TTL, prefix="geo-")
        self.pending: SessionStore[CandidateSet] = SessionStore(CANDIDATE_TTL)
        self.active: SessionStore[ActiveTask] = SessionStore(TASK_TTL)

    def pad_clarification(self, candidate: DispatchCandidate) -> LocationClarification:
        slot = "start" if not candidate.start_id else "end"
        slots = [
            LocationSlot(
                key=key,
                label=SLOT_LABELS[key],
                phrase="",
                resolved_name=self.agent.pad_names[pad_id] if pad_id else None,
            )
            for key, pad_id in (("start", candidate.start_id), ("end", candidate.end_id))
        ]
        return LocationClarification(slot=slot, label=SLOT_LABELS[slot], slots=slots, options=[], map_pick=True)

    def resume_pending(
        self, pending: PendingDispatch, query: str, options: SpatialOptions, steps: list[ThoughtStep]
    ) -> SpatialChoices | DispatchNotice:
        try:
            hazards = self.task_hazards(options)
        except ValueError as exc:
            return self.notice("constraint_conflict", str(exc), steps)
        candidate = pending.candidate
        missing = "start" if not candidate.start_id else "end"
        event(steps, "action", f"继续确认{SLOT_LABELS[missing]}，沿用已识别的地点、航层和避让要求。")
        roles, _ = route_texts(AVOID_CLAUSE.sub("", query))
        selected = {
            slot: Reference(Place(self.airspace.vertiports[pad_id].properties.name,
                                  self.airspace.vertiports[pad_id].position, pad_id), exact=True)
            for slot, pad_id in (("start", candidate.start_id), ("end", candidate.end_id))
            if pad_id
        }
        for slot, point in (("start", options.start), ("end", options.end)):
            if point:
                selected[slot] = Reference(Place("地图点选位置", point), exact=True)
        slots = {
            "start": candidate.start_id or roles.get("start", query if missing == "start" and not roles else ""),
            "end": candidate.end_id or roles.get("end", query if missing == "end" and not roles else ""),
        }
        draft = SpatialDraft(
            slots=slots,
            selected=selected,
            parsed=candidate,
            constraints=pending.constraints,
            hazards=hazards,
            mission_type=candidate.mission_type,
            compare=bool(COMPARE.search(query)),
            mode=pending.mode,
            model_called=False,
            allow_layer_fallback=pending.allow_layer_fallback,
        )
        return self.advance(draft, steps)

    def applies(self, query: str, options: SpatialOptions | None, mentioned: list[str]) -> bool:
        if (options and (options.start or options.end or options.avoid)) or COORDINATES.search(query) or SEARCH.search(query):
            return True
        roles, via = route_texts(AVOID_CLAUSE.sub("", query))
        if via or VIA_MARKER.search(query) or COMPARE.search(query):
            return True
        if len(roles) == 2 and len(mentioned) < 2:
            return True
        if any(location_phrase(text) in GENERIC_PLACES for text in roles.values()):
            return True
        return any(any(not place.pad_id and (place.kind or len(mentioned) < 2)
                       for place in self.matches(text)) for text in roles.values())

    @staticmethod
    def is_new_route(query: str) -> bool:
        roles, _ = route_texts(AVOID_CLAUSE.sub("", query))
        return len(roles) == 2 and not SLOT_EDIT.fullmatch(query)

    @staticmethod
    def is_airspace_query(query: str) -> bool:
        return bool(AIRSPACE_QUERY.search(query) and not (len(route_texts(query)[0]) == 2 and re.search(r"配送|巡检|起飞", query)))

    def matches(self, text: str) -> list[Place]:
        matched = []
        spans = []
        for key in self.match_keys:
            token = rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])" if key.isascii() else re.escape(key)
            for match in re.finditer(token, place_key(text)):
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
            side = re.search(r"([东西南北])(?:边|侧|面)(?:那[个处座])?$", location_phrase(text))
            if side and not ROOFTOP.search(text):
                options = tuple(Reference(place, exact=bool(place.pad_id)) for place in matches)
                directional = [ref for ref in options
                               if self.option_label(ref.place, options).endswith(f" · {side[1]}侧")]
                if len(directional) == 1:
                    return self.named_reference(directional[0], options)
            raise AmbiguousPlace(matches)
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

        if len(via_texts) > MAX_VISITS:
            return self.notice("needs_spatial_location", "每次任务最多支持 3 个途经点，请拆成多次任务。", steps)
        if not via_texts and VIA_MARKER.search(task_query):
            via_texts = [""]
        start_text = roles.get("start", parsed.start_id or "")
        if start_text in {"信息学部起飞", "信息学部出发"} and parsed.start_id == "VP01":
            start_text = "VP01"
        slots = {"start": start_text, **{f"via-{i}": text for i, text in enumerate(via_texts, 1)},
                 "end": roles.get("end", parsed.end_id or "")}
        selected = {slot: Reference(Place("地图点选位置", point), exact=True)
                    for slot, point in (("start", options.start), ("end", options.end)) if point}
        allow_layer_fallback = not (parsed.prefer_high_altitude or LOW_FLIGHT.search(query) or constraints.cruise)
        draft = SpatialDraft(slots, selected, parsed, constraints, hazards, kind, bool(COMPARE.search(query)), mode, called,
                             allow_layer_fallback=allow_layer_fallback)
        return self.advance(draft, steps, model_called=called)

    def resolve_location(self, text: str, slot: str, resolved: dict[str, Reference]) -> Reference | LocationIssue:
        phrase = location_phrase(text)
        label = SLOT_LABELS[slot]
        roof = bool(ROOFTOP.search(phrase))
        generic = GENERIC_PLACES.get(phrase)
        if generic:
            kind, words = generic
            places = [place for place in self.catalog if place.kind == kind
                      and (not words or any(word in place.name for word in words))]
            anchor = resolved.get("end" if slot == "start" else "start")
            if anchor:
                places.sort(key=lambda place: self.distance_to(place, anchor.place))
            options = tuple(Reference(place) for place in places[:4])
            if options:
                return LocationIssue(f"你说的{label}「{phrase}」在场景内有多个选择，请选具体位置。", options)
        try:
            reference = self.reference(text)
        except AmbiguousPlace as exc:
            places = exc.places
            if roof:
                places = [place for place in places if place.pad_id
                          and self.airspace.vertiports[place.pad_id].roof_obstacle is not None]
            if not places:
                return LocationIssue(f"{label}「{phrase}」没有可核对的屋顶起降坪，请填写已登记的屋顶起降坪编号。", map_pick=False)
            choices = sorted(places, key=lambda place: (place.name, place.point.lon, place.point.lat))[:4]
            options = tuple(Reference(place, exact=bool(place.pad_id)) for place in choices)
            return LocationIssue(f"{label}「{phrase}」匹配到多个位置，请选择一个，也可以描述方位或附近地标。", options, not roof)
        except ValueError as exc:
            return LocationIssue(f"{label}「{phrase}」：{exc}", map_pick=not roof)
        if reference:
            if not bounded(reference.place.point):
                return LocationIssue(f"{label}「{phrase}」在校园场景范围外，请点选场景内的位置或换一个地标。")
            if slot.startswith("via-") and reference.direction:
                return LocationIssue(f"{label}「{phrase}」只描述了方位，请在地图上点出具体飞越位置。")
            return reference
        if not phrase:
            return LocationIssue(f"还没有指定{label}。可以选校门、道路、广场或在地图上点选。")
        ranked = [(SequenceMatcher(None, place_key(phrase), place_key(place.name), autojunk=False).ratio(), place)
                  for place in self.catalog
                  if (not roof or place.pad_id and self.airspace.vertiports[place.pad_id].roof_obstacle is not None)]
        choices = sorted(((score, place) for score, place in ranked if score >= .65), key=lambda item: -item[0])[:3]
        options = tuple(Reference(place, exact=bool(place.pad_id)) for _, place in choices)
        if options:
            return LocationIssue(f"地图未找到{label}「{phrase}」的确切位置。你指的是下面哪一处？", options, not roof)
        if roof:
            return LocationIssue(f"未找到{label}「{phrase}」对应的屋顶起降坪，请填写已登记的屋顶起降坪编号。", map_pick=False)
        return LocationIssue(f"地图未收录{label}「{phrase}」。可以说它靠近哪条路、哪处校门或广场，也可以直接在地图上点选。")

    def distance_to(self, place: Place, anchor: Place) -> float:
        x, y, _ = self.airspace.frame.to_local(Position(lon=place.point.lon, lat=place.point.lat, alt=0))
        ax, ay, _ = self.airspace.frame.to_local(Position(lon=anchor.point.lon, lat=anchor.point.lat, alt=0))
        return hypot(x - ax, y - ay)

    @staticmethod
    def option_label(place: Place, options: tuple[Reference, ...]) -> str:
        group = [item.place for item in options if item.place.name == place.name]
        if len(group) == 2:
            first, last = group
            lon_span = abs(first.point.lon - last.point.lon)
            lat_span = abs(first.point.lat - last.point.lat)
            if lon_span * cos(place.point.lat * pi / 180) >= lat_span:
                side = "西侧" if place.point.lon == min(item.point.lon for item in group) else "东侧"
            else:
                side = "南侧" if place.point.lat == min(item.point.lat for item in group) else "北侧"
            return f"{place.name} · {side}"
        return f"{place.name} · 位置 {group.index(place) + 1}" if len(group) > 1 else place.name

    def named_reference(self, reference: Reference, options: tuple[Reference, ...]) -> Reference:
        label = self.option_label(reference.place, options)
        return replace(reference, place=replace(reference.place, name=label))

    def ask(
        self, turn: LocationTurn, steps: list[ThoughtStep], *, model_called: bool = False,
        status: DispatchNoticeType = "needs_spatial_location",
    ) -> DispatchNotice:
        draft, issue = turn.draft, turn.issue
        token = self.dialogs.issue(turn)
        slots = [LocationSlot(key=key, label=SLOT_LABELS[key], phrase=location_phrase(phrase),
                              resolved_name=turn.resolved[key].place.name if key in turn.resolved else None)
                 for key, phrase in draft.slots.items()]
        options = [LocationOption(label=self.option_label(ref.place, issue.options),
                                  reply=f"选项{i}", point=ref.place.point)
                   for i, ref in enumerate(issue.options, 1)]
        return build_notice(
            steps, status, issue.reason, mode=draft.mode, model_called=model_called, token=token,
            clarification=LocationClarification(
                slot=turn.slot, label=SLOT_LABELS[turn.slot], slots=slots, options=options,
                map_pick=issue.map_pick, expand_search=issue.expand_search,
            ),
        )

    def advance(
        self, draft: SpatialDraft, steps: list[ThoughtStep], *, model_called: bool = False,
    ) -> SpatialChoices | DispatchNotice:
        resolved = draft.selected.copy()
        missing = {}
        for slot, text in draft.slots.items():
            if slot in resolved:
                if resolved[slot].place.name == "地图点选位置" and ROOFTOP.search(text):
                    resolved.pop(slot)
                    missing[slot] = LocationIssue(f"{SLOT_LABELS[slot]}要求屋顶起降，请填写已登记的屋顶起降坪编号。", map_pick=False)
                elif not bounded(resolved[slot].place.point):
                    resolved.pop(slot)
                    missing[slot] = LocationIssue(f"{SLOT_LABELS[slot]}的点选坐标超出校园场景，请在场景内重新点选。")
                continue
            candidate = self.resolve_location(text, slot, resolved)
            if isinstance(candidate, LocationIssue):
                missing[slot] = candidate
            else:
                resolved[slot] = candidate
        if missing:
            slot = next(iter(missing))
            issue = missing[slot]
            if location_phrase(draft.slots[slot]) in GENERIC_PLACES:
                issue = self.resolve_location(draft.slots[slot], slot, resolved)
            return self.ask(LocationTurn(draft, resolved, slot, issue), steps, model_called=model_called)
        return self.plan_draft(draft, resolved, steps)

    def reply(self, query: str, token: str, steps: list[ThoughtStep]) -> SpatialChoices | DispatchNotice:
        turn = self.dialogs.get(token)
        if not turn:
            return self.notice("clarification_closed", "这次地点确认已过期，请重新发送原始调度指令。", steps)
        event(steps, "action", f"继续确认{SLOT_LABELS[turn.slot]}，沿用已识别的地点、航层和避让要求。")
        draft = turn.draft
        slot = turn.slot
        if query.rstrip("。！!").strip() in {"扩大搜索范围", "扩大范围"} and turn.issue.expand_search:
            event(steps, "action", f"继续查找距离原地标 {EXPANDED_RADIUS_M} 米内的可达起降点。")
            return self.plan_draft(replace(draft, expand_search=True), turn.resolved, steps)
        if (AVOID_CLAUSE.search(query) or LOW_FLIGHT.search(query) or UNSUPPORTED_STOP.search(query)
                or ALT_CONTEXT.search(query) and MEASUREMENT.search(query)):
            detail = f"当前在确认{SLOT_LABELS[slot]}，原航层和避让要求仍保留。若要同时修改约束，请点「另起任务」并发送更新后的指令。"
            return self.ask(replace(turn, issue=replace(turn.issue, reason=detail)), steps)
        edit = SLOT_EDIT.fullmatch(query.rstrip("。！!"))
        if edit:
            key = {"起点": "start", "终点": "end"}.get(edit[1])
            slot = key or f"via-{'一二三'.find(edit[2]) + 1 if not edit[2].isascii() else edit[2]}"
            query = edit[3]
            if slot not in draft.slots:
                detail = f"原任务中没有{SLOT_LABELS[slot]}。这里正在确认{SLOT_LABELS[turn.slot]}。{turn.issue.reason}"
                return self.ask(replace(turn, issue=replace(turn.issue, reason=detail)), steps)

        options = turn.issue.options if slot == turn.slot else ()
        phrase = query.strip(" ，,。！？!?")
        index = re.fullmatch(r"(?:选项|选|第)\s*([一二三四1234])(?:[个项处])?(?:那个|就行|吧)?", phrase)
        reference = None
        if index:
            number = int(index[1]) if index[1].isascii() else "一二三四".index(index[1]) + 1
            if number <= len(options):
                reference = options[number - 1]
            else:
                return self.ask(turn, steps)
        elif options:
            side = re.fullmatch(r"(?:选|要)?\s*([东西南北])(?:边|侧|面)(?:那[个处座])?(?:吧)?", phrase)
            labels = [(self.option_label(ref.place, options), ref) for ref in options]
            matched = [ref for label, ref in labels if place_key(label) == place_key(phrase)
                       or side and label.endswith(f" · {side[1]}侧")]
            if len(matched) == 1:
                reference = matched[0]

        context = REPLY_CONTEXT.fullmatch(phrase)
        original = draft.slots[slot]
        keep_roof = ROOFTOP.search(original) and not edit
        if reference is None and context and options:
            anchors = self.matches(context[1])
            if len(anchors) == 1:
                ranked = sorted((self.distance_to(ref.place, anchors[0]), index)
                                for index, ref in enumerate(options))
                if len(ranked) == 1 or ranked[1][0] - ranked[0][0] >= 10:
                    reference = options[ranked[0][1]]
        if reference is None and not edit and (re.fullmatch(r"(?:都行|随便|不知道|哪个|哪个都可以|都可以|继续|好的?|是的?)(?:吧)?", phrase)
                                                or not phrase):
            return self.ask(turn, steps)
        if reference is None and keep_roof:
            phrase = f"{phrase}楼顶" if not ROOFTOP.search(phrase) else phrase
        if reference is None and context and not options and not keep_roof:
            phrase = context[1]
        slots = {**draft.slots, slot: phrase}
        selected = {key: ref for key, ref in turn.resolved.items() if key != slot}
        if reference:
            slots[slot] = original
            selected[slot] = self.named_reference(reference, options)
        return self.advance(replace(draft, slots=slots, selected=selected), steps)

    def plan_draft(self, draft: SpatialDraft, resolved: dict[str, Reference], steps: list[ThoughtStep]) -> SpatialChoices | DispatchNotice:
        parsed, hazards = draft.parsed, draft.hazards
        start, end = resolved["start"], resolved["end"]
        via = tuple(RouteReference(name=place.name, lon=place.point.lon, lat=place.point.lat, osm_ref=place.osm_ref)
                    for key in draft.slots if key.startswith("via-") for place in (resolved[key].place,))
        field, blocked = self.airspace.with_hazards(hazards, parsed.avoid_zone_ids)
        primary = "inspection" if parsed.prefer_high_altitude else draft.mission_type
        alternate = "inspection" if primary == "delivery" else "delivery"
        corridors = (primary, alternate) if draft.allow_layer_fallback else (primary,)
        radius = EXPANDED_RADIUS_M if draft.expand_search else NEAR_RADIUS_M
        accepted, rejected = [], Counter()
        accessible = {"start": False, "end": False}
        blocked_visits = []
        pad_access = {}
        event(steps, "action", "正在核对起降通道、巡航层接入和完整航线。")
        for corridor in corridors:
            layer = MISSION_LAYERS[corridor]
            levels = self.agent.planner.cruise_levels(layer, draft.constraints.cruise)
            if not levels:
                continue
            blocked_visits.append(self.blocked_visits(via, levels, field))
            if blocked_visits[-1]:
                continue

            def connects(pad: Vertiport) -> bool:
                key = (pad.position, pad.roof_obstacle.id if pad.roof_obstacle else None, levels)
                if key not in pad_access:
                    pad_access[key] = self.agent.planner.can_access_pad(pad, levels, field, blocked)
                return pad_access[key]

            seen_pairs = set()
            for dense in (False, True):
                if dense:
                    event(steps, "action", f"加密搜索原地标 {radius} 米内能接入航层的起降点。")
                starts = self.sites(start, "起点", field, rejected, connects, radius, dense)
                ends = self.sites(end, "终点", field, rejected, connects, radius, dense)
                accessible["start"] |= bool(starts)
                accessible["end"] |= bool(ends)
                for origin, target in sorted(product(starts, ends), key=lambda pair: sum(item.site.offset_m for item in pair)):
                    pair = (origin.pad.position, target.pad.position)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    intent = DispatchIntent(**parsed.model_dump(exclude={"start_id", "end_id", "mission_type"}),
                                            start_id=origin.site.id, end_id=target.site.id, mission_type=draft.mission_type,
                                            constraints=draft.constraints, via=via)
                    task = ActiveTask(intent, {item.site.id: item.pad for item in (origin, target)}, hazards, corridor,
                                      draft.allow_layer_fallback)
                    try:
                        route, _, preferred = self.route(task)
                    except NoRouteError:
                        rejected["候选组合无法连成完整航线"] += 1
                        continue
                    accepted.append((origin.site, target.site, task, route, corridor, preferred, None))
                    if len(accepted) == 3:
                        break
                if accepted or (start.exact and end.exact):
                    break
            if accepted:
                if corridor != primary:
                    event(steps, "observation", f"默认航层未找到可行航线，已用 {layer.min_alt:g}–{layer.max_alt:g} 米航层复核。")
                break

        if not accepted:
            if not blocked_visits:
                return self.notice("constraint_conflict", "指定巡航高度与当前任务航层不兼容，请调整航层后重新规划。", steps)
            visit_slots = [key for key in draft.slots if key.startswith("via-")]
            blocked_everywhere = set.intersection(*blocked_visits)
            if blocked_everywhere:
                index = min(blocked_everywhere)
                slot = visit_slots[index]
                detail = f"{SLOT_LABELS[slot]}「{via[index].name}」在允许的航层内没有可飞越的高度，可替换该途经点。"
                issue = LocationIssue(detail)
            else:
                slot = next((key for key in ("start", "end") if not accessible[key]), "end")
                if via and all(accessible.values()):
                    slot = visit_slots[0]
                can_expand = not draft.expand_search and any(not ref.exact and not ref.place.pad_id for ref in (start, end))
                if slot in accessible and not accessible[slot]:
                    detail = f"{SLOT_LABELS[slot]}「{resolved[slot].place.name}」周边未找到满足净空和航层接入条件的起降点。"
                else:
                    detail = "已检查可接入航层的起降点，当前约束下仍未找到完整航线。"
                detail += (" 可扩大搜索范围，或点选附近的另一处位置。" if can_expand
                           else f" 可换一个{SLOT_LABELS[slot]}或在地图上点选。")
                issue = LocationIssue(detail, expand_search=can_expand)
            return self.ask(LocationTurn(draft, resolved, slot, issue), steps, status="constraint_conflict")
        compare = draft.compare or bool(via)
        if compare:
            best = min(accepted, key=self.rank)
            accepted.extend(self.alternatives(best))
        ranked = sorted(accepted, key=self.rank)
        plans = {}
        for number, (origin, target, task, route, corridor, _, fixed) in enumerate(ranked[:3], start=1):
            clearance = route.kpis.min_clearance_m
            notes = []
            if corridor != primary:
                layer = MISSION_LAYERS[corridor]
                notes.append(f"改用 {layer.min_alt:g}–{layer.max_alt:g} 米航层")
            if max(origin.offset_m, target.offset_m) > NEAR_RADIUS_M:
                notes.append("采用扩展范围内的起降点")
            label = f"{fixed:g} m 巡航" if fixed is not None else "可行航线"
            label = ("推荐" if number == 1 else f"方案 {number}") + " · " + label
            option = SpatialRouteOption(
                id=f"choice-{number}", start=origin, end=target, corridor=corridor,
                waypoints=route.waypoints, kpis=route.kpis, route_label=label,
                route_visits=route_visits(via, route), planning_note=" · ".join(notes) or None,
                airspace_check=self.airspace.check_airspace(route.waypoints, route.flight_seconds),
                recommendation=f"已校验 {len(via) + 2} 个任务位置 · 航程 {route.kpis.distance_m:.1f} m"
                + (f" · 最小净空 {clearance:.1f} m" if clearance is not None else "")
                + "。",
            )
            plans[option.id] = CandidatePlan(option, task, fixed)
        if via:
            event(steps, "observation", "依序飞越：" + " → ".join(place.name for place in via) + "。")
        event(steps, "observation", f"完成起降及避障复核，共 {len(plans)} 条可行航线。")
        task_zones = self.task_zones(hazards)
        token = self.pending.issue(CandidateSet(plans, task_zones, tuple(steps), draft.mode, draft.model_called))
        return SpatialChoices(
            candidate_token=token, detail="请选择可行航线，核对起降和途经位置后确认。", mode=draft.mode,
            choice_type="routes" if compare else "sites",
            model_called=draft.model_called, thought_steps=steps, options=[plan.option for plan in plans.values()],
            rejected=dict(rejected), task_zones=task_zones,
        )


    def blocked_visits(self, via: tuple[RouteReference, ...], levels: tuple[int, ...], field: ObstacleField) -> set[int]:
        altitudes = [self.airspace.grid.center((0, 0, level))[2] for level in levels]
        blocked = set()
        for index, place in enumerate(via):
            x, y, _ = self.airspace.frame.to_local(Position(lon=place.lon, lat=place.lat, alt=0))
            if not any(field.segment_clear((x, y, alt), (x, y, alt)) for alt in altitudes):
                blocked.add(index)
        return blocked

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

    def sites(
        self, reference: Reference, role: str, field: ObstacleField, rejected: Counter[str],
        connects: Callable[[Vertiport], bool], radius: int, dense: bool,
    ) -> list[SiteChoice]:
        place = reference.place
        if not bounded(place.point):
            rejected["位置超出场景范围"] += 1
            return []
        if place.pad_id:
            pad = self.airspace.vertiports[place.pad_id]
            if not connects(pad):
                rejected["无法接入允许的航层"] += 1
                return []
            return [SiteChoice(TaskSite(id=place.pad_id, name=place.name, position=pad.position,
                                        reference_name=place.name, offset_m=0, reference_position=place.point), pad)]

        frame = self.airspace.frame
        x, y, _ = frame.to_local(Position(lon=place.point.lon, lat=place.point.lat, alt=0))
        dx, dy = reference.direction or (0, 0)
        offsets = [(0, 0)]
        if not reference.exact:
            offsets.extend((dist * cos(angle * pi / 4), dist * sin(angle * pi / 4))
                           for dist in (12, 18, 24, 36, 48, 60, 84, 96) for angle in range(8))
            if dense:
                axis = range(-radius, radius + 1, SITE_STEP_M)
                offsets.extend(product(axis, repeat=2))

        chosen: list[SiteChoice] = []
        chosen_xy: list[tuple[float, float]] = []
        if not reference.exact and not reference.direction and place.osm_ref:
            for pad_id in self.pads_by_source.get(place.osm_ref, []):
                pad = self.airspace.vertiports[pad_id]
                px, py, _ = frame.to_local(pad.position)
                offset = hypot(px - x, py - y)
                if offset <= radius and connects(pad):
                    chosen.append(SiteChoice(TaskSite(id=pad_id, name=pad.properties.name, position=pad.position,
                                                      reference_name=place.name, offset_m=round(offset, 1),
                                                      reference_position=place.point), pad))
                    chosen_xy.append((px, py))

        shifts = dict.fromkeys((round(a + 28 * dx, 5), round(b + 28 * dy, 5)) for a, b in offsets)
        for a, b in sorted(shifts, key=lambda offset: hypot(*offset)):
            offset = hypot(a, b)
            if offset > radius or len(chosen) >= 5:
                break
            if reference.direction and a * dx + b * dy <= 5:
                continue
            east, north = x + a, y + b
            if any(hypot(east - px, north - py) < 14 for px, py in chosen_xy):
                continue
            position = self.ground_position(east, north, field, rejected)
            if not position:
                continue
            if reference.exact:
                position = Position(lon=place.point.lon, lat=place.point.lat, alt=position.alt)
            index = len(chosen) + 1
            identifier = f"TMP-{'S' if role == '起点' else 'E'}{index}"
            name = f"{place.name} · {role}候选 {index}"
            pad = Vertiport(VertiportProperties(id=identifier, name=name, type="logistics", altitude=position.alt),
                            position, None)
            if not connects(pad):
                rejected["无法接入允许的航层"] += 1
                continue
            chosen.append(SiteChoice(TaskSite(id=identifier, name=name, position=position, reference_name=place.name,
                                              offset_m=round(offset, 1), reference_position=place.point), pad))
            chosen_xy.append((east, north))
        return chosen

    def ground_position(self, east: float, north: float, field: ObstacleField, rejected: Counter[str]) -> Position | None:
        terrain = field.terrain
        alt = max(0, float(terrain.elevation(east, north))) if terrain is not None else 0
        position = self.airspace.frame.to_position((east, north, alt))
        point = Point(east, north)
        if not bounded(position):
            reason = "位置超出场景范围"
        elif self.ground_exclusions.covers(point):
            reason = "位置落在道路或水面"
        else:
            nearby = field.index.query(point.buffer(5), predicate="intersects")
            if any(field.obstacles[index].min_alt <= alt + 3 and field.obstacles[index].max_alt >= alt for index in nearby):
                reason = "起降净空不足或落在围栏内"
            else:
                if terrain is None:
                    return position
                samples = [terrain.elevation(east + a, north + b) for a, b in ((5, 0), (-5, 0), (0, 5), (0, -5))]
                if max(samples) - min(samples) <= 1.5:
                    return position
                reason = "地形格网显示起伏较大"
        rejected[reason] += 1
        return None

    def route(self, task: ActiveTask, fixed_altitude: float | None = None) -> tuple[AssessedRoute, MissionType, bool]:
        intent = task.intent
        corridor = task.corridor
        constraints = intent.constraints
        strict = constraints.cruise
        if fixed_altitude is not None:
            strict = intersect_limits(strict, CruiseLimits(min_alt=fixed_altitude, max_alt=fixed_altitude))
        try:
            softer = intersect_limits(strict, constraints.preferred_cruise)
        except ValueError:
            variants = [(strict, False)]
        else:
            variants = [(softer, True), (strict, False)] if softer != strict else [(strict, True)]
        for limits, preferred in variants:
            try:
                route = self.agent.plan_3d_route(task.pads.get(intent.start_id, intent.start_id),
                                                 task.pads.get(intent.end_id, intent.end_id),
                                                 corridor, task.hazards, avoid_zone_ids=intent.avoid_zone_ids,
                                                 cruise_limits=limits, via=intent.via)
            except NoRouteError:
                continue
            return route, corridor, preferred
        raise NoRouteError("No compliant candidate route")

    def confirm(self, payload: ConfirmSpatialRequest) -> DispatchResult | DispatchNotice:
        selection = self.pending.get(payload.candidate_token)
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
        token = self.activate(task)
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
        return tuple(
            Obstacle.from_hazard(hazard, self.airspace.frame, index).to_feature(
                self.airspace.frame, feature_id=f"task-avoid-{index}", name=f"任务避让区 {index}", reason="task_avoidance",
            )
            for index, hazard in enumerate(hazards, start=1)
        )

    def activate(self, task: ActiveTask) -> str:
        return self.active.issue(task)

    def restart(self, token: str) -> str | None:
        task = self.active_task(token)
        return self.activate(replace(task, active_hazards=())) if task else None

    def remember_hazard(self, token: str, hazard: DynamicHazard) -> ActiveTask | None:
        def append_hazard(task: ActiveTask) -> ActiveTask:
            if hazard in task.active_hazards:
                return task
            if len(task.active_hazards) >= 20:
                raise ValueError("本任务已达到临时围栏上限。")
            return replace(task, active_hazards=(*task.active_hazards, hazard))

        return self.active.update(token, append_hazard)

    def active_task(self, token: str) -> ActiveTask | None:
        return self.active.get(token)

    @staticmethod
    def notice(status: DispatchNoticeType, detail: str, steps: list[ThoughtStep]) -> DispatchNotice:
        return build_notice(steps, status, detail)
