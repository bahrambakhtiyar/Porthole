"""HTTP server: serves the pages, relays set-up messages through the hub, and runs
the sharer's local-only actions (quit, recording)."""

import errno
import html
import ipaddress
import json
import os
import re
import selectors
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from . import paths, qr, sandbox
from .discovery import Discovery
from .hub import Hub, HubError, device_label
from .recorder import Recorder, RecorderError, folder_problem, open_folder

MAX_BODY = 65536
MAX_CHUNK = 16 * 1024 * 1024
PING_SECONDS = 15  # keep-alive event on quiet streams; the pages reconnect after 40 s of silence
ORPHAN_SECONDS = 10  # an open recording is closed when the page that made it isn't back within this time
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,64}")  # a sharer page's token (fullmatch: `$` would let a final "\n" through)
# A device's key on the sharer page: its viewer token with the address it was allowed from ("token@address", an
# approval) or alone (a denial), or its viewer id ("v-" and 8 hex digits) when it has no token.
_DEVICE_KEY = re.compile(r"[A-Za-z0-9_-]{8,64}(?:@[0-9A-Fa-f.:]{2,45})?")
_SWITCHES = ("ask", "easy", "smooth")  # the sharer page's switches, kept for this run with its approvals
REQUEST_TIMEOUT = 30
RETRY_SECONDS = 5  # how often the "nothing shared yet" page tries again
ACCEPT_PAUSE_FIRST = 0.01  # the pause after a failed accept(), doubled after each failure in a row...
ACCEPT_PAUSE_MAX = 0.5  # ...up to this
ACCEPT_GAP_SECONDS = 1.0  # failures further apart than this (nothing was waiting in between) aren't in a row
ACCEPT_STOP_SECONDS = 5.0  # accept() failing without a break for this long: the server can't take connections
# accept() errors that pause like the rest but never stop the server: errors about one connection, which is dropped
# from the queue (a phone that gave up while waiting, say), and running short of file handles or memory, which a
# device on the network can cause for as long as it likes by holding connections open, and which ends when they
# close. 10004, 10024 and 10055 are Windows' WSAEINTR, WSAEMFILE and WSAENOBUFS.
_PASSING_ACCEPT_NAMES = (
    "ECONNABORTED", "ECONNRESET", "EPROTO", "EINTR", "EAGAIN", "EWOULDBLOCK", "ETIMEDOUT",
    "ENETDOWN", "ENETUNREACH", "EHOSTDOWN", "EHOSTUNREACH", "ENONET", "ENOPROTOOPT", "EOPNOTSUPP",
    "EMFILE", "ENFILE", "ENOBUFS", "ENOMEM",
)  # fmt: skip
_PASSING_ACCEPT_ERRORS = frozenset(
    {getattr(errno, name) for name in _PASSING_ACCEPT_NAMES if hasattr(errno, name)} | {10004, 10024, 10055}
)


def _split(target: str):
    """The request target split up; None when it can't be read (`GET http://[::1/x`)."""
    try:
        return urlsplit(target)
    except ValueError:
        return None


def runs_from(folder: Path) -> str:
    """Where this copy of Porthole runs from, as /api/ping reports it. Run as an AppImage, that is the
    AppImage file ($APPIMAGE), because its mount folder changes on every run."""
    return sandbox.appimage(folder) or str(folder)


APP_FOLDER = runs_from(paths.app_folder())  # reported by /api/ping

_STATIC = {
    "share.js": "text/javascript; charset=utf-8",
    "view.js": "text/javascript; charset=utf-8",
    "signal.js": "text/javascript; charset=utf-8",
    "quality.js": "text/javascript; charset=utf-8",
    "zoom.js": "text/javascript; charset=utf-8",
    "settings.js": "text/javascript; charset=utf-8",
    "share-helpers.js": "text/javascript; charset=utf-8",
    "theme.js": "text/javascript; charset=utf-8",
    "theme-boot.js": "text/javascript; charset=utf-8",
    "view-helpers.js": "text/javascript; charset=utf-8",
    "retry-page.js": "text/javascript; charset=utf-8",  # the "Nothing shared yet" page's quiet check
    "style.css": "text/css; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "manifest.json": "application/manifest+json",  # the viewer's home-screen shortcut
    "icon-180.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}
# The message pages' style sheet. A page shown to a Host the check refuses ("Use the link") asks for it by that same
# Host, so it is served whatever the Host: it is a public file, so a page by a name a stranger controls gains nothing.
_STYLE_PATH = "/static/style.css"
_SECRET_PARAMS = ("room", "client")  # hidden in --verbose logs: the room code and a device's approval token
MASK = "****"
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
}


