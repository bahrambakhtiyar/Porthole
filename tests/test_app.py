"""app.py: the address watcher, the "already running" check, and running frozen or without a console."""

import contextlib
import errno
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from porthole import app, launcher
from porthole import webserver
from porthole.hub import Hub
from porthole.webserver import make_server
from tests.test_appwindow import FakeBrowser
from tests.test_paths import FrozenMixin, MacBundleMixin
from tests.test_webserver import WEB, AppRunMixin


class SpyDiscovery:
    def __init__(self):
        self.moves = []

    def set_address(self, ip):
        self.moves.append(ip)


class AddressesTest(unittest.TestCase):
    def test_holder_keeps_a_copy(self):
        given = ["192.168.1.20"]
        addresses = app.Addresses(given)
        given.append("10.0.0.2")
        self.assertEqual(addresses.get(), ["192.168.1.20"])
        addresses.get().append("1.2.3.4")
        self.assertEqual(addresses.get(), ["192.168.1.20"])

    def test_refresh_moves_the_easy_address_on_a_change(self):
        addresses = app.Addresses(["192.168.1.20"])
        discovery = SpyDiscovery()
        self.assertFalse(app.refresh_addresses(addresses, discovery, find=lambda: ["192.168.1.20"]))
        self.assertEqual(discovery.moves, [])
        self.assertTrue(app.refresh_addresses(addresses, discovery, find=lambda: ["192.168.1.33", "10.0.0.2"]))
        self.assertEqual(addresses.get(), ["192.168.1.33", "10.0.0.2"])
        self.assertEqual(discovery.moves, ["192.168.1.33"])
        self.assertTrue(app.refresh_addresses(addresses, discovery, find=lambda: ["10.0.0.2"]))
        self.assertEqual(discovery.moves, ["192.168.1.33", "10.0.0.2"])

    def test_only_a_new_first_address_moves_the_name(self):
        addresses = app.Addresses(["192.168.1.20"])
        discovery = SpyDiscovery()
        self.assertTrue(app.refresh_addresses(addresses, discovery, find=lambda: ["192.168.1.20", "10.0.0.2"]))
        self.assertEqual(discovery.moves, [])  # the name already points at the first address
        self.assertEqual(addresses.get(), ["192.168.1.20", "10.0.0.2"])

    def test_losing_the_network_withdraws_the_name(self):
        addresses = app.Addresses(["192.168.1.20"])
        discovery = SpyDiscovery()
        self.assertTrue(app.refresh_addresses(addresses, discovery, find=lambda: []))
        self.assertEqual(addresses.get(), [])
        self.assertEqual(discovery.moves, [None])

    def test_a_network_that_comes_later_becomes_the_link_and_the_easy_address(self):
        # Started with no network: the address that shows up later is the link (no restart needed), and the easy
        # address, if wanted, is announced on it.
        addresses = app.Addresses([])
        discovery = SpyDiscovery()
        self.assertFalse(app.refresh_addresses(addresses, discovery, find=lambda: []))
        self.assertTrue(app.refresh_addresses(addresses, discovery, find=lambda: ["192.168.1.20"]))
        self.assertEqual(addresses.get(), ["192.168.1.20"])
        self.assertEqual(discovery.moves, ["192.168.1.20"])
        self.assertEqual(app.viewer_urls(addresses.get(), 8765, "room1")[0], "http://192.168.1.20:8765/v/room1")

    def test_refresh_never_raises(self):
        addresses = app.Addresses(["192.168.1.20"])

        def broken():
            raise OSError("no network stack")

        class BrokenDiscovery:
            def set_address(self, ip):
                raise RuntimeError("boom")

        self.assertFalse(app.refresh_addresses(addresses, SpyDiscovery(), find=broken))
        self.assertEqual(addresses.get(), ["192.168.1.20"])
        self.assertTrue(app.refresh_addresses(addresses, BrokenDiscovery(), find=lambda: ["10.0.0.2"]))
        self.assertEqual(addresses.get(), ["10.0.0.2"])

    def test_watcher_checks_every_interval_until_stopped(self):
        addresses = app.Addresses(["192.168.1.20"])
        discovery = SpyDiscovery()
        found = iter([["192.168.1.20"], ["10.0.0.2"], ["10.0.0.2"]])
        changes = []
        done = threading.Event()

        def find():
            try:
                return next(found)
            except StopIteration:
                done.set()
                return ["10.0.0.2"]

        stop = app.start_address_watcher(addresses, discovery, interval=0.01, find=find, on_change=changes.append)
        self.assertTrue(done.wait(3))
        stop.set()
        self.assertEqual(changes, [["10.0.0.2"]])
        self.assertEqual(discovery.moves, ["10.0.0.2"])
        self.assertEqual(app.ADDRESS_SECONDS, 10)


class AppWatcherTest(AppRunMixin, unittest.TestCase):
    """How app.main wires the address watcher."""

    def run_watched(self, args=()):
        self.watchers = []

        def fake_start(addresses, discovery, **kwargs):
            self.watchers.append((addresses, discovery, kwargs))
            return threading.Event()

        with mock.patch.object(app, "start_address_watcher", fake_start):
            return self.run_main(args)

    def test_watcher_runs_and_the_server_reads_the_current_links(self):
        self.assertEqual(self.run_watched(), 0)
        self.assertEqual(len(self.watchers), 1)
        addresses, discovery, _ = self.watchers[0]
        self.assertIs(discovery, self.discoveries[0])
        links = self.server_args[3]
        self.assertTrue(callable(links))
        self.assertEqual(links(), [f"http://192.168.1.20:8765/v/{self.hub_room()}", f"http://10.0.0.2:8765/v/{self.hub_room()}"])
        addresses.set(["10.0.0.9"])
        self.assertEqual(links(), [f"http://10.0.0.9:8765/v/{self.hub_room()}"])

    def hub_room(self):
        return self.server_args[0].room

    def test_no_watcher_with_host(self):
        self.assertEqual(self.run_watched(["--host", "10.9.9.9"]), 0)
        self.assertEqual(self.watchers, [])
        self.assertEqual(self.server_args[3](), [f"http://10.9.9.9:8765/v/{self.hub_room()}"])


