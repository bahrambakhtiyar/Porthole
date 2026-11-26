#!/usr/bin/env python3
"""Take out of a python-appimage AppDir what Porthole never uses, before it is packed.

    python3 packaging/appimage/trim.py [--check | --dry-run] APPDIR

The AppDir is python-appimage's: a portable Python in opt/python3.X, its libraries in usr/lib. Taken out:
GNU Readline and GDBM (GPL-3.0), Tcl/Tk with libXft and libXrender, ncurses (with panel and tinfo), SQLite,
libcrypt and libnsl; the Python modules that load them; the pure-Python packages built on those (tkinter,
idlelib, turtle, turtledemo, sqlite3, curses, dbm, crypt); Tcl/Tk's own folders; and ensurepip, whose copy of pip
carries none of the licences of the libraries pip includes (Porthole never makes a venv). Everything else stays,
OpenSSL, libffi, bzip2 and xz (liblzma) among it, which THIRD-PARTY-NOTICES.txt next to AppRun covers.

It removes nothing and exits with 1 when:
- a file that stays still needs one of those libraries (an ELF NEEDED entry), Python itself included;
- a library that stays in usr/lib isn't named in THIRD-PARTY-NOTICES.txt (a newer base image brought one:
  take it out here, or add it and its licence to the notices);
- THIRD-PARTY-NOTICES.txt or Python's LICENSE.txt (opt/python3.X/lib/python3.X/LICENSE.txt) is missing;
- a symbolic link that stays points at something taken out, or an ELF file can't be read.

--check changes nothing and also fails while anything it would take out is still there (CI runs it on the
built AppImage). --dry-run prints what would go. Standard library only.
"""

import argparse
import os
import re
import shutil
import struct
import sys
from pathlib import Path

NOTICES = "THIRD-PARTY-NOTICES.txt"

# Shared libraries Porthole never loads, by file name (any version), in usr/lib.
REMOVED_LIBRARIES = re.compile(
    r"lib(?:"
    r"readline|history"  # GNU Readline (GPL-3.0)
    r"|gdbm|gdbm_compat"  # GDBM (GPL-3.0)
    r"|tcl\d[\d.]*|tk\d[\d.]*|Xft|Xrender"  # Tcl/Tk and the X libraries only Tk uses
    r"|(?:ncurses|panel|form|menu|tinfo)w?"  # ncurses
    r"|sqlite3|crypt|nsl"
    r")\.so(?:\.[\d.]+)?"
)
# Python's C modules (lib-dynload) that load them, by module name.
REMOVED_MODULES = {"readline", "_gdbm", "_dbm", "_tkinter", "_curses", "_curses_panel", "_sqlite3", "_crypt", "nis"}
# Pure-Python packages and modules of the standard library that only work with those, and ensurepip.
REMOVED_PACKAGES = ("tkinter", "idlelib", "turtledemo", "sqlite3", "curses", "dbm", "ensurepip")
REMOVED_PY_MODULES = ("turtle", "crypt")
# Tcl/Tk's script libraries.
TCLTK_FOLDER = re.compile(r"(?:tcl|tk)\d[\d.]*|tcltk")

DT_NULL, DT_NEEDED, DT_STRTAB, DT_SONAME = 0, 1, 5, 14
PT_LOAD, PT_DYNAMIC = 1, 2


class Refused(Exception):
    pass


def removed_library(name: str) -> bool:
    return REMOVED_LIBRARIES.fullmatch(name) is not None


def named_in(name: str, text: str) -> bool:
    """Whether the notices name this file (whole, not as part of a longer name)."""
    return re.search(rf"(?<![\w.+-]){re.escape(name)}(?![\w+-]|\.\w)", text) is not None


def elf_dynamic(path: Path) -> tuple[str | None, list[str]] | None:
    """(SONAME, NEEDED) of an ELF file, read the way the loader does (program headers), or None when the file
    isn't ELF. A file with no dynamic section gives (None, []). ValueError when the ELF can't be read."""
    with open(path, "rb") as f:
        if f.read(4) != b"\x7fELF":
            return None
        data = b"\x7fELF" + f.read()
    try:
        return _dynamic(data)
    except (struct.error, IndexError, ValueError, UnicodeDecodeError) as err:
        raise ValueError(f"can't read its ELF dynamic section ({err})") from None


