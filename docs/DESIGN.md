# wxpipe design

wxpipe turns each NOAA GFS run into static map tiles for the weatherstation app and publishes them on GitHub Pages from a scheduled GitHub Actions workflow. There is no server. Blueprint references: `rea-analysis/notes/REBUILD_BLUEPRINT.md` §2.3, §6.2 and §11.

## Input

- NOAA GFS 0.25° from the NOAA Open Data bucket on AWS (`noaa-gfs-bdp-pds`), public domain. Only the needed records are read, by HTTP byte range from the `.idx` offsets: about 5 MB per forecast hour instead of 530 MB.
- A run is used once its last hour (f384) is published. The workflow checks hourly and stops early when the newest complete run is already on the site.

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

- `manifest.json`: the current release and the previous one, with the frame times, levels, region, fields and their tables.
- `gfs/<release>/<field>/<WxH-step>/<i>-<j>.wxt`: one WXT1 file per tile and field, holding every frame (`wxpipe/wxt.py`). Each frame is a separate zlib stream at an offset listed in the header, so the app can read the whole file or single frames with HTTP Range requests (GitHub Pages answers 206). A release has 648 files.

## Storage budget (measured)

Measured on 9 frames of the 2026-11-07 12z run and scaled to 209 frames:

| Level | MB per release |
|---|---|
| 0.25° region | 25 |
| 0.5° world | 149 |
| 1° world | 46 |
| 2° world | 13 |
| **All 6 fields** | **233** |

Two releases take 466 MB of the 1 GB GitHub Pages limit. Each further field costs about 40 MB per release, or 80 MB with two releases. Ways past the limit: a second public repository with its own 1 GB site, or a smaller 0.5° area.

## Workflow

`.github/workflows/publish.yml` runs hourly at minute 17.

1. `wxpipe.check` runs first on the runner's own Python, using only the standard library. It finds the newest complete run and compares it with the live `manifest.json`. When nothing is new, the job ends there, with no install and no cache restore. The manifest URL is built from `GITHUB_REPOSITORY_OWNER` and `GITHUB_REPOSITORY`, because the payload of the schedule event has no repository object.
2. When there is a new run, the workflow installs the pinned packages. On Linux the ecCodes library comes from `eccodeslib`, which findlibs locates inside the package. The workflow checks that the library loads, then runs the unit tests and restores the previous site from the Actions cache.
3. It builds that run with `--cycle`, keeps the new release and the previous one, uploads the site and deploys it with `actions/deploy-pages`.
4. A heartbeat commit at most once a week keeps the schedule enabled, because GitHub disables schedules after 60 days without activity.

On the 9-frame sample, a local run took 25 s, including the download. A full run fetches about 1.1 GB from AWS on the runner.
