"""Porthole run frozen, end to end: started the way a built Windows program or Mac app starts it.

The Windows and Mac builds themselves are on the private repository's archive/windows-macos branch; the frozen-app
support they use (porthole/paths.py, the no-console and Mac alert paths in porthole/app.py) stays in the app, so
these runs keep it working. Each one starts a real server in a child process with sys.frozen and sys._MEIPASS set
by hand.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# Runs Porthole the way the windowed Porthole.exe does: frozen, its files in the bundle, no console at all.
FROZEN_START = """
import sys
bundle, program = sys.argv[1], sys.argv[2]
sys.frozen = True
sys._MEIPASS = bundle
sys.executable = program
sys.argv = [program] + sys.argv[3:]
sys.stdout = sys.stderr = None
from porthole.app import main
sys.exit(main())
"""


class FrozenRunTest(unittest.TestCase):
    def test_a_frozen_windowed_porthole_serves_its_bundle_and_quits(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        program_dir = Path(tmp.name) / "Porthole"
        bundle = program_dir / "_internal"
        shutil.copytree(ROOT / "web", bundle / "web")
        page = bundle / "web" / "share.html"
        page.write_bytes(page.read_bytes() + b"<!-- the bundled copy -->")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen(
            [sys.executable, "-c", FROZEN_START, str(bundle), str(program_dir / "Porthole.exe"),
             "--no-browser", "--no-discovery", "--port", str(port), "--record-dir", str(Path(tmp.name) / "rec")],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(proc.stderr.close)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        info = None
        deadline = time.monotonic() + 30
        while info is None and time.monotonic() < deadline and proc.poll() is None:
            try:
                with direct.open(f"{base}/api/ping", timeout=2) as resp:
                    info = json.loads(resp.read())
            except OSError:
                time.sleep(0.2)
        self.assertIsNotNone(info, proc.stderr.read().decode(errors="replace") if proc.poll() is not None else "no answer")
        self.assertEqual(Path(info["folder"]), program_dir.resolve())  # the program's folder, not the bundle
        with direct.open(f"{base}/share", timeout=5) as resp:
            self.assertIn(b"<!-- the bundled copy -->", resp.read())
        quit_request = urllib.request.Request(f"{base}/api/quit", data=b"", method="POST", headers={"Origin": base})
        with direct.open(quit_request, timeout=5) as resp:
            self.assertEqual(resp.status, 204)
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(proc.stderr.read(), b"")  # nothing tried to write anywhere


# Runs Porthole the way Porthole.app opened from Finder does: frozen on a Mac, its files in Contents/Frameworks,
# its output going to /dev/null. Alerts are written to a file instead of shown.
MAC_FROZEN_START = """
import os, sys
# The standard library modules that look at the system when they load, loaded first: on a real Mac this changes
# nothing, elsewhere they would look for Mac-only parts (urllib.request's _scproxy).
import http.server, selectors, subprocess, urllib.request, uuid, webbrowser
app_dir, alerts = sys.argv[1], sys.argv[2]
sys.platform = "darwin"
sys.frozen = True
sys._MEIPASS = os.path.join(app_dir, "Contents", "Frameworks")
sys.executable = os.path.join(app_dir, "Contents", "MacOS", "Porthole")
sys.argv = [sys.executable] + sys.argv[3:]
sys.stdout = sys.stderr = open(os.devnull, "w")
from porthole import app
def record(message, info=False):
    with open(alerts, "a", encoding="utf-8") as handle:
        handle.write(("info: " if info else "error: ") + message + "\\n")
app.show_dialog = record
sys.exit(app.main())
"""


@unittest.skipIf(os.name == "nt", "pretends to be a Mac, which a Windows Python can't")
class MacFrozenRunTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.apps = Path(tmp.name) / "Applications"
        self.bundle = self.apps / "Porthole.app"
        resources = self.bundle / "Contents" / "Resources"
        shutil.copytree(ROOT / "web", resources / "web")
        page = resources / "web" / "share.html"
        page.write_bytes(page.read_bytes() + b"<!-- the bundled copy -->")
        (self.bundle / "Contents" / "MacOS").mkdir(parents=True)
        (self.bundle / "Contents" / "Frameworks").mkdir()
        (self.bundle / "Contents" / "Frameworks" / "web").symlink_to(Path("..") / "Resources" / "web")
        self.alerts = Path(tmp.name) / "alerts.txt"
        self.records = Path(tmp.name) / "rec"

    def start(self, *args):
        return subprocess.Popen(
            [sys.executable, "-c", MAC_FROZEN_START, str(self.bundle), str(self.alerts), *args],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def alerts_shown(self) -> str:
        return self.alerts.read_text(encoding="utf-8") if self.alerts.exists() else ""

    def test_serves_its_bundle_names_the_applications_folder_and_quits(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        proc = self.start("--no-browser", "--no-discovery", "--port", str(port), "--record-dir", str(self.records))
        self.addCleanup(proc.stderr.close)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        info = None
        deadline = time.monotonic() + 30
        while info is None and time.monotonic() < deadline and proc.poll() is None:
            try:
                with direct.open(f"{base}/api/ping", timeout=2) as resp:
                    info = json.loads(resp.read())
            except OSError:
                time.sleep(0.2)
        self.assertIsNotNone(info, proc.stderr.read().decode(errors="replace") if proc.poll() is not None else "no answer")
        self.assertEqual(Path(info["folder"]), self.apps.resolve())  # the folder that holds Porthole.app
        with direct.open(f"{base}/share", timeout=5) as resp:
            self.assertIn(b"<!-- the bundled copy -->", resp.read())
        quit_request = urllib.request.Request(f"{base}/api/quit", data=b"", method="POST", headers={"Origin": base})
        with direct.open(quit_request, timeout=5) as resp:
            self.assertEqual(resp.status, 204)
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(self.alerts_shown(), "")  # nothing went wrong, so nothing popped up

    def test_bad_arguments_show_in_an_alert(self):
        proc = self.start("--port", "abc")
        self.addCleanup(proc.stderr.close)
        self.assertEqual(proc.wait(timeout=30), 2)
        shown = self.alerts_shown()
        self.assertTrue(shown.startswith("error: Porthole couldn't start: "), shown)
        self.assertIn("must be a number", shown)

    def test_install_launcher_says_where_the_app_goes(self):
        proc = self.start("--install-launcher")
        self.addCleanup(proc.stderr.close)
        self.assertEqual(proc.wait(timeout=30), 1)
        self.assertIn("info: On a Mac there's no launcher to add", self.alerts_shown())


if __name__ == "__main__":
    unittest.main()
