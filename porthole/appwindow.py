"""Open the sharer page in its own app window (Chrome, Edge, Chromium or Brave), so closing it can stop Porthole.

The browser runs with its own profile folder, so it is a separate browser process that exits when the
window closes, and the page's settings and notification permission are kept between runs. On a Mac the
browser keeps running after its last window closes, so there the sharer page tells instead: once it has been
gone for a few seconds, Porthole counts the window as closed. macOS can also send the person's own Chrome
windows and links to the browser Porthole started, so Porthole ends that browser only while it shows nothing
but Porthole's page, which it asks the browser over a private DevTools pipe.
"""

import json
import ntpath
import os
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePath, PurePosixPath

LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
)
# Windows: Chrome, then Edge, then Brave, each looked for in these folders in turn: (folder variable, default
# when it is missing). A 32-bit Python on 64-bit Windows gets Program Files (x86) in PROGRAMFILES, but
# ProgramW6432 always names the real 64-bit folder, where 64-bit Chrome and Brave are.
WINDOWS_BROWSERS = (
    ntpath.join("Google", "Chrome", "Application", "chrome.exe"),
    ntpath.join("Microsoft", "Edge", "Application", "msedge.exe"),
    ntpath.join("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
)
WINDOWS_FOLDERS = (
    ("PROGRAMW6432", None),
    ("PROGRAMFILES", r"C:\Program Files"),
    ("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ("LOCALAPPDATA", None),
)
MAC_BROWSERS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Chromium.app/Contents/MacOS/Chromium",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
)
WINDOW_SIZE = "960,860"
WM_CLASS = "Porthole"  # matches StartupWMClass in the desktop file, so the window groups under Porthole's icon
# A browser that exits this soon handed the page to this profile's browser that was already running (say, a
# window left from an earlier run), couldn't start, or its window was closed straight away.
EARLY_EXIT_SECONDS = 5
LOCK_WAIT_SECONDS = 2  # after a clean early exit, how long to look for a browser that took the page
LOCK_STEP_SECONDS = 0.25
WATCH_SECONDS = 1  # how often to check whether the browser that took the page still runs
# macOS: the browser keeps running after its last window closes, so the sharer page tells instead.
PAGE_GONE_SECONDS = 5  # the page gone this long means its window closed (a reload is back within a second or two)
PAGE_FIRST_SECONDS = 60  # no page this long after the window opened means it never showed
PAGE_STEP_SECONDS = 0.5  # how often to look
END_WAIT_SECONDS = 3  # after asking the browser to quit, how long before it is forced to
DEVTOOLS_SECONDS = 2  # how long to wait for the browser's answer over its DevTools pipe
# macOS: run the browser through /bin/sh so it gets --remote-debugging-pipe's pipes where Chrome looks for
# them: it reads commands from fd 3 and answers on fd 4 (Popen hands them over as fds 0 and 1). Chrome quits
# as soon as fd 3 reaches end-of-file, and Porthole's end closes when Porthole stops, so the browser also
# keeps a copy of the sending end as fd 5 (handed over as fd 2): a browser left running outlives Porthole.
# Single-digit numbers only, which the Mac's bash 3.2 and dash both take.
DEVTOOLS_SHELL = 'exec "$0" "$@" 3<&0 4>&1 5>&2 0</dev/null 1>/dev/null 2>/dev/null'


def _upper_env(environ) -> dict:
    return {key.upper(): value for key, value in (os.environ if environ is None else environ).items()}


def _windows_folders(env: dict) -> list[str]:
    """The folders to look for browsers in, in order, each once (64-bit Python sees the same folder twice)."""
    folders, seen = [], set()
    for variable, default in WINDOWS_FOLDERS:
        folder = env.get(variable) or default
        if folder and ntpath.normcase(ntpath.normpath(folder)) not in seen:
            seen.add(ntpath.normcase(ntpath.normpath(folder)))
            folders.append(folder)
    return folders


def find_browser(platform=None, which=shutil.which, exists=os.path.exists, environ=None, home=None) -> list[str] | None:
    """The command that starts a Chromium-family browser, or None if there is none."""
    platform = platform or sys.platform
    if platform == "win32":
        folders = _windows_folders(_upper_env(environ))
        for inside in WINDOWS_BROWSERS:
            for folder in folders:
                path = ntpath.join(folder, inside)
                if exists(path):
                    return [path]
        return None
    if platform == "darwin":
        user_apps = str(PurePosixPath(home or Path.home()) / "Applications")  # macOS paths, whatever runs this
        for folder in ("/Applications", user_apps):
            for inside in MAC_BROWSERS:
                path = f"{folder}/{inside}"
                if exists(path):
                    return [path]
        return None
    for name in LINUX_BROWSERS:
        path = which(name)
        if path:
            return [path]
    return None


def profile_dir(platform=None, environ=None, home=None, browser=None) -> PurePath:
    """The app window's own browser profile: under the user's config folder (Local AppData on Windows)."""
    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    home = home if home is not None else Path.home()
    kind = type(home)
    if platform == "win32":
        # This PC's own data, as Chrome keeps its profiles: in Roaming it would travel with a roaming profile.
        local = _upper_env(env).get("LOCALAPPDATA")
        base = kind(local) if local else home / "AppData" / "Local"
        return base / "Porthole" / "browser"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Porthole" / "browser"
    if browser and str(browser[0]).startswith("/snap/bin/"):
        # A snap-packaged browser may only write to its own folder, not to hidden folders in the home folder.
        snap_name = str(browser[0])[len("/snap/bin/") :].split(".")[0]
        return home / "snap" / snap_name / "common" / "porthole-browser"
    config = env.get("XDG_CONFIG_HOME")
    base = kind(config) if config else home / ".config"
    return base / "porthole" / "browser"


def app_window_command(browser: list[str], url: str, profile, platform=None) -> list[str]:
    platform = platform or sys.platform
    command = [
        *browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={WINDOW_SIZE}",
    ]
    if platform.startswith("linux"):
        command.append(f"--class={WM_CLASS}")
    return command


def lock_owner(profile, hostname: str | None = None) -> int | None:
    """The process that has this browser profile open on this PC, or None. Chromium marks a profile in use
    with a SingletonLock link to "<host name>-<process id>" (Linux and macOS)."""
    try:
        target = os.readlink(os.path.join(os.fspath(profile), "SingletonLock"))
    except (OSError, ValueError):
        return None
    host, sep, pid = target.rpartition("-")
    here = hostname if hostname is not None else socket.gethostname()
    if not sep or not host or not (pid.isascii() and pid.isdigit()) or host != here:
        return None
    return int(pid)


def process_alive(pid: int) -> bool:
    """Whether process `pid` still runs. Linux and macOS only: on Windows os.kill would end it."""
    if os.name == "nt" or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 only checks
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # runs, as another user
    except OSError:
        return False
    return True


def end_process(proc, wait: float | None = None) -> None:
    """End a process Porthole started itself, found by its handle and never by name: ask it to quit, then force
    it after `wait` seconds. Nothing happens when it has already ended. Never raises."""
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=END_WAIT_SECONDS if wait is None else wait)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:  # it ended meanwhile
        pass


