import ctypes
import ipaddress
import socket
import struct
import sys
import unittest
from unittest import mock

from porthole import netinfo
from porthole.netinfo import list_ipv4, rank_addresses, usable_interface

IFF_UP, IFF_LOOPBACK, IFF_POINTOPOINT, IFF_RUNNING, IFF_MULTICAST = 0x1, 0x8, 0x10, 0x40, 0x1000


class RankAddressesTest(unittest.TestCase):
    def test_rank_this_pc(self):
        entries = [
            ("lo", "127.0.0.1"),
            ("wlp0s20f3", "192.168.0.31"),
            ("br-f54b4fbe6153", "172.19.0.1"),
            ("docker0", "172.17.0.1"),
            ("vpn0", "100.96.0.2"),
        ]
        self.assertEqual(rank_addresses(entries), ["192.168.0.31"])

    def test_rank_order(self):
        entries = [
            ("eth0", "10.0.0.5"),
            ("x", "172.16.4.2"),
            ("wlan0", "192.168.1.20"),
            ("wlan0", "192.168.1.20"),
        ]
        self.assertEqual(rank_addresses(entries), ["192.168.1.20", "10.0.0.5", "172.16.4.2"])

    def test_rank_drops_link_local_and_tunnels(self):
        entries = [
            ("eth0", "169.254.3.4"),
            ("tun0", "10.8.0.2"),
            ("wg0", "10.9.0.2"),
            ("tailscale0", "100.101.1.1"),
            ("virbr0", "192.168.122.1"),
            ("eth1", "192.168.5.5"),
        ]
        self.assertEqual(rank_addresses(entries), ["192.168.5.5"])

    def test_rank_skips_bad_values(self):
        self.assertEqual(rank_addresses([("eth0", "not-an-ip"), ("eth1", "::1")]), [])

    def test_default_route_goes_first(self):
        entries = [("", "192.168.56.1"), ("", "172.25.160.1"), ("", "10.20.30.40"), ("", "10.20.30.40")]
        self.assertEqual(rank_addresses(entries, default="10.20.30.40"), ["10.20.30.40", "192.168.56.1", "172.25.160.1"])

    def test_default_route_ignored_when_filtered(self):
        entries = [("wlp0s20f3", "192.168.0.31"), ("vpn0", "100.96.0.2")]
        self.assertEqual(rank_addresses(entries, default="100.96.0.2"), ["192.168.0.31"])

    def test_vm_and_container_adapters_skipped(self):
        names = [
            "vboxnet0", "vmnet8", "lxcbr0", "lxdbr0", "incusbr0", "podman0", "cni0", "flannel.1", "kube-bridge",
            "waydroid0", "mpqemubr0", "mpbr0", "anbox0",  # Waydroid, Multipass (QEMU and LXD), Anbox
        ]  # fmt: skip
        entries = [(name, f"192.168.{i + 100}.1") for i, name in enumerate(names)] + [("wlp2s0", "10.0.0.12")]
        self.assertEqual(rank_addresses(entries), ["10.0.0.12"])

    def test_mac_virtual_interfaces_skipped(self):
        # Internet Sharing, UTM, Parallels and Docker Desktop add bridges; AirDrop and the like add the rest.
        names = ["bridge100", "bridge101", "utun3", "awdl0", "llw0", "anpi0"]
        entries = [(name, f"192.168.{i + 60}.1") for i, name in enumerate(names)] + [("en0", "192.168.1.20")]
        self.assertEqual(rank_addresses(entries, platform="darwin"), ["192.168.1.20"])

    def test_a_linux_bridge_can_hold_the_main_address(self):
        entries = [("bridge0", "192.168.1.20"), ("ap0", "192.168.12.1")]
        self.assertEqual(rank_addresses(entries, platform="linux"), ["192.168.1.20", "192.168.12.1"])

    def test_rank_empty(self):
        self.assertEqual(rank_addresses([]), [])


class UsableInterfaceTest(unittest.TestCase):
    def test_up_interfaces_are_usable(self):
        self.assertTrue(usable_interface(IFF_UP))
        self.assertTrue(usable_interface(IFF_UP | IFF_RUNNING | IFF_MULTICAST | 0x2))

    def test_down_interfaces_are_skipped(self):
        self.assertFalse(usable_interface(0))
        self.assertFalse(usable_interface(IFF_RUNNING | IFF_MULTICAST))

    def test_point_to_point_links_are_skipped(self):
        # VPNs and dial-up links: a phone on the Wi-Fi can't reach them.
        self.assertFalse(usable_interface(IFF_UP | IFF_POINTOPOINT))
        self.assertFalse(usable_interface(IFF_UP | IFF_RUNNING | IFF_POINTOPOINT))
        self.assertFalse(usable_interface(IFF_POINTOPOINT))

    def test_constants(self):
        self.assertEqual((netinfo.SIOCGIFFLAGS, netinfo.IFF_UP, netinfo.IFF_POINTOPOINT), (0x8913, 0x1, 0x10))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux interface reading")