def _token(value: str) -> str:
    """A sharer page's token as it sent it, or "" (no token, or not one)."""
    return value if _TOKEN.fullmatch(value) else ""


def plain_cause(err: BaseException) -> str:
    """Why something Porthole tried to start failed, in a few plain words the page puts after its own ("Couldn't open
    your browser: ..."): never Python's error numbers, and never the page's words again."""
    if isinstance(err, webbrowser.Error):  # Porthole's own when no browser started, or Python's "could not locate ..."
        return "no browser was found"
    if isinstance(err, FileNotFoundError) and err.filename:
        return f"{Path(str(err.filename)).name} is missing"
    if isinstance(err, PermissionError):
        return "Porthole isn't allowed to start it"
    if isinstance(err, OSError) and err.strerror:
        return err.strerror[:1].lower() + err.strerror[1:]
    return "something went wrong"


def _plain_ip(ip: str) -> str:
    return ip[7:] if ip.startswith("::ffff:") else ip


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(_plain_ip(ip)).is_loopback
    except ValueError:
        return False


def _host_name(host: str | None) -> str | None:
    """The name in a Host header, lower-cased, without the port or IPv6 brackets."""
    if not host:
        return None
    name = host.strip()
    if name.startswith("["):
        name = name[1 : name.find("]")] if "]" in name else ""
    elif name.count(":") == 1:
        name = name.split(":")[0]
    return name.lower() or None


def _host_kind(host: str | None, easy_host: str | None = None) -> str | None:
    """"ip" for `localhost` or an IP literal (a DNS-rebinding attack needs a domain name),
    "easy" for the announced `.local` name, None for anything else."""
    name = _host_name(host)
    if name is None:
        return None
    if name == "localhost":
        return "ip"
    try:
        ipaddress.ip_address(name)
        return "ip"
    except ValueError:
        pass
    if easy_host and name.rstrip(".") == easy_host.lower():
        return "easy"
    return None


def _easy_path_ok(method: str, path: str, query: dict) -> bool:
    """The easy address only reaches the viewer side: never the sharer page or /api/*."""
    if method == "POST":
        return path == "/signal"
    if path == "/" or path.startswith("/v/") or path.startswith("/static/"):
        return True
    return path == "/events" and query.get("role", [""])[0] == "viewer"


def _host_ok(host: str | None, discovery: Discovery | None, method: str, path: str, query: dict, client: str = "") -> bool:
    """IP literals and localhost reach everything; the easy address only the viewer side, and after it was turned off,
    while it stays for the devices watching by it, only from those devices (`client`, the device's address)."""
    kind = _host_kind(host, _easy_host(discovery))
    return kind == "ip" or (kind == "easy" and _easy_path_ok(method, path, query) and discovery.admits(client))


def _easy_host(discovery: Discovery | None) -> str | None:
    return discovery.state()["host"] if discovery else None


def _redirect_host(host: str | None) -> str:
    name = _host_name(host) or ""
    return f"[{name}]" if ":" in name else name


def _message_page(title: str, text: str, refresh: int | None = None) -> bytes:
    """A short page with one message. With `refresh` it checks again every so many seconds and goes on
    once the answer changes: quietly, with a script (a reload made screen readers read the page out again
    each time); browsers without scripts reload it."""
    seconds = int(refresh) if refresh else 0
    check = (
        f'<noscript><meta http-equiv="refresh" content="{seconds}"></noscript>'
        '<script type="module" src="/static/retry-page.js"></script>'
        if seconds
        else ""
    )
    retry = f' data-retry="{seconds}"' if seconds else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">{check}'
        f'<title>{html.escape(title)} · Porthole</title><link rel="stylesheet" href="/static/style.css"></head>'
        f'<body class="message-page"{retry}><main class="message"><h1>{html.escape(title)}</h1>'
        f"<p>{text}</p></main></body></html>"
    ).encode()


def _use_the_link() -> bytes:
    """For a page opened by a name the Host check refuses: another name for this PC (its own `mypc.local`, a router's
    name for it) or a name that isn't the easy address. Refused all the same (a name a stranger controls could point
    at this PC: DNS rebinding), with words a phone user can act on."""
    return _message_page("Use the link", "Open the link or scan the QR code shown on the sharing PC.")


