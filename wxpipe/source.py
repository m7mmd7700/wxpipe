"""The standard-library part of each source's description: name, last hour, cycle step, file URLs and run discovery.

`check.py` imports this module on the runner's own Python before any install, so it imports nothing
outside the standard library and nothing of wxpipe. The full part of each description, with the grid,
the fields and the fetch functions, is `wxpipe.config.Source`, which holds one of these.
"""

from __future__ import annotations

import http.client
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class SourceInfo:
    name: str             # the source's folder on the site, its key in the manifest and the tile header's "source"
    last_hour: int        # last forecast hour of a run; a run is complete once this hour's index is published
    cycle_hours: int      # hours between two runs
    base_url: str         # the first copy's ROOT
    file_pattern: str     # str.format pattern of a file's URL, with {base}, {cycle} (a datetime) and {hour}
    idx_suffix: str       # appended to the pattern for a file's index
    lookback_cycles: int  # how many cycles back the discovery looks for a complete run
    file_suffix: str = ""  # appended to the pattern for the file itself
    copies: tuple[str, ...] = ()  # the ROOTs of the source's copies in the order tried, the first base_url; () for one


GFS = SourceInfo(
    name="gfs",
    last_hour=384,
    cycle_hours=6,
    base_url="https://noaa-gfs-bdp-pds.s3.amazonaws.com",
    file_pattern="{base}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{hour:03d}",
    idx_suffix=".idx",
    lookback_cycles=6,
)

# ECMWF IFS 0.25 degree from three copies of ECMWF's open data, tried in this order: ECMWF's own data
# portal, which refused none of the probe's requests of 2026-11-24; the AWS bucket's CloudFront door;
# the AWS bucket by its S3 host, which answered "503 SlowDown" to all ten attempts of one read from
# GitHub's runner. The probe found every file it compared the same bytes on two copies. The path below
# each ROOT is the same. The 00 and 12 runs to 360 h; a run is complete once step 360's index is
# there; four cycles back is two days.
IFS_PORTAL = "https://data.ecmwf.int/forecasts"
IFS = SourceInfo(
    name="ifs",
    last_hour=360,
    cycle_hours=12,
    base_url=IFS_PORTAL,
    file_pattern="{base}/{cycle:%Y%m%d}/{cycle:%H}z/ifs/0p25/oper/{cycle:%Y%m%d%H}0000-{hour}h-oper-fc",
    idx_suffix=".index",
    lookback_cycles=4,
    file_suffix=".grib2",
    copies=(IFS_PORTAL,
            "https://d2zvc0wgha4k2l.cloudfront.net",
            "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"),
)

# The standard-library parts of every source, in the order of wxpipe.config.SOURCES (checked by a test).
INFOS: tuple[SourceInfo, ...] = (GFS, IFS)


def info(name: str) -> SourceInfo:
    for i in INFOS:
        if i.name == name:
            return i
    raise KeyError(name)


def _stem(source: SourceInfo, cycle: datetime, hour: int) -> str:
    return source.file_pattern.format(base=source.base_url, cycle=cycle, hour=hour)


def file_url(source: SourceInfo, cycle: datetime, hour: int) -> str:
    return _stem(source, cycle, hour) + source.file_suffix


def idx_url(source: SourceInfo, cycle: datetime, hour: int) -> str:
    return _stem(source, cycle, hour) + source.idx_suffix


def roots(source: SourceInfo) -> tuple[str, ...]:
    """The ROOTs of the source's copies in the order tried; a source of one copy has its base URL alone."""
    return source.copies or (source.base_url,)


def on_root(source: SourceInfo, url: str, root: str) -> str:
    """[url], built on the source's base URL, on the copy whose ROOT is [root]."""
    if not url.startswith(source.base_url):
        raise ValueError(f"{url} is not on {source.base_url}")
    return root + url[len(source.base_url):]


def label(root: str) -> str:
    """A copy's name in the logs and the run's summary: its ROOT without `https://`."""
    return root.removeprefix("https://")


def exists_on_copies(source: SourceInfo, exists: Callable[[str], bool]) -> Callable[[str], bool]:
    """An existence test of a URL built on the source's base URL that asks each copy in order: the first
    copy that answers true or false decides; a copy whose test raises an OSError or an
    http.client.HTTPException is passed over, and any other exception is raised at once; when every
    copy raised, the first error is raised. For a source of several copies, each copy passed over is
    printed in one line. For a source of one copy it answers, and raises, as [exists].

    The function returned keeps, in `decisions`, one (url, copy, answer) per URL asked, in order: the
    copy's label and its answer, or None and None when every copy raised (`discovery_line`)."""
    several = len(roots(source)) > 1
    decisions: list[tuple[str, str | None, bool | None]] = []

    def on_copies(url: str) -> bool:
        urls = [on_root(source, url, root) for root in roots(source)]
        first: BaseException | None = None
        for root, copy_url in zip(roots(source), urls):
            try:
                answer = exists(copy_url)
            except (OSError, http.client.HTTPException) as e:
                if several:
                    print(f"{source.name} discovery: {label(root)} passed over: {type(e).__name__}: {e}")
                if first is None:
                    first = e
                continue
            decisions.append((url, label(root), answer))
            return answer
        decisions.append((url, None, None))
        raise first

    on_copies.decisions = decisions
    return on_copies


def discovery_line(source: SourceInfo, on_copies: Callable[[str], bool]) -> str | None:
    """For a source of several copies, after its discovery by [on_copies] of `exists_on_copies`: which
    copy decided the newest cycle and its answer, or that every copy raised. None for one copy, or
    when nothing was asked."""
    decisions = getattr(on_copies, "decisions", None)
    if len(roots(source)) < 2 or not decisions:
        return None
    _, copy, answer = decisions[0]
    if copy is None:
        return f"{source.name} discovery: the newest cycle: every copy raised"
    return f"{source.name} discovery: the newest cycle decided by {copy}: {'true' if answer else 'false'}"


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
