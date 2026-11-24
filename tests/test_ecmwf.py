"""ECMWF's index files and records on the fixtures of run 2026112312, and the reads of ECMWF's copies
with a fake opener: no network, no sleep."""

import email.message
import http.client
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wxpipe import config, ecmwf, source

FIXTURES = Path(__file__).parent / "fixtures" / "ifs"
INSTANT = ("2t:sfc", "10u:sfc", "10v:sfc", "msl:sfc", "tcc:sfc")
REAL_HTTP_GET = ecmwf.http_get
PORTAL, CLOUDFRONT, BUCKET = source.IFS.copies
HOSTS = ("data.ecmwf.int/forecasts", "d2zvc0wgha4k2l.cloudfront.net", "ecmwf-forecasts.s3.eu-central-1.amazonaws.com")


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    """Every test here: a real read or a real sleep fails the test; the tests of http_get pass their
    own opener and wait to the real function. Each test starts on the first copy with no counts."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    def no_sleep(seconds):
        raise AssertionError("slept")

    monkeypatch.setattr(ecmwf, "http_get", refuse)
    monkeypatch.setattr(time, "sleep", no_sleep)
    ecmwf.reads_reset()
    yield
    ecmwf.reads_reset()


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


# ---- one copy's reads (D7, M4) ------------------------------------------------------------------

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


def test_four_failures_raise_after_three_waits():
    # A6, changed by M4: four attempts on one copy.
    failures = [http_error(503), TimeoutError("timed out"), ConnectionError("reset"), http_error(500)]
    opener = Opener(*failures)
    with pytest.raises(urllib.error.HTTPError) as e:
        get(opener)
    assert e.value.code == 500
    assert e.value.waits == [5, 10, 20] == list(ecmwf.WAITS)
    assert len(opener.requests) == 4
    # A URL error and an HTTPException are tried again too.
    opener = Opener(urllib.error.URLError("no route"), http.client.IncompleteRead(b"x"), Answer(200, b"b"))
    assert get(opener) == (b"b", [5, 10])


def grib2(n: int) -> bytes:
    """A made-up GRIB2 message of [n] bytes, n >= 20: its markers, edition and length field right."""
    return b"GRIB" + b"\0\0\0" + b"\x02" + n.to_bytes(8, "big") + b"\0" * (n - 20) + b"7777"


def test_a_range_read_needs_206_and_its_exact_length():
    opener = Opener(Answer(206, grib2(20)))
    assert get(opener, (100, 119)) == (grib2(20), [])
    assert opener.requests[0].get_header("Range") == "bytes=100-119"
    with pytest.raises(OSError, match="expected HTTP 206"):
        get(Opener(Answer(200, b"x" * 10)), (100, 109))
    with pytest.raises(OSError, match="gave 9 bytes, expected 10"):
        get(Opener(Answer(206, b"x" * 9)), (100, 109))
    # A whole read takes any 200.
    assert get(Opener(Answer(200, b"whole"))) == (b"whole", [])


def test_read_goes_through_http_get_looked_up_when_called(monkeypatch):
    asked = []
    monkeypatch.setattr(ecmwf, "http_get", lambda url, byte_range=None: asked.append((url, byte_range)) or b"ok")
    assert ecmwf.read(PORTAL + "/a.index") == b"ok"
    assert config.IFS.read(PORTAL + "/a.grib2", (0, 9)) == b"ok"
    assert asked == [(PORTAL + "/a.index", None), (PORTAL + "/a.grib2", (0, 9))]
    with pytest.raises(ValueError, match="is not on"):
        ecmwf.read("https://example.invalid/a.index")
    assert len(asked) == 2


# ---- M3, M4: the copies, through the real http_get ------------------------------------------------

INDEX_BODY = b'{"param": "2t", "levtype": "sfc", "step": "0", "_offset": 0, "_length": 20}\n'
PATH = "/20261123/12z/ifs/0p25/oper/20261123120000-0h-oper-fc"


class Copies:
    """A urlopen routed by the request's ROOT: each copy gives its answers in turn, its last one again
    once they run out. Records every request's URL; [hold] is called before each request."""

    def __init__(self, portal=(), cloudfront=(), bucket=(), hold=None):
        self.answers = {PORTAL: list(portal), CLOUDFRONT: list(cloudfront), BUCKET: list(bucket)}
        self.asked = {PORTAL: 0, CLOUDFRONT: 0, BUCKET: 0}
        self.urls = []
        self.hold = hold
        self.lock = threading.Lock()

    def __call__(self, request, timeout=None):
        url = request.full_url
        root = next(r for r in self.answers if url.startswith(r))
        if self.hold is not None:
            self.hold(root)
        with self.lock:
            self.urls.append(url)
            answers = self.answers[root]
            answer = answers[min(self.asked[root], len(answers) - 1)]
            self.asked[root] += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def copies(monkeypatch):
    """The real http_get, with urlopen replaced by a Copies the test sets and time.sleep by a list."""
    waits = []
    box = {}
    monkeypatch.setattr(ecmwf, "http_get", REAL_HTTP_GET)
    monkeypatch.setattr(time, "sleep", waits.append)
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: box["copies"](request, timeout))

    def use(c):
        box["copies"] = c
        return c

    use.waits = waits
    return use


