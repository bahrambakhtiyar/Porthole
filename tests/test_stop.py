"""Porthole as a real process: Ctrl+C, SIGTERM (kill, logging out) and SIGHUP (the terminal window closed) all stop
it the same clean way. Viewers are told it ended, and a recording the sharer page is finishing still gets through."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipIf(os.name == "nt", "POSIX signals")
class StopSignalsTest(unittest.TestCase):
    def start(self, ignore_hangup=False):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        env = {key: value for key, value in os.environ.items() if key != "PORTHOLE_COMMAND"}
        command = [
            sys.executable, str(ROOT / "start.py"), "--no-browser", "--no-discovery", "--host", "127.0.0.1",
            "--port", str(self.port), "--record-dir", str(self.folder),
        ]  # fmt: skip
        # As `nohup` starts it: a hangup is to be ignored.
        hush = (lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN)) if ignore_hangup else None
        self.proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, preexec_fn=hush
        )
        self.addCleanup(self.end)
        self.lines = []
        ready = threading.Event()

        def read():
            for line in self.proc.stdout:
                self.lines.append(line.rstrip("\n"))
                if line.startswith("Press Ctrl+C"):
                    ready.set()
            ready.set()

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.assertTrue(ready.wait(20), self.lines)
        [viewers] = [line for line in self.lines if "Viewers (phone or PC):" in line]
        self.room = viewers.rsplit("/v/", 1)[1]

    def end(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        self.proc.stdout.close()

    def connection(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def viewer(self):
        """A viewer's event stream; a list that fills with its messages."""
        conn = self.connection()
        self.addCleanup(conn.close)
        conn.request("GET", f"/events?room={self.room}&role=viewer", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        messages = []

        def read():
            try:
                while True:
                    line = resp.fp.readline().decode()
                    if not line:
                        return
                    if line.startswith("data: "):
                        messages.append(json.loads(line[6:])["type"])
            except (OSError, ValueError):
                return

        threading.Thread(target=read, daemon=True).start()
        self.wait_for(lambda: messages)
        return messages

    def api(self, path, body=b""):
        conn = self.connection()
        try:
            origin = f"http://127.0.0.1:{self.port}"
            conn.request("POST", path, body=body, headers={"Host": f"127.0.0.1:{self.port}", "Origin": origin})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    @staticmethod
    def wait_for(check, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.05)
        return False

    def stops_cleanly(self, signum):
        self.start()
        messages = self.viewer()
        self.proc.send_signal(signum)
        self.assertEqual(self.proc.wait(10), 0, self.lines)
        self.assertTrue(self.wait_for(lambda: "ended" in messages), messages)  # not left retrying for ever
        self.assertIn("Stopped.", self.lines)

    def test_ctrl_c(self):
        self.stops_cleanly(signal.SIGINT)

    def test_sigterm_stops_it_like_ctrl_c(self):
        self.stops_cleanly(signal.SIGTERM)

    def test_closing_the_terminal_stops_it_like_ctrl_c(self):
        self.stops_cleanly(signal.SIGHUP)

    def test_more_ctrl_c_while_it_stops_only_skip_the_wait(self):
        # People press Ctrl+C again when "Stopped." doesn't bring the prompt back at once: no tracebacks, and the
        # rest of the stop (the easy address's goodbye, closing a recording) still happens.
        self.start()
        self.proc.send_signal(signal.SIGINT)
        time.sleep(0.5)  # a press during the wait only skips it; then keep pressing until Porthole has gone
        deadline = time.monotonic() + 5
        while self.proc.poll() is None and time.monotonic() < deadline:
            self.proc.send_signal(signal.SIGINT)
            time.sleep(0.01)
        self.assertEqual(self.proc.wait(10), 0, self.lines)
        self.reader.join(5)
        self.assertFalse([line for line in self.lines if "Traceback" in line or "KeyboardInterrupt" in line], self.lines)
        self.assertIn("Stopped.", self.lines)

    def test_a_hangup_set_to_be_ignored_stays_ignored(self):
        self.start(ignore_hangup=True)
        messages = self.viewer()
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(1)
        self.assertIsNone(self.proc.poll())
        self.assertNotIn("ended", messages)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(10), 0)

    def test_a_recording_the_page_is_finishing_still_gets_through(self):
        # On "ended" the sharer page stops its recorder, sends the last piece and asks to stop the recording: the
        # server must still take those after Ctrl+C, or the page says the recording failed.
        self.start()
        status, body = self.api("/api/recording/start", json.dumps({"ext": "webm"}).encode())
        self.assertEqual(status, 200, body)
        rec_id = json.loads(body)["id"]
        self.proc.send_signal(signal.SIGINT)
        time.sleep(0.2)  # the page hears "ended" and its recorder hands over the last piece
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec_id}&seq=0", b"last piece")[0], 204)
        status, body = self.api(f"/api/recording/stop?id={rec_id}")
        self.assertEqual(status, 200, body)
        saved = json.loads(body)
        self.assertEqual(saved["bytes"], len(b"last piece"))
        self.assertEqual(self.proc.wait(10), 0, self.lines)
        self.assertEqual(Path(saved["path"]).read_bytes(), b"last piece")


if __name__ == "__main__":
    unittest.main()
