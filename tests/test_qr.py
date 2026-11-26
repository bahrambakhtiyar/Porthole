import unittest

from porthole.qr import encode, to_svg

try:
    import numpy
    import zxingcpp

    HAVE_ZXING = True
except ImportError:
    HAVE_ZXING = False


def read_format_copies(m):
    """Return the two 15-bit format words (bit i at index i) as integers."""
    size = len(m)
    first = 0
    second = 0
    # First copy, around the top-left finder (row, col).
    spots1 = [(i, 8) for i in range(6)] + [(7, 8), (8, 8), (8, 7)] + [(8, 14 - i) for i in range(9, 15)]
    # Second copy, split between the top-right and bottom-left finders.
    spots2 = [(8, size - 1 - i) for i in range(8)] + [(size - 15 + i, 8) for i in range(8, 15)]
    for i, (r, c) in enumerate(spots1):
        first |= int(m[r][c]) << i
    for i, (r, c) in enumerate(spots2):
        second |= int(m[r][c]) << i
    return first, second


def decode(matrix):
    scale, border = 8, 4
    size = len(matrix)
    side = (size + 2 * border) * scale
    image = numpy.full((side, side), 255, dtype=numpy.uint8)
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                y, x = (r + border) * scale, (c + border) * scale
                image[y : y + scale, x : x + scale] = 0
    result = zxingcpp.read_barcode(image)
    return result.text if result else None


class QrTest(unittest.TestCase):
    def test_version_sizes(self):
        for n, size in [(1, 21), (14, 21), (15, 25), (42, 29), (43, 33), (122, 45), (180, 53)]:
            self.assertEqual(len(encode(b"a" * n)), size, n)

    def test_matrix_is_square_booleans(self):
        m = encode(b"hello")
        self.assertTrue(all(len(row) == len(m) for row in m))
        self.assertTrue(all(isinstance(v, bool) for row in m for v in row))

    def test_too_long_raises(self):
        self.assertRaises(ValueError, encode, b"a" * 181)

    def test_finder_patterns(self):
        m = encode(b"finder")
        size = len(m)
        for top, left in [(0, 0), (0, size - 7), (size - 7, 0)]:
            for r in range(7):
                for c in range(7):
                    ring = max(abs(r - 3), abs(c - 3))
                    self.assertEqual(m[top + r][left + c], ring != 2, (top, left, r, c))

    def test_timing_pattern(self):
        m = encode(b"timing" * 5)
        size = len(m)
        for i in range(8, size - 8):
            self.assertEqual(m[6][i], i % 2 == 0, ("row", i))
            self.assertEqual(m[i][6], i % 2 == 0, ("col", i))

    def test_dark_module(self):
        m = encode(b"x")
        self.assertTrue(m[len(m) - 8][8])

    def test_format_info_is_level_m(self):
        for text in (b"a", b"http://192.168.0.31:8765/v/AbCdEf12", b"z" * 150):
            first, second = read_format_copies(encode(text))
            self.assertEqual(first, second)
            word = first ^ 0x5412
            data = word >> 10
            self.assertEqual(data >> 3, 0b00)  # level M
            rem = data
            for _ in range(10):
                rem = (rem << 1) ^ ((rem >> 9) * 0x537)
            self.assertEqual(word & 0x3FF, rem & 0x3FF)

    def test_version_info_for_version_7(self):
        m = encode(b"a" * 110)  # 107-122 bytes -> version 7
        size = len(m)
        self.assertEqual(size, 45)
        bits_a = bits_b = 0
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            bits_a |= int(m[b][a]) << i
            bits_b |= int(m[a][b]) << i
        self.assertEqual(bits_a, bits_b)
        self.assertEqual(bits_a >> 12, 7)

    def test_svg_shape(self):
        svg = to_svg(encode(b"hi"))
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('viewBox="0 0 29 29"', svg)
        self.assertIn("<path", svg)
        self.assertIn('shape-rendering="crispEdges"', svg)
        self.assertNotIn("style=", svg)

    @unittest.skipUnless(HAVE_ZXING, "zxingcpp not installed")
    def test_decodes_with_reference_decoder(self):
        for text in ["http://192.168.0.31:8765/v/AbCdEf12", "a", "x" * 14, "y" * 42, "z" * 100, "v" * 110, "u" * 150, "w" * 180, "Ə ş ğ"]:
            self.assertEqual(decode(encode(text.encode())), text, text)


if __name__ == "__main__":
    unittest.main()