def _read_choices(body) -> dict | None:
    """The approvals and switches in a sharer page's report, as Porthole keeps them; None if they aren't well formed.
    Settings other than the three switches are dropped, and so is a device key that isn't one: the page sends every
    key in every report, so refusing the report for one key would refuse all later ones too. A key left out only
    means that device is asked about again."""
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        return None
    settings = body.get("settings")
    if not isinstance(settings, dict) or not all(isinstance(settings.get(name), bool) for name in _SWITCHES):
        return None
    lists = {}
    for name in ("approved", "denied"):
        keys = body.get(name)
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            return None
        lists[name] = [key for key in keys if _DEVICE_KEY.fullmatch(key)]
    return {"settings": {name: settings[name] for name in _SWITCHES}, **lists}


def mask_path(path: str, room: str) -> str:
    """A request path for the log, with the room code (and device tokens) replaced by ****."""
    try:
        parts = urlsplit(path)
    except ValueError:
        parts = None
    if parts is not None:
        route = unquote(parts.path)
        route = MASK.join(route.split(room)) if room else route
        if route.startswith("/v/"):
            route = "/v/" + MASK
        query = parts.query
        if query:
            pairs = parse_qsl(query, keep_blank_values=True)
            query = urlencode([(key, MASK if key in _SECRET_PARAMS else value) for key, value in pairs], safe="*")
        path = urlunsplit(("", "", route, query, ""))
    return MASK.join(path.split(room)) if room else path


def _tune_stream_socket(sock: socket.socket) -> None:
    """Keep-alive probes so a phone that vanished (no goodbye) leaves within about half a minute."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    # TCP_KEEPALIVE is macOS's name for TCP_KEEPIDLE (seconds of quiet before the first probe).
    for name, value in (
        ("TCP_KEEPIDLE", 10),
        ("TCP_KEEPINTVL", 5),
        ("TCP_KEEPCNT", 3),
        ("TCP_USER_TIMEOUT", 20000),
        ("TCP_KEEPALIVE", 10),
    ):
        option = getattr(socket, name, None)
        if option is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass
    if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 5000))
        except (OSError, AttributeError):
            pass


class AcceptFailed(Exception):
    """accept() has kept failing: this server can't take connections any more (its snap removed under it, say)."""


class AcceptGuard:
    """Pauses after each failed accept(), longer after each failure in a row. The listening socket stays readable
    while accept() fails, so without a pause the server loop would try again at once and spin the CPU. Failures
    that go on without a break for ACCEPT_STOP_SECONDS raise AcceptFailed, so Porthole stops and frees its port;
    passing ones (about one connection, or a shortage of file handles that ends when connections close) never do."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._sleep = sleep
        self.succeeded()

    def succeeded(self) -> None:
        self._pause = ACCEPT_PAUSE_FIRST
        self._last: float | None = None  # when the pause after the last failure ended
        self._stuck_since: float | None = None  # first failure of the current stretch (passing errors left out)

    def failed(self, err: OSError) -> None:
        now = self._clock()
        if self._last is not None and now - self._last > ACCEPT_GAP_SECONDS:
            self.succeeded()  # nothing was waiting in between, so these aren't failures in a row
        if err.errno not in _PASSING_ACCEPT_ERRORS:
            if self._stuck_since is None:
                self._stuck_since = now
            elif now - self._stuck_since >= ACCEPT_STOP_SECONDS:
                raise AcceptFailed(str(err)) from err
        self._sleep(self._pause)
        self._pause = min(self._pause * 2, ACCEPT_PAUSE_MAX)
        self._last = self._clock()


class LanServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.accept_guard = AcceptGuard()
        super().__init__(*args, **kwargs)

    def get_request(self):
        """Accept a connection. A failure pauses (see AcceptGuard) and is passed on, so socketserver skips it as
        before; AcceptFailed is not an OSError, so it ends serve_forever."""
        try:
            request = super().get_request()
        except OSError as err:
            self.accept_guard.failed(err)
            raise
        self.accept_guard.succeeded()
        return request

    def handle_error(self, request, client_address):
        """A client that dropped its connection or gave up on a reply (a phone leaving the Wi-Fi, a browser
        cancelling) is normal on a LAN: say nothing, as a traceback looks as if Porthole crashed. Anything else is a
        bug, printed as before."""
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        if os.name == "nt":
            # On Windows SO_REUSEADDR lets a second program take a port that is already in use.
            self.allow_reuse_address = False
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind would also look up the address's full name (socket.getfqdn), which nothing here
        # uses and which can hang for about 30 s on a Mac: bind as TCPServer does, then set the two fields.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class _QrCache:
    """The QR code for the first viewer link, drawn again only when the address list changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._key: tuple[str, ...] | None = None
        self._svg = ""

    def get(self, urls: list[str]) -> str:
        key = tuple(urls)
        with self._lock:
            if key != self._key:
                self._svg = qr.to_svg(qr.encode(urls[0].encode())) if urls else ""
                self._key = key
            return self._svg