class LinuxInterfacesTest(unittest.TestCase):
    """_linux_interfaces with a fake ioctl: name -> (IPv4 address or None, flags or None)."""

    def run_with(self, table):
        def fake_ioctl(fd, request, arg):
            name = arg[:16].rstrip(b"\0").decode()
            address, flags = table[name]
            if request == netinfo.SIOCGIFADDR:
                if address is None:
                    raise OSError(99, "no address")
                return arg[:20] + socket.inet_aton(address) + arg[24:]
            if request == netinfo.SIOCGIFFLAGS:
                if flags is None:
                    raise OSError(19, "no such device")
                return arg[:16] + struct.pack("H", flags) + arg[18:]
            raise AssertionError(hex(request))

        names = [(i + 1, name) for i, name in enumerate(table)]
        with mock.patch("fcntl.ioctl", fake_ioctl), mock.patch.object(netinfo.socket, "if_nameindex", lambda: names):
            return netinfo._linux_interfaces()

    def test_skips_down_and_point_to_point_interfaces(self):
        found = self.run_with(
            {
                "lo": ("127.0.0.1", IFF_UP | IFF_LOOPBACK | IFF_RUNNING),
                "wlp0s20f3": ("192.168.0.31", IFF_UP | IFF_RUNNING | IFF_MULTICAST),
                "enp3s0": ("10.0.0.5", 0x2 | IFF_MULTICAST),  # cable unplugged and interface down
                "ppp0": ("10.64.1.2", IFF_UP | IFF_POINTOPOINT | IFF_RUNNING),
                "eth1": (None, IFF_UP | IFF_RUNNING),
            }
        )
        self.assertEqual(found, [("lo", "127.0.0.1"), ("wlp0s20f3", "192.168.0.31")])

    def test_unreadable_flags_keep_the_interface(self):
        self.assertEqual(self.run_with({"wlan0": ("192.168.1.20", None)}), [("wlan0", "192.168.1.20")])

    def test_never_raises(self):
        def broken():
            raise OSError("no interfaces")

        with mock.patch.object(netinfo.socket, "if_nameindex", broken):
            self.assertIsInstance(list_ipv4(), list)


def fake_ioctl_for(table, address_request, flags_request, seen=None):
    """A fake ioctl answering for the interfaces in `table`: name -> (IPv4 address or None, flags or None)."""

    def fake_ioctl(fd, request, arg):
        if seen is not None:
            seen.add(request)
        name = arg[:16].rstrip(b"\0").decode()
        address, flags = table[name]
        if request == address_request:
            if address is None:
                raise OSError(49, "no address")
            return arg[:20] + socket.inet_aton(address) + arg[24:]
        if request == flags_request:
            if flags is None:
                raise OSError(6, "no such device")
            return arg[:16] + struct.pack("H", flags) + arg[18:]
        raise OSError(25, "inappropriate ioctl for device")  # what an unknown request gets

    return fake_ioctl


