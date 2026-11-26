"""The licence: LICENSE is the GNU AGPL-3.0 text, the README says so, and nothing still says it is coming later.

Each package carrying LICENSE is tested next to its build: tests/test_packaging.py (.deb, AppImage, snap).
"""

import hashlib
import re
import unittest
from pathlib import Path

from tests.private_docs import GOING_PUBLIC, PLAN, private_doc, read_private_doc

ROOT = Path(__file__).resolve().parent.parent
LICENSE = ROOT / "LICENSE"
README = ROOT / "README.md"
DESIGN = ROOT / "docs" / "design.md"
SOURCE = "https://www.gnu.org/licenses/agpl-3.0.txt"
# The file as gnu.org serves it (fetched on 23 Nov 2026). Never edit it.
SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"


class LicenceFileTest(unittest.TestCase):
    def setUp(self):
        self.data = LICENSE.read_bytes()
        self.lines = self.data.decode("ascii").split("\n")

    def test_is_the_agpl_3_text(self):
        self.assertEqual(self.lines[0].strip(), "GNU AFFERO GENERAL PUBLIC LICENSE")
        self.assertEqual(self.lines[1].strip(), "Version 3, 19 November 2007")
        stripped = [line.strip() for line in self.lines]
        self.assertIn("13. Remote Network Interaction; Use with the GNU General Public License.", stripped)
        self.assertIn("END OF TERMS AND CONDITIONS", stripped)
        self.assertEqual(len(self.lines) - 1, 661)  # 661 lines, the last one ending in a newline
        self.assertEqual(self.lines[-1], "")

    def test_verbatim_from_gnu_org(self):
        # .gitattributes keeps LF in every checkout (Windows too), so the bytes are the same everywhere.
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), SHA256)

    def test_plain_text_the_packages_can_use(self):
        # The .deb indents it line by line as DEP-5 text.
        self.assertNotIn(b"\r", self.data)
        self.assertNotIn(b"\t", self.data)
        for number, line in enumerate(self.lines, 1):
            self.assertFalse(line.startswith("."), number)  # DEP-5 would read " ." as an empty line
            self.assertEqual(line, line.rstrip(), number)


class ReadmeLicenceTest(unittest.TestCase):
    def section(self) -> str:
        text = README.read_text(encoding="utf-8")
        start = text.index("\n## Licence\n")
        end = text.find("\n## ", start + 1)
        return text[start : end if end > 0 else len(text)]

    def test_near_the_end(self):
        # The README's last section (the tests section it used to follow moved to docs/design.md).
        text = README.read_text(encoding="utf-8")
        self.assertGreater(text.index("\n## Licence\n"), text.index("\n## Contributing\n"))
        self.assertEqual(text.find("\n## ", text.index("\n## Licence\n") + 1), -1)

    def test_says_what_the_licence_is_and_how_to_contribute(self):
        section = self.section()
        for words in ("free software", "GNU Affero General Public License", "[LICENSE](LICENSE)", "(CLA.md)",
                      "(CONTRIBUTING.md)"):
            self.assertIn(words, section)


# Files people read, and the packaging files: none may still say the licence is missing or coming later.
PENDING = re.compile(
    r"(?i)LICENSE comes later|no licen[cs]e (page )?yet|until LICENSE exists|LICENSE needs the AGPL"
    r"|uncomment `?LicenseFile|commented `?LicenseFile"
)


def text_files() -> list[Path]:
    found = [README, *ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md"), *(ROOT / ".github" / "workflows").glob("*.yml"),
             *(ROOT / "snap").rglob("*"), *(ROOT / "packaging").rglob("*")]
    binary = (".png", ".ico", ".pyc")
    return sorted({path for path in found if path.is_file() and path.suffix not in binary})


