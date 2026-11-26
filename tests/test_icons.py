"""Porthole's icon is the ring, the mark in the page header (since 23 Nov 2026): a white ring centred on a blue
rounded tile, and nothing else. These tests read every drawn file back, pixel by pixel, and check the picture
against the header's mark in web/style.css rather than against the drawing tool's own numbers."""

import importlib.util
import json
import math
import re
import struct
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SVG_NS = "{http://www.w3.org/2000/svg}"
BLUE = (37, 99, 235)  # #2563eb, the icon's blue since before the rename (the light theme's --accent is #2557d6)
WHITE = (255, 255, 255)
OUTER = (0.28, 0.30)  # the ring's outer radius, as a share of the tile's width: a diameter of 56-60 %
SAFE_ZONE = 0.40  # a maskable icon's safe zone: the centre circle of 80 % diameter
CORNER = 14 / 64  # the tile's corner radius as a share of its width (design.md, "Logo: the ring")


def mark_shape():
    """The header's mark: (width, border) in px, from the .mark rule in web/style.css."""
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    body = re.search(r"(?m)^\.mark\s*\{([^}]*)\}", css).group(1)
    width = int(re.search(r"(?<![-\w])width:\s*(\d+)px", body).group(1))
    border = int(re.search(r"border:\s*(\d+)px solid var\(--accent\)", body).group(1))
    assert re.search(r"border-radius:\s*50%", body) and re.search(r"\*\s*\{\s*box-sizing:\s*border-box", css)
    return width, border  # border-box: the width includes the border


def inner_share():
    """The ring's inner radius as a share of its outer radius, as in the header's mark."""
    width, border = mark_shape()
    return 1 - 2 * border / width


def read_png(data):
    """(colour type, pixels as rows of (r, g, b, a)) of an 8-bit RGB or RGBA PNG."""
    assert data[:8] == PNG_MAGIC
    pos, idat, header = 8, b"", None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    width, height, depth, colour, _compression, _filter, interlace = header
    assert depth == 8 and colour in (2, 6) and interlace == 0, header
    channels = 4 if colour == 6 else 3
    stride = width * channels
    raw = zlib.decompress(idat)
    rows, prev = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1 : start + 1 + stride])
        if kind:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                if kind == 1:
                    line[i] = (line[i] + a) & 255
                elif kind == 2:
                    line[i] = (line[i] + b) & 255
                elif kind == 3:
                    line[i] = (line[i] + (a + b) // 2) & 255
                else:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    opaque = (255,) if channels == 3 else ()
    return colour, [[tuple(row[x * channels : (x + 1) * channels]) + opaque for x in range(width)] for row in rows]


def ico_images(data):
    """(size, PNG bytes) of each picture in an .ico file."""
    assert data[:4] == b"\x00\x00\x01\x00"
    found = []
    for i in range(struct.unpack("<H", data[4:6])[0]):
        width, _h, _c, _r, _planes, _bits, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i : 22 + 16 * i])
        found.append((width or 256, data[offset : offset + length]))
    return found


