"""The probes of D11 and W4: the committed messages' real decode, skipped on the runner, the rules by a
fake decode, and the child process that run.py starts, with a fake subprocess.run."""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from wxpipe import config, ecmwf, gfs, grib, probe, run

ROOT = Path(__file__).resolve().parents[1]
ON_RUNNER = os.environ.get("GITHUB_ACTIONS") == "true"
# R9: the runner decodes no IFS or GFS-Wave message in a test; the probe before each job is the check there.
real_decode = pytest.mark.skipif(ON_RUNNER, reason="no real IFS or GFS-Wave decode in a test on the runner (R9)")


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(gfs, "http_get", refuse)
    monkeypatch.setattr(gfs, "exists", refuse)
    monkeypatch.setattr(ecmwf, "http_get", refuse)


def test_the_probe_messages_are_found_from_the_package():
    # R10, for every source's probes.
    assert probe.SAMPLES == Path(probe.__file__).parent / "samples"
    probes = [p for s in config.SOURCES for p in s.probes]
    assert [p.file for p in probes] == ["gfswave-htsgw-jpeg.grib2", "ifs-2t-ccsds.grib2", "ifs-msl-ccsds.grib2"]
    for p in probes:
        assert (probe.SAMPLES / p.file).is_file()


@real_decode
def test_the_probe_passes_on_the_committed_messages():
    # A14.
    probe.check(config.IFS)


def test_the_two_probe_messages():
    # F7: CCSDS at 9 bits (2t, K) and at 12 bits (msl, Pa), with the local decode's figures.
    assert config.IFS.probes == (
        config.Probe("ifs-2t-ccsds.grib2", 221.997971, 315.997971, 278.805962),
        config.Probe("ifs-msl-ccsds.grib2", 95764.125000, 104952.125000, 100852.455598),
    )
    assert [(probe.SAMPLES / p.file).stat().st_size for p in config.IFS.probes] == [314997, 511939]


@real_decode
def test_a_probe_with_altered_figures_fails():
    t2m, msl = config.IFS.probes
    for p, altered in ((t2m, dataclasses.replace(t2m, mean=t2m.mean + 0.02)),
                       (t2m, dataclasses.replace(t2m, minimum=222.1)),
                       (t2m, dataclasses.replace(t2m, maximum=315.9)),
                       (msl, dataclasses.replace(msl, mean=msl.mean - 0.02)),
                       (msl, dataclasses.replace(msl, maximum=104952.2))):
        others = tuple(q for q in config.IFS.probes if q is not p)
        with pytest.raises(ValueError, match=f"{p.file}: .* within 0.01"):
            probe.check(dataclasses.replace(config.IFS, probes=others + (altered,)))


@real_decode
def test_the_probe_in_a_child_process():
    run.probe(config.IFS)
    with pytest.raises(run.JobDropped, match="probe of nothing failed, exit 1"):
        run.probe(dataclasses.replace(config.IFS, info=dataclasses.replace(config.IFS.info, name="nothing")))


def fake_values(mean=278.805962, low=221.997971, high=315.997971):
    """A shared grid with the given minimum and maximum whose mean is [mean]."""
    values = np.full((config.NY, config.NX), mean)
    values[0, 0], values[0, 1] = low, high
    values[0, 2] = 3 * mean - low - high   # low + high + this = 3 * mean
    return values


def decode_as_recorded(change=None):
    """A fake decode giving each probe message, known by its size, a grid with its recorded figures;
    [change](probe, values) may alter one."""
    by_size = {(probe.SAMPLES / p.file).stat().st_size: p for p in config.IFS.probes}

    def decode(message, src):
        p = by_size[len(message)]
        values = fake_values(p.mean, p.minimum, p.maximum)
        return values if change is None else change(p, values)

    return decode


def test_the_probe_checks_shape_finite_values_and_figures(monkeypatch):
    # A14 without ecCodes: a fake decode; each probe message is checked.
    monkeypatch.setattr(grib, "decode", decode_as_recorded())
    probe.check(config.IFS)

    def nan_in_msl(p, values):
        if "msl" in p.file:
            values[5, 5] = np.nan
        return values

    monkeypatch.setattr(grib, "decode", decode_as_recorded(nan_in_msl))
    with pytest.raises(ValueError, match="ifs-msl-ccsds.grib2: 1 values are not finite"):
        probe.check(config.IFS)
    monkeypatch.setattr(grib, "decode", decode_as_recorded(lambda p, values: values[:-1]))
    with pytest.raises(ValueError, match="shape"):
        probe.check(config.IFS)
    monkeypatch.setattr(grib, "decode",
                        decode_as_recorded(lambda p, values: values + 0.02 if "msl" in p.file else values))
    with pytest.raises(ValueError, match="ifs-msl-ccsds.grib2: minimum"):
        probe.check(config.IFS)


def test_the_probes_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(grib, "decode", decode_as_recorded())
    assert probe.main(["ifs"]) == 0
    monkeypatch.setattr(grib, "decode", decode_as_recorded(
        lambda p, values: fake_values(mean=279.0) if "2t" in p.file else values))
    assert probe.main(["ifs"]) == 1
    assert "probe of ifs failed: ValueError: ifs-2t-ccsds.grib2: mean 279.000000" in capsys.readouterr().err
    assert probe.main(["nothing"]) == 1
    assert probe.main([]) == 1


