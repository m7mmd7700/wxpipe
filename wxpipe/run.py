"""Builds releases of map tiles for one or more sources into one site, and writes its manifest once.

The workflow's form builds the sources that `wxpipe.check` found, every forecast hour and field:

    python -m wxpipe.run --out site --work work --cycles gfs:2026110712 --all-cycles gfs:2026110712

When the site has no readable manifest, a lost cache, it builds every source of --all-cycles instead,
and refuses the run when GFS is not among the sources built.

The local form builds one source:

    python -m wxpipe.run --out site [--source gfs|ifs] [--cycle 2026110712] [--hours 0,1,6] [--fields temperature.2m]

Without --cycle it takes the newest run whose last forecast hour is published. With
--published-manifest it stops early when that run is already on the site.

Each source is a job of its own, built in the order of `config.SOURCES`: its probe, its index reads,
its gaps, then its release, built under <work>/stage/ and moved into the site once whole. A job that
fails at any step, a record missing that a frame needs among them, is skipped with an `::error::` line
and a `**Skipped:**` summary line, and leaves the site as it was; the other jobs go on. In the
workflow's form, a run that builds GFS defers every other source to the next run, with a `::notice::`
and a `**Deferred:**` line, and a job whose release is earlier than the restored site's is skipped. A
run fails only when no job was built. It writes `published=true|false` to $GITHUB_OUTPUT and each built
source's lines to $GITHUB_STEP_SUMMARY when those variables are set; a source that counts its reads per
copy, IFS, adds a `**Reads:**` line for each job that reached its probe, built or skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import check, config, gfs, grib, precip, site, source, summary, tiles, wxt


class RunRefused(ValueError):
    """A run in which every source was skipped, or a lost cache's run without GFS: nothing is published."""


class JobDropped(Exception):
    """A source's job dropped for a reason the pipeline states itself: its gaps, its probe or its read
    time limit."""


LOST_MANIFEST = "The site has no readable manifest.json: every source is built in this run."
ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT = 300


def clock() -> float:
    """Seconds, for each job's time and its read time limit; tests replace it."""
    return time.time()


