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

from wxpipe import check, gfs, source

ROOT = Path(__file__).resolve().parents[1]
STDLIB_MODULES = ["check", "source", "gfs", "site", "gate", "summary"]

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
