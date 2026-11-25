"""ECMWF IFS on real data: decoded tiles against each raw message's own values, the grid's orientation by
ecCodes' coordinates, and the precipitation rates.

It reads the sample of run 2026112312 cached under work/ifs/ in the layout of `run.Fetcher`, steps 0, 3,
6, 9, 144, 150, 156, 354 and 360, and skips when that sample is not on disk, as on the runner. The
expected values come from the raw messages through ecCodes, with the units read from the sample (brief
§3.4: K, m, m/s, Pa, 0 to 1), never from `grib.decode`'s array or a field's scale.
"""

from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, ecmwf, gfs, tiles, wxt
from wxpipe.run import Fetcher, build_instant, build_precip, parse_cycle, write_field

ROOT = Path(__file__).resolve().parents[1]
CYCLE = "2026112312"
SAMPLE = ROOT / "work" / "ifs" / CYCLE
SAMPLE_HOURS = (0, 3, 6, 9, 144, 150, 156, 354, 360)

pytestmark = pytest.mark.skipif(not all((SAMPLE / f"f{h:03d}.idx").exists() for h in SAMPLE_HOURS),
                                reason="IFS sample not downloaded")

# The raw message's value to the stored value, by the sample's units.
TO_STORED = {
    "temperature.2m": lambda v: v - 273.15,     # K
    "wind_u.10m": lambda v: v,                  # m/s
    "wind_v.10m": lambda v: v,
    "pressure.msl": lambda v: v / 100.0,        # Pa
    "cloud.total": lambda v: v * 100.0,         # 0 to 1
}
MM_PER_M = 1000.0                               # tp in m
RIYADH = (24.7, 46.7)
PACIFIC = (35.0, -140.0)                        # east of 180


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    """A cached message that is missing fails the test instead of being downloaded."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setattr(ecmwf, "http_get", refuse)


def fetcher() -> Fetcher:
    return Fetcher(config.IFS, parse_cycle(CYCLE), ROOT / "work")


def index(hours) -> dict:
    f = fetcher()
    return {h: f.idx(h) for h in hours}


def raw_message(idx: dict, field: config.Field, h: int) -> bytes:
    if field.kind == "precip_rate":
        record = ecmwf.accumulation_records(idx[h], field.grib, h)[(0, h)]
    else:
        record = ecmwf.instant_record(idx[h], field.grib, h)
    return (SAMPLE / f"f{h:03d}" / f"{record.number}.grib2").read_bytes()


def raw_on_shared_grid(message: bytes) -> np.ndarray:
    """The message's values placed on the shared grid by ecCodes' own latitudes and longitudes."""
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        values = np.asarray(eccodes.codes_get_values(handle), dtype=np.float64)
        lats = np.asarray(eccodes.codes_get_array(handle, "latitudes"), dtype=np.float64)
        lons = np.asarray(eccodes.codes_get_array(handle, "longitudes"), dtype=np.float64)
    finally:
        eccodes.codes_release(handle)
    rows = np.rint((config.LAT0 - lats) / config.DEG).astype(int)
    cols = np.rint(((lons - config.LON0) % 360.0) / config.DEG).astype(int) % config.NX
    grid = np.full((config.NY, config.NX), np.nan)
    grid[rows, cols] = values
    assert np.isfinite(grid).all(), "a cell of the shared grid got no value"
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


def tile_value(site: Path, field: config.Field, row: int, col: int, frame: int = 0) -> float:
    """The value the written tiles hold at a shared-grid cell, from the finest level that has the cell."""
    target = row * config.NX + col
    numbers = np.arange(config.NY * config.NX).reshape(config.NY, config.NX)
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            hit = np.argwhere(tile.cut(numbers) == target)
            if len(hit):
                _, frames = wxt.read(site / field.name / tile.size.path / f"{tile.path}.wxt")
                j, i = hit[0]
                return float(field.lut.decode(frames[frame][1])[j, i])
    raise AssertionError(f"no tile holds row {row}, column {col}")


def assert_region_tiles_match(site: Path, field: config.Field, expected: np.ndarray, frame: int = 0) -> int:
    """Every region tile's values against [expected] within half a table step; returns the cells checked."""
    checked = 0
    for tile in tiles.tiles_for(config.LEVELS[0]):
        header, frames = wxt.read(site / field.name / tile.size.path / f"{tile.path}.wxt")
        assert header["source"] == "ifs" and header["release"] == CYCLE
        values = field.lut.decode(frames[frame][1])
        want = tile.cut(expected)
        inside = (want >= field.lut.min_value) & (want <= field.lut.max_value)
        steps = np.vectorize(field.lut.step)(want[inside])
        assert np.all(np.abs(values[inside] - want[inside]) <= steps / 2 + 1e-6)
        checked += int(inside.sum())
    return checked


