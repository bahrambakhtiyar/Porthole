"""The README's note on how Porthole is made: it says plainly that an AI wrote most of it, and who decides and tests.

Launch communities ask for this (some require an AI disclosure), and the repository already credits Claude in its
commit history, so the README says it up front instead of leaving people to find out."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def section(text: str, heading: str) -> str:
    start = text.index(f"\n## {heading}\n")
    return text[start : text.index("\n## ", start + 1)]


class HowItIsMadeTest(unittest.TestCase):
    def setUp(self):
        self.text = README.read_text(encoding="utf-8")
        self.note = section(self.text, "How Porthole is made")

    def test_says_an_ai_wrote_most_of_it(self):
        self.assertIn("with a lot of help from Claude, Anthropic's AI, which wrote most of the code and the docs", self.note)

    def test_says_who_decides_and_tests(self):
        self.assertIn("Bahram decides what Porthole does", self.note)
        self.assertIn("tests it on his own Ubuntu PC with an iPhone and an Android phone", self.note)

    def test_points_to_the_checks_and_the_design_notes(self):
        self.assertIn("[docs/manual-tests.md](docs/manual-tests.md)", self.note)
        self.assertIn("[docs/design.md](docs/design.md)", self.note)
        for path in ("docs/manual-tests.md", "docs/design.md"):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_comes_right_after_how_it_works(self):
        self.assertLess(self.text.index("\n## How it works\n"), self.text.index("\n## How Porthole is made\n"))
        self.assertLess(self.text.index("\n## How Porthole is made\n"), self.text.index("\n## Troubleshooting\n"))

    def test_the_test_count_it_claims_holds(self):
        # "over a thousand automated tests": count the test methods, so the claim can't quietly go stale.
        count = sum(
            line.lstrip().startswith("def test_")
            for path in (ROOT / "tests").glob("test_*.py")
            for line in path.read_text(encoding="utf-8").splitlines()
        )
        self.assertIn("over a thousand automated tests", self.note)
        self.assertGreater(count, 1000)


if __name__ == "__main__":
    unittest.main()
