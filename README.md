# wxpipe

Map tiles for a personal weather app, built from NOAA GFS, NOAA GFS-Wave and ECMWF IFS forecasts by a scheduled GitHub Actions workflow and served from GitHub Pages. The design is in [docs/DESIGN.md](docs/DESIGN.md).

Weather data: NOAA NCEP Global Forecast System (GFS) and NOAA NCEP GFS-Wave, its global wave model, public domain; and ECMWF IFS open data, © European Centre for Medium-Range Weather Forecasts (ECMWF), published under a Creative Commons Attribution 4.0 International (CC BY 4.0) licence, https://creativecommons.org/licenses/by/4.0/. Source www.ecmwf.int. Based on data and products of ECMWF; ECMWF does not accept any liability whatsoever for any error or omission in the data, their availability, or for any loss or damage arising from their use. Modified: the columns rolled to start at 0°E, units converted, accumulated precipitation turned into rates, values quantised to 8-bit tables and cut into map tiles.

## Run locally

Python 3.12 or later.

```
pip install -r requirements.txt
python -m pytest
python -m wxpipe.run --out work/site --work work --cycle 2026110712 --hours 0,1,6,7
python -m wxpipe.run --out work/site --work work --source gfswave --cycle 2026112412 --hours 0,1,6,7
python -m wxpipe.run --out work/site --work work --source ifs --cycle 2026112312 --hours 0,3,6,9
python -m wxpipe.probe gfswave
python -m wxpipe.probe ifs
python -m wxpipe.budget work/site
python -m wxpipe.gate --site work/site
```

`wxpipe.run` builds one source, `--source gfs` by default, `--source gfswave` or `--source ifs`. Without `--cycle` the newest complete run is used, and without `--hours` all of the source's frames (209 for GFS and GFS-Wave, 16 days; 85 for IFS, 15 days). A GFS-Wave or an IFS build first runs the probe, `wxpipe.probe`, which checks that the local ecCodes decodes the source's committed messages, GFS-Wave's JPEG 2000 one and IFS's two CCSDS ones; an IFS build also deletes every other cycle's folder under `<work>/ifs/`. `wxpipe.budget` takes `--source` the same way. `wxpipe.gate` is the size gate the workflow runs before the upload: it may delete releases from the folder it is given, and it writes `status.json` there; `--limit-bytes` sets the limit, 900,000,000 by default.

The workflow's form of `wxpipe.run` takes `--cycles` and `--all-cycles`, lists of `<source>:<cycle>` that `wxpipe.check` writes. In it, GFS-Wave's job runs in a child process of at most 20 minutes, the local form with `--source gfswave`, so a crash or a hang of it never stops GFS's publication.
