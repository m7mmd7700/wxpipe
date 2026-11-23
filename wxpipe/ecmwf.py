"""ECMWF IFS 0.25 degree files on ECMWF's open-data mirror on AWS: index parsing, record pickers, HTTP reads.

The URLs and the run discovery come from IFS's description in `wxpipe.source`. An index is one JSON
object per line, each with `param`, `levtype`, `step`, `_offset` and `_length`. The mirror answers
"503 SlowDown" often, so every read retries with a backoff of up to ten attempts. Standard library
only, like `gfs.py`.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

USER_AGENT = "wxpipe/0.1 (personal weather app pipeline)"

# The waits between the ten attempts of a read, in seconds; a Retry-After header's seconds replace
# the wait when given, at most MAX_WAIT.
WAITS: tuple[int, ...] = (5, 10, 20, 40, 60, 60, 60, 60, 60)
MAX_WAIT = 60
TIMEOUT = 60


@dataclass(frozen=True)
class IdxRecord:
    number: int       # the record's 1-based line in the index, its name in the cache
    offset: int
    end: int          # last byte, inclusive
    param: str
    levtype: str
    step: str

    @property
    def key(self) -> str:
        return f"{self.param}:{self.levtype}"


def parse_idx(text: str) -> list[IdxRecord]:
    """Parses lines like `{"step": "0", "levtype": "sfc", "param": "2t", "_offset": 1211505, "_length": 313971, ...}`."""
    records = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            offset, length = row["_offset"], row["_length"]
            param, levtype, step = row["param"], row["levtype"], row["step"]
        except (ValueError, TypeError, KeyError) as e:
            raise ValueError(f"bad index line {number}: {line!r}") from e
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (offset, length)) or length <= 0:
            raise ValueError(f"bad index line {number}: {line!r}")
        records.append(IdxRecord(number, offset, offset + length - 1, str(param), str(levtype), str(step)))
    return records


_WINDOW = re.compile(r"^([0-9]+)-([0-9]+)$")


def instant_record(records: list[IdxRecord], key: str, fhour: int) -> IdxRecord | None:
    """The value at the forecast hour itself, whose step is the hour."""
    want = str(fhour)
    return next((r for r in records if r.key == key and r.step == want), None)


def accumulation_records(records: list[IdxRecord], key: str, fhour: int) -> dict[tuple[int, int], IdxRecord]:
    """Accumulations ending at [fhour], by window. IFS's `tp` is the total since the run's start, its
    step the hour itself, so it is keyed (0, fhour); a step written `a-b` is kept as (a, b) when b is
    the hour. Nothing at hour 0, as for GFS, so step 0's constant message is never read."""
    out: dict[tuple[int, int], IdxRecord] = {}
    if fhour == 0:
        return out
    for r in records:
        if r.key != key:
            continue
        m = _WINDOW.match(r.step)
        if m:
            window = (int(m[1]), int(m[2]))
        elif r.step == str(fhour):
            window = (0, fhour)
        else:
            continue
        if window[1] == fhour and window not in out:
            out[window] = r
    return out


def _retry_after(error: urllib.error.HTTPError) -> int | None:
    value = (error.headers.get("Retry-After") if error.headers is not None else None) or ""
    value = value.strip()
    return int(value) if value.isascii() and value.isdigit() else None


def http_get(url: str, byte_range: tuple[int, int | None] | None = None,
             opener: Callable | None = None, wait: Callable[[float], None] | None = None) -> bytes:
    """The body of [url], or of its bytes [byte_range] inclusive.

    A 429 or 5xx answer, a timeout or a connection error is tried again, up to len(WAITS) + 1
    attempts, waiting WAITS or the Retry-After header's seconds, at most MAX_WAIT; any other HTTP
    error, a 404 among them, is raised at once. A range read must answer 206 with exactly the asked
    length. [opener] and [wait] default to urllib's urlopen and time.sleep, and tests inject them.
    """
    opener = urllib.request.urlopen if opener is None else opener
    wait = time.sleep if wait is None else wait
    headers = {"User-Agent": USER_AGENT}
    if byte_range:
        start, end = byte_range
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    attempts = len(WAITS) + 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        delay = None
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
                status = response.status
                data = response.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            if last:
                raise
            delay = _retry_after(e)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError):
            if last:
                raise
        else:
            if byte_range:
                start, end = byte_range
                if status != 206:
                    raise OSError(f"expected HTTP 206 for a range read of {url}, got {status}")
                if end is not None and len(data) != end - start + 1:
                    raise OSError(f"range read of {url} gave {len(data)} bytes, expected {end - start + 1}")
            return data
        wait(min(MAX_WAIT, WAITS[attempt] if delay is None else delay))
    raise AssertionError("unreachable")


def read(url: str, byte_range: tuple[int, int | None] | None = None) -> bytes:
    """The byte-range reader of IFS's description. It looks `http_get` up when called, so a test that
    replaces `ecmwf.http_get` replaces every read the pipeline makes from the mirror."""
    return http_get(url, byte_range)
