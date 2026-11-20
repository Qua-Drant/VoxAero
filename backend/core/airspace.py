"""Load WGS84 airspace and model its obstacles in a local ENU grid."""

import json
from dataclasses import dataclass
from math import atan2, ceil, cos, degrees, dist, floor, hypot, pi, radians, sin, sqrt
from pathlib import Path
from typing import Iterator

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.strtree import STRtree

from backend.config import (
    BUILDING_BUFFER_M,
    CEILING_M,
    DATA_DIR,
    HORIZONTAL_RESOLUTION_M,
    ORIGIN_LON_LAT,
    STUDY_BOUNDS,
    VERTICAL_RESOLUTION_M,
)
from backend.schemas import (
    BuildingProperties,
    DynamicHazard,
    NoFlyProperties,
    Position,
    VertiportProperties,
)

LocalPoint = tuple[float, float, float]
Voxel = tuple[int, int, int]
Bounds = tuple[float, float, float, float]

WGS84_A = 6_378_137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
WGS84_AXES = np.array([WGS84_A, WGS84_A, WGS84_A * (1 - WGS84_F)])


class LocalENU:
    """WGS84 east/north from the tangent frame with height above the modeled flat ground."""

    def __init__(self, lon: float, lat: float) -> None:
        lam, phi = radians(lon), radians(lat)
        self.origin = self._surface_ecef(np.array([[lon, lat]]))[0]
        self.east = np.array([-sin(lam), cos(lam), 0.0])
        self.north = np.array([-sin(phi) * cos(lam), -sin(phi) * sin(lam), cos(phi)])
        self.up = np.array([cos(phi) * cos(lam), cos(phi) * sin(lam), sin(phi)])
        self.horizontal_axes = np.stack((self.east, self.north), axis=1)

    @staticmethod
    def _surface_ecef(lon_lat: np.ndarray) -> np.ndarray:
        lam, phi = np.radians(lon_lat).T
        normal_radius = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(phi) ** 2)
        return np.column_stack((
            normal_radius * np.cos(phi) * np.cos(lam),
            normal_radius * np.cos(phi) * np.sin(lam),
            normal_radius * (1 - WGS84_E2) * np.sin(phi),
        ))

    def project_xy(self, lon_lat: np.ndarray) -> np.ndarray:
        return (self._surface_ecef(lon_lat) - self.origin) @ self.horizontal_axes

    def to_local(self, position: Position) -> LocalPoint:
        east, north = self.project_xy(np.array([[position.lon, position.lat]]))[0]
        return float(east), float(north), position.alt

    def to_position(self, point: LocalPoint) -> Position:
        east, north, height = point
        tangent = self.origin + self.east * east + self.north * north
        scaled_point = tangent / WGS84_AXES
        scaled_up = self.up / WGS84_AXES
        # Intersect the line along local up with the WGS84 ellipsoid near the origin.
        a = float(scaled_up @ scaled_up)
        b = float(scaled_point @ scaled_up)
        c = float(scaled_point @ scaled_point) - 1
        x, y, z = tangent - self.up * c / (b + sqrt(b * b - a * c))
        return Position(
            lon=degrees(atan2(y, x)),
            lat=degrees(atan2(z, hypot(x, y) * (1 - WGS84_E2))),
            alt=height,
        )


@dataclass(frozen=True)
class Obstacle:
    id: str
    footprint: Polygon
    min_alt: float
    max_alt: float

    @classmethod
    def from_hazard(cls, hazard: DynamicHazard, frame: LocalENU, index: int) -> "Obstacle":
        center = frame.to_local(Position(lon=hazard.center_lon, lat=hazard.center_lat, alt=0))
        # Circumscribe the requested circle so the polygon cannot miss its edge.
        quad_segs = 24
        radius = hazard.radius_m / cos(pi / (4 * quad_segs))
        return cls(
            f"dynamic-{index}",
            Point(center[:2]).buffer(radius, quad_segs=quad_segs),
            hazard.min_alt,
            hazard.max_alt,
        )


