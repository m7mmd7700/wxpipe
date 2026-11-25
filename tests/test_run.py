"""run.py offline: gaps, each source failing on its own, IFS deferred by a run that builds GFS while
GFS-Wave is built in it, the stage folder, the cache paths, the summary and the outputs."""

import dataclasses
import email.message
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, ecmwf, gfs, grib, run, site, source

FIXTURES = Path(__file__).parent / "fixtures"
REAL_HTTP_GET = ecmwf.http_get   # taken before the autouse fixture replaces it
REAL_SUBPROCESS_RUN = subprocess.run   # likewise, for the one test of a real child process
HOURS = (0, 6, 7, 120, 384)
CYCLE = "2026110712"
IFS_CYCLE = "2026112312"
IFS_HOURS = (0, 150, 360)


def fixture_text(fhour: int) -> str:
    return (FIXTURES / f"gfs.t12z.f{fhour:03d}.idx").read_text(encoding="utf-8")


def index(hours=HOURS, drop=()):
    """The fixtures' records by hour, without the (hour, "VAR:level", fcst) records of [drop]."""
    out = {}
    for h in hours:
        out[h] = [r for r in gfs.parse_idx(fixture_text(h)) if (h, r.key, r.fcst) not in drop]
    return out


@pytest.fixture(autouse=True)
def no_requests(monkeypatch, tmp_path):
    """Every test here: any request fails the test, and the workflow's files point into tmp_path. A job
    skips on any exception, so each refused call is also recorded and fails the test at its end."""
    made = []

    def refuse(*args, **kwargs):
        made.append(args)
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setattr(ecmwf, "http_get", refuse)
    monkeypatch.setattr(run, "probe", refuse)   # a test with an ifs job names its own probe
    monkeypatch.setattr(run.subprocess, "run", refuse)   # a test of a child job names its own (F1)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    ecmwf.reads_reset()   # each test starts on IFS's first copy with no counts
    yield tmp_path
    ecmwf.reads_reset()
    assert made == [], f"requests were made: {made!r}"


# ---- gaps -----------------------------------------------------------------------------------

def test_no_gap_on_the_full_set():
    for method in ("bucket", "since-start"):
        assert run.gaps(config.GFS, index(), HOURS, method) == []


def test_a_missing_instant_record_is_a_gap():
    idx = index(drop={(6, "TMP:2 m above ground", "6 hour fcst")})
    assert run.gaps(config.GFS, idx, HOURS, "bucket") == [("temperature.2m", 6)]
    idx = index(drop={(0, "TCDC:entire atmosphere", "anl")})
    assert run.gaps(config.GFS, idx, HOURS, "bucket") == [("cloud.total", 0)]


def test_missing_accumulations_are_a_gap():
    idx = index(drop={(7, "APCP:surface", "6-7 hour acc fcst"), (7, "APCP:surface", "0-7 hour acc fcst")})
    # Hour 7's interval has nothing; hour 120's interval needs (0, 7) to subtract.
    assert run.gaps(config.GFS, idx, HOURS, "bucket") == [("precipitation.sfc", 7), ("precipitation.sfc", 120)]


def test_the_first_frame_needs_no_accumulation():
    assert gfs.accumulation_records(index()[0], "APCP:surface", 0) == {}
    assert run.gaps(config.GFS, index(), HOURS, "bucket") == []
    idx = index(hours=(6, 7), drop={(6, "APCP:surface", "0-6 hour acc fcst")})
    assert run.gaps(config.GFS, idx, (6, 7), "bucket") == []


def test_a_missing_earlier_window_is_a_gap_at_the_interval_that_needs_it():
    idx = index(drop={(7, "APCP:surface", "0-7 hour acc fcst")})
    assert run.gaps(config.GFS, idx, HOURS, "bucket") == [("precipitation.sfc", 120)]
    # since-start needs (0, 7) at hour 7 itself as well.
    assert run.gaps(config.GFS, idx, HOURS, "since-start") == [("precipitation.sfc", 7), ("precipitation.sfc", 120)]


def test_gaps_of_chosen_fields_only():
    idx = index(drop={(6, "TMP:2 m above ground", "6 hour fcst")})
    assert run.gaps(config.GFS, idx, HOURS, "bucket", [config.GFS.field("cloud.total")]) == []


def test_repeated_records_are_accepted():
    # f006 holds APCP 0-6 twice; the first copy is kept.
    recs = gfs.accumulation_records(index()[6], "APCP:surface", 6)
    assert recs[(0, 6)].number == 596
    assert run.gaps(config.GFS, index(), HOURS, "bucket") == []


# ---- a refused run through run.main -----------------------------------------------------------

def test_a_refused_run_changes_nothing(no_requests):
    tmp = no_requests
    work = tmp / "work"
    cache = work / "gfs" / CYCLE
    cache.mkdir(parents=True)
    for h in (0, 6, 7):
        text = fixture_text(h)
        if h == 6:
            text = "\n".join(line for line in text.splitlines() if ":UGRD:10 m above ground:6 hour fcst:" not in line) + "\n"
        (cache / f"f{h:03d}.idx").write_bytes(text.encode("utf-8"))
    out = tmp / "site"
    release = out / "gfs" / CYCLE
    release.mkdir(parents=True)
    (release / "tile.wxt").write_bytes(b"old tile")
    manifest = b'{"version": 1, "sources": {"gfs": {"release": "2026110712"}}}'
    (out / "manifest.json").write_bytes(manifest)

    with pytest.raises(run.RunRefused, match=r"wind_u\.10m at hour 6"):
        run.main(["--out", str(out), "--work", str(work), "--cycle", CYCLE, "--hours", "0,6,7"])

    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*")) == [
        "gfs", f"gfs/{CYCLE}", f"gfs/{CYCLE}/tile.wxt", "manifest.json"]
    assert (release / "tile.wxt").read_bytes() == b"old tile"
    assert (out / "manifest.json").read_bytes() == manifest
    assert not (tmp / "output.txt").exists()
    assert "wind_u.10m at hour 6" in (tmp / "summary.md").read_text(encoding="utf-8")


# ---- a whole local build with fakes, no GRIB data -----------------------------------------------

def fake_build(monkeypatch, work: Path):
    """The fixtures' index files in the cache, and decode replaced by a constant grid."""
    cache = work / "gfs" / CYCLE
    cache.mkdir(parents=True, exist_ok=True)
    for h in (0, 6, 7):
        (cache / f"f{h:03d}.idx").write_bytes(fixture_text(h).encode("utf-8"))
    monkeypatch.setattr(run.Fetcher, "messages", lambda self, items: {(h, r.number): b"" for h, r in items})
    monkeypatch.setattr(grib, "decode", lambda message, src: np.full((config.NY, config.NX), 280.0))


def test_a_local_build_writes_the_manifest_and_keeps_the_previous_release_on_a_rerun(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    fake_build(monkeypatch, work)
    old = {"release": "2026110706", "path": "gfs/2026110706"}
    older = tmp / "site" / "gfs" / "2026110700"
    older.mkdir(parents=True)
    (out / "gfs" / "2026110706").mkdir()
    other = out / "ifs" / "2026110700"
    other.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"version": 1, "generated_at": "x", "sources": {
        "gfs": old, "ifs": {"release": "2026110700", "path": "ifs/2026110700"}}}), encoding="utf-8")
    args = ["--out", str(out), "--work", str(work), "--cycle", CYCLE, "--hours", "0,6,7", "--fields", "temperature.2m"]

    assert run.main(args) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfs"]["release"] == CYCLE
    assert manifest["sources"]["ifs"]["release"] == "2026110700"
    assert manifest["previous"] == {"gfs": old}
    assert not older.exists() and (out / "gfs" / "2026110706").is_dir() and other.is_dir()
    assert len(list((out / "gfs" / CYCLE / "temperature.2m").rglob("*.wxt"))) == 108
    outputs = (tmp / "output.txt").read_text(encoding="utf-8")
    assert outputs == f"published=true\nrelease={CYCLE}\n"
    assert f"gfs {CYCLE}: 3 frames, 1 fields" in (tmp / "summary.md").read_text(encoding="utf-8")

    # A rerun of the same release keeps the previous one (D4).
    assert run.main(args) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["previous"] == {"gfs": old}
    assert (out / "gfs" / "2026110706").is_dir()


