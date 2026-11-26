"""packaging/appimage/trim.py, which takes out of the AppImage what Porthole never uses (the GPL libraries
among them) before it is packed, and packaging/appimage/THIRD-PARTY-NOTICES.txt, which covers what stays.

The fake AppDir here is laid out like python-appimage's (a manylinux2014 Python 3.12): tiny ELF files that
carry only the NEEDED entries of the real ones, so the trim's library checks run on them.
"""

import hashlib
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIM = ROOT / "packaging" / "appimage" / "trim.py"
NOTICES = ROOT / "packaging" / "appimage" / "THIRD-PARTY-NOTICES.txt"
APACHE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"  # apache.org's LICENSE-2.0.txt
LGPL_SHA256 = "20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95"  # /usr/share/common-licenses/LGPL-2.1
RUNTIME = "https://github.com/AppImage/type2-runtime/commit/"  # what --appimage-version prints, then the commit

# The licence texts as their authors wrote them, whitespace aside. The MIT permission notice with its condition,
# and its disclaimer:
MIT = (
    'Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated '
    'documentation files (the "Software"), to deal in the Software without restriction, including without '
    'limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the '
    'Software, and to permit persons to whom the Software is furnished to do so, subject to the following '
    'conditions: The above copyright notice and this permission notice shall be included in all copies or '
    'substantial portions of the Software.'
)
MIT_DISCLAIMER = (
    'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED '
    'TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL '
    'THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF '
    'CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER '
    'DEALINGS IN THE SOFTWARE.'
)
# The two conditions of the BSD licences, and their disclaimer (whose middle names the holders).
BSD_SOURCE = ("Redistributions of source code must retain the above copyright notice, this list of conditions and the "
              "following disclaimer.")
BSD_BINARY = ("Redistributions in binary form must reproduce the above copyright notice, this list of conditions and "
              "the following disclaimer in the documentation and/or other materials provided with the distribution.")
BSD_DISCLAIMER = re.compile(
    r"THIS SOFTWARE IS PROVIDED BY [A-Z ()]+ \S*AS IS\S* AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT "
    r"LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED\. "
    r"IN NO EVENT SHALL [A-Z ()]+ BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR "
    r"CONSEQUENTIAL DAMAGES \(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF "
    r"USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION\) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN "
    r"CONTRACT, STRICT LIABILITY, OR TORT \(INCLUDING NEGLIGENCE OR OTHERWISE\) ARISING IN ANY WAY OUT OF THE USE "
    r"OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE\."
)
ZLIB = (
    "This software is provided 'as-is', without any express or implied warranty. In no event will the authors be "
    "held liable for any damages arising from the use of this software. Permission is granted to anyone to use this "
    "software for any purpose, including commercial applications, and to alter it and redistribute it freely, "
    "subject to the following restrictions: 1. The origin of this software must not be misrepresented; you must not "
    "claim that you wrote the original software. If you use this software in a product, an acknowledgment in the "
    "product documentation would be appreciated but is not required. 2. Altered source versions must be plainly "
    "marked as such, and must not be misrepresented as being the original software. 3. This notice may not be "
    "removed or altered from any source distribution."
)

_spec = importlib.util.spec_from_file_location("appimage_trim", TRIM)
trim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trim)


