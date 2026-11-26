"""Saves recordings that the sharer page uploads in small pieces."""

import contextlib
import errno
import os
import secrets
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from porthole import sandbox

ALLOWED_EXTENSIONS = ("mp4", "webm")
FOLDERID_VIDEOS = "18989B1D-99B5-455B-841C-AB7C74E4DDFC"  # Windows' Videos folder, wherever the user moved it
# Why a write failed, in plain words for the page ("The recording stopped: the disk is full."). Windows reports a full
# disk as ENOSPC too; it has no EDQUOT.
_WRITE_REASONS = {
    "ENOSPC": "the disk is full",
    "EDQUOT": "the disk is full",  # out of the space this user may use
    "EFBIG": "the file is too big for this disk",  # 4 GB on a FAT32 stick
    "EROFS": "the disk is read-only",
    "EACCES": "Porthole isn't allowed to write there",
    "EPERM": "Porthole isn't allowed to write there",
}
_WRITE_REASON_CODES = {getattr(errno, name): reason for name, reason in _WRITE_REASONS.items() if hasattr(errno, name)}
OTHER_WRITE_REASON = "the disk can't be written to"  # EIO (a stick pulled out) and anything else


def write_reason(err: OSError) -> str:
    return _WRITE_REASON_CODES.get(err.errno, OTHER_WRITE_REASON)


