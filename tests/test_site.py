"""The site of several sources: the manifest merge, pruning per source, and which sources a run builds."""

import json
import re
from datetime import datetime, timezone

import pytest

from wxpipe import config, gfs, run, site

NOW = datetime(2026, 11, 7, 15, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_requests(monkeypatch, tmp_path):
    """Every test here: any request fails the test, and the workflow's files point into tmp_path."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))


def entry(name: str, release: str) -> dict:
    return {"release": release, "path": f"{name}/{release}"}


def manifest(sources: dict, previous: dict | None = None) -> dict:
    return site.assemble(sources, previous or {}, NOW)


# ---- the merge -------------------------------------------------------------------------------

def test_a_build_of_one_source_keeps_the_other_unchanged():
    old = manifest({"gfs": entry("gfs", "2026110706"), "ifs": entry("ifs", "2026110700")},
                   {"gfs": entry("gfs", "2026110700"), "ifs": entry("ifs", "2026110612")})
    new = site.merge(old, {"gfs": entry("gfs", "2026110712")}, 2, NOW)
    assert new["sources"] == {"gfs": entry("gfs", "2026110712"), "ifs": entry("ifs", "2026110700")}
    assert new["previous"] == {"gfs": entry("gfs", "2026110706"), "ifs": entry("ifs", "2026110612")}
    assert list(new["sources"]) == ["gfs", "ifs"]


def test_a_new_release_moves_the_old_current_to_previous():
    old = manifest({"gfs": entry("gfs", "2026110706")}, {"gfs": entry("gfs", "2026110700")})
    new = site.merge(old, {"gfs": entry("gfs", "2026110712")}, 2, NOW)
    assert new["sources"] == {"gfs": entry("gfs", "2026110712")}
    assert new["previous"] == {"gfs": entry("gfs", "2026110706")}


def test_a_rerun_of_the_same_release_keeps_the_old_previous():
    old = manifest({"gfs": entry("gfs", "2026110712")}, {"gfs": entry("gfs", "2026110706")})
    rebuilt = dict(entry("gfs", "2026110712"), frames=[1])
    new = site.merge(old, {"gfs": rebuilt}, 2, NOW)
    assert new["sources"] == {"gfs": rebuilt}
    assert new["previous"] == {"gfs": entry("gfs", "2026110706")}
    # A rerun on a site whose source had no previous release still has none.
    new = site.merge(manifest({"gfs": entry("gfs", "2026110712")}), {"gfs": rebuilt}, 2, NOW)
    assert "previous" not in new


def test_cases_with_no_previous_release():
    built = {"gfs": entry("gfs", "2026110712")}
    for old in (None, {}, manifest({"ifs": entry("ifs", "2026110700")})):
        new = site.merge(old, built, 2, NOW)
        assert "previous" not in new or "gfs" not in new["previous"]
    old = manifest({"gfs": entry("gfs", "2026110706")}, {"gfs": entry("gfs", "2026110700")})
    assert "previous" not in site.merge(old, built, 1, NOW)
    # keep=1 drops only the built source's previous release.
    old = manifest({"gfs": entry("gfs", "2026110706"), "ifs": entry("ifs", "2026110700")},
                   {"ifs": entry("ifs", "2026110612")})
    assert site.merge(old, built, 1, NOW)["previous"] == {"ifs": entry("ifs", "2026110612")}


def test_an_unreadable_manifest_is_read_as_none(tmp_path):
    path = tmp_path / "manifest.json"
    assert site.read_manifest(path) is None
    path.write_bytes(b"{not json")
    assert site.read_manifest(path) is None
    path.write_bytes(b"[1, 2]")
    assert site.read_manifest(path) is None
    path.write_bytes(b'{"version": 1}')
    assert site.read_manifest(path) == {"version": 1}


GOLDEN = """{
 "version": 1,
 "generated_at": "2026-11-07T15:30:00Z",
 "sources": {
  "gfs": {
   "release": "2026110712",
   "run_time": 1794052800,
   "path": "gfs/2026110712",
   "frames": [
    1794052800,
    1794056400
   ],
   "levels": [
    {
     "size": "60x60/1",
     "region_only": true
    },
    {
     "size": "120x120/2",
     "region_only": false
    },
    {
     "size": "240x240/4",
     "region_only": false
    },
    {
     "size": "480x480/8",
     "region_only": false
    }
   ],
   "region": {
    "west": 15.0,
    "east": 75.0,
    "south": 0.0,
    "north": 45.0
   },
   "fields": {
    "temperature.2m": {
     "units": "C",
     "values": {
      "scale": "lin",
      "breakpoints": [
       [
        0,
        -70.0
       ],
       [
        20,
        -30.0
       ],
       [
        230,
        50.0
       ],
       [
        254,
        60.0
       ]
      ],
      "missing": 255
     }
    }
   },
   "attribution": "NOAA NCEP Global Forecast System (GFS). Public domain."
  }
 }
}"""


def test_a_gfs_only_manifest_has_todays_shape():
    cycle = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    built = {"gfs": run.release_entry(config.GFS, cycle, (0, 1), [config.GFS.field("temperature.2m")])}
    new = site.merge(None, built, 2, NOW)
    assert json.dumps(new, indent=1) == GOLDEN
    live = site.merge(None, built, 2, datetime.now(timezone.utc))
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", live["generated_at"])


# ---- pruning ---------------------------------------------------------------------------------

def test_pruning_deletes_only_the_built_sources_old_folders(tmp_path):
    for path in ("gfs/2026110700", "gfs/2026110706", "gfs/2026110712", "ifs/2026110612", "ifs/2026110700",
                 "ifs/2026110512"):
        (tmp_path / path).mkdir(parents=True)
    (tmp_path / "gfs" / "notes.txt").write_bytes(b"a file, not a release")
    new = manifest({"gfs": entry("gfs", "2026110712"), "ifs": entry("ifs", "2026110700")},
                   {"gfs": entry("gfs", "2026110706"), "ifs": entry("ifs", "2026110612")})
    assert site.prune(tmp_path, new, {"gfs": {}}) == ["gfs/2026110700"]
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "gfs", "gfs/2026110706", "gfs/2026110712", "gfs/notes.txt",
        "ifs", "ifs/2026110512", "ifs/2026110612", "ifs/2026110700"]


def test_pruning_a_source_with_no_folder_yet(tmp_path):
    assert site.prune(tmp_path, manifest({"gfs": entry("gfs", "2026110712")}), {"gfs": {}}) == []


# ---- which sources a run builds ----------------------------------------------------------------

def test_to_build():
    cycles = site.parse_cycles("ifs:2026110712")
    every = site.parse_cycles("gfs:2026110712,ifs:2026110712")
    assert every == {"gfs": "2026110712", "ifs": "2026110712"}
    assert site.to_build(cycles, every, site_has_manifest=True) == {"ifs": "2026110712"}
    assert site.to_build(cycles, every, site_has_manifest=False) == every
    assert site.to_build({}, every, site_has_manifest=True) == {}
    assert site.parse_cycles("") == {} and site.parse_cycles(None) == {}
    assert site.format_cycles(every) == "gfs:2026110712,ifs:2026110712"


def test_to_build_reads_an_unreadable_manifest_as_none(tmp_path):
    (tmp_path / "manifest.json").write_bytes(b"\x00 broken")
    has = site.read_manifest(tmp_path / "manifest.json") is not None
    assert site.to_build({"gfs": "2026110712"}, {"gfs": "2026110712", "ifs": "2026110700"}, has) == {
        "gfs": "2026110712", "ifs": "2026110700"}