class ObstacleField:
    def __init__(self, obstacles: tuple[Obstacle, ...]) -> None:
        self.obstacles = obstacles
        self.index = STRtree([obstacle.footprint for obstacle in obstacles])

    def segment_clear(
        self,
        start: LocalPoint,
        end: LocalPoint,
        *,
        roof_access: Obstacle | None = None,
    ) -> bool:
        """Check a continuous segment against the closed 3D obstacle volumes."""
        vertical = start[:2] == end[:2]
        projected = Point(start[:2]) if vertical else LineString([start[:2], end[:2]])
        z0, z1 = start[2], end[2]
        for index in self.index.query(projected, predicate="intersects"):
            obstacle = self.obstacles[index]
            if vertical and obstacle is roof_access and min(z0, z1) + BUILDING_BUFFER_M >= obstacle.max_alt:
                continue
            if max(z0, z1) < obstacle.min_alt or min(z0, z1) > obstacle.max_alt:
                continue
            if vertical or z0 == z1:
                return False

            # A sloped segment can pass the footprint above or below the obstacle.
            dz = z1 - z0
            t0, t1 = sorted(((obstacle.min_alt - z0) / dz, (obstacle.max_alt - z0) / dz))
            dx, dy = end[0] - start[0], end[1] - start[1]
            a, b = max(0.0, t0), min(1.0, t1)
            entry = (start[0] + a * dx, start[1] + a * dy)
            exit = (start[0] + b * dx, start[1] + b * dy)
            overlap = Point(entry) if entry == exit else LineString([entry, exit])
            if obstacle.footprint.intersects(overlap):
                return False
        return True

    def segment_clearance(
        self, start: LocalPoint, end: LocalPoint, *, roof_access: Obstacle | None = None
    ) -> float:
        """Return a lower bound on distance to the buffered obstacle volumes, in meters."""
        vertical = start[:2] == end[:2]
        trace = Point(start[:2]) if vertical else LineString([start[:2], end[:2]])
        low, high = sorted((start[2], end[2]))
        spacing = 0.0
        if not vertical and low != high:
            length = dist(start, end)
            divisions = ceil(length / 2)
            spacing = length / divisions
            positions = np.linspace(start, end, divisions + 1)
            samples = shapely.points(positions[:, :2])
            heights = positions[:, 2]

        best = float("inf")
        for obstacle in self.obstacles:
            if vertical and obstacle is roof_access:
                continue
            horizontal = trace.distance(obstacle.footprint)
            vertical_gap = max(obstacle.min_alt - high, low - obstacle.max_alt, 0)
            lower_bound = hypot(horizontal, vertical_gap)
            if lower_bound >= best:
                continue
            if spacing:
                xy = shapely.distance(samples, obstacle.footprint)
                z = np.maximum(np.maximum(obstacle.min_alt - heights, heights - obstacle.max_alt), 0)
                # Distance to a volume is 1-Lipschitz along the segment.
                lower_bound = max(lower_bound, float(np.hypot(xy, z).min()) - spacing / 2)
            best = min(best, lower_bound)
        return best


@dataclass(frozen=True)
class Vertiport:
    properties: VertiportProperties
    position: Position
    roof_obstacle: Obstacle | None


