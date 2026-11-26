"""A tiny mDNS responder: answers "who is porthole.local?" with this PC's address.

Only A records (IPv4) for one name; IPv6 questions get an NSEC "no such record".
Runs next to the system's own responder (avahi, Windows) by sharing UDP port 5353.
"""

import ipaddress
import socket
import struct
import threading
import time

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
TYPE_A = 1
TYPE_AAAA = 28
TYPE_NSEC = 47
TYPE_ANY = 255
CLASS_IN = 1
TOP_BIT = 0x8000  # cache-flush in answers, unicast-response in questions
LEGACY_TTL = 10  # RFC 6762 section 6.7: answers to one-shot queries live at most 10 s


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise ValueError(f"bad label in {name!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def read_name(packet: bytes, offset: int) -> tuple[str, int]:
    """Read a (possibly compressed) name; returns it lower-cased and the offset after it."""
    labels: list[str] = []
    pos = offset
    end = None
    jumps = 0
    while True:
        if pos >= len(packet):
            raise ValueError("name runs past the packet")
        length = packet[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(packet):
                raise ValueError("truncated pointer")
            pointer = ((length & 0x3F) << 8) | packet[pos + 1]
            jumps += 1
            if pointer >= pos or jumps > 20:  # pointers must go backwards: no loops
                raise ValueError("bad pointer")
            if end is None:
                end = pos + 2
            pos = pointer
            continue
        if length & 0xC0:
            raise ValueError("bad label length")
        pos += 1
        if pos + length > len(packet):
            raise ValueError("label runs past the packet")
        labels.append(packet[pos : pos + length].decode("utf-8", "replace"))
        pos += length
    return ".".join(labels).lower(), (end if end is not None else pos)


def parse_questions(packet: bytes) -> list[tuple[str, int, bool]]:
    """Questions of a query as (name, type, wants unicast); [] for responses or junk."""
    if len(packet) < 12:
        return []
    _qid, flags, qdcount, _an, _ns, _ar = struct.unpack(">HHHHHH", packet[:12])
    if flags & 0x8000:
        return []
    questions = []
    pos = 12
    try:
        for _ in range(qdcount):
            name, pos = read_name(packet, pos)
            if pos + 4 > len(packet):
                return []
            qtype, qclass = struct.unpack(">HH", packet[pos : pos + 4])
            pos += 4
            questions.append((name, qtype, bool(qclass & TOP_BIT)))
    except ValueError:
        return []
    return questions


def a_record(name: str, ip: str, ttl: int, cache_flush: bool = True) -> bytes:
    rclass = CLASS_IN | (TOP_BIT if cache_flush else 0)
    return encode_name(name) + struct.pack(">HHIH", TYPE_A, rclass, ttl, 4) + socket.inet_aton(ip)


def nsec_record(name: str, ttl: int, cache_flush: bool = True) -> bytes:
    """"`name` has an A record and nothing else" (RFC 6762 section 6.1), so IPv6 lookups end at once."""
    rclass = CLASS_IN | (TOP_BIT if cache_flush else 0)
    rdata = encode_name(name) + bytes([0, 1, 0x40])  # next name: itself; window 0, 1 byte, bit 1 = A
    return encode_name(name) + struct.pack(">HHIH", TYPE_NSEC, rclass, ttl, len(rdata)) + rdata


def response(name: str, ip: str, ttl: int = 120) -> bytes:
    return struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + a_record(name, ip, ttl)


def _from_local_network(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def answer(
    packet: bytes,
    source: tuple[str, int],
    name: str,
    ip: str,
    multicast: tuple[str, int] = (MDNS_GROUP, MDNS_PORT),
) -> list[tuple[bytes, tuple[str, int]]]:
    """Replies to send for an incoming packet: [] unless it asks about our name."""
    wanted = [q for q in parse_questions(packet) if q[0] == name.lower() and q[1] in (TYPE_A, TYPE_ANY, TYPE_AAAA)]
    if not wanted:
        return []
    legacy = source[1] != multicast[1]
    if legacy and not _from_local_network(source[0]):
        return []  # one-shot replies go straight back to the sender: never to the internet

    def records(ttl: int, cache_flush: bool) -> list[bytes]:
        out = []
        if any(q[1] in (TYPE_A, TYPE_ANY) for q in wanted):
            out.append(a_record(name, ip, ttl, cache_flush))
        if any(q[1] == TYPE_AAAA for q in wanted):
            out.append(nsec_record(name, ttl, cache_flush))
        return out

    if legacy:
        # A plain DNS client asking the mDNS address directly: answer it like DNS.
        found = records(LEGACY_TTL, cache_flush=False)
        qid = struct.unpack(">H", packet[:2])[0]
        header = struct.pack(">HHHHHH", qid, 0x8400, len(wanted), len(found), 0, 0)
        questions = b"".join(encode_name(name) + struct.pack(">HH", q[1], CLASS_IN) for q in wanted)
        return [(header + questions + b"".join(found), source)]
    found = records(120, cache_flush=True)
    return [(struct.pack(">HHHHHH", 0, 0x8400, 0, len(found), 0, 0) + b"".join(found), multicast)]


def answers_for(packet: bytes, name: str) -> list[str]:
    """IPv4 addresses a response gives for `name`. Never raises: bad packets give what was read."""
    if len(packet) < 12:
        return []
    _qid, flags, qdcount, ancount, nscount, arcount = struct.unpack(">HHHHHH", packet[:12])
    if not flags & 0x8000:
        return []
    found = []
    pos = 12
    try:
        for _ in range(qdcount):
            _qname, pos = read_name(packet, pos)
            pos += 4
        for _ in range(ancount + nscount + arcount):
            rname, pos = read_name(packet, pos)
            rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", packet[pos : pos + 10])
            pos += 10
            rdata = packet[pos : pos + rdlength]
            pos += rdlength
            if len(rdata) != rdlength:
                break  # cut short
            if rtype == TYPE_A and rdlength == 4 and rname == name.lower():
                found.append(socket.inet_ntoa(rdata))
    except (OSError, ValueError, struct.error):
        return found
    return found


class Announcer:
    """Answers for `<name>.local` until stopped. Picks `<name>-2` and so on if the name is taken."""

    def __init__(
        self,
        name: str,
        ip: str,
        port: int = MDNS_PORT,
        group: str = MDNS_GROUP,
        join: bool = True,
        probe: bool = True,
        socket_factory=socket.socket,
    ):
        self.base = name
        self.name = name
        self.ip = ip
        self.port = port
        self.group = group
        self.join = join
        self.probe = probe
        self._socket_factory = socket_factory
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None
        self._running = False

    @property
    def hostname(self) -> str:
        return f"{self.name}.local"

    @property
    def alive(self) -> bool:
        """True while the name is being answered."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def _open(self) -> socket.socket:
        """The shared UDP socket. On Windows the system's DNS client may keep port 5353 for itself: then this
        raises, and the easy address shows as not available."""
        sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("" if self.join else self.ip, self.port))
            if self.join:
                membership = socket.inet_aton(self.group) + socket.inet_aton(self.ip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.ip))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            sock.settimeout(0.25)
        except OSError:
            sock.close()
            raise
        return sock

    def start(self) -> None:
        sock = self._open()
        self._sock = sock
        try:
            if self.probe:
                self._pick_name()
            self._running = True
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
        except BaseException:
            # Never leave port 5353 held after a failed start (including "no free name").
            self._running = False
            self._sock = None
            sock.close()
            raise
        self._announce(120)
        self._timer = threading.Timer(1.0, self._announce, args=(120,))
        self._timer.daemon = True
        self._timer.start()

    def _pick_name(self) -> None:
        for n in range(1, 10):
            candidate = self.base if n == 1 else f"{self.base}-{n}"
            if not self._taken(f"{candidate}.local"):
                self.name = candidate
                return
        raise OSError("no free name on the network")

    def _taken(self, host: str) -> bool:
        """Does another device answer for `host`? Bad packets and socket hiccups are skipped."""
        probe = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + encode_name(host) + struct.pack(">HH", TYPE_ANY, CLASS_IN)
        for _ in range(3):
            self._sock.sendto(probe, (self.group, self.port))
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                try:
                    data, _addr = self._sock.recvfrom(9000)
                except (socket.timeout, BlockingIOError):
                    break
                except OSError:  # e.g. Windows reports an ICMP "port unreachable" as a reset
                    continue
                if any(ip != self.ip for ip in answers_for(data, host)):
                    return True
        return False

    def _serve(self) -> None:
        sock = self._sock
        while self._running and sock is not None:
            try:
                data, source = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                if not self._running or sock.fileno() == -1:
                    break  # stopped, or the socket was closed
                time.sleep(0.05)  # one bad packet (e.g. a Windows ICMP reset): keep serving
                continue
            for payload, destination in answer(data, source, self.hostname, self.ip, (self.group, self.port)):
                try:
                    sock.sendto(payload, destination)
                except OSError:
                    pass
        self._running = False

    def _announce(self, ttl: int) -> None:
        sock = self._sock
        if sock and self.join:
            try:
                sock.sendto(response(self.hostname, self.ip, ttl), (self.group, self.port))
            except OSError:
                pass

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
        if not self._sock:
            return
        self._announce(0)  # goodbye: caches drop the name
        self._running = False
        if self._thread:
            self._thread.join(1)
        self._sock.close()
        self._sock = None
