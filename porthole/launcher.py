"""Add or remove a "Porthole" launcher: Linux app menu, or Windows Start menu and desktop. A Mac has none: the
built app goes in the Applications folder, and from source Porthole is started with python3 start.py."""

import base64
import ntpath
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from porthole import paths, sandbox

APP_NAME = "Porthole"
DESKTOP_FILE = "porthole.desktop"
LEGACY_DESKTOP_FILES = ("lan-screen-share.desktop",)  # names used before the rename
ROOT = paths.resource_root()  # in a built Porthole.exe, the bundle inside its folder
SCRIPT = ROOT / "start.py"
ICON_PNG = ROOT / "assets" / "icon.png"
WEB_ICON = ROOT / "web" / "icon-512.png"  # in every build; the AppImage and the .deb leave out assets/
ICON_ICO = ROOT / "assets" / "icon.ico"
MAC_INSTALL = "On a Mac there's no launcher to add: put Porthole in the Applications folder."
MAC_UNINSTALL = "On a Mac there's no launcher to remove: to remove Porthole, drag it from the Applications folder to the Bin."
# Run from source (the only way while the Mac app is parked) there is no app to put anywhere.
MAC_SOURCE_INSTALL = "On a Mac there's no launcher to add: start Porthole with python3 start.py."
NOT_IN_MENU = f"{APP_NAME} wasn't in the app menu."
OLDER_REMOVED = f"Removed an older {APP_NAME} menu entry; the package's own entry stays."
COMMENT = "Share this screen with phones and PCs on the same network"  # with the name, tells the entries Porthole wrote
MAC_SOURCE_UNINSTALL = "On a Mac there's no launcher to remove: Porthole run from source never adds one."


class NotOnThisSystem(RuntimeError):
    """This system has no launchers to add or remove. The message says what to do instead."""


# ---------- Linux ----------


