import contextlib
import errno
import http.client
import io
import json
import os
import re
import socket
import struct
import types
import threading
import time
import unittest
import webbrowser
from pathlib import Path

from porthole import app as share
import tempfile

from porthole.hub import MAX_VIEWERS_PER_ADDRESS as MAX_TABS, Hub
from porthole.recorder import Recorder
from unittest import mock

from porthole import webserver
from porthole.discovery import Discovery
from porthole.webserver import LanServer, is_loopback, make_redirect_server, make_server
from tests.test_discovery import FakeAnnouncer

WEB = Path(__file__).resolve().parent.parent / "web"
VIEWER_URL = "http://192.168.0.31:8765/v/room1"


class ServerTest(unittest.TestCase):
    def start(self, is_local=None, viewer_urls=(VIEWER_URL,), recorder=None, discovery=None, hub=None, **extra):
        self.hub = hub or Hub(room="room1")
        kwargs = {} if is_local is None else {"is_local": is_local}
        urls = viewer_urls if callable(viewer_urls) else list(viewer_urls)
        server = make_server(self.hub, 0, WEB, urls, recorder=recorder, discovery=discovery, **kwargs, **extra)
        self.thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def origin(self):
        return {"Origin": f"http://127.0.0.1:{self.port}"}

    def api(self, path, body=b"", headers=None):
        return self.request("POST", path, body=body, headers={**self.origin(), **(headers or {})})

    def request(self, method, path, body=None, host=None, headers=None, source=None):
        """`source`: the address the request comes from (another loopback address stands in for another device)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5, source_address=(source, 0) if source else None)
        all_headers = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        conn.request(method, path, body=body, headers=all_headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data.decode("utf-8", "replace")

    def open_events(self, role, room="room1", client="", extra="", headers=None, host=None, source=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5, source_address=(source, 0) if source else None)
        extra = (f"&client={client}" if client else "") + extra
        all_headers = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        conn.request("GET", f"/events?room={room}&role={role}{extra}", headers=all_headers)
        resp = conn.getresponse()
        self.addCleanup(conn.close)
        return resp

    @staticmethod
    def read_event(resp):
        while True:
            line = resp.fp.readline().decode()
            if line.startswith("data: "):
                resp.fp.readline()  # blank line ending the event
                return json.loads(line[6:])

    def signal(self, peer_id, body, room="room1"):
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.request("POST", f"/signal?room={room}&id={peer_id}", body=data, headers={"Content-Type": "application/json"})

    # --- pages ---

    def test_root_redirects_local_client_to_share(self):
        self.start()
        resp, _ = self.request("GET", "/")
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.getheader("Location"), "/share")

    def test_root_for_lan_client_is_404(self):
        self.start(is_local=lambda ip: False)
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 404)
        self.assertIn("Ask the person sharing for the link", body)

    def test_share_page_served_to_loopback(self):
        self.start()
        resp, body = self.request("GET", "/share")
        self.assertEqual(resp.status, 200)
        self.assertIn("Start sharing", body)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))

    def test_share_page_refused_to_lan_client(self):
        self.start(is_local=lambda ip: False)
        resp, body = self.request("GET", "/share")
        self.assertEqual(resp.status, 403)
        self.assertIn(f"http://127.0.0.1:{self.port}/share", body)
        self.assertIn("on the PC that runs Porthole", body)

    def test_viewer_page_right_room(self):
        self.start()
        resp, body = self.request("GET", "/v/room1")
        self.assertEqual(resp.status, 200)
        self.assertIn("<video", body)

    def test_viewer_page_wrong_room_says_the_link_doesnt_work(self):
        # Porthole can't tell a link from an earlier run from a mistyped one: the words fit both.
        self.start()
        resp, body = self.request("GET", "/v/oldroom")
        self.assertEqual(resp.status, 404)
        self.assertIn(
            "<h1>This link doesn't work</h1><p>It's from an earlier share or has a typo. Check it against the link on "
            "the sharing PC, or scan the QR code.</p>",
            body.replace("&#x27;", "'"),
        )
        self.assertNotIn("restarted", body)

    def test_message_pages_say_where_you_are(self):
        self.start()
        resp, body = self.request("GET", "/v/oldroom")
        self.assertIn("<title>This link doesn&#x27;t work · Porthole</title>", body)
        self.assertIn('<html lang="en">', body)
        self.assertRegex(body, r"<main class=\"message\"><h1>This link doesn&#x27;t work</h1><p>")
        self.assertNotIn("http-equiv", body)  # only the waiting page checks again
        resp, body = self.request("GET", "/static/nope.js")
        self.assertIn("<title>Not found · Porthole</title>", body)

    def test_static_whitelist(self):
        self.start()
        resp, _ = self.request("GET", "/static/signal.js")
        self.assertEqual(resp.status, 200)
        self.assertIn("javascript", resp.getheader("Content-Type"))
        resp, _ = self.request("GET", "/static/style.css")
        self.assertIn("text/css", resp.getheader("Content-Type"))
        resp, _ = self.request("GET", "/static/settings.js")
        self.assertEqual(resp.status, 200)
        self.assertIn("javascript", resp.getheader("Content-Type"))
        for path in ("/static/../share.py", "/static/hub.py", "/static/share.html", "/nope"):
            resp, _ = self.request("GET", path)
            self.assertEqual(resp.status, 404, path)

    def test_new_static_files_have_their_types(self):
        self.start()
        expected = {
            "/static/theme.js": "text/javascript; charset=utf-8",
            "/static/view-helpers.js": "text/javascript; charset=utf-8",
            "/static/retry-page.js": "text/javascript; charset=utf-8",
            "/static/manifest.json": "application/manifest+json",
            "/static/icon-180.png": "image/png",
            "/static/icon-192.png": "image/png",
            "/static/icon-512.png": "image/png",
        }
        for path, content_type in expected.items():
            resp, _ = self.request("GET", path)
            self.assertEqual(resp.status, 200, path)
            self.assertEqual(resp.getheader("Content-Type"), content_type, path)

    def test_icons_are_png_files_of_the_right_size(self):
        self.start()
        for size in (180, 192, 512):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", f"/static/icon-{size}.png", headers={"Host": f"127.0.0.1:{self.port}"})
            data = conn.getresponse().read()
            conn.close()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", size)
            width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            self.assertEqual((width, height), (size, size))

    def test_manifest_for_the_home_screen(self):
        self.start()
        resp, body = self.request("GET", "/static/manifest.json")
        manifest = json.loads(body)
        self.assertEqual(manifest["name"], "Porthole")
        self.assertEqual(manifest["short_name"], "Porthole")
        # No start_url: the shortcut opens the page it was added from (/v/<room>). "/" works for
        # other devices only while the easy address is on, so a shortcut to it was a dead end.
        self.assertNotIn("start_url", manifest)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["background_color"].lower(), "#000000")
        self.assertEqual(manifest["theme_color"].lower(), "#000000")
        icons = {icon["sizes"]: icon for icon in manifest["icons"]}
        self.assertEqual(set(icons), {"192x192", "512x512"})
        for size, icon in icons.items():
            self.assertEqual(icon["type"], "image/png")
            # A versioned address (?v=ring), so phones fetch the ring, not an icon they kept (see test_pages).
            self.assertEqual(icon["src"], f"/static/icon-{size.split('x')[0]}.png?v=ring")
            self.assertEqual(self.request("GET", icon["src"])[0].status, 200)

    def test_home_screen_files_reach_the_easy_address(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        for path in ("/static/manifest.json", "/static/icon-180.png", "/static/icon-192.png"):
            self.assertEqual(self.request("GET", path, host=f"porthole.local:{self.port}")[0].status, 200, path)

    def test_api_info_loopback_only(self):
        self.start()
        resp, body = self.request("GET", "/api/info")
        self.assertEqual(resp.status, 200)
        info = json.loads(body)
        self.assertEqual(info["room"], "room1")
        self.assertEqual(info["viewer_urls"], [VIEWER_URL])
        self.assertTrue(info["qr_svg"].startswith("<svg"))

    def test_api_info_refused_to_lan_client(self):
        self.start(is_local=lambda ip: False)
        resp, _ = self.request("GET", "/api/info")
        self.assertEqual(resp.status, 403)

    def test_api_info_without_addresses(self):
        self.start(viewer_urls=())
        info = json.loads(self.request("GET", "/api/info")[1])
        self.assertEqual(info["viewer_urls"], [])
        self.assertEqual(info["qr_svg"], "")

    def test_rejects_unknown_host_header(self):
        self.start()
        resp, _ = self.request("GET", "/share", host=f"evil.example:{self.port}")
        self.assertEqual(resp.status, 400)

    def assert_use_the_link(self, resp, body, label=""):
        self.assertEqual(resp.status, 400, label)
        self.assertIn("<title>Use the link · Porthole</title>", body, label)
        self.assertIn("<h1>Use the link</h1><p>Open the link or scan the QR code shown on the sharing PC.</p>", body, label)
        self.assertNotIn("IP address", body, label)
        self.assertNotIn("room1", body, label)

    def test_another_name_for_the_pc_says_to_use_the_link(self):
        # Another name for the PC (its own mypc.local, a router's name) is still refused, as any name that isn't the easy
        # address is (a page by a name a stranger controls could reach Porthole: DNS rebinding), but in words a phone
        # user can act on, not "Bad request · Open this page by its IP address".
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: False, discovery=discovery)
        for host in (f"mypc.local:{self.port}", "mypc.lan", f"evil.example:{self.port}", f"porthole.local.evil:{self.port}"):
            for path in ("/", "/v/room1", "/static/view.js", "/share", "/events?room=room1&role=viewer"):
                self.assert_use_the_link(*self.request("GET", path, host=host), f"{host} {path}")
        # The easy address still reaches only the viewer side; the sharer page stays out of its reach, in the same words.
        self.assert_use_the_link(*self.request("GET", "/share", host=f"porthole.local:{self.port}"))
        resp, body = self.request("POST", "/signal?room=room1&id=x", body="{}", host=f"mypc.local:{self.port}")
        self.assertEqual((resp.status, json.loads(body)), (400, {"error": "bad host"}))  # requests from pages: unchanged

    def test_the_use_the_link_page_gets_its_style_sheet(self):
        # The page is shown only to a Host the check refuses, and the browser asks for its style sheet by that same
        # Host: refused as well, the answer was the 400 page itself and the message showed unstyled. The style sheet is
        # a public file, so it is served whatever the Host; everything else stays refused.
        page = webserver._use_the_link().decode()
        self.assertEqual(re.findall(r'(?:src|href)="([^"]*)"', page), ["/static/style.css"])
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: False, discovery=discovery)
        css = (WEB / "style.css").read_text()
        for host in (f"mypc.local:{self.port}", "mypc.lan", f"evil.example:{self.port}"):
            resp, body = self.request("GET", "/static/style.css", host=host)
            self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "text/css; charset=utf-8"), host)
            self.assertEqual(body, css, host)
            for path in ("/static/view.js", "/static/style.css/", "/static/style.css.map", "/static/../share.html"):
                self.assertEqual(self.request("GET", path, host=host)[0].status, 400, (host, path))
        resp, body = self.request("POST", "/static/style.css", body="{}", host=f"mypc.local:{self.port}")
        self.assertEqual(resp.status, 400)

    def test_accepts_ip_and_localhost_hosts(self):
        self.start()
        for host in (f"localhost:{self.port}", f"192.168.0.31:{self.port}", f"[::1]:{self.port}", "10.1.2.3"):
            resp, _ = self.request("GET", "/v/room1", host=host)
            self.assertEqual(resp.status, 200, host)

    def test_frame_ancestors_and_xfo(self):
        self.start()
        resp, _ = self.request("GET", "/share")
        self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")

    def test_share_refuses_cross_site_fetch(self):
        self.start()
        for path in ("/share", "/api/info"):
            resp, _ = self.request("GET", path, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(resp.status, 403, path)
        resp, _ = self.request("GET", "/share", headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(resp.status, 200)

    def test_ping(self):
        self.start()
        resp, body = self.request("GET", "/api/ping")
        root = str(Path(share.__file__).resolve().parent.parent)  # the folder Porthole runs from
        self.assertEqual(json.loads(body), {"app": "porthole", "folder": root})

    def test_ping_refused_to_lan_client(self):
        self.start(is_local=lambda ip: False)
        self.assertEqual(self.request("GET", "/api/ping")[0].status, 403)

    # --- live addresses: /api/info follows the PC's current addresses ---

    def test_info_follows_changing_addresses(self):
        current = {"urls": [VIEWER_URL]}
        self.start(viewer_urls=lambda: current["urls"])
        first = json.loads(self.request("GET", "/api/info")[1])
        self.assertEqual(first["viewer_urls"], [VIEWER_URL])
        moved = "http://10.0.0.7:8765/v/room1"
        current["urls"] = [moved, VIEWER_URL]
        second = json.loads(self.request("GET", "/api/info")[1])
        self.assertEqual(second["viewer_urls"], [moved, VIEWER_URL])
        self.assertTrue(second["qr_svg"].startswith("<svg"))
        self.assertNotEqual(second["qr_svg"], first["qr_svg"])  # the code now holds the new link
        current["urls"] = []
        third = json.loads(self.request("GET", "/api/info")[1])
        self.assertEqual((third["viewer_urls"], third["qr_svg"]), ([], ""))

    def test_a_network_that_comes_later_brings_the_link(self):
        # Started with no network: "This PC has no network" until the PC connects; then the link and its QR code show,
        # with no restart (a restart would make a new link).
        current = {"urls": []}
        self.start(viewer_urls=lambda: current["urls"])
        before = json.loads(self.request("GET", "/api/info")[1])
        self.assertEqual((before["viewer_urls"], before["qr_svg"]), ([], ""))
        current["urls"] = [VIEWER_URL]
        after = json.loads(self.request("GET", "/api/info")[1])
        self.assertEqual(after["viewer_urls"], [VIEWER_URL])
        self.assertTrue(after["qr_svg"].startswith("<svg"))
        self.assertEqual(after["room"], before["room"])
        self.assertEqual(self.request("GET", "/v/room1")[0].status, 200)

    def test_info_draws_the_qr_code_once_per_address_list(self):
        current = {"urls": [VIEWER_URL]}
        self.start(viewer_urls=lambda: current["urls"])
        with mock.patch.object(webserver.qr, "encode", wraps=webserver.qr.encode) as encode:
            for _ in range(3):
                self.request("GET", "/api/info")
            self.assertEqual(encode.call_count, 1)
            current["urls"] = ["http://10.0.0.7:8765/v/room1"]
            self.request("GET", "/api/info")
            self.request("GET", "/api/info")
            self.assertEqual(encode.call_count, 2)

    def test_viewers_keep_working_when_the_address_changes(self):
        current = {"urls": [VIEWER_URL]}
        self.start(viewer_urls=lambda: current["urls"])
        viewer = self.open_events("viewer")
        vid = self.read_event(viewer)["id"]
        sharer = self.open_events("sharer")
        sid = self.read_event(sharer)["id"]
        current["urls"] = ["http://10.0.0.7:8765/v/room1"]
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["room"], "room1")
        self.assertEqual(self.signal(sid, {"to": vid, "type": "offer", "sdp": "v=0"})[0].status, 204)
        self.assertEqual(self.read_event(viewer), {"type": "offer", "sdp": "v=0", "from": "sharer"})

    # --- a viewer that reconnects ---

    TOKEN = "Abc_123456789"

    @staticmethod
    def raw_event(resp):
        """The next event's lines (without the blank line ending it)."""
        lines = []
        while (line := resp.fp.readline().decode()) not in ("\n", ""):
            lines.append(line.rstrip("\n"))
        return lines

    def reconnect(self, **how):
        """A viewer with a token connects, then connects again while its first stream is still open (a phone whose
        connection died without the server noticing yet), sending its previous id as `how` says. Returns the first
        stream, its hello, the second hello and what the sharer page heard about the second."""
        sharer = self.open_events("sharer")
        self.read_event(sharer)
        first = self.open_events("viewer", client=self.TOKEN)
        hello = self.read_event(first)
        self.read_event(sharer)  # viewer-joined
        prev = how.get("prev")
        second = self.open_events(
            "viewer",
            client=how.get("client", self.TOKEN),
            extra=f"&prev={prev(hello)}" if prev else "",
            headers={"Last-Event-ID": how["last"](hello)} if "last" in how else None,
            source=how.get("source"),
        )
        return first, hello, self.read_event(second), self.read_event(sharer)

    def test_a_viewer_hello_names_its_id_for_the_browser_to_send_back(self):
        # EventSource keeps the last event id and sends it as Last-Event-ID when it reconnects by itself.
        self.start()
        lines = self.raw_event(self.open_events("viewer", client=self.TOKEN))
        self.assertEqual(len(lines), 2, lines)
        hello = json.loads(lines[1].removeprefix("data: "))
        self.assertEqual(lines[0], f"id: {hello['id']}")
        # The sharer page's stream carries no id: a sharer page never takes over a viewer's row.
        self.assertTrue(self.raw_event(self.open_events("sharer"))[0].startswith("data: "))

    def test_a_viewer_back_with_its_previous_id_keeps_its_row(self):
        self.start()
        first, hello, again, joined = self.reconnect(prev=lambda h: h["id"])
        self.assertNotEqual(again["id"], hello["id"])
        self.assertEqual(again["code"], hello["code"])
        self.assertEqual((joined["type"], joined["id"], joined["replaces"]), ("viewer-joined", again["id"], hello["id"]))
        self.assertEqual(self.read_event(first), {"type": "replaced"})
        self.assertEqual(first.fp.read(), b"")  # and the server has closed the old stream
        self.assertEqual(self.hub.viewers(), [{"id": again["id"], "device": "Device"}])
        # Its handler finished before closing it, and the hub forgot it (one Peer and queue per takeover otherwise).
        self.assertNotIn(hello["id"], self.hub._leaving)

    def test_the_browser_s_last_event_id_counts_and_wins_over_an_older_one_in_the_address(self):
        # A reconnect by the browser itself keeps the address the page opened with, whose prev may be older.
        self.start()
        first, hello, again, joined = self.reconnect(prev=lambda h: "v-00000000", last=lambda h: h["id"])
        self.assertEqual((again["code"], joined["replaces"]), (hello["code"], hello["id"]))
        self.assertEqual(self.read_event(first), {"type": "replaced"})

    def test_another_device_with_the_id_and_the_token_joins_as_a_new_one(self):
        # Ids and tokens travel in plain HTTP on the Wi-Fi: without the phone's address they take over nothing.
        self.start()
        first, hello, again, joined = self.reconnect(prev=lambda h: h["id"], source="127.0.0.2")
        self.assertNotEqual(again["code"], hello["code"])
        self.assertNotIn("replaces", joined)
        self.assertEqual(len(self.hub.viewers()), 2)

    def test_another_browser_with_the_id_joins_as_a_new_one(self):
        self.start()
        first, hello, again, joined = self.reconnect(prev=lambda h: h["id"], client="Zyx_987654321")
        self.assertNotEqual(again["code"], hello["code"])
        self.assertNotIn("replaces", joined)
        self.assertEqual(len(self.hub.viewers()), 2)

    # --- easy address ---

    def discovery(self, **kwargs):
        return Discovery("porthole", "192.168.1.20", 8765, announcer_factory=lambda name, ip: FakeAnnouncer(name, ip), **kwargs)

    def start_sharing(self):
        """Connect a sharer page and report sharing, as the page does after Start sharing."""
        sharer = self.open_events("sharer")
        self.read_event(sharer)
        self.assertEqual(self.api("/api/status", json.dumps({"sharing": True}))[0].status, 204)
        return sharer

    def test_easy_name_is_an_accepted_host(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        for host in (f"porthole.local:{self.port}", "porthole.local", f"PortHole.Local:{self.port}"):
            self.assertEqual(self.request("GET", "/v/room1", host=host)[0].status, 200, host)
        self.assertEqual(self.request("GET", "/v/room1", host=f"other.local:{self.port}")[0].status, 400)
        self.assertEqual(self.request("GET", "/v/room1", host=f"porthole.local.evil:{self.port}")[0].status, 400)

    def test_easy_name_only_reaches_the_viewer_side(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        easy = f"porthole.local:{self.port}"
        self.assertEqual(self.request("GET", "/static/view.js", host=easy)[0].status, 200)
        for path in ("/share", "/api/info", "/api/ping", "/events?room=room1&role=sharer", "/nope"):
            self.assertEqual(self.request("GET", path, host=easy)[0].status, 400, path)
        for path in ("/api/quit", "/api/status", "/api/discovery", "/api/recording/start", "/nope"):
            resp, _ = self.request("POST", path, body="{}", host=easy, headers=self.origin())
            self.assertEqual(resp.status, 400, path)
        self.assertTrue(self.thread.is_alive())  # the refused quit did nothing

    def test_easy_name_can_watch(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        easy = f"porthole.local:{self.port}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/events?room=room1&role=viewer", headers={"Host": easy})
        viewer = conn.getresponse()
        self.addCleanup(conn.close)
        self.assertEqual(viewer.status, 200)
        vid = self.read_event(viewer)["id"]
        resp, body = self.request("POST", f"/signal?room=room1&id={vid}", body=json.dumps({"to": "sharer", "type": "ready"}), host=easy)
        self.assertEqual(resp.status, 409)  # reached the hub: nobody is sharing yet
        self.assertEqual(json.loads(body)["error"], "nobody is sharing")

    def test_root_opens_the_share_only_while_easy_address_is_on_and_sharing(self):
        local = {"yes": True}
        discovery = self.discovery(look_seconds=0.3)
        self.start(is_local=lambda ip: local["yes"], discovery=discovery)
        easy = f"porthole.local:{self.port}"
        sharer = self.start_sharing()
        local["yes"] = False
        self.assertEqual(self.request("GET", "/", host=easy)[0].status, 404)  # easy address off
        discovery.set_enabled(True)
        resp, _ = self.request("GET", "/", host=easy)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.getheader("Location"), "/v/room1")
        self.assertEqual(self.request("GET", "/")[0].status, 302)  # by IP too
        local["yes"] = True
        resp, _ = self.request("GET", "/")
        self.assertEqual(resp.getheader("Location"), "/share")  # this PC still gets the sharer page
        self.assertEqual(self.api("/api/status", json.dumps({"sharing": False}))[0].status, 204)
        local["yes"] = False
        resp, body = self.request("GET", "/", host=easy)
        self.assertEqual(resp.status, 503)  # not sharing: a page that tries again, never the room
        self.assertNotIn("room1", body)
        discovery.set_enabled(False)
        # This device was waiting for a share by the name: a moment longer it is told to use the link (in real life the
        # name then goes, so nothing answers by it at all).
        self.assert_use_the_link(*self.request("GET", "/", host=easy))
        time.sleep(0.5)
        resp, body = self.request("GET", "/", host=easy)
        self.assertEqual(resp.status, 404)  # easy address off: a dead end, as before
        self.assertIn("Ask the person sharing for the link", body)
        self.assertNotIn('http-equiv="refresh"', body)
        sharer.close()

    def assert_waiting_page(self, resp, body):
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.getheader("Retry-After"), "5")
        # A reload every 5 s made screen readers read the page out again each time: a script checks
        # quietly instead, and the refresh is kept only for browsers without scripts.
        self.assertIn('<noscript><meta http-equiv="refresh" content="5"></noscript>', body)
        self.assertNotRegex(body, r'(?<!<noscript>)<meta http-equiv="refresh"')
        self.assertIn('<script type="module" src="/static/retry-page.js"></script>', body)
        self.assertIn('data-retry="5"', body)
        self.assertIn("Nothing shared yet", body)
        self.assertIn("Keep this page open", body)
        self.assertNotIn("Ask the person sharing for the link", body)  # they typed what the sharer showed
        self.assertNotIn("room1", body)  # the room stays private until something is shared
        self.assertNotRegex(body, r"<script(?![^>]*\bsrc=)|style=")  # the CSP allows no inline script or style

    def test_easy_address_typed_before_start_sharing_waits_then_opens_the_share(self):
        local = {"yes": True}
        discovery = self.discovery()
        discovery.set_enabled(True)  # the sharer page turns it on as soon as it opens
        self.start(is_local=lambda ip: local["yes"], discovery=discovery)
        easy = f"porthole.local:{self.port}"
        local["yes"] = False
        self.assert_waiting_page(*self.request("GET", "/", host=easy))
        self.assert_waiting_page(*self.request("GET", "/"))  # by IP too, like the redirect
        self.assertEqual(self.request("GET", "/static/retry-page.js", host=easy)[0].status, 200)  # its quiet check
        local["yes"] = True
        sharer = self.start_sharing()
        local["yes"] = False
        resp, _ = self.request("GET", "/", host=easy)  # what the page's refresh asks for
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.getheader("Location"), "/v/room1")
        sharer.close()

    def test_waiting_page_is_only_for_the_root(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: False, discovery=discovery)
        resp, body = self.request("GET", "/nope", host=f"192.168.1.20:{self.port}")
        self.assertEqual(resp.status, 404)
        self.assertNotIn('http-equiv="refresh"', body)

    def test_root_stops_redirecting_when_the_sharer_page_goes_away(self):
        local = {"yes": True}
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: local["yes"], discovery=discovery)
        sharer = self.start_sharing()
        sharer.close()
        deadline = time.monotonic() + 3
        while self.hub.sharing and time.monotonic() < deadline:
            time.sleep(0.05)
        local["yes"] = False
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 503)  # back to the page that tries again
        self.assertNotIn("room1", body)

    def test_old_room_link_opens_the_current_room_while_easy_address_is_on(self):
        local = {"yes": True}
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: local["yes"], discovery=discovery)
        sharer = self.start_sharing()
        local["yes"] = False
        for host in (f"porthole.local:{self.port}", f"192.168.1.20:{self.port}"):
            resp, _ = self.request("GET", "/v/oldroom42", host=host)
            self.assertEqual(resp.status, 302, host)
            self.assertEqual(resp.getheader("Location"), "/v/room1", host)
        resp, body = self.request("GET", "/v/room1")
        self.assertEqual(resp.status, 200)  # the current room is served as before
        self.assertIn("<video", body)
        sharer.close()

    def test_old_room_link_waits_while_nothing_is_shared(self):
        # The room stays private until something is shared, as for "/": a page that tries again.
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: False, discovery=discovery)
        self.assert_waiting_page(*self.request("GET", "/v/oldroom42", host=f"porthole.local:{self.port}"))

    def test_a_device_waiting_by_the_name_is_told_to_use_the_link_once_it_goes_off(self):
        # Before, the name went at once: the waiting page's check couldn't find it, and it promised the share for ever.
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: False, discovery=discovery)
        easy = f"porthole.local:{self.port}"
        self.assert_waiting_page(*self.request("GET", "/", host=easy))
        discovery.set_enabled(False)
        self.assert_use_the_link(*self.request("GET", "/", host=easy))
        discovery.stop()

    def test_old_room_link_has_expired_while_easy_address_is_off(self):
        local = {"yes": True}
        discovery = self.discovery()
        self.start(is_local=lambda ip: local["yes"], discovery=discovery)
        sharer = self.start_sharing()
        local["yes"] = False
        resp, body = self.request("GET", "/v/oldroom42")
        self.assertEqual(resp.status, 404)
        self.assertIn("This link doesn&#x27;t work", body)
        self.assertNotIn("room1", body)
        sharer.close()

    def test_old_room_link_has_expired_without_discovery(self):
        self.start(is_local=lambda ip: False)
        resp, body = self.request("GET", "/v/oldroom42")
        self.assertEqual(resp.status, 404)
        self.assertIn("This link doesn&#x27;t work", body)

    def test_info_lists_recent_refusals(self):
        self.start(hub=Hub(room="room1", join_limit=100))
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["refusals"], [])
        streams = [self.open_events("viewer") for _ in range(MAX_TABS)]
        for stream in streams:
            self.read_event(stream)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        android = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
        conn.request("GET", "/events?room=room1&role=viewer", headers={"Host": f"127.0.0.1:{self.port}", "User-Agent": android})
        self.addCleanup(conn.close)
        self.assert_refused(conn.getresponse(), "too-many-tabs")
        refusals = json.loads(self.request("GET", "/api/info")[1])["refusals"]
        self.assertEqual(len(refusals), 1)
        self.assertEqual((refusals[0]["device"], refusals[0]["reason"]), ("Android phone", "too-many-tabs"))
        self.assertLess(abs(refusals[0]["time"] - time.time() * 1000), 60_000)

    def test_info_reports_easy_address_and_sharing(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        info = json.loads(self.request("GET", "/api/info")[1])
        self.assertEqual(info["easy"]["url"], "http://porthole.local:8765")
        self.assertTrue(info["easy"]["enabled"])
        self.assertFalse(info["sharing"])
        self.start_sharing()
        self.assertTrue(json.loads(self.request("GET", "/api/info")[1])["sharing"])

    def test_info_without_discovery(self):
        self.start()
        info = json.loads(self.request("GET", "/api/info")[1])
        self.assertIsNone(info["easy"])
        self.assertFalse(info["sharing"])

    def test_discovery_switch_needs_same_origin(self):
        discovery = self.discovery()
        self.start(discovery=discovery)
        body = json.dumps({"enabled": True})
        self.assertEqual(self.request("POST", "/api/discovery", body=body)[0].status, 403)
        self.assertFalse(discovery.state()["enabled"])
        resp, data = self.api("/api/discovery", body)
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(data)["enabled"])
        resp, data = self.api("/api/discovery", json.dumps({"enabled": False}))
        self.assertFalse(json.loads(data)["enabled"])
        for bad in (json.dumps({"enabled": "yes"}), json.dumps({"enabled": 1}), json.dumps([True]), "{nope", json.dumps({})):
            self.assertEqual(self.api("/api/discovery", bad)[0].status, 400, bad)

    def test_discovery_switch_refused_to_lan_client(self):
        discovery = self.discovery()
        self.start(is_local=lambda ip: False, discovery=discovery)
        self.assertEqual(self.api("/api/discovery", json.dumps({"enabled": True}))[0].status, 403)
        self.assertFalse(discovery.state()["enabled"])

    def test_discovery_switch_without_discovery_503(self):
        self.start()
        self.assertEqual(self.api("/api/discovery", json.dumps({"enabled": True}))[0].status, 503)

    def test_discovery_switch_reports_a_blocked_name(self):
        discovery = self.discovery()
        discovery.blocked_reason = "Easy address is turned off for this run."
        self.start(discovery=discovery)
        resp, data = self.api("/api/discovery", json.dumps({"enabled": True}))
        self.assertEqual(resp.status, 200)
        state = json.loads(data)
        self.assertFalse(state["enabled"])
        self.assertFalse(state["available"])
        self.assertEqual(state["error"], "Easy address is turned off for this run.")

    def test_status_api(self):
        self.start()
        self.open_events("sharer")
        body = json.dumps({"sharing": True})
        self.assertEqual(self.request("POST", "/api/status", body=body)[0].status, 403)
        self.assertEqual(self.request("POST", "/api/status", body=body, headers={"Origin": "http://evil.example"})[0].status, 403)
        self.assertFalse(self.hub.sharing)
        for bad in (json.dumps({"sharing": "yes"}), json.dumps({"sharing": None}), json.dumps({}), json.dumps([1]), "{nope"):
            self.assertEqual(self.api("/api/status", bad)[0].status, 400, bad)
        self.assertEqual(self.api("/api/status", body)[0].status, 204)
        self.assertTrue(self.hub.sharing)

    # --- this run's approvals and switches, kept for the page that takes over (Open in my browser) ---

    CHOICES = {"settings": {"ask": True, "easy": False, "smooth": True}, "approved": ["Xy7_client-01"], "denied": ["v-0123abcd"]}

    def keep(self, body, headers=None):
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.api("/api/choices", data, headers={"X-Porthole-Room": "room1", **(headers or {})})

    def test_porthole_keeps_the_sharer_page_s_approvals_and_switches(self):
        # After Open in my browser the browser tab has its own saved settings: it reads this run's from Porthole.
        self.start()
        self.assertIsNone(json.loads(self.request("GET", "/api/info")[1])["choices"])
        sharer_id = self.read_event(self.open_events("sharer"))["id"]
        self.assertEqual(self.keep({"id": sharer_id, **self.CHOICES})[0].status, 204)
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"], self.CHOICES)

    def test_only_the_sharer_page_can_change_the_kept_choices(self):
        # A replaced page (sharing moved to another tab) or a waiting one must not undo what the sharer page allowed.
        self.start()
        old = self.read_event(self.open_events("sharer"))["id"]
        self.assertEqual(self.keep({"id": old, **self.CHOICES})[0].status, 204)
        new = self.read_event(self.open_events("sharer", extra="&takeover=1"))["id"]
        emptied = {"settings": {"ask": False, "easy": False, "smooth": False}, "approved": [], "denied": []}
        for peer in (old, "s-nothere", ""):
            resp, body = self.keep({"id": peer, **emptied})
            self.assertEqual(resp.status, 409, peer)
            self.assertIn("error", json.loads(body))
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"], self.CHOICES)
        self.assertEqual(self.keep({"id": new, **emptied})[0].status, 204)
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"], emptied)

    def test_kept_choices_must_be_switches_and_device_keys(self):
        # Only the three switches, and only device keys as the sharer page knows them (a viewer's token or its id).
        self.start()
        sharer_id = self.read_event(self.open_events("sharer"))["id"]
        good = {"id": sharer_id, **self.CHOICES}
        bad_bodies = [
            "{nope",
            [1],
            {**good, "id": 5},
            {k: v for k, v in good.items() if k != "settings"},
            {**good, "settings": {"ask": True, "easy": False}},
            {**good, "settings": {"ask": "yes", "easy": False, "smooth": True}},
            {**good, "settings": ["ask"]},
            {**good, "approved": "Xy7_client-01"},
            {**good, "approved": [5]},
            {k: v for k, v in good.items() if k != "denied"},
        ]
        for bad in bad_bodies:
            self.assertEqual(self.keep(bad)[0].status, 400, bad)
        self.assertIsNone(json.loads(self.request("GET", "/api/info")[1])["choices"])
        # Unknown settings are dropped, never kept.
        self.assertEqual(self.keep({**good, "settings": {**self.CHOICES["settings"], "theme": "dark"}})[0].status, 204)
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"], self.CHOICES)

    def test_an_approval_tied_to_an_address_is_kept(self):
        # The sharer page ties an approval to the viewer's token and the address it was allowed from ("token@address").
        self.start()
        sharer_id = self.read_event(self.open_events("sharer"))["id"]
        choices = {**self.CHOICES, "approved": ["Xy7_client-01@192.168.1.30", "Xy7_client-01@fe80::1", "v-0a1b2c3d"]}
        self.assertEqual(self.keep({"id": sharer_id, **choices})[0].status, 204)
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"], choices)
        odd = {**self.CHOICES, "approved": ["Xy7_client-01@", "Xy7_client-01@host name", "@192.168.1.30"]}
        self.assertEqual(self.keep({"id": sharer_id, **odd})[0].status, 204)
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"]["approved"], [])

    def test_a_device_key_porthole_cant_keep_is_dropped_not_the_whole_report(self):
        # One odd key must not stop Porthole keeping the rest: the page sends every key in every report, so one refused
        # report would mean all later ones are, and a page that takes over would get old approvals and switches. A key
        # left out is asked about again, never let in.
        self.start()
        sharer_id = self.read_event(self.open_events("sharer"))["id"]
        emptied = {"settings": {"ask": False, "easy": False, "smooth": False}, "approved": [], "denied": []}
        for bad in ("short", "has space in it", "x" * 65, "abcdefgh\n", ""):
            self.assertEqual(self.keep({"id": sharer_id, **emptied})[0].status, 204)
            for name in ("approved", "denied"):
                report = {"id": sharer_id, **self.CHOICES, name: [bad, *self.CHOICES[name], bad]}
                self.assertEqual(self.keep(report)[0].status, 204, (name, bad))
                self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"], self.CHOICES, (name, bad))

    def test_kept_choices_are_local_same_origin_and_this_run_only(self):
        self.start()
        sharer_id = self.read_event(self.open_events("sharer"))["id"]
        body = json.dumps({"id": sharer_id, **self.CHOICES})
        self.assertEqual(self.request("POST", "/api/choices", body=body)[0].status, 403)  # no Origin
        self.assertEqual(self.request("POST", "/api/choices", body=body, headers={"Origin": "http://evil.example"})[0].status, 403)
        cross = {**self.origin(), "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request("POST", "/api/choices", body=body, headers=cross)[0].status, 403)
        self.assertEqual(self.keep(body, headers={"X-Porthole-Room": "old-room"})[0].status, 409)  # a page from an earlier run
        self.assertIsNone(json.loads(self.request("GET", "/api/info")[1])["choices"])

    def test_kept_choices_refused_to_lan_client(self):
        self.start(is_local=lambda ip: False)
        self.assertEqual(self.keep({"id": "s-x", **self.CHOICES})[0].status, 403)

    def test_status_api_refused_to_lan_client(self):
        self.start(is_local=lambda ip: False)
        self.assertEqual(self.api("/api/status", json.dumps({"sharing": True}))[0].status, 403)

    def redirect_helper(self, discovery):
        helper = make_redirect_server(0, 8765, discovery)
        threading.Thread(target=helper.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(helper.server_close)
        self.addCleanup(helper.shutdown)
        port = helper.server_address[1]

        def get(host, path="/"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path, headers={"Host": host})
            resp = conn.getresponse()
            body = resp.read().decode()
            conn.close()
            return resp, body

        return get

    def test_redirect_server_points_to_the_main_port(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        get = self.redirect_helper(discovery)
        for host in ("porthole.local", "porthole.local:80"):
            resp, _ = get(host)
            self.assertEqual(resp.status, 302, host)
            self.assertEqual(resp.getheader("Location"), "http://porthole.local:8765/")
        resp, _ = get("192.168.1.20", "/anything")
        self.assertEqual(resp.getheader("Location"), "http://192.168.1.20:8765/")
        discovery.set_enabled(False)
        resp, body = get("porthole.local")
        self.assertEqual(resp.status, 404)
        self.assertIn("<h1>", body)

    def test_redirect_server_checks_the_host(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        get = self.redirect_helper(discovery)
        for host in ("evil.example", "other.local", ""):
            self.assertEqual(get(host)[0].status, 400, host)
        self.assert_use_the_link(*get("mypc.local"))

    def test_redirect_server_s_use_the_link_page_gets_its_style_sheet(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        get = self.redirect_helper(discovery)
        for host in ("mypc.local", "evil.example", "porthole.local"):
            resp, body = get(host, "/static/style.css")
            self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "text/css; charset=utf-8"), host)
            self.assertEqual(body, (WEB / "style.css").read_text(), host)
        self.assertEqual(get("mypc.local", "/static/view.js")[0].status, 400)

    def test_redirect_server_uses_the_main_servers_host_rule(self):
        discovery = self.discovery()
        discovery.set_enabled(True)
        get = self.redirect_helper(discovery)
        for path in ("/v/room1", "/static/view.js", "/events?room=room1&role=viewer"):
            self.assertEqual(get("porthole.local", path)[0].status, 302, path)
        for path in ("/share", "/api/info", "/events?room=room1&role=sharer"):
            self.assertEqual(get("porthole.local", path)[0].status, 400, path)
            self.assertEqual(get("192.168.1.20", path)[0].status, 302, path)

    # --- the easy address turned off while devices watch by it ---

    def other_devices(self, *addresses):
        """Loopback addresses that stand in for devices on the network (Linux and Windows answer on all of 127/8;
        macOS has only 127.0.0.1 unless more are added)."""
        for address in addresses:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=5, source_address=(address, 0)).close()
            except OSError as err:
                self.skipTest(f"this system can't connect from {address}: {err}")

    def lingering_discovery(self, linger):
        self.announcers = []

        def factory(name, ip):
            self.announcers.append(FakeAnnouncer(name, ip))
            self.announcers[-1].start()
            return self.announcers[-1]

        return Discovery("porthole", "192.168.1.20", 8765, announcer_factory=factory, linger_seconds=linger)

    def wait_until(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.05)
        return condition()

    def test_easy_address_turned_off_stays_for_the_devices_watching_by_it(self):
        # Ask off turns Easy address off. A phone that came by the name keeps its picture, and with the name still
        # answered for it, it can reconnect and get the next share. Nobody else gets in by the name: "/" no longer
        # sends anyone to the room, and any other device that comes by the name is told to use the link.
        phone, newcomer = "127.0.0.2", "127.0.0.3"
        discovery = self.lingering_discovery(linger=0.3)
        discovery.set_enabled(True)
        self.start(is_local=lambda ip: ip == "127.0.0.1", discovery=discovery)
        self.other_devices(phone, newcomer)
        easy = f"porthole.local:{self.port}"
        sharer = self.start_sharing()
        resp, _ = self.request("GET", "/", host=easy, source=phone)
        self.assertEqual(resp.getheader("Location"), "/v/room1")
        watching = self.open_events("viewer", host=easy, source=phone)
        self.assertEqual(self.read_event(watching)["type"], "hello")

        state = json.loads(self.api("/api/discovery", json.dumps({"enabled": False}))[1])
        self.assertFalse(state["enabled"])
        self.assertFalse(self.announcers[0].stopped)  # still answered for the phone

        # The phone carries on: a new stream after a blip, its messages, the page again.
        again = self.open_events("viewer", host=easy, source=phone)
        vid = self.read_event(again)["id"]
        resp, _ = self.request("POST", f"/signal?room=room1&id={vid}", body=json.dumps({"to": "sharer", "type": "ready"}), host=easy, source=phone)
        self.assertEqual(resp.status, 204)
        self.assertEqual(self.request("GET", "/v/room1", host=easy, source=phone)[0].status, 200)
        resp, body = self.request("GET", "/", host=easy, source=phone)
        self.assertEqual(resp.status, 404)  # off: "/" sends nobody to the room any more
        self.assertNotIn("room1", body)

        # Any other device that comes by the name is turned away, whatever it asks for.
        for path in ("/", "/v/room1", "/v/oldroom42", "/static/view.js", "/events?room=room1&role=viewer"):
            self.assert_use_the_link(*self.request("GET", path, host=easy, source=newcomer), path)
        resp, body = self.request("POST", f"/signal?room=room1&id={vid}", body="{}", host=easy, source=newcomer)
        self.assertEqual(resp.status, 400)
        # The link works for it as ever: whoever has the link may watch, as Ask (now off) allows.
        self.assertEqual(self.request("GET", "/v/room1", host=f"192.168.1.20:{self.port}", source=newcomer)[0].status, 200)

        # Once the phone has gone and the wait is over, the name goes.
        watching.close()
        again.close()
        self.assertTrue(self.wait_until(lambda: self.announcers[0].stopped))
        self.assertEqual(len(self.announcers), 1)
        sharer.close()

    def test_easy_address_turned_off_goes_at_once_when_nobody_watches_by_it(self):
        discovery = self.lingering_discovery(linger=5)
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        by_link = self.open_events("viewer")  # came by the link, not the name
        self.read_event(by_link)
        self.api("/api/discovery", json.dumps({"enabled": False}))
        self.assertTrue(self.announcers[0].stopped)
        by_link.close()

    def test_a_viewer_by_the_name_that_was_denied_doesnt_keep_it(self):
        # Denied (or removed) by the sharer, a device isn't one of those watching, even if its stream stays open.
        discovery = self.lingering_discovery(linger=5)
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        sharer = self.open_events("sharer")
        sid = self.read_event(sharer)["id"]
        viewer = self.open_events("viewer", host=f"porthole.local:{self.port}")
        vid = self.read_event(viewer)["id"]
        self.assertEqual(self.read_event(sharer)["type"], "viewer-joined")
        self.assertEqual(self.signal(sid, {"to": vid, "type": "denied"})[0].status, 204)
        self.assertEqual(self.read_event(viewer)["type"], "denied")
        self.api("/api/discovery", json.dumps({"enabled": False}))
        self.assertTrue(self.wait_until(lambda: self.announcers[0].stopped, 2))
        viewer.close()
        sharer.close()

    def test_a_refused_viewer_stream_by_the_name_doesnt_keep_it(self):
        discovery = self.lingering_discovery(linger=5)
        discovery.set_enabled(True)
        self.start(discovery=discovery)
        resp = self.open_events("viewer", room="wrong", host=f"porthole.local:{self.port}")
        self.assertEqual(resp.status, 403)
        resp.close()
        self.api("/api/discovery", json.dumps({"enabled": False}))
        self.assertTrue(self.announcers[0].stopped)

    # --- local-only actions ---

    def test_quit_needs_same_origin(self):
        self.start()
        self.assertEqual(self.request("POST", "/api/quit")[0].status, 403)
        self.assertEqual(self.request("POST", "/api/quit", headers={"Origin": "http://evil.example"})[0].status, 403)
        self.assertEqual(self.api("/api/quit", headers={"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(self.api("/api/quit")[0].status, 204)
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def test_open_in_browser_is_local_and_same_origin_only(self):
        calls = []
        self.start(open_in_browser=lambda: calls.append("open"))
        self.assertEqual(self.request("POST", "/api/open-in-browser")[0].status, 403)
        evil = {"Origin": "http://evil.example"}
        self.assertEqual(self.request("POST", "/api/open-in-browser", headers=evil)[0].status, 403)
        self.assertEqual(self.api("/api/open-in-browser", headers={"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(calls, [])
        self.assertEqual(self.request("GET", "/api/open-in-browser")[0].status, 404)
        self.assertEqual(self.api("/api/open-in-browser")[0].status, 204)
        self.assertEqual(calls, ["open"])

    def test_open_in_browser_from_another_device_is_refused(self):
        calls = []
        self.start(is_local=lambda ip: False, open_in_browser=lambda: calls.append("open"))
        self.assertEqual(self.api("/api/open-in-browser")[0].status, 403)
        self.assertEqual(calls, [])

    def test_open_in_browser_without_a_handler_or_when_it_fails(self):
        self.start()
        resp, body = self.api("/api/open-in-browser")
        self.assertEqual(resp.status, 503)
        self.assertIn("error", json.loads(body))

        def broken():
            raise OSError("no browser")

        self.start(open_in_browser=broken)
        resp, body = self.api("/api/open-in-browser")
        self.assertEqual(resp.status, 500)
        self.assertIn("error", json.loads(body))

    def test_open_in_browser_that_fails_says_why_in_plain_words(self):
        # The page puts this after "Couldn't open your browser: ", so no "can't open the browser" again and no error
        # numbers ("[Errno 2] No such file or directory: 'xdg-open'").
        for err, cause in (
            (webbrowser.Error("no browser was found"), "no browser was found"),
            (webbrowser.Error("could not locate runnable browser"), "no browser was found"),  # Python's own words
            (FileNotFoundError(errno.ENOENT, "No such file or directory", "xdg-open"), "xdg-open is missing"),
            (PermissionError(errno.EACCES, "Permission denied", "/usr/bin/x-www-browser"), "Porthole isn't allowed to start it"),
            (OSError(errno.EAGAIN, "Resource temporarily unavailable"), "resource temporarily unavailable"),
            (RuntimeError("boom"), "something went wrong"),
        ):
            with self.subTest(cause=cause):

                def broken(err=err):
                    raise err

                self.start(open_in_browser=broken)
                resp, body = self.api("/api/open-in-browser")
                self.assertEqual(resp.status, 500)
                self.assertEqual(json.loads(body), {"error": cause})

    def open_folder_failing(self, err):
        """POST /api/open-folder while opening the folder fails with `err`; the recordings folder and the answer."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "Screen shares"
        self.start(recorder=Recorder(folder))
        with mock.patch.object(webserver, "open_folder", side_effect=err):
            resp, body = self.api("/api/open-folder")
        self.assertEqual(resp.status, 500)
        return folder, json.loads(body)["error"]

    def test_open_folder_without_xdg_open_says_where_the_recording_is(self):
        # Before: "can't open the folder: [Errno 2] No such file or directory: 'xdg-open'".
        folder, error = self.open_folder_failing(FileNotFoundError(errno.ENOENT, "No such file or directory", "xdg-open"))
        self.assertEqual(error, f"Couldn't open the folder. The recording is in {folder}. Opening folders needs xdg-utils.")

    def test_open_folder_failing_otherwise_says_where_the_recording_is(self):
        # Windows' startfile or the Mac's open failing, or no permission: the folder, without the Linux part.
        folder, error = self.open_folder_failing(OSError(errno.EACCES, "Permission denied"))
        self.assertEqual(error, f"Couldn't open the folder. The recording is in {folder}.")

    def test_a_sharer_page_in_porthole_s_own_window_ties_it_again(self):
        # After a browser tab took over, the window's page sharing again makes closing the window stop Porthole again.
        calls = []
        self.start(window_back=lambda: calls.append("back"))
        self.read_event(self.open_events("sharer"))
        self.read_event(self.open_events("viewer", extra="&window=1"))
        self.assertEqual(calls, [])
        self.read_event(self.open_events("sharer", extra="&takeover=1&window=1"))
        self.assertEqual(calls, ["back"])

    def test_a_page_from_an_earlier_run_cant_drive_this_one(self):
        # Porthole killed and started again on the same port: the old sharer page is still open, and its Quit must
        # not stop the new run. The page sends its room with every action.
        self.start()
        stale = {"X-Porthole-Room": "old-room"}
        resp, body = self.api("/api/quit", headers=stale)
        self.assertEqual(resp.status, 409)
        self.assertIn("earlier run", json.loads(body)["error"])
        self.assertFalse(self.hub.ended)
        self.assertEqual(self.api("/api/status", json.dumps({"sharing": False}), headers=stale)[0].status, 409)
        self.assertEqual(self.api("/api/status", json.dumps({"sharing": False}), headers={"X-Porthole-Room": "room1"})[0].status, 204)
        self.assertEqual(self.api("/api/status", json.dumps({"sharing": False}))[0].status, 204)  # without it, as before

    def test_release_window_is_local_and_same_origin_only(self):
        # The app window's page, moved to a browser tab with "Use this one", lets go of the window: closing it
        # must no longer stop Porthole.
        calls = []
        self.start(release_window=lambda: calls.append("release"))
        self.assertEqual(self.request("POST", "/api/release-window")[0].status, 403)
        self.assertEqual(self.request("POST", "/api/release-window", headers={"Origin": "http://evil.example"})[0].status, 403)
        self.assertEqual(self.api("/api/release-window", headers={"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(calls, [])
        self.assertEqual(self.api("/api/release-window")[0].status, 204)
        self.assertEqual(calls, ["release"])
        self.start(is_local=lambda ip: False, release_window=lambda: calls.append("release"))
        self.assertEqual(self.api("/api/release-window")[0].status, 403)
        self.start()
        self.assertEqual(self.api("/api/release-window")[0].status, 503)
        self.assertEqual(calls, ["release"])

    def test_the_sharer_page_takes_the_window_tag(self):
        self.start()
        resp, body = self.request("GET", "/share?window")
        self.assertEqual(resp.status, 200)
        self.assertIn("share.js", body)

    def test_quit_tells_viewers_first(self):
        self.start()
        viewer = self.open_events("viewer")
        self.read_event(viewer)
        self.assertEqual(self.api("/api/quit")[0].status, 204)
        self.assertEqual(self.read_event(viewer), {"type": "ended"})

    def test_second_sharer_tab_is_busy_until_it_takes_over(self):
        self.start()
        first = self.open_events("sharer")
        self.read_event(first)
        info = json.loads(self.request("GET", "/api/info")[1])
        self.assertTrue(info["sharer_active"])
        self.assertEqual(self.open_events("sharer").status, 409)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/events?room=room1&role=sharer&takeover=1", headers={"Host": f"127.0.0.1:{self.port}"})
        second = conn.getresponse()
        self.addCleanup(conn.close)
        self.assertEqual(second.status, 200)
        self.assertEqual(self.read_event(first), {"type": "replaced"})

    def test_recording_chunk_cut_short_is_400(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = Recorder(Path(tmp.name))
        self.addCleanup(recorder.close_all)
        self.start(recorder=recorder)
        rec = json.loads(self.api("/api/recording/start", json.dumps({"ext": "webm"}))[1])
        sock = socket.create_connection(("127.0.0.1", self.port))
        head = (
            f"POST /api/recording/chunk?id={rec['id']}&seq=0 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Origin: http://127.0.0.1:{self.port}\r\nContent-Length: 1000\r\n\r\n"
        )
        sock.sendall(head.encode() + b"x" * 10)
        sock.shutdown(socket.SHUT_WR)
        reply = sock.recv(200).decode()
        sock.close()
        self.assertIn(" 400 ", reply.splitlines()[0])
        resp, body = self.api(f"/api/recording/chunk?id={rec['id']}&seq=0", b"whole piece")
        self.assertEqual(resp.status, 204)

    def test_local_actions_refused_to_lan_client(self):
        self.start(is_local=lambda ip: False)
        self.assertEqual(self.api("/api/quit")[0].status, 403)

    def test_recording_endpoints_round_trip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = Recorder(Path(tmp.name))
        self.addCleanup(recorder.close_all)
        self.start(recorder=recorder)
        resp, body = self.api("/api/recording/start", json.dumps({"ext": "webm"}))
        self.assertEqual(resp.status, 200)
        rec = json.loads(body)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=0", b"hello ")[0].status, 204)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"world")[0].status, 204)
        resp, body = self.api(f"/api/recording/stop?id={rec['id']}")
        done = json.loads(body)
        self.assertEqual(done["bytes"], 11)
        self.assertEqual(Path(done["path"]).read_bytes(), b"hello world")

    def test_a_recording_with_no_picture_is_deleted(self):
        # Started while paused and stopped before Resume, the page says nothing was recorded (`recorded=0`): the file,
        # holding at most a header, goes, and the answer says so instead of naming a saved file.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = Recorder(Path(tmp.name))
        self.addCleanup(recorder.close_all)
        self.start(recorder=recorder)
        rec = json.loads(self.api("/api/recording/start", json.dumps({"ext": "webm"}))[1])
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=0", b"header")[0].status, 204)
        resp, body = self.api(f"/api/recording/stop?id={rec['id']}&recorded=0")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body), {"empty": True, "name": rec["name"], "bytes": 0})
        self.assertEqual(os.listdir(tmp.name), [])

    def test_a_full_disk_stops_the_recording_and_says_why(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = Recorder(Path(tmp.name))
        self.addCleanup(recorder.close_all)
        self.start(recorder=recorder)
        rec = json.loads(self.api("/api/recording/start", json.dumps({"ext": "webm"}))[1])
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=0", b"saved")[0].status, 204)
        real = recorder._open[rec["id"]].handle

        class FullDisk:
            def write(self, data):
                raise OSError(errno.ENOSPC, "No space left on device")

            def __getattr__(self, name):
                return getattr(real, name)

        recorder._open[rec["id"]].handle = FullDisk()
        resp, body = self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"lost")
        self.assertEqual(resp.status, 507)
        self.assertEqual(json.loads(body), {"error": "the recording stopped: the disk is full", "stopped": "the disk is full"})
        resp, body = self.api(f"/api/recording/stop?id={rec['id']}")
        self.assertEqual(resp.status, 200)
        done = json.loads(body)
        self.assertEqual((done["name"], done["bytes"], done["stopped"]), (rec["name"], 5, "the disk is full"))
        self.assertEqual(Path(done["path"]).read_bytes(), b"saved")

    def test_recording_chunk_wrong_seq_409(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = Recorder(Path(tmp.name))
        self.addCleanup(recorder.close_all)
        self.start(recorder=recorder)
        rec = json.loads(self.api("/api/recording/start", json.dumps({"ext": "mp4"}))[1])
        resp, body = self.api(f"/api/recording/chunk?id={rec['id']}&seq=5", b"x")
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(body)["expected"], 0)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=abc", b"x")[0].status, 400)

    def test_recording_bad_extension_400(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.start(recorder=Recorder(Path(tmp.name)))
        self.assertEqual(self.api("/api/recording/start", json.dumps({"ext": "exe"}))[0].status, 400)

    def test_recording_without_recorder_503(self):
        self.start()
        self.assertEqual(self.api("/api/recording/start", json.dumps({"ext": "webm"}))[0].status, 503)

    # --- a recording left open when the sharer page goes away ---

    def recording_server(self, delay, page=""):
        """A sharer page (known by its `page` token, if given) with a recording open, one piece in."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = Recorder(Path(tmp.name))
        self.addCleanup(recorder.close_all)
        self.start(recorder=recorder, orphan_delay=delay)
        sharer = self.open_events("sharer", extra=f"&page={page}" if page else "")
        self.read_event(sharer)
        body = {"ext": "webm", "page": page} if page else {"ext": "webm"}
        rec = json.loads(self.api("/api/recording/start", json.dumps(body))[1])
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=0", b"piece")[0].status, 204)
        return sharer, rec

    def wait_until_sharer_gone(self):
        deadline = time.monotonic() + 3
        while self.hub.sharer_active() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.hub.sharer_active())

    def test_orphaned_recording_is_closed_after_the_delay(self):
        self.assertEqual(webserver.ORPHAN_SECONDS, 10)
        sharer, rec = self.recording_server(delay=0.3)
        sharer.close()
        self.wait_until_sharer_gone()
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"more")[0].status, 204)  # not yet
        time.sleep(0.6)
        resp, _ = self.api(f"/api/recording/chunk?id={rec['id']}&seq=2", b"late")
        self.assertEqual(resp.status, 404)  # closed: the file keeps what was saved

    def test_recording_stays_open_when_a_sharer_comes_back_in_time(self):
        sharer, rec = self.recording_server(delay=0.4)
        sharer.close()
        self.wait_until_sharer_gone()
        back = self.open_events("sharer")  # e.g. the page reloaded
        self.read_event(back)
        time.sleep(0.7)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"more")[0].status, 204)

    def test_a_sharer_that_comes_and_goes_again_restarts_the_wait(self):
        sharer, rec = self.recording_server(delay=0.5)
        sharer.close()
        self.wait_until_sharer_gone()
        time.sleep(0.3)
        back = self.open_events("sharer")
        self.read_event(back)
        back.close()
        self.wait_until_sharer_gone()
        time.sleep(0.35)  # past the first wait, not the second
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"more")[0].status, 204)
        time.sleep(0.5)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=2", b"late")[0].status, 404)

    def test_a_replaced_sharer_leaving_does_not_close_the_recording(self):
        sharer, rec = self.recording_server(delay=0.2)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/events?room=room1&role=sharer&takeover=1", headers={"Host": f"127.0.0.1:{self.port}"})
        self.addCleanup(conn.close)
        self.read_event(conn.getresponse())
        self.assertEqual(self.read_event(sharer), {"type": "replaced"})
        time.sleep(0.5)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"more")[0].status, 204)

    def test_the_same_page_reconnecting_keeps_its_recording(self):
        # After a network blip the page carries on recording: its stream comes back with the same page token.
        sharer, rec = self.recording_server(delay=0.4, page="page-one-1234")
        sharer.close()
        self.wait_until_sharer_gone()
        self.read_event(self.open_events("sharer", extra="&page=page-one-1234"))
        time.sleep(0.7)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"more")[0].status, 204)

    def test_a_page_whose_new_stream_opens_before_the_old_one_closes_keeps_its_recording(self):
        # A silent stream is replaced at once; the server notices the old one gone about a second later.
        sharer, rec = self.recording_server(delay=0.3, page="page-one-1234")
        self.read_event(self.open_events("sharer", extra="&takeover=1&page=page-one-1234"))
        self.read_event(sharer)  # "replaced": its stream ends
        sharer.close()
        time.sleep(0.6)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"more")[0].status, 204)

    def test_a_reloaded_page_does_not_keep_the_old_recording_open(self):
        # A reloaded (or newly opened) page has no recorder and doesn't know the recording: it can never finish it.
        sharer, rec = self.recording_server(delay=0.3, page="page-one-1234")
        sharer.close()
        self.wait_until_sharer_gone()
        self.read_event(self.open_events("sharer", extra="&page=page-two-5678"))
        time.sleep(0.6)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"late")[0].status, 404)

    def test_a_replaced_page_that_never_stops_its_recording_has_it_closed(self):
        # "Use this one": the old tab stops its recording itself, but closed before it could, the file stays open.
        sharer, rec = self.recording_server(delay=0.3, page="page-one-1234")
        taker = self.open_events("sharer", extra="&takeover=1&page=page-two-5678")
        self.read_event(taker)
        self.assertEqual(self.read_event(sharer), {"type": "replaced"})
        sharer.close()
        time.sleep(0.7)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=1", b"late")[0].status, 404)

    def test_the_old_page_leaving_never_closes_the_new_page_s_recording(self):
        sharer, _ = self.recording_server(delay=0.3, page="page-one-1234")
        taker = self.open_events("sharer", extra="&takeover=1&page=page-two-5678")
        self.read_event(taker)
        self.read_event(sharer)
        body = json.dumps({"ext": "webm", "page": "page-two-5678"})
        rec = json.loads(self.api("/api/recording/start", body)[1])
        sharer.close()
        time.sleep(0.7)
        self.assertEqual(self.api(f"/api/recording/chunk?id={rec['id']}&seq=0", b"mine")[0].status, 204)

    def test_an_event_stream_that_fails_to_start_leaves_no_peer_behind(self):
        # Out of file handles, the stream's selector can't be made: the page must not stay "connected" for good,
        # or every later sharer page hears "sharing is on in another window or tab".
        self.start()
        real = webserver.selectors.DefaultSelector
        calls = []

        def failing_once():
            calls.append(1)
            if len(calls) == 1:
                raise OSError(errno.EMFILE, "Too many open files")
            return real()

        with mock.patch.object(webserver.selectors, "DefaultSelector", failing_once):
            with contextlib.suppress(http.client.RemoteDisconnected):  # closed with no reply
                self.open_events("sharer").read()
            self.assertFalse(self.hub.sharer_active())
            self.assertEqual(self.open_events("sharer").status, 200)

    def test_the_replaced_page_hears_when_porthole_s_own_window_took_over(self):
        self.start()
        first = self.open_events("sharer")
        self.read_event(first)
        self.read_event(self.open_events("sharer", extra="&takeover=1&window=1"))
        self.assertEqual(self.read_event(first), {"type": "replaced", "window": True})

    def test_an_ipad_is_told_apart_from_a_mac_by_its_touch_screen(self):
        self.start()
        sharer = self.open_events("sharer")
        self.read_event(sharer)
        mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.1 Safari/605.1.15"
        self.read_event(self.open_events("viewer", extra="&touch=1", headers={"User-Agent": mac}))
        self.assertEqual(self.read_event(sharer)["device"], "iPad")
        self.read_event(self.open_events("viewer", headers={"User-Agent": mac}))
        self.assertEqual(self.read_event(sharer)["device"], "Mac")

    # --- signalling ---

    def test_events_and_signal_round_trip(self):
        self.start()
        viewer = self.open_events("viewer")
        self.assertEqual(viewer.getheader("Content-Type"), "text/event-stream")
        hello = self.read_event(viewer)
        self.assertEqual(hello["type"], "hello")
        vid = hello["id"]
        sharer = self.open_events("sharer")
        s_hello = self.read_event(sharer)
        self.assertEqual(s_hello["viewers"][0]["id"], vid)
        resp, _ = self.signal(s_hello["id"], {"to": vid, "type": "offer", "sdp": "v=0"})
        self.assertEqual(resp.status, 204)
        self.assertEqual(self.read_event(viewer), {"type": "offer", "sdp": "v=0", "from": "sharer"})

    def test_closed_events_connection_is_noticed_quickly(self):
        self.start()
        viewer = self.open_events("viewer")
        self.read_event(viewer)
        self.assertEqual(len(self.hub.viewers()), 1)
        viewer.close()
        deadline = time.monotonic() + 3
        while self.hub.viewers() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.hub.viewers(), [])

    def test_events_wrong_room_403(self):
        self.start()
        self.assertEqual(self.open_events("viewer", room="nope").status, 403)

    def test_events_bad_role_400(self):
        self.start()
        self.assertEqual(self.open_events("boss").status, 400)

    def test_sharer_events_refused_to_lan_client(self):
        self.start(is_local=lambda ip: False)
        self.assertEqual(self.open_events("sharer").status, 403)

    def test_signal_bad_json_400(self):
        self.start()
        viewer = self.open_events("viewer")
        vid = self.read_event(viewer)["id"]
        self.assertEqual(self.signal(vid, "{not json")[0].status, 400)
        self.assertEqual(self.signal(vid, "[1, 2]")[0].status, 400)

    def test_signal_too_big_413(self):
        self.start()
        self.assertEqual(self.signal("v-x", b"x" * 70000)[0].status, 413)

    def test_signal_wrong_room_403(self):
        self.start()
        self.assertEqual(self.signal("v-x", {"to": "sharer", "type": "ready"}, room="nope")[0].status, 403)

    def test_signal_type_must_be_string(self):
        self.start()
        viewer = self.open_events("viewer")
        vid = self.read_event(viewer)["id"]
        self.open_events("sharer")
        resp, _ = self.signal(vid, {"to": "sharer", "type": []})
        self.assertEqual(resp.status, 400)

    def test_signal_deep_json_400(self):
        self.start()
        self.assertEqual(self.signal("v-x", "[" * 60000)[0].status, 400)

    def test_sharer_signal_refused_from_lan(self):
        local = {"yes": True}
        self.start(is_local=lambda ip: local["yes"])
        viewer = self.open_events("viewer")
        vid = self.read_event(viewer)["id"]
        sharer = self.open_events("sharer")
        sid = self.read_event(sharer)["id"]
        local["yes"] = False
        resp, _ = self.signal(sid, {"to": vid, "type": "stopped"})
        self.assertEqual(resp.status, 403)

    def assert_refused(self, resp, reason):
        """A refused viewer gets a normal event stream with one "refused" event, then the end."""
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(self.read_event(resp), {"type": "refused", "reason": reason})
        self.assertEqual(resp.read(), b"")  # closed: nothing else follows

    def test_too_many_tabs_get_a_refused_event(self):
        self.start(hub=Hub(room="room1", join_limit=100))
        streams = [self.open_events("viewer") for _ in range(MAX_TABS)]
        for stream in streams:
            self.read_event(stream)
        self.assert_refused(self.open_events("viewer"), "too-many-tabs")
        self.assertEqual(len(self.hub.viewers()), MAX_TABS)  # the refused one never joined

    def test_too_many_tries_get_a_refused_event(self):
        self.start()
        for _ in range(6):
            stream = self.open_events("viewer")
            self.read_event(stream)
            stream.close()
            deadline = time.monotonic() + 5  # gone from the hub, so only the tries count, not open tabs
            while self.hub.viewers() and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assert_refused(self.open_events("viewer"), "too-many-tries")

    def test_other_events_errors_keep_their_status(self):
        self.start()
        self.assertEqual(self.open_events("viewer", room="nope").status, 403)

    # --- keep-alives ---

    def test_stream_sends_ping_data_events(self):
        self.assertEqual(webserver.PING_SECONDS, 15)
        with mock.patch.object(webserver, "PING_SECONDS", 0.2):
            self.start()
            viewer = self.open_events("viewer")
            self.read_event(viewer)  # hello
            started = time.monotonic()
            line = viewer.fp.readline().decode()
            self.assertEqual(line, 'data: {"type": "ping"}\n')  # a data event, not a ": ping" comment
            self.assertLess(time.monotonic() - started, 1.5)
            viewer.fp.readline()
            self.assertEqual(self.read_event(viewer), {"type": "ping"})  # and again

    def test_events_client_token_reaches_sharer(self):
        self.start()
        sharer = self.open_events("sharer")
        self.read_event(sharer)
        self.open_events("viewer", client="Tok_en-12345")
        self.assertEqual(self.read_event(sharer)["client"], "Tok_en-12345")

    def test_events_client_token_ending_in_a_newline_counts_as_none(self):
        # `client=abcdefgh%0A` arrives as "abcdefgh\n". Kept, it would become the device's key on the sharer page, which
        # Porthole can't keep: every report that lists it would be refused.
        self.start()
        sharer = self.open_events("sharer")
        sharer_id = self.read_event(sharer)["id"]
        self.open_events("viewer", client="abcdefgh%0A")
        joined = self.read_event(sharer)
        self.assertEqual(joined["client"], "")
        # The page then knows it by its id, which Porthole keeps.
        report = {"id": sharer_id, "settings": {"ask": True, "easy": False, "smooth": False}, "approved": [], "denied": [joined["id"]]}
        resp, _ = self.api("/api/choices", json.dumps(report), headers={"X-Porthole-Room": "room1"})
        self.assertEqual(resp.status, 204)
        self.assertEqual(json.loads(self.request("GET", "/api/info")[1])["choices"]["denied"], [joined["id"]])

    def test_signal_hub_error_status_passed_through(self):
        self.start()
        resp, body = self.signal("v-unknown", {"to": "sharer", "type": "ready"})
        self.assertEqual(resp.status, 404)
        self.assertEqual(json.loads(body)["error"], "unknown peer")


