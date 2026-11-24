"""GFS 0.25 degree files on the NOAA Open Data bucket on AWS: .idx parsing, record pickers, HTTP reads.

GFS-Wave's files, in the same bucket with the same .idx form, are read by the same functions, each of
its messages checked whole (`read_checked`). The URLs and the run discovery come from each source's
description in `wxpipe.source`. Standard library only,
since `check.py` imports this module before any install.
"""

from __future__ import annotations

import http.client
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


def check_message(url: str, byte_range: tuple[int, int | None], data: bytes) -> None:
    """Raises OSError unless [data], read for [byte_range], is one whole GRIB2 message: exactly the asked
    length when the range's end is known, the four bytes `GRIB` first, the edition byte at offset 7
    equal to 2, the length field at offsets 8 to 15, big-endian, equal to the body's length, and the
    four bytes `7777` last. The checks of ECMWF's range reads (`ecmwf._accept`)."""
    start, end = byte_range
    if end is not None and len(data) != end - start + 1:
        raise OSError(f"range read of {url} gave {len(data)} bytes, expected {end - start + 1}")
    if data[:4] != b"GRIB":
        raise OSError(f"range read of {url} does not begin with GRIB")
    if len(data) < 16 or data[7] != 2:
        raise OSError(f"range read of {url} is not a GRIB edition 2 message")
    if int.from_bytes(data[8:16], "big") != len(data):
        raise OSError(f"range read of {url}: the message's length field "
                      f"{int.from_bytes(data[8:16], 'big')} is not its {len(data)} bytes")
    if data[-4:] != b"7777":
        raise OSError(f"range read of {url} does not end with 7777")


def read_checked(url: str, byte_range: tuple[int, int | None] | None = None) -> bytes:
    """The byte-range reader of GFS-Wave's description: `read`, and each range read checked whole by
    `check_message`, so a short or foreign answer skips the source instead of being decoded. A whole
    read, an index, is returned as given. It looks `http_get` up when called, as `read` does."""
    data = http_get(url, byte_range)
    if byte_range:
        check_message(url, byte_range, data)
    return data


# The waits between the four attempts of `exists`, in seconds: shorter than ECMWF's reads, since GFS's
# bucket has not been seen to throttle.
EXISTS_WAITS: tuple[int, ...] = (1, 2, 4)


def exists(url: str, opener=None, wait=None) -> bool:
    """Whether [url] answers, by a HEAD request; the run discovery of every source uses it.

    A 404 or any other 4xx answer but 429 is False. A 429 or 5xx answer, a timeout or a connection
    error is tried again after EXISTS_WAITS and then raised, so the caller can tell "not there" from
    "not known". [opener] and [wait] default to urllib's urlopen and time.sleep, and tests inject them.
    """
    opener = urllib.request.urlopen if opener is None else opener
    wait = time.sleep if wait is None else wait
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    attempts = len(EXISTS_WAITS) + 1
    for attempt in range(attempts):
        try:
            with opener(request, timeout=30) as response:
                return response.status == 200
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                return False
            if attempt == attempts - 1:
                raise
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError):
            if attempt == attempts - 1:
                raise
        wait(EXISTS_WAITS[attempt])
    raise AssertionError("unreachable")
