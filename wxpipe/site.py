"""The site of several sources: the manifest merge, pruning per source, and which sources a run builds.

Standard library only; pure functions on dicts and folders, used by `wxpipe.run` and `wxpipe.gate`.
The manifest keeps one shape for any number of sources: `version`, `generated_at`, `sources` (each
source's current release), `previous` (each source's previous release, present only when it holds
an entry) and `held_back` (each source whose current release the size gate dropped, with that release,
present only when it holds an entry; `wxpipe.check` does not offer that release again).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def read_manifest(path: Path) -> dict | None:
    """The site's manifest, or None when it is absent or unreadable."""
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _entries(manifest: dict | None, key: str) -> dict:
    value = (manifest or {}).get(key)
    return value if isinstance(value, dict) else {}


def assemble(sources: dict, previous: dict, now: datetime, held_back: dict | None = None) -> dict:
    """A manifest in the one shape, `previous` and `held_back` each left out when it is empty."""
    manifest = {"version": 1, "generated_at": now.strftime(TIME_FORMAT), "sources": sources}
    if previous:
        manifest["previous"] = previous
    if held_back:
        manifest["held_back"] = held_back
    return manifest


def merge(old_manifest: dict | None, built: dict, keep: int, now: datetime) -> dict:
    """The site's new manifest after a run that built the sources of [built], name -> new entry.

    Each built source's entry replaces its old one. Its previous entry is none when [keep] is under 2
    or the old manifest lacks the source; the old current one when the releases differ; and the old
    previous one, if any, when the same release was built again, so a rerun after a failed deploy
    keeps what phones on the old manifest still read. Every other source is kept as it is, its
    `held_back` entry among it; a built source's `held_back` entry is left out, since its new build
    replaces what the size gate held back.
    """
    old_sources = _entries(old_manifest, "sources")
    old_previous = _entries(old_manifest, "previous")
    sources = dict(old_sources)
    previous = dict(old_previous)
    held_back = {name: release for name, release in _entries(old_manifest, "held_back").items()
                 if name not in built}
    for name, entry in built.items():
        prior = old_sources.get(name)
        kept = None
        if keep >= 2 and isinstance(prior, dict) and prior.get("release") is not None:
            kept = prior if prior["release"] != entry["release"] else old_previous.get(name)
        sources[name] = entry
        if kept:
            previous[name] = kept
        else:
            previous.pop(name, None)
    return assemble(sources, previous, now, held_back)


def releases(manifest: dict | None, name: str) -> set[str]:
    """The releases of source [name] that the manifest names: its current and its previous one."""
    out = set()
    for key in ("sources", "previous"):
        entry = _entries(manifest, key).get(name)
        if isinstance(entry, dict) and entry.get("release"):
            out.add(entry["release"])
    return out


def prune(site: Path, manifest: dict, built) -> list[str]:
    """Deletes every folder under <site>/<source>/ of each built source that is neither its current nor
    its previous release. Other sources' folders are never read. Returns the deleted paths, `/`-joined."""
    deleted = []
    for name in sorted(built):
        root = Path(site) / name
        if not root.is_dir():
            continue
        keep = releases(manifest, name)
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and folder.name not in keep:
                shutil.rmtree(folder)
                deleted.append(f"{name}/{folder.name}")
    return deleted


def parse_cycles(text: str | None) -> dict[str, str]:
    """`gfs:2026110712,ifs:2026110700` -> {"gfs": "2026110712", "ifs": "2026110700"}; empty text -> {}."""
    out: dict[str, str] = {}
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, cycle = item.partition(":")
        if not sep or not name or not cycle:
            raise ValueError(f"expected <name>:<cycle>, got {item!r}")
        out[name] = cycle
    return out


def format_cycles(cycles: dict[str, str]) -> str:
    return ",".join(f"{name}:{cycle}" for name, cycle in cycles.items())


def to_build(cycles: dict[str, str], all_cycles: dict[str, str], site_has_manifest: bool) -> dict[str, str]:
    """The sources a workflow run builds: the new ones, or every source with a complete run when the
    restored site has no readable manifest (a lost cache), so the site is never published without a
    source. The previous releases are gone after a lost cache until later runs build new ones."""
    return dict(cycles if site_has_manifest else all_cycles)
