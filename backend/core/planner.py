"""Three-dimensional voxel A* with checked terminal segments and line-of-sight smoothing."""

from heapq import heapify, heappop, heappush
from itertools import pairwise, product
from math import dist, inf, sqrt

import numpy as np

from backend.config import (
    CEILING_M,
    CLEARANCE_COST_FACTOR,
    GROUND_LAYER,
    LAYER_DEVIATION_COST_FACTOR,
    MISSION_LAYERS,
    STUDY_BOUNDS,
    AirspaceLayer,
)
from backend.core.airspace import Airspace, LocalPoint, Obstacle, ObstacleField, Vertiport, Voxel
from backend.schemas import CruiseLimits, DynamicHazard, FlightPhase, MissionType, Position, RoutePlan, Waypoint


class NoRouteError(ValueError):
    """A compliant route could not be found at the configured resolution."""


class RoutePlanner:
    def __init__(self, airspace: Airspace) -> None:
        self.airspace = airspace
        self.grid = airspace.grid
        xy2, z2 = self.grid.xy_size**2, self.grid.z_size**2
        self.moves = tuple(
            (dx, dy, dz, sqrt((dx * dx + dy * dy) * xy2 + dz * dz * z2))
            for dx, dy, dz in product((-1, 0, 1), repeat=3)
            if dx or dy or dz
        )

    def plan_3d_route(
        self,
        start: str | Vertiport | Position,
        end_id: str | Vertiport,
        mission_type: MissionType,
        extra_nofly_zones: tuple[DynamicHazard, ...] = (),
        *,
        avoid_zone_ids: tuple[str, ...] = (),
        cruise_limits: CruiseLimits | None = None,
        via: tuple[Position, ...] = (),
    ) -> RoutePlan:
        """Plan from a registered or validated task pad, or an airborne WGS84 position."""
        if mission_type not in MISSION_LAYERS:
            raise ValueError(f"Unknown mission type: {mission_type}")
        layer = MISSION_LAYERS[mission_type]
        end_pad = self._pad(end_id) if isinstance(end_id, str) else end_id
        origin_is_pad = not isinstance(start, Position)
        if origin_is_pad:
            start_pad = self._pad(start) if isinstance(start, str) else start
            if not via and (start_pad.properties.id == end_pad.properties.id or start_pad.position == end_pad.position):
                raise NoRouteError("Origin and destination vertiports must be different")
            origin = start_pad.position
            origin_roof = start_pad.roof_obstacle
        else:
            origin = start
            origin_roof = None
            if origin.alt < GROUND_LAYER.max_alt:
                raise NoRouteError("An airborne origin must be at or above 30 m")

        min_lon, min_lat, max_lon, max_lat = STUDY_BOUNDS
        places = (origin, *via, end_pad.position)
        for position in places:
            if not (min_lon <= position.lon <= max_lon and min_lat <= position.lat <= max_lat):
                raise NoRouteError("Route endpoints must be inside the study area")
        frame = self.airspace.frame
        anchors = tuple(frame.to_local(place) for place in places)
        west, south, east, north = self.grid.bounds
        for x, y, z in anchors:
            if not (west <= x <= east and south <= y <= north and 0 <= z <= CEILING_M):
                raise NoRouteError("Route endpoints must be inside the planning volume")

        levels = self.cruise_levels(layer, cruise_limits)
        if not levels:
            raise NoRouteError("No grid levels satisfy the cruise altitude limits")
        field, blocked = self.airspace.with_hazards(extra_nofly_zones, avoid_zone_ids)
        points: list[tuple[LocalPoint, FlightPhase]] = []
        visits = []
        for index, (start_point, end_point) in enumerate(pairwise(anchors)):
            first = index == 0
            terminal = index == len(anchors) - 2
            if via and start_point[:2] == end_point[:2]:
                raise NoRouteError("Consecutive route locations must be different")
            departure_pad = first and origin_is_pad
            starts = self._attachments(start_point, levels, field, blocked, origin_roof if first else None, departure_pad)
            goals = self._attachments(end_point, levels, field, blocked, end_pad.roof_obstacle if terminal else None, terminal)
            if not starts or not goals:
                raise NoRouteError("No safe connection between a route location and its mission layer")

            voxels = self._search(starts, goals, end_point, levels, blocked, layer)
            centers = [self.grid.center(voxel) for voxel in voxels]
            departure = (*start_point[:2], centers[0][2])
            arrival = (*end_point[:2], centers[-1][2])
            cruise = self._smooth([departure, *centers, arrival], field)
            if first:
                phase: FlightPhase = "takeoff" if departure_pad else "transition" if start_point != cruise[0] else "cruise"
                points.append((start_point, phase))
            if start_point != cruise[0]:
                points.append((cruise[0], "takeoff" if departure_pad else "transition"))
            points.extend((point, "cruise") for point in cruise[1:])
            if points[-1][0] != end_point:
                points.append((end_point, "landing" if terminal else "cruise"))
            if not terminal:
                visits.append(len(points) - 1)

        exact = {anchor[:2]: place for anchor, place in zip(anchors, places, strict=True)}
        waypoints = []
        for point, phase in points:
            position = exact.get(point[:2]) or frame.to_position(point)
            waypoints.append(Waypoint(lon=position.lon, lat=position.lat, alt=point[2], phase=phase))
        length = sum(dist(a, b) for (a, _), (b, _) in pairwise(points))
        return RoutePlan(waypoints=waypoints, distance_m=length, visit_indices=tuple(visits))

    def cruise_levels(self, layer: AirspaceLayer, limits: CruiseLimits | None = None) -> tuple[int, ...]:
        return tuple(
            index for index in range(self.grid.shape[2])
            if layer.min_alt <= (altitude := self.grid.center((0, 0, index))[2]) <= layer.max_alt
            and (limits is None or limits.allows(altitude))
        )

    def _pad(self, pad_id: str) -> Vertiport:
        try:
            return self.airspace.vertiports[pad_id]
        except KeyError as exc:
            raise ValueError(f"Unknown vertiport ID: {pad_id}") from exc

    def _attachments(
        self,
        endpoint: LocalPoint,
        levels: tuple[int, ...],
        field: ObstacleField,
        blocked: np.ndarray,
        roof: Obstacle | None,
        is_pad: bool,
    ) -> dict[Voxel, float]:
        costs = {}
        for k in levels:
            z = self.grid.center((0, 0, k))[2]
            if is_pad and z <= endpoint[2]:
                continue
            anchor = (*endpoint[:2], z)
            if not field.segment_clear(endpoint, anchor, roof_access=roof):
                continue
            for voxel in self.grid.nearby(anchor, k):
                if blocked[voxel]:
                    continue
                center = self.grid.center(voxel)
                if field.segment_clear(anchor, center):
                    costs[voxel] = abs(endpoint[2] - z) + dist(anchor, center)
        return costs

    def _search(
        self,
        starts: dict[Voxel, float],
        goals: dict[Voxel, float],
        end: LocalPoint,
        levels: tuple[int, ...],
        blocked: np.ndarray,
        layer: AirspaceLayer,
    ) -> list[Voxel]:
        nx, ny, _ = self.grid.shape
        west, south, _, _ = self.grid.bounds
        xy, dz = self.grid.xy_size, self.grid.z_size
        lowest, highest = levels[0], levels[-1]
        altitude_cost = [
            1 + LAYER_DEVIATION_COST_FACTOR * abs((k + 0.5) * dz - layer.midpoint)
            / (layer.max_alt - layer.midpoint)
            for k in range(highest + 1)
        ]

        padded = np.pad(blocked, 1)
        near_obstacle = (
            padded[:-2, 1:-1, 1:-1] | padded[2:, 1:-1, 1:-1]
            | padded[1:-1, :-2, 1:-1] | padded[1:-1, 2:, 1:-1]
            | padded[1:-1, 1:-1, :-2] | padded[1:-1, 1:-1, 2:]
        ).tolist()
        occupied = blocked.tolist()
        east_sq = [(west + (i + 0.5) * xy - end[0]) ** 2 for i in range(nx)]
        north_sq = [(south + (j + 0.5) * xy - end[1]) ** 2 for j in range(ny)]
        up_sq = [((k + 0.5) * dz - end[2]) ** 2 for k in range(highest + 1)]

        costs = starts.copy()
        parents: dict[Voxel, Voxel] = {}
        queue = [
            (g + sqrt(east_sq[i] + north_sq[j] + up_sq[k]), -g, (i, j, k))
            for (i, j, k), g in starts.items()
        ]
        heapify(queue)
        best_cost, best_goal = inf, None
        while queue:
            estimate, neg_g, voxel = heappop(queue)
            if estimate >= best_cost:
                break
            g = -neg_g
            if g != costs[voxel]:
                continue
            if voxel in goals and g + goals[voxel] < best_cost:
                best_cost, best_goal = g + goals[voxel], voxel

            i, j, k = voxel
            for dx, dy, dk, length in self.moves:
                x, y, z = i + dx, j + dy, k + dk
                if not (0 <= x < nx and 0 <= y < ny and lowest <= z <= highest):
                    continue
                if occupied[x][y][z]:
                    continue
                # Adjacent center-to-center edges lie entirely inside their two closed voxels.
                neighbor = (x, y, z)
                penalty = altitude_cost[z] + CLEARANCE_COST_FACTOR * near_obstacle[x][y][z]
                new_g = g + length * penalty
                if new_g >= costs.get(neighbor, inf):
                    continue
                costs[neighbor], parents[neighbor] = new_g, voxel
                estimate = new_g + sqrt(east_sq[x] + north_sq[y] + up_sq[z])
                heappush(queue, (estimate, -new_g, neighbor))

        if best_goal is None:
            raise NoRouteError("No collision-free voxel path in the mission layer")
        path = [best_goal]
        while path[-1] in parents:
            path.append(parents[path[-1]])
        return path[::-1]

    @staticmethod
    def _smooth(points: list[LocalPoint], field: ObstacleField) -> list[LocalPoint]:
        """Retain the farthest continuously visible waypoint at each step."""
        smoothed = [points[0]]
        i = 0
        while i < len(points) - 1:
            for j in range(len(points) - 1, i, -1):
                if field.segment_clear(points[i], points[j]):
                    if points[j] != smoothed[-1]:
                        smoothed.append(points[j])
                    i = j
                    break
            else:
                raise NoRouteError("Voxel path failed the continuous collision check")
        return smoothed
