"""Room state and message routing between the sharer and its viewers.

The hub never sees video. It only forwards the small WebRTC set-up messages
(offer, answer, network candidates) and tells each side who joined or left.
"""

import copy
import hmac
from queue import Empty, Queue
from collections import deque
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

SHARER = "sharer"  # the address viewers send to, and the "from" on the sharer's messages

# "paused" and "resumed": the viewer page shows a small "Paused" note over the picture (the card says it only in its middle).
_FROM_SHARER = {"offer", "candidate", "stopped", "waiting", "denied", "removed", "paused", "resumed"}
_FROM_VIEWER = {"answer", "candidate", "ready", "quality"}
_WAITING_REASONS = {"approval", "screen"}
_QUALITY_MODES = {"auto", "high", "saver"}
_CLIENT_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,64}")  # fullmatch: `$` would let a final "\n" through
# Open viewer connections (tabs) per device address. Browsers open at most 6 connections to one address and each
# tab's event stream holds one, so with 6 nothing else gets through; at 5 a 6th tab still loads and hears why not.
MAX_VIEWERS_PER_ADDRESS = 5
# A closed viewer's row (one with a browser token, never denied or removed) stays this long before it is dropped, so a
# reloaded page, back after its old stream closed, takes it over (_row_to_take_over) instead of joining as a new row.
KEEP_SECONDS = 2.0
JOIN_LIMIT = 6  # new viewer connections per device address ...
JOIN_WINDOW = 60  # ... in this many seconds
_CODES = range(100, 1000)  # 3-digit check codes
REFUSALS_KEPT = 5  # refused viewers the sharer page can hear about ...
REFUSAL_SECONDS = 600  # ... from the last ten minutes


