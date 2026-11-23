# wxpipe

Map tiles for a personal weather app, built from NOAA GFS forecasts by a scheduled GitHub Actions workflow and served from GitHub Pages. The design is in [docs/DESIGN.md](docs/DESIGN.md).

Weather data: NOAA NCEP Global Forecast System (GFS), public domain.

## Run locally

Python 3.12 or later.

```
pip install -r requirements.txt
python -m pytest
python -m wxpipe.run --out work/site --work work --cycle 2026110712 --hours 0,1,6,7
python -m wxpipe.budget work/site
python -m wxpipe.gate --site work/site
```

`wxpipe.run` builds one source, `--source gfs` by default, the only source so far. Without `--cycle` the newest complete run is used, and without `--hours` all of the source's frames (209 for GFS, 16 days). `wxpipe.budget` takes `--source` the same way. `wxpipe.gate` is the size gate the workflow runs before the upload: it may delete releases from the folder it is given, and it writes `status.json` there; `--limit-bytes` sets the limit, 900,000,000 by default.

The workflow's form of `wxpipe.run` takes `--cycles` and `--all-cycles`, lists of `<source>:<cycle>` that `wxpipe.check` writes.