class VerboseTest(unittest.TestCase):
    """--verbose: one line per request on stderr, with the room code hidden."""

    start = ServerTest.start
    request = ServerTest.request

    def logged(self, verbose, requests):
        err = io.StringIO()
        with mock.patch.object(webserver.sys, "stderr", err):
            self.start(verbose=verbose) if verbose is not None else self.start()
            for method, path in requests:
                self.request(method, path)
            time.sleep(0.1)  # the log line is written as the response starts
        return err.getvalue()

    def test_off_by_default(self):
        self.assertEqual(self.logged(None, [("GET", "/v/room1"), ("GET", "/nope")]), "")
        self.assertEqual(self.logged(False, [("GET", "/v/room1")]), "")

    def test_each_request_is_logged_with_time_ip_method_path_and_status(self):
        out = self.logged(True, [("GET", "/v/room1"), ("GET", "/static/view.js"), ("GET", "/nope")])
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d 127\.0\.0\.1 GET /v/\*\*\*\* 200$")
        self.assertRegex(lines[1], r" GET /static/view\.js 200$")
        self.assertRegex(lines[2], r" GET /nope 404$")

    def test_the_room_code_never_appears(self):
        out = self.logged(
            True,
            [
                ("GET", "/v/room1"),
                ("GET", "/v/oldroom42"),
                ("GET", "/events?room=room1&role=nobody"),
                ("POST", "/signal?room=room1&id=v-1234"),
                ("GET", "/static/room1"),
            ],
        )
        self.assertNotIn("room1", out)
        self.assertNotIn("oldroom42", out)
        self.assertIn("GET /events?room=****&role=nobody 400", out)
        self.assertIn("POST /signal?room=****&id=v-1234 ", out)
        self.assertIn("GET /static/**** 404", out)

    def test_the_device_token_is_hidden_too(self):
        out = self.logged(True, [("GET", "/events?room=nope&role=viewer&client=Secret_token-123")])
        self.assertNotIn("Secret_token-123", out)
        self.assertIn("client=****", out)

    def test_mask_path(self):
        mask = webserver.mask_path
        self.assertEqual(mask("/v/abcDEF12", "abcDEF12"), "/v/****")
        self.assertEqual(mask("/v/whatever?x=1", "abcDEF12"), "/v/****?x=1")
        self.assertEqual(mask("/events?role=viewer&room=abc%44EF12", "abcDEF12"), "/events?role=viewer&room=****")
        self.assertEqual(mask("/share", "abcDEF12"), "/share")
        self.assertEqual(mask("/static/abcDEF12.js", "abcDEF12"), "/static/****.js")


