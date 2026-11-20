"""GeoJSON properties and route models shared by the spatial modules."""

from __future__ import annotations

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


class FeatureProperties(BaseModel, frozen=True):
    id: NonemptyString
    name: NonemptyString


class BuildingProperties(FeatureProperties):
    height: Annotated[FiniteFloat, Field(ge=12, le=65)]
    category: Literal["teaching", "lab", "dorm", "canteen", "library"]


class VertiportProperties(FeatureProperties):
    type: Literal["logistics", "inspection", "emergency"]
    altitude: NonnegativeMeters


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


class Position(BaseModel, frozen=True):
    lon: Longitude
    lat: Latitude
    alt: NonnegativeMeters


class Waypoint(Position):
    """Phase describes the segment arriving at this waypoint."""

    phase: FlightPhase


class RoutePlan(BaseModel):
    waypoints: list[Waypoint]
    distance_m: NonnegativeMeters


class DispatchRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class DispatchIntent(BaseModel):
    mission_type: MissionType
    start_id: NonemptyString
    end_id: NonemptyString
    avoid_zone_ids: tuple[str, ...] = ()
    priority: Literal["normal", "urgent"] = "normal"
    prefer_high_altitude: bool = False


class ReplanRequest(BaseModel):
    current_pos: Position
    target_id: NonemptyString
    emergency_type: EmergencyType
    mission_type: MissionType | None = None
    dynamic_hazard: DynamicHazard | None = None
    active_hazards: Annotated[tuple[DynamicHazard, ...], Field(max_length=20)] = ()
    avoid_zone_ids: Annotated[tuple[str, ...], Field(max_length=20)] = ()

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


class MissionResult(BaseModel):
    corridor: MissionType
    waypoints: list[Waypoint]
    kpis: RouteKPIs
    thought_steps: list[ThoughtStep]


class DispatchResult(MissionResult):
    mode: LLMMode
    intent: DispatchIntent
    start_name: str
    end_name: str


class HazardPolygon(BaseModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]]


class HazardFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    properties: NoFlyProperties
    geometry: HazardPolygon


class ReplanResult(MissionResult):
    emergency_type: EmergencyType
    original_target_id: str
    target_id: str
    temporary_nofly_zone: HazardFeature | None = None
