# wxpipe design

wxpipe turns each NOAA GFS run and each ECMWF IFS run into static map tiles for the weatherstation app and publishes them on GitHub Pages from a scheduled GitHub Actions workflow. There is no server. Blueprint references: `rea-analysis/notes/REBUILD_BLUEPRINT.md` §2.3, §6.2 and §11.

## Input

- NOAA GFS 0.25° from the NOAA Open Data bucket on AWS (`noaa-gfs-bdp-pds`), public domain. Only the needed records are read, by HTTP byte range from the `.idx` offsets: about 5 MB per forecast hour instead of 530 MB.
- ECMWF IFS 0.25° from three copies of ECMWF's open data, under CC BY 4.0, tried in this order: ECMWF's data portal, `https://data.ecmwf.int/forecasts`; the CloudFront door of ECMWF's AWS bucket, `https://d2zvc0wgha4k2l.cloudfront.net`; the AWS bucket by its S3 host, `https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com` (`ecmwf-forecasts`, eu-central-1). The order is from a probe of the three on 2026-11-24, below: the portal refused none of its requests, and the bucket's S3 host is the one address seen to refuse GitHub's runner. Azure's copy is left out, since it refuses a read without a short-lived token, and Google Cloud's, since no ECMWF page gives its address. Below each copy's ROOT the key path is the same, `<yyyymmdd>/<HH>z/ifs/0p25/oper/<yyyymmddHH0000>-<step>h-oper-fc.grib2`, with a `.index` beside each file: one JSON object per line with `param`, `levtype`, `step`, `_offset` and `_length`. Only the six needed records are read, by HTTP byte range, with the standard library as for GFS: about 3.2 MB per step, about 270 MB for a run's 85 steps, and 85 index files of about 42 KB each. The `ecmwf-opendata` client is not used, since it adds a package for what two byte-range reads do.
- A run is used once its last hour is published: GFS's f384, IFS's step 360. The workflow checks hourly and builds a source only when its newest complete run is later than the one on the site.

## Sources

The pipeline builds several sources into one site: GFS (`gfs`) and ECMWF IFS (`ifs`); GFS-Wave (`gfswave`) is planned. Each source is a description in two parts, so a change of a source's layout is one edit of values, not of code:

- **The standard-library part,** `SourceInfo` in `wxpipe/source.py`: the name, the last forecast hour, the cycle step, the first copy's ROOT, and the copies, the file name pattern with the file's and the index's suffixes, and how many cycles to look back. With them come the URL functions and the discovery of the newest complete run. `wxpipe.check` imports only this part and `wxpipe/gfs.py`, so it runs on the runner's own Python before any install; a test imports every standard-library module, `wxpipe/ecmwf.py` among them, in a subprocess with numpy and eccodes blocked.
- **The full part,** `Source` in `wxpipe/config.py`: its `SourceInfo`, the grid as delivered (columns, rows, first point, increments, scanning flags), the forecast hours, the fields with their tables, the fetch functions (index parser, record pickers, byte-range reader), the attribution and its parts, and how it is read: its reads at a time, its read time limit, whether its download cache keeps one run, and its probe messages.

| | GFS | IFS |
|---|---|---|
| Copies | one, the NOAA bucket | three, tried in order: the portal, CloudFront, the AWS bucket |
| Runs | 00, 06, 12 and 18 UTC, to 384 h | 00 and 12 UTC, to 360 h; the 06 and 18 runs end at 90 or 144 h and are not used |
| Frames | 209: hourly to 120 h, then every 3 h | 85: every 3 h to 144 h, then every 6 h to 360 h |
| Discovery | 6 cycles back, by f384's `.idx` | 4 cycles back, two days, by step 360's `.index` |
| Delivered grid | 1440 × 721 from 90°N and 0°E | 1440 × 721 from 90°N and 180°E, rolled by 720 columns |
| Reads at a time | `--workers`, 8 by default | 2 |
| Read time limit | none | 45 minutes from the job's start, over every read |
| In a run that builds GFS | built first | deferred to the next run |

