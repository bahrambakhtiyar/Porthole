"""appwindow.py: find a Chromium-family browser and open the sharer page in its own app window."""

import json
import ntpath
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from porthole import appwindow

URL = "http://127.0.0.1:8765/share"
WINDOW_URL = URL + "?window"  # what app.py opens in the app window


def which_from(found: dict):
    """A fake shutil.which that knows only the commands in `found`."""
    return lambda name: found.get(name)


def exists_from(paths):
    present = set(paths)
    return lambda path: path in present


class FindBrowserLinuxTest(unittest.TestCase):
    def find(self, found):
        return appwindow.find_browser(platform="linux", which=which_from(found), exists=exists_from([]))

    def test_order_of_preference(self):
        names = [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
            "brave-browser",
        ]
        self.assertEqual(list(appwindow.LINUX_BROWSERS), names)
        for i, name in enumerate(names):
            found = {other: f"/usr/bin/{other}" for other in names[i:]}
            self.assertEqual(self.find(found), [f"/usr/bin/{name}"], name)

    def test_none_when_only_other_browsers_exist(self):
        self.assertIsNone(self.find({"firefox": "/usr/bin/firefox", "epiphany": "/usr/bin/epiphany"}))
        self.assertIsNone(self.find({}))

    def test_other_unix_systems_search_the_path_too(self):
        found = appwindow.find_browser(
            platform="freebsd14", which=which_from({"chromium": "/usr/local/bin/chromium"}), exists=exists_from([])
        )
        self.assertEqual(found, ["/usr/local/bin/chromium"])


class FindBrowserWindowsTest(unittest.TestCase):
    ENV = {
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "LOCALAPPDATA": r"C:\Users\Ayla\AppData\Local",
    }
    CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    CHROME_X86 = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    CHROME_USER = r"C:\Users\Ayla\AppData\Local\Google\Chrome\Application\chrome.exe"
    EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    EDGE_64 = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    BRAVE_USER = r"C:\Users\Ayla\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"

    def find(self, present, env=None):
        return appwindow.find_browser(
            platform="win32", which=which_from({}), exists=exists_from(present), environ=env or self.ENV
        )

    def test_each_usual_place(self):
        for path in (self.CHROME, self.CHROME_X86, self.CHROME_USER, self.EDGE, self.EDGE_64, self.BRAVE, self.BRAVE_USER):
            self.assertEqual(self.find([path]), [path], path)

    def test_chrome_before_edge_before_brave(self):
        self.assertEqual(self.find([self.BRAVE, self.EDGE, self.CHROME_USER]), [self.CHROME_USER])
        self.assertEqual(self.find([self.BRAVE, self.EDGE]), [self.EDGE])

    def test_none_without_a_chromium_browser(self):
        self.assertIsNone(self.find([r"C:\Program Files\Mozilla Firefox\firefox.exe"]))

    def test_variable_names_ignore_case_and_have_defaults(self):
        env = {"PROGRAMFILES": r"D:\Apps", "localappdata": r"E:\Local"}
        self.assertEqual(
            self.find([r"D:\Apps\Google\Chrome\Application\chrome.exe"], env), [r"D:\Apps\Google\Chrome\Application\chrome.exe"]
        )
        self.assertEqual(
            self.find([r"E:\Local\Google\Chrome\Application\chrome.exe"], env), [r"E:\Local\Google\Chrome\Application\chrome.exe"]
        )
        # Program Files (x86) falls back to the usual folder when the variable is missing.
        self.assertEqual(self.find([self.EDGE], {"PROGRAMFILES": r"D:\Apps"}), [self.EDGE])

    # A 32-bit Python on 64-bit Windows sees Program Files (x86) in PROGRAMFILES; ProgramW6432 always
    # names the real 64-bit folder (it is set for 32- and 64-bit programs alike on 64-bit Windows).
    ENV_32_ON_64 = {
        "ProgramFiles": r"C:\Program Files (x86)",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "ProgramW6432": r"C:\Program Files",
        "LOCALAPPDATA": r"C:\Users\Ayla\AppData\Local",
    }

    def test_32_bit_python_finds_64_bit_browsers(self):
        for path in (self.CHROME, self.EDGE_64, self.BRAVE):
            self.assertEqual(self.find([path], self.ENV_32_ON_64), [path], path)
        for path in (self.CHROME_X86, self.EDGE, self.CHROME_USER, self.BRAVE_USER):
            self.assertEqual(self.find([path], self.ENV_32_ON_64), [path], path)

    def test_the_64_bit_folder_comes_first(self):
        self.assertEqual(self.find([self.CHROME_X86, self.CHROME], self.ENV_32_ON_64), [self.CHROME])
        self.assertEqual(self.find([self.CHROME_USER, self.CHROME], self.ENV_32_ON_64), [self.CHROME])
        self.assertEqual(self.find([self.EDGE, self.EDGE_64], self.ENV_32_ON_64), [self.EDGE_64])

    def test_chrome_edge_brave_order_with_programw6432(self):
        # A browser earlier in the order wins wherever it is.
        self.assertEqual(self.find([self.EDGE_64, self.CHROME_USER], self.ENV_32_ON_64), [self.CHROME_USER])
        self.assertEqual(self.find([self.BRAVE, self.EDGE], self.ENV_32_ON_64), [self.EDGE])

    def test_folders_in_order_and_each_once(self):
        def checked(env):
            seen = []
            appwindow.find_browser(platform="win32", which=which_from({}), exists=lambda path: seen.append(path) and False, environ=env)
            return seen

        chrome = ntpath.join("Google", "Chrome", "Application", "chrome.exe")
        self.assertEqual(
            checked(self.ENV_32_ON_64)[:3],
            [ntpath.join(folder, chrome) for folder in (r"C:\Program Files", r"C:\Program Files (x86)", r"C:\Users\Ayla\AppData\Local")],
        )
        for env in (self.ENV, self.ENV_32_ON_64, {**self.ENV, "ProgramW6432": r"C:\Program Files"}, {}):
            seen = checked(env)
            self.assertEqual(len(seen), len({ntpath.normcase(path) for path in seen}), env)
        # 64-bit Python: ProgramW6432 and ProgramFiles name the same folder, which is looked in once.
        self.assertEqual(len(checked({**self.ENV, "ProgramW6432": r"C:\Program Files"})), 9)
        self.assertEqual(len(checked({**self.ENV, "PROGRAMW6432": r"c:\program files"})), 9)

    def test_32_bit_windows_has_no_programw6432(self):
        env = {"ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\Ayla\AppData\Local"}
        self.assertEqual(self.find([self.CHROME], env), [self.CHROME])
        self.assertEqual(self.find([self.CHROME_USER], env), [self.CHROME_USER])


