"""Runs the headless-Chrome browser tests (tests/browser/run.mjs).

Skipped when Chrome/Chromium or Node 22+ is missing, unless REQUIRE_BROWSER=1, which fails instead.
"""

import ntpath
import os
import shutil
import signal
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "tests" / "browser" / "run.mjs"
TIMEOUT = 180
CHROME_NAMES = ("google-chrome", "chromium", "chromium-browser")
# Windows: Chrome (machine-wide or for one user), then Chromium, under these folders; run.mjs has the same list.
_WINDOWS_CHROMES = (ntpath.join("Google", "Chrome", "Application", "chrome.exe"), ntpath.join("Chromium", "Application", "chrome.exe"))
_WINDOWS_FOLDERS = (("PROGRAMFILES", r"C:\Program Files"), ("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), ("LOCALAPPDATA", None))
_MAC_CHROMES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def chrome_install_paths(platform: str, environ) -> list[str]:
    """Where Chrome is installed when it isn't on the PATH (Windows and macOS)."""
    if platform == "win32":
        env = {key.upper(): value for key, value in environ.items()}
        folders = [env.get(variable) or default for variable, default in _WINDOWS_FOLDERS]
        return [ntpath.join(folder, inside) for inside in _WINDOWS_CHROMES for folder in folders if folder]
    if platform == "darwin":
        return list(_MAC_CHROMES)
    return []


def find_chrome(environ=None, platform=None, which=shutil.which, isfile=os.path.isfile) -> str | None:
    """CHROME_PATH if set (and only that), else Chrome or Chromium on the PATH, else in its install folder."""
    env = os.environ if environ is None else environ
    path = env.get("CHROME_PATH")
    if path:
        return path if isfile(path) else None
    for name in CHROME_NAMES:
        found = which(name)
        if found:
            return found
    for candidate in chrome_install_paths(platform or sys.platform, env):
        if isfile(candidate):
            return candidate
    return None


def find_node() -> str | None:
    """Node 22 or newer (it has a built-in WebSocket)."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout
        major = int(version.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return node if major >= 22 else None


def stop(proc: subprocess.Popen, windows: bool | None = None, run=subprocess.run) -> None:
    """Stop the runner with everything it started (Chrome, Porthole). Linux and macOS: ask its process group
    to stop (the runner then stops Chrome and the server and removes its folders), forced after 5 s.
    Windows has no process groups to signal: end the whole process tree at once."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        try:
            run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()  # in case taskkill was missing or too late
        except OSError:
            pass
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


class BrowserTest(unittest.TestCase):
    def test_sharing_in_headless_chrome(self):
        chrome, node = find_chrome(), find_node()
        missing = [name for name, found in (("Chrome or Chromium", chrome), ("Node 22 or newer", node)) if not found]
        if missing:
            message = f"{' and '.join(missing)} not found (only needed for the browser tests)"
            if os.environ.get("REQUIRE_BROWSER") == "1":
                self.fail(message + "; REQUIRE_BROWSER=1 asks for them")
            self.skipTest(message)

        env = {**os.environ, "PYTHON": sys.executable, "CHROME_PATH": chrome}
        posix = os.name == "posix"
        proc = subprocess.Popen(
            [node, str(RUN)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=posix,  # so a timeout can stop Chrome and the server too
        )
        try:
            output, _ = proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            stop(proc)
            output, _ = proc.communicate()
            self.fail(f"the browser tests took more than {TIMEOUT} s:\n{output[-4000:]}")
        self.assertEqual(proc.returncode, 0, output[-6000:])


class WrapperTest(unittest.TestCase):
    """The skip-or-fail rule, checked without starting a browser."""

    def run_without_chrome(self, require: str | None):
        env = {"CHROME_PATH": str(ROOT / "no-such-chrome")}
        if require is not None:
            env["REQUIRE_BROWSER"] = require
        with unittest.mock.patch.dict(os.environ, env):
            if require is None:
                os.environ.pop("REQUIRE_BROWSER", None)
            result = unittest.TestResult()
            BrowserTest("test_sharing_in_headless_chrome").run(result)
        return result

    def test_missing_chrome_is_skipped(self):
        result = self.run_without_chrome(None)
        self.assertEqual((len(result.skipped), len(result.failures)), (1, 0))
        self.assertIn("Chrome or Chromium", result.skipped[0][1])

    def test_missing_chrome_fails_when_required(self):
        result = self.run_without_chrome("1")
        self.assertEqual((len(result.skipped), len(result.failures)), (0, 1))
        self.assertIn("REQUIRE_BROWSER=1", result.failures[0][1])

    def test_chrome_path_wins(self):
        with unittest.mock.patch.dict(os.environ, {"CHROME_PATH": sys.executable}):
            self.assertEqual(find_chrome(), sys.executable)
        with unittest.mock.patch.dict(os.environ, {"CHROME_PATH": str(ROOT / "no-such-chrome")}):
            self.assertIsNone(find_chrome())


class FindChromeTest(unittest.TestCase):
    """Chrome is on the PATH on Linux, but on Windows and macOS it sits in its install folder."""

    WINDOWS_ENV = {
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "LOCALAPPDATA": r"C:\Users\runner\AppData\Local",
    }

    def find(self, present, platform, environ=None, on_path=None):
        return find_chrome(
            environ=self.WINDOWS_ENV if environ is None else environ,
            platform=platform,
            which=lambda name: (on_path or {}).get(name),
            isfile=lambda path: path in present,
        )

    def test_the_path_comes_first(self):
        self.assertEqual(self.find([], "linux", environ={}, on_path={"chromium": "/usr/bin/chromium"}), "/usr/bin/chromium")
        self.assertIsNone(self.find([], "linux", environ={}))

    def test_windows_install_folders(self):
        for path in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Users\runner\AppData\Local\Google\Chrome\Application\chrome.exe",
            r"C:\Users\runner\AppData\Local\Chromium\Application\chrome.exe",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.find([path], "win32"), path)

    def test_windows_folders_without_their_variables(self):
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        self.assertEqual(self.find([chrome], "win32", environ={}), chrome)
        self.assertEqual(self.find([r"D:\Apps\Google\Chrome\Application\chrome.exe"], "win32", environ={"PROGRAMFILES": r"D:\Apps"}),
                         r"D:\Apps\Google\Chrome\Application\chrome.exe")

    def test_chrome_before_chromium(self):
        chrome = r"C:\Users\runner\AppData\Local\Google\Chrome\Application\chrome.exe"
        chromium = r"C:\Program Files\Chromium\Application\chrome.exe"
        self.assertEqual(self.find([chromium, chrome], "win32"), chrome)

    def test_mac_applications_folder(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        self.assertEqual(self.find([chrome], "darwin", environ={}), chrome)

    def test_windows_paths_are_not_tried_elsewhere(self):
        self.assertIsNone(self.find([r"C:\Program Files\Google\Chrome\Application\chrome.exe"], "linux"))


class StopTest(unittest.TestCase):
    """A browser test that runs too long is stopped with everything it started (Chrome, Porthole)."""

    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

    def test_windows_ends_the_whole_process_tree(self):
        calls = []
        proc = self.FakeProcess()
        stop(proc, windows=True, run=lambda args, **kwargs: calls.append(args))
        self.assertEqual(calls, [["taskkill", "/PID", "4242", "/T", "/F"]])
        self.assertTrue(proc.killed)

    def test_windows_without_taskkill_still_ends_the_runner(self):
        def missing(args, **kwargs):
            raise FileNotFoundError("taskkill")

        proc = self.FakeProcess()
        stop(proc, windows=True, run=missing)
        self.assertTrue(proc.killed)

    @unittest.skipIf(os.name == "nt", "process groups exist on Linux and macOS")
    def test_linux_and_mac_signal_the_process_group(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        stop(proc, windows=False)
        self.assertIsNotNone(proc.wait(timeout=10))


if __name__ == "__main__":
    unittest.main()
