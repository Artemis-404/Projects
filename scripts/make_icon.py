"""Generate the UQAB app icon as a PNG, with no image library.

The mark is five vertical bars of different heights on a dark rounded square: the
persona timetable (record sizes over slots) that the project is about. Drawn with
plain pixel maths so the build has no Pillow dependency.

Usage::

    python scripts/make_icon.py                       # app/assets/icon.png + build/icon.png
    python scripts/make_icon.py --size 256 --out x.png
"""

from __future__ import annotations

import argparse
import io
import struct
import zlib
from pathlib import Path

BG = (14, 17, 23, 255)
ACCENT = (88, 166, 255, 255)
ACCENT_DEEP = (31, 111, 235, 255)
EDGE = (35, 42, 51, 255)
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def _rounded(x: int, y: int, size: int, radius: float) -> bool:
    """True when (x, y) is inside a rounded square of the given size."""
    r = radius
    if x < r and y < r:
        return (x - r) ** 2 + (y - r) ** 2 <= r * r
    if x > size - r and y < r:
        return (x - (size - r)) ** 2 + (y - r) ** 2 <= r * r
    if x < r and y > size - r:
        return (x - r) ** 2 + (y - (size - r)) ** 2 <= r * r
    if x > size - r and y > size - r:
        return (x - (size - r)) ** 2 + (y - (size - r)) ** 2 <= r * r
    return True


def render(size: int) -> bytes:
    """Return RGBA pixel rows for one icon size."""
    pixels = bytearray()
    scale = size / 512.0
    radius = max(2.0, 96.0 * scale)
    bar_width = max(1.0, 52.0 * scale)
    gap = max(1.0, 30.0 * scale)
    heights = (0.34, 0.56, 0.78, 0.50, 0.28)
    centre_y = size / 2.0
    baseline = centre_y
    total = len(heights) * bar_width + (len(heights) - 1) * gap
    left = (size - total) / 2.0

    for y in range(size):
        pixels.append(0)  # PNG filter type for the row
        for x in range(size):
            if not _rounded(x, y, size, radius):
                pixels.extend((0, 0, 0, 0))
                continue
            colour = BG
            # Subtle inner edge so the icon reads on light backgrounds.
            if not _rounded(x, y, size, radius + 1.5):
                colour = EDGE
            for index, height in enumerate(heights):
                bar_left = left + index * (bar_width + gap)
                bar_right = bar_left + bar_width
                bar_height = height * size * 0.72
                top = baseline - bar_height / 2.0
                bottom = baseline + bar_height / 2.0
                if bar_left <= x < bar_right and top <= y < bottom:
                    colour = ACCENT_DEEP if index % 2 else ACCENT
                    break
            pixels.extend(colour)
    return bytes(pixels)


def write_png(path: Path, size: int) -> Path:
    """Write one RGBA PNG."""
    raw = render(size)
    width = height = size

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    body = zlib.compress(raw, 9)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", body) + chunk(b"IEND", b"")
    )
    return path


ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def write_ico(path: Path, sizes: tuple[int, ...] = ICO_SIZES) -> Path:
    """Write a multi-resolution .ico (PNG-compressed entries, Vista and later)."""
    images: list[tuple[int, bytes]] = []
    for size in sizes:
        buffer = io.BytesIO()
        _png_bytes(buffer, size)
        images.append((size, buffer.getvalue()))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for size, data in images:
        side = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
        payload += data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + payload)
    return path


def _png_bytes(handle: io.BytesIO, size: int) -> None:
    """Write one RGBA PNG into a file-like object."""
    raw = render(size)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    handle.write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the UQAB icon PNG and ICO")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    if args.out:
        target = write_png(Path(args.out), args.size)
        print(f"wrote {target}")
        return 0
    for directory in ("app/assets", "build"):
        target = write_png(root / directory / "icon.png", args.size)
        print(f"wrote {target}")
    ico = write_ico(root / "app" / "assets" / "icon.ico")
    print(f"wrote {ico}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