class MacInterfacesTest(unittest.TestCase):
    """macOS reads each interface's address and flags as Linux does, with the BSD request numbers (struct ifreq
    keeps both at the same places). So the Mac's own name (like ayla-mac.local) is never looked up: that is a
    .local lookup, which macOS 15 lets an app make only with Local Network access, and it can be slow."""

    TABLE = {
        "lo0": ("127.0.0.1", IFF_UP | IFF_LOOPBACK | IFF_RUNNING),
        "en0": ("192.168.1.23", IFF_UP | IFF_RUNNING | IFF_MULTICAST),  # Wi-Fi
        "utun3": ("10.8.0.2", IFF_UP | IFF_POINTOPOINT | IFF_RUNNING),  # a VPN
        "en5": (None, IFF_UP | IFF_RUNNING),  # no IPv4 address
        "bridge0": ("169.254.10.1", 0x2),  # down
    }

    def run_mac(self, ioctl, default="192.168.1.23"):
        self.lookups = []

        def gethostbyname_ex(name):
            self.lookups.append(name)
            return name, [], ["192.168.1.23", "169.254.3.3"]

        names = [(i + 1, name) for i, name in enumerate(self.TABLE)]
        with (
            mock.patch.object(netinfo.sys, "platform", "darwin"),
            mock.patch.object(netinfo, "_ioctl", ioctl),
            mock.patch.object(netinfo.socket, "if_nameindex", lambda: names),
            mock.patch.object(netinfo.socket, "gethostname", lambda: "ayla-mac.local"),
            mock.patch.object(netinfo.socket, "gethostbyname_ex", gethostbyname_ex),
            mock.patch.object(netinfo, "_default_route_address", lambda: default),
        ):
            return list_ipv4(), netinfo.lan_addresses()

    def test_reads_the_interfaces_and_never_looks_up_the_name(self):
        seen = set()
        found, ranked = self.run_mac(fake_ioctl_for(self.TABLE, netinfo.MAC_SIOCGIFADDR, netinfo.MAC_SIOCGIFFLAGS, seen))
        self.assertEqual(found, [("lo0", "127.0.0.1"), ("en0", "192.168.1.23")])
        self.assertEqual(ranked, ["192.168.1.23"])
        self.assertEqual(self.lookups, [])
        self.assertEqual(seen, {netinfo.MAC_SIOCGIFADDR, netinfo.MAC_SIOCGIFFLAGS})

    def test_falls_back_when_the_interfaces_cant_be_read(self):
        # Requests this Mac doesn't understand: the older way, by name and the default route, still works.
        found, ranked = self.run_mac(fake_ioctl_for(self.TABLE, 1, 2))
        self.assertEqual(found, [("", "192.168.1.23"), ("", "192.168.1.23"), ("", "169.254.3.3")])
        self.assertEqual(ranked, ["192.168.1.23"])
        self.assertEqual(set(self.lookups), {"ayla-mac.local"})  # once for each of the two calls

    def test_falls_back_when_reading_raises(self):
        def broken(fd, request, arg):
            raise RuntimeError("no ioctl here")

        found, _ = self.run_mac(broken)
        self.assertEqual(found[0], ("", "192.168.1.23"))

    def test_the_request_numbers(self):
        # _IOWR('i', n, struct ifreq): in and out (0xC0000000), the 32-byte struct ifreq, group 'i', number n.
        def iowr(number):
            return 0xC0000000 | (32 << 16) | (ord("i") << 8) | number

        self.assertEqual(netinfo.MAC_SIOCGIFADDR, iowr(33))
        self.assertEqual(netinfo.MAC_SIOCGIFFLAGS, iowr(17))
        self.assertEqual((netinfo.MAC_SIOCGIFADDR, netinfo.MAC_SIOCGIFFLAGS), (0xC0206921, 0xC0206911))

    @unittest.skipUnless(sys.platform == "darwin", "a real Mac")
    def test_a_real_mac(self):
        found = netinfo._interfaces(netinfo.MAC_SIOCGIFADDR, netinfo.MAC_SIOCGIFFLAGS)
        self.assertIn(("lo0", "127.0.0.1"), found)


@unittest.skipUnless(sys.platform.startswith("linux"), "a real Linux PC")
class RealLinuxInterfacesTest(unittest.TestCase):
    def test_the_loopback_interface_is_read(self):
        self.assertIn(("lo", "127.0.0.1"), netinfo._linux_interfaces())


class OtherSystemsTest(unittest.TestCase):
    """Windows when its adapters can't be read (and other systems): the PC's addresses by its name, with the
    default-route address first."""

    def run_with(self, hostname, by_name, default="192.168.1.5"):
        def gethostbyname_ex(name):
            if isinstance(by_name, BaseException):
                raise by_name
            return name, [], list(by_name)

        def gethostname():
            if isinstance(hostname, BaseException):
                raise hostname
            return hostname

        def unreadable():
            raise OSError("no adapters")

        with (
            mock.patch.object(netinfo.sys, "platform", "win32"),
            mock.patch.object(netinfo, "_read_windows_adapters", unreadable),
            mock.patch.object(netinfo.socket, "gethostname", gethostname),
            mock.patch.object(netinfo.socket, "gethostbyname_ex", gethostbyname_ex),
            mock.patch.object(netinfo, "_default_route_address", lambda: default),
        ):
            return list_ipv4(), netinfo.lan_addresses()

    def test_addresses_by_name_with_the_default_route_first(self):
        found, ranked = self.run_with("AYLA-PC", ["192.168.56.1", "10.0.0.7"], default="10.0.0.7")
        self.assertEqual(found, [("", "10.0.0.7"), ("", "192.168.56.1"), ("", "10.0.0.7")])
        self.assertEqual(ranked, ["10.0.0.7", "192.168.56.1"])

    def test_a_pc_name_that_cant_be_looked_up_still_gives_the_default_route(self):
        # A Windows PC name can hold characters a name lookup refuses (UnicodeError), or not resolve at all.
        for problem in (UnicodeError("label too long"), socket.gaierror(11001, "host not found"), OSError("no network")):
            with self.subTest(problem=problem):
                found, ranked = self.run_with("PC", problem)
                self.assertEqual(found, [("", "192.168.1.5")])
                self.assertEqual(ranked, ["192.168.1.5"])

    def test_no_pc_name_at_all(self):
        found, _ = self.run_with(OSError("no name"), ["10.0.0.7"])
        self.assertEqual(found, [("", "192.168.1.5")])

    def test_nothing_found_is_an_empty_list(self):
        found, ranked = self.run_with("PC", OSError("offline"), default=None)
        self.assertEqual((found, ranked), ([], []))


