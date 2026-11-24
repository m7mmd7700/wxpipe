"""ECMWF IFS 0.25 degree files from ECMWF's open-data copies: index parsing, record pickers, HTTP reads.

The URLs and the run discovery come from IFS's description in `wxpipe.source`, whose copies are tried
in order: ECMWF's data portal, the AWS bucket's CloudFront door, the AWS bucket by its S3 host. An
index is one JSON object per line, each with `param`, `levtype`, `step`, `_offset` and `_length`.

`http_get` is one copy's read: up to four attempts, each answer checked as a whole GRIB2 message or a
readable index, and every attempt counted under the URL's host. `read` goes on to the next copy when
one fails, and the copy that answers stays current for the job's later reads. `reads_report` gives
the counts per copy for the run's summary. Standard library only, like `gfs.py`.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from . import source

USER_AGENT = "wxpipe/0.1 (personal weather app pipeline)"

# The waits between the four attempts of a read on one copy, in seconds; a Retry-After header's
# seconds replace the wait when given, at most MAX_WAIT. Then the read goes on to the next copy.
WAITS: tuple[int, ...] = (5, 10, 20)
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


# ---- the counts and the current copy, shared by a job's workers -----------------------------------

COUNTS = ("requests", "refused", "missing", "errors", "files", "bytes")

_lock = threading.Lock()
_current = 0                          # the index in source.roots(source.IFS) of the copy tried first
_counts: dict[str, dict[str, int]] = {}   # by host


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def _label(root: str) -> str:
    """A copy's name in the log and the summary: its ROOT without `https://`."""
    return source.label(root)


def _count(url: str, key: str, n: int = 1) -> None:
    with _lock:
        _counts.setdefault(_host(url), dict.fromkeys(COUNTS, 0))[key] += n


def reads_reset() -> None:
    """Makes the first copy current and zeroes every count; called at each IFS job's start."""
    global _current
    with _lock:
        _current = 0
        _counts.clear()


def reads_counts(url_or_root: str) -> dict[str, int]:
    """The counts so far of the host of [url_or_root], zeros when it had no request."""
    with _lock:
        return dict(_counts.get(_host(url_or_root), dict.fromkeys(COUNTS, 0)))


def reads_report(source_name: str, release: str) -> str:
    """The run summary's line of a job's reads: for each of IFS's copies in order, those with no request
    too, its requests, the 429 and 5xx answers among them, the 404s, the other errors, the files read
    and their megabytes."""
    parts = []
    for root in source.roots(source.IFS):
        c = reads_counts(root)
        parts.append(f"{_label(root)} {c['requests']} requests, {c['refused']} refused, {c['missing']} missing, "
                     f"{c['errors']} errors, {c['files']} files, {c['bytes'] / 1e6:.1f} MB")
    return f"**Reads:** {source_name} {release}: " + "; ".join(parts)


# ---- one copy's read ------------------------------------------------------------------------------

def _accept(url: str, byte_range: tuple[int, int | None] | None, status: int, data: bytes) -> None:
    """Raises OSError unless the answer is what was asked: a range, 206 with exactly the asked length and
    one whole GRIB2 message; a whole read of an index, UTF-8 text with at least one record; any other
    whole read, any answer."""
    if byte_range:
        start, end = byte_range
        if status != 206:
            raise OSError(f"expected HTTP 206 for a range read of {url}, got {status}")
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
    elif url.endswith(".index"):
        try:
            records = parse_idx(data.decode("utf-8"))
        except ValueError as e:   # UnicodeDecodeError among them
            raise OSError(f"index {url} does not parse: {str(e)[:200]}") from None   # a page can be long
        if not records:
            raise OSError(f"index {url} holds no record")


def http_get(url: str, byte_range: tuple[int, int | None] | None = None,
             opener: Callable | None = None, wait: Callable[[float], None] | None = None) -> bytes:
    """The body of [url] on one copy, or of its bytes [byte_range] inclusive.

    A 429 or 5xx answer, a timeout or a connection error is tried again, up to len(WAITS) + 1
    attempts, waiting WAITS or the Retry-After header's seconds, at most MAX_WAIT; any other HTTP
    error, a 404 among them, is raised at once. An answer is accepted only by `_accept`; one that is
    not raises OSError at once, with no second attempt. Every attempt is counted under the URL's host:
    requests, and one of refused (429, 5xx), missing (404), errors (anything else) or files with their
    bytes. [opener] and [wait] default to urllib's urlopen and time.sleep, looked up when called, and
    tests inject them.
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
        _count(url, "requests")
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
                status = response.status
                data = response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _count(url, "missing")
                raise
            if e.code != 429 and e.code < 500:
                _count(url, "errors")
                raise
            _count(url, "refused")
            if last:
                raise
            delay = _retry_after(e)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError):
            _count(url, "errors")
            if last:
                raise
        except BaseException:   # not a read's error: counted, so the counts add up, and raised at once
            _count(url, "errors")
            raise
        else:
            try:
                _accept(url, byte_range, status, data)
            except OSError:
                _count(url, "errors")
                raise
            _count(url, "files")
            _count(url, "bytes", len(data))
            return data
        wait(min(MAX_WAIT, WAITS[attempt] if delay is None else delay))
    raise AssertionError("unreachable")


# ---- the copies ---------------------------------------------------------------------------------

def read(url: str, byte_range: tuple[int, int | None] | None = None) -> bytes:
    """The byte-range reader of IFS's description: [url], built on IFS's base URL, read from its copies.

    The read tries the current copy, then each other copy in list order from there, wrapping round to
    the earlier ones last. A copy is passed over only on an OSError or an http.client.HTTPException
    from `http_get`; anything else stops the read at once. A read that answers on its first copy never
    moves the current copy; only a read that passed over a copy can move it: the copy that answered
    becomes the current one for every later read of the job, from every worker, with one log line
    naming the copy it leaves and the last error this read met there. After every copy failed, one
    OSError names each copy and its last error in the order tried. A URL not on the base URL raises
    ValueError.

    The known limit: after a move there is no return to the first copy until a read fails on every
    later one; the run summary's `**Reads:**` line shows that case.

    It looks `http_get` up when called, so a test that replaces `ecmwf.http_get` replaces every read
    the pipeline makes, its checks and its counts with it.
    """
    global _current
    roots = source.roots(source.IFS)
    urls = [source.on_root(source.IFS, url, root) for root in roots]
    with _lock:
        first = _current
    order = [(first + k) % len(roots) for k in range(len(roots))]
    failures: list[tuple[str, BaseException]] = []
    for i in order:
        try:
            data = http_get(urls[i], byte_range)
        except (OSError, http.client.HTTPException) as e:
            failures.append((roots[i], e))
            continue
        if failures:
            with _lock:
                left, moved = _current, _current != i
                _current = i
            if moved:
                # The last error met on the copy it leaves; the last error of all when this read never
                # tried that copy, another worker having moved the current copy meanwhile.
                error = next((e for r, e in reversed(failures) if r == roots[left]), failures[-1][1])
                print(f"ifs reads: moved from {_label(roots[left])} to {_label(roots[i])}: "
                      f"{type(error).__name__}: {error}")
        return data
    raise OSError("every copy failed: " + "; ".join(
        f"{_label(root)}: {type(e).__name__}: {e}" for root, e in failures))