def test_the_workflow_form_builds_every_source_when_the_manifest_is_lost(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    fake_build(monkeypatch, work)
    # The workflow's form builds every forecast hour and field of a source; three hours and one field here.
    small = dataclasses.replace(config.GFS, forecast_hours=(0, 6, 7), fields=config.GFS.fields[:1])
    monkeypatch.setattr(config, "SOURCES", (small,))

    assert run.main(["--out", str(out), "--work", str(work), "--cycles", "", "--all-cycles", f"gfs:{CYCLE}"]) == 0
    assert (out / "gfs" / CYCLE).is_dir()
    assert "published=true" in (tmp / "output.txt").read_text(encoding="utf-8")
    assert (tmp / "summary.md").read_text(encoding="utf-8").startswith(run.LOST_MANIFEST + "\n")
    assert run.LOST_MANIFEST == "The site has no readable manifest.json: every source is built in this run."

    # With a manifest on the site, only --cycles is built: nothing here, and no line about a lost cache.
    (tmp / "output.txt").unlink()
    (tmp / "summary.md").unlink()
    assert run.main(["--out", str(out), "--work", str(work), "--cycles", "", "--all-cycles", f"gfs:{CYCLE}"]) == 0
    assert (tmp / "output.txt").read_text(encoding="utf-8") == "published=false\n"
    assert not (tmp / "summary.md").exists()


# ---- two sources ----------------------------------------------------------------------------------

def two_sources(monkeypatch, work: Path, alt_drop: str | None = None):
    """GFS and a synthetic source "alt" with GFS's own description, three hours and one field each;
    the fixtures' index files in both caches, [alt_drop] removed from alt's hour 6. "alt" comes first in
    the sources' order, so it runs before GFS; like GFS's own description it does not wait for GFS, so
    a run that builds GFS would not defer it either."""
    from wxpipe import source

    fake_build(monkeypatch, work)
    gfs_small = dataclasses.replace(config.GFS, forecast_hours=(0, 6, 7), fields=config.GFS.fields[:1])
    alt = dataclasses.replace(gfs_small, info=dataclasses.replace(source.GFS, name="alt"))
    monkeypatch.setattr(config, "SOURCES", (alt, gfs_small))
    monkeypatch.setattr(source, "INFOS", (alt.info, gfs_small.info))
    cache = work / "alt" / CYCLE
    cache.mkdir(parents=True)
    for h in (0, 6, 7):
        text = fixture_text(h)
        if h == 6 and alt_drop:
            text = "\n".join(line for line in text.splitlines() if alt_drop not in line) + "\n"
        (cache / f"f{h:03d}.idx").write_bytes(text.encode("utf-8"))


def site_with_two_sources(out: Path) -> bytes:
    for path in ("gfs/2026110706", "gfs/2026110700", "alt/2026110706"):
        (out / path).mkdir(parents=True)
        (out / path / "0-0.wxt").write_bytes(b"old " + path.encode())
    manifest = json.dumps({"version": 1, "generated_at": "x", "sources": {
        "gfs": {"release": "2026110706", "path": "gfs/2026110706"},
        "alt": {"release": "2026110706", "path": "alt/2026110706"}},
        "previous": {"gfs": {"release": "2026110700", "path": "gfs/2026110700"}}}, indent=1).encode()
    (out / "manifest.json").write_bytes(manifest)
    return manifest


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


def test_a_gap_in_another_source_skips_it_and_builds_gfs(no_requests, monkeypatch, capsys):
    # Rewritten by D3: a gap in one source skips that source only (was: the whole run refused).
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    two_sources(monkeypatch, work, alt_drop=":TMP:2 m above ground:6 hour fcst:")
    site_with_two_sources(out)
    before = tree(out)

    cycles = f"gfs:{CYCLE},alt:{CYCLE}"
    assert run.main(["--out", str(out), "--work", str(work), "--cycles", cycles, "--all-cycles", cycles]) == 0
    printed = capsys.readouterr().out
    assert f"::error::Skipped alt {CYCLE}: records missing: temperature.2m at hour 6" in printed
    skipped = [line for line in printed.splitlines() if line.startswith("::error::")]
    assert len(skipped) == 1 and "gfs" not in skipped[0]
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"**Skipped:** alt {CYCLE}: records missing: temperature.2m at hour 6" in text
    # The skipped source's folders and entries are as they were; GFS's new release is in.
    assert {k: v for k, v in tree(out).items() if k.startswith("alt")} == {k: v for k, v in before.items() if k.startswith("alt")}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["alt"] == {"release": "2026110706", "path": "alt/2026110706"}
    assert manifest["sources"]["gfs"]["release"] == CYCLE
    assert (out / "gfs" / CYCLE).is_dir() and not (out / "alt" / CYCLE).exists()
    assert (tmp / "output.txt").read_text(encoding="utf-8") == f"published=true\nrelease={CYCLE}\n"


def test_a_grid_refused_in_another_source_skips_it_and_publishes_gfs(no_requests, monkeypatch, capsys):
    # Rewritten by D3 and D4: a refused grid skips its source only, its stage folder deleted (was: no
    # manifest written, nothing pruned, no output).
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    two_sources(monkeypatch, work)

    def decode(message, src):
        if src.info.name == "alt":
            raise ValueError("unexpected alt grid: Ni 720 (expected 1440)")
        return np.full((config.NY, config.NX), 280.0)

    monkeypatch.setattr(grib, "decode", decode)
    site_with_two_sources(out)

    cycles = f"gfs:{CYCLE},alt:{CYCLE}"
    assert run.main(["--out", str(out), "--work", str(work), "--cycles", cycles, "--all-cycles", cycles]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["alt"] == {"release": "2026110706", "path": "alt/2026110706"}
    assert manifest["sources"]["gfs"]["release"] == CYCLE
    assert manifest["previous"] == {"gfs": {"release": "2026110706", "path": "gfs/2026110706"}}
    # GFS's oldest release is pruned; the skipped source's folder stays and its new one never appears.
    assert not (out / "gfs" / "2026110700").exists() and (out / "gfs" / "2026110706").is_dir()
    assert (out / "alt" / "2026110706").is_dir() and not (out / "alt" / CYCLE).exists()
    assert not (work / "stage" / "alt" / CYCLE).exists()
    assert f"::error::Skipped alt {CYCLE}: ValueError: unexpected alt grid: Ni 720 (expected 1440)" in capsys.readouterr().out
    assert "unexpected alt grid: Ni 720 (expected 1440)" in (tmp / "summary.md").read_text(encoding="utf-8")
    assert "published=true" in (tmp / "output.txt").read_text(encoding="utf-8")


def test_read_manifest_takes_only_a_json_object(no_requests, monkeypatch):
    path = no_requests / "live.json"
    path.write_bytes(b"[1, 2]")
    assert run.read_manifest(str(path)) is None
    path.write_bytes(b'{"sources": {}}')
    assert run.read_manifest(str(path)) == {"sources": {}}
    for body, expected in ((b'"text"', None), (b"[]", None), (b'{"sources": {}}', {"sources": {}})):
        monkeypatch.setattr(gfs, "http_get", lambda url, byte_range=None, retries=4, timeout=60, body=body: body)
        assert run.read_manifest("https://example.invalid/site/manifest.json") == expected


def test_the_two_forms_exclude_each_other(no_requests):
    for args in (["--cycles", "gfs:2026110712", "--source", "gfs"],
                 ["--cycles", "gfs:2026110712"],
                 ["--cycles", "gfs:2026110712", "--all-cycles", "gfs:2026110712", "--hours", "0"],
                 ["--all-cycles", "gfs:2026110712"]):
        with pytest.raises(SystemExit):
            run.main(["--out", str(no_requests / "site")] + args)


# ---- paths, summary, outputs --------------------------------------------------------------------

def test_the_cache_paths_are_todays():
    fetcher = run.Fetcher(config.GFS, run.parse_cycle(CYCLE), Path("work"))
    assert fetcher.idx_path(7).as_posix() == "work/gfs/2026110712/f007.idx"
    assert fetcher.message_path(7, 612).as_posix() == "work/gfs/2026110712/f007/612.grib2"


def test_the_summary_lines_of_a_built_source(monkeypatch, tmp_path):
    from wxpipe import summary

    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    summary.append(run.summary_lines("gfs", CYCLE, 209, 6, 125.0, 1_134_000_000))
    assert target.read_text(encoding="utf-8") == "- gfs 2026110712: 209 frames, 6 fields, 2.1 min, 1134.0 MB downloaded\n"


def test_the_published_output():
    assert run.published({"gfs": {}}) == "true"
    assert run.published({"gfs": {}, "ifs": {}}) == "true"
    assert run.published({}) == "false"
    assert run.published([]) == "false"


def test_a_source_entry_has_todays_keys():
    entry = run.release_entry(config.GFS, datetime(2026, 11, 7, 12, tzinfo=timezone.utc), (0, 1), config.GFS.fields)
    assert list(entry) == ["release", "run_time", "path", "frames", "levels", "region", "fields", "attribution"]
    assert entry["path"] == "gfs/2026110712" and entry["frames"] == [entry["run_time"], entry["run_time"] + 3600]
    assert site.merge(None, {"gfs": entry}, 2, datetime(2026, 11, 7, tzinfo=timezone.utc))["sources"] == {"gfs": entry}
    assert entry["attribution"] == "NOAA NCEP Global Forecast System (GFS). Public domain."


IFS_ATTRIBUTION_2026 = (
    "ECMWF IFS open data. "
    "This data is published under a Creative Commons Attribution 4.0 International (CC BY 4.0). "
    "https://creativecommons.org/licenses/by/4.0/ "
    "© 2026 European Centre for Medium-Range Weather Forecasts (ECMWF) "
    "Source www.ecmwf.int "
    "Based on data and products of the European Centre for Medium-Range Weather Forecasts (ECMWF). "
    "ECMWF does not accept any liability whatsoever for any error or omission in the data, their availability, "
    "or for any loss or damage arising from their use. "
    "Modified: the columns rolled to start at 0°E, units converted, accumulated precipitation turned into "
    "rates, values quantised to 8-bit tables and cut into map tiles."
)


def test_an_ifs_entry_holds_the_attribution_and_its_parts():
    # A15 (D10).
    entry = run.release_entry(config.IFS, run.parse_cycle(IFS_CYCLE), IFS_HOURS, config.IFS.fields)
    assert list(entry) == ["release", "run_time", "path", "frames", "levels", "region", "fields", "attribution",
                           "attribution_parts"]
    parts = entry["attribution_parts"]
    assert list(parts) == ["licence", "licence_url", "copyright", "source", "credit", "liability", "modified"]
    assert parts["copyright"] == "© 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)"
    assert parts["licence_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert entry["attribution"] == IFS_ATTRIBUTION_2026
    assert entry["attribution"] == "ECMWF IFS open data. " + " ".join(parts.values())
    assert entry["path"] == f"ifs/{IFS_CYCLE}" and len(entry["frames"]) == 3
    # The year is the run's.
    later = run.release_entry(config.IFS, datetime(2027, 1, 1, 0, tzinfo=timezone.utc), (0,), config.IFS.fields)
    assert later["attribution_parts"]["copyright"].startswith("© 2027 ")
    assert "© 2027 European Centre" in later["attribution"]
    # The manifest keeps the parts in the ifs entry; GFS's entry has none.
    gfs_entry = run.release_entry(config.GFS, run.parse_cycle(CYCLE), (0,), config.GFS.fields)
    merged = site.merge(None, {"gfs": gfs_entry, "ifs": entry}, 2, datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert "attribution_parts" not in merged["sources"]["gfs"]
    assert merged["sources"]["ifs"]["attribution_parts"] == parts


# ---- IFS: gaps on the fixtures --------------------------------------------------------------------

def ifs_text(fhour: int, drop=()) -> str:
    """The IFS fixture of [fhour], without the records whose (step, param) is in [drop]."""
    text = (FIXTURES / "ifs" / f"ifs-f{fhour:03d}.index").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    kept = [line for line in lines if (json.loads(line)["step"], json.loads(line)["param"]) not in drop]
    return "\n".join(kept) + "\n"


def ifs_index(drop=()):
    return {h: ecmwf.parse_idx(ifs_text(h, drop)) for h in IFS_HOURS}


def test_ifs_gaps_on_the_fixtures():
    # A8.
    for method in ("bucket", "since-start"):
        assert run.gaps(config.IFS, ifs_index(), IFS_HOURS, method) == []
    assert run.gaps(config.IFS, ifs_index({("150", "10v")}), IFS_HOURS, "bucket") == [("wind_v.10m", 150)]
    assert run.gaps(config.IFS, ifs_index({("0", "2t")}), IFS_HOURS, "bucket") == [("temperature.2m", 0)]
    assert run.gaps(config.IFS, ifs_index({("360", "tp")}), IFS_HOURS, "bucket") == [("precipitation.sfc", 360)]
    # Hour 150's total is missing, so hour 360's interval has nothing to subtract either.
    assert run.gaps(config.IFS, ifs_index({("150", "tp")}), IFS_HOURS, "bucket") == [
        ("precipitation.sfc", 150), ("precipitation.sfc", 360)]
    # Step 0's tp is never needed.
    assert run.gaps(config.IFS, ifs_index({("0", "tp")}), IFS_HOURS, "bucket") == []


# ---- GFS and IFS through run.main, each failing on its own (D3, D4, D17) ---------------------------

IFS_OLD, IFS_OLDER = "2026112300", "2026112212"


REAL_MESSAGES = run.Fetcher.messages


def gfs_and_ifs(monkeypatch, work: Path, ifs_drop=(), ifs_first=False, seed_ifs=True) -> list[str]:
    """GFS's fixtures as in fake_build, and IFS with hours 0, 150 and 360 and two fields, its fixtures in
    its cache without the records of [ifs_drop]. The probe is a fake that records the sources it probed.

    A run that builds GFS defers IFS, whose description waits for GFS (F2, W1), so the tests of an IFS
    failure beside a built GFS put IFS first in the sources' order with [ifs_first]; then IFS runs
    before GFS, and GFS is not yet built when IFS's job starts."""
    fake_build(monkeypatch, work)
    gfs_small = dataclasses.replace(config.GFS, forecast_hours=(0, 6, 7), fields=config.GFS.fields[:1])
    ifs_small = dataclasses.replace(config.IFS, forecast_hours=IFS_HOURS,
                                    fields=(config.IFS.field("temperature.2m"), config.IFS.field("wind_u.10m")))
    monkeypatch.setattr(config, "SOURCES", (ifs_small, gfs_small) if ifs_first else (gfs_small, ifs_small))
    probed = []
    monkeypatch.setattr(run, "probe", lambda src: probed.append(src.info.name))
    if seed_ifs:
        cache = work / "ifs" / IFS_CYCLE
        cache.mkdir(parents=True, exist_ok=True)
        for h in IFS_HOURS:
            (cache / f"f{h:03d}.idx").write_bytes(ifs_text(h, ifs_drop).encode("utf-8"))
    return probed


def site_with_gfs_and_ifs(out: Path) -> dict:
    for path in ("gfs/2026110706", f"ifs/{IFS_OLD}", f"ifs/{IFS_OLDER}"):
        (out / path).mkdir(parents=True)
        (out / path / "0-0.wxt").write_bytes(b"old " + path.encode())
    manifest = {"version": 1, "generated_at": "x", "sources": {
        "gfs": {"release": "2026110706", "path": "gfs/2026110706"},
        "ifs": {"release": IFS_OLD, "path": f"ifs/{IFS_OLD}"}},
        "previous": {"ifs": {"release": IFS_OLDER, "path": f"ifs/{IFS_OLDER}"}}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


BOTH = f"gfs:{CYCLE},ifs:{IFS_CYCLE}"


def run_both(out: Path, work: Path, *extra: str) -> int:
    return run.main(["--out", str(out), "--work", str(work), "--cycles", BOTH, "--all-cycles", BOTH, *extra])


def assert_ifs_skipped_and_gfs_published(tmp: Path, out: Path, work: Path, before: dict, printed: str, reason: str):
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfs"]["release"] == CYCLE and (out / "gfs" / CYCLE).is_dir()
    assert manifest["sources"]["ifs"] == before["sources"]["ifs"]
    assert manifest["previous"]["ifs"] == before["previous"]["ifs"]
    assert (out / "ifs" / IFS_OLD).is_dir() and (out / "ifs" / IFS_OLDER).is_dir()
    assert not (out / "ifs" / IFS_CYCLE).exists()
    assert not (work / "stage" / "ifs" / IFS_CYCLE).exists()
    errors = [line for line in printed.splitlines() if line.startswith("::error::")]
    assert len(errors) == 1 and errors[0].startswith(f"::error::Skipped ifs {IFS_CYCLE}: ") and reason in errors[0]
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"**Skipped:** ifs {IFS_CYCLE}: " in text and reason in text
    assert f"- gfs {CYCLE}: 3 frames, 1 fields" in text and f"- ifs {IFS_CYCLE}" not in text
    assert (tmp / "output.txt").read_text(encoding="utf-8").startswith("published=true\n")


def test_a_gap_in_ifs_skips_it_and_gfs_is_published(no_requests, monkeypatch, capsys):
    # A9, a gap.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_drop={("150", "2t")}, ifs_first=True)
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out,
                                         "records missing: temperature.2m at hour 150")


def test_a_failed_ifs_probe_skips_it_and_gfs_is_published(no_requests, monkeypatch, capsys):
    # A9, the probe.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True)

    def probe(src):
        raise run.JobDropped(f"probe of {src.info.name} failed, exit 1: ValueError: mean 1.0")

    monkeypatch.setattr(run, "probe", probe)
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out,
                                         "probe of ifs failed, exit 1")


def test_an_ifs_message_read_failing_midway_skips_it_and_gfs_is_published(no_requests, monkeypatch, capsys):
    # A9, a message read that still fails after its retries.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True)
    calls = {"ifs": 0}

    def messages(self, items):
        if self.source.info.name == "ifs":
            calls["ifs"] += 1
            if calls["ifs"] == 2:
                raise OSError("HTTP Error 503: Slow Down")
        return {(h, r.number): b"" for h, r in items}

    monkeypatch.setattr(run.Fetcher, "messages", messages)
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert calls["ifs"] == 2
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out,
                                         "OSError: HTTP Error 503: Slow Down")


def test_an_ifs_decode_raising_skips_it_and_gfs_is_published(no_requests, monkeypatch, capsys):
    # A9, a decode error that is not a ValueError, as ecCodes' own are not.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = gfs_and_ifs(monkeypatch, work, ifs_first=True)

    def decode(message, src):
        if src.info.name == "ifs":
            raise RuntimeError("Wrong message length")
        return np.full((config.NY, config.NX), 280.0)

    monkeypatch.setattr(grib, "decode", decode)
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert probed == ["ifs"]
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out,
                                         "RuntimeError: Wrong message length")


def run_ifs_alone(out: Path, work: Path, *extra: str) -> int:
    """The workflow's form in an hour where only IFS is new."""
    return run.main(["--out", str(out), "--work", str(work), "--cycles", f"ifs:{IFS_CYCLE}", "--all-cycles", BOTH, *extra])


def test_ifs_alone_is_built_and_published(no_requests, monkeypatch, capsys):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = gfs_and_ifs(monkeypatch, work)
    before = site_with_gfs_and_ifs(out)
    assert run_ifs_alone(out, work) == 0
    assert probed == ["ifs"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfs"] == before["sources"]["gfs"]
    assert manifest["sources"]["ifs"]["release"] == IFS_CYCLE
    assert list(manifest["sources"]["ifs"])[-2:] == ["attribution", "attribution_parts"]
    assert manifest["previous"]["ifs"]["release"] == IFS_OLD
    assert not (out / "ifs" / IFS_OLDER).exists() and (out / "ifs" / IFS_OLD).is_dir()
    assert len(list((out / "ifs" / IFS_CYCLE / "wind_u.10m").rglob("*.wxt"))) == 108
    assert "::error::" not in capsys.readouterr().out
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"- ifs {IFS_CYCLE}: 3 frames, 2 fields" in text
    assert (tmp / "output.txt").read_text(encoding="utf-8") == f"published=true\nrelease={IFS_CYCLE}\n"


# ---- F2: a run that builds GFS defers IFS, the source that waits for GFS ------------------------------

def test_a_run_that_builds_gfs_defers_ifs(no_requests, monkeypatch, capsys):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = gfs_and_ifs(monkeypatch, work)
    before = site_with_gfs_and_ifs(out)
    reads = []
    monkeypatch.setattr(ecmwf, "http_get", lambda *a, **k: reads.append(a))
    assert run_both(out, work) == 0
    assert probed == [] and reads == []
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfs"]["release"] == CYCLE
    assert manifest["sources"]["ifs"] == before["sources"]["ifs"] and manifest["previous"]["ifs"] == before["previous"]["ifs"]
    assert not (out / "ifs" / IFS_CYCLE).exists() and not (work / "stage" / "ifs").exists()
    printed = capsys.readouterr().out
    assert f"::notice::Deferred ifs {IFS_CYCLE}: GFS is built in this run; tried in the next run" in printed
    assert "::error::" not in printed
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"**Deferred:** ifs {IFS_CYCLE}: GFS is built in this run" in text and "**Skipped:**" not in text
    assert (tmp / "output.txt").read_text(encoding="utf-8") == f"published=true\nrelease={CYCLE}\n"


def test_a_skipped_gfs_lets_ifs_build_and_publish(no_requests, monkeypatch, capsys):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = gfs_and_ifs(monkeypatch, work)
    text = fixture_text(6)
    (work / "gfs" / CYCLE / "f006.idx").write_bytes(
        ("\n".join(line for line in text.splitlines() if ":TMP:2 m above ground:6 hour fcst:" not in line) + "\n").encode())
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert probed == ["ifs"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfs"] == before["sources"]["gfs"]
    assert manifest["sources"]["ifs"]["release"] == IFS_CYCLE and (out / "ifs" / IFS_CYCLE).is_dir()
    printed = capsys.readouterr().out
    assert f"::error::Skipped gfs {CYCLE}: records missing: temperature.2m at hour 6" in printed
    assert "::notice::Deferred" not in printed


def test_the_local_form_never_defers(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work)
    assert run.main(["--out", str(out), "--work", str(work), "--source", "ifs", "--cycle", IFS_CYCLE,
                     "--hours", "0,150,360", "--fields", "temperature.2m"]) == 0
    assert (out / "ifs" / IFS_CYCLE / "temperature.2m").is_dir()


# ---- F6: never a release earlier than the restored site's --------------------------------------------

def test_a_release_earlier_than_the_sites_is_skipped(no_requests, monkeypatch, capsys):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    fake_build(monkeypatch, work)
    (out / "gfs" / "2026110718").mkdir(parents=True)
    manifest = json.dumps({"version": 1, "generated_at": "x", "sources": {
        "gfs": {"release": "2026110718", "path": "gfs/2026110718"}}}).encode()
    (out / "manifest.json").write_bytes(manifest)
    before = tree(out)
    with pytest.raises(run.RunRefused, match="every source skipped"):
        run.main(["--out", str(out), "--work", str(work), "--cycles", f"gfs:{CYCLE}", "--all-cycles", f"gfs:{CYCLE}"])
    assert f"::error::Skipped gfs {CYCLE}: the site already holds the later release 2026110718" in capsys.readouterr().out
    assert tree(out) == before


def test_the_sites_own_release_is_built_again(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    fake_build(monkeypatch, work)
    (out / "gfs" / CYCLE).mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"version": 1, "generated_at": "x", "sources": {
        "gfs": {"release": CYCLE, "path": f"gfs/{CYCLE}"}}}), encoding="utf-8")
    small = dataclasses.replace(config.GFS, forecast_hours=(0, 6, 7), fields=config.GFS.fields[:1])
    monkeypatch.setattr(config, "SOURCES", (small,))
    assert run.main(["--out", str(out), "--work", str(work), "--cycles", f"gfs:{CYCLE}", "--all-cycles", f"gfs:{CYCLE}"]) == 0
    assert len(list((out / "gfs" / CYCLE / "temperature.2m").rglob("*.wxt"))) == 108


class RecordingPool:
    """A ThreadPoolExecutor that records its worker count."""

    sizes: list = []

    def __init__(self, max_workers):
        RecordingPool.sizes.append(max_workers)
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(max_workers)

    def __enter__(self):
        return self.pool.__enter__()

    def __exit__(self, *exc):
        return self.pool.__exit__(*exc)


def test_an_ifs_job_reads_two_at_a_time_and_gfs_takes_workers(no_requests, monkeypatch):
    # A9b: the index pools and the fetchers.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True)
    fetchers = []

    class Recording(run.Fetcher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            fetchers.append(self)

    monkeypatch.setattr(run, "Fetcher", Recording)
    RecordingPool.sizes = []
    monkeypatch.setattr(run, "ThreadPoolExecutor", RecordingPool)
    site_with_gfs_and_ifs(out)
    assert run_both(out, work, "--workers", "5") == 0
    assert [(f.source.info.name, f.workers) for f in fetchers] == [("ifs", 2), ("gfs", 5)]
    assert RecordingPool.sizes == [2, 5]


def test_a_fetchers_message_pool_takes_its_workers(monkeypatch):
    # A9b: the message pool.
    RecordingPool.sizes = []
    monkeypatch.setattr(run, "ThreadPoolExecutor", RecordingPool)
    fetcher = run.Fetcher(config.IFS, run.parse_cycle(IFS_CYCLE), Path("work"), workers=config.IFS.workers)
    monkeypatch.setattr(fetcher, "_message", lambda h, r: b"x")
    records = ecmwf.parse_idx(ifs_text(150))
    assert fetcher.messages([(150, records[0]), (150, records[1])]) == {(150, 1): b"x", (150, 2): b"x"}
    assert RecordingPool.sizes == [2]


def test_the_ifs_cache_paths_and_reads(no_requests, monkeypatch):
    # A9b: D14's cache paths, and a miss read through ecmwf.http_get by the file's URL and its range.
    tmp = no_requests
    fetcher = run.Fetcher(config.IFS, run.parse_cycle(IFS_CYCLE), Path("work"))
    assert fetcher.idx_path(150).as_posix() == f"work/ifs/{IFS_CYCLE}/f150.idx"
    assert fetcher.message_path(150, 58).as_posix() == f"work/ifs/{IFS_CYCLE}/f150/58.grib2"

    asked = []

    def fake_get(url, byte_range=None):
        asked.append((url, byte_range))
        return ifs_text(150).encode("utf-8") if byte_range is None else b"m" * (byte_range[1] - byte_range[0] + 1)

    monkeypatch.setattr(ecmwf, "http_get", fake_get)
    fetcher = run.Fetcher(config.IFS, run.parse_cycle(IFS_CYCLE), tmp / "work")
    records = fetcher.idx(150)
    t2m = config.IFS.instant_record(records, "2t:sfc", 150)
    assert t2m.number == 58
    data = fetcher.messages([(150, t2m)])[(150, 58)]
    assert len(data) == 321810
    base = "https://data.ecmwf.int/forecasts/20261123/12z/ifs/0p25/oper/20261123120000-150h-oper-fc"
    assert asked == [(base + ".index", None), (base + ".grib2", (25137435, 25137435 + 321810 - 1))]
    assert (tmp / "work" / "ifs" / IFS_CYCLE / "f150.idx").is_file()
    assert (tmp / "work" / "ifs" / IFS_CYCLE / "f150" / "58.grib2").stat().st_size == 321810
    # A second read is from the cache.
    assert run.Fetcher(config.IFS, run.parse_cycle(IFS_CYCLE), tmp / "work").idx(150) == records
    assert len(asked) == 2


def test_an_ifs_job_keeps_one_cycle_and_is_dropped_past_its_read_limit(no_requests, monkeypatch, capsys):
    # A9c (D17).
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True)
    other = work / "ifs" / IFS_OLD
    other.mkdir(parents=True)
    (other / "f000.idx").write_bytes(b"old")
    (work / "ifs" / "notes").mkdir()
    gfs_other = work / "gfs" / "2026110700"
    gfs_other.mkdir(parents=True)
    now = {"t": 1000.0}
    monkeypatch.setattr(run, "clock", lambda: now["t"])

    def slow_probe(src):
        now["t"] += 45 * 60 + 1

    monkeypatch.setattr(run, "probe", slow_probe)
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    # The other cycle is deleted, its own kept with every file read; GFS's cache and other names untouched.
    assert not other.exists() and (work / "ifs" / "notes").is_dir() and gfs_other.is_dir()
    assert sorted(p.name for p in (work / "ifs" / IFS_CYCLE).iterdir()) == ["f000.idx", "f150.idx", "f360.idx"]
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out, "read time limit of 45 min")