Adapter = netinfo.WindowsAdapter
UP, DOWN = 1, 2  # IfOperStatus
ETHERNET, WIFI, LOOPBACK, TUNNEL, VIRTUAL = 6, 71, 24, 131, 53  # IfType


def adapter(friendly, description, addresses, gateway=False, status=UP, if_type=ETHERNET):
    return Adapter(friendly, description, status, if_type, tuple(addresses), gateway)


# A Windows laptop with WSL, Hyper-V, VirtualBox, VMware and a few VPNs installed.
WIFI_ADAPTER = adapter("Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz", ["192.168.1.20"], gateway=True, if_type=WIFI)
VIRTUAL_ADAPTERS = [
    adapter("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", ["172.25.160.1"]),
    adapter("vEthernet (WSL (Hyper-V firewall))", "Hyper-V Virtual Ethernet Adapter #3", ["172.26.0.1"]),
    adapter("vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter #2", ["172.30.48.1"]),
    adapter("vEthernet (nat)", "Hyper-V Virtual Ethernet Adapter #4", ["172.17.80.1"]),  # Docker
    adapter("VirtualBox Host-Only Network", "VirtualBox Host-Only Ethernet Adapter", ["192.168.56.1"]),
    adapter("VMware Network Adapter VMnet1", "VMware Virtual Ethernet Adapter for VMnet1", ["192.168.100.1"]),
    adapter("VMware Network Adapter VMnet8", "VMware Virtual Ethernet Adapter for VMnet8", ["192.168.200.1"]),
    adapter("Npcap Loopback Adapter", "Npcap Loopback Adapter", ["10.255.255.1"]),
    adapter("OpenVPN TAP-Windows6", "TAP-Windows Adapter V9", ["10.8.0.6"]),
    adapter("OpenVPN Data Channel Offload", "OpenVPN Data Channel Offload", ["10.9.0.6"], if_type=VIRTUAL),
    adapter("home", "WireGuard Tunnel", ["10.10.0.2"], if_type=VIRTUAL),
    adapter("Tailscale", "Tailscale Tunnel", ["100.80.1.2"], if_type=VIRTUAL),
    adapter("ZeroTier One [8056c2e21c000001]", "ZeroTier Virtual Port", ["10.147.17.5"]),
    adapter("Bluetooth Network Connection", "Bluetooth Device (Personal Area Network)", ["192.168.44.2"]),
    adapter("Local Area Connection 3", "Wintun Userspace Tunnel", ["10.11.0.2"], if_type=VIRTUAL),
]


