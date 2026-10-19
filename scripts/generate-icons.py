"""Generate the DeskWeave desktop icons without third-party image libraries.

Produces PNG icons (32/128/256/512) and a Windows .ico from a tiny procedural
"DW" mark. Run with: ``python scripts/generate-icons.py``.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parents[1] / "apps" / "desktop" / "src-tauri" / "icons"

BG = (79, 70, 229, 255)
FG = (255, 255, 255, 255)
ACCENT = (129, 140, 248, 255)


def _pixel(x: int, y: int, size: int) -> tuple[int, int, int, int]:
    """A rounded indigo tile with a lighter diagonal band."""
    margin = max(1, size // 12)
    radius = size // 5
    # Rounded-corner mask.
    cx = min(max(x, margin + radius), size - margin - radius - 1)
    cy = min(max(y, margin + radius), size - margin - radius - 1)
    if (
        (x - cx) ** 2 + (y - cy) ** 2 > radius**2
        and (x < margin + radius or x > size - margin - radius - 1)
        and (y < margin + radius or y > size - margin - radius - 1)
    ):
        return (0, 0, 0, 0)
    if abs(x - y) < max(1, size // 16):
        return ACCENT
    return BG


def _png(size: int) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(_pixel(x, y, size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _ico(size: int) -> bytes:
    pixels = bytearray()
    for y in range(size - 1, -1, -1):  # bottom-up
        for x in range(size):
            r, g, b, a = _pixel(x, y, size)
            pixels.extend((b, g, r, a))
    row_bytes = ((size + 31) // 32) * 4
    and_mask = bytes(row_bytes * size)
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels) + len(and_mask), 0, 0, 0, 0
    )
    image = header + bytes(pixels) + and_mask
    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), 22)
    return icondir + entry + image


def main() -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size, name in [
        (32, "32x32.png"),
        (128, "128x128.png"),
        (256, "128x128@2x.png"),
        (512, "icon.png"),
    ]:
        (ICON_DIR / name).write_bytes(_png(size))
    (ICON_DIR / "icon.ico").write_bytes(_ico(32))
    print(f"wrote icons to {ICON_DIR}")


if __name__ == "__main__":
    main()