class DevToolsPipe:
    """Porthole's ends of the browser's DevTools pipe (--remote-debugging-pipe): JSON messages, each ended by a
    NUL byte. Only Porthole holds them; no network port is opened. Porthole only asks which pages are open."""

    def __init__(self, send_fd: int, receive_fd: int, timeout: float | None = None):
        self._send = send_fd
        self._receive = receive_fd
        self._timeout = DEVTOOLS_SECONDS if timeout is None else timeout
        self._lock = threading.Lock()  # one command at a time; close() waits for it
        self._last_id = 0
        self._buffer = b""
        self._closed = False

    def call(self, method: str, params: dict | None = None) -> dict | None:
        """Send one command and return its result. None if the browser has gone, doesn't answer in time, or
        answers with an error. Never raises."""
        with self._lock:
            if self._closed:
                return None
            self._last_id += 1
            ident = self._last_id
            try:
                data = json.dumps({"id": ident, "method": method, "params": params or {}}).encode() + b"\0"
                while data:
                    data = data[os.write(self._send, data) :]
                deadline = time.monotonic() + self._timeout
                while True:
                    while b"\0" in self._buffer:
                        raw, self._buffer = self._buffer.split(b"\0", 1)
                        message = json.loads(raw)
                        if isinstance(message, dict) and message.get("id") == ident:
                            result = message.get("result")
                            return result if isinstance(result, dict) and "error" not in message else None
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([self._receive], [], [], left)[0]:
                        return None
                    chunk = os.read(self._receive, 65536)
                    if not chunk:
                        return None  # the browser has gone
                    self._buffer += chunk
            except (OSError, ValueError):  # a broken pipe, or something that isn't JSON
                return None

    def page_urls(self) -> list[str] | None:
        """The addresses of the pages open in the browser (tabs and windows, not its own parts), or None when
        it can't tell."""
        result = self.call("Target.getTargets")
        infos = result.get("targetInfos") if result else None
        if not isinstance(infos, list):
            return None
        urls = []
        for info in infos:
            if isinstance(info, dict) and info.get("type") == "page":
                url = info.get("url")
                if not isinstance(url, str):
                    return None
                urls.append(url)
        return urls

    def close(self) -> None:
        """Let go of Porthole's ends. A browser still running keeps running: it holds its own copy."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for fd in (self._send, self._receive):
                try:
                    os.close(fd)
                except OSError:
                    pass


def with_devtools_pipe(command: list[str]) -> tuple[list[str], dict, DevToolsPipe, tuple[int, int]]:
    """macOS: the browser command run so that the browser gets a DevTools pipe (see DEVTOOLS_SHELL). Returns the
    command, the Popen streams, Porthole's ends, and the browser's ends to close once it has started."""
    to_browser, send = os.pipe()
    try:
        receive, from_browser = os.pipe()
    except OSError:
        os.close(to_browser)
        os.close(send)
        raise
    wrapped = ["/bin/sh", "-c", DEVTOOLS_SHELL, *command, "--remote-debugging-pipe"]
    streams = {"stdin": to_browser, "stdout": from_browser, "stderr": send}
    return wrapped, streams, DevToolsPipe(send, receive), (to_browser, from_browser)


def _without_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def watch_page(
    present,
    on_gone,
    stop=None,
    clock=time.monotonic,
    sleep=None,
    gone_after: float | None = None,
    first_within: float | None = None,
    step: float | None = None,
) -> bool:
    """macOS: tell from the sharer page when the app window has closed, since the browser keeps running.

    `present()` says whether a sharer page is connected now. Once one has been, gone for `gone_after`
    seconds (a reload is back sooner) means the window closed; none within `first_within` seconds of the
    start means it never showed. Then on_gone() runs and this returns True. Returns False once `stop` is set.
    A check that fails counts as present: never stop Porthole by mistake."""
    stop = stop or threading.Event()
    sleep = sleep or stop.wait
    gone_after = PAGE_GONE_SECONDS if gone_after is None else gone_after
    first_within = PAGE_FIRST_SECONDS if first_within is None else first_within
    step = PAGE_STEP_SECONDS if step is None else step
    started = clock()
    seen = False
    away_since = None
    while not stop.is_set():
        try:
            here = bool(present())
        except Exception:
            here = True
        now = clock()
        if here:
            seen, away_since = True, None
        elif not seen:
            if now - started >= first_within:
                on_gone()
                return True
        elif away_since is None:
            away_since = now
        elif now - away_since >= gone_after:
            on_gone()
            return True
        sleep(step)
    return False


class AppWindow:
    """The app window Porthole opened: the browser process it started, found by its handle and nothing else.

    `url` is the page Porthole opened in it, and `pages()` lists the pages the browser shows now (None when it
    can't tell; on a Mac it asks the browser over its DevTools pipe)."""

    def __init__(self, proc, url: str | None = None, pages=None, devtools: DevToolsPipe | None = None):
        self.proc = proc
        self.url = url
        self._pages = pages
        self._devtools = devtools
        self._untied = threading.Event()
        self._stop = threading.Event()  # stops the page watch (macOS)
        self._closing = threading.Event()  # Porthole is stopping and ends the browser itself
        self.page_left = threading.Event()  # the sharer page has gone, so the window has closed (macOS)
        self.settled = threading.Event()  # the browser Porthole started has exited, and what that meant is done
        self.started = time.monotonic()
        self._watch_page = None  # starts a page watch (macOS), set by open_app_window

    @property
    def tied(self) -> bool:
        return not self._untied.is_set()

    def untie(self) -> None:
        """The page moved to a browser tab ("Open in my browser", "Use this one"): closing the window no longer
        stops Porthole."""
        self._untied.set()
        self._stop.set()

    def retie(self) -> None:
        """The page in this window shares again: closing it stops Porthole again. Nothing changes once nothing
        watches the window any more (`settled`); while a browser that took the page is watched, it is tied again
        though the one Porthole started has exited."""
        if not self._untied.is_set() or self.settled.is_set():
            return
        self._stop = threading.Event()  # the old page watch has stopped; a new one gets its own
        self._untied.clear()
        if self._watch_page is not None:
            self._watch_page()

    def settle(self) -> None:
        """Wait, a few seconds at most, until it is known whether the browser started. One that couldn't has
        opened a browser tab instead by then. For a copy that ends right after opening the window ("Already
        running"): the waiting thread would otherwise end with it before it could."""
        deadline = time.monotonic() + EARLY_EXIT_SECONDS + LOCK_WAIT_SECONDS + 1
        while not self.settled.wait(0.05):
            now = time.monotonic()
            if now >= deadline or (now >= self.started + EARLY_EXIT_SECONDS and self.proc.poll() is None):
                return

    def shows_only_ours(self) -> bool:
        """Whether the browser shows nothing but Porthole's own page, or nothing at all. False when it can't
        tell. On a Mac, a Dock click or a link from another app can open the person's own pages in this browser
        (macOS sends them to the running copy of Chrome), and "Open in my browser" can put its tab here too."""
        if self._pages is None or self.url is None:
            return False
        try:
            urls = self._pages()
            return isinstance(urls, list) and all(
                isinstance(url, str) and _without_fragment(url) == self.url for url in urls
            )
        except Exception:
            return False

    def close(self, wait: float | None = None) -> None:
        """Porthole is stopping: end the browser it started if that still runs (on a Mac it outlives its
        windows), but only while it shows nothing but Porthole's page. A browser the person uses too is left
        running. Never raises."""
        self._closing.set()
        self._stop.set()
        try:
            if self.proc.poll() is None and self.shows_only_ours():
                end_process(self.proc, wait)
        except Exception:
            pass
        finally:
            self.release()

    def release(self) -> None:
        """Let go of the DevTools pipe. A browser still running keeps running."""
        if self._devtools is not None:
            self._devtools.close()


def open_app_window(
    url: str,
    on_closed=None,
    on_failed=None,
    find=None,
    popen=None,
    profile=None,
    platform=None,
    clock=None,
    sleep=None,
    owner=None,
    alive=None,
    page_present=None,
    watch=None,
    pages=None,
) -> AppWindow | None:
    """Open `url` in an app window. None if there is no Chromium-family browser or it couldn't start.

    From a background thread: on_closed() once the window has closed, and on_failed() if the browser stopped
    at once with an error, so the caller can open a browser tab instead. On a Mac, `page_present()` (whether
    the sharer page is connected) tells when the window has closed, since the browser outlives it there, and
    `pages()` lists the pages the browser shows (by default asked over its DevTools pipe)."""
    browser = (find or find_browser)()
    if not browser:
        return None
    platform = platform or sys.platform
    folder = Path(profile) if profile is not None else Path(profile_dir(browser=browser))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # the browser makes it, or says why it can't
    extra = {} if platform == "win32" else {"start_new_session": True}  # Ctrl+C in the terminal leaves the window alone
    command = app_window_command(browser, url, folder, platform)
    streams = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    devtools, browser_ends = None, ()
    if platform == "darwin":
        try:
            command, streams, devtools, browser_ends = with_devtools_pipe(command)
        except OSError:
            pass  # no pipe: Porthole then can't tell what the browser shows, so it never ends it
    try:
        proc = (popen or subprocess.Popen)(command, **streams, **extra)
    except (OSError, ValueError):
        if devtools is not None:
            devtools.close()
        return None
    finally:
        for fd in browser_ends:  # the browser has its copies now
            os.close(fd)
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    owner = owner or lock_owner
    alive = alive or process_alive
    if pages is None and devtools is not None:
        pages = devtools.page_urls
    window = AppWindow(proc, url, pages, devtools)
    reported = threading.Lock()

    def closed() -> None:
        """Report the window closed, once, unless the page moved to a browser tab."""
        if on_closed and window.tied and reported.acquire(blocking=False):
            on_closed()

    def other_browser() -> int | None:
        """A running browser, other than the one just started, that holds Porthole's profile."""
        pid = owner(folder)
        return pid if pid is not None and pid != getattr(proc, "pid", None) and alive(pid) else None

    def wait():
        try:
            follow()
        finally:
            window.settled.set()
            window._stop.set()  # the window's fate is known: no more page watching
            window.release()  # the browser Porthole started has exited: nothing left to ask

    def follow():
        started = clock()
        try:
            code = proc.wait()
        except Exception:
            code = None
        if (not window.tied or window._closing.is_set()) and not window.page_left.is_set():
            # Porthole let go of the window (the page is in a browser tab now) or is stopping and ended the
            # browser itself: a quick exit then is no failed start, so no tab opens, and nothing is reported.
            return
        if window.page_left.is_set() or clock() - started >= EARLY_EXIT_SECONDS:
            closed()  # the window was open for a while and has closed (or its page had gone, on a Mac)
            return
        if platform == "win32":
            # No lock to look at. A clean exit handed the page to a window already open, which can't be
            # watched: Quit stops Porthole then.
            if code != 0 and on_failed:
                on_failed()
            return
        taker = other_browser()
        if taker is None and code == 0:
            # A clean exit: the window was closed at once, or a launcher script started the browser and
            # left, and that browser locks the profile a moment later.
            waited = 0.0
            while taker is None and waited < LOCK_WAIT_SECONDS:
                sleep(LOCK_STEP_SECONDS)
                waited += LOCK_STEP_SECONDS
                taker = other_browser()
        if taker is not None:
            if not on_closed:
                return
            # Stops when that browser's last window closes; on a Mac, where it outlives its windows, when the
            # page has gone. That browser isn't the one Porthole started, so it is left running.
            while alive(taker) and not window.page_left.is_set():
                sleep(WATCH_SECONDS)
            closed()
        elif code != 0:
            if on_failed:
                on_failed()
        else:
            closed()

    def page_gone() -> None:
        """macOS: the sharer page has gone, so its window has closed. End the browser Porthole started if it
        shows nothing else (the waiting thread then reports the window closed); a browser the person uses too
        is left running, and the window is reported closed from here."""
        if not window.tied:
            return
        window.page_left.set()
        if window.shows_only_ours():
            end_process(proc)
        else:
            closed()

    def start_page_watch() -> None:
        threading.Thread(
            target=watch or watch_page,
            args=(page_present, page_gone),
            kwargs={"stop": window._stop},
            name="porthole-page-watch",
            daemon=True,
        ).start()

    # Always wait, so the finished browser process is collected.
    threading.Thread(target=wait, name="porthole-app-window", daemon=True).start()
    if platform == "darwin" and on_closed and page_present is not None:
        window._watch_page = start_page_watch
        start_page_watch()
    return window
