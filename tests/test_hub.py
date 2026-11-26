import time
import unittest

from porthole import hub as hub_module
from porthole.hub import Hub, HubError, device_label, rewrite_candidate

MDNS = "candidate:1 1 udp 2122260223 3f2a9c1e-77b1-4c55-9d0e-1a2b3c4d5e6f.local 54321 typ host generation 0"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 Version/18.1 Mobile/15E148 Safari/604.1"


def cand(text):
    return {"candidate": text, "sdpMid": "0", "sdpMLineIndex": 0, "usernameFragment": "abcd"}


class HubTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(room="room1")

    def drain(self, peer):
        out = []
        while True:
            msg = self.hub.next_message(peer.id, timeout=0.01)
            if msg is None:
                return out
            out.append(msg)

    def sharer(self):
        return self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC")

    def viewer(self, client_ip="192.168.0.50", local_ip="192.168.0.31", device="iPhone", client=""):
        return self.hub.connect("viewer", client_ip, local_ip, device, client)

    def test_random_room_when_none_given(self):
        a, b = Hub(), Hub()
        self.assertGreaterEqual(len(a.room), 8)
        self.assertNotEqual(a.room, b.room)

    def test_viewer_gets_hello_with_id_and_code(self):
        v = self.viewer()
        self.assertTrue(v.id.startswith("v-"))
        self.assertTrue(100 <= v.code <= 999)
        self.assertEqual(self.drain(v), [{"type": "hello", "id": v.id, "code": v.code}])

    def test_sharer_gets_unique_id(self):
        s = self.sharer()
        self.assertTrue(s.id.startswith("s-"))

    def test_viewer_join_notifies_sharer(self):
        s = self.sharer()
        v = self.viewer(device="iPhone")
        self.assertEqual(
            self.drain(s),
            [
                {"type": "hello", "id": s.id, "viewers": []},
                {
                    "type": "viewer-joined", "id": v.id, "device": "iPhone", "client": "", "code": v.code, "joined": v.joined,
                    "address": "192.168.0.50",
                },  # fmt: skip
            ],
        )

    def test_sharer_hello_lists_existing_viewers(self):
        v = self.viewer(device="iPhone")
        s = self.sharer()
        self.assertEqual(
            self.drain(s),
            [
                {
                    "type": "hello",
                    "id": s.id,
                    "viewers": [
                        {"id": v.id, "device": "iPhone", "client": "", "code": v.code, "joined": v.joined, "address": "192.168.0.50"}
                    ],
                }
            ],
        )
        self.assertEqual(self.hub.viewers(), [{"id": v.id, "device": "iPhone"}])

    def test_offer_relayed_to_viewer_with_from(self):
        s, v = self.sharer(), self.viewer()
        self.drain(v)
        self.hub.send(s.id, {"to": v.id, "type": "offer", "sdp": "x"})
        self.assertEqual(self.drain(v), [{"type": "offer", "sdp": "x", "from": "sharer"}])

    def test_answer_relayed_to_sharer(self):
        s, v = self.sharer(), self.viewer()
        self.drain(s)
        self.hub.send(v.id, {"to": "sharer", "type": "answer", "sdp": "y"})
        self.assertEqual(self.drain(s), [{"type": "answer", "sdp": "y", "from": v.id}])

    def test_viewer_cannot_send_offer(self):
        self.sharer()
        v = self.viewer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(v.id, {"to": "sharer", "type": "offer", "sdp": "x"})
        self.assertEqual(ctx.exception.status, 403)

    def test_viewer_cannot_message_other_viewer(self):
        self.sharer()
        v1, v2 = self.viewer(), self.viewer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(v1.id, {"to": v2.id, "type": "answer", "sdp": "x"})
        self.assertEqual(ctx.exception.status, 403)

    def test_unknown_sender_is_404(self):
        with self.assertRaises(HubError) as ctx:
            self.hub.send("v-nope", {"to": "sharer", "type": "ready"})
        self.assertEqual(ctx.exception.status, 404)

    def test_missing_sdp_is_400(self):
        s, v = self.sharer(), self.viewer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(s.id, {"to": v.id, "type": "offer"})
        self.assertEqual(ctx.exception.status, 400)

    def test_bad_candidate_is_400(self):
        s, v = self.sharer(), self.viewer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(s.id, {"to": v.id, "type": "candidate", "candidate": "nope"})
        self.assertEqual(ctx.exception.status, 400)

    def test_sharer_to_gone_viewer_is_404(self):
        s = self.sharer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(s.id, {"to": "v-gone", "type": "stopped"})
        self.assertEqual(ctx.exception.status, 404)

    def test_viewer_to_absent_sharer_is_409(self):
        v = self.viewer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(v.id, {"to": "sharer", "type": "ready"})
        self.assertEqual(ctx.exception.status, 409)

    def test_viewer_candidate_gets_real_ip_copy(self):
        s, v = self.sharer(), self.viewer(client_ip="192.168.0.50")
        self.drain(s)
        self.hub.send(v.id, {"to": "sharer", "type": "candidate", "candidate": cand(MDNS)})
        msgs = self.drain(s)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["candidate"]["candidate"], MDNS)
        self.assertIn("192.168.0.50 54321 typ host", msgs[1]["candidate"]["candidate"])
        self.assertEqual(msgs[1]["candidate"]["sdpMid"], "0")
        self.assertEqual(msgs[1]["from"], v.id)

    def test_sharer_candidate_uses_viewer_local_ip(self):
        s, v = self.sharer(), self.viewer(local_ip="192.168.0.31")
        self.drain(v)
        self.hub.send(s.id, {"to": v.id, "type": "candidate", "candidate": cand(MDNS)})
        msgs = self.drain(v)
        self.assertEqual(len(msgs), 2)
        self.assertIn("192.168.0.31 54321 typ host", msgs[1]["candidate"]["candidate"])

    def test_non_mdns_candidate_not_copied(self):
        s, v = self.sharer(), self.viewer()
        self.drain(s)
        self.hub.send(v.id, {"to": "sharer", "type": "candidate", "candidate": cand("candidate:2 1 udp 1 192.168.0.9 5000 typ host")})
        self.assertEqual(len(self.drain(s)), 1)

    def test_sharer_disconnect_tells_viewers(self):
        s, v = self.sharer(), self.viewer()
        self.drain(v)
        self.hub.disconnect(s.id)
        self.assertEqual(self.drain(v), [{"type": "sharer-left"}])

    def test_viewer_disconnect_tells_sharer(self):
        s, v = self.sharer(), self.viewer()
        self.drain(s)
        self.hub.disconnect(v.id)
        self.hub.disconnect(v.id)  # idempotent
        self.assertEqual(self.drain(s), [{"type": "viewer-left", "id": v.id}])
        self.assertEqual(self.hub.viewers(), [])

    def test_second_sharer_replaces_first(self):
        old = self.sharer()
        v = self.viewer()
        self.drain(old)
        new = self.sharer()
        self.assertEqual(self.drain(old), [{"type": "replaced"}])
        self.drain(new)
        self.hub.send(v.id, {"to": "sharer", "type": "answer", "sdp": "z"})
        self.assertEqual(self.drain(new), [{"type": "answer", "sdp": "z", "from": v.id}])
        # The replaced sharer's late disconnect must not remove the new one.
        self.hub.disconnect(old.id)
        self.hub.send(v.id, {"to": "sharer", "type": "ready"})
        self.assertEqual(self.drain(new), [{"type": "ready", "from": v.id}])

    def test_the_replaced_page_hears_when_porthole_s_own_window_took_over(self):
        # A page in Porthole's own window lets go of that window when a browser tab takes over, so closing it leaves
        # the share running; another Porthole window taking over shares its browser, so the tie must stay.
        old = self.sharer()
        self.drain(old)
        self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC", in_window=True)
        self.assertEqual(self.drain(old), [{"type": "replaced", "window": True}])

    def test_new_sharer_tells_viewers_old_one_left(self):
        self.sharer()
        v = self.viewer()
        self.drain(v)
        self.sharer()  # e.g. the sharer page was reloaded
        self.assertEqual(self.drain(v), [{"type": "sharer-left"}])

    def test_replaced_sharer_cannot_send(self):
        old = self.sharer()
        v = self.viewer()
        self.sharer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(old.id, {"to": v.id, "type": "stopped"})
        self.assertEqual(ctx.exception.status, 409)

    def test_client_token_kept_when_valid(self):
        s = self.sharer()
        self.drain(s)
        v = self.viewer(client="Abc_123-xyzQ")
        self.assertEqual(v.client, "Abc_123-xyzQ")
        self.assertEqual(self.drain(s)[0]["client"], "Abc_123-xyzQ")
        s2 = self.sharer()
        self.assertEqual(self.drain(s2)[0]["viewers"][0]["client"], "Abc_123-xyzQ")

    def test_invalid_client_token_dropped(self):
        # A final "\n" too: `$` matches before it, and the page would get a device key Porthole can't keep.
        bad_tokens = ("short", "has space in it", "x" * 65, "<script>alert</script>", "abcdefgh\n", "\nabcdefgh")
        for n, bad in enumerate(bad_tokens):  # each from its own device: one device may have only so many tabs
            self.assertEqual(self.viewer(client_ip=f"192.168.0.{60 + n}", client=bad).client, "", repr(bad))

    def test_offer_carries_counter_and_restart(self):
        s, v = self.sharer(), self.viewer()
        self.drain(v)
        self.hub.send(s.id, {"to": v.id, "type": "offer", "sdp": "x", "n": 3, "restart": True, "evil": 1})
        self.assertEqual(self.drain(v), [{"type": "offer", "sdp": "x", "n": 3, "restart": True, "from": "sharer"}])

    def test_offer_bad_counter_or_restart_is_400(self):
        s, v = self.sharer(), self.viewer()
        for extra in ({"n": "3"}, {"n": True}, {"restart": "yes"}):
            with self.assertRaises(HubError) as ctx:
                self.hub.send(s.id, {"to": v.id, "type": "offer", "sdp": "x", **extra})
            self.assertEqual(ctx.exception.status, 400, extra)

    def test_answer_carries_counter(self):
        s, v = self.sharer(), self.viewer()
        self.drain(s)
        self.hub.send(v.id, {"to": "sharer", "type": "answer", "sdp": "y", "n": 2})
        self.assertEqual(self.drain(s), [{"type": "answer", "sdp": "y", "n": 2, "from": v.id}])

    def test_waiting_reason_relayed_and_checked(self):
        s, v = self.sharer(), self.viewer()
        self.drain(v)
        self.hub.send(s.id, {"to": v.id, "type": "waiting", "reason": "approval"})
        self.assertEqual(self.drain(v), [{"type": "waiting", "reason": "approval", "from": "sharer"}])
        for reason in ("coffee", [], {}):
            with self.subTest(reason=reason), self.assertRaises(HubError) as ctx:
                self.hub.send(s.id, {"to": v.id, "type": "waiting", "reason": reason})
            self.assertEqual(ctx.exception.status, 400)

    def test_denied_and_removed_relayed(self):
        s, v = self.sharer(), self.viewer()
        self.drain(v)
        self.hub.send(s.id, {"to": v.id, "type": "denied"})
        self.hub.send(s.id, {"to": v.id, "type": "removed"})
        self.assertEqual([m["type"] for m in self.drain(v)], ["denied", "removed"])

    def test_pause_and_resume_relayed_to_viewers(self):
        # A viewer zoomed in on a corner of the Paused card sees a plain dark picture: the sharer page tells viewers,
        # and their page shows a small "Paused" note over the picture.
        s, v = self.sharer(), self.viewer()
        self.drain(v)
        self.hub.send(s.id, {"to": v.id, "type": "paused"})
        self.hub.send(s.id, {"to": v.id, "type": "resumed", "extra": "<b>dropped</b>"})
        self.assertEqual(self.drain(v), [{"type": "paused", "from": "sharer"}, {"type": "resumed", "from": "sharer"}])

    def test_only_the_sharer_page_pauses(self):
        s, v = self.sharer(), self.viewer()
        for kind in ("paused", "resumed"):
            with self.subTest(kind=kind), self.assertRaises(HubError) as ctx:
                self.hub.send(v.id, {"to": "sharer", "type": kind})
            self.assertEqual(ctx.exception.status, 403)
        self.drain(s)
        other = self.viewer(client_ip="192.168.0.51")
        self.drain(v)
        with self.assertRaises(HubError) as ctx:
            self.hub.send(other.id, {"to": v.id, "type": "paused"})
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.drain(v), [])

    def test_quality_mode_relayed_and_checked(self):
        s, v = self.sharer(), self.viewer()
        self.drain(s)
        self.hub.send(v.id, {"to": "sharer", "type": "quality", "mode": "saver"})
        self.assertEqual(self.drain(s), [{"type": "quality", "mode": "saver", "from": v.id}])
        for mode in ("ultra", [], {}, None):  # a list or an object must not crash the request either
            with self.subTest(mode=mode), self.assertRaises(HubError) as ctx:
                self.hub.send(v.id, {"to": "sharer", "type": "quality", "mode": mode})
            self.assertEqual(ctx.exception.status, 400)

    def test_type_must_be_a_string(self):
        self.sharer()
        v = self.viewer()
        with self.assertRaises(HubError) as ctx:
            self.hub.send(v.id, {"to": "sharer", "type": []})
        self.assertEqual(ctx.exception.status, 400)

    def test_open_tabs_limit_leaves_the_browser_a_connection(self):
        # Browsers open at most 6 connections to one address, and each viewer tab holds one for its event stream:
        # with 6 tabs nothing else gets through (no picture, no page load, no refusal). At 5, a 6th tab still
        # loads and is told why it can't join.
        self.assertEqual(hub_module.MAX_VIEWERS_PER_ADDRESS, 5)

    def test_viewer_cap_per_address(self):
        self.hub = Hub(room="room1", join_limit=100)  # only the open-tabs cap matters here
        for _ in range(hub_module.MAX_VIEWERS_PER_ADDRESS):
            self.viewer(client_ip="192.168.0.77")
        with self.assertRaises(HubError) as ctx:
            self.viewer(client_ip="192.168.0.77")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.refusal, "too-many-tabs")
        self.viewer(client_ip="192.168.0.78")  # another device is fine

    # --- repeat joins: at most JOIN_LIMIT new viewer connections per device address per window ---

    def clocked_hub(self, **kwargs):
        self.now = 1000.0
        self.hub = Hub(room="room1", clock=lambda: self.now, **kwargs)

    def test_join_limit_defaults(self):
        self.assertEqual((hub_module.JOIN_LIMIT, hub_module.JOIN_WINDOW), (6, 60))

    def test_too_many_new_connections_per_minute_are_refused(self):
        self.clocked_hub()
        for _ in range(6):
            self.hub.disconnect(self.viewer(client_ip="192.168.0.77").id)  # e.g. reloading again and again
            self.now += 5
        with self.assertRaises(HubError) as ctx:
            self.viewer(client_ip="192.168.0.77")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.refusal, "too-many-tries")
        self.viewer(client_ip="192.168.0.78")  # another device is fine
        self.now += 31  # the first join is now more than a minute old
        self.viewer(client_ip="192.168.0.77")

    def test_refused_tries_do_not_count(self):
        self.clocked_hub()
        for _ in range(6):
            self.hub.disconnect(self.viewer(client_ip="192.168.0.77").id)
        for _ in range(5):
            with self.assertRaises(HubError):
                self.viewer(client_ip="192.168.0.77")
        self.now += 60.5
        for _ in range(2):  # the window is empty again, whatever was refused
            self.viewer(client_ip="192.168.0.77")

    def test_open_tabs_reason_comes_first(self):
        self.clocked_hub(join_limit=8)
        for _ in range(hub_module.MAX_VIEWERS_PER_ADDRESS):
            self.viewer(client_ip="192.168.0.77")
        with self.assertRaises(HubError) as ctx:
            self.viewer(client_ip="192.168.0.77")
        self.assertEqual(ctx.exception.refusal, "too-many-tabs")

    def test_sharer_connections_are_not_limited(self):
        self.clocked_hub()
        for _ in range(10):
            self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC")
        for _ in range(hub_module.MAX_VIEWERS_PER_ADDRESS):
            self.viewer(client_ip="127.0.0.1")

    def test_other_errors_have_no_refusal_reason(self):
        with self.assertRaises(HubError) as ctx:
            self.hub.connect("boss", "1.2.3.4", "1.2.3.4", "Device")
        self.assertIsNone(ctx.exception.refusal)

    # --- refusals: the sharer page shows the newest one ---

    def refusing_hub(self):
        self.now = 1000.0
        self.wall = 1_763_733_900.0  # epoch seconds
        self.hub = Hub(room="room1", join_limit=100, clock=lambda: self.now, wall_clock=lambda: self.wall)
        for _ in range(hub_module.MAX_VIEWERS_PER_ADDRESS):
            self.viewer(client_ip="192.168.0.77")

    def refuse(self, device="Android phone"):
        with self.assertRaises(HubError):
            self.viewer(client_ip="192.168.0.77", device=device)

    def test_no_refusals_at_first(self):
        self.assertEqual(self.hub.refusals(), [])

    def test_a_refusal_is_kept_with_time_device_and_reason(self):
        self.refusing_hub()
        self.refuse("Android phone")
        self.assertEqual(
            self.hub.refusals(),
            [{"time": 1_763_733_900_000, "device": "Android phone", "reason": "too-many-tabs"}],
        )

    def test_too_many_tries_is_kept_too(self):
        self.clocked_hub()
        for _ in range(6):
            self.hub.disconnect(self.viewer(client_ip="192.168.0.77").id)
        with self.assertRaises(HubError):
            self.viewer(client_ip="192.168.0.77", device="Mac")
        self.assertEqual([(r["device"], r["reason"]) for r in self.hub.refusals()], [("Mac", "too-many-tries")])

    def test_only_the_last_five_are_kept_oldest_first(self):
        self.refusing_hub()
        for i in range(7):
            self.wall += 1
            self.refuse(f"Device {i}")
        self.assertEqual([r["device"] for r in self.hub.refusals()], [f"Device {i}" for i in range(2, 7)])

    def test_refusals_older_than_ten_minutes_are_dropped(self):
        self.assertEqual(hub_module.REFUSAL_SECONDS, 600)
        self.assertEqual(hub_module.REFUSALS_KEPT, 5)
        self.refusing_hub()
        self.refuse("iPhone")
        self.now += 300
        self.refuse("Mac")
        self.now += 301  # the first is now 601 s old
        self.assertEqual([r["device"] for r in self.hub.refusals()], ["Mac"])
        self.now += 300
        self.assertEqual(self.hub.refusals(), [])

    def test_other_errors_are_not_refusals(self):
        with self.assertRaises(HubError):
            self.hub.connect("boss", "1.2.3.4", "1.2.3.4", "Device")
        self.sharer()
        with self.assertRaises(HubError):
            self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC", takeover=False)
        self.assertEqual(self.hub.refusals(), [])

    def test_refusals_returns_copies(self):
        self.refusing_hub()
        self.refuse()
        self.hub.refusals()[0]["device"] = "changed"
        self.hub.refusals().clear()
        self.assertEqual(self.hub.refusals()[0]["device"], "Android phone")

    # --- check codes: tell waiting devices apart ---

    def test_codes_are_unique_among_current_viewers(self):
        self.hub = Hub(room="room1", join_limit=1000)
        viewers = [self.viewer(client_ip=f"10.0.{i // 200}.{i % 200}") for i in range(900)]
        codes = [v.code for v in viewers]
        self.assertEqual(sorted(codes), list(range(100, 1000)))  # every 3-digit code, none twice
        extra = self.viewer(client_ip="10.9.9.9")  # all taken: still gets a code, never hangs
        self.assertTrue(100 <= extra.code <= 999)

    def test_code_of_a_viewer_that_left_can_be_used_again(self):
        self.hub = Hub(room="room1", join_limit=1000)
        viewers = [self.viewer(client_ip=f"10.0.{i // 200}.{i % 200}") for i in range(900)]
        self.hub.disconnect(viewers[123].id)
        self.assertEqual(self.viewer(client_ip="10.9.9.9").code, viewers[123].code)

    def test_sharer_hello_lists_codes(self):
        v1, v2 = self.viewer(client_ip="10.0.0.1"), self.viewer(client_ip="10.0.0.2")
        s = self.sharer()
        listed = self.drain(s)[0]["viewers"]
        self.assertEqual([(x["id"], x["code"]) for x in listed], [(v1.id, v1.code), (v2.id, v2.code)])
        self.assertNotEqual(v1.code, v2.code)

    # --- join time: when the viewer connected, kept by the hub so a sharer reload doesn't reset it ---

    def test_join_time_is_wall_clock_milliseconds(self):
        self.now_wall = 1_763_733_720.25  # 21 Nov 2026, 14:02 UTC
        self.hub = Hub(room="room1", wall_clock=lambda: self.now_wall)
        v = self.viewer()
        self.assertEqual(v.joined, 1_763_733_720_250)
        self.assertIsInstance(v.joined, int)

    def test_join_time_is_sent_with_viewer_joined(self):
        self.now_wall = 1_763_733_720.0
        self.hub = Hub(room="room1", wall_clock=lambda: self.now_wall)
        s = self.sharer()
        v = self.viewer()
        joined = self.drain(s)[1]
        self.assertEqual((joined["type"], joined["id"], joined["joined"]), ("viewer-joined", v.id, 1_763_733_720_000))

    def test_sharer_reload_keeps_each_viewers_join_time(self):
        self.now_wall = 1_763_733_720.0  # 14:02
        self.hub = Hub(room="room1", wall_clock=lambda: self.now_wall)
        v1 = self.viewer(client_ip="10.0.0.1")
        self.now_wall += 180  # 14:05
        v2 = self.viewer(client_ip="10.0.0.2")
        self.sharer()
        self.now_wall += 900  # 14:20: the sharer page reloads
        s2 = self.sharer()
        listed = self.drain(s2)[0]["viewers"]
        self.assertEqual(
            [(x["id"], x["joined"]) for x in listed], [(v1.id, 1_763_733_720_000), (v2.id, 1_763_733_900_000)]
        )

    def test_sharers_have_no_join_time(self):
        self.assertEqual(self.sharer().joined, 0)

    def test_second_sharer_without_takeover_is_busy(self):
        self.sharer()
        with self.assertRaises(HubError) as ctx:
            self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC", takeover=False)
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(self.hub.sharer_active())

    def test_sharer_active(self):
        self.assertFalse(self.hub.sharer_active())
        s = self.sharer()
        self.assertTrue(self.hub.sharer_active())
        self.hub.disconnect(s.id)
        self.assertFalse(self.hub.sharer_active())

    # --- sharing state (the sharer page reports it; "/" redirects only while sharing) ---

    def test_sharing_starts_false_and_follows_the_page(self):
        self.assertFalse(self.hub.sharing)
        self.sharer()
        self.hub.sharing = True
        self.assertTrue(self.hub.sharing)
        self.hub.sharing = False
        self.assertFalse(self.hub.sharing)

    def test_sharing_needs_a_sharer_page(self):
        self.hub.sharing = True
        self.assertFalse(self.hub.sharing)

    def test_sharing_resets_when_the_sharer_leaves(self):
        s = self.sharer()
        self.hub.sharing = True
        self.hub.disconnect(s.id)
        self.assertFalse(self.hub.sharing)

    def test_sharing_resets_when_the_sharer_is_replaced(self):
        self.sharer()
        self.hub.sharing = True
        self.sharer()
        self.assertFalse(self.hub.sharing)

    def test_sharing_kept_when_a_viewer_or_an_old_sharer_leaves(self):
        old = self.sharer()
        self.sharer()
        self.hub.sharing = True
        v = self.viewer()
        self.hub.disconnect(v.id)
        self.hub.disconnect(old.id)
        self.assertTrue(self.hub.sharing)

    def test_sharing_report_from_a_replaced_tab_is_ignored(self):
        old = self.sharer()
        new = self.sharer()
        self.hub.set_sharing(True, new.id)
        self.hub.set_sharing(False, old.id)  # the replaced tab saying it stopped
        self.assertTrue(self.hub.sharing)
        self.hub.set_sharing(False, new.id)
        self.assertFalse(self.hub.sharing)

    # --- this run's approvals and switches, kept for a page that takes over ---

    CHOICES = {"settings": {"ask": True, "easy": False, "smooth": True}, "approved": ["client-aaaa"], "denied": ["v-0123abcd"]}

    def test_no_choices_until_the_sharer_page_reports_them(self):
        self.assertIsNone(self.hub.choices())
        s = self.sharer()
        self.assertIsNone(self.hub.choices())
        self.assertTrue(self.hub.keep_choices(self.CHOICES, s.id))
        self.assertEqual(self.hub.choices(), self.CHOICES)

    def test_only_the_current_sharer_page_s_choices_count(self):
        # A page that was replaced, or one that isn't the sharer (a waiting page has no id), can't change them.
        old = self.sharer()
        self.assertTrue(self.hub.keep_choices(self.CHOICES, old.id))
        new = self.sharer()
        other = {"settings": {"ask": False, "easy": False, "smooth": False}, "approved": ["client-bbbb"], "denied": []}
        self.assertFalse(self.hub.keep_choices(other, old.id))
        self.assertFalse(self.hub.keep_choices(other, "s-nothere"))
        self.assertFalse(self.hub.keep_choices(other, self.viewer().id))
        self.assertEqual(self.hub.choices(), self.CHOICES)
        self.assertTrue(self.hub.keep_choices(other, new.id))
        self.assertEqual(self.hub.choices(), other)

    def test_choices_outlast_the_sharer_page(self):
        # Open in my browser: the page in Porthole's own window lets go, and the browser tab reads them after.
        s = self.sharer()
        self.hub.keep_choices(self.CHOICES, s.id)
        self.hub.disconnect(s.id)
        self.assertEqual(self.hub.choices(), self.CHOICES)
        self.assertFalse(self.hub.keep_choices(self.CHOICES, s.id))  # gone: no longer the sharer

    def test_choices_are_copies(self):
        s = self.sharer()
        given = {"settings": {"ask": True, "easy": True, "smooth": False}, "approved": ["client-aaaa"], "denied": []}
        self.hub.keep_choices(given, s.id)
        given["approved"].append("client-bbbb")
        given["settings"]["ask"] = False
        self.hub.choices()["denied"].append("client-cccc")
        self.hub.choices()["settings"]["easy"] = False
        self.assertEqual(self.hub.choices(), {"settings": {"ask": True, "easy": True, "smooth": False}, "approved": ["client-aaaa"], "denied": []})

    def test_a_new_run_starts_with_no_choices(self):
        s = self.sharer()
        self.hub.keep_choices(self.CHOICES, s.id)
        self.assertIsNone(Hub().choices())

    def test_end_tells_every_viewer(self):
        self.sharer()
        v1, v2 = self.viewer(), self.viewer()
        self.drain(v1)
        self.drain(v2)
        self.hub.end()
        self.assertEqual(self.drain(v1), [{"type": "ended"}])
        self.assertEqual(self.drain(v2), [{"type": "ended"}])

    def test_end_tells_the_sharer_page_too(self):
        s = self.sharer()
        self.drain(s)
        self.hub.end()
        self.assertEqual(self.drain(s), [{"type": "ended"}])

    def test_a_page_that_connects_while_porthole_stops_hears_it_ended(self):
        # Porthole serves a moment longer after Quit (up to 3 s while a recording finishes): a page connecting then
        # must not be left waiting, then retrying for ever once Porthole has gone.
        self.hub.end()
        v = self.viewer()
        self.assertEqual([m["type"] for m in self.drain(v)], ["hello", "ended"])
        s = self.sharer()
        self.assertEqual([m["type"] for m in self.drain(s)], ["hello", "ended"])

    def test_role_of(self):
        s, v = self.sharer(), self.viewer()
        self.assertEqual(self.hub.role_of(s.id), "sharer")
        self.assertEqual(self.hub.role_of(v.id), "viewer")
        self.assertIsNone(self.hub.role_of("nope"))

    def test_check_room(self):
        self.assertTrue(self.hub.check_room("room1"))
        self.assertFalse(self.hub.check_room("room2"))
        self.assertFalse(self.hub.check_room(""))

    def test_next_message_timeout_returns_none(self):
        v = self.viewer()
        self.drain(v)
        self.assertIsNone(self.hub.next_message(v.id, timeout=0.01))

    def test_next_message_for_unknown_peer_returns_none(self):
        self.assertIsNone(self.hub.next_message("v-unknown", timeout=0.01))