def test_an_ifs_job_within_its_read_limit_is_built(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work)
    now = {"t": 1000.0}
    monkeypatch.setattr(run, "clock", lambda: now["t"])
    monkeypatch.setattr(run, "probe", lambda src: now.update(t=now["t"] + 45 * 60 - 1))
    site_with_gfs_and_ifs(out)
    assert run_ifs_alone(out, work) == 0
    assert (out / "ifs" / IFS_CYCLE).is_dir()


def ifs_mirror(monkeypatch, now: dict, step: float = 0.0, on=lambda url, byte_range: True):
    """A fake ecmwf.http_get serving the IFS fixtures and messages of their exact length; each read
    for which [on] holds moves the clock by [step]. Returns the reads made."""
    reads = []

    def fake_get(url, byte_range=None):
        reads.append((url, byte_range))
        if on(url, byte_range):
            now["t"] += step
        if byte_range is None:
            hour = int(url.rsplit("-", 3)[-3].rstrip("h"))
            return ifs_text(hour).encode("utf-8")
        return b"m" * (byte_range[1] - byte_range[0] + 1)

    monkeypatch.setattr(ecmwf, "http_get", fake_get)
    return reads


def ifs_reads_messages(monkeypatch):
    """IFS's message reads go through the real Fetcher.messages; GFS's stay fakes."""
    fake = run.Fetcher.messages

    def messages(self, items):
        return REAL_MESSAGES(self, items) if self.source.info.name == "ifs" else fake(self, items)

    monkeypatch.setattr(run.Fetcher, "messages", messages)


