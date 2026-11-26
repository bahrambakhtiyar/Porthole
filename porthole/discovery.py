"""The easy address: announces `<name>.local` on the network while the sharer page asks for it.

Nothing is announced until `set_enabled(True)`; the server never turns it on by itself. Turned off, it stops new
visitors only: while devices that came by the name are still watching, the name is still answered, for them alone.
"""

import threading
import time
from collections.abc import Callable

from .mdns import Announcer

NO_ADDRESS = "Easy address can't work: this PC has no network address."
START_FAILED = "Easy address couldn't start on this network."
# Turned off, the name stays for the devices watching by it until each has been gone this long: a phone back from a
# blip or a short lock reconnects by the name. Two minutes, as long as a device may keep the name's answer anyway.
LINGER_SECONDS = 120
# A device on "Nothing shared yet" by the name checks every 5 s but has no stream: turned off, the name is still
# answered this long after its last check, for nobody's use, so that check gets "Use the link" instead of no answer.
LOOK_SECONDS = 15


class Discovery:
    def __init__(
        self,
        name: str,
        ip: str | None,
        port: int,
        announcer_factory: Callable = Announcer,
        linger_seconds: float = LINGER_SECONDS,
        look_seconds: float = LOOK_SECONDS,
    ):
        self.name = name
        self.port = port
        self.short_port: int | None = None  # 80 when the port-80 helper runs, so plain `name.local` works
        self.blocked_reason: str | None = None  # set (e.g. by --no-discovery) to refuse turning it on
        self.detail = ""  # the last start failure, for debugging; never shown on the page
        self._factory = announcer_factory
        self._ip = ip
        self._lock = threading.Lock()  # guards the fields below; held only briefly
        self._change = threading.Lock()  # one change at a time; the slow network work runs under this one only
        self._announcer = None
        self._wanted = False  # the sharer page asked for it
        self._error = ""
        self._host = f"{name}.local"  # the name announced last (may be name-2.local after a clash)
        self._stopped = False
        self._linger = linger_seconds
        self._look = look_seconds
        self._lookers: dict[str, float] = {}  # device address -> its last check by the name while nothing was shared
        self._look_until = 0.0  # turned off with such devices about: the name is answered until then
        self._streams: dict[str, int] = {}  # device address -> its open viewer streams that came by the name
        self._away: dict[str, tuple[int, float]] = {}  # device address -> (departure number, when) once it has none
        self._departures = 0
        # Set while the name stays after being turned off: the devices that were watching by it, the only ones it is
        # still for. None otherwise.
        self._staying: set[str] | None = None

    # --- control ---

    def set_enabled(self, on: bool) -> dict:
        with self._change:
            if on:
                with self._lock:
                    allowed = not self._stopped and not self.blocked_reason
                    if allowed:
                        self._wanted = True
                        self._staying = None
                    start = allowed and self._announcer is None
                if start:
                    self._start()
            else:
                waits = []
                with self._lock:
                    self._wanted = False
                    self._error = ""
                    look = False
                    if self._staying is None and self._announcer is not None:
                        # Devices watching by the name, or between two streams (gone less than the wait), keep it;
                        # nobody else gets it. The set is never added to afterwards. Devices waiting for a share by
                        # it keep it answered a moment too, but may not use it: their next check gets "Use the link".
                        now = time.monotonic()
                        self._away = {ip: gone for ip, gone in self._away.items() if now - gone[1] < self._linger}
                        look = any(now - seen < self._look for seen in self._lookers.values())
                        self._lookers.clear()
                        self._look_until = now + self._look if look else 0.0
                        staying = set(self._streams) | set(self._away)
                        self._staying = staying or (set() if look else None)
                        waits = [(ip, number, self._linger - (now - when)) for ip, (number, when) in self._away.items()]
                    stay = self._staying is not None
                if stay:
                    for ip, number, seconds in waits:
                        self._wait_for(ip, number, seconds)
                    if look:
                        self._wait_for_lookers(self._look)
                else:
                    self._stop()
            return self.state()

    # --- devices watching by the name ---

    def guest_arrived(self, ip: str) -> None:
        """A viewer stream that came by the name opened (the web server calls this)."""
        with self._lock:
            self._streams[ip] = self._streams.get(ip, 0) + 1
            self._away.pop(ip, None)  # back within the wait

    def guest_left(self, ip: str, turned_away: bool = False) -> None:
        """That stream closed. Once the device has had none for the wait, the name is no longer kept for it.
        `turned_away`: the sharer denied or removed it, so it isn't watching (even if its stream stays open): no wait."""
        with self._lock:
            left = self._streams.get(ip, 0) - 1
            if left > 0:
                self._streams[ip] = left
                return
            self._streams.pop(ip, None)
            self._departures += 1
            number = self._departures
            wait = 0.0 if turned_away else self._linger
            self._away[ip] = (number, time.monotonic() - (self._linger - wait))  # turned away: the wait is over
            waiting = self._staying is not None and ip in self._staying  # otherwise nothing hangs on it yet
        if waiting:
            self._wait_for(ip, number, wait)

    def looker_seen(self, ip: str) -> None:
        """A device checked "Nothing shared yet" by the name (the web server calls this)."""
        with self._lock:
            self._lookers[ip] = time.monotonic()

    def _wait_for_lookers(self, seconds: float) -> None:
        timer = threading.Timer(max(0.0, seconds), self._lookers_done)
        timer.daemon = True
        timer.start()

    def _lookers_done(self) -> None:
        """The moment for devices that were waiting for a share by the name is over."""
        with self._change:
            with self._lock:
                if self._staying is None or self._staying or time.monotonic() < self._look_until:
                    return  # on again, devices still watching by it, or a later moment of its own
                self._staying = None
            self._stop()  # goodbye

    def admits(self, ip: str) -> bool:
        """Whether a request by the name from this device may go on: always, except while the name stays after being
        turned off, when only the devices that were watching by it may use it."""
        with self._lock:
            return self._staying is None or ip in self._staying

    def _wait_for(self, ip: str, number: int, seconds: float) -> None:
        timer = threading.Timer(max(0.0, seconds), self._gone, args=(ip, number))
        timer.daemon = True
        timer.start()

    def _gone(self, ip: str, number: int) -> None:
        """The wait after a device's last stream by the name is over."""
        with self._change:
            with self._lock:
                if self._away.get(ip, (None, 0.0))[0] != number:
                    return  # it came back (and maybe left again: that departure has a wait of its own)
                del self._away[ip]
                if self._staying is None:
                    return
                self._staying.discard(ip)
                # The last of them has gone; devices waiting for a share by the name may still have a moment.
                done = not self._staying and time.monotonic() >= self._look_until
                if done:
                    self._staying = None
            if done:
                self._stop()  # the last of them has gone: goodbye

    def set_address(self, ip: str | None) -> None:
        """Move the name to a new address: goodbye on the old one, then announce on the new one."""
        with self._change:
            with self._lock:
                if ip == self._ip:
                    return
                self._ip = ip
                self._error = ""
                self._staying = None  # devices on the old network can't reach this PC by any name
                restart = self._wanted and not self._stopped
            self._stop()
            if restart:
                self._start()

    def stop(self) -> None:
        """Porthole is quitting: withdraw the name for good."""
        with self._change:
            with self._lock:
                self._stopped = True
                self._wanted = False
                self._staying = None
            self._stop()

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()

    # --- slow work: called with self._change held, never self._lock ---

    def _start(self) -> None:
        with self._lock:
            ip = self._ip
        if not ip:
            return
        announcer = self._factory(self.name, ip)
        try:
            announcer.start()  # checks the name on the network: up to about a second
        except Exception as err:  # port 5353 held by another program, no free name, ...
            with self._lock:
                self._error = START_FAILED
                self.detail = str(err)
            return
        with self._lock:
            self._announcer = announcer
            self._host = announcer.hostname
            self._error = ""

    def _stop(self) -> None:
        with self._lock:
            announcer, self._announcer = self._announcer, None
        if announcer is not None:
            try:
                announcer.stop()  # sends the goodbye
            except Exception:
                pass

    def _state_locked(self) -> dict:
        if self.blocked_reason:
            return {
                "enabled": False,
                "available": False,
                "blocked": True,  # off for this whole run (--no-discovery): the page shows a note, not an error
                "host": None,
                "url": None,
                "short_url": None,
                "error": self.blocked_reason,
            }
        error = self._error if self._ip else NO_ADDRESS
        host = self._host
        return {
            # Off while the name stays for the devices watching by it: "/" and old links send nobody to the room.
            "enabled": self._wanted and self._announcer is not None,
            "available": not error,
            "blocked": False,
            "host": host,
            "url": f"http://{host}:{self.port}",
            "short_url": f"http://{host}" if self.short_port == 80 else None,
            "error": error,
        }
