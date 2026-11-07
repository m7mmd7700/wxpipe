"""The workflow's cheap first step: is there a complete GFS run that the site does not have yet?

    python -m wxpipe.check --manifest https://<owner>.github.io/<repo>/manifest.json

Standard library only, so it runs on the runner's own Python before any install. It writes
`new=true|false` and `cycle=YYYYMMDDHH` to $GITHUB_OUTPUT when that variable is set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from datetime import datetime, timezone

from . import gfs

# Last forecast hour of a run; wxpipe.config.FORECAST_HOURS ends here too (checked by a test).
LAST_HOUR = 384
SOURCE = "gfs"


def is_new(release: str, live_manifest: dict | None) -> bool:
    published = (live_manifest or {}).get("sources", {}).get(SOURCE, {}).get("release")
    return published != release


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxpipe.check")
    p.add_argument("--manifest", required=True, help="URL of the live manifest.json")
    args = p.parse_args(argv)

    cycle = gfs.latest_complete_cycle(datetime.now(timezone.utc), LAST_HOUR)
    if cycle is None:
        print("No complete GFS run found.")
        _output(new="false")
        return 0
    release = f"{cycle:%Y%m%d%H}"
    try:
        live = json.loads(gfs.http_get(args.manifest, retries=2).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        live = None
    new = is_new(release, live)
    on_site = (live or {}).get("sources", {}).get(SOURCE, {}).get("release", "nothing")
    print(f"Newest complete GFS run: {release}; on the site: {on_site}; build: {'yes' if new else 'no'}")
    _output(new=str(new).lower(), cycle=release)
    return 0


def _output(**values: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


if __name__ == "__main__":
    sys.exit(main())
