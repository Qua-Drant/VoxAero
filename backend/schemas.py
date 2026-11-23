"""GeoJSON properties and route models shared by the spatial modules."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, FiniteFloat, StringConstraints, model_validator

Longitude = Annotated[FiniteFloat, Field(ge=-180, le=180)]
Latitude = Annotated[FiniteFloat, Field(ge=-90, le=90)]
NonnegativeMeters = Annotated[FiniteFloat, Field(ge=0)]
NonemptyString = Annotated[str, Field(min_length=1)]

MissionType = Literal["delivery", "inspection"]
FlightPhase = Literal["takeoff", "cruise", "landing", "transition"]
EmergencyType = Literal["wind_gust", "temp_nofly", "low_battery"]
LLMMode = Literal["live_llm", "fallback_mock"]
DispatchNoticeType = Literal[
    "help", "out_of_scope", "needs_intent", "needs_clarification", "clarification_closed", "constraint_conflict",
    "needs_spatial_location", "airspace_report",
]


class FeatureProperties(BaseModel, frozen=True):
    id: NonemptyString
    name: NonemptyString


class BuildingProperties(FeatureProperties):
    height: Annotated[FiniteFloat, Field(gt=0)]
    category: Literal["teaching", "lab", "dorm", "canteen", "library", "other"]
    base_alt: NonnegativeMeters = 0
    collision_top_alt: NonnegativeMeters | None = None

    @model_validator(mode="after")
    def check_collision_height(self) -> BuildingProperties:
        if self.collision_top_alt is not None and self.collision_top_alt < self.base_alt + self.height - 1e-8:
            raise ValueError("The collision ceiling must cover the modeled roof")
        return self


class VertiportProperties(FeatureProperties):
    type: Literal["logistics", "inspection", "emergency"]
    altitude: NonnegativeMeters
    host_building_id: NonemptyString | None = None


class AltitudeRange(BaseModel, frozen=True):
    min_alt: NonnegativeMeters
    max_alt: NonnegativeMeters

    @model_validator(mode="after")
    def check_altitude_order(self) -> AltitudeRange:
        if self.max_alt <= self.min_alt:
            raise ValueError("max_alt must be greater than min_alt")
        return self


class NoFlyProperties(FeatureProperties, AltitudeRange):
    reason: NonemptyString


class DynamicHazard(AltitudeRange):
    center_lon: Longitude
    center_lat: Latitude
    radius_m: Annotated[FiniteFloat, Field(gt=0)]


class GroundPoint(BaseModel, frozen=True):
    lon: Longitude
    lat: Latitude


class Position(GroundPoint):
    alt: NonnegativeMeters


class TaskAvoidance(GroundPoint):
    radius_m: Annotated[FiniteFloat, Field(ge=5, le=250)]


class SpatialOptions(BaseModel, frozen=True):
    start: GroundPoint | None = None
    end: GroundPoint | None = None
    avoid: Annotated[tuple[TaskAvoidance, ...], Field(max_length=3)] = ()


class AirspaceCheck(BaseModel, frozen=True):
    status: Literal["pending", "source_checked"] = "pending"
    detail: str = "当前使用场景空域规则，外部空域数据待核验。"
    source_name: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    valid_until: datetime | None = None


class Waypoint(Position):
    """Phase describes the segment arriving at this waypoint."""

    phase: FlightPhase


class RoutePlan(BaseModel):
    waypoints: list[Waypoint]
    distance_m: NonnegativeMeters


class CruiseLimits(BaseModel, frozen=True):
    min_alt: NonnegativeMeters | None = None
    max_alt: NonnegativeMeters | None = None
    min_exclusive: bool = False
    max_exclusive: bool = False

    def allows(self, altitude: float) -> bool:
        if self.min_alt is not None and (altitude < self.min_alt or self.min_exclusive and altitude == self.min_alt):
            return False
        if self.max_alt is not None and (altitude > self.max_alt or self.max_exclusive and altitude == self.max_alt):
            return False
        return True


class DispatchConstraints(BaseModel, frozen=True):
    cruise: CruiseLimits | None = None
    deadline_seconds: NonnegativeMeters | None = None
    preferred_cruise: CruiseLimits | None = None
    preferred_deadline_seconds: NonnegativeMeters | None = None


class DispatchRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    scene_version: str | None = None
    clarification_token: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = None
    spatial: SpatialOptions | None = None


class DispatchCandidate(BaseModel):
    mission_type: MissionType
    start_id: NonemptyString | None = None
    end_id: NonemptyString | None = None
    avoid_zone_ids: tuple[str, ...] = ()
    priority: Literal["normal", "urgent"] = "normal"
    prefer_high_altitude: bool = False


class DispatchIntent(DispatchCandidate):
    start_id: NonemptyString
    end_id: NonemptyString
    constraints: DispatchConstraints = Field(default_factory=DispatchConstraints)


class ReplanRequest(BaseModel):
    current_pos: Position
    target_id: NonemptyString
    emergency_type: EmergencyType
    mission_type: MissionType | None = None
    dynamic_hazard: DynamicHazard | None = None
    active_hazards: Annotated[tuple[DynamicHazard, ...], Field(max_length=20)] = ()
    avoid_zone_ids: Annotated[tuple[str, ...], Field(max_length=20)] = ()
    scene_version: str | None = None
    cruise_limits: CruiseLimits | None = None
    remaining_deadline_seconds: FiniteFloat | None = None
    mission_token: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = None

    @model_validator(mode="after")
    def check_hazard(self) -> ReplanRequest:
        if self.emergency_type != "low_battery" and self.dynamic_hazard is None:
            raise ValueError("dynamic_hazard is required for wind_gust and temp_nofly")
        return self


class ThoughtStep(BaseModel):
    step: int
    role: Literal["thought", "action", "observation"]
    content: str


class RouteKPIs(BaseModel):
    distance_m: NonnegativeMeters
    estimated_seconds: NonnegativeMeters
    ground_estimated_seconds: NonnegativeMeters
    time_saving_percent: FiniteFloat | None
    min_clearance_m: NonnegativeMeters | None
    compliant: bool
    compliance_percent: Annotated[FiniteFloat, Field(ge=0, le=100)]


class AssessedRoute(RoutePlan):
    kpis: RouteKPIs
    flight_seconds: NonnegativeMeters


class MissionResult(BaseModel):
    airspace_check: AirspaceCheck | None = None
    corridor: MissionType
    waypoints: list[Waypoint]
    kpis: RouteKPIs
    thought_steps: list[ThoughtStep]
    scene_version: str | None = None


class DispatchResult(MissionResult):
    status: Literal["planned"] = "planned"
    mode: LLMMode
    model_called: bool = False
    model_fallback: bool = False
    intent: DispatchIntent
    start_name: str
    end_name: str
    mission_token: str | None = None
    task_sites: tuple[TaskSite, ...] = ()
    task_zones: tuple[HazardFeature, ...] = ()


class DispatchNotice(BaseModel):
    status: DispatchNoticeType
    detail: str
    thought_steps: list[ThoughtStep]
    mode: LLMMode | None = None
    model_called: bool = False
    clarification_token: str | None = None
    scene_version: str | None = None
    airspace_check: AirspaceCheck | None = None


class HazardPolygon(BaseModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]]


class HazardFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    properties: NoFlyProperties
    geometry: HazardPolygon


class TaskSite(BaseModel, frozen=True):
    id: str
    name: str
    position: Position
    reference_name: str
    offset_m: NonnegativeMeters


class SpatialRouteOption(BaseModel):
    id: str
    start: TaskSite
    end: TaskSite
    corridor: MissionType
    waypoints: list[Waypoint]
    kpis: RouteKPIs
    airspace_check: AirspaceCheck
    recommendation: str


class SpatialChoices(BaseModel):
    status: Literal["choose_endpoints"] = "choose_endpoints"
    detail: str
    candidate_token: str
    scene_version: str | None = None
    mode: LLMMode
    model_called: bool
    thought_steps: list[ThoughtStep]
    options: list[SpatialRouteOption]
    rejected: dict[str, int]
    task_zones: tuple[HazardFeature, ...] = ()


class ConfirmSpatialRequest(BaseModel):
    candidate_token: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    option_id: Annotated[str, StringConstraints(min_length=1, max_length=24)]
    scene_version: str | None = None


class ReplanResult(MissionResult):
    emergency_type: EmergencyType
    original_target_id: str
    target_id: str
    temporary_nofly_zone: HazardFeature | None = None