class Done:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_run_probe_reports_an_exit_a_crash_and_a_timeout(monkeypatch):
    # A14: run.probe with a fake subprocess.run.
    calls = []

    def answer(result):
        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if isinstance(result, BaseException):
                raise result
            return result
        return fake_run

    monkeypatch.setenv("PYTHONPATH", "elsewhere")
    monkeypatch.setattr(subprocess, "run", answer(Done(0, "probe of ifs: 1 message(s) decoded as recorded")))
    run.probe(config.IFS)
    args, kwargs = calls[0]
    assert args == [sys.executable, "-m", "wxpipe.probe", "ifs"]
    assert Path(kwargs["cwd"]) == ROOT and kwargs["timeout"] == 300
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep) == [str(ROOT), "elsewhere"]
    assert kwargs["env"].get("PATH") == os.environ.get("PATH")

    monkeypatch.setattr(subprocess, "run", answer(Done(1, "", "probe of ifs failed: ValueError: mean 1.0\n")))
    with pytest.raises(run.JobDropped, match="probe of ifs failed, exit 1: probe of ifs failed: ValueError: mean 1.0"):
        run.probe(config.IFS)
    monkeypatch.setattr(subprocess, "run", answer(Done(-11)))
    with pytest.raises(run.JobDropped, match="exit -11: no output"):
        run.probe(config.IFS)
    monkeypatch.setattr(subprocess, "run", answer(subprocess.TimeoutExpired(["python"], 300)))
    with pytest.raises(run.JobDropped, match="timed out after 300 s"):
        run.probe(config.IFS)


# ---- GFS-Wave: the JPEG 2000 probe (W4) ---------------------------------------------------------------

WAVE_PROBE = config.GFSWAVE.probes[0]


def test_the_wave_probe_message():
    # A5: S12's HTSGW of 2026112206 at f000, JPEG 2000 at 10 bits, with the local decode's figures.
    assert config.GFSWAVE.probes == (config.Probe("gfswave-htsgw-jpeg.grib2", 0.0, 8.27, 2.267598, missing=468208),)
    assert (probe.SAMPLES / WAVE_PROBE.file).stat().st_size == 424696


@real_decode
def test_the_wave_probe_passes_on_the_committed_message():
    # A5: the real JPEG 2000 decode, skipped on the runner (R9).
    probe.check(config.GFSWAVE)


@real_decode
def test_a_wave_probe_with_altered_figures_fails():
    # A5: each rule named in the failure.
    for altered, reason in ((dataclasses.replace(WAVE_PROBE, missing=468207), "468208 values missing, expected 468207"),
                            (dataclasses.replace(WAVE_PROBE, missing=None), "468208 values are not finite"),
                            (dataclasses.replace(WAVE_PROBE, mean=2.29), "mean 2.267598, expected 2.290000 within 0.01"),
                            (dataclasses.replace(WAVE_PROBE, minimum=0.02), "minimum 0.000000, expected 0.020000"),
                            (dataclasses.replace(WAVE_PROBE, maximum=8.25), "maximum 8.270000, expected 8.250000")):
        with pytest.raises(ValueError, match=f"gfswave-htsgw-jpeg.grib2: {reason}"):
            probe.check(dataclasses.replace(config.GFSWAVE, probes=(altered,)))


@real_decode
def test_the_wave_probe_in_a_child_process():
    run.probe(config.GFSWAVE)


def wave_values(missing=468208, low=0.0, high=8.27, mean=2.267598):
    """A shared grid with [missing] cells of NaN, the rest of mean [mean] with the given extremes."""
    n = config.NY * config.NX - missing
    values = np.full(config.NY * config.NX, (mean * n - low - high) / (n - 2))
    values[:missing] = np.nan
    values[missing], values[missing + 1] = low, high
    return values.reshape(config.NY, config.NX)


def test_the_wave_probe_counts_the_missing_values_and_takes_the_figures_over_the_rest(monkeypatch):
    # A5 without ecCodes, so it runs on the runner too: a fake decode.
    monkeypatch.setattr(grib, "decode", lambda message, src: wave_values())
    probe.check(config.GFSWAVE)
    for values, reason in ((wave_values(missing=468209), "468209 values missing, expected 468208"),
                           (wave_values(missing=0), "0 values missing, expected 468208"),
                           (wave_values(high=8.3), "maximum 8.300000"),
                           (wave_values(mean=2.3), "mean 2.300000")):
        monkeypatch.setattr(grib, "decode", lambda message, src, v=values: v)
        with pytest.raises(ValueError, match=f"gfswave-htsgw-jpeg.grib2: {reason}"):
            probe.check(config.GFSWAVE)
    # Every value missing leaves no figure to take.
    monkeypatch.setattr(grib, "decode", lambda message, src: np.full((config.NY, config.NX), np.nan))
    with pytest.raises(ValueError, match="values missing|no value present"):
        probe.check(config.GFSWAVE)
    # The shape is checked first, as for IFS.
    monkeypatch.setattr(grib, "decode", lambda message, src: wave_values()[:-1])
    with pytest.raises(ValueError, match="shape"):
        probe.check(config.GFSWAVE)


def test_ifs_probes_still_require_every_value_finite(monkeypatch):
    # A5: a probe without a missing count keeps today's rule, whatever the count.
    monkeypatch.setattr(grib, "decode", decode_as_recorded(lambda p, values: wave_values() if "2t" in p.file else values))
    with pytest.raises(ValueError, match="ifs-2t-ccsds.grib2: 468208 values are not finite"):
        probe.check(config.IFS)


def test_the_wave_probes_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(grib, "decode", lambda message, src: wave_values())
    assert probe.main(["gfswave"]) == 0
    assert "probe of gfswave: 1 message(s) decoded as recorded" in capsys.readouterr().out
    monkeypatch.setattr(grib, "decode", lambda message, src: wave_values(missing=5))
    assert probe.main(["gfswave"]) == 1
    assert "probe of gfswave failed: ValueError: gfswave-htsgw-jpeg.grib2: 5 values missing" in capsys.readouterr().err