class FindBrowserMacTest(unittest.TestCase):
    def find(self, present, home="/Users/ayla"):
        return appwindow.find_browser(platform="darwin", which=which_from({}), exists=exists_from(present), home=home)

    def test_applications_folder(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        chromium = "/Applications/Chromium.app/Contents/MacOS/Chromium"
        brave = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        self.assertEqual(self.find([brave, chromium, edge, chrome]), [chrome])
        self.assertEqual(self.find([brave, chromium, edge]), [edge])
        self.assertEqual(self.find([brave, chromium]), [chromium])
        self.assertEqual(self.find([brave]), [brave])

    def test_the_users_own_applications_folder(self):
        mine = "/Users/ayla/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        self.assertEqual(self.find([mine]), [mine])

    def test_none_with_only_safari(self):
        self.assertIsNone(self.find(["/Applications/Safari.app/Contents/MacOS/Safari"]))


class CommandTest(unittest.TestCase):
    def test_linux_command(self):
        command = appwindow.app_window_command(["/usr/bin/google-chrome"], URL, PurePosixPath("/home/ayla/.config/porthole/browser"), platform="linux")
        self.assertEqual(
            command,
            [
                "/usr/bin/google-chrome",
                f"--app={URL}",
                "--user-data-dir=/home/ayla/.config/porthole/browser",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=960,860",
                "--class=Porthole",
            ],
        )

    def test_class_only_on_linux(self):
        for platform in ("win32", "darwin"):
            command = appwindow.app_window_command(["chrome"], URL, Path("profile"), platform=platform)
            self.assertNotIn("--class=Porthole", command, platform)
            self.assertIn(f"--app={URL}", command)
            self.assertIn("--window-size=960,860", command)

    def test_browser_may_be_several_words(self):
        command = appwindow.app_window_command(["flatpak", "run", "com.google.Chrome"], URL, Path("p"), platform="linux")
        self.assertEqual(command[:4], ["flatpak", "run", "com.google.Chrome", f"--app={URL}"])


class ProfileDirTest(unittest.TestCase):
    def test_linux_uses_the_config_folder(self):
        self.assertEqual(
            appwindow.profile_dir(platform="linux", environ={}, home=PurePosixPath("/home/ayla")),
            PurePosixPath("/home/ayla/.config/porthole/browser"),
        )
        self.assertEqual(
            appwindow.profile_dir(platform="linux", environ={"XDG_CONFIG_HOME": "/data/config"}, home=PurePosixPath("/home/ayla")),
            PurePosixPath("/data/config/porthole/browser"),
        )

    def test_windows_uses_local_appdata(self):
        # A browser profile belongs to this PC (Chrome keeps its own in LOCALAPPDATA): in Roaming it
        # would be copied around with a roaming profile.
        env = {"APPDATA": r"C:\Users\Ayla\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\Ayla\AppData\Local"}
        folder = appwindow.profile_dir(platform="win32", environ=env, home=PureWindowsPath(r"C:\Users\Ayla"))
        self.assertEqual(folder, PureWindowsPath(r"C:\Users\Ayla\AppData\Local\Porthole\browser"))
        named = appwindow.profile_dir(platform="win32", environ={"localappdata": r"D:\Local"}, home=PureWindowsPath(r"C:\Users\Ayla"))
        self.assertEqual(named, PureWindowsPath(r"D:\Local\Porthole\browser"))
        for env in ({}, {"APPDATA": r"C:\Users\Ayla\AppData\Roaming"}, {"LOCALAPPDATA": ""}):
            fallback = appwindow.profile_dir(platform="win32", environ=env, home=PureWindowsPath(r"C:\Users\Ayla"))
            self.assertEqual(fallback, PureWindowsPath(r"C:\Users\Ayla\AppData\Local\Porthole\browser"), env)

    def test_mac_uses_application_support(self):
        self.assertEqual(
            appwindow.profile_dir(platform="darwin", environ={}, home=PurePosixPath("/Users/ayla")),
            PurePosixPath("/Users/ayla/Library/Application Support/Porthole/browser"),
        )

    def test_a_snap_browser_keeps_its_profile_in_its_own_folder(self):
        # A snap-packaged Chromium can't write to hidden folders in the home folder.
        self.assertEqual(
            appwindow.profile_dir(
                platform="linux", environ={}, home=PurePosixPath("/home/ayla"), browser=["/snap/bin/chromium"]
            ),
            PurePosixPath("/home/ayla/snap/chromium/common/porthole-browser"),
        )
        self.assertEqual(
            appwindow.profile_dir(platform="linux", environ={}, home=PurePosixPath("/home/ayla"), browser=["/usr/bin/chromium"]),
            PurePosixPath("/home/ayla/.config/porthole/browser"),
        )


class FakeProcess:
    def __init__(self, args, code=0, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.code = code
        self.ended = threading.Event()

    def wait(self, timeout=None):
        self.ended.wait()
        return self.code


class FakeClock:
    """time.monotonic for the waiting thread: the browser ran for `ran` seconds."""

    def __init__(self, ran):
        self.ran = ran
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 100.0 if self.calls == 1 else 100.0 + self.ran


class OpenAppWindowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = Path(tmp.name) / "porthole" / "browser"
        self.started = []

    def popen(self, args, **kwargs):
        proc = FakeProcess(args, **kwargs)
        self.started.append(proc)
        return proc

    def test_starts_the_browser_and_reports_when_it_exits(self):
        closed = threading.Event()
        ok = appwindow.open_app_window(
            URL,
            closed.set,
            find=lambda: ["/usr/bin/chromium"],
            popen=self.popen,
            profile=self.profile,
            platform="linux",
            clock=FakeClock(ran=600),
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.started), 1)
        proc = self.started[0]
        self.assertEqual(proc.args[0], "/usr/bin/chromium")
        self.assertIn(f"--app={URL}", proc.args)
        self.assertIn(f"--user-data-dir={self.profile}", proc.args)
        self.assertTrue(self.profile.is_dir())  # made before the browser starts
        self.assertIs(proc.kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertIs(proc.kwargs.get("stderr"), subprocess.DEVNULL)
        self.assertFalse(closed.wait(0.1))  # still open
        proc.ended.set()  # the window closes, so the browser exits
        self.assertTrue(closed.wait(3))

    def test_no_browser_means_false_and_nothing_started(self):
        ok = appwindow.open_app_window(URL, lambda: None, find=lambda: None, popen=self.popen, profile=self.profile)
        self.assertFalse(ok)
        self.assertEqual(self.started, [])

    def test_a_browser_that_fails_to_start_means_false(self):
        def broken(args, **kwargs):
            raise FileNotFoundError(args[0])

        closed = threading.Event()
        ok = appwindow.open_app_window(URL, closed.set, find=lambda: ["/gone/chrome"], popen=broken, profile=self.profile)
        self.assertFalse(ok)
        self.assertFalse(closed.wait(0.1))

    def test_without_on_closed_nothing_waits(self):
        ok = appwindow.open_app_window(URL, None, find=lambda: ["/usr/bin/chromium"], popen=self.popen, profile=self.profile)
        self.assertTrue(ok)
        self.started[0].ended.set()  # nothing to call, and nothing breaks

    def test_the_profile_folder_follows_the_browser(self):
        seen = []

        def fake_profile_dir(browser=None, **kwargs):
            seen.append(browser)
            return self.profile

        original = appwindow.profile_dir
        appwindow.profile_dir = fake_profile_dir
        try:
            appwindow.open_app_window(URL, None, find=lambda: ["/snap/bin/chromium"], popen=self.popen)
        finally:
            appwindow.profile_dir = original
        self.assertEqual(seen, [["/snap/bin/chromium"]])
        self.assertIn(f"--user-data-dir={self.profile}", self.started[0].args)

    def test_the_page_url_is_passed_as_given(self):
        appwindow.open_app_window(URL + "?window", None, find=lambda: ["/usr/bin/chromium"], popen=self.popen, profile=self.profile)
        self.assertIn(f"--app={URL}?window", self.started[0].args)

    def test_windows_paths_are_joined_with_backslashes(self):
        self.assertEqual(ntpath.join(r"C:\Program Files", "Google"), r"C:\Program Files\Google")  # sanity
        found = appwindow.find_browser(
            platform="win32",
            which=which_from({}),
            exists=lambda path: "\\Google\\Chrome\\Application\\chrome.exe" in path,
            environ={"ProgramFiles": r"C:\Program Files"},
        )
        self.assertEqual(found, [r"C:\Program Files\Google\Chrome\Application\chrome.exe"])


@unittest.skipIf(os.name == "nt", "the browser's lock is a symbolic link on Linux and macOS")
class LockOwnerTest(unittest.TestCase):
    """Chromium marks a profile in use with a SingletonLock link to "<host name>-<process id>"."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = Path(tmp.name)

    def lock(self, target):
        (self.profile / "SingletonLock").symlink_to(target)

    def test_reads_the_process_on_this_pc(self):
        self.lock("ayla-pc-4242")
        self.assertEqual(appwindow.lock_owner(self.profile, hostname="ayla-pc"), 4242)

    def test_uses_this_pcs_name_by_default(self):
        self.lock(f"{socket.gethostname()}-77")
        self.assertEqual(appwindow.lock_owner(self.profile), 77)

    def test_none_without_a_lock(self):
        self.assertIsNone(appwindow.lock_owner(self.profile, hostname="ayla-pc"))
        self.assertIsNone(appwindow.lock_owner(self.profile / "missing", hostname="ayla-pc"))

    def test_another_pc_or_an_odd_link_is_no_owner(self):
        for target in ("other-pc-4242", "ayla-pc-", "ayla-pc-12x", "4242", "ayla-pc--5", "ayla-pc-\u00b2", "ayla-pc-\u0664\u0662"):
            with self.subTest(target=target):
                (self.profile / "SingletonLock").unlink(missing_ok=True)
                self.lock(target)
                self.assertIsNone(appwindow.lock_owner(self.profile, hostname="ayla-pc"))

    def test_a_plain_file_is_no_owner(self):
        (self.profile / "SingletonLock").write_text("ayla-pc-4242", encoding="utf-8")
        self.assertIsNone(appwindow.lock_owner(self.profile, hostname="ayla-pc"))


@unittest.skipIf(os.name == "nt", "checked with signal 0, which would end the process on Windows")
class ProcessAliveTest(unittest.TestCase):
    def test_this_process_is_alive(self):
        self.assertTrue(appwindow.process_alive(os.getpid()))

    def test_a_finished_process_is_not(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(appwindow.process_alive(proc.pid))

    def test_odd_numbers_are_not(self):
        for pid in (0, -1):
            self.assertFalse(appwindow.process_alive(pid))


class WindowsProcessCheckTest(unittest.TestCase):
    def test_never_signals_a_process_on_windows(self):
        # On Windows os.kill(pid, 0) ends the process instead of checking it.
        with (
            mock.patch.object(appwindow.os, "name", "nt"),
            mock.patch.object(appwindow.os, "kill", side_effect=AssertionError("os.kill would end it")),
        ):
            self.assertFalse(appwindow.process_alive(os.getpid()))


class EarlyExitTest(unittest.TestCase):
    """A browser that exits within a few seconds either handed the page to this profile's browser that was
    already running (say, a window left from an earlier run), couldn't start, or its window was closed at once."""

    HOST = "ayla-pc"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = Path(tmp.name) / "browser"
        self.events = []
        self.done = threading.Event()
        self.alive_answers = []  # what process_alive says, in turn; then False
        self.checked = []
        self.slept = []

    def alive(self, pid):
        self.checked.append(pid)
        return self.alive_answers.pop(0) if self.alive_answers else False

    def sleep(self, seconds):
        self.slept.append(seconds)

    def owner(self, profile):
        """A fake lock_owner: the process number in this profile's lock, if any (LockOwnerTest reads real
        locks; a fake keeps these tests free of symbolic links, which need extra rights on Windows)."""
        self.assertEqual(Path(profile), self.profile)
        return self.lock_pid

    lock_pid = None

    def lock(self, pid):
        self.lock_pid = pid

    def closed(self):
        self.events.append("closed")
        self.done.set()

    def failed(self):
        self.events.append("failed")
        self.done.set()

    def run_browser(self, code=0, ran=1.0, platform="linux", tied=True, wait=3.0):
        def popen(args, **kwargs):
            proc = FakeProcess(args, code=code)
            proc.ended.set()  # exits straight away
            return proc

        ok = appwindow.open_app_window(
            URL,
            self.closed if tied else None,
            on_failed=self.failed,
            find=lambda: ["/usr/bin/chromium"],
            popen=popen,
            profile=self.profile,
            platform=platform,
            clock=FakeClock(ran),
            sleep=self.sleep,
            owner=self.owner,
            alive=self.alive,
        )
        self.assertTrue(ok)
        return self.done.wait(wait)  # the fake browser and clock make the thread finish at once

    def test_a_late_exit_is_the_window_closing(self):
        self.assertTrue(self.run_browser(code=0, ran=600))
        self.assertEqual(self.events, ["closed"])
        self.assertEqual(self.checked, [])

    def test_a_late_exit_with_an_error_still_means_the_window_is_gone(self):
        self.assertTrue(self.run_browser(code=1, ran=600))
        self.assertEqual(self.events, ["closed"])

    def test_handed_to_a_running_browser_watches_that_browser(self):
        # A window left open from an earlier run took the page: Porthole stops when that browser exits,
        # that is, when its last window closes.
        self.lock(4242)
        self.alive_answers = [True, True, True, True]
        self.assertTrue(self.run_browser(code=0))
        self.assertEqual(self.events, ["closed"])
        self.assertEqual(set(self.checked), {4242})
        self.assertGreaterEqual(len(self.checked), 5)
        self.assertIn(appwindow.WATCH_SECONDS, self.slept)

    def test_handed_on_even_if_the_exit_code_is_not_zero(self):
        self.lock(4242)
        self.alive_answers = [True, True]
        self.assertTrue(self.run_browser(code=21))
        self.assertEqual(self.events, ["closed"])

    def test_an_error_without_a_running_browser_falls_back_to_a_tab(self):
        self.assertTrue(self.run_browser(code=1))
        self.assertEqual(self.events, ["failed"])
        self.assertEqual(self.slept, [])  # at once: nothing to wait for

    def test_a_stale_lock_is_not_a_running_browser(self):
        self.lock(4242)  # left by a browser that crashed: nothing runs with that number
        self.assertTrue(self.run_browser(code=1))
        self.assertEqual(self.events, ["failed"])
        self.assertEqual(self.checked, [4242])

    def test_a_clean_exit_with_a_stale_lock_is_the_window_closing(self):
        self.lock(4242)
        self.assertTrue(self.run_browser(code=0))
        self.assertEqual(self.events, ["closed"])
        self.assertNotIn("failed", self.events)

    def test_a_window_closed_at_once_stops_porthole(self):
        # No lock left behind and a clean exit: the window was closed right after it opened.
        self.assertTrue(self.run_browser(code=0))
        self.assertEqual(self.events, ["closed"])
        self.assertTrue(self.slept)  # it looked for a browser that took the page for a moment first
        self.assertLessEqual(sum(self.slept), appwindow.LOCK_WAIT_SECONDS + 0.01)

    def test_a_browser_that_takes_a_moment_to_lock_is_found(self):
        # A launcher script that starts the browser and exits: the lock appears a moment later.
        real_sleep = self.sleep

        def sleep(seconds):
            real_sleep(seconds)
            if len(self.slept) == 2:
                self.lock(5151)

        self.sleep = sleep
        self.alive_answers = [True, True]
        self.assertTrue(self.run_browser(code=0))
        self.assertEqual(self.events, ["closed"])
        self.assertIn(5151, self.checked)

    def test_its_own_process_number_in_the_lock_is_not_another_browser(self):
        self.lock(12345)

        def popen(args, **kwargs):
            proc = FakeProcess(args, code=1)
            proc.pid = 12345
            proc.ended.set()
            return proc

        appwindow.open_app_window(
            URL, self.closed, on_failed=self.failed, find=lambda: ["/usr/bin/chromium"], popen=popen,
            profile=self.profile, platform="linux", clock=FakeClock(1.0), sleep=self.sleep, owner=self.owner,
            alive=lambda pid: True,
        )
        self.assertTrue(self.done.wait(3))
        self.assertEqual(self.events, ["failed"])

    def test_not_tied_it_never_watches_but_still_falls_back(self):
        # "Already running" opens a window that this copy's lifetime doesn't follow.
        self.lock(4242)
        self.alive_answers = [True] * 10
        self.assertFalse(self.run_browser(code=0, tied=False, wait=0.3))
        self.assertEqual(self.events, [])
        self.assertNotIn(appwindow.WATCH_SECONDS, self.slept)
        self.done.clear()
        self.lock_pid = None
        self.assertTrue(self.run_browser(code=1, tied=False))
        self.assertEqual(self.events, ["failed"])

    def test_tied_again_while_watching_a_browser_that_took_the_page(self):
        # The window opened in a browser already running with Porthole's profile (say, one left from an earlier
        # run): the one Porthole started has exited, but that browser is still watched, so it can be tied again.
        self.lock(4242)
        self.alive_answers = [True] * 6
        gate = threading.Event()

        def sleep(seconds):
            self.slept.append(seconds)
            gate.wait(3)

        def popen(args, **kwargs):
            proc = FakeBrowser(args)
            proc.ended.set()  # hands the page over and exits at once
            return proc

        window = appwindow.open_app_window(
            URL, self.closed, on_failed=self.failed, find=lambda: ["/usr/bin/chromium"], popen=popen,
            profile=self.profile, platform="linux", clock=FakeClock(1.0), sleep=sleep, owner=self.owner,
            alive=self.alive,
        )
        window.untie()
        window.retie()
        self.assertTrue(window.tied)
        gate.set()
        self.assertTrue(self.done.wait(3))
        self.assertEqual(self.events, ["closed"])

    def test_settle_waits_until_a_failed_start_has_opened_a_tab(self):
        # "Already running" ends right after opening the window: it waits for this before it does.
        def popen(args, **kwargs):
            proc = FakeBrowser(args, code=1)
            threading.Timer(0.2, proc.ended.set).start()  # fails a moment after starting
            return proc

        window = appwindow.open_app_window(
            URL, None, on_failed=self.failed, find=lambda: ["/usr/bin/chromium"], popen=popen, profile=self.profile,
            platform="linux", sleep=self.sleep, owner=self.owner, alive=self.alive,
        )
        window.settle()
        self.assertEqual(self.events, ["failed"])

    def test_settle_returns_once_the_browser_has_run_a_while(self):
        def popen(args, **kwargs):
            return FakeBrowser(args)  # keeps running

        window = appwindow.open_app_window(
            URL, None, on_failed=self.failed, find=lambda: ["/usr/bin/chromium"], popen=popen, profile=self.profile,
            platform="linux", sleep=self.sleep, owner=self.owner, alive=self.alive,
        )
        started = time.monotonic()
        with mock.patch.object(appwindow, "EARLY_EXIT_SECONDS", 0.3):
            window.settle()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.events, [])
        window.proc.ended.set()

    def test_windows_has_no_lock_to_read(self):
        # A quick clean exit there handed the page to a window already open: Porthole keeps running.
        self.lock(4242)
        self.alive_answers = [True] * 10
        self.assertFalse(self.run_browser(code=0, platform="win32", wait=0.3))
        self.assertEqual(self.events, [])
        self.assertEqual(self.checked, [])
        self.assertTrue(self.run_browser(code=1, platform="win32"))
        self.assertEqual(self.events, ["failed"])

    def test_the_early_exit_limit(self):
        self.assertEqual(appwindow.EARLY_EXIT_SECONDS, 5)


class FakeTime:
    """time.monotonic and a sleep that only moves this clock on."""

    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PageWatchTest(unittest.TestCase):
    """macOS: Chrome keeps running after its last window closes, so its exit can't say the window closed. The
    sharer page can: once it has connected, gone for longer than a reload takes means the window closed."""

    def watch(self, present_at, stop_at=120.0):
        """Run watch_page with a fake clock: present_at(t) says whether a sharer page is connected at time t."""
        time = FakeTime()
        stop = threading.Event()
        gone = []

        def present():
            if time.now >= stop_at:
                stop.set()
            return present_at(time.now)

        result = appwindow.watch_page(present, lambda: gone.append(time.now), stop=stop, clock=time.clock, sleep=time.sleep)
        return result, gone

    def test_a_reload_within_the_grace_keeps_running(self):
        # The page reloads at 10 s and is back 1.5 s later.
        result, gone = self.watch(lambda t: not 10 <= t < 11.5)
        self.assertFalse(result)
        self.assertEqual(gone, [])

    def test_gone_longer_than_the_grace_means_the_window_closed(self):
        result, gone = self.watch(lambda t: t < 10)
        self.assertTrue(result)
        self.assertEqual(len(gone), 1)
        self.assertGreaterEqual(gone[0], 10 + appwindow.PAGE_GONE_SECONDS)
        self.assertLessEqual(gone[0], 10 + appwindow.PAGE_GONE_SECONDS + 2 * appwindow.PAGE_STEP_SECONDS)

    def test_a_page_that_never_came_means_the_window_never_showed(self):
        result, gone = self.watch(lambda t: False)
        self.assertTrue(result)
        self.assertGreaterEqual(gone[0], appwindow.PAGE_FIRST_SECONDS)
        self.assertLessEqual(gone[0], appwindow.PAGE_FIRST_SECONDS + 2 * appwindow.PAGE_STEP_SECONDS)

    def test_a_slow_first_page_is_waited_for(self):
        # A cold browser can take a while: the grace starts only once the page has come.
        result, gone = self.watch(lambda t: t >= 30)
        self.assertFalse(result)
        self.assertEqual(gone, [])

    def test_several_reloads_never_add_up(self):
        result, gone = self.watch(lambda t: int(t) % 4 != 3)  # away for 1 s in every 4
        self.assertFalse(result)
        self.assertEqual(gone, [])

    def test_a_check_that_fails_counts_as_present(self):
        def broken(t):
            raise RuntimeError("hub gone")

        # Checks that always fail, for two minutes: counted as "no page", the window would count as never
        # shown after PAGE_FIRST_SECONDS and Porthole would stop.
        result, gone = self.watch(broken, stop_at=2 * appwindow.PAGE_FIRST_SECONDS)
        self.assertFalse(result)
        self.assertEqual(gone, [])

        def fails_after_10_s(t):
            if t >= 10:
                raise RuntimeError("hub gone")
            return True

        # The page was there, then the checks fail: counted as "gone", Porthole would stop 5 s later.
        result, gone = self.watch(fails_after_10_s, stop_at=10 + 4 * appwindow.PAGE_GONE_SECONDS)
        self.assertFalse(result)
        self.assertEqual(gone, [])

    def test_stops_when_asked(self):
        stop = threading.Event()
        stop.set()
        self.assertFalse(appwindow.watch_page(lambda: True, lambda: self.fail("gone"), stop=stop))

    def test_the_grace(self):
        # A reload is back within a second or two (the server notices a closed page within about a second).
        self.assertEqual(appwindow.PAGE_GONE_SECONDS, 5)
        self.assertEqual(appwindow.PAGE_FIRST_SECONDS, 60)


class FakeBrowser(FakeProcess):
    """A browser process that answers terminate() and kill() (or ignores terminate)."""

    pid = 4321

    def __init__(self, args, code=0, ignores_terminate=False, **kwargs):
        super().__init__(args, code, **kwargs)
        self.ignores_terminate = ignores_terminate
        self.signals = []

    def poll(self):
        return self.code if self.ended.is_set() else None

    def wait(self, timeout=None):
        if not self.ended.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.code

    def terminate(self):
        self.signals.append("terminate")
        if not self.ignores_terminate:
            self.code = -15
            self.ended.set()

    def kill(self):
        self.signals.append("kill")
        self.code = -9
        self.ended.set()


class MacAppWindowTest(unittest.TestCase):
    """open_app_window on macOS: when the sharer page has been gone for a while, Porthole reports the window
    closed, as the browser's exit does on Linux, and ends the browser it started (by its handle, nothing else)
    when nothing but Porthole's page is open in it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = Path(tmp.name) / "browser"
        self.browsers = []
        self.launches = []  # (command, keyword arguments) for each browser started
        self.watches = []  # (present, on_gone, stop) for each page watch started
        self.watching = threading.Event()
        self.closed = []
        self.done = threading.Event()

    def popen(self, args, **kwargs):
        self.launches.append((list(args), kwargs))
        browser = FakeBrowser(args, ignores_terminate=self.ignores_terminate)
        self.browsers.append(browser)
        return browser

    ignores_terminate = False

    def fake_watch(self, present, on_gone, stop=None):
        self.watches.append((present, on_gone, stop))
        self.watching.set()
        return False

    def on_closed(self):
        self.closed.append("closed")
        self.done.set()

    def open(self, platform="darwin", tied=True, page_present=lambda: True, pages=lambda: [], **kwargs):
        """`pages()`: the pages open in the browser now (none: the app window has closed)."""
        window = appwindow.open_app_window(
            WINDOW_URL,
            self.on_closed if tied else None,
            on_failed=lambda: self.closed.append("failed"),
            find=lambda: ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
            popen=self.popen,
            profile=self.profile,
            platform=platform,
            page_present=page_present,
            watch=self.fake_watch,
            pages=pages,
            **kwargs,
        )
        self.assertTrue(window)
        self.addCleanup(window.release)
        return window

    def test_the_page_gone_ends_our_browser_and_reports_the_window_closed(self):
        present = lambda: True  # noqa: E731
        window = self.open(page_present=present)
        self.assertTrue(self.watching.wait(3))
        watched_present, on_gone, _stop = self.watches[0]
        self.assertIs(watched_present, present)
        self.assertIs(window.proc, self.browsers[0])
        self.assertEqual(self.closed, [])
        on_gone()  # the sharer page has been away for longer than the grace
        self.assertTrue(self.done.wait(3))
        self.assertEqual(self.browsers[0].signals, ["terminate"])
        self.assertEqual(self.closed, ["closed"])

    def test_the_page_gone_with_only_our_page_left_ends_our_browser(self):
        # The page watch went by the hub; the window may still show Porthole's page (say, it never connected).
        self.open(pages=lambda: [WINDOW_URL])
        self.assertTrue(self.watching.wait(3))
        self.watches[0][1]()
        self.assertTrue(self.done.wait(3))
        self.assertEqual(self.browsers[0].signals, ["terminate"])
        self.assertEqual(self.closed, ["closed"])

    def test_the_page_gone_leaves_a_browser_the_person_uses(self):
        # macOS can send the person's own Chrome (a Dock click, a link from another app) to the Chrome Porthole
        # started. Ending it would close their windows: Porthole stops and leaves that browser running.
        for others in (["https://mail.example/inbox"], ["chrome://newtab/"], ["http://127.0.0.1:8765/share"]):
            with self.subTest(others=others):
                self.closed.clear()
                self.done.clear()
                self.watching.clear()
                self.watches.clear()
                self.open(pages=lambda: list(others))
                self.assertTrue(self.watching.wait(3))
                self.watches[0][1]()
                self.assertTrue(self.done.wait(3))
                self.assertEqual(self.browsers[-1].signals, [])
                self.assertEqual(self.closed, ["closed"])

    def test_the_page_gone_leaves_the_browser_when_it_cant_tell(self):
        # No answer from the browser (or a broken one): never end what might hold the person's windows.
        def broken():
            raise OSError("pipe gone")

        for pages in (lambda: None, broken, None):
            with self.subTest(pages=pages):
                self.closed.clear()
                self.done.clear()
                self.watching.clear()
                self.watches.clear()
                self.open(pages=pages) if pages is not None else self.open_without_devtools()
                self.assertTrue(self.watching.wait(3))
                self.watches[0][1]()
                self.assertTrue(self.done.wait(3))
                self.assertEqual(self.browsers[-1].signals, [])
                self.assertEqual(self.closed, ["closed"])

    def open_without_devtools(self):
        """The Mac browser started without its DevTools pipe (the pipe couldn't be made)."""
        with mock.patch.object(appwindow.os, "pipe", mock.Mock(side_effect=OSError("too many open files"))):
            window = self.open(pages=None)
        self.assertEqual(self.launches[-1][1].get("stdin"), subprocess.DEVNULL)
        self.assertNotIn("--remote-debugging-pipe", self.launches[-1][0])
        return window

    def test_a_browser_left_running_is_reported_closed_only_once(self):
        self.open(pages=lambda: ["https://mail.example/inbox"])
        self.assertTrue(self.watching.wait(3))
        self.watches[0][1]()
        self.assertTrue(self.done.wait(3))
        self.browsers[0].ended.set()  # later the person quits that browser
        self.assertFalse(threading.Event().wait(0.2))
        self.assertEqual(self.closed, ["closed"])

    def test_the_mac_browser_gets_a_devtools_pipe(self):
        # Through the pipe Porthole asks the browser which pages are open. A pipe, not a network port: nothing
        # else on the PC can reach it. The browser reads it as fd 3 and answers on fd 4 (Chrome's rule).
        self.open()
        command, kwargs = self.launches[0]
        self.assertEqual(command[:3], ["/bin/sh", "-c", appwindow.DEVTOOLS_SHELL])
        self.assertEqual(command[3], "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        self.assertIn(f"--app={WINDOW_URL}", command)
        self.assertIn(f"--user-data-dir={self.profile}", command)
        self.assertIn("--remote-debugging-pipe", command)
        self.assertNotIn("--remote-debugging-port", " ".join(command))
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIsInstance(kwargs.get(stream), int, stream)
        self.assertTrue(kwargs.get("start_new_session"))

    def test_the_browser_ends_of_the_pipe_are_closed_in_porthole(self):
        window = self.open()
        kwargs = self.launches[0][1]
        for stream in ("stdin", "stdout"):  # the browser's reading and answering ends: only the browser holds them
            with self.assertRaises(OSError, msg=stream):
                os.fstat(kwargs[stream])
        os.fstat(kwargs["stderr"])  # Porthole's sending end, which the browser also keeps a copy of
        window.release()
        with self.assertRaises(OSError):
            os.fstat(kwargs["stderr"])
        window.release()  # twice is fine

    def test_linux_and_windows_get_no_pipe(self):
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                self.open(platform=platform)
                command, kwargs = self.launches[-1]
                self.assertEqual(command[0], "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                self.assertNotIn("--remote-debugging-pipe", command)
                self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)

    def test_the_shell_line_uses_single_digit_numbers_only(self):
        # The Mac's /bin/sh (bash 3.2) and dash both take these; numbers above 9 aren't portable.
        self.assertEqual(
            appwindow.DEVTOOLS_SHELL, 'exec "$0" "$@" 3<&0 4>&1 5>&2 0</dev/null 1>/dev/null 2>/dev/null'
        )

    def test_a_browser_that_ignores_terminate_is_killed(self):
        self.ignores_terminate = True
        with mock.patch.object(appwindow, "END_WAIT_SECONDS", 0.05):
            self.open()
            self.assertTrue(self.watching.wait(3))
            self.watches[0][1]()
        self.assertTrue(self.done.wait(3))
        self.assertEqual(self.browsers[0].signals, ["terminate", "kill"])
        self.assertEqual(self.closed, ["closed"])

    def test_only_on_a_mac(self):
        # Linux and Windows browsers exit when their window closes: their exit says so, as before.
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                self.open(platform=platform)
        self.assertFalse(self.watching.wait(0.2))
        self.assertEqual(self.watches, [])

    def test_never_without_a_tie_or_a_page_to_watch(self):
        self.open(tied=False)  # "Already running": this copy's lifetime doesn't follow the window
        self.open(page_present=None)
        self.assertFalse(self.watching.wait(0.2))

    def test_quitting_the_browser_still_stops_porthole_once(self):
        self.open(clock=FakeClock(ran=600))
        self.assertTrue(self.watching.wait(3))
        self.browsers[0].ended.set()  # Cmd+Q in the app window
        self.assertTrue(self.done.wait(3))
        self.assertTrue(self.watches[0][2].wait(3))  # the page watch is told to stop
        self.watches[0][1]()  # a late "page gone" changes nothing
        self.assertEqual(self.browsers[0].signals, [])
        self.assertEqual(self.closed, ["closed"])

    def test_untied_it_never_ends_the_browser_or_reports(self):
        # "Open in my browser": the page moved to a tab, so the window no longer stops Porthole.
        window = self.open()
        self.assertTrue(self.watching.wait(3))
        window.untie()
        self.assertTrue(self.watches[0][2].is_set())  # the page watch stops
        self.watches[0][1]()
        self.assertEqual(self.browsers[0].signals, [])
        self.browsers[0].ended.set()
        self.assertFalse(self.done.wait(0.3))
        self.assertEqual(self.closed, [])

    def test_tied_again_its_closing_stops_porthole(self):
        # A browser tab took over ("Use this one"), then the window's page shared again (reloaded, or Use this one
        # there): closing the window must stop Porthole again, as the README and docs/design.md say.
        window = self.open(platform="linux", clock=FakeClock(ran=600))
        window.untie()
        window.retie()
        self.assertTrue(window.tied)
        self.browsers[0].ended.set()
        self.assertTrue(self.done.wait(3))
        self.assertEqual(self.closed, ["closed"])

    def test_tied_again_on_a_mac_the_page_is_watched_again(self):
        window = self.open()
        self.assertTrue(self.watching.wait(3))
        window.untie()
        self.watching.clear()
        window.retie()
        self.assertTrue(self.watching.wait(3))
        self.assertFalse(self.watches[-1][2].is_set())  # a watch of its own, not the stopped one
        self.watches[-1][1]()  # the page has gone: the window closed
        self.assertTrue(self.done.wait(3))
        self.assertEqual(self.closed, ["closed"])

    def test_a_browser_that_has_exited_stays_untied(self):
        window = self.open(platform="linux")
        window.untie()
        self.browsers[0].ended.set()
        self.assertTrue(window.settled.wait(3))
        window.retie()
        self.assertFalse(window.tied)
        self.assertEqual(self.closed, [])

    def test_a_browser_porthole_ends_as_it_stops_opens_no_tab(self):
        # Quit in the first seconds: close() ends the browser, and its quick exit (by a signal) must not count
        # as a browser that couldn't start, which would open a browser tab while Porthole stops.
        for tied in (True, False):
            with self.subTest(tied=tied):
                self.closed.clear()
                window = self.open(pages=lambda: [], tied=tied)
                window.close()
                self.assertEqual(self.browsers[-1].signals, ["terminate"])
                self.assertFalse(self.done.wait(0.3))
                self.assertEqual(self.closed, [])

    def test_after_open_in_my_browser_a_quick_exit_opens_no_second_tab(self):
        # The page is in a browser tab already; the app window's browser then failing changes nothing.
        window = self.open()
        window.untie()
        self.browsers[0].code = 1
        self.browsers[0].ended.set()
        self.assertFalse(self.done.wait(0.3))
        self.assertEqual(self.closed, [])

    def test_handed_to_a_browser_already_running_stops_when_the_page_is_gone(self):
        # A window left from an earlier run took the page (its lock names it). On a Mac that browser keeps
        # running without windows, so the page tells when to stop. It isn't ours to end: it is left alone.
        signalled = []

        def popen(args, **kwargs):
            browser = FakeBrowser(args)
            browser.ended.set()  # hands the page over and exits at once
            self.browsers.append(browser)
            return browser

        with mock.patch.object(appwindow.os, "kill", lambda *a: signalled.append(a)):
            window = appwindow.open_app_window(
                WINDOW_URL, self.on_closed, on_failed=lambda: self.closed.append("failed"),
                find=lambda: ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"], popen=popen,
                profile=self.profile, platform="darwin", clock=FakeClock(1.0), sleep=lambda s: threading.Event().wait(0.005),
                owner=lambda profile: 5151, alive=lambda pid: True, page_present=lambda: True, watch=self.fake_watch,
            )
            self.assertTrue(window)
            self.addCleanup(window.release)
            self.assertTrue(self.watching.wait(3))
            self.assertFalse(self.done.wait(0.2))  # the taker still runs
            self.watches[0][1]()
            self.assertTrue(self.done.wait(3))
        self.assertEqual(self.closed, ["closed"])
        self.assertEqual(signalled, [])
        self.assertEqual(self.browsers[0].signals, [])


class CloseTest(unittest.TestCase):
    """AppWindow.close() when Porthole stops (macOS): end the browser Porthole started, which outlives its
    windows there, but only while it shows nothing but Porthole's own page. macOS can send the person's own
    Chrome windows and links to that browser, and ending it would close them."""

    def window(self, pages, browser=None):
        return appwindow.AppWindow(browser or FakeBrowser(["chrome"]), WINDOW_URL, pages=pages)

    def test_ends_a_browser_that_shows_only_our_page(self):
        for pages in ([WINDOW_URL], [WINDOW_URL + "#top"], [WINDOW_URL, WINDOW_URL], []):
            with self.subTest(pages=pages):
                browser = FakeBrowser(["chrome"])
                self.window(lambda: list(pages), browser).close()
                self.assertEqual(browser.signals, ["terminate"])

    def test_leaves_a_browser_the_person_uses(self):
        for others in (
            ["https://mail.example/inbox"],
            ["chrome://newtab/"],
            ["http://127.0.0.1:8765/share"],  # "Open in my browser" opened its tab in this browser
            ["http://127.0.0.1:8765/share?window=other"],
            [""],
        ):
            with self.subTest(others=others):
                browser = FakeBrowser(["chrome"])
                self.window(lambda: [WINDOW_URL, *others], browser).close()
                self.assertEqual(browser.signals, [])

    def test_leaves_the_browser_when_it_cant_tell(self):
        def broken():
            raise OSError("pipe gone")

        for pages in (lambda: None, broken, None, lambda: "not a list", lambda: [None]):
            with self.subTest(pages=pages):
                browser = FakeBrowser(["chrome"])
                self.window(pages, browser).close()
                self.assertEqual(browser.signals, [])

    def test_without_a_page_address_nothing_counts_as_ours(self):
        browser = FakeBrowser(["chrome"])
        appwindow.AppWindow(browser, pages=lambda: [WINDOW_URL]).close()
        self.assertEqual(browser.signals, [])

    def test_untied_the_same_rule_applies(self):
        # After "Open in my browser" the app window closes itself. If the tab opened in the person's own browser,
        # Porthole's is left without windows: end it. If it opened in Porthole's, leave it (see above).
        browser = FakeBrowser(["chrome"])
        window = self.window(lambda: [], browser)
        window.untie()
        window.close()
        self.assertEqual(browser.signals, ["terminate"])
        browser = FakeBrowser(["chrome"])
        window = self.window(lambda: ["http://127.0.0.1:8765/share"], browser)
        window.untie()
        window.close()
        self.assertEqual(browser.signals, [])

    def test_close_forces_one_that_wont_quit(self):
        browser = FakeBrowser(["chrome"], ignores_terminate=True)
        self.window(lambda: [WINDOW_URL], browser).close(wait=0.05)
        self.assertEqual(browser.signals, ["terminate", "kill"])

    def test_close_leaves_a_finished_browser_alone(self):
        asked = []
        browser = FakeBrowser(["chrome"])
        browser.ended.set()
        self.window(lambda: asked.append("pages") or [WINDOW_URL], browser).close()
        self.assertEqual(browser.signals, [])
        self.assertEqual(asked, [])  # nothing to ask a browser that has gone

    def test_close_never_raises(self):
        class Gone(FakeBrowser):
            def terminate(self):
                raise ProcessLookupError("gone")

        self.window(lambda: [WINDOW_URL], Gone(["chrome"])).close(wait=0.05)  # nothing raised

    def test_close_stops_the_page_watch_and_lets_go_of_the_pipe(self):
        released = []
        window = appwindow.AppWindow(FakeBrowser(["chrome"]), WINDOW_URL, pages=lambda: ["https://mail.example/"])
        window._devtools = types.SimpleNamespace(close=lambda: released.append("pipe"))
        window.close()
        self.assertTrue(window._stop.is_set())
        self.assertEqual(released, ["pipe"])

    @unittest.skipIf(os.name == "nt", "a POSIX process stands in for the browser")
    def test_a_real_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        appwindow.end_process(proc, wait=5)
        self.assertIsNotNone(proc.wait(timeout=10))


@unittest.skipIf(os.name == "nt", "the DevTools pipe is for the Mac (select needs a socket on Windows)")
class DevToolsPipeTest(unittest.TestCase):
    """Chrome's DevTools protocol over --remote-debugging-pipe: JSON messages, each ended by a NUL byte. Porthole
    only asks which pages are open."""

    def setUp(self):
        self.received = []
        self.open_fds = set()  # the stand-in browser's ends not closed yet
        self.fresh()

    def fresh(self):
        """A new pipe pair: Porthole's DevToolsPipe on one side, the stand-in browser's ends on the other."""
        self.browser_read, to_browser = os.pipe()
        from_browser, self.browser_send = os.pipe()
        self.open_fds.update((self.browser_read, self.browser_send))
        self.pipe = appwindow.DevToolsPipe(to_browser, from_browser, timeout=1)
        self.threads = []  # the stand-in browsers on this pair
        # Cleanups run last in, first out: Porthole's ends close first, so the stand-in browser reads the end of its
        # pipe and stops, and only then are the browser's own ends closed. Closed while its thread still ran, their
        # numbers went to the next test's pipes, and the old thread read and answered the new test's command.
        self.addCleanup(self.close_browser, self.browser_read, self.browser_send)
        self.addCleanup(self.join_browsers, self.threads)
        self.addCleanup(self.pipe.close)

    def close_browser(self, *fds):
        """Close the stand-in browser's ends (by default the newest pair's), each only once."""
        for fd in fds or (self.browser_read, self.browser_send):
            if fd in self.open_fds:
                self.open_fds.discard(fd)
                os.close(fd)

    @staticmethod
    def join_browsers(threads):
        for thread in threads:
            thread.join(timeout=5)

    def browser(self, answer):
        """A stand-in browser: for each message, answer(message) gives the bytes to send back. It keeps to the pipe
        pair it started on, even after fresh() makes another."""
        read_fd, send_fd = self.browser_read, self.browser_send

        def run():
            buffer = b""
            while True:
                try:
                    chunk = os.read(read_fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\0" in buffer:
                    raw, buffer = buffer.split(b"\0", 1)
                    message = json.loads(raw)
                    self.received.append(message)
                    reply = answer(message)
                    if reply:
                        try:
                            os.write(send_fd, reply)
                        except OSError:  # Porthole has let go already
                            return

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.threads.append(thread)

    @staticmethod
    def message(data) -> bytes:
        return json.dumps(data).encode() + b"\0"

    def test_sends_one_command_and_reads_its_answer(self):
        def answer(message):
            return (
                self.message({"method": "Target.targetCreated", "params": {}})  # an event: skipped
                + self.message({"id": message["id"] + 100, "result": {"wrong": True}})  # another answer: skipped
                + self.message({"id": message["id"], "result": {"ok": 1}})
            )

        self.browser(answer)
        self.assertEqual(self.pipe.call("Target.getTargets"), {"ok": 1})
        self.assertEqual(self.pipe.call("Target.getTargets"), {"ok": 1})
        self.assertEqual([m["method"] for m in self.received], ["Target.getTargets"] * 2)
        self.assertNotEqual(self.received[0]["id"], self.received[1]["id"])

    def test_an_answer_split_over_several_reads(self):
        def answer(message):
            reply = self.message({"id": message["id"], "result": {"targetInfos": []}})
            os.write(self.browser_send, reply[:7])
            threading.Event().wait(0.05)
            return reply[7:]

        self.browser(answer)
        self.assertEqual(self.pipe.page_urls(), [])

    def test_page_urls_lists_only_pages(self):
        infos = [
            {"type": "page", "url": WINDOW_URL},
            {"type": "service_worker", "url": "chrome-extension://abc/sw.js"},
            {"type": "browser_ui", "url": "chrome://omnibox-popup.top-chrome/"},
            {"type": "page", "url": "https://mail.example/inbox"},
            {"type": "iframe", "url": "https://ads.example/"},
        ]
        self.browser(lambda m: self.message({"id": m["id"], "result": {"targetInfos": infos}}))
        self.assertEqual(self.pipe.page_urls(), [WINDOW_URL, "https://mail.example/inbox"])

    def test_no_answer_in_time_is_none(self):
        self.browser(lambda m: None)
        self.assertIsNone(self.pipe.call("Target.getTargets"))

    def test_an_error_or_an_odd_answer_is_none(self):
        for reply in (
            {"error": {"code": -32601, "message": "not found"}},
            {"result": {"targetInfos": "nope"}},
            {"result": {}},
            {"result": "odd"},
            {"result": {"targetInfos": [{"type": "page"}]}},  # a page without an address
        ):
            with self.subTest(reply=reply):
                self.fresh()
                self.browser(lambda m, r=reply: self.message({"id": m["id"], **r}))
                self.assertIsNone(self.pipe.page_urls())

    def test_a_browser_that_has_gone_is_none(self):
        self.close_browser()  # it ended: no one reads or answers
        self.assertIsNone(self.pipe.call("Target.getTargets"))
        self.assertIsNone(self.pipe.page_urls())

    def test_garbage_from_the_browser_is_none(self):
        self.browser(lambda m: b"\xff not json\0")
        self.assertIsNone(self.pipe.page_urls())

    def test_after_close_it_answers_none(self):
        self.browser(lambda m: self.message({"id": m["id"], "result": {"targetInfos": []}}))
        self.pipe.close()
        self.pipe.close()  # twice is fine
        self.assertIsNone(self.pipe.page_urls())


STAND_IN_BROWSER = r'''
import json, os, sys

# A stand-in for Chrome with --remote-debugging-pipe: commands on fd 3, answers on fd 4. It answers
# Target.getTargets with the pages listed in pages.json in its profile folder, and leaves "eof" there when
# fd 3 reaches end-of-file, where Chrome would quit.
profile = next(arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--user-data-dir="))
null = os.stat(os.devnull)
quiet = all(os.path.samestat(os.fstat(fd), null) for fd in (0, 1, 2))
with open(os.path.join(profile, "started.json"), "w") as report:
    json.dump({"quiet": quiet, "pipe": "--remote-debugging-pipe" in sys.argv}, report)
buffer = b""
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buffer += chunk
    while b"\0" in buffer:
        raw, buffer = buffer.split(b"\0", 1)
        message = json.loads(raw)
        with open(os.path.join(profile, "pages.json")) as listing:
            pages = json.load(listing)
        infos = [{"type": "page", "url": url} for url in pages] + [{"type": "service_worker", "url": "x"}]
        os.write(4, json.dumps({"id": message["id"], "result": {"targetInfos": infos}}).encode() + b"\0")
open(os.path.join(profile, "eof"), "w").close()
'''


@unittest.skipIf(os.name == "nt", "the DevTools pipe is for the Mac; the shell line needs a POSIX shell")
class DevToolsLaunchTest(unittest.TestCase):
    """The pipe through a real /bin/sh to a real process: fds 3 and 4 reach the browser, and the browser keeps
    its own copy of the sending end, so Porthole stopping never ends it (Chrome quits when that pipe closes)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.script = self.folder / "browser.py"
        self.script.write_text(STAND_IN_BROWSER, encoding="utf-8")
        self.profile = self.folder / "profile"
        self.profile.mkdir()
        self.set_pages([URL])

    def set_pages(self, pages):
        (self.profile / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    def start(self):
        window = appwindow.open_app_window(
            URL, None, find=lambda: [sys.executable, "-I", str(self.script)], profile=self.profile, platform="darwin"
        )
        self.assertTrue(window)
        self.addCleanup(lambda: window.proc.poll() is None and window.proc.kill())
        self.addCleanup(window.release)
        for _ in range(200):
            if (self.profile / "started.json").exists():
                break
            threading.Event().wait(0.05)
        started = json.loads((self.profile / "started.json").read_text(encoding="utf-8"))
        self.assertEqual(started, {"quiet": True, "pipe": True})
        return window

    def test_porthole_reads_the_pages_and_ends_a_browser_with_only_its_own(self):
        window = self.start()
        self.assertTrue(window.shows_only_ours())
        window.close()
        self.assertIsNotNone(window.proc.wait(timeout=10))

    def test_a_browser_the_person_uses_outlives_porthole(self):
        window = self.start()
        self.set_pages([URL, "https://mail.example/inbox"])
        self.assertFalse(window.shows_only_ours())
        window.close()  # leaves it running, and lets go of Porthole's ends of the pipe
        self.assertFalse(threading.Event().wait(0.5))
        self.assertIsNone(window.proc.poll())
        self.assertFalse((self.profile / "eof").exists())  # Chrome would have quit here


@unittest.skipIf(os.name == "nt", "the DevTools pipe is for the Mac")
class RealChromeDevToolsTest(unittest.TestCase):
    """The same with real Chrome (headless): it answers over the pipe, and it keeps running after Porthole lets
    go of its ends. Skipped without Chrome, unless REQUIRE_BROWSER=1."""

    def setUp(self):
        from tests.test_browser import find_chrome

        self.chrome = find_chrome()
        if not self.chrome:
            if os.environ.get("REQUIRE_BROWSER") == "1":
                self.fail("REQUIRE_BROWSER=1 but there is no Chrome or Chromium")
            self.skipTest("no Chrome or Chromium")

    @staticmethod
    def remove_profile(folder):
        # Chrome's helper processes can still write to the profile for a moment after the browser has ended.
        for _ in range(50):
            shutil.rmtree(folder, ignore_errors=True)
            if not os.path.exists(folder):
                return
            threading.Event().wait(0.1)

    def start(self, url):
        folder = tempfile.mkdtemp(prefix="porthole-profile-")
        self.addCleanup(self.remove_profile, folder)
        # --use-mock-keychain: on a Mac a fresh profile would otherwise ask the Keychain (others ignore it).
        command = [self.chrome, "--headless=new", "--use-mock-keychain"]
        window = appwindow.open_app_window(url, None, find=lambda: command, profile=Path(folder), platform="darwin")
        self.assertTrue(window)
        self.addCleanup(lambda: appwindow.end_process(window.proc, wait=5))
        self.addCleanup(window.release)
        for _ in range(100):
            if window.shows_only_ours():
                return window
            threading.Event().wait(0.1)
        self.fail("Chrome never listed Porthole's page over the pipe")

    def test_chrome_with_only_our_page_is_ended(self):
        window = self.start("data:text/html,porthole")
        window.close()
        self.assertIsNotNone(window.proc.wait(timeout=15))

    def test_chrome_the_person_uses_is_left_running(self):
        window = self.start("data:text/html,porthole")
        self.assertIsNotNone(window._devtools.call("Target.createTarget", {"url": "data:text/html,mine"}))
        self.assertFalse(window.shows_only_ours())
        window.close()
        self.assertFalse(threading.Event().wait(1.5))
        self.assertIsNone(window.proc.poll())  # without Chrome's own copy of the pipe it would have quit


if __name__ == "__main__":
    unittest.main()
