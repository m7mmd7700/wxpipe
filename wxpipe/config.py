"""What the pipeline produces: grid, forecast hours, fields, tile ladder and full-resolution region.

The rationale for each choice is in docs/DESIGN.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from .quantize import Lut

SOURCE = "gfs"

# GFS 0.25 degree grid: row 0 is 90N, column 0 is 0E, both in 0.25 degree steps.
NX, NY = 1440, 721
LAT0, LON0, DEG = 90.0, 0.0, 0.25

# Hourly to 120 h, then every 3 h to 384 h: the full GFS range, 16 days, 209 frames.
FORECAST_HOURS: tuple[int, ...] = tuple(range(0, 121)) + tuple(range(123, 385, 3))

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
    grib: str                 # "VAR:level" as written in the GFS .idx files
    lut: Lut
    kind: str = "instant"     # "instant", or "precip_rate" (rate from accumulations)
    scale: float = 1.0        # stored value = raw * scale + offset
    offset: float = 0.0


WIND_LUT = Lut([(0, -80.0), (20, -40.0), (234, 40.0), (254, 80.0)])

FIELDS: tuple[Field, ...] = (
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
)


def field(name: str) -> Field:
    for f in FIELDS:
        if f.name == name:
            return f
    raise KeyError(name)