class HubError(Exception):
    """`refusal` names why a viewer was turned away ("too-many-tabs", "too-many-tries"), so the
    page can say so; None for every other error."""

    def __init__(self, status: int, reason: str, refusal: str | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.refusal = refusal


@dataclass
class Peer:
    id: str
    role: str  # "sharer" or "viewer"
    client_ip: str  # the peer's address as the server sees it
    local_ip: str  # this PC's address on the peer's network
    device: str
    client: str = ""  # random token the viewer's browser keeps, to recognise a returning device
    code: int = 0  # viewers: a 3-digit check code shown on the waiting screen and on the sharer's row
    joined: int = 0  # viewers: when they connected (epoch ms), shown on the sharer's row
    turned_away: bool = False  # viewers: the sharer denied or removed it, so its row can never be taken over
    closed: bool = False  # viewers: its stream has closed; the row waits a moment for a reload (KEEP_SECONDS)
    queue: Queue = field(default_factory=Queue)  # before Python 3.14, `queue.Queue` here would read this field, not the module


def rewrite_candidate(candidate: str, ip: str) -> str | None:
    """Swap a hidden `xxxx.local` host address for a real IP; None if not such a candidate."""
    parts = candidate.split(" ")
    if len(parts) < 8 or not parts[4].endswith(".local") or " typ host" not in candidate:
        return None
    parts[4] = ip
    return " ".join(parts)


def device_label(user_agent: str, touch: bool = False) -> str:
    """What the sharer reads for a device. `touch`: the viewer page says the device has a touch screen. An iPad asks
    for desktop sites, so its browser says Mac; that is the only way to tell them apart (Macs have no touch screen)."""
    ua = user_agent or ""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua or (touch and "Macintosh" in ua):
        return "iPad"
    if "Android" in ua:
        # Chrome leaves "Mobile" out on tablets; Firefox says "Tablet".
        return "Android tablet" if "Tablet" in ua or "Mobile" not in ua else "Android phone"
    if "Windows" in ua:
        return "Windows PC"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "Mac"
    if "CrOS" in ua:
        return "Chromebook"
    if "Linux" in ua or "X11" in ua:
        return "Linux PC"
    return "Device"


class Hub:
    def __init__(
        self,
        room: str | None = None,
        join_limit: int = JOIN_LIMIT,
        join_window: float = JOIN_WINDOW,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        keep_seconds: float = KEEP_SECONDS,
    ):
        self.room = room or secrets.token_urlsafe(6)
        self._keep = keep_seconds
        self.ended = False  # Porthole is quitting (Quit, Ctrl+C, the app window closed)
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}  # every open connection, including a replaced sharer
        # Viewer connections whose row a reconnect of the same device took over: out of every list, but their event
        # stream still delivers its last message ("replaced") and then ends.
        self._leaving: dict[str, Peer] = {}
        self._sharer_id: str | None = None
        self._sharing = False  # the sharer page says its screen is being shared
        self._join_limit = join_limit
        self._join_window = join_window
        self._clock = clock  # for the join limit
        self._wall_clock = wall_clock  # for the join time shown to the sharer
        self._joins: dict[str, list[float]] = {}  # device address -> times of recent viewer connections
        self._refusals: deque = deque(maxlen=REFUSALS_KEPT)  # (clock time, entry for the sharer page)
        self._choices: dict | None = None  # this run's approvals and switches, as the sharer page last reported them

    def check_room(self, room: str) -> bool:
        return hmac.compare_digest(room.encode(), self.room.encode())

    def _viewers(self) -> list[Peer]:
        return [p for p in self._peers.values() if p.role == "viewer"]

    def viewers(self) -> list[dict]:
        with self._lock:
            return [{"id": p.id, "device": p.device} for p in self._viewers()]

    def role_of(self, peer_id: str) -> str | None:
        with self._lock:
            peer = self._peers.get(peer_id)
            return peer.role if peer else None

    def sharer_active(self) -> bool:
        with self._lock:
            return self._current_sharer() is not None

    @property
    def sharing(self) -> bool:
        with self._lock:
            return self._sharing

    def set_sharing(self, value: bool, sharer_id: str | None = None) -> None:
        """Record what the sharer page reports; a report from a replaced tab no longer counts."""
        with self._lock:
            if sharer_id is not None and sharer_id != self._sharer_id:
                return
            self._sharing = bool(value) and self._current_sharer() is not None

    @sharing.setter
    def sharing(self, value: bool) -> None:
        """Set by the sharer page; only true while a sharer page is connected."""
        with self._lock:
            self._sharing = bool(value) and self._current_sharer() is not None

    def keep_choices(self, choices: dict, sharer_id: str) -> bool:
        """Keep this run's approvals and switches as the sharer page reports them, so a page that takes over (in another
        browser after Open in my browser, with other saved settings) starts from them. Only the page that is the sharer
        now counts: a replaced or waiting page must not undo what it allowed or denied. False if it isn't."""
        with self._lock:
            if not sharer_id or sharer_id != self._sharer_id:
                return False
            self._choices = copy.deepcopy(choices)
            return True

    def choices(self) -> dict | None:
        """This run's approvals and switches (a copy), or None until a sharer page has reported them."""
        with self._lock:
            return copy.deepcopy(self._choices)

    def connect(
        self,
        role: str,
        client_ip: str,
        local_ip: str,
        device: str,
        client: str = "",
        takeover: bool = True,
        in_window: bool = False,
        previous: str = "",
    ) -> Peer:
        """`in_window`: a sharer page in Porthole's own window. The page it replaces hears so: that window shares the
        browser Porthole is tied to, so the replaced page must not untie it. `previous`: a viewer's id from its
        connection before (it reconnected); see _row_to_take_over."""
        if role not in ("sharer", "viewer"):
            raise HubError(400, "unknown role")
        client = client if isinstance(client, str) and _CLIENT_TOKEN.fullmatch(client) else ""
        with self._lock:
            old = self._row_to_take_over(previous, client, client_ip) if role == "viewer" else None
            if role == "viewer":
                try:
                    self._check_viewer_allowed(client_ip, replacing=old)
                except HubError as err:
                    self._note_refusal(device, err.refusal)
                    raise
            if role == "sharer" and not takeover and self._current_sharer() is not None:
                raise HubError(409, "sharing is running in another tab")
            prefix = "s-" if role == "sharer" else "v-"
            peer = Peer(prefix + secrets.token_hex(4), role, client_ip, local_ip, device, client)
            if role == "viewer":
                if old:  # the same device, back: its row keeps its code and join time (the page keeps the name)
                    peer.code, peer.joined = old.code, old.joined
                    self._retire(old)
                else:
                    peer.code = self._new_code()
                    peer.joined = int(self._wall_clock() * 1000)
                self._joins.setdefault(client_ip, []).append(self._clock())  # a takeover is a try too
            self._peers[peer.id] = peer
            if role == "sharer":
                self._sharing = False  # a new sharer page starts out not sharing
                old = self._peers.get(self._sharer_id) if self._sharer_id else None
                if old:
                    old.queue.put({"type": "replaced", "window": True} if in_window else {"type": "replaced"})
                    # The old tab's picture is gone; the new tab starts out not sharing.
                    for viewer in self._viewers():
                        viewer.queue.put({"type": "sharer-left"})
                self._sharer_id = peer.id
                # Each viewer's address too: the sharer page ties an approval to its token and the address it was allowed
                # from, as a reconnect needs both to keep its row.
                viewers = [
                    {"id": p.id, "device": p.device, "client": p.client, "code": p.code, "joined": p.joined, "address": p.client_ip}
                    for p in self._viewers()
                ]
                peer.queue.put({"type": "hello", "id": peer.id, "viewers": viewers})
            else:
                peer.queue.put({"type": "hello", "id": peer.id, "code": peer.code})
                sharer = self._current_sharer()
                if sharer:
                    joined = {
                        "type": "viewer-joined",
                        "id": peer.id,
                        "device": device,
                        "client": client,
                        "code": peer.code,
                        "joined": peer.joined,
                        "address": client_ip,
                    }
                    if old:
                        joined["replaces"] = old.id  # the sharer page hands that row to this connection
                    sharer.queue.put(joined)
            if self.ended:  # Porthole is stopping, and serves a moment longer: this page must not keep retrying either
                peer.queue.put({"type": "ended"})
            return peer

    def _row_to_take_over(self, previous, client: str, client_ip: str) -> Peer | None:
        """The row a reconnecting viewer may take over, or None (it then joins as a new device; nothing tells it why).
        Only the device that held the row: `previous` names a viewer connection that is still listed (not left, not
        already taken over), was never denied or removed, and has the same per-browser token (a valid one: 96 random
        bits, kept in that browser) and the same address as this one. Ids travel in plain HTTP on the Wi-Fi, so an id
        alone is worth nothing: another device has another address, and another browser on it another token."""
        if not client or not isinstance(previous, str):
            return None
        old = self._peers.get(previous)
        if old is None or old.role != "viewer" or old.turned_away or not old.client:
            return None
        if not hmac.compare_digest(old.client.encode(), client.encode()) or old.client_ip != client_ip:
            return None
        return old

    def _retire(self, old: Peer) -> None:
        """A reconnect took over this viewer's row: it leaves every list at once, and its event stream ends after
        "replaced" (a phone's dead connection hears nothing; another live tab rejoins as a device of its own)."""
        del self._peers[old.id]
        if not old.closed:  # a closed one has no stream left to deliver it, and was disconnected already
            self._leaving[old.id] = old
        old.queue.put({"type": "replaced"})

    def _check_viewer_allowed(self, client_ip: str, replacing: Peer | None = None) -> None:
        """Too many open tabs, or too many new connections lately, from one device: refused. A connection that takes
        over one of the device's rows (`replacing`) leaves it with as many tabs as before."""
        open_tabs = sum(1 for p in self._viewers() if p.client_ip == client_ip and p is not replacing and not p.closed)
        if open_tabs >= MAX_VIEWERS_PER_ADDRESS:
            raise HubError(429, "too many connections from this device", refusal="too-many-tabs")
        cutoff = self._clock() - self._join_window
        for address in list(self._joins):  # forget old joins, so the table never grows
            recent = [t for t in self._joins[address] if t > cutoff]
            if recent:
                self._joins[address] = recent
            else:
                del self._joins[address]
        if len(self._joins.get(client_ip, ())) >= self._join_limit:
            raise HubError(429, "too many tries from this device", refusal="too-many-tries")

    def _note_refusal(self, device: str, reason: str | None) -> None:
        if reason:
            entry = {"time": int(self._wall_clock() * 1000), "device": device, "reason": reason}
            self._refusals.append((self._clock(), entry))

    def refusals(self) -> list[dict]:
        """Viewers turned away lately, oldest first: {"time": epoch ms, "device", "reason"}."""
        with self._lock:
            cutoff = self._clock() - REFUSAL_SECONDS
            while self._refusals and self._refusals[0][0] <= cutoff:
                self._refusals.popleft()
            return [dict(entry) for _, entry in self._refusals]

    def _new_code(self) -> int:
        """A random 3-digit code no current viewer has (any code once all 900 are taken)."""
        used = {p.code for p in self._viewers()}
        free = [code for code in _CODES if code not in used]
        return secrets.choice(free or _CODES)

    def _current_sharer(self) -> Peer | None:
        return self._peers.get(self._sharer_id) if self._sharer_id else None

    def disconnect(self, peer_id: str) -> None:
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None:
                self._leaving.pop(peer_id, None)  # its row went to a reconnect, which the sharer already heard of
                return
            if peer.role == "viewer" and peer.client and not peer.turned_away and not peer.closed and self._keep > 0:
                peer.closed = True  # its row waits a moment: a reloaded page takes it over
                timer = threading.Timer(self._keep, self._drop, args=(peer_id,))
                timer.daemon = True
                timer.start()
                return
            del self._peers[peer_id]
            if peer.role == "viewer":
                sharer = self._current_sharer()
                if sharer:
                    sharer.queue.put({"type": "viewer-left", "id": peer.id})
            elif peer.id == self._sharer_id:
                self._sharer_id = None
                self._sharing = False
                for viewer in self._viewers():
                    viewer.queue.put({"type": "sharer-left"})

    def _drop(self, peer_id: str) -> None:
        """The wait for a closed viewer's reload is over: its row goes, unless a reconnect has taken it over."""
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None or not peer.closed:
                return
            del self._peers[peer_id]
            sharer = self._current_sharer()
            if sharer:
                sharer.queue.put({"type": "viewer-left", "id": peer.id})

    def end(self) -> None:
        """Porthole is quitting: tell every viewer, so they stop retrying, and the sharer page."""
        with self._lock:
            self.ended = True
            for viewer in self._viewers():
                viewer.queue.put({"type": "ended"})
            sharer = self._current_sharer()
            if sharer:
                sharer.queue.put({"type": "ended"})

    def next_message(self, peer_id: str, timeout: float) -> dict | None:
        with self._lock:
            peer = self._peers.get(peer_id) or self._leaving.get(peer_id)
        if peer is None:
            return None
        try:
            return peer.queue.get(timeout=timeout)
        except Empty:
            return None

    def send(self, from_id: str, msg: dict) -> None:
        with self._lock:
            sender = self._peers.get(from_id)
            if sender is None:
                raise HubError(404, "unknown peer")
            kind = msg.get("type")
            if not isinstance(kind, str):
                raise HubError(400, "type must be a string")
            to = msg.get("to")
            if sender.role == "sharer":
                if from_id != self._sharer_id:
                    raise HubError(409, "sharing moved to another tab")
                if kind not in _FROM_SHARER:
                    raise HubError(403, "not allowed")
                target = self._peers.get(to) if isinstance(to, str) else None
                if target is None or target.role != "viewer":
                    raise HubError(404, "viewer has left")
                if kind in ("denied", "removed"):
                    target.turned_away = True  # its row is never taken over again, even before its stream closes
                fix_ip = target.local_ip
                from_label = SHARER
            else:
                if kind not in _FROM_VIEWER or to != SHARER:
                    raise HubError(403, "not allowed")
                target = self._current_sharer()
                if target is None:
                    raise HubError(409, "nobody is sharing")
                fix_ip = sender.client_ip
                from_label = sender.id

            out = {"type": kind, "from": from_label}
            copy = None
            if kind in ("offer", "answer"):
                sdp = msg.get("sdp")
                if not isinstance(sdp, str) or not sdp:
                    raise HubError(400, "sdp missing")
                out["sdp"] = sdp
                n = msg.get("n")
                if n is not None:
                    if not isinstance(n, int) or isinstance(n, bool):
                        raise HubError(400, "n must be a number")
                    out["n"] = n
                restart = msg.get("restart")
                if restart is not None:
                    if kind != "offer" or not isinstance(restart, bool):
                        raise HubError(400, "restart must be true or false")
                    out["restart"] = restart
            elif kind == "waiting":
                if not isinstance(msg.get("reason"), str) or msg["reason"] not in _WAITING_REASONS:
                    raise HubError(400, "unknown reason")
                out["reason"] = msg["reason"]
            elif kind == "quality":
                if not isinstance(msg.get("mode"), str) or msg["mode"] not in _QUALITY_MODES:
                    raise HubError(400, "unknown quality mode")
                out["mode"] = msg["mode"]
            elif kind == "candidate":
                cand = msg.get("candidate")
                if not isinstance(cand, dict) or not isinstance(cand.get("candidate"), str):
                    raise HubError(400, "candidate missing")
                clean = {
                    "candidate": cand["candidate"],
                    "sdpMid": cand.get("sdpMid") if isinstance(cand.get("sdpMid"), str) else None,
                    "sdpMLineIndex": cand.get("sdpMLineIndex") if isinstance(cand.get("sdpMLineIndex"), int) else None,
                    "usernameFragment": cand.get("usernameFragment")
                    if isinstance(cand.get("usernameFragment"), str)
                    else None,
                }
                out["candidate"] = clean
                fixed = rewrite_candidate(clean["candidate"], fix_ip)
                if fixed:
                    copy = {"type": kind, "from": from_label, "candidate": {**clean, "candidate": fixed}}
            target.queue.put(out)
            if copy:
                target.queue.put(copy)
