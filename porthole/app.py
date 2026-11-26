"""Porthole: share this PC's screen with phones and PCs on the same network.

Run:  python3 start.py   (or python3 -m porthole), then press Start sharing in the page that opens.
Stop: Ctrl+C, the Quit button on the sharer page, or closing the page's own app window.
"""

import argparse
import errno
import ipaddress
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import NamedTuple

from porthole import appwindow, paths, sandbox
from porthole.discovery import Discovery
from porthole.hub import Hub
from porthole.netinfo import lan_addresses
from porthole.recorder import Recorder, default_record_dir
from porthole.webserver import AcceptFailed, make_redirect_server, make_server, runs_from

APP_FOLDER = paths.app_folder()  # the folder Porthole runs from (the program's folder when built)
ADDRESS_SECONDS = 10  # how often the PC's network addresses are read again
DEFAULT_PORT = 8765
SPARE_PORTS = range(8766, 8786)  # tried in turn when the default port is busy; then any free port
PING_SECONDS = 2.0  # how long a program on a busy port gets to say it is Porthole
QUIT_WAIT_SECONDS = 5.0  # how long another Porthole asked to quit gets to let go of the port
COMMAND_VARIABLE = "PORTHOLE_COMMAND"  # set by the .deb's and the snap's start scripts: the command that starts them
_IN_USE = {errno.EADDRINUSE, 10048}  # 10048 = WSAEADDRINUSE
_ACCESS_DENIED = {errno.EACCES, 10013}  # 10013 = WSAEACCES (Windows reserved/excluded ports)
# Either can mean a program holds the port: on Windows a port held with SO_EXCLUSIVEADDRUSE (as Porthole holds its
# own) may refuse a second bind as forbidden rather than in use.
_TAKEN = _IN_USE | _ACCESS_DENIED
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # used with fullmatch: `$` would let a final "\n" through
_SHORT_PORT_PLATFORMS = ("win32", "darwin")  # Linux needs a system setting for port 80
DISCOVERY_OFF = "Easy address is turned off for this run."
ACCEPT_STOPPED = "Porthole can't accept connections any more, so it stopped."
WINDOW_TAG = "window"  # /share?window: the page can tell it is in Porthole's own window
STOP_SECONDS = 0.5  # once Porthole is stopping, a moment for the event streams to deliver "ended"...
RECORDING_STOP_SECONDS = 3.0  # ...and up to this long while the sharer page finishes a recording
OPEN_ANSWER_SECONDS = 1.0  # "Open in my browser": a browser still starting after this long counts as opened


def run_hint(command: str | None = None) -> str:
    """How to start Porthole again by hand, for hints like "Try: <this> --port 8766". `command` is what the
    .deb's and the snap's start scripts say starts them ($PORTHOLE_COMMAND, `porthole`)."""
    if paths.frozen():
        bundle = paths.app_bundle()
        if bundle is not None:  # the Mac app, typed in Terminal
            return f"open -a {shlex.quote(bundle.stem)} --args"
        name = paths.program_name()
        return f'"{name}"' if " " in name else name
    appimage = sandbox.appimage()  # its own file: the folder it runs from changes on every run
    if appimage:
        return shlex.quote(appimage)
    if command:
        return shlex.quote(command)
    return "py start.py" if os.name == "nt" else "python3 start.py"


def say(*lines: str) -> None:
    """Print to the terminal, if there is one. A windowed app (Porthole.exe, pythonw) has no sys.stdout, and
    a closed or broken console must never stop Porthole."""
    try:
        for line in lines:
            print(line, flush=True)  # print() does nothing when sys.stdout is None
    except (OSError, ValueError):
        pass


def _tolerant_output() -> None:
    """Never fail on a character the console can't show: redirected output on Windows uses the ANSI code page
    (cp1252), which has no "ə", say, in a recordings folder name. Such characters print as "?"."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def no_console() -> bool:
    """True with nowhere to print: a windowed app (Porthole.exe built without a console, or pythonw)."""
    return sys.stdout is None or sys.stderr is None


def mac_app() -> bool:
    """True for the built Porthole.app. Opened from Finder its output goes to /dev/null, not None, so
    no_console() can't tell: it shows problems on screen all the same, as with --launcher."""
    return sys.platform == "darwin" and paths.frozen()


def viewer_urls(addresses: list[str], port: int, room: str) -> list[str]:
    return [f"http://{address}:{port}/v/{room}" for address in addresses]


