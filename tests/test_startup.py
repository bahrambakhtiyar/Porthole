"""Starting when the port is taken (since 23 Nov 2026).

Without --port: the same copy of Porthole on 8765 is opened as before; another copy (another install) is asked
to quit the way its own Quit button does and this one takes its port; anything else (not Porthole, no answer,
can't be stopped, still busy, not allowed) moves this one to the next free port. With --port the clear error
stays, and its hint names the command for how Porthole was started. A server whose accept() keeps failing stops
instead of spinning.

No test here binds or talks to ports 8765-8790: those numbers appear only with a fake binder and fake pings, and
the tests with real sockets use ports the system picks (port 0)."""

import contextlib
import errno
import io
import json
import os
import socket
import tempfile
import threading
import types
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from porthole import app, sandbox, webserver
from porthole.hub import Hub
from tests.test_webserver import WEB

OTHER = "/opt/porthole"  # another copy's folder, as its /api/ping says


class FakeBound:
    """What main() gets from a fake binder: a server on `port` that returns from serve_forever at once."""

    def __init__(self, port, fail=None):
        self.server_address = ("0.0.0.0", port)
        self.fail = fail  # an exception serve_forever raises, as a real server does when accept() keeps failing
        self.events = []

    def serve_forever(self, *args, **kwargs):
        self.events.append("served")
        if self.fail:
            raise self.fail

    def shutdown(self):
        self.events.append("shutdown")

    def server_close(self):
        self.events.append("closed")