def test_the_read_limit_drops_ifs_during_its_message_reads(no_requests, monkeypatch, capsys):
    # F1: one limit for every read, the build's message reads included.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True)
    ifs_reads_messages(monkeypatch)
    now = {"t": 1000.0}
    monkeypatch.setattr(run, "clock", lambda: now["t"])
    monkeypatch.setattr(run, "probe", lambda src: now.update(t=now["t"] + 45 * 60 - 10))
    reads = ifs_mirror(monkeypatch, now, step=5.0)
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    kept = sorted((work / "ifs" / IFS_CYCLE).rglob("*.grib2"))
    assert 1 <= len(kept) == len(reads) < 6
    assert all(p.stat().st_size > 0 for p in kept) and not list((work / "ifs").rglob("*.part"))
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out, "read time limit of 45 min")


def test_the_check_after_the_gaps_drops_ifs(no_requests, monkeypatch, capsys):
    # F8: the limit passes while the last index is read, so only the check before the build drops the job.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True, seed_ifs=False)
    now = {"t": 1000.0}
    monkeypatch.setattr(run, "clock", lambda: now["t"])
    reads = ifs_mirror(monkeypatch, now, step=45 * 60 + 1, on=lambda url, byte_range: "-360h-" in url)
    built_messages = []
    fake = run.Fetcher.messages
    monkeypatch.setattr(run.Fetcher, "messages",
                        lambda self, items: built_messages.append(self.source.info.name) or fake(self, items))
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert len(reads) == 3 and all(r[1] is None for r in reads)
    assert sorted(p.name for p in (work / "ifs" / IFS_CYCLE).iterdir()) == ["f000.idx", "f150.idx", "f360.idx"]
    assert "ifs" not in built_messages
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out, "read time limit of 45 min")


# ---- F4: the download cache ------------------------------------------------------------------------

def test_a_short_cached_message_is_read_again(no_requests, monkeypatch):
    tmp = no_requests
    now = {"t": 0.0}
    reads = ifs_mirror(monkeypatch, now)
    fetcher = run.Fetcher(config.IFS, run.parse_cycle(IFS_CYCLE), tmp / "work")
    record = config.IFS.instant_record(ecmwf.parse_idx(ifs_text(150)), "2t:sfc", 150)
    path = fetcher.message_path(150, record.number)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cut short")
    assert len(fetcher.messages([(150, record)])[(150, record.number)]) == 321810
    assert path.stat().st_size == 321810 and len(reads) == 1
    # Of the right size, it is reused.
    assert len(fetcher.messages([(150, record)])[(150, record.number)]) == 321810 and len(reads) == 1
    assert not list((tmp / "work").rglob("*.part"))


def test_a_bad_cached_index_is_read_again(no_requests, monkeypatch):
    tmp = no_requests
    reads = ifs_mirror(monkeypatch, {"t": 0.0})
    fetcher = run.Fetcher(config.IFS, run.parse_cycle(IFS_CYCLE), tmp / "work")
    path = fetcher.idx_path(150)
    path.parent.mkdir(parents=True)
    path.write_bytes(ifs_text(150).encode("utf-8")[:1000])   # cut inside a line
    assert len(fetcher.idx(150)) == 191 and len(reads) == 1
    assert path.read_bytes() == ifs_text(150).encode("utf-8")
    assert not list((tmp / "work").rglob("*.part"))


def test_the_cache_is_written_through_a_part_file(no_requests, monkeypatch):
    tmp = no_requests
    replaced = []
    real_replace = run.os.replace

    def replace(src, dst):
        replaced.append((Path(src).name, Path(dst).name))
        return real_replace(src, dst)

    monkeypatch.setattr(run.os, "replace", replace)
    reads = ifs_mirror(monkeypatch, {"t": 0.0})
    fetcher = run.Fetcher(config.IFS, run.parse_cycle(IFS_CYCLE), tmp / "work")
    record = config.IFS.instant_record(fetcher.idx(150), "2t:sfc", 150)
    fetcher.messages([(150, record)])
    assert replaced == [("f150.idx.part", "f150.idx"), ("58.grib2.part", "58.grib2")] and len(reads) == 2
    assert not list((tmp / "work").rglob("*.part"))


def test_a_gfs_message_without_an_end_is_reused(no_requests):
    # GFS's last record has no end: its cached file is reused whatever its size.
    fetcher = run.Fetcher(config.GFS, run.parse_cycle(CYCLE), no_requests / "work")
    record = gfs.IdxRecord(696, 100, None, "X", "surface", "anl")
    path = fetcher.message_path(0, 696)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cached")
    assert fetcher.messages([(0, record)]) == {(0, 696): b"cached"}


def test_every_source_skipped_refuses_the_run_and_changes_nothing(no_requests, monkeypatch):
    # A10.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_drop={("360", "10u")})
    text = fixture_text(6)
    (work / "gfs" / CYCLE / "f006.idx").write_bytes(
        ("\n".join(line for line in text.splitlines() if ":TMP:2 m above ground:6 hour fcst:" not in line) + "\n").encode())
    site_with_gfs_and_ifs(out)
    before = tree(out)
    with pytest.raises(run.RunRefused, match="every source skipped") as e:
        run_both(out, work)
    assert f"gfs {CYCLE}: records missing: temperature.2m at hour 6" in str(e.value)
    assert f"ifs {IFS_CYCLE}: records missing: wind_u.10m at hour 360" in str(e.value)
    assert tree(out) == before
    assert not (tmp / "output.txt").exists()
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert "**Refused:** run refused, every source skipped" in text and text.count("**Skipped:**") == 2


def test_a_lost_cache_without_gfs_publishes_nothing(no_requests, monkeypatch):
    # A10b (D16): no manifest on the site, GFS with a gap, IFS good.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work)
    text = fixture_text(7)
    (work / "gfs" / CYCLE / "f007.idx").write_bytes(
        ("\n".join(line for line in text.splitlines() if ":TMP:2 m above ground:7 hour fcst:" not in line) + "\n").encode())
    out.mkdir()
    with pytest.raises(run.RunRefused, match="no readable manifest.json and GFS was not built"):
        run.main(["--out", str(out), "--work", str(work), "--cycles", "", "--all-cycles", BOTH])
    assert not (out / "manifest.json").exists()
    assert not (tmp / "output.txt").exists()
    assert "**Skipped:** gfs" in (tmp / "summary.md").read_text(encoding="utf-8")


def test_a_lost_cache_with_gfs_built_defers_ifs(no_requests, monkeypatch):
    # F2 on a lost cache: GFS alone is published.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = gfs_and_ifs(monkeypatch, work)
    assert run.main(["--out", str(out), "--work", str(work), "--cycles", "", "--all-cycles", BOTH]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest["sources"]) == ["gfs"] and probed == []
    assert f"**Deferred:** ifs {IFS_CYCLE}" in (tmp / "summary.md").read_text(encoding="utf-8")


def test_a_decode_failing_midway_leaves_no_folder_of_its_release(no_requests, monkeypatch, capsys):
    # A11 (D4): a ValueError at the second field of IFS.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True)
    calls = {"ifs": 0}

    def decode(message, src):
        if src.info.name == "ifs":
            calls["ifs"] += 1
            if calls["ifs"] > len(IFS_HOURS):
                raise ValueError("grid of shape (361, 720) is not the shared (721, 1440)")
        return np.full((config.NY, config.NX), 280.0)

    monkeypatch.setattr(grib, "decode", decode)
    before = site_with_gfs_and_ifs(out)
    stage = work / "stage" / "ifs" / IFS_CYCLE
    seen = {}
    real_write = run.write_field

    def write_field(release_dir, src, field, release, frames):
        seen[src.info.name] = release_dir
        return real_write(release_dir, src, field, release, frames)

    monkeypatch.setattr(run, "write_field", write_field)
    assert run_both(out, work) == 0
    assert seen["ifs"] == stage and seen["gfs"] == work / "stage" / "gfs" / CYCLE
    assert calls["ifs"] == len(IFS_HOURS) + 1
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out,
                                         "ValueError: grid of shape (361, 720)")
    assert not (work / "stage" / "gfs" / CYCLE).exists()


def test_a_rerun_replaces_its_folder_and_a_stale_stage_is_removed(no_requests, monkeypatch):
    # A11 (D4): a rerun of the current release, with a stage folder left by a killed run.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    fake_build(monkeypatch, work)
    current = out / "gfs" / CYCLE
    current.mkdir(parents=True)
    (current / "stale.wxt").write_bytes(b"from the first try")
    (out / "manifest.json").write_text(json.dumps({"version": 1, "generated_at": "x", "sources": {
        "gfs": {"release": CYCLE, "path": f"gfs/{CYCLE}"}}}), encoding="utf-8")
    stale_stage = work / "stage" / "gfs" / CYCLE
    (stale_stage / "temperature.2m").mkdir(parents=True)
    (stale_stage / "temperature.2m" / "killed.wxt").write_bytes(b"half written")

    assert run.main(["--out", str(out), "--work", str(work), "--cycle", CYCLE, "--hours", "0,6,7",
                     "--fields", "temperature.2m"]) == 0
    assert not (current / "stale.wxt").exists()
    assert not (current / "temperature.2m" / "killed.wxt").exists()
    assert len(list((current / "temperature.2m").rglob("*.wxt"))) == 108
    assert not stale_stage.exists()
    assert not (work / "stage" / "gfs" / f"{CYCLE}.old").exists()


# ---- round 2: frame 0 of the precipitation rate -----------------------------------------------------

class KeyedFetcher:
    """Hands each record's message as its own key, `<hour>:<number>`, for a decode that maps it to a value."""

    def messages(self, items):
        return {(h, r.number): f"{h}:{r.number}".encode() for h, r in items}


def precip_frames(monkeypatch, src, hours, idx, totals: dict) -> tuple[list, dict]:
    """build_precip with a fake decode: the record of (hour, window) holds the constant totals[(hour, window)],
    in the source's own units."""
    field = src.field("precipitation.sfc")
    values = {}
    for h in hours:
        for window, r in src.accumulation_records(idx[h], field.grib, h).items():
            values[f"{h}:{r.number}".encode()] = totals[(h, window)]
    monkeypatch.setattr(grib, "decode", lambda message, s: np.full((config.NY, config.NX), values[message]))
    report: dict = {}
    frames = run.build_precip(src, field, run.parse_cycle(CYCLE), hours, idx, KeyedFetcher(), "bucket", report)
    return frames, report


def test_frame_zero_takes_the_first_intervals_rate_for_gfs(monkeypatch):
    # A23, GFS-like: 12 mm by hour 6, a 3 mm bucket over 6 to 7 h.
    hours = (0, 6, 7)
    totals = {(6, (0, 6)): 12.0, (7, (6, 7)): 3.0, (7, (0, 7)): 15.0}
    frames, report = precip_frames(monkeypatch, config.GFS, hours, index(hours), totals)
    lut = config.GFS.field("precipitation.sfc").lut
    assert [t for t, _ in frames] == [run.valid_time(run.parse_cycle(CYCLE), h) for h in hours]
    assert np.array_equal(frames[0][1], frames[1][1])
    assert np.allclose(lut.decode(frames[0][1]), 2.0, atol=lut.step(2.0) / 2)
    assert np.allclose(lut.decode(frames[2][1]), 3.0, atol=lut.step(3.0) / 2)
    assert [e["interval"] for e in report["intervals"]] == [[0, 6], [6, 7]]


def test_frame_zero_takes_the_first_intervals_rate_for_ifs(monkeypatch):
    # A23, IFS-like: totals since the start in m, 0.03 by 150 h and 0.09 by 360 h.
    totals = {(150, (0, 150)): 0.03, (360, (0, 360)): 0.09}
    frames, report = precip_frames(monkeypatch, config.IFS, IFS_HOURS, ifs_index(), totals)
    lut = config.IFS.field("precipitation.sfc").lut
    assert np.array_equal(frames[0][1], frames[1][1])
    assert np.allclose(lut.decode(frames[0][1]), 0.2, atol=lut.step(0.2) / 2)          # 30 mm over 150 h
    assert np.allclose(lut.decode(frames[2][1]), 60 / 210, atol=lut.step(60 / 210) / 2)
    assert [e["interval"] for e in report["intervals"]] == [[0, 150], [150, 360]]