class _OrphanWatch:
    """Closes a recording once the sharer page that started it has gone and isn't back in time. A page is known by
    the token it sends with its event stream and its recording (`page`): after a network blip it comes back with the
    same one and carries on; a reloaded or new page has a new one, and can never finish the old recording."""

    def __init__(self, recorder: Recorder | None, delay: float):
        self._recorder = recorder
        self._delay = delay
        self._lock = threading.Lock()
        self._arrivals: dict[str, int] = {}  # page token -> event streams it opened so far
        self._open: dict[str, int] = {}  # page token -> its event streams open now

    def sharer_arrived(self, page: str) -> None:
        with self._lock:
            self._arrivals[page] = self._arrivals.get(page, 0) + 1
            self._open[page] = self._open.get(page, 0) + 1

    def sharer_left(self, page: str) -> None:
        with self._lock:
            self._open[page] -= 1
            if not self._open[page]:
                del self._open[page]
            seen = self._arrivals[page]
        if self._recorder is None:
            return
        timer = threading.Timer(self._delay, self._check, args=(page, seen))
        timer.daemon = True
        timer.start()

    def _check(self, page: str, seen: int) -> None:
        with self._lock:
            # A new stream came and went meanwhile: its own leaving started a wait of its own.
            came_back = page in self._open or self._arrivals[page] != seen
        if not came_back:
            self._recorder.close_owned_by(page)


