# wxpipe design

wxpipe turns each NOAA GFS run, each NOAA GFS-Wave run and each ECMWF IFS run into static map tiles for the weatherstation app and publishes them on GitHub Pages from a scheduled GitHub Actions workflow. There is no server. Blueprint references: `rea-analysis/notes/REBUILD_BLUEPRINT.md` §2.3, §6.2 and §11.

## Input

- NOAA GFS 0.25° from the NOAA Open Data bucket on AWS (`noaa-gfs-bdp-pds`), public domain. Only the needed records are read, by HTTP byte range from the `.idx` offsets: about 5 MB per forecast hour instead of 530 MB. The gust's record, `GUST:surface`, record 14 in every index of the sample run 2026110712, adds 597,001 to 612,357 bytes an hour, 5,444,322 bytes over the nine hours 0, 1, 6, 7, 120, 123, 126, 381 and 384, so about 126 MB more for a run's 209 hours, an estimate from the nine.
- NOAA GFS-Wave 0.25°, the global wave model of the Global Forecast System, from the same bucket, public domain: `gfs.<yyyymmdd>/<HH>/wave/gridded/gfswave.t<HH>z.global.0p25.f<FFF>.grib2`, with a `.idx` of the same form as GFS's beside each file. Of each file's 19 records, about 11.6 MB, only two are read by byte range, `HTSGW` and `PERPW`, records 5 and 6 at every hour of the sample run 2026112412: about 0.92 MB per forecast hour, about 193 MB for a run's 209 hours, and 209 index files of about 1 KB each.
- ECMWF IFS 0.25° from three copies of ECMWF's open data, under CC BY 4.0, tried in this order: ECMWF's data portal, `https://data.ecmwf.int/forecasts`; the CloudFront door of ECMWF's AWS bucket, `https://d2zvc0wgha4k2l.cloudfront.net`; the AWS bucket by its S3 host, `https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com` (`ecmwf-forecasts`, eu-central-1). The order is from a probe of the three on 2026-11-24, below: the portal refused none of its requests, and the bucket's S3 host is the one address seen to refuse GitHub's runner. Azure's copy is left out, since it refuses a read without a short-lived token, and Google Cloud's, since no ECMWF page gives its address. Below each copy's ROOT the key path is the same, `<yyyymmdd>/<HH>z/ifs/0p25/oper/<yyyymmddHH0000>-<step>h-oper-fc.grib2`, with a `.index` beside each file: one JSON object per line with `param`, `levtype`, `step`, `_offset` and `_length`. Only the seven needed records are read, by HTTP byte range, with the standard library as for GFS: for the six fields before the gust about 3.2 MB per step, about 270 MB for a run's 85 steps, and 85 index files of about 42 KB each; the gust's messages add 53,431,705 bytes over the 85 steps of the sample run 2026112312, about 53 MB, step 0's never being read. The `ecmwf-opendata` client is not used, since it adds a package for what two byte-range reads do.
- A run is used once its last hour is published: GFS's and GFS-Wave's f384, IFS's step 360. The workflow checks hourly and builds a source only when its newest complete run is later than the one on the site and not held back by the size gate.

## Sources

The pipeline builds several sources into one site: GFS (`gfs`), GFS-Wave (`gfswave`) and ECMWF IFS (`ifs`), in that order. Each source is a description in two parts, so a change of a source's layout is one edit of values, not of code:

- **The standard-library part,** `SourceInfo` in `wxpipe/source.py`: the name, the last forecast hour, the cycle step, the first copy's ROOT, and the copies, the file name pattern with the file's and the index's suffixes, and how many cycles to look back. With them come the URL functions and the discovery of the newest complete run. `wxpipe.check` imports only this part and `wxpipe/gfs.py`, so it runs on the runner's own Python before any install; a test imports every standard-library module, `wxpipe/ecmwf.py` among them, in a subprocess with numpy and eccodes blocked.
- **The full part,** `Source` in `wxpipe/config.py`: its `SourceInfo`, the grid as delivered (columns, rows, first point, increments, scanning flags), the forecast hours, the fields with their tables, the fetch functions (index parser, record pickers, byte-range reader), the attribution and its parts, and how it is read: its reads at a time, its read time limit, whether its download cache keeps one run, its probe messages, whether a run that builds GFS defers it (`waits_for_gfs`), and whether its job runs in a child process in the workflow's form (`child_seconds`).

| | GFS | GFS-Wave | IFS |
|---|---|---|---|
| Copies | one, the NOAA bucket | one, the NOAA bucket | three, tried in order: the portal, CloudFront, the AWS bucket |
| Runs | 00, 06, 12 and 18 UTC, to 384 h | 00, 06, 12 and 18 UTC, to 384 h | 00 and 12 UTC, to 360 h; the 06 and 18 runs end at 90 or 144 h and are not used |
| Frames | 209: hourly to 120 h, then every 3 h | 209, GFS's own hours | 85: every 3 h to 144 h, then every 6 h to 360 h |
| Discovery | 6 cycles back, by f384's `.idx` | 6 cycles back, by f384's `.idx` | 4 cycles back, two days, by step 360's `.index` |
| Delivered grid | 1440 × 721 from 90°N and 0°E | 1440 × 721 from 90°N and 0°E, land and ice missing | 1440 × 721 from 90°N and 180°E, rolled by 720 columns |
| Reads at a time | `--workers`, 8 by default | `--workers` | 2 |
| Each message | taken as read | checked whole (below) | checked whole (below) |
| Read time limit | none | 15 minutes from the job's start, over every read | 45 minutes from the job's start, over every read |
| Probe | none | one JPEG 2000 message | two CCSDS messages |
| Process, in the workflow's form | the run's own | a child process, at most 20 minutes | the run's own |
| In a run that builds GFS | built first | built after GFS, in its child, and published with it | deferred to the next run |