def test_a_single_hour_leaves_frame_zero_missing(monkeypatch):
    # A23: no interval, no rate.
    from wxpipe.quantize import MISSING

    frames, report = precip_frames(monkeypatch, config.GFS, (6,), index((6,)), {(6, (0, 6)): 12.0})
    assert len(frames) == 1 and np.all(frames[0][1] == MISSING)
    assert "intervals" not in report


def test_a_failed_move_on_a_rerun_keeps_the_old_release(no_requests, monkeypatch):
    # F5: the old folder is set aside, and put back when the move of the new one fails.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    fake_build(monkeypatch, work)
    current = out / "gfs" / CYCLE
    (current / "temperature.2m").mkdir(parents=True)
    (current / "temperature.2m" / "3-4.wxt").write_bytes(b"the published tile")
    manifest = json.dumps({"version": 1, "generated_at": "x", "sources": {
        "gfs": {"release": CYCLE, "path": f"gfs/{CYCLE}"}}}).encode()
    (out / "manifest.json").write_bytes(manifest)
    before = tree(out)
    stage = work / "stage" / "gfs" / CYCLE
    real_move = run.shutil.move

    def move(src, dst):
        if Path(src) == stage:
            Path(dst).mkdir(parents=True)
            (Path(dst) / "partial.wxt").write_bytes(b"half moved")
            raise OSError("disk full")
        return real_move(src, dst)

    monkeypatch.setattr(run.shutil, "move", move)
    with pytest.raises(run.RunRefused, match="OSError: disk full"):
        run.main(["--out", str(out), "--work", str(work), "--cycle", CYCLE, "--hours", "0,6,7",
                  "--fields", "temperature.2m"])
    assert tree(out) == before
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["sources"]["gfs"]["path"] == f"gfs/{CYCLE}"
    assert not (work / "stage" / "gfs" / f"{CYCLE}.old").exists() and not stage.exists()


# ---- the mirror fix: IFS's copies through run.main (B7) ----------------------------------------------

PORTAL, CLOUDFRONT, BUCKET = source.IFS.copies
HOSTS = [root.removeprefix("https://") for root in source.IFS.copies]


def grib2(n: int) -> bytes:
    """A made-up GRIB2 message of [n] bytes whose markers, edition and length field pass the reads' check."""
    return b"GRIB" + b"\0\0\0" + b"\x02" + n.to_bytes(8, "big") + b"\0" * (n - 20) + b"7777"


class Served:
    def __init__(self, status: int, body: bytes):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def slowdown(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 503, "SlowDown", email.message.Message(), None)


def ifs_copies(monkeypatch, refusing=(PORTAL,)) -> dict:
    """The real ecmwf.http_get, with urlopen replaced by copies that answer "503 SlowDown" when their ROOT
    is in [refusing], and otherwise serve the IFS fixtures and made-up messages of each asked length;
    time.sleep records its waits. Returns the requests per ROOT and the waits."""
    seen = {PORTAL: 0, CLOUDFRONT: 0, BUCKET: 0, "waits": []}

    def urlopen(request, timeout=None):
        url = request.full_url
        root = next(r for r in (PORTAL, CLOUDFRONT, BUCKET) if url.startswith(r))
        seen[root] += 1
        if root in refusing:
            raise slowdown(url)
        if url.endswith(".index"):
            return Served(200, ifs_text(int(url.rsplit("-", 3)[-3].rstrip("h"))).encode("utf-8"))
        start, end = (int(v) for v in request.get_header("Range").removeprefix("bytes=").split("-"))
        return Served(206, grib2(end - start + 1))

    monkeypatch.setattr(ecmwf, "http_get", REAL_HTTP_GET)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(time, "sleep", seen["waits"].append)
    return seen


def ifs_with(monkeypatch, **changes) -> None:
    """IFS's entry of config.SOURCES, as gfs_and_ifs set it, with [changes]."""
    monkeypatch.setattr(config, "SOURCES", tuple(
        dataclasses.replace(s, **changes) if s.info.name == "ifs" else s for s in config.SOURCES))


def summary_of(tmp: Path) -> list[str]:
    return (tmp / "summary.md").read_text(encoding="utf-8").splitlines()


def test_an_ifs_job_whose_first_copy_refuses_is_built_from_the_second(no_requests, monkeypatch, capsys):
    # B7: one read at a time, so every count is exact.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, seed_ifs=False)
    ifs_with(monkeypatch, workers=1)
    ifs_reads_messages(monkeypatch)
    seen = ifs_copies(monkeypatch)
    before = site_with_gfs_and_ifs(out)
    assert run_ifs_alone(out, work) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["ifs"]["release"] == IFS_CYCLE and manifest["sources"]["gfs"] == before["sources"]["gfs"]
    # The first read is refused 4 times on the portal, then every read is on CloudFront.
    idx = ifs_index()
    records = [config.IFS.instant_record(idx[h], key, h) for key in ("2t:sfc", "10u:sfc") for h in IFS_HOURS]
    size = sum(len(ifs_text(h).encode("utf-8")) for h in IFS_HOURS) + sum(r.end - r.offset + 1 for r in records)
    assert (seen[PORTAL], seen[CLOUDFRONT], seen[BUCKET], seen["waits"]) == (4, 9, 0, [5, 10, 20])
    reads = (f"**Reads:** ifs {IFS_CYCLE}: {HOSTS[0]} 4 requests, 4 refused, 0 missing, 0 errors, 0 files, 0.0 MB; "
             f"{HOSTS[1]} 9 requests, 0 refused, 0 missing, 0 errors, 9 files, {size / 1e6:.1f} MB; "
             f"{HOSTS[2]} 0 requests, 0 refused, 0 missing, 0 errors, 0 files, 0.0 MB")
    lines = summary_of(tmp)
    built = next(k for k, line in enumerate(lines) if line.startswith(f"- ifs {IFS_CYCLE}: 3 frames, 2 fields"))
    assert lines[built + 1] == f"- {reads}"
    printed = capsys.readouterr().out.splitlines()
    assert f"ifs reads: moved from {HOSTS[0]} to {HOSTS[1]}: HTTPError: HTTP Error 503: SlowDown" in printed
    assert reads in printed


def test_an_ifs_job_failing_on_every_copy_writes_its_line_after_skipped(no_requests, monkeypatch, capsys):
    # B7: two workers, so only the line's place and hosts are asserted.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True, seed_ifs=False)
    ifs_copies(monkeypatch, refusing=(PORTAL, CLOUDFRONT, BUCKET))
    before = site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert_ifs_skipped_and_gfs_published(tmp, out, work, before, capsys.readouterr().out,
                                         f"OSError: every copy failed: {HOSTS[0]}: HTTPError: HTTP Error 503: SlowDown; "
                                         f"{HOSTS[1]}: HTTPError")
    lines = summary_of(tmp)
    skipped = next(k for k, line in enumerate(lines) if line.startswith(f"**Skipped:** ifs {IFS_CYCLE}: "))
    assert lines[skipped + 1] == ""   # its own paragraph
    reads = lines[skipped + 2]
    assert reads.startswith(f"**Reads:** ifs {IFS_CYCLE}: {HOSTS[0]} ")
    assert reads.index(HOSTS[0]) < reads.index(HOSTS[1]) < reads.index(HOSTS[2])
    assert all(f"{h} 0 requests" not in reads for h in HOSTS)


def test_an_ifs_job_past_its_read_limit_writes_its_line_after_skipped(no_requests, monkeypatch):
    # B7.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_first=True)
    now = {"t": 1000.0}
    monkeypatch.setattr(run, "clock", lambda: now["t"])
    monkeypatch.setattr(run, "probe", lambda src: now.update(t=now["t"] + 45 * 60 + 1))
    site_with_gfs_and_ifs(out)
    # A count left from before the job: the job's start zeroes it.
    REAL_HTTP_GET(PORTAL + "/x.txt", opener=lambda request, timeout=None: Served(200, b"x"), wait=None)
    assert ecmwf.reads_counts(PORTAL)["requests"] == 1
    assert run_both(out, work) == 0
    lines = summary_of(tmp)
    skipped = next(k for k, line in enumerate(lines) if line.startswith(f"**Skipped:** ifs {IFS_CYCLE}: read time limit"))
    assert lines[skipped + 1] == ""
    assert lines[skipped + 2] == (f"**Reads:** ifs {IFS_CYCLE}: " + "; ".join(
        f"{h} 0 requests, 0 refused, 0 missing, 0 errors, 0 files, 0.0 MB" for h in HOSTS))


def test_a_refused_run_holds_the_ifs_line_before_refused(no_requests, monkeypatch):
    # B7.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, ifs_drop={("360", "10u")})
    text = fixture_text(6)
    (work / "gfs" / CYCLE / "f006.idx").write_bytes(
        ("\n".join(line for line in text.splitlines() if ":TMP:2 m above ground:6 hour fcst:" not in line) + "\n").encode())
    site_with_gfs_and_ifs(out)
    with pytest.raises(run.RunRefused):
        run_both(out, work)
    lines = summary_of(tmp)
    skipped = next(k for k, line in enumerate(lines) if line.startswith(f"**Skipped:** ifs {IFS_CYCLE}: "))
    refused = next(k for k, line in enumerate(lines) if line.startswith("**Refused:** "))
    assert lines[skipped + 1] == ""
    assert lines[skipped + 2].startswith(f"**Reads:** ifs {IFS_CYCLE}: {HOSTS[0]} 0 requests") and skipped + 2 < refused
    assert sum(line.startswith("**Reads:**") for line in lines) == 1


def test_a_lost_cache_refused_after_ifs_is_built_holds_its_line_before_refused(no_requests, monkeypatch):
    # B7, F4 (D16): no manifest on the site, GFS with a gap, IFS good and built, the run refused.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work)
    text = fixture_text(7)
    (work / "gfs" / CYCLE / "f007.idx").write_bytes(
        ("\n".join(line for line in text.splitlines() if ":TMP:2 m above ground:7 hour fcst:" not in line) + "\n").encode())
    out.mkdir()
    with pytest.raises(run.RunRefused, match="GFS was not built"):
        run.main(["--out", str(out), "--work", str(work), "--cycles", "", "--all-cycles", BOTH])
    lines = summary_of(tmp)
    reads = next(k for k, line in enumerate(lines) if line.startswith(f"**Reads:** ifs {IFS_CYCLE}: {HOSTS[0]} "))
    refused = next(k for k, line in enumerate(lines) if line.startswith("**Refused:** "))
    assert lines[reads - 1] == "" and reads < refused
    assert sum(line.startswith("**Reads:**") for line in lines) == 1
    assert not (out / "manifest.json").exists()


