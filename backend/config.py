"""Spatial settings for the Wuhan University prototype."""

from dataclasses import dataclass
from pathlib import Path

from backend.schemas import MissionType

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
STUDY_BOUNDS = (114.349, 30.524, 114.362, 30.533)
ORIGIN_LON_LAT = (114.3555, 30.5285)

HORIZONTAL_RESOLUTION_M = 18.0
VERTICAL_RESOLUTION_M = 10.0
CEILING_M = 100.0
BUILDING_BUFFER_M = 5.0
CLEARANCE_COST_FACTOR = 0.25
LAYER_DEVIATION_COST_FACTOR = 0.06


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