def make_elf(needed=(), soname=None, bits=64, little=True, vaddr=0x400000) -> bytes:
    """A minimal ELF shared object: a PT_LOAD over the whole file (mapped at `vaddr`, so addresses and file
    offsets differ, as in real files), a PT_DYNAMIC and its string table."""
    end = "<" if little else ">"
    ehsize, phentsize, dynent = (64, 56, 16) if bits == 64 else (52, 32, 8)
    phoff, phnum = ehsize, 2
    strtab, at = b"\0", {}
    for name in [*needed, *([soname] if soname else [])]:
        at[name] = len(strtab)
        strtab += name.encode() + b"\0"
    strtab_off = phoff + phentsize * phnum
    dyn_off = (strtab_off + len(strtab) + 7) // 8 * 8
    entries = [(1, at[name]) for name in needed]
    if soname:
        entries.append((14, at[soname]))
    entries += [(5, vaddr + strtab_off), (10, len(strtab)), (0, 0)]
    dynsize = dynent * len(entries)
    total = dyn_off + dynsize
    ident = b"\x7fELF" + bytes([2 if bits == 64 else 1, 1 if little else 2, 1, 0]) + bytes(8)
    if bits == 64:
        header = ident + struct.pack(end + "HHIQQQIHHHHHH", 3, 62, 1, 0, phoff, 0, 0, ehsize, phentsize, phnum, 64, 0, 0)
        load = struct.pack(end + "IIQQQQQQ", 1, 5, 0, vaddr, vaddr, total, total, 0x1000)
        dyn = struct.pack(end + "IIQQQQQQ", 2, 6, dyn_off, vaddr + dyn_off, vaddr + dyn_off, dynsize, dynsize, 8)
        body = b"".join(struct.pack(end + "qQ", tag, value) for tag, value in entries)
    else:
        header = ident + struct.pack(end + "HHIIIIIHHHHHH", 3, 3, 1, 0, phoff, 0, 0, ehsize, phentsize, phnum, 40, 0, 0)
        load = struct.pack(end + "IIIIIIII", 1, 0, vaddr, vaddr, total, total, 5, 0x1000)
        dyn = struct.pack(end + "IIIIIIII", 2, dyn_off, vaddr + dyn_off, vaddr + dyn_off, dynsize, dynsize, 6, 4)
        body = b"".join(struct.pack(end + "iI", tag, value) for tag, value in entries)
    data = header + load + dyn + strtab
    return data + bytes(dyn_off - len(data)) + body


LIBC = ["libc.so.6"]
EXT = ".cpython-312-x86_64-linux-gnu.so"
PY = "opt/python3.12/lib/python3.12"

# usr/lib of the real AppImage: file name -> NEEDED (system libraries left out where nothing hinges on them).
LIBRARIES = {
    "libssl.so.3": ["libcrypto.so.3", *LIBC],
    "libcrypto.so.3": ["libdl.so.2", *LIBC],
    "libffi.so.6": LIBC,
    "libbz2.so.1": LIBC,
    "liblzma.so.5": ["libpthread.so.0", *LIBC],
    "libreadline.so.6": ["libtinfo.so.5", *LIBC],
    "libgdbm.so.4": LIBC,
    "libgdbm_compat.so.4": ["libgdbm.so.4", *LIBC],
    "libtcl8.6.so": ["libz.so.1", *LIBC],
    "libtk8.6.so": ["libXft.so.2", "libX11.so.6", *LIBC],
    "libXft.so.2": ["libXrender.so.1", "libX11.so.6", *LIBC],
    "libXrender.so.1": ["libX11.so.6", *LIBC],
    "libncursesw.so.5": ["libtinfo.so.5", *LIBC],
    "libpanelw.so.5": ["libncursesw.so.5", *LIBC],
    "libtinfo.so.5": LIBC,
    "libsqlite3.so": ["libz.so.1", *LIBC],
    "libcrypt.so.2": LIBC,
    "libnsl.so.1": LIBC,
}
KEPT_LIBRARIES = ["libssl.so.3", "libcrypto.so.3", "libffi.so.6", "libbz2.so.1", "liblzma.so.5"]
# lib-dynload: module -> NEEDED.
MODULES = {
    "_ssl": ["libssl.so.3", "libcrypto.so.3", *LIBC],
    "_hashlib": ["libcrypto.so.3", *LIBC],
    "_ctypes": ["libffi.so.6", *LIBC],
    "_bz2": ["libbz2.so.1", *LIBC],
    "_lzma": ["liblzma.so.5", *LIBC],
    "_uuid": ["libuuid.so.1", *LIBC],
    "zlib": ["libz.so.1", *LIBC],
    "_json": LIBC,
    "_socket": LIBC,
    "readline": ["libreadline.so.6", *LIBC],
    "_gdbm": ["libgdbm.so.4", *LIBC],
    "_dbm": ["libgdbm_compat.so.4", *LIBC],
    "_tkinter": ["libtk8.6.so", "libtcl8.6.so", *LIBC],
    "_curses": ["libncursesw.so.5", "libtinfo.so.5", *LIBC],
    "_curses_panel": ["libpanelw.so.5", "libncursesw.so.5", "libtinfo.so.5", *LIBC],
    "_sqlite3": ["libsqlite3.so", *LIBC],
    "_crypt": ["libcrypt.so.2", *LIBC],
    "nis": ["libnsl.so.1", *LIBC],
}
REMOVED_MODULES = {"readline", "_gdbm", "_dbm", "_tkinter", "_curses", "_curses_panel", "_sqlite3", "_crypt", "nis"}
PURE = [
    "ssl.py", "hmac.py", "secrets.py", "uuid.py", "shelve.py", "turtle.py", "crypt.py", "json/__init__.py",
    "ctypes/__init__.py", "http/server.py", "tkinter/__init__.py", "tkinter/ttk.py", "idlelib/idle.py",
    "turtledemo/__main__.py", "sqlite3/__init__.py", "sqlite3/dbapi2.py", "curses/__init__.py", "dbm/__init__.py",
    "dbm/dumb.py", "dbm/gnu.py", "dbm/ndbm.py", "__pycache__/ssl.cpython-312.pyc", "__pycache__/crypt.cpython-312.pyc",
    "__pycache__/turtle.cpython-312.pyc", "sqlite3/__pycache__/__init__.cpython-312.pyc", "ensurepip/__init__.py",
    "ensurepip/__main__.py", "ensurepip/_bundled/pip-25.0.1-py3-none-any.whl", "venv/__init__.py",
    "site-packages/sitecustomize.py",
]


