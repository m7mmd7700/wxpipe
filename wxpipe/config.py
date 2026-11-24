"""What the pipeline produces: the shared output grid, the tile ladder, the full-resolution region, and each source.

A source is described in two parts. Its standard-library part, `wxpipe.source.SourceInfo`, holds what
the cheap check needs: name, last hour, cycle step and run discovery. Its full part, `Source` here,
adds the grid as delivered, the forecast hours, the fields with their tables, the fetch functions, the
attribution, and how the source is read: its workers, its read time limit, its cache, its probe
messages, whether a run that builds GFS defers it, and whether its job runs in a child process. The rationale for each choice is in docs/DESIGN.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import ecmwf, gfs, source as source_info
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
    lon0: float               # where the delivered first column lies; decode rolls it onto LON0
    di: float                 # direction increments, degrees
    dj: float
    j_scans_positively: int   # 0: rows run north to south
    i_scans_negatively: int   # 0: columns run west to east


@dataclass(frozen=True)
class Probe:
    """A committed message in `wxpipe/samples/` that the runner's ecCodes must decode to these figures
    before the source is built (`wxpipe.probe`). With [missing] set, exactly that many values must be
    missing, land and ice in a wave message, and the figures are over the present values; without it,
    every value must be present."""

    file: str
    minimum: float
    maximum: float
    mean: float
    missing: int | None = None


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
    # The manifest's one attribution string; with parts, it is followed by their values.
    attribution: str
    # The licence's elements as (key, value) pairs, `{year}` standing for the run's year; None: none.
    attribution_parts: tuple[tuple[str, str], ...] | None = None
    workers: int | None = None                # reads at a time; None: run.py's --workers
    read_limit_seconds: float | None = None   # a read started after this from the job's start drops it; None: no limit
    cache_one_cycle: bool = False             # a job deletes the other cycles' folders of its download cache
    probes: tuple[Probe, ...] = ()
    # In the workflow's form, a run that builds GFS defers this source to the next run.
    waits_for_gfs: bool = False
    # In the workflow's form, the job runs in a child process bound by these seconds, so a crash or a hang
    # of it ends the child only; None: the job runs in the run's own process. The local form, which the
    # child runs, always builds in its own process.
    child_seconds: float | None = None
    # The counts of the job's reads per copy: zeroed at the job's start, and the run summary's line of
    # them; None: the source writes no such line.
    reads_reset: Callable[[], None] | None = None
    reads_report: Callable[[str, str], str] | None = None

    @property
    def name(self) -> str:
        return self.info.name

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def attribution_parts_for(self, year: int) -> dict[str, str] | None:
        """The licence's elements for a run of [year], in their order; None for a source without them."""
        if self.attribution_parts is None:
            return None
        return {key: value.replace("{year}", str(year)) for key, value in self.attribution_parts}

    def attribution_for(self, year: int) -> str:
        """The manifest's attribution for a run of [year]: the source's text, then its parts' values."""
        parts = self.attribution_parts_for(year)
        return self.attribution if parts is None else " ".join([self.attribution, *parts.values()])


# The tables, shared by every source so the app's spectra and legends apply to each.
TEMPERATURE_LUT = Lut([(0, -70.0), (20, -30.0), (230, 50.0), (254, 60.0)])
PRECIPITATION_LUT = Lut([(0, 0.0), (60, 1.2), (140, 8.0), (200, 30.0), (254, 150.0)])
WIND_LUT = Lut([(0, -80.0), (20, -40.0), (234, 40.0), (254, 80.0)])
PRESSURE_LUT = Lut([(0, 870.0), (20, 950.0), (234, 1050.0), (254, 1090.0)])
CLOUD_LUT = Lut([(0, 0.0), (254, 100.0)])
# GFS-Wave's own: 0.05 m a byte to 10 m and about 0.185 m above; 0.1 s a byte to 20 s and about 0.185 s
# above. The sample run 2026112412's largest values were 11.98 m and 26.64 s.
WAVE_HEIGHT_LUT = Lut([(0, 0.0), (200, 10.0), (254, 20.0)])
WAVE_PERIOD_LUT = Lut([(0, 0.0), (200, 20.0), (254, 30.0)])