def _dynamic(data: bytes) -> tuple[str | None, list[str]]:
    elf_class, encoding = data[4], data[5]
    if elf_class not in (1, 2) or encoding not in (1, 2):
        raise ValueError("unknown ELF class or byte order")
    end = "<" if encoding == 1 else ">"
    if elf_class == 2:
        (phoff,) = struct.unpack_from(end + "Q", data, 0x20)
        phentsize, phnum = struct.unpack_from(end + "HH", data, 0x36)
        header, entry = end + "IIQQQQQQ", end + "qQ"
    else:
        (phoff,) = struct.unpack_from(end + "I", data, 0x1C)
        phentsize, phnum = struct.unpack_from(end + "HH", data, 0x2A)
        header, entry = end + "IIIIIIII", end + "iI"
    if phnum and phentsize < struct.calcsize(header):
        raise ValueError("program headers too small")
    segments = []  # (type, file offset, address, size in the file)
    for i in range(phnum):
        fields = struct.unpack_from(header, data, phoff + i * phentsize)
        if elf_class == 2:
            kind, _, offset, address, _, size = fields[:6]
        else:
            kind, offset, address, _, size = fields[:5]
        segments.append((kind, offset, address, size))
    dynamic = [(offset, size) for kind, offset, _, size in segments if kind == PT_DYNAMIC]
    if not dynamic:
        return None, []  # linked statically, or not a program or library
    offset, size = dynamic[0]
    step = struct.calcsize(entry)
    needed, soname, strtab = [], None, None
    for at in range(offset, offset + size - step + 1, step):
        tag, value = struct.unpack_from(entry, data, at)
        if tag == DT_NULL:
            break
        if tag == DT_NEEDED:
            needed.append(value)
        elif tag == DT_SONAME:
            soname = value
        elif tag == DT_STRTAB:
            strtab = value
    if strtab is None:
        if needed or soname is not None:
            raise ValueError("no string table")
        return None, []
    base = next((offset + strtab - address for kind, offset, address, size in segments
                 if kind == PT_LOAD and address <= strtab < address + size), None)
    if base is None:
        raise ValueError("string table outside the file")

    def string(at: int) -> str:
        start = base + at
        if not 0 <= start < len(data):
            raise ValueError("name outside the file")
        return data[start : data.index(b"\0", start)].decode("utf-8")

    return (string(soname) if soname is not None else None), [string(n) for n in needed]


def python_lib(appdir: Path) -> Path:
    """opt/python3.X/lib/python3.X, the bundled standard library."""
    found = sorted(p for p in appdir.glob("opt/python3.*/lib/python3.*") if p.is_dir() and not p.is_symlink())
    if len(found) != 1:
        raise Refused(f"{appdir} isn't a python-appimage AppDir: expected one opt/python3.X/lib/python3.X, "
                      f"found {len(found)}.")
    return found[0]


def plan(appdir: Path) -> list[Path]:
    """What to take out (files, links and folders), each once."""
    lib = python_lib(appdir)
    found: list[Path] = []
    usr_lib = appdir / "usr" / "lib"
    if usr_lib.is_dir():
        for path in sorted(usr_lib.iterdir()):
            if removed_library(path.name) or (path.is_dir() and not path.is_symlink() and TCLTK_FOLDER.fullmatch(path.name)):
                found.append(path)
    tcltk = appdir / "usr" / "share" / "tcltk"
    if tcltk.exists() or tcltk.is_symlink():
        found.append(tcltk)
    for path in sorted(lib.parent.iterdir()):  # opt/python3.X/lib: Tcl/Tk folders, if a build puts them there
        if path.is_dir() and not path.is_symlink() and TCLTK_FOLDER.fullmatch(path.name):
            found.append(path)
    dynload = lib / "lib-dynload"
    if dynload.is_dir():
        found += [path for path in sorted(dynload.iterdir()) if path.name.split(".")[0] in REMOVED_MODULES]
    for name in REMOVED_PACKAGES:
        if (lib / name).exists() or (lib / name).is_symlink():
            found.append(lib / name)
    for name in REMOVED_PY_MODULES:
        module = lib / f"{name}.py"
        if module.exists() or module.is_symlink():
            found.append(module)
        found += sorted((lib / "__pycache__").glob(f"{name}.*.pyc"))
    return found