Every source is put on the shared output grid, 1440 × 721 at 0.25°, row 0 at 90°N and column 0 at 0°E, inside `grib.decode`, so the tiles and the app's domain are the same for all sources and `wxpipe/tiles.py` takes no source. Decode first checks the delivered grid against the source's description (`grib.check_grid`) and refuses any other, then rolls its columns by where its first column lies (`grib.roll_to_shared`: `round((lon0 - 0) / 0.25) mod 1440` columns, 720 for IFS, none for GFS), then refuses any shape but the shared one. The roll was checked on the sample before any tile code: at step 0 of 2026-11-23 12 UTC, ecCodes' nearest point to Riyadh, 24.75°N 46.75°E, holds 302.4167 K, and the rolled array holds the same at row 261, column 187; at 35°N 140°W ecCodes and the rolled array both hold 291.6667 K at row 220, column 880.

### Each source fails on its own

Each source of a run is a job of its own, run in the order of `config.SOURCES`, GFS first. A job's steps are: for IFS, the deletion of every other cycle's folder under `work/ifs/` and the probe (below); its index reads; its gaps; its build. `run.gaps` lists every record a frame needs that is missing: an instant field's record at an hour, or the accumulations a precipitation interval needs; without it the build would write frames of missing bytes. Extra, reordered or repeated records are accepted, the first copy of a repeated one being used.

A job that fails at any step, by a gap, a failed probe, its read time limit, a read that still fails after its retries, a decode error, a write or a move, any exception included, is skipped: its stage folder is deleted, an `::error::` line and a `**Skipped:** <source> <release>: <reason>` line in the run's summary name it, and the next job goes on. A skipped source keeps its entries in the manifest and its folders on the site, so the GFS the app reads keeps updating whatever ECMWF does. A run fails, with an error naming each source and its reason and no `$GITHUB_OUTPUT`, only when no job was built; the site then stays as it was.

**A run that builds GFS defers the others.** In the workflow's form, once GFS's job has built its release, every later job of the run is not run: a `::notice::Deferred <source> <release>: GFS is built in this run; tried in the next run` line and a `**Deferred:** <source> <release>: GFS is built in this run` summary line name it. A deferred source is not a skipped one: the next hourly run, in which GFS is not new, builds it alone. So GFS never waits on ECMWF's reads in one run, which with the copies' SlowDown answers can take up to the read limit and more. When GFS's job is skipped, or GFS is not in the run, the other jobs run as before. The local form (`--source`) never defers.

**Never an earlier release.** In the workflow's form, a job whose release is earlier than the one the restored site's own manifest names for its source, by the check's rule below, is skipped with that reason; the same release is built again, as after a failed deploy.

**The stage folder.** A job builds its release under `<work>/stage/<source>/<release>/`, a stale one from a killed run removed first, and moves it to `<site>/<source>/<release>/` only once every field is written. A folder of the same release, from a rerun, is first set aside as `<work>/stage/<source>/<release>.old` and deleted only once the new one is in place; if the move fails, any partial folder is removed and the old one put back. So a skipped source never leaves a part-built folder in the site, and a rerun of the current release never leaves the manifest pointing at deleted tiles.

**A lost cache.** When the restored site has no readable manifest and GFS was not built in the run, the run is refused and writes no manifest, so the live site stays as it was rather than being published without GFS. With the deferral, a lost-cache run that builds GFS defers `ifs` and publishes GFS alone; one whose GFS was skipped reads `ifs` into its cache and then refuses, publishing nothing.

**Accepted risks.** While `ifs` keeps failing, it never reaches the live manifest, so the check offers it again every hour, and an hour in which `ifs` is the only new source ends in a red run. Its downloads are kept between runs (below), so a later try reads only what is missing. The runs list is read after a push for repeated red runs. The gate never drops `ifs`'s current release, so GFS's room under the limit rests on the measured sizes.

### ECMWF's reads