def _features(
    document: dict, layer: str, geometry_type: str
) -> Iterator[tuple[dict, Point | Polygon]]:
    if (
        not isinstance(document, dict)
        or document.get("type") != "FeatureCollection"
        or not isinstance(document.get("features"), list)
    ):
        raise ValueError(f"{layer} must be a GeoJSON FeatureCollection")
    seen = set()
    for feature in document["features"]:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"{layer} contains an invalid GeoJSON Feature")
        raw_geometry = feature.get("geometry")
        if not isinstance(raw_geometry, dict) or raw_geometry.get("type") != geometry_type:
            raise ValueError(f"{layer} features must have {geometry_type} geometry")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"{layer} features must have a properties object")
        feature_id = properties.get("id")
        if not isinstance(feature_id, str) or not feature_id or feature_id in seen:
            raise ValueError(f"{layer} feature IDs must be nonempty and unique")
        seen.add(feature_id)
        geometry = shape(raw_geometry)
        if geometry.is_empty or not geometry.is_valid or shapely.get_coordinate_dimension(geometry) != 2:
            raise ValueError(f"{layer} contains an invalid 2D geometry for {feature_id}")
        min_lon, min_lat, max_lon, max_lat = geometry.bounds
        if not (-180 <= min_lon <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
            raise ValueError(f"{layer} has coordinates outside WGS84 bounds for {feature_id}")
        yield properties, geometry


class VoxelGrid:
    """A grid of closed voxel volumes with obstacle masks in east/north/up order."""

    def __init__(self, bounds: Bounds) -> None:
        self.bounds = bounds
        self.xy_size = HORIZONTAL_RESOLUTION_M
        self.z_size = VERTICAL_RESOLUTION_M
        west, south, east, north = bounds
        self.shape = (
            floor((east - west) / self.xy_size + 0.5),
            floor((north - south) / self.xy_size + 0.5),
            ceil(CEILING_M / self.z_size),
        )

    def center(self, voxel: Voxel) -> LocalPoint:
        i, j, k = voxel
        west, south, _, _ = self.bounds
        return (
            west + (i + 0.5) * self.xy_size,
            south + (j + 0.5) * self.xy_size,
            (k + 0.5) * self.z_size,
        )

    def nearby(self, point: LocalPoint, level: int) -> Iterator[Voxel]:
        west, south, _, _ = self.bounds
        i = floor((point[0] - west) / self.xy_size)
        j = floor((point[1] - south) / self.xy_size)
        for x in range(max(0, i - 1), min(self.shape[0], i + 2)):
            for y in range(max(0, j - 1), min(self.shape[1], j + 2)):
                yield x, y, level

    def rasterize(self, obstacles: tuple[Obstacle, ...]) -> np.ndarray:
        blocked = np.zeros(self.shape, dtype=bool)
        west, south, _, _ = self.bounds
        levels = np.arange(self.shape[2]) * self.z_size
        for obstacle in obstacles:
            min_x, min_y, max_x, max_y = obstacle.footprint.bounds
            x0 = max(0, floor((min_x - west) / self.xy_size) - 1)
            x1 = min(self.shape[0], floor((max_x - west) / self.xy_size) + 1)
            y0 = max(0, floor((min_y - south) / self.xy_size) - 1)
            y1 = min(self.shape[1], floor((max_y - south) / self.xy_size) + 1)
            height_hits = (levels <= obstacle.max_alt) & (levels + self.z_size >= obstacle.min_alt)
            if x0 >= x1 or y0 >= y1 or not height_hits.any():
                continue
            x = west + np.arange(x0, x1)[:, None] * self.xy_size
            y = south + np.arange(y0, y1)[None, :] * self.xy_size
            cells = shapely.box(x, y, x + self.xy_size, y + self.xy_size)
            footprint_hits = shapely.intersects(obstacle.footprint, cells)
            blocked[x0:x1, y0:y1] |= footprint_hits[..., None] & height_hits
        return blocked


class Airspace:
    def __init__(
        self,
        frame: LocalENU,
        bounds: Bounds,
        field: ObstacleField,
        vertiports: dict[str, Vertiport],
        geojson: dict[str, dict] | None = None,
    ) -> None:
        self.frame = frame
        self.grid = VoxelGrid(bounds)
        self.field = field
        self.vertiports = vertiports
        self.geojson = {} if geojson is None else geojson
        self.static_voxels = self.grid.rasterize(field.obstacles)
        self.static_voxels.flags.writeable = False

    @classmethod
    def from_directory(cls, directory: Path = DATA_DIR) -> "Airspace":
        frame = LocalENU(*ORIGIN_LON_LAT)
        west, south, east, north = STUDY_BOUNDS
        bounds = shapely.transform(shapely.box(west, south, east, north), frame.project_xy).bounds
        layers = {
            key: json.loads((directory / filename).read_text(encoding="utf-8"))
            for key, filename in (
                ("buildings", "whu_buildings.geojson"),
                ("vertiports", "vertiports.geojson"),
                ("nofly_zones", "nofly_zones.geojson"),
            )
        }
        buildings = []
        for data, geometry in _features(layers["buildings"], "whu_buildings.geojson", "Polygon"):
            properties = BuildingProperties.model_validate(data)
            footprint = shapely.transform(geometry, frame.project_xy)
            obstacle = Obstacle(
                properties.id, footprint.buffer(BUILDING_BUFFER_M), 0, properties.height + BUILDING_BUFFER_M
            )
            buildings.append((properties.height, footprint, obstacle))

        obstacles = [obstacle for _, _, obstacle in buildings]
        for data, geometry in _features(layers["nofly_zones"], "nofly_zones.geojson", "Polygon"):
            zone = NoFlyProperties.model_validate(data)
            footprint = shapely.transform(geometry, frame.project_xy)
            obstacles.append(Obstacle(zone.id, footprint, zone.min_alt, zone.max_alt))

        vertiports = {}
        for data, geometry in _features(layers["vertiports"], "vertiports.geojson", "Point"):
            properties = VertiportProperties.model_validate(data)
            position = Position(lon=geometry.x, lat=geometry.y, alt=properties.altitude)
            east, north, _ = frame.to_local(position)
            pad_point = Point(east, north)
            roofs = [
                (height, obstacle)
                for height, footprint, obstacle in buildings
                if footprint.covers(pad_point) and 0 <= position.alt - height <= BUILDING_BUFFER_M
            ]
            roof = max(roofs, key=lambda match: match[0])[1] if roofs else None
            vertiports[properties.id] = Vertiport(properties, position, roof)

        return cls(frame, bounds, ObstacleField(tuple(obstacles)), vertiports, layers)

    def with_hazards(
        self, hazards: tuple[DynamicHazard, ...], avoid_zone_ids: tuple[str, ...] = ()
    ) -> tuple[ObstacleField, np.ndarray]:
        if not hazards and not avoid_zone_ids:
            return self.field, self.static_voxels
        dynamic = tuple(
            Obstacle.from_hazard(hazard, self.frame, index)
            for index, hazard in enumerate(hazards, start=1)
        )
        static_by_id = {obstacle.id: obstacle for obstacle in self.field.obstacles}
        full_height = tuple(
            Obstacle(zone_id, static_by_id[zone_id].footprint, 0, CEILING_M)
            for zone_id in dict.fromkeys(avoid_zone_ids)
            if static_by_id[zone_id].min_alt > 0 or static_by_id[zone_id].max_alt < CEILING_M
        )
        extra = dynamic + full_height
        if not extra:
            return self.field, self.static_voxels
        field = ObstacleField(self.field.obstacles + extra)
        blocked = self.static_voxels | self.grid.rasterize(extra)
        return field, blocked
