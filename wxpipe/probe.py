"""The probe: does this machine's ecCodes decode a source's committed messages to their known figures?

    python -m wxpipe.probe ifs
    python -m wxpipe.probe gfswave

Exits 0 when every probe message of the source decodes onto the shared grid, with every value finite,
or, for a probe that records a missing count, exactly that many values missing, and its minimum,
maximum and mean over the present values each within TOLERANCE of the recorded figures; 1 with the
reason otherwise.
`wxpipe.run` runs it in a child process before it reads a source's index files, so a library that
cannot read the source's packing, or that crashes on it, skips that source only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from . import config, grib

SAMPLES = Path(__file__).parent / "samples"
TOLERANCE = 0.01


def check(source: config.Source) -> None:
    """Raises ValueError naming the first probe message that fails, and why."""
    for p in source.probes:
        values = grib.decode((SAMPLES / p.file).read_bytes(), source)
        if values.shape != (config.NY, config.NX):
            raise ValueError(f"{p.file}: shape {values.shape}, expected {(config.NY, config.NX)}")
        present = np.isfinite(values)
        absent = int(np.count_nonzero(~present))
        if p.missing is None:
            if absent:
                raise ValueError(f"{p.file}: {absent} values are not finite")
        elif absent != p.missing:
            raise ValueError(f"{p.file}: {absent} values missing, expected {p.missing}")
        values = values[present]
        if values.size == 0:
            raise ValueError(f"{p.file}: no value present")
        got = {"minimum": float(values.min()), "maximum": float(values.max()), "mean": float(values.mean())}
        for name, value in got.items():
            expected = getattr(p, name)
            if abs(value - expected) > TOLERANCE:
                raise ValueError(f"{p.file}: {name} {value:.6f}, expected {expected:.6f} within {TOLERANCE}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m wxpipe.probe <source>", file=sys.stderr)
        return 1
    name = args[0]
    try:
        source = config.source(name)
        check(source)
    except Exception as e:   # any failure, ecCodes' own errors included, is the probe's answer
        print(f"probe of {name} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"probe of {name}: {len(source.probes)} message(s) decoded as recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
