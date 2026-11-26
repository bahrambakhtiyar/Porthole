import socket
import struct
import threading
import time
import unittest

from porthole.mdns import (
    MDNS_GROUP,
    MDNS_PORT,
    TYPE_ANY,
    Announcer,
    a_record,
    answer,
    answers_for,
    encode_name,
    nsec_record,
    parse_questions,
    read_name,
    response,
)

NAME = "screen.local"
IP = "192.168.1.20"


def query(name, qtype=1, qid=0, unicast=False):
    header = struct.pack(">HHHHHH", qid, 0, 1, 0, 0, 0)
    qclass = 0x8001 if unicast else 1
    return header + encode_name(name) + struct.pack(">HH", qtype, qclass)


class NameTest(unittest.TestCase):
    def test_encode_and_read(self):
        data = encode_name("Screen.Local")
        self.assertEqual(data, b"\x06Screen\x05Local\x00")
        self.assertEqual(read_name(data, 0), ("screen.local", len(data)))

    def test_read_follows_compression_pointer(self):
        packet = b"\x00" * 12 + encode_name("screen.local") + b"\x03www\xc0\x0c"
        name, end = read_name(packet, 12 + len(encode_name("screen.local")))
        self.assertEqual(name, "www.screen.local")
        self.assertEqual(end, len(packet))

    def test_pointer_loop_is_rejected(self):
        packet = b"\x00" * 12 + b"\xc0\x0c"
        with self.assertRaises(ValueError):
            read_name(packet, 12)


class QuestionTest(unittest.TestCase):
    def test_parse_questions(self):
        self.assertEqual(parse_questions(query("Screen.local", unicast=True)), [("screen.local", 1, True)])

    def test_responses_and_junk_have_no_questions(self):
        self.assertEqual(parse_questions(response(NAME, IP)), [])
        self.assertEqual(parse_questions(b"\x00\x01"), [])
        self.assertEqual(parse_questions(b"\x00" * 4 + b"\x00\x05" + b"\x00" * 6 + b"\x01"), [])


