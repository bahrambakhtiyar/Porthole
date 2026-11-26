import socket
import threading
import time
import unittest

from porthole.discovery import START_FAILED, Discovery
from porthole.mdns import Announcer


class FakeAnnouncer:
    def __init__(self, name, ip, fail=None, rename=None):
        self.name = rename or name
        self.ip = ip
        self.fail = fail
        self.started = False
        self.stopped = False

    @property
    def hostname(self):
        return f"{self.name}.local"

    def start(self):
        if self.fail:
            raise OSError(98, self.fail)
        self.started = True

    def stop(self):
        self.started = False
        self.stopped = True


class DiscoveryTest(unittest.TestCase):
    def make(self, ip="192.168.1.20", fail=None, rename=None):
        self.made = []

        def factory(name, address):
            announcer = FakeAnnouncer(name, address, fail, rename)
            self.made.append(announcer)
            return announcer

        return Discovery("porthole", ip, 8765, announcer_factory=factory)

    def test_nothing_is_announced_until_asked(self):
        discovery = self.make()
        self.assertEqual(self.made, [])
        state = discovery.state()
        self.assertFalse(state["enabled"])
        self.assertTrue(state["available"])
        self.assertEqual(state["host"], "porthole.local")

    def test_enable_announces_and_reports_the_address(self):
        discovery = self.make()
        state = discovery.set_enabled(True)
        self.assertTrue(self.made[0].started)
        self.assertEqual(
            state,
            {
                "enabled": True,
                "available": True,
                "blocked": False,
                "host": "porthole.local",
                "url": "http://porthole.local:8765",
                "short_url": None,
                "error": "",
            },
        )

    def test_state_uses_the_name_the_announcer_picked(self):
        discovery = self.make(rename="porthole-2")
        state = discovery.set_enabled(True)
        self.assertEqual(state["host"], "porthole-2.local")
        self.assertEqual(state["url"], "http://porthole-2.local:8765")

    def test_short_url_when_port_80_is_served(self):
        discovery = self.make()
        self.assertIsNone(discovery.short_port)
        discovery.short_port = 80
        self.assertEqual(discovery.set_enabled(True)["short_url"], "http://porthole.local")

    def test_disable_withdraws_the_name(self):
        discovery = self.make()
        discovery.set_enabled(True)
        state = discovery.set_enabled(False)
        self.assertTrue(self.made[0].stopped)
        self.assertFalse(state["enabled"])
        self.assertTrue(state["available"])

    def test_enable_twice_keeps_one_announcer(self):
        discovery = self.make()
        discovery.set_enabled(True)
        discovery.set_enabled(True)
        self.assertEqual(len(self.made), 1)

    def test_unavailable_when_the_name_cant_be_announced(self):
        state = self.make(fail="Address already in use").set_enabled(True)
        self.assertFalse(state["enabled"])
        self.assertFalse(state["available"])
        self.assertTrue(state["error"])
        # Short and plain: no system error text.
        self.assertNotIn("Address already in use", state["error"])
        self.assertNotIn("98", state["error"])
        self.assertLess(len(state["error"]), 80)

    def test_failed_start_can_be_turned_off_and_tried_again(self):
        discovery = self.make(fail="busy")
        discovery.set_enabled(True)
        state = discovery.set_enabled(False)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["error"], "")
        discovery.set_enabled(True)
        self.assertEqual(len(self.made), 2)

    def test_unavailable_without_a_network_address(self):
        discovery = self.make(ip=None)
        self.assertFalse(discovery.state()["available"])
        state = discovery.set_enabled(True)
        self.assertFalse(state["enabled"])
        self.assertFalse(state["available"])
        self.assertIn("no network address", state["error"])
        self.assertEqual(self.made, [])

    def test_blocked_reason_refuses_to_enable(self):
        discovery = self.make()
        discovery.blocked_reason = "Easy address is turned off for this run."
        state = discovery.set_enabled(True)
        self.assertEqual(self.made, [])
        self.assertFalse(state["enabled"])
        self.assertFalse(state["available"])
        self.assertEqual(state["error"], "Easy address is turned off for this run.")
        self.assertIs(state["blocked"], True)  # the page shows the switch off, with a plain note
        self.assertEqual(discovery.state(), state)

    def test_not_blocked_otherwise(self):
        discovery = self.make()
        self.assertIs(discovery.state()["blocked"], False)
        self.assertIs(discovery.set_enabled(True)["blocked"], False)

    def test_set_address_moves_an_enabled_name(self):
        discovery = self.make()
        discovery.set_enabled(True)
        discovery.set_address("10.0.0.5")
        self.assertTrue(self.made[0].stopped)  # goodbye on the old address
        self.assertEqual(self.made[1].ip, "10.0.0.5")
        self.assertTrue(self.made[1].started)
        self.assertTrue(discovery.state()["enabled"])

    def test_set_address_while_off_only_remembers_it(self):
        discovery = self.make()
        discovery.set_address("10.0.0.5")
        self.assertEqual(self.made, [])
        discovery.set_enabled(True)
        self.assertEqual(self.made[0].ip, "10.0.0.5")

    def test_set_address_to_the_same_ip_does_nothing(self):
        discovery = self.make()
        discovery.set_enabled(True)
        discovery.set_address("192.168.1.20")
        self.assertEqual(len(self.made), 1)
        self.assertTrue(self.made[0].started)

    def test_losing_the_address_withdraws_the_name_until_it_comes_back(self):
        discovery = self.make()
        discovery.set_enabled(True)
        discovery.set_address(None)
        self.assertTrue(self.made[0].stopped)
        state = discovery.state()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["available"])
        discovery.set_address("10.0.0.5")
        self.assertTrue(self.made[1].started)
        self.assertTrue(discovery.state()["enabled"])

    def test_stop(self):
        discovery = self.make()
        discovery.set_enabled(True)
        discovery.stop()
        self.assertTrue(self.made[0].stopped)
        self.assertFalse(discovery.state()["enabled"])

    def test_stop_is_final(self):
        discovery = self.make()
        discovery.stop()
        self.assertFalse(discovery.set_enabled(True)["enabled"])
        self.assertEqual(self.made, [])

    def test_threads_never_start_two_announcers(self):
        discovery = self.make()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(20):
                discovery.set_enabled(True)
                discovery.state()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(self.made), 1)
        self.assertEqual(sum(1 for a in self.made if a.started), 1)