def fake_appdir(root: Path, porthole: bool = True) -> Path:
    """A python-appimage AppDir as `build app` leaves it before packing: with Porthole's files and the notices
    (which build.sh hands it with -x), or, with porthole=False, only python-appimage's own part."""
    files = {
        "AppRun": b"#! /bin/bash\nexec \"${APPDIR}/usr/bin/python3.12\" -s -E \"${APPDIR}/start.py\" \"$@\"\n",
        "usr/bin/python3.12": b"#! /bin/bash\nexport TCL_LIBRARY=\"${APPDIR}/usr/share/tcltk/tcl8.6\"\n",
        "usr/share/applications/porthole.desktop": b"[Desktop Entry]\n",
        "usr/share/tcltk/tcl8.6/init.tcl": b"",
        "usr/share/tcltk/tk8.6/tk.tcl": b"",
        "opt/python3.12/bin/python3.12": make_elf(["libpthread.so.0", "libdl.so.2", "libutil.so.1", "libm.so.6", *LIBC]),
        "opt/python3.12/bin/pip3.12": b"#!/bin/sh\n",
        f"{PY}/LICENSE.txt": b"A. HISTORY OF THE SOFTWARE\n",
        f"{PY}/site-packages/certifi/cacert.pem": b"",
    }
    for name, needed in LIBRARIES.items():
        files[f"usr/lib/{name}"] = make_elf(needed, soname=None if name == "libsqlite3.so" else name)
    for module, needed in MODULES.items():
        files[f"{PY}/lib-dynload/{module}{EXT}"] = make_elf(needed)
    for name in PURE:
        files[f"{PY}/{name}"] = b""
    if porthole:
        files.update({
            "LICENSE": (ROOT / "LICENSE").read_bytes(),
            "THIRD-PARTY-NOTICES.txt": NOTICES.read_bytes(),
            "start.py": b"print('porthole')\n",
            "porthole/app.py": b"",
            "web/share.html": b"",
        })
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if data.startswith((b"#!", b"\x7fELF")):
            path.chmod(0o755)
    links = {
        "usr/bin/python3": "python3.12",
        "usr/bin/python": "python3",
        "porthole.desktop": "usr/share/applications/porthole.desktop",
        "opt/_internal/certs.pem": "../python3.12/lib/python3.12/site-packages/certifi/cacert.pem",
    }
    for name, target in links.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, root / name)
    return root


# What the trim takes out, relative to the AppDir.
REMOVED = {
    *(f"usr/lib/{name}" for name in LIBRARIES if name not in KEPT_LIBRARIES),
    *(f"{PY}/lib-dynload/{module}{EXT}" for module in REMOVED_MODULES),
    "usr/share/tcltk",
    *(f"{PY}/{name}" for name in ("tkinter", "idlelib", "turtledemo", "sqlite3", "curses", "dbm", "turtle.py",
                                   "crypt.py", "__pycache__/turtle.cpython-312.pyc", "__pycache__/crypt.cpython-312.pyc",
                                   "ensurepip")),
}