Every source is put on the shared output grid, 1440 × 721 at 0.25°, row 0 at 90°N and column 0 at 0°E, inside `grib.decode`, so the tiles and the app's domain are the same for all sources and `wxpipe/tiles.py` takes no source. Decode first checks the delivered grid against the source's description (`grib.check_grid`) and refuses any other, then rolls its columns by where its first column lies (`grib.roll_to_shared`: `round((lon0 - 0) / 0.25) mod 1440` columns, 720 for IFS, none for GFS and GFS-Wave), then refuses any shape but the shared one. The roll was checked on the sample before any tile code: at step 0 of 2026-11-23 12 UTC, ecCodes' nearest point to Riyadh, 24.75°N 46.75°E, holds 302.4167 K, and the rolled array holds the same at row 261, column 187; at 35°N 140°W ecCodes and the rolled array both hold 291.6667 K at row 220, column 880. GFS-Wave's orientation was checked the same way on its sample, run 2026-11-24 12 UTC, at all nine hours and for both fields: at 15°N 65°E in the Arabian Sea, row 300, column 260 of the unrolled array equals ecCodes' nearest point, such as 0.68 m and 11.99 s at f000; at Riyadh, row 261, column 187, the cell is missing, and ecCodes' nearest value there is its missing value 9999.

### Each source fails on its own

Each source of a run is a job of its own, run in the order of `config.SOURCES`, GFS first, then GFS-Wave, then IFS. A job's steps are: for IFS, the deletion of every other cycle's folder under `work/ifs/`; for GFS-Wave and IFS, the probe (below); its index reads; its gaps; its build. In the workflow's form GFS-Wave's steps run in a child process (below). `run.gaps` lists every record a frame needs that is missing: an instant field's record at an hour, its `grib` key's or one of its alternates' (below), never hour 0's for a field whose frame 0 comes from the next frame, or the accumulations a precipitation interval needs; without it the build would write frames of missing bytes. Extra, reordered or repeated records are accepted, the first copy of a repeated one being used.

A job that fails at any step, by a gap, a failed probe, its read time limit, a read that still fails after its retries, a decode error, a write or a move, any exception included, is skipped: its stage folder is deleted, an `::error::` line and a `**Skipped:** <source> <release>: <reason>` line in the run's summary name it, and the next job goes on. A skipped source keeps its entries in the manifest and its folders on the site, so the GFS the app reads keeps updating whatever GFS-Wave or ECMWF does. A run fails, with an error naming each source and its reason and no `$GITHUB_OUTPUT`, only when no job was built; the site then stays as it was.

**A run that builds GFS defers IFS and builds GFS-Wave.** In the workflow's form, once GFS's job has built its release, each later job whose source waits for GFS (`waits_for_gfs`, true for IFS alone) is not run: a `::notice::Deferred <source> <release>: GFS is built in this run; tried in the next run` line and a `**Deferred:** <source> <release>: GFS is built in this run` summary line name it. A deferred source is not a skipped one: the next hourly run, in which GFS is not new, builds it. So GFS never waits on ECMWF's reads in one run, which with the copies' SlowDown answers can take up to the read limit and more. GFS-Wave does not wait: it is built right after GFS in the same run and published with it. Its files come from GFS's bucket at GFS's own hours, about 3.5 to 5.2 hours after the cycle, and the hourly schedule's runs have come 5 to 12 hours apart, so a scheduled run nearly always finds GFS new; deferred like IFS, GFS-Wave would mostly wait for a run in which GFS is not new. The price, chosen on 2026-11-25: every run that builds GFS publishes later by GFS-Wave's own minutes, its reads and its 418 JPEG 2000 decodes, bounded by its child process (below). When GFS's job is skipped, or GFS is not in the run, every job runs. The local form (`--source`) never defers.