class AnswerTest(unittest.TestCase):
    def test_multicast_answer_for_our_name(self):
        replies = answer(query(NAME), ("192.168.1.50", MDNS_PORT), NAME, IP)
        self.assertEqual(len(replies), 1)
        data, dest = replies[0]
        self.assertEqual(dest, (MDNS_GROUP, MDNS_PORT))
        qid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
        self.assertEqual((qid, flags, qd, an), (0, 0x8400, 0, 1))
        self.assertEqual(answers_for(data, NAME), [IP])
        self.assertIn(struct.pack(">HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(IP), data)

    def test_any_query_is_answered(self):
        self.assertEqual(len(answer(query(NAME, qtype=255), ("192.168.1.50", MDNS_PORT), NAME, IP)), 1)

    def test_legacy_unicast_answer(self):
        replies = answer(query(NAME, qid=0x1234), ("192.168.1.50", 40000), NAME, IP)
        data, dest = replies[0]
        self.assertEqual(dest, ("192.168.1.50", 40000))
        qid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
        self.assertEqual((qid, qd, an), (0x1234, 1, 1))
        self.assertIn(struct.pack(">HHIH", 1, 1, 10, 4) + socket.inet_aton(IP), data)
        self.assertEqual(answers_for(data, NAME), [IP])

    def test_other_names_types_and_responses_get_nothing(self):
        source = ("192.168.1.50", MDNS_PORT)
        self.assertEqual(answer(query("printer.local"), source, NAME, IP), [])
        self.assertEqual(answer(query("printer.local", qtype=28), source, NAME, IP), [])
        self.assertEqual(answer(query(NAME, qtype=16), source, NAME, IP), [])  # TXT
        self.assertEqual(answer(response(NAME, IP), source, NAME, IP), [])

    # --- AAAA: say "no IPv6" with NSEC (RFC 6762 section 6.1), so clients stop waiting ---

    NSEC_RDATA = encode_name(NAME) + bytes([0, 1, 0x40])  # next name = us; window 0, 1 byte, only A

    def test_nsec_record(self):
        self.assertEqual(
            nsec_record(NAME, 120),
            encode_name(NAME) + struct.pack(">HHIH", 47, 0x8001, 120, len(self.NSEC_RDATA)) + self.NSEC_RDATA,
        )
        self.assertEqual(nsec_record(NAME, 10, cache_flush=False)[len(encode_name(NAME)) :][:8], struct.pack(">HHI", 47, 1, 10))

    def test_aaaa_question_gets_nsec(self):
        replies = answer(query("Screen.Local", qtype=28), ("192.168.1.50", MDNS_PORT), NAME, IP)
        self.assertEqual(len(replies), 1)
        data, dest = replies[0]
        self.assertEqual(dest, (MDNS_GROUP, MDNS_PORT))
        self.assertEqual(struct.unpack(">HHHHHH", data[:12]), (0, 0x8400, 0, 1, 0, 0))
        self.assertEqual(data[12:], nsec_record(NAME, 120))
        self.assertEqual(answers_for(data, NAME), [])

    def test_legacy_unicast_aaaa_gets_nsec_with_the_question(self):
        replies = answer(query(NAME, qtype=28, qid=0x55), ("192.168.1.50", 40000), NAME, IP)
        data, dest = replies[0]
        self.assertEqual(dest, ("192.168.1.50", 40000))
        qid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
        self.assertEqual((qid, flags, qd, an), (0x55, 0x8400, 1, 1))
        question = encode_name(NAME) + struct.pack(">HH", 28, 1)
        self.assertEqual(data[12:], question + nsec_record(NAME, 10, cache_flush=False))

    # --- one-shot (legacy unicast) queries only from the local network ---

    def test_legacy_unicast_from_a_public_address_is_ignored(self):
        for source in (("8.8.8.8", 40000), ("100.64.1.2", 40000), ("not-an-ip", 40000)):
            self.assertEqual(answer(query(NAME, qid=1), source, NAME, IP), [], source)

    def test_legacy_unicast_from_private_and_link_local_addresses(self):
        for address in ("10.1.2.3", "172.16.5.5", "192.168.1.50", "169.254.10.20", "127.0.0.1"):
            self.assertEqual(len(answer(query(NAME, qid=1), (address, 40000), NAME, IP)), 1, address)

    def test_multicast_query_from_any_address_is_answered_on_the_group(self):
        replies = answer(query(NAME), ("8.8.8.8", MDNS_PORT), NAME, IP)
        self.assertEqual([dest for _data, dest in replies], [(MDNS_GROUP, MDNS_PORT)])

    # --- bad packets never raise ---

    def test_truncated_packets_never_raise(self):
        whole = response(NAME, IP)
        for cut in range(len(whole)):
            self.assertEqual(answers_for(whole[:cut], NAME), [], cut)
            self.assertEqual(answer(whole[:cut], ("192.168.1.50", MDNS_PORT), NAME, IP), [])
        self.assertEqual(answers_for(whole, NAME), [IP])

    def test_junk_packets_never_raise(self):
        for junk in (b"", b"\xff" * 5, b"\x00\x00\x84\x00" + b"\xff" * 30, b"\x00\x00\x84\x00\x00\x00\x00\x01" + b"\x00" * 4 + b"\xc0"):
            self.assertEqual(answers_for(junk, NAME), [])

    def test_goodbye_has_ttl_zero(self):
        self.assertIn(struct.pack(">HHIH", 1, 0x8001, 0, 4), response(NAME, IP, ttl=0))

    def test_a_record_without_cache_flush(self):
        self.assertEqual(a_record(NAME, IP, 10, cache_flush=False)[-14:-4], struct.pack(">HHIH", 1, 1, 10, 4))


class FakeSocket:
    """Stands in for the UDP socket: hands out queued packets or errors, records what is sent."""

    def __init__(self, events=()):
        self.events = list(events)
        self.sent = []
        self.closed = False

    def recvfrom(self, size):
        if self.closed:
            raise OSError(9, "Bad file descriptor")
        if not self.events:
            time.sleep(0.01)
            raise socket.timeout()
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    def sendto(self, data, destination):
        if self.closed:
            raise OSError(9, "Bad file descriptor")
        self.sent.append((data, destination))

    def fileno(self):
        return -1 if self.closed else 7

    def close(self):
        self.closed = True


def free_udp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def wait_for(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


class AnnouncerTest(unittest.TestCase):
    SOURCE = ("192.168.1.50", MDNS_PORT)

    def loopback(self, **kwargs):
        return Announcer("porthole", "127.0.0.1", port=free_udp_port(), group="127.0.0.1", join=False, **kwargs)

    def test_taken_name_moves_to_dash_2(self):
        announcer = self.loopback()
        asked = []
        announcer._taken = lambda host: asked.append(host) or host == "porthole.local"
        announcer.start()
        self.addCleanup(announcer.stop)
        self.assertEqual(asked, ["porthole.local", "porthole-2.local"])
        self.assertEqual(announcer.hostname, "porthole-2.local")
        self.assertTrue(announcer.alive)

    def test_no_free_name_raises_and_closes_the_socket(self):
        announcer = self.loopback()
        opened = []
        real_open = announcer._open
        announcer._open = lambda: opened.append(real_open()) or opened[-1]
        announcer._taken = lambda host: True
        with self.assertRaises(OSError):
            announcer.start()
        self.assertEqual(opened[0].fileno(), -1)
        self.assertFalse(announcer.alive)
        announcer.stop()  # harmless after a failed start

    def test_start_closes_the_socket_when_anything_fails(self):
        announcer = self.loopback()
        sock = FakeSocket()
        announcer._open = lambda: sock

        def broken(host):
            raise RuntimeError("boom")

        announcer._taken = broken
        with self.assertRaises(RuntimeError):
            announcer.start()
        self.assertTrue(sock.closed)
        self.assertFalse(announcer.alive)

    def test_probe_survives_bad_packets_and_asks_with_plain_class_in(self):
        announcer = Announcer("porthole", IP, join=False)
        other = response("porthole.local", "192.168.1.99")
        announcer._sock = FakeSocket(
            [
                (other[:-2], self.SOURCE),  # truncated
                ConnectionResetError(10054, "reset"),  # Windows: ICMP port unreachable
                (b"\xff" * 5, self.SOURCE),
                (other, self.SOURCE),
            ]
        )
        self.assertTrue(announcer._taken("porthole.local"))
        probe, destination = announcer._sock.sent[0]
        self.assertEqual(destination, (MDNS_GROUP, MDNS_PORT))
        self.assertEqual(parse_questions(probe), [("porthole.local", TYPE_ANY, False)])
        self.assertTrue(probe.endswith(struct.pack(">HH", TYPE_ANY, 1)))

    def test_probe_ignores_answers_with_our_own_address(self):
        announcer = Announcer("porthole", IP, join=False)
        announcer._sock = FakeSocket([(response("porthole.local", IP), self.SOURCE)])
        self.assertFalse(announcer._taken("porthole.local"))
        self.assertEqual(len(announcer._sock.sent), 3)

    def test_serving_survives_socket_errors(self):
        announcer = Announcer("porthole", IP, join=False, probe=False)
        sock = FakeSocket([ConnectionResetError(10054, "reset"), OSError(101, "unreachable"), (query(NAME.replace("screen", "porthole")), self.SOURCE)])
        announcer._open = lambda: sock
        announcer.start()
        self.addCleanup(announcer.stop)
        self.assertTrue(wait_for(lambda: sock.sent))
        self.assertEqual(answers_for(sock.sent[0][0], "porthole.local"), [IP])
        self.assertTrue(announcer.alive)

    def test_serving_stops_when_the_socket_closes(self):
        announcer = Announcer("porthole", IP, join=False, probe=False)
        sock = FakeSocket()
        announcer._open = lambda: sock
        announcer.start()
        self.assertTrue(announcer.alive)
        sock.close()
        self.assertTrue(wait_for(lambda: not announcer.alive))

    def test_stop_ends_serving(self):
        announcer = self.loopback(probe=False)
        announcer.start()
        self.assertTrue(announcer.alive)
        announcer.stop()
        self.assertFalse(announcer.alive)


class AnnouncerSocketTest(unittest.TestCase):
    def test_legacy_unicast_round_trip_on_loopback(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        announcer = Announcer("screen", "127.0.0.1", port=port, group="127.0.0.1", join=False, probe=False)
        announcer.start()
        self.addCleanup(announcer.stop)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2)
        self.addCleanup(client.close)
        client.sendto(query("screen.local", qid=7), ("127.0.0.1", port))
        data, _ = client.recvfrom(2048)
        self.assertEqual(struct.unpack(">H", data[:2])[0], 7)
        self.assertEqual(answers_for(data, "screen.local"), ["127.0.0.1"])
        self.assertEqual(announcer.hostname, "screen.local")


if __name__ == "__main__":
    unittest.main()
