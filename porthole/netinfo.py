"""Find this computer's local-network IPv4 addresses, best candidate first."""

import functools
import ipaddress
import socket
import struct
import sys
import types
from typing import NamedTuple

# Interfaces that are never the one a phone on the Wi-Fi can reach.
_SKIP_PREFIXES = (
    "docker", "br-", "veth", "virbr", "tun", "tap", "wg", "tailscale", "zt",
    "vboxnet", "vmnet", "lxcbr", "lxdbr", "incusbr", "podman", "cni", "flannel", "kube",
    "waydroid", "mpqemubr", "mpbr", "anbox",  # Android containers (Waydroid, Anbox) and Multipass's bridges
)
# macOS only (on Linux a bridge can carry the PC's main address): bridges for virtual machines and Internet
# Sharing, VPN tunnels, and AirDrop's and other Apple links. A Mac's Wi-Fi and Ethernet are en0, en1, ...
_MAC_SKIP_PREFIXES = ("bridge", "utun", "awdl", "llw", "anpi")
# Windows only (its adapters have long names and descriptions, not Linux-style ones): words that mark an
# adapter a phone on the Wi-Fi can't reach: Hyper-V's switches (WSL, Docker, the Default Switch), VirtualBox
# and VMware host networks, Npcap's loopback, VPNs, and Bluetooth tethering. The PC's own mobile hotspot
# ("Wi-Fi Direct Virtual Adapter") is not among them: phones on it reach the PC.
_WINDOWS_SKIP_WORDS = (
    "vethernet", "hyper-v virtual ethernet", "wsl", "virtualbox", "vmware", "loopback",
    "tap-windows", "openvpn", "wintun", "wireguard", "tailscale", "zerotier", "bluetooth",
    # VPN apps: the word itself, and the names their adapters go by.
    "vpn", "anyconnect", "pangp", "globalprotect", "fortinet", "forticlient", "nordlynx", "mullvad", "juniper",
    "pulse secure", "sonicwall", "zscaler",
)
# Hyper-V's external switch moves the PC's own address to a vEthernet adapter, which then has the router as
# its gateway; the internal switches (WSL, Docker, the Default Switch) have none. People name an external switch
# themselves, so "WSL" in its name doesn't count against it either.
_WINDOWS_SWITCH_WORDS = frozenset(("vethernet", "hyper-v virtual ethernet", "wsl"))

SIOCGIFADDR = 0x8915  # Linux ioctl: an interface's IPv4 address
SIOCGIFFLAGS = 0x8913  # Linux ioctl: an interface's flags
# The same on macOS: _IOWR('i', 33, struct ifreq) and _IOWR('i', 17, struct ifreq). struct ifreq keeps the
# address and the flags at the same places as on Linux.
MAC_SIOCGIFADDR = 0xC0206921
MAC_SIOCGIFFLAGS = 0xC0206911
IFF_UP = 0x1
IFF_POINTOPOINT = 0x10  # VPNs, dial-up and mobile links: never the Wi-Fi a phone is on (same value on macOS)

# Windows: GetAdaptersAddresses in iphlpapi.dll.
AF_INET = 2
GAA_FLAGS = 0x2 | 0x4 | 0x8 | 0x80  # skip anycast, multicast and DNS servers; include gateways
ERROR_BUFFER_OVERFLOW = 111
ERROR_NO_DATA = 232
IF_OPER_STATUS_UP = 1
IF_TYPE_PPP = 23  # Windows' own VPN and dial-up connections
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
_GAA_FIRST_SIZE = 15_000  # Microsoft's suggested first buffer
_GAA_TRIES = 3  # the list can grow between the size answer and the next call
_MAX_ITEMS = 256  # never follow a broken list for ever
_MAX_NAME = 512  # characters

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_PREFERRED = (
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
)


def usable_interface(flags: int) -> bool:
    """An interface a nearby device could reach: up, and not a point-to-point link."""
    return bool(flags & IFF_UP) and not flags & IFF_POINTOPOINT


def _ioctl(fd: int, request: int, arg: bytes) -> bytes:
    import fcntl  # Linux and macOS only

    return fcntl.ioctl(fd, request, arg)


def _interfaces(address_request: int, flags_request: int) -> list[tuple[str, str]]:
    """(name, IPv4 address) of each interface that is up and not point-to-point, read with ioctl."""
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _index, name in socket.if_nameindex():
            request = struct.pack("256s", name[:15].encode())
            try:
                packed = _ioctl(sock.fileno(), address_request, request)
            except OSError:
                continue  # interface has no IPv4 address
            try:
                flags = struct.unpack_from("H", _ioctl(sock.fileno(), flags_request, request), 16)[0]
            except (OSError, struct.error):
                flags = None  # can't tell: keep it, as before
            if flags is not None and not usable_interface(flags):
                continue
            found.append((name, socket.inet_ntoa(packed[20:24])))
    return found


def _linux_interfaces() -> list[tuple[str, str]]:
    return _interfaces(SIOCGIFADDR, SIOCGIFFLAGS)