class BindTest(unittest.TestCase):
    def test_second_server_on_same_port_fails(self):
        first = make_server(Hub(), 0, WEB, [])
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            make_server(Hub(), first.server_address[1], WEB, [])

    def test_starting_never_looks_up_names(self):
        # http.server looks up the full name of the address it listens on (socket.getfqdn). On a Mac that
        # reverse lookup can hang for about 30 s, so every start (and every test server) waited that long.
        def slow_lookup(*args):
            raise AssertionError("socket.getfqdn was called")

        with mock.patch.object(socket, "getfqdn", slow_lookup):
            server = make_server(Hub(), 0, WEB, [])
            self.addCleanup(server.server_close)
            helper = make_redirect_server(0, server.server_address[1], Discovery("porthole", None, 8765))
            self.addCleanup(helper.server_close)
        self.assertEqual(server.server_port, server.server_address[1])
        self.assertEqual(server.server_name, "0.0.0.0")

    def test_windows_bind_is_exclusive(self):
        calls = []

        class FakeSocket:
            def setsockopt(self, *args):
                calls.append(args)

            def bind(self, address):
                pass

            def getsockname(self):
                return ("127.0.0.1", 1234)

        server = LanServer.__new__(LanServer)
        server.socket = FakeSocket()
        server.server_address = ("127.0.0.1", 0)
        with mock.patch.object(socket, "SO_EXCLUSIVEADDRUSE", -5, create=True), mock.patch.object(webserver.os, "name", "nt"):
            server.server_bind()
        self.assertIn((socket.SOL_SOCKET, -5, 1), calls)
        self.assertNotIn((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), calls)


