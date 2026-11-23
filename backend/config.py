"""Spatial settings for the Wuhan University prototype."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from backend.schemas import LLMMode, MissionType

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"
FRONTEND_DIR = PROJECT_DIR / "frontend"
STUDY_BOUNDS = (114.349, 30.524, 114.362, 30.533)
ORIGIN_LON_LAT = (114.3555, 30.5285)

HORIZONTAL_RESOLUTION_M = 18.0
VERTICAL_RESOLUTION_M = 10.0
CEILING_M = 100.0
BUILDING_BUFFER_M = 5.0
CLEARANCE_COST_FACTOR = 0.25
LAYER_DEVIATION_COST_FACTOR = 0.06
BATTERY_PERCENT_PER_SECOND = 0.10
BATTERY_PERCENT_PER_CLIMB_M = 0.02
BATTERY_RESERVE_PERCENT = 2.0


@dataclass(frozen=True)
class AirspaceLayer:
    id: str
    name: str
    min_alt: float
    max_alt: float

    @property
    def midpoint(self) -> float:
        return (self.min_alt + self.max_alt) / 2


GROUND_LAYER = AirspaceLayer("ground", "地面干扰与树冠缓冲区", 0.0, 30.0)
DELIVERY_LAYER = AirspaceLayer("delivery", "物流配送主航道层", 30.0, 60.0)
INSPECTION_LAYER = AirspaceLayer("inspection", "安防巡检与高速通道层", 60.0, 90.0)
AIRSPACE_LAYERS = (GROUND_LAYER, DELIVERY_LAYER, INSPECTION_LAYER)
MISSION_LAYERS: dict[MissionType, AirspaceLayer] = {
    "delivery": DELIVERY_LAYER,
    "inspection": INSPECTION_LAYER,
}


@dataclass(frozen=True)
class LLMSettings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.openai.com/v1"
    model: str = ""

    @classmethod
    def from_environment(cls) -> "LLMSettings":
        file_values = dotenv_values(PROJECT_DIR / ".env")
        return cls(
            api_key=(os.getenv("LLM_API_KEY", file_values.get("LLM_API_KEY")) or "").strip(),
            base_url=(os.getenv("LLM_BASE_URL", file_values.get("LLM_BASE_URL")) or "").strip() or cls.base_url,
            model=(os.getenv("LLM_MODEL", file_values.get("LLM_MODEL")) or "").strip(),
        )

    @property
    def mode(self) -> LLMMode:
        return "live_llm" if self.api_key and self.model else "fallback_mock"
