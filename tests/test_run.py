"""run.py offline: the refusal of a run with missing records, the cache paths, the summary and the outputs."""

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, gfs, grib, run, site

FIXTURES = Path(__file__).parent / "fixtures"
HOURS = (0, 6, 7, 120, 384)
CYCLE = "2026110712"


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
    """Every test here: any request fails the test, and the workflow's files point into tmp_path."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return tmp_path


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
    """GFS and a synthetic second source "alt" with GFS's own description, three hours and one field each;
    the fixtures' index files in both caches, [alt_drop] removed from alt's hour 6."""
    from wxpipe import source

    fake_build(monkeypatch, work)
    gfs_small = dataclasses.replace(config.GFS, forecast_hours=(0, 6, 7), fields=config.GFS.fields[:1])
    alt = dataclasses.replace(gfs_small, info=dataclasses.replace(source.GFS, name="alt"))
    monkeypatch.setattr(config, "SOURCES", (gfs_small, alt))
    monkeypatch.setattr(source, "INFOS", (gfs_small.info, alt.info))
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


def test_a_gap_in_the_second_source_refuses_the_run_before_the_first_is_built(no_requests, monkeypatch):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    two_sources(monkeypatch, work, alt_drop=":TMP:2 m above ground:6 hour fcst:")
    site_with_two_sources(out)
    before = tree(out)

    cycles = f"gfs:{CYCLE},alt:{CYCLE}"
    with pytest.raises(run.RunRefused, match=rf"alt {CYCLE}: temperature\.2m at hour 6") as e:
        run.main(["--out", str(out), "--work", str(work), "--cycles", cycles, "--all-cycles", cycles])
    assert "gfs" not in str(e.value).replace("gfs/", "")
    assert tree(out) == before
    assert f"alt {CYCLE}: temperature.2m at hour 6" in (tmp / "summary.md").read_text(encoding="utf-8")


def test_a_grid_refused_in_the_second_source_writes_no_manifest_and_prunes_nothing(no_requests, monkeypatch, capsys):
    tmp = no_requests
    work, out = tmp / "work", tmp / "site"
    two_sources(monkeypatch, work)

    def decode(message, src):
        if src.info.name == "alt":
            raise ValueError("unexpected alt grid: Ni 720 (expected 1440)")
        return np.full((config.NY, config.NX), 280.0)

    monkeypatch.setattr(grib, "decode", decode)
    manifest = site_with_two_sources(out)

    cycles = f"gfs:{CYCLE},alt:{CYCLE}"
    with pytest.raises(ValueError, match="unexpected alt grid"):
        run.main(["--out", str(out), "--work", str(work), "--cycles", cycles, "--all-cycles", cycles])
    assert (out / "manifest.json").read_bytes() == manifest
    assert (out / "gfs" / "2026110700").is_dir() and (out / "gfs" / "2026110706").is_dir()
    assert (out / "alt" / "2026110706").is_dir()
    assert "::error::unexpected alt grid: Ni 720 (expected 1440)" in capsys.readouterr().out
    assert "unexpected alt grid: Ni 720 (expected 1440)" in (tmp / "summary.md").read_text(encoding="utf-8")
    assert not (tmp / "output.txt").exists()


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
