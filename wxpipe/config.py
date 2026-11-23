"""What the pipeline produces: the shared output grid, the tile ladder, the full-resolution region, and each source.

A source is described in two parts. Its standard-library part, `wxpipe.source.SourceInfo`, holds what
the cheap check needs: name, last hour, cycle step and run discovery. Its full part, `Source` here,
adds the grid as delivered, the forecast hours, the fields with their tables, the fetch functions and
the attribution. The rationale for each choice is in docs/DESIGN.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import gfs, source as source_info
from .quantize import Lut
from .source import SourceInfo

# The shared output grid of every source: row 0 is 90N, column 0 is 0E, both in 0.25 degree steps.
# Decode puts each source's delivered grid on it, so the tiles and the app's domain are the same for all.
NX, NY = 1440, 721
LAT0, LON0, DEG = 90.0, 0.0, 0.25

# The user's region at full 0.25 degree resolution (blueprint 11.3): 12 tiles of the 60x60/1 level.
REGION = {"west": 15.0, "east": 75.0, "south": 0.0, "north": 45.0}


@dataclass(frozen=True)
class Level:
    size: str          # "WxH/step"
    region_only: bool  # True: only the tiles that cover REGION


LEVELS: tuple[Level, ...] = (
    Level("60x60/1", True),
    Level("120x120/2", False),
    Level("240x240/4", False),
    Level("480x480/8", False),
)


@dataclass(frozen=True)
class Field:
    name: str                 # data name used in tile paths and the manifest
    units: str                # units-engine key of the stored values
    grib: str                 # "VAR:level" as written in the source's index files
    lut: Lut
    kind: str = "instant"     # "instant", or "precip_rate" (rate from accumulations)
    scale: float = 1.0        # stored value = raw * scale + offset
    offset: float = 0.0


@dataclass(frozen=True)
class Grid:
    """A source's grid as its GRIB2 messages deliver it; `grib.check_grid` refuses any other."""

    ni: int                   # columns
    nj: int                   # rows
    lat0: float               # first grid point, degrees
    lon0: float
    di: float                 # direction increments, degrees
    dj: float
    j_scans_positively: int   # 0: rows run north to south
    i_scans_negatively: int   # 0: columns run west to east


@dataclass(frozen=True)
class Source:
    info: SourceInfo
    grid: Grid
    forecast_hours: tuple[int, ...]
    fields: tuple[Field, ...]
    # The fetch functions: the index parser, the record pickers and the byte-range reader.
    parse_idx: Callable
    instant_record: Callable
    accumulation_records: Callable
    read: Callable
    attribution: str

    @property
    def name(self) -> str:
        return self.info.name

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)


WIND_LUT = Lut([(0, -80.0), (20, -40.0), (234, 40.0), (254, 80.0)])

GFS = Source(
    info=source_info.GFS,
    grid=Grid(ni=1440, nj=721, lat0=90.0, lon0=0.0, di=0.25, dj=0.25, j_scans_positively=0, i_scans_negatively=0),
    # Hourly to 120 h, then every 3 h to 384 h: the full GFS range, 16 days, 209 frames.
    forecast_hours=tuple(range(0, 121)) + tuple(range(123, 385, 3)),
    fields=(
        Field("temperature.2m", "C", "TMP:2 m above ground",
              Lut([(0, -70.0), (20, -30.0), (230, 50.0), (254, 60.0)]), offset=-273.15),
        Field("precipitation.sfc", "mm hr**-1", "APCP:surface",
              Lut([(0, 0.0), (60, 1.2), (140, 8.0), (200, 30.0), (254, 150.0)]), kind="precip_rate"),
        # Wind as signed components: they interpolate correctly between frames and feed streamlines,
        # and they leave no from/to direction convention to settle (open question 6).
        Field("wind_u.10m", "m s**-1", "UGRD:10 m above ground", WIND_LUT),
        Field("wind_v.10m", "m s**-1", "VGRD:10 m above ground", WIND_LUT),
        Field("pressure.msl", "hPa", "PRMSL:mean sea level",
              Lut([(0, 870.0), (20, 950.0), (234, 1050.0), (254, 1090.0)]), scale=0.01),
        Field("cloud.total", "%", "TCDC:entire atmosphere", Lut([(0, 0.0), (254, 100.0)])),
    ),
    parse_idx=gfs.parse_idx,
    instant_record=gfs.instant_record,
    accumulation_records=gfs.accumulation_records,
    read=gfs.read,
    attribution="NOAA NCEP Global Forecast System (GFS). Public domain.",
)

# Every source, in the order of wxpipe.source.INFOS (checked by a test).
SOURCES: tuple[Source, ...] = (GFS,)


def source(name: str) -> Source:
    for s in SOURCES:
        if s.info.name == name:
            return s
    raise KeyError(name)