class WindowsAdapterChoiceTest(unittest.TestCase):
    """Which Windows adapters give viewer links, from the adapters' own names and gateways (plain data)."""

    def rank(self, adapters, default=None, platform="win32"):
        entries, gateways = netinfo._windows_entries(adapters)
        return rank_addresses(entries, default=default, platform=platform, gateways=gateways)

    def test_virtual_adapters_are_skipped(self):
        self.assertEqual(self.rank(VIRTUAL_ADAPTERS + [WIFI_ADAPTER], default="192.168.1.20"), ["192.168.1.20"])

    def test_skipped_without_a_default_route_too(self):
        # Before: with no default route, VirtualBox's 192.168.56.1 could be the first link.
        self.assertEqual(self.rank(VIRTUAL_ADAPTERS + [WIFI_ADAPTER]), ["192.168.1.20"])
        offline_wifi = adapter("Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz", ["192.168.1.20"], if_type=WIFI)
        self.assertEqual(self.rank(VIRTUAL_ADAPTERS + [offline_wifi]), ["192.168.1.20"])

    def test_each_kind_of_virtual_adapter_alone(self):
        for virtual in VIRTUAL_ADAPTERS:
            self.assertEqual(self.rank([virtual]), [], virtual.friendly)

    def test_the_names_carry_the_friendly_name_and_the_description(self):
        entries, gateways = netinfo._windows_entries([WIFI_ADAPTER, VIRTUAL_ADAPTERS[0]])
        self.assertEqual([address for _, address in entries], ["192.168.1.20", "172.25.160.1"])
        self.assertIn("Wi-Fi", entries[0][0])
        self.assertIn("Intel(R) Wi-Fi 6 AX201 160MHz", entries[0][0])
        self.assertIn("vEthernet (WSL)", entries[1][0])
        self.assertIn("Hyper-V Virtual Ethernet Adapter", entries[1][0])
        self.assertEqual(gateways, {"192.168.1.20"})
        for name, _ in entries:
            self.assertIsInstance(name, str)

    def test_adapters_that_are_down_loopback_or_tunnels_are_left_out(self):
        entries, _ = netinfo._windows_entries(
            [
                adapter("Ethernet", "Realtek PCIe GbE Family Controller", ["10.0.0.5"], status=DOWN),
                adapter("Loopback Pseudo-Interface 1", "Software Loopback Interface 1", ["127.0.0.1"], if_type=LOOPBACK),
                adapter("Teredo", "Teredo Tunneling Pseudo-Interface", ["10.1.1.1"], if_type=TUNNEL),
                adapter("Ethernet 2", "Intel(R) Ethernet Connection I219-V", ["192.168.0.40"], gateway=True),
                adapter("Ethernet 3", "Dormant", ["192.168.9.9"], status=5),
            ]
        )
        self.assertEqual([address for _, address in entries], ["192.168.0.40"])

    def test_every_address_of_an_adapter(self):
        entries, gateways = netinfo._windows_entries([adapter("Ethernet", "Intel", ["192.168.0.40", "10.0.0.40"], gateway=True)])
        self.assertEqual([address for _, address in entries], ["192.168.0.40", "10.0.0.40"])
        self.assertEqual(gateways, {"192.168.0.40", "10.0.0.40"})

    def test_adapters_with_a_gateway_come_first(self):
        # The PC's mobile hotspot (192.168.137.1) works for phones on it, so it stays, after the network
        # that has a router; without a default route the gateway still decides.
        hotspot = adapter("Local Area Connection* 10", "Microsoft Wi-Fi Direct Virtual Adapter #2", ["192.168.137.1"])
        ethernet = adapter("Ethernet", "Intel(R) Ethernet Connection I219-V", ["10.0.0.5"], gateway=True)
        self.assertEqual(self.rank([hotspot, ethernet]), ["10.0.0.5", "192.168.137.1"])
        self.assertEqual(self.rank([hotspot, ethernet], default="10.0.0.5"), ["10.0.0.5", "192.168.137.1"])

    def test_the_default_route_address_still_goes_first(self):
        hotspot = adapter("Local Area Connection* 10", "Microsoft Wi-Fi Direct Virtual Adapter #2", ["192.168.137.1"])
        ethernet = adapter("Ethernet", "Intel(R) Ethernet Connection I219-V", ["10.0.0.5"], gateway=True)
        self.assertEqual(self.rank([ethernet, hotspot], default="192.168.137.1"), ["192.168.137.1", "10.0.0.5"])
        # ... but only when it survives the filters.
        self.assertEqual(self.rank([ethernet, VIRTUAL_ADAPTERS[0]], default="172.25.160.1"), ["10.0.0.5"])

    def test_a_hyper_v_external_switch_keeps_the_pcs_own_address(self):
        # With an external virtual switch, the PC's own network address moves to a vEthernet adapter, which
        # has the router as its gateway; the internal ones (WSL, Default Switch) have none.
        external = adapter("vEthernet (External)", "Hyper-V Virtual Ethernet Adapter #2", ["192.168.1.20"], gateway=True)
        self.assertEqual(self.rank([external] + VIRTUAL_ADAPTERS[:3], default="192.168.1.20"), ["192.168.1.20"])
        self.assertEqual(self.rank([external] + VIRTUAL_ADAPTERS[:3]), ["192.168.1.20"])

    def test_an_external_switch_named_after_wsl_keeps_the_pcs_own_address(self):
        # People name their external switch themselves ("WSL bridge"); the gateway still shows it is the real one.
        external = adapter("vEthernet (WSL bridge)", "Hyper-V Virtual Ethernet Adapter #5", ["192.168.1.20"], gateway=True)
        self.assertEqual(self.rank([external] + VIRTUAL_ADAPTERS[:3]), ["192.168.1.20"])

    def test_windows_own_vpn_connections_are_skipped(self):
        # Windows' built-in VPN and dial-up connections are PPP adapters (IfType 23), which may have a gateway.
        ppp = adapter("Work VPN", "WAN Miniport (IKEv2)", ["10.200.0.5"], gateway=True, if_type=23)
        self.assertEqual(self.rank([ppp, WIFI_ADAPTER], default="10.200.0.5"), ["192.168.1.20"])

    def test_common_vpn_apps_are_skipped(self):
        vpns = [
            adapter("Ethernet 3", "Cisco AnyConnect Secure Mobility Client Virtual Miniport Adapter for Windows x64", ["10.20.0.5"]),
            adapter("Ethernet 4", "PANGP Virtual Ethernet Adapter Secure", ["10.21.0.5"]),
            adapter("Ethernet 5", "Fortinet SSL VPN Virtual Ethernet Adapter", ["10.22.0.5"]),
            adapter("NordLynx", "NordLynx Tunnel", ["10.5.0.2"], if_type=VIRTUAL),
            adapter("Mullvad", "Mullvad Tunnel", ["10.64.0.2"], if_type=VIRTUAL),
            adapter("Ethernet 6", "Juniper Networks Virtual Adapter", ["10.23.0.5"]),
            adapter("Ethernet 7", "Some Company VPN Adapter", ["10.24.0.5"], gateway=True),
        ]
        for vpn in vpns:
            with self.subTest(vpn=vpn.description):
                self.assertEqual(self.rank([vpn, WIFI_ADAPTER], default=vpn.addresses[0]), ["192.168.1.20"])

    def test_a_vpn_with_a_gateway_is_still_skipped(self):
        vpn = adapter("OpenVPN TAP-Windows6", "TAP-Windows Adapter V9", ["10.8.0.6"], gateway=True)
        self.assertEqual(self.rank([vpn, WIFI_ADAPTER], default="10.8.0.6"), ["192.168.1.20"])

    def test_a_windows_pc_inside_hyper_v_or_azure_keeps_its_adapter(self):
        # The guest's adapter is "Microsoft Hyper-V Network Adapter": the PC's real network, not a host switch.
        guest = adapter("Ethernet", "Microsoft Hyper-V Network Adapter", ["10.1.0.4"])
        self.assertEqual(self.rank([guest]), ["10.1.0.4"])

    def test_windows_words_apply_only_on_windows(self):
        entries = [("Bluetooth Network Connection / Bluetooth Device (Personal Area Network)", "192.168.44.2"),
                   ("VMware Network Adapter VMnet8 / VMware Virtual Ethernet Adapter", "192.168.200.1")]
        for platform in ("linux", "darwin"):
            self.assertEqual(rank_addresses(entries, platform=platform), ["192.168.44.2", "192.168.200.1"], platform)
        self.assertEqual(rank_addresses(entries, platform="win32"), [])

    def test_linux_style_names_are_still_skipped_everywhere(self):
        for platform in ("linux", "darwin", "win32"):
            self.assertEqual(rank_addresses([("docker0", "172.17.0.1"), ("wg0", "10.9.0.2")], platform=platform), [], platform)