class HelpersTest(unittest.TestCase):
    def test_is_loopback(self):
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("::1"))
        self.assertTrue(is_loopback("::ffff:127.0.0.1"))
        self.assertFalse(is_loopback("192.168.0.5"))
        self.assertFalse(is_loopback("garbage"))

    def test_viewer_urls(self):
        self.assertEqual(
            share.viewer_urls(["192.168.0.31", "10.0.0.2"], 8765, "abc"),
            ["http://192.168.0.31:8765/v/abc", "http://10.0.0.2:8765/v/abc"],
        )

    def test_run_hint(self):
        with mock.patch.object(share.os, "name", "nt"):
            self.assertEqual(share.run_hint(), "py start.py")
        with mock.patch.object(share.os, "name", "posix"):
            self.assertEqual(share.run_hint(), "python3 start.py")

    def test_bad_port_rejected(self):
        for bad in ("0", "70000", "abc"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                share.main(["--port", bad, "--no-browser"])

    def test_bad_name_rejected(self):
        for bad in ("Screen!", "-x", "a" * 41, "my.name", "", "Porthole"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                share.main(["--name", bad, "--no-browser"])

    def test_name_check(self):
        for good in ("porthole", "office-2", "a", "9" * 40):
            self.assertEqual(share._name(good), good)
        for bad in ("porthole\n", "porthole\r\n", "\nporthole", "a" * 40 + "\n", "Screen!", ""):
            with self.assertRaises(share.argparse.ArgumentTypeError, msg=repr(bad)):
                share._name(bad)

    def test_bad_host_rejected(self):
        for bad in ("mypc.local", "::1", "1.2.3"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                share.main(["--host", bad, "--no-browser"])

    def test_already_running_returns_0(self):
        server = make_server(Hub(), 0, WEB, [])
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = share.main(["--port", str(server.server_address[1]), "--no-browser"])
        self.assertEqual(code, 0)
        self.assertIn("Already running", out.getvalue())

    def test_port_in_use_returns_1(self):
        with socket.socket() as held:
            held.bind(("0.0.0.0", 0))
            held.listen()
            port = held.getsockname()[1]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = share.main(["--port", str(port), "--no-browser"])
        self.assertEqual(code, 1)
        # "py start.py" on Windows, "python3 start.py" elsewhere (test_run_hint checks both).
        self.assertIn(f"Port {port} is already in use. Try: {share.run_hint()} --port {port + 1}", out.getvalue())


class KeepAliveTest(unittest.TestCase):
    """Event streams get keep-alive probes, so a phone that vanished (no goodbye) leaves within about half a
    minute: after 10 s of quiet, a probe every 5 s, three missed probes."""

    class Sock:
        def __init__(self):
            self.options = []

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

    def tune(self, **names):
        fake = types.SimpleNamespace(SOL_SOCKET=1, SO_KEEPALIVE=8, IPPROTO_TCP=6, **names)
        sock = self.Sock()
        with mock.patch.object(webserver, "socket", fake):
            webserver._tune_stream_socket(sock)
        return sock.options

    def test_linux(self):
        options = self.tune(TCP_KEEPIDLE=4, TCP_KEEPINTVL=5, TCP_KEEPCNT=6, TCP_USER_TIMEOUT=18)
        self.assertEqual(options, [(1, 8, 1), (6, 4, 10), (6, 5, 5), (6, 6, 3), (6, 18, 20000)])

    def test_macos_calls_the_quiet_time_tcp_keepalive(self):
        # macOS has no TCP_KEEPIDLE: without TCP_KEEPALIVE the first probe would wait two hours.
        options = self.tune(TCP_KEEPALIVE=0x10, TCP_KEEPINTVL=0x101, TCP_KEEPCNT=0x102)
        self.assertEqual(options, [(1, 8, 1), (6, 0x101, 5), (6, 0x102, 3), (6, 0x10, 10)])

    def test_a_real_socket(self):
        with socket.socket() as sock:
            webserver._tune_stream_socket(sock)
            self.assertTrue(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
            idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
            if idle is not None and os.name != "nt":
                self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, idle), 10)


class FakeServer:
    def __init__(self, interrupt=False, on_serve=None):
        self.interrupt = interrupt
        self.on_serve = on_serve  # runs while "serving", e.g. to wait for the address watcher
        self.served = False
        self.shut_down = False
        self.closed = False

    def serve_forever(self, *args, **kwargs):
        self.served = True
        if self.on_serve:
            self.on_serve()
        if self.interrupt:
            raise KeyboardInterrupt

    def shutdown(self):
        self.shut_down = True

    def server_close(self):
        self.closed = True


class SpyDiscovery(Discovery):
    def __init__(self, name, ip, port, **kwargs):
        super().__init__(name, ip, port, announcer_factory=lambda n, a: FakeAnnouncer(n, a))
        self.args = (name, ip, port)
        self.enable_calls = []
        self.stopped = False

    def set_enabled(self, on):
        self.enable_calls.append(on)
        return super().set_enabled(on)

    def stop(self):
        self.stopped = True
        super().stop()


class AppRunMixin:
    """Runs app.main with a fake server, discovery and port 80 helper (also used by test_app)."""

    def run_main(
        self,
        args=(),
        platform="linux",
        interrupt=False,
        helper_error=None,
        addresses=("192.168.1.20", "10.0.0.2"),
        on_serve=None,
        no_browser=True,
        stdout="capture",
    ):
        """`stdout`: "capture" keeps the output in self.output; None runs with no console at all (a windowed
        app has no sys.stdout or sys.stderr); a stream writes there."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server = FakeServer(interrupt=interrupt, on_serve=on_serve)
        self.helper = FakeServer()
        self.discoveries = []
        self.server_args = ()
        self.server_kwargs = {}
        self.helper_args = []

        def fake_make_server(*a, **kwargs):
            self.server_args = a
            self.server_kwargs = kwargs
            return self.server

        def fake_discovery(*a, **kwargs):
            discovery = SpyDiscovery(*a, **kwargs)
            self.discoveries.append(discovery)
            return discovery

        def fake_redirect(*a):
            self.helper_args.append(a)
            if helper_error:
                raise helper_error
            return self.helper

        out = io.StringIO() if stdout == "capture" else stdout
        with (
            mock.patch.object(share, "make_server", fake_make_server),
            mock.patch.object(share, "make_redirect_server", fake_redirect),
            mock.patch.object(share, "Discovery", fake_discovery),
            mock.patch.object(share, "lan_addresses", lambda: list(addresses)),
            mock.patch.object(share.sys, "platform", platform),
            mock.patch.object(share.time, "sleep", lambda s: None),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(None) if stdout is None else contextlib.nullcontext(),
        ):
            flags = ["--no-browser"] if no_browser else []
            code = share.main([*flags, "--port", "8765", "--record-dir", tmp.name, *args])
        self.output = out.getvalue() if isinstance(out, io.StringIO) else ""
        return code


class AppDiscoveryTest(AppRunMixin, unittest.TestCase):
    """How app.main wires the easy address: built, never switched on at start, stopped on exit."""

    def test_discovery_is_built_but_never_switched_on_at_start(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.discoveries), 1)
        discovery = self.discoveries[0]
        self.assertEqual(discovery.args, ("porthole", "192.168.1.20", 8765))
        self.assertIs(self.server_kwargs.get("discovery"), discovery)
        self.assertNotIn(True, discovery.enable_calls)
        self.assertTrue(discovery.stopped)
        self.assertIsNone(discovery.blocked_reason)

    def test_name_and_host_options(self):
        self.run_main(["--name", "office-2", "--host", "10.9.9.9"])
        self.assertEqual(self.discoveries[0].args, ("office-2", "10.9.9.9", 8765))

    def test_name_with_a_line_break_is_refused(self):
        # `$` in a pattern also matches before a final "\n"; no browser could ever type that name.
        for bad in ("porthole\n", "porthole\r\n", "\nporthole", "port\nhole"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=repr(bad)):
                self.run_main(["--name", bad])
            self.assertEqual(self.discoveries, [], repr(bad))  # never announced

    def test_no_address_means_no_ip(self):
        self.run_main(addresses=())
        self.assertIsNone(self.discoveries[0].args[1])

    def test_no_discovery_blocks_the_switch(self):
        self.run_main(["--no-discovery"])
        discovery = self.discoveries[0]
        self.assertTrue(discovery.blocked_reason)
        self.assertFalse(discovery.set_enabled(True)["enabled"])

    def test_no_discovery_skips_the_port_80_helper(self):
        # It only ever redirects while the easy address is on, which --no-discovery rules out.
        self.assertEqual(self.run_main(["--no-discovery"], platform="win32"), 0)
        self.assertEqual(self.helper_args, [])
        self.assertIsNone(self.discoveries[0].short_port)

    def test_port_80_helper_only_on_windows_and_mac(self):
        for platform in ("linux", "freebsd14"):
            self.run_main(platform=platform)
            self.assertEqual(self.helper_args, [], platform)
            self.assertIsNone(self.discoveries[0].short_port)
        for platform in ("win32", "darwin"):
            self.run_main(platform=platform)
            discovery = self.discoveries[0]
            self.assertEqual(self.helper_args, [(80, 8765, discovery)], platform)
            self.assertEqual(discovery.short_port, 80)
            self.assertTrue(self.helper.shut_down and self.helper.closed, platform)

    def test_port_80_helper_bind_errors_are_ignored(self):
        code = self.run_main(platform="win32", helper_error=PermissionError(13, "denied"))
        self.assertEqual(code, 0)
        self.assertIsNone(self.discoveries[0].short_port)
        self.assertTrue(self.server.served)

    def test_a_mac_that_refuses_port_80_carries_on(self):
        # macOS lets ordinary users listen on port 80 on all addresses (since 10.14); if it says no, plain
        # porthole.local just isn't offered.
        code = self.run_main(platform="darwin", helper_error=PermissionError(13, "Permission denied"))
        self.assertEqual(code, 0)
        self.assertEqual(self.helper_args, [(80, 8765, self.discoveries[0])])
        self.assertIsNone(self.discoveries[0].short_port)
        self.assertTrue(self.server.served)

    def test_ctrl_c_stops_discovery_and_helper(self):
        self.assertEqual(self.run_main(platform="darwin", interrupt=True), 0)
        self.assertTrue(self.discoveries[0].stopped)
        self.assertTrue(self.helper.shut_down and self.helper.closed)
        self.assertTrue(self.server.closed)

    def test_verbose_is_off_unless_asked(self):
        self.run_main()
        self.assertIs(self.server_kwargs.get("verbose"), False)
        self.run_main(["--verbose"])
        self.assertIs(self.server_kwargs.get("verbose"), True)

    def test_terminal_output_is_unchanged(self):
        self.run_main(platform="win32")
        self.assertNotIn(".local", self.output)
        self.assertIn("Porthole is running.", self.output)


class FakeClock:
    """time.monotonic and time.sleep for AcceptGuard: sleeping moves the clock on."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class QuietTerminalTest(unittest.TestCase):
    """A phone dropping a connection, or a browser giving up on a reply, is normal on a LAN: it must not print a
    traceback, which looks as if Porthole crashed. A request line that can't be read gets a plain 400."""

    start = ServerTest.start
    request = ServerTest.request

    def raw(self, data, reset=False):
        """Send raw bytes; what came back. `reset` drops the connection hard instead of reading the reply."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(data)
            if reset:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return b""
            reply = b""
            while chunk := sock.recv(65536):
                reply += chunk
            return reply
        finally:
            sock.close()

    def quietly(self, action):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action()
            time.sleep(0.3)  # the handler meets the reset on its own thread
            resp, _ = self.request("GET", "/api/ping")  # and the server is still there
            self.assertEqual(resp.status, 200)
        return err.getvalue()

    def test_connections_dropped_mid_request_print_nothing(self):
        self.start()
        for data in (b"", b"GET /static/vi", b"POST /signal?room=room1&id=x HTTP/1.1\r\nContent-Length: 100\r\n\r\n{"):
            with self.subTest(data=data):
                self.assertEqual(self.quietly(lambda: self.raw(data, reset=True)), "")

    def test_an_unreadable_address_in_the_request_line_gets_a_400(self):
        self.start()
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                request = f"{method} http://[::1/x HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Length: 0\r\n\r\n"
                reply = []
                self.assertEqual(self.quietly(lambda: reply.append(self.raw(request.encode()))), "")
                self.assertRegex(reply[0].split(b"\r\n", 1)[0], rb"^HTTP/1\.[01] 400 ")

    def test_the_port_80_helper_answers_one_with_a_400_too(self):
        discovery = Discovery("porthole", None, 8765)
        helper = make_redirect_server(0, 8765, discovery)
        threading.Thread(target=helper.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(helper.server_close)
        self.addCleanup(helper.shutdown)
        self.port = helper.server_address[1]
        reply = self.raw(f"GET http://[::1/x HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
        self.assertRegex(reply.split(b"\r\n", 1)[0], rb"^HTTP/1\.[01] 400 ")


class AcceptGuardTest(unittest.TestCase):
    """A listening socket stays readable while accept() fails (a snap removed under a running Porthole): without a
    pause between tries the server loop spins the CPU. Failures in a row pause longer each time; failures that go
    on without a break for ACCEPT_STOP_SECONDS stop the server; passing ones never do."""

    def setUp(self):
        self.clock = FakeClock()
        self.guard = webserver.AcceptGuard(clock=self.clock.monotonic, sleep=self.clock.sleep)

    def fail(self, code=errno.EACCES):
        self.guard.failed(OSError(code, os.strerror(code)))

    def fail_for(self, seconds, code=errno.EACCES):
        """Fail again and again, as when the socket stays readable, until `seconds` have passed."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.fail(code)

    def test_each_failure_in_a_row_pauses_longer_up_to_a_limit(self):
        for _ in range(10):
            self.fail()
        first = webserver.ACCEPT_PAUSE_FIRST
        self.assertEqual(self.clock.sleeps[:4], [first, first * 2, first * 4, first * 8])
        self.assertEqual(max(self.clock.sleeps), webserver.ACCEPT_PAUSE_MAX)
        self.assertEqual(self.clock.sleeps[-1], webserver.ACCEPT_PAUSE_MAX)

    def test_failing_without_a_break_stops_the_server(self):
        self.fail_for(webserver.ACCEPT_STOP_SECONDS - 0.6)  # not yet
        with self.assertRaises(webserver.AcceptFailed):
            self.fail_for(2)
        self.assertGreaterEqual(self.clock.now - 1000.0, webserver.ACCEPT_STOP_SECONDS)
        self.assertLess(len(self.clock.sleeps), 40)  # a few tries a second, not a spin

    def test_a_success_starts_again_from_the_shortest_pause(self):
        self.fail_for(2, errno.EMFILE)  # out of file handles for a moment
        self.guard.succeeded()
        self.clock.sleeps.clear()
        self.fail()
        self.assertEqual(self.clock.sleeps, [webserver.ACCEPT_PAUSE_FIRST])

    def test_a_brief_shortage_of_file_handles_never_stops_it(self):
        for _ in range(5):
            self.fail_for(webserver.ACCEPT_STOP_SECONDS - 1, errno.EMFILE)
            self.guard.succeeded()  # a connection got through: the stretch ends
        self.fail_for(webserver.ACCEPT_STOP_SECONDS - 1, errno.EMFILE)  # nothing raised

    def test_running_out_of_file_handles_or_memory_never_stops_it(self):
        # A device on the network can hold enough connections open to use up the file handles for as long as it
        # likes; the share must come back once they close, not stop for good. 10024 and 10055: Windows' WSAEMFILE
        # and WSAENOBUFS.
        for code in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM, 10024, 10055):
            with self.subTest(code=errno.errorcode.get(code, code)):
                self.fail_for(webserver.ACCEPT_STOP_SECONDS * 4, code)  # nothing raised
                self.assertLessEqual(max(self.clock.sleeps), webserver.ACCEPT_PAUSE_MAX)  # but no spinning either
                self.guard.succeeded()

    def test_errors_about_one_connection_never_stop_it(self):
        # A phone that gave up while its connection waited: the next accept() takes the next one.
        for code in (errno.ECONNABORTED, errno.ECONNRESET, errno.EPROTO):
            with self.subTest(code=errno.errorcode[code]):
                self.fail_for(webserver.ACCEPT_STOP_SECONDS * 4, code)  # nothing raised
                self.assertLessEqual(max(self.clock.sleeps), webserver.ACCEPT_PAUSE_MAX)  # but no spinning either
                self.guard.succeeded()

    def test_failures_apart_from_each_other_dont_add_up(self):
        # Nothing waiting in between (the socket wasn't readable): those aren't failures in a row.
        for _ in range(10):
            self.fail()
            self.clock.now += webserver.ACCEPT_GAP_SECONDS + 1
        self.assertEqual(self.clock.sleeps, [webserver.ACCEPT_PAUSE_FIRST] * 10)


class AcceptStopsServerTest(unittest.TestCase):
    """A real server whose accept() always fails, with a connection waiting so its socket stays readable."""

    class FailingSocket:
        def __init__(self, real):
            self.real = real
            self.accepts = 0

        def fileno(self):
            return self.real.fileno()

        def accept(self):
            self.accepts += 1
            raise PermissionError(errno.EACCES, "Permission denied")

        def close(self):
            self.real.close()

    def test_serve_forever_backs_off_then_raises_accept_failed(self):
        server = make_server(Hub(), 0, WEB, [])
        self.addCleanup(server.server_close)
        failing = self.FailingSocket(server.socket)
        server.socket = failing
        client = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        self.addCleanup(client.close)
        raised = []

        def serve():
            try:
                server.serve_forever(poll_interval=0.05)
            except Exception as err:  # noqa: BLE001 - what the test looks at
                raised.append(err)

        with mock.patch.object(webserver, "ACCEPT_STOP_SECONDS", 1.0):
            began = time.monotonic()
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual([type(err) for err in raised], [webserver.AcceptFailed])
        self.assertGreaterEqual(time.monotonic() - began, 1.0)
        self.assertLess(failing.accepts, 40)  # a busy loop would have made many thousands of tries
        server.shutdown()  # serve_forever has ended, so this returns at once (no deadlock)

    def test_a_good_connection_after_a_failure_is_served(self):
        server = make_server(Hub(), 0, WEB, [])
        self.addCleanup(server.server_close)
        real_accept = server.socket.accept
        calls = []

        class Flaky:
            def fileno(self):
                return server_socket.fileno()

            def accept(self):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError(errno.EACCES, "Permission denied")  # one that counts toward stopping
                return real_accept()

            def close(self):
                server_socket.close()

        server_socket = server.socket
        server.socket = Flaky()
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{server.server_address[1]}"})
        resp = conn.getresponse()
        self.assertEqual(json.loads(resp.read())["app"], "porthole")
        conn.close()
        self.assertGreaterEqual(len(calls), 2)
        # The connection that got through ends the stretch of failures, so a later one doesn't add up with it.
        self.assertIsNone(server.accept_guard._stuck_since)
        self.assertEqual(server.accept_guard._pause, webserver.ACCEPT_PAUSE_FIRST)

    def test_the_port_80_helper_lets_go_quietly(self):
        # Its thread must end without a traceback in the terminal, and give up port 80.
        class Stuck:
            closed = False

            def serve_forever(self):
                raise webserver.AcceptFailed("Permission denied")

            def server_close(self):
                self.closed = True

        stuck = Stuck()
        discovery = Discovery("porthole", None, 8765)
        discovery.short_port = 80
        hooks = []
        with mock.patch.object(threading, "excepthook", lambda args: hooks.append(args)):
            thread = threading.Thread(target=share._serve_helper, args=(stuck, discovery), daemon=True)
            thread.start()
            thread.join(5)
        self.assertEqual(hooks, [])
        self.assertTrue(stuck.closed)
        # The page stops offering the plain `<name>.local` address, which now only gets "connection refused".
        self.assertIsNone(discovery.short_port)
        self.assertIsNone(discovery.state()["short_url"])


if __name__ == "__main__":
    unittest.main()
