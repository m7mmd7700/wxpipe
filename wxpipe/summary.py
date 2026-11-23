"""Lines for the run's summary page, and the deploy job's own summary.

    python3 -m wxpipe.summary deploy --start <epoch s> --end <epoch s> --attempts 1|2 --outcome success

Standard library only, so the deploy job runs it on the runner's own Python with no install. The
deploy job's span runs from its first step to the summary step; from 360 s on, a `::warning` line is
printed for the run's page. `main` returns 0 for every outcome, so the summary never fails the job.
"""

from __future__ import annotations

import argparse
import os
import sys

WARN_SECONDS = 360


def append(lines: list[str]) -> None:
    """Appends [lines] to $GITHUB_STEP_SUMMARY when that variable is set."""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target and lines:
        with open(target, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")


def _seconds(start: str, end: str) -> int | None:
    try:
        return int(end) - int(start)
    except ValueError:
        return None


def deploy_lines(start: str, end: str, attempts: int, outcome: str) -> list[str]:
    span = _seconds(start, end)
    minutes = "unknown" if span is None else f"{span / 60:.1f} min"
    return ["### Deploy", "", f"- Deploy job: {minutes}, {attempts} attempt{'s' if attempts != 1 else ''}, outcome {outcome}"]


def deploy_warning(start: str, end: str) -> str | None:
    span = _seconds(start, end)
    if span is None or span < WARN_SECONDS:
        return None
    return f"::warning::The deploy job took {span / 60:.1f} min, {WARN_SECONDS // 60} min or more"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxpipe.summary")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("deploy", help="the deploy job's minutes, attempts and outcome")
    d.add_argument("--start", default="", help="the job's first step, epoch seconds")
    d.add_argument("--end", default="", help="the summary step, epoch seconds")
    d.add_argument("--attempts", type=int, default=1)
    d.add_argument("--outcome", default="unknown")
    args = p.parse_args(argv)

    lines = deploy_lines(args.start, args.end, args.attempts, args.outcome or "unknown")
    for line in lines:
        print(line)
    append(lines)
    warning = deploy_warning(args.start, args.end)
    if warning:
        print(warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