class WindowsAdapter(NamedTuple):
    friendly: str  # "Wi-Fi", "vEthernet (WSL)"
    description: str  # "Intel(R) Wi-Fi 6 AX201 160MHz", "Hyper-V Virtual Ethernet Adapter"
    oper_status: int  # 1 is up
    if_type: int  # 23 PPP (Windows VPN), 24 loopback, 131 tunnel
    addresses: tuple[str, ...]  # IPv4
    gateway: bool  # has a real IPv4 gateway (a router)


@functools.lru_cache(maxsize=None)
def _windows_structs() -> types.SimpleNamespace:
    """The parts of IP_ADAPTER_ADDRESSES_LH, IP_ADAPTER_UNICAST_ADDRESS_LH (the gateway record starts the same
    way) and SOCKET_ADDRESS that are read, up to FirstGatewayAddress. Fixed-size integers, so the layout is
    Windows' wherever ctypes runs with the same pointer size (the tests build these records on Linux); the
    names are UTF-16 pointers read by hand, since ctypes' wchar has 4 bytes outside Windows."""
    import ctypes

    u32, ptr = ctypes.c_uint32, ctypes.c_void_p

    class SocketAddress(ctypes.Structure):
        _fields_ = [("lpSockaddr", ptr), ("iSockaddrLength", ctypes.c_int32)]

    class Unicast(ctypes.Structure):
        _fields_ = [("Length", u32), ("Flags", u32), ("Next", ptr), ("Address", SocketAddress)]

    class Adapter(ctypes.Structure):
        _fields_ = [
            ("Length", u32), ("IfIndex", u32), ("Next", ptr), ("AdapterName", ptr),
            ("FirstUnicastAddress", ptr), ("FirstAnycastAddress", ptr), ("FirstMulticastAddress", ptr),
            ("FirstDnsServerAddress", ptr), ("DnsSuffix", ptr), ("Description", ptr), ("FriendlyName", ptr),
            ("PhysicalAddress", ctypes.c_uint8 * 8), ("PhysicalAddressLength", u32), ("Flags", u32), ("Mtu", u32),
            ("IfType", u32), ("OperStatus", ctypes.c_int32), ("Ipv6IfIndex", u32), ("ZoneIndices", u32 * 16),
            ("FirstPrefix", ptr), ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
            ("FirstWinsServerAddress", ptr), ("FirstGatewayAddress", ptr),
        ]

    return types.SimpleNamespace(SocketAddress=SocketAddress, Unicast=Unicast, Adapter=Adapter)


def _get_adapters_addresses():
    """iphlpapi's GetAdaptersAddresses as call(family, flags, buffer, size). Raises outside Windows."""
    import ctypes

    function = ctypes.WinDLL("iphlpapi").GetAdaptersAddresses  # no WinDLL outside Windows: AttributeError
    function.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    function.restype = ctypes.c_uint32
    return lambda family, flags, buffer, size: function(family, flags, None, buffer, ctypes.byref(size))


def _walk(first, kind):
    """The records of a linked list (each starts with Length, then Next)."""
    address, count = first, 0
    while address and count < _MAX_ITEMS:
        record = kind.from_address(address)
        yield record
        address, count = record.Next, count + 1


def _utf16_at(address) -> str:
    import ctypes

    if not address:
        return ""
    units = []
    for i in range(_MAX_NAME):
        unit = ctypes.c_uint16.from_address(address + 2 * i).value
        if not unit:
            break
        units.append(unit)
    return struct.pack(f"<{len(units)}H", *units).decode("utf-16-le", errors="replace")


def _ipv4_at(socket_address) -> str | None:
    import ctypes

    pointer = socket_address.lpSockaddr
    if not pointer or socket_address.iSockaddrLength < 8 or ctypes.c_uint16.from_address(pointer).value != AF_INET:
        return None
    return socket.inet_ntoa(ctypes.string_at(pointer + 4, 4))  # sockaddr_in: family, port, then the address


def _read_windows_adapters(call=None) -> list[WindowsAdapter]:
    """Every adapter with its IPv4 addresses, as GetAdaptersAddresses reports them. `call(family, flags, buffer,
    size)` stands in for the API in tests. Raises when they can't be read (no iphlpapi, an error code, an odd
    record), so the caller can fall back."""
    import ctypes

    call = call or _get_adapters_addresses()
    structs = _windows_structs()
    size = ctypes.c_uint32(_GAA_FIRST_SIZE)
    for _ in range(_GAA_TRIES):
        buffer = ctypes.create_string_buffer(size.value)
        code = call(AF_INET, GAA_FLAGS, buffer, size)
        if code == ERROR_BUFFER_OVERFLOW:
            continue  # `size` now holds the size needed
        if code == ERROR_NO_DATA:
            return []
        if code:
            raise OSError(code, "GetAdaptersAddresses failed")
        adapters = []
        for record in _walk(ctypes.addressof(buffer), structs.Adapter):
            if record.Length < ctypes.sizeof(structs.Adapter):
                raise OSError(0, "adapter record shorter than the fields read")
            unicast = (_ipv4_at(item.Address) for item in _walk(record.FirstUnicastAddress, structs.Unicast))
            gateways = (_ipv4_at(item.Address) for item in _walk(record.FirstGatewayAddress, structs.Unicast))
            adapters.append(
                WindowsAdapter(
                    _utf16_at(record.FriendlyName),
                    _utf16_at(record.Description),
                    record.OperStatus,
                    record.IfType,
                    tuple(address for address in unicast if address),
                    any(gateway not in (None, "0.0.0.0") for gateway in gateways),  # VPNs may show 0.0.0.0
                )
            )
        return adapters
    raise OSError(ERROR_BUFFER_OVERFLOW, "GetAdaptersAddresses kept asking for more room")


