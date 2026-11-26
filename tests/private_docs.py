"""The project's private notes: its plans and its steps for going public. They never go into the public repository.
On the owner's PC they are in private/, which git ignores; in the private repository they were in docs/. A test that
checks one finds it here and skips, saying why, when this copy doesn't have it (the public repository's CI has none).

NOTES is the one list of them. tools/export_public.sh reads it to leave them out of the public copy, and refuses a
copy in which any other file still names one: this file is the only one that may.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES = ("plan.md", "plan-v2.md", "plan-v3.md", "plan-v4.md", "going-public.md")  # a plain tuple: the export reads it
PLAN = NOTES[3]  # the current plan
GOING_PUBLIC = NOTES[4]  # the steps for going public


def private_doc(name: str, root: Path = ROOT) -> Path | None:
    """private/<name>, else docs/<name>, else None."""
    for path in (root / "private" / name, root / "docs" / name):
        if path.is_file():
            return path
    return None


def read_private_doc(test: unittest.TestCase, name: str) -> str:
    """The text of a private note; skips `test` when this copy doesn't have it."""
    path = private_doc(name)
    if path is None:
        test.skipTest(f"{name} is one of the project's private notes (private/ or docs/), and this copy doesn't have it")
    return path.read_text(encoding="utf-8")
