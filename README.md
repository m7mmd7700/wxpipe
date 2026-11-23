# wxpipe

Map tiles for a personal weather app, built from NOAA GFS and ECMWF IFS forecasts by a scheduled GitHub Actions workflow and served from GitHub Pages. The design is in [docs/DESIGN.md](docs/DESIGN.md).

Weather data: NOAA NCEP Global Forecast System (GFS), public domain; and ECMWF IFS open data, © European Centre for Medium-Range Weather Forecasts (ECMWF), published under a Creative Commons Attribution 4.0 International (CC BY 4.0) licence, https://creativecommons.org/licenses/by/4.0/. Source www.ecmwf.int. Based on data and products of ECMWF; ECMWF does not accept any liability whatsoever for any error or omission in the data, their availability, or for any loss or damage arising from their use. Modified: the columns rolled to start at 0°E, units converted, accumulated precipitation turned into rates, values quantised to 8-bit tables and cut into map tiles.

## Run locally

Python 3.12 or later.

```
pip install -r requirements.txt
python -m pytest
python -m wxpipe.run --out work/site --work work --cycle 2026110712 --hours 0,1,6,7
python -m wxpipe.run --out work/site --work work --source ifs --cycle 2026112312 --hours 0,3,6,9
python -m wxpipe.probe ifs
python -m wxpipe.budget work/site
python -m wxpipe.gate --site work/site
```

`wxpipe.run` builds one source, `--source gfs` by default, or `--source ifs`. Without `--cycle` the newest complete run is used, and without `--hours` all of the source's frames (209 for GFS, 16 days; 85 for IFS, 15 days). An IFS build first runs the probe, `wxpipe.probe`, which checks that the local ecCodes decodes the two committed CCSDS messages, and deletes every other cycle's folder under `<work>/ifs/`. `wxpipe.budget` takes `--source` the same way. `wxpipe.gate` is the size gate the workflow runs before the upload: it may delete releases from the folder it is given, and it writes `status.json` there; `--limit-bytes` sets the limit, 900,000,000 by default.

The workflow's form of `wxpipe.run` takes `--cycles` and `--all-cycles`, lists of `<source>:<cycle>` that `wxpipe.check` writes.