class Addresses:
    """This PC's current network addresses: the watcher updates them, the server reads them."""

    def __init__(self, initial: list[str]):
        self._lock = threading.Lock()
        self._list = list(initial)

    def get(self) -> list[str]:
        with self._lock:
            return list(self._list)

    def set(self, new: list[str]) -> None:
        with self._lock:
            self._list = list(new)


def refresh_addresses(addresses: Addresses, discovery, find=None) -> bool:
    """Read the addresses again. On a change, move the easy address to the new first address,
    then update the links. True if they changed. Never raises."""
    try:
        found = list((find or lan_addresses)())
    except Exception:
        return False
    old = addresses.get()
    if found == old:
        return False
    first = found[0] if found else None
    if first != (old[0] if old else None):
        try:
            discovery.set_address(first)  # goodbye on the old address, announce on the new one
        except Exception:
            pass
    addresses.set(found)
    return True


def start_address_watcher(addresses: Addresses, discovery, interval: float = ADDRESS_SECONDS, find=None, on_change=None):
    """Re-read the addresses every `interval` seconds in a background thread. Set the returned event to stop."""
    stop = threading.Event()

    def watch():
        while not stop.wait(interval):
            if refresh_addresses(addresses, discovery, find) and on_change:
                try:
                    on_change(addresses.get())
                except Exception:
                    pass

    threading.Thread(target=watch, name="porthole-addresses", daemon=True).start()
    return stop


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise argparse.ArgumentTypeError("must be this PC's IPv4 address, like 192.168.1.20") from None


def _name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("use 1 to 40 lower-case letters, digits or dashes, not starting with a dash")
    return value


def start_port_80_helper(port: int, discovery: Discovery):
    """On Windows and macOS, also listen on port 80 so plain `<name>.local` works. None if it can't."""
    if sys.platform not in _SHORT_PORT_PLATFORMS or discovery.blocked_reason:
        return None  # with --no-discovery it could only ever answer "nothing here"
    try:
        helper = make_redirect_server(80, port, discovery)
    except OSError:  # in use, or not allowed here
        return None
    discovery.short_port = 80
    threading.Thread(target=_serve_helper, args=(helper, discovery), daemon=True).start()
    return helper


def _serve_helper(helper, discovery: Discovery) -> None:
    """Run the port 80 helper. If it can't take connections any more, it lets go of port 80 quietly (the main
    port says so itself if it is stuck too), and the page goes back to the address with the port."""
    try:
        helper.serve_forever()
    except AcceptFailed:
        discovery.short_port = None
        helper.server_close()


_ERROR_ICON, _INFO_ICON = 0x10, 0x40  # Windows message box icons
_TO_FRONT = 0x10000  # MB_SETFOREGROUND: started from the Start menu there is no window, so don't open behind others
DIALOG_SECONDS = 300  # macOS: an alert nobody answers goes away after this long, so Porthole can end


def mac_alert_command(message: str, info: bool = False) -> list[str]:
    """osascript showing an alert. The title and the text are arguments, read with `on run argv`: never pasted
    into the script, so no text can change what it does. The title comes first, so a text that starts with a
    dash is never read as an option."""
    kind = "informational" if info else "critical"
    script = (
        "on run argv",
        "activate",  # in front of other windows: an app opened from Finder has none of its own
        f"display alert (item 1 of argv) message (item 2 of argv) as {kind} giving up after {DIALOG_SECONDS}",
        "end run",
    )
    command = ["/usr/bin/osascript"]
    for line in script:
        command += ["-e", line]
    return command + ["Porthole", message]


