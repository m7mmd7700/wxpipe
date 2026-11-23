# wxpipe design

wxpipe turns each NOAA GFS run into static map tiles for the weatherstation app and publishes them on GitHub Pages from a scheduled GitHub Actions workflow. There is no server. Blueprint references: `rea-analysis/notes/REBUILD_BLUEPRINT.md` §2.3, §6.2 and §11.

## Input

- NOAA GFS 0.25° from the NOAA Open Data bucket on AWS (`noaa-gfs-bdp-pds`), public domain. Only the needed records are read, by HTTP byte range from the `.idx` offsets: about 5 MB per forecast hour instead of 530 MB.
- A run is used once its last hour (f384) is published. The workflow checks hourly and stops early when the newest complete run is already on the site.

## Sources

The pipeline is built for several sources on one site. GFS is the only one so far; ECMWF IFS (`ifs`) and GFS-Wave (`gfswave`) are planned. Each source is a description in two parts, so a change of a source's layout is one edit of values, not of code:

- **The standard-library part,** `SourceInfo` in `wxpipe/source.py`: the name, the last forecast hour, the cycle step, the bucket's base URL, the file name pattern, the index file's suffix and how many cycles to look back. With them come the URL functions and the discovery of the newest complete run. `wxpipe.check` imports only this part and `wxpipe/gfs.py`, so it runs on the runner's own Python before any install; a test imports every standard-library module in a subprocess with numpy and eccodes blocked.
- **The full part,** `Source` in `wxpipe/config.py`: its `SourceInfo`, the grid as delivered (columns, rows, first point, increments, scanning flags), the forecast hours, the fields with their tables, the fetch functions (index parser, record pickers, byte-range reader) and the attribution.

Every source is put on the shared output grid, 1440 × 721 at 0.25°, row 0 at 90°N and column 0 at 0°E, inside `grib.decode`, so the tiles and the app's domain are the same for all sources and `wxpipe/tiles.py` takes no source. GFS already delivers that grid; a grid that starts at 180°E is rolled in decode from S14, when such a source comes, and until then a decoded grid of any other shape is refused. A message whose delivered grid differs from its source's description is refused too (`grib.check_grid`). That refusal comes during the build, after the release folder being built is removed, so the run's site folder is left part-built; the live site stays safe because the workflow neither uploads the site nor saves it in the cache after a failed step.

### A run that differs is refused

Before anything is written, `run.gaps` reads the run's index files and lists every record a frame needs that is missing: an instant field's record at an hour, or the accumulations a precipitation interval needs. Every source's index files are read and checked first; any gap stops the run with one error naming each source and release with its gaps, written as an `::error::` line and into the run's summary, and the site and its release folders stay as they were; without this the build would write frames of missing bytes. Extra, reordered or repeated records are accepted, the first copy of a repeated one being used.

No field is built on GFS's time-averaged precipitation-type flags.

## Frames

Every forecast hour GFS provides: hourly from 0 to 120 h, then every 3 h to 384 h. That makes 209 frames over 16 days, the longest forecast available, as the user asked. Frame times are absolute epoch seconds.

## Fields and quantisation

Values are stored as one byte per grid cell through a piecewise-linear table (breakpoints in each file header). Byte 255 means missing. The tables put most of the 255 levels where values usually fall:

| Field | GFS record | Units | Table (byte: value) | Step in the main range |
|---|---|---|---|---|
| `temperature.2m` | TMP 2 m | °C | 0: -70, 20: -30, 230: 50, 254: 60 | 0.38 °C |
| `precipitation.sfc` | APCP surface (rate, below) | mm/h | 0: 0, 60: 1.2, 140: 8, 200: 30, 254: 150 | 0.02 mm/h below 1.2 |
| `wind_u.10m`, `wind_v.10m` | UGRD, VGRD 10 m | m/s | 0: -80, 20: -40, 234: 40, 254: 80 | 0.37 m/s |
| `pressure.msl` | PRMSL | hPa | 0: 870, 20: 950, 234: 1050, 254: 1090 | 0.47 hPa |
| `cloud.total` | TCDC entire atmosphere | % | 0: 0, 254: 100 | 0.39 % |

Wind is stored as signed u and v components, not speed and direction. Components interpolate correctly between frames, feed streamlines directly, and leave no from/to convention to settle, which closes blueprint question 6. Point mode (Open-Meteo) keeps speed plus a "from" direction.

The app must treat byte 255 as missing (blueprint B2): its LUT maps 255 to "discard" in M3.

### Precipitation rate

