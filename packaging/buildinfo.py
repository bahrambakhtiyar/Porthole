#!/usr/bin/env python3
"""The version of a build, for CI (the Linux workflow gives the snap a tag's version with it).

    python packaging/buildinfo.py                    # prints the version, for example 0.1.0
    python packaging/buildinfo.py --check-metainfo   # stops unless the AppStream metainfo's newest release is it

The version is PORTHOLE_VERSION (CI sets it from a vX.Y.Z tag), else the vX.Y.Z git tag on this commit,
else 0.1.0. It must be three numbers of at most 65535, as the Windows and Mac builds will need too (they are
parked on the archive/windows-macos branch of the private repository, whose copy of this file adds their build
details). Python standard library only.
"""

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_VERSION = "0.1.0"
_VERSION = re.compile(r"\d{1,5}(?:\.\d{1,5}){2}")  # three numbers everywhere
_REPO = Path(__file__).resolve().parent.parent
METAINFO = _REPO / "packaging" / "linux" / "io.github.bahrambakhtiyar.Porthole.metainfo.xml"


def git_tag() -> str | None:
    """The vX.Y.Z tag on the current commit, or None. Never raises."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "--match", "v[0-9]*"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tag = result.stdout.strip()
    return tag if result.returncode == 0 and tag else None


def porthole_version(environ=None, tag=git_tag) -> str:
    """The version to build. Raises ValueError for one that isn't three numbers of at most 65535."""
    env = os.environ if environ is None else environ
    value = (env.get("PORTHOLE_VERSION") or "").strip() or (tag() or "").strip() or DEFAULT_VERSION
    if value[:1] in ("v", "V"):
        value = value[1:]
    if not _VERSION.fullmatch(value) or any(int(part) > 65535 for part in value.split(".")):
        raise ValueError(f"Not a usable version: {value!r}. Use three numbers, like 1.2.3.")
    return value


def newest_release(path: Path = METAINFO) -> str | None:
    """The version of the first (newest) <release> in AppStream metainfo, or None. Software centres show it."""
    release = ET.parse(path).getroot().find("releases/release")
    return release.get("version") if release is not None else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        version = porthole_version()
    except ValueError as err:
        print(err, file=sys.stderr)
        return 1
    if "--check-metainfo" in argv:
        newest = newest_release()
        if newest != version:
            print(
                f"{METAINFO.relative_to(_REPO).as_posix()} says the newest release is {newest}, not {version}. "
                f'Add <release version="{version}" date="YYYY-MM-DD"> above the others.',
                file=sys.stderr,
            )
            return 1
        return 0
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
