"""Builds releases of map tiles for one or more sources into one site, and writes its manifest once.

The workflow's form builds the sources that `wxpipe.check` found, every forecast hour and field:

    python -m wxpipe.run --out site --work work --cycles gfs:2026110712 --all-cycles gfs:2026110712

When the site has no readable manifest, a lost cache, it builds every source of --all-cycles instead.

The local form builds one source:

    python -m wxpipe.run --out site [--source gfs] [--cycle 2026110712] [--hours 0,1,6] [--fields temperature.2m]

Without --cycle it takes the newest run whose last forecast hour is published. With
--published-manifest it stops early when that run is already on the site.

Every source's index files are read and checked before anything is written: a run with a missing
record that a frame needs is refused, and the site stays as it was. It writes `published=true|false`
to $GITHUB_OUTPUT and each built source's lines to $GITHUB_STEP_SUMMARY when those variables are set.
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import config, gfs, grib, precip, site, source, summary, tiles, wxt


class RunRefused(ValueError):
    """A run whose index files lack a record that a frame needs."""


LOST_MANIFEST = "The site has no readable manifest.json: every source is built in this run."


def parse_cycle(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def valid_time(cycle: datetime, fhour: int) -> int:
    return int((cycle + timedelta(hours=fhour)).timestamp())


class Fetcher:
    """Byte-range reads of one source's run with a cache under work/, so reruns and tests read local files:
    <work>/<source>/<cycle>/f<FFF>.idx for an index, <work>/<source>/<cycle>/f<FFF>/<record>.grib2 for a message."""

    def __init__(self, src: config.Source, cycle: datetime, work: Path, workers: int = 8):
        self.source = src
        self.cycle = cycle
        self.root = work / src.info.name / f"{cycle:%Y%m%d%H}"
        self.workers = workers
        self.downloaded = 0

    def idx_path(self, fhour: int) -> Path:
        return self.root / f"f{fhour:03d}.idx"

    def message_path(self, fhour: int, number: int) -> Path:
        return self.root / f"f{fhour:03d}" / f"{number}.grib2"

    def idx(self, fhour: int) -> list:
        path = self.idx_path(fhour)
        if not path.exists():
            data = self.source.read(source.idx_url(self.source.info, self.cycle, fhour))
            self.downloaded += len(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return self.source.parse_idx(path.read_text(encoding="utf-8"))

    def _message(self, fhour: int, record) -> bytes:
        path = self.message_path(fhour, record.number)
        if path.exists():
            return path.read_bytes()
        data = self.source.read(source.file_url(self.source.info, self.cycle, fhour), (record.offset, record.end))
        self.downloaded += len(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def messages(self, items: list[tuple[int, object]]) -> dict[tuple[int, int], bytes]:
        with ThreadPoolExecutor(self.workers) as pool:
            data = list(pool.map(lambda item: self._message(*item), items))
        return {(h, r.number): d for (h, r), d in zip(items, data)}


def gaps(src: config.Source, idx: dict, hours, method: str, fields=None) -> list[tuple[str, int]]:
    """Every (field name, frame hour) for which the index records lack what the frame needs, where the
    build would otherwise write a frame of missing bytes. Extra, reordered or repeated records are not gaps.

    An instant field needs its record at the hour. Precipitation needs, for each interval (prev, now],
    the accumulation window that `precip.rate` would take with [method], and the earlier window it
    subtracts; the first frame has no interval.
    """
    out = []
    for field in (src.fields if fields is None else fields):
        if field.kind == "precip_rate":
            prev, prev_keys = None, set()
            for h in hours:
                keys = set(src.accumulation_records(idx[h], field.grib, h))
                if prev is not None:
                    a = precip.window_start(dict.fromkeys(keys), prev, method)
                    if a is None or (a, h) not in keys or (a < prev and (a, prev) not in prev_keys):
                        out.append((field.name, h))
                prev, prev_keys = h, keys
        else:
            out.extend((field.name, h) for h in hours if src.instant_record(idx[h], field.grib, h) is None)
    return out


def build_instant(src: config.Source, field: config.Field, cycle, hours, idx, fetcher) -> list[tuple[int, np.ndarray]]:
    records = {h: src.instant_record(idx[h], field.grib, h) for h in hours}
    data = fetcher.messages([(h, r) for h, r in records.items() if r is not None])
    frames = []
    for h in hours:
        r = records[h]
        if r is None:
            grid = np.full((config.NY, config.NX), np.nan)
        else:
            grid = grib.decode(data[(h, r.number)], src) * field.scale + field.offset
        frames.append((valid_time(cycle, h), field.lut.encode(grid)))
    return frames


def build_precip(src: config.Source, field, cycle, hours, idx, fetcher, method: str, report: dict) -> list[tuple[int, np.ndarray]]:
    records = {h: src.accumulation_records(idx[h], field.grib, h) for h in hours}
    data = fetcher.messages([(h, r) for h, recs in records.items() for r in recs.values()])
    other = "since-start" if method == "bucket" else "bucket"
    frames, prev_h, prev_w = [], None, {}
    for h in hours:
        now_w = {w: grib.decode(data[(h, r.number)], src) * field.scale + field.offset for w, r in records[h].items()}
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


def write_field(release_dir: Path, src: config.Source, field: config.Field, release: str, frames) -> dict:
    sizes = {}
    for level in config.LEVELS:
        total = count = 0
        for tile in tiles.tiles_for(level):
            header = {
                "source": src.info.name,
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


def release_entry(src: config.Source, cycle: datetime, hours, fields) -> dict:
    """A source's entry in the manifest, in the key order the app and the site have always had."""
    release = f"{cycle:%Y%m%d%H}"
    return {
        "release": release,
        "run_time": int(cycle.timestamp()),
        "path": f"{src.info.name}/{release}",
        "frames": [valid_time(cycle, h) for h in hours],
        "levels": [{"size": lv.size, "region_only": lv.region_only} for lv in config.LEVELS],
        "region": config.REGION,
        "fields": {f.name: {"units": f.units, "values": f.lut.header()} for f in fields},
        "attribution": src.attribution,
    }


