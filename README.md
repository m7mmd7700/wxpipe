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
```

Without `--cycle` the newest complete run is used, and without `--hours` all 209 frames (16 days).