**GFS-Wave's job in a child process.** In the workflow's form, a source with `child_seconds`, GFS-Wave alone with 20 minutes, is built in a child process, chosen on 2026-11-25 after the review found that a crash of the decoder library inside the run's own process would end the whole run before its manifest is written, so that every later run would find GFS and GFS-Wave new again and crash the same way. After the check against an earlier release, `wxpipe.run` runs `python -m wxpipe.run` in the local form, `--source gfswave --cycle <release> --hours <the job's hours> --keep 1`, with the run's `--work`, `--precip-method` and `--workers`, into a site of its own at `<work>/child/gfswave/<release>/site`, a stale one removed first. The child's environment is the run's with the repository on `PYTHONPATH` and without `GITHUB_OUTPUT` and `GITHUB_STEP_SUMMARY`; its output is printed after it ends, each line marked `gfswave child: `, so a `::error::` line of the child is not a second annotation of the run. The probe, the gaps, the 15-minute read limit and each message's checks all run inside the child. A non-zero exit, a crash, or the 20 minutes passing, after which the child is killed, skips `gfswave` by the rule above, its reason the exit code or the timeout and the child's last output line; so does an exit 0 whose manifest lacks the job's release or whose release folder is missing. Otherwise the child's entry is merged as the job's, its release folder is moved into the site as a stage folder is, and its build report gives the job's minutes and downloaded megabytes. The child's folder is deleted in every case. **The accepted risk, rewritten:** a crash, a hang, a stalled or a trickling read of the waves ends or times out the child only, so GFS's upload waits at most the child's 20 minutes more than without the waves, and GFS is always published. A probe the child starts is its own child, with its own 300 s timeout.

**Never an earlier release.** In the workflow's form, a job whose release is earlier than the one the restored site's own manifest names for its source, by the check's rule below, is skipped with that reason; the same release is built again, as after a failed deploy.

**The stage folder.** A job builds its release under `<work>/stage/<source>/<release>/`, a stale one from a killed run removed first, and moves it to `<site>/<source>/<release>/` only once every field is written. A folder of the same release, from a rerun, is first set aside as `<work>/stage/<source>/<release>.old` and deleted only once the new one is in place; if the move fails, any partial folder is removed and the old one put back. So a skipped source never leaves a part-built folder in the site, and a rerun of the current release never leaves the manifest pointing at deleted tiles.

**A lost cache.** When the restored site has no readable manifest and GFS was not built in the run, the run is refused and writes no manifest, so the live site stays as it was rather than being published without GFS. With the deferral, a lost-cache run that builds GFS builds `gfswave` beside it, defers `ifs` and publishes GFS and GFS-Wave; one whose GFS was skipped builds `gfswave` and reads `ifs` into its cache, and then refuses, publishing nothing.

**Accepted risks.** While `ifs` keeps failing, it never reaches the live manifest, so the check offers it again every hour, and an hour in which `ifs` is the only new source ends in a red run; the same holds for `gfswave`, whose every try in a run that builds GFS also adds its minutes to that run. IFS's downloads are kept between runs (below), so a later try reads only what is missing; GFS-Wave's are not, so each try reads its files again, about 193 MB for a full run. The runs list is read after a push for repeated red runs. The gate never drops `ifs`'s current release, so GFS's room under the limit rests on the measured sizes.

### ECMWF's reads

The AWS bucket answers "503 SlowDown" often. Fetching the sample of run 2026112312 from it took 270 requests one at a time for 140 distinct ones, 126 of them answered "503 SlowDown"; every one was answered at last, one index file only at its 17th attempt, about 15 minutes after its first. On GitHub's runner it was worse: the first IFS build, run 45291639100, failed after 15 minutes, one read answered "503 SlowDown" at all its ten attempts, and the Actions cache kept 120 KB, about three index files of 85. So IFS is read from three copies (Input, above). A probe of run 2026112312 from a computer outside GitHub on 2026-11-24, one request at a time, compared each copy's files with the sample: the portal answered all 10 index reads and served step 0's index, the `2t` message and ten index files, 12 of 12, the sample's bytes; CloudFront answered 6 of 10, refusing 4 with 503, and served 8 of 8 the sample's bytes, its `2t` message at the second attempt; the bucket answered 4 of 10. So 20 comparisons on two copies, every one the same bytes. The runner was not probed.

**Each read, on one copy (`ecmwf.http_get`).** A 429 or 5xx answer, a timeout or a connection error is tried again, up to 4 attempts, waiting 5, 10 and 20 seconds, or the `Retry-After` header's seconds when given, at most 60; then the read goes on to the next copy. A 404, or any other 4xx, goes on to the next copy at once, with no wait. An answer is accepted on a copy only when it passes these checks; one that does not counts as an error on that copy and goes on to the next at once:
- a byte range of a message: status 206, exactly the asked length, the four bytes `GRIB` first, the edition byte at offset 7 equal to 2, the length field at offsets 8 to 15, big-endian, equal to the body's length, and the four bytes `7777` last;
- a whole read of an `.index`: UTF-8 text from which the index parser gives at least one record;
- any other whole read: any answer.

So one run's files may come from several copies, and the cache of files read earlier from the bucket stays usable. A valid message of another field at the same offset and length would pass these checks; only a parameter check in decode would catch it, and there is none.

**The copies (`ecmwf.read`).** A job's reads start on the first copy. A read tries the current copy, then each other copy in list order from there, wrapping round to the earlier ones last; a copy is passed over only on an `OSError` or an `http.client.HTTPException` from its read, and anything else stops the read at once. A read that answers on its first copy never moves the current copy; only a read that passed over a copy can move it: the copy that answered becomes the current copy for every later read of the job, from both workers, with one log line, `ifs reads: moved from <copy> to <copy>: <error>`, the error being the last one this read met on the copy it leaves. Once every copy has failed, the read raises one `OSError` naming each copy and its last error in the order tried, and the job is skipped by the rule above. A failed read does not cancel the field's reads already queued in the worker pool: the pool lets each of them run to its own end, on every copy in turn, before the failure reaches the job; the read limit drops each one that starts after the limit has passed, so the 45 minutes, plus a read in progress, bound them. The known limit: after a move there is no return to the first copy until a read fails on every later one; the `**Reads:**` line shows that case. IFS's jobs read 2 files at a time.

**The `**Reads:**` line.** For each IFS job that reached its start, built or skipped, the run's summary holds one line, printed at once to the job's log: `**Reads:** ifs <release>: <copy> <n> requests, <n> refused, <n> missing, <n> errors, <n> files, <x.x> MB; <copy> ...`, every copy named in order, those with no request too, a copy being its ROOT without `https://`. Requests counts every attempt; refused, the 429 and 5xx answers; missing, the 404s; errors, any other 4xx, a timeout, a connection error, a wrong status or length or a failed check; files, the reads accepted, with their megabytes of 1,000,000 bytes; so requests are refused, missing, errors and files together. The counts are zeroed at each IFS job's start. A built job's line is a bullet of the Build list right after the job's own; a skipped job's comes right after its `**Skipped:**` line. A line that cannot be built says `not available` with its error and never fails the job. GFS writes no such line.