def make_server(
    hub: Hub,
    port: int,
    web_dir: Path,
    viewer_urls: list[str] | Callable[[], list[str]],
    is_local: Callable[[str], bool] = is_loopback,
    recorder: Recorder | None = None,
    discovery: Discovery | None = None,
    orphan_delay: float = ORPHAN_SECONDS,
    verbose: bool = False,
    open_in_browser: Callable[[], None] | None = None,
    release_window: Callable[[], None] | None = None,
    window_back: Callable[[], None] | None = None,
) -> ThreadingHTTPServer:
    """`viewer_urls` is the list of viewer links, or a function giving the current list
    (the PC's address can change while Porthole runs). `verbose` logs each request to stderr.
    `open_in_browser` opens the sharer page in a browser tab, for the page's "Open in my browser".
    `release_window` lets go of Porthole's own window once its page has moved to a browser tab ("Use this one"),
    so closing the window no longer stops Porthole, and `window_back` ties it again once a page in that window shares
    again."""
    qr_cache = _QrCache()
    orphans = _OrphanWatch(recorder, orphan_delay)

    def current_urls() -> list[str]:
        return list(viewer_urls() if callable(viewer_urls) else viewer_urls)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LanShare"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

        def log_message(self, format, *args):  # keep the terminal quiet
            pass

        def log_request(self, code="-", size="-"):
            """--verbose: time, client, method, path (room code hidden) and status, one line each."""
            if not verbose or sys.stderr is None:  # no console when started from a launcher
                return
            status = getattr(code, "value", code)
            path = mask_path(getattr(self, "path", "") or "", hub.room)
            line = f"{time.strftime('%H:%M:%S')} {self.client_ip()} {getattr(self, 'command', None) or '-'} {path} {status}"
            line = "".join(c if c.isprintable() else "?" for c in line)  # no terminal control codes
            try:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass

        # --- helpers ---

        def client_ip(self) -> str:
            return _plain_ip(self.client_address[0])

        def local(self) -> bool:
            return is_local(self.client_ip())

        def cross_site(self) -> bool:
            return self.headers.get("Sec-Fetch-Site") == "cross-site"

        def local_api_ok(self) -> bool:
            """Local client and a same-origin page, so other websites can't trigger actions."""
            port = self.port()
            origin = self.headers.get("Origin")
            if origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return False
            return self.local() and self.headers.get("Sec-Fetch-Site") in (None, "same-origin")

        def port(self) -> int:
            return self.server.server_address[1]

        def host_ok(self, method: str, path: str, query: dict) -> bool:
            return _host_ok(self.headers.get("Host"), discovery, method, path, query, self.client_ip())

        def by_name(self) -> bool:
            """Whether this request came by the easy address (`porthole.local`), not by the PC's address."""
            return _host_kind(self.headers.get("Host"), _easy_host(discovery)) == "easy"

        def read_json(self):
            """The request body as JSON; a sentinel `...` after sending an error."""
            raw = self.read_body(MAX_BODY)
            return ... if raw is None else self.parse_json(raw)

        def send(self, status: int, body: bytes = b"", content_type: str = "text/html; charset=utf-8", extra=None):
            self.send_response(status)
            for key, value in {**_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            if status != 204:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, status: int, data) -> None:
            self.send(status, json.dumps(data).encode(), "application/json")

        def send_file(self, path: Path, content_type: str) -> None:
            try:
                body = path.read_bytes()
            except OSError:
                return self.send(404, _message_page("Not found", "This file doesn't exist."))
            self.send(200, body, content_type)

        def read_body(self, limit: int) -> bytes | None:
            """The request body, or None after sending an error."""
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length > limit:
                self.close_connection = True
                self.send_json(413, {"error": "too big"})
                return None
            if length < 0:
                self.send_json(400, {"error": "bad length"})
                return None
            data = self.rfile.read(length)
            if len(data) != length:  # the sender went away mid-body: never keep half a piece
                self.close_connection = True
                self.send_json(400, {"error": "incomplete body"})
                return None
            return data

        @staticmethod
        def parse_json(raw: bytes):
            try:
                return json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                return None

        # --- GET ---

        def do_GET(self):
            url = _split(self.path)
            if url is None:
                return self.send(400, _message_page("Bad request", "Open this page by its IP address."))
            path = url.path
            query = parse_qs(url.query)
            if path == _STYLE_PATH:
                return self.send_file(web_dir / "style.css", _STATIC["style.css"])
            if not self.host_ok("GET", path, query):
                return self.send(400, _use_the_link())

            if path == "/":
                if self.local():
                    return self.send(302, extra={"Location": "/share"})
                # Typing the easy address opens the share, but only while something is being shared.
                if self.easy_on():
                    return self.to_current_room()
                return self.send(404, _message_page("Nothing here", "Ask the person sharing for the link."))
            if path == "/share":
                if not self.local():
                    address = f"http://127.0.0.1:{self.port()}/share"
                    return self.send(
                        403,
                        _message_page(
                            "Open the sharer page on this PC",
                            f"Screen sharing only works when you open <b>{address}</b> on the PC that runs Porthole.",
                        ),
                    )
                if self.cross_site():
                    return self.send(403, _message_page("Blocked", "Open the sharer page directly, not from another website."))
                return self.send_file(web_dir / "share.html", "text/html; charset=utf-8")
            if path.startswith("/v/"):
                if hub.check_room(path[3:]):
                    return self.send_file(web_dir / "view.html", "text/html; charset=utf-8")
                if self.easy_on():
                    # A saved link or home-screen shortcut from an earlier run keeps working.
                    return self.to_current_room()
                return self.send(
                    404,
                    _message_page(
                        # A link from an earlier run, or a mistyped one: Porthole can't tell them apart.
                        "This link doesn't work",
                        "It's from an earlier share or has a typo. Check it against the link on the sharing PC, or scan"
                        " the QR code.",
                    ),
                )
            if path.startswith("/static/"):
                name = path[len("/static/") :]
                if name in _STATIC:
                    return self.send_file(web_dir / name, _STATIC[name])
                return self.send(404, _message_page("Not found", "This file doesn't exist."))
            if path == "/api/info":
                if not self.local() or self.cross_site():
                    return self.send_json(403, {"error": "local only"})
                urls = current_urls()
                return self.send_json(
                    200,
                    {
                        "room": hub.room,
                        "viewer_urls": urls,
                        "qr_svg": qr_cache.get(urls),
                        "sharer_active": hub.sharer_active(),
                        "sharing": hub.sharing,
                        "easy": discovery.state() if discovery else None,
                        "refusals": hub.refusals(),
                        "choices": hub.choices(),  # this run's approvals and switches (see choices())
                    },
                )
            if path == "/api/ping":
                if not self.local():
                    return self.send_json(403, {"error": "local only"})
                return self.send_json(200, {"app": "porthole", "folder": APP_FOLDER})
            if path == "/events":
                return self.events(query)
            return self.send(404, _message_page("Not found", "Ask the person sharing for the link."))

        def easy_on(self) -> bool:
            return bool(discovery and discovery.state()["enabled"])

        def to_current_room(self) -> None:
            """While sharing, go to the current room; until then a page that tries again by itself,
            so the room code stays private until something is shared."""
            if hub.sharing:
                return self.send(302, extra={"Location": f"/v/{hub.room}"})
            if discovery is not None and self.by_name():
                discovery.looker_seen(self.client_ip())  # turned off, the name is answered a moment for it
            # The sharer page shows the address before Start sharing: wait here, never in a dead end.
            return self.send(
                503,
                _message_page(
                    "Nothing shared yet",
                    "Keep this page open. The screen will show here once sharing starts.",
                    refresh=RETRY_SECONDS,
                ),
                extra={"Retry-After": str(RETRY_SECONDS)},
            )

        def events(self, query) -> None:
            room = query.get("room", [""])[0]
            role = query.get("role", [""])[0]
            if not hub.check_room(room):
                return self.send_json(403, {"error": "wrong room"})
            if role not in ("sharer", "viewer"):
                return self.send_json(400, {"error": "unknown role"})
            if role == "sharer" and (not self.local() or self.cross_site()):
                return self.send_json(403, {"error": "local only"})
            def flag(name: str) -> bool:
                return query.get(name, ["0"])[0] == "1"

            page = _token(query.get("page", [""])[0])  # which sharer page this is (see _OrphanWatch)
            # A viewer that reconnects names its connection before: the browser itself sends the id of the last event
            # it got (the hello's) when it reconnects on its own; the page puts it in the address when it opens a new
            # stream or after a reload. The browser's is the newer one. The hub decides whether it counts.
            previous = self.headers.get("Last-Event-ID") or query.get("prev", [""])[0]
            try:
                peer = hub.connect(
                    role,
                    self.client_ip(),
                    _plain_ip(self.connection.getsockname()[0]),
                    device_label(self.headers.get("User-Agent", ""), touch=flag("touch")),
                    query.get("client", [""])[0],
                    takeover=flag("takeover"),
                    in_window=flag("window"),
                    previous=previous if role == "viewer" else "",
                )
            except HubError as err:
                if err.refusal:
                    return self.refuse(err.refusal)
                return self.send_json(err.status, {"error": err.reason})

            if role == "sharer":
                orphans.sharer_arrived(page)
                if flag("window") and window_back is not None:
                    window_back()
            # A viewer that came by the name: turned off, the name stays for it while it watches (see Discovery).
            guest = role == "viewer" and discovery is not None and self.by_name()
            if guest:
                discovery.guest_arrived(self.client_ip())
            selector = None
            try:
                # Inside the try: out of file handles, this fails, and the peer must still be removed.
                selector = selectors.DefaultSelector()
                self.start_stream()
                _tune_stream_socket(self.connection)
                selector.register(self.connection, selectors.EVENT_READ)
                last_sent = time.monotonic()
                while True:
                    wait = min(1.0, max(0.0, last_sent + PING_SECONDS - time.monotonic()))
                    msg = hub.next_message(peer.id, timeout=wait)
                    if msg is None:
                        # Notice a closed tab within about a second, not at the next write.
                        if self.client_gone(selector):
                            break
                        if time.monotonic() - last_sent < PING_SECONDS:
                            continue
                        msg = {"type": "ping"}  # a data event, so the page can tell the stream is alive
                    # A viewer's hello names its id as the event's id, which the browser sends back as Last-Event-ID
                    # when it reconnects by itself (see previous above). Ids are "v-" and hex digits: no line breaks.
                    event_id = f"id: {peer.id}\n" if role == "viewer" and msg.get("type") == "hello" else ""
                    self.wfile.write(f"{event_id}data: {json.dumps(msg)}\n\n".encode())
                    self.wfile.flush()
                    last_sent = time.monotonic()
                    if msg.get("type") == "replaced":
                        break
                    if guest and msg.get("type") in ("denied", "removed"):
                        guest = False  # turned away by the sharer: not one of the devices watching by the name
                        discovery.guest_left(self.client_ip(), turned_away=True)
            except (OSError, ValueError):
                pass
            finally:
                if selector is not None:
                    selector.close()
                hub.disconnect(peer.id)
                if role == "sharer":
                    orphans.sharer_left(page)
                if guest:
                    discovery.guest_left(self.client_ip())

        def start_stream(self) -> None:
            self.send_response(200)
            for key, value in _HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def refuse(self, reason: str) -> None:
            """Tell a refused viewer why in one event, then close (a bare error status says nothing)."""
            try:
                self.start_stream()
                self.wfile.write(f"data: {json.dumps({'type': 'refused', 'reason': reason})}\n\n".encode())
                self.wfile.flush()
            except OSError:
                pass

        def client_gone(self, selector: selectors.BaseSelector) -> bool:
            """An event-stream client never sends after its request, so readable means closed."""
            try:
                if not selector.select(0):
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

        # --- POST ---

        def do_POST(self):
            url = _split(self.path)
            if url is None:
                return self.send_json(400, {"error": "bad request"})
            query = parse_qs(url.query)
            if not self.host_ok("POST", url.path, query):
                return self.send_json(400, {"error": "bad host"})
            if url.path == "/signal":
                return self.signal(query)
            if url.path.startswith("/api/"):
                if not self.local_api_ok():
                    return self.send_json(403, {"error": "only the sharer page on this PC can do that"})
                # The sharer page says which run it belongs to: one left open from an earlier run (Porthole killed and
                # started again on the same port) must not stop or change this one.
                page_room = self.headers.get("X-Porthole-Room")
                if page_room is not None and not hub.check_room(page_room):
                    return self.send_json(409, {"error": "this page is from an earlier run of Porthole: reload it"})
                if url.path == "/api/quit":
                    hub.end()
                    self.send(204)
                    # A moment for the event streams to deliver "ended" before the server stops.
                    threading.Timer(0.5, self.server.shutdown).start()
                    return None
                if url.path == "/api/status":
                    return self.status()
                if url.path == "/api/choices":
                    return self.choices()
                if url.path == "/api/open-in-browser":
                    return self.open_in_browser()
                if url.path == "/api/release-window":
                    if release_window is None:
                        return self.send_json(503, {"error": "not available"})
                    release_window()
                    return self.send(204)
                if url.path == "/api/discovery":
                    return self.easy_address()
                if url.path.startswith("/api/recording/") or url.path == "/api/open-folder":
                    return self.recording(url.path, query)
            return self.send_json(404, {"error": "not found"})

        def status(self) -> None:
            """The sharer page says whether its screen is being shared."""
            body = self.read_json()
            if body is ...:
                return None
            sharing = body.get("sharing") if isinstance(body, dict) else None
            if not isinstance(sharing, bool):
                return self.send_json(400, {"error": "sharing must be true or false"})
            sharer_id = body.get("id")
            hub.set_sharing(sharing, sharer_id if isinstance(sharer_id, str) else None)
            return self.send(204)

        def choices(self) -> None:
            """The sharer page reports this run's approvals and switches after each change. Porthole keeps them for the
            page that takes over, which may be in another browser with other saved settings (Open in my browser). Only
            the three switches and device keys as the page knows them; only from the page that is the sharer now."""
            body = self.read_json()
            if body is ...:
                return None
            kept = _read_choices(body)
            if kept is None:
                return self.send_json(400, {"error": "choices must be the switches and lists of device keys"})
            sharer_id = body.get("id")
            if not isinstance(sharer_id, str) or not hub.keep_choices(kept, sharer_id):
                return self.send_json(409, {"error": "only the page that shares keeps its choices"})
            return self.send(204)

        def open_in_browser(self) -> None:
            """Porthole's own window can't see the browser's tabs, so the page asks for a normal browser tab."""
            if open_in_browser is None:
                return self.send_json(503, {"error": "not available"})
            try:
                open_in_browser()
            except Exception as err:  # no browser found (webbrowser.Error), or one that couldn't start
                return self.send_json(500, {"error": plain_cause(err)})
            return self.send(204)

        def easy_address(self) -> None:
            if discovery is None:
                return self.send_json(503, {"error": "the easy address is not available"})
            body = self.read_json()
            if body is ...:
                return None
            enabled = body.get("enabled") if isinstance(body, dict) else None
            if not isinstance(enabled, bool):
                return self.send_json(400, {"error": "enabled must be true or false"})
            return self.send_json(200, discovery.set_enabled(enabled))

        def signal(self, query) -> None:
            raw = self.read_body(MAX_BODY)
            if raw is None:
                return None
            if not hub.check_room(query.get("room", [""])[0]):
                return self.send_json(403, {"error": "wrong room"})
            body = self.parse_json(raw)
            if not isinstance(body, dict):
                return self.send_json(400, {"error": "body must be a JSON object"})
            peer_id = query.get("id", [""])[0]
            if hub.role_of(peer_id) == "sharer" and not self.local():
                return self.send_json(403, {"error": "local only"})
            try:
                hub.send(peer_id, body)
            except HubError as err:
                return self.send_json(err.status, {"error": err.reason})
            return self.send(204)

        def recording(self, path: str, query) -> None:
            if recorder is None:
                return self.send_json(503, {"error": "recording is not available"})
            rec_id = query.get("id", [""])[0]
            try:
                if path == "/api/open-folder":
                    try:
                        open_folder(recorder.folder)
                    except Exception as err:  # the file manager (xdg-open on Linux) may be missing
                        return self.send_json(500, {"error": folder_problem(recorder.folder, err)})
                    return self.send(204)
                if path == "/api/recording/start":
                    raw = self.read_body(MAX_BODY)
                    if raw is None:
                        return None
                    body = self.parse_json(raw)
                    ext = body.get("ext") if isinstance(body, dict) else None
                    page = body.get("page") if isinstance(body, dict) else None  # the page recording (see _OrphanWatch)
                    owner = _token(page if isinstance(page, str) else "")
                    return self.send_json(200, recorder.start(ext if isinstance(ext, str) else "", owner=owner))
                if path == "/api/recording/chunk":
                    try:
                        seq = int(query.get("seq", [""])[0])
                    except ValueError:
                        return self.send_json(400, {"error": "seq must be a number"})
                    data = self.read_body(MAX_CHUNK)
                    if data is None:
                        return None
                    recorder.append(rec_id, seq, data)
                    return self.send(204)
                if path == "/api/recording/stop":
                    # recorded=0: the page recorded no picture (started while paused, stopped before Resume)
                    recorded = query.get("recorded", ["1"])[0] != "0"
                    return self.send_json(200, recorder.stop(rec_id, recorded=recorded))
            except RecorderError as err:
                payload = {"error": err.reason}
                if err.expected is not None:
                    payload["expected"] = err.expected
                if err.stopped:
                    payload["stopped"] = err.stopped  # a write error stopped it: why, in plain words for the page
                return self.send_json(err.status, payload)
            return self.send_json(404, {"error": "not found"})

    return LanServer(("0.0.0.0", port), Handler)


