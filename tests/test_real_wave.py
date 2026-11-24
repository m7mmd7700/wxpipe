"""GFS-Wave on real data: decoded tiles against each raw message's own values, land missing, and the
grid's orientation at a known sea point and a known land point.

It reads the sample of run 2026112412 cached under work/gfswave/ in the layout of `run.Fetcher`, both
fields' messages at the hours 0, 1, 6, 7, 120, 123, 126, 381 and 384, and skips when that sample is not
on disk, as on the runner. The expected values come from the raw messages through ecCodes, its own
latitudes, longitudes and missing value, never from `grib.decode`'s array; the fields are stored in the
messages' own units, m and s.
"""

from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, ecmwf, gfs, tiles, wxt
from wxpipe.quantize import MISSING
from wxpipe.run import Fetcher, build_instant, parse_cycle, write_field

ROOT = Path(__file__).resolve().parents[1]
CYCLE = "2026112412"
SAMPLE = ROOT / "work" / "gfswave" / CYCLE
SAMPLE_HOURS = (0, 1, 6, 7, 120, 123, 126, 381, 384)
HOURS = (0, 123, 384)
FIELDS = [f.name for f in config.GFSWAVE.fields]
SEA = (15.0, 65.0)          # the Arabian Sea
RIYADH = (24.75, 46.75)     # land

pytestmark = pytest.mark.skipif(not all((SAMPLE / f"f{h:03d}.idx").exists() for h in SAMPLE_HOURS),
                                reason="GFS-Wave sample not downloaded")


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    """A cached message that is missing fails the test instead of being downloaded."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setattr(ecmwf, "http_get", refuse)


def fetcher() -> Fetcher:
    return Fetcher(config.GFSWAVE, parse_cycle(CYCLE), ROOT / "work")


def index(hours) -> dict:
    f = fetcher()
    return {h: f.idx(h) for h in hours}


def raw_message(idx: dict, field: config.Field, h: int) -> bytes:
    record = gfs.instant_record(idx[h], field.grib, h)
    return (SAMPLE / f"f{h:03d}" / f"{record.number}.grib2").read_bytes()


def raw_on_shared_grid(message: bytes) -> np.ndarray:
    """The message's values placed on the shared grid by ecCodes' own latitudes and longitudes, its
    missing value, land and ice, as NaN."""
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        values = np.asarray(eccodes.codes_get_values(handle), dtype=np.float64)
        lats = np.asarray(eccodes.codes_get_array(handle, "latitudes"), dtype=np.float64)
        lons = np.asarray(eccodes.codes_get_array(handle, "longitudes"), dtype=np.float64)
        assert eccodes.codes_get(handle, "bitmapPresent") == 1
        values[values == eccodes.codes_get(handle, "missingValue")] = np.nan
    finally:
        eccodes.codes_release(handle)
    rows = np.rint((config.LAT0 - lats) / config.DEG).astype(int)
    cols = np.rint(((lons - config.LON0) % 360.0) / config.DEG).astype(int) % config.NX
    grid = np.full((config.NY, config.NX), np.inf)   # inf: a cell that got no value
    grid[rows, cols] = values
    assert not np.isinf(grid).any(), "a cell of the shared grid got no value"
    return grid


def nearest(message: bytes, lat: float, lon: float) -> tuple[float, float, float]:
    """ecCodes' nearest grid point to (lat, lon) in the raw message: its latitude, longitude and value."""
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        point = eccodes.codes_grib_find_nearest(handle, lat, lon)[0]
    finally:
        eccodes.codes_release(handle)
    return float(point["lat"]), float(point["lon"]), float(point["value"])


def cell(lat: float, lon: float) -> tuple[int, int]:
    return round((config.LAT0 - lat) / config.DEG), round(((lon - config.LON0) % 360.0) / config.DEG) % config.NX


def built(tmp_path: Path, name: str, h: int) -> tuple[config.Field, dict, np.ndarray]:
    """The field's one frame at hour [h] built and written under [tmp_path], with the index and the
    raw message's values on the shared grid."""
    field = config.GFSWAVE.field(name)
    idx = index((h,))
    expected = raw_on_shared_grid(raw_message(idx, field, h))
    frames = build_instant(config.GFSWAVE, field, parse_cycle(CYCLE), (h,), idx, fetcher())
    write_field(tmp_path, config.GFSWAVE, field, CYCLE, frames)
    return field, idx, expected


def tile_byte(site: Path, field: config.Field, row: int, col: int) -> int:
    """The byte the written tiles hold at a shared-grid cell, from the finest level that has the cell."""
    target = row * config.NX + col
    numbers = np.arange(config.NY * config.NX).reshape(config.NY, config.NX)
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            hit = np.argwhere(tile.cut(numbers) == target)
            if len(hit):
                _, frames = wxt.read(site / field.name / tile.size.path / f"{tile.path}.wxt")
                j, i = hit[0]
                return int(frames[0][1][j, i])
    raise AssertionError(f"no tile holds row {row}, column {col}")


@pytest.mark.parametrize("h", HOURS)
@pytest.mark.parametrize("name", FIELDS)
def test_every_tile_equals_the_raw_message_and_land_is_missing(name, h, tmp_path):
    # A11: every level's tiles, every present cell within half a table step, every missing cell 255.
    field, _, expected = built(tmp_path, name, h)
    present = np.isfinite(expected)
    # None of the sample's values lies outside the table (brief §3.4 S4).
    assert field.lut.min_value <= float(expected[present].min()) and float(expected[present].max()) <= field.lut.max_value
    checked = missing = 0
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            header, frames = wxt.read(tmp_path / field.name / tile.size.path / f"{tile.path}.wxt")
            assert header["source"] == "gfswave" and header["release"] == CYCLE and header["units"] == field.units
            data = frames[0][1]
            want = tile.cut(expected)
            here = np.isfinite(want)
            assert np.all(data[~here] == MISSING)
            values = field.lut.decode(data[here])
            steps = np.vectorize(field.lut.step, otypes=[float])(want[here])   # a tile may be all land
            assert np.all(np.abs(values - want[here]) <= steps / 2 + 1e-6)
            assert not np.any(data[here] == MISSING)
            checked += int(here.sum())
            missing += int((~here).sum())
    assert checked > 100_000 and missing > 100_000


@pytest.mark.parametrize("h", HOURS)
@pytest.mark.parametrize("name", FIELDS)
def test_the_orientation_at_the_arabian_sea_and_riyadh(name, h, tmp_path):
    # A11: the sea point equals ecCodes' nearest point; Riyadh, land, is missing.
    field, idx, _ = built(tmp_path, name, h)
    message = raw_message(idx, field, h)
    lat, lon, raw = nearest(message, *SEA)
    assert (lat, lon) == pytest.approx(SEA, abs=1e-4) and raw != 9999   # ecCodes' longitudes carry float noise
    got = field.lut.decode(np.uint8(tile_byte(tmp_path, field, *cell(lat, lon))))
    assert abs(float(got) - raw) <= field.lut.step(raw) / 2 + 1e-6, (got, raw)
    lat, lon, raw = nearest(message, *RIYADH)
    assert (lat, lon) == pytest.approx(RIYADH, abs=1e-4) and raw == 9999
    assert tile_byte(tmp_path, field, *cell(lat, lon)) == MISSING