class AppAddressChangeTest(AppRunMixin, unittest.TestCase):
    """app.main with the real address watcher: what the terminal says when the address changes."""

    def run_with_change(self, *steps, start=("192.168.1.20",)):
        """Runs Porthole while the PC's addresses become each of `steps` in turn (each one seen by the watcher)."""
        current = list(start)
        changed = threading.Event()
        real_start = app.start_address_watcher

        def fast_start(addresses, discovery, **kwargs):
            on_change = kwargs.pop("on_change")
            kwargs.pop("interval", None)

            def told(value):
                on_change(value)
                changed.set()

            return real_start(addresses, discovery, interval=0.01, on_change=told, **kwargs)

        def serve():  # the PC moves to another network while Porthole serves
            for step in steps:
                changed.clear()
                current[:] = step
                self.assertTrue(changed.wait(3))

        with mock.patch.object(app, "start_address_watcher", fast_start):
            self.assertEqual(self.run_main(addresses=current, on_serve=serve), 0)
        return self.server_args[0].room

    def test_terminal_prints_the_new_viewer_link(self):
        room = self.run_with_change(["10.0.0.7"])
        self.assertIn(f"The network address changed. Viewers (phone or PC): http://10.0.0.7:8765/v/{room}\n", self.output)

    def test_terminal_says_when_no_address_is_left(self):
        # In the sharer page's plain words ("network address" is a tech word).
        self.run_with_change([])
        self.assertIn("This PC has no network now, so other devices can't connect.\n", self.output)

    def test_terminal_says_when_this_pc_has_no_network_at_start(self):
        self.assertEqual(self.run_main(addresses=[]), 0)
        self.assertIn("  This PC has no network, so other devices can't connect.\n", self.output)
        self.assertNotIn("network address", self.output)

    def test_back_on_the_network_with_the_same_link_says_so(self):
        # A Wi-Fi drop: the address comes back as it was, so the link people have still works. Before, the terminal
        # said "The network address changed" with that same link.
        room = self.run_with_change([], ["192.168.1.20"])
        link = f"http://192.168.1.20:8765/v/{room}"
        after = self.output.split("This PC has no network now, so other devices can't connect.\n", 1)[1]
        self.assertEqual(after.splitlines()[0], f"Back on the network. Viewers (phone or PC): {link} (same link as before)")
        self.assertNotIn("The network address changed", self.output)

    def test_back_on_the_network_with_another_address_says_the_link_changed(self):
        room = self.run_with_change([], ["10.0.0.7"])
        self.assertIn(f"The network address changed. Viewers (phone or PC): http://10.0.0.7:8765/v/{room}\n", self.output)
        self.assertNotIn("Back on the network", self.output)

    def test_back_after_a_change_compares_with_the_last_link_shown(self):
        # 192.168.1.20, then 10.0.0.7, then no network, then 192.168.1.20 again: people have the 10.0.0.7 link, so the
        # link did change.
        room = self.run_with_change(["10.0.0.7"], [], ["192.168.1.20"])
        self.assertIn(f"The network address changed. Viewers (phone or PC): http://192.168.1.20:8765/v/{room}\n", self.output)
        self.assertNotIn("Back on the network", self.output)

    def test_a_network_that_comes_later_is_not_called_back(self):
        # Started with no network: there was no link before, so the first one isn't "the same link as before".
        room = self.run_with_change(["192.168.1.20"], start=())
        self.assertIn(f"The network address changed. Viewers (phone or PC): http://192.168.1.20:8765/v/{room}\n", self.output)
        self.assertNotIn("Back on the network", self.output)

    def test_another_address_coming_leaves_the_link_alone(self):
        # USB tethering or the PC's own hotspot adds an address: the link shown, the QR code and the easy address
        # stay as they were, so the terminal mustn't say the address changed.
        self.run_with_change(["192.168.1.20", "192.168.42.10"])
        self.assertNotIn("The network address changed", self.output)
        self.assertNotIn("Back on the network", self.output)


class NetworkNewsTest(unittest.TestCase):
    """app.network_news: what the terminal says when the main viewer link changes."""

    LINK = "http://192.168.1.20:8765/v/room1"
    OTHER = "http://10.0.0.7:8765/v/room1"

    def test_nothing_new(self):
        self.assertIsNone(app.network_news(self.LINK, self.LINK, self.LINK))
        self.assertIsNone(app.network_news(None, self.LINK, None))  # still no network
        self.assertIsNone(app.network_news(None, None, None))

    def test_the_network_goes(self):
        self.assertEqual(app.network_news(self.LINK, self.LINK, None), "This PC has no network now, so other devices can't connect.")

    def test_back_with_the_same_link(self):
        self.assertEqual(
            app.network_news(None, self.LINK, self.LINK),
            f"Back on the network. Viewers (phone or PC): {self.LINK} (same link as before)",
        )

    def test_a_new_link(self):
        expected = f"The network address changed. Viewers (phone or PC): {self.OTHER}"
        self.assertEqual(app.network_news(self.LINK, self.LINK, self.OTHER), expected)  # moved without a drop
        self.assertEqual(app.network_news(None, self.LINK, self.OTHER), expected)  # back with another address
        self.assertEqual(app.network_news(None, None, self.OTHER), expected)  # the first network since start


SHARER = "http://127.0.0.1:8765/share"
WINDOW = SHARER + "?window"  # the page can tell it is in Porthole's own window


class FakeWindow:
    """What appwindow.open_app_window gives back: the app window's browser process, which Porthole can let go
    of (untie) or end (close)."""

    def __init__(self, steps=None):
        self.calls = []
        self.steps = steps

    def untie(self):
        self.calls.append("untie")

    def retie(self):
        self.calls.append("retie")

    def settle(self):
        self.calls.append("settle")

    def close(self):
        self.calls.append("close")
        if self.steps is not None:
            self.steps.append("close window")