def test_an_ifs_job_dropped_before_its_prepare_writes_no_line(no_requests, monkeypatch, capsys):
    # B7: the site already holds a later ifs release.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work)
    site_with_gfs_and_ifs(out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["sources"]["ifs"] = {"release": "2026112400", "path": "ifs/2026112400"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    with pytest.raises(run.RunRefused, match="every source skipped"):
        run_ifs_alone(out, work)
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert "the site already holds the later release 2026112400" in text
    assert "**Reads:**" not in text and "**Reads:**" not in capsys.readouterr().out


def test_a_reads_report_that_raises_gives_the_not_available_line(no_requests, monkeypatch, capsys):
    # B7: the run goes on and publishes.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work)

    def broken(name, release):
        raise RuntimeError("no counts")

    ifs_with(monkeypatch, reads_report=broken)
    site_with_gfs_and_ifs(out)
    assert run_ifs_alone(out, work) == 0
    lines = summary_of(tmp)
    built = next(k for k, line in enumerate(lines) if line.startswith(f"- ifs {IFS_CYCLE}: "))
    assert lines[built + 1] == f"- **Reads:** ifs {IFS_CYCLE}: not available: RuntimeError: no counts"
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["sources"]["ifs"]["release"] == IFS_CYCLE
    assert (tmp / "output.txt").read_text(encoding="utf-8") == f"published=true\nrelease={IFS_CYCLE}\n"


def test_a_gfs_job_writes_no_reads_line(no_requests, monkeypatch, capsys):
    # B7: GFS built and published as before, with no line.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work)
    site_with_gfs_and_ifs(out)
    assert run_both(out, work) == 0
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["sources"]["gfs"]["release"] == CYCLE
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"- gfs {CYCLE}: 3 frames, 1 fields" in text
    assert "**Reads:**" not in text and "**Reads:**" not in capsys.readouterr().out


def test_the_local_form_discovers_ifs_on_the_second_copy(no_requests, monkeypatch, capsys):
    # B7: --source ifs with no --cycle; the first copy's existence test raises, the second answers.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    gfs_and_ifs(monkeypatch, work, seed_ifs=False)
    asked = []

    def exists(url):
        asked.append(url)
        if url.startswith(PORTAL):
            raise TimeoutError("timed out")
        return url.startswith(CLOUDFRONT)

    monkeypatch.setattr(gfs, "exists", exists)
    ifs_mirror(monkeypatch, {"t": 0.0})
    assert run.main(["--out", str(out), "--work", str(work), "--source", "ifs",
                     "--hours", "0,150,360", "--fields", "temperature.2m"]) == 0
    assert len(asked) == 2 and asked[0].startswith(PORTAL) and asked[1] == CLOUDFRONT + asked[0][len(PORTAL):]
    assert asked[1].endswith("-360h-oper-fc.index")
    day, hour = asked[1][len(CLOUDFRONT) + 1:].split("/")[:2]
    built = sorted(p.name for p in (out / "ifs").iterdir())
    assert built == [day + hour.removesuffix("z")]
    assert (out / "ifs" / built[0] / "temperature.2m").is_dir()
    # F6: the copy passed over, and the copy that decided the newest cycle.
    printed = capsys.readouterr().out.splitlines()
    assert printed[:2] == [f"ifs discovery: {HOSTS[0]} passed over: TimeoutError: timed out",
                           f"ifs discovery: the newest cycle decided by {HOSTS[1]}: true"]


# ---- GFS-Wave: gaps on the fixtures (A4) --------------------------------------------------------------

WAVE_CYCLE = "2026112412"
WAVE_HOURS = (0, 123, 384)
WAVE_OLD, WAVE_OLDER = "2026112406", "2026112400"


def wave_text(fhour: int, drop=()) -> str:
    """The GFS-Wave fixture of [fhour], without the lines holding any text of [drop]."""
    text = (FIXTURES / "gfswave" / f"gfswave-f{fhour:03d}.idx").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if line.strip() and not any(d in line for d in drop)) + "\n"


def wave_index(drop=()):
    return {h: gfs.parse_idx(wave_text(h, drop)) for h in WAVE_HOURS}


def test_gfswave_gaps_on_the_fixtures():
    # A4.
    for method in ("bucket", "since-start"):
        assert run.gaps(config.GFSWAVE, wave_index(), WAVE_HOURS, method) == []
    idx = wave_index()
    for h in WAVE_HOURS:
        assert [r.number for r in idx[h]] == list(range(1, 20))
        assert config.GFSWAVE.instant_record(idx[h], "HTSGW:surface", h).number == 5
        assert config.GFSWAVE.instant_record(idx[h], "PERPW:surface", h).number == 6
    assert run.gaps(config.GFSWAVE, wave_index({":HTSGW:surface:123 hour fcst:"}), WAVE_HOURS, "bucket") == [
        ("wave_height.sfc", 123)]
    assert run.gaps(config.GFSWAVE, wave_index({":PERPW:surface:anl:"}), WAVE_HOURS, "bucket") == [
        ("wave_period.sfc", 0)]
    assert run.gaps(config.GFSWAVE, wave_index({":HTSGW:surface:384 hour fcst:", ":PERPW:surface:384 hour fcst:"}),
                    WAVE_HOURS, "bucket") == [("wave_height.sfc", 384), ("wave_period.sfc", 384)]
    # f000's HTSGW message is the sample's 428,678 bytes.
    r = config.GFSWAVE.instant_record(idx[0], "HTSGW:surface", 0)
    assert r.end - r.offset + 1 == 428678


def test_a_gfswave_entry_holds_its_two_fields_and_attribution():
    # A10.
    entry = run.release_entry(config.GFSWAVE, run.parse_cycle(WAVE_CYCLE), WAVE_HOURS, config.GFSWAVE.fields)
    assert list(entry) == ["release", "run_time", "path", "frames", "levels", "region", "fields", "attribution"]
    assert entry["path"] == f"gfswave/{WAVE_CYCLE}" and len(entry["frames"]) == 3
    assert entry["frames"][-1] - entry["run_time"] == 384 * 3600
    assert entry["fields"] == {
        "wave_height.sfc": {"units": "m", "values": {"scale": "lin", "breakpoints": [[0, 0.0], [200, 10.0], [254, 20.0]],
                                                     "missing": 255}},
        "wave_period.sfc": {"units": "s", "values": {"scale": "lin", "breakpoints": [[0, 0.0], [200, 20.0], [254, 30.0]],
                                                     "missing": 255}},
    }
    assert entry["attribution"] == ("NOAA NCEP GFS-Wave, the global 0.25 degree wave model of the Global Forecast "
                                    "System. Public domain.")
    assert "attribution_parts" not in entry
    # GFS's and IFS's entries keep their keys and order.
    gfs_entry = run.release_entry(config.GFS, run.parse_cycle(CYCLE), (0,), config.GFS.fields)
    ifs_entry = run.release_entry(config.IFS, run.parse_cycle(IFS_CYCLE), (0,), config.IFS.fields)
    assert list(gfs_entry) == list(entry)
    assert list(ifs_entry) == list(entry) + ["attribution_parts"]


# ---- GFS-Wave through run.main: built in GFS's run (W1), on its own (D3), its limit (W5) -------------

def three_sources(monkeypatch, work: Path, wave_drop=(), seed_wave=True, in_child=False) -> list[str]:
    """GFS and IFS as in gfs_and_ifs, and GFS-Wave with hours 0, 123 and 384 and both fields, its
    fixtures in its cache without the lines of [wave_drop]; in config.SOURCES' own order. The probe is a
    fake that records the sources it probed (R9).

    GFS-Wave's job runs in a child process in the workflow's form (F1); the tests of the deferral rule
    and of the in-process path build it in the run's own process, with child_seconds None, unless
    [in_child]."""
    probed = gfs_and_ifs(monkeypatch, work)
    gfs_small, ifs_small = config.SOURCES
    wave_small = dataclasses.replace(config.GFSWAVE, forecast_hours=WAVE_HOURS,
                                     child_seconds=config.GFSWAVE.child_seconds if in_child else None)
    monkeypatch.setattr(config, "SOURCES", (gfs_small, wave_small, ifs_small))
    if seed_wave:
        cache = work / "gfswave" / WAVE_CYCLE
        cache.mkdir(parents=True, exist_ok=True)
        for h in WAVE_HOURS:
            (cache / f"f{h:03d}.idx").write_bytes(wave_text(h, wave_drop).encode("utf-8"))
    return probed


def site_with_three_sources(out: Path) -> dict:
    manifest = site_with_gfs_and_ifs(out)
    for path in (f"gfswave/{WAVE_OLD}", f"gfswave/{WAVE_OLDER}"):
        (out / path).mkdir(parents=True)
        (out / path / "0-0.wxt").write_bytes(b"old " + path.encode())
    manifest["sources"]["gfswave"] = {"release": WAVE_OLD, "path": f"gfswave/{WAVE_OLD}"}
    manifest["previous"]["gfswave"] = {"release": WAVE_OLDER, "path": f"gfswave/{WAVE_OLDER}"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


ALL_THREE = f"gfs:{CYCLE},gfswave:{WAVE_CYCLE},ifs:{IFS_CYCLE}"


def run_three(out: Path, work: Path, cycles: str = ALL_THREE, all_cycles: str = ALL_THREE) -> int:
    return run.main(["--out", str(out), "--work", str(work), "--cycles", cycles, "--all-cycles", all_cycles])


def assert_ifs_deferred(tmp: Path, out: Path, before: dict | None, printed: str):
    if before is not None:
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["sources"]["ifs"] == before["sources"]["ifs"]
        assert manifest["previous"]["ifs"] == before["previous"]["ifs"]
    assert not (out / "ifs" / IFS_CYCLE).exists()
    assert f"::notice::Deferred ifs {IFS_CYCLE}: GFS is built in this run; tried in the next run" in printed
    assert f"**Deferred:** ifs {IFS_CYCLE}: GFS is built in this run" in (tmp / "summary.md").read_text(encoding="utf-8")


def test_a_run_that_builds_gfs_builds_gfswave_and_defers_ifs(no_requests, monkeypatch, capsys):
    # A7 (W1): GFS, GFS-Wave and IFS all new.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = three_sources(monkeypatch, work)
    before = site_with_three_sources(out)
    assert run_three(out, work) == 0
    assert probed == ["gfswave"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["sources"]) == ["gfs", "ifs", "gfswave"]
    assert manifest["sources"]["gfs"]["release"] == CYCLE
    assert manifest["sources"]["gfswave"]["release"] == WAVE_CYCLE
    assert manifest["previous"]["gfswave"] == before["sources"]["gfswave"]
    assert sorted(manifest["sources"]["gfswave"]["fields"]) == ["wave_height.sfc", "wave_period.sfc"]
    for field in ("wave_height.sfc", "wave_period.sfc"):
        assert len(list((out / "gfswave" / WAVE_CYCLE / field).rglob("*.wxt"))) == 108
    assert not (out / "gfswave" / WAVE_OLDER).exists() and (out / "gfswave" / WAVE_OLD).is_dir()
    printed = capsys.readouterr().out
    assert "::error::" not in printed
    assert_ifs_deferred(tmp, out, before, printed)
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"- gfs {CYCLE}: 3 frames, 1 fields" in text and f"- gfswave {WAVE_CYCLE}: 3 frames, 2 fields" in text
    assert "**Deferred:** gfswave" not in text and "**Reads:**" not in text
    # Two sources built: no single release in the output.
    assert (tmp / "output.txt").read_text(encoding="utf-8") == "published=true\n"


def test_without_gfs_gfswave_and_ifs_are_both_built(no_requests, monkeypatch, capsys):
    # A7: GFS not among the jobs.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = three_sources(monkeypatch, work)
    before = site_with_three_sources(out)
    assert run_three(out, work, cycles=f"gfswave:{WAVE_CYCLE},ifs:{IFS_CYCLE}") == 0
    assert probed == ["gfswave", "ifs"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfs"] == before["sources"]["gfs"]
    assert manifest["sources"]["gfswave"]["release"] == WAVE_CYCLE and manifest["sources"]["ifs"]["release"] == IFS_CYCLE
    assert "::notice::Deferred" not in capsys.readouterr().out


def assert_wave_skipped_gfs_published_ifs_deferred(tmp: Path, out: Path, work: Path, before: dict, printed: str,
                                                   reason: str):
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfs"]["release"] == CYCLE and (out / "gfs" / CYCLE).is_dir()
    assert manifest["sources"]["gfswave"] == before["sources"]["gfswave"]
    assert manifest["previous"]["gfswave"] == before["previous"]["gfswave"]
    assert (out / "gfswave" / WAVE_OLD).is_dir() and (out / "gfswave" / WAVE_OLDER).is_dir()
    assert not (out / "gfswave" / WAVE_CYCLE).exists() and not (work / "stage" / "gfswave" / WAVE_CYCLE).exists()
    errors = [line for line in printed.splitlines() if line.startswith("::error::")]
    assert len(errors) == 1 and errors[0].startswith(f"::error::Skipped gfswave {WAVE_CYCLE}: ") and reason in errors[0]
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"**Skipped:** gfswave {WAVE_CYCLE}: " in text and reason in text
    assert f"- gfs {CYCLE}: 3 frames, 1 fields" in text and f"- gfswave {WAVE_CYCLE}" not in text
    assert (tmp / "output.txt").read_text(encoding="utf-8") == f"published=true\nrelease={CYCLE}\n"
    assert_ifs_deferred(tmp, out, before, printed)


# ---- GFS-Wave's four failure kinds in the local form, which the child runs (A7 after F1) --------------
# Each drops the job; a local run of one job then raises RunRefused, which is the child's non-zero exit,
# and leaves the site as it was.

def run_wave_locally(out: Path, work: Path) -> int:
    return run.main(["--out", str(out), "--work", str(work), "--source", "gfswave", "--cycle", WAVE_CYCLE,
                     "--hours", ",".join(str(h) for h in WAVE_HOURS)])


def assert_wave_dropped_locally(tmp: Path, out: Path, work: Path, before: dict, error, printed: str, reason: str):
    assert "every source skipped" in str(error.value) and reason in str(error.value)
    assert tree(out) == before
    assert not (out / "gfswave" / WAVE_CYCLE).exists() and not (work / "stage" / "gfswave" / WAVE_CYCLE).exists()
    assert f"::error::Skipped gfswave {WAVE_CYCLE}: " in printed and reason in printed
    assert f"**Skipped:** gfswave {WAVE_CYCLE}: " in (tmp / "summary.md").read_text(encoding="utf-8")
    assert not (tmp / "output.txt").exists()


def test_a_gap_in_gfswave_drops_its_local_run(no_requests, monkeypatch, capsys):
    # A7, a gap.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work, wave_drop={":HTSGW:surface:123 hour fcst:"})
    site_with_three_sources(out)
    before = tree(out)
    with pytest.raises(run.RunRefused) as e:
        run_wave_locally(out, work)
    assert_wave_dropped_locally(tmp, out, work, before, e, capsys.readouterr().out,
                                "records missing: wave_height.sfc at hour 123")


def test_a_failed_gfswave_probe_drops_its_local_run(no_requests, monkeypatch, capsys):
    # A7, the probe: the JPEG 2000 check failing on the runner.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work)

    def probe(src):
        raise run.JobDropped(f"probe of {src.info.name} failed, exit -11: no output")

    monkeypatch.setattr(run, "probe", probe)
    site_with_three_sources(out)
    before = tree(out)
    with pytest.raises(run.RunRefused) as e:
        run_wave_locally(out, work)
    assert_wave_dropped_locally(tmp, out, work, before, e, capsys.readouterr().out, "probe of gfswave failed, exit -11")


def test_a_gfswave_read_failing_midway_drops_its_local_run(no_requests, monkeypatch, capsys):
    # A7 and F8: through the real Fetcher and its worker pool, gfs.http_get a named fake answering whole
    # messages for the first field's ranges and a body ending 7776 for the second field's, so
    # gfs.read_checked refuses it.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work)
    monkeypatch.setattr(run.Fetcher, "messages", REAL_MESSAGES)
    idx = wave_index()
    period = {gfs.instant_record(idx[h], "PERPW:surface", h).offset for h in WAVE_HOURS}
    asked = []

    def wave_bucket(url, byte_range=None):
        assert byte_range is not None, url   # the index files are cached
        asked.append(byte_range[0])
        n = byte_range[1] - byte_range[0] + 1
        return grib2(n)[:-4] + b"7776" if byte_range[0] in period else grib2(n)

    monkeypatch.setattr(gfs, "http_get", wave_bucket)
    site_with_three_sources(out)
    before = tree(out)
    with pytest.raises(run.RunRefused) as e:
        run_wave_locally(out, work)
    assert_wave_dropped_locally(tmp, out, work, before, e, capsys.readouterr().out, "does not end with 7777")
    # The first field's three messages were read and kept; no refused message was written to the cache.
    assert sum(1 for b in asked if b not in period) == len(WAVE_HOURS) and any(b in period for b in asked)
    kept = sorted(p.relative_to(work / "gfswave" / WAVE_CYCLE).as_posix()
                  for p in (work / "gfswave" / WAVE_CYCLE).rglob("*.grib2"))
    assert kept == [f"f{h:03d}/5.grib2" for h in WAVE_HOURS]


def test_a_gfswave_decode_raising_drops_its_local_run(no_requests, monkeypatch, capsys):
    # A7, a decode error that is not a ValueError, as ecCodes' own are not.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work)

    def decode(message, src):
        if src.info.name == "gfswave":
            raise RuntimeError("JPEG 2000 decoding failed")
        return np.full((config.NY, config.NX), 280.0)

    monkeypatch.setattr(grib, "decode", decode)
    site_with_three_sources(out)
    before = tree(out)
    with pytest.raises(run.RunRefused) as e:
        run_wave_locally(out, work)
    assert_wave_dropped_locally(tmp, out, work, before, e, capsys.readouterr().out,
                                "RuntimeError: JPEG 2000 decoding failed")


def test_a_lost_cache_builds_gfs_and_gfswave_and_defers_ifs(no_requests, monkeypatch, capsys):
    # A7: no manifest on the site, all three in --all-cycles.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = three_sources(monkeypatch, work)
    out.mkdir()
    assert run_three(out, work, cycles="") == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["sources"]) == ["gfs", "gfswave"] and "previous" not in manifest
    assert probed == ["gfswave"]
    assert_ifs_deferred(tmp, out, None, capsys.readouterr().out)


def test_a_lost_cache_with_gfs_skipped_builds_gfswave_and_ifs_and_is_refused(no_requests, monkeypatch, capsys):
    # A7 (D16): GFS with a gap; GFS-Wave and IFS built, then the run refused, no manifest written.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = three_sources(monkeypatch, work)
    text = fixture_text(7)
    (work / "gfs" / CYCLE / "f007.idx").write_bytes(
        ("\n".join(line for line in text.splitlines() if ":TMP:2 m above ground:7 hour fcst:" not in line) + "\n").encode())
    out.mkdir()
    with pytest.raises(run.RunRefused, match="no readable manifest.json and GFS was not built"):
        run_three(out, work, cycles="")
    assert probed == ["gfswave", "ifs"]
    assert (work / f"report-gfswave-{WAVE_CYCLE}.json").is_file() and (work / f"report-ifs-{IFS_CYCLE}.json").is_file()
    assert not (out / "manifest.json").exists() and not (tmp / "output.txt").exists()
    assert "::notice::Deferred" not in capsys.readouterr().out


def test_gfswave_past_its_read_limit_is_dropped_and_gfs_is_published(no_requests, monkeypatch, capsys):
    # A8 (W5): an injected clock past 15 minutes before GFS-Wave's build.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work)
    now = {"t": 1000.0}
    monkeypatch.setattr(run, "clock", lambda: now["t"])
    monkeypatch.setattr(run, "probe", lambda src: now.update(t=now["t"] + 15 * 60 + 1))
    before = site_with_three_sources(out)
    assert run_three(out, work) == 0
    # The index files it had are kept; nothing else of it was read.
    assert sorted(p.name for p in (work / "gfswave" / WAVE_CYCLE).iterdir()) == ["f000.idx", "f123.idx", "f384.idx"]
    assert_wave_skipped_gfs_published_ifs_deferred(tmp, out, work, before, capsys.readouterr().out,
                                                   "read time limit of 15 min passed")


def test_gfswave_within_its_read_limit_is_built(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work)
    now = {"t": 1000.0}
    monkeypatch.setattr(run, "clock", lambda: now["t"])
    monkeypatch.setattr(run, "probe", lambda src: now.update(t=now["t"] + 15 * 60 - 1))
    site_with_three_sources(out)
    assert run_three(out, work) == 0
    assert (out / "gfswave" / WAVE_CYCLE).is_dir()


def test_gfswave_messages_are_read_through_read_checked(no_requests, monkeypatch):
    # W5 through the real Fetcher: the file's URL and its range, a foreign answer refused; GFS-Wave's
    # cache paths under work/gfswave/.
    tmp = no_requests
    fetcher = run.Fetcher(config.GFSWAVE, run.parse_cycle(WAVE_CYCLE), tmp / "work")
    assert fetcher.idx_path(123).relative_to(tmp).as_posix() == f"work/gfswave/{WAVE_CYCLE}/f123.idx"
    asked = []
    body = {"message": None}

    def fake_get(url, byte_range=None):
        asked.append((url, byte_range))
        if byte_range is None:
            return wave_text(123).encode("utf-8")
        return body["message"](byte_range[1] - byte_range[0] + 1)

    monkeypatch.setattr(gfs, "http_get", fake_get)
    record = config.GFSWAVE.instant_record(fetcher.idx(123), "HTSGW:surface", 123)
    body["message"] = lambda n: b"x" * n
    with pytest.raises(OSError, match="does not begin with GRIB"):
        fetcher.messages([(123, record)])
    assert not fetcher.message_path(123, record.number).exists()
    body["message"] = grib2
    data = fetcher.messages([(123, record)])[(123, 5)]
    assert len(data) == record.end - record.offset + 1 == 3457677 - 3033846
    stem = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261124/12/wave/gridded/gfswave.t12z.global.0p25.f123.grib2"
    assert asked == [(stem + ".idx", None), (stem, (3033846, 3457676)), (stem, (3033846, 3457676))]
    assert fetcher.message_path(123, 5).stat().st_size == len(data)


def test_the_local_form_builds_gfswave(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = three_sources(monkeypatch, work)
    assert run.main(["--out", str(out), "--work", str(work), "--source", "gfswave", "--cycle", WAVE_CYCLE,
                     "--hours", "0,123,384"]) == 0
    assert probed == ["gfswave"]
    assert sorted(p.name for p in (out / "gfswave" / WAVE_CYCLE).iterdir()) == ["wave_height.sfc", "wave_period.sfc"]


# ---- F1 (W11): GFS-Wave's job in a child process in the workflow's form ------------------------------

ON_RUNNER = os.environ.get("GITHUB_ACTIONS") == "true"
CHILD_REPORT = {"seconds": 90.0, "downloaded_bytes": 12_300_000}


class ChildDone:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def fake_child(monkeypatch, returncode=0, raises=None, manifest="good", folder=True, stdout="", stderr=""):
    """subprocess.run replaced by a fake child that records its call. With exit 0 it writes what a child
    writes, under the paths of the command line: its site's manifest ([manifest]: "good", "none" with no
    gfswave entry, or "other" naming another release), its release folder when [folder], and its report."""
    calls = []

    def run_child(args, **kwargs):
        calls.append((list(args), kwargs))
        if raises is not None:
            raise raises
        if returncode == 0:
            site_dir, work = Path(args[args.index("--out") + 1]), Path(args[args.index("--work") + 1])
            release = args[args.index("--cycle") + 1]
            entry = run.release_entry(config.GFSWAVE, run.parse_cycle(release), WAVE_HOURS, config.GFSWAVE.fields)
            sources = {"good": {"gfswave": entry}, "none": {},
                       "other": {"gfswave": dict(entry, release=WAVE_OLD, path=f"gfswave/{WAVE_OLD}")}}[manifest]
            site_dir.mkdir(parents=True, exist_ok=True)
            (site_dir / "manifest.json").write_text(json.dumps({"version": 1, "sources": sources}), encoding="utf-8")
            if folder:
                tile = site_dir / "gfswave" / release / "wave_height.sfc" / "60x60-1" / "1-3.wxt"
                tile.parent.mkdir(parents=True)
                tile.write_bytes(b"child tile")
            (work / f"report-gfswave-{release}.json").write_text(json.dumps(CHILD_REPORT), encoding="utf-8")
        return ChildDone(returncode, stdout, stderr)

    monkeypatch.setattr(run.subprocess, "run", run_child)
    return calls


def test_gfswave_is_built_in_a_child_process(no_requests, monkeypatch, capsys):
    # F1: the command line, cwd, environment and timeout; the entry merged and the release moved in.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    probed = three_sources(monkeypatch, work, in_child=True)
    stale = work / "child" / "gfswave" / WAVE_CYCLE / "site" / "left.wxt"   # a killed run's
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    calls = fake_child(monkeypatch, stdout="wave_height.sfc: 1.0 MB in 1 s\n")
    monkeypatch.setenv("PYTHONPATH", "elsewhere")
    before = site_with_three_sources(out)
    assert run_three(out, work) == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    child_site = work / "child" / "gfswave" / WAVE_CYCLE / "site"
    assert args == [sys.executable, "-m", "wxpipe.run", "--out", str(child_site), "--work", str(work),
                    "--source", "gfswave", "--cycle", WAVE_CYCLE, "--hours", "0,123,384", "--keep", "1",
                    "--precip-method", "bucket", "--workers", "8"]
    assert Path(kwargs["cwd"]) == run.ROOT and kwargs["timeout"] == 1200 == 20 * 60
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    env = kwargs["env"]
    assert "GITHUB_OUTPUT" not in env and "GITHUB_STEP_SUMMARY" not in env
    assert env["PYTHONPATH"].split(os.pathsep) == [str(run.ROOT), "elsewhere"]
    assert env.get("PATH") == os.environ.get("PATH")
    # The parent probes nothing itself: the child does.
    assert probed == []
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["gfswave"] == run.release_entry(config.GFSWAVE, run.parse_cycle(WAVE_CYCLE), WAVE_HOURS,
                                                               config.GFSWAVE.fields)
    assert manifest["previous"]["gfswave"] == before["sources"]["gfswave"]
    assert manifest["sources"]["gfs"]["release"] == CYCLE
    assert (out / "gfswave" / WAVE_CYCLE / "wave_height.sfc" / "60x60-1" / "1-3.wxt").read_bytes() == b"child tile"
    assert not (out / "gfswave" / WAVE_OLDER).exists()
    assert not (work / "child").exists() and not (work / "stage" / "gfswave").exists()
    printed = capsys.readouterr().out
    assert "gfswave child: wave_height.sfc: 1.0 MB in 1 s" in printed and "::error::" not in printed
    text = (tmp / "summary.md").read_text(encoding="utf-8")
    assert f"- gfswave {WAVE_CYCLE}: 3 frames, 2 fields, 1.5 min, 12.3 MB downloaded" in text
    assert f"GFSWAVE {WAVE_CYCLE}: 3 frames, 2 fields, 90.0 s, 12.3 MB downloaded" in printed
    assert_ifs_deferred(tmp, out, before, printed)
    assert (tmp / "output.txt").read_text(encoding="utf-8") == "published=true\n"


RUN_REFUSED = (f"Traceback (most recent call last):\n  ...\nwxpipe.run.RunRefused: run refused, every source "
               f"skipped: gfswave {WAVE_CYCLE}: records missing: wave_height.sfc at hour 123\n")


@pytest.mark.parametrize("child, reason", [
    (dict(returncode=1, stdout=f"::error::Skipped gfswave {WAVE_CYCLE}: records missing\n", stderr=RUN_REFUSED),
     "child of gfswave failed, exit 1: wxpipe.run.RunRefused: run refused, every source skipped"),
    (dict(returncode=-11), "child of gfswave failed, exit -11: no output"),
    (dict(raises=subprocess.TimeoutExpired(["python"], 1200, output=b"wave_height.sfc: 1.0 MB in 50 s\n")),
     "child of gfswave timed out after 1200 s: wave_height.sfc: 1.0 MB in 50 s"),
    (dict(manifest="none"), "child of gfswave exited 0 but its manifest holds no gfswave entry, not 2026112412"),
    (dict(manifest="other"), "child of gfswave exited 0 but its manifest holds release 2026112406, not 2026112412"),
    (dict(folder=False), "child of gfswave exited 0 but wrote no folder gfswave/2026112412"),
], ids=["exit 1", "crash", "timeout", "no entry", "another release", "no folder"])
def test_a_failed_child_skips_gfswave_and_gfs_is_published(no_requests, monkeypatch, capsys, child, reason):
    # F1: each way a child can fail; GFS built and published, IFS deferred, nothing of the new release
    # in the site and nothing under <work>/child.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work, in_child=True)
    fake_child(monkeypatch, **child)
    before = site_with_three_sources(out)
    assert run_three(out, work) == 0
    printed = capsys.readouterr().out
    assert_wave_skipped_gfs_published_ifs_deferred(tmp, out, work, before, printed, reason)
    assert not (work / "child").exists()
    # The child's own lines are in the log, marked as the child's, so its ::error:: is not a second one.
    if "stdout" in child:
        assert f"gfswave child: ::error::Skipped gfswave {WAVE_CYCLE}: records missing" in printed


def test_a_lost_cache_builds_gfswave_in_its_child(no_requests, monkeypatch, capsys):
    # F1 on a lost cache: GFS in the run's process, GFS-Wave in its child, IFS deferred.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work, in_child=True)
    calls = fake_child(monkeypatch)
    out.mkdir()
    assert run_three(out, work, cycles="") == 0
    assert len(calls) == 1
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["sources"]) == ["gfs", "gfswave"] and "previous" not in manifest
    assert (out / "gfswave" / WAVE_CYCLE).is_dir() and not (work / "child").exists()
    assert_ifs_deferred(tmp, out, None, capsys.readouterr().out)


def test_the_local_form_never_starts_a_child(no_requests, monkeypatch):
    # F1: --source builds in its own process, since it is what the child runs.
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    three_sources(monkeypatch, work, in_child=True)
    assert config.source("gfswave").child_seconds == 1200
    assert run_wave_locally(out, work) == 0   # the autouse fixture refuses any subprocess.run
    assert (out / "gfswave" / WAVE_CYCLE).is_dir()


@pytest.mark.skipif(ON_RUNNER, reason="no real GFS-Wave decode in a test on the runner (R9)")
def test_a_real_child_builds_gfswave_from_its_cache(no_requests, monkeypatch, capsys):
    # F1 with a real child process: a cache of two hours under tmp_path, each message the committed probe
    # message behind a synthetic index line whose record length is the file's size, so run.Fetcher reuses
    # every file. The job's hours come from the source's forecast hours, the workflow form's rule, so the
    # source is replaced by one of two hours here; the child takes them from --hours. Any request the
    # child might still make goes to an unreachable proxy.
    from wxpipe import probe

    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    hours = (0, 1)
    message = (probe.SAMPLES / "gfswave-htsgw-jpeg.grib2").read_bytes()
    n = len(message)
    cache = work / "gfswave" / WAVE_CYCLE
    for h in hours:
        form = "anl" if h == 0 else f"{h} hour fcst"
        lines = [f"5:0:d={WAVE_CYCLE}:HTSGW:surface:{form}:", f"6:{n}:d={WAVE_CYCLE}:PERPW:surface:{form}:",
                 f"7:{2 * n}:d={WAVE_CYCLE}:DIRPW:surface:{form}:"]
        (cache / f"f{h:03d}").mkdir(parents=True)
        (cache / f"f{h:03d}.idx").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for number in (5, 6):
            (cache / f"f{h:03d}" / f"{number}.grib2").write_bytes(message)
    wave = dataclasses.replace(config.GFSWAVE, forecast_hours=hours)
    monkeypatch.setattr(config, "SOURCES", (config.GFS, wave, config.IFS))
    monkeypatch.setattr(run.subprocess, "run", REAL_SUBPROCESS_RUN)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    (out / "gfs" / CYCLE).mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"version": 1, "generated_at": "x", "sources": {
        "gfs": {"release": CYCLE, "path": f"gfs/{CYCLE}"}}}), encoding="utf-8")
    cycles = f"gfswave:{WAVE_CYCLE}"
    assert run.main(["--out", str(out), "--work", str(work), "--cycles", cycles, "--all-cycles", cycles]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["sources"]["gfswave"]
    assert entry["release"] == WAVE_CYCLE and len(entry["frames"]) == 2
    assert manifest["sources"]["gfs"]["release"] == CYCLE
    for field in ("wave_height.sfc", "wave_period.sfc"):
        assert len(list((out / "gfswave" / WAVE_CYCLE / field).rglob("*.wxt"))) == 108
    assert not (work / "child").exists()
    printed = capsys.readouterr().out
    assert "::error::" not in printed and "gfswave child: " in printed
    assert f"- gfswave {WAVE_CYCLE}: 2 frames, 2 fields" in (tmp / "summary.md").read_text(encoding="utf-8")


# ---- the gust: alternates and frame 0 from the next frame (M6 S16, D1 and D2) ----------------------

def ifs_gust_index(hours, as_hour=None, to_10fg3=()):
    """IFS's fixture records by hour. With [as_hour], hour h holds the f150 fixture's records restamped
    to step h; the steps in [to_10fg3] hold `10fg3` in place of `10fg`."""
    out = {}
    for h in hours:
        fixture = 150 if as_hour else h
        text = (FIXTURES / "ifs" / f"ifs-f{fixture:03d}.index").read_text(encoding="utf-8")
        if as_hour:
            text = text.replace('"step": "150"', f'"step": "{h}"')
        if h in to_10fg3:
            text = text.replace('"param": "10fg"', '"param": "10fg3"')
        out[h] = ecmwf.parse_idx(text)
    return out


class GustFetcher:
    """A fetcher whose message of hour h is the text of h / 2, read by the fake decode below; every
    (hour, record) asked is kept."""

    def __init__(self):
        self.asked = []

    def messages(self, items):
        self.asked.extend(items)
        return {(h, r.number): str(h / 2).encode("ascii") for h, r in items}


@pytest.fixture
def gust_decode(monkeypatch):
    monkeypatch.setattr(grib, "decode", lambda message, src: np.full((config.NY, config.NX), float(message)))


def test_the_gust_is_a_gap_where_neither_its_key_nor_an_alternate_is_present():
    # A16: GFS's gust needs its record at every hour, hour 0's `anl` included.
    assert run.gaps(config.GFS, index(), HOURS, "bucket") == []
    idx = index(drop={(6, "GUST:surface", "6 hour fcst")})
    assert run.gaps(config.GFS, idx, HOURS, "bucket") == [("wind_gust.sfc", 6)]
    idx = index(drop={(0, "GUST:surface", "anl")})
    assert run.gaps(config.GFS, idx, HOURS, "bucket") == [("wind_gust.sfc", 0)]
    # IFS's: 10fg, else 10fg3; with neither, a gap; hour 0 never asked, so never a gap.
    assert run.gaps(config.IFS, ifs_index(), IFS_HOURS, "bucket") == []
    assert run.gaps(config.IFS, ifs_index({("150", "10fg")}), IFS_HOURS, "bucket") == [("wind_gust.sfc", 150)]
    assert run.gaps(config.IFS, ifs_index({("0", "10fg")}), IFS_HOURS, "bucket") == []
    gust = config.IFS.field("wind_gust.sfc")
    moved = ifs_gust_index(IFS_HOURS, to_10fg3=(150,))
    assert run.gaps(config.IFS, moved, IFS_HOURS, "bucket") == []
    record = run.instant_record(config.IFS, gust, moved[150], 150)
    assert record.key == "10fg3:sfc" and record.step == "150"
    assert run.instant_record(config.IFS, gust, ifs_index()[150], 150).key == "10fg:sfc"
    assert run.instant_record(config.IFS, gust, ifs_index({("150", "10fg")})[150], 150) is None
    # Both keys at one step: `grib`'s record is taken, the alternate only where it is absent (the review's V3).
    text = (FIXTURES / "ifs" / "ifs-f150.index").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if '"param": "10fg"' in x)
    both = ecmwf.parse_idx(text.rstrip("\n") + "\n" + line.replace('"param": "10fg"', '"param": "10fg3"') + "\n")
    assert sum(r.key in ("10fg:sfc", "10fg3:sfc") for r in both) == 2
    assert run.instant_record(config.IFS, gust, both, 150).key == "10fg:sfc"


