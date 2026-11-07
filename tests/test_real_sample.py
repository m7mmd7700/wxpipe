"""M2 exit-criterion test on real GFS data: a decoded tile matches the GRIB grid within half a LUT step.

It reads the sample that `python -m wxpipe.run --cycle 2026110712 --hours ...` caches under work/,
and skips when that sample is not on disk.
"""

from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, gfs, tiles, wxt
from wxpipe.run import Fetcher, build_instant, parse_cycle, write_field

ROOT = Path(__file__).resolve().parents[1]
CYCLE = "2026110712"
SAMPLE = ROOT / "work" / "gfs" / CYCLE
HOUR = 1

pytestmark = pytest.mark.skipif(not (SAMPLE / f"f{HOUR:03d}.idx").exists(), reason="GFS sample not downloaded")


@pytest.mark.parametrize("name", ["temperature.2m", "wind_u.10m", "pressure.msl", "cloud.total"])
def test_tiles_round_trip_to_the_grib_values(name, tmp_path):
    from wxpipe import grib

    field = config.field(name)
    cycle = parse_cycle(CYCLE)
    fetcher = Fetcher(cycle, ROOT / "work")
    idx = {HOUR: fetcher.idx(HOUR)}
    record = gfs.instant_record(idx[HOUR], field.grib, HOUR)
    truth = grib.decode((SAMPLE / f"f{HOUR:03d}" / f"{record.number}.grib2").read_bytes()) * field.scale + field.offset
    assert np.isfinite(truth).all()

    frames = build_instant(field, cycle, (HOUR,), idx, fetcher)
    write_field(tmp_path, field, CYCLE, frames)
    checked = 0
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            header, decoded = wxt.read(tmp_path / field.name / tile.size.path / f"{tile.path}.wxt")
            assert header["grid"]["grid"] == [tile.x0, tile.x1, tile.y0, tile.y1]
            values = field.lut.decode(decoded[0][1])
            expected = tile.cut(truth)
            inside = (expected >= field.lut.min_value) & (expected <= field.lut.max_value)
            steps = np.vectorize(field.lut.step)(expected[inside])
            assert np.all(np.abs(values[inside] - expected[inside]) <= steps / 2 + 1e-6)
            checked += inside.sum()
    assert checked > 100_000


def test_grid_orientation_puts_riyadh_where_the_app_expects_it():
    from wxpipe import grib

    field = config.field("temperature.2m")
    idx = Fetcher(parse_cycle(CYCLE), ROOT / "work").idx(HOUR)
    record = gfs.instant_record(idx, field.grib, HOUR)
    grid = grib.decode((SAMPLE / f"f{HOUR:03d}" / f"{record.number}.grib2").read_bytes()) - 273.15
    # Row 0 is 90N; Riyadh (24.7N, 46.7E) sits near row 261, column 187. In November, mid-afternoon there
    # (13 UTC, hour 1 of the 12z run) is warm, and the polar row is far colder.
    riyadh = grid[round((90 - 24.7) / 0.25), round(46.7 / 0.25)]
    assert 15 < riyadh < 40
    assert grid[0].mean() < riyadh - 20
