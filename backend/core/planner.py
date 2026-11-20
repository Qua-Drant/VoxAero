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
from backend.schemas import DynamicHazard, FlightPhase, MissionType, Position, RoutePlan, Waypoint


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
        start: str | Position,
        end_id: str,
        mission_type: MissionType,
        extra_nofly_zones: tuple[DynamicHazard, ...] = (),
    ) -> RoutePlan:
        """Plan from a vertiport ID or an airborne WGS84 position to a vertiport."""
        if mission_type not in MISSION_LAYERS:
            raise ValueError(f"Unknown mission type: {mission_type}")
        if start == end_id:
            raise ValueError("Origin and destination vertiports must be different")
        layer = MISSION_LAYERS[mission_type]
        end_pad = self._pad(end_id)
        origin_is_pad = isinstance(start, str)
        if origin_is_pad:
            start_pad = self._pad(start)
            origin = start_pad.position
            origin_roof = start_pad.roof_obstacle
        else:
            origin = start
            origin_roof = None
            if origin.alt < GROUND_LAYER.max_alt:
                raise NoRouteError("An airborne origin must be at or above 30 m")

        min_lon, min_lat, max_lon, max_lat = STUDY_BOUNDS
        for position in (origin, end_pad.position):
            if not (min_lon <= position.lon <= max_lon and min_lat <= position.lat <= max_lat):
                raise NoRouteError("Route endpoints must be inside the study area")
        frame = self.airspace.frame
        start_point = frame.to_local(origin)
        end_point = frame.to_local(end_pad.position)
        west, south, east, north = self.grid.bounds
        for x, y, z in (start_point, end_point):
            if not (west <= x <= east and south <= y <= north and 0 <= z <= CEILING_M):
                raise NoRouteError("Route endpoints must be inside the planning volume")

        field, blocked = self.airspace.with_hazards(extra_nofly_zones)
        levels = tuple(
            k
            for k in range(self.grid.shape[2])
            if layer.min_alt <= self.grid.center((0, 0, k))[2] <= layer.max_alt
        )
        starts = self._attachments(start_point, levels, field, blocked, origin_roof, origin_is_pad)
        goals = self._attachments(end_point, levels, field, blocked, end_pad.roof_obstacle, True)
        if not starts or not goals:
            raise NoRouteError("No safe connection between an endpoint and its mission layer")

        voxels = self._search(starts, goals, end_point, levels, blocked, layer)
        centers = [self.grid.center(voxel) for voxel in voxels]
        departure = (*start_point[:2], centers[0][2])
        arrival = (*end_point[:2], centers[-1][2])
        cruise = self._smooth([departure, *centers, arrival], field)

        initial_phase: FlightPhase = "takeoff" if origin_is_pad else "transition"
        points: list[tuple[LocalPoint, FlightPhase]] = [(start_point, initial_phase)]
        if start_point != cruise[0]:
            points.append((cruise[0], initial_phase))
        elif not origin_is_pad:
            points[0] = (start_point, "cruise")
        points.extend((point, "cruise") for point in cruise[1:])
        if points[-1][0] != end_point:
            points.append((end_point, "landing"))

        waypoints = []
        for point, phase in points:
            if point[:2] == start_point[:2]:
                lon, lat = origin.lon, origin.lat
            elif point[:2] == end_point[:2]:
                lon, lat = end_pad.position.lon, end_pad.position.lat
            else:
                position = frame.to_position(point)
                lon, lat = position.lon, position.lat
            waypoints.append(Waypoint(lon=lon, lat=lat, alt=point[2], phase=phase))
        length = sum(dist(a, b) for (a, _), (b, _) in pairwise(points))
        return RoutePlan(waypoints=waypoints, distance_m=length)

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
