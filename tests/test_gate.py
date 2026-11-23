"""The size gate on synthetic sites with a small limit: each outcome, status.json and the summary."""

import json
from pathlib import Path

import pytest

from wxpipe import gate

# Bytes of each release folder's one file.
SIZES = {"gfs/2026110712": 100, "gfs/2026110706": 100, "ifs/2026110700": 50, "ifs/2026110612": 50,
         "gfswave/2026110712": 40, "gfswave/2026110706": 30}
SOURCES = {"gfs": "2026110712", "ifs": "2026110700", "gfswave": "2026110712"}
PREVIOUS = {"gfs": "2026110706", "ifs": "2026110612", "gfswave": "2026110706"}


def entry(name, release):
    return {"release": release, "path": f"{name}/{release}"}


def make_site(root: Path, sources=SOURCES, previous=PREVIOUS) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for releases in (sources, previous):
        for name, release in releases.items():
            folder = root / name / release
            folder.mkdir(parents=True)
            (folder / "0-0.wxt").write_bytes(b"x" * SIZES[f"{name}/{release}"])
    manifest = {"version": 1, "generated_at": "2026-11-07T15:30:00Z",
                "sources": {n: entry(n, r) for n, r in sources.items()}}
    if previous:
        manifest["previous"] = {n: entry(n, r) for n, r in previous.items()}
    (root / "manifest.json").write_bytes(json.dumps(manifest, indent=1).encode("utf-8"))
    return root


def files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    return tmp_path


def run_gate(root: Path, limit: int) -> tuple[int, dict, dict]:
    code = gate.main(["--site", str(root), "--limit-bytes", str(limit)])
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    return code, status, json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def test_under_the_limit_nothing_is_dropped_and_the_manifest_is_untouched(env):
    root = make_site(env / "site")
    before = gate.site_bytes(root)
    raw = (root / "manifest.json").read_bytes()
    (root / "status.json").write_bytes(b"x" * 10_000)   # an older status.json is not counted
    code, status, _ = run_gate(root, before)
    assert code == 0
    assert (root / "manifest.json").read_bytes() == raw
    assert status["dropped"] == [] and status["total_bytes"] == status["total_bytes_before"] == before
    assert set(status) == {"generated_at", "limit_bytes", "total_bytes_before", "total_bytes", "dropped", "sources"}
    assert status["limit_bytes"] == before and status["sources"] == SOURCES
    assert before == sum(SIZES.values()) + len(raw)


def test_candidate_1_drops_ifs_previous_release(env):
    root = make_site(env / "site")
    before = gate.site_bytes(root)
    code, status, manifest = run_gate(root, before - 50)
    assert code == 0
    assert status["dropped"] == [{"source": "ifs", "release": "2026110612", "role": "previous"}]
    assert not (root / "ifs" / "2026110612").exists()
    assert manifest["previous"] == {"gfs": entry("gfs", "2026110706"), "gfswave": entry("gfswave", "2026110706")}
    assert manifest["sources"] == {n: entry(n, r) for n, r in SOURCES.items()}
    assert status["total_bytes"] == gate.site_bytes(root) <= before - 50
    assert status["sources"] == SOURCES


def test_candidate_2_drops_gfswave_previous_release_too(env):
    root = make_site(env / "site")
    before = gate.site_bytes(root)
    code, status, manifest = run_gate(root, before - 80)
    assert code == 0
    assert status["dropped"] == [{"source": "ifs", "release": "2026110612", "role": "previous"},
                                 {"source": "gfswave", "release": "2026110706", "role": "previous"}]
    assert manifest["previous"] == {"gfs": entry("gfs", "2026110706")}
    assert status["sources"] == SOURCES
    assert status["total_bytes"] == gate.site_bytes(root) <= before - 80
    assert files(root) == ["gfs/2026110706/0-0.wxt", "gfs/2026110712/0-0.wxt", "gfswave/2026110712/0-0.wxt",
                           "ifs/2026110700/0-0.wxt", "manifest.json", "status.json"]


