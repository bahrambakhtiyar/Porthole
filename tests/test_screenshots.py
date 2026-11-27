"""The README pictures (docs/screenshots/), made by tools/store_images.mjs with the store pictures.

The tool needs Chrome and takes a while, so it isn't run here: run `node tools/store_images.mjs --readme-only`
(or without the option for the store pictures too) by hand after changing the pages.
"""

import re
import shutil
import struct
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
TOOL = ROOT / "tools" / "store_images.mjs"
PAIRS = (("hero-light.png", "hero-dark.png"), ("allow-light.png", "allow-dark.png"))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NODE = shutil.which("node")


def picture(text: str, light: str, dark: str) -> str:
    """The <picture> in `text` that shows `light`, or `dark` in a dark theme (GitHub follows the reader's theme)."""
    for block in re.findall(r"<picture>.*?</picture>", text, re.S):
        if f'src="docs/screenshots/{light}"' in block:
            assert f'<source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/{dark}">' in block, block
            return block
    raise AssertionError(f"no <picture> shows docs/screenshots/{light}")


def readme_section(heading: str) -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}\n")
    return text[start : text.index("\n## ", start + 1)]


class ReadmePicturesTest(unittest.TestCase):
    def test_they_are_small_pngs_of_one_size(self):
        for pair in PAIRS:
            for name in pair:
                with self.subTest(name=name):
                    data = (SHOTS / name).read_bytes()
                    self.assertEqual(data[:8], PNG_SIGNATURE)
                    self.assertEqual(struct.unpack(">II", data[16:24]), (1600, 900))  # README_SIZE in the tool
                    self.assertLess(len(data), 1_000_000, "keep README pictures small")

    def test_only_these_pictures_are_there(self):
        # The old raw page captures and the demo animation are gone; nothing is left behind unused.
        self.assertEqual(sorted(path.name for path in SHOTS.iterdir()), sorted(name for pair in PAIRS for name in pair))

    def test_the_readme_shows_the_hero_at_the_top(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        block = picture(text, "hero-light.png", "hero-dark.png")
        self.assertRegex(block, r'<img src="[^"]+" alt="[^"]{40,}" width="800">')
        self.assertLess(text.index("docs/screenshots/hero-light.png"), text.index("\n## Why Porthole\n"))

    def test_the_readme_shows_allow_in_the_three_steps(self):
        block = picture(readme_section("Share your screen in 3 steps"), "allow-light.png", "allow-dark.png")
        self.assertRegex(block, r'<img src="[^"]+" alt="[^"]{40,}" width="800">')
        self.assertNotRegex(block, r"alt=\"[^\"]*\b\d{3}\b")  # no code from one capture in the words

    def test_the_tool_makes_them(self):
        text = TOOL.read_text(encoding="utf-8")
        for words in ("const README_OUT = path.join(ROOT, 'docs', 'screenshots');", "const README_SIZE = { width: 1600, height: 900 };",
                      "['hero-light', readmeHero(pages, false)]", "['hero-dark', readmeHero(pages, true)]",
                      "['allow-light', readmeAllow(pages, false)]", "['allow-dark', readmeAllow(pages, true)]",
                      "'--readme-only'", "sharerWaitingDark", "phoneWaitingDark"):
            self.assertIn(words, text)

    def test_the_tool_uses_the_example_address(self):
        text = TOOL.read_text(encoding="utf-8")
        for words in ("'192.168.1.20'", "'--no-browser'", "'--host'", "'--record-dir'", "/api/quit", "porthole-settings", "easy: false"):
            self.assertIn(words, text)

    def test_the_old_tools_are_gone_everywhere(self):
        self.assertFalse((ROOT / "tools" / "screenshots.mjs").exists())
        self.assertFalse((ROOT / "tools" / "demo.mjs").exists())
        for path in [ROOT / "README.md", ROOT / "docs" / "design.md", ROOT / "packaging" / "snapstore" / "listing.md",
                     ROOT / "tests" / "browser" / "support.mjs"]:
            text = path.read_text(encoding="utf-8")
            for old in ("tools/screenshots.mjs", "tools/demo.mjs", "demo-light.gif", "sharer-light.png"):
                self.assertNotIn(old, text, f"{path.name} still names {old}")

    def test_the_file_list_says_the_tool_makes_them(self):
        text = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
        start = text.index("\n## How it works\n")
        section = text[start : text.index("\n## ", start + 1)]
        self.assertIn("`node tools/store_images.mjs --readme-only`", section)

    @unittest.skipUnless(NODE, "node not installed (only needed for the browser-code tests)")
    def test_the_tool_parses(self):
        result = subprocess.run([NODE, "--check", str(TOOL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