def is_under(path: Path, folders: list[Path]) -> bool:
    return any(path == folder or folder in path.parents for folder in folders)


def kept_files(appdir: Path, removed: list[Path]):
    """Every file and link that stays, as (path, is a link)."""
    for folder, dirs, files in os.walk(appdir):
        here = Path(folder)
        dirs[:] = [name for name in dirs if not is_under(here / name, removed)]
        for name in files + [d for d in dirs if (here / d).is_symlink()]:
            path = here / name
            if not is_under(path, removed):
                yield path, path.is_symlink()


def problems(appdir: Path, removed: list[Path]) -> list[str]:
    """Why the trim can't go ahead, if anything stops it."""
    found = []

    def rel(path: Path) -> str:
        return path.relative_to(appdir).as_posix()

    lib = python_lib(appdir)
    if not (lib / "LICENSE.txt").is_file():
        found.append(f"Python's licence {rel(lib / 'LICENSE.txt')} is missing (THIRD-PARTY-NOTICES.txt points to it).")
    notices_path = appdir / NOTICES
    notices = notices_path.read_text(encoding="utf-8") if notices_path.is_file() else None
    if notices is None:
        found.append(f"{NOTICES} is missing next to AppRun.")
    usr_lib = appdir / "usr" / "lib"
    if notices is not None and usr_lib.is_dir():
        for path in sorted(usr_lib.iterdir()):
            if ".so" in path.name and not is_under(path, removed) and not named_in(path.name, notices):
                found.append(f"usr/lib/{path.name} stays but {NOTICES} doesn't name it: take it out in "
                             f"packaging/appimage/trim.py, or add it and its licence to the notices.")
    gone_names = set()
    for path in removed:
        if path.is_file() and not path.is_symlink():
            gone_names.add(path.name)
            try:
                soname = (elf_dynamic(path) or (None, []))[0]
            except ValueError:
                soname = None
            if soname:
                gone_names.add(soname)
    for path, link in kept_files(appdir, removed):
        if link:
            target = Path(os.path.normpath(path.parent / os.readlink(path)))
            if is_under(target, removed):
                found.append(f"{rel(path)} stays but points at {os.readlink(path)}, which is taken out.")
            continue
        try:
            dynamic = elf_dynamic(path)
        except ValueError as err:
            found.append(f"{rel(path)}: {err}.")
            continue
        except OSError as err:
            found.append(f"{rel(path)}: can't be read ({err.strerror}).")
            continue
        if dynamic:
            for name in dynamic[1]:
                if name in gone_names or removed_library(name):
                    found.append(f"{rel(path)} stays but needs {name}, which is taken out.")
    return found


def size_of(path: Path) -> int:
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    return sum((Path(folder) / name).lstat().st_size for folder, _, files in os.walk(path) for name in files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Take out of a python-appimage AppDir what Porthole never uses.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="change nothing; fail if anything to take out is still there")
    mode.add_argument("--dry-run", action="store_true", help="print what would be taken out, change nothing")
    parser.add_argument("appdir", type=Path)
    args = parser.parse_args(argv)
    appdir = args.appdir.resolve()
    try:
        removed = plan(appdir)
        found = problems(appdir, removed)
    except Refused as err:
        print(f"trim.py: {err}", file=sys.stderr)
        return 1
    if args.check and removed:
        found += [f"{path.relative_to(appdir).as_posix()} is still there." for path in removed]
    if found:
        print(f"trim.py: refused ({appdir}), nothing changed:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    if args.check:
        print(f"Trimmed as it should be: {appdir}")
        return 0
    total = 0
    for path in removed:
        total += size_of(path)
        print(f"  {'would remove' if args.dry_run else 'removed'}: {path.relative_to(appdir).as_posix()}")
        if args.dry_run:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    verb = "Would remove" if args.dry_run else "Removed"
    print(f"{verb} {len(removed)} files and folders ({total / 1e6:.1f} MB) that Porthole never uses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