PHONE = "192.168.1.30"  # watching by the name
NEWCOMER = "192.168.1.31"


class StaysForViewersTest(unittest.TestCase):
    """Turning Easy address off (Ask off does it too) stops new visitors only: the name is still answered while
    devices that came by it are connected (or back within the wait), and only those devices may use it; then it
    goes. Before, the name went at once, so they kept their picture but couldn't reconnect or get the next share."""

    def make(self, linger=0.2, look=0.2):
        self.made = []

        def factory(name, address):
            announcer = FakeAnnouncer(name, address)
            self.made.append(announcer)
            return announcer

        return Discovery(
            "porthole", "192.168.1.20", 8765, announcer_factory=factory, linger_seconds=linger, look_seconds=look
        )

    def wait_until(self, condition, seconds=3):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def test_off_keeps_the_name_for_devices_watching_by_it_only(self):
        discovery = self.make()
        self.assertTrue(discovery.admits(NEWCOMER))  # nothing stays yet: the usual rules
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        state = discovery.set_enabled(False)
        self.assertFalse(state["enabled"])  # off for the page, "/" and old links: nobody new is sent to the room
        self.assertEqual(state["host"], "porthole.local")
        self.assertTrue(self.made[0].started)  # still answered
        self.assertFalse(self.made[0].stopped)
        self.assertTrue(discovery.admits(PHONE))
        self.assertFalse(discovery.admits(NEWCOMER))
        self.assertEqual(discovery.state(), state)

    def test_the_name_goes_once_they_have_left_and_the_wait_is_over(self):
        discovery = self.make(linger=0.5)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.guest_arrived(PHONE)  # two tabs on the phone
        discovery.set_enabled(False)
        discovery.guest_left(PHONE)
        time.sleep(0.7)
        self.assertFalse(self.made[0].stopped)  # one tab still watching
        discovery.guest_left(PHONE)
        self.assertFalse(self.made[0].stopped)  # it may be back in a moment (a blip, a locked phone)
        self.assertTrue(self.wait_until(lambda: self.made[0].stopped))
        self.assertTrue(discovery.admits(NEWCOMER))  # the name is gone: the usual rules again
        self.assertEqual(len(self.made), 1)

    def test_a_device_back_within_the_wait_keeps_it(self):
        discovery = self.make(linger=1)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.set_enabled(False)
        discovery.guest_left(PHONE)
        time.sleep(0.1)
        discovery.guest_arrived(PHONE)  # its stream came back after a blip
        time.sleep(1.2)
        self.assertFalse(self.made[0].stopped)
        self.assertTrue(discovery.admits(PHONE))

    def test_a_device_reconnecting_when_it_goes_off_counts(self):
        discovery = self.make(linger=1)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.guest_left(PHONE)  # between two streams
        discovery.set_enabled(False)
        self.assertFalse(self.made[0].stopped)
        self.assertTrue(discovery.admits(PHONE))
        self.assertTrue(self.wait_until(lambda: self.made[0].stopped))  # it never came back

    def test_a_device_gone_longer_than_the_wait_doesnt_count(self):
        discovery = self.make(linger=0.1)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.guest_left(PHONE)
        time.sleep(0.3)
        discovery.set_enabled(False)
        self.assertTrue(self.made[0].stopped)

    def test_off_keeps_the_name_a_moment_for_devices_waiting_for_a_share(self):
        # A device on "Nothing shared yet" by the name has no stream, so the name went at once: its next check found
        # nothing, and it waited for ever. Kept a moment for nobody's use, its next check gets "Use the link".
        discovery = self.make(look=0.4)
        discovery.set_enabled(True)
        discovery.looker_seen(NEWCOMER)
        self.assertFalse(discovery.set_enabled(False)["enabled"])
        self.assertFalse(self.made[0].stopped)
        self.assertFalse(discovery.admits(NEWCOMER))  # it may not use the name: it is told to use the link
        self.assertTrue(self.wait_until(lambda: self.made[0].stopped))
        self.assertTrue(discovery.admits(NEWCOMER))

    def test_a_device_that_waited_long_ago_doesnt_keep_it(self):
        discovery = self.make(look=0.1)
        discovery.set_enabled(True)
        discovery.looker_seen(NEWCOMER)
        time.sleep(0.3)
        discovery.set_enabled(False)
        self.assertTrue(self.made[0].stopped)

    def test_the_waiting_devices_moment_outlasts_a_watcher_who_leaves_early(self):
        discovery = self.make(linger=0.05, look=0.6)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.looker_seen(NEWCOMER)
        discovery.set_enabled(False)
        discovery.guest_left(PHONE)
        time.sleep(0.3)
        self.assertFalse(self.made[0].stopped)  # the phone's wait is over, the waiting device's isn't
        self.assertFalse(discovery.admits(PHONE))
        self.assertTrue(self.wait_until(lambda: self.made[0].stopped))

    def test_off_with_nobody_watching_by_the_name_withdraws_it_at_once(self):
        discovery = self.make()
        discovery.set_enabled(True)
        self.assertFalse(discovery.set_enabled(False)["enabled"])
        self.assertTrue(self.made[0].stopped)
        self.assertTrue(discovery.admits(NEWCOMER))

    def test_turning_it_off_again_keeps_the_same_devices(self):
        # The sharer page sends its saved choice again (a reload, Use this one): that must not let anyone else in.
        discovery = self.make(linger=5)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.set_enabled(False)
        discovery.guest_arrived(NEWCOMER)  # can't happen by the name (the Host check refuses it), but whatever comes
        discovery.set_enabled(False)
        self.assertTrue(discovery.admits(PHONE))
        self.assertFalse(discovery.admits(NEWCOMER))
        self.assertFalse(self.made[0].stopped)
        discovery.stop()

    def test_on_again_while_it_stays_keeps_the_same_announcer(self):
        discovery = self.make(linger=5)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.set_enabled(False)
        self.assertTrue(discovery.set_enabled(True)["enabled"])
        self.assertEqual(len(self.made), 1)
        self.assertTrue(discovery.admits(NEWCOMER))
        discovery.guest_left(PHONE)
        discovery.set_enabled(False)  # off again with the phone just gone: it may still come back
        self.assertFalse(self.made[0].stopped)
        discovery.stop()

    def test_quitting_withdraws_it_at_once(self):
        discovery = self.make(linger=5)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.set_enabled(False)
        discovery.stop()
        self.assertTrue(self.made[0].stopped)

    def test_a_new_address_ends_it(self):
        # The PC moved to another network: devices on the old one can't reach it by any name.
        discovery = self.make(linger=5)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.set_enabled(False)
        discovery.set_address("10.0.0.5")
        self.assertTrue(self.made[0].stopped)
        self.assertEqual(len(self.made), 1)  # off: not announced on the new address
        self.assertTrue(discovery.admits(NEWCOMER))

    def test_devices_that_came_by_the_address_dont_keep_the_name(self):
        discovery = self.make()
        discovery.set_enabled(True)
        discovery.set_enabled(False)  # only devices that came by the link (no guest_arrived) are watching
        self.assertTrue(self.made[0].stopped)

    def test_a_device_turned_away_doesnt_keep_it(self):
        # Denied or removed by the sharer: not one of the devices watching, even with its stream still open.
        discovery = self.make(linger=5)
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.guest_left(PHONE, turned_away=True)
        discovery.set_enabled(False)
        self.assertTrue(self.made[0].stopped)
        # And turned away while the name stays: it goes for that device at once, and with it the name, if it was the last.
        discovery.set_enabled(True)
        discovery.guest_arrived(PHONE)
        discovery.guest_arrived(NEWCOMER)
        discovery.set_enabled(False)
        discovery.guest_left(NEWCOMER, turned_away=True)
        self.assertTrue(self.wait_until(lambda: not discovery.admits(NEWCOMER), 1))
        self.assertTrue(discovery.admits(PHONE))
        self.assertFalse(self.made[1].stopped)
        discovery.guest_left(PHONE, turned_away=True)
        self.assertTrue(self.wait_until(lambda: self.made[1].stopped, 1))

    def test_a_blocked_or_failed_name_has_nothing_to_keep(self):
        discovery = self.make()
        discovery.blocked_reason = "Easy address is turned off for this run."
        discovery.guest_arrived(PHONE)
        discovery.set_enabled(False)
        self.assertEqual(self.made, [])