class BoundServer:
    """A real server main() really bound; serve_forever serves it in the background while `during` runs."""

    def __init__(self, real, during=None):
        self.real = real
        self.server_address = real.server_address
        self.during = during

    def serve_forever(self, *args, **kwargs):
        if self.during is None:
            return
        thread = threading.Thread(target=self.real.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            self.during(self.real.server_address[1])
        finally:
            self.real.shutdown()
            thread.join(5)

    def shutdown(self):
        pass

    def server_close(self):
        self.real.server_close()


class StartupMixin:
    """Runs app.main with the page opener, dialogs and network addresses replaced, and records the output."""

    def run_main(self, *args, binder, platform="linux", env=None, redirect=None, dialog=None, opener=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.opened = []
        self.dialogs = []
        self.discoveries = []
        self.command_seen = []  # PORTHOLE_COMMAND as a browser Porthole starts would see it
        real_discovery = app.Discovery

        def spy_open(url, browser_tab=False, **kwargs):
            self.opened.append(url)
            self.command_seen.append(os.environ.get(app.COMMAND_VARIABLE))
            return "tab"

        def spy_discovery(*a, **kwargs):
            discovery = real_discovery(*a, **kwargs)
            self.discoveries.append(discovery)
            return discovery

        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(app, "make_server", binder))
            stack.enter_context(mock.patch.object(app, "open_sharer_page", opener or spy_open))
            record = lambda message, info=False: self.dialogs.append((message, info))  # noqa: E731
            stack.enter_context(mock.patch.object(app, "show_dialog", dialog or record))
            stack.enter_context(mock.patch.object(app, "lan_addresses", lambda: ["192.168.1.20"]))
            stack.enter_context(mock.patch.object(app, "Discovery", spy_discovery))
            stack.enter_context(mock.patch.object(app.sys, "platform", platform))
            stack.enter_context(mock.patch.dict(os.environ, env or {}))
            if redirect is not None:
                stack.enter_context(mock.patch.object(app, "make_redirect_server", redirect))
            stack.enter_context(contextlib.redirect_stdout(out))
            self.code = app.main([*args, "--record-dir", tmp.name])
        self.output = out.getvalue()
        return self.code


class FakeBinderMixin(StartupMixin):
    """No sockets at all: binding, pings and quit requests are all fakes, so 8765 is only a number here."""

    def setUp(self):
        self.busy = set()  # ports in use
        self.denied = set()  # ports that can't be used here (reserved, or need admin rights)
        self.portholes = {}  # port -> what a Porthole there says about itself
        self.quit_answer = True  # what a Porthole asked to quit says
        self.frees_port = True  # whether it then really lets go of its port
        self.any_port = 54321  # what port 0 gives
        self.serve_error = None
        self.tried, self.pings, self.quits, self.servers = [], [], [], []
        for name, value in (
            ("_running_porthole", self.fake_ping),
            ("_held", lambda port: port in self.busy),
            ("ask_to_quit", self.fake_quit),
            ("QUIT_WAIT_SECONDS", 0.3),
            ("DEFAULT_PORT", 8765),
            ("SPARE_PORTS", range(8766, 8786)),
        ):
            patcher = mock.patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_ping(self, port, *args, **kwargs):
        self.pings.append(port)
        return self.portholes.get(port)

    def fake_quit(self, port):
        self.quits.append(port)
        if self.quit_answer and self.frees_port:
            self.busy.discard(port)
            self.portholes.pop(port, None)
        return self.quit_answer

    def bind(self, hub, port, web_dir, links, **kwargs):
        self.tried.append(port)
        self.links = links
        if port in self.denied:
            raise OSError(errno.EACCES, "Permission denied")
        if port in self.busy:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        server = FakeBound(port or self.any_port, self.serve_error)
        self.servers.append(server)
        return server

    def start(self, *args, **kwargs):
        return self.run_main(*args, binder=self.bind, **kwargs)


class DefaultPortMovesTest(FakeBinderMixin, unittest.TestCase):
    """Without --port, a busy default port moves Porthole to the next free one, and everything uses that port."""

    def test_a_free_default_port_is_used_as_before(self):
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.tried, [8765])
        self.assertEqual(self.pings, [])
        self.assertNotIn("busy", self.output)
        self.assertIn("Sharer (open on this PC): http://127.0.0.1:8765/share", self.output)

    def test_tries_the_next_ports_in_turn(self):
        self.busy = {8765, 8766, 8767}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.tried, [8765, 8766, 8767, 8768])
        lines = self.output.splitlines()
        self.assertEqual(lines[0], "Port 8765 is busy, so Porthole uses port 8768.")
        self.assertEqual(lines[1], "Porthole is running.")

    def test_everything_uses_the_port_actually_used(self):
        self.busy = {8765}
        self.assertEqual(self.start(), 0)
        self.assertIn("Port 8765 is busy, so Porthole uses port 8766.", self.output)
        self.assertIn("Sharer (open on this PC): http://127.0.0.1:8766/share", self.output)
        self.assertIn("http://192.168.1.20:8766/v/", self.output)
        self.assertIn("sudo ufw allow 8766/tcp", self.output)
        self.assertNotIn(":8765/", self.output)
        self.assertEqual(self.opened, ["http://127.0.0.1:8766/share"])
        [url] = self.links()  # what the server builds the links and the QR code from
        self.assertTrue(url.startswith("http://192.168.1.20:8766/v/"), url)
        [discovery] = self.discoveries
        self.assertEqual(discovery.port, 8766)
        self.assertEqual(discovery.state()["url"], "http://porthole.local:8766")  # the easy address

    def test_the_port_80_helper_points_at_the_port_actually_used(self):
        self.busy = {8765}
        calls = []

        def fake_redirect(port, target, discovery):
            calls.append((port, target))
            return FakeBound(port)

        self.assertEqual(self.start(platform="win32", redirect=fake_redirect), 0)
        self.assertEqual(calls, [(80, 8766)])

    def test_after_the_spare_ports_any_free_port(self):
        self.busy = {8765, *range(8766, 8786)}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.tried, [8765, *range(8766, 8786), 0])
        self.assertIn("Port 8765 is busy, so Porthole uses port 54321.", self.output)
        self.assertIn("Sharer (open on this PC): http://127.0.0.1:54321/share", self.output)
        self.assertEqual(self.opened, ["http://127.0.0.1:54321/share"])
        self.assertTrue(self.links()[0].startswith("http://192.168.1.20:54321/v/"))
        self.assertEqual(self.discoveries[0].port, 54321)

    def test_no_port_at_all(self):
        self.busy = {8765, *range(8766, 8786), 0}
        self.assertEqual(self.start(), 1)
        self.assertEqual(self.tried, [8765, *range(8766, 8786), 0])
        self.assertIn("Couldn't start:", self.output)
        self.assertNotIn("Porthole is running.", self.output)

    def test_a_default_port_that_cant_be_used_here_moves_too(self):
        # Reserved, or needs admin rights (Windows' excluded port ranges). Asking costs nothing there: nothing
        # listens, so the ping is refused at once.
        self.denied = {8765}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.tried, [8765, 8766])
        self.assertEqual(self.pings, [8765])
        self.assertEqual(self.quits, [])
        self.assertIn("Port 8765 can't be used here, so Porthole uses port 8766.", self.output)
        self.assertNotIn("Try:", self.output)

    def test_windows_access_denied_counts_too(self):
        def bind(hub, port, *a, **k):
            self.tried.append(port)
            if port == 8765:
                raise OSError(10013, "An attempt was made to access a socket in a way forbidden")
            return FakeBound(port)

        self.assertEqual(self.run_main(binder=bind), 0)
        self.assertEqual(self.tried, [8765, 8766])

    def test_windows_in_use_counts_too(self):
        def bind(hub, port, *a, **k):
            self.tried.append(port)
            if port == 8765:
                raise OSError(10048, "Only one usage of each socket address is normally permitted")
            return FakeBound(port)

        self.assertEqual(self.run_main(binder=bind), 0)
        self.assertEqual(self.tried, [8765, 8766])
        self.assertIn("Port 8765 is busy, so Porthole uses port 8766.", self.output)

    def test_on_windows_a_porthole_holding_the_port_for_itself_is_still_found(self):
        # Porthole binds with SO_EXCLUSIVEADDRUSE on Windows, and a second bind may then be refused as
        # "forbidden" (WSAEACCES) rather than "in use": a second click must still open the running copy.
        self.denied = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": str(app.APP_FOLDER)}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.servers, [])
        self.assertIn("Already running. Opened the sharer page.", self.output)

    def test_on_windows_another_copy_holding_the_port_for_itself_is_stopped(self):
        self.denied = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}

        def fake_quit(port):
            self.quits.append(port)
            self.denied.discard(port)
            return True

        with mock.patch.object(app, "ask_to_quit", fake_quit):
            self.assertEqual(self.start(), 0)
        self.assertEqual(self.quits, [8765])
        self.assertEqual(self.tried[-1], 8765)
        self.assertIn(f"Stopped the Porthole from {OTHER} and started this one.", self.output)

    def test_spare_ports_that_cant_be_used_are_skipped(self):
        # Without asking: Windows can reserve whole ranges, and there a refused connection takes a second or two.
        self.busy = {8765}
        self.denied = set(range(8766, 8776))
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.tried, [8765, *range(8766, 8777)])
        self.assertEqual(self.pings, [8765])

    def test_something_else_on_the_port_isnt_asked_anything(self):
        self.busy = {8765}  # and no Porthole answers there
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.pings, [8765])
        self.assertEqual(self.quits, [])

    def test_other_problems_at_the_default_port_stop_with_the_error(self):
        def bind(hub, port, *a, **k):
            self.tried.append(port)
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")

        self.assertEqual(self.run_main(binder=bind), 1)
        self.assertEqual(self.tried, [8765])
        self.assertIn("Couldn't start: [Errno", self.output)

    def test_the_same_copy_on_a_spare_port_is_opened_not_started_again(self):
        # 8765 is held by something else, so this copy runs on 8766: a second click on the menu entry opens it.
        self.busy = {8765, 8766}
        self.portholes = {8766: {"app": "porthole", "folder": str(app.APP_FOLDER)}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.tried, [8765, 8766])
        self.assertEqual(self.servers, [])
        self.assertEqual(self.opened, ["http://127.0.0.1:8766/share"])
        self.assertIn("Already running. Opened the sharer page.", self.output)
        self.assertEqual(self.quits, [])

    def test_the_same_copy_on_a_spare_port_is_opened_once_the_default_port_is_free_again(self):
        # This copy moved to 8766 while something held 8765, and that has gone since: starting it again opens the
        # running one instead of a second copy (a second room, a second easy address) on 8765.
        self.busy = {8766}
        self.portholes = {8766: {"app": "porthole", "folder": str(app.APP_FOLDER)}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.opened, ["http://127.0.0.1:8766/share"])
        self.assertIn("Already running. Opened the sharer page.", self.output)
        [server] = self.servers  # 8765 was bound, then let go without serving
        self.assertEqual(server.events, ["closed"])
        self.assertEqual(self.quits, [])

    def test_with_the_default_port_free_only_spare_ports_in_use_are_asked(self):
        self.busy = {8766, 8770}
        self.portholes = {8766: {"app": "porthole", "folder": OTHER}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.pings, [8766, 8770])
        self.assertEqual(self.quits, [])  # another copy on a spare port is left alone
        self.assertEqual(self.tried, [8765])
        self.assertEqual(self.servers[0].events[0], "served")
        self.assertNotIn("busy", self.output)
        self.assertIn("Sharer (open on this PC): http://127.0.0.1:8765/share", self.output)

    def test_an_explicit_port_doesnt_look_at_the_spare_ports(self):
        self.busy = {8766}
        self.portholes = {8766: {"app": "porthole", "folder": str(app.APP_FOLDER)}}
        self.assertEqual(self.start("--port", "8765"), 0)
        self.assertEqual(self.pings, [])
        self.assertIn("Sharer (open on this PC): http://127.0.0.1:8765/share", self.output)

    def test_another_copy_on_a_spare_port_is_left_alone(self):
        self.busy = {8765, 8766}
        self.portholes = {8766: {"app": "porthole", "folder": OTHER}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.quits, [])
        self.assertEqual(self.tried, [8765, 8766, 8767])
        self.assertIn("Port 8765 is busy, so Porthole uses port 8767.", self.output)