**The read time limit.** An IFS job has one limit of 45 minutes from its start over its probe, its index reads and its message reads, each read checked before it starts; once it is passed, the next read drops the job by the rule above, its stage folder deleted and every file already read kept. A read already in progress is not cut, so its own attempts can overrun the limit by up to 825 s, about 14 minutes, three copies of four attempts timing out at 60 s with their waits of 35 s; or by up to about 21 minutes when every wait is a `Retry-After` of 60 s.

**The download cache.** Every index and message is written to `<name>.part` in its folder and then renamed onto its name, so a killed run never leaves a short file under the final name. A cached message is reused only when its size is its record's length, when the index gives one; otherwise it is read again. A cached index that does not parse is deleted and read once more.

The workflow keeps `work/ifs` in the Actions cache. It is restored before the build only in a run that builds `ifs` and not GFS, since a run that builds GFS defers `ifs`; it is saved after the build, also after a failed build, whenever it holds a file, and a failed save never stops the gate or the upload. Both use the key `ifs-work-<run id>-<run attempt>`, restored from the newest `ifs-work-` entry. When GFS is in a run's build list but its job is skipped, `ifs` then builds in that run without its cache restored, reading from ECMWF's copies again, and its files are saved for later. At an IFS job's start, `wxpipe.run` deletes every cycle folder under `work/ifs/` but the one it builds, so the cache holds one run.

### GFS-Wave's reads

GFS-Wave is read from GFS's bucket by `gfs.http_get`, as GFS is: 4 attempts at most, each with a 60 s timeout, waiting 1, 2 and 4 s between them, a 404 raised at once, and a range answered with anything but 206 refused. Its reader, `gfs.read_checked`, then checks each range read as one whole GRIB2 message, the checks of ECMWF's range reads: exactly the asked length when the range's end is known, the four bytes `GRIB` first, the edition byte at offset 7 equal to 2, the length field at offsets 8 to 15, big-endian, equal to the body's length, and the four bytes `7777` last. A read that fails them raises `OSError` naming the failed check, and the job is skipped by the rule above; nothing that fails is written to the download cache. A whole read, an index, is taken as given and parsed. GFS keeps `gfs.read`, so GFS's own reads do not change.

**The read time limit.** A GFS-Wave job has one limit of 15 minutes from its start over its probe, its index reads and its message reads, as IFS's 45, each read checked before it starts; once it is passed, the next read drops the job, its stage folder deleted, and GFS is published without it. The limit bounds the reads and not the build: a field is read whole and then decoded and written, so the first field's decode and write fall inside the window before the second field's reads. A read already in progress is not cut: it overruns the limit by up to about 247 s when each of its 4 attempts times out once at 60 s, with their waits, and by about 8 minutes when each attempt's connect and its first byte both wait their 60 s; then comes the decode and write of the field whose reads were done. A read whose answer trickles is not cut by the socket timeout, which counts per operation. A failed read does not cancel the field's reads already queued in the worker pool, which run on to their own end; the read limit bounds the ones not yet started. In the workflow, the child's 20 minutes are the hard bound over all of it: GFS's upload never waits longer for the waves, and GFS's own reads from the same bucket are bounded, as before, only by the build job's 180 minutes.

**No kept downloads.** A GFS-Wave job dropped by its limit keeps its downloads under `work/gfswave/` for that run only: the workflow keeps no Actions cache of them, so a later try reads its files again. A full job reads 209 index files, 215,624 bytes for the run 2026-11-24 12 UTC, and 418 messages, 192,925,577 bytes, about 193 MB.

### The probe

The runner's ecCodes comes from the `eccodeslib` wheel. IFS's messages are CCSDS-packed and GFS-Wave's are JPEG 2000 (`grid_jpeg`), which GFS's are not: GFS's messages are `grid_complex_spatial_differencing`, so no GFS build decodes either packing. Before an IFS or a GFS-Wave job reads its index files, `wxpipe.run` runs `python -m wxpipe.probe <source>` in a child process, with a timeout of 300 s: it decodes the source's committed messages and requires for each the shared grid's shape; for IFS's, every value finite; for GFS-Wave's, exactly the recorded count of values missing, land and ice; and the minimum, maximum and mean over the present values within 0.01 of the local decode's figures, in the message's own units:

| Message | What | Bits | Missing | Minimum | Maximum | Mean |
|---|---|---|---|---|---|---|
| `wxpipe/samples/ifs-2t-ccsds.grib2`, 314,997 bytes | `2t` of 2026-11-22 00 UTC, step 0 | 9 | none | 221.997971 K | 315.997971 K | 278.805962 K |
| `wxpipe/samples/ifs-msl-ccsds.grib2`, 511,939 bytes | `msl` of 2026-11-23 12 UTC, step 0 | 12 | none | 95764.125000 Pa | 104952.125000 Pa | 100852.455598 Pa |
| `wxpipe/samples/gfswave-htsgw-jpeg.grib2`, 424,696 bytes | `HTSGW` of 2026-11-22 06 UTC, f000 | 10 | 468,208 | 0.000000 m | 8.270000 m | 2.267598 m |

A failure, a timeout or a crash of the child skips its source only. Every message of the IFS sample is `grid_ccsds` with no bitmap, at 0 to 12 bits per value; the probe covers the 9-bit and the 12-bit cases. Every message of the GFS-Wave sample is `grid_jpeg` with a bitmap, at 10 to 12 bits per value, decoded by OpenJPEG through ecCodes; one message covers the codec. No test that runs on the runner decodes an IFS or a GFS-Wave message: the probe tests skip when `GITHUB_ACTIONS` is `true`, and the real-sample tests skip without the sample. The probe prints one line on success, which `wxpipe.run` captures and does not print, so its pass on the runner is read from the job's build.

### The check builds only a newer release

`wxpipe.check` counts a source as new only when its newest complete release is later than the live one, compared as `YYYYMMDDHH` text. A discovery falls back to an older cycle only when the newest run's last index answers 403 or 404 for a moment; a SlowDown answer is retried and, when every copy of the source gives one, counts as no complete run that hour; this rule keeps any fallback from replacing a newer release. An unreadable live manifest, a source absent from it, or a live release that is not a 10-digit string counts as new. A release the size gate held back is not new: when the live manifest's `held_back` names the source with a 10-digit release not earlier than the newest complete one, the source is not built, and the check's line ends `; held back by the size gate: <release>`; its next new cycle is offered again (the size gate, below). The discovery's existence test, `gfs.exists`, answers false for 404 and any other 4xx but 429, and tries a 429 or 5xx answer, a timeout or a connection error again after 1, 2 and 4 seconds before raising. For a source of several copies, IFS, the discovery asks the copies in order and the first definite answer decides (`source.exists_on_copies`): a copy answering true or false decides, a copy whose test raises an `OSError` or an `http.client.HTTPException` is passed over, any other exception is raised at once, and the first error is raised only when every copy raised. Each copy passed over is printed in one line, `ifs discovery: <copy> passed over: <error>`, and after the source's discovery one more line names the copy whose answer decided the newest cycle and that answer, `ifs discovery: the newest cycle decided by <copy>: true` or `false`, or says `every copy raised`; GFS and GFS-Wave print neither. When every copy of a source hangs, the check step can take about 25 minutes for it, against about 8.5 for a source of one copy; the check asks GFS's bucket for two sources, GFS and GFS-Wave, so when that bucket hangs the two take about twice 8.5 minutes, and GFS's build waits for the check in that run. So in the hours before a new run, the portal's definite "not there" lets the discovery look at the older cycle, and a throttled copy cannot fail it; a wrong "false" is harmless, since a fallback builds only a release later than the live one. GFS's and GFS-Wave's one copy answers as `gfs.exists` alone. The check prints one source's error as a `::warning::` line with its name and counts that source as having no complete run, and every other source goes on.

No field is built on GFS's time-averaged precipitation-type flags.

## Frames

Every forecast hour each source provides, the longest forecast available, as the user asked: GFS hourly from 0 to 120 h, then every 3 h to 384 h, 209 frames over 16 days; GFS-Wave the same 209 frames; IFS every 3 h to 144 h, then every 6 h to 360 h, 85 frames over 15 days. Frame times are absolute epoch seconds. Frame 0 of two fields has no value of its own and takes frame 1's: the precipitation rate, every source's, and ECMWF's gust (below); with a single hour that frame stays missing.

## Fields and quantisation

Values are stored as one byte per grid cell through a piecewise-linear table (breakpoints in each file header). Byte 255 means missing. The tables put most of the 255 levels where values usually fall:

| Field | GFS record | IFS record, `sfc` (its units: conversion) | Units | Table (byte: value) | Step in the main range |
|---|---|---|---|---|---|
| `temperature.2m` | TMP 2 m | `2t` (K: -273.15) | °C | 0: -70, 20: -30, 230: 50, 254: 60 | 0.38 °C |
| `precipitation.sfc` | APCP surface (rate, below) | `tp` (m since the start: × 1000, rate below) | mm/h | 0: 0, 60: 1.2, 140: 8, 200: 30, 254: 150 | 0.02 mm/h below 1.2 |
| `wind_u.10m`, `wind_v.10m` | UGRD, VGRD 10 m | `10u`, `10v` (m/s) | m/s | 0: -80, 20: -40, 234: 40, 254: 80 | 0.37 m/s |
| `pressure.msl` | PRMSL | `msl` (Pa: × 0.01) | hPa | 0: 870, 20: 950, 234: 1050, 254: 1090 | 0.47 hPa |
| `cloud.total` | TCDC entire atmosphere | `tcc` (0 to 1: × 100) | % | 0: 0, 254: 100 | 0.39 % |
| `wind_gust.sfc` | GUST surface, instant | `10fg`, else `10fg3` (m/s, the maximum over the interval before the step; frame 0 from frame 1, below) | m/s | 0: 0, 120: 60, 140: 80 | 0.5 m/s to 60 m/s, 1 m/s above |

