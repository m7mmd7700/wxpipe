"""The two-part source description: URLs, run discovery, GFS's values, and the delivered grid's check."""

from datetime import datetime, timezone

import pytest

from wxpipe import check, config, ecmwf, gfs, grib, source

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
    assert source.info("ifs").last_hour == config.IFS.forecast_hours[-1]
    # A3: GFS, then GFS-Wave, then IFS, in both parts.
    assert source.INFOS == (source.GFS, source.GFSWAVE, source.IFS)
    assert config.SOURCES == (config.GFS, config.GFSWAVE, config.IFS)
    assert config.IFS.info is source.IFS and config.source("ifs") is config.IFS
    assert config.GFSWAVE.info is source.GFSWAVE and config.source("gfswave") is config.GFSWAVE
    assert source.info("gfswave") is source.GFSWAVE
    assert source.info("gfswave").last_hour == config.GFSWAVE.forecast_hours[-1]


# ---- GFS-Wave ----------------------------------------------------------------------------------

def test_gfswave_urls():
    # A1: findings W1's path for a 00, a 12 and an 18 cycle at hours 0 and 384.
    root = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
    for cycle, day, hh in ((datetime(2026, 11, 24, 0, tzinfo=timezone.utc), "20261124", "00"),
                           (datetime(2026, 11, 24, 12, tzinfo=timezone.utc), "20261124", "12"),
                           (datetime(2026, 11, 22, 18, tzinfo=timezone.utc), "20261122", "18")):
        for hour in (0, 384):
            stem = f"{root}/gfs.{day}/{hh}/wave/gridded/gfswave.t{hh}z.global.0p25.f{hour:03d}.grib2"
            assert source.file_url(source.GFSWAVE, cycle, hour) == stem
            assert source.idx_url(source.GFSWAVE, cycle, hour) == stem + ".idx"
    assert source.file_url(source.GFSWAVE, datetime(2026, 11, 24, 12, tzinfo=timezone.utc), 0) == (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261124/12/wave/gridded/gfswave.t12z.global.0p25.f000.grib2")
    assert source.GFSWAVE.file_suffix == "" and source.GFSWAVE.copies == ()
    assert source.roots(source.GFSWAVE) == (source.GFS.base_url,)
    # GFS's URLs are today's.
    assert source.file_url(source.GFS, CYCLE, 7) == (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261107/12/atmos/gfs.t12z.pgrb2.0p25.f007")


def test_gfswave_newest_complete_cycle():
    # A2: 6-hourly, by f384's index, six cycles back, none when none exists.
    now = datetime(2026, 11, 24, 15, 41, tzinfo=timezone.utc)   # the newest 6-hourly cycle is 2026112412
    asked = []

    def exists_from(published):
        def exists(url):
            asked.append(url)
            return url in published
        return exists

    def idx(day, hour):
        return source.idx_url(source.GFSWAVE, datetime(2026, 11, day, hour, tzinfo=timezone.utc), 384)

    assert source.latest_complete_cycle(source.GFSWAVE, now, exists_from({idx(24, 6), idx(24, 0)})) == datetime(
        2026, 11, 24, 6, tzinfo=timezone.utc)
    assert asked == [idx(24, 12), idx(24, 6)]
    assert all("/wave/gridded/gfswave." in url and url.endswith(".global.0p25.f384.grib2.idx") for url in asked)
    asked.clear()
    oldest = idx(23, 6)   # six cycles back: 24 12, 24 06, 24 00, 23 18, 23 12, 23 06
    assert source.latest_complete_cycle(source.GFSWAVE, now, exists_from({oldest})) == datetime(
        2026, 11, 23, 6, tzinfo=timezone.utc)
    assert source.latest_complete_cycle(source.GFSWAVE, now, exists_from({idx(23, 0)})) is None
    assert source.latest_complete_cycle(source.GFSWAVE, now, exists_from(set())) is None
    assert len(asked) == 6 + 6 + 6


def test_gfswave_description_holds_its_values():
    # A3.
    s = config.GFSWAVE
    assert (s.info.name, s.info.last_hour, s.info.cycle_hours, s.info.lookback_cycles) == ("gfswave", 384, 6, 6)
    assert (s.info.base_url, s.info.idx_suffix) == ("https://noaa-gfs-bdp-pds.s3.amazonaws.com", ".idx")
    assert s.forecast_hours == config.GFS.forecast_hours and len(s.forecast_hours) == 209
    assert s.grid == config.GFS.grid == config.Grid(1440, 721, 90.0, 0.0, 0.25, 0.25, 0, 0)
    assert [(f.name, f.units, f.grib, f.kind, f.scale, f.offset) for f in s.fields] == [
        ("wave_height.sfc", "m", "HTSGW:surface", "instant", 1.0, 0.0),
        ("wave_period.sfc", "s", "PERPW:surface", "instant", 1.0, 0.0),
    ]
    assert [f.lut.breakpoints for f in s.fields] == [
        ((0, 0.0), (200, 10.0), (254, 20.0)),
        ((0, 0.0), (200, 20.0), (254, 30.0)),
    ]
    assert s.field("wave_height.sfc").lut is config.WAVE_HEIGHT_LUT
    assert s.field("wave_period.sfc").lut is config.WAVE_PERIOD_LUT
    assert (s.parse_idx, s.instant_record, s.accumulation_records, s.read) == (
        gfs.parse_idx, gfs.instant_record, gfs.accumulation_records, gfs.read_checked)
    assert s.workers is None and s.read_limit_seconds == 900 and not s.cache_one_cycle
    assert s.probes == (config.Probe("gfswave-htsgw-jpeg.grib2", 0.0, 8.27, 2.267598, missing=468208),)
    assert s.attribution_parts is None and s.reads_reset is None and s.reads_report is None
    assert s.attribution == ("NOAA NCEP GFS-Wave, the global 0.25 degree wave model of the Global Forecast "
                             "System. Public domain.")
    # F1: only GFS-Wave's job runs in a child process in the workflow's form, bound by 20 minutes.
    assert [(x.name, x.child_seconds) for x in config.SOURCES] == [("gfs", None), ("gfswave", 1200), ("ifs", None)]
    # Only IFS waits for GFS.
    assert [(x.name, x.waits_for_gfs) for x in config.SOURCES] == [("gfs", False), ("gfswave", False), ("ifs", True)]
    # IFS's probes record no missing count.
    assert all(p.missing is None for p in config.IFS.probes)
    # The sample's grid (brief §3.4 S3) passes; IFS's 180E start does not.
    grib.check_grid(config.GFSWAVE, **GOOD)
    with pytest.raises(ValueError, match="unexpected gfswave grid: longitudeOfFirstGridPointInDegrees 180.0"):
        grib.check_grid(config.GFSWAVE, **dict(GOOD, lon0=180.0))


# ---- IFS -------------------------------------------------------------------------------------

MIRROR = "https://data.ecmwf.int/forecasts"   # the first copy's ROOT, ECMWF's data portal
CLOUDFRONT = "https://d2zvc0wgha4k2l.cloudfront.net"
BUCKET = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"


def test_ifs_urls():
    # A1: the key path of findings E2, for a 00 and a 12 cycle at hours 0 and 360.
    c00 = datetime(2026, 11, 22, 0, tzinfo=timezone.utc)
    c12 = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    assert source.file_url(source.IFS, c00, 0) == f"{MIRROR}/20261122/00z/ifs/0p25/oper/20261122000000-0h-oper-fc.grib2"
    assert source.idx_url(source.IFS, c00, 0) == f"{MIRROR}/20261122/00z/ifs/0p25/oper/20261122000000-0h-oper-fc.index"
    assert source.file_url(source.IFS, c12, 360) == f"{MIRROR}/20261123/12z/ifs/0p25/oper/20261123120000-360h-oper-fc.grib2"
    assert source.idx_url(source.IFS, c12, 360) == f"{MIRROR}/20261123/12z/ifs/0p25/oper/20261123120000-360h-oper-fc.index"
    assert source.file_url(source.IFS, c00, 360).endswith("/20261122000000-360h-oper-fc.grib2")
    assert source.idx_url(source.IFS, c12, 0).endswith("/20261123120000-0h-oper-fc.index")
    # GFS's file has no suffix of its own.
    assert source.GFS.file_suffix == ""


def test_ifs_copies_and_their_urls():
    # B1: the three copies in order, the first the base URL; GFS has one.
    assert source.IFS.copies == (MIRROR, CLOUDFRONT, BUCKET)
    assert source.IFS.copies[0] == source.IFS.base_url
    assert source.roots(source.IFS) == source.IFS.copies
    assert source.GFS.copies == () and source.roots(source.GFS) == (source.GFS.base_url,)
    path = "/20261123/12z/ifs/0p25/oper/20261123120000-360h-oper-fc"
    c12 = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    for suffix, url in ((".grib2", source.file_url(source.IFS, c12, 360)), (".index", source.idx_url(source.IFS, c12, 360))):
        assert url == MIRROR + path + suffix
        assert [source.on_root(source.IFS, url, root) for root in source.roots(source.IFS)] == [
            MIRROR + path + suffix, CLOUDFRONT + path + suffix, BUCKET + path + suffix]
    with pytest.raises(ValueError, match="is not on"):
        source.on_root(source.IFS, BUCKET + path + ".index", CLOUDFRONT)
    with pytest.raises(ValueError, match="is not on"):
        source.on_root(source.IFS, "https://example.invalid/a.index", MIRROR)
    gfs_url = source.idx_url(source.GFS, CYCLE, 7)
    assert source.on_root(source.GFS, gfs_url, source.GFS.base_url) == gfs_url


# ---- M5: discovery over the copies ----------------------------------------------------------------

def recording(answers: dict):
    """An existence test that answers by each URL's ROOT, from [answers]: True, False or an exception."""
    asked = []

    def exists(url):
        asked.append(url)
        for root, answer in answers.items():
            if url.startswith(root):
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError(f"no answer for {url}")

    exists.asked = asked
    return exists


def test_exists_on_copies_the_first_definite_answer_decides():
    # B2.
    url = source.idx_url(source.IFS, datetime(2026, 11, 23, 12, tzinfo=timezone.utc), 360)
    path = url[len(MIRROR):]
    for answer in (True, False):
        exists = recording({MIRROR: answer, CLOUDFRONT: AssertionError("asked"), BUCKET: AssertionError("asked")})
        assert source.exists_on_copies(source.IFS, exists)(url) is answer
        assert exists.asked == [url]
        exists = recording({MIRROR: ConnectionError("reset"), CLOUDFRONT: TimeoutError("t"), BUCKET: answer})
        assert source.exists_on_copies(source.IFS, exists)(url) is answer
        assert exists.asked == [MIRROR + path, CLOUDFRONT + path, BUCKET + path]
    exists = recording({MIRROR: ConnectionError("reset"), CLOUDFRONT: TimeoutError("t"), BUCKET: OSError("o")})
    with pytest.raises(ConnectionError, match="reset"):
        source.exists_on_copies(source.IFS, exists)(url)
    assert len(exists.asked) == 3


def test_exists_on_copies_passes_over_read_errors_only():
    # F6 (a): an OSError or an http.client.HTTPException is passed over; anything else is raised at once.
    import http.client

    url = source.idx_url(source.IFS, datetime(2026, 11, 23, 12, tzinfo=timezone.utc), 360)
    exists = recording({MIRROR: http.client.RemoteDisconnected("closed"), CLOUDFRONT: True, BUCKET: False})
    assert source.exists_on_copies(source.IFS, exists)(url) is True
    exists = recording({MIRROR: http.client.IncompleteRead(b"x"), CLOUDFRONT: False, BUCKET: True})
    assert source.exists_on_copies(source.IFS, exists)(url) is False
    exists = recording({MIRROR: RuntimeError("a bug"), CLOUDFRONT: True, BUCKET: True})
    with pytest.raises(RuntimeError, match="a bug"):
        source.exists_on_copies(source.IFS, exists)(url)
    assert exists.asked == [url]


def test_exists_on_copies_prints_each_copy_passed_over(capsys):
    # F6 (b): one line per copy passed over, for a source of several copies; none for GFS's one.
    url = source.idx_url(source.IFS, datetime(2026, 11, 23, 12, tzinfo=timezone.utc), 360)
    exists = recording({MIRROR: TimeoutError("timed out"), CLOUDFRONT: ConnectionError("reset"), BUCKET: True})
    assert source.exists_on_copies(source.IFS, exists)(url) is True
    assert capsys.readouterr().out.splitlines() == [
        "ifs discovery: data.ecmwf.int/forecasts passed over: TimeoutError: timed out",
        "ifs discovery: d2zvc0wgha4k2l.cloudfront.net passed over: ConnectionError: reset"]

    def raises(u):
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        source.exists_on_copies(source.GFS, raises)(source.idx_url(source.GFS, CYCLE, 384))
    assert capsys.readouterr().out == ""


def test_the_discovery_line_names_the_copy_that_decided_the_newest_cycle():
    # F6 (c).
    now = datetime(2026, 11, 24, 9, 41, tzinfo=timezone.utc)   # the newest 12-hourly cycle is 2026112400
    older = source.idx_url(source.IFS, datetime(2026, 11, 23, 12, tzinfo=timezone.utc), 360)

    def portal_false(url):
        if url.startswith(MIRROR):
            return url == older
        raise AssertionError("asked")

    on_copies = source.exists_on_copies(source.IFS, portal_false)
    source.latest_complete_cycle(source.IFS, now, on_copies)
    assert source.discovery_line(source.IFS, on_copies) == (
        "ifs discovery: the newest cycle decided by data.ecmwf.int/forecasts: false")

    on_copies = source.exists_on_copies(source.IFS, recording({MIRROR: OSError("o"), CLOUDFRONT: True, BUCKET: True}))
    source.latest_complete_cycle(source.IFS, now, on_copies)
    assert source.discovery_line(source.IFS, on_copies) == (
        "ifs discovery: the newest cycle decided by d2zvc0wgha4k2l.cloudfront.net: true")

    on_copies = source.exists_on_copies(source.IFS, recording({MIRROR: OSError("o"), CLOUDFRONT: OSError("o"),
                                                               BUCKET: TimeoutError("t")}))
    with pytest.raises(OSError):
        source.latest_complete_cycle(source.IFS, now, on_copies)
    assert source.discovery_line(source.IFS, on_copies) == "ifs discovery: the newest cycle: every copy raised"

    # Nothing for GFS's one copy, or before anything was asked.
    on_copies = source.exists_on_copies(source.GFS, lambda u: True)
    source.latest_complete_cycle(source.GFS, now, on_copies)
    assert source.discovery_line(source.GFS, on_copies) is None
    assert source.discovery_line(source.IFS, source.exists_on_copies(source.IFS, lambda u: True)) is None


def test_ifs_discovery_over_the_copies():
    # B2: a run found on the bucket when the portal raises; the older cycle when the portal says
    # "not there" for the newest and the later copies raise.
    now = datetime(2026, 11, 24, 9, 41, tzinfo=timezone.utc)   # the newest 12-hourly cycle is 2026112400
    c24, c23 = (datetime(2026, 11, 24, 0, tzinfo=timezone.utc), datetime(2026, 11, 23, 12, tzinfo=timezone.utc))
    newest = source.idx_url(source.IFS, c24, 360)[len(MIRROR):]
    older = source.idx_url(source.IFS, c23, 360)[len(MIRROR):]

    def portal_raises(url):
        if url.startswith(MIRROR) or url.startswith(CLOUDFRONT):
            raise ConnectionError("reset")
        return url == BUCKET + newest

    assert source.latest_complete_cycle(source.IFS, now, source.exists_on_copies(source.IFS, portal_raises)) == c24

    def later_copies_raise(url):
        if url.startswith(MIRROR):
            return url == MIRROR + older
        raise TimeoutError("timed out")

    assert source.latest_complete_cycle(source.IFS, now, source.exists_on_copies(source.IFS, later_copies_raise)) == c23


def test_gfs_discovery_answers_as_exists_alone():
    # B2: GFS's one copy.
    url = source.idx_url(source.GFS, CYCLE, 384)
    for answer in (True, False):
        assert source.exists_on_copies(source.GFS, lambda u: answer)(url) is answer

    def raises(u):
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        source.exists_on_copies(source.GFS, raises)(url)
    asked = []
    source.exists_on_copies(source.GFS, lambda u: asked.append(u) or False)(url)
    assert asked == [url]


def test_ifs_newest_complete_cycle():
    # A2: 12-hourly, by step 360's index, four cycles back.
    now = datetime(2026, 11, 24, 9, 41, tzinfo=timezone.utc)   # the newest 12-hourly cycle is 2026112400
    asked = []

    def exists_from(published):
        def exists(url):
            asked.append(url)
            return url in published
        return exists

    def idx(day, hour):
        return source.idx_url(source.IFS, datetime(2026, 11, day, hour, tzinfo=timezone.utc), 360)

    assert source.latest_complete_cycle(source.IFS, now, exists_from({idx(23, 12), idx(23, 0)})) == datetime(
        2026, 11, 23, 12, tzinfo=timezone.utc)
    assert asked == [idx(24, 0), idx(23, 12)]
    assert all(url.endswith("-360h-oper-fc.index") for url in asked)
    asked.clear()
    assert source.latest_complete_cycle(source.IFS, now, exists_from({idx(22, 12)})) == datetime(
        2026, 11, 22, 12, tzinfo=timezone.utc)
    assert source.latest_complete_cycle(source.IFS, now, exists_from({idx(22, 0)})) is None
    assert source.latest_complete_cycle(source.IFS, now, exists_from(set())) is None
    assert asked[-4:] == [idx(24, 0), idx(23, 12), idx(23, 0), idx(22, 12)]
    assert len(asked) == 4 + 4 + 4


def test_ifs_description_holds_its_values():
    # A3.
    s = config.IFS
    assert (s.info.name, s.info.last_hour, s.info.cycle_hours, s.info.lookback_cycles) == ("ifs", 360, 12, 4)
    assert (s.info.base_url, s.info.file_suffix, s.info.idx_suffix) == (MIRROR, ".grib2", ".index")
    assert len(s.forecast_hours) == 85
    assert s.forecast_hours == tuple(range(0, 145, 3)) + tuple(range(150, 361, 6))
    assert s.forecast_hours[48:50] == (144, 150) and s.forecast_hours[-1] == 360
    assert [(f.name, f.grib, f.kind) for f in s.fields] == [
        ("temperature.2m", "2t:sfc", "instant"),
        ("precipitation.sfc", "tp:sfc", "precip_rate"),
        ("wind_u.10m", "10u:sfc", "instant"),
        ("wind_v.10m", "10v:sfc", "instant"),
        ("pressure.msl", "msl:sfc", "instant"),
        ("cloud.total", "tcc:sfc", "instant"),
    ]
    # GFS's names, units and tables; each field's scale and offset from the sample's units (K, m, m/s, Pa, 0-1).
    assert [(f.name, f.units, f.lut.breakpoints) for f in s.fields] == [
        (f.name, f.units, f.lut.breakpoints) for f in config.GFS.fields]
    assert {f.name: (f.scale, f.offset) for f in s.fields} == {
        "temperature.2m": (1.0, -273.15),
        "precipitation.sfc": (1000.0, 0.0),
        "wind_u.10m": (1.0, 0.0),
        "wind_v.10m": (1.0, 0.0),
        "pressure.msl": (0.01, 0.0),
        "cloud.total": (100.0, 0.0),
    }
    assert s.grid == config.Grid(1440, 721, 90.0, 180.0, 0.25, 0.25, 0, 0)
    assert (s.parse_idx, s.instant_record, s.accumulation_records, s.read) == (
        ecmwf.parse_idx, ecmwf.instant_record, ecmwf.accumulation_records, ecmwf.read)
    assert s.workers == 2 and s.read_limit_seconds == 45 * 60 and s.cache_one_cycle
    assert s.probes == (config.Probe("ifs-2t-ccsds.grib2", 221.997971, 315.997971, 278.805962),
                        config.Probe("ifs-msl-ccsds.grib2", 95764.125000, 104952.125000, 100852.455598))
    assert [k for k, _ in s.attribution_parts] == [
        "licence", "licence_url", "copyright", "source", "credit", "liability", "modified"]
    # GFS reads with --workers, without a time limit, a kept cache or a probe.
    g = config.GFS
    assert (g.workers, g.read_limit_seconds, g.cache_one_cycle, g.probes, g.attribution_parts) == (None, None, False, (), None)
    assert g.attribution_for(2026) == g.attribution


def test_check_grid_passes_ifs_own_values():
    grib.check_grid(config.IFS, **dict(GOOD, lon0=180.0))
    with pytest.raises(ValueError, match="longitudeOfFirstGridPointInDegrees 0.0"):
        grib.check_grid(config.IFS, **GOOD)


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
