"""The docs, which go into the public repository as they are."""

import re
import tempfile
import unittest
from pathlib import Path

from tests.private_docs import NOTES, private_doc, read_private_doc

ROOT = Path(__file__).resolve().parent.parent
BRANCH = "archive/windows-macos"
NEW_BLOCK = re.compile(r"^\s*(?:$|#|[-*]\s|\d+\.\s)")  # a blank line, a heading or a list item


def blocks(text: str) -> list[str]:
    """Paragraphs and list items, each one whole."""
    found, current = [], []
    for line in text.splitlines():
        if NEW_BLOCK.match(line) and current:
            found.append("\n".join(current))
            current = []
        if line.strip():
            current.append(line)
    if current:
        found.append("\n".join(current))
    return found


class ArchiveBranchTest(unittest.TestCase):
    def test_every_pointer_to_the_archive_branch_says_it_is_in_the_private_repository(self):
        # The public repository starts fresh from main's files and never gets that branch, so a doc that sends
        # readers to it must say where it is.
        docs = [*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md"), *(ROOT / "packaging").rglob("*.md")]
        pointers = 0
        for path in docs:
            for block in blocks(path.read_text(encoding="utf-8")):
                if BRANCH in block:
                    pointers += 1
                    with self.subTest(path=path.relative_to(ROOT).as_posix(), block=block.strip()[:80]):
                        self.assertRegex(block, r"(?i)\bprivate\b")
        self.assertGreater(pointers, 0)

    def test_blocks(self):
        text = "# Title\n\nOne\nparagraph.\n- an item\n  carried on\n- another\n1. numbered\n"
        self.assertEqual(blocks(text), ["# Title", "One\nparagraph.", "- an item\n  carried on", "- another", "1. numbered"])


class PrivateDocTest(unittest.TestCase):
    """The project's private notes are found in private/ first, then in docs/; without them their tests skip."""

    def test_private_first_then_docs_then_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(private_doc(NOTES[0], root))
            (root / "docs").mkdir()
            (root / "docs" / NOTES[0]).write_text("in docs")
            self.assertEqual(private_doc(NOTES[0], root), root / "docs" / NOTES[0])
            (root / "private").mkdir()
            (root / "private" / NOTES[0]).write_text("in private")
            self.assertEqual(private_doc(NOTES[0], root), root / "private" / NOTES[0])
            (root / "private" / NOTES[1]).mkdir()  # a folder by that name is not the note
            self.assertIsNone(private_doc(NOTES[1], root))

    def test_a_missing_note_skips_with_the_reason(self):
        case = unittest.TestCase()
        with self.assertRaises(unittest.SkipTest) as caught:
            read_private_doc(case, "no-such-note.md")
        self.assertIn("private notes", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