def read_manifest(location: str | None) -> dict | None:
    """The live manifest for --published-manifest, from a URL or a path; None unless it is a JSON object."""
    if not location:
        return None
    if not location.startswith("http"):
        return site.read_manifest(Path(location))
    try:
        manifest = json.loads(gfs.http_get(location, retries=2).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def set_output(**values) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def published(built) -> str:
    """$GITHUB_OUTPUT's `published`: true when any source was built."""
    return "true" if built else "false"


def summary_lines(name: str, release: str, frames: int, fields: int, seconds: float, downloaded: int) -> list[str]:
    return [f"- {name} {release}: {frames} frames, {fields} fields, {seconds / 60:.1f} min, "
            f"{downloaded / 1e6:.1f} MB downloaded"]


@dataclass
class Job:
    """One source's run to build, with its fetcher and index files once read."""

    source: config.Source
    cycle: datetime
    hours: tuple[int, ...]
    fields: list
    fetcher: Fetcher | None = None
    idx: dict | None = None
    report: dict | None = None

    @property
    def release(self) -> str:
        return f"{self.cycle:%Y%m%d%H}"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wxpipe.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, type=Path, help="site folder; the new releases are added here")
    p.add_argument("--work", default=Path("work"), type=Path, help="download cache")
    form = p.add_mutually_exclusive_group()
    form.add_argument("--cycles", help="the workflow's form: <name>:<cycle>,... of the new sources")
    form.add_argument("--source", help="the local form: one source, default gfs")
    p.add_argument("--all-cycles", help="with --cycles: <name>:<cycle>,... of every source with a complete run")
    p.add_argument("--cycle", help="local form: run as YYYYMMDDHH; default: the newest complete run")
    p.add_argument("--hours", help="local form: comma-separated forecast hours; default: all of the source's")
    p.add_argument("--fields", help="local form: comma-separated field names; default: all")
    p.add_argument("--published-manifest", help="local form: URL or path of the live manifest; stop if its run is this one")
    p.add_argument("--keep", type=int, default=2, help="releases kept on the site per source")
    p.add_argument("--precip-method", choices=["bucket", "since-start"], default="bucket")
    p.add_argument("--workers", type=int, default=8)
    return p


def _source(p: argparse.ArgumentParser, name: str) -> config.Source:
    try:
        return config.source(name)
    except KeyError:
        p.error(f"unknown source {name!r}")


