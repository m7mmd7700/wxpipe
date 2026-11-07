"""Unit tests that need no network and no GRIB data."""

from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, gfs, precip, tiles, wxt
from wxpipe.quantize import MISSING, Lut

FIXTURES = Path(__file__).parent / "fixtures"


# ---- quantisation ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", config.FIELDS, ids=lambda f: f.name)
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
    assert len(config.FORECAST_HOURS) == 209
    assert config.FORECAST_HOURS[120] == 120 and config.FORECAST_HOURS[121] == 123
    assert config.FORECAST_HOURS[-1] == 384


def test_the_cheap_check_agrees_with_the_config():
    from wxpipe import check

    assert check.LAST_HOUR == config.FORECAST_HOURS[-1]
    assert check.SOURCE == config.SOURCE
    assert check.is_new("2026110712", None)
    assert check.is_new("2026110712", {"sources": {"gfs": {"release": "2026110706"}}})
    assert not check.is_new("2026110712", {"sources": {"gfs": {"release": "2026110712"}}})


def test_the_cheap_check_imports_no_third_party_package():
    import ast

    source = (Path(__file__).resolve().parents[1] / "wxpipe" / "gfs.py").read_text(encoding="utf-8")
    source += (Path(__file__).resolve().parents[1] / "wxpipe" / "check.py").read_text(encoding="utf-8")
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not imported & {"numpy", "eccodes"}


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
