"""The two-part source description: URLs, run discovery, GFS's values, and the delivered grid's check."""

from datetime import datetime, timezone

import pytest

from wxpipe import check, config, gfs, grib, source

CYCLE = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def test_gfs_urls_are_todays():
    base = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261107/12/atmos/gfs.t12z.pgrb2.0p25.f007"
    assert source.file_url(source.GFS, CYCLE, 7) == base
    assert source.idx_url(source.GFS, CYCLE, 7) == base + ".idx"
    assert source.idx_url(source.GFS, datetime(2026, 1, 2, 0, tzinfo=timezone.utc), 384) == (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260102/00/atmos/gfs.t00z.pgrb2.0p25.f384.idx")
    # gfs.py takes its URLs from the description.
    assert gfs.file_url(CYCLE, 7) == base
    assert gfs.idx_url(CYCLE, 7) == base + ".idx"


def test_the_newest_complete_cycle():
    now = datetime(2026, 11, 8, 3, 41, tzinfo=timezone.utc)   # the newest 6-hourly cycle is 2026110800
    asked = []

    def exists_from(published):
        def exists(url):
            asked.append(url)
            return url in published
        return exists

    def idx(cycle):
        return source.idx_url(source.GFS, cycle, 384)

    older = datetime(2026, 11, 7, 6, tzinfo=timezone.utc)
    assert source.latest_complete_cycle(source.GFS, now, exists_from({idx(older), idx(CYCLE)})) == CYCLE
    assert asked[:2] == [idx(datetime(2026, 11, 8, 0, tzinfo=timezone.utc)), idx(datetime(2026, 11, 7, 18, tzinfo=timezone.utc))]

    # Looked back six cycles, to 2026110618, and no further.
    asked.clear()
    oldest = datetime(2026, 11, 6, 18, tzinfo=timezone.utc)
    assert source.latest_complete_cycle(source.GFS, now, exists_from({idx(oldest)})) == oldest
    assert source.latest_complete_cycle(source.GFS, now, exists_from({idx(datetime(2026, 11, 6, 12, tzinfo=timezone.utc))})) is None
    assert source.latest_complete_cycle(source.GFS, now, exists_from(set())) is None
    assert len(asked) == 6 + 6 + 6

    # Another last hour, as the local form of run.py asks with --hours.
    asked.clear()
    assert source.latest_complete_cycle(source.GFS, now, exists_from(set()), last_hour=7) is None
    assert asked[0].endswith(".f007.idx")


def test_gfs_description_holds_todays_values():
    s = config.GFS
    assert s.info is source.GFS
    assert (s.info.name, s.info.last_hour, s.info.cycle_hours, s.info.lookback_cycles) == ("gfs", 384, 6, 6)
    assert s.info.base_url == "https://noaa-gfs-bdp-pds.s3.amazonaws.com" and s.info.idx_suffix == ".idx"
    assert len(s.forecast_hours) == 209 and s.forecast_hours[-1] == 384
    assert s.forecast_hours == tuple(range(0, 121)) + tuple(range(123, 385, 3))
    assert [(f.name, f.grib, f.kind) for f in s.fields] == [
        ("temperature.2m", "TMP:2 m above ground", "instant"),
        ("precipitation.sfc", "APCP:surface", "precip_rate"),
        ("wind_u.10m", "UGRD:10 m above ground", "instant"),
        ("wind_v.10m", "VGRD:10 m above ground", "instant"),
        ("pressure.msl", "PRMSL:mean sea level", "instant"),
        ("cloud.total", "TCDC:entire atmosphere", "instant"),
    ]
    tables = {f.name: (f.lut.breakpoints, f.units, f.scale, f.offset) for f in s.fields}
    assert tables == {
        "temperature.2m": (((0, -70.0), (20, -30.0), (230, 50.0), (254, 60.0)), "C", 1.0, -273.15),
        "precipitation.sfc": (((0, 0.0), (60, 1.2), (140, 8.0), (200, 30.0), (254, 150.0)), "mm hr**-1", 1.0, 0.0),
        "wind_u.10m": (((0, -80.0), (20, -40.0), (234, 40.0), (254, 80.0)), "m s**-1", 1.0, 0.0),
        "wind_v.10m": (((0, -80.0), (20, -40.0), (234, 40.0), (254, 80.0)), "m s**-1", 1.0, 0.0),
        "pressure.msl": (((0, 870.0), (20, 950.0), (234, 1050.0), (254, 1090.0)), "hPa", 0.01, 0.0),
        "cloud.total": (((0, 0.0), (254, 100.0)), "%", 1.0, 0.0),
    }
    assert s.grid == config.Grid(1440, 721, 90.0, 0.0, 0.25, 0.25, 0, 0)
    assert (config.NX, config.NY, config.LAT0, config.LON0, config.DEG) == (1440, 721, 90.0, 0.0, 0.25)
    assert s.attribution == "NOAA NCEP Global Forecast System (GFS). Public domain."
    assert (s.parse_idx, s.instant_record, s.accumulation_records, s.read) == (
        gfs.parse_idx, gfs.instant_record, gfs.accumulation_records, gfs.read)
    assert config.source("gfs") is s
    with pytest.raises(KeyError):
        config.source("nothing")


def test_both_parts_list_the_same_sources():
    assert [i.name for i in source.INFOS] == [s.info.name for s in config.SOURCES]
    assert all(i is s.info for i, s in zip(source.INFOS, config.SOURCES))
    # check.py takes the last hour from the standard-library part, through source.INFOS.
    assert check.source.INFOS is source.INFOS
    assert source.info("gfs").last_hour == config.GFS.forecast_hours[-1]


GOOD = dict(ni=1440, nj=721, lat0=90.0, lon0=0.0, di=0.25, dj=0.25, j_scans_positively=0, i_scans_negatively=0)


def test_check_grid_passes_gfs_own_values():
    grib.check_grid(config.GFS, **GOOD)
    grib.check_grid(config.GFS, **dict(GOOD, lat0=90.0 + 5e-7, di=0.25 - 5e-7))


@pytest.mark.parametrize("key, value, name", [
    ("ni", 1439, "Ni"),
    ("nj", 361, "Nj"),
    ("lat0", -90.0, "latitudeOfFirstGridPointInDegrees"),
    ("lon0", 180.0, "longitudeOfFirstGridPointInDegrees"),
    ("di", 0.5, "iDirectionIncrementInDegrees"),
    ("dj", 0.25 + 1e-5, "jDirectionIncrementInDegrees"),
    ("j_scans_positively", 1, "jScansPositively"),
    ("i_scans_negatively", 1, "iScansNegatively"),
])
def test_check_grid_refuses_each_differing_value(key, value, name):
    with pytest.raises(ValueError, match=name) as e:
        grib.check_grid(config.GFS, **dict(GOOD, **{key: value}))
    assert str(e.value).count("(expected") == 1


def test_decode_refuses_a_grid_that_is_not_the_shared_one():
    import numpy as np

    shared = np.zeros((config.NY, config.NX))
    assert grib.on_shared_grid(shared) is shared
    for shape in ((361, 720), (config.NX, config.NY), (config.NY, config.NX + 1)):
        with pytest.raises(ValueError, match="not the shared"):
            grib.on_shared_grid(np.zeros(shape))


def test_check_grid_names_every_differing_value():
    with pytest.raises(ValueError) as e:
        grib.check_grid(config.GFS, **dict(GOOD, ni=1439, lon0=180.0))
    assert "Ni 1439" in str(e.value) and "longitudeOfFirstGridPointInDegrees 180.0" in str(e.value)
