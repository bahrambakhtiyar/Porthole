import errno
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from porthole import recorder, sandbox
from porthole.recorder import Recorder, RecorderError, default_record_dir

NOW = datetime(2026, 11, 21, 14, 5, 33)


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "Videos" / "Screen shares"
        self.rec = Recorder(self.folder)
        self.addCleanup(self.rec.close_all)

    def test_start_creates_folder_and_named_file(self):
        info = self.rec.start("mp4", now=NOW)
        self.assertEqual(info["name"], "Screen share 2026-11-21 14-05-33.mp4")
        self.assertEqual(info["folder"], str(self.folder))
        self.assertTrue((self.folder / info["name"]).exists())
        self.assertTrue(info["id"])

    def test_name_clash_adds_number(self):
        first = self.rec.start("webm", now=NOW)
        self.rec.append(first["id"], 0, b"frames")  # an empty one would be deleted
        self.rec.stop(first["id"])
        second = self.rec.start("webm", now=NOW)
        self.assertEqual(second["name"], "Screen share 2026-11-21 14-05-33 (2).webm")

    def test_bad_extension_is_400(self):
        with self.assertRaises(RecorderError) as ctx:
            self.rec.start("exe", now=NOW)
        self.assertEqual(ctx.exception.status, 400)

    def test_chunks_append_in_order(self):
        info = self.rec.start("webm", now=NOW)
        self.rec.append(info["id"], 0, b"abc")
        self.rec.append(info["id"], 1, b"defg")
        done = self.rec.stop(info["id"])
        self.assertEqual(done["bytes"], 7)
        self.assertEqual(Path(done["path"]).read_bytes(), b"abcdefg")
        self.assertEqual(done["name"], info["name"])

    def test_wrong_sequence_is_409_with_expected(self):
        info = self.rec.start("webm", now=NOW)
        self.rec.append(info["id"], 0, b"a")
        with self.assertRaises(RecorderError) as ctx:
            self.rec.append(info["id"], 3, b"b")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.expected, 1)

    def test_unknown_id_is_404(self):
        with self.assertRaises(RecorderError) as ctx:
            self.rec.append("nope", 0, b"a")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(RecorderError):
            self.rec.stop("nope")

    def test_second_start_closes_first(self):
        first = self.rec.start("webm", now=NOW)
        self.rec.start("webm", now=datetime(2026, 11, 21, 14, 6, 0))
        with self.assertRaises(RecorderError) as ctx:
            self.rec.append(first["id"], 0, b"a")
        self.assertEqual(ctx.exception.status, 404)

    def test_stop_on_full_disk_is_507(self):
        info = self.rec.start("webm", now=NOW)

        class FullDisk:
            def close(self):
                raise OSError(28, "No space left on device")

        real = self.rec._open[info["id"]].handle
        self.rec._open[info["id"]].handle = FullDisk()
        real.close()
        with self.assertRaises(RecorderError) as ctx:
            self.rec.stop(info["id"])
        self.assertEqual(ctx.exception.status, 507)

    def test_default_record_dir(self):
        self.assertEqual(default_record_dir().name, "Screen shares")

    # --- nothing recorded (started while paused, stopped before Resume) ---

    def test_a_recording_with_nothing_in_it_is_deleted(self):
        info = self.rec.start("webm", now=NOW)
        done = self.rec.stop(info["id"])
        self.assertEqual(done, {"empty": True, "name": info["name"], "bytes": 0})
        self.assertEqual(os.listdir(self.folder), [])

    def test_the_page_saying_nothing_was_recorded_deletes_the_file(self):
        # Started while paused, the browser may still send a file header: no picture, so nothing to keep.
        info = self.rec.start("mp4", now=NOW)
        self.rec.append(info["id"], 0, b"header only")
        done = self.rec.stop(info["id"], recorded=False)
        self.assertTrue(done["empty"])
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_recording_with_something_in_it_is_kept(self):
        info = self.rec.start("webm", now=NOW)
        self.rec.append(info["id"], 0, b"frames")
        done = self.rec.stop(info["id"])
        self.assertNotIn("empty", done)
        self.assertEqual(Path(done["path"]).read_bytes(), b"frames")

    def test_an_empty_recording_closed_with_its_page_or_at_quit_is_deleted(self):
        kept = self.rec.start("webm", now=NOW, owner="page-one-1234")
        self.rec.append(kept["id"], 0, b"frames")
        self.rec.close_owned_by("page-one-1234")
        empty = self.rec.start("webm", now=datetime(2026, 11, 21, 14, 6, 0), owner="page-two-1234")
        self.rec.close_owned_by("page-two-1234")
        self.assertEqual(os.listdir(self.folder), [kept["name"]])
        empty = self.rec.start("webm", now=datetime(2026, 11, 21, 14, 7, 0))
        self.rec.close_all()  # Porthole quitting
        self.assertEqual(os.listdir(self.folder), [kept["name"]])
        empty = self.rec.start("webm", now=datetime(2026, 11, 21, 14, 8, 0))
        self.rec.start("webm", now=datetime(2026, 11, 21, 14, 9, 0))  # a new recording closes the old one
        self.assertNotIn(empty["name"], os.listdir(self.folder))

    # --- cut short by a full disk or another write error ---

    def fail_writes(self, rec_id, err, keep=0):
        """The recording's next write puts `keep` bytes in the file, then fails with `err`."""
        real = self.rec._open[rec_id].handle

        class Failing:
            def write(self, data):
                if keep:
                    real.write(bytes(data[:keep]))
                raise err

            def __getattr__(self, name):
                return getattr(real, name)

        self.rec._open[rec_id].handle = Failing()

    def test_a_full_disk_stops_the_recording_and_keeps_what_was_saved(self):
        info = self.rec.start("webm", now=NOW)
        self.rec.append(info["id"], 0, b"abc")
        self.fail_writes(info["id"], OSError(errno.ENOSPC, "No space left on device"), keep=2)
        with self.assertRaises(RecorderError) as ctx:
            self.rec.append(info["id"], 1, b"defgh")
        self.assertEqual(ctx.exception.status, 507)
        self.assertEqual(ctx.exception.stopped, "the disk is full")
        path = self.folder / info["name"]
        self.assertEqual(path.read_bytes(), b"abc")  # the half-written piece is taken off again
        with self.assertRaises(RecorderError) as ctx:  # a late piece: still stopped, nothing more written
            self.rec.append(info["id"], 1, b"defgh")
        self.assertEqual((ctx.exception.status, ctx.exception.stopped), (507, "the disk is full"))
        done = self.rec.stop(info["id"])
        self.assertEqual(done, {"path": str(path), "name": info["name"], "bytes": 3, "stopped": "the disk is full"})
        self.assertEqual(path.read_bytes(), b"abc")
        self.assertFalse(self.rec.busy())

    def test_a_full_disk_before_anything_was_saved_leaves_no_file(self):
        info = self.rec.start("webm", now=NOW)
        self.fail_writes(info["id"], OSError(errno.ENOSPC, "No space left on device"), keep=1)
        with self.assertRaises(RecorderError):
            self.rec.append(info["id"], 0, b"abc")
        self.assertEqual(self.rec.stop(info["id"]), {"empty": True, "name": info["name"], "bytes": 0, "stopped": "the disk is full"})
        self.assertEqual(os.listdir(self.folder), [])

    def test_other_write_errors_name_a_plain_reason(self):
        reasons = {
            "EDQUOT": "the disk is full",  # out of the space this user may use
            "EFBIG": "the file is too big for this disk",  # 4 GB on a FAT32 stick
            "EROFS": "the disk is read-only",
            "EACCES": "Porthole isn't allowed to write there",
            "EIO": "the disk can't be written to",  # a stick pulled out, say
        }
        for name, reason in reasons.items():
            code = getattr(errno, name, None)
            if code is None:  # Windows has no EDQUOT
                continue
            with self.subTest(name):
                info = self.rec.start("webm", now=NOW)
                self.rec.append(info["id"], 0, b"abc")
                self.fail_writes(info["id"], OSError(code, os.strerror(code)))
                with self.assertRaises(RecorderError) as ctx:
                    self.rec.append(info["id"], 1, b"more")
                self.assertEqual(ctx.exception.stopped, reason)
                self.assertNotIn("Errno", ctx.exception.reason)
                self.assertEqual(self.rec.stop(info["id"])["stopped"], reason)

    def test_a_short_write_carries_on_with_the_rest(self):
        # Writing straight to the file can take only part of a piece at a time (Porthole doesn't buffer, so a write
        # that fails is known at once and never comes back at close): the rest follows.
        info = self.rec.start("webm", now=NOW)
        real = self.rec._open[info["id"]].handle

        class Short:
            def write(self, data):
                return real.write(bytes(data[:2]))

            def __getattr__(self, name):
                return getattr(real, name)

        self.rec._open[info["id"]].handle = Short()
        self.rec.append(info["id"], 0, b"abcdefg")
        self.assertEqual(Path(self.rec.stop(info["id"])["path"]).read_bytes(), b"abcdefg")

    def test_the_file_is_written_unbuffered(self):
        info = self.rec.start("webm", now=NOW)
        self.assertIsInstance(self.rec._open[info["id"]].handle, io.FileIO)


