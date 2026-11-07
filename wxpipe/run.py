"""Builds one GFS release of map tiles and the manifest.

    python -m wxpipe.run --out site [--cycle 2026110712] [--hours 0,1,6] [--fields temperature.2m]

Without --cycle it takes the newest run whose last forecast hour is published. With
--published-manifest it stops early when that run is already on the site. It writes
`published=true|false` to $GITHUB_OUTPUT when that variable is set.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import config, gfs, grib, precip, tiles, wxt

ATTRIBUTION = "NOAA NCEP Global Forecast System (GFS). Public domain."


def parse_cycle(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def valid_time(cycle: datetime, fhour: int) -> int:
    return int((cycle + timedelta(hours=fhour)).timestamp())


class Fetcher:
    """Byte-range reads with a cache under work/, so reruns and tests read local files."""

    def __init__(self, cycle: datetime, work: Path, workers: int = 8):
        self.cycle = cycle
        self.root = work / config.SOURCE / f"{cycle:%Y%m%d%H}"
        self.workers = workers
        self.downloaded = 0

    def idx(self, fhour: int) -> list[gfs.IdxRecord]:
        path = self.root / f"f{fhour:03d}.idx"
        if not path.exists():
            data = gfs.http_get(gfs.idx_url(self.cycle, fhour))
            self.downloaded += len(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return gfs.parse_idx(path.read_text(encoding="utf-8"))

    def _message(self, fhour: int, record: gfs.IdxRecord) -> bytes:
        path = self.root / f"f{fhour:03d}" / f"{record.number}.grib2"
        if path.exists():
            return path.read_bytes()
        data = gfs.http_get(gfs.file_url(self.cycle, fhour), (record.offset, record.end))
        self.downloaded += len(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def messages(self, items: list[tuple[int, gfs.IdxRecord]]) -> dict[tuple[int, int], bytes]:
        with ThreadPoolExecutor(self.workers) as pool:
            data = list(pool.map(lambda item: self._message(*item), items))
        return {(h, r.number): d for (h, r), d in zip(items, data)}


def build_instant(field: config.Field, cycle, hours, idx, fetcher) -> list[tuple[int, np.ndarray]]:
    records = {h: gfs.instant_record(idx[h], field.grib, h) for h in hours}
    data = fetcher.messages([(h, r) for h, r in records.items() if r is not None])
    frames = []
    for h in hours:
        r = records[h]
        if r is None:
            grid = np.full((config.NY, config.NX), np.nan)
        else:
            grid = grib.decode(data[(h, r.number)]) * field.scale + field.offset
        frames.append((valid_time(cycle, h), field.lut.encode(grid)))
    return frames


def build_precip(field, cycle, hours, idx, fetcher, method: str, report: dict) -> list[tuple[int, np.ndarray]]:
    records = {h: gfs.accumulation_records(idx[h], field.grib, h) for h in hours}
    data = fetcher.messages([(h, r) for h, recs in records.items() for r in recs.values()])
    other = "since-start" if method == "bucket" else "bucket"
    frames, prev_h, prev_w = [], None, {}
    for h in hours:
        now_w = {w: grib.decode(data[(h, r.number)]) * field.scale + field.offset for w, r in records[h].items()}
        grid = None
        if prev_h is not None:
            grid, negative = precip.rate(h, prev_h, now_w, prev_w, method)
            alt, alt_negative = precip.rate(h, prev_h, now_w, prev_w, other)
            entry = {"interval": [prev_h, h], method: negative, other: alt_negative}
            if grid is not None and alt is not None:
                diff = np.abs(grid - alt)
                entry["max_abs_diff_mm_h"] = round(float(np.nanmax(diff)), 4)
                entry["cells_differing_over_0.05"] = int(np.count_nonzero(diff > 0.05))
            report.setdefault("intervals", []).append(entry)
        if grid is None:
            grid = np.full((config.NY, config.NX), np.nan)
        frames.append((valid_time(cycle, h), field.lut.encode(grid)))
        prev_h, prev_w = h, now_w
    return frames


def write_field(release_dir: Path, field: config.Field, release: str, frames) -> dict:
    sizes = {}
    for level in config.LEVELS:
        total = count = 0
        for tile in tiles.tiles_for(level):
            header = {
                "source": config.SOURCE,
                "release": release,
                "data": field.name,
                "units": field.units,
                "grid": {
                    "level": str(tile.size), "i": tile.i, "j": tile.j,
                    "grid": [tile.x0, tile.x1, tile.y0, tile.y1],
                    "steps": [tile.size.step, tile.size.step], "nx": tile.nx, "ny": tile.ny,
                },
                "values": field.lut.header(),
            }
            path = release_dir / field.name / tile.size.path / f"{tile.path}.wxt"
            total += wxt.write(path, header, [(t, tile.cut(grid)) for t, grid in frames])
            count += 1
        sizes[level.size] = {"files": count, "bytes": total}
    return sizes


def read_manifest(source: str | None) -> dict | None:
    if not source:
        return None
    try:
        if source.startswith("http"):
            return json.loads(gfs.http_get(source, retries=2).decode("utf-8"))
        path = Path(source)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def set_output(**values) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxpipe.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, type=Path, help="site folder; the new release is added here")
    p.add_argument("--work", default=Path("work"), type=Path, help="download cache")
    p.add_argument("--cycle", help="run as YYYYMMDDHH; default: the newest complete run")
    p.add_argument("--hours", help="comma-separated forecast hours; default: all 209")
    p.add_argument("--fields", help="comma-separated field names; default: all")
    p.add_argument("--published-manifest", help="URL or path of the live manifest; stop if its run is this one")
    p.add_argument("--keep", type=int, default=2, help="releases kept on the site")
    p.add_argument("--precip-method", choices=["bucket", "since-start"], default="bucket")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)

    hours = tuple(int(h) for h in args.hours.split(",")) if args.hours else config.FORECAST_HOURS
    fields = [config.field(n) for n in args.fields.split(",")] if args.fields else list(config.FIELDS)
    if args.cycle:
        cycle = parse_cycle(args.cycle)
    else:
        cycle = gfs.latest_complete_cycle(datetime.now(timezone.utc), hours[-1])
        if cycle is None:
            print("No complete GFS run found.")
            set_output(published="false")
            return 0
    release = f"{cycle:%Y%m%d%H}"

    live = read_manifest(args.published_manifest)
    if live and live.get("sources", {}).get(config.SOURCE, {}).get("release") == release:
        print(f"GFS {release} is already published.")
        set_output(published="false", release=release)
        return 0

    started = time.time()
    fetcher = Fetcher(cycle, args.work, args.workers)
    with ThreadPoolExecutor(args.workers) as pool:
        idx = dict(zip(hours, pool.map(fetcher.idx, hours)))
    out = args.out
    release_dir = out / config.SOURCE / release
    if release_dir.exists():
        shutil.rmtree(release_dir)
    report = {"release": release, "hours": list(hours), "precip_method": args.precip_method, "fields": {}}
    for field in fields:
        t0 = time.time()
        if field.kind == "precip_rate":
            precip_report: dict = {}
            frames = build_precip(field, cycle, hours, idx, fetcher, args.precip_method, precip_report)
            report["precipitation"] = precip_report
        else:
            frames = build_instant(field, cycle, hours, idx, fetcher)
        sizes = write_field(release_dir, field, release, frames)
        report["fields"][field.name] = {"seconds": round(time.time() - t0, 1), "levels": sizes}
        print(f"{field.name}: {sum(s['bytes'] for s in sizes.values()) / 1e6:.1f} MB in {time.time() - t0:.0f} s")

    # The release already on the site becomes "previous", so a phone holding the old manifest can still
    # read its tiles while the new one propagates. Older releases are deleted.
    previous = None
    old = read_manifest(str(out / "manifest.json"))
    if args.keep >= 2 and old and old.get("sources", {}).get(config.SOURCE, {}).get("release") not in (None, release):
        previous = old["sources"][config.SOURCE]
    keep = {release} | ({previous["release"]} if previous else set())
    current = {
        "release": release,
        "run_time": int(cycle.timestamp()),
        "path": f"{config.SOURCE}/{release}",
        "frames": [valid_time(cycle, h) for h in hours],
        "levels": [{"size": lv.size, "region_only": lv.region_only} for lv in config.LEVELS],
        "region": config.REGION,
        "fields": {f.name: {"units": f.units, "values": f.lut.header()} for f in fields},
        "attribution": ATTRIBUTION,
    }
    manifest = {"version": 1, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sources": {config.SOURCE: current}}
    if previous:
        manifest["previous"] = {config.SOURCE: previous}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    for folder in (out / config.SOURCE).iterdir():
        if folder.is_dir() and folder.name not in keep:
            shutil.rmtree(folder)

    report["downloaded_bytes"] = fetcher.downloaded
    report["seconds"] = round(time.time() - started, 1)
    args.work.mkdir(parents=True, exist_ok=True)
    (args.work / f"report-{release}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"GFS {release}: {len(hours)} frames, {len(fields)} fields, {report['seconds']} s, "
          f"{fetcher.downloaded / 1e6:.1f} MB downloaded")
    set_output(published="true", release=release)
    return 0


if __name__ == "__main__":
    sys.exit(main())
