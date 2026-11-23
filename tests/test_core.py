"""Unit tests that need no network and no GRIB data."""

from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, gfs, precip, tiles, wxt
from wxpipe.quantize import MISSING, Lut

FIXTURES = Path(__file__).parent / "fixtures"


# ---- quantisation ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", config.GFS.fields, ids=lambda f: f.name)
def test_every_field_round_trips_within_half_a_step(field):
    rng = np.random.default_rng(7)
    values = rng.uniform(field.lut.min_value, field.lut.max_value, 20_000)
    back = field.lut.decode(field.lut.encode(values))
    tolerance = np.array([field.lut.step(v) for v in values]) / 2 + 1e-9
    assert np.all(np.abs(back - values) <= tolerance)


def test_missing_and_clipping():
    lut = Lut([(0, 0.0), (254, 100.0)])
    data = lut.encode([np.nan, -5.0, 0.0, 100.0, 1e9])
    assert data.tolist() == [MISSING, 0, 0, 254, 254]
    assert np.isnan(lut.decode([MISSING])[0])


def test_single_values_work_like_arrays():
    lut = Lut([(0, 0.0), (254, 100.0)])
    assert int(lut.encode(50.0)) == 127
    assert int(lut.encode(np.nan)) == MISSING
    assert float(lut.decode(np.uint8(127))) == pytest.approx(50.0, abs=0.2)
    assert np.isnan(float(lut.decode(np.uint8(MISSING))))


def test_bad_breakpoints_are_refused():
    for bad in ([(0, 0.0)], [(1, 0.0), (254, 1.0)], [(0, 1.0), (254, 0.0)], [(0, 0.0), (255, 1.0)]):
        with pytest.raises(ValueError):
            Lut(bad)


# ---- tiles ----------------------------------------------------------------------------------

def test_the_middle_east_box_is_twelve_full_resolution_tiles():
    region = tiles.tiles_for(config.LEVELS[0])
    assert sorted((t.i, t.j) for t in region) == [(i, j) for i in range(1, 5) for j in range(3, 6)]
    assert all((t.nx, t.ny) == (61, 61) for t in region)


def test_global_levels_cover_the_grid():
    counts = {lv.size: len(tiles.tiles_for(lv)) for lv in config.LEVELS[1:]}
    assert counts == {"120x120/2": 72, "240x240/4": 18, "480x480/8": 6}


def test_wrap_column_and_short_bottom_tiles():
    last = tiles.Tile(tiles.TileSize.parse("120x120/2"), 11, 0)
    assert last.cols()[-1] == 0                       # column 1440 is column 0 again
    bottom = tiles.Tile(tiles.TileSize.parse("480x480/8"), 0, 1)
    assert (bottom.y0, bottom.y1, bottom.ny) == (480, 720, 31)


def test_cut_matches_direct_indexing():
    grid = np.arange(config.NY * config.NX, dtype=np.int64).reshape(config.NY, config.NX)
    tile = tiles.Tile(tiles.TileSize.parse("240x240/4"), 5, 2)
    cut = tile.cut(grid)
    assert cut.shape == (tile.ny, tile.nx)
    assert cut[0, 0] == grid[480, 1200]
    assert cut[-1, -1] == grid[720, 0]
    assert cut[3, 7] == grid[480 + 12, 1200 + 28]


def test_tile_size_parsing():
    assert tiles.TileSize.parse("60x60") == tiles.TileSize(60, 60, 1)
    for bad in ("60x", "60x60/7", "0x60/1"):
        with pytest.raises(ValueError):
            tiles.TileSize.parse(bad)


# ---- WXT1 -----------------------------------------------------------------------------------

