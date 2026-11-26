"""The README pictures (docs/screenshots/) and the tools that make them (tools/screenshots.mjs, tools/demo.mjs).

The tools themselves need Chrome (and ffmpeg for the demo) and take a while, so they aren't run here: run
`node tools/screenshots.mjs` and `node tools/demo.mjs` by hand.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
TOOL = ROOT / "tools" / "screenshots.mjs"
DEMO_TOOL = ROOT / "tools" / "demo.mjs"
NAMES = ("sharer-light.png", "sharer-dark.png", "viewer-light.png", "viewer-dark.png")
DEMOS = ("demo-light.gif", "demo-dark.gif")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NODE = shutil.which("node")


def picture(text: str, light: str, dark: str) -> str:
    """The <picture> in `text` that shows `light`, or `dark` in a dark theme (GitHub follows the reader's theme)."""
    for block in re.findall(r"<picture>.*?</picture>", text, re.S):
        if f'src="docs/screenshots/{light}"' in block:
            assert f'srcset="docs/screenshots/{dark}"' in block, block
            return block
    raise AssertionError(f"no <picture> shows docs/screenshots/{light}")


def gif_frames(data: bytes) -> tuple[list[int], int | None]:
    """The delay of each frame in hundredths of a second, and the loop count (0: for ever; None: plays once)."""
    assert data[:6] == b"GIF89a", data[:6]
    flags = data[10]
    pos = 13 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)  # after the global colour table

    def skip_sub_blocks(pos):
        while data[pos]:
            pos += data[pos] + 1
        return pos + 1

    delays, loop, delay = [], None, 0
    while True:
        kind = data[pos]
        if kind == 0x3B:  # trailer
            return delays, loop
        if kind == 0x21:  # extension
            label = data[pos + 1]
            if label == 0xF9:  # graphic control: the next frame's delay
                delay = int.from_bytes(data[pos + 4 : pos + 6], "little")
            elif label == 0xFF and data[pos + 3 : pos + 14] == b"NETSCAPE2.0":
                loop = int.from_bytes(data[pos + 16 : pos + 18], "little")
            pos = skip_sub_blocks(pos + 2)
        elif kind == 0x2C:  # an image
            flags = data[pos + 9]
            pos += 10 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)
            pos = skip_sub_blocks(pos + 1)  # after the LZW code size
            delays.append(delay)
            delay = 0
        else:
            raise AssertionError(f"not a GIF block at {pos}: {kind:#x}")


class ScreenshotTest(unittest.TestCase):
    def test_the_screenshots_are_pngs(self):
        for name in NAMES:
            with self.subTest(name=name):
                data = (SHOTS / name).read_bytes()
                self.assertEqual(data[:8], PNG_SIGNATURE)
                self.assertLess(len(data), 500_000, "keep README pictures small")

    def test_the_readme_shows_them(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        # Each one in a <picture> that follows the reader's light or dark theme on GitHub.
        for page in ("sharer", "viewer"):
            with self.subTest(page=page):
                block = picture(text, f"{page}-light.png", f"{page}-dark.png")
                self.assertIn(f'<source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/{page}-dark.png">', block)
                self.assertRegex(block, r'<img src="[^"]+" alt="[^"]{20,}"')
        # Near the top: before the install section.
        self.assertLess(text.index("docs/screenshots/"), text.index("\n## Install\n"))

    def test_the_file_list_names_the_tools(self):
        # The list of files, with what each tool makes, moved from the README to docs/design.md.
        text = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
        start = text.index("\n## How it works\n")
        section = text[start : text.index("\n## ", start + 1)]
        self.assertIn("`tools/screenshots.mjs`", section)
        self.assertIn("`tools/demo.mjs`", section)
        self.assertIn("`node tools/demo.mjs`", section)

    def test_the_tool_uses_the_example_address(self):
        text = TOOL.read_text(encoding="utf-8")
        for words in ("'192.168.1.20'", "'--no-browser'", "'--host'", "'--record-dir'", "/api/quit", "porthole-settings", "easy: false"):
            self.assertIn(words, text)

    @unittest.skipUnless(NODE, "node not installed (only needed for the browser-code tests)")
    def test_the_tool_parses(self):
        result = subprocess.run([NODE, "--check", str(TOOL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


class DemoTest(unittest.TestCase):
    """The short looping demo in the README's "Share your screen in 3 steps", made by tools/demo.mjs."""

    def test_the_demos_are_short_looping_gifs(self):
        for name in DEMOS:
            with self.subTest(name=name):
                data = (SHOTS / name).read_bytes()
                self.assertLess(len(data), 2_000_000, "keep the demo small")
                delays, loop = gif_frames(data)
                self.assertGreater(len(delays), 10, "an animation, not a still picture")
                self.assertEqual(loop, 0, "loops for ever")
                self.assertTrue(4 <= sum(delays) / 100 <= 15, f"a few seconds, not {sum(delays) / 100} s")

    def test_gif_frames(self):
        # A tiny GIF: one 1 × 1 frame shown for 0.5 s, looping for ever.
        gif = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
               b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
               b"\x21\xf9\x04\x00\x32\x00\x00\x00"
               b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b")
        self.assertEqual(gif_frames(gif), ([50], 0))

    def test_the_readme_shows_it_in_the_three_steps(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        start = text.index("\n## Share your screen in 3 steps\n")
        section = text[start : text.index("\n## ", start + 1)]
        block = picture(section, "demo-light.gif", "demo-dark.gif")
        self.assertIn('<source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/demo-dark.gif">', block)
        self.assertRegex(block, r'<img src="[^"]+" alt="[^"]{20,}"')

    def test_the_tool_uses_the_example_address_and_a_port_of_its_own(self):
        text = DEMO_TOOL.read_text(encoding="utf-8")
        for words in ("'192.168.1.20'", "'--no-browser'", "'--host'", "'--record-dir'", "/api/quit", "porthole-settings",
                      "easy: false", "port > 8800", "demo-${theme}.gif", "'-loop', '0'"):
            self.assertIn(words, text)

    @unittest.skipUnless(NODE, "node not installed (only needed for the browser-code tests)")
    def test_the_tool_parses(self):
        result = subprocess.run([NODE, "--check", str(DEMO_TOOL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