def make_redirect_server(
    port: int, target_port: int, discovery: Discovery, web_dir: Path | None = None
) -> ThreadingHTTPServer:
    """Port 80 helper: sends plain `http://<name>.local` to the main port while the easy address is on. It serves no
    files but the message pages' style sheet (from `web_dir`), for its own "Use the link" page."""
    style = (web_dir or paths.web_dir()) / "style.css"

    class RedirectHandler(BaseHTTPRequestHandler):
        server_version = "Porthole"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

        def log_message(self, format, *args):
            pass

        def send(self, status: int, body: bytes = b"", extra=None, content_type: str = "text/html; charset=utf-8"):
            self.send_response(status)
            for key, value in {**_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            url = _split(self.path)
            if url is None:
                return self.send(400, _message_page("Bad request", "Open this page by its IP address."))
            if url.path == _STYLE_PATH:
                try:
                    return self.send(200, style.read_bytes(), content_type=_STATIC["style.css"])
                except OSError:
                    return self.send(404, _message_page("Not found", "This file doesn't exist."))
            host = self.headers.get("Host")
            if not _host_ok(host, discovery, "GET", url.path, parse_qs(url.query), _plain_ip(self.client_address[0])):
                return self.send(400, _use_the_link())
            if not discovery.state()["enabled"]:
                return self.send(404, _message_page("Nothing here", "Ask the person sharing for the link."))
            return self.send(302, extra={"Location": f"http://{_redirect_host(host)}:{target_port}/"})

    return LanServer(("0.0.0.0", port), RedirectHandler)