def test_ifs_gust_frame_0_is_frame_1_and_hour_0_is_never_read(gust_decode):
    # A16: over (0, 150, 360) no record of hour 0 is asked, and frame 0's bytes equal frame 1's.
    gust = config.IFS.field("wind_gust.sfc")
    cycle = run.parse_cycle(IFS_CYCLE)
    fetcher = GustFetcher()
    frames = run.build_instant(config.IFS, gust, cycle, IFS_HOURS, ifs_index(), fetcher)
    assert sorted(h for h, _ in fetcher.asked) == [150, 360]
    assert [t for t, _ in frames] == [run.valid_time(cycle, h) for h in IFS_HOURS]
    assert np.array_equal(frames[0][1], frames[1][1]) and frames[0][1] is not frames[1][1]
    assert np.all(frames[1][1] == gust.lut.encode(75.0)) and np.all(frames[2][1] == gust.lut.encode(180.0))
    assert not np.array_equal(frames[1][1], frames[2][1])
    # Hour 0 alone: nothing read, and the frame stays missing.
    fetcher = GustFetcher()
    alone = run.build_instant(config.IFS, gust, cycle, (0,), ifs_index(), fetcher)
    assert fetcher.asked == [] and len(alone) == 1 and np.all(alone[0][1] == gust.lut.encode(np.nan))
    # A job without hour 0 reads every hour's own record, 10fg3 where 10fg is absent.
    fetcher = GustFetcher()
    own = run.build_instant(config.IFS, gust, cycle, (3, 6), ifs_gust_index((3, 6), as_hour=True, to_10fg3=(6,)), fetcher)
    assert [(h, r.key) for h, r in fetcher.asked] == [(3, "10fg:sfc"), (6, "10fg3:sfc")]
    assert np.all(own[0][1] == gust.lut.encode(1.5)) and np.all(own[1][1] == gust.lut.encode(3.0))
    assert not np.array_equal(own[0][1], own[1][1])


