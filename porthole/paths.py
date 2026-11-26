"""Where Porthole's own files are, run from source or as a frozen app.

From source, web/ and assets/ sit next to start.py. In a frozen app (PyInstaller now, maybe others later)
the bundled files are unpacked in sys._MEIPASS, and the program itself is sys.executable. In the Mac's
Porthole.app the program is Contents/MacOS/Porthole, and sys._MEIPASS is Contents/Frameworks, where
PyInstaller links in web/ and assets/ from Contents/Resources.
"""

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent  # the folder with start.py, porthole/ and web/


def frozen() -> bool:
    """True when Porthole runs as a built program (Porthole.exe), not from source."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """The folder that holds web/ and assets/."""
    if frozen():
        bundle = getattr(sys, "_MEIPASS", None)
        return Path(bundle) if bundle else Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def web_dir() -> Path:
    return resource_root() / "web"


def app_bundle() -> Path | None:
    """The Porthole.app a frozen Porthole runs inside on a Mac, else None."""
    if not frozen() or sys.platform != "darwin":
        return None
    program = Path(sys.executable).resolve()
    contents = program.parent.parent
    if program.parent.name == "MacOS" and contents.name == "Contents" and contents.parent.suffix.lower() == ".app":
        return contents.parent
    return None


def app_folder() -> Path:
    """The folder Porthole runs from, as "already running" names it. Frozen, that is the program's folder:
    the bundle is a folder inside it (one-folder build) or a new temporary folder on every run (one-file).
    On a Mac it is the folder that holds Porthole.app (say /Applications), not the folder inside the app."""
    if frozen():
        bundle = app_bundle()
        if bundle is not None:
            return bundle.parent
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def program_name() -> str:
    """The built program's file name, as typed to start it (Porthole.exe; Porthole inside Porthole.app)."""
    return Path(sys.executable).name