def main(argv: list[str] | None = None) -> int:
    p = _parser()
    args = p.parse_args(argv)
    out = args.out

    if args.cycles is not None:
        local = [o for o in ("cycle", "hours", "fields", "published_manifest") if getattr(args, o) is not None]
        if local or args.all_cycles is None:
            p.error("--cycles takes --all-cycles and none of --cycle, --hours, --fields, --published-manifest")
        has_manifest = site.read_manifest(out / "manifest.json") is not None
        try:
            chosen = site.to_build(site.parse_cycles(args.cycles), site.parse_cycles(args.all_cycles), has_manifest)
        except ValueError as e:
            p.error(str(e))
        if not has_manifest:
            print(LOST_MANIFEST)
            summary.append([LOST_MANIFEST])
        jobs = []
        for name, cycle in chosen.items():
            src = _source(p, name)
            jobs.append(Job(src, parse_cycle(cycle), src.forecast_hours, list(src.fields)))
        if not jobs:
            print("Nothing to build.")
            set_output(published=published([]))
            return 0
    else:
        if args.all_cycles is not None:
            p.error("--all-cycles goes with --cycles")
        src = _source(p, args.source or "gfs")
        hours = tuple(int(h) for h in args.hours.split(",")) if args.hours else src.forecast_hours
        fields = [src.field(n) for n in args.fields.split(",")] if args.fields else list(src.fields)
        if args.cycle:
            cycle = parse_cycle(args.cycle)
        else:
            cycle = source.latest_complete_cycle(src.info, datetime.now(timezone.utc), gfs.exists, hours[-1])
            if cycle is None:
                print(f"No complete {src.info.name.upper()} run found.")
                set_output(published=published([]))
                return 0
        job = Job(src, cycle, hours, fields)
        live = read_manifest(args.published_manifest)
        if live and ((live.get("sources") or {}).get(src.info.name) or {}).get("release") == job.release:
            print(f"{src.info.name.upper()} {job.release} is already published.")
            set_output(published=published([]), release=job.release)
            return 0
        jobs = [job]

    # Every source's index files are read first, then every source's gaps are checked, all before any
    # release folder is touched; one error names every source and release with its gaps.
    started = time.time()
    for job in jobs:
        job.fetcher = Fetcher(job.source, job.cycle, args.work, args.workers)
        with ThreadPoolExecutor(args.workers) as pool:
            job.idx = dict(zip(job.hours, pool.map(job.fetcher.idx, job.hours)))
    refused = []
    for job in jobs:
        missing = gaps(job.source, job.idx, job.hours, args.precip_method, job.fields)
        if missing:
            refused.append(f"{job.source.info.name} {job.release}: " + ", ".join(f"{name} at hour {h}" for name, h in missing))
    if refused:
        _fail(RunRefused("run refused, records missing: " + "; ".join(refused)))

    # A delivered grid that differs stops the build here, before the manifest is written or anything pruned.
    try:
        built, lines = _build(jobs, out, args, started)
    except RunRefused:
        raise
    except ValueError as e:
        _fail(e)
    _finish(jobs, out, args, built, lines)
    return 0


def _fail(error: Exception) -> None:
    """Names a refused run or a refused grid on the run's page and in its summary, then raises it again."""
    message = str(error)
    print(f"::error::{message}")
    summary.append([f"**Refused:** {message}"])
    raise error


def _build(jobs: list, out: Path, args, started: float) -> tuple[dict, list[str]]:
    """Builds every job's release folder; returns each source's manifest entry and its summary lines."""
    built = {}
    lines = []
    for k, job in enumerate(jobs):
        t_source = started if k == 0 else time.time()   # the first source's time includes the index reads
        name, release = job.source.info.name, job.release
        release_dir = out / name / release
        if release_dir.exists():
            shutil.rmtree(release_dir)
        report = {"release": release, "hours": list(job.hours), "precip_method": args.precip_method, "fields": {}}
        for field in job.fields:
            t0 = time.time()
            if field.kind == "precip_rate":
                precip_report: dict = {}
                frames = build_precip(job.source, field, job.cycle, job.hours, job.idx, job.fetcher,
                                      args.precip_method, precip_report)
                report["precipitation"] = precip_report
            else:
                frames = build_instant(job.source, field, job.cycle, job.hours, job.idx, job.fetcher)
            sizes = write_field(release_dir, job.source, field, release, frames)
            report["fields"][field.name] = {"seconds": round(time.time() - t0, 1), "levels": sizes}
            print(f"{field.name}: {sum(s['bytes'] for s in sizes.values()) / 1e6:.1f} MB in {time.time() - t0:.0f} s")
        built[name] = release_entry(job.source, job.cycle, job.hours, job.fields)
        report["downloaded_bytes"] = job.fetcher.downloaded
        report["seconds"] = round(time.time() - t_source, 1)
        args.work.mkdir(parents=True, exist_ok=True)
        (args.work / f"report-{name}-{release}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        lines += summary_lines(name, release, len(job.hours), len(job.fields), report["seconds"], job.fetcher.downloaded)
        job.report = report
    return built, lines


def _finish(jobs: list, out: Path, args, built: dict, lines: list[str]) -> None:
    """Writes the manifest once, prunes, and reports."""
    # The manifest is written once for every source. A source's release already on the site becomes its
    # "previous", so a phone holding the old manifest can still read its tiles while the new one
    # propagates; a rerun of the same release keeps the previous one. Older releases are deleted.
    manifest = site.merge(site.read_manifest(out / "manifest.json"), built, args.keep, datetime.now(timezone.utc))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    site.prune(out, manifest, built)

    summary.append(["### Build", ""] + lines)
    for job in jobs:
        print(f"{job.source.info.name.upper()} {job.release}: {len(job.hours)} frames, {len(job.fields)} fields, "
              f"{job.report['seconds']} s, {job.fetcher.downloaded / 1e6:.1f} MB downloaded")
    values = {"published": published(built)}
    if len(jobs) == 1:
        values["release"] = jobs[0].release
    set_output(**values)


if __name__ == "__main__":
    sys.exit(main())
