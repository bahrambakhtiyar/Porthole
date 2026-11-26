"""Small QR Code encoder (ISO/IEC 18004): byte mode, error correction level M, versions 1-9.

Enough for a viewer link. No outside libraries.
"""

# version: (EC codewords per block, [(block count, data codewords per block), ...], alignment centres)
_VERSIONS = {
    1: (10, [(1, 16)], []),
    2: (16, [(1, 28)], [6, 18]),
    3: (26, [(1, 44)], [6, 22]),
    4: (18, [(2, 32)], [6, 26]),
    5: (24, [(2, 43)], [6, 30]),
    6: (16, [(4, 27)], [6, 34]),
    7: (18, [(4, 31)], [6, 22, 38]),
    8: (22, [(2, 38), (2, 39)], [6, 24, 42]),
    9: (22, [(3, 36), (2, 37)], [6, 26, 46]),
}
_LEVEL_M = 0b00
_MAX_BYTES = 180


def _data_codewords(version: int) -> int:
    return sum(count * size for count, size in _VERSIONS[version][1])


def _capacity(version: int) -> int:
    return (_data_codewords(version) * 8 - 12) // 8  # 4-bit mode + 8-bit length


# --- Reed-Solomon over GF(256), polynomial 0x11D ---


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


# --- Codewords ---


def _codewords(data: bytes, version: int) -> list[int]:
    total_data = _data_codewords(version)
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in range(length - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), 8)
    for byte in data:
        put(byte, 8)
    capacity_bits = total_data * 8
    put(0, min(4, capacity_bits - len(bits)))
    put(0, (-len(bits)) % 8)
    payload = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(payload) < total_data:
        payload.append(pad)
        pad ^= 0xEC ^ 0x11

    ec_len, groups, _ = _VERSIONS[version]
    divisor = _rs_divisor(ec_len)
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for count, size in groups:
        for _ in range(count):
            block = payload[pos : pos + size]
            pos += size
            data_blocks.append(block)
            ec_blocks.append(_rs_remainder(block, divisor))

    result: list[int] = []
    for i in range(max(len(b) for b in data_blocks)):
        result.extend(block[i] for block in data_blocks if i < len(block))
    for i in range(ec_len):
        result.extend(block[i] for block in ec_blocks)
    return result


# --- Matrix ---


class _Grid:
    def __init__(self, version: int):
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, dark: bool) -> None:
        self.dark[row][col] = dark
        self.function[row][col] = True

    def draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for row, col in [(3, 3), (3, size - 4), (size - 4, 3)]:
            self._finder(row, col)
        centres = _VERSIONS[self.version][2]
        last = len(centres) - 1
        for i, row in enumerate(centres):
            for j, col in enumerate(centres):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                self._alignment(row, col)
        self.draw_format(0)  # reserve; redrawn with the real mask later
        self.draw_version()

    def _finder(self, row: int, col: int) -> None:
        for dr in range(-4, 5):
            for dc in range(-4, 5):
                r, c = row + dr, col + dc
                if 0 <= r < self.size and 0 <= c < self.size:
                    ring = max(abs(dr), abs(dc))
                    self.set(r, c, ring not in (2, 4))

    def _alignment(self, row: int, col: int) -> None:
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                self.set(row + dr, col + dc, max(abs(dr), abs(dc)) != 1)

    def draw_format(self, mask: int) -> None:
        data = (_LEVEL_M << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | (rem & 0x3FF)) ^ 0x5412
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        size = self.size
        for i in range(6):
            self.set(i, 8, bit[i])
        self.set(7, 8, bit[6])
        self.set(8, 8, bit[7])
        self.set(8, 7, bit[8])
        for i in range(9, 15):
            self.set(8, 14 - i, bit[i])
        for i in range(8):
            self.set(8, size - 1 - i, bit[i])
        for i in range(8, 15):
            self.set(size - 15 + i, 8, bit[i])
        self.set(size - 8, 8, True)  # the dark module

    def draw_version(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.version << 12) | (rem & 0xFFF)
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = self.size - 11 + i % 3, i // 3
            self.set(b, a, dark)
            self.set(a, b, dark)

    def draw_codewords(self, codewords: list[int]) -> None:
        size = self.size
        total_bits = len(codewords) * 8
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(size):
                row = size - 1 - vert if upward else vert
                for col in (right, right - 1):
                    if not self.function[row][col] and i < total_bits:
                        self.dark[row][col] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for row in range(self.size):
            for col in range(self.size):
                if not self.function[row][col] and _mask_hit(mask, row, col):
                    self.dark[row][col] = not self.dark[row][col]


def _mask_hit(mask: int, row: int, col: int) -> bool:
    x, y = col, row
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return x * y % 2 + x * y % 3 == 0
    if mask == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    return ((x + y) % 2 + x * y % 3) % 2 == 0


_FINDER_LIKE = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])


def _penalty(m: list[list[bool]]) -> int:
    size = len(m)
    score = 0
    lines = [list(map(int, row)) for row in m] + [[int(m[r][c]) for r in range(size)] for c in range(size)]
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        for i in range(size - 10):
            if line[i : i + 11] in _FINDER_LIKE:
                score += 40
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(map(sum, m))
    total = size * size
    score += 10 * (abs(dark * 20 - total * 10) // total)
    return score


def encode(data: bytes) -> list[list[bool]]:
    """Encode bytes as a QR matrix (True = dark)."""
    if len(data) > _MAX_BYTES:
        raise ValueError(f"QR text too long: {len(data)} bytes (max {_MAX_BYTES})")
    version = next(v for v in _VERSIONS if _capacity(v) >= len(data))
    codewords = _codewords(data, version)
    best: list[list[bool]] | None = None
    best_score = None
    for mask in range(8):
        grid = _Grid(version)
        grid.draw_function_patterns()
        grid.draw_codewords(codewords)
        grid.apply_mask(mask)
        grid.draw_format(mask)
        score = _penalty(grid.dark)
        if best_score is None or score < best_score:
            best, best_score = grid.dark, score
    assert best is not None
    return best


def to_svg(matrix: list[list[bool]], quiet: int = 4) -> str:
    """Render a QR matrix as a crisp black-on-white SVG."""
    side = len(matrix) + 2 * quiet
    parts = [
        f"M{c + quiet} {r + quiet}h1v1h-1z" for r, row in enumerate(matrix) for c, dark in enumerate(row) if dark
    ]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" shape-rendering="crispEdges">'
        f'<rect width="{side}" height="{side}" fill="#fff"/>'
        f'<path d="{"".join(parts)}" fill="#000"/></svg>'
    )
