"""The workflow's cheap first step: which sources have a complete run that the site does not have yet?

    python -m wxpipe.check --manifest https://<owner>.github.io/<repo>/manifest.json

Standard library only, so it runs on the runner's own Python before any install. It prints one line
per source and writes to $GITHUB_OUTPUT, when that variable is set:

    new=true|false              true if any source is new
    build=<name>:<cycle>,...    the new sources
    all=<name>:<cycle>,...      every source with a complete run, for a site that lost its manifest

A source is new when its newest complete run is later than the live one, so a discovery that falls
back to an older cycle never replaces a newer release. An unreadable live manifest, a source absent
from it, or a live release that is not a YYYYMMDDHH string counts the source as new. One source's
discovery error is printed with its name and counts as no complete run for that source alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timezone

from . import gfs, source

_RELEASE = re.compile(r"[0-9]{10}")


def now() -> datetime:
    return datetime.now(timezone.utc)


def published(name: str, live_manifest: dict | None) -> str | None:
    entry = ((live_manifest or {}).get("sources") or {}).get(name)
    return entry.get("release") if isinstance(entry, dict) else None


def is_new(name: str, release: str, live_manifest: dict | None) -> bool:
    """True when [release] is later than the live one, compared as YYYYMMDDHH text, or when there is no
    readable live release of [name]."""
    live = published(name, live_manifest)
    if not isinstance(live, str) or not _RELEASE.fullmatch(live):
        return True
    return release > live


def outputs(found: dict[str, str | None], live_manifest: dict | None) -> dict[str, str]:
    """$GITHUB_OUTPUT's values from each source's newest complete run (None: no complete run)."""
    complete = {name: release for name, release in found.items() if release is not None}
    new = {name: release for name, release in complete.items() if is_new(name, release, live_manifest)}
    return {
        "new": "true" if new else "false",
        "build": ",".join(f"{n}:{r}" for n, r in new.items()),
        "all": ",".join(f"{n}:{r}" for n, r in complete.items()),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxpipe.check")
    p.add_argument("--manifest", required=True, help="URL of the live manifest.json")
    args = p.parse_args(argv)

    try:
        live = json.loads(gfs.http_get(args.manifest, retries=2).decode("utf-8"))
        live = live if isinstance(live, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        live = None
    found: dict[str, str | None] = {}
    for info in source.INFOS:
        try:
            cycle = source.latest_complete_cycle(info, now(), gfs.exists)
        except Exception as e:   # one source's discovery error stays its own
            print(f"::warning::{info.name}: discovery failed, no complete run counted: {type(e).__name__}: {e}")
            found[info.name] = None
            continue
        release = None if cycle is None else f"{cycle:%Y%m%d%H}"
        found[info.name] = release
        if release is None:
            print(f"{info.name}: no complete run found")
        else:
            new = is_new(info.name, release, live)
            print(f"{info.name}: newest complete run {release}; on the site: "
                  f"{published(info.name, live) or 'nothing'}; build: {'yes' if new else 'no'}")
    _output(**outputs(found, live))
    return 0


def _output(**values: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


if __name__ == "__main__":
    sys.exit(main())