GFS = Source(
    info=source_info.GFS,
    grid=Grid(ni=1440, nj=721, lat0=90.0, lon0=0.0, di=0.25, dj=0.25, j_scans_positively=0, i_scans_negatively=0),
    # Hourly to 120 h, then every 3 h to 384 h: the full GFS range, 16 days, 209 frames.
    forecast_hours=tuple(range(0, 121)) + tuple(range(123, 385, 3)),
    fields=(
        Field("temperature.2m", "C", "TMP:2 m above ground", TEMPERATURE_LUT, offset=-273.15),
        Field("precipitation.sfc", "mm hr**-1", "APCP:surface", PRECIPITATION_LUT, kind="precip_rate"),
        # Wind as signed components: they interpolate correctly between frames and feed streamlines,
        # and they leave no from/to direction convention to settle (open question 6).
        Field("wind_u.10m", "m s**-1", "UGRD:10 m above ground", WIND_LUT),
        Field("wind_v.10m", "m s**-1", "VGRD:10 m above ground", WIND_LUT),
        Field("pressure.msl", "hPa", "PRMSL:mean sea level", PRESSURE_LUT, scale=0.01),
        Field("cloud.total", "%", "TCDC:entire atmosphere", CLOUD_LUT),
    ),
    parse_idx=gfs.parse_idx,
    instant_record=gfs.instant_record,
    accumulation_records=gfs.accumulation_records,
    read=gfs.read,
    attribution="NOAA NCEP Global Forecast System (GFS). Public domain.",
)

# ECMWF's licence page lists six elements; the licence is split into its text and its link, and
# `modified` is wxpipe's own statement of what it changed.
IFS_ATTRIBUTION_PARTS: tuple[tuple[str, str], ...] = (
    ("licence", "This data is published under a Creative Commons Attribution 4.0 International (CC BY 4.0)."),
    ("licence_url", "https://creativecommons.org/licenses/by/4.0/"),
    ("copyright", "© {year} European Centre for Medium-Range Weather Forecasts (ECMWF)"),
    ("source", "Source www.ecmwf.int"),
    ("credit", "Based on data and products of the European Centre for Medium-Range Weather Forecasts (ECMWF)."),
    ("liability", "ECMWF does not accept any liability whatsoever for any error or omission in the data, "
                  "their availability, or for any loss or damage arising from their use."),
    ("modified", "Modified: the columns rolled to start at 0°E, units converted, accumulated precipitation "
                 "turned into rates, values quantised to 8-bit tables and cut into map tiles."),
)

