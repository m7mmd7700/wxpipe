"""WXT1: one tile of one field with all its frames (blueprint 6.2), plus a frame index for HTTP Range reads.

Layout, little-endian:
    magic   b"WXT1"            4 bytes
    hlen    uint32             length of the JSON header
    header  UTF-8 JSON         grid, values (LUT), frames index, ...
    frames  zlib streams       one per frame, at the offsets in the index

Each frame is ny * nx uint8 values, row-major from north to south, compressed on its own with zlib,
so a client can fetch the whole file or any single frame with a Range request (GitHub Pages answers
206). header["frames"] is a list of [valid_time_epoch_s, offset, length], offsets counted from the
first byte after the header.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np

MAGIC = b"WXT1"


def encode(header: dict, frames: list[tuple[int, np.ndarray]], level: int = 9) -> bytes:
    payloads = [zlib.compress(np.ascontiguousarray(grid, dtype=np.uint8).tobytes(), level) for _, grid in frames]
    index, offset = [], 0
    for (t, _), payload in zip(frames, payloads):
        index.append([int(t), offset, len(payload)])
        offset += len(payload)
    full = dict(header, wxt=1, format="zlib", frames=index)
    head = json.dumps(full, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return MAGIC + struct.pack("<I", len(head)) + head + b"".join(payloads)


def write(path: Path, header: dict, frames: list[tuple[int, np.ndarray]]) -> int:
    data = encode(header, frames)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def decode(data: bytes) -> tuple[dict, list[tuple[int, np.ndarray]]]:
    if data[:4] != MAGIC:
        raise ValueError("not a WXT1 file")
    (hlen,) = struct.unpack("<I", data[4:8])
    header = json.loads(data[8:8 + hlen].decode("utf-8"))
    base = 8 + hlen
    ny, nx = header["grid"]["ny"], header["grid"]["nx"]
    frames = []
    for t, offset, length in header["frames"]:
        raw = zlib.decompress(data[base + offset: base + offset + length])
        if len(raw) != nx * ny:
            raise ValueError(f"frame {t}: {len(raw)} bytes, expected {nx * ny}")
        frames.append((t, np.frombuffer(raw, dtype=np.uint8).reshape(ny, nx)))
    return header, frames


def read(path: Path) -> tuple[dict, list[tuple[int, np.ndarray]]]:
    return decode(Path(path).read_bytes())