# ---------- Windows: reading the adapters through GetAdaptersAddresses ----------

def utf16(text):
    return text.encode("utf-16-le") + b"\0\0"


def sockaddr_in(address):
    return struct.pack("<HH4s8x", 2, 0, socket.inet_aton(address))  # AF_INET is 2 on Windows


class FakeGetAdaptersAddresses:
    """Stands in for iphlpapi's GetAdaptersAddresses: lays the adapters out in the caller's buffer as Windows
    does (real IP_ADAPTER_ADDRESSES records, linked lists, UTF-16 names, sockaddr_in addresses), and asks for
    a bigger buffer first when the one given is too small."""

    def __init__(self, adapters, code=0):
        self.adapters = adapters  # (friendly, description, status, if_type, addresses, gateway addresses)
        self.code = code
        self.calls = []

    def layout(self, base, buffer):
        structs = netinfo._windows_structs()
        pieces = []  # (offset, bytes) to copy in
        offset = 0

        def take(size):
            nonlocal offset
            start = offset
            offset = (offset + size + 7) // 8 * 8
            return start

        records = []
        for friendly, description, status, if_type, addresses, gateways in self.adapters:
            record = take(ctypes.sizeof(structs.Adapter))
            names = (take(len(utf16(friendly))), take(len(utf16(description))))
            pieces += [(names[0], utf16(friendly)), (names[1], utf16(description))]
            unicast = []
            for address in addresses:
                at, sock = take(ctypes.sizeof(structs.Unicast)), take(16)
                pieces.append((sock, sockaddr_in(address)))
                unicast.append((at, sock))
            gateway = []
            for address in gateways:
                at, sock = take(ctypes.sizeof(structs.Unicast)), take(16)
                pieces.append((sock, sockaddr_in(address)))
                gateway.append((at, sock))
            records.append((record, names, status, if_type, unicast, gateway))
        if buffer is None:
            return offset
        for at, data in pieces:
            ctypes.memmove(base + at, data, len(data))

        def chain(items):
            for i, (at, sock) in enumerate(items):
                item = structs.Unicast.from_buffer(buffer, at)
                item.Length = ctypes.sizeof(structs.Unicast)
                item.Next = base + items[i + 1][0] if i + 1 < len(items) else None
                item.Address.lpSockaddr = base + sock
                item.Address.iSockaddrLength = 16
            return base + items[0][0] if items else None

        for i, (at, names, status, if_type, unicast, gateway) in enumerate(records):
            record = structs.Adapter.from_buffer(buffer, at)
            record.Length = ctypes.sizeof(structs.Adapter)
            record.Next = base + records[i + 1][0] if i + 1 < len(records) else None
            record.Description = base + names[1]
            record.FriendlyName = base + names[0]
            record.OperStatus = status
            record.IfType = if_type
            record.FirstUnicastAddress = chain(unicast)
            record.FirstGatewayAddress = chain(gateway)
        return offset

    def __call__(self, family, flags, buffer, size):
        self.calls.append((family, flags, size.value))
        if self.code:
            return self.code
        needed = self.layout(0, None)
        if size.value < needed:
            size.value = needed
            return 111  # ERROR_BUFFER_OVERFLOW
        self.layout(ctypes.addressof(buffer), buffer)
        return 0


