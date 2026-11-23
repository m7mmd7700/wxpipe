"""ECMWF's index files and records on the fixtures of run 2026112312, and the mirror's reads with a fake
opener: no network, no sleep."""

import email.message
import time
import urllib.error
from pathlib import Path

import pytest

from wxpipe import config, ecmwf

FIXTURES = Path(__file__).parent / "fixtures" / "ifs"
INSTANT = ("2t:sfc", "10u:sfc", "10v:sfc", "msl:sfc", "tcc:sfc")
REAL_HTTP_GET = ecmwf.http_get


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    """Every test here: a real read or a real sleep fails the test; the tests of http_get pass their
    own opener and wait to the real function."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    def no_sleep(seconds):
        raise AssertionError("slept")

    monkeypatch.setattr(ecmwf, "http_get", refuse)
    monkeypatch.setattr(time, "sleep", no_sleep)


def records(fhour: int):
    return ecmwf.parse_idx((FIXTURES / f"ifs-f{fhour:03d}.index").read_text(encoding="utf-8"))


# ---- the index -------------------------------------------------------------------------------

def test_parse_idx_on_the_fixtures():
    # A4.
    assert [len(records(h)) for h in (0, 150, 360)] == [194, 191, 191]
    t2m = [r for r in records(0) if r.key == "2t:sfc"]
    assert len(t2m) == 1
    assert (t2m[0].number, t2m[0].offset, t2m[0].end, t2m[0].step) == (3, 1211505, 1525475, "0")
    first = records(0)[0]
    assert (first.number, first.offset, first.end, first.key) == (1, 0, 896850, "z:sfc")
    # Every record's numbers are its 1-based line, and the byte ranges do not overlap.
    for h in (0, 150, 360):
        recs = records(h)
        assert [r.number for r in recs] == list(range(1, len(recs) + 1))
        spans = sorted((r.offset, r.end) for r in recs)
        assert all(a_end < b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:]))


@pytest.mark.parametrize("line", [
    "not json",
    '["a", "list"]',
    '{"param": "2t", "levtype": "sfc", "step": "0", "_offset": 0}',
    '{"param": "2t", "levtype": "sfc", "_offset": 0, "_length": 10}',
    '{"param": "2t", "levtype": "sfc", "step": "0", "_offset": "0", "_length": 10}',
    '{"param": "2t", "levtype": "sfc", "step": "0", "_offset": 0, "_length": 0}',
])
def test_a_bad_index_line_raises(line):
    good = '{"param": "2t", "levtype": "sfc", "step": "0", "_offset": 0, "_length": 10}'
    assert len(ecmwf.parse_idx(good + "\n\n")) == 1
    with pytest.raises(ValueError, match="bad index line 2"):
        ecmwf.parse_idx(good + "\n" + line + "\n")


# ---- the records -----------------------------------------------------------------------------

@pytest.mark.parametrize("fhour", [0, 150, 360])
def test_each_instant_field_at_its_hour(fhour):
    # A5.
    recs = records(fhour)
    for key in INSTANT:
        r = ecmwf.instant_record(recs, key, fhour)
        assert r is not None and r.key == key and r.step == str(fhour)
    assert ecmwf.instant_record(recs, "2t:sfc", fhour + 3) is None
    assert ecmwf.instant_record(recs, "nothing:sfc", fhour) is None
    # Every field of IFS's description finds its record.
    for field in config.IFS.fields:
        if field.kind == "instant":
            assert config.IFS.instant_record(recs, field.grib, fhour) is not None


def test_tp_is_the_total_since_the_start():
    # A5: tp at hour h is keyed (0, h); nothing at hour 0; nothing for an absent key.
    for h in (150, 360):
        got = ecmwf.accumulation_records(records(h), "tp:sfc", h)
        assert list(got) == [(0, h)] and got[(0, h)].key == "tp:sfc" and got[(0, h)].step == str(h)
    assert ecmwf.accumulation_records(records(0), "tp:sfc", 0) == {}
    assert ecmwf.accumulation_records(records(150), "nothing:sfc", 150) == {}
    assert ecmwf.accumulation_records(records(150), "tp:sfc", 156) == {}
    tp150 = ecmwf.accumulation_records(records(150), "tp:sfc", 150)[(0, 150)]
    assert (tp150.number, tp150.offset, tp150.end) == (44, 18468191, 18468191 + 533093 - 1)


def test_a_step_written_as_a_window():
    # A5: a made-up "a-b" step is kept as (a, b) when b is the hour.
    text = "\n".join([
        '{"param": "tp", "levtype": "sfc", "step": "144-150", "_offset": 0, "_length": 10}',
        '{"param": "tp", "levtype": "sfc", "step": "0-150", "_offset": 10, "_length": 10}',
        '{"param": "tp", "levtype": "sfc", "step": "0-144", "_offset": 20, "_length": 10}',
        '{"param": "tp", "levtype": "sfc", "step": "144-150", "_offset": 30, "_length": 10}',
    ])
    recs = ecmwf.parse_idx(text)
    got = ecmwf.accumulation_records(recs, "tp:sfc", 150)
    assert sorted(got) == [(0, 150), (144, 150)]
    assert got[(144, 150)].number == 1   # the first copy is kept
    assert list(ecmwf.accumulation_records(recs, "tp:sfc", 144)) == [(0, 144)]


# ---- the mirror's reads (D7) --------------------------------------------------------------------

class Answer:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.invalid/x", code, "SlowDown", headers, None)


class Opener:
    """Gives its answers in turn, an Answer or an exception, and records each request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers[len(self.requests) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def get(opener, byte_range=None):
    waits = []
    try:
        return REAL_HTTP_GET("https://example.invalid/x", byte_range, opener=opener, wait=waits.append), waits
    except BaseException as e:
        e.waits = waits
        raise


def test_a_slowdown_then_the_body():
    # A6.
    opener = Opener(http_error(503), Answer(200, b"body"))
    assert get(opener) == (b"body", [5])
    assert opener.requests[0].get_header("User-agent") == ecmwf.USER_AGENT


def test_retry_after_is_honoured_and_capped():
    assert get(Opener(http_error(503, "7"), Answer(200, b"b"))) == (b"b", [7])
    assert get(Opener(http_error(429, "300"), Answer(200, b"b"))) == (b"b", [60])
    assert get(Opener(http_error(503, "soon"), Answer(200, b"b"))) == (b"b", [5])


def test_a_404_is_raised_at_once():
    opener = Opener(http_error(404))
    with pytest.raises(urllib.error.HTTPError) as e:
        get(opener)
    assert e.value.code == 404 and e.value.waits == [] and len(opener.requests) == 1
    with pytest.raises(urllib.error.HTTPError) as e:
        get(Opener(http_error(403)))
    assert e.value.waits == []


def test_ten_failures_raise_after_nine_waits():
    failures = [http_error(503), TimeoutError("timed out"), ConnectionError("reset"),
                urllib.error.URLError("no route")] + [http_error(500)] * 6
    opener = Opener(*failures)
    with pytest.raises(urllib.error.HTTPError) as e:
        get(opener)
    assert e.value.code == 500
    assert e.value.waits == [5, 10, 20, 40, 60, 60, 60, 60, 60] == list(ecmwf.WAITS)
    assert len(opener.requests) == 10


def test_a_range_read_needs_206_and_its_exact_length():
    opener = Opener(Answer(206, b"x" * 10))
    assert get(opener, (100, 109)) == (b"x" * 10, [])
    assert opener.requests[0].get_header("Range") == "bytes=100-109"
    with pytest.raises(OSError, match="expected HTTP 206"):
        get(Opener(Answer(200, b"x" * 10)), (100, 109))
    with pytest.raises(OSError, match="gave 9 bytes, expected 10"):
        get(Opener(Answer(206, b"x" * 9)), (100, 109))
    # A whole read takes any 200.
    assert get(Opener(Answer(200, b"whole"))) == (b"whole", [])


def test_read_goes_through_http_get_looked_up_when_called(monkeypatch):
    asked = []
    monkeypatch.setattr(ecmwf, "http_get", lambda url, byte_range=None: asked.append((url, byte_range)) or b"ok")
    assert ecmwf.read("https://example.invalid/a.index") == b"ok"
    assert config.IFS.read("https://example.invalid/a.grib2", (0, 9)) == b"ok"
    assert asked == [("https://example.invalid/a.index", None), ("https://example.invalid/a.grib2", (0, 9))]
