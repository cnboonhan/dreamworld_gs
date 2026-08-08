"""Pure-stdlib PNG helpers (no cv2/PIL) shared by the render tools."""

import struct
import zlib

import numpy as np


def _chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def write_png(path, arr):
    """Write an (H, W, 3) uint8 RGB array as a PNG."""
    h, w = arr.shape[:2]
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 6))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def write_png16(path, arr):
    """Write an (H, W) uint16 array as a 16-bit grayscale PNG (big-endian)."""
    h, w = arr.shape
    be = np.ascontiguousarray(arr.astype(">u2"))
    raw = b"".join(b"\x00" + be[y].tobytes() for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 6))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
