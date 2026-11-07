"""Measures a built release and scales it to a full run: python -m wxpipe.budget site [--frames 209]."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

from . import config

PAGES_LIMIT = 1_000_000_000


def measure(release_dir: Path) -> dict:
    """Per field and level: files, header bytes, frame count and compressed frame bytes."""
    stats: dict = {}
    for path in sorted(release_dir.rglob("*.wxt")):
        field, level = path.parent.parent.name, path.parent.name
        data = path.read_bytes()
        (hlen,) = struct.unpack("<I", data[4:8])
        header = json.loads(data[8:8 + hlen])
        s = stats.setdefault(field, {}).setdefault(level, {"files": 0, "header": 0, "frames": 0, "frame_bytes": 0})
        s["files"] += 1
        s["header"] += 8 + hlen
        s["frames"] += len(header["frames"])
        s["frame_bytes"] += sum(length for _, _, length in header["frames"])
    return stats


def scale(stats: dict, frames: int) -> dict:
    """Bytes for [frames] frames per file: measured mean frame size, index growing with the frame count."""
    out = {}
    for field, levels in stats.items():
        out[field] = {}
        for level, s in levels.items():
            per_frame = s["frame_bytes"] / s["frames"]
            frames_per_file = s["frames"] / s["files"]
            header_per_file = s["header"] / s["files"] + (frames - frames_per_file) * 22
            out[field][level] = s["files"] * (header_per_file + per_frame * frames)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxpipe.budget")
    p.add_argument("site", type=Path)
    p.add_argument("--frames", type=int, default=len(config.FORECAST_HOURS))
    p.add_argument("--keep", type=int, default=2)
    args = p.parse_args(argv)
    manifest = json.loads((args.site / "manifest.json").read_text(encoding="utf-8"))
    release = manifest["sources"][config.SOURCE]
    stats = measure(args.site / release["path"])
    full = scale(stats, args.frames)
    levels = [lv.size.replace("/", "-") for lv in config.LEVELS]
    print(f"Measured {release['release']} ({len(release['frames'])} frames), scaled to {args.frames} frames, MB:")
    print(f"{'field':20}" + "".join(f"{lv:>12}" for lv in levels) + f"{'total':>10}")
    grand = 0.0
    for field, by_level in full.items():
        total = sum(by_level.values())
        grand += total
        print(f"{field:20}" + "".join(f"{by_level.get(lv, 0) / 1e6:12.1f}" for lv in levels) + f"{total / 1e6:10.1f}")
    print(f"{'all fields':20}" + "".join(f"{sum(f.get(lv, 0) for f in full.values()) / 1e6:12.1f}" for lv in levels)
          + f"{grand / 1e6:10.1f}")
    print(f"One release: {grand / 1e6:.0f} MB; {args.keep} kept: {args.keep * grand / 1e6:.0f} MB "
          f"of the {PAGES_LIMIT / 1e6:.0f} MB GitHub Pages limit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
