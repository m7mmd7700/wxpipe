"""The cheap check: its outputs, and that it and the other standard-library modules import no third-party package."""

import ast
import dataclasses
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wxpipe import check, ecmwf, gfs, source

ROOT = Path(__file__).resolve().parents[1]
STDLIB_MODULES = ["check", "source", "gfs", "ecmwf", "site", "gate", "summary"]
REAL_EXISTS = gfs.exists   # for the test of its answers, with an injected opener

BLOCKER = """
import sys

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("numpy", "eccodes"):
            raise ImportError("blocked " + name)
        return None

sys.meta_path.insert(0, Blocker())
"""


@pytest.fixture(autouse=True)
def no_requests(monkeypatch, tmp_path):
    """Every test here: any request fails the test unless a test replaces the fakes, and the workflow's
    files point into tmp_path."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setattr(ecmwf, "http_get", refuse)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))


def run_python(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_the_standard_library_modules_import_with_numpy_and_eccodes_blocked():
    imports = "".join(f"import wxpipe.{m}\n" for m in STDLIB_MODULES)
    done = run_python(BLOCKER + imports + "assert 'numpy' not in sys.modules and 'eccodes' not in sys.modules\n")
    assert done.returncode == 0, done.stderr


def test_the_blocker_blocks():
    done = run_python(BLOCKER + "import wxpipe.config\n")
    assert done.returncode != 0
    assert "blocked numpy" in done.stderr


@pytest.mark.parametrize("name", STDLIB_MODULES)
def test_the_standard_library_modules_parse_as_python_3_12(name):
    text = (ROOT / "wxpipe" / f"{name}.py").read_text(encoding="utf-8")
    ast.parse(text, feature_version=(3, 12))


# ---- the outputs ----------------------------------------------------------------------------

NOW = datetime(2026, 11, 8, 3, 41, tzinfo=timezone.utc)
NEWEST = "2026110800"


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """No request: the existence test answers only for the newest cycle's last index, and http_get
    returns the live manifest the test sets, or fails as unreadable."""
    live = {"body": None}
    complete = source.idx_url(source.GFS, datetime(2026, 11, 8, 0, tzinfo=timezone.utc), source.GFS.last_hour)

    def fake_get(url, byte_range=None, retries=4, timeout=60):
        assert url.endswith("/manifest.json") and byte_range is None, url
        if live["body"] is None:
            raise OSError("unreadable")
        return live["body"]

    monkeypatch.setattr(gfs, "http_get", fake_get)
    monkeypatch.setattr(gfs, "exists", lambda url: url == complete)
    monkeypatch.setattr(check, "now", lambda: NOW)
    output = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return live, output


def outputs_of(output: Path) -> dict:
    return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())


def test_a_new_gfs_run(offline):
    live, output = offline
    live["body"] = json.dumps({"sources": {"gfs": {"release": "2026110718"}}}).encode()
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "true", "build": f"gfs:{NEWEST}", "all": f"gfs:{NEWEST}"}


def test_a_current_gfs_run(offline):
    live, output = offline
    live["body"] = json.dumps({"sources": {"gfs": {"release": NEWEST}}}).encode()
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "false", "build": "", "all": f"gfs:{NEWEST}"}


def test_an_unreadable_live_manifest_counts_every_source_as_new(offline):
    live, output = offline
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "true", "build": f"gfs:{NEWEST}", "all": f"gfs:{NEWEST}"}
    live["body"] = b"not json"
    output.unlink()
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output)["new"] == "true"


def test_no_complete_run(offline, monkeypatch):
    live, output = offline
    monkeypatch.setattr(gfs, "exists", lambda url: False)
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "false", "build": "", "all": ""}


def test_the_check_asks_for_each_sources_own_last_hour(offline, monkeypatch):
    asked = []

    def exists(url):
        asked.append(url)
        return False

    monkeypatch.setattr(source, "INFOS", (dataclasses.replace(source.GFS, last_hour=7),))
    monkeypatch.setattr(gfs, "exists", exists)
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert len(asked) == source.GFS.lookback_cycles
    assert all(url.endswith(".f007.idx") for url in asked)


def test_outputs_with_two_sources():
    live = {"sources": {"gfs": {"release": "2026110800"}, "ifs": {"release": "2026110712"}}}
    got = check.outputs({"gfs": "2026110800", "ifs": "2026110800", "gfswave": None}, live)
    assert got == {"new": "true", "build": "ifs:2026110800", "all": "gfs:2026110800,ifs:2026110800"}


# ---- D5: only a newer release is new ------------------------------------------------------------

def test_only_a_newer_release_is_new():
    # A12.
    def live(release):
        return {"sources": {"ifs": {"release": release}}}

    assert check.is_new("ifs", "2026112312", live("2026112300"))
    assert not check.is_new("ifs", "2026112312", live("2026112312"))
    assert not check.is_new("ifs", "2026112300", live("2026112312"))   # discovery fell back to an older run
    assert check.is_new("ifs", "2026112312", None)                     # unreadable manifest
    assert check.is_new("ifs", "2026112312", {"sources": {"gfs": {"release": "2026112312"}}})
    other_digits = "".join(chr(0x0660 + int(d)) for d in "2026112400")   # digits that are not 0-9
    for bad in (2026112400, "20261124", "2026112400x", None, ["2026112400"], other_digits):
        assert check.is_new("ifs", "2026112312", live(bad)), bad
    assert check.outputs({"gfs": "2026110718", "ifs": "2026112300"},
                         {"sources": {"gfs": {"release": "2026110800"}, "ifs": {"release": "2026112300"}}}) == {
        "new": "false", "build": "", "all": "gfs:2026110718,ifs:2026112300"}


# ---- D6: discovery's errors stay in their source ---------------------------------------------------

def test_one_sources_discovery_error_leaves_the_other(offline, monkeypatch, capsys):
    # A13.
    live, output = offline
    live["body"] = json.dumps({"sources": {"gfs": {"release": "2026110718"}}}).encode()
    complete = source.idx_url(source.GFS, datetime(2026, 11, 8, 0, tzinfo=timezone.utc), source.GFS.last_hour)

    def exists(url):
        if any(url.startswith(root) for root in source.roots(source.IFS)):   # every IFS copy raises
            raise ConnectionError("connection reset")
        return url == complete

    monkeypatch.setattr(gfs, "exists", exists)
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "true", "build": f"gfs:{NEWEST}", "all": f"gfs:{NEWEST}"}
    printed = capsys.readouterr().out
    assert ("::warning::ifs: discovery failed, no complete run counted: ConnectionError: connection reset"
            in printed)
    assert "ifs discovery: the newest cycle: every copy raised\n" in printed   # F6 (c)

    # And the other way round: GFS's error leaves IFS's run as it would be.
    ifs_complete = source.idx_url(source.IFS, datetime(2026, 11, 7, 12, tzinfo=timezone.utc), 360)

    def exists2(url):
        if url.startswith(source.GFS.base_url):
            raise TimeoutError("timed out")
        return url == ifs_complete

    monkeypatch.setattr(gfs, "exists", exists2)
    output.unlink()
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "true", "build": "ifs:2026110712", "all": "ifs:2026110712"}


def test_a_non_read_error_on_a_copy_stops_that_sources_discovery(offline, monkeypatch, capsys):
    # F6 (a): not an OSError nor an HTTPException, so the other copies are not asked; GFS goes on.
    live, output = offline
    gfs_complete = source.idx_url(source.GFS, datetime(2026, 11, 8, 0, tzinfo=timezone.utc), source.GFS.last_hour)
    asked = []

    def exists(url):
        asked.append(url)
        if url.startswith(source.IFS.base_url):
            raise RuntimeError("a bug")
        return url == gfs_complete

    monkeypatch.setattr(gfs, "exists", exists)
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "true", "build": f"gfs:{NEWEST}", "all": f"gfs:{NEWEST}"}
    printed = capsys.readouterr().out
    assert "::warning::ifs: discovery failed, no complete run counted: RuntimeError: a bug" in printed
    assert "passed over" not in printed
    assert [u for u in asked if not u.startswith(source.GFS.base_url)] == [
        source.idx_url(source.IFS, datetime(2026, 11, 8, 0, tzinfo=timezone.utc), 360)]


def test_ifs_newest_run_found_on_the_second_copy(offline, monkeypatch, capsys):
    # B8: the first copy raises, the second has the run; GFS's lines and outputs as without copies.
    live, output = offline
    live["body"] = json.dumps({"sources": {"gfs": {"release": "2026110718"}, "ifs": {"release": "2026110700"}}}).encode()
    gfs_complete = source.idx_url(source.GFS, datetime(2026, 11, 8, 0, tzinfo=timezone.utc), source.GFS.last_hour)
    ifs_complete = source.idx_url(source.IFS, datetime(2026, 11, 7, 12, tzinfo=timezone.utc), 360)
    portal, cloudfront = source.IFS.copies[:2]
    asked = []

    def exists(url):
        asked.append(url)
        if url.startswith(portal):
            raise TimeoutError("timed out")
        if url.startswith(cloudfront):
            return url == cloudfront + ifs_complete[len(portal):]
        return url == gfs_complete

    monkeypatch.setattr(gfs, "exists", exists)
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "true", "build": f"gfs:{NEWEST},ifs:2026110712",
                                  "all": f"gfs:{NEWEST},ifs:2026110712"}
    printed = capsys.readouterr().out
    portal_label, cloudfront_label = (r.removeprefix("https://") for r in (portal, cloudfront))
    assert printed.splitlines() == [
        f"gfs: newest complete run {NEWEST}; on the site: 2026110718; build: yes",
        "gfswave: no complete run found",
        f"ifs discovery: {portal_label} passed over: TimeoutError: timed out",
        f"ifs discovery: {portal_label} passed over: TimeoutError: timed out",
        f"ifs discovery: the newest cycle decided by {cloudfront_label}: false",
        "ifs: newest complete run 2026110712; on the site: 2026110700; build: yes"]
    # GFS's bucket is asked for GFS's newest run, then for GFS-Wave's six cycles, none complete here.
    wave = [source.idx_url(source.GFSWAVE, datetime(2026, 11, d, h, tzinfo=timezone.utc), 384)
            for d, h in ((8, 0), (7, 18), (7, 12), (7, 6), (7, 0), (6, 18))]
    assert [u for u in asked if u.startswith(source.GFS.base_url)] == [gfs_complete] + wave


# ---- W6: a release the size gate held back is not rebuilt every hour ----------------------------------

def held_live(held=None, live="2026110712"):
    manifest = {"sources": {"gfs": {"release": "2026110800"}, "gfswave": {"release": live}}}
    if held is not None:
        manifest["held_back"] = held
    return manifest


def test_a_held_release_is_not_new():
    # A9: the gate held back 2026110718 and published 2026110712 as current.
    live = held_live({"gfswave": "2026110718"})
    assert check.held_release("gfswave", live) == "2026110718"
    assert check.held("gfswave", "2026110718", live) and check.held("gfswave", "2026110706", live)
    assert not check.held("gfswave", "2026110800", live)
    # The held release itself, and an earlier one, are not new; a later one is.
    assert check.outputs({"gfs": "2026110800", "gfswave": "2026110718"}, live) == {
        "new": "false", "build": "", "all": "gfs:2026110800,gfswave:2026110718"}
    assert check.outputs({"gfs": "2026110800", "gfswave": "2026110800"}, live) == {
        "new": "true", "build": "gfswave:2026110800", "all": "gfs:2026110800,gfswave:2026110800"}
    # is_new alone is unchanged: the held release is later than the live one.
    assert check.is_new("gfswave", "2026110718", live)


def test_a_held_back_entry_counts_only_for_its_source_and_a_good_release():
    assert not check.held("gfs", "2026110800", held_live({"gfswave": "2026110718"}))
    for bad in (None, {}, [], "2026110718", {"gfswave": 2026110718}, {"gfswave": "20261107"},
                {"gfswave": "".join(chr(0x0660 + int(d)) for d in "2026110718")}, {"gfswave": None}):
        live = held_live(bad)
        assert check.held_release("gfswave", live) is None and not check.held("gfswave", "2026110718", live), bad
        assert check.outputs({"gfswave": "2026110718"}, live)["build"] == "gfswave:2026110718"
    assert not check.held("gfswave", "2026110718", None)


def test_the_check_names_a_held_release(offline, monkeypatch, capsys):
    # A9: the printed line of a held source, through check.main.
    live, output = offline
    live["body"] = json.dumps(held_live({"gfswave": "2026110800"}, live="2026110718")).encode()
    gfs_complete = source.idx_url(source.GFS, datetime(2026, 11, 8, 0, tzinfo=timezone.utc), source.GFS.last_hour)
    wave_complete = source.idx_url(source.GFSWAVE, datetime(2026, 11, 8, 0, tzinfo=timezone.utc), 384)
    monkeypatch.setattr(gfs, "exists", lambda url: url in (gfs_complete, wave_complete))
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output) == {"new": "false", "build": "", "all": f"gfs:{NEWEST},gfswave:{NEWEST}"}
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [
        f"gfs: newest complete run {NEWEST}; on the site: {NEWEST}; build: no",
        f"gfswave: newest complete run {NEWEST}; on the site: 2026110718; build: no; "
        f"held back by the size gate: 2026110800"]
    # A later cycle than the held one is new, and its line says nothing of the gate.
    live["body"] = json.dumps(held_live({"gfswave": "2026110718"}, live="2026110712")).encode()
    output.unlink()
    assert check.main(["--manifest", "https://example.invalid/site/manifest.json"]) == 0
    assert outputs_of(output)["build"] == f"gfswave:{NEWEST}"
    assert f"gfswave: newest complete run {NEWEST}; on the site: 2026110712; build: yes" in capsys.readouterr().out.splitlines()


class Answer:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    import urllib.error
    return urllib.error.HTTPError("https://example.invalid/x", code, "answer", None, None)


def opener_of(*answers):
    """A urlopen that gives [answers] in turn: a status, or an exception to raise."""
    asked = []

    def opener(request, timeout=None):
        asked.append(request.get_method())
        answer = answers[len(asked) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return Answer(answer)

    opener.asked = asked
    return opener


def test_exists_answers_and_retries(monkeypatch):
    # A13: the real gfs.exists, taken before the autouse fixture replaced it, with an injected opener
    # and waits; no opener here reaches the network, and a sleep fails the test.
    import time

    def no_sleep(seconds):
        raise AssertionError("slept")

    monkeypatch.setattr(time, "sleep", no_sleep)
    exists = REAL_EXISTS
    waits = []
    assert exists("https://example.invalid/x", opener=opener_of(200), wait=waits.append) is True
    assert exists("https://example.invalid/x", opener=opener_of(http_error(404)), wait=waits.append) is False
    assert exists("https://example.invalid/x", opener=opener_of(http_error(403)), wait=waits.append) is False
    assert waits == []
    opener = opener_of(http_error(503), 200)
    assert exists("https://example.invalid/x", opener=opener, wait=waits.append) is True
    assert waits == [1] and opener.asked == ["HEAD", "HEAD"]
    waits.clear()
    opener = opener_of(http_error(429), TimeoutError("t"), ConnectionError("c"), http_error(503))
    with pytest.raises(Exception) as e:
        exists("https://example.invalid/x", opener=opener, wait=waits.append)
    assert getattr(e.value, "code", None) == 503
    assert waits == list(gfs.EXISTS_WAITS) == [1, 2, 4] and len(opener.asked) == 4