def _windows_entries(adapters) -> tuple[list[tuple[str, str]], set[str]]:
    """(name, address) pairs of the adapters that are up and not loopback, a tunnel or a PPP link, and the addresses of
    those with a gateway. Each name holds the adapter's own name and its description, for the skip words."""
    entries, gateways = [], set()
    for adapter in adapters:
        if adapter.oper_status != IF_OPER_STATUS_UP or adapter.if_type in (IF_TYPE_PPP, IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
            continue
        name = " / ".join(part for part in (adapter.friendly, adapter.description) if part)
        for address in adapter.addresses:
            entries.append((name, address))
            if adapter.gateway:
                gateways.add(address)
    return entries, gateways


def _default_route_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # UDP connect sends nothing
            return sock.getsockname()[0]
    except OSError:
        return None


def _read() -> tuple[list[tuple[str, str]], set[str]]:
    """(interface name, IPv4 address) pairs, and the addresses on adapters with a gateway (only Windows says).
    Never raises."""
    if sys.platform.startswith("linux"):
        try:
            return _linux_interfaces(), set()
        except Exception:
            pass
    elif sys.platform == "darwin":
        # Read the interfaces rather than look up the Mac's own name (ayla-mac.local): that is a .local lookup,
        # which macOS 15 lets an app make only with Local Network access, and it can be slow. Should nothing come
        # back (the requests not understood), the older way below still works.
        try:
            found = _interfaces(MAC_SIOCGIFADDR, MAC_SIOCGIFFLAGS)
        except Exception:
            found = []
        if found:
            return found, set()
    elif sys.platform == "win32":
        # The adapters themselves, with names that show the virtual ones and with their gateways; the PC's
        # name only gives bare addresses. Should they not be readable, the older way below still works.
        try:
            found, gateways = _windows_entries(_read_windows_adapters())
        except Exception:
            found, gateways = [], set()
        if found:
            return found, gateways
    entries: list[tuple[str, str]] = []
    try:
        entries = [("", ip) for ip in socket.gethostbyname_ex(socket.gethostname())[2]]
    except (OSError, UnicodeError):  # a Windows PC name may hold characters a name lookup refuses
        pass
    default = _default_route_address()
    if default:
        entries.insert(0, ("", default))
    return entries, set()


def list_ipv4() -> list[tuple[str, str]]:
    """Return (interface name, IPv4 address) pairs. Never raises."""
    return _read()[0]


def _skipped_name(name: str, platform: str, gateway: bool = False) -> bool:
    name = name.lower()
    if platform == "win32":
        words = {word for word in _WINDOWS_SKIP_WORDS if word in name}
        if words:
            # Only Hyper-V's external switch, which carries the PC's own network, has a gateway.
            return not (gateway and words <= _WINDOWS_SWITCH_WORDS)
    return name.startswith(_SKIP_PREFIXES) or (platform == "darwin" and name.startswith(_MAC_SKIP_PREFIXES))


def rank_addresses(
    entries: list[tuple[str, str]], default: str | None = None, platform: str | None = None, gateways=()
) -> list[str]:
    """Keep addresses a nearby device could reach, ordered most likely first.

    `default` is the address the PC uses for its default route; when it survives the
    filters it goes first, since it is the network the PC is really using. `gateways` holds
    the addresses on adapters with a gateway (a router), which only Windows reports: they
    come before the others, since phones are most likely on the network with the router.
    """
    platform = platform or sys.platform
    gateways = set(gateways or ())
    groups = len(_PREFERRED) + 1
    buckets: list[list[str]] = [[] for _ in range(2 * groups)]  # with a gateway first, then the rest
    seen = set()
    for name, raw in entries:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local or ip in _CGNAT:
            continue
        if _skipped_name(name, platform, raw in gateways) or raw in seen:
            continue
        seen.add(raw)
        first = 0 if raw in gateways else groups
        for rank, network in enumerate(_PREFERRED):
            if ip in network:
                buckets[first + rank].append(raw)
                break
        else:
            buckets[first + groups - 1].append(raw)
    ranked = [ip for bucket in buckets for ip in bucket]
    if default in ranked:
        ranked.remove(default)
        ranked.insert(0, default)
    return ranked


def lan_addresses() -> list[str]:
    entries, gateways = _read()
    return rank_addresses(entries, default=_default_route_address(), gateways=gateways)