def daytime_hour() -> int:
    """The first sample step whose valid time is 06 to 12 UTC, daytime at Riyadh."""
    run_hour = int(CYCLE[-2:])
    return next(h for h in SAMPLE_HOURS if 6 <= (run_hour + h) % 24 <= 12)


# The instant fields that read their own record at every step: the two tests below build the daytime step
# alone, step 0 for the 12 UTC run, which the gust never reads (M6 S16, D2); the gust's own tests follow.
INSTANT = [f.name for f in config.IFS.fields if f.kind == "instant" and f.first_frame == "own"]


@pytest.mark.parametrize("name", INSTANT)
def test_region_tiles_equal_the_raw_message(name, tmp_path):
    field = config.IFS.field(name)
    h = daytime_hour()
    idx = index((h,))
    expected = TO_STORED[name](raw_on_shared_grid(raw_message(idx, field, h)))
    frames = build_instant(config.IFS, field, parse_cycle(CYCLE), (h,), idx, fetcher())
    write_field(tmp_path, config.IFS, field, CYCLE, frames)
    assert assert_region_tiles_match(tmp_path, field, expected) > 40_000


@pytest.mark.parametrize("name", INSTANT)
def test_the_orientation_at_riyadh_and_east_of_180(name, tmp_path):
    field = config.IFS.field(name)
    h = daytime_hour()
    idx = index((h,))
    message = raw_message(idx, field, h)
    frames = build_instant(config.IFS, field, parse_cycle(CYCLE), (h,), idx, fetcher())
    write_field(tmp_path, config.IFS, field, CYCLE, frames)
    for lat, lon in (RIYADH, PACIFIC):
        near_lat, near_lon, raw = nearest(message, lat, lon)
        want = TO_STORED[name](raw)
        got = tile_value(tmp_path, field, *cell(near_lat, near_lon))
        assert abs(got - want) <= field.lut.step(want) / 2 + 1e-6, (lat, lon, got, want)
        if name == "temperature.2m" and (lat, lon) == RIYADH:
            assert 10 < want < 40


@pytest.mark.parametrize("interval", [(3, 6), (144, 150)])
def test_the_rate_is_the_difference_of_the_totals(interval, tmp_path):
    field = config.IFS.field("precipitation.sfc")
    a, b = interval
    idx = index(interval)
    before = raw_on_shared_grid(raw_message(idx, field, a))
    after = raw_on_shared_grid(raw_message(idx, field, b))
    expected = np.maximum((after - before) * MM_PER_M / (b - a), 0.0)
    report: dict = {}
    frames = build_precip(config.IFS, field, parse_cycle(CYCLE), interval, idx, fetcher(), "bucket", report)
    write_field(tmp_path, config.IFS, field, CYCLE, frames)
    assert assert_region_tiles_match(tmp_path, field, expected, frame=1) > 40_000
    if interval == (3, 6):
        # The largest 3-hour rate on the globe is a plausible one, so the scale is mm, not m.
        assert 1.0 < float(expected.max()) < 300.0


class RecordingFetcher(Fetcher):
    """The sample's fetcher, keeping every (hour, record) whose message the build asks for."""

    def __init__(self):
        super().__init__(config.IFS, parse_cycle(CYCLE), ROOT / "work")
        self.asked = []

    def messages(self, items):
        self.asked.extend(items)
        return super().messages(items)