### The wind gust

`wind_gust.sfc` is the seventh field of GFS and of IFS, appended after the six, so their entries, tables, order and tiles stay as they were and a manifest differs only by the new field's entry. One table for both, so the app's wind spectrum applies to each: the sample runs' largest values were 38.72 m/s for GFS, at f007 of 2026110712, and 45.05 m/s for ECMWF, at step 6 of 2026112312. The table, 0.5 m/s a byte to 60 m/s and 1 m/s a byte above to 80 m/s, was the user's choice after a first table of 0.2 m/s a byte, which projected the site at 890.0 MB, 10.0 MB under the gate; with this one the projection is about 838.6 MB, 61.4 MB under it (Storage budget, below). Its largest decode error over the samples was 0.22 m/s for GFS and 0.24 m/s for ECMWF.

- **GFS:** `GUST:surface`, record 14 in every index of the sample, instant, `anl` at hour 0 and `<h> hour fcst` after it, the forms the other instant fields have. Its messages are `grid_complex_spatial_differencing` at 9 or 10 bits, `m s**-1`, on GFS's delivered grid, no bitmap.
- **ECMWF:** `10fg:sfc`, the 10 m gust's maximum over the interval before the step, found by its step as every IFS record is; where a step's index has no `10fg`, `10fg3:sfc`, the maximum over the last 3 hours, is read in its place. In the sample run the gust record's line varies by step, from 5 to 32 over the 85 indexes, each holding one gust record: `10fg` at every step but 144, where it is `10fg3`. The messages are `grid_ccsds`, `m s**-1`, `stepType` `max`, from 90°N and 180°E, rolled by 720 columns as the six fields are, no bitmap. Their intervals, by `stepRange`: one hour at the sample's steps 3, 6 and 9 (`2-3`, `5-6`, `8-9`), the steps between 9 and 141 not read in the sample; three hours at step 144 (`141-144`, `10fg3`); six hours at the steps after it (`144-150` to `354-360`). Step 0's message is 224 bytes, `bitsPerValue` 0, `stepRange` `0`, every value 0: no maximum fills it.

Two rules of `config.Field` serve ECMWF's gust alone, and are refused for any field that is not instant:
- **`alternates`,** the keys tried in order at an hour where `grib`'s record is absent. `run.gaps` and `run.build_instant` find an instant field's record through one function, `run.instant_record`, which tries `grib` then each alternate through the source's own record picker; with none present the hour is a gap.
- **`first_frame`,** `"own"` by default, or `"next"`: hour 0's record is never read and hour 0 is never a gap; when the job's hours begin at 0 and hold two or more, frame 0 takes frame 1's encoded bytes, as the rain's frame 0 does; with hour 0 alone that frame stays missing; a job whose hours do not include 0 reads every hour's own record.

GFS-Wave has two fields of its own, instant, stored in the messages' units, land and ice missing through the messages' bitmap, so byte 255:

| Field | GFS-Wave record | Units | Table (byte: value) | Step in the main range |
|---|---|---|---|---|
| `wave_height.sfc` | HTSGW surface, significant height of combined wind waves and swell | m | 0: 0, 200: 10, 254: 20 | 0.05 m to 10 m, about 0.185 m above |
| `wave_period.sfc` | PERPW surface, primary wave mean period | s | 0: 0, 200: 20, 254: 30 | 0.1 s to 20 s, about 0.185 s above |

The sample run 2026-11-24 12 UTC held 0 to 11.98 m and 1.09 to 26.64 s at its nine hours. A value above a table's last breakpoint clips to byte 254, as for every field. The names follow the house form `<quantity>.<level>`, which the app's reader requires.

GFS and IFS use the same names, units and tables, so the app's spectra and legends apply to each. IFS's parameter names are ecmwf.int's; their units were read from the sample's messages, since no ECMWF page states them. An IFS record is found in the index by `param:levtype`, such as `2t:sfc`, and by its step, the hour itself; no code relies on a record's line, which changes from run to run.

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

- `manifest.json`: for each source, its current release under `sources` and its previous one under `previous`, with the frame times, levels, region, fields and their tables, and its attribution. `previous` is left out when no source has one. After `previous`, `held_back` names each source whose current release the size gate dropped, with that release, `{"gfswave": "<release>"}`; it is left out when empty, and the app ignores it (below).
- `<source>/<release>/<field>/<WxH-step>/<i>-<j>.wxt`, such as `gfs/2026110712/...`, `gfswave/2026112412/...` or `ifs/2026112312/...`: one WXT1 file per tile and field, holding every frame (`wxpipe/wxt.py`). Each frame is a separate zlib-format stream at an offset listed in the header, so the app can read the whole file or single frames with HTTP Range requests (GitHub Pages answers 206). A release of GFS or IFS has 756 files, seven fields, one of GFS-Wave 216, 108 per field.
- The frames are written by libdeflate at level 12, through the `deflate` package. Its zlib-format streams inflate with any zlib inflater, so the header's `"format": "zlib"` and the app are unchanged. On the 9-frame release of the 2026-11-07 12z run, they are 3.52% smaller than zlib level 9, and take 3.80 times as long to encode. The encode time on the runner is not measured: scaled from this computer, about 80 s for 209 frames.
- `status.json`: written by the size gate, below.