The AWS bucket answers "503 SlowDown" often. Fetching the sample of run 2026112312 from it took 270 requests one at a time for 140 distinct ones, 126 of them answered "503 SlowDown"; every one was answered at last, one index file only at its 17th attempt, about 15 minutes after its first. On GitHub's runner it was worse: the first IFS build, run 45291639100, failed after 15 minutes, one read answered "503 SlowDown" at all its ten attempts, and the Actions cache kept 120 KB, about three index files of 85. So IFS is read from three copies (Input, above). A probe of run 2026112312 from a computer outside GitHub on 2026-11-24, one request at a time, compared each copy's files with the sample: the portal answered all 10 index reads and served step 0's index, the `2t` message and ten index files, 12 of 12, the sample's bytes; CloudFront answered 6 of 10, refusing 4 with 503, and served 8 of 8 the sample's bytes, its `2t` message at the second attempt; the bucket answered 4 of 10. So 20 comparisons on two copies, every one the same bytes. The runner was not probed.

**Each read, on one copy (`ecmwf.http_get`).** A 429 or 5xx answer, a timeout or a connection error is tried again, up to 4 attempts, waiting 5, 10 and 20 seconds, or the `Retry-After` header's seconds when given, at most 60; then the read goes on to the next copy. A 404, or any other 4xx, goes on to the next copy at once, with no wait. An answer is accepted on a copy only when it passes these checks; one that does not counts as an error on that copy and goes on to the next at once:
- a byte range of a message: status 206, exactly the asked length, the four bytes `GRIB` first, the edition byte at offset 7 equal to 2, the length field at offsets 8 to 15, big-endian, equal to the body's length, and the four bytes `7777` last;
- a whole read of an `.index`: UTF-8 text from which the index parser gives at least one record;
- any other whole read: any answer.

So one run's files may come from several copies, and the cache of files read earlier from the bucket stays usable. A valid message of another field at the same offset and length would pass these checks; only a parameter check in decode would catch it, and there is none.

**The copies (`ecmwf.read`).** A job's reads start on the first copy. A read tries the current copy, then each other copy in list order from there, wrapping round to the earlier ones last; a copy is passed over only on an `OSError` or an `http.client.HTTPException` from its read, and anything else stops the read at once. A read that answers on its first copy never moves the current copy; only a read that passed over a copy can move it: the copy that answered becomes the current copy for every later read of the job, from both workers, with one log line, `ifs reads: moved from <copy> to <copy>: <error>`, the error being the last one this read met on the copy it leaves. Once every copy has failed, the read raises one `OSError` naming each copy and its last error in the order tried, and the job is skipped by the rule above; since the worker pool cancels the queued reads at the first failure, a job refused everywhere ends at its first such read, with fast refusals after 2 or 3 reads, 24 to 36 requests and about 2 to 4 minutes. The known limit: after a move there is no return to the first copy until a read fails on every later one; the `**Reads:**` line shows that case. IFS's jobs read 2 files at a time.

**The `**Reads:**` line.** For each IFS job that reached its start, built or skipped, the run's summary holds one line, printed at once to the job's log: `**Reads:** ifs <release>: <copy> <n> requests, <n> refused, <n> missing, <n> errors, <n> files, <x.x> MB; <copy> ...`, every copy named in order, those with no request too, a copy being its ROOT without `https://`. Requests counts every attempt; refused, the 429 and 5xx answers; missing, the 404s; errors, any other 4xx, a timeout, a connection error, a wrong status or length or a failed check; files, the reads accepted, with their megabytes of 1,000,000 bytes; so requests are refused, missing, errors and files together. The counts are zeroed at each IFS job's start. A built job's line is a bullet of the Build list right after the job's own; a skipped job's comes right after its `**Skipped:**` line. A line that cannot be built says `not available` with its error and never fails the job. GFS writes no such line.

**The read time limit.** An IFS job has one limit of 45 minutes from its start over its probe, its index reads and its message reads, each read checked before it starts; once it is passed, the next read drops the job by the rule above, its stage folder deleted and every file already read kept. A read already in progress is not cut, so its own attempts can overrun the limit by up to 825 s, about 14 minutes, three copies of four attempts timing out at 60 s with their waits of 35 s; or by up to about 21 minutes when every wait is a `Retry-After` of 60 s.

**The download cache.** Every index and message is written to `<name>.part` in its folder and then renamed onto its name, so a killed run never leaves a short file under the final name. A cached message is reused only when its size is its record's length, when the index gives one; otherwise it is read again. A cached index that does not parse is deleted and read once more.