GFS stores APCP two ways: the total since the run started, and a bucket that restarts every 6 hours. The rate over (previous frame, frame] uses the bucket when it covers the previous frame, and the difference of two buckets otherwise (`wxpipe/precip.py`). This was measured on the 2026-11-07 12z run:

| Interval | Negative cells: bucket / since-start | Largest difference between methods |
|---|---|---|
| 6 to 7 h | 0 / 0 | 0.125 mm/h, 133,105 cells over 0.05 |
| 120 to 123 h | 0 / 1 | 0.04 mm/h |
| 381 to 384 h | 0 / 3 | 0.04 mm/h |

Differencing the large since-start totals adds packing noise of up to 0.125 mm/h and occasional negative rates, so the bucket method is the default. Negative results are clamped to zero.

## Tiles

| Level | Coverage | Tiles | Samples per tile |
|---|---|---|---|
| `60x60/1` (0.25°) | the user's region, 15°E to 75°E and 0° to 45°N | 12 | 61 × 61 |
| `120x120/2` (0.5°) | world | 72 | 61 × 61 |
| `240x240/4` (1°) | world | 18 | 61 × 61, or 61 × 31 in the bottom row |
| `480x480/8` (2°) | world | 6 | 61 × 61, or 61 × 31 in the bottom row |

Tile (i, j) spans columns i·W to i·W + W and rows j·H to j·H + H, ends included, so neighbours share an edge. Column 1440 is column 0 again. The rules match `RegularLatLonDomain` in the app (`core:config`).

## Files

- `manifest.json`: for each source, its current release under `sources` and its previous one under `previous`, with the frame times, levels, region, fields and their tables. `previous` is left out when no source has one.
- `<source>/<release>/<field>/<WxH-step>/<i>-<j>.wxt`, such as `gfs/2026110712/...`: one WXT1 file per tile and field, holding every frame (`wxpipe/wxt.py`). Each frame is a separate zlib stream at an offset listed in the header, so the app can read the whole file or single frames with HTTP Range requests (GitHub Pages answers 206). A GFS release has 648 files.
- `status.json`: written by the size gate, below.

### The manifest merge and pruning

A run builds each of its sources into the same site folder, then writes the manifest once (`wxpipe/site.py`). Each built source's entry replaces its old one, and its old current release becomes its previous one, so a phone holding the old manifest can still read its tiles while the new one propagates. A rerun of the same release, such as a run built again after a failed deploy, keeps the source's previous release as it was. Every source not built in the run keeps its entries. Then each built source's folders that are neither its current nor its previous release are deleted, under `<source>/` only; another source's folders are never touched.

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
2. When there is a new run, the workflow installs the pinned packages. On Linux the ecCodes library comes from `eccodeslib`, which findlibs locates inside the package. The workflow checks that the library loads, then runs the unit tests and restores the previous site from the Actions cache.
3. It builds the new sources with `--cycles` and `--all-cycles`, every forecast hour and field of each, keeping each source's new release and its previous one.
   **A lost cache:** when the restored site has no readable `manifest.json`, the Actions cache having been evicted, every source with a complete run is built in that run, so the site is never published without a source. Every previous release is gone after a lost cache, until later runs build new ones; the app falls back after a 404 and keeps what it has cached. `wxpipe.run` writes a line to the run's summary when it finds no readable `manifest.json`. A normal run takes 2 to 4 minutes; the build job's timeout is 180 minutes in every run, since a job's timeout cannot read a step's output, so only a run that hangs reaches it.
4. The size gate runs, then the site is uploaded and saved in the Actions cache for the next run.
5. The deploy job deploys it with `actions/deploy-pages`. If the first deploy fails, a timeout being the expected case, it is retried once; the job fails only when the retry fails too. Its last step writes the job's minutes, from its first step to the summary step, its attempts and its outcome into the run's summary (`wxpipe/summary.py`), with a warning from 6 minutes. That step never fails the job. `status.json` cannot hold the deploy's minutes, since the upload ends the build job before the deploy job starts.
6. A heartbeat commit at most once a week keeps the schedule enabled, because GitHub disables schedules after 60 days without activity. The heartbeat job runs when the deploy job succeeds, and is skipped when the deploy job fails or is skipped.

The run's summary also holds, from `wxpipe.run`, each built source's release, frames, fields, minutes and downloaded MB, and from the gate its totals and what it dropped. The install, test, cache and upload steps keep the times the run's page already shows.

On the 9-frame sample, a local run took 25 s, including the download. A full GFS run fetches about 1.1 GB from AWS on the runner.