class DefaultPortTakeoverTest(FakeBinderMixin, unittest.TestCase):
    """Without --port, a Porthole on the default port: the same copy is opened, another copy is stopped."""

    def test_the_same_copy_is_opened_and_this_one_ends(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": str(app.APP_FOLDER)}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.quits, [])  # a second click on the menu entry never ends a running share
        self.assertEqual(self.servers, [])
        self.assertEqual(self.opened, ["http://127.0.0.1:8765/share"])
        self.assertIn("Already running. Opened the sharer page.", self.output)

    def test_it_waits_to_see_the_window_start_before_it_ends(self):
        # A browser that can't start falls back to a tab from the window's own thread, which would die with this
        # copy if it ended at once: every later click on the menu entry would then open nothing.
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": str(app.APP_FOLDER)}}
        settled = []

        def opener(url, browser_tab=False, on_window=None, **kwargs):
            self.opened.append(url)
            on_window(types.SimpleNamespace(settle=lambda: settled.append(url)))
            return "window"

        self.assertEqual(self.start(opener=opener), 0)
        self.assertEqual(settled, ["http://127.0.0.1:8765/share"])
        self.assertIn("Already running. Opened the sharer page.", self.output)

    def test_ctrl_c_while_it_asks_a_program_on_the_port_ends_quietly(self):
        # Start-up can wait up to 2 s for a ping and 5 s for another copy to quit: Ctrl+C then prints no traceback.
        def impatient(*args, **kwargs):
            raise KeyboardInterrupt

        with mock.patch.object(app, "start_server", impatient):
            self.assertEqual(self.start(), 0)
        self.assertIn("\nStopped.", self.output)  # on a line of its own, below the terminal's ^C
        self.assertEqual(self.servers, [])

    def test_a_server_thread_interrupted_before_it_starts_is_not_waited_for(self):
        # Ctrl+C right after "Press Ctrl+C": if the server's thread never started, shutdown() would wait for it for
        # ever, with every later Ctrl+C ignored. Only a started thread counts as serving.
        class NeverStarts(threading.Thread):
            def start(self):
                raise KeyboardInterrupt

        serving = threading.Event()
        with mock.patch.object(app.threading, "Thread", NeverStarts), self.assertRaises(KeyboardInterrupt):
            app._serve(types.SimpleNamespace(serve_forever=lambda: None), serving)
        self.assertFalse(serving.is_set())

    def test_with_no_browser_it_says_where_the_running_one_is(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": str(app.APP_FOLDER)}}
        self.assertEqual(self.start("--no-browser"), 0)
        self.assertEqual(self.opened, [])
        self.assertIn("Already running: http://127.0.0.1:8765/share", self.output)
        self.assertNotIn("Opened", self.output)

    def test_an_older_copy_that_doesnt_say_its_folder_counts_as_the_same(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole"}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.quits, [])
        self.assertIn("Already running.", self.output)

    def test_another_copy_is_stopped_and_this_one_takes_its_port(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.quits, [8765])
        self.assertEqual(self.tried[-1], 8765)
        self.assertNotIn(8766, self.tried)
        lines = self.output.splitlines()
        self.assertEqual(lines[0], f"Stopped the Porthole from {OTHER} and started this one.")
        self.assertIn("Sharer (open on this PC): http://127.0.0.1:8765/share", self.output)
        self.assertEqual(self.opened, ["http://127.0.0.1:8765/share"])
        self.assertEqual(self.dialogs, [])  # a terminal: no pop-up

    def test_it_waits_for_the_stopped_copy_to_let_go_of_the_port(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        self.frees_port = False
        attempts = []

        def bind(hub, port, *a, **k):
            if port == 8765:
                attempts.append(time.monotonic())
                if len(attempts) < 4:  # the stopped copy still closing
                    raise OSError(errno.EADDRINUSE, "Address already in use")
            return FakeBound(port)

        with mock.patch.object(app, "QUIT_WAIT_SECONDS", 5):
            self.assertEqual(self.run_main(binder=bind), 0)
        self.assertEqual(len(attempts), 4)
        gaps = [later - earlier for earlier, later in zip(attempts[1:], attempts[2:])]
        self.assertTrue(all(gap >= 0.05 for gap in gaps), gaps)  # it waits between tries, never spins
        self.assertIn(f"Stopped the Porthole from {OTHER} and started this one.", self.output)

    def test_another_start_of_this_copy_that_takes_the_port_first_is_opened(self):
        # A double click while another install runs: both starts ask it to quit, and whichever binds its port
        # first wins. The other opens that one rather than a second copy of this install on a spare port.
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}

        def quit_and_lose_the_race(port):
            self.quits.append(port)
            self.portholes[port] = {"app": "porthole", "folder": str(app.APP_FOLDER)}  # the other start's
            return True

        began = time.monotonic()
        with mock.patch.object(app, "ask_to_quit", quit_and_lose_the_race), mock.patch.object(app, "QUIT_WAIT_SECONDS", 5):
            self.assertEqual(self.start("--launcher"), 0)
        self.assertLess(time.monotonic() - began, 3)  # it didn't wait for the port to free
        self.assertEqual(self.servers, [])
        self.assertNotIn(8766, self.tried)
        self.assertEqual(self.opened, ["http://127.0.0.1:8765/share"])
        self.assertIn("Already running. Opened the sharer page.", self.output)
        self.assertNotIn("Stopped", self.output)
        time.sleep(0.1)  # a notice would show from its own thread
        self.assertFalse(any("Stopped" in message for message, _ in self.dialogs), self.dialogs)

    def test_from_a_launcher_the_notice_shows_on_screen_without_holding_up_the_start(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        shown = threading.Event()
        release = threading.Event()

        def slow_dialog(message, info=False):  # a message box waits for OK
            self.dialogs.append((message, info))
            shown.set()
            release.wait(10)

        began = time.monotonic()
        try:
            self.assertEqual(self.start("--launcher", dialog=slow_dialog), 0)
            self.assertLess(time.monotonic() - began, 5)  # it served and ended without waiting for OK
            self.assertEqual(self.servers[0].events[0], "served")
            self.assertTrue(shown.wait(5))
        finally:
            release.set()
        self.assertEqual(self.dialogs, [(f"Stopped the Porthole from {OTHER} and started this one.", True)])

    def test_from_a_launcher_a_moved_port_is_only_printed(self):
        self.busy = {8765}
        self.assertEqual(self.start("--launcher"), 0)
        self.assertIn("Port 8765 is busy, so Porthole uses port 8766.", self.output)
        self.assertEqual(self.dialogs, [])

    def test_a_copy_that_refuses_to_quit_is_left_and_this_one_moves(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        self.quit_answer = False
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.quits, [8765])
        self.assertEqual(self.tried, [8765, 8766])
        self.assertIn(f"Couldn't stop the Porthole from {OTHER}, so this one uses port 8766.", self.output)
        self.assertNotIn("Stopped the Porthole", self.output)
        self.assertEqual(self.opened, ["http://127.0.0.1:8766/share"])

    def test_a_port_that_never_frees_moves_this_one_after_the_wait(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        self.frees_port = False  # says yes, but its port stays taken
        began = time.monotonic()
        self.assertEqual(self.start(), 0)
        self.assertGreaterEqual(time.monotonic() - began, 0.3)  # QUIT_WAIT_SECONDS in these tests
        self.assertEqual(self.quits, [8765])
        self.assertGreater(self.tried.count(8765), 1)  # tried again while waiting
        self.assertEqual(self.tried[-1], 8766)
        self.assertIn(f"Stopped the Porthole from {OTHER}, but port 8765 is still busy, so this one uses port 8766.", self.output)

    def test_from_a_launcher_a_stopped_copy_shows_on_screen_even_when_this_one_moves(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        self.frees_port = False
        self.assertEqual(self.start("--launcher"), 0)
        for _ in range(50):  # shown from its own thread
            if self.dialogs:
                break
            time.sleep(0.02)
        self.assertEqual(
            self.dialogs, [(f"Stopped the Porthole from {OTHER}, but port 8765 is still busy, so this one uses port 8766.", True)]
        )

    def test_from_a_launcher_a_copy_that_refuses_is_only_printed(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        self.quit_answer = False
        self.assertEqual(self.start("--launcher"), 0)
        self.assertIn(f"Couldn't stop the Porthole from {OTHER}, so this one uses port 8766.", self.output)
        self.assertEqual(self.dialogs, [])

    def test_a_porthole_on_an_explicit_port_is_never_stopped(self):
        self.busy = {8765}
        self.portholes = {8765: {"app": "porthole", "folder": OTHER}}
        self.assertEqual(self.start("--port", "8765"), 0)
        self.assertEqual(self.quits, [])
        self.assertEqual(self.tried, [8765])
        self.assertIn(f"Porthole is already running from {OTHER}. Opened it.", self.output)
        self.assertEqual(self.opened, ["http://127.0.0.1:8765/share"])


class ExplicitPortTest(FakeBinderMixin, unittest.TestCase):
    """--port: a busy or forbidden port keeps its clear error and Porthole never moves to another one."""

    def test_a_busy_port_is_an_error_and_nothing_else_is_tried(self):
        self.busy = {8765}
        with mock.patch.object(app.os, "name", "posix"):
            self.assertEqual(self.start("--port", "8765"), 1)
        self.assertEqual(self.tried, [8765])
        self.assertIn("Port 8765 is already in use. Try: python3 start.py --port 8766", self.output)
        self.assertEqual(self.quits, [])

    def test_another_busy_port(self):
        self.busy = {9000}
        with mock.patch.object(app.os, "name", "posix"):
            self.assertEqual(self.start("--port", "9000"), 1)
        self.assertEqual(self.tried, [9000])
        self.assertIn("Port 9000 is already in use. Try: python3 start.py --port 9001", self.output)

    def test_a_port_that_cant_be_used_here(self):
        self.denied = {80}
        with mock.patch.object(app.os, "name", "posix"):
            self.assertEqual(self.start("--port", "80"), 1)
        self.assertEqual(self.tried, [80])
        self.assertIn(
            "Port 80 can't be used here (reserved or needs admin rights). Try: python3 start.py --port 81", self.output
        )

    def test_the_last_port_suggests_the_one_below(self):
        self.busy = {65535}
        with mock.patch.object(app.os, "name", "posix"):
            self.assertEqual(self.start("--port", "65535"), 1)
        self.assertIn("Try: python3 start.py --port 65534", self.output)


class RunHintTest(FakeBinderMixin, unittest.TestCase):
    """The command in "Try: <command> --port 8766" is the one that starts Porthole the way it was started."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("APPIMAGE", "APPDIR", app.COMMAND_VARIABLE):
            os.environ.pop(name, None)

    def test_from_source(self):
        with mock.patch.object(app.os, "name", "posix"):
            self.assertEqual(app.run_hint(), "python3 start.py")
        with mock.patch.object(app.os, "name", "nt"):
            self.assertEqual(app.run_hint(), "py start.py")

    def test_the_deb_and_the_snap(self):
        # Their start scripts set PORTHOLE_COMMAND=porthole: both are started with `porthole`.
        self.assertEqual(app.run_hint("porthole"), "porthole")

    def appimage(self, path):
        mount = "/tmp/.mount_PorthoAb12"
        return (
            mock.patch.dict(os.environ, {"APPIMAGE": path, "APPDIR": mount}),
            mock.patch.object(sandbox, "APP_ROOT", Path(mount)),
        )

    def test_an_appimage_names_its_own_file(self):
        first, second = self.appimage("/home/ayla/Apps/Porthole-x86_64.AppImage")
        with first, second:
            self.assertEqual(app.run_hint(), "/home/ayla/Apps/Porthole-x86_64.AppImage")
            self.assertEqual(app.run_hint("porthole"), "/home/ayla/Apps/Porthole-x86_64.AppImage")

    def test_an_appimage_path_with_a_space_is_quoted(self):
        first, second = self.appimage("/home/ayla/My Apps/Porthole-x86_64.AppImage")
        with first, second:
            self.assertEqual(app.run_hint(), "'/home/ayla/My Apps/Porthole-x86_64.AppImage'")

    def test_appimage_variables_inherited_from_another_app_are_ignored(self):
        # A source copy started from a terminal inside another AppImage inherits its $APPIMAGE and $APPDIR.
        with mock.patch.dict(os.environ, {"APPIMAGE": "/opt/Editor.AppImage", "APPDIR": "/tmp/.mount_EditorXy12"}):
            with mock.patch.object(app.os, "name", "posix"):
                self.assertEqual(app.run_hint(), "python3 start.py")

    def test_the_deb_says_porthole_when_its_port_is_busy(self):
        self.busy = {9000}
        self.assertEqual(self.start("--port", "9000", env={app.COMMAND_VARIABLE: "porthole"}), 1)
        self.assertIn("Port 9000 is already in use. Try: porthole --port 9001", self.output)

    def test_the_appimage_names_its_file_when_its_port_is_busy(self):
        self.busy = {9000}
        first, second = self.appimage("/home/ayla/Apps/Porthole-x86_64.AppImage")
        with first, second:
            self.assertEqual(self.start("--port", "9000"), 1)
        self.assertIn("Port 9000 is already in use. Try: /home/ayla/Apps/Porthole-x86_64.AppImage --port 9001", self.output)

    def test_programs_porthole_starts_dont_inherit_the_command(self):
        # The browser or file manager Porthole opens must not pass it on, say to a terminal opened from there,
        # where a copy run from source would then name the wrong command.
        self.assertEqual(self.start(env={app.COMMAND_VARIABLE: "porthole"}), 0)
        self.assertEqual(self.command_seen, [None])


class AcceptStopTest(FakeBinderMixin, unittest.TestCase):
    """A server whose accept() keeps failing (its snap removed while it runs) stops and frees its port."""

    def test_stops_with_a_plain_line_and_frees_the_port(self):
        self.serve_error = webserver.AcceptFailed("Permission denied")
        self.assertEqual(self.start(), 1)
        [server] = self.servers
        self.assertEqual(server.events[0], "served")
        self.assertIn("closed", server.events)  # the port is free again
        self.assertIn("Porthole can't accept connections any more, so it stopped.", self.output)
        self.assertTrue(self.output.rstrip().endswith("Porthole can't accept connections any more, so it stopped."))
        self.assertEqual(self.dialogs, [])

    def test_viewers_are_told_it_ended(self):
        self.serve_error = webserver.AcceptFailed("Permission denied")
        ended = []
        real_end = app.Hub.end
        with mock.patch.object(app.Hub, "end", lambda hub: (ended.append(True), real_end(hub))):
            self.assertEqual(self.start(), 1)
        self.assertEqual(ended, [True])

    def test_from_a_launcher_it_shows_on_screen_after_the_port_is_free(self):
        self.serve_error = webserver.AcceptFailed("Permission denied")
        order = []

        def bind(*args, **kwargs):
            server = self.bind(*args, **kwargs)
            server.server_close = lambda: order.append("closed")
            return server

        self.assertEqual(self.run_main("--launcher", binder=bind, dialog=lambda message, info=False: order.append(message)), 1)
        self.assertEqual(order, ["closed", "Porthole can't accept connections any more, so it stopped."])


# --- with real sockets on ports the system picks ---


class PingHandler(BaseHTTPRequestHandler):
    """A stand-in Porthole: answers /api/ping for `folder` and /api/quit with `quit_status`."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/api/ping":
            return self.send_error(404)
        body = json.dumps({"app": "porthole", "folder": self.server.folder}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.quit_requests.append((self.client_address[0], dict(self.headers)))
        if self.server.quit_status is None:  # never answers: a Porthole whose quit is stuck
            self.server.release.wait(10)
            return
        self.send_response(self.server.quit_status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class RealSocketsTest(StartupMixin, unittest.TestCase):
    def setUp(self):
        self.tried = []
        for name, value in (("SPARE_PORTS", ()), ("PING_SECONDS", 0.5), ("QUIT_WAIT_SECONDS", 0.5)):
            patcher = mock.patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def real_binder(self, during=None):
        def bind(hub, port, *a, **k):
            self.tried.append(port)
            server = webserver.make_server(hub, port, *a, **k)
            self.addCleanup(server.server_close)
            return BoundServer(server, during)

        return bind

    def serve(self, server):
        """Serve in the background, then close the port, as Porthole does when it stops."""
        done = threading.Event()

        def run():
            server.serve_forever(poll_interval=0.05)
            server.server_close()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()) if not done.is_set() else None)
        return done

    def other_porthole(self, **kwargs):
        server = webserver.make_server(Hub(), 0, WEB, [], **kwargs)
        return server, server.server_address[1], self.serve(server)

    def stand_in(self, quit_status):
        # All addresses, as the real server binds: on a Mac a bind to all addresses would succeed next to one on
        # 127.0.0.1 alone, and the port wouldn't count as busy.
        server = webserver.LanServer(("0.0.0.0", 0), PingHandler)
        server.folder, server.quit_status, server.quit_requests = OTHER, quit_status, []
        server.release = threading.Event()
        self.addCleanup(server.release.set)
        self.serve(server)
        return server, server.server_address[1]

    def ping(self, port):
        with mock.patch.object(app, "PING_SECONDS", 2):
            return app._running_porthole(port)

    def test_the_same_copy_is_opened_and_keeps_running(self):
        other, port, _ = self.other_porthole()
        with mock.patch.object(app, "DEFAULT_PORT", port):
            self.assertEqual(self.run_main(binder=self.real_binder()), 0)
        self.assertIn("Already running. Opened the sharer page.", self.output)
        self.assertEqual(self.opened, [f"http://127.0.0.1:{port}/share"])
        self.assertIsNotNone(self.ping(port))

    def test_another_copy_is_really_stopped_and_its_port_taken(self):
        other, port, done = self.other_porthole()
        with mock.patch.object(webserver, "APP_FOLDER", OTHER), mock.patch.object(app, "QUIT_WAIT_SECONDS", 5):
            with mock.patch.object(app, "DEFAULT_PORT", port):
                self.assertEqual(self.run_main(binder=self.real_binder()), 0)
        self.assertTrue(done.wait(5))  # the other copy stopped
        if os.name == "nt" and self.tried[-1] == 0:
            # Windows may keep a port bound with SO_EXCLUSIVEADDRUSE until its last connections are gone.
            self.assertIn(f"Stopped the Porthole from {OTHER}, but port {port} is still busy", self.output)
            return
        self.assertEqual(self.tried[-1], port)
        self.assertNotIn(0, self.tried)
        self.assertIn(f"Stopped the Porthole from {OTHER} and started this one.", self.output)
        self.assertEqual(self.opened, [f"http://127.0.0.1:{port}/share"])

    def test_the_quit_request_is_one_only_a_program_on_this_pc_can_send(self):
        stand_in, port = self.stand_in(204)
        self.assertTrue(app.ask_to_quit(port))
        [(client, headers)] = stand_in.quit_requests
        self.assertEqual(client, "127.0.0.1")
        self.assertEqual(headers.get("Origin"), f"http://127.0.0.1:{port}")  # its own page's origin
        self.assertEqual(headers.get("Host"), f"127.0.0.1:{port}")
        self.assertNotIn("Sec-Fetch-Site", headers)

    def test_web_pages_and_other_devices_still_cant_stop_porthole(self):
        # The guard on /api/quit is unchanged: a request from another device is refused, and so is one that a
        # web page from another site makes (the browser sends that site as its Origin).
        server, port, done = self.other_porthole(is_local=lambda ip: False)
        self.assertFalse(app.ask_to_quit(port))
        server2, port2, done2 = self.other_porthole()
        for headers in ({"Origin": "http://evil.example"}, {"Origin": f"http://127.0.0.1:{port2}", "Sec-Fetch-Site": "cross-site"}, {}):
            request = urllib.request.Request(f"http://127.0.0.1:{port2}/api/quit", data=b"", method="POST", headers=headers)
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)
            ctx.exception.close()
            self.assertEqual(ctx.exception.code, 403)
        self.assertFalse(done.is_set())
        self.assertFalse(done2.is_set())
        self.assertIsNotNone(self.ping(port2))

    def test_a_copy_that_refuses_to_quit_keeps_running_and_this_one_moves(self):
        stand_in, port = self.stand_in(403)
        with mock.patch.object(app, "DEFAULT_PORT", port):
            self.assertEqual(self.run_main(binder=self.real_binder()), 0)
        self.assertEqual(len(stand_in.quit_requests), 1)
        self.assertEqual(self.tried[0], port)
        self.assertEqual(self.tried[-1], 0)
        moved = self.opened[0].split(":")[2].split("/")[0]
        self.assertNotEqual(int(moved), port)
        self.assertIn(f"Couldn't stop the Porthole from {OTHER}, so this one uses port {moved}.", self.output)
        self.assertIsNotNone(self.ping(port))

    def test_a_copy_that_never_answers_the_quit_request_is_left_and_this_one_moves(self):
        stand_in, port = self.stand_in(None)
        began = time.monotonic()
        with mock.patch.object(app, "DEFAULT_PORT", port):
            self.assertEqual(self.run_main(binder=self.real_binder()), 0)
        self.assertLess(time.monotonic() - began, 3)  # gave up after the ping's wait (0.5 s here)
        self.assertEqual(len(stand_in.quit_requests), 1)
        self.assertEqual(self.tried[-1], 0)
        self.assertIn(f"Couldn't stop the Porthole from {OTHER}, so this one uses port", self.output)

    def test_the_same_copy_on_a_spare_port_is_found_with_the_default_port_free(self):
        moved, spare, _ = self.other_porthole()  # this copy, moved there by an earlier start
        with socket.socket() as probe:  # a port nothing listens on
            probe.bind(("0.0.0.0", 0))
            free = probe.getsockname()[1]
        with mock.patch.object(app, "DEFAULT_PORT", free), mock.patch.object(app, "SPARE_PORTS", (spare,)):
            self.assertEqual(self.run_main(binder=self.real_binder()), 0)
        self.assertIn("Already running. Opened the sharer page.", self.output)
        self.assertEqual(self.opened, [f"http://127.0.0.1:{spare}/share"])
        self.assertEqual(self.tried, [free])
        with socket.socket() as again:  # and the default port was let go
            again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            again.bind(("0.0.0.0", free))

    def test_ctrl_c_before_it_serves_still_ends(self):
        # Ctrl+C just before serving (the page opens on a thread of its own, so the last step on the main thread is
        # the port 80 helper): the server never served, and shutdown() would wait for it for ever.
        def bind(hub, port, *a, **k):
            server = webserver.make_server(hub, port, *a, **k)
            self.addCleanup(server.server_close)
            return server

        def interrupted(*a, **k):
            raise KeyboardInterrupt

        with socket.socket() as probe:
            probe.bind(("0.0.0.0", 0))
            port = probe.getsockname()[1]
        codes = []
        with mock.patch.object(app, "DEFAULT_PORT", port), mock.patch.object(app, "start_port_80_helper", interrupted):
            thread = threading.Thread(target=lambda: codes.append(self.run_main(binder=bind)), daemon=True)
            thread.start()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(codes, [0])
        self.assertIn("Stopped.", self.output)

    def test_held_tells_a_port_something_listens_on(self):
        with socket.socket() as listener:
            listener.bind(("0.0.0.0", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(app._held(port))
        self.assertFalse(app._held(port))

    def test_a_port_that_never_frees(self):
        stand_in, port = self.stand_in(204)  # says yes, but keeps running
        with mock.patch.object(app, "DEFAULT_PORT", port):
            self.assertEqual(self.run_main(binder=self.real_binder()), 0)
        self.assertEqual(self.tried[-1], 0)
        self.assertGreater(self.tried.count(port), 1)
        self.assertIn(f"Stopped the Porthole from {OTHER}, but port {port} is still busy, so this one uses port", self.output)

    def test_a_listener_that_never_answers_moves_this_one_and_the_page_uses_the_new_port(self):
        info = {}

        def during(port):
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with direct.open(f"http://127.0.0.1:{port}/api/info", timeout=5) as resp:
                info.update(json.loads(resp.read()))
            info["port"] = port

        with socket.socket() as held:
            held.bind(("0.0.0.0", 0))
            held.listen()
            port = held.getsockname()[1]
            began = time.monotonic()
            with mock.patch.object(app, "DEFAULT_PORT", port):
                self.assertEqual(self.run_main(binder=self.real_binder(during)), 0)
        self.assertLess(time.monotonic() - began, 5)  # gave up on the silent listener after the ping's wait
        moved = info["port"]
        self.assertNotEqual(moved, port)
        self.assertEqual(self.tried, [port, 0])
        self.assertIn(f"Port {port} is busy, so Porthole uses port {moved}.", self.output)
        self.assertEqual(self.opened, [f"http://127.0.0.1:{moved}/share"])
        self.assertTrue(info["viewer_urls"] and all(f":{moved}/v/" in url for url in info["viewer_urls"]), info)
        self.assertTrue(info["qr_svg"].startswith("<svg"))
        self.assertEqual(info["easy"]["url"], f"http://porthole.local:{moved}")
        self.assertIn(f"sudo ufw allow {moved}/tcp", self.output)

    def test_a_web_server_that_isnt_porthole(self):
        class NotPorthole(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_error(404)

        other = webserver.LanServer(("0.0.0.0", 0), NotPorthole)
        self.serve(other)
        port = other.server_address[1]
        with mock.patch.object(app, "DEFAULT_PORT", port):
            self.assertEqual(self.run_main(binder=self.real_binder()), 0)
        self.assertEqual(self.tried, [port, 0])
        self.assertIn(f"Port {port} is busy, so Porthole uses port", self.output)

    def test_an_explicit_busy_port_stays_an_error(self):
        with socket.socket() as held:
            held.bind(("0.0.0.0", 0))
            held.listen()
            port = held.getsockname()[1]
            self.assertEqual(self.run_main("--port", str(port), binder=self.real_binder()), 1)
        self.assertEqual(self.tried, [port])
        self.assertIn(f"Port {port} is already in use. Try: ", self.output)


if __name__ == "__main__":
    unittest.main()
