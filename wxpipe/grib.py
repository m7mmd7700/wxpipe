"""Decodes one GRIB2 message of the GFS 0.25 degree grid into a (NY, NX) array, north row first."""

from __future__ import annotations

import eccodes
import numpy as np

from . import config


def decode(message: bytes) -> np.ndarray:
    handle = eccodes.codes_new_from_message(message)
    try:
        ni = eccodes.codes_get(handle, "Ni")
        nj = eccodes.codes_get(handle, "Nj")
        if (ni, nj) != (config.NX, config.NY):
            raise ValueError(f"unexpected grid {ni}x{nj}")
        if eccodes.codes_get(handle, "jScansPositively") != 0 or eccodes.codes_get(handle, "iScansNegatively") != 0:
            raise ValueError("unexpected scanning order")
        lat = eccodes.codes_get(handle, "latitudeOfFirstGridPointInDegrees")
        lon = eccodes.codes_get(handle, "longitudeOfFirstGridPointInDegrees")
        if abs(lat - config.LAT0) > 1e-6 or abs(lon - config.LON0) > 1e-6:
            raise ValueError(f"unexpected first grid point {lat}, {lon}")
        values = np.asarray(eccodes.codes_get_values(handle), dtype=np.float64).reshape(nj, ni)
        if eccodes.codes_get(handle, "bitmapPresent"):
            values[values == eccodes.codes_get(handle, "missingValue")] = np.nan
        return values
    finally:
        eccodes.codes_release(handle)