IFS = Source(
    info=source_info.IFS,
    # The delivered grid starts at 180E; decode rolls it by 720 columns onto the shared grid.
    grid=Grid(ni=1440, nj=721, lat0=90.0, lon0=180.0, di=0.25, dj=0.25, j_scans_positively=0, i_scans_negatively=0),
    # Every 3 h to 144 h, then every 6 h to 360 h: the 00 and 12 runs' full range, 15 days, 85 frames.
    forecast_hours=tuple(range(0, 145, 3)) + tuple(range(150, 361, 6)),
    # The same names, units and tables as GFS's; the parameters are ecmwf.int's, at level type sfc.
    fields=(
        Field("temperature.2m", "C", "2t:sfc", TEMPERATURE_LUT, offset=-273.15),      # K
        # Total precipitation since the run's start, in m, as a rate in mm/h over each interval.
        Field("precipitation.sfc", "mm hr**-1", "tp:sfc", PRECIPITATION_LUT, kind="precip_rate", scale=1000.0),
        Field("wind_u.10m", "m s**-1", "10u:sfc", WIND_LUT),
        Field("wind_v.10m", "m s**-1", "10v:sfc", WIND_LUT),
        Field("pressure.msl", "hPa", "msl:sfc", PRESSURE_LUT, scale=0.01),             # Pa
        Field("cloud.total", "%", "tcc:sfc", CLOUD_LUT, scale=100.0),                   # 0 to 1
    ),
    parse_idx=ecmwf.parse_idx,
    instant_record=ecmwf.instant_record,
    accumulation_records=ecmwf.accumulation_records,
    read=ecmwf.read,
    attribution="ECMWF IFS open data.",
    attribution_parts=IFS_ATTRIBUTION_PARTS,
    # Read from three copies in order, the data portal first (wxpipe.source.IFS). The AWS bucket behind
    # the other two answered "503 SlowDown" to all ten attempts of one read from GitHub's runner, and to
    # 126 of the sample fetch's 270 requests; the portal refused none in the probe of 2026-11-24, and is
    # limited to between 500 and 2000 simultaneous connections for all its users. So two reads at a
    # time, and a job still reading after 45 minutes is dropped, its downloads kept for the next hour's
    # try in the Actions cache.
    workers=2,
    read_limit_seconds=45 * 60,
    cache_one_cycle=True,
    # CCSDS messages at two bit widths, the figures from the local ecCodes decode in each message's units:
    # S12's 2t of 2026112200 at step 0, 9 bits, in K; msl of 2026112312 at step 0, 12 bits, in Pa.
    probes=(Probe("ifs-2t-ccsds.grib2", minimum=221.997971, maximum=315.997971, mean=278.805962),
            Probe("ifs-msl-ccsds.grib2", minimum=95764.125000, maximum=104952.125000, mean=100852.455598)),
    reads_reset=ecmwf.reads_reset,
    reads_report=ecmwf.reads_report,
    # A run that builds GFS does not wait for ECMWF's reads, which its copies' SlowDown answers can
    # stretch to the read limit and more; the next run, in which GFS is not new, builds it.
    waits_for_gfs=True,
)

GFSWAVE = Source(
    info=source_info.GFSWAVE,
    # GFS's delivered grid: 0E first, north first, so no roll.
    grid=Grid(ni=1440, nj=721, lat0=90.0, lon0=0.0, di=0.25, dj=0.25, j_scans_positively=0, i_scans_negatively=0),
    # GFS's 209 hours: hourly to 120 h, then every 3 h to 384 h.
    forecast_hours=GFS.forecast_hours,
    # Land and ice are missing in the messages' bitmap, and so byte 255 in the tiles.
    fields=(
        Field("wave_height.sfc", "m", "HTSGW:surface", WAVE_HEIGHT_LUT),    # significant height of combined waves
        Field("wave_period.sfc", "s", "PERPW:surface", WAVE_PERIOD_LUT),    # primary wave mean period
    ),
    parse_idx=gfs.parse_idx,
    instant_record=gfs.instant_record,
    accumulation_records=gfs.accumulation_records,
    # Each message checked whole: GFS-Wave's JPEG 2000 messages are read by a decoder library that
    # GFS's builds never use.
    read=gfs.read_checked,
    attribution="NOAA NCEP GFS-Wave, the global 0.25 degree wave model of the Global Forecast System. Public domain.",
    # Built in GFS's run, so GFS's publication waits for it: a job still reading after 15 minutes is
    # dropped and GFS is published without it; and the job runs in a child process of at most 20
    # minutes, so a crash of the JPEG 2000 decoder or a stalled read ends the child, never GFS's run.
    read_limit_seconds=15 * 60,
    child_seconds=20 * 60,
    # JPEG 2000 at 10 bits, the figures over the present values from the local ecCodes decode, in m:
    # S12's HTSGW of 2026112206 at f000.
    probes=(Probe("gfswave-htsgw-jpeg.grib2", minimum=0.0, maximum=8.27, mean=2.267598, missing=468208),),
)

# Every source, in the order of wxpipe.source.INFOS (checked by a test). GFS comes first, so its job is
# built first; then GFS-Wave, from the same bucket, built in the same run; ECMWF's slow reads last.
SOURCES: tuple[Source, ...] = (GFS, GFSWAVE, IFS)


def source(name: str) -> Source:
    for s in SOURCES:
        if s.info.name == name:
            return s
    raise KeyError(name)