def test_gfs_gust_reads_its_own_record_at_hour_0(gust_decode):
    # A16: "own", today's behaviour, by GFS's gust at hour 0 from its `anl` record.
    gust = config.GFS.field("wind_gust.sfc")
    fetcher = GustFetcher()
    frames = run.build_instant(config.GFS, gust, run.parse_cycle(CYCLE), (0, 6), index((0, 6)), fetcher)
    assert [(h, r.number, r.fcst) for h, r in fetcher.asked] == [(0, 14, "anl"), (6, 14, "6 hour fcst")]
    assert np.all(frames[0][1] == gust.lut.encode(0.0)) and np.all(frames[1][1] == gust.lut.encode(3.0))


@pytest.mark.parametrize("src, cycle, hours", [(config.GFS, CYCLE, (0, 1)), (config.IFS, IFS_CYCLE, IFS_HOURS)],
                         ids=["gfs", "ifs"])
def test_the_entry_lists_the_gust_last_and_every_other_field_as_before(src, cycle, hours):
    # A17.
    entry = run.release_entry(src, run.parse_cycle(cycle), hours, src.fields)
    six = run.release_entry(src, run.parse_cycle(cycle), hours, src.fields[:6])
    assert list(entry["fields"]) == list(six["fields"]) + ["wind_gust.sfc"]
    assert entry["fields"]["wind_gust.sfc"] == {"units": "m s**-1", "values": config.GUST_LUT.header()}
    # The user's table after round 1 (the fix round's F10).
    assert config.GUST_LUT.header() == {"scale": "lin", "breakpoints": [[0, 0.0], [120, 60.0], [140, 80.0]], "missing": 255}
    assert {k: v for k, v in entry["fields"].items() if k != "wind_gust.sfc"} == six["fields"]
    assert list(entry) == list(six) and {k: v for k, v in entry.items() if k != "fields"} == {
        k: v for k, v in six.items() if k != "fields"}