class ReadWindowsAdaptersTest(unittest.TestCase):
    ADAPTERS = [
        ("Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz", 1, 71, ["192.168.1.20"], ["192.168.1.1"]),
        ("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", 1, 6, ["172.25.160.1"], []),
        ("Ethernet", "Realtek PCIe GbE Family Controller", 2, 6, [], []),
        ("Ethernet 2", "Intel(R) Ethernet", 1, 6, ["10.0.0.5", "10.0.0.6"], ["0.0.0.0"]),  # VPN-style 0.0.0.0
        ("Wi-Fi 2", "Café adapter ə", 1, 71, ["192.168.2.3"], ["0.0.0.0", "192.168.2.1"]),
    ]

    def test_the_records_are_read(self):
        fake = FakeGetAdaptersAddresses(self.ADAPTERS)
        adapters = netinfo._read_windows_adapters(fake)
        self.assertEqual(
            adapters,
            [
                Adapter("Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz", 1, 71, ("192.168.1.20",), True),
                Adapter("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", 1, 6, ("172.25.160.1",), False),
                Adapter("Ethernet", "Realtek PCIe GbE Family Controller", 2, 6, (), False),
                Adapter("Ethernet 2", "Intel(R) Ethernet", 1, 6, ("10.0.0.5", "10.0.0.6"), False),
                Adapter("Wi-Fi 2", "Café adapter ə", 1, 71, ("192.168.2.3",), True),
            ],
        )

    def test_asks_for_ipv4_with_gateways_and_nothing_else(self):
        fake = FakeGetAdaptersAddresses(self.ADAPTERS)
        netinfo._read_windows_adapters(fake)
        family, flags, _ = fake.calls[0]
        self.assertEqual(family, 2)  # AF_INET
        # GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER | GAA_FLAG_INCLUDE_GATEWAYS
        self.assertEqual(flags, 0x2 | 0x4 | 0x8 | 0x80)

    def test_a_bigger_buffer_when_asked(self):
        many = [(f"Ethernet {i}", "Intel(R) Ethernet " * 20, 1, 6, [f"10.0.{i}.5"], []) for i in range(80)]
        fake = FakeGetAdaptersAddresses(many)
        adapters = netinfo._read_windows_adapters(fake)
        self.assertEqual(len(adapters), 80)
        self.assertEqual(len(fake.calls), 2)
        self.assertGreater(fake.calls[1][2], fake.calls[0][2])

    def test_errors_raise_so_the_caller_falls_back(self):
        for code in (87, 8, 1):  # ERROR_INVALID_PARAMETER, ERROR_NOT_ENOUGH_MEMORY, anything else
            with self.assertRaises(OSError):
                netinfo._read_windows_adapters(FakeGetAdaptersAddresses(self.ADAPTERS, code=code))

    def test_no_adapters_at_all(self):
        self.assertEqual(netinfo._read_windows_adapters(FakeGetAdaptersAddresses([], code=232)), [])  # ERROR_NO_DATA

    def test_a_buffer_that_keeps_growing_gives_up(self):
        def always_more(family, flags, buffer, size):
            size.value += 1000
            return 111

        with self.assertRaises(OSError):
            netinfo._read_windows_adapters(always_more)

    def test_the_record_layout(self):
        # IP_ADAPTER_ADDRESSES_LH as the Windows SDK lays it out (x64; 32-bit in brackets).
        structs = netinfo._windows_structs()
        wide = ctypes.sizeof(ctypes.c_void_p) == 8
        offsets = {
            "Next": 8,
            "FirstUnicastAddress": 24 if wide else 16,
            "Description": 64 if wide else 36,
            "FriendlyName": 72 if wide else 40,
            "IfType": 100 if wide else 64,
            "OperStatus": 104 if wide else 68,
            "FirstGatewayAddress": 208 if wide else 164,
        }
        for field, offset in offsets.items():
            self.assertEqual(getattr(structs.Adapter, field).offset, offset, field)
        self.assertEqual(structs.Unicast.Address.offset, 16 if wide else 12)
        self.assertEqual(structs.Unicast.Next.offset, 8)

    def test_a_short_record_is_refused(self):
        # A record shorter than the fields read (an old Windows): fall back rather than read past it.
        fake = FakeGetAdaptersAddresses(self.ADAPTERS)
        original = fake.layout

        def short(base, buffer):
            size = original(base, buffer)
            if buffer is not None:
                netinfo._windows_structs().Adapter.from_buffer(buffer, 0).Length = 64
            return size

        fake.layout = short
        with self.assertRaises(OSError):
            netinfo._read_windows_adapters(fake)