The workflow keeps `work/ifs` in the Actions cache. It is restored before the build only in a run that builds `ifs` and not GFS, since a run that builds GFS defers `ifs`; it is saved after the build, also after a failed build, whenever it holds a file, and a failed save never stops the gate or the upload. Both use the key `ifs-work-<run id>-<run attempt>`, restored from the newest `ifs-work-` entry. When GFS is in a run's build list but its job is skipped, `ifs` then builds in that run without its cache restored, reading from ECMWF's copies again, and its files are saved for later. At an IFS job's start, `wxpipe.run` deletes every cycle folder under `work/ifs/` but the one it builds, so the cache holds one run.

### The probe

The runner's ecCodes comes from the `eccodeslib` wheel, and IFS's messages are CCSDS-packed, which GFS's are not. Before an IFS job reads its index files, `wxpipe.run` runs `python -m wxpipe.probe ifs` in a child process, with a timeout of 300 s: it decodes two committed CCSDS messages and requires for each the shared grid's shape, every value finite, and the minimum, maximum and mean within 0.01 of the local decode's figures, in the message's own units:

| Message | What | Bits | Minimum | Maximum | Mean |
|---|---|---|---|---|---|
| `wxpipe/samples/ifs-2t-ccsds.grib2`, 314,997 bytes | `2t` of 2026-11-22 00 UTC, step 0 | 9 | 221.997971 K | 315.997971 K | 278.805962 K |
| `wxpipe/samples/ifs-msl-ccsds.grib2`, 511,939 bytes | `msl` of 2026-11-23 12 UTC, step 0 | 12 | 95764.125000 Pa | 104952.125000 Pa | 100852.455598 Pa |

A failure, a timeout or a crash of the child skips `ifs` only. Every message of the sample is `grid_ccsds` with no bitmap, at 0 to 12 bits per value; the probe covers the 9-bit and the 12-bit cases. No test that runs on the runner decodes an IFS message: the probe test skips when `GITHUB_ACTIONS` is `true`, and the real-sample tests skip without the sample.

### The check builds only a newer release

`wxpipe.check` counts a source as new only when its newest complete release is later than the live one, compared as `YYYYMMDDHH` text. A discovery falls back to an older cycle only when the newest run's last index answers 403 or 404 for a moment; a SlowDown answer is retried and, when every copy of the source gives one, counts as no complete run that hour; this rule keeps any fallback from replacing a newer release. An unreadable live manifest, a source absent from it, or a live release that is not a 10-digit string counts as new. The discovery's existence test, `gfs.exists`, answers false for 404 and any other 4xx but 429, and tries a 429 or 5xx answer, a timeout or a connection error again after 1, 2 and 4 seconds before raising. For a source of several copies, IFS, the discovery asks the copies in order and the first definite answer decides (`source.exists_on_copies`): a copy answering true or false decides, a copy whose test raises an `OSError` or an `http.client.HTTPException` is passed over, any other exception is raised at once, and the first error is raised only when every copy raised. Each copy passed over is printed in one line, `ifs discovery: <copy> passed over: <error>`, and after the source's discovery one more line names the copy whose answer decided the newest cycle and that answer, `ifs discovery: the newest cycle decided by <copy>: true` or `false`, or says `every copy raised`; GFS prints neither. When every copy of a source hangs, the check step can take about 25 minutes, against about 8.5 with one copy, and GFS's build waits for it in that run. So in the hours before a new run, the portal's definite "not there" lets the discovery look at the older cycle, and a throttled copy cannot fail it; a wrong "false" is harmless, since a fallback builds only a release later than the live one. GFS's one copy answers as `gfs.exists` alone. The check prints one source's error as a `::warning::` line with its name and counts that source as having no complete run, and every other source goes on.

No field is built on GFS's time-averaged precipitation-type flags.

## Frames

Every forecast hour each source provides, the longest forecast available, as the user asked: GFS hourly from 0 to 120 h, then every 3 h to 384 h, 209 frames over 16 days; IFS every 3 h to 144 h, then every 6 h to 360 h, 85 frames over 15 days. Frame times are absolute epoch seconds.

## Fields and quantisation

Values are stored as one byte per grid cell through a piecewise-linear table (breakpoints in each file header). Byte 255 means missing. The tables put most of the 255 levels where values usually fall:

