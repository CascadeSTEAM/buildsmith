"""Strict stdlib PNG decode/encode and a pixel diff — the oracle's arithmetic.

Scope is deliberately narrow: Playwright's Chromium emits 8-bit, non-interlaced
RGB/RGBA PNGs and that is all the decoder accepts. Anything else raises
`UnsupportedPng`, which callers surface as exit 2 — "could not check" must
never pass as "checked and fine". No Pillow: the repo ships zero runtime
dependencies, and a codec this scoped is small enough to own and test.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# colour type -> samples per pixel, for the subset we accept
_CHANNELS = {0: 1, 2: 3, 6: 4}


class UnsupportedPng(Exception):
    """A well-formed PNG outside the strict subset the oracle understands."""


class CorruptPng(Exception):
    """Not a decodable PNG at all."""


@dataclass
class Image:
    width: int
    height: int
    channels: int          # 1 grey, 3 RGB, 4 RGBA
    pixels: bytearray      # row-major, width*height*channels bytes

    def pixel(self, x: int, y: int) -> tuple[int, ...]:
        i = (y * self.width + x) * self.channels
        return tuple(self.pixels[i:i + self.channels])


def decode(data: bytes) -> Image:
    """Decode a PNG byte string within the strict subset, or raise."""
    if data[:8] != _SIGNATURE:
        raise CorruptPng("missing PNG signature")
    pos = 8
    width = height = bitdepth = colourtype = interlace = None
    idat = bytearray()
    while pos < len(data):
        if pos + 8 > len(data):
            raise CorruptPng("truncated chunk header")
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise CorruptPng(f"truncated {ctype!r} chunk")
        pos += 12 + length  # header + body + crc (crc not verified; zlib is)
        if ctype == b"IHDR":
            width, height, bitdepth, colourtype, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat.extend(body)
        elif ctype == b"PLTE":
            raise UnsupportedPng("palette PNGs are outside the oracle's subset")
        elif ctype == b"IEND":
            break
    if width is None:
        raise CorruptPng("no IHDR")
    if bitdepth != 8 or colourtype not in _CHANNELS or interlace != 0:
        raise UnsupportedPng(
            f"bitdepth={bitdepth} colourtype={colourtype} interlace={interlace}"
            " — the oracle accepts 8-bit non-interlaced grey/RGB/RGBA only")
    channels = _CHANNELS[colourtype]
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise CorruptPng(f"IDAT stream: {exc}") from exc

    stride = width * channels
    if len(raw) != (stride + 1) * height:
        raise CorruptPng("decompressed size disagrees with IHDR")

    pixels = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        offset = y * (stride + 1)
        ftype = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        _unfilter(line, prev, ftype, channels)
        pixels[y * stride:(y + 1) * stride] = line
        prev = line
    return Image(width, height, channels, pixels)


def _unfilter(line: bytearray, prev: bytearray, ftype: int, bpp: int) -> None:
    if ftype == 0:                                    # None
        return
    if ftype == 1:                                    # Sub
        for i in range(bpp, len(line)):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:                                  # Up
        for i in range(len(line)):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:                                  # Average
        for i in range(len(line)):
            a = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:                                  # Paeth
        for i in range(len(line)):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            if pa <= pb and pa <= pc:
                pred = a
            elif pb <= pc:
                pred = b
            else:
                pred = c
            line[i] = (line[i] + pred) & 0xFF
    else:
        raise CorruptPng(f"unknown filter type {ftype}")


def encode(img: Image) -> bytes:
    """Encode with filter 0 everywhere — simple, valid, and good enough for
    diff artifacts and tests."""
    if img.channels not in (1, 3, 4):
        raise UnsupportedPng(f"cannot encode {img.channels}-channel image")
    colourtype = {1: 0, 3: 2, 4: 6}[img.channels]
    stride = img.width * img.channels
    raw = bytearray()
    for y in range(img.height):
        raw.append(0)
        raw.extend(img.pixels[y * stride:(y + 1) * stride])

    def chunk(ctype: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", img.width, img.height, 8, colourtype, 0, 0, 0)
    return (_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
@dataclass
class DiffResult:
    """Outcome of comparing one screenshot pair."""
    total: int = 0
    differing: int = 0
    bbox: tuple[int, int, int, int] | None = None   # x0, y0, x1, y1 inclusive
    size_mismatch: str = ""                          # non-empty = structural fail
    notes: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return (self.differing / self.total) if self.total else 1.0

    def within(self, threshold: float) -> bool:
        return not self.size_mismatch and self.ratio <= threshold


def diff(a: Image, b: Image, *, tolerance: int = 2) -> DiffResult:
    """Count pixels whose max channel delta exceeds `tolerance`.

    A size mismatch is a structural failure, not a 100% pixel difference —
    the page grew or shrank, which no threshold should be able to absorb.
    """
    if (a.width, a.height) != (b.width, b.height):
        return DiffResult(size_mismatch=(
            f"{a.width}x{a.height} vs {b.width}x{b.height}"))
    if a.channels != b.channels:
        return DiffResult(size_mismatch=(
            f"{a.channels}ch vs {b.channels}ch"))

    total = a.width * a.height
    n = a.channels
    pa, pb = a.pixels, b.pixels
    differing = 0
    x0 = y0 = 1 << 30
    x1 = y1 = -1
    for idx in range(total):
        base = idx * n
        for c in range(n):
            d = pa[base + c] - pb[base + c]
            if d > tolerance or d < -tolerance:
                differing += 1
                x, y = idx % a.width, idx // a.width
                if x < x0:
                    x0 = x
                if y < y0:
                    y0 = y
                if x > x1:
                    x1 = x
                if y > y1:
                    y1 = y
                break
    bbox = (x0, y0, x1, y1) if differing else None
    return DiffResult(total=total, differing=differing, bbox=bbox)


def diff_artifact(a: Image, b: Image, *, tolerance: int = 2) -> Image:
    """A same-size image: greyscaled `a`, changed pixels painted solid red.

    Only valid when the pair already compared without a size mismatch.
    """
    n = a.channels
    out = Image(a.width, a.height, 3, bytearray(a.width * a.height * 3))
    for idx in range(a.width * a.height):
        base = idx * n
        grey = sum(a.pixels[base:base + min(n, 3)]) // min(n, 3)
        changed = False
        for c in range(n):
            d = a.pixels[base + c] - b.pixels[base + c]
            if d > tolerance or d < -tolerance:
                changed = True
                break
        o = idx * 3
        if changed:
            out.pixels[o:o + 3] = b"\xff\x00\x00"
        else:
            # lift greys toward white so the red reads at a glance
            g = 128 + grey // 2
            out.pixels[o:o + 3] = bytes((g, g, g))
    return out