def _exec_arg(value: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec."""
    quoted = "".join("\\" + ch if ch in '"`$\\' else ch for ch in value)  # quoting rule
    quoted = quoted.replace("\\", "\\\\")  # string rule, applied on top
    return '"' + quoted.replace("%", "%%") + '"'


def desktop_entry(python: str, script: Path, icon: Path) -> str:
    return _entry([python, str(script)], icon)


def _entry(command: list[str], icon: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Comment={COMMENT}\n"
        f"Exec={' '.join(_exec_arg(part) for part in command)} --launcher\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Network;RemoteAccess;\n"
        "StartupNotify=false\n"
    )


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))


def _apps_dir() -> Path:
    return _data_home() / "applications"


def _kept_icon() -> Path:
    """Where an AppImage keeps its icon for the launcher: its own files exist only while it runs."""
    return _data_home() / "icons" / "hicolor" / "512x512" / "apps" / "porthole.png"


def _icon() -> Path:
    return ICON_PNG if ICON_PNG.exists() else WEB_ICON


def _remove_legacy(apps_dir: Path) -> None:
    for name in LEGACY_DESKTOP_FILES:
        (apps_dir / name).unlink(missing_ok=True)


def install_linux(python: str, script: Path, icon: Path, apps_dir: Path) -> Path:
    return _install_entry([python, str(script)], icon, apps_dir)


def _install_entry(command: list[str], icon: Path, apps_dir: Path) -> Path:
    apps_dir.mkdir(parents=True, exist_ok=True)
    _remove_legacy(apps_dir)
    path = apps_dir / DESKTOP_FILE
    path.write_text(_entry(command, icon), encoding="utf-8")
    path.chmod(0o755)
    return path


def _entry_fields(path: Path) -> dict[str, str] | None:
    """The keys of a desktop entry's [Desktop Entry] group; None for anything else (no file, a link, not an entry)."""
    if path.is_symlink() or not path.is_file():
        return None  # a link points at an entry someone else keeps (the package's, say): never Porthole's to remove
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    fields: dict[str, str] = {}
    group = None
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
        elif group == "Desktop Entry" and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            fields.setdefault(key.strip(), value.strip())
    return fields or None


def _ours(path: Path) -> bool:
    """Whether `path` is an entry --install-launcher wrote: its name and comment. The package's entry, another app's
    porthole.desktop, or a link to one are not; an entry Porthole wrote and someone edited (another Exec) still is."""
    fields = _entry_fields(path)
    return bool(fields) and fields.get("Name") == APP_NAME and fields.get("Comment") == COMMENT


def uninstall_linux(apps_dir: Path) -> bool:
    _remove_legacy(apps_dir)
    path = apps_dir / DESKTOP_FILE
    if not _ours(path):
        return False
    path.unlink()
    return True


def _refresh_menu(apps_dir: Path) -> None:
    try:
        subprocess.run(["update-desktop-database", str(apps_dir)], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # the menu picks the file up on its own, just a little later


# ---------- Windows ----------


def _ps(value) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def shortcut_script(target: str, arguments: str, workdir: str, icon, remove: bool = False) -> str:
    """PowerShell that adds (or removes) a "Porthole" shortcut in the Start menu and on the desktop."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$name = {_ps(APP_NAME + '.lnk')}",
        "$places = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))",
    ]
    if remove:
        lines.append("foreach ($p in $places) { Remove-Item -LiteralPath (Join-Path $p $name) -ErrorAction SilentlyContinue }")
        return "\n".join(lines)
    lines += [
        "$shell = New-Object -ComObject WScript.Shell",
        "foreach ($p in $places) {",
        "  $link = $shell.CreateShortcut((Join-Path $p $name))",
        f"  $link.TargetPath = {_ps(target)}",
        f"  $link.Arguments = {_ps(arguments)}",
        f"  $link.WorkingDirectory = {_ps(workdir)}",
        f"  $link.IconLocation = {_ps(icon)}",
        "  $link.Description = 'Share this screen with phones and PCs on the same network'",
        "  $link.Save()",
        "}",
    ]
    return "\n".join(lines)


def windows_script(pythonw: str, script: Path, icon: Path, remove: bool = False) -> str:
    """The shortcut when running from source: pythonw.exe (no console window) with start.py."""
    return shortcut_script(pythonw, f'"{script}" --launcher', ntpath.dirname(str(script)), icon, remove)


def _windows_shortcut(remove: bool = False) -> str:
    if paths.frozen():
        # The built Porthole.exe starts itself; its icon is inside it. (The installer makes these too.)
        program = sys.executable
        return shortcut_script(program, "--launcher", ntpath.dirname(program), program, remove)
    return windows_script(_pythonw(), SCRIPT, ICON_ICO, remove)


def _pythonw() -> str:
    """pythonw.exe runs without a console window."""
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else python)


def _run_powershell(script: str) -> None:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")  # no command-line quoting traps
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flashing up from Porthole.exe
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell failed")


# ---------- entry points ----------


def install() -> list[str]:
    if sys.platform.startswith("win"):
        _run_powershell(_windows_shortcut())
        return [f"Added {APP_NAME} to the Start menu and the desktop."]
    if sys.platform == "darwin":
        raise NotOnThisSystem(MAC_INSTALL if paths.frozen() else MAC_SOURCE_INSTALL)
    apps = _apps_dir()
    appimage = sandbox.appimage(ROOT)  # the .AppImage file, when Porthole runs from one
    if appimage:
        icon = _kept_icon()
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(WEB_ICON, icon)
        path = _install_entry([appimage], icon, apps)
        remove_hint = f"Remove it with: {shlex.quote(appimage)} --uninstall-launcher"
    else:
        path = install_linux(sys.executable, SCRIPT, _icon(), apps)
        remove_hint = "Remove it with: python3 start.py --uninstall-launcher"
    _refresh_menu(apps)
    return [f"Added {APP_NAME} to the app menu ({path}).", remove_hint]


def remove_older_entry() -> bool:
    """For a package that brings its own menu entry (the .deb): removes the user entry an earlier --install-launcher
    left, from the AppImage or a folder. It has the package entry's file name, so it came first in the menu and hid
    that one. Removes the AppImage's copy of the icon too when that entry used it. Never the package's own entry
    (only the user's folder is looked at) nor a porthole.desktop that Porthole didn't write. True if it removed one."""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False
    apps = _apps_dir()
    path = apps / DESKTOP_FILE
    if not _ours(path):
        return False
    icon = (_entry_fields(path) or {}).get("Icon", "")
    path.unlink()
    if icon and Path(icon) == _kept_icon():
        _kept_icon().unlink(missing_ok=True)
    _refresh_menu(apps)
    return True


def uninstall() -> list[str]:
    if sys.platform.startswith("win"):
        _run_powershell(_windows_shortcut(remove=True))
        return [f"Removed {APP_NAME} from the Start menu and the desktop."]
    if sys.platform == "darwin":
        raise NotOnThisSystem(MAC_UNINSTALL if paths.frozen() else MAC_SOURCE_UNINSTALL)
    apps = _apps_dir()
    _kept_icon().unlink(missing_ok=True)
    if uninstall_linux(apps):
        _refresh_menu(apps)
        return [f"Removed {APP_NAME} from the app menu."]
    return [NOT_IN_MENU]