| Field | GFS record | IFS record, `sfc` (its units: conversion) | Units | Table (byte: value) | Step in the main range |
|---|---|---|---|---|---|
| `temperature.2m` | TMP 2 m | `2t` (K: -273.15) | °C | 0: -70, 20: -30, 230: 50, 254: 60 | 0.38 °C |
| `precipitation.sfc` | APCP surface (rate, below) | `tp` (m since the start: × 1000, rate below) | mm/h | 0: 0, 60: 1.2, 140: 8, 200: 30, 254: 150 | 0.02 mm/h below 1.2 |
| `wind_u.10m`, `wind_v.10m` | UGRD, VGRD 10 m | `10u`, `10v` (m/s) | m/s | 0: -80, 20: -40, 234: 40, 254: 80 | 0.37 m/s |
| `pressure.msl` | PRMSL | `msl` (Pa: × 0.01) | hPa | 0: 870, 20: 950, 234: 1050, 254: 1090 | 0.47 hPa |
| `cloud.total` | TCDC entire atmosphere | `tcc` (0 to 1: × 100) | % | 0: 0, 254: 100 | 0.39 % |

Both sources use the same names, units and tables, so the app's spectra and legends apply to each. IFS's parameter names are ecmwf.int's; their units were read from the sample's messages, since no ECMWF page states them. An IFS record is found in the index by `param:levtype`, such as `2t:sfc`, and by its step, the hour itself; no code relies on a record's line, which changes from run to run.

Wind is stored as signed u and v components, not speed and direction. Components interpolate correctly between frames, feed streamlines directly, and leave no from/to convention to settle, which closes blueprint question 6. Point mode (Open-Meteo) keeps speed plus a "from" direction.

The app must treat byte 255 as missing (blueprint B2): its LUT maps 255 to "discard" in M3.

### Precipitation rate

GFS stores APCP two ways: the total since the run started, and a bucket that restarts every 6 hours. The rate over (previous frame, frame] uses the bucket when it covers the previous frame, and the difference of two buckets otherwise (`wxpipe/precip.py`). Frame 0, the run's first hour, has no interval of its own, so it takes the first interval's rate, the same values as frame 1, for every source; only a release of a single hour, which has no interval, leaves frame 0 missing. This was measured on the 2026-11-07 12z run:

| Interval | Negative cells: bucket / since-start | Largest difference between methods |
|---|---|---|
| 6 to 7 h | 0 / 0 | 0.125 mm/h, 133,105 cells over 0.05 |
| 120 to 123 h | 0 / 1 | 0.04 mm/h |
| 381 to 384 h | 0 / 3 | 0.04 mm/h |

Differencing the large since-start totals adds packing noise of up to 0.125 mm/h and occasional negative rates, so the bucket method is the default. Negative results are clamped to zero.

IFS stores `tp` only as the total since the run's start, in metres, its index step the hour itself; it is keyed as the window (0, hour), so both methods take the difference of two totals over each 3 h or 6 h interval, divided by the interval's hours. Step 0's `tp`, a constant message, is never read. IFS's tp is packed at 12 bits per message, so the difference of two totals carries up to one packing step of noise in either direction: negatives, -0.061 mm over 3 to 6 h on the sample, are clamped to zero and counted in the build report, and their positive counterparts remain, about one table step, 0.02 mm/h, at the low end.

## Tiles

| Level | Coverage | Tiles | Samples per tile |
|---|---|---|---|
| `60x60/1` (0.25°) | the user's region, 15°E to 75°E and 0° to 45°N | 12 | 61 × 61 |
| `120x120/2` (0.5°) | world | 72 | 61 × 61 |
| `240x240/4` (1°) | world | 18 | 61 × 61, or 61 × 31 in the bottom row |
| `480x480/8` (2°) | world | 6 | 61 × 61, or 61 × 31 in the bottom row |

Tile (i, j) spans columns i·W to i·W + W and rows j·H to j·H + H, ends included, so neighbours share an edge. Column 1440 is column 0 again. The rules match `RegularLatLonDomain` in the app (`core:config`).

## Files

