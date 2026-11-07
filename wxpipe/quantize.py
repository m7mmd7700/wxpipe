"""Piecewise-linear 8-bit quantisation. Byte 255 means missing; real values use bytes 0..254."""

from __future__ import annotations

import numpy as np

MISSING = 255


class Lut:
    """Maps values to bytes and back through (byte, value) breakpoints that both rise.

    Values between breakpoints are linear in both directions, so the app can rebuild the same table
    from the file header. Values outside the first and last breakpoint clip to the end bytes.
    """

    def __init__(self, breakpoints):
        points = tuple((int(b), float(v)) for b, v in breakpoints)
        if len(points) < 2:
            raise ValueError("a LUT needs at least two breakpoints")
        bytes_ = [b for b, _ in points]
        values = [v for _, v in points]
        if bytes_[0] != 0 or bytes_[-1] > MISSING - 1:
            raise ValueError("breakpoint bytes must start at 0 and end at most at 254")
        if any(b2 <= b1 for b1, b2 in zip(bytes_, bytes_[1:])) or any(v2 <= v1 for v1, v2 in zip(values, values[1:])):
            raise ValueError("breakpoints must rise in both byte and value")
        self.breakpoints = points
        self._b = np.array(bytes_, dtype=np.float64)
        self._v = np.array(values, dtype=np.float64)

    @property
    def min_value(self) -> float:
        return float(self._v[0])

    @property
    def max_value(self) -> float:
        return float(self._v[-1])

    def encode(self, values) -> np.ndarray:
        """Bytes for [values]; NaN becomes MISSING."""
        values = np.asarray(values, dtype=np.float64)
        missing = np.isnan(values)
        filled = np.where(missing, self._v[0], values)
        out = np.rint(np.interp(filled, self._v, self._b)).astype(np.uint8)
        out[missing] = MISSING
        return out

    def decode(self, data) -> np.ndarray:
        """Values for bytes [data]; MISSING becomes NaN."""
        data = np.asarray(data)
        out = np.interp(data.astype(np.float64), self._b, self._v)
        out[data == MISSING] = np.nan
        return out

    def step(self, value: float) -> float:
        """The value change per byte around [value]: the larger step of the segments that touch it."""
        steps = np.diff(self._v) / np.diff(self._b)
        k = int(np.clip(np.searchsorted(self._v, value, side="right") - 1, 0, len(steps) - 1))
        neighbours = [steps[k]]
        if k > 0 and np.isclose(value, self._v[k]):
            neighbours.append(steps[k - 1])
        return float(max(neighbours))

    def header(self) -> dict:
        return {"scale": "lin", "breakpoints": [[b, v] for b, v in self.breakpoints], "missing": MISSING}
