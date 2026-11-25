"""M2 exit-criterion test on real GFS data: a decoded tile matches the GRIB grid within half a LUT step.

It reads the sample that `python -m wxpipe.run --cycle 2026110712 --hours ...` caches under work/,
and skips when that sample is not on disk.
"""

from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, ecmwf, gfs, tiles, wxt
from wxpipe.run import Fetcher, build_instant, parse_cycle, write_field

ROOT = Path(__file__).resolve().parents[1]
CYCLE = "2026110712"
SAMPLE = ROOT / "work" / "gfs" / CYCLE
HOUR = 1

pytestmark = pytest.mark.skipif(not (SAMPLE / f"f{HOUR:03d}.idx").exists(), reason="GFS sample not downloaded")


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    """A cached message that is missing fails the test instead of being downloaded."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setattr(ecmwf, "http_get", refuse)


@pytest.mark.parametrize("name", ["temperature.2m", "wind_u.10m", "pressure.msl", "cloud.total"])
def test_tiles_round_trip_to_the_grib_values(name, tmp_path):
    from wxpipe import grib

    field = config.GFS.field(name)
    cycle = parse_cycle(CYCLE)
    fetcher = Fetcher(config.GFS, cycle, ROOT / "work")
    idx = {HOUR: fetcher.idx(HOUR)}
    record = gfs.instant_record(idx[HOUR], field.grib, HOUR)
    truth = grib.decode((SAMPLE / f"f{HOUR:03d}" / f"{record.number}.grib2").read_bytes(), config.GFS) * field.scale + field.offset
    assert np.isfinite(truth).all()

    frames = build_instant(config.GFS, field, cycle, (HOUR,), idx, fetcher)
    write_field(tmp_path, config.GFS, field, CYCLE, frames)
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


def raw_values(message: bytes) -> np.ndarray:
    """The message's values through ecCodes alone, never `grib.decode`: GFS's grid is 0E first and north
    first, scanning flags 0 and 0, so the values fill the shared grid row by row."""
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        assert (eccodes.codes_get(handle, "Ni"), eccodes.codes_get(handle, "Nj")) == (config.NX, config.NY)
        values = np.asarray(eccodes.codes_get_values(handle), dtype=np.float64)
    finally:
        eccodes.codes_release(handle)
    return values.reshape(config.NY, config.NX)


def nearest(message: bytes, lat: float, lon: float) -> tuple[float, float, float]:
    """ecCodes' nearest grid point to (lat, lon) in the raw message: its latitude, longitude and value."""
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        point = eccodes.codes_grib_find_nearest(handle, lat, lon)[0]
    finally:
        eccodes.codes_release(handle)
    return float(point["lat"]), float(point["lon"]), float(point["value"])


def tile_value(site: Path, field: config.Field, row: int, col: int) -> float:
    """The value the written tiles hold at a shared-grid cell, from the finest level that has the cell."""
    target = row * config.NX + col
    numbers = np.arange(config.NY * config.NX).reshape(config.NY, config.NX)
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            hit = np.argwhere(tile.cut(numbers) == target)
            if len(hit):
                _, frames = wxt.read(site / field.name / tile.size.path / f"{tile.path}.wxt")
                j, i = hit[0]
                return float(field.lut.decode(frames[0][1])[j, i])
    raise AssertionError(f"no tile holds row {row}, column {col}")


def test_gust_tiles_equal_the_raw_message_at_every_level_and_at_riyadh(tmp_path):
    # M6 S16, A18: GFS's gust at hour 1, its record found by the build's own rule.
    from wxpipe import run

    field = config.GFS.field("wind_gust.sfc")
    cycle = parse_cycle(CYCLE)
    fetcher = Fetcher(config.GFS, cycle, ROOT / "work")
    idx = {HOUR: fetcher.idx(HOUR)}
    record = run.instant_record(config.GFS, field, idx[HOUR], HOUR)
    assert record.key == "GUST:surface" and record.fcst == f"{HOUR} hour fcst"
    message = (SAMPLE / f"f{HOUR:03d}" / f"{record.number}.grib2").read_bytes()
    truth = raw_values(message)
    assert np.isfinite(truth).all()

    frames = build_instant(config.GFS, field, cycle, (HOUR,), idx, fetcher)
    write_field(tmp_path, config.GFS, field, CYCLE, frames)
    checked = 0
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            header, decoded = wxt.read(tmp_path / field.name / tile.size.path / f"{tile.path}.wxt")
            assert header["units"] == "m s**-1" and header["values"] == config.GUST_LUT.header()
            values = field.lut.decode(decoded[0][1])
            expected = tile.cut(truth)
            inside = (expected >= field.lut.min_value) & (expected <= field.lut.max_value)
            steps = np.vectorize(field.lut.step)(expected[inside])
            assert np.all(np.abs(values[inside] - expected[inside]) <= steps / 2 + 1e-6)
            checked += inside.sum()
    assert checked > 100_000
    near_lat, near_lon, want = nearest(message, 24.75, 46.75)
    got = tile_value(tmp_path, field, round((config.LAT0 - near_lat) / config.DEG),
                     round(((near_lon - config.LON0) % 360.0) / config.DEG) % config.NX)
    assert abs(got - want) <= field.lut.step(want) / 2 + 1e-6, (got, want)


def test_grid_orientation_puts_riyadh_where_the_app_expects_it():
    from wxpipe import grib

    field = config.GFS.field("temperature.2m")
    idx = Fetcher(config.GFS, parse_cycle(CYCLE), ROOT / "work").idx(HOUR)
    record = gfs.instant_record(idx, field.grib, HOUR)
    grid = grib.decode((SAMPLE / f"f{HOUR:03d}" / f"{record.number}.grib2").read_bytes(), config.GFS) - 273.15
    # Row 0 is 90N; Riyadh (24.7N, 46.7E) sits near row 261, column 187. In November, mid-afternoon there
    # (13 UTC, hour 1 of the 12z run) is warm, and the polar row is far colder.
    riyadh = grid[round((90 - 24.7) / 0.25), round(46.7 / 0.25)]
    assert 15 < riyadh < 40
    assert grid[0].mean() < riyadh - 20
