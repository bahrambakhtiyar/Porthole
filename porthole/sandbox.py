"""Whether Porthole runs inside a Snap or Flatpak sandbox or as an AppImage, and the user's real home folder."""

import os
from pathlib import Path

from porthole import paths

FLATPAK_INFO = Path("/.flatpak-info")  # every Flatpak sandbox has this file at its root
APP_ROOT = paths.resource_root()  # the folder with start.py, porthole/ and web/


def _inside(folder: Path, root: str) -> bool:
    try:
        return folder.resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError, ValueError):  # RuntimeError: a symlink loop on Python 3.12 and older
        return False


def kind() -> str:
    """'snap', 'flatpak' or 'none'.

    $SNAP counts only when Porthole's own files are inside it: a terminal inside another snap app (an editor,
    say) passes its $SNAP and $SNAP_NAME on to everything it starts, including Porthole run from source."""
    snap = os.environ.get("SNAP")
    if snap and os.environ.get("SNAP_NAME") and _inside(APP_ROOT, snap):
        return "snap"
    if os.environ.get("FLATPAK_ID") or FLATPAK_INFO.exists():
        return "flatpak"
    return "none"


def sandboxed() -> bool:
    return kind() != "none"


def real_home() -> Path:
    """The user's own home folder. Inside a snap, $HOME is the snap's private folder instead."""
    real = os.environ.get("SNAP_REAL_HOME")
    return Path(real) if real else Path.home()


def appimage(folder: Path | None = None) -> str | None:
    """The .AppImage file Porthole runs from, or None when it isn't run as an AppImage.

    The AppImage runtime sets $APPIMAGE (that file) and $APPDIR (where it is mounted) for its app, and every
    program started from that app inherits them, say a terminal inside an editor that is an AppImage. So they
    count only when Porthole's own files (`folder`, by default the folder with start.py) are inside $APPDIR."""
    file, mount = os.environ.get("APPIMAGE"), os.environ.get("APPDIR")
    if not file or not mount:
        return None
    try:
        inside = Path(folder if folder is not None else APP_ROOT).resolve().is_relative_to(Path(mount).resolve())
    except (OSError, RuntimeError, ValueError):  # RuntimeError: a symlink loop on Python 3.12 and older
        return None
    return file if inside else None