class SlowAnnouncer(FakeAnnouncer):
    def start(self):
        time.sleep(0.8)  # like the real name check on the network
        super().start()


class DiscoveryLockTest(unittest.TestCase):
    def test_state_stays_quick_while_the_name_is_checked(self):
        discovery = Discovery("porthole", "192.168.1.20", 8765, announcer_factory=lambda n, ip: SlowAnnouncer(n, ip))
        worker = threading.Thread(target=discovery.set_enabled, args=(True,))
        worker.start()
        time.sleep(0.1)
        began = time.monotonic()
        discovery.state()
        self.assertLess(time.monotonic() - began, 0.2)
        worker.join()
        self.assertTrue(discovery.state()["enabled"])


class HeldPortSocket:
    """A UDP socket that can't have port 5353: on Windows the system's DNS client may hold it for itself."""

    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.closed = False
        self.options = []

    def setsockopt(self, level, option, value):
        self.options.append(option)
        if self.fail_on == "join" and option == socket.IP_ADD_MEMBERSHIP:
            raise OSError(10065, "A socket operation was attempted to an unreachable host")

    def bind(self, address):
        if self.fail_on == "bind":
            raise OSError(10013, "An attempt was made to access a socket in a way forbidden by its access permissions")

    def settimeout(self, seconds):
        pass

    def sendto(self, data, destination):
        if self.fail_on == "send":
            # macOS 15 blocks multicast until the person allows Local Network access for the app.
            raise OSError(65, "No route to host")

    def recvfrom(self, size):
        raise socket.timeout()

    def close(self):
        self.closed = True