- `manifest.json`: for each source, its current release under `sources` and its previous one under `previous`, with the frame times, levels, region, fields and their tables, and its attribution. `previous` is left out when no source has one.
- `<source>/<release>/<field>/<WxH-step>/<i>-<j>.wxt`, such as `gfs/2026110712/...` or `ifs/2026112312/...`: one WXT1 file per tile and field, holding every frame (`wxpipe/wxt.py`). Each frame is a separate zlib-format stream at an offset listed in the header, so the app can read the whole file or single frames with HTTP Range requests (GitHub Pages answers 206). A release of either source has 648 files.
- The frames are written by libdeflate at level 12, through the `deflate` package. Its zlib-format streams inflate with any zlib inflater, so the header's `"format": "zlib"` and the app are unchanged. On the 9-frame release of the 2026-11-07 12z run, they are 3.52% smaller than zlib level 9, and take 3.80 times as long to encode. The encode time on the runner is not measured: scaled from this computer, about 80 s for 209 frames.
- `status.json`: written by the size gate, below.

### The manifest merge and pruning

A run builds each of its sources into the same site folder, then writes the manifest once (`wxpipe/site.py`), for the sources built; a skipped source's entries stay as they were. Each built source's entry replaces its old one, and its old current release becomes its previous one, so a phone holding the old manifest can still read its tiles while the new one propagates. A rerun of the same release, such as a run built again after a failed deploy, keeps the source's previous release as it was. Every source not built in the run keeps its entries. Then each built source's folders that are neither its current nor its previous release are deleted, under `<source>/` only; another source's folders are never touched.

### The attribution

Each release's `attribution` is one non-empty string, since the app requires one. GFS's is "NOAA NCEP Global Forecast System (GFS). Public domain." IFS's is "ECMWF IFS open data." followed, joined by single spaces, by the six elements of ECMWF's licence page in seven keys, which also go, in this order, into the `ifs` entry's object `attribution_parts`, right after `attribution`; the app ignores unknown keys until it shows them:

| Key | Value |
|---|---|
| `licence` | This data is published under a Creative Commons Attribution 4.0 International (CC BY 4.0). |
| `licence_url` | https://creativecommons.org/licenses/by/4.0/ |
| `copyright` | © <the run's year> European Centre for Medium-Range Weather Forecasts (ECMWF) |
| `source` | Source www.ecmwf.int |
| `credit` | Based on data and products of the European Centre for Medium-Range Weather Forecasts (ECMWF). |
| `liability` | ECMWF does not accept any liability whatsoever for any error or omission in the data, their availability, or for any loss or damage arising from their use. |
| `modified` | wxpipe's own statement: Modified: the columns rolled to start at 0°E, units converted, accumulated precipitation turned into rates, values quantised to 8-bit tables and cut into map tiles. |

The licence page's verbatim text is read again before the app puts it on screen.

## Storage budget (measured)

Measured on 9 frames of the 2026-11-07 12z run and scaled to 209 frames:

| Level | MB per release |
|---|---|
| 0.25° region | 25 |
| 0.5° world | 149 |
| 1° world | 46 |
| 2° world | 13 |
| **All 6 fields** | **233** |

Two releases take 466 MB of the 1 GB GitHub Pages limit. Each further field costs about 40 MB per release, or 80 MB with two releases. Ways past the limit: a second public repository with its own 1 GB site, or a smaller 0.5° area. `wxpipe.budget` measures one source's current release, `--source gfs` by default; it is a local estimate tool, not the gate.

IFS, measured on the 9-step sample of the 2026-11-23 12z run: 648 tiles, 9,913,720 bytes; one release scaled to 85 frames is about 92.3 MB. The live site of two GFS releases held 453,123,135 bytes on 2026-11-23; with two IFS releases it comes to about 637.7 MB, under the gate's 900 MB with about 262 MB to spare, and to about 545.4 MB after the gate's first candidate, which drops `ifs`'s previous release.

## The size gate

`wxpipe.gate` runs between the build and the upload. It sums every file of the site folder, the tiles and `manifest.json`, and compares the total with 900,000,000 bytes, the plan's 900 MB. `status.json` is outside the limit, since the gate writes it after the sum. Above the limit it degrades before it fails, trying these candidates in order on the projected total before any file is deleted:

1. drop `ifs`'s previous release;
2. and drop `gfswave`'s previous release;
3. or, in place of 2: drop `gfswave`'s current release and publish its previous one as current, if it has one.

The first candidate at or under the limit is applied: its folders are deleted and the manifest is rewritten, an emptied `previous` dropped. GFS is never dropped. If no candidate fits, nothing is deleted, the gate exits 1, the workflow uploads nothing, and the live site stays as it was. When nothing is dropped the manifest is not rewritten.

In every case the gate writes `status.json` beside the manifest: `generated_at`, `limit_bytes`, `total_bytes_before`, `total_bytes` after, `dropped` (each with `source`, `release` and `role`, `previous` or `current`) and `sources` (each source's current release after the gate). So a check reads the site's real total and what was dropped from the live site, like the manifest. The run's summary says the same.

## Workflow

`.github/workflows/publish.yml` runs hourly at minute 17.

1. `wxpipe.check` runs first on the runner's own Python, using only the standard library. For each source it finds the newest complete run and compares it with the live `manifest.json`, printing one line per source. It writes `new` (true if any source is new), `build` (the new sources) and `all` (every source with a complete run), the last two as `<source>:<cycle>` lists. An unreadable live manifest counts every source as new. When nothing is new, the job ends there, with no install and no cache restore. The manifest URL is built from `GITHUB_REPOSITORY_OWNER` and `GITHUB_REPOSITORY`, because the payload of the schedule event has no repository object.
2. When there is a new run, the workflow installs the pinned packages. On Linux the ecCodes library comes from `eccodeslib`, which findlibs locates inside the package. The workflow checks that the library loads, then runs the unit tests and restores the previous site from the Actions cache, and ECMWF's downloads (`work/ifs`) when the run builds `ifs` and not GFS.
3. It builds the new sources with `--cycles` and `--all-cycles`, every forecast hour and field of each, keeping each source's new release and its previous one; a source that fails is skipped and the others are built, and a run that builds GFS defers the others (above). Then `work/ifs` is saved in the Actions cache, also after a failed build, whenever it holds a file; a failed save does not stop the run.
   **A lost cache:** when the restored site has no readable `manifest.json`, the Actions cache having been evicted, every source with a complete run is built in that run, so the site is never published without a source; a run in which GFS is then not built is refused and publishes nothing, and one that builds GFS defers `ifs` to the next run. Every previous release is gone after a lost cache, until later runs build new ones; the app falls back after a 404 and keeps what it has cached. `wxpipe.run` writes a line to the run's summary when it finds no readable `manifest.json`. A GFS run takes 2 to 4 minutes, an IFS run longer, its reads limited to 45 minutes, a read in progress overrunning it by up to 825 s, about 14 minutes, or about 21 minutes when every wait is a `Retry-After` of 60 s; the build job's timeout is 180 minutes in every run, since a job's timeout cannot read a step's output, so only a run that hangs reaches it.
4. The size gate runs, then the site is uploaded and saved in the Actions cache for the next run.
5. The deploy job deploys it with `actions/deploy-pages`. If the first deploy fails, a timeout being the expected case, it is retried once; the job fails only when the retry fails too. Its last step writes the job's minutes, from its first step to the summary step, its attempts and its outcome into the run's summary (`wxpipe/summary.py`), with a warning from 6 minutes. That step never fails the job. `status.json` cannot hold the deploy's minutes, since the upload ends the build job before the deploy job starts.
6. A heartbeat commit at most once a week keeps the schedule enabled, because GitHub disables schedules after 60 days without activity. The heartbeat job runs when the deploy job succeeds, and is skipped when the deploy job fails or is skipped.

The run's summary also holds, from `wxpipe.run`, each built source's release, frames, fields, minutes and downloaded MB, each skipped source's `**Skipped:**` line, each IFS job's `**Reads:**` line and each deferred source's `**Deferred:**` line, and from the gate its totals and what it dropped. A source's minutes are its own job's, from the start of its probe or its index reads to the move of its release into the site. The install, test, cache and upload steps keep the times the run's page already shows.

On the 9-frame sample, a local run took 25 s, including the download. A full GFS run fetches about 1.1 GB from AWS on the runner, a full IFS run about 270 MB from ECMWF's copies.