TOKEN = "Abc_123456789"  # the viewer's per-browser token (see view.js deviceToken)
OTHER_TOKEN = "Zyx_987654321"
PHONE = "192.168.0.50"


class ReconnectTest(unittest.TestCase):
    """A viewer that reconnects (a locked phone, a Wi-Fi drop, a reload) with the id of its connection before takes
    over its own row: the sharer page keeps one row, with its name, code and approval, and the old connection ends.
    Only the device that held the row can do that: the same token, the same address, and a row still there that was
    never turned away. Anything else joins as a new device, with nothing said about why."""

    def setUp(self):
        self.hub = Hub(room="room1", join_limit=100)
        self.sharer = self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC")

    def viewer(self, client=TOKEN, ip=PHONE, previous=""):
        return self.hub.connect("viewer", ip, "192.168.0.31", "iPhone", client, previous=previous)

    def drain(self, peer_or_id):
        peer_id = getattr(peer_or_id, "id", peer_or_id)
        out = []
        while (msg := self.hub.next_message(peer_id, timeout=0.01)) is not None:
            out.append(msg)
        return out

    def joined_fresh(self, old, new):
        """`new` joined as a device of its own: its own code, the old row left alone."""
        self.assertNotEqual(new.code, old.code)
        joined = self.drain(self.sharer)
        self.assertEqual([m["type"] for m in joined], ["viewer-joined"])
        self.assertNotIn("replaces", joined[0])
        self.assertEqual(self.drain(new), [{"type": "hello", "id": new.id, "code": new.code}])
        self.assertEqual(self.drain(old), [])  # the old connection hears nothing
        self.assertEqual({v["id"] for v in self.hub.viewers()}, {old.id, new.id})

    def test_the_same_device_takes_over_its_row(self):
        old = self.viewer()
        self.drain(self.sharer)
        self.drain(old)
        new = self.viewer(previous=old.id)
        self.assertNotEqual(new.id, old.id)  # a fresh id: the old one is dead for good
        self.assertEqual((new.code, new.joined), (old.code, old.joined))
        self.assertEqual(self.drain(new), [{"type": "hello", "id": new.id, "code": old.code}])
        self.assertEqual(
            self.drain(self.sharer),
            [
                {
                    "type": "viewer-joined",
                    "id": new.id,
                    "device": "iPhone",
                    "client": TOKEN,
                    "code": old.code,
                    "joined": old.joined,
                    "address": PHONE,  # the sharer page ties an approval to the token and this address
                    "replaces": old.id,
                }
            ],
        )
        self.assertEqual(self.drain(old), [{"type": "replaced"}])  # its stream writes this and ends
        self.assertEqual(self.hub.viewers(), [{"id": new.id, "device": "iPhone"}])
        self.hub.disconnect(old.id)  # the old stream's handler finishing: the sharer page already moved the row
        self.assertEqual(self.drain(self.sharer), [])
        self.assertEqual(self.hub.viewers(), [{"id": new.id, "device": "iPhone"}])
        self.assertNotIn(old.id, self.hub._leaving)  # forgotten, not kept (with its queue) for the rest of the run

    def test_the_old_connection_can_no_longer_send_or_be_sent_to(self):
        old = self.viewer()
        self.viewer(previous=old.id)
        with self.assertRaises(HubError) as ctx:
            self.hub.send(self.sharer.id, {"to": old.id, "type": "offer", "sdp": "x"})
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(HubError) as ctx:
            self.hub.send(old.id, {"to": "sharer", "type": "ready"})
        self.assertEqual((ctx.exception.status, ctx.exception.reason), (404, "unknown peer"))
        self.assertIsNone(self.hub.role_of(old.id))

    def test_another_browser_joins_as_a_new_device(self):
        # Another device that learnt the id (it travels in plain HTTP on the Wi-Fi), from the same address even.
        old = self.viewer()
        self.drain(self.sharer)
        self.drain(old)
        self.joined_fresh(old, self.viewer(client=OTHER_TOKEN, previous=old.id))

    def test_no_token_joins_as_a_new_device(self):
        old = self.viewer()
        self.drain(self.sharer)
        self.drain(old)
        self.joined_fresh(old, self.viewer(client="", previous=old.id))

    def test_a_token_that_isnt_one_joins_as_a_new_device(self):
        old = self.viewer()
        self.drain(self.sharer)
        self.drain(old)
        self.joined_fresh(old, self.viewer(client=TOKEN + "\n", previous=old.id))

    def test_another_address_joins_as_a_new_device(self):
        # A device that copied the token and the id but isn't the phone.
        old = self.viewer()
        self.drain(self.sharer)
        self.drain(old)
        self.joined_fresh(old, self.viewer(ip="192.168.0.66", previous=old.id))

    def test_an_unknown_or_odd_id_joins_as_a_new_device(self):
        old = self.viewer()
        self.drain(self.sharer)
        self.drain(old)
        for previous in ("v-00000000", self.sharer.id, "", None, 7, ["x"]):
            with self.subTest(previous=previous):
                new = self.viewer(previous=previous)
                self.assertNotEqual(new.code, old.code)
                joined = self.drain(self.sharer)
                self.assertNotIn("replaces", joined[0])
                self.assertEqual(self.drain(old), [])
                self.hub.disconnect(new.id)
                self.drain(self.sharer)
        self.assertEqual(self.hub.role_of(self.sharer.id), "sharer")  # the sharer was never touched

    def test_a_row_that_was_denied_or_removed_is_never_revived(self):
        for kind in ("denied", "removed"):
            with self.subTest(kind=kind):
                self.setUp()  # a fresh hub: the last round's new row is kept a moment after it closes
                old = self.viewer()
                self.drain(self.sharer)
                self.hub.send(self.sharer.id, {"to": old.id, "type": kind})
                self.drain(old)
                new = self.viewer(previous=old.id)  # before its stream has closed
                self.joined_fresh(old, new)  # the sharer page then turns it away again, as for any new device
                self.hub.disconnect(old.id)
                self.hub.disconnect(new.id)
                self.drain(self.sharer)

    def test_a_closed_row_is_kept_a_moment_for_a_reload(self):
        # A reloaded page is back after its old stream closed: its row waits for it, so the phone
        # keeps its name, code and place instead of coming back as a new row.
        self.assertEqual(hub_module.KEEP_SECONDS, 2.0)
        old = self.viewer()
        self.drain(self.sharer)
        self.hub.disconnect(old.id)
        self.assertEqual(self.drain(self.sharer), [])  # not dropped yet
        new = self.viewer(previous=old.id)
        joined = self.drain(self.sharer)
        self.assertEqual([(m["type"], m.get("replaces")) for m in joined], [("viewer-joined", old.id)])
        self.assertEqual(new.code, old.code)
        self.assertEqual(self.hub.viewers(), [{"id": new.id, "device": "iPhone"}])

    def test_a_closed_row_is_dropped_after_the_wait(self):
        self.hub = Hub(room="room1", join_limit=100, keep_seconds=0.1)
        self.sharer = self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC")
        old = self.viewer()
        self.drain(self.sharer)
        self.hub.disconnect(old.id)
        self.assertEqual(self.drain(self.sharer), [])
        time.sleep(0.3)
        self.assertEqual(self.drain(self.sharer), [{"type": "viewer-left", "id": old.id}])
        self.assertEqual(self.hub.viewers(), [])

    def test_only_a_row_with_a_token_never_turned_away_is_kept(self):
        plain = self.viewer(client="")
        self.drain(self.sharer)
        self.hub.disconnect(plain.id)
        self.assertEqual(self.drain(self.sharer), [{"type": "viewer-left", "id": plain.id}])
        denied = self.viewer()
        self.drain(self.sharer)
        self.hub.send(self.sharer.id, {"to": denied.id, "type": "denied"})
        self.hub.disconnect(denied.id)
        self.assertEqual(self.drain(self.sharer), [{"type": "viewer-left", "id": denied.id}])

    def test_a_closed_row_isnt_an_open_tab(self):
        tabs = [self.viewer() for _ in range(hub_module.MAX_VIEWERS_PER_ADDRESS)]
        self.hub.disconnect(tabs[0].id)  # kept a moment, but its tab is closed
        self.viewer()  # a fifth open tab is still allowed

    def test_a_row_that_already_left_is_not_revived(self):
        self.hub = Hub(room="room1", join_limit=100, keep_seconds=0.05)
        self.sharer = self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC")
        old = self.viewer()
        self.hub.disconnect(old.id)
        time.sleep(0.2)  # the wait for a reload is over
        self.drain(self.sharer)
        new = self.viewer(previous=old.id)
        joined = self.drain(self.sharer)
        self.assertEqual([m["type"] for m in joined], ["viewer-joined"])
        self.assertNotIn("replaces", joined[0])
        self.assertEqual(self.hub.viewers(), [{"id": new.id, "device": "iPhone"}])

    def test_a_row_is_taken_over_once(self):
        # Two tabs with the same remembered id (a duplicated tab copies sessionStorage): the second gets a new row
        # instead of taking the row back, so the two never push each other out in turn.
        old = self.viewer()
        self.drain(old)
        first = self.viewer(previous=old.id)
        self.drain(self.sharer)
        self.drain(first)
        second = self.viewer(previous=old.id)
        self.assertNotEqual(second.code, first.code)
        joined = self.drain(self.sharer)
        self.assertNotIn("replaces", joined[0])
        self.assertEqual(self.drain(old), [{"type": "replaced"}])  # once, from the first takeover
        self.assertEqual({v["id"] for v in self.hub.viewers()}, {first.id, second.id})

    def test_the_new_row_can_be_taken_over_in_turn(self):
        old = self.viewer()
        first = self.viewer(previous=old.id)
        second = self.viewer(previous=first.id)
        self.assertEqual(second.code, old.code)
        self.assertEqual(self.drain(first)[-1], {"type": "replaced"})
        self.assertEqual(self.hub.viewers(), [{"id": second.id, "device": "iPhone"}])

    def test_a_takeover_isnt_another_tab(self):
        # A device with the most tabs open reconnects one of them: it has as many open as before, so it is let in.
        tabs = [self.viewer() for _ in range(hub_module.MAX_VIEWERS_PER_ADDRESS)]
        with self.assertRaises(HubError) as ctx:
            self.viewer()
        self.assertEqual(ctx.exception.refusal, "too-many-tabs")
        new = self.viewer(previous=tabs[0].id)
        self.assertEqual(new.code, tabs[0].code)
        self.assertEqual(len(self.hub.viewers()), hub_module.MAX_VIEWERS_PER_ADDRESS)

    def test_a_takeover_still_counts_as_a_try(self):
        self.hub = Hub(room="room1", join_limit=2)
        old = self.viewer()
        self.viewer(previous=old.id)
        with self.assertRaises(HubError) as ctx:
            self.viewer(previous=old.id)
        self.assertEqual(ctx.exception.refusal, "too-many-tries")

    def test_a_refused_takeover_leaves_the_row_alone(self):
        self.hub = Hub(room="room1", join_limit=1)
        sharer = self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC")
        old = self.viewer()
        self.drain(sharer)
        self.drain(old)
        with self.assertRaises(HubError):
            self.viewer(previous=old.id)
        self.assertEqual(self.drain(old), [])
        self.assertEqual(self.hub.viewers(), [{"id": old.id, "device": "iPhone"}])

    def test_a_takeover_with_no_sharer_page(self):
        # The sharer page is reloading: the new connection still takes the row, and the page lists only it.
        self.hub = Hub(room="room1")
        old = self.viewer()
        new = self.viewer(previous=old.id)
        self.assertEqual(self.drain(old), [{"type": "hello", "id": old.id, "code": old.code}, {"type": "replaced"}])
        sharer = self.hub.connect("sharer", "127.0.0.1", "127.0.0.1", "Linux PC")
        self.assertEqual([v["id"] for v in self.drain(sharer)[0]["viewers"]], [new.id])