def tree(root: Path) -> set[str]:
    """Every file, link and folder under root, relative."""
    found = set()
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            found.add((Path(folder) / name).relative_to(root).as_posix())
    return found


def under(path: str, removed: set[str]) -> bool:
    return any(path == r or path.startswith(r + "/") for r in removed)


class ElfReaderTest(unittest.TestCase):
    def write(self, data: bytes) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "lib.so"
        path.write_bytes(data)
        return path

    def test_64_bit_little_endian(self):
        path = self.write(make_elf(["libtinfo.so.5", "libc.so.6"], soname="libreadline.so.6"))
        self.assertEqual(trim.elf_dynamic(path), ("libreadline.so.6", ["libtinfo.so.5", "libc.so.6"]))

    def test_32_bit_big_endian(self):
        path = self.write(make_elf(["libc.so.6"], soname="libx.so.1", bits=32, little=False, vaddr=0x8000))
        self.assertEqual(trim.elf_dynamic(path), ("libx.so.1", ["libc.so.6"]))

    def test_not_elf(self):
        self.assertIsNone(trim.elf_dynamic(self.write(b"#!/bin/sh\n")))
        self.assertIsNone(trim.elf_dynamic(self.write(b"")))

    def test_a_broken_elf_file_is_an_error(self):
        with self.assertRaises(ValueError):
            trim.elf_dynamic(self.write(make_elf(["libc.so.6"])[:80]))

    @unittest.skipUnless(shutil.which("readelf") and sys.platform.startswith("linux"), "needs readelf, on Linux")
    def test_agrees_with_readelf_on_real_files(self):
        import _ctypes
        import _json

        real = [Path(sys.executable).resolve()]
        real += [Path(m.__file__) for m in (_ctypes, _json) if getattr(m, "__file__", None)]
        for path in real:
            out = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True, timeout=30).stdout
            needed = re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", out)
            soname = re.findall(r"\(SONAME\)\s+Library soname: \[(.+?)\]", out)
            with self.subTest(path=str(path)):
                self.assertEqual(trim.elf_dynamic(path), (soname[0] if soname else None, needed))


class TrimTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.appdir = fake_appdir(Path(tmp.name) / "AppDir")

    def run_trim(self, *args, appdir=None):
        return subprocess.run([sys.executable, str(TRIM), *args, str(appdir or self.appdir)], capture_output=True,
                              text=True, timeout=60)

    def test_takes_out_the_list_and_keeps_the_rest(self):
        before = tree(self.appdir)
        result = self.run_trim()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = tree(self.appdir)
        self.assertEqual(after, {path for path in before if not under(path, REMOVED)})
        for name in KEPT_LIBRARIES:
            self.assertIn(f"usr/lib/{name}", after)
        for kept in ("_ssl", "_hashlib", "_ctypes", "_bz2", "_lzma", "_uuid", "zlib", "_json", "_socket"):
            self.assertIn(f"{PY}/lib-dynload/{kept}{EXT}", after)
        for kept in ("ssl.py", "hmac.py", "secrets.py", "uuid.py", "json/__init__.py", "http/server.py",
                     "LICENSE.txt", "__pycache__/ssl.cpython-312.pyc", "venv/__init__.py",
                     "site-packages/sitecustomize.py"):
            self.assertIn(f"{PY}/{kept}", after)
        self.assertIn("usr/share", after)  # only tcltk goes
        self.assertIn("Removed", result.stdout)
        self.assertIn("usr/lib/libreadline.so.6", result.stdout)

    def test_a_second_run_finds_nothing_to_do(self):
        self.assertEqual(self.run_trim().returncode, 0)
        before = tree(self.appdir)
        result = self.run_trim()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree(self.appdir), before)

    def test_check_passes_on_a_trimmed_appdir_and_fails_before(self):
        result = self.run_trim("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usr/lib/libgdbm.so.4", result.stdout + result.stderr)
        self.assertEqual(self.run_trim().returncode, 0)
        before = tree(self.appdir)
        result = self.run_trim("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tree(self.appdir), before)  # --check never changes anything

    def test_dry_run_changes_nothing(self):
        before = tree(self.appdir)
        result = self.run_trim("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usr/lib/libreadline.so.6", result.stdout)
        self.assertEqual(tree(self.appdir), before)

    def refused(self, *words):
        before = tree(self.appdir)
        result = self.run_trim()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for word in words:
            self.assertIn(word, result.stderr)
        self.assertEqual(tree(self.appdir), before)  # nothing removed

    def test_refuses_when_a_kept_file_needs_a_removed_library(self):
        (self.appdir / PY / "lib-dynload" / f"_ssl{EXT}").write_bytes(make_elf(["libssl.so.3", "libtinfo.so.5"]))
        self.refused(f"lib-dynload/_ssl{EXT}", "libtinfo.so.5")

    def test_refuses_when_python_itself_needs_a_removed_library(self):
        (self.appdir / "opt/python3.12/bin/python3.12").write_bytes(make_elf(["libreadline.so.6", *LIBC]))
        self.refused("opt/python3.12/bin/python3.12", "libreadline.so.6")

    def test_refuses_a_kept_library_the_notices_dont_name(self):
        # A newer base image bringing another library: someone has to decide and add its licence.
        (self.appdir / "usr/lib/libexpat.so.1").write_bytes(make_elf(LIBC, soname="libexpat.so.1"))
        self.refused("usr/lib/libexpat.so.1", "THIRD-PARTY-NOTICES.txt")

    def test_refuses_without_the_notices(self):
        (self.appdir / "THIRD-PARTY-NOTICES.txt").unlink()
        self.refused("THIRD-PARTY-NOTICES.txt")

    def test_refuses_without_pythons_licence(self):
        (self.appdir / PY / "LICENSE.txt").unlink()
        self.refused("LICENSE.txt")

    def test_refuses_a_kept_link_to_a_removed_file(self):
        os.symlink("../usr/lib/libsqlite3.so", self.appdir / "opt" / "libsqlite3.so")
        self.refused("opt/libsqlite3.so")

    def test_refuses_a_broken_elf_file(self):
        (self.appdir / "usr/lib/libffi.so.6").write_bytes(make_elf(LIBC)[:80])
        self.refused("usr/lib/libffi.so.6")

    def test_refuses_a_folder_that_isnt_a_python_appimage_appdir(self):
        empty = self.appdir.parent / "empty"
        empty.mkdir()
        result = self.run_trim(appdir=empty)
        self.assertEqual(result.returncode, 1)
        self.assertIn("opt/python3", result.stderr)

    def test_other_names_of_the_same_libraries_go_too(self):
        # Another base image may carry other versions (libreadline.so.8, libncursesw.so.6, libtcl9.0.so).
        for name in ("libreadline.so.8", "libhistory.so.8", "libncursesw.so.6", "libtinfo.so.6", "libpanelw.so.6",
                     "libformw.so.6", "libtcl9.0.so", "libtk9.0.so", "libgdbm.so.6", "libgdbm_compat.so.4.0.0",
                     "libsqlite3.so.0", "libnsl.so.2", "libcrypt.so.1"):
            (self.appdir / "usr/lib" / name).write_bytes(make_elf(LIBC))
        (self.appdir / "opt/python3.12/lib/tcl8.6").mkdir()
        result = self.run_trim()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(p.name for p in (self.appdir / "usr/lib").iterdir()), sorted(KEPT_LIBRARIES))
        self.assertFalse((self.appdir / "opt/python3.12/lib/tcl8.6").exists())

    def test_names_that_only_look_alike_stay(self):
        self.assertTrue(trim.removed_library("libcrypt.so.2"))
        self.assertFalse(trim.removed_library("libcrypto.so.3"))  # OpenSSL, not libcrypt
        self.assertFalse(trim.removed_library("libtinfo-config"))
        self.assertTrue(trim.removed_library("libtk8.6.so"))
        self.assertFalse(trim.removed_library("libtkfoo.so"))