def test_wxt_round_trip_and_frame_index():
    rng = np.random.default_rng(1)
    frames = [(1_794_000_000 + 3600 * k, rng.integers(0, 256, (31, 61), dtype=np.uint8)) for k in range(5)]
    data = wxt.encode({"grid": {"nx": 61, "ny": 31}, "data": "x"}, frames)
    header, back = wxt.decode(data)
    assert header["wxt"] == 1 and header["format"] == "zlib"
    assert [t for t, _ in back] == [t for t, _ in frames]
    assert all(np.array_equal(a, b) for (_, a), (_, b) in zip(frames, back))
    offsets = [(o, n) for _, o, n in header["frames"]]
    assert all(o2 == o1 + n1 for (o1, n1), (o2, _) in zip(offsets, offsets[1:]))


def mixed_frames():
    """Frames like real fields (smooth, banded, constant with missing bytes) and one of noise."""
    rng = np.random.default_rng(3)
    y, x = np.mgrid[0:61, 0:61]
    smooth = (127 + 100 * np.sin(x / 9.0) * np.cos(y / 7.0)).astype(np.uint8)
    banded = ((x // 4 + y // 6) % 255).astype(np.uint8)
    flat = np.full((61, 61), 254, dtype=np.uint8)
    flat[20:30, 5:50] = MISSING
    noise = rng.integers(0, 256, (61, 61), dtype=np.uint8)
    return [(1_794_052_800 + 3600 * k, grid) for k, grid in enumerate((smooth, banded, flat, noise, smooth[::-1]))]


def split(data: bytes) -> tuple[dict, list[bytes]]:
    import json
    import struct

    (hlen,) = struct.unpack("<I", data[4:8])
    header = json.loads(data[8:8 + hlen].decode("utf-8"))
    base = 8 + hlen
    return header, [data[base + o: base + o + n] for _, o, n in header["frames"]]


def test_frames_inflate_with_zlib_to_the_same_bytes():
    import zlib

    frames = mixed_frames()
    header, streams = split(wxt.encode({"grid": {"nx": 61, "ny": 61}, "data": "x"}, frames))
    assert len(streams) == len(frames) == 5
    for (_, grid), stream in zip(frames, streams):
        assert zlib.decompress(stream) == grid.tobytes()
    assert sum(n for _, _, n in header["frames"]) == sum(map(len, streams))


def test_the_header_is_zlib_9s_but_for_the_frames_offsets_and_lengths():
    import json
    import zlib

    frames = mixed_frames()
    tile = {"source": "gfs", "release": "2026110712", "data": "x", "units": "C",
            "grid": {"level": "60x60/1", "i": 1, "j": 3, "grid": [60, 120, 180, 240], "steps": [1, 1], "nx": 61, "ny": 61},
            "values": {"scale": "lin", "breakpoints": [[0, 0.0], [254, 100.0]], "missing": 255}}
    # The header zlib level 9 gave before, built by the old rule.
    old_index, offset = [], 0
    for t, grid in frames:
        n = len(zlib.compress(grid.tobytes(), 9))
        old_index.append([t, offset, n])
        offset += n
    old = dict(tile, wxt=1, format="zlib", frames=old_index)

    new, _ = split(wxt.encode(tile, frames))
    assert list(new) == list(old)
    assert {k: v for k, v in new.items() if k != "frames"} == {k: v for k, v in old.items() if k != "frames"}
    assert new["wxt"] == 1 and new["format"] == "zlib"
    assert [t for t, _, _ in new["frames"]] == [t for t, _, _ in old["frames"]]
    assert [n for _, _, n in new["frames"]] != [n for _, _, n in old["frames"]]


def test_the_frames_are_libdeflates_level_12():
    import deflate

    frames = mixed_frames()
    _, streams = split(wxt.encode({"grid": {"nx": 61, "ny": 61}, "data": "x"}, frames))
    for (_, grid), stream in zip(frames, streams):
        assert stream == deflate.zlib_compress(grid.tobytes(), 12)


# ---- GFS index files ------------------------------------------------------------------------

def records(fhour: int):
    return gfs.parse_idx((FIXTURES / f"gfs.t12z.f{fhour:03d}.idx").read_text(encoding="utf-8"))


def test_instant_records_use_anl_at_hour_zero():
    assert gfs.instant_record(records(0), "TMP:2 m above ground", 0).fcst == "anl"
    assert gfs.instant_record(records(7), "TCDC:entire atmosphere", 7).fcst == "7 hour fcst"
    r = gfs.instant_record(records(384), "PRMSL:mean sea level", 384)
    assert r.end is not None and r.end > r.offset


def test_accumulation_windows():
    assert gfs.accumulation_window("6-7 hour acc fcst") == (6, 7)
    assert gfs.accumulation_window("0-5 day acc fcst") == (0, 120)
    assert gfs.accumulation_window("0-16 day acc fcst") == (0, 384)
    assert gfs.accumulation_window("6-7 hour ave fcst") is None
    assert gfs.accumulation_records(records(0), "APCP:surface", 0) == {}
    assert set(gfs.accumulation_records(records(6), "APCP:surface", 6)) == {(0, 6)}
    assert set(gfs.accumulation_records(records(7), "APCP:surface", 7)) == {(6, 7), (0, 7)}
    assert set(gfs.accumulation_records(records(120), "APCP:surface", 120)) == {(114, 120), (0, 120)}
    assert set(gfs.accumulation_records(records(384), "APCP:surface", 384)) == {(378, 384), (0, 384)}


def test_forecast_hours_cover_sixteen_days():
    assert len(config.GFS.forecast_hours) == 209
    assert config.GFS.forecast_hours[120] == 120 and config.GFS.forecast_hours[121] == 123
    assert config.GFS.forecast_hours[-1] == 384


def test_the_cheap_check_agrees_with_the_config(monkeypatch):
    from wxpipe import check, source

    def refuse(*args, **kwargs):
        raise AssertionError("a request was made")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)

    assert source.GFS.last_hour == config.GFS.forecast_hours[-1]
    assert source.GFS.name == config.GFS.info.name == "gfs"
    assert check.is_new("gfs", "2026110712", None)
    assert check.is_new("gfs", "2026110712", {"sources": {"gfs": {"release": "2026110706"}}})
    assert not check.is_new("gfs", "2026110712", {"sources": {"gfs": {"release": "2026110712"}}})


# The import test of the cheap check, which parsed gfs.py and check.py, is replaced by the subprocess
# test in tests/test_check.py, which blocks numpy and eccodes and imports every standard-library module.


# ---- the roll onto the shared grid (D9) ------------------------------------------------------

def test_roll_to_shared_on_a_small_grid():
    # A7: 8 columns of 45 degrees.
    from wxpipe import grib

    values = np.arange(16).reshape(2, 8)
    assert grib.roll_to_shared(values, 180.0, deg=45.0).tolist() == np.roll(values, 4, axis=1).tolist()
    ninety = grib.roll_to_shared(values, 90.0, deg=45.0)
    assert ninety[:, 0].tolist() == values[:, 6].tolist()          # the delivered column 6 lies at 0E
    assert ninety[:, 2].tolist() == values[:, 0].tolist()          # the delivered column 0 lies at 90E
    assert grib.roll_to_shared(values, 0.0, deg=45.0).tolist() == values.tolist()


def test_roll_to_shared_on_the_ifs_width():
    from wxpipe import grib

    row = np.arange(config.NX, dtype=np.float64).reshape(1, config.NX)
    rolled = grib.roll_to_shared(row, 180.0)
    assert rolled.tolist() == np.roll(row, 720, axis=1).tolist()
    assert rolled[0, 0] == 720.0          # the delivered column 720 lies at 0E
    assert rolled[0, 720] == 0.0          # the delivered first column lies at 180E
    assert grib.roll_to_shared(row, 0.0).tolist() == row.tolist()


class FakeCodes:
    """Stands in for eccodes in grib.decode: IFS's delivered grid, values 0, 1, 2, ... and no bitmap."""

    def __init__(self, src):
        g = src.grid
        self.keys = {"Ni": g.ni, "Nj": g.nj, "latitudeOfFirstGridPointInDegrees": g.lat0,
                     "longitudeOfFirstGridPointInDegrees": g.lon0, "iDirectionIncrementInDegrees": g.di,
                     "jDirectionIncrementInDegrees": g.dj, "jScansPositively": g.j_scans_positively,
                     "iScansNegatively": g.i_scans_negatively, "bitmapPresent": 0}
        self.released = []

    def codes_new_from_message(self, message):
        return "handle"

    def codes_get(self, handle, key):
        return self.keys[key]

    def codes_get_values(self, handle):
        return np.arange(self.keys["Ni"] * self.keys["Nj"], dtype=np.float64)

    def codes_release(self, handle):
        self.released.append(handle)


def test_decode_checks_rolls_then_checks_the_shape(monkeypatch):
    # A7: the order by a spy, with no real IFS decode (R9).
    from wxpipe import grib

    calls = []
    fake = FakeCodes(config.IFS)
    monkeypatch.setattr(grib, "eccodes", fake)
    for name in ("check_grid", "roll_to_shared", "on_shared_grid"):
        real = getattr(grib, name)
        monkeypatch.setattr(grib, name, lambda *a, _real=real, _name=name, **k: (calls.append(_name), _real(*a, **k))[1])
    values = grib.decode(b"message", config.IFS)
    assert calls == ["check_grid", "roll_to_shared", "on_shared_grid"]
    assert fake.released == ["handle"]
    delivered = np.arange(config.NX * config.NY, dtype=np.float64).reshape(config.NY, config.NX)
    assert values.shape == (config.NY, config.NX)
    assert np.array_equal(values, np.roll(delivered, 720, axis=1))
    assert values[0, 0] == 720.0
    # GFS's delivered grid is the shared one: no roll.
    monkeypatch.setattr(grib, "eccodes", FakeCodes(config.GFS))
    assert np.array_equal(grib.decode(b"message", config.GFS), delivered)


# ---- precipitation --------------------------------------------------------------------------

def grid(value):
    return np.full((2, 2), float(value))


def test_both_methods_agree_on_consistent_accumulations():
    # Hours 5 -> 6 -> 7: since-start totals 10, 12, 15 mm; the 6-7 bucket holds 3 mm.
    w5, w6, w7 = {(0, 5): grid(10)}, {(0, 6): grid(12)}, {(0, 7): grid(15), (6, 7): grid(3)}
    for method in ("bucket", "since-start"):
        assert precip.rate(6, 5, w6, w5, method)[0].tolist() == grid(2).tolist()
        assert precip.rate(7, 6, w7, w6, method)[0].tolist() == grid(3).tolist()


def test_three_hour_steps_and_bucket_differences():
    w120 = {(0, 120): grid(50), (114, 120): grid(4)}
    w123 = {(0, 123): grid(53), (120, 123): grid(3)}
    w126 = {(0, 126): grid(59), (120, 126): grid(9)}
    assert precip.rate(123, 120, w123, w120, "bucket")[0][0, 0] == pytest.approx(1.0)
    assert precip.rate(126, 123, w126, w123, "bucket")[0][0, 0] == pytest.approx(2.0)
    assert precip.rate(126, 123, w126, w123, "since-start")[0][0, 0] == pytest.approx(2.0)


def test_negative_rates_are_counted_and_clamped():
    result, negative = precip.rate(7, 6, {(0, 7): grid(9)}, {(0, 6): grid(10)}, "since-start")
    assert negative == 4 and result.max() == 0.0


def test_missing_accumulations_give_no_rate():
    assert precip.rate(1, 0, {}, {}, "bucket") == (None, 0)
    assert precip.rate(7, 6, {(6, 7): grid(1)}, {}, "since-start") == (None, 0)