def test_candidate_3_publishes_gfswave_older_release_instead(env):
    root = make_site(env / "site")
    before = gate.site_bytes(root)
    # Candidate 2 saves 80 bytes, candidate 3 saves 90: only 3 fits.
    code, status, manifest = run_gate(root, before - 85)
    assert code == 0
    assert status["dropped"] == [{"source": "ifs", "release": "2026110612", "role": "previous"},
                                 {"source": "gfswave", "release": "2026110712", "role": "current"}]
    assert manifest["sources"]["gfswave"] == entry("gfswave", "2026110706")
    assert list(manifest["sources"]) == ["gfs", "ifs", "gfswave"]
    assert manifest["previous"] == {"gfs": entry("gfs", "2026110706")}
    assert not (root / "gfswave" / "2026110712").exists() and (root / "gfswave" / "2026110706").is_dir()
    assert status["sources"] == {"gfs": "2026110712", "ifs": "2026110700", "gfswave": "2026110706"}


def test_none_fits_deletes_nothing_and_fails(env):
    root = make_site(env / "site")
    before = gate.site_bytes(root)
    listing = files(root)
    raw = (root / "manifest.json").read_bytes()
    code, status, _ = run_gate(root, before - 95)
    assert code == 1
    assert files(root) == sorted(listing + ["status.json"])
    assert (root / "manifest.json").read_bytes() == raw
    assert status["dropped"] == [] and status["total_bytes"] == status["total_bytes_before"] == before
    assert status["sources"] == SOURCES


def test_gfs_is_never_dropped(env):
    root = make_site(env / "site", {"gfs": "2026110712"}, {"gfs": "2026110706"})
    raw = (root / "manifest.json").read_bytes()
    code, status, _ = run_gate(root, 10)
    assert code == 1 and status["dropped"] == []
    assert (root / "gfs" / "2026110706").is_dir() and (root / "manifest.json").read_bytes() == raw
    for limit in range(0, gate.site_bytes(make_site(env / "big")) + 1, 5):
        assert all(d["source"] != "gfs" for d in gate.plan(
            gate.sitelib.read_manifest(env / "big" / "manifest.json"), gate.site_bytes(env / "big"),
            {f"{n}/{r}": SIZES[f"{n}/{r}"] for n, r in list(SOURCES.items()) + list(PREVIOUS.items())}, limit) or [])


def test_an_emptied_previous_key_is_dropped(env):
    root = make_site(env / "site", {"gfs": "2026110712", "ifs": "2026110700"}, {"ifs": "2026110612"})
    code, status, manifest = run_gate(root, gate.site_bytes(root) - 50)
    assert code == 0 and status["dropped"] == [{"source": "ifs", "release": "2026110612", "role": "previous"}]
    assert "previous" not in manifest
    assert list(manifest) == ["version", "generated_at", "sources"]


def test_candidate_3_is_skipped_without_a_gfswave_previous_release(env):
    root = make_site(env / "site", SOURCES, {"ifs": "2026110612"})
    code, status, _ = run_gate(root, gate.site_bytes(root) - 60)
    assert code == 1 and status["dropped"] == []
    assert (root / "gfswave" / "2026110712").is_dir() and (root / "ifs" / "2026110612").is_dir()


def test_a_site_with_no_manifest(env):
    root = env / "site"
    (root / "gfs" / "2026110712").mkdir(parents=True)
    (root / "gfs" / "2026110712" / "0-0.wxt").write_bytes(b"x" * 100)
    code = gate.main(["--site", str(root), "--limit-bytes", "100"])
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert code == 0 and status["dropped"] == [] and status["sources"] == {} and status["total_bytes"] == 100
    assert not (root / "manifest.json").exists()
    assert gate.main(["--site", str(root), "--limit-bytes", "99"]) == 1
    assert (root / "gfs" / "2026110712" / "0-0.wxt").exists()


def test_the_default_limit():
    assert gate.LIMIT_BYTES == 900_000_000


def test_the_summary_lines(env):
    root = make_site(env / "site")
    before = gate.site_bytes(root)
    run_gate(root, before - 50)
    text = (env / "summary.md").read_text(encoding="utf-8")
    assert text.startswith("### Size gate\n\n- Site: ")
    assert "- Dropped ifs 2026110612 (previous)\n" in text
    assert "- Published: gfs 2026110712, ifs 2026110700, gfswave 2026110712\n" in text

    other = make_site(env / "other")
    (env / "summary.md").unlink()
    run_gate(other, 10)
    text = (env / "summary.md").read_text(encoding="utf-8")
    assert "- Dropped nothing\n" in text and "- Failed: no candidate fits the limit; nothing is published\n" in text
