"""The standard-library part of each source's description: name, last hour, cycle step, file URLs and run discovery.

`check.py` imports this module on the runner's own Python before any install, so it imports nothing
outside the standard library and nothing of wxpipe. The full part of each description, with the grid,
the fields and the fetch functions, is `wxpipe.config.Source`, which holds one of these.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class SourceInfo:
    name: str             # the source's folder on the site, its key in the manifest and the tile header's "source"
    last_hour: int        # last forecast hour of a run; a run is complete once this hour's index is published
    cycle_hours: int      # hours between two runs
    base_url: str         # the bucket
    file_pattern: str     # str.format pattern of a file's URL, with {base}, {cycle} (a datetime) and {hour}
    idx_suffix: str       # appended to a file's URL for its index
    lookback_cycles: int  # how many cycles back the discovery looks for a complete run


GFS = SourceInfo(
    name="gfs",
    last_hour=384,
    cycle_hours=6,
    base_url="https://noaa-gfs-bdp-pds.s3.amazonaws.com",
    file_pattern="{base}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{hour:03d}",
    idx_suffix=".idx",
    lookback_cycles=6,
)

# The standard-library parts of every source, in the order of wxpipe.config.SOURCES (checked by a test).
INFOS: tuple[SourceInfo, ...] = (GFS,)


def info(name: str) -> SourceInfo:
    for i in INFOS:
        if i.name == name:
            return i
    raise KeyError(name)


def file_url(source: SourceInfo, cycle: datetime, hour: int) -> str:
    return source.file_pattern.format(base=source.base_url, cycle=cycle, hour=hour)


def idx_url(source: SourceInfo, cycle: datetime, hour: int) -> str:
    return file_url(source, cycle, hour) + source.idx_suffix


def latest_complete_cycle(source: SourceInfo, now: datetime, exists: Callable[[str], bool],
                          last_hour: int | None = None) -> datetime | None:
    """The newest run on the source's cycle step whose last forecast hour is already published.

    [exists] tells whether a URL answers; [last_hour] defaults to the source's own.
    """
    hour = source.last_hour if last_hour is None else last_hour
    newest = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    newest -= timedelta(hours=newest.hour % source.cycle_hours)
    for k in range(source.lookback_cycles):
        cycle = newest - timedelta(hours=source.cycle_hours * k)
        if exists(idx_url(source, cycle, hour)):
            return cycle
    return None