### The manifest merge and pruning

A run builds each of its sources into the same site folder, then writes the manifest once (`wxpipe/site.py`), for the sources built; a skipped source's entries stay as they were. Each built source's entry replaces its old one, and its old current release becomes its previous one, so a phone holding the old manifest can still read its tiles while the new one propagates. A rerun of the same release, such as a run built again after a failed deploy, keeps the source's previous release as it was. Every source not built in the run keeps its entries, its `held_back` entry among them; a built source's `held_back` entry is left out, since its new build replaces what the gate held back, and an empty `held_back` is left out, as an empty `previous` is. Then each built source's folders that are neither its current nor its previous release are deleted, under `<source>/` only; another source's folders are never touched.

### The attribution

Each release's `attribution` is one non-empty string, since the app requires one. GFS's is "NOAA NCEP Global Forecast System (GFS). Public domain." GFS-Wave's is "NOAA NCEP GFS-Wave, the global 0.25 degree wave model of the Global Forecast System. Public domain." IFS's is "ECMWF IFS open data." followed, joined by single spaces, by the six elements of ECMWF's licence page in seven keys, which also go, in this order, into the `ifs` entry's object `attribution_parts`, right after `attribution`; the app ignores unknown keys until it shows them:

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

GFS, measured on 9 frames of the 2026-11-07 12z run and scaled to 209 frames; IFS and GFS-Wave follow below it, with the site's total for two releases of each source:

| Level | MB per release |
|---|---|
| 0.25° region | 25 |
| 0.5° world | 149 |
| 1° world | 46 |
| 2° world | 13 |
| **All 6 fields** | **233** |

Two GFS releases of the six fields take 466 MB of the 1 GB GitHub Pages limit; with IFS and GFS-Wave the site's total is about 725.8 MB before the gust, by the live site's bytes (The wind gust, below), where S15's estimate from the samples was about 726.5 MB (GFS-Wave, below). A further field costs what its values cost to compress: the gust, measured with its table on the samples, adds about 39.5 MB to a GFS release and about 16.9 MB to an IFS release (The wind gust, below), so about 112.8 MB with two releases of each, where the six fields' mean is about 38.8 MB per GFS release. Ways past the limit: a second public repository with its own 1 GB site, or a smaller 0.5° area. `wxpipe.budget` measures one source's current release, `--source gfs` by default; it is a local estimate tool, not the gate.

IFS, measured on the 9-step sample of the 2026-11-23 12z run: 648 tiles, 9,913,720 bytes; one release scaled to 85 frames is about 92.3 MB. The live site of two GFS releases held 453,123,135 bytes on 2026-11-23; with two IFS releases it comes to about 637.7 MB, under the gate's 900 MB with about 262 MB to spare, and to about 545.4 MB after the gate's first candidate, which drops `ifs`'s previous release.

GFS-Wave, measured on the 9-frame sample of the 2026-11-24 12z run, hours 0, 1, 6, 7, 120, 123, 126, 381 and 384: 216 tiles, 2,170,314 bytes. Scaled to 209 frames, in MB per release:

| Level | `wave_height.sfc` | `wave_period.sfc` |
|---|---|---|
| 0.25° region | 1.07 | 1.18 |
| 0.5° world | 17.29 | 14.90 |
| 1° world | 5.81 | 5.46 |
| 2° world | 1.77 | 1.81 |

One release is about 49.3 MB, far below the plan's upper bound of 78 MB, since land is one constant byte. The live site of 627,938,323 bytes on 2026-11-24, two GFS and two IFS releases, comes with two GFS-Wave releases to about 726.5 MB, about 173.5 MB under the gate's 900 MB. The two fields' decode and write took 1.4 s and 1.8 s for 9 frames on a local computer, so about 33 s and 42 s, about 74 s in all, for 209 frames there; the runner is not measured. A full job reads 209 index files, 215,624 bytes, and 418 messages, 192,925,577 bytes, about 193 MB.

The wind gust, measured on the samples by local builds of GFS's 9 frames and IFS's 9 steps and scaled, with the table of 0.5 m/s a byte: one GFS gust release about 39.5 MB at 209 frames, one IFS gust release about 16.9 MB at 85 steps. With two releases of every source the site comes to about 838.6 MB, 61.4 MB under the gate's 900 MB, over a base of 725,839,899 bytes: the live site's 676,939,899 bytes with one GFS-Wave release, plus a second GFS-Wave release of 48.9 MB. Its decode and write took about 0.8 s for 9 frames locally, so about 19 s for GFS's 209 frames and about 8 s for IFS's 85 steps; its reads add about 126 MB to a GFS job and 53,431,705 bytes, about 53 MB, to an IFS job.

## The size gate

`wxpipe.gate` runs between the build and the upload. It sums every file of the site folder, the tiles and `manifest.json`, and compares the total with 900,000,000 bytes, the plan's 900 MB. `status.json` is outside the limit, since the gate writes it after the sum. Above the limit it degrades before it fails, trying these candidates in order on the projected total before any file is deleted:

1. drop `ifs`'s previous release;
2. and drop `gfswave`'s previous release;
3. or, in place of 2: drop `gfswave`'s current release and publish its previous one as current, if it has one.