class HelpersTest(unittest.TestCase):
    def test_rewrite_candidate(self):
        self.assertEqual(
            rewrite_candidate(MDNS, "10.0.0.2"),
            "candidate:1 1 udp 2122260223 10.0.0.2 54321 typ host generation 0",
        )
        self.assertIsNone(rewrite_candidate("candidate:2 1 udp 1 192.168.0.9 5000 typ host", "10.0.0.2"))
        self.assertIsNone(rewrite_candidate("candidate:3 1 udp 1 abc.local 5000 typ srflx raddr 0.0.0.0 rport 0", "10.0.0.2"))
        self.assertIsNone(rewrite_candidate("", "10.0.0.2"))

    def test_device_label(self):
        self.assertEqual(device_label(IPHONE), "iPhone")
        self.assertEqual(device_label("Mozilla/5.0 (Linux; Android 15; Pixel 9) Chrome/131.0 Mobile Safari/537.36"), "Android phone")
        self.assertEqual(device_label("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0"), "Windows PC")
        self.assertEqual(device_label("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15"), "Mac")
        self.assertEqual(device_label("Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0"), "Linux PC")
        self.assertEqual(device_label("Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X)"), "iPad")
        # An iPad asks for desktop sites, so its browser says Mac: only its touch screen tells it apart.
        ipad = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.1 Safari/605.1.15"
        self.assertEqual(device_label(ipad, touch=True), "iPad")
        self.assertEqual(device_label(ipad), "Mac")
        # Tablets and Chromebooks are named for what they are, so the sharer isn't looking for a phone or a PC.
        tablet = "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        self.assertEqual(device_label(tablet), "Android tablet")
        self.assertEqual(device_label("Mozilla/5.0 (Android 14; Tablet; rv:132.0) Gecko/132.0 Firefox/132.0"), "Android tablet")
        self.assertEqual(device_label("Mozilla/5.0 (Android 14; Mobile; rv:132.0) Gecko/132.0 Firefox/132.0"), "Android phone")
        chromebook = "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        self.assertEqual(device_label(chromebook), "Chromebook")
        self.assertEqual(device_label("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0", touch=True), "Windows PC")
        self.assertEqual(device_label(""), "Device")


if __name__ == "__main__":
    unittest.main()
