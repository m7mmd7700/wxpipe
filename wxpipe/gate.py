"""The size gate between the build and the upload: the site must fit the limit, or nothing is published.

    python -m wxpipe.gate --site site [--limit-bytes 900000000]

It sums every file of the site folder, manifest.json included and status.json excluded, since the gate
writes that file after the sum. Above the limit it degrades before it fails, trying these candidates
in order on the projected total, before any file is deleted:

    1. drop ifs's previous release;
    2. and drop gfswave's previous release;
    3. or, in place of 2: drop gfswave's current release and publish its previous one as current.

The first candidate at or under the limit is applied: its folders are deleted and the manifest is
rewritten. If none fits, nothing is deleted and the exit code is 1, so the workflow uploads nothing
and the live site stays as it was. GFS is never dropped. In every case it writes <site>/status.json
and appends the same facts to $GITHUB_STEP_SUMMARY. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import site as sitelib
from . import summary

LIMIT_BYTES = 900_000_000   # the plan's 900 MB, in budget.py's decimal megabytes
STATUS = "status.json"
MANIFEST = "manifest.json"


def folder_bytes(folder: Path, skip: Path | None = None) -> int:
    total = 0
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            path = Path(root) / name
            if skip is not None and path == skip:
                continue
            total += os.path.getsize(path)
    return total


def site_bytes(site: Path) -> int:
    """Every file under the site folder but its status.json."""
    return folder_bytes(site, skip=site / STATUS)


def _entry(manifest: dict, key: str, name: str) -> dict | None:
    entry = (manifest.get(key) or {}).get(name)
    return entry if isinstance(entry, dict) and entry.get("path") else None


def candidates(manifest: dict) -> list[list[dict]]:
    """Each candidate as its drops, {"source", "release", "role"}, in the gate's order. Candidates that
    would drop nothing new are left out."""
    drops = []
    ifs_previous = _entry(manifest, "previous", "ifs")
    wave_previous = _entry(manifest, "previous", "gfswave")
    wave_current = _entry(manifest, "sources", "gfswave")
    first = [{"source": "ifs", "release": ifs_previous["release"], "role": "previous"}] if ifs_previous else []
    if first:
        drops.append(first)
    if wave_previous:
        drops.append(first + [{"source": "gfswave", "release": wave_previous["release"], "role": "previous"}])
        if wave_current:
            drops.append(first + [{"source": "gfswave", "release": wave_current["release"], "role": "current"}])
    return drops


def _path(manifest: dict, drop: dict) -> str:
    key = "previous" if drop["role"] == "previous" else "sources"
    return manifest[key][drop["source"]]["path"]


def plan(manifest: dict | None, total: int, sizes: dict[str, int], limit: int) -> list[dict] | None:
    """The drops of the first candidate whose projected total fits, [] when the site fits as it is, or
    None when nothing fits. [sizes] maps each release path to its bytes."""
    if total <= limit:
        return []
    for drops in candidates(manifest or {}):
        if total - sum(sizes[_path(manifest, d)] for d in drops) <= limit:
            return drops
    return None


def apply(site: Path, manifest: dict, drops: list[dict], now: datetime) -> dict:
    """Deletes the dropped releases' folders and returns the rewritten manifest."""
    sources = dict(manifest.get("sources") or {})
    previous = dict(manifest.get("previous") or {})
    for drop in drops:
        name = drop["source"]
        folder = site / _path(manifest, drop)
        if folder.is_dir():
            shutil.rmtree(folder)
        if drop["role"] == "previous":
            previous.pop(name, None)
        else:
            # The current release goes; the previous one is published as current in its place.
            sources[name] = previous.pop(name)
    return sitelib.assemble(sources, previous, now)


def status(now: datetime, limit: int, before: int, after: int, dropped: list[dict], manifest: dict | None) -> dict:
    return {
        "generated_at": now.strftime(sitelib.TIME_FORMAT),
        "limit_bytes": limit,
        "total_bytes_before": before,
        "total_bytes": after,
        "dropped": dropped,
        "sources": {name: entry.get("release") for name, entry in ((manifest or {}).get("sources") or {}).items()},
    }


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def summary_lines(state: dict, fits: bool) -> list[str]:
    mb = _mb
    lines = ["### Size gate", "",
             f"- Site: {mb(state['total_bytes_before'])} before, {mb(state['total_bytes'])} after, "
             f"limit {mb(state['limit_bytes'])}"]
    for d in state["dropped"]:
        lines.append(f"- Dropped {d['source']} {d['release']} ({d['role']})")
    if not state["dropped"]:
        lines.append("- Dropped nothing")
    if fits:
        lines.append("- Published: " + ", ".join(f"{n} {r}" for n, r in state["sources"].items()))
    else:
        lines.append("- Failed: no candidate fits the limit; nothing is published")
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxpipe.gate")
    p.add_argument("--site", required=True, type=Path, help="the built site folder")
    p.add_argument("--limit-bytes", type=int, default=LIMIT_BYTES)
    args = p.parse_args(argv)
    site, limit = args.site, args.limit_bytes

    now = datetime.now(timezone.utc)
    manifest = sitelib.read_manifest(site / MANIFEST)
    before = site_bytes(site)
    sizes = {}
    for drops in candidates(manifest or {}):
        for d in drops:
            path = _path(manifest, d)
            sizes[path] = folder_bytes(site / path)
    drops = plan(manifest, before, sizes, limit)

    if drops is None:
        state = status(now, limit, before, before, [], manifest)
    else:
        if drops:
            manifest = apply(site, manifest, drops, now)
            (site / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        state = status(now, limit, before, site_bytes(site), drops, manifest)
    (site / STATUS).write_text(json.dumps(state, indent=1), encoding="utf-8")

    lines = summary_lines(state, drops is not None)
    for line in lines:
        print(line)
    summary.append(lines)
    return 1 if drops is None else 0


if __name__ == "__main__":
    sys.exit(main())