The first candidate at or under the limit is applied: its folders are deleted and the manifest is rewritten, an emptied `previous` dropped. GFS is never dropped. If no candidate fits, nothing is deleted, the gate exits 1, the workflow uploads nothing, and the live site stays as it was. When nothing is dropped the manifest is not rewritten.

**A release held back.** After candidate 3 the live manifest names `gfswave`'s previous release as current, so the hourly check would see the newest run as new every hour, build it, and see the gate drop it again. So when the gate drops a source's current release, the manifest it writes records it under `held_back`, `{"gfswave": "<the dropped release>"}`, any earlier `held_back` carried, a source dropped again taking its newly dropped release. `wxpipe.check` counts a release not later than the held one as not new (above), and later merges carry the entry for as long as the source is not built. At its next new cycle the source is offered again and built, its entry is left out, and the gate may hold that release back in turn; each such try of `gfswave` adds its minutes to that run's GFS publication. A drop of previous releases alone writes no `held_back`.

In every case the gate writes `status.json` beside the manifest: `generated_at`, `limit_bytes`, `total_bytes_before`, `total_bytes` after, `dropped` (each with `source`, `release` and `role`, `previous` or `current`) and `sources` (each source's current release after the gate). So a check reads the site's real total and what was dropped from the live site, like the manifest. The run's summary says the same.

## Workflow

`.github/workflows/publish.yml` runs hourly at minute 17.

1. `wxpipe.check` runs first on the runner's own Python, using only the standard library. For each source it finds the newest complete run and compares it with the live `manifest.json` and its `held_back`, printing one line per source. It writes `new` (true if any source is new), `build` (the new sources) and `all` (every source with a complete run), the last two as `<source>:<cycle>` lists. An unreadable live manifest counts every source as new. When nothing is new, the job ends there, with no install and no cache restore. The manifest URL is built from `GITHUB_REPOSITORY_OWNER` and `GITHUB_REPOSITORY`, because the payload of the schedule event has no repository object.
2. When there is a new run, the workflow installs the pinned packages. On Linux the ecCodes library comes from `eccodeslib`, which findlibs locates inside the package. The workflow checks that the library loads, then runs the unit tests and restores the previous site from the Actions cache, and ECMWF's downloads (`work/ifs`) when the run builds `ifs` and not GFS.
3. It builds the new sources with `--cycles` and `--all-cycles`, every forecast hour and field of each, keeping each source's new release and its previous one; a source that fails is skipped and the others are built, and a run that builds GFS builds `gfswave` after it, in a child process of at most 20 minutes, and defers `ifs` (above). Then `work/ifs` is saved in the Actions cache, also after a failed build, whenever it holds a file; a failed save does not stop the run.
   **A lost cache:** when the restored site has no readable `manifest.json`, the Actions cache having been evicted, every source with a complete run is built in that run, so the site is never published without a source; a run in which GFS is then not built is refused and publishes nothing, and one that builds GFS builds `gfswave` beside it and defers `ifs` to the next run. Every previous release is gone after a lost cache, until later runs build new ones; the app falls back after a 404 and keeps what it has cached. `wxpipe.run` writes a line to the run's summary when it finds no readable `manifest.json`. A run that builds GFS took 3 min 49 s for its build job in S15's live run `45520423826`: GFS 96.5 s with 1171.2 MB downloaded, then GFS-Wave's job in its child 57.0 s with 193.0 MB, its child bounded at 20 minutes; the deploy took 21 s. The gust adds to a GFS job about 126 MB of reads and about 19 s of decode and write by a local measure, and to an IFS job about 53 MB and about 8 s. An IFS run is longer, its reads limited to 45 minutes, a read in progress overrunning it by up to 825 s, about 14 minutes, or about 21 minutes when every wait is a `Retry-After` of 60 s; the build job's timeout is 180 minutes in every run, since a job's timeout cannot read a step's output, so only a run that hangs reaches it.
4. The size gate runs, then the site is uploaded and saved in the Actions cache for the next run.
5. The deploy job deploys it with `actions/deploy-pages`. If the first deploy fails, a timeout being the expected case, it is retried once; the job fails only when the retry fails too. Its last step writes the job's minutes, from its first step to the summary step, its attempts and its outcome into the run's summary (`wxpipe/summary.py`), with a warning from 6 minutes. That step never fails the job. `status.json` cannot hold the deploy's minutes, since the upload ends the build job before the deploy job starts.
6. A heartbeat commit at most once a week keeps the schedule enabled, because GitHub disables schedules after 60 days without activity. The heartbeat job runs when the deploy job succeeds, and is skipped when the deploy job fails or is skipped.

The run's summary also holds, from `wxpipe.run`, each built source's release, frames, fields, minutes and downloaded MB, each skipped source's `**Skipped:**` line, each IFS job's `**Reads:**` line and each deferred source's `**Deferred:**` line, and from the gate its totals and what it dropped. A source's minutes are its own job's, from the start of its probe or its index reads to the move of its release into the site; for GFS-Wave's child, the child's own job, read with its downloaded megabytes from the child's build report. The install, test, cache and upload steps keep the times the run's page already shows.

On the 9-frame sample, a local run took 25 s, including the download. A full GFS run fetched 1171.2 MB from AWS on the runner in S15's live run, before the gust, and fetches about 126 MB more with it, by the sample's nine hours; a full GFS-Wave run about 193 MB from the same bucket; a full IFS run about 270 MB from ECMWF's copies, and about 53 MB more with the gust.
