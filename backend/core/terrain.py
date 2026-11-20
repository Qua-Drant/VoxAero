"""Local elevation grid shared by the terrain display and airspace checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from backend.core.airspace import LocalENU, LocalPoint, VoxelGrid

CONTACT_TOLERANCE_M = 0.03


class TerrainSurface:
    def __init__(self, asset: dict, frame: LocalENU) -> None:
        west, south, east, north = asset["bounds"]
        samples = np.asarray(asset["elevations_m"], dtype=float)
        if samples.ndim != 2 or min(samples.shape) < 2 or not np.isfinite(samples).all():
            raise ValueError("Expected a finite two-dimensional terrain height grid")
        self.heights = samples[::-1]
        rows, cols = self.heights.shape
        self.x = frame.project_xy(np.column_stack((
            np.linspace(west, east, cols), np.full(cols, (north + south) / 2),
        )))[:, 0]
        self.y = frame.project_xy(np.column_stack((
            np.full(rows, (west + east) / 2), np.linspace(south, north, rows),
        )))[:, 1]
        self.highest = float(samples.max())

    def elevation(self, east, north):
        east = np.clip(east, self.x[0], self.x[-1])
        north = np.clip(north, self.y[0], self.y[-1])
        i = np.clip(np.searchsorted(self.x, east, side="right") - 1, 0, len(self.x) - 2)
        j = np.clip(np.searchsorted(self.y, north, side="right") - 1, 0, len(self.y) - 2)
        u = (east - self.x[i]) / (self.x[i + 1] - self.x[i])
        v = (north - self.y[j]) / (self.y[j + 1] - self.y[j])
        bottom = (1 - u) * self.heights[j, i] + u * self.heights[j, i + 1]
        top = (1 - u) * self.heights[j + 1, i] + u * self.heights[j + 1, i + 1]
        return (1 - v) * bottom + v * top

    def segment_clear(self, a: LocalPoint, b: LocalPoint) -> bool:
        if min(a[2], b[2]) >= self.highest:
            return True
        if a[:2] == b[:2]:
            return min(a[2], b[2]) >= self.elevation(*a[:2]) - CONTACT_TOLERANCE_M
        delta = np.subtract(b, a)
        crossings = [0.0, 1.0]
        for axis, boundaries in enumerate((self.x, self.y)):
            if delta[axis]:
                fractions = (boundaries - a[axis]) / delta[axis]
                crossings.extend(fractions[(0 < fractions) & (fractions < 1)])
        endpoints = np.unique(crossings)
        middle = (endpoints[:-1] + endpoints[1:]) / 2

        def gap(t):
            x, y, z = (np.asarray(a) + t[:, None] * delta).T
            return z - self.elevation(x, y)

        ends = gap(endpoints)
        if min(ends) < -CONTACT_TOLERANCE_M:
            return False
        # Along a line inside a bilinear cell the height difference is quadratic.
        quadratic = 2 * (ends[:-1] + ends[1:] - 2 * gap(middle))
        linear = ends[1:] - ends[:-1] - quadratic
        convex = quadratic > 0
        vertex = -linear[convex] / (2 * quadratic[convex])
        internal = (vertex > 0) & (vertex < 1)
        if not internal.any():
            return True
        minimum = gap(endpoints[:-1][convex][internal] + vertex[internal] * np.diff(endpoints)[convex][internal])
        return bool((minimum >= -CONTACT_TOLERANCE_M).all())

    def voxel_mask(self, grid: VoxelGrid) -> np.ndarray:
        west, south, _, _ = grid.bounds
        columns = west + np.arange(grid.shape[0] + 1) * grid.xy_size
        rows = south + np.arange(grid.shape[1] + 1) * grid.xy_size
        west_cells = np.clip(np.searchsorted(self.x, columns[:-1], side="right") - 1, 0, len(self.x) - 2)
        east_cells = np.clip(np.searchsorted(self.x, columns[1:]), 1, len(self.x) - 1)
        south_cells = np.clip(np.searchsorted(self.y, rows[:-1], side="right") - 1, 0, len(self.y) - 2)
        north_cells = np.clip(np.searchsorted(self.y, rows[1:]), 1, len(self.y) - 1)
        tops = np.empty(grid.shape[:2])
        for i, (x0, x1) in enumerate(zip(west_cells, east_cells, strict=True)):
            for j, (y0, y1) in enumerate(zip(south_cells, north_cells, strict=True)):
                tops[i, j] = self.heights[y0:y1 + 1, x0:x1 + 1].max()
        return tops[..., None] >= np.arange(grid.shape[2]) * grid.z_size
