"""Just enough PNG for perception: read the size, decode pixels, encode test images. Stdlib only.

Screenshots come from the platform (``screencapture``, CDP, ``adb exec-out screencap -p``) as
PNG. Visual diffing needs their pixels; HighhX does not depend on an imaging library, so this
decodes the common screenshot formats: 8-bit grayscale, gray+alpha, RGB, RGBA and palette images,
non-interlaced. Anything else raises :class:`PNGError`, and the caller falls back to comparing
digests.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PNGError(ValueError):
    pass


@dataclass(frozen=True)
class Image:
    """Decoded pixels as RGB rows (alpha dropped, palette expanded)."""

    width: int
    height: int
    rgb: bytes

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * 3
        return self.rgb[i], self.rgb[i + 1], self.rgb[i + 2]

    def luminance(self) -> list[int]:
        """One 0-255 value per pixel (ITU-R BT.601 weights)."""
        px = self.rgb
        return [(299 * px[i] + 587 * px[i + 1] + 114 * px[i + 2]) // 1000 for i in range(0, len(px), 3)]


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(SIGNATURE) or data[12:16] != b"IHDR":
        raise PNGError("not a PNG image")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not data.startswith(SIGNATURE):
        raise PNGError("not a PNG image")
    out = []
    pos = len(SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        out.append((kind, data[pos + 8 : pos + 8 + length]))
        pos += 12 + length
        if kind == b"IEND":
            break
    return out


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(data: bytes) -> Image:
    chunks = _chunks(data)
    header = next((body for kind, body in chunks if kind == b"IHDR"), None)
    if header is None:
        raise PNGError("PNG without a header")
    width, height, depth, color, _compression, _filter, interlace = struct.unpack(">IIBBBBB", header)
    if depth != 8 or color not in _CHANNELS or interlace != 0:
        raise PNGError(f"unsupported PNG (bit depth {depth}, color type {color}, interlace {interlace})")
    palette = next((body for kind, body in chunks if kind == b"PLTE"), b"")
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    channels = _CHANNELS[color]
    stride = width * channels
    rows: list[bytearray] = []
    previous = bytearray(stride)
    pos = 0
    for _ in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                corner = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, up, corner)) & 0xFF
            elif kind != 0:
                raise PNGError(f"unknown PNG filter {kind}")
        rows.append(line)
        previous = line
    rgb = bytearray()
    for line in rows:
        if color == 2:
            rgb += line
        elif color == 6:
            for i in range(0, stride, 4):
                rgb += line[i : i + 3]
        elif color == 0:
            for v in line:
                rgb += bytes((v, v, v))
        elif color == 4:
            for i in range(0, stride, 2):
                rgb += bytes((line[i], line[i], line[i]))
        else:  # palette
            for index in line:
                rgb += palette[index * 3 : index * 3 + 3] or b"\x00\x00\x00"
    return Image(width, height, bytes(rgb))


def encode(width: int, height: int, rgb: bytes) -> bytes:
    """An RGB PNG (filter 0). Used for fixtures and for crops handed to a model."""
    if len(rgb) != width * height * 3:
        raise PNGError("pixel data does not match the size")

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def crop(image: Image, x: int, y: int, width: int, height: int) -> Image:
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(image.width, x + width), min(image.height, y + height)
    if x1 <= x0 or y1 <= y0:
        raise PNGError("the crop is outside the image")
    rows = [image.rgb[(r * image.width + x0) * 3 : (r * image.width + x1) * 3] for r in range(y0, y1)]
    return Image(x1 - x0, y1 - y0, b"".join(rows))


def solid(width: int, height: int, color: tuple[int, int, int] = (255, 255, 255)) -> bytearray:
    return bytearray(bytes(color) * (width * height))


def fill(rgb: bytearray, width: int, box: tuple[int, int, int, int], color: tuple[int, int, int]) -> None:
    """Paint ``box`` (x, y, w, h) in an RGB buffer (fixtures)."""
    x, y, w, h = box
    for row in range(y, y + h):
        start = (row * width + x) * 3
        rgb[start : start + w * 3] = bytes(color) * w
