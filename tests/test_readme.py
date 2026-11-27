"""The README's one-line credit: Porthole is built by Bahram Bakhtiyar with help from Claude, Anthropic's AI.

Some launch communities ask for an AI disclosure, and the commit history already credits Claude, so the README says
it plainly. The owner wants just that line, without detail on who did what (27 Nov 2026)."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CREDIT = "Porthole is built by Bahram Bakhtiyar with help from Claude, Anthropic's AI."


def section(text: str, heading: str) -> str:
    start = text.index(f"\n## {heading}\n")
    return text[start : text.index("\n## ", start + 1)]


class CreditTest(unittest.TestCase):
    def setUp(self):
        self.text = README.read_text(encoding="utf-8")

    def test_one_line_under_how_it_works(self):
        self.assertIn(f"\n{CREDIT}\n", section(self.text, "How it works"))
        self.assertEqual(self.text.count("Claude"), 1)  # said once, with no more detail

    def test_no_section_of_its_own(self):
        self.assertNotIn("\n## How Porthole is made\n", self.text)


if __name__ == "__main__":
    unittest.main()