def maskable(path):
    manifest = json.loads((ROOT / "web" / "manifest.json").read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        if icon["src"] == f"/static/{path.name}":
            return "maskable" in icon.get("purpose", "").split()
    return False


def icons():
    """Every drawn PNG: (name, PNG bytes, full square). iPhone's touch icon fills its square, opaque: iPhone rounds
    the corners itself and shows transparent parts as black. So does a manifest icon marked maskable."""
    found = []
    for name in ("assets/icon.png", "snap/gui/porthole.png"):
        found.append((name, (ROOT / name).read_bytes(), False))
    for size, data in ico_images((ROOT / "assets" / "icon.ico").read_bytes()):
        found.append((f"assets/icon.ico ({size} px)", data, False))
    for size in (180, 192, 512):
        path = ROOT / "web" / f"icon-{size}.png"
        found.append((f"web/{path.name}", path.read_bytes(), size == 180 or maskable(path)))
    return found


def whiteness(pixel):
    """How far a colour lies from the tile blue (0) to white (1)."""
    r, g = pixel[0], pixel[1]
    return ((r - BLUE[0]) + (g - BLUE[1])) / ((255 - BLUE[0]) + (255 - BLUE[1]))


def on_blue_white_line(pixel):
    w = whiteness(pixel)
    return all(abs(pixel[i] - (BLUE[i] + w * (255 - BLUE[i]))) <= 3 for i in range(3))


def tool():
    spec = importlib.util.spec_from_file_location("make_icons", ROOT / "tools" / "make_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MarkTest(unittest.TestCase):
    def test_the_header_mark_is_a_ring(self):
        self.assertEqual(mark_shape(), (22, 5))


class PngIconsTest(unittest.TestCase):
    """Every PNG: assets/icon.png, the pictures inside assets/icon.ico, the snap's icon and the web icons."""

    def setUp(self):
        self.icons = [(name, *read_png(data), full) for name, data, full in icons()]
        self.assertEqual(len(self.icons), 9)

    def test_square_files(self):
        for name, _colour, pixels, _full in self.icons:
            with self.subTest(name):
                self.assertEqual(len(pixels), len(pixels[0]))

    def test_centre_is_the_tile_blue(self):
        for name, _colour, pixels, _full in self.icons:
            size = len(pixels)
            with self.subTest(name):
                self.assertEqual(pixels[size // 2][size // 2], BLUE + (255,))

    def test_the_ring_is_white_on_all_four_sides(self):
        middle = OUTER[0] * (1 + inner_share()) / 2  # the middle of the ring, whatever its size within the range
        for name, _colour, pixels, _full in self.icons:
            size = len(pixels)
            c, k = size // 2, int(size / 2 + middle * size)
            with self.subTest(name):
                for x, y in ((k, c), (size - 1 - k, c), (c, k), (c, size - 1 - k)):
                    r, g, b, a = pixels[y][x]
                    self.assertEqual(a, 255, (x, y))
                    self.assertGreaterEqual(min(r, g, b), 240, (x, y))

    def test_nothing_but_the_ring_on_the_tile(self):
        # Within one pixel of the ring's possible edges colours may blend (smooth edges); everywhere else a
        # pixel is pure tile blue, pure white or transparent. No other colour anywhere.
        share = inner_share()
        inner, outer = (OUTER[0] * share, OUTER[1] * share), OUTER
        for name, _colour, pixels, _full in self.icons:
            size = len(pixels)
            with self.subTest(name):
                for y, row in enumerate(pixels):
                    for x, pixel in enumerate(row):
                        d = math.hypot(x + 0.5 - size / 2, y + 0.5 - size / 2)
                        if d < outer[1] * size + 1:
                            self.assertEqual(pixel[3], 255, (x, y))  # the ring sits on solid tile
                        if not pixel[3]:
                            continue
                        self.assertTrue(on_blue_white_line(pixel), (x, y, pixel))
                        if d < inner[0] * size - 1 or d > outer[1] * size + 1:
                            self.assertEqual(pixel[:3], BLUE, (x, y))
                        elif inner[1] * size + 1 < d < outer[0] * size - 1:
                            self.assertEqual(pixel[:3], WHITE, (x, y))

    def test_ring_size_and_thickness_match_the_header_mark(self):
        width, border = mark_shape()
        for name, _colour, pixels, _full in self.icons:
            size = len(pixels)
            if size < 128:
                continue  # too few pixels to measure to a hundredth
            with self.subTest(name):
                c = size // 2
                lines = [  # from the centre outwards, right, left, down and up
                    [pixels[c][x] for x in range(c, size)],
                    [pixels[c][x] for x in range(c - 1, -1, -1)],
                    [pixels[y][c] for y in range(c, size)],
                    [pixels[y][c] for y in range(c - 1, -1, -1)],
                ]
                for line in lines:
                    weights = [whiteness(p) for p in line]
                    thickness = sum(weights)
                    middle = sum(w * (i + 0.5) for i, w in enumerate(weights)) / thickness
                    outer = middle + thickness / 2
                    self.assertGreaterEqual(2 * outer / size, 2 * OUTER[0])
                    self.assertLessEqual(2 * outer / size, 2 * OUTER[1])
                    self.assertAlmostEqual(thickness / (2 * outer), border / width, delta=0.01)

    def test_corners(self):
        for name, colour, pixels, full in self.icons:
            size = len(pixels)
            corners = [pixels[0][0], pixels[0][-1], pixels[-1][0], pixels[-1][-1]]
            edges = [pixels[0][size // 2], pixels[-1][size // 2], pixels[size // 2][0], pixels[size // 2][-1]]
            with self.subTest(name):
                for pixel in edges:
                    self.assertEqual(pixel, BLUE + (255,))  # the tile reaches the edges
                if full:
                    self.assertEqual(colour, 2)  # no alpha at all
                    for pixel in corners:
                        self.assertEqual(pixel, BLUE + (255,))
                else:
                    for pixel in corners:
                        self.assertEqual(pixel[3], 0)  # rounded, with transparent corners

    def test_corner_radius(self):
        # Measured from the clear area each corner leaves, r² (1 - π/4): smooth edges make that hold to a
        # fraction of a pixel, so it works at 16 px as well as at 512.
        for name, _colour, pixels, full in self.icons:
            if full:
                continue
            size = len(pixels)
            block = range(size // 4)  # a corner, well clear of the ring
            with self.subTest(name):
                for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
                    area = 0.0
                    for y in block:
                        row = pixels[size - 1 - y if flip_y else y]
                        for x in block:
                            area += 1 - row[size - 1 - x if flip_x else x][3] / 255
                    radius = math.sqrt(area / (1 - math.pi / 4)) / size
                    self.assertAlmostEqual(radius, CORNER, delta=0.4 / 64, msg=(flip_x, flip_y))

    def test_iphone_icon_is_opaque(self):
        (name, colour, pixels, full), = [icon for icon in self.icons if icon[0] == "web/icon-180.png"]
        self.assertTrue(full)
        self.assertEqual(colour, 2)
        self.assertEqual({p[3] for row in pixels for p in row}, {255})

    def test_maskable_icons_keep_the_ring_in_the_safe_zone(self):
        # Not marked maskable today; if they ever are, the ring (at most 60 % wide) already fits in its safe zone.
        self.assertLess(OUTER[1], SAFE_ZONE)

    def test_smooth_edges(self):
        for name, _colour, pixels, full in self.icons:
            flat = [p for row in pixels for p in row]
            with self.subTest(name):
                blends = {round(whiteness(p), 2) for p in flat if p[3] == 255 and 0.05 < whiteness(p) < 0.95}
                self.assertGreaterEqual(len(blends), 3)  # the ring's edges are anti-aliased
                if not full:
                    alphas = {p[3] for p in flat if 0 < p[3] < 255}
                    self.assertGreaterEqual(len(alphas), 2)  # so are the tile's corners (at 16 px a 3.5 px curve)


class SvgIconTest(unittest.TestCase):
    """web/icon.svg: the browser tab and the title bar of Porthole's own app window."""

    def setUp(self):
        self.text = (ROOT / "web" / "icon.svg").read_text(encoding="utf-8")
        self.svg = ET.fromstring(self.text)

    def shapes(self, tag):
        return self.svg.findall(f"{SVG_NS}{tag}")

    def test_only_a_tile_and_a_ring(self):
        self.assertEqual(self.svg.get("viewBox"), "0 0 64 64")
        # Every element: no monitor, waves, gradients, filters or text.
        self.assertEqual([child.tag.removeprefix(SVG_NS) for child in self.svg.iter()][1:], ["rect", "circle"])
        (tile,), (ring,) = self.shapes("rect"), self.shapes("circle")
        self.assertLessEqual(set(tile.attrib), {"width", "height", "rx", "fill"})
        self.assertLessEqual(set(ring.attrib), {"cx", "cy", "r", "fill", "stroke", "stroke-width"})

    def test_tile(self):
        (tile,) = self.shapes("rect")
        self.assertEqual((tile.get("width"), tile.get("height")), ("64", "64"))
        self.assertEqual(tile.get("fill").lower(), "#2563eb")
        self.assertEqual(float(tile.get("rx")) / 64, CORNER)

    def test_white_ring_like_the_header_mark(self):
        (ring,) = self.shapes("circle")
        self.assertEqual((ring.get("cx"), ring.get("cy")), ("32", "32"))
        self.assertEqual(ring.get("fill"), "none")
        self.assertIn(ring.get("stroke").lower(), ("#fff", "#ffffff", "white"))
        radius, stroke = float(ring.get("r")), float(ring.get("stroke-width"))
        outer = radius + stroke / 2
        self.assertGreaterEqual(outer / 64, OUTER[0])
        self.assertLessEqual(outer / 64, OUTER[1])
        width, border = mark_shape()
        self.assertAlmostEqual(stroke / (2 * outer), border / width, places=3)

    def test_drawn_by_the_tool(self):
        # One set of numbers for the SVG and the PNGs: tools/make_icons.py writes both.
        self.assertEqual(self.text, tool().svg())