def show_dialog(message: str, info: bool = False) -> None:
    """Show a message on screen: a message box on Windows, an alert on a Mac, a desktop notification elsewhere.
    Never raises."""
    try:
        if os.name == "nt":
            import ctypes

            flags = (_INFO_ICON if info else _ERROR_ICON) | _TO_FRONT
            ctypes.windll.user32.MessageBoxW(None, message, "Porthole", flags)
        elif sys.platform == "darwin":
            subprocess.run(
                mac_alert_command(message, info),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DIALOG_SECONDS + 10,
                check=False,
            )
        else:
            subprocess.run(
                # With Porthole's own picture (the ring), from the pages' folder that every build has.
                ["notify-send", "-i", str(paths.web_dir() / "icon-512.png"), "Porthole", message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                env=_notice_environment(),
            )
    except Exception:
        pass


def _notice_environment() -> dict | None:
    """notify-send's environment; None leaves it as it is. Inside a snap, libnotify sends notices through the
    desktop portal, which on a real Ubuntu answered "The app by ID porthole_porthole could not be found", and
    nothing showed. NOTIFY_IGNORE_PORTAL makes it talk to the notification service directly, as it does outside
    a snap; the snap's `desktop` plug allows that."""
    if sandbox.kind() != "snap":
        return None
    return {**os.environ, "NOTIFY_IGNORE_PORTAL": "1"}


def report_error(message: str, launcher: bool, info: bool = False) -> None:
    """Print the message; when started from a launcher or with no console, also show it on screen.
    `info` marks a plain notice, shown without the error icon."""
    say(message)
    if launcher:
        show_dialog(message, info)


def show_notice(message: str, launcher: bool) -> None:
    """Print a notice; when started from a launcher, also show it on screen without holding up the start (a
    message box waits for OK, and Porthole serves meanwhile)."""
    say(message)
    if launcher:
        threading.Thread(target=show_dialog, args=(message, True), name="porthole-notice", daemon=True).start()


class _Parser(argparse.ArgumentParser):
    """With `dialogs` set (no console, the Mac app, or --launcher), problems with the arguments and --help show
    in a dialog: a windowed app has no sys.stderr, so they would otherwise vanish, or crash older Pythons."""

    dialogs = False

    def error(self, message):
        if self.dialogs:
            show_dialog(f"Porthole couldn't start: {message}")
            self.exit(2)
        super().error(message)

    def print_help(self, file=None):
        if self.dialogs:
            show_dialog(self.format_help(), info=True)
            return
        super().print_help(file)

    def _print_message(self, message, file=None):
        try:
            super()._print_message(message, file)
        except (AttributeError, OSError, ValueError):  # no stderr (Python 3.10 writes to None and fails)
            pass


def _direct():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a system proxy


def _running_porthole(port: int) -> dict | None:
    """What a Porthole already on this port says about itself; None if it isn't Porthole or doesn't answer
    within PING_SECONDS (one whose server is stuck, say)."""
    try:
        with _direct().open(f"http://127.0.0.1:{port}/api/ping", timeout=PING_SECONDS) as resp:
            info = json.loads(resp.read())
    except urllib.error.HTTPError as err:  # something else answered
        err.close()
        return None
    except Exception:
        return None
    return info if isinstance(info, dict) and info.get("app") == "porthole" else None


def ask_to_quit(port: int) -> bool:
    """Ask the Porthole on this port to quit, as its own Quit button does: POST /api/quit from this PC with its own
    page's origin. Its guard is unchanged: other devices aren't local, and a web page can't send another site's
    origin, so only a program on this PC can do this. True if it said yes."""
    base = f"http://127.0.0.1:{port}"
    request = urllib.request.Request(f"{base}/api/quit", data=b"", method="POST", headers={"Origin": base})
    try:
        with _direct().open(request, timeout=PING_SECONDS) as resp:
            return resp.status == 204
    except urllib.error.HTTPError as err:  # refused
        err.close()
        return False
    except Exception:
        return False


def same_folder(info: dict) -> bool:
    """Whether the running Porthole runs from this folder (an older one that doesn't say counts as the same)."""
    folder = info.get("folder")
    if not isinstance(folder, str) or not folder:
        return True
    norm = lambda path: os.path.normcase(os.path.realpath(path))  # noqa: E731
    return norm(folder) == norm(runs_from(APP_FOLDER))


class Start(NamedTuple):
    """How start_server went: a server on `port`, a Porthole already there to open instead, or an error."""

    server: object = None
    port: int = DEFAULT_PORT
    note: str | None = None  # said once it runs: the port moved, or another Porthole was stopped
    stopped_other: bool = False  # another Porthole was stopped: from a launcher the note shows on screen too
    running: dict | None = None  # a Porthole of this copy (or on an explicit --port) runs on `port`
    error: str | None = None


def _wait_for_port(build, port: int) -> Start | None:
    """Bind `port` once the Porthole asked to quit has let go of it. If another start of this copy takes it first
    (a double click while another install runs: both ask it to quit), that one is opened instead. None if the
    port is still busy after QUIT_WAIT_SECONDS."""
    deadline = time.monotonic() + QUIT_WAIT_SECONDS
    while True:
        try:
            return Start(build(port), port)
        except OSError as err:
            if err.errno not in _TAKEN:
                return None
        running = _running_porthole(port)
        if running is not None and same_folder(running):
            return Start(port=port, running=running)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def _held(port: int) -> bool:
    """Whether something listens on `port`, found by binding it as the server would: instant everywhere, where
    connecting to a port nobody listens on can take a second or two (Windows)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the server: a just-closed port is free
        try:
            probe.bind(("0.0.0.0", port))
        except OSError as err:
            return err.errno in _IN_USE
    return False


def _moved_here_earlier() -> Start | None:
    """A Porthole of this copy that an earlier start moved to a spare port, while the default port was busy.
    Only spare ports something listens on are asked."""
    for spare in SPARE_PORTS:
        if _held(spare):
            running = _running_porthole(spare)
            if running is not None and same_folder(running):
                return Start(port=spare, running=running)
    return None


def start_server(build, wanted: int | None, command: str | None = None) -> Start:
    """Bind the server with `build(port)`. `wanted` is --port, or None for the default port.

    With --port, a busy or forbidden port is an error (its hint names the command, see run_hint), and a Porthole
    already there is opened. Without it, at the default port: the same copy is opened; another copy (another
    install) is asked to quit and this one takes its port; anything else (not Porthole, no answer, can't be
    stopped, still busy, can't be used here) moves this one to the first free spare port, then to any free port.
    A copy moved to a spare port that way is opened, not started again, even once the default port is free."""
    port = DEFAULT_PORT if wanted is None else wanted
    try:
        server = build(port)
    except OSError as err:
        first = err
    else:
        moved = _moved_here_earlier() if wanted is None else None
        if moved is None:
            return Start(server, port)
        server.server_close()
        return moved
    if wanted is not None:
        running = _running_porthole(port)
        if running is not None:
            return Start(port=port, running=running)
        other = port + 1 if port < 65535 else port - 1
        if first.errno in _IN_USE:
            return Start(port=port, error=f"Port {port} is already in use. Try: {run_hint(command)} --port {other}")
        if first.errno in _ACCESS_DENIED:
            return Start(
                port=port,
                error=f"Port {port} can't be used here (reserved or needs admin rights). "
                f"Try: {run_hint(command)} --port {other}",
            )
        return Start(port=port, error=f"Couldn't start: {first}")

    if first.errno not in _TAKEN:
        return Start(port=port, error=f"Couldn't start: {first}")
    reason = f"Port {port} is busy" if first.errno in _IN_USE else f"Port {port} can't be used here"
    who, stopped = "Porthole", False
    # Ask even when the port is refused as forbidden: Windows may refuse a port another Porthole holds that way.
    running = _running_porthole(port)
    if running is not None:
        if same_folder(running):
            return Start(port=port, running=running)  # a second click on the menu entry: never stop the share
        folder = running["folder"]
        stopped = ask_to_quit(port)
        if stopped:
            waited = _wait_for_port(build, port)
            if waited is not None and waited.server is None:
                return waited  # another start of this copy got the port first
            if waited is not None:
                return waited._replace(note=f"Stopped the Porthole from {folder} and started this one.", stopped_other=True)
            reason = f"Stopped the Porthole from {folder}, but port {port} is still busy"  # Windows can hold it a while
        else:
            reason = f"Couldn't stop the Porthole from {folder}"
        who = "this one"

    last = first
    for spare in (*SPARE_PORTS, 0):
        try:
            server = build(spare)
        except OSError as err:
            last = err
            if spare and err.errno in _IN_USE:  # never ask reserved ports (whole ranges on Windows)
                running = _running_porthole(spare)
                if running is not None and same_folder(running):  # this copy moved here earlier: open it
                    return Start(port=spare, running=running)
            continue
        used = server.server_address[1] if spare == 0 else spare
        return Start(server, used, f"{reason}, so {who} uses port {used}.", stopped_other=stopped)
    return Start(port=port, error=f"Couldn't start: {last}")


def _serve(server, serving: threading.Event) -> None:
    """Serve on a thread of its own and wait for it here, so Ctrl+C (or SIGTERM, SIGHUP) interrupts this wait, not
    the server, which goes on taking connections while the pages finish (see _stop_serving). Whatever ended
    serve_forever (AcceptFailed) is raised here. `serving` is set once the server's thread has started."""
    failed = []

    def run():
        try:
            server.serve_forever()
        except BaseException as err:  # noqa: BLE001 - raised again below, in the main thread
            failed.append(err)

    thread = threading.Thread(target=run, name="porthole-server", daemon=True)
    thread.start()
    serving.set()  # only now: shutdown() would wait for ever for a thread that never started
    while thread.is_alive():
        thread.join(0.2)  # a timeout, so signals are handled while waiting on every system
    if failed:
        raise failed[0]


def _stop_serving(server, hub: Hub, recorder: Recorder, serving: threading.Event, stopper: "_Stopper") -> None:
    """Stop after Ctrl+C: tell the pages it ended, give them a moment (longer while the sharer page sends a
    recording's last piece and closes it), then stop the server. A second Ctrl+C skips the wait; after it, more
    presses do nothing (see _Stopper). A server that never started serving isn't shut down: shutdown() would wait
    for it for ever."""
    hub.end()  # tell viewers, so they don't keep retrying
    try:
        stopper.phase = _Stopper.WAITING  # inside the try: the one press that may come now is caught here
        time.sleep(STOP_SECONDS)
        deadline = time.monotonic() + RECORDING_STOP_SECONDS - STOP_SECONDS
        while recorder.busy() and time.monotonic() < deadline:
            time.sleep(0.05)
        stopper.phase = _Stopper.FINISHING
    except KeyboardInterrupt:
        pass
    if serving.is_set():
        server.shutdown()


class _Stopper:
    """Ctrl+C, SIGTERM (kill, logging out) and SIGHUP (the terminal window closed) all stop Porthole the same way,
    so viewers hear it ended, the easy address says goodbye and a recording is closed. The first one stops it (as
    KeyboardInterrupt). While the pages get their moment (WAITING), one more Ctrl+C skips it. After that, and while
    the stop finishes, more do nothing: people press Ctrl+C again when the prompt doesn't come back at once, and
    that must print no traceback or cut the stop short. A signal set to be ignored (`nohup`, or Ctrl+C for a
    program started in the background) stays ignored. Only from the main thread; restore() puts the old handlers
    back."""

    SERVING, STOPPING, WAITING, FINISHING = range(4)

    def __init__(self):
        self.phase = self.SERVING
        self._previous = {}

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for name, usual in (("SIGINT", signal.default_int_handler), ("SIGTERM", signal.SIG_DFL), ("SIGHUP", signal.SIG_DFL)):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                if signal.getsignal(number) == usual:
                    self._previous[number] = signal.signal(number, self._on_signal)
            except (OSError, ValueError):
                continue

    def _on_signal(self, signum, frame) -> None:
        if self.phase == self.SERVING:
            self.phase = self.STOPPING
            raise KeyboardInterrupt
        if self.phase == self.WAITING and signum == signal.SIGINT:
            self.phase = self.FINISHING
            raise KeyboardInterrupt

    def restore(self) -> None:
        for number, handler in self._previous.items():
            try:
                signal.signal(number, handler)
            except (OSError, ValueError, TypeError):
                pass


class _Opening:
    """Opens the sharer page on a thread of its own. Python's webbrowser waits up to 5 s for a browser it starts (for
    ever for a $BROWSER it doesn't know), and Porthole must serve meanwhile: the page, and every phone, waited."""

    def __init__(self, open_page):
        self._result = []
        self.thread = threading.Thread(target=self._run, args=(open_page,), name="porthole-open-page", daemon=True)
        self.thread.start()

    def _run(self, open_page) -> None:
        try:
            self._result.append((open_page(), None))
        except Exception as err:  # noqa: BLE001 - raised again by result(), if it is still waited for
            self._result.append((None, err))

    def result(self, wait: float):
        """What open_sharer_page() said, if it has said it within `wait` seconds; None while it is still opening."""
        self.thread.join(wait)
        if not self._result:
            return None
        value, err = self._result[0]
        if err is not None:
            raise err
        return value


def open_sharer_page(url: str, browser_tab: bool = False, on_closed=None, page_present=None, on_window=None) -> str:
    """Open the sharer page and say how: 'window' (its own app window; on_closed() runs when it closes, and a
    browser that can't start falls back to a tab), 'xdg-open' (in a sandbox, the default browser), 'tab'
    (a tab in the default browser) or 'none' (no browser could be started). On a Mac `page_present()` tells the app
    window when the page has gone (the browser outlives its window there); on_window() gets the app window, to let go
    of it or end it."""
    if sandbox.sandboxed():
        # A strict snap can't start the PC's Chrome as an app window, and webbrowser fails silently in a Flatpak.
        try:
            proc = subprocess.Popen(
                ["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError:
            pass
        else:
            threading.Thread(target=proc.wait, name="porthole-xdg-open", daemon=True).start()
            return "xdg-open"
    elif not browser_tab:
        window = appwindow.open_app_window(
            f"{url}?{WINDOW_TAG}", on_closed, on_failed=lambda: webbrowser.open(url), page_present=page_present
        )
        if window:
            if on_window:
                on_window(window)
            return "window"
    return "tab" if webbrowser.open(url) else "none"  # False: it found no browser it could start


def network_news(shown: str | None, last: str | None, now: str | None) -> str | None:
    """What the terminal says when the main viewer link goes from `shown` to `now` (None: this PC has no network).
    `last` is the last link it showed, kept while the network was gone: a Wi-Fi drop usually gives the same address
    back, and then the link people have still works. None when the link stayed."""
    if now == shown:
        return None
    if now is None:
        return "This PC has no network now, so other devices can't connect."
    if shown is None and now == last:
        return f"Back on the network. Viewers (phone or PC): {now} (same link as before)"
    return f"The network address changed. Viewers (phone or PC): {now}"


def main(argv: list[str] | None = None) -> int:
    _tolerant_output()
    argv = sys.argv[1:] if argv is None else list(argv)
    # Named as people start it (`porthole`, the AppImage file), not after start.py, which the packages run for them.
    parser = _Parser(prog=run_hint(os.environ.get(COMMAND_VARIABLE)), description="Share this PC's screen on the local network.")
    parser.dialogs = no_console() or mac_app() or "--launcher" in argv
    # None: the default port, which moves to a free one when busy; a port given here never moves.
    parser.add_argument("--port", type=_port, help=f"port to listen on (default {DEFAULT_PORT}, or a free one if that is busy)")
    parser.add_argument("--host", type=_ipv4, help="IPv4 address to put in the viewer link, if the guessed one is wrong")
    parser.add_argument("--record-dir", type=Path, help="folder for recordings (default: Videos/Screen shares; Movies on a Mac)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the sharer page automatically")
    parser.add_argument(
        "--browser-tab", action="store_true", help="open the sharer page in a browser tab instead of its own window"
    )
    parser.add_argument("--name", type=_name, default="porthole", help="easy address name, as in porthole.local (default porthole)")
    parser.add_argument("--no-discovery", action="store_true", help="never announce the easy address")
    parser.add_argument("--verbose", action="store_true", help="log each request (room code hidden) to the terminal")
    parser.add_argument("--launcher", action="store_true", help=argparse.SUPPRESS)  # started from a desktop launcher
    parser.add_argument("--install-launcher", action="store_true", help="add Porthole to the app menu")
    parser.add_argument("--uninstall-launcher", action="store_true", help="remove it from the app menu")
    args = parser.parse_args(argv)
    # The .deb's and the snap's start scripts say how they are started, for hints. Taken out of the environment,
    # so programs Porthole starts (the browser, a file manager) don't pass it on to a copy run from source.
    command = os.environ.pop(COMMAND_VARIABLE, None)
    # No terminal to read: show start-up problems on screen. A windowed Porthole.exe always has no console,
    # and the Mac app opened from Finder writes to /dev/null.
    launcher_mode = args.launcher or no_console() or mac_app()

    if args.install_launcher or args.uninstall_launcher:
        # A snap, a Flatpak or the .deb (its start script sets PORTHOLE_COMMAND) brings its own menu entry.
        packaged = sandbox.sandboxed() or command is not None
        already = "Porthole is already in your app menu."
        comes_with_it = "Porthole's menu entry comes with the app. Uninstall the app to remove it."
        if sandbox.sandboxed():  # it can't reach the user's menu folder, and its entry has a name of its own
            report_error(already if args.install_launcher else comes_with_it, launcher_mode, info=True)
            return 0
        from porthole import launcher

        try:
            if packaged:
                # The .deb: an entry an earlier --install-launcher left (AppImage, folder) has the package entry's name
                # and hides it, so either flag takes it away; the package's own entry is never touched.
                removed = launcher.remove_older_entry()
                messages = [launcher.OLDER_REMOVED if removed else already if args.install_launcher else comes_with_it]
            else:
                messages = launcher.install() if args.install_launcher else launcher.uninstall()
        except launcher.NotOnThisSystem as err:  # a Mac: no launchers there; the message says what to do
            report_error(str(err), launcher_mode, info=True)
            return 1
        except Exception as err:
            report_error(f"Couldn't change the launcher: {err}", launcher_mode)
            return 1
        report_error("\n".join(messages), launcher_mode, info=True)
        return 0

    port = DEFAULT_PORT if args.port is None else args.port  # the port actually used, once the server is bound
    sharer_url = f"http://127.0.0.1:{port}/share"
    hub = Hub()
    addresses = Addresses([args.host] if args.host else lan_addresses())

    def links() -> list[str]:  # read on every request: the address can change while Porthole runs
        return viewer_urls(addresses.get(), port, hub.room)

    first = addresses.get()[0] if addresses.get() else None
    recorder = Recorder(args.record_dir or default_record_dir())
    # Built here but never switched on at start: the sharer page turns it on.
    discovery = Discovery(args.name, first, port)
    if args.no_discovery:
        discovery.blocked_reason = DISCOVERY_OFF
    released = threading.Event()  # set once the page moved to a browser tab: the app window no longer stops Porthole
    windows = []  # the app window this copy opened and is tied to
    opening = []  # pages being opened (_Opening), waited for a moment as Porthole stops
    at_start = []  # the one opened at start, if any

    def opened_at_start() -> None:
        """The app window opened at start is in `windows` once its opening is done (it opens on a thread of its own,
        see _Opening); asked by its own page, that is long done: a second at most."""
        for page in at_start:
            page.thread.join(OPEN_ANSWER_SECONDS)

    def release_window() -> None:
        """The page in the app window has moved to a browser tab: closing the window no longer stops Porthole."""
        released.set()
        opened_at_start()
        for window in windows:
            window.untie()

    def window_back() -> None:
        """A page in the app window shares again (a tab had taken over): closing the window stops Porthole again."""
        released.clear()
        opened_at_start()
        for window in windows:
            window.retie()

    def open_in_browser() -> None:
        """The page's "Open in my browser": its own window can't see the browser's tabs, so open a tab. With no browser
        to start, the window is tied again (it stays the sharer page) and the page hears why."""
        release_window()
        try:
            # No browser to start is known at once; one still starting after a moment counts as opened, so the page
            # gets its answer instead of waiting for webbrowser (up to 5 s).
            opened = _Opening(lambda: open_sharer_page(sharer_url, browser_tab=True))
            opening.append(opened)
            if opened.result(OPEN_ANSWER_SECONDS) == "none":
                raise webbrowser.Error("no browser was found")
        except Exception:
            window_back()
            raise

    def build(at: int):
        return make_server(
            hub,
            at,
            paths.web_dir(),
            links,
            recorder=recorder,
            discovery=discovery,
            verbose=args.verbose,
            open_in_browser=open_in_browser,
            release_window=release_window,
            window_back=window_back,
        )

    try:
        start = start_server(build, args.port, command)  # can wait a few seconds for a program on the port
    except KeyboardInterrupt:
        say("\nStopped.")  # below the terminal's ^C, as when Porthole stops while serving
        return 0
    if start.running is not None:
        # Open first: a launcher's message box waits for OK, and it says "Opened it."
        # The running copy's lifetime is never tied to this new window.
        running_url = f"http://127.0.0.1:{start.port}/share"
        opened = []
        if not args.no_browser:
            open_sharer_page(running_url, args.browser_tab, on_window=opened.append)
        where = ": " + running_url if args.no_browser else None  # nothing opened: say where it is instead
        if same_folder(start.running):
            say(f"Already running{where}" if where else "Already running. Opened the sharer page.")
        else:
            folder = start.running["folder"]
            message = f"Porthole is already running from {folder}{where}" if where else f"Porthole is already running from {folder}. Opened it."
            report_error(message, launcher_mode, info=True)
        try:
            for window in opened:  # a browser that can't start opens a tab instead, from a thread that ends with this copy
                window.settle()
        except KeyboardInterrupt:
            pass  # the page is open, or opening in a session of its own: nothing to undo
        return 0
    if start.server is None:
        report_error(start.error, launcher_mode)
        return 1
    server, port = start.server, start.port
    sharer_url = f"http://127.0.0.1:{port}/share"
    discovery.port = port
    urls = links()

    if start.stopped_other:
        show_notice(start.note, launcher_mode)
    elif start.note:
        say(start.note)
    say("Porthole is running.", f"  Sharer (open on this PC): {sharer_url}")
    if urls:
        for i, url in enumerate(urls):
            label = "Viewers (phone or PC):" if i == 0 else "or:"
            say(f"  {label:<25} {url}")
    else:
        say(
            "  This PC has no network, so other devices can't connect.",
            "  Connect this PC to Wi-Fi or a network cable, or pass --host <address>.",
        )
    say(f"  Recordings go to:         {recorder.folder}")
    if sys.platform == "win32":
        # The installer adds a firewall rule; the plain folder (or running from source) makes Windows ask.
        program = "Porthole" if paths.frozen() else "Python"
        say(f"Viewers can't connect? Allow {program} through Windows Firewall when asked.")
    elif sys.platform == "darwin":
        # The Mac's firewall is off unless turned on; then it asks once for each app.
        program = "Porthole" if paths.frozen() else "Python"
        say(f"Viewers can't connect? If macOS asks whether {program} may accept incoming connections, click Allow.")
    else:
        say(f"Viewers can't connect? If Ubuntu's firewall is on, run: sudo ufw allow {port}/tcp")

    helper = None
    stuck = False  # the server stopped taking connections (see AcceptGuard)
    stopped = threading.Event()  # set once Porthole is stopping, whatever stopped it
    said = threading.Event()  # the closing app window has said why Porthole stopped

    def window_closed() -> None:
        """The app window has closed: stop the same way Quit does. After Quit (the page closes its window at once,
        before the server stops) or Ctrl+C, Porthole is stopping already."""
        if stopped.is_set() or released.is_set() or hub.ended:
            return
        said.set()
        say("Window closed. Stopped.")
        hub.end()  # tell viewers, so they don't keep retrying
        time.sleep(STOP_SECONDS)
        server.shutdown()

    shown = [urls[0] if urls else None]  # the main viewer link the terminal shows now (None: no network)
    last = [shown[0]]  # the last link it showed, kept while the network is gone

    def links_changed() -> None:
        now = links()
        link = now[0] if now else None  # only another address coming or going leaves the link, QR code and name alone
        news = network_news(shown[0], last[0], link)
        shown[0] = link
        if link:
            last[0] = link
        if news:
            say(news)

    # With --host the address is fixed; otherwise follow the PC moving to another network.
    # The watcher passes the new addresses; the terminal shows the full viewer link built from them.
    stop_watching = (
        None if args.host else start_address_watcher(addresses, discovery, on_change=lambda _: links_changed())
    )
    stopper = _Stopper()
    serving = threading.Event()
    try:
        stopper.install()
        say("Press Ctrl+C or the Quit button on the sharer page to stop.")  # once Ctrl+C is handled
        helper = start_port_80_helper(port, discovery)
        if not args.no_browser:  # opened while Porthole serves (see _Opening)
            at_start.append(
                _Opening(
                    lambda: open_sharer_page(
                        sharer_url, args.browser_tab, on_closed=window_closed, page_present=hub.sharer_active,
                        on_window=windows.append,
                    )
                )
            )  # fmt: skip
            opening.extend(at_start)
        _serve(server, serving)
        if hub.ended and not said.is_set():  # Quit on the sharer page, or another Porthole asked this one to quit
            say("Stopped.")
    except KeyboardInterrupt:  # Ctrl+C, or SIGTERM or SIGHUP (see _Stopper)
        say("\nStopped.")
        _stop_serving(server, hub, recorder, serving, stopper)
    except AcceptFailed:  # its snap removed while it runs, say: stop and free the port instead of hanging on
        stuck = True
        hub.end()
        time.sleep(STOP_SECONDS)
    finally:
        stopper.phase = _Stopper.FINISHING  # whatever stopped it: from here on Ctrl+C does nothing
        stopped.set()
        if stop_watching:
            stop_watching.set()
        server.server_close()
        discovery.stop()  # goodbye: the name leaves the network
        if helper:
            helper.shutdown()
            helper.server_close()
        recorder.close_all()
        if sys.platform == "darwin":
            # A Mac browser keeps running after its window closes: end the one Porthole started (its own profile,
            # found by its process handle), but only while it shows nothing but Porthole's page. macOS can send
            # the person's own Chrome windows and links (and "Open in my browser") to it; then it stays.
            for window in windows:
                window.close()
        for page in opening:  # a page still opening when Porthole stops: a moment, then it is left to itself
            page.thread.join(1)
        stopper.restore()
    if stuck:
        report_error(ACCEPT_STOPPED, launcher_mode)  # after the port is free: a message box waits for OK
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
