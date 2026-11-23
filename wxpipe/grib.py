"""Decodes one GRIB2 message of a source onto the shared (NY, NX) grid, north row first."""

from __future__ import annotations

import eccodes
import numpy as np

from . import config


def check_grid(source: config.Source, ni: int, nj: int, lat0: float, lon0: float, di: float, dj: float,
               j_scans_positively: int, i_scans_negatively: int) -> None:
    """Raises ValueError naming every value that differs from the source's delivered grid.

    Degrees are compared within 1e-6.
    """
    want = source.grid
    found = {"Ni": (ni, want.ni), "Nj": (nj, want.nj),
             "latitudeOfFirstGridPointInDegrees": (lat0, want.lat0),
             "longitudeOfFirstGridPointInDegrees": (lon0, want.lon0),
             "iDirectionIncrementInDegrees": (di, want.di), "jDirectionIncrementInDegrees": (dj, want.dj),
             "jScansPositively": (j_scans_positively, want.j_scans_positively),
             "iScansNegatively": (i_scans_negatively, want.i_scans_negatively)}
    degrees = {"latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
               "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees"}
    wrong = [f"{key} {got} (expected {expected})" for key, (got, expected) in found.items()
             if (abs(got - expected) > 1e-6 if key in degrees else got != expected)]
    if wrong:
        raise ValueError(f"unexpected {source.info.name} grid: " + ", ".join(wrong))


def roll_to_shared(values: np.ndarray, lon0: float, deg: float = config.DEG) -> np.ndarray:
    """[values] with their columns rolled so that column 0 lies at the shared grid's LON0, for a
    delivered grid whose first column lies at [lon0] in steps of [deg]: 720 columns for a 0.25 degree
    grid that starts at 180E, none for one that starts at 0E."""
    shift = round((lon0 - config.LON0) / deg) % values.shape[1]
    return np.roll(values, shift, axis=1)


def on_shared_grid(values: np.ndarray) -> np.ndarray:
    """[values] if they are on the shared (NY, NX) grid; ValueError otherwise."""
    if values.shape != (config.NY, config.NX):
        raise ValueError(f"grid of shape {values.shape} is not the shared {(config.NY, config.NX)}")
    return values


def decode(message: bytes, source: config.Source) -> np.ndarray:
    """The message's values on the shared grid. A delivered grid other than the source's is refused.

    The delivered grid is checked against the source's description, then its columns are rolled onto
    the shared grid by where its first column lies (IFS's starts at 180E), then its shape is checked.
    """
    handle = eccodes.codes_new_from_message(message)
    try:
        ni = eccodes.codes_get(handle, "Ni")
        nj = eccodes.codes_get(handle, "Nj")
        check_grid(source, ni, nj,
                   eccodes.codes_get(handle, "latitudeOfFirstGridPointInDegrees"),
                   eccodes.codes_get(handle, "longitudeOfFirstGridPointInDegrees"),
                   eccodes.codes_get(handle, "iDirectionIncrementInDegrees"),
                   eccodes.codes_get(handle, "jDirectionIncrementInDegrees"),
                   eccodes.codes_get(handle, "jScansPositively"),
                   eccodes.codes_get(handle, "iScansNegatively"))
        values = np.asarray(eccodes.codes_get_values(handle), dtype=np.float64).reshape(nj, ni)
        values = on_shared_grid(roll_to_shared(values, source.grid.lon0))
        if eccodes.codes_get(handle, "bitmapPresent"):
            values[values == eccodes.codes_get(handle, "missingValue")] = np.nan
        return values
    finally:
        eccodes.codes_release(handle)
