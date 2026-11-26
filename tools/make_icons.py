#!/usr/bin/env python3
"""Draw Porthole's icon, the ring: a white ring centred on a blue rounded tile, the same ring as the mark in the
page header (a 22 px circle with a 5 px border, `.mark` in web/style.css). One set of numbers makes every file:

  web/icon.svg            the browser tab, and the title bar of Porthole's own app window
  assets/icon.png         256 px: the menu icon (.deb, AppImage, from source)
  snap/gui/porthole.png   the snap's menu icon, the same file
  assets/icon.ico         16, 32, 48 and 256 px: the Windows shortcut, run from source
  web/icon-180.png        iPhone's home-screen icon: the whole square, opaque (iPhone rounds the corners itself
                          and shows transparent parts as black)
  web/icon-192.png        the web manifest's icons (not marked maskable, so the rounded tile as everywhere else)
  web/icon-512.png

Edges are smoothed by how much of each pixel a shape covers, worked out from the distance to its edge.
Standard library only. Run from the project folder:  python3 tools/make_icons.py
"""

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
BLUE = (37, 99, 235)  # #2563eb
WHITE = (255, 255, 255)
# In the SVG's 64 x 64 units. The ring is the header's mark at 1.7 times its size: 37.4 wide (58 % of the
# tile), 8.5 thick (5/22 of its width, as the mark's border).
TILE, CORNER = 64, 14
RING_OUTER, RING_WIDTH = 18.7, 8.5
RING_MIDDLE = RING_OUTER - RING_WIDTH / 2  # 14.45, where the SVG's stroke runs
WEB_SIZES = (180, 192, 512)  # apple-touch-icon, then the two sizes in web/manifest.json
OPAQUE_SIZES = (180,)  # drawn as a whole square, with no alpha
ICO_SIZES = (16, 32, 48, 256)


def svg():
    """web/icon.svg: the same tile and ring; the stroke runs along the middle of the ring."""

    def num(value):
        return f"{value:g}"

    blue = "#%02x%02x%02x" % BLUE
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {TILE} {TILE}">\n'
        f'  <rect width="{TILE}" height="{TILE}" rx="{CORNER}" fill="{blue}"/>\n'
        f'  <circle cx="{num(TILE / 2)}" cy="{num(TILE / 2)}" r="{num(RING_MIDDLE)}" fill="none"'
        f' stroke="#ffffff" stroke-width="{num(RING_WIDTH)}"/>\n'
        "</svg>\n"
    )


def tile_distance(x, y, corner):
    """Signed distance from the rounded tile's edge, negative inside."""
    half = TILE / 2
    qx, qy = abs(x - half) - (half - corner), abs(y - half) - (half - corner)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - corner


def ring_distance(x, y):
    """Signed distance from the ring's edges, negative inside."""
    return abs(math.hypot(x - TILE / 2, y - TILE / 2) - RING_MIDDLE) - RING_WIDTH / 2


def coverage(distance, pixel):
    """The share of a pixel a shape covers, from the distance of the pixel's centre to the shape's edge."""
    return min(1.0, max(0.0, 0.5 - distance / pixel))


def render(size, opaque=False):
    """Rows of (r, g, b, a) floats; the tile fills the whole square when opaque."""
    pixel = TILE / size
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            x, y = (px + 0.5) * pixel, (py + 0.5) * pixel
            tile = 1.0 if opaque else coverage(tile_distance(x, y, CORNER), pixel)
            ring = coverage(ring_distance(x, y), pixel)
            alpha = ring + tile * (1 - ring)  # the white ring over the blue tile
            if alpha:
                row.append(tuple((WHITE[i] * ring + BLUE[i] * tile * (1 - ring)) / alpha for i in range(3)) + (alpha,))
            else:
                row.append((0.0, 0.0, 0.0, 0.0))
        rows.append(row)
    return rows


def png(size, opaque=False):
    """An 8-bit PNG: RGBA, or RGB (no alpha at all) when opaque."""

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    lines = []
    for row in render(size, opaque):
        line = bytearray(b"\0")  # filter: none
        for r, g, b, a in row:
            line += bytes((round(r), round(g), round(b))) + (b"" if opaque else bytes((round(255 * a),)))
        lines.append(bytes(line))
    header = struct.pack(">IIBBBBB", size, size, 8, 2 if opaque else 6, 0, 0, 0)
    return PNG_MAGIC + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(lines), 9)) + chunk(b"IEND", b"")



def ico(images):
    count = len(images)
    out = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    for size, data in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8", newline="\n")
    else:
        path.write_bytes(data)
    print(f"Wrote {path.relative_to(ROOT).as_posix()}")


def main():
    write(ROOT / "web" / "icon.svg", svg())
    pictures = {size: png(size) for size in ICO_SIZES}
    write(ROOT / "assets" / "icon.png", pictures[256])
    write(ROOT / "snap" / "gui" / "porthole.png", pictures[256])
    write(ROOT / "assets" / "icon.ico", ico([(size, pictures[size]) for size in ICO_SIZES]))
    for size in WEB_SIZES:
        write(ROOT / "web" / f"icon-{size}.png", png(size, opaque=size in OPAQUE_SIZES))


if __name__ == "__main__":
    main()
