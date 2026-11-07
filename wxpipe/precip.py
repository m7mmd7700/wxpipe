"""Hourly precipitation rate from GFS accumulations.

GFS stores APCP two ways in each file after hour 6: the total since the run started (`0-7 hour acc`,
`0-5 day acc`, ...) and a bucket that restarts every 6 hours (`6-7 hour acc`, `120-126 hour acc`).
The rate over the frame interval (prev, now] is

    (A(a, now) - A(a, prev)) / (now - prev),   with A(a, prev) = 0 when a == prev,

where a is the start of an accumulation window that ends at `now` and covers `prev`. The "bucket"
method picks the latest such start, so it works with small amounts; "since-start" always uses a = 0.
Which one the pipeline uses is decided by measurement on real data (docs/DESIGN.md).
"""

from __future__ import annotations

import numpy as np

Windows = dict[tuple[int, int], np.ndarray]


def window_start(now_windows: Windows, prev: int, method: str) -> int | None:
    starts = sorted(a for (a, _) in now_windows if a <= prev)
    if not starts:
        return None
    if method == "since-start":
        return 0 if 0 in starts else None
    if method == "bucket":
        return starts[-1]
    raise ValueError(f"unknown method {method!r}")


def rate(now: int, prev: int, now_windows: Windows, prev_windows: Windows, method: str) -> tuple[np.ndarray | None, int]:
    """Rate in mm/h over (prev, now], and how many cells came out negative before clamping to zero.

    Returns (None, 0) when the needed accumulations are missing.
    """
    a = window_start(now_windows, prev, method)
    if a is None or (a, now) not in now_windows:
        return None, 0
    amount = now_windows[(a, now)]
    if a < prev:
        before = prev_windows.get((a, prev))
        if before is None:
            return None, 0
        amount = amount - before
    result = amount / float(now - prev)
    negative = int(np.count_nonzero(result < 0))
    return np.where(result < 0, 0.0, result), negative
