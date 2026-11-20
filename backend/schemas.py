"""GeoJSON properties and route models shared by the spatial modules."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, FiniteFloat, model_validator

Longitude = Annotated[FiniteFloat, Field(ge=-180, le=180)]
Latitude = Annotated[FiniteFloat, Field(ge=-90, le=90)]
NonnegativeMeters = Annotated[FiniteFloat, Field(ge=0)]
NonemptyString = Annotated[str, Field(min_length=1)]

MissionType = Literal["delivery", "inspection"]
FlightPhase = Literal["takeoff", "cruise", "landing", "transition"]


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