class HeldPortTest(unittest.TestCase):
    """The real Announcer when port 5353 can't be used: a plain "not available" state, never a crash,
    and no socket left open."""

    def run_with(self, fail_on):
        self.sockets = []

        def open_socket(*args):
            sock = HeldPortSocket(fail_on)
            self.sockets.append(sock)
            return sock

        discovery = Discovery(
            "porthole", "192.168.1.20", 8765, announcer_factory=lambda name, ip: Announcer(name, ip, socket_factory=open_socket)
        )
        return discovery, discovery.set_enabled(True)

    def test_port_held_by_another_program(self):
        for fail_on in ("bind", "join"):
            with self.subTest(fail_on=fail_on):
                discovery, state = self.run_with(fail_on)
                self.assertEqual(
                    (state["enabled"], state["available"], state["error"]), (False, False, START_FAILED), fail_on
                )
                self.assertEqual(len(self.sockets), 1)
                self.assertTrue(self.sockets[0].closed)
                self.assertIn("forbidden" if fail_on == "bind" else "unreachable", discovery.detail)
                # Turning it off and on again tries once more, still without raising.
                discovery.set_enabled(False)
                self.assertEqual(discovery.set_enabled(True)["error"], START_FAILED)
                discovery.stop()

    def test_a_mac_refusing_to_send_before_local_network_access_is_allowed(self):
        discovery, state = self.run_with("send")
        self.assertEqual((state["enabled"], state["available"], state["error"]), (False, False, START_FAILED))
        self.assertEqual(len(self.sockets), 1)
        self.assertTrue(self.sockets[0].closed)
        self.assertIn("No route to host", discovery.detail)
        discovery.stop()

    def test_shares_port_5353_with_the_systems_own_responder(self):
        # macOS's mDNSResponder (and avahi) keep port 5353 open with SO_REUSEPORT: both sides must ask for it.
        discovery, _ = self.run_with("bind")
        options = self.sockets[0].options
        self.assertIn(socket.SO_REUSEADDR, options)
        if hasattr(socket, "SO_REUSEPORT"):  # Linux and macOS; Windows has none
            self.assertIn(socket.SO_REUSEPORT, options)
        discovery.stop()


if __name__ == "__main__":
    unittest.main()
