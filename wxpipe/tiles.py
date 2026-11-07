"""Tile geometry on the GFS grid, the same rules as RegularLatLonDomain in the app (core:config)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from . import config

_SIZE = re.compile(r"^(\d{1,5})x(\d{1,5})(?:/(\d{1,5}))?$")


@dataclass(frozen=True)
class TileSize:
    w: int
    h: int
    step: int = 1

    @staticmethod
    def parse(text: str) -> "TileSize":
        m = _SIZE.match(text.strip())
        if not m:
            raise ValueError(f"invalid tile size {text!r}: expected WxH or WxH/step")
        size = TileSize(int(m[1]), int(m[2]), int(m[3]) if m[3] else 1)
        if size.w <= 0 or size.h <= 0 or size.step <= 0 or size.w % size.step or size.h % size.step:
            raise ValueError(f"invalid tile size {text!r}")
        return size

    def __str__(self) -> str:
        return f"{self.w}x{self.h}/{self.step}"

    @property
    def path(self) -> str:
        return f"{self.w}x{self.h}-{self.step}"


@dataclass(frozen=True)
class Tile:
    """Tile (i, j) spans columns i*W .. i*W + W and rows j*H .. j*H + H, both ends included, so it
    shares its last column and row with the next tile. Column NX is column 0 again (the grid wraps);
    rows stop at the last grid row, so the bottom tiles of a level can be shorter."""

    size: TileSize
    i: int
    j: int

    @property
    def x0(self) -> int:
        return self.i * self.size.w

    @property
    def x1(self) -> int:
        return self.x0 + self.size.w

    @property
    def y0(self) -> int:
        return self.j * self.size.h

    @property
    def y1(self) -> int:
        return min(self.y0 + self.size.h, config.NY - 1)

    @property
    def nx(self) -> int:
        return self.size.w // self.size.step + 1

    @property
    def ny(self) -> int:
        return (self.y1 - self.y0) // self.size.step + 1

    def rows(self) -> np.ndarray:
        return np.arange(self.y0, self.y1 + 1, self.size.step)

    def cols(self) -> np.ndarray:
        return np.arange(self.x0, self.x1 + 1, self.size.step) % config.NX

    def cut(self, grid: np.ndarray) -> np.ndarray:
        """The tile's samples from a full (NY, NX) grid, row-major from north to south."""
        return grid[np.ix_(self.rows(), self.cols())]

    @property
    def path(self) -> str:
        return f"{self.i}-{self.j}"


def tiles_covering(size: TileSize, south: float, north: float, west: float, east: float) -> list[Tile]:
    """Tiles that cover a box, by the rule of RegularLatLonDomain.tilesCovering in the app."""
    span = east - west if east > west else east + 360.0 - west
    x0 = ((west - config.LON0) % 360.0) / config.DEG
    x1 = x0 + span / config.DEG
    y_north = (config.LAT0 - north) / config.DEG
    y_south = (config.LAT0 - south) / config.DEG
    last_row = (config.NY - 2) // size.h
    i_min = math.floor(x0 / size.w)
    i_max = math.ceil(x1 / size.w) - 1
    j_min = min(max(math.floor(min(y_north, y_south) / size.h), 0), last_row)
    j_max = min(max(math.ceil(max(y_north, y_south) / size.h) - 1, 0), last_row)
    columns = config.NX // size.w
    return [Tile(size, i % columns, j) for i in range(i_min, i_max + 1) for j in range(j_min, j_max + 1)]


def tiles_for(level: config.Level) -> list[Tile]:
    size = TileSize.parse(level.size)
    if config.NX % size.w:
        raise ValueError(f"{size} does not divide the {config.NX} columns")
    if level.region_only:
        return tiles_covering(size, **config.REGION)
    rows = (config.NY - 2) // size.h + 1
    return [Tile(size, i, j) for i in range(config.NX // size.w) for j in range(rows)]