class DefaultRecordDirTest(unittest.TestCase):
    """The Videos folder: user-dirs.dirs in the real home, then xdg-user-dir (not in a sandbox), then ~/Videos."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.calls = []
        self.answer = None  # what the fake xdg-user-dir prints; None means it isn't installed
        self.kind = "none"
        self.patch(sandbox, "real_home", lambda: self.home)
        self.patch(sandbox, "kind", lambda: self.kind)
        self.patch(recorder.subprocess, "run", self.fake_run)
        self.patch(recorder.sys, "platform", "linux")

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_run(self, args, **kwargs):
        self.calls.append(list(args))
        if self.answer is None:
            raise FileNotFoundError("xdg-user-dir")
        out = self.answer + (b"\n" if isinstance(self.answer, bytes) else "\n")
        if not kwargs.get("text") and isinstance(out, str):
            out = os.fsencode(out)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr=b"" if not kwargs.get("text") else "")

    def test_a_videos_path_that_isnt_utf_8_is_used_not_a_crash(self):
        # An old user-dirs.dirs with a Latin-1 "Vidéos": xdg-user-dir prints those bytes. Read as text, Porthole
        # stopped at every start with a traceback (from the menu: nothing at all).
        name = os.fsdecode(b"Vid\xe9os")
        try:
            (self.home / name).mkdir()
        except (OSError, UnicodeEncodeError):
            self.skipTest("this file system can't hold that name")
        self.answer = os.fsencode(str(self.home / name))
        self.assertEqual(recorder.default_record_dir(), self.home / name / "Screen shares")

    def user_dirs(self, text):
        config = self.home / ".config"
        config.mkdir(exist_ok=True)
        (config / "user-dirs.dirs").write_text(text, encoding="utf-8")

    def folder(self, name):
        path = self.home / name
        path.mkdir(parents=True)
        return path

    def test_reads_the_videos_line(self):
        videos = self.folder("Filmlər")
        self.user_dirs(
            "# This file is written by xdg-user-dirs-update\n"
            '# XDG_VIDEOS_DIR="$HOME/Commented"\n'
            'XDG_DESKTOP_DIR="$HOME/Desktop"\n'
            "\n"
            '  XDG_VIDEOS_DIR = "$HOME/Filmlər"  \n'
            'XDG_MUSIC_DIR="$HOME/Music"\n'
        )
        self.assertEqual(default_record_dir(), videos / "Screen shares")
        self.assertEqual(self.calls, [])  # no need to ask xdg-user-dir

    @unittest.skipIf(os.name == "nt", "user-dirs.dirs names Linux paths, which start with /")
    def test_absolute_paths(self):
        elsewhere = self.folder("disk/Clips")
        self.user_dirs(f'XDG_VIDEOS_DIR="{elsewhere.as_posix()}"\n')
        self.assertEqual(default_record_dir(), elsewhere / "Screen shares")

    def test_braced_paths(self):
        braced = self.folder("Braced")
        self.user_dirs('XDG_VIDEOS_DIR="${HOME}/Braced"\n')
        self.assertEqual(default_record_dir(), braced / "Screen shares")

    def test_unquoted_and_escaped_values(self):
        plain = self.folder("Plain")
        self.user_dirs("XDG_VIDEOS_DIR=$HOME/Plain\n")
        self.assertEqual(default_record_dir(), plain / "Screen shares")
        dollar = self.folder("Clips$1")
        self.user_dirs('XDG_VIDEOS_DIR="$HOME/Clips\\$1"\n')
        self.assertEqual(default_record_dir(), dollar / "Screen shares")

    def test_the_last_videos_line_wins(self):
        self.folder("First")
        second = self.folder("Second")
        self.user_dirs('XDG_VIDEOS_DIR="$HOME/First"\nXDG_VIDEOS_DIR="$HOME/Second"\n')
        self.assertEqual(default_record_dir(), second / "Screen shares")

    def test_home_itself_a_missing_folder_or_a_relative_path_fall_back(self):
        for line in ('XDG_VIDEOS_DIR="$HOME/"', 'XDG_VIDEOS_DIR="$HOME"', 'XDG_VIDEOS_DIR="$HOME/Gone"',
                     'XDG_VIDEOS_DIR="Videos"', 'XDG_VIDEOS_DIR="$HOMEWORK/x"', 'XDG_VIDEOS_DIR="$HOME/x'):
            self.user_dirs(line + "\n")
            self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares", line)

    def test_an_unreadable_file_falls_back(self):
        config = self.home / ".config"
        config.mkdir()
        (config / "user-dirs.dirs").write_bytes(b"\xff\xfe\x00 not text")
        self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")

    def test_asks_xdg_user_dir_when_the_file_has_no_answer(self):
        localised = self.folder("Vidéos")
        self.answer = str(localised)
        self.assertEqual(default_record_dir(), localised / "Screen shares")
        self.assertEqual(self.calls, [["xdg-user-dir", "VIDEOS"]])

    def test_xdg_user_dir_saying_home_is_ignored(self):
        self.answer = str(self.home)
        self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")

    def test_never_asks_xdg_user_dir_in_a_sandbox(self):
        # Inside a snap it would read the snap's own home, not the real one.
        self.answer = str(self.folder("Wrong"))
        for kind in ("snap", "flatpak"):
            self.kind = kind
            self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")
        self.assertEqual(self.calls, [])

    def test_never_asks_xdg_user_dir_off_linux(self):
        self.answer = str(self.folder("Wrong"))
        with (
            mock.patch.object(recorder.sys, "platform", "win32"),
            mock.patch.object(recorder, "_known_folder", lambda folder_id: None),
        ):
            self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")
        self.assertEqual(self.calls, [])

    # --- macOS: Movies is the Mac's video folder ---

    def test_a_mac_uses_the_movies_folder(self):
        self.folder("Elsewhere")
        self.user_dirs('XDG_VIDEOS_DIR="$HOME/Elsewhere"\n')  # a Linux file, never read on a Mac
        self.answer = str(self.folder("Wrong"))
        self.patch(recorder.sys, "platform", "darwin")
        self.assertEqual(default_record_dir(), self.home / "Movies" / "Screen shares")
        self.assertEqual(self.calls, [])

    # --- Windows: the Videos known folder, which follows a folder moved to OneDrive or another drive ---

    def on_windows(self, known):
        self.asked = []

        def fake_known(folder_id):
            self.asked.append(folder_id)
            if isinstance(known, BaseException):
                raise known
            return known

        self.patch(recorder.sys, "platform", "win32")
        self.patch(recorder, "_known_folder", fake_known)

    def test_windows_uses_the_videos_known_folder(self):
        moved = self.folder("OneDrive/Videos")
        self.on_windows(moved)
        self.assertEqual(default_record_dir(), moved / "Screen shares")
        self.assertEqual(self.asked, ["18989B1D-99B5-455B-841C-AB7C74E4DDFC"])  # FOLDERID_Videos
        self.assertEqual(self.calls, [])

    def test_windows_without_the_known_folder_uses_home_videos(self):
        for known in (None, OSError("no shell32"), self.home / "Gone"):
            with self.subTest(known=known):
                self.on_windows(known)
                self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")

    def test_windows_never_reads_user_dirs(self):
        self.folder("Elsewhere")
        self.user_dirs('XDG_VIDEOS_DIR="$HOME/Elsewhere"\n')
        self.on_windows(None)
        self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")

    def test_a_snap_reads_the_real_home(self):
        videos = self.folder("Videolar")
        self.user_dirs('XDG_VIDEOS_DIR="$HOME/Videolar"\n')
        self.kind = "snap"
        self.assertEqual(default_record_dir(), videos / "Screen shares")

    LOCKED = "/media/other/locked"

    def locked(self):
        """Checking a folder under LOCKED fails the way Python 3.13 and older fail on a folder that can't be
        reached: Path.is_dir() raises PermissionError instead of returning False (3.14 returns False)."""
        real_is_dir, real_stat = Path.is_dir, os.stat

        def refused(path):
            return str(path).startswith(self.LOCKED)

        def is_dir(path, *args, **kwargs):
            if refused(path):
                raise PermissionError(13, "Permission denied", str(path))
            return real_is_dir(path, *args, **kwargs)

        def stat(path, *args, **kwargs):  # what os.path.isdir() and friends call
            if refused(path):
                raise PermissionError(13, "Permission denied", str(path))
            return real_stat(path, *args, **kwargs)

        self.patch(Path, "is_dir", is_dir)
        self.patch(os, "stat", stat)

    def test_a_videos_folder_that_cant_be_reached_falls_back(self):
        # A Videos folder on a drive mounted for another account, or one a snap may not look at.
        self.user_dirs('XDG_VIDEOS_DIR="/media/other/locked/Videos"\n')
        self.locked()
        self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")

    def test_xdg_user_dir_naming_a_folder_that_cant_be_reached_falls_back(self):
        self.answer = "/media/other/locked/Videos"
        self.locked()
        self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")
        self.assertEqual(self.calls, [["xdg-user-dir", "VIDEOS"]])

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs folder permissions")
    def test_a_real_folder_whose_parent_cant_be_searched_falls_back(self):
        # The same on a real disk: Python 3.10 to 3.13 raise here, 3.14 returns False.
        parent = self.folder("locked")
        (parent / "Videos").mkdir()
        parent.chmod(0)
        self.addCleanup(parent.chmod, 0o700)
        self.user_dirs(f'XDG_VIDEOS_DIR="{(parent / "Videos").as_posix()}"\n')
        self.answer = str(parent / "Videos")
        self.assertEqual(default_record_dir(), self.home / "Videos" / "Screen shares")


class KnownFolderTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows known folders")
    def test_reads_the_real_videos_folder(self):
        found = recorder._known_folder(recorder.FOLDERID_VIDEOS)
        self.assertIsInstance(found, Path)
        self.assertTrue(found.is_absolute(), found)

    @unittest.skipIf(sys.platform == "win32", "checks the other systems")
    def test_none_on_other_systems(self):
        self.assertIsNone(recorder._known_folder(recorder.FOLDERID_VIDEOS))


class OpenFolderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "Videos" / "Screen shares"

    def test_windows_opens_explorer_with_startfile(self):
        opened = []
        with (
            mock.patch.object(recorder.sys, "platform", "win32"),
            mock.patch.object(recorder.os, "startfile", opened.append, create=True),
            mock.patch.object(recorder.subprocess, "Popen", side_effect=AssertionError("no Popen on Windows")),
        ):
            recorder.open_folder(self.folder)
        self.assertEqual(opened, [str(self.folder)])
        self.assertTrue(self.folder.is_dir())  # made first, so there is something to open

    def test_mac_uses_open(self):
        started = []
        with (
            mock.patch.object(recorder.sys, "platform", "darwin"),
            mock.patch.object(recorder.subprocess, "Popen", lambda args, **kwargs: started.append(args)),
        ):
            recorder.open_folder(self.folder)
        self.assertEqual(started, [["open", str(self.folder)]])

    def test_linux_uses_xdg_open(self):
        started = []
        with (
            mock.patch.object(recorder.sys, "platform", "linux"),
            mock.patch.object(recorder.subprocess, "Popen", lambda args, **kwargs: started.append(args)),
        ):
            recorder.open_folder(self.folder)
        self.assertEqual(started, [["xdg-open", str(self.folder)]])

    def test_a_folder_that_cant_be_opened_is_named_in_plain_words(self):
        missing = FileNotFoundError(errno.ENOENT, "No such file or directory", "xdg-open")
        self.assertEqual(
            recorder.folder_problem(self.folder, missing),
            f"Couldn't open the folder. The recording is in {self.folder}. Opening folders needs xdg-utils.",
        )
        for err in (
            OSError(errno.EACCES, "Permission denied"),  # no permission, or Windows' startfile with no file manager
            FileNotFoundError(errno.ENOENT, "No such file or directory", "open"),  # the Mac's open, if it were missing
            RuntimeError("boom"),
        ):
            with self.subTest(err=err):
                self.assertEqual(recorder.folder_problem(self.folder, err), f"Couldn't open the folder. The recording is in {self.folder}.")


@unittest.skipIf(os.name == "nt", "HOME decides the home folder only on Linux and macOS")
class RealHomeInASnapTest(unittest.TestCase):
    def test_uses_snap_real_home_not_the_snap_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            (real / ".config").mkdir(parents=True)
            (real / "Movies").mkdir()
            (real / ".config" / "user-dirs.dirs").write_text('XDG_VIDEOS_DIR="$HOME/Movies"\n', encoding="utf-8")
            env = {"SNAP": "/snap/porthole/x1", "SNAP_NAME": "porthole", "SNAP_REAL_HOME": str(real),
                   "HOME": str(Path(tmp) / "snap-home")}
            # Snaps are Linux only: on a Mac Movies would be picked by name, whatever user-dirs.dirs says.
            with mock.patch.dict(os.environ, env), mock.patch.object(recorder.sys, "platform", "linux"):
                self.assertEqual(default_record_dir(), real / "Movies" / "Screen shares")


if __name__ == "__main__":
    unittest.main()
