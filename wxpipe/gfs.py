"""GFS 0.25 degree files on the NOAA Open Data bucket on AWS: .idx parsing, record pickers, HTTP reads.

The URLs and the run discovery come from GFS's description in `wxpipe.source`. Standard library only,
since `check.py` imports this module before any install.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from . import source

USER_AGENT = "wxpipe/0.1 (personal weather app pipeline)"


def file_url(cycle: datetime, fhour: int) -> str:
    return source.file_url(source.GFS, cycle, fhour)


def idx_url(cycle: datetime, fhour: int) -> str:
    return source.idx_url(source.GFS, cycle, fhour)


@dataclass(frozen=True)
class IdxRecord:
    number: int
    offset: int
    end: int | None   # last byte, inclusive; None means to the end of the file
    var: str
    level: str
    fcst: str

    @property
    def key(self) -> str:
        return f"{self.var}:{self.level}"


def parse_idx(text: str) -> list[IdxRecord]:
    """Parses lines like `612:398451232:d=2026110712:TMP:2 m above ground:7 hour fcst:`."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 6:
            raise ValueError(f"bad .idx line: {line!r}")
        rows.append((int(parts[0]), int(parts[1]), parts[3], parts[4], parts[5]))
    records = []
    for k, (number, offset, var, level, fcst) in enumerate(rows):
        end = rows[k + 1][1] - 1 if k + 1 < len(rows) else None
        records.append(IdxRecord(number, offset, end, var, level, fcst))
    return records


_ACC = re.compile(r"^(\d+)-(\d+) (hour|day) acc fcst$")


def accumulation_window(fcst: str) -> tuple[int, int] | None:
    """`6-7 hour acc fcst` -> (6, 7); `0-5 day acc fcst` -> (0, 120); anything else -> None."""
    m = _ACC.match(fcst)
    if not m:
        return None
    k = 24 if m[3] == "day" else 1
    return int(m[1]) * k, int(m[2]) * k


def instant_record(records: list[IdxRecord], key: str, fhour: int) -> IdxRecord | None:
    """The value at the forecast hour itself: `anl` at hour 0, `N hour fcst` after it."""
    want = "anl" if fhour == 0 else f"{fhour} hour fcst"
    return next((r for r in records if r.key == key and r.fcst == want), None)


def accumulation_records(records: list[IdxRecord], key: str, fhour: int) -> dict[tuple[int, int], IdxRecord]:
    """Accumulations ending at [fhour], by window. GFS repeats some records; the first copy is kept."""
    out: dict[tuple[int, int], IdxRecord] = {}
    for r in records:
        if r.key != key:
            continue
        window = accumulation_window(r.fcst)
        if window and window[1] == fhour and window not in out:
            out[window] = r
    return out


def http_get(url: str, byte_range: tuple[int, int | None] | None = None, retries: int = 4, timeout: int = 60) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if byte_range:
        start, end = byte_range
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                data = response.read()
                if byte_range and response.status != 206:
                    raise OSError(f"expected HTTP 206 for a range read, got {response.status}")
                return data
        except urllib.error.HTTPError as e:
            if e.code == 404 or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def read(url: str, byte_range: tuple[int, int | None] | None = None) -> bytes:
    """The byte-range reader of GFS's description. It looks `http_get` up when called, so a test that
    replaces `gfs.http_get` replaces every read the pipeline makes."""
    return http_get(url, byte_range)


def exists(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return False