@pytest.mark.parametrize("h, key", [(3, "10fg:sfc"), (144, "10fg3:sfc"), (360, "10fg:sfc")])
def test_gust_tiles_equal_the_raw_message_at_every_level_and_the_orientation(h, key, tmp_path):
    # M6 S16, A19: the gust in m/s as delivered, its record found by the build's own rule, grib then
    # alternates; step 144 holds 10fg3 alone. Every level within half a table step, then Riyadh and a
    # point east of 180 against ecCodes' nearest point.
    from wxpipe import run

    field = config.IFS.field("wind_gust.sfc")
    idx = index((h,))
    record = run.instant_record(config.IFS, field, idx[h], h)
    assert record.key == key and record.step == str(h)
    message = (SAMPLE / f"f{h:03d}" / f"{record.number}.grib2").read_bytes()
    expected = raw_on_shared_grid(message)
    frames = build_instant(config.IFS, field, parse_cycle(CYCLE), (h,), idx, fetcher())
    write_field(tmp_path, config.IFS, field, CYCLE, frames)
    checked = 0
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            header, decoded = wxt.read(tmp_path / field.name / tile.size.path / f"{tile.path}.wxt")
            assert header["source"] == "ifs" and header["values"] == config.GUST_LUT.header()
            values = field.lut.decode(decoded[0][1])
            want = tile.cut(expected)
            inside = (want >= field.lut.min_value) & (want <= field.lut.max_value)
            steps = np.vectorize(field.lut.step)(want[inside])
            assert np.all(np.abs(values[inside] - want[inside]) <= steps / 2 + 1e-6)
            checked += int(inside.sum())
    assert checked > 100_000
    for lat, lon in (RIYADH, PACIFIC):
        near_lat, near_lon, want = nearest(message, lat, lon)
        got = tile_value(tmp_path, field, *cell(near_lat, near_lon))
        assert abs(got - want) <= field.lut.step(want) / 2 + 1e-6, (lat, lon, got, want)


def test_gust_frame_0_is_frame_1_and_step_0_is_never_read():
    # M6 S16, A19: a build of the nine steps; step 0's constant message is not asked.
    field = config.IFS.field("wind_gust.sfc")
    recording = RecordingFetcher()
    frames = build_instant(config.IFS, field, parse_cycle(CYCLE), SAMPLE_HOURS, index(SAMPLE_HOURS), recording)
    assert sorted(h for h, _ in recording.asked) == list(SAMPLE_HOURS[1:])
    assert frames[0][1].tobytes() == frames[1][1].tobytes()
    assert (frames[1][1] != 255).any(), "frame 1 holds values, so frame 0's copy is no missing frame"
    # Frame 0 keeps the run's own time, three hours before frame 1's (the review's W2).
    run_time = int(parse_cycle(CYCLE).timestamp())
    assert frames[0][0] == run_time and frames[1][0] == run_time + 3 * 3600
    assert len(frames) == len(SAMPLE_HOURS)


def packing_step_mm(message: bytes) -> float:
    """The message's packing step, 2**binaryScaleFactor * 10**-decimalScaleFactor, in mm."""
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        e = eccodes.codes_get(handle, "binaryScaleFactor")
        d = eccodes.codes_get(handle, "decimalScaleFactor")
    finally:
        eccodes.codes_release(handle)
    return 2.0 ** e * 10.0 ** -d * MM_PER_M


def test_negative_rates_before_the_clamp_are_packing_noise(tmp_path):
    # tp is packed at 12 bits per message, so the difference of two totals carries up to one packing
    # step of each in either direction; the clamp leaves no negative value in the tiles.
    field = config.IFS.field("precipitation.sfc")
    idx = index(SAMPLE_HOURS)
    tp0 = next(r for r in idx[0] if r.key == field.grib and r.step == "0")   # read here only, never by the build
    totals = {h: raw_message(idx, field, h) for h in SAMPLE_HOURS if h}
    totals[0] = (SAMPLE / "f000" / f"{tp0.number}.grib2").read_bytes()
    for a, b in zip(SAMPLE_HOURS, SAMPLE_HOURS[1:]):
        difference = (raw_on_shared_grid(totals[b]) - raw_on_shared_grid(totals[a])) * MM_PER_M
        allowed = packing_step_mm(totals[a]) + packing_step_mm(totals[b])
        assert float(difference.min()) >= -allowed - 1e-9, (a, b, float(difference.min()), allowed)
    report: dict = {}
    frames = build_precip(config.IFS, field, parse_cycle(CYCLE), SAMPLE_HOURS, idx, fetcher(), "bucket", report)
    assert [e["interval"] for e in report["intervals"]] == [list(p) for p in zip(SAMPLE_HOURS, SAMPLE_HOURS[1:])]
    write_field(tmp_path, config.IFS, field, CYCLE, frames)
    for level in config.LEVELS:
        for tile in tiles.tiles_for(level):
            _, decoded = wxt.read(tmp_path / field.name / tile.size.path / f"{tile.path}.wxt")
            for _, data in decoded:
                values = field.lut.decode(data)
                assert not np.any(values[np.isfinite(values)] < 0)
