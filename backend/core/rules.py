"""Normalize an operator-supplied airspace snapshot against the scene datum."""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, FiniteFloat, HttpUrl, TypeAdapter, model_validator

from backend.schemas import AirspaceCheck, GroundPoint, Latitude, Longitude


class RuleSource(BaseModel, frozen=True):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    url: HttpUrl
    version: Annotated[str, Field(min_length=1, max_length=80)]
    horizontal_datum: Literal["EPSG:4326"]
    vertical_datum: Literal["EGM96", "scene_relative"]
    reference_elevation_m: FiniteFloat | None = None
    coverage_bbox: tuple[Longitude, Latitude, Longitude, Latitude]
    valid_from: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def check_order(self):
        west, south, east, north = self.coverage_bbox
        if west >= east or south >= north or self.valid_from >= self.valid_until:
            raise ValueError("External airspace coverage and validity must be ordered")
        return self

    def check(self, points: list[GroundPoint], seconds: float = 0) -> AirspaceCheck:
        now = datetime.now(timezone.utc)
        west, south, east, north = self.coverage_bbox
        covered = bool(points) and all(west <= p.lon <= east and south <= p.lat <= north for p in points)
        valid = self.valid_from <= now and now + timedelta(seconds=seconds) <= self.valid_until
        detail = ("已按有效的外部空域快照复核划定范围；运行授权需另行核验。" if covered and valid
                  else "外部快照不覆盖该位置或时段，仍待核验。已导入围栏继续作为仿真限制。")
        return AirspaceCheck(
            status="source_checked" if covered and valid else "pending", detail=detail,
            source_name=self.name, source_url=str(self.url), source_version=self.version, valid_until=self.valid_until,
        )


def normalize_rules(collection: dict, terrain: dict | None) -> tuple[RuleSource, list[dict]]:
    source = RuleSource.model_validate(collection["rule_source"])
    if collection["type"] != "FeatureCollection" or not isinstance(collection["features"], list):
        raise ValueError("Expected an external airspace FeatureCollection")
    reference = (terrain or {}).get("reference_elevation_m")
    if source.vertical_datum == "EGM96":
        if reference is None or terrain.get("source_vertical_datum") != "EGM96":
            raise ValueError("EGM96 rules require an EGM96 terrain reference")
        offset = reference
    else:
        if source.reference_elevation_m != reference:
            raise ValueError("The external rules use a different scene height reference")
        offset = 0

    normalized = []
    timestamp = TypeAdapter(AwareDatetime)
    for feature in collection["features"]:
        props = feature["properties"]
        low = float(props["min_alt"]) - offset
        high = float(props["max_alt"]) - offset
        if not low < high:
            raise ValueError("External rule height range is invalid")
        effective_from = timestamp.validate_python(props.get("effective_from", source.valid_from))
        effective_until = timestamp.validate_python(props.get("effective_until", source.valid_until))
        if effective_from >= effective_until:
            raise ValueError("External rule effective period is invalid")
        if high < 0:
            continue
        normalized.append({
            **feature,
            "properties": {
                **props, "id": "EXT-" + props["id"], "min_alt": max(0, low), "max_alt": max(.001, high),
                "source_kind": "external", "source_name": source.name, "source_version": source.version,
                "effective_from": effective_from.isoformat(), "effective_until": effective_until.isoformat(),
            },
        })
    return source, normalized