class NothingSaysTheLicenceIsPendingTest(unittest.TestCase):
    def test_no_pending_wording(self):
        files = text_files()
        self.assertGreater(len(files), 20)
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            match = PENDING.search(text)
            self.assertIsNone(match, f"{path.relative_to(ROOT)}: {match and match.group(0)}")

    def test_going_public_lists_the_licence_as_done(self):
        text = read_private_doc(self, GOING_PUBLIC)
        start = text.index("## 2.")
        section = text[start : text.index("\n## ", start + 1)]
        item = next(line for line in section.splitlines() if "**The licence text" in line)
        for words in ("done", "23 Nov 2026", SOURCE, SHA256):
            self.assertIn(words, item)
        self.assertIn("**The CLA.**", section)  # still waiting for the owner

    def test_third_party_notices_done_for_the_appimage_and_waiting_for_windows_and_mac(self):
        # The AppImage brings its own Python and bundles OpenSSL, whose licence asks for its text with every copy:
        # done on 26 Nov 2026 (THIRD-PARTY-NOTICES.txt). The Windows and Mac half (Python, OpenSSL, libffi) waits
        # on the archive branch with those builds.
        text = read_private_doc(self, GOING_PUBLIC)
        plan = read_private_doc(self, PLAN).splitlines()
        start = text.index("## 2.")
        section = text[start : text.index("\n## ", start + 1)]
        item = next(line for line in section.splitlines() if "**Third-party notices" in line)
        for words in ("AppImage", "done", "26 Nov 2026", "THIRD-PARTY-NOTICES.txt", "OpenSSL", "LICENSE.txt",
                      "GPL", "archive/windows-macos", "private"):
            self.assertIn(words, item)
        self.assertNotIn("still to check", item)
        done = [line for line in plan if line.startswith("- [x] Third-party notices")]
        waiting = [line for line in plan if line.startswith("- [ ] Third-party notices")]
        self.assertEqual(len(done), 1, done)
        self.assertIn("AppImage", done[0])
        self.assertIn("26 Nov 2026", done[0])
        self.assertEqual(len(waiting), 1, waiting)
        self.assertIn("Windows and Mac", waiting[0])
        self.assertNotIn("AppImage", waiting[0])

    def test_the_copyleft_code_still_in_the_packages_waits_for_the_owner(self):
        # Taking the GPL libraries out of the AppImage's Python doesn't leave it without source obligations: the
        # AppImage runtime has libfuse (LGPL-2.1) linked in, and the snap's libnotify-bin brings libnotify and
        # gdk-pixbuf (LGPL) and libjbig0 and shared-mime-info (GPL-2+) that core24 doesn't have. Where their source
        # comes from is still to be decided, in the project's private notes; the docs that give the reason for the
        # trim say so too (the next test).
        text = read_private_doc(self, GOING_PUBLIC)
        plan = read_private_doc(self, PLAN).splitlines()
        start = text.index("## 2.")
        section = text[start : text.index("\n## ", start + 1)]
        item = next(line for line in section.splitlines() if "**Source for the LGPL and GPL code" in line)
        for words in ("AppImage runtime", "libfuse", "LGPL-2.1", "--appimage-version", "snap", "libnotify-bin",
                      "gdk-pixbuf", "libjbig0", "core24", "owner"):
            self.assertIn(words, item)
        waiting = [line for line in plan if line.startswith("- [ ] Source for the LGPL and GPL code")]
        self.assertEqual(len(waiting), 1, waiting)

    def test_the_docs_that_give_the_reason_for_the_trim_name_the_copyleft_code_left(self):
        # The AppImage's README is public, so this runs everywhere; the plan, a private note, only where it is.
        plan = private_doc(PLAN)
        for path in [ROOT / "packaging" / "appimage" / "README.md", *([plan] if plan else [])]:
            text = path.read_text(encoding="utf-8")
            reason = next(line for line in text.splitlines() if "would oblige a source-code offer" in line)
            with self.subTest(path=path.name):
                self.assertIn("libfuse", reason)
                self.assertIn("LGPL-2.1", reason)


class LicenceIsNotFoundByPathsTest(unittest.TestCase):
    """porthole/paths.py never looks for the licence, and no doc says it does."""

    def test_paths_py_doesnt_look_for_it(self):
        self.assertNotIn("LICENSE", (ROOT / "porthole" / "paths.py").read_text(encoding="utf-8"))

    def test_no_doc_says_paths_py_finds_it(self):
        for path in (DESIGN, README):
            text = path.read_text(encoding="utf-8")
            # A sentence (or table cell) that names paths.py must not list the licence among what it finds,
            # nor follow one about the licence when it says "them".
            sentences = re.split(r"(?<=[.;])\s+(?=[A-Z`#])|\n\n|\|", text)
            for before, sentence in zip([""] + sentences, sentences):
                if "paths.py" not in sentence:
                    continue
                with self.subTest(path=path.name, sentence=sentence.strip()[:60]):
                    where = f"{path.relative_to(ROOT)}: {sentence.strip()[:200]}"
                    self.assertNotRegex(sentence, r"(?i)licen[cs]e", where)
                    if re.search(r"\bthem\b", sentence):
                        self.assertNotRegex(before, r"(?i)licen[cs]e", where)


if __name__ == "__main__":
    unittest.main()
