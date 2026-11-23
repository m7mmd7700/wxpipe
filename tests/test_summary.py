"""The deploy job's summary: minutes, attempts, outcome, the 6-minute warning, and an exit code of 0 always."""

import pytest

from wxpipe import summary


def test_the_deploy_lines():
    lines = summary.deploy_lines("1000", "1125", 1, "success")
    assert lines == ["### Deploy", "", "- Deploy job: 2.1 min, 1 attempt, outcome success"]
    assert summary.deploy_lines("1000", "1600", 2, "failure")[-1] == "- Deploy job: 10.0 min, 2 attempts, outcome failure"
    assert summary.deploy_lines("", "1600", 1, "skipped")[-1] == "- Deploy job: unknown, 1 attempt, outcome skipped"


def test_the_warning_starts_at_360_seconds():
    assert summary.deploy_warning("1000", "1359") is None
    assert summary.deploy_warning("1000", "1360") == "::warning::The deploy job took 6.0 min, 6 min or more"
    assert summary.deploy_warning("", "1360") is None


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped"])
def test_main_returns_0_for_every_outcome(outcome, monkeypatch, tmp_path, capsys):
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    code = summary.main(["deploy", "--start", "1000", "--end", "1400", "--attempts", "2", "--outcome", outcome])
    assert code == 0
    assert target.read_text(encoding="utf-8") == f"### Deploy\n\n- Deploy job: 6.7 min, 2 attempts, outcome {outcome}\n"
    assert "::warning::The deploy job took 6.7 min" in capsys.readouterr().out


def test_no_summary_file_and_no_warning(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert summary.main(["deploy", "--start", "1000", "--end", "1018", "--attempts", "1", "--outcome", "success"]) == 0
    assert "::warning::" not in capsys.readouterr().out