def notice_sections(text: str) -> dict[str, str]:
    """{heading: its words up to the next heading} for the headings underlined with - or ~ ("2.1 expat")."""
    lines = text.splitlines()
    found, current = {}, None
    for i, line in enumerate(lines):
        below = lines[i + 1] if i + 1 < len(lines) else ""
        if line.strip() and len(below) == len(line) and re.fullmatch(r"-+|~+", below):
            current = line
            found[current] = []
        elif current is not None and not (re.fullmatch(r"-+|~+", line) and found[current] == []):
            found[current].append(line)
    return {heading: " ".join(" ".join(body).split()) for heading, body in found.items()}


class NoticesTest(unittest.TestCase):
    """packaging/appimage/THIRD-PARTY-NOTICES.txt, which build.sh puts next to LICENSE in the AppImage."""

    def setUp(self):
        self.text = NOTICES.read_text(encoding="utf-8")
        self.words = " ".join(self.text.split())  # the prose, whatever the line breaks
        self.sections = notice_sections(self.text)

    def section(self, start: str) -> str:
        found = [words for heading, words in self.sections.items() if heading.startswith(start)]
        self.assertEqual(len(found), 1, f"one section whose heading starts with {start!r}: {list(self.sections)}")
        return found[0]

    def lgpl_span(self) -> tuple[int, int]:
        start = self.text.index("                  GNU LESSER GENERAL PUBLIC LICENSE\n")
        end = self.text.index("That's all there is to it!\n", start) + len("That's all there is to it!\n")
        return start, end

    def test_plain_text(self):
        data = NOTICES.read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertNotIn(b"\t", data)
        self.assertTrue(data.endswith(b"\n"))
        start, end = self.lgpl_span()
        outside = self.text[:start] + self.text[end:]  # the LGPL is as the FSF wrote it, one line of 81 included
        for number, line in enumerate(outside.splitlines(), 1):
            self.assertLessEqual(len(line), 80, number)

    def test_names_each_bundled_library_and_its_licence(self):
        for name in KEPT_LIBRARIES:
            self.assertIn(name, self.text)
        for words in ("OpenSSL", "Apache License", "libffi", "bzip2", "Julian R Seward", "XZ Utils", "public domain",
                      "expat", "libmpdec", "Stefan Krah", "Mersenne Twister", "SipHash", "dtoa", "cfuhash", "HACL*",
                      "Unicode", "uvloop"):
            self.assertIn(words, self.words)

    def test_points_to_pythons_own_licence(self):
        self.assertIn("opt/python3.12/lib/python3.12/LICENSE.txt", self.text)
        self.assertIn("Python Software Foundation License", self.words)

    def test_says_what_was_taken_out(self):
        for words in ("GNU Readline and GDBM (both GPL-3.0)", "Tcl/Tk", "ncurses", "SQLite", "ensurepip"):
            self.assertIn(words, self.words)

    def test_doesnt_send_anyone_to_the_ensurepip_wheel_for_licences(self):
        # The pip wheel in ensurepip carried only pip's own licence, none of the libraries pip includes; the trim
        # takes ensurepip out (Porthole never makes a venv), so the notices don't mention its wheel.
        self.assertNotIn("_bundled", self.text)
        self.assertNotIn("wheel", self.words)

    def test_has_the_whole_apache_2_licence(self):
        # The file as /usr/share/common-licenses/Apache-2.0 has it, from its leading empty line to its last line.
        start = self.text.index("\n                                 Apache License\n")
        end = self.text.index("   limitations under the License.\n", start) + len("   limitations under the License.\n")
        self.assertEqual(hashlib.sha256(self.text[start:end].encode("ascii")).hexdigest(), APACHE_SHA256)

    def test_has_the_whole_lgpl_2_1(self):
        # For the AppImage runtime's libfuse: the file as /usr/share/common-licenses/LGPL-2.1 has it.
        start, end = self.lgpl_span()
        self.assertEqual(hashlib.sha256(self.text[start:end].encode("ascii")).hexdigest(), LGPL_SHA256)
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", self.section("11."))

    def test_each_mit_notice_is_whole(self):
        # The MIT licence asks for its permission notice, with its condition, in every copy; all but SipHash's
        # (which has none) come with the disclaimer too.
        for start in ("2.1 expat", "2.7 HACL*", "2.9 asyncio", "4. libffi", "8. Files from python-appimage",
                      "9.1 type2-runtime", "9.6 musl", "9.7 mimalloc"):
            with self.subTest(section=start):
                words = self.section(start)
                self.assertIn(MIT, words)
                self.assertIn(MIT_DISCLAIMER, words)
                self.assertIn("Copyright", words)
        self.assertIn(MIT, self.section("2.4 SipHash"))

    def test_each_bsd_notice_is_whole(self):
        for start, third in (("2.2 libmpdec", None),
                             ("2.3 Mersenne Twister", "3. The names of its contributors may not be used to endorse "
                              "or promote products derived from this software without specific prior written "
                              "permission."),
                             ("2.6 cfuhash", "Neither the name of the author nor the names of its contributors may be "
                              "used to endorse or promote products derived from this software without specific prior "
                              "written permission."),
                             ("9.3 squashfuse", None),
                             ("9.4 zstd", "Neither the name Facebook, nor Meta, nor the names of its contributors may "
                              "be used to endorse or promote products derived from this software without specific "
                              "prior written permission.")):
            with self.subTest(section=start):
                words = self.section(start)
                self.assertIn(BSD_SOURCE, words)
                self.assertIn(BSD_BINARY, words)
                if third:
                    self.assertIn(third, words)
                self.assertRegex(words, BSD_DISCLAIMER)
                self.assertIn("Copyright", words)

    def test_the_bzip2_licence_is_whole(self):
        words = self.section("5. bzip2")
        for condition in (
            "1. " + BSD_SOURCE,
            "2. The origin of this software must not be misrepresented; you must not claim that you wrote the "
            "original software. If you use this software in a product, an acknowledgment in the product documentation "
            "would be appreciated but is not required.",
            "3. Altered source versions must be plainly marked as such, and must not be misrepresented as being the "
            "original software.",
            "4. The name of the author may not be used to endorse or promote products derived from this software "
            "without specific prior written permission.",
        ):
            self.assertIn(condition, words)
        self.assertRegex(words, BSD_DISCLAIMER)

    def test_the_zlib_notice_is_whole(self):
        words = self.section("9.5 zlib")
        self.assertIn(ZLIB, words)
        self.assertIn("Jean-loup Gailly and Mark Adler", words)

    def test_names_the_appimage_runtime_and_its_parts(self):
        # The runtime appimagetool puts at the start of the AppImage file has libfuse (LGPL-2.1), squashfuse, zstd,
        # zlib, musl and mimalloc linked in. CI checks that the built AppImage's --appimage-version is the one
        # named here.
        runtime = self.section("9. The AppImage runtime")
        named = set(re.findall(re.escape(RUNTIME) + r"[0-9a-f]{7,40}\b", self.text))
        self.assertEqual(len(named), 1, named)
        self.assertIn(named.pop(), runtime)
        self.assertIn("--appimage-version", runtime)
        for part in ("type2-runtime", "libfuse 3.15.0", "squashfuse 0.5.2", "zstd 1.5.6", "zlib 1.3.2", "musl",
                     "mimalloc", "LGPL-2.1", "Source:"):
            self.assertIn(part, runtime)
        self.assertIn("section 11", self.section("9.2 libfuse"))
        # The C library and zlib are in the AppImage file after all: in the runtime.
        self.assertNotIn("The C library, zlib and libuuid are not in the AppImage", self.words)

    def test_credits_the_files_python_appimage_put_in(self):
        words = self.section("8. Files from python-appimage")
        for name in ("AppRun", "usr/bin/python3.12", "opt/python3.12/lib/python3.12/site-packages/sitecustomize.py",
                     "Valentin Niess", "https://github.com/niess/python-appimage"):
            self.assertIn(name, words)

    def test_the_table_of_contents_matches_the_sections(self):
        contents = re.findall(r"^  (\d+)\. (.+)$", self.text[: self.text.index("\n1. Python\n")], re.M)
        headings = [h for h in self.sections if re.match(r"\d+\. ", h)]
        self.assertEqual([f"{number}. {title}" for number, title in contents], headings)

    def test_every_kept_library_of_the_real_appimage_is_named_the_way_trim_checks(self):
        # trim.py refuses a library in usr/lib that this file doesn't name; these are the five it keeps.
        for name in KEPT_LIBRARIES:
            self.assertFalse(trim.removed_library(name), name)
            self.assertTrue(trim.named_in(name, self.text), name)


if __name__ == "__main__":
    unittest.main()