class OpenPageTest(AppRunMixin, unittest.TestCase):
    """How app.main opens the sharer page, and closing its app window stops Porthole."""

    def setUp(self):
        self.kind = "none"
        self.window_ok = True
        self.windows = []  # (url, on_closed, on_failed)
        self.page_checks = []  # what each app window was given to watch the sharer page with
        self.handles = []  # the FakeWindow each app window gave back
        self.tabs = []
        self.xdg = []
        self.xdg_error = None
        self.steps = []
        real_end = Hub.end

        def spy_end(hub):
            self.steps.append("end")
            real_end(hub)

        def fake_window(url, on_closed=None, on_failed=None, page_present=None):
            self.windows.append((url, on_closed, on_failed))
            self.page_checks.append(page_present)
            if not self.window_ok:
                return None
            handle = FakeWindow(self.steps)
            self.handles.append(handle)
            return handle

        def fake_popen(args, **kwargs):
            if self.xdg_error:
                raise self.xdg_error
            self.xdg.append(list(args))
            return types.SimpleNamespace(wait=lambda: 0)

        for target, name, value in (
            (app.sandbox, "kind", lambda: self.kind),
            (app.appwindow, "open_app_window", fake_window),
            (app.webbrowser, "open", lambda url: self.tabs.append(url) or True),  # True: a browser started
            (app.subprocess, "Popen", fake_popen),
            (Hub, "end", spy_end),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_page(self, *args, on_serve=None, platform="linux"):
        return self.run_main(list(args), no_browser=False, on_serve=on_serve, platform=platform)

    def test_opens_its_own_app_window(self):
        self.assertEqual(self.open_page(), 0)
        self.assertEqual(len(self.windows), 1)
        url, on_closed, on_failed = self.windows[0]
        self.assertEqual(url, WINDOW)
        self.assertTrue(callable(on_closed))
        self.assertTrue(callable(on_failed))
        self.assertEqual((self.tabs, self.xdg), ([], []))

    def test_a_window_that_fails_to_open_falls_back_to_a_tab(self):
        # The browser stopped at once with an error: the page opens in a browser tab, without the window tag.
        self.assertEqual(self.open_page(on_serve=lambda: self.windows[0][2]()), 0)
        self.assertEqual(self.tabs, [SHARER])
        self.assertFalse(self.server.shut_down)  # nothing to tie to now: Quit stops it

    def test_a_tab_when_there_is_no_chromium_browser(self):
        self.window_ok = False
        self.assertEqual(self.open_page(), 0)
        self.assertEqual(len(self.windows), 1)
        self.assertEqual(self.tabs, [SHARER])

    def test_browser_tab_flag(self):
        self.assertEqual(self.open_page("--browser-tab"), 0)
        self.assertEqual(self.windows, [])
        self.assertEqual(self.tabs, [SHARER])

    def test_no_browser_opens_nothing(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual((self.windows, self.tabs, self.xdg), ([], [], []))

    def test_a_sandbox_uses_xdg_open(self):
        for kind in ("snap", "flatpak"):
            self.kind = kind
            self.xdg.clear()
            self.assertEqual(self.open_page(), 0)
            self.assertEqual(self.xdg, [["xdg-open", SHARER]], kind)
        self.assertEqual((self.windows, self.tabs), ([], []))

    def test_a_sandbox_without_xdg_open_uses_a_tab(self):
        self.kind = "flatpak"
        self.xdg_error = FileNotFoundError("xdg-open")
        self.assertEqual(self.open_page(), 0)
        self.assertEqual(self.tabs, [SHARER])
        self.assertEqual(self.windows, [])

    def test_quit_says_stopped_once_even_if_the_window_closes_first(self):
        # Quit: the server stops 0.5 s after hub.end(), but the page closes its window at once, and the browser can
        # be gone before the server stops. That is still a quit, not the window closing.
        def quit_then_close():
            self.server_args[0].end()  # what POST /api/quit does first
            self.windows[0][1]()  # the browser process exits

        self.server_shutdown_spy()
        self.assertEqual(self.open_page(on_serve=quit_then_close), 0)
        self.assertNotIn("Window closed", self.output)
        self.assertEqual(self.output.splitlines()[-1], "Stopped.")
        self.assertEqual(self.steps.count("end"), 1)

    def test_closing_the_window_stops_porthole_like_quit(self):
        def close_window():
            self.steps.append("serving")
            self.windows[0][1]()  # the browser process exits while Porthole serves

        real_shutdown = self.server_shutdown_spy()
        self.assertEqual(self.open_page(on_serve=close_window), 0)
        self.assertEqual(self.steps, ["serving", "end", "shutdown"])
        self.assertTrue(real_shutdown())
        self.assertIn("Window closed. Stopped.", self.output)

    def server_shutdown_spy(self):
        """Record when the fake server is shut down; returns a check that it was."""
        from tests.test_webserver import FakeServer

        real = FakeServer.shutdown

        def spy(server):
            self.steps.append("shutdown")
            real(server)

        patcher = mock.patch.object(FakeServer, "shutdown", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return lambda: self.server.shut_down

    def test_a_window_closed_right_after_it_opened_stops_porthole_too(self):
        # appwindow tells a hand-off to an open window from a closed window; app.py no longer guesses by time.
        self.assertFalse(hasattr(app, "HANDOFF_SECONDS"))
        self.server_shutdown_spy()
        self.assertEqual(self.open_page(on_serve=lambda: self.windows[0][1]()), 0)
        self.assertEqual(self.steps, ["end", "shutdown"])

    def test_open_in_my_browser_opens_a_tab_and_unties_the_window(self):
        # The app window's own profile can't see the browser's tabs: the page asks for a normal tab.
        # After that, closing the app window must not stop Porthole; Quit does.
        def hand_over():
            self.server_kwargs["open_in_browser"]()
            self.windows[0][1]()  # the app window closes itself

        self.assertEqual(self.open_page(on_serve=hand_over), 0)
        self.assertEqual(self.tabs, [SHARER])
        self.assertFalse(self.server.shut_down)
        self.assertNotIn("end", self.steps)
        self.assertNotIn("Window closed", self.output)

    def test_a_slow_browser_doesnt_hold_up_serving(self):
        # Python's webbrowser waits up to 5 s for a browser it starts (for ever for some $BROWSER values): the sharer
        # page and every phone waited meanwhile. The page opens while Porthole serves.
        self.window_ok = False
        serving = threading.Event()
        seen = []

        def slow_open(url):
            seen.append(serving.wait(3))
            self.tabs.append(url)
            return True

        with mock.patch.object(app.webbrowser, "open", slow_open):
            self.assertEqual(self.open_page(on_serve=serving.set), 0)
        self.assertEqual(seen, [True])
        self.assertEqual(self.tabs, [SHARER])

    def test_open_in_my_browser_answers_while_a_slow_browser_starts(self):
        # The page waited 5 s for its answer, the window's page still the sharer. A browser that can't be found answers
        # at once (see below); one still starting after a moment counts as opened.
        started = threading.Event()
        took = []

        def slow_open(url):
            started.wait(5)
            self.tabs.append(url)
            return True

        def hand_over():
            begin = time.monotonic()
            self.server_kwargs["open_in_browser"]()
            took.append(time.monotonic() - begin)
            started.set()

        with mock.patch.object(app.webbrowser, "open", slow_open):
            self.assertEqual(self.open_page(on_serve=hand_over), 0)
        self.assertLess(took[0], 2.5)
        self.assertEqual(self.tabs, [SHARER])
        self.assertEqual(self.handles[0].calls, ["untie"])

    def test_a_tab_that_takes_over_unties_the_window(self):
        # "Use this one" in a browser tab: the app window's page says "You can close this one" and lets go of the
        # window, so closing it leaves the new share running.
        def taken_over():
            self.server_kwargs["release_window"]()
            self.windows[0][1]()  # the person closes the app window

        self.assertEqual(self.open_page(on_serve=taken_over), 0)
        self.assertEqual(self.tabs, [])  # nothing else opens
        self.assertFalse(self.server.shut_down)
        self.assertNotIn("end", self.steps)
        self.assertNotIn("Window closed", self.output)

    def test_a_window_page_that_shares_again_ties_the_window_again(self):
        # A browser tab took over, then the window's page shared again (reloaded, or Use this one there): closing the
        # window stops Porthole again, as the README and docs/design.md say.
        def taken_back():
            self.server_kwargs["release_window"]()
            self.server_kwargs["window_back"]()  # a sharer page in Porthole's own window has connected
            self.windows[0][1]()  # the person closes the app window

        self.server_shutdown_spy()
        self.assertEqual(self.open_page(on_serve=taken_back), 0)
        self.assertEqual(self.handles[0].calls, ["untie", "retie"])
        self.assertIn("Window closed. Stopped.", self.output)

    def test_open_in_my_browser_with_no_browser_to_start_fails_and_keeps_the_window_tied(self):
        # webbrowser.open answers False when it couldn't start any browser. The page must hear that it failed (it
        # would say "Porthole is open in your browser" and close its window), in plain words, and closing the window
        # must still stop Porthole.
        failed = []

        def try_it():
            with mock.patch.object(app.webbrowser, "open", lambda url: False):
                try:
                    self.server_kwargs["open_in_browser"]()
                except app.webbrowser.Error as err:
                    failed.append(str(err))

        self.assertEqual(self.open_page(on_serve=try_it), 0)
        self.assertEqual(failed, ["no browser was found"])
        self.assertEqual(self.handles[0].calls, ["untie", "retie"])

    def test_open_in_my_browser_that_raises_keeps_the_window_tied(self):
        failed = []

        def broken(url):
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

        def try_it():
            with mock.patch.object(app.webbrowser, "open", broken):
                try:
                    self.server_kwargs["open_in_browser"]()
                except OSError as err:
                    failed.append(err.errno)

        self.assertEqual(self.open_page(on_serve=try_it), 0)
        self.assertEqual(failed, [errno.EAGAIN])  # the server turns it into plain words (webserver.plain_cause)
        self.assertEqual(self.handles[0].calls, ["untie", "retie"])

    def test_open_in_my_browser_in_a_sandbox_without_xdg_open_or_a_browser_fails(self):
        self.kind = "snap"
        self.xdg_error = FileNotFoundError(errno.ENOENT, "No such file or directory", "xdg-open")
        failed = []

        def try_it():
            with mock.patch.object(app.webbrowser, "open", lambda url: False):
                try:
                    self.server_kwargs["open_in_browser"]()
                except app.webbrowser.Error as err:
                    failed.append(str(err))

        self.assertEqual(self.open_page(on_serve=try_it), 0)
        self.assertEqual(failed, ["no browser was found"])

    def test_open_in_my_browser_in_a_sandbox_uses_xdg_open(self):
        self.kind = "snap"
        self.assertEqual(self.open_page(on_serve=lambda: self.server_kwargs["open_in_browser"]()), 0)
        self.assertEqual(self.xdg, [["xdg-open", SHARER], ["xdg-open", SHARER]])

    def test_open_in_my_browser_is_there_with_no_browser_too(self):
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(callable(self.server_kwargs["open_in_browser"]))

    def test_the_window_can_watch_the_sharer_page(self):
        # On a Mac the browser outlives its window, so appwindow watches whether the sharer page is connected.
        self.assertEqual(self.open_page(platform="darwin"), 0)
        hub = self.server_args[0]
        self.assertEqual(self.page_checks, [hub.sharer_active])

    def test_on_a_mac_porthole_ends_its_own_window_when_it_stops(self):
        # The browser Porthole started keeps running without windows on a Mac: end it with Porthole. close()
        # ends it only while it shows nothing but Porthole's page (see test_appwindow.CloseTest).
        self.assertEqual(self.open_page(platform="darwin"), 0)
        self.assertEqual(self.handles[0].calls, ["close"])

    def test_on_a_mac_a_window_let_go_of_is_asked_too(self):
        # After "Open in my browser" the app window closes itself. A browser left without windows would stay
        # in the Dock and catch the person's own Chrome launches, so close() still checks it at stop: it ends
        # it when nothing is open there, and leaves it when the tab (or anything else) opened in it.
        self.assertEqual(self.open_page(platform="darwin", on_serve=lambda: self.server_kwargs["open_in_browser"]()), 0)
        self.assertEqual(self.handles[0].calls, ["untie", "close"])

    def test_closing_the_window_on_a_mac_stops_porthole_then_ends_the_browser(self):
        self.server_shutdown_spy()
        # --no-discovery: no port 80 helper, whose shutdown the spy would count too.
        self.assertEqual(self.open_page("--no-discovery", platform="darwin", on_serve=lambda: self.windows[0][1]()), 0)
        self.assertEqual(self.steps, ["end", "shutdown", "close window"])

    def test_linux_and_windows_leave_the_window_to_the_page(self):
        # There the browser exits with its window, and the page closes its own window when Porthole stops.
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                self.handles.clear()
                self.assertEqual(self.open_page(platform=platform), 0)
                self.assertEqual(self.handles[0].calls, [])

    def test_open_in_my_browser_lets_go_of_the_window(self):
        def hand_over():
            self.server_kwargs["open_in_browser"]()

        self.assertEqual(self.open_page(on_serve=hand_over), 0)
        self.assertEqual(self.handles[0].calls, ["untie"])

    def test_a_browser_tab_on_a_mac_is_never_touched(self):
        # --browser-tab, the fallback and "Open in my browser" use the person's own browser: Porthole never ends it.
        self.assertEqual(self.open_page("--browser-tab", platform="darwin"), 0)
        self.assertEqual((self.windows, self.handles), ([], []))
        self.assertEqual(self.tabs, [SHARER])
        self.window_ok = False
        self.assertEqual(self.open_page(platform="darwin"), 0)  # no Chromium-family browser: a tab
        self.assertEqual(self.handles, [])
        self.assertEqual(self.tabs, [SHARER, SHARER])

    def test_closing_the_window_after_porthole_stopped_does_nothing(self):
        self.assertEqual(self.open_page(), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.windows[0][1]()
        self.assertFalse(self.server.shut_down)
        self.assertNotIn("end", self.steps)
        self.assertEqual(out.getvalue(), "")


class MacOpenInMyBrowserTest(AppRunMixin, unittest.TestCase):
    """"Open in my browser" on a Mac, through app.main with the real app window (appwindow.open_app_window and
    AppWindow) and a stand-in browser. The window is untied, so its closing no longer stops Porthole; when
    Porthole stops, the browser it started is ended only while it shows nothing but Porthole's own page."""

    CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.browsers = []
        self.watches = []  # (page_present, page_gone) of each page watch the window started
        self.watching = threading.Event()
        self.tabs = []
        self.pages = []  # what Porthole's browser shows when asked, at the end
        real_open = app.appwindow.open_app_window

        def popen(args, **kwargs):
            browser = FakeBrowser(args)
            self.browsers.append(browser)
            return browser

        def watch(present, on_gone, stop=None):
            self.watches.append((present, on_gone, stop))
            self.watching.set()

        def open_window(url, on_closed=None, on_failed=None, page_present=None):
            self.window = real_open(
                url, on_closed, on_failed=on_failed, page_present=page_present, find=lambda: [self.CHROME],
                popen=popen, profile=Path(tmp.name) / "browser", platform="darwin", watch=watch,
                pages=lambda: list(self.pages),
            )
            return self.window

        for target, name, value in (
            (app.appwindow, "open_app_window", open_window),
            (app.webbrowser, "open", lambda url: self.tabs.append(url) or True),  # True: a browser started
            (app.sandbox, "kind", lambda: "none"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_mac(self, hand_over=True, pages=()):
        def serving():
            self.assertTrue(self.watching.wait(3))
            if hand_over:
                self.server_kwargs["open_in_browser"]()
            self.pages = list(pages)
            self.watches[0][1]()  # the sharer page has gone for good: the app window has closed
            # The browser's end is reported from its own thread: give a stop the time to happen while serving.
            pause = threading.Event()
            for _ in range(10 if hand_over else 300):
                if self.server.shut_down:
                    break
                pause.wait(0.01)
            self.while_serving = list(self.browsers[0].signals)

        # --no-discovery: no port 80 helper.
        self.assertEqual(self.run_main(["--no-discovery"], no_browser=False, on_serve=serving, platform="darwin"), 0)
        return self.browsers[0]

    def test_tied_the_window_closing_stops_porthole(self):
        browser = self.run_mac(hand_over=False)
        self.assertTrue(self.server.shut_down)
        self.assertIn("Window closed. Stopped.", self.output)
        self.assertEqual(browser.signals, ["terminate"])

    def test_open_in_my_browser_unties_the_window(self):
        browser = self.run_mac()
        self.assertEqual(self.tabs, [SHARER])
        self.assertFalse(self.window.tied)
        self.assertTrue(self.watches[0][2].is_set())  # the page watch stops
        self.assertFalse(self.server.shut_down)  # closing the window no longer stops Porthole: Quit does
        self.assertNotIn("Window closed", self.output)
        self.assertEqual(self.while_serving, [])  # nor does it end the browser then; only Porthole's stop may
        self.assertEqual(browser.signals, ["terminate"])

    def test_at_stop_a_browser_with_only_our_page_or_nothing_is_ended(self):
        # The tab opened in the person's own browser: Porthole's copy is left without windows (or still shows
        # only the app window's page), and would sit in the Dock catching Chrome launches.
        for pages in ([], [WINDOW]):
            with self.subTest(pages=pages):
                self.browsers.clear()
                self.watches.clear()
                self.watching.clear()
                browser = self.run_mac(pages=pages)
                self.assertFalse(self.server.shut_down)
                self.assertEqual(browser.signals, ["terminate"])

    def test_at_stop_a_browser_that_shows_anything_else_is_left(self):
        # macOS can put the person's own pages, or the tab "Open in my browser" opened, in Porthole's copy of Chrome.
        for pages in (["http://127.0.0.1:8765/share"], [WINDOW, "https://mail.example/inbox"], ["chrome://newtab/"]):
            with self.subTest(pages=pages):
                self.browsers.clear()
                self.watches.clear()
                self.watching.clear()
                browser = self.run_mac(pages=pages)
                self.assertFalse(self.server.shut_down)
                self.assertEqual(browser.signals, [])


class ReportErrorTest(unittest.TestCase):
    """The Windows message box: an error icon for errors, an information icon for notices."""

    def boxes(self, *args, **kwargs):
        shown = []
        user32 = types.SimpleNamespace(MessageBoxW=lambda owner, text, title, flags: shown.append((text, title, flags)))
        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(user32=user32))
        with (
            mock.patch.object(app, "os", types.SimpleNamespace(name="nt")),
            mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            app.report_error(*args, **kwargs)
        return shown

    # 0x10000 (MB_SETFOREGROUND): started from the Start menu there is no window yet, and the box must not
    # open behind other windows.
    def test_errors_show_the_error_icon(self):
        self.assertEqual(
            self.boxes("Port 8765 is already in use.", True), [("Port 8765 is already in use.", "Porthole", 0x10 | 0x10000)]
        )

    def test_notices_show_the_information_icon(self):
        self.assertEqual(self.boxes("Opened it.", True, info=True), [("Opened it.", "Porthole", 0x40 | 0x10000)])

    def test_no_box_without_a_launcher(self):
        self.assertEqual(self.boxes("Opened it.", False, info=True), [])


class AlreadyRunningTest(unittest.TestCase):
    def running_server(self):
        server = make_server(Hub(), 0, WEB, [])
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    window_ok = False  # whether a Chromium-family browser "opens" the app window
    kind = "none"

    def run_main(self, port, *extra):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.opened = []
        self.windows = []
        self.page_checks = []
        self.failed_handlers = []
        self.xdg = []
        self.reported = []
        self.steps = []  # opening the page and showing a message, in order
        real_report = app.report_error

        def fake_window(url, on_closed=None, on_failed=None, page_present=None):
            self.windows.append((url, on_closed))
            self.failed_handlers.append(on_failed)
            self.page_checks.append(page_present)
            if self.window_ok:
                self.steps.append("open")
                return FakeWindow()
            return None

        def fake_popen(args, **kwargs):
            self.xdg.append(list(args))
            self.steps.append("open")
            return types.SimpleNamespace(wait=lambda: 0)

        def spy_report(message, launcher, info=False):
            self.reported.append((message, launcher, info))
            self.steps.append("message")
            real_report(message, False)  # prints; never a real pop-up in tests

        def spy_open(url):
            self.opened.append(url)
            self.steps.append("open")

        out = io.StringIO()
        with (
            mock.patch.object(app.webbrowser, "open", spy_open),
            mock.patch.object(app.appwindow, "open_app_window", fake_window),
            mock.patch.object(app.subprocess, "Popen", fake_popen),
            mock.patch.object(app.sandbox, "kind", lambda: self.kind),
            mock.patch.object(app, "report_error", spy_report),
            contextlib.redirect_stdout(out),
        ):
            code = app.main(["--port", str(port), "--record-dir", tmp.name, *extra])
        return code, out.getvalue()

    def test_same_folder_says_already_running(self):
        port = self.running_server()
        code, out = self.run_main(port)
        self.assertEqual(code, 0)
        self.assertIn("Already running. Opened the sharer page.", out)
        self.assertEqual(self.opened, [f"http://127.0.0.1:{port}/share"])
        self.assertEqual(self.reported, [])

    def test_other_folder_is_named_and_opened(self):
        port = self.running_server()
        with mock.patch.object(webserver, "APP_FOLDER", "/home/someone/old-porthole"):
            code, out = self.run_main(port)
        self.assertEqual(code, 0)
        message = "Porthole is already running from /home/someone/old-porthole. Opened it."
        self.assertIn(message, out)
        self.assertNotIn("Already running.", out)
        self.assertEqual(self.reported, [(message, False, True)])  # a notice, not an error
        self.assertEqual(self.opened, [f"http://127.0.0.1:{port}/share"])

    def test_other_folder_opens_the_page_before_the_message(self):
        # On Windows the launcher's message box waits for OK; "Opened it." must already be true.
        port = self.running_server()
        with mock.patch.object(webserver, "APP_FOLDER", "/opt/porthole"):
            code, _ = self.run_main(port, "--launcher")
        self.assertEqual(code, 0)
        self.assertEqual(self.steps, ["open", "message"])

    def test_other_folder_shows_on_screen_from_a_launcher(self):
        port = self.running_server()
        with mock.patch.object(webserver, "APP_FOLDER", "/opt/porthole"):
            code, _ = self.run_main(port, "--launcher", "--no-browser")
        self.assertEqual(code, 0)
        # --no-browser: nothing opens, so it says where the running one is instead.
        message = f"Porthole is already running from /opt/porthole: http://127.0.0.1:{port}/share"
        self.assertEqual(self.reported, [(message, True, True)])
        self.assertEqual(self.opened, [])

    def test_an_older_porthole_without_a_folder_counts_as_the_same(self):
        self.assertTrue(app.same_folder({"app": "porthole"}))
        self.assertTrue(app.same_folder({"app": "porthole", "folder": str(app.APP_FOLDER)}))
        self.assertTrue(app.same_folder({"app": "porthole", "folder": 42}))
        self.assertFalse(app.same_folder({"app": "porthole", "folder": "/elsewhere"}))
        port = self.running_server()
        with mock.patch.object(app, "_running_porthole", lambda port: {"app": "porthole"}):
            code, out = self.run_main(port, "--no-browser")
        self.assertEqual(code, 0)
        self.assertIn(f"Already running: http://127.0.0.1:{port}/share", out)  # --no-browser: nothing opened
        self.assertNotIn("Opened", out)

    def test_opens_an_app_window_but_never_ties_this_copy_to_it(self):
        port = self.running_server()
        self.window_ok = True
        code, out = self.run_main(port)
        self.assertEqual(code, 0)
        self.assertEqual(self.windows, [(f"http://127.0.0.1:{port}/share?window", None)])  # no on_closed
        self.assertEqual(self.page_checks, [None])  # and nothing to watch: this copy ends now
        self.assertEqual(self.opened, [])
        self.assertIn("Already running. Opened the sharer page.", out)
        with mock.patch.object(app.webbrowser, "open", self.opened.append):  # never a real browser in tests
            self.failed_handlers[0]()  # a browser that can't start still gets a tab instead
        self.assertEqual(self.opened, [f"http://127.0.0.1:{port}/share"])

    def test_ctrl_c_while_it_waits_for_the_window_ends_quietly(self):
        # "Already running" waits a few seconds to see the window start; Ctrl+C then must print no traceback.
        port = self.running_server()
        self.window_ok = True

        def impatient(*args, **kwargs):
            raise KeyboardInterrupt

        with mock.patch.object(FakeWindow, "settle", impatient, create=True):
            code, out = self.run_main(port)
        self.assertEqual(code, 0)
        self.assertIn("Already running. Opened the sharer page.", out)

    def test_browser_tab_flag_opens_a_tab(self):
        port = self.running_server()
        self.window_ok = True
        code, _ = self.run_main(port, "--browser-tab")
        self.assertEqual(code, 0)
        self.assertEqual(self.windows, [])
        self.assertEqual(self.opened, [f"http://127.0.0.1:{port}/share"])

    def test_in_a_sandbox_opens_with_xdg_open(self):
        port = self.running_server()
        self.window_ok = True
        self.kind = "snap"
        code, _ = self.run_main(port)
        self.assertEqual(code, 0)
        self.assertEqual(self.xdg, [["xdg-open", f"http://127.0.0.1:{port}/share"]])
        self.assertEqual(self.windows, [])
        self.assertEqual(self.opened, [])

    def test_app_folder_is_the_folder_with_start_py(self):
        self.assertTrue((Path(app.APP_FOLDER) / "start.py").is_file())
        with mock.patch.dict(app.os.environ, {}, clear=False):
            app.os.environ.pop("APPIMAGE", None)
            self.assertEqual(webserver.runs_from(app.APP_FOLDER), str(app.APP_FOLDER))
        self.assertEqual(webserver.APP_FOLDER, webserver.runs_from(Path(webserver.__file__).resolve().parent.parent))

    def test_an_appimage_runs_from_its_file_not_its_mount_folder(self):
        # Each run mounts the AppImage in a new folder ($APPDIR), so the folder would never match.
        appimage = "/home/ayla/Apps/Porthole-x86_64.AppImage"
        mount = Path("/tmp/.mount_PorthoAbC123")
        with (
            mock.patch.dict(app.os.environ, {"APPIMAGE": appimage, "APPDIR": str(mount)}),
            mock.patch.object(app, "APP_FOLDER", mount),
        ):
            self.assertEqual(webserver.runs_from(mount), appimage)
            self.assertTrue(app.same_folder({"app": "porthole", "folder": appimage}))
            self.assertFalse(app.same_folder({"app": "porthole", "folder": str(mount)}))

    def test_appimage_variables_inherited_from_another_app_are_ignored(self):
        # A source copy started from a terminal inside another AppImage (an editor, say) inherits its
        # $APPIMAGE and $APPDIR. It still runs from its own folder.
        editor = "/opt/Editor.AppImage"
        for env in ({"APPIMAGE": editor}, {"APPIMAGE": editor, "APPDIR": "/tmp/.mount_EditorXy12"}):
            with self.subTest(env=env), mock.patch.dict(app.os.environ, env):
                if "APPDIR" not in env:
                    app.os.environ.pop("APPDIR", None)
                self.assertEqual(webserver.runs_from(app.APP_FOLDER), str(app.APP_FOLDER))
                self.assertTrue(app.same_folder({"app": "porthole", "folder": str(app.APP_FOLDER)}))
                self.assertFalse(app.same_folder({"app": "porthole", "folder": editor}))


class PortInUseMixin:
    """app.main when the port is taken by something that isn't Porthole."""

    def run_port_in_use(self, *args, stdout="capture", platform="linux"):
        """`platform` as in AppRunMixin.run_main: tests never run as the frozen Mac app by accident (it would show
        a real alert on a Mac)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = io.StringIO() if stdout == "capture" else stdout
        with (
            mock.patch.object(app.sys, "platform", platform),
            mock.patch.object(app, "make_server", side_effect=OSError(errno.EADDRINUSE, "in use")),
            mock.patch.object(app, "_running_porthole", lambda port: None),
            mock.patch.object(app, "lan_addresses", lambda: ["192.168.1.20"]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(None) if stdout is None else contextlib.nullcontext(),
        ):
            code = app.main(["--no-browser", "--port", "8765", "--record-dir", tmp.name, *args])
        self.output = out.getvalue() if isinstance(out, io.StringIO) else ""
        return code


class FrozenAppTest(FrozenMixin, PortInUseMixin, AppRunMixin, unittest.TestCase):
    """Built with PyInstaller: the pages come from the bundle and the hints name the program."""

    def test_serves_the_pages_from_the_bundle(self):
        self.freeze()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(Path(self.server_args[2]), self.bundle / "web")

    def test_run_hint_names_the_program(self):
        self.freeze()
        self.assertEqual(app.run_hint(), Path(self.exe).name)

    def test_a_program_name_with_a_space_is_quoted(self):
        self.freeze()
        with mock.patch.object(sys, "executable", str(self.program_dir / "My Porthole.exe")):
            self.assertEqual(app.run_hint(), '"My Porthole.exe"')

    def test_port_in_use_names_the_program(self):
        self.freeze()
        self.assertEqual(self.run_port_in_use(), 1)
        self.assertIn(f"Port 8765 is already in use. Try: {Path(self.exe).name} --port 8766", self.output)

    def test_firewall_hint_names_porthole_not_python(self):
        self.freeze()
        self.assertEqual(self.run_main(platform="win32"), 0)
        self.assertIn("Allow Porthole through Windows Firewall when asked.", self.output)
        self.assertNotIn("Python", self.output)

    def test_from_source_the_firewall_hint_names_python(self):
        self.assertEqual(self.run_main(platform="win32"), 0)
        self.assertIn("Allow Python through Windows Firewall when asked.", self.output)
        self.assertNotIn("ufw", self.output)


class NoConsoleTest(PortInUseMixin, AppRunMixin, unittest.TestCase):
    """A windowed app (frozen without a console, or pythonw) has no sys.stdout or sys.stderr. It must never
    write to them, and it shows start-up problems in a dialog, as with --launcher."""

    def setUp(self):
        self.dialogs = []
        patcher = mock.patch.object(app, "show_dialog", lambda message, info=False: self.dialogs.append((message, info)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def main_without_console(self, *args):
        with contextlib.redirect_stdout(None), contextlib.redirect_stderr(None):
            return app.main(list(args))

    def test_runs_without_a_console(self):
        self.assertEqual(self.run_main(stdout=None), 0)
        self.assertTrue(self.server.served)
        self.assertEqual(self.dialogs, [])

    def test_bad_arguments_show_a_dialog(self):
        with self.assertRaises(SystemExit) as ctx:
            self.main_without_console("--port", "abc")
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(len(self.dialogs), 1)
        message, info = self.dialogs[0]
        self.assertTrue(message.startswith("Porthole couldn't start: "), message)
        self.assertIn("--port", message)
        self.assertIn("must be a number", message)
        self.assertFalse(info)

    def test_unknown_arguments_show_a_dialog(self):
        with self.assertRaises(SystemExit) as ctx:
            self.main_without_console("--colour", "blue")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--colour", self.dialogs[0][0])

    def test_help_shows_in_a_dialog(self):
        with self.assertRaises(SystemExit) as ctx:
            self.main_without_console("--help")
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(len(self.dialogs), 1)
        message, info = self.dialogs[0]
        self.assertIn("--browser-tab", message)
        self.assertTrue(info)

    def test_port_in_use_shows_a_dialog(self):
        self.assertEqual(self.run_port_in_use(stdout=None), 1)
        self.assertEqual(self.dialogs, [(f"Port 8765 is already in use. Try: {app.run_hint()} --port 8766", False)])

    def test_install_launcher_says_what_it_did_in_a_dialog(self):
        with mock.patch("porthole.launcher.install", return_value=["Added Porthole to the Start menu and the desktop."]):
            self.assertEqual(self.main_without_console("--install-launcher"), 0)
        self.assertEqual(self.dialogs, [("Added Porthole to the Start menu and the desktop.", True)])

    def test_install_launcher_problems_show_in_a_dialog(self):
        with mock.patch("porthole.launcher.install", side_effect=RuntimeError("PowerShell failed")):
            self.assertEqual(self.main_without_console("--install-launcher"), 1)
        self.assertEqual(self.dialogs, [("Couldn't change the launcher: PowerShell failed", False)])

    def test_launcher_flag_shows_bad_arguments_in_a_dialog_too(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            app.main(["--launcher", "--port", "0"])
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("must be between 1 and 65535", self.dialogs[0][0])

    def test_a_console_gets_the_usual_messages_and_no_dialog(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            app.main(["--port", "abc"])
        self.assertEqual(self.dialogs, [])
        self.assertIn("usage:", err.getvalue())
        self.assertIn("must be a number", err.getvalue())

    def test_usage_names_the_command_people_type(self):
        # The .deb's and the snap's start scripts run start.py, but people start them as `porthole`.
        err = io.StringIO()
        with mock.patch.dict(os.environ, {app.COMMAND_VARIABLE: "porthole"}), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                app.main(["--port", "abc"])
        self.assertIn("usage: porthole [-h]", err.getvalue())
        self.assertIn("porthole: error: argument --port: must be a number", err.getvalue())

    def test_a_console_shows_port_problems_without_a_dialog(self):
        self.assertEqual(self.run_port_in_use(), 1)
        self.assertEqual(self.dialogs, [])
        self.assertIn("is already in use", self.output)


class MacDialogTest(unittest.TestCase):
    """macOS: errors and notices show in an alert through osascript (notify-send doesn't exist there). The text
    is passed as an argument and read with `on run argv`, never pasted into the script."""

    def alert(self, message, info=False, run=None):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((list(args), kwargs))
            return subprocess.CompletedProcess(args, 0)

        with (
            mock.patch.object(app, "os", types.SimpleNamespace(name="posix")),
            mock.patch.object(app.sys, "platform", "darwin"),
            mock.patch.object(app.subprocess, "run", run or fake_run),
        ):
            app.show_dialog(message, info)
        return calls

    @staticmethod
    def script(args):
        return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-e"]

    def test_an_error_alert(self):
        [(args, kwargs)] = self.alert("Port 8765 is already in use.")
        self.assertEqual(args[0], "/usr/bin/osascript")
        script = self.script(args)
        self.assertEqual(script[0], "on run argv")
        self.assertEqual(script[-1], "end run")
        self.assertIn(
            f"display alert (item 1 of argv) message (item 2 of argv) as critical giving up after {app.DIALOG_SECONDS}",
            script,
        )
        self.assertEqual(args[-2:], ["Porthole", "Port 8765 is already in use."])
        self.assertIs(kwargs.get("check"), False)
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertGreater(kwargs.get("timeout"), app.DIALOG_SECONDS)

    def test_the_alert_comes_to_the_front(self):
        # Opened from Finder, Porthole has no window of its own, so without "activate" the alert can open
        # behind other windows, where nobody sees it.
        for info in (False, True):
            [(args, _)] = self.alert("Port 8765 is already in use.", info=info)
            script = self.script(args)
            self.assertIn("activate", script)
            display = next(i for i, line in enumerate(script) if line.startswith("display alert"))
            self.assertLess(script.index("on run argv"), script.index("activate"))
            self.assertLess(script.index("activate"), display)

    def test_a_notice_is_informational(self):
        [(args, _)] = self.alert("Opened it.", info=True)
        self.assertTrue(any("as informational" in line for line in self.script(args)))
        self.assertFalse(any("as critical" in line for line in self.script(args)))

    def test_the_text_never_becomes_part_of_the_script(self):
        nasty = 'Folder "x" & (do shell script "touch /tmp/pwned") -- end run'
        [(args, _)] = self.alert(nasty)
        self.assertEqual(args[-1], nasty)
        for line in self.script(args):
            self.assertNotIn("pwned", line)
        # The title comes first, so a message starting with a dash is never read as an option.
        [(args, _)] = self.alert("--port must be a number")
        self.assertEqual(args[-2:], ["Porthole", "--port must be a number"])

    def test_never_raises(self):
        for error in (FileNotFoundError("osascript"), subprocess.TimeoutExpired("osascript", 1), OSError("no")):
            with self.subTest(error=error):
                def broken(args, **kwargs):
                    raise error

                self.alert("Port 8765 is already in use.", run=broken)  # nothing raised

    def test_linux_keeps_its_desktop_notification(self):
        calls = []
        with (
            mock.patch.object(app, "os", types.SimpleNamespace(name="posix")),
            mock.patch.object(app.sys, "platform", "linux"),
            mock.patch.object(app.subprocess, "run", lambda args, **kwargs: calls.append(list(args))),
        ):
            app.show_dialog("Port 8765 is already in use.")
        self.assertEqual(calls, [["notify-send", "-i", str(app.paths.web_dir() / "icon-512.png"), "Porthole", "Port 8765 is already in use."]])


class SnapNoticeTest(unittest.TestCase):
    """Inside the snap, notify-send's notices went to the desktop portal, which answered "The app by ID
    porthole_porthole could not be found" on a real Ubuntu (23 Nov 2026), and nothing showed. There it talks to
    the notification service directly (NOTIFY_IGNORE_PORTAL, read by libnotify), which the snap's `desktop` plug
    allows."""

    def notify(self, kind, env=None):
        calls = []
        with (
            mock.patch.object(app.os, "name", "posix"),
            mock.patch.object(app.sys, "platform", "linux"),
            mock.patch.object(app.sandbox, "kind", lambda: kind),
            mock.patch.dict(os.environ, env or {}),
            mock.patch.object(app.subprocess, "run", lambda args, **kwargs: calls.append((list(args), kwargs))),
        ):
            app.show_dialog("Porthole is already running from /usr/share/porthole. Opened it.", info=True)
        [(args, kwargs)] = calls
        self.assertEqual(args, ["notify-send", "-i", str(app.paths.web_dir() / "icon-512.png"), "Porthole", "Porthole is already running from /usr/share/porthole. Opened it."])
        return kwargs.get("env")

    def test_in_the_snap_notices_skip_the_portal(self):
        env = self.notify("snap", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "SNAP": "/snap/porthole/x1"})
        self.assertEqual(env["NOTIFY_IGNORE_PORTAL"], "1")
        # Everything else is passed on: notify-send needs the session bus to reach the notification service.
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/1000/bus")
        self.assertEqual(env["SNAP"], "/snap/porthole/x1")

    def test_elsewhere_the_environment_is_left_alone(self):
        for kind in ("none", "flatpak"):
            with self.subTest(kind=kind):
                self.assertIsNone(self.notify(kind))

    @unittest.skipIf(os.name == "nt", "a shell script stands in for notify-send")
    def test_a_real_notify_send_sees_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = Path(tmp) / "seen"
            fake = Path(tmp) / "notify-send"
            fake.write_text(f'#!/bin/sh\nprintf "%s|%s|%s|%s|%s" "$NOTIFY_IGNORE_PORTAL" "$1" "$2" "$3" "$4" > "{seen}"\n')
            fake.chmod(0o755)
            with (
                mock.patch.object(app.sys, "platform", "linux"),
                mock.patch.object(app.sandbox, "kind", lambda: "snap"),
                mock.patch.dict(os.environ, {"PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}"}),
            ):
                app.show_dialog("Port 8765 is busy, so Porthole uses port 8766.", info=True)
            icon = app.paths.web_dir() / "icon-512.png"
            self.assertEqual(seen.read_text(), f"1|-i|{icon}|Porthole|Port 8765 is busy, so Porthole uses port 8766.")
            self.assertTrue(icon.is_file())


class MacAppTest(MacBundleMixin, PortInUseMixin, AppRunMixin, unittest.TestCase):
    """The built Porthole.app opened from Finder: its output goes to /dev/null, not None, so the "no console"
    check can't tell. Frozen on a Mac it behaves as --launcher: problems show on screen."""

    def setUp(self):
        self.dialogs = []
        patcher = mock.patch.object(app, "show_dialog", lambda message, info=False: self.dialogs.append((message, info)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def main_from_finder(self, *args):
        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            return app.main(list(args))

    def test_bad_arguments_show_on_screen(self):
        self.freeze_mac()
        with self.assertRaises(SystemExit) as ctx:
            self.main_from_finder("--port", "abc")
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("must be a number", self.dialogs[0][0])
        self.assertFalse(self.dialogs[0][1])

    def test_help_shows_on_screen(self):
        self.freeze_mac()
        with self.assertRaises(SystemExit) as ctx:
            self.main_from_finder("--help")
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--browser-tab", self.dialogs[0][0])
        self.assertTrue(self.dialogs[0][1])

    def test_the_run_hint_is_a_command_people_can_type(self):
        self.freeze_mac()
        self.assertEqual(app.run_hint(), "open -a Porthole --args")
        self.freeze_mac(name="My Porthole.app")
        self.assertEqual(app.run_hint(), "open -a 'My Porthole' --args")

    def test_port_in_use_shows_on_screen_with_that_command(self):
        self.freeze_mac()
        self.assertEqual(self.run_port_in_use(platform="darwin"), 1)
        message = "Port 8765 is already in use. Try: open -a Porthole --args --port 8766"
        self.assertEqual(self.dialogs, [(message, False)])
        self.assertIn(message, self.output)

    def test_runs_normally_without_any_alert(self):
        self.freeze_mac()
        self.assertEqual(self.run_main(platform="darwin"), 0)
        self.assertTrue(self.server.served)
        self.assertEqual(self.dialogs, [])

    def test_firewall_hint_on_a_mac(self):
        self.freeze_mac()
        self.assertEqual(self.run_main(platform="darwin"), 0)
        self.assertIn("If macOS asks whether Porthole may accept incoming connections, click Allow.", self.output)
        self.assertNotIn("ufw", self.output)

    def test_firewall_hint_from_source_on_a_mac_names_python(self):
        self.assertEqual(self.run_main(platform="darwin"), 0)
        self.assertIn("If macOS asks whether Python may accept incoming connections, click Allow.", self.output)
        self.assertNotIn("ufw", self.output)

    def test_from_source_on_a_mac_a_terminal_gets_no_alert(self):
        with mock.patch.object(app.sys, "platform", "darwin"), self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                app.main(["--port", "abc"])
        self.assertEqual(self.dialogs, [])

    def test_frozen_elsewhere_with_a_console_gets_no_alert(self):
        self.freeze_mac(platform="linux")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            app.main(["--port", "abc"])
        self.assertEqual(self.dialogs, [])

    def test_install_launcher_says_where_the_app_goes(self):
        self.freeze_mac()
        for flag in ("--install-launcher", "--uninstall-launcher"):
            with self.subTest(flag=flag):
                self.dialogs.clear()
                with mock.patch.object(launcher, "_apps_dir", side_effect=AssertionError("no Linux menu on a Mac")):
                    self.assertEqual(self.main_from_finder(flag), 1)
                [(message, info)] = self.dialogs
                self.assertIn("Applications", message)
                self.assertNotIn("Couldn't change the launcher", message)
                self.assertTrue(info)


class OutputTest(AppRunMixin, unittest.TestCase):
    def test_a_folder_name_the_console_cant_show_never_stops_porthole(self):
        # Windows writes redirected output in the ANSI code page (cp1252), which has no "ə": printing the
        # recordings folder must not raise UnicodeEncodeError at start.
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = os.path.join(tmp.name, "Filmlər")
        self.assertEqual(self.run_main(["--record-dir", folder], stdout=stream), 0)
        stream.flush()
        text = raw.getvalue().decode("cp1252")
        self.assertIn("Porthole is running.", text)
        self.assertIn("Filml?r", text)

    def test_say_ignores_a_broken_console(self):
        class Broken(io.StringIO):
            def write(self, text):
                raise OSError(22, "Invalid argument")

        with contextlib.redirect_stdout(Broken()):
            app.say("Porthole is running.")  # nothing raised


if __name__ == "__main__":
    unittest.main()