class WindowsListTest(unittest.TestCase):
    """list_ipv4 and lan_addresses on Windows: the adapters, or the older way when they can't be read."""

    def run_win(self, read, default="192.168.1.20"):
        self.lookups = []

        def gethostbyname_ex(name):
            self.lookups.append(name)
            return name, [], ["192.168.56.1", "192.168.1.20"]

        with (
            mock.patch.object(netinfo.sys, "platform", "win32"),
            mock.patch.object(netinfo, "_read_windows_adapters", read),
            mock.patch.object(netinfo.socket, "gethostname", lambda: "AYLA-PC"),
            mock.patch.object(netinfo.socket, "gethostbyname_ex", gethostbyname_ex),
            mock.patch.object(netinfo, "_default_route_address", lambda: default),
        ):
            return list_ipv4(), netinfo.lan_addresses()

    def test_the_adapters_are_used(self):
        found, ranked = self.run_win(lambda: VIRTUAL_ADAPTERS[:5] + [WIFI_ADAPTER])
        self.assertEqual([address for _, address in found], ["172.25.160.1", "172.26.0.1", "172.30.48.1", "172.17.80.1", "192.168.56.1", "192.168.1.20"])
        self.assertEqual(ranked, ["192.168.1.20"])
        self.assertEqual(self.lookups, [])

    def test_the_gateway_reaches_the_ranking(self):
        hotspot = adapter("Local Area Connection* 10", "Microsoft Wi-Fi Direct Virtual Adapter #2", ["192.168.137.1"])
        ethernet = adapter("Ethernet", "Intel(R) Ethernet Connection I219-V", ["10.0.0.5"], gateway=True)
        _, ranked = self.run_win(lambda: [hotspot, ethernet], default=None)
        self.assertEqual(ranked, ["10.0.0.5", "192.168.137.1"])

    def test_any_failure_falls_back_to_the_older_way(self):
        def raises(error):
            def read():
                raise error
            return read

        for read in (raises(OSError(87, "bad")), raises(AttributeError("no WinDLL")), raises(ValueError("odd")), lambda: []):
            with self.subTest(read=read):
                found, ranked = self.run_win(read)
                self.assertEqual(found, [("", "192.168.1.20"), ("", "192.168.56.1"), ("", "192.168.1.20")])
                self.assertEqual(ranked, ["192.168.1.20", "192.168.56.1"])

    def test_only_down_adapters_fall_back_too(self):
        found, _ = self.run_win(lambda: [adapter("Ethernet", "Intel", ["10.0.0.5"], status=DOWN)])
        self.assertEqual(found[0], ("", "192.168.1.20"))

    @unittest.skipIf(sys.platform == "win32", "Windows has iphlpapi")
    def test_without_iphlpapi_it_falls_back(self):
        # Here the real reader fails (no iphlpapi, no ctypes.WinDLL): the older way is used, without raising.
        with (
            mock.patch.object(netinfo.sys, "platform", "win32"),
            mock.patch.object(netinfo.socket, "gethostname", lambda: "AYLA-PC"),
            mock.patch.object(netinfo.socket, "gethostbyname_ex", lambda name: (name, [], ["10.0.0.7"])),
            mock.patch.object(netinfo, "_default_route_address", lambda: None),
        ):
            self.assertEqual(list_ipv4(), [("", "10.0.0.7")])


@unittest.skipUnless(sys.platform == "win32", "GetAdaptersAddresses exists only on Windows")
class RealWindowsAdaptersTest(unittest.TestCase):
    def test_the_real_adapters(self):
        adapters = netinfo._read_windows_adapters()
        self.assertIsInstance(adapters, list)
        entries, gateways = netinfo._windows_entries(adapters)
        for name, address in entries:
            self.assertIsInstance(name, str)
            self.assertIsInstance(address, str)
            self.assertEqual(ipaddress.ip_address(address).version, 4)
        self.assertLessEqual(gateways, {address for _, address in entries})
        found = list_ipv4()
        for name, address in found:
            self.assertIsInstance(name, str)
            self.assertEqual(ipaddress.ip_address(address).version, 4)
        default = netinfo._default_route_address()
        if default:
            self.assertIn(default, [address for _, address in entries])
            self.assertIn(default, [address for _, address in found])


class ListIpv4Test(unittest.TestCase):
    def test_list_ipv4_runs(self):
        result = list_ipv4()
        self.assertIsInstance(result, list)
        for name, ip in result:
            self.assertIsInstance(name, str)
            self.assertIsInstance(ip, str)


if __name__ == "__main__":
    unittest.main()