def parse_cycle(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def valid_time(cycle: datetime, fhour: int) -> int:
    return int((cycle + timedelta(hours=fhour)).timestamp())


def write_atomically(path: Path, data: bytes) -> None:
    """Writes [path] through `<name>.part` in the same folder, so a killed run never leaves a short file
    under the final name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


class Fetcher:
    """Byte-range reads of one source's run with a cache under work/, so reruns and tests read local files:
    <work>/<source>/<cycle>/f<FFF>.idx for an index, <work>/<source>/<cycle>/f<FFF>/<record>.grib2 for a message.

    Every file is written atomically. A cached message is reused only when its size is its record's
    length, when that is known; a cached index that does not parse is read again once."""

    def __init__(self, src: config.Source, cycle: datetime, work: Path, workers: int = 8):
        self.source = src
        self.cycle = cycle
        self.root = work / src.info.name / f"{cycle:%Y%m%d%H}"
        self.workers = workers
        self.downloaded = 0
        self.deadline: float | None = None   # by clock(); a read started after it drops the job

    def idx_path(self, fhour: int) -> Path:
        return self.root / f"f{fhour:03d}.idx"

    def message_path(self, fhour: int, number: int) -> Path:
        return self.root / f"f{fhour:03d}" / f"{number}.grib2"

    def check_deadline(self) -> None:
        if self.deadline is not None and clock() > self.deadline:
            raise JobDropped(f"read time limit of {self.source.read_limit_seconds / 60:.0f} min passed; "
                             "the files read so far are kept")

    def _read_idx(self, fhour: int, path: Path) -> None:
        data = self.source.read(source.idx_url(self.source.info, self.cycle, fhour))
        self.downloaded += len(data)
        write_atomically(path, data)

    def idx(self, fhour: int) -> list:
        self.check_deadline()
        path = self.idx_path(fhour)
        if not path.exists():
            self._read_idx(fhour, path)
            return self.source.parse_idx(path.read_text(encoding="utf-8"))
        try:
            return self.source.parse_idx(path.read_text(encoding="utf-8"))
        except ValueError:   # a cached index that does not parse is read once more
            path.unlink()
            self._read_idx(fhour, path)
            return self.source.parse_idx(path.read_text(encoding="utf-8"))

    def _message(self, fhour: int, record) -> bytes:
        self.check_deadline()
        path = self.message_path(fhour, record.number)
        if path.exists():
            if record.end is None or path.stat().st_size == record.end - record.offset + 1:
                return path.read_bytes()
        data = self.source.read(source.file_url(self.source.info, self.cycle, fhour), (record.offset, record.end))
        self.downloaded += len(data)
        write_atomically(path, data)
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
    # Frame 0 has no interval of its own: it takes the first interval's rate, for every source. With a
    # single hour there is no interval, and frame 0 stays missing.
    if len(frames) > 1:
        frames[0] = (frames[0][0], frames[1][1].copy())
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
    """A source's entry in the manifest, in the key order the app and the site have always had; a source
    with the licence's elements adds them as `attribution_parts` right after `attribution`."""
    release = f"{cycle:%Y%m%d%H}"
    entry = {
        "release": release,
        "run_time": int(cycle.timestamp()),
        "path": f"{src.info.name}/{release}",
        "frames": [valid_time(cycle, h) for h in hours],
        "levels": [{"size": lv.size, "region_only": lv.region_only} for lv in config.LEVELS],
        "region": config.REGION,
        "fields": {f.name: {"units": f.units, "values": f.lut.header()} for f in fields},
        "attribution": src.attribution_for(cycle.year),
    }
    parts = src.attribution_parts_for(cycle.year)
    if parts is not None:
        entry["attribution_parts"] = parts
    return entry


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
    started: float | None = None   # by clock(), at its probe or its index reads

    @property
    def release(self) -> str:
        return f"{self.cycle:%Y%m%d%H}"

    @property
    def label(self) -> str:
        return f"{self.source.info.name} {self.release}"


def probe(src: config.Source) -> None:
    """Runs `python -m wxpipe.probe <name>` in a child process, so that an ecCodes crash ends the child
    only. A non-zero exit, a crash or a timeout raises JobDropped with the child's text."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH", "")) if p)
    name = src.info.name
    try:
        done = subprocess.run([sys.executable, "-m", "wxpipe.probe", name], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise JobDropped(f"probe of {name} timed out after {PROBE_TIMEOUT} s") from None
    if done.returncode != 0:
        text = " ".join(((done.stderr or "") + " " + (done.stdout or "")).split())
        raise JobDropped(f"probe of {name} failed, exit {done.returncode}: {text or 'no output'}")


def clear_other_cycles(src: config.Source, work: Path, cycle: datetime) -> list[str]:
    """Deletes every cycle folder under <work>/<name>/ but [cycle]'s, so the kept cache holds one run.
    Only folders named as a cycle, YYYYMMDDHH, are touched. Returns the deleted names."""
    root = work / src.info.name
    if not root.is_dir():
        return []
    keep = f"{cycle:%Y%m%d%H}"
    deleted = []
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and re.fullmatch(r"[0-9]{10}", folder.name) and folder.name != keep:
            shutil.rmtree(folder)
            deleted.append(folder.name)
    return deleted


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
        lost = not has_manifest
        if lost:
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
            on_copies = source.exists_on_copies(src.info, gfs.exists)
            try:
                cycle = source.latest_complete_cycle(src.info, datetime.now(timezone.utc), on_copies, hours[-1])
            finally:
                line = source.discovery_line(src.info, on_copies)
                if line is not None:
                    print(line)
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
        lost = False   # a local build into a new folder is not a lost cache

    workflow = args.cycles is not None
    on_site = site.read_manifest(out / "manifest.json") if workflow else None
    # Each job on its own, in the order of config.SOURCES: one source's failure skips that source only.
    order = {s.info.name: k for k, s in enumerate(config.SOURCES)}
    jobs.sort(key=lambda job: order.get(job.source.info.name, len(order)))
    built: dict = {}
    done: list[Job] = []
    lines: list[str] = []
    skipped: list[str] = []
    built_reads: list[str] = []   # the built jobs' **Reads:** lines, for a run refused after its builds
    for job in jobs:
        name = job.source.info.name
        if workflow and "gfs" in built:
            # GFS never waits on another source's reads: the next hourly run, in which GFS is not new,
            # builds the others alone.
            print(f"::notice::Deferred {job.label}: GFS is built in this run; tried in the next run")
            summary.append([f"**Deferred:** {job.label}: GFS is built in this run"])
            continue
        try:
            if workflow:
                _refuse_an_earlier_release(job, on_site)
            _prepare(job, args)
            built[name] = _build(job, out, args)
        except Exception as e:   # anything, a programming error included, so GFS still publishes
            skipped.append(_skip(job, e, args))
            continue
        done.append(job)
        lines += summary_lines(name, job.release, len(job.hours), len(job.fields),
                               job.report["seconds"], job.fetcher.downloaded)
        reads = _reads_line(job)
        if reads is not None:
            lines.append(f"- {reads}")   # a bullet of its own: a bare line would render inside the one above
            built_reads.append(reads)

    if not built:
        _fail(RunRefused("run refused, every source skipped: " + "; ".join(skipped)))
    if lost and "gfs" not in built:
        for reads in built_reads:   # the Build list is never written, so the built jobs' lines go here
            summary.append(["", reads])
        _fail(RunRefused("run refused: the site has no readable manifest.json and GFS was not built, so "
                         "no manifest is written and nothing is published; the live site stays as it was"))
    _finish(done, out, args, built, lines)
    return 0


def _refuse_an_earlier_release(job: Job, on_site: dict | None) -> None:
    """Drops a job whose release is earlier than the one the restored site already holds, by
    `check.is_new`'s rule; the same release is built again, as a rerun after a failed deploy."""
    name = job.source.info.name
    current = check.published(name, on_site)
    if current != job.release and not check.is_new(name, job.release, on_site):
        raise JobDropped(f"the site already holds the later release {current}")


def _prepare(job: Job, args) -> None:
    """A job's steps before its build: the cache's other cycles cleared, the probe, the index reads under
    the source's time limit, which then runs on through the build's message reads, and the gaps. Any
    failure raises. The source's read counts are zeroed first."""
    src = job.source
    if src.reads_reset is not None:
        src.reads_reset()
    if src.cache_one_cycle:
        clear_other_cycles(src, args.work, job.cycle)
    job.started = clock()
    if src.probes:
        probe(src)
    job.fetcher = Fetcher(src, job.cycle, args.work, src.workers or args.workers)
    if src.read_limit_seconds is not None:
        job.fetcher.deadline = job.started + src.read_limit_seconds
    with ThreadPoolExecutor(job.fetcher.workers) as pool:
        job.idx = dict(zip(job.hours, pool.map(job.fetcher.idx, job.hours)))
    missing = gaps(src, job.idx, job.hours, args.precip_method, job.fields)
    if missing:
        raise JobDropped("records missing: " + ", ".join(f"{name} at hour {h}" for name, h in missing))
    job.fetcher.check_deadline()


def _skip(job: Job, error: Exception, args) -> str:
    """Deletes a dropped job's stage folder and names it on the run's page and in its summary."""
    shutil.rmtree(_stage(job, args), ignore_errors=True)
    reason = str(error) if isinstance(error, JobDropped) else f"{type(error).__name__}: {error}"
    text = f"{job.label}: {reason}"
    print(f"::error::Skipped {text}")
    summary.append([f"**Skipped:** {text}"])
    reads = _reads_line(job)
    if reads is not None:
        summary.append(["", reads])   # a paragraph of its own after the **Skipped:** line
    return text


def _reads_line(job: Job) -> str | None:
    """The source's `**Reads:**` line of a job that reached `_prepare`, printed at once; None for a
    source without one or a job dropped before it. It never raises: a failure gives a line saying so."""
    report = job.source.reads_report
    if report is None or job.started is None:
        return None
    try:
        line = report(job.source.info.name, job.release)
    except Exception as e:
        line = f"**Reads:** {job.label}: not available: {type(e).__name__}: {e}"
    print(line)
    return line


def _fail(error: Exception) -> None:
    """Names a refused run on the run's page and in its summary, then raises it again."""
    message = str(error)
    print(f"::error::{message}")
    summary.append([f"**Refused:** {message}"])
    raise error


def _stage(job: Job, args) -> Path:
    return args.work / "stage" / job.source.info.name / job.release


def _build(job: Job, out: Path, args) -> dict:
    """Builds a job's release under <work>/stage/<name>/<release>/, then moves it whole into the site,
    replacing a folder of the same release. Returns the source's manifest entry."""
    name, release = job.source.info.name, job.release
    stage = _stage(job, args)
    if stage.exists():   # left by a killed run
        shutil.rmtree(stage)
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
        sizes = write_field(stage, job.source, field, release, frames)
        report["fields"][field.name] = {"seconds": round(time.time() - t0, 1), "levels": sizes}
        print(f"{field.name}: {sum(s['bytes'] for s in sizes.values()) / 1e6:.1f} MB in {time.time() - t0:.0f} s")
    entry = release_entry(job.source, job.cycle, job.hours, job.fields)
    # The job's time runs from its probe or index reads to its move; the move is the last step that can
    # fail, so a skipped job never leaves its release in the site.
    report["downloaded_bytes"] = job.fetcher.downloaded
    report["seconds"] = round(clock() - job.started, 1)
    args.work.mkdir(parents=True, exist_ok=True)
    (args.work / f"report-{name}-{release}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    _move_in(stage, out / name / release, args.work / "stage" / name / f"{release}.old")
    job.report = report
    return entry


def _move_in(stage: Path, release_dir: Path, old: Path) -> None:
    """Moves a built release into the site. A folder of the same release is set aside at [old] first and
    deleted only once the new one is in place; if the move fails, any partial folder is removed and the
    old one put back."""
    release_dir.parent.mkdir(parents=True, exist_ok=True)
    if old.exists():
        shutil.rmtree(old)
    if release_dir.exists():
        old.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(release_dir), str(old))
    try:
        shutil.move(str(stage), str(release_dir))
    except BaseException:
        if release_dir.exists():
            shutil.rmtree(release_dir, ignore_errors=True)
        if old.exists():
            shutil.move(str(old), str(release_dir))
        raise
    if old.exists():
        shutil.rmtree(old)


def _finish(jobs: list, out: Path, args, built: dict, lines: list[str]) -> None:
    """Writes the manifest once, prunes, and reports the built jobs."""
    # The manifest is written once for every source. A source's release already on the site becomes its
    # "previous", so a phone holding the old manifest can still read its tiles while the new one
    # propagates; a rerun of the same release keeps the previous one. Older releases are deleted. A
    # skipped source keeps its entries and its folders.
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