def test_the_first_copy_answering_is_read_in_one_request(copies, capsys):
    # B3.
    c = copies(Copies(portal=[Answer(200, INDEX_BODY)]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert c.urls == [PORTAL + PATH + ".index"] and copies.waits == []
    assert "moved" not in capsys.readouterr().out


def test_four_refusals_move_the_read_and_the_current_copy(copies, capsys):
    # B3: 503 four times on the first copy, then 200 on the second.
    c = copies(Copies(portal=[http_error(503)], cloudfront=[Answer(200, INDEX_BODY)]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert c.urls == [PORTAL + PATH + ".index"] * 4 + [CLOUDFRONT + PATH + ".index"]
    assert copies.waits == [5, 10, 20]
    assert capsys.readouterr().out == (
        f"ifs reads: moved from {HOSTS[0]} to {HOSTS[1]}: HTTPError: HTTP Error 503: SlowDown\n")
    # The next read starts on the second copy, with no line.
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert c.urls[5:] == [CLOUDFRONT + PATH + ".index"]
    assert capsys.readouterr().out == ""


def test_a_404_and_a_409_go_on_at_once(copies):
    # B3.
    for code in (404, 409):
        ecmwf.reads_reset()
        c = copies(Copies(portal=[http_error(code)], cloudfront=[Answer(200, INDEX_BODY)]))
        assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
        assert c.urls == [PORTAL + PATH + ".index", CLOUDFRONT + PATH + ".index"]
    assert copies.waits == []


def test_every_copy_failing_raises_one_error_naming_each(copies):
    # B3: 4 attempts on each, 12 in all, one OSError naming each copy and its last error in order.
    c = copies(Copies(portal=[http_error(503)], cloudfront=[TimeoutError("timed out")],
                      bucket=[http_error(500)]))
    with pytest.raises(OSError) as e:
        ecmwf.read(PORTAL + PATH + ".index")
    assert str(e.value) == (f"every copy failed: {HOSTS[0]}: HTTPError: HTTP Error 503: SlowDown; "
                            f"{HOSTS[1]}: TimeoutError: timed out; {HOSTS[2]}: HTTPError: HTTP Error 500: SlowDown")
    assert c.asked == {PORTAL: 4, CLOUDFRONT: 4, BUCKET: 4} and len(c.urls) == 12
    assert copies.waits == [5, 10, 20] * 3


def test_retry_after_is_honoured_and_capped_on_a_copy(copies):
    # B3.
    copies(Copies(portal=[http_error(503, "7"), http_error(429, "300"), Answer(200, INDEX_BODY)]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert copies.waits == [7, 60]


def test_a_failure_on_the_third_copy_wraps_to_the_first(copies, capsys):
    # B3.
    c = copies(Copies(portal=[http_error(404), Answer(200, INDEX_BODY)], cloudfront=[http_error(404)],
                      bucket=[Answer(200, INDEX_BODY), http_error(404)]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY     # the third copy becomes current
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY     # and its 404 wraps to the first
    assert c.urls == [PORTAL + PATH + ".index", CLOUDFRONT + PATH + ".index", BUCKET + PATH + ".index",
                      BUCKET + PATH + ".index", PORTAL + PATH + ".index"]
    out = capsys.readouterr().out.splitlines()
    assert out == [f"ifs reads: moved from {HOSTS[0]} to {HOSTS[2]}: HTTPError: HTTP Error 404: SlowDown",
                   f"ifs reads: moved from {HOSTS[2]} to {HOSTS[0]}: HTTPError: HTTP Error 404: SlowDown"]
    assert copies.waits == []


def test_another_exception_stops_the_read_at_once(copies, monkeypatch):
    # B3: neither an OSError nor an http.client.HTTPException.
    c = copies(Copies(portal=[RuntimeError("a bug")], cloudfront=[Answer(200, INDEX_BODY)]))
    with pytest.raises(RuntimeError, match="a bug"):
        ecmwf.read(PORTAL + PATH + ".index")
    assert c.urls == [PORTAL + PATH + ".index"] and copies.waits == []
    p = ecmwf.reads_counts(PORTAL)
    assert (p["requests"], p["errors"]) == (1, 1)
    # A fake http_get raising so stops it too.
    asked = []

    def fake(url, byte_range=None):
        asked.append(url)
        raise AssertionError("refused")

    monkeypatch.setattr(ecmwf, "http_get", fake)
    with pytest.raises(AssertionError):
        ecmwf.read(PORTAL + PATH + ".index")
    assert asked == [PORTAL + PATH + ".index"]


# ---- M6: each answer checked ----------------------------------------------------------------------

def bad_messages():
    good = grib2(40)
    return {
        "a 200": Answer(200, good),
        "a short body": Answer(206, good[:39]),
        "no GRIB": Answer(206, b"GRIX" + good[4:]),
        "edition 1": Answer(206, good[:7] + b"\x01" + good[8:]),
        "a length field off": Answer(206, good[:8] + (41).to_bytes(8, "big") + good[16:]),
        "no 7777": Answer(206, good[:-4] + b"7778"),
    }


@pytest.mark.parametrize("case", list(bad_messages()))
def test_a_bad_message_is_read_from_the_next_copy(copies, case):
    # B4.
    c = copies(Copies(portal=[bad_messages()[case]], cloudfront=[Answer(206, grib2(40))]))
    assert ecmwf.read(PORTAL + PATH + ".grib2", (1000, 1039)) == grib2(40)
    assert c.urls == [PORTAL + PATH + ".grib2", CLOUDFRONT + PATH + ".grib2"] and copies.waits == []
    assert ecmwf.reads_counts(PORTAL)["errors"] == 1


@pytest.mark.parametrize("body", [b"\xff\xfe not UTF-8", b"<html>busy</html>\n", b"", b"\n\n"])
def test_a_bad_index_is_read_from_the_next_copy(copies, body):
    # B4: not UTF-8, not an index, or no record.
    c = copies(Copies(portal=[Answer(200, body)], cloudfront=[Answer(200, INDEX_BODY)]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert c.urls == [PORTAL + PATH + ".index", CLOUDFRONT + PATH + ".index"]
    assert ecmwf.reads_counts(PORTAL)["errors"] == 1


def test_a_good_index_and_another_whole_read_are_taken_as_they_are(copies):
    # B4: an index needs no GRIB markers; a whole read of another URL takes any 200.
    c = copies(Copies(portal=[Answer(200, INDEX_BODY), Answer(200, b"any text")]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert ecmwf.read(PORTAL + "/LICENCE.txt") == b"any text"
    assert len(c.urls) == 2


# ---- M7: the counts ------------------------------------------------------------------------------

def test_the_counts_of_a_scripted_sequence(copies):
    # B5: per copy, requests = refused + missing + errors + files.
    copies(Copies(portal=[http_error(503)],
                  cloudfront=[TimeoutError("t"), Answer(200, INDEX_BODY), http_error(404), http_error(403)],
                  bucket=[Answer(206, grib2(40)), Answer(200, b"busy"), Answer(206, grib2(30))]))
    # The portal refuses 4 times; the second copy times out once, then answers.
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    # The second copy has no such file; the bucket has it.
    assert ecmwf.read(PORTAL + PATH + ".grib2", (0, 39)) == grib2(40)
    # The bucket answers a page, the portal refuses 4 times more, the second copy answers 403.
    with pytest.raises(OSError, match="every copy failed"):
        ecmwf.read(PORTAL + PATH + ".index")
    p, cf, b = (ecmwf.reads_counts(r) for r in (PORTAL, CLOUDFRONT, BUCKET))
    assert p == {"requests": 8, "refused": 8, "missing": 0, "errors": 0, "files": 0, "bytes": 0}
    assert cf == {"requests": 4, "refused": 0, "missing": 1, "errors": 2, "files": 1, "bytes": len(INDEX_BODY)}
    assert b == {"requests": 2, "refused": 0, "missing": 0, "errors": 1, "files": 1, "bytes": 40}
    assert copies.waits == [5, 10, 20, 5, 5, 10, 20]
    for c in (p, cf, b):
        assert c["requests"] == c["refused"] + c["missing"] + c["errors"] + c["files"]


def test_reads_report_and_reset(copies):
    # B5: the line's exact text, every copy in order, one with no request; a host on no copy left out.
    copies(Copies(portal=[http_error(404)], cloudfront=[Answer(200, INDEX_BODY)]))
    REAL_HTTP_GET("https://example.invalid/x.txt", opener=lambda r, timeout=None: Answer(200, b"z"), wait=None)
    assert ecmwf.reads_counts("https://example.invalid")["files"] == 1
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert ecmwf.reads_report("ifs", "2026112312") == (
        f"**Reads:** ifs 2026112312: {HOSTS[0]} 1 requests, 0 refused, 1 missing, 0 errors, 0 files, 0.0 MB; "
        f"{HOSTS[1]} 1 requests, 0 refused, 0 missing, 0 errors, 1 files, 0.0 MB; "
        f"{HOSTS[2]} 0 requests, 0 refused, 0 missing, 0 errors, 0 files, 0.0 MB")


def test_reads_report_gives_megabytes_with_one_decimal(copies):
    copies(Copies(portal=[Answer(206, grib2(1_250_000))]))
    ecmwf.read(PORTAL + PATH + ".grib2", (0, 1_249_999))
    assert f"{HOSTS[0]} 1 requests, 0 refused, 0 missing, 0 errors, 1 files, 1.2 MB;" in ecmwf.reads_report("ifs", "r")


def test_reads_reset_zeroes_the_counts_and_makes_the_first_copy_current(copies):
    c = copies(Copies(portal=[http_error(404)], cloudfront=[Answer(200, INDEX_BODY)]))
    ecmwf.read(PORTAL + PATH + ".index")
    ecmwf.reads_reset()
    assert all(ecmwf.reads_counts(r)["requests"] == 0 for r in (PORTAL, CLOUDFRONT, BUCKET))
    assert ecmwf.reads_counts("https://example.invalid")["requests"] == 0
    c.answers[PORTAL] = [Answer(200, INDEX_BODY)]
    c.asked[PORTAL] = 0
    ecmwf.read(PORTAL + PATH + ".index")
    assert c.urls[-1] == PORTAL + PATH + ".index"


# ---- two workers -------------------------------------------------------------------------------

def test_two_workers_failing_together_move_the_current_copy_once(copies, capsys):
    # B6: both threads held at their first request until both have started on the first copy.
    barrier = threading.Barrier(2)
    first = {"held": 0}
    lock = threading.Lock()

    def hold(root):
        if root != PORTAL:
            return
        with lock:
            first["held"] += 1
            wait = first["held"] <= 2
        if wait:
            barrier.wait(timeout=60)

    c = copies(Copies(portal=[http_error(503)], cloudfront=[Answer(200, INDEX_BODY)], hold=hold))
    results, errors = [], []

    def worker():
        try:
            results.append(ecmwf.read(PORTAL + PATH + ".index"))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == [] and results == [INDEX_BODY, INDEX_BODY]
    assert c.asked == {PORTAL: 8, CLOUDFRONT: 2, BUCKET: 0}
    out = capsys.readouterr().out.splitlines()
    assert out == [f"ifs reads: moved from {HOSTS[0]} to {HOSTS[1]}: HTTPError: HTTP Error 503: SlowDown"]
    p, cf = ecmwf.reads_counts(PORTAL), ecmwf.reads_counts(CLOUDFRONT)
    assert (p["requests"], p["refused"], cf["requests"], cf["files"]) == (8, 8, 2, 2)
    # The next read starts on the second copy.
    ecmwf.read(PORTAL + PATH + ".index")
    assert c.urls[-1] == CLOUDFRONT + PATH + ".index"


# ---- the fix round --------------------------------------------------------------------------------

def test_an_http_exception_on_every_attempt_passes_the_copy_over(copies, capsys):
    # F1: IncompleteRead is not an OSError; the read still goes on to the next copy.
    c = copies(Copies(portal=[http.client.IncompleteRead(b"x")], cloudfront=[Answer(200, INDEX_BODY)]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert c.asked[PORTAL] == 4 and c.asked[CLOUDFRONT] == 1
    assert capsys.readouterr().out == (
        f"ifs reads: moved from {HOSTS[0]} to {HOSTS[1]}: IncompleteRead: IncompleteRead(1 bytes read)\n")
    ecmwf.read(PORTAL + PATH + ".index")
    assert c.urls[-1] == CLOUDFRONT + PATH + ".index"   # the current copy


def test_a_read_answering_on_its_first_copy_never_moves_the_current_copy(copies, capsys):
    # F2: another worker moves the current copy while this read is under way; this read then answers
    # on its own first copy, the portal, and leaves the current copy where the other worker set it.
    state = {"held": False}

    def hold(root):
        if root == PORTAL and not state["held"]:
            state["held"] = True
            assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY   # the other worker's read

    c = copies(Copies(portal=[http_error(404), Answer(200, INDEX_BODY)], cloudfront=[Answer(200, INDEX_BODY)],
                      hold=hold))
    assert ecmwf.read(PORTAL + PATH + ".grib2" + ".index") == INDEX_BODY
    assert c.urls == [PORTAL + PATH + ".index", CLOUDFRONT + PATH + ".index", PORTAL + PATH + ".grib2.index"]
    assert capsys.readouterr().out == f"ifs reads: moved from {HOSTS[0]} to {HOSTS[1]}: HTTPError: HTTP Error 404: SlowDown\n"
    ecmwf.read(PORTAL + PATH + ".index")
    assert c.urls[-1] == CLOUDFRONT + PATH + ".index"   # still the other worker's copy


def test_the_move_line_names_the_error_met_on_the_copy_it_leaves(copies, capsys):
    # F5: the portal refuses, CloudFront times out, the bucket answers: the line names the portal's 503.
    copies(Copies(portal=[http_error(503)], cloudfront=[TimeoutError("timed out")], bucket=[Answer(200, INDEX_BODY)]))
    assert ecmwf.read(PORTAL + PATH + ".index") == INDEX_BODY
    assert capsys.readouterr().out == (
        f"ifs reads: moved from {HOSTS[0]} to {HOSTS[2]}: HTTPError: HTTP Error 503: SlowDown\n")


@pytest.mark.parametrize("name", ["ifs-2t-ccsds.grib2", "ifs-msl-ccsds.grib2"])
def test_the_committed_real_messages_pass_the_check(name):
    # F3: each committed message, found from the package's own folder (R10), passes the range check as
    # a whole message; nothing is decoded (R9).
    from datetime import datetime, timezone

    data = (Path(ecmwf.__file__).parent / "samples" / name).read_bytes()
    url = source.file_url(source.IFS, datetime(2026, 11, 23, 12, tzinfo=timezone.utc), 0)
    assert url.startswith(PORTAL)
    ecmwf._accept(url, (0, len(data) - 1), 206, data)