class RecorderError(Exception):
    def __init__(self, status: int, reason: str, expected: int | None = None, stopped: str | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.expected = expected
        self.stopped = stopped  # a write error stopped the recording: why, in plain words


class _Recording:
    def __init__(self, path: Path, handle, owner: str = ""):
        self.path = path
        self.handle = handle
        self.owner = owner  # the sharer page that records (its page token), the only one that can finish it
        self.next_seq = 0
        self.bytes = 0  # in the file, all whole pieces: written unbuffered, so a write that fails is known at once
        self.stopped = ""  # set when a write failed: why; the file is closed then, keeping what it holds


class Recorder:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._lock = threading.Lock()
        self._open: dict[str, _Recording] = {}

    def start(self, ext: str, now: datetime | None = None, owner: str = "") -> dict:
        if ext not in ALLOWED_EXTENSIONS:
            raise RecorderError(400, "unsupported file type")
        stamp = (now or datetime.now()).strftime("%Y-%m-%d %H-%M-%S")
        with self._lock:
            self._close_all_locked()
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                path, handle = self._create(stamp, ext)
            except OSError as err:
                raise RecorderError(507, f"can't create the file: {err.strerror or err}") from err
            rec_id = secrets.token_hex(8)
            self._open[rec_id] = _Recording(path, handle, owner)
            return {"id": rec_id, "name": path.name, "folder": str(self.folder)}

    def _create(self, stamp: str, ext: str):
        for n in range(1, 1000):
            suffix = "" if n == 1 else f" ({n})"
            path = self.folder / f"Screen share {stamp}{suffix}.{ext}"
            try:
                # Unbuffered: a write that fails (a full disk) fails at once, never later at close, and what is in the
                # file is exactly the pieces taken so far.
                return path, open(path, "xb", buffering=0)
            except FileExistsError:
                continue
        raise OSError("too many recordings with the same name")

    def append(self, rec_id: str, seq: int, data: bytes) -> None:
        """Adds the next piece. A write error (a full disk) stops the recording: the file keeps the pieces before this
        one and is closed, and this and every later piece get a 507 that says why (`stopped`)."""
        with self._lock:
            rec = self._open.get(rec_id)
            if rec is None:
                raise RecorderError(404, "no such recording")
            if rec.stopped:
                raise RecorderError(507, f"the recording stopped: {rec.stopped}", stopped=rec.stopped)
            if seq != rec.next_seq:
                raise RecorderError(409, "piece out of order", expected=rec.next_seq)
            try:
                rest = memoryview(data)
                while rest:
                    written = rec.handle.write(rest)  # a file may take part of it: the rest follows
                    if not written:
                        raise OSError(errno.EIO, "the file took nothing")
                    rest = rest[written:]
            except OSError as err:
                rec.stopped = write_reason(err)
                # Back to the last whole piece (the half-written one is of no use, and gives some space back), closed.
                with contextlib.suppress(OSError, ValueError):
                    rec.handle.truncate(rec.bytes)
                with contextlib.suppress(OSError, ValueError):
                    rec.handle.close()
                raise RecorderError(507, f"the recording stopped: {rec.stopped}", stopped=rec.stopped) from err
            rec.next_seq += 1
            rec.bytes += len(data)

    def stop(self, rec_id: str, recorded: bool = True) -> dict:
        """Finishes a recording. A file with nothing in it is deleted (`empty`): no pieces, or `recorded` False when
        the page says no picture was recorded (started while paused and stopped before Resume, which leaves at most a
        file header). `stopped` says why a write error ended it early; what it holds is kept."""
        with self._lock:
            rec = self._open.pop(rec_id, None)
            if rec is None:
                raise RecorderError(404, "no such recording")
            try:
                rec.handle.close()
            except OSError as err:
                raise RecorderError(507, f"can't finish the file: {err.strerror or err}") from err
            if not rec.bytes or not recorded:
                self._delete(rec)
                result = {"empty": True, "name": rec.path.name, "bytes": 0}
            else:
                result = {"path": str(rec.path), "name": rec.path.name, "bytes": rec.bytes}
            if rec.stopped:
                result["stopped"] = rec.stopped
            return result

    @staticmethod
    def _delete(rec: _Recording) -> None:
        with contextlib.suppress(OSError):
            rec.path.unlink(missing_ok=True)

    def _close_locked(self, rec: _Recording) -> None:
        """Closes a recording nobody will finish; what it holds is kept, and a file with nothing in it is deleted."""
        with contextlib.suppress(OSError):
            rec.handle.close()
        if not rec.bytes:
            self._delete(rec)

    def _close_all_locked(self) -> None:
        for rec in self._open.values():
            self._close_locked(rec)
        self._open.clear()

    def busy(self) -> bool:
        """Whether a recording is open."""
        with self._lock:
            return bool(self._open)

    def close_all(self) -> None:
        with self._lock:
            self._close_all_locked()

    def close_owned_by(self, owner: str) -> None:
        """Closes the recordings a sharer page started (it has gone for good); what they hold is kept."""
        with self._lock:
            for rec_id, rec in list(self._open.items()):
                if rec.owner == owner:
                    self._close_locked(rec)
                    del self._open[rec_id]


def _unquote(value: str) -> str | None:
    """A user-dirs.dirs value: "double quoted" with backslash escapes, 'single quoted', or bare."""
    if value.startswith('"'):
        out, i = [], 1
        while i < len(value):
            ch = value[i]
            if ch == "\\" and i + 1 < len(value):
                out.append(value[i + 1])
                i += 2
                continue
            if ch == '"':
                return "".join(out)
            out.append(ch)
            i += 1
        return None  # no closing quote
    if value.startswith("'"):
        end = value.find("'", 1)
        return value[1:end] if end > 0 else None
    return value


def _is_folder(path: Path) -> bool:
    """Whether `path` is a folder; False when it can't be checked. Path.is_dir() raises PermissionError on
    Python 3.13 and older when a parent folder can't be searched, and this runs at every start."""
    try:
        return os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _videos_from_user_dirs(home: Path) -> Path | None:
    """XDG_VIDEOS_DIR from <home>/.config/user-dirs.dirs, if it names an existing folder other than home."""
    try:
        text = (home / ".config" / "user-dirs.dirs").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "XDG_VIDEOS_DIR":
            continue
        value = _unquote(value.strip())
        if value is None:
            continue
        for prefix in ("$HOME", "${HOME}"):
            rest = value[len(prefix) :]
            if value.startswith(prefix) and (not rest or rest.startswith("/")):
                found = home / rest.lstrip("/")
                break
        else:
            if value.startswith("/"):
                found = Path(value)
    if found is None or found == home or not _is_folder(found):
        return None
    return found


def _videos_from_xdg_user_dir(home: Path) -> Path | None:
    try:
        out = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    # As bytes, turned into a path the way the system does: a folder name that isn't UTF-8 (an old Latin-1
    # "Vidéos") is still that folder, not a decoding error that stops Porthole at every start.
    printed = os.fsdecode(out.stdout.strip())
    candidate = Path(printed)
    if out.returncode == 0 and printed and candidate != home and _is_folder(candidate):
        return candidate
    return None


def _known_folder(folder_id: str) -> Path | None:
    """A Windows known folder by its id (SHGetKnownFolderPath). None anywhere else, or when it can't be read."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        wanted = uuid.UUID(folder_id)
        guid = GUID(wanted.time_low, wanted.time_mid, wanted.time_hi_version, (ctypes.c_ubyte * 8)(*wanted.bytes[8:]))
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        get_path = shell32.SHGetKnownFolderPath
        get_path.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        get_path.restype = ctypes.c_long  # an HRESULT: 0 is success
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        found = ctypes.c_void_p()
        result = get_path(ctypes.byref(guid), 0, None, ctypes.byref(found))
        try:
            text = ctypes.wstring_at(found.value) if result == 0 and found.value else ""
        finally:
            ole32.CoTaskMemFree(found)  # Windows asks for it to be freed even when the call failed
    except Exception:  # an old Windows, or a missing system library: fall back to ~/Videos
        return None
    return Path(text) if text else None


def _windows_videos() -> Path | None:
    """The Videos folder, even when it moved (to OneDrive, say, or another drive); None if it can't be found."""
    try:
        found = _known_folder(FOLDERID_VIDEOS)
    except Exception:
        return None
    return found if found is not None and _is_folder(found) else None


def default_record_dir() -> Path:
    """The user's Videos folder (localised name on Linux) plus 'Screen shares'.

    Windows: the Videos known folder, else ~/Videos. macOS: ~/Movies, the Mac's video folder (Finder shows
    its name in the user's language). Elsewhere, first XDG_VIDEOS_DIR in the real home's user-dirs.dirs (this
    works inside a snap too), then `xdg-user-dir VIDEOS` (never in a sandbox, where it would read the
    sandbox's own home), then ~/Videos."""
    home = sandbox.real_home()
    if sys.platform.startswith("win"):
        return (_windows_videos() or home / "Videos") / "Screen shares"
    if sys.platform == "darwin":
        return home / "Movies" / "Screen shares"
    videos = _videos_from_user_dirs(home)
    if videos is None and sys.platform.startswith("linux") and not sandbox.sandboxed():
        videos = _videos_from_xdg_user_dir(home)
    return (videos or home / "Videos") / "Screen shares"


def open_folder(path: Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only: opens it in File Explorer
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def folder_problem(folder: Path, err: BaseException) -> str:
    """What the page says when the recordings folder couldn't be opened: where the recording is, so it can be found
    by hand, and on Linux, when `xdg-open` is missing, what opening folders needs. No error numbers."""
    words = f"Couldn't open the folder. The recording is in {folder}."
    if isinstance(err, FileNotFoundError) and Path(str(err.filename or "")).name == "xdg-open":
        words += " Opening folders needs xdg-utils."
    return words
