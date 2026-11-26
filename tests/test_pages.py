import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def body_of(script, start):
    """The text of a top-level function (or block) that begins with `start`, up to its closing brace."""
    text = script[script.index(start) :]
    return text[: text.index("\n}\n")]


class PagesTest(unittest.TestCase):
    def test_view_video_attributes(self):
        tag = re.search(r"<video[^>]*>", read("view.html")).group(0)
        for attribute in ("autoplay", "muted", "playsinline"):
            self.assertIn(attribute, tag)

    def test_pages_use_only_local_assets(self):
        for name in ("share.html", "view.html"):
            self.assertNotRegex(read(name), r'(src|href)="(https?:)?//', name)

    def test_no_inline_scripts_or_styles(self):  # the CSP blocks them
        for name in ("share.html", "view.html"):
            text = read(name)
            self.assertNotRegex(text, r"<script(?![^>]*\bsrc=)[^>]*>", name)
            self.assertNotIn("style=", text, name)
            self.assertNotIn("<style", text, name)

    def test_pages_have_viewport_and_module_script(self):
        for name, script in (("share.html", "/static/share.js"), ("view.html", "/static/view.js")):
            text = read(name)
            self.assertIn('name="viewport"', text, name)
            self.assertIn(f'<script type="module" src="{script}"></script>', text, name)

    def test_share_page_buttons(self):
        text = read("share.html")
        for label in (">Start sharing<", ">Pause<", ">Change<", ">Record<", ">Stop sharing<", ">Quit<"):
            self.assertIn(label, text)
        for gone in ("Change what's shared", ">Stop<", "Quit app"):
            self.assertNotIn(gone, text)

    def test_share_page_v2_controls(self):
        text = read("share.html")
        for label in ("Ask before someone watches", "Smooth motion", "Open folder"):
            self.assertIn(label, text)
        for element in ('id="ask"', 'id="smooth"', 'id="record-status"', 'rel="icon"'):
            self.assertIn(element, text)

    def test_share_page_easy_address(self):
        text = read("share.html")
        self.assertIn("Easy address", text)
        for element in ('type="checkbox" id="easy"', 'id="easy-address"', 'id="easy-error"'):
            self.assertIn(element, text)
        script = read("share.js")
        for call in ("/api/discovery", "/api/status", "from '/static/settings.js'"):
            self.assertIn(call, script)

    def test_view_page_v2_controls(self):
        text = read("view.html")
        for element in ('id="quality"', 'id="fit"', 'id="weak"', 'rel="icon"'):
            self.assertIn(element, text)
        self.assertNotIn("video.addEventListener('click'", read("view.js"))

    def test_peer_connections_use_no_ice_servers(self):
        self.assertIn("iceServers: []", read("signal.js"))
        for name in ("share.js", "view.js"):
            self.assertIn("createPeer(", read(name), name)
            self.assertNotIn("new RTCPeerConnection", read(name), name)

    # --- version 4 fix list ---

    def test_recording_saves_key_frames_every_2_seconds(self):
        options = re.search(r"new MediaRecorder\(stream, \{([^}]*)\}", read("share.js")).group(1)
        self.assertIn("videoKeyFrameIntervalDuration: 2000", options)

    def test_sharer_page_follows_address_changes(self):
        script = read("share.js")
        self.assertRegex(script, r"setInterval\(refreshInfo, 10000\)")
        self.assertIn("function drawJoin(", script)

    def test_viewer_waiting_screen_shows_the_check_code(self):
        self.assertIn('id="code"', read("view.html"))
        self.assertIn("`Your code: ${", read("view.js"))

    def test_viewer_explains_refusals_and_stops(self):
        script = read("view.js")
        self.assertIn("case 'refused':", script)
        self.assertIn("Too many Porthole tabs are open on this device. Close one and try again.", script)
        self.assertIn("Too many tries. Wait a minute, then try again.", script)

    def test_viewer_keeps_its_id_for_a_reload_and_forgets_it_when_turned_away(self):
        # A reload names the connection before, so Porthole keeps the device's row (see test_hub.ReconnectTest); a page
        # the sharer denied or removed starts afresh on Try again. Not on every ending: a page being reloaded hears its
        # stream close too, and must keep the id for the new page.
        script = read("view.js")
        self.assertIn("previous: keptViewerId(kept, room)", script)
        messages = body_of(script, "function onMessage(")
        self.assertIn("keepViewerId(kept, room, msg.id);", messages)
        turned_away = messages[messages.index("case 'denied':") :]
        turned_away = turned_away[: turned_away.index("break;")]
        self.assertIn("case 'removed':", turned_away)
        self.assertIn("keepViewerId(kept, room, '');", turned_away)
        self.assertNotIn("keepViewerId", body_of(script, "function leave("))
        self.assertNotIn("keepViewerId", body_of(script, "function onStatus("))
        self.assertIn("window.sessionStorage", script)  # this tab only
        self.assertNotIn("localStorage.setItem('porthole-viewer", script)

    def test_sharer_hands_a_row_to_the_device_s_new_connection(self):
        script = read("share.js")
        joined = script[script.index("case 'viewer-joined':") :]
        joined = joined[: joined.index("break;")]
        self.assertIn("msg.replaces", joined)
        self.assertIn("replaceViewer(", joined)
        replace = body_of(script, "function replaceViewer(")
        for part in ("closeLink(old);", "renameKey(viewers, old.id, msg.id, viewer);", "old.device", "old.number", "announcedWaiting", "admit(viewer);"):
            self.assertIn(part, replace)

    def test_viewer_retries_its_answer_once(self):
        script = read("view.js")
        self.assertIn("retryOnce(", script)
        self.assertIn("link === peer", script)

    def test_sharer_rows_and_waiting_notification(self):
        text = read("share.html")
        self.assertRegex(text, r'<button id="deny-all"[^>]*hidden[^>]*>Deny all</button>')
        script = read("share.js")
        self.assertIn("from '/static/share-helpers.js'", script)
        self.assertIn("tag: 'porthole-waiting'", script)
        self.assertNotIn("tag: viewer.id", script)  # one notification, not one per device
        for call in ("rowName(", "rowDetails(", "nextNumber(", "waitingNoteStep("):
            self.assertIn(call, script)

    def test_sharer_rows_use_the_servers_join_time(self):
        # A reload rebuilds the rows from the hello list; the time must stay when the viewer joined.
        script = read("share.js")
        self.assertIn("joinTime(", script)
        self.assertIn("addViewer(v.id, v.device, v.client, v.code, v.joined, v.address)", script)
        self.assertIn("addViewer(msg.id, msg.device, msg.client, msg.code, msg.joined, msg.address)", script)
        self.assertNotIn("joined: Date.now()", script)

    def test_row_buttons_have_ids_from_the_check_code(self):
        script = read("share.js")
        for action in ("allow", "deny", "remove"):
            self.assertIn(f"'{action}'", script)
        self.assertIn("`${action}-${viewer.code}`", script)

    def test_whole_screen_warning(self):
        text = read("share.html")
        self.assertRegex(
            text,
            r'id="monitor-hint"[^>]*hidden[^>]*>Viewers can see your notifications\. '
            r"Turn on Do not disturb, or share a window instead\.<",
        )
        self.assertIn("isWholeScreen(", read("share.js"))

    def test_sharer_stream_has_no_silence_check(self):
        # Reopening the sharer's own stream would race its old connection and look like "another tab".
        self.assertIn("silenceMs: 0", read("share.js"))
        self.assertNotIn("silenceMs", read("view.js"))  # viewers use the 40 s default


    # --- version 4, P4: redesign ---

    def test_no_shadows_or_gradients(self):
        css = read("style.css")
        self.assertNotIn("box-shadow", css)
        self.assertNotIn("text-shadow", css)
        self.assertNotIn("gradient", css)

    def test_no_decorative_glyphs(self):
        for name in ("share.html", "view.html", "share.js", "view.js", "share-helpers.js"):
            text = read(name)
            for glyph in ("\u25cf", "\u2713", "\u2192", "\u2022", "\u2714", "\u2190", "\u2191", "\u2193"):
                self.assertNotIn(glyph, text, f"{name}: {glyph!r}")
            self.assertNotRegex(text, "[\U0001F300-\U0001FAFF\u2600-\u27BF]", name)  # emoji

    def test_no_tech_words_on_the_pages(self):
        for name in ("share.html", "view.html", "manifest.json"):
            for word in ("share.py", "server", "WebRTC", "SSE", "mDNS", "localhost"):
                self.assertNotRegex(read(name), rf"(?i)\b{re.escape(word)}\b", f"{name}: {word}")

    def test_no_all_caps_text(self):
        css = read("style.css")
        self.assertNotIn("uppercase", css)
        for name in ("share.html", "view.html"):
            texts = re.findall(r">([^<>]+)<", read(name))
            for text in texts:
                for word in re.findall(r"[A-Za-z]{3,}", text):
                    self.assertFalse(word.isupper(), f"{name}: {text!r}")

    def test_font_weights_are_400_and_600_only(self):
        weights = set(re.findall(r"font-weight:\s*([^;]+);", read("style.css")))
        self.assertLessEqual(weights, {"400", "600"}, weights)
        self.assertNotIn("bold", read("style.css"))
        for name in ("share.html", "view.html"):
            self.assertNotRegex(read(name), r"<b>|<strong>", name)

    def test_times_and_sizes_use_tabular_numbers(self):
        css = read("style.css")
        # The recording length and size, join times and codes, the refusal time, the easy address.
        for selector in (".record-line {", ".viewer-meta {", ".code {", ".line {", ".easy-host {"):
            block = css[css.index(selector) : css.index("}", css.index(selector))]
            self.assertIn("font-variant-numeric: tabular-nums", block, selector)

    def test_preview_shows_what_viewers_see_while_paused(self):
        script = read("share.js")
        self.assertIn("function showPreview(", script)
        self.assertRegex(script, r"srcObject = paused \? paused\.preview : stream")

    def test_one_column_680_wide(self):
        css = read("style.css")
        self.assertIn("max-width: 680px", css)
        self.assertNotIn('class="card', read("share.html"))

    def test_theme_tokens_for_light_dark_and_auto(self):
        css = read("style.css")
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertRegex(css, r'@media \(prefers-color-scheme: dark\) \{\s*:root\[data-theme="auto"\]')
        for name in ("share.html", "view.html"):
            self.assertRegex(read(name), r'<html lang="en" data-theme="auto">', name)
        self.assertRegex(css, r":focus-visible\s*\{[^}]*outline:")

    def test_theme_switch_on_the_sharer_page(self):
        text = read("share.html")
        for choice, label in (("auto", "Auto"), ("light", "Light"), ("dark", "Dark")):
            self.assertRegex(
                text,
                rf'<input type="radio" name="theme" id="theme-{choice}" value="{choice}"[^>]*>\s*<span>{label}</span>',
            )
        self.assertRegex(text, r'role="radiogroup" aria-label="Theme"')
        script = read("share.js")
        self.assertIn("from '/static/theme.js'", script)
        theme = read("theme.js")
        self.assertIn("'porthole-theme'", theme)
        self.assertIn("data-theme", theme)

    def test_sharer_header_status_and_sections(self):
        text = read("share.html")
        self.assertRegex(text, r'<span class="mark" aria-hidden="true"></span>\s*Porthole')
        self.assertRegex(text, r'<button id="quit" class="text" type="button">Quit</button>')
        self.assertRegex(text, r'id="status-line"[^>]*aria-live="polite"')
        self.assertRegex(text, r'<span id="state">Not sharing</span>')
        for heading in (">Join<", ">Watching<", ">Settings<"):
            self.assertIn(heading, text)
        # Order: header, status, main action, waiting viewers, join, watching, settings.
        order = [text.index(marker) for marker in ('id="quit"', 'id="status-line"', 'id="start"', 'id="waiting"', ">Join<", ">Watching<", ">Settings<")]
        self.assertEqual(order, sorted(order))

    def test_stop_sharing_is_not_red(self):
        self.assertRegex(read("share.html"), r'<button id="stop" type="button"( class="[^"]*")?[^>]*>Stop sharing</button>')
        self.assertNotRegex(read("share.html"), r'id="stop"[^>]*class="[^"]*danger')
        self.assertNotIn(".danger", read("style.css"))

    def test_normal_endings_are_notices_not_errors(self):
        script = read("share.js")
        self.assertIn("showNotice('Porthole has stopped. You can close this page.')", script)
        self.assertIn("showNotice('Sharing moved to another window or tab. You can close this one.')", script)
        self.assertIn('id="notice"', read("share.html"))

    def test_sharer_wording_fits_a_window_and_a_tab(self):
        # The page runs in Porthole's own window (no tabs) or in a browser tab.
        text, script = read("share.html"), read("share.js")
        self.assertIn('<p id="busy-text">Sharing is on in another window or tab.</p>', text)
        self.assertRegex(text, r'<button id="takeover" class="primary" type="button">Use this one</button>')
        for page, name in ((text, "share.html"), (script, "share.js")):
            for words in ("close this tab", "another tab", "Use this tab", "Moved to another tab"):
                self.assertNotIn(words, page, f"{name}: {words}")
        self.assertIn("endPage('Moved')", script)

    def test_open_in_my_browser_in_porthole_s_own_window(self):
        # The app window has its own browser profile, so the browser's tabs can't be picked there.
        text, script = read("share.html"), read("share.js")
        self.assertRegex(
            text,
            r'<p id="browser-hint" class="line" hidden>To share a browser tab or its sound, open Porthole in your browser\. '
            r'<button id="open-browser" class="text small" type="button">Open in my browser</button></p>',
        )
        self.assertLess(text.index('id="start"'), text.index('id="browser-hint"'))
        self.assertLess(text.index('id="browser-hint"'), text.index('id="controls"'))
        self.assertIn("inAppWindow(location.search)", script)
        self.assertIn("browserHintShown({ appWindow, sharing, inactive })", script)
        self.assertIn("$('audio-hint').textContent = audioHint(appWindow, navigator.userAgent);", script)
        self.assertIn("$('open-browser').addEventListener('click', openInBrowser)", script)
        self.assertIn("act('/api/open-in-browser')", script)
        self.assertIn("showNotice('Porthole is open in your browser. You can close this window.')", script)

    def test_porthole_s_own_window_closes_when_porthole_stops(self):
        script = read("share.js")
        self.assertIn("if (appWindow) window.close();", script)
        quit_body = script[script.index("async function quit()") : script.index("// ---------- messages from the server")]
        self.assertIn("closeOwnWindow();", quit_body)
        ended = script[script.index("case 'ended':") : script.index("case 'replaced':")]
        self.assertIn("endStopped();", ended)
        self.assertIn("closeOwnWindow();", body_of(script, "function endStopped()"))
        replaced = script[script.index("case 'replaced':") : script.index("render();\n}", script.index("case 'replaced':"))]
        self.assertNotIn("closeOwnWindow", replaced)  # the note says where sharing went
        # ...and the app window's page lets go of the window, so closing it, as the note says, doesn't stop Porthole.
        self.assertIn("if (appWindow && !msg.window) api('/api/release-window')", replaced)
        opened = script[script.index("async function openInBrowser()") :]
        opened = opened[: opened.index("\n}\n")]
        self.assertIn("window.close();", opened)

    def test_quit_says_its_note_once_when_ended_got_there_first(self):
        # /api/quit queues "ended" on this page's own stream before it answers: whichever arrives first ends the
        # page, and the other must not say "Porthole has stopped" again.
        script = read("share.js")
        quit_body = script[script.index("async function quit()") : script.index("// ---------- messages from the server")]
        after_reply = quit_body[quit_body.index("if (!result.ok)") :]
        self.assertLess(after_reply.index("if (inactive) return;"), after_reply.index("showNotice("))

    def test_use_this_one_reads_the_approvals_and_switches_again(self):
        # The page may have waited on "Sharing is on in another window or tab" for minutes while the other page
        # allowed, denied and removed devices and changed the switches: taking over starts from those, as Porthole
        # keeps them (the other page may be in another browser), else as this browser saved them.
        script = read("share.js")
        connect = body_of(script, "async function connect(takeover)")
        self.assertIn("await readChoicesAgain();", connect)
        self.assertLess(connect.index("await readChoicesAgain();"), connect.index("new Signal("))
        self.assertIn("if (inactive) return;", connect[connect.index("await readChoicesAgain();") :])
        reread = body_of(script, "async function readChoicesAgain()")
        for line in ("const info = await readInfo();", "useChoices(info.choices)", "settings = loadSettings(readSavedSettings());",
                     "approved = loadSet('porthole-approved');", "denied = loadSet('porthole-denied');", "render();"):
            self.assertIn(line, reread)
        self.assertLess(reread.index("useChoices(info.choices)"), reread.index("loadSettings(readSavedSettings())"))

    def test_start_sharing_asks_for_notifications_while_ask_is_on(self):
        # Ask is on from the start, so "2 waiting" never showed while the browser was asked only when Ask was turned on
        # by hand. Start sharing asks too: right after the screen picker opens (both need the click), only while the
        # browser has no answer, and once per page (askToNotify, tested in tests/js).
        script = read("share.js")
        start = re.search(r"\nasync function start\(\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("const picking = pickStream();", start)
        self.assertLess(start.index("const picking = pickStream();"), start.index("askForNotifications();"))
        self.assertLess(start.index("askForNotifications();"), start.index("next = await picking;"))
        ask = body_of(script, "function askForNotifications()")
        self.assertIn("askToNotify({ ask: settings.ask, permission, askedBefore: notifyAsked })", ask)
        self.assertLess(ask.index("notifyAsked = true;"), ask.index("Notification.requestPermission()"))
        set_ask = body_of(script, "function setAsk(on)")
        self.assertIn("askForNotifications();", set_ask)
        self.assertEqual(script.count("Notification.requestPermission()"), 1)

    def test_no_network_says_the_link_comes_back_by_itself(self):
        # A restart isn't needed (the link comes back with the network) and would make a new link.
        text = read("share.html")
        self.assertIn('<p id="no-network" class="error" hidden>This PC has no network. Connect it to Wi-Fi or a network cable; '
                      "the link shows here once it's connected.</p>", text)
        self.assertNotIn("restart Porthole", text)
        join = body_of(read("share.js"), "function drawJoin(info)")
        self.assertIn("$('join').hidden = !first;", join)
        self.assertIn("$('no-network').hidden = Boolean(first);", join)

    def test_the_waiting_page_says_whether_the_other_page_shares(self):
        # Only open elsewhere: "Porthole is open in another window or tab." Sharing: "Sharing is on in ...".
        script = read("share.js")
        busy = body_of(script, "function showBusy(sharing)")
        self.assertIn("setText($('busy-text'), busyText(sharing));", busy)
        # The status line says it too ("Sharing in another window or tab"), and is read out by itself: saying the
        # waiting line in the live region as well would be read twice.
        self.assertNotIn("announce(", busy)
        self.assertIn("showBusy(info.sharing);", body_of(script, "async function onStatus(status)"))
        init = body_of(script, "async function init()")
        self.assertIn("const slot = info.sharer_active ? await sharerSlotFrees() : { free: true };", init)
        self.assertIn("showBusy(slot.sharing);", init)
        # It follows the other page: sharing starts or stops there while this one waits. Once that page has gone (no
        # sharer), this one takes the free slot instead (see the test below).
        refresh = body_of(script, "async function refreshInfo()")
        self.assertIn("if (waiting) redrawBusy(info.sharing);", refresh)
        self.assertLess(refresh.index("connect(false);"), refresh.index("if (waiting) redrawBusy(info.sharing);"))
        redraw = body_of(script, "function redrawBusy(sharing)")
        self.assertIn("elsewhere = Boolean(sharing);", redraw)
        self.assertIn("render();", redraw)  # the status line follows; written only on a change, so said once
        self.assertNotIn("announce(", redraw)

    def test_the_waiting_page_says_in_its_status_line_that_another_page_shares(self):
        # It said "Not sharing" and "Watching: No one yet", so Use this one looked harmless though it cuts everyone
        # watching the other page. Now the status line says where sharing is, and there is no Watching list there.
        script, text = read("share.js"), read("share.html")
        self.assertIn('<section id="watching" class="section" aria-labelledby="viewers-title">', text)
        self.assertIn("let elsewhere = null;", script)
        render = body_of(script, "function render()")
        self.assertIn("statusText({ sharing, paused: Boolean(paused), recording: Boolean(recording), elsewhere })", render)
        self.assertIn("$('watching').hidden = elsewhere !== null;", render)
        busy = body_of(script, "function showBusy(sharing)")
        self.assertIn("elsewhere = Boolean(sharing);", busy)
        self.assertLess(busy.index("elsewhere = Boolean(sharing);"), busy.index("render();"))
        # Taking over, or ending, it is the page's own again.
        connect = body_of(script, "async function connect(takeover)")
        self.assertLess(connect.index("elsewhere = null;"), connect.index("render();"))
        self.assertIn("elsewhere = null;", body_of(script, "function endPage(state)"))
        # Its Watching section is hidden: a device turned away from the other page isn't news here.
        self.assertIn("if (refusalDrawn && elsewhere === null) announce(line);", body_of(script, "function drawRefusal(info)"))

    def test_porthole_keeps_this_run_s_approvals_and_switches(self):
        # After Open in my browser the page is in another browser with other saved settings: devices let in waited
        # again, denied ones were asked about, and the switches went back to the defaults (Easy address on). The
        # sharer page now reports each change to Porthole, and a page that takes over starts from Porthole's copy.
        script = read("share.js")
        keep = body_of(script, "function keepChoices()")
        self.assertIn("if (!signal || !signal.id || inactive || !controlsOn) return;", keep)
        self.assertIn("tellServer('/api/choices', { id: signal.id, settings: { ...settings }, approved: [...approved], denied: [...denied] });", keep)
        save = body_of(script, "function saveSettings(change)")
        self.assertIn("keepChoices();", save)
        # A switch changed on a page that took over is saved in its browser on its own, without the ones taken over.
        self.assertIn("localStorage.setItem('porthole-settings', JSON.stringify(change(loadSettings(readSavedSettings()))));", save)
        self.assertIn("saveSettings((saved) => withAsk(saved, on));", body_of(script, "function setAsk(on)"))
        self.assertIn("saveSettings((saved) => withEasy(saved, on));", body_of(script, "function setEasy(on)"))
        self.assertIn("saveSettings((saved) => ({ ...saved, smooth: on }));", script)
        self.assertNotIn("saveSettings();", script)
        self.assertIn("keepChoices();", body_of(script, "function saveSets()"))
        hello = script[script.index("case 'hello':") : script.index("case 'viewer-joined':")]
        self.assertIn("keepChoices();", hello)
        use = body_of(script, "function useChoices(choices)")
        for line in ("settings = loadSettings(JSON.stringify(choices.settings));", "approved = new Set(", "denied = new Set("):
            self.assertIn(line, use)
        self.assertNotIn("save", use)  # used for this run: the browser's own saved settings stay as they were
        init = body_of(script, "async function init()")
        self.assertIn("if (!useChoices(info.choices)) {", init)
        self.assertLess(init.index("useChoices(info.choices)"), init.index("drawJoin(info);"))
        # Open in my browser: what is still on its way to Porthole gets there before the browser tab reads it.
        opened = body_of(script, "async function openInBrowser()")
        self.assertLess(opened.index("await serverQueue;"), opened.index("act('/api/open-in-browser')"))

    def test_porthole_gone_ends_the_page(self):
        # Crashed or killed: Quit said "Couldn't quit Porthole: Failed to fetch", and Record and Open folder did
        # nothing. A request with no answer at all is checked (twice, a second apart); if Porthole is gone, the page
        # ends as when it stops, and says so.
        script = read("share.js")
        self.assertIn('const GONE = "Porthole isn\'t running any more. You can close this page.";', script)
        gone = body_of(script, "async function portholeGone()")
        self.assertIn("readInfo()", gone)
        self.assertIn("later(1000, readInfo)", gone)
        self.assertIn("endGone();", gone)
        end = body_of(script, "function endGone()")
        for line in ("if (inactive) return;", "stop();", "endPage('Stopped');", "showNotice(GONE);"):
            self.assertIn(line, end)
        act = body_of(script, "async function act(path, body = '')")
        self.assertIn("if (await portholeGone()) return null;", act)
        quit = body_of(script, "async function quit()")
        self.assertIn("const result = await act('/api/quit');", quit)
        self.assertNotIn("err.message", quit)
        self.assertIn("act('/api/recording/start'", body_of(script, "async function beginRecording()"))
        # Record is turned back on after it starts: not once the page has ended meanwhile.
        self.assertIn("$('record').disabled = inactive || !controlsOn;", body_of(script, "async function startRecording()"))
        # A recording's end with no answer: the page's note says Porthole has gone, with no "Failed to fetch" error.
        stop = body_of(script, "function stopRecording(")
        self.assertIn("problem = (await portholeGone()) ? '' : problem || `Couldn't finish the recording: ${err.message}`;", stop)
        self.assertIn("act('/api/open-folder')", body_of(script, "async function openFolder()"))
        self.assertIn("$('open-folder').addEventListener('click', openFolder);", script)
        # Lost contact: the page checks at once, and every 10 s anyway, instead of "Retrying..." for ever.
        status = body_of(script, "async function onStatus(status)")
        reconnecting = status[status.index("if (status === 'reconnecting')") : status.index("} else if (status === 'closed')")]
        self.assertIn("noticeGone();", reconnecting)
        refresh = body_of(script, "async function refreshInfo()")
        self.assertLess(refresh.index("if (waiting && answeringRun(info, room) === 'none') {"), refresh.index("endGone();"))

    def test_the_no_sound_note_fits_the_browser(self):
        script = read("share.js")
        self.assertIn("$('audio-hint').textContent = audioHint(appWindow, navigator.userAgent);", script)

    def test_a_browser_that_cant_share_keeps_saying_so(self):
        # Connecting turns the controls on and clears errors: Start sharing must stay off, and the note stay.
        script = read("share.js")
        self.assertIn("const canShare = testMode || Boolean(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);", script)
        enabled = script[script.index("function setControlsEnabled(on)") :]
        enabled = enabled[: enabled.index("\n}\n")]
        self.assertIn("(id === 'start' && !canShare)", enabled)
        status = script[script.index("async function onStatus(status)") :]
        status = status[: status.index("\n}\n")]
        self.assertIn("showError(canShare ? '' : CANT_SHARE);", status)

    def test_a_link_back_after_a_blip_offers_play_again_if_the_picture_is_paused(self):
        # "Tap to play" was the only way to start the picture; a blip hid it behind "Can't connect", and the link
        # coming back must not clear the screen to a still frame with nothing to tap.
        script = read("view.js")
        connected = script[script.index("if (state === 'connected') {") :]
        connected = connected[: connected.index("} else if")]
        self.assertIn("if (video.readyState >= 2) show(video.paused && video.srcObject ? 'tap' : null);", connected)

    def test_the_page_waiting_while_another_shares_cant_cut_the_share(self):
        # It never connected, so it has no viewers or recording of its own: Quit asks the server whether something
        # is shared, the Easy address switch and Open in my browser stay off with the rest, and the title counts
        # no waiting devices once a page has let go.
        script = read("share.js")
        quit = body_of(script, "async function quit()")
        self.assertIn("const live = controlsOn ? liveHere() : await sharingElsewhere();", quit)
        self.assertIn("if (live && !window.confirm('Stop sharing and quit Porthole?')) return;", quit)
        self.assertIn("return Boolean(info.sharing);", body_of(script, "async function sharingElsewhere()"))
        enabled = body_of(script, "function setControlsEnabled(on)")
        self.assertIn("controlsOn = on;", enabled)
        self.assertIn("'open-browser'", enabled)
        self.assertIn("$('easy').disabled = view.disabled || !controlsOn;", body_of(script, "function renderEasy()"))
        self.assertIn("if (inactive || !controlsOn) return;", body_of(script, "async function applyEasy()"))
        self.assertIn("const waitingCount = inactive ? 0 : waitingList.length;", body_of(script, "function render()"))

    def test_a_page_that_finds_another_sharing_waits_with_its_controls_off(self):
        # Up to 3 s for an old tab to let go: until then it is not the sharer, so nothing on it may act yet.
        script = read("share.js")
        init = body_of(script, "async function init()")
        self.assertIn("if (info.sharer_active) setControlsEnabled(false);", init)
        self.assertLess(init.index("if (info.sharer_active) setControlsEnabled(false);"), init.index("sharerSlotFrees()"))
        quit = body_of(script, "async function quit()")
        self.assertIn("const live = controlsOn ? liveHere() : await sharingElsewhere();", quit)
        self.assertIn("picks.stopped();", body_of(script, "function showBusy(sharing)"))
        self.assertIn("if (!signal || !signal.id) return;", body_of(script, "function reportSharing()"))

    def test_the_waiting_page_takes_the_free_slot_when_the_other_page_closes(self):
        # Nothing would be shared or open, and a phone waiting to be let in would have nobody to let it in.
        refresh = body_of(read("share.js"), "async function refreshInfo()")
        self.assertIn("if (waiting && !info.sharer_active) {", refresh)
        self.assertIn("connect(false);", refresh)

    def test_a_page_that_let_go_lists_no_devices(self):
        # After Stopped or Moved those devices are gone or belong to the other page.
        ended = body_of(read("share.js"), "function endPage(state)")
        self.assertIn("for (const viewer of viewers.values()) closeLink(viewer);", ended)
        self.assertIn("viewers.clear();", ended)

    def test_try_again_waits_until_porthole_answers(self):
        # A reload with Porthole still down shows the browser's own error page: a dead end in a home-screen app.
        script = read("view.js")
        self.assertIn("$('retry').addEventListener('click', tryAgain);", script)
        again = body_of(script, "async function tryAgain()")
        for line in ("show('connecting');", "await fetch(location.href, { cache: 'no-store' });", "location.reload();"):
            self.assertIn(line, again)

    def test_the_free_slot_is_taken_only_once_a_reloading_page_has_had_its_chance(self):
        # A reloaded sharer page takes up to 3 s to get its slot back (sharerSlotFrees): look again after that.
        script = read("share.js")
        self.assertIn("const SLOT_GRACE_MS = 4000;", script)
        refresh = body_of(script, "async function refreshInfo()")
        block = refresh[refresh.index("if (waiting && !info.sharer_active) {") :]
        self.assertIn("const again = await later(SLOT_GRACE_MS, readInfo);", block)
        self.assertIn("if (again && again.room === room && !again.sharer_active) connect(false);", block)

    def test_try_again_waits_through_a_404_or_two(self):
        # While Porthole starts, the easy address is off for a moment and an old link answers 404 "expired".
        again = body_of(read("view.js"), "async function tryAgain()")
        self.assertIn("notFound = res.status === 404 ? notFound + 1 : 0;", again)
        self.assertIn("if (notFound === 0 || notFound >= 3) {", again)

    def test_a_pinch_follows_the_two_fingers_it_measures(self):
        # Three fingers, one lifted: the pinch must restart from the two that are left, or the picture jumps.
        script = read("view.js")
        self.assertIn("if (pointers.size >= 2) pinch = pinchState();", script)
        self.assertIn("pinch = pointers.size >= 2 ? pinchState() : null;", script)

    def test_the_weak_signal_badge_settles_before_it_changes(self):
        script = read("view.js")
        self.assertIn("badge = weakBadge(badge, isWeak(statsPrev));", script)
        self.assertIn("badge = weakBadge();", body_of(script, "function closeLink()"))

    def test_a_device_not_let_in_keeps_its_screen_when_the_sharer_page_goes(self):
        # Nothing changed for it: it keeps "Ask the sharer to let you in" and its code, not "Sharing stopped".
        messages = body_of(read("view.js"), "function onMessage(msg)")
        self.assertIn("if (msg.type === 'sharer-left' && !link && (screenShown === SCREENS.approval || screenShown === SCREENS.waiting)) break;", messages)

    def test_the_sound_button_follows_the_picture_whatever_changed_it(self):
        # The iPhone's own full-screen player and Firefox's picture-in-picture have mute buttons of their own.
        self.assertIn("video.addEventListener('volumechange', updateSound);", read("view.js"))

    def test_a_device_refused_by_the_name_sees_use_the_link_not_ended(self):
        # After Easy address went off and its wait was over, the share still runs: the page shows why it can't go on.
        closed = body_of(read("view.js"), "async function onClosed()")
        self.assertIn("if (answer && answer.status === 400) {", closed)
        self.assertIn("location.reload();", closed[closed.index("if (answer && answer.status === 400) {") :])

    def test_an_approval_is_tied_to_the_token_and_address_a_denial_to_the_token(self):
        script = read("share.js")
        admit = body_of(script, "function admit(viewer)")
        self.assertIn("if (settings.ask && !approved.has(approvalKey(viewer))) {", admit)
        self.assertIn("if (denied.has(denialKey(viewer))) {", admit)
        self.assertIn("approved.add(approvalKey(viewer));", body_of(script, "function allow(viewer)"))
        away = body_of(script, "function sendAway(viewer, type)")
        self.assertIn("denied.add(denialKey(viewer));", away)
        self.assertIn("forgetApprovals(viewer);", away)
        self.assertNotIn("keyOf", script)

    def test_a_stale_page_in_porthole_s_own_window_closes(self):
        # Its browser would otherwise keep the new run's window from being followed: closing that wouldn't stop it.
        script = read("share.js")
        status = body_of(script, "async function onStatus(status)")
        self.assertIn("closeOwnWindow();", status[status.index("endPage('Stopped');") :])

    def test_after_moved_the_saved_recording_can_still_be_opened(self):
        # Porthole still runs: Open folder works, though the rest of the page has let go.
        script = read("share.js")
        replaced = script[script.index("case 'replaced':") : script.index("render();\n}", script.index("case 'replaced':"))]
        self.assertIn("$('open-folder').disabled = false;", replaced)

    def test_the_mini_window_names_the_code(self):
        self.assertIn("`${rowName(first)} · Code ${first.code}`", read("share.js"))

    def test_full_screen_and_pop_out_say_when_they_are_on(self):
        script = read("view.js")
        for event in ("'fullscreenchange'", "'webkitfullscreenchange'", "'enterpictureinpicture'", "'leavepictureinpicture'", "'webkitpresentationmodechanged'"):
            self.assertIn(event, script)
        self.assertIn("$('fullscreen').setAttribute('aria-pressed'", script)
        self.assertIn("$('pip').setAttribute('aria-pressed'", script)

    def test_quit_asks_only_when_it_would_cut_something_live(self):
        # Someone watching a share, or a recording: devices only waiting (or let in before sharing) see nothing.
        script = read("share.js")
        live = body_of(script, "function liveHere()")
        self.assertIn("return Boolean(recording) || (Boolean(stream) && [...viewers.values()].some((v) => v.state !== 'pending'));", live)

    def test_a_page_from_an_earlier_run_ends_and_sends_its_room(self):
        # Porthole killed and started again: the old page's stream is refused. It ends, and its actions carry its
        # room, so they can never stop or change the new run.
        script = read("share.js")
        status = body_of(script, "async function onStatus(status)")
        self.assertIn("stop();", status)
        self.assertIn("endPage('Stopped');", status)
        self.assertLess(status.index("endPage('Stopped');"), status.index("showError('Porthole has stopped or restarted."))
        self.assertIn("'X-Porthole-Room': room", body_of(script, "async function api("))

    def test_a_page_from_an_earlier_run_says_porthole_was_restarted_when_the_new_run_answers(self):
        # "Porthole has stopped or restarted. Start it again and reload this page." showed though the new run was up.
        script = read("share.js")
        self.assertIn("const RESTARTED = 'Porthole was restarted. Use its new page, or reload this one.';", script)
        status = body_of(script, "async function onStatus(status)")
        closed = status[status.index("} else if (status === 'closed') {") :]
        self.assertIn("const run = answeringRun(info, room);", closed)
        self.assertIn("if (run === 'another') {", closed)
        self.assertIn("endRestarted();", closed)
        # This run, with no sharer page now (the one that held the slot has just gone): this page takes the slot.
        self.assertIn("connect(false);", closed)
        # The old words only when nothing answers.
        self.assertLess(closed.index("endRestarted();"), closed.index("showError('Porthole has stopped or restarted. Start it again and reload this page.');"))
        restarted = body_of(script, "function endRestarted()")
        for line in ("stop();", "endPage('Stopped');", "showNotice(RESTARTED);", "closeOwnWindow();"):
            self.assertIn(line, restarted)
        # The page waiting while another one shares notices by itself, at its 10-second check.
        refresh = body_of(script, "async function refreshInfo()")
        self.assertIn("if (waiting && answeringRun(info, room) === 'another') {", refresh)
        self.assertLess(refresh.index("endRestarted();"), refresh.index("endStopped();"))
        # A page that ended because Porthole stopped or went away (killed, then started again some seconds later)
        # keeps checking, and says it was restarted once the new run answers.
        for ending in ("function endStopped()", "function endGone()"):
            self.assertIn("runEnded = true;", body_of(script, ending))
        self.assertIn("runEnded = true;", closed)
        self.assertNotIn("runEnded = true;", body_of(script, "function endRestarted()"))
        dead = refresh[: refresh.index("const waiting")]
        self.assertIn("if (inactive) {", dead)
        self.assertIn("if (runEnded && answeringRun(await readInfo(), room) === 'another') {", dead)
        for line in ("runEnded = false;", "showError('');", "showNotice(RESTARTED);", "closeOwnWindow();"):
            self.assertIn(line, dead)

    def test_a_viewer_from_an_earlier_run_says_porthole_was_restarted(self):
        # It said "The sharer closed Porthole." though the sharer had started it again. Its own address tells: the new
        # run answers with "This link doesn't work", or with the easy address, another room.
        script = read("view.js")
        self.assertIn("restarted: { title: 'Ended', detail: 'Porthole was restarted. Use its new link, or try again.', final: true },", script)
        closed = body_of(script, "async function onClosed()")
        self.assertIn("wasRestarted(", closed)
        self.assertIn("location.pathname", closed)
        self.assertIn("leave(restarted ? 'restarted' : 'ended');", closed)
        status = body_of(script, "function onStatus(")
        self.assertIn("onClosed();", status)
        self.assertNotIn("leave('ended');", status)

    def test_the_waiting_page_notices_porthole_has_stopped(self):
        # No event stream, so no "ended": it checks /api/info itself, and ends like any page once Porthole has gone.
        script = read("share.js")
        refresh = body_of(script, "async function refreshInfo()")
        self.assertIn("if (!info) info = await later(1000, readInfo);", refresh)
        self.assertIn("if (waiting && answeringRun(info, room) === 'none') {", refresh)
        self.assertIn("endStopped();", refresh)
        stopped = body_of(script, "function endStopped()")
        for line in ("endPage('Stopped');", "showNotice('Porthole has stopped. You can close this page.');", "closeOwnWindow();"):
            self.assertIn(line, stopped)
        self.assertIn("$('busy').hidden = true;", body_of(script, "function endPage(state)"))

    def test_only_a_browser_tab_taking_over_unties_porthole_s_window(self):
        # Another Porthole window lives in the same browser: the tie stays, so closing the last window still stops it.
        script = read("share.js")
        self.assertIn("if (appWindow && !msg.window) api('/api/release-window').catch(() => {});", script)
        connect = body_of(script, "function connect(takeover)")
        self.assertIn("params: { page: PAGE, window: appWindow ? 1 : 0 }", connect)

    def test_a_recording_belongs_to_the_page_that_started_it(self):
        script = read("share.js")
        self.assertIn("JSON.stringify({ ext: mimeType.startsWith('video/mp4') ? 'mp4' : 'webm', page: PAGE })", script)
        self.assertIn("const PAGE = ", script)

    def test_a_picture_paused_from_outside_offers_play_again(self):
        # Closing picture-in-picture, headphones going, a media key: the browser pauses the video by itself.
        script = read("view.js")
        paused = script[script.index("video.addEventListener('pause', () => {") :]
        paused = paused[: paused.index("\n});\n")]
        self.assertIn("if (gone || !playing || !video.srcObject) return;", paused)
        self.assertIn("setPlaying(false);", paused)
        self.assertIn("show('tap');", paused)

    def test_zoom_belongs_to_one_picture(self):
        # Fit shows only over a picture, and a share that ends takes its zoom with it.
        script = read("view.js")
        self.assertIn("$('fit').hidden = !playing || view.scale <= 1.001;", script)
        messages = body_of(script, "function onMessage(msg)")
        for case in ("case 'hello':", "case 'waiting':"):
            self.assertIn("unzoom();", messages[messages.index(case) :].split("break;")[0], case)
        ended = messages[messages.index("case 'sharer-left':") :]
        self.assertIn("unzoom();", ended[: ended.index("show('stopped');")])

    def test_the_viewer_says_it_has_a_touch_screen(self):
        # An iPad's browser says Mac; the sharer should still read "iPad".
        script = read("view.js")
        self.assertIn("params: { touch: navigator.maxTouchPoints > 1 ? 1 : 0 }", script)

    def test_waiting_viewers_are_pinned_under_the_main_action(self):
        text = read("share.html")
        self.assertRegex(text, r'<section id="waiting"[^>]*hidden')
        self.assertRegex(text, r'<button id="deny-all"[^>]*hidden[^>]*>Deny all</button>')
        self.assertIn('id="waiting-list"', text)

    def test_join_section(self):
        text = read("share.html")
        for element in ('id="qr"', 'id="easy-host"', 'id="link"', 'id="other-links"'):
            self.assertIn(element, text)
        self.assertRegex(text, r'<button id="copy" type="button"[^>]*>Copy</button>')
        self.assertRegex(text, r'<details id="other"[^>]*>\s*<summary>Other addresses</summary>')
        css = read("style.css")
        self.assertRegex(css, r"\.qr \{[^}]*width: 160px")

    def test_settings_are_plain_switches_with_short_lines(self):
        text = read("share.html")
        for name in ("ask", "easy", "smooth"):
            self.assertRegex(text, rf'<input type="checkbox" id="{name}" class="toggle" role="switch"')
        self.assertIn("New devices wait until you allow them.", text)
        # Turning Easy address on also turns Ask on: the line says so, or Ask seems to flip by itself.
        self.assertIn('<span id="easy-sub" class="sub">People on this Wi-Fi can type a short name. Also turns on Ask.</span>', text)
        self.assertNotIn("For videos and games", text)

    def test_viewer_rows_keep_numbers_in_the_tooltip(self):
        script = read("share.js")
        self.assertIn("rowTooltip(", script)
        self.assertIn("rowState(", script)
        self.assertNotIn("`Watching · ${", script)

    def test_every_control_has_a_label(self):
        for name in ("share.html", "view.html"):
            text = read(name)
            for button in re.findall(r"<button[^>]*>(.*?)</button>", text, re.S):
                self.assertTrue(button.strip(), f"{name}: an empty button")
            for tag in re.findall(r"<(?:input|select)[^>]*>", text):
                if 'type="radio"' in tag or 'type="checkbox"' in tag:
                    continue  # wrapped in a <label> with text, checked below
                self.assertIn("aria-label=", tag, f"{name}: {tag}")
        share = read("share.html")
        for label in re.findall(r'<label class="switch-row">(.*?)</label>', share, re.S):
            self.assertRegex(label, r"<span[^>]*>[A-Z][^<]+</span>")

    def test_spoken_names_start_with_the_visible_text(self):
        # Voice control users say what they see ("tap 1:1"): an aria-label must not replace it.
        for name in ("share.html", "view.html"):
            for tag, aria, visible in re.findall(r'(<button[^>]*aria-label="([^"]*)"[^>]*>)(.*?)</button>', read(name), re.S):
                visible = " ".join(re.sub(r"<[^>]+>", "", visible).split())
                self.assertTrue(aria.lower().startswith(visible.lower()), f"{name}: {tag}{visible}")

    def test_copy_says_copied_to_screen_readers_too(self):
        # A fixed aria-label would hide the "Copied" text the button switches to.
        self.assertIn('<button id="copy" type="button">Copy</button>', read("share.html"))
        self.assertIn("$('copy').textContent = 'Copied'", read("share.js"))

    def test_share_this_tab_instead_options(self):
        script = read("share.js")
        for option in (
            "surfaceSwitching: 'include'",
            "selfBrowserSurface: 'exclude'",
            "preferCurrentTab: false",
            "systemAudio: 'include'",
            "monitorTypeSurfaces: 'include'",
        ):
            self.assertIn(option, script)

    def test_refusal_note(self):
        self.assertRegex(read("share.html"), r'id="refusal"[^>]*hidden')
        script = read("share.js")
        self.assertIn("refusalLine(", script)
        self.assertIn("info.refusals", script)

    def test_pause_sends_a_card_and_pauses_the_recording(self):
        script = read("share.js")
        self.assertIn("'Paused'", script)
        self.assertIn("canvas.width = 1280", script)
        self.assertIn("canvas.height = 720", script)
        self.assertIn("captureStream(2)", script)
        self.assertIn("replaceTrack(", script)
        self.assertIn(".recorder.pause()", script)
        self.assertIn(".recorder.resume()", script)
        self.assertRegex(script, r"enabled = false")

    def test_change_says_why_when_the_pick_cant_be_captured(self):
        # Start sharing said "Couldn't start sharing: ..."; Change said nothing, and the share went on as if nothing
        # had happened. Both give the same cause now (pickError), and Change leaves the share running.
        script = read("share.js")
        start = body_of(script, "async function start()")
        self.assertIn("const problem = pickError(\"Couldn't start sharing\", err);", start)
        change = body_of(script, "async function change()")
        self.assertIn("} catch (err) {", change)
        self.assertIn("const problem = pickError(\"Couldn't change what's shared\", err);", change)
        self.assertIn("if (problem) showError(problem);", change)
        failed = change[change.index("} catch (err) {") : change.index("if (!pick.keep(next))")]
        for touched in ("useStream(", "stop(", "stopRecording(", "getTracks()"):
            self.assertNotIn(touched, failed)  # the current share stays as it was
        # In test mode a failed pick doesn't leave a timer drawing a picture nobody can stop.
        picture = body_of(script, "function testPicture()")
        self.assertLess(picture.index("canvas.captureStream(30)"), picture.index("setInterval("))

    def test_viewers_hear_about_the_pause_and_show_a_note_over_the_picture(self):
        # "Paused" is drawn only in the middle of the card: zoomed in on a corner, a viewer saw a plain dark picture.
        script, view, page, css = read("share.js"), read("view.js"), read("view.html"), read("style.css")
        self.assertIn("tellViewers({ type: 'paused' });", body_of(script, "function pause()"))
        self.assertIn("tellViewers({ type: 'resumed' });", body_of(script, "async function resume()"))
        self.assertIn("if (viewer.link) signal.send(viewer.id, message);", body_of(script, "function tellViewers(message)"))
        # A device that joins while paused hears it right after its offer, in order.
        offer = body_of(script, "async function offerTo(id)")
        self.assertIn("if (paused) signal.send(id, { type: 'paused' });", offer)
        self.assertLess(offer.index("type: 'offer'"), offer.index("if (paused) signal.send(id, { type: 'paused' });"))
        # The note: over the picture (not zoomed with it), never a live region of its own; the page's one live region
        # says "Paused" once.
        self.assertIn('<div id="paused" class="badge paused-note" hidden>Paused</div>', page)
        self.assertLess(page.index('id="paused"'), page.index('id="overlay"'))
        block = css[css.index(".paused-note {") : css.index("}", css.index(".paused-note {"))]
        self.assertIn("right: calc(12px + env(safe-area-inset-right));", block)
        self.assertIn("left: auto;", block)
        messages = body_of(view, "function onMessage(")
        self.assertIn("case 'paused':", messages)
        self.assertIn("case 'resumed':", messages)
        self.assertIn("showPaused(msg.type === 'paused');", messages)
        self.assertIn("$('paused').hidden = !(sharerPaused && playing);", body_of(view, "function showPaused(on)"))
        self.assertIn("paused: sharerPaused,", body_of(view, "function speak()"))
        # Any new connection, a stop or an ending starts without it; the sharer says it again where it still holds.
        self.assertIn("showPaused(false);", body_of(view, "function closeLink()"))
        self.assertIn("showPaused(sharerPaused);", body_of(view, "function setPlaying(on)"))

    def test_a_recording_with_no_picture_says_nothing_was_recorded(self):
        # Started while paused and stopped before Resume: the page tells Porthole no picture went in, and shows
        # Porthole's answer ("Nothing was recorded.", no Open folder) instead of a Saved line.
        script = read("share.js")
        self.assertIn("rec.recorded = !paused;", script)
        resume = script[script.index("function resumeRecording(") :]
        self.assertIn("rec.recorded = true;", resume[: resume.index("\n}\n")])
        self.assertIn("const nothing = rec.recorded ? '' : '&recorded=0';", script)
        render = script[script.index("function renderRecording()") :]
        render = render[: render.index("\n}\n")]
        for line in ("const outcome = recordingOutcome(lastSaved);", "$('open-folder').hidden = !outcome.folder;", "announce(outcome.line);"):
            self.assertIn(line, render)

    def test_change_while_paused_then_stop_shows_the_part_saved_before_change(self):
        # Change continues a recording in a new file, which has nothing in it while paused. Stopped before Resume, that
        # file goes, and the page shows the part before Change again (keptRecording, tested in tests/js).
        script = read("share.js")
        change = body_of(script, "async function change()")
        self.assertLess(change.index("await stopRecording();"), change.index("const earlier = lastSaved;"))
        self.assertLess(change.index("await startRecording();"), change.index("recording.earlier = earlier;"))

    def test_a_recording_a_full_disk_stops_keeps_its_saved_line(self):
        # A 507 that says why it stopped (`stopped`) is no failure to save: the recording ends as usual (Saved line,
        # Open folder), and the error says why it stopped and that what came before is saved.
        script = read("share.js")
        upload = script[script.index("async function upload(") :]
        upload = upload[: upload.index("\n}\n")]
        self.assertIn("result.status === 507 && result.data && result.data.stopped", upload)
        self.assertIn("stopRecording(stoppedByDisk ? '' : `Couldn't save the recording: ${apiError(result)}`, rec);", upload)
        stop = script[script.index("function stopRecording(") :]
        stop = stop[: stop.index("\n}\n")]
        self.assertIn("lastSaved = keptRecording(result.data, rec.earlier);", stop)
        self.assertIn("problem = recordingOutcome(lastSaved).error;", stop)
        self.assertLess(stop.index("problem = recordingOutcome"), stop.index("if (problem) showError(problem);"))

    def test_stop_wins_over_an_open_screen_picker(self):
        # The mini window's Stop works while the picker of Start or Change covers the page. The
        # guard itself is tested in tests/js; check that every pick and every stop use it.
        script = read("share.js")
        self.assertIn("const picks = newPickGuard();", script)
        stop = re.search(r"\nfunction stop\(\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertLess(stop.index("picks.stopped();"), stop.index("if (!stream) return;"))  # also with nothing shared yet
        for name in ("start", "change"):
            body = re.search(rf"\nasync function {name}\(\) \{{(.*?)\n\}}", script, re.S).group(1)
            self.assertLess(body.index("picks.begin()"), body.index("pickStream()"), name)
            self.assertLess(body.index("pickStream()"), body.index(".keep("), name)
            self.assertLess(body.index(".keep("), body.index("useStream(next)"), name)
        change = re.search(r"\nasync function change\(\) \{(.*?)\n\}", script, re.S).group(1)
        # Change waits for the old recording to close first: a stop then wins too.
        waits = change.index("await stopRecording()")
        self.assertLess(waits, change.index(".keep(", waits))
        self.assertLess(change.index(".keep(", waits), change.index("useStream(next)"))

    def test_mini_controls(self):
        self.assertRegex(read("share.html"), r'<button id="mini" type="button"[^>]*hidden[^>]*>Mini controls</button>')
        script = read("share.js")
        self.assertIn("'documentPictureInPicture' in window", script)
        self.assertIn("requestWindow({ width: 280, height: 180 })", script)
        self.assertIn("/static/style.css", script)

    def test_live_status_lines_are_written_only_when_they_change(self):
        # Both status lines are read out by screen readers; render() runs every 2 s. The browser
        # test checks the main one; the mini window can't be opened there, so check the code.
        script = read("share.js")
        self.assertRegex(script, r"function setText\(element, text\) \{\s*if \(element\.textContent !== text\) element\.textContent = text;")
        self.assertNotRegex(script, r"\$\('state'\)\.textContent =")
        self.assertNotRegex(script, r"mini\.state\.textContent =")
        self.assertIn("setText(mini.state, ", script)

    def test_viewer_bar_buttons(self):
        text = read("view.html")
        for element_id, label in (
            ("unmute", "Sound"),
            ("fit", "Fit"),
            ("actual", "1:1"),
            ("pip", "Pop out"),
            ("save", "Save"),
            ("fullscreen", "Full screen"),
        ):
            self.assertRegex(text, rf'<button id="{element_id}" type="button"[^>]*>{re.escape(label)}</button>')
        self.assertRegex(text, r'<button id="fullscreen" type="button"[^>]*hidden')  # until video plays
        self.assertRegex(text, r'<select id="quality" aria-label="Quality">')

    def test_viewer_bar_is_hidden_when_idle_not_faded(self):
        css = read("style.css")
        idle = re.search(r"\.view-bar\.idle \{([^}]*)\}", css).group(1)
        self.assertIn("visibility: hidden", idle)
        self.assertIn("pointer-events: none", css[css.index(".view-bar {") :])

    def test_the_tap_that_wakes_the_bar_presses_nothing(self):
        # The browser test taps for real; this keeps the parts in place where Chrome is missing.
        self.assertRegex(read("style.css"), r"\.view-bar\.waking > \* \{ pointer-events: none; \}")
        script = read("view.js")
        self.assertIn("bar.classList.add('waking')", script)
        self.assertRegex(script, r"for \(const type of \['pointerup', 'pointercancel'\]\)")

    def test_viewer_respects_the_notch(self):
        css = read("style.css")
        for part in (".view-bar {", ".badge {", ".overlay {"):
            block = css[css.index(part) : css.index("}", css.index(part))]
            self.assertIn("env(safe-area-inset-", block, part)
        self.assertIn("viewport-fit=cover", read("view.html"))

    def test_viewer_screens(self):
        text = read("view.html")
        self.assertRegex(text, r'<button id="retry" type="button" class="primary"[^>]*hidden[^>]*>Try again</button>')
        self.assertIn('id="detail"', text)
        script = read("view.js")
        for state in ("'Waiting'", "'Connecting'", "\"Can't connect\"", "'Ended'"):
            self.assertIn(state, script, state)
        self.assertRegex(text, r'id="weak"[^>]*hidden[^>]*>.*Weak signal</div>')
        self.assertIn("location.reload()", script)

    def test_viewer_features(self):
        script = read("view.js")
        self.assertIn("requestPictureInPicture()", script)
        self.assertIn("pictureInPictureEnabled", script)
        self.assertIn("webkitSetPresentationMode('picture-in-picture')", script)
        self.assertIn("actualSizeScale(", script)
        self.assertIn("pictureName(", script)
        self.assertIn("drawImage(video", script)

    def test_viewer_home_screen_links(self):
        text = read("view.html")
        self.assertIn('<link rel="manifest" href="/static/manifest.json?v=ring">', text)
        self.assertIn('<link rel="apple-touch-icon" href="/static/icon-180.png?v=ring">', text)

    def test_icons_have_a_new_address_for_the_ring(self):
        # Browsers keep a page's icon in a store of their own, apart from the HTTP cache (Porthole's answers say
        # no-store), and look it up by the icon's address. With the old address, Porthole's own window kept
        # showing the picture from before the ring (seen on a real Ubuntu, 26 Nov 2026). So the pages ask for
        # the icons by an address that names the ring. Change the picture, change the address too.
        for page in ("share.html", "view.html"):
            with self.subTest(page=page):
                self.assertIn('<link rel="icon" href="/static/icon.svg?v=ring" type="image/svg+xml">', read(page))
        manifest = read("manifest.json")
        self.assertIn('"/static/icon-192.png?v=ring"', manifest)
        self.assertIn('"/static/icon-512.png?v=ring"', manifest)
        for page in ("share.html", "view.html"):
            self.assertNotRegex(read(page), r'href="/static/(icon[^"?]*|manifest\.json)"')

    def test_the_icon_address_changes_with_the_picture(self):
        # If web/icon.svg changes, this fails: give the icons a new ?v= in share.html, view.html and manifest.json
        # (see the test above), then update this hash.
        import hashlib

        self.assertEqual(hashlib.sha256((WEB / "icon.svg").read_bytes()).hexdigest(), "cf4cc8c0fc363c7ed0f0a24142632a8f8044acaee44de24a532b1ae0bbb8e9a6")

    def test_icons_are_drawn_by_the_tool(self):
        tool = (WEB.parent / "tools" / "make_icons.py").read_text(encoding="utf-8")
        for size in (180, 192, 512):
            self.assertIn(str(size), tool)
            self.assertTrue((WEB / f"icon-{size}.png").is_file(), size)

class AccessibilityTest(unittest.TestCase):
    """The accessibility pass (23 Nov 2026). Behaviour is checked in the browser test and tests/js."""

    def css_rules(self):
        css = re.sub(r"/\*.*?\*/", "", read("style.css"), flags=re.S)
        return [(" ".join(selector.split()), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]

    def test_page_titles_say_where_you_are(self):
        titles = {name: re.search(r"<title>([^<]*)</title>", read(name)).group(1) for name in ("share.html", "view.html")}
        # The sharer page is plain "Porthole" (since 23 Nov 2026): it names the app window and its tab.
        self.assertEqual(titles["share.html"], "Porthole")
        self.assertEqual(titles["view.html"], "Shared screen · Porthole")
        script = read("share.js")
        self.assertIn("const TITLE = 'Porthole';", script)  # the waiting count goes in front: "(2) Porthole"
        self.assertIn("doc.title = 'Mini controls · Porthole';", script)
        # A home-screen shortcut on an iPhone is named after this, not the longer page title.
        self.assertIn('<meta name="apple-mobile-web-app-title" content="Porthole">', read("view.html"))

    def test_viewer_landmark_heading_and_picture_name(self):
        text = read("view.html")
        self.assertRegex(text, r'<main id="stage" class="stage">')
        self.assertRegex(text, r'<h1 id="status" class="overlay-title">Connecting</h1>')
        self.assertRegex(text, r'<video id="video"[^>]*aria-label="Shared screen"')
        self.assertIn("<main", read("share.html"))

    def test_announcements_go_through_one_quiet_live_region_per_page(self):
        # A live region that is shown or hidden is often not read out, and one written on every redraw
        # is read out again and again: each page has one that is always there and written on change.
        for name in ("share.html", "view.html"):
            self.assertIn('<p id="say" class="visually-hidden" role="status"></p>', read(name), name)
        view = read("view.html")
        self.assertNotRegex(view, r'id="overlay"[^>]*(role=|aria-live)')
        self.assertNotRegex(view, r'id="weak"[^>]*role=')
        script = read("share.js")
        self.assertIn('class="visually-hidden" role="status" data-mini="say"', script)
        self.assertIn("waitingNews(", script)
        # Cleared, then written a moment later: the same news again is read again (newsWriter).
        self.assertRegex(script, r"const announce = newsWriter\(\s*\(text\) => \(\$\('say'\)\.textContent = text\),\s*\(step\) => setTimeout\(step, \d+\),\s*\);")
        self.assertNotIn("setText($('say')", script)
        self.assertIn("spokenState(", read("view.js"))
        self.assertRegex(read("view.js"), r"setText\(\$\('say'\), ")
        self.assertRegex(read("style.css"), r"\.visually-hidden \{[^}]*clip: rect\(0 0 0 0\)")

    def test_errors_and_notes_are_written_only_when_they_change(self):
        script = read("share.js")
        for name in ("showError", "showNotice"):
            body = re.search(rf"\nfunction {name}\(text\) \{{(.*?)\n\}}", script, re.S).group(1)
            self.assertIn("setText(", body, name)
            self.assertNotIn(".textContent = text", body, name)
        self.assertIn('id="error" class="error" role="alert"', read("share.html"))

    def test_switches_are_named_by_their_label_and_described_by_its_line(self):
        text = read("share.html")
        names = {"ask": "Ask before someone watches", "easy": "Easy address", "smooth": "Smooth motion"}
        for name, label in names.items():
            tag = re.search(rf'<input type="checkbox" id="{name}"[^>]*>', text).group(0)
            self.assertIn('role="switch"', tag)
            self.assertIn(f'aria-labelledby="{name}-name"', tag)
            self.assertIn(f'<span id="{name}-name">{label}</span>', text)
        self.assertIn('aria-describedby="ask-sub"', text)
        self.assertIn('<span id="ask-sub" class="sub">New devices wait until you allow them.</span>', text)
        # Easy address: its line, and the error or note that explains a greyed-out switch.
        self.assertIn('aria-describedby="easy-sub easy-error easy-note"', text)
        for element in ('id="easy-sub" class="sub"', 'id="easy-error"', 'id="easy-note"'):
            self.assertIn(element, text)

    def test_lines_that_appear_later_are_announced(self):
        # An Easy address error or note, or "no network", that shows up after the page was drawn is news;
        # the switch's description alone isn't read again when it changes.
        script = read("share.js")
        easy = re.search(r"\nfunction renderEasy\(\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("lineNews(", easy)
        self.assertRegex(easy, r"return ")
        self.assertRegex(script, r"const easyNews = renderEasy\(\);\s*if \(easyNews\) said\.push\(easyNews\);")
        join = re.search(r"\nfunction drawJoin\(info\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertRegex(join, r"announce\(lineNews\(")

    def test_focused_row_reads_short_and_keeps_numbers_off_it(self):
        # A row takes the focus after Allow, Deny or Remove: it reads the device, code and state (rowLabel),
        # and the live numbers' tooltip sits on a part that never takes focus.
        script = read("share.js")
        update = re.search(r"\nfunction updateRow\(row, viewer\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("rowLabel(viewer, Boolean(stream))", update)
        self.assertIn("row.item.setAttribute('aria-label', label)", update)
        self.assertIn("row.main.setAttribute('title', tip)", update)
        self.assertNotIn("row.item.setAttribute('title'", script)

    def test_viewer_zooms_and_moves_by_keyboard(self):
        script = read("view.js")
        self.assertIn("viewForKey", script)
        keys = re.search(r"\n// Keys: .*?\ndocument\.addEventListener\('keydown', \(event\) => \{(.*?)\n\}\);", script, re.S).group(1)
        for guard in ("event.ctrlKey", "event.metaKey", "event.altKey", "select", "event.preventDefault()"):
            self.assertIn(guard, keys)

    def test_qr_code_says_what_it_is_once(self):
        text = read("share.html")
        self.assertIn('<div id="qr" class="qr" role="img" aria-label="QR code to open the link on a phone"></div>', text)
        # The picture inside is drawn by Porthole; it must not be a second, unnamed image.
        self.assertIn("setAttribute('aria-hidden', 'true')", read("share.js"))

    def test_focus_rings_in_every_theme_and_over_the_picture(self):
        rules = dict(self.css_rules())
        self.assertRegex(rules[":focus-visible"], r"outline: 2px solid var\(--accent\)")
        self.assertRegex(rules[".segmented input:focus-visible + span"], r"outline: 2px solid var\(--accent\)")
        # The viewer's controls float over any picture: the ring is drawn inside the button, on its own colour.
        bar = rules[".view-bar :focus-visible"]
        self.assertRegex(bar, r"outline: 2px solid #fff")
        self.assertRegex(bar, r"outline-offset: -\d+px")
        self.assertIn("outline-color: #000", rules['.view-bar button[aria-pressed="true"]:focus-visible'])

    def test_reduced_motion_turns_off_every_transition_and_animation(self):
        css = re.sub(r"/\*.*?\*/", "", read("style.css"), flags=re.S)
        start = css.index("@media (prefers-reduced-motion: reduce)")
        block = css[start : css.index("\n}", start)]
        calm = set()
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block[block.index("{") + 1 :]):
            if re.search(r"(transition|animation): none", body):
                calm.update(" ".join(s.split()) for s in selectors.split(","))
        moving = [selector for selector, body in self.css_rules() if re.search(r"(transition|animation):\s*(?!none)", body)]
        self.assertTrue(moving)
        for selector in moving:
            for part in selector.split(","):
                self.assertIn(part.strip(), calm, part)

    def test_targets_are_at_least_24px(self):
        rules = dict(self.css_rules())
        for selector in ("button", "button.small", ".segmented span", ".toggle", ".view-bar button, .view-bar select"):
            height = int(re.search(r"(?<![-\w])height: (\d+)px", rules[selector]).group(1))
            self.assertGreaterEqual(height, 24, selector)
        self.assertIn("min-height: 24px", rules[".other summary"])  # was 21 px tall

    def test_viewer_bar_is_a_named_group(self):
        # role="toolbar" promises moving with arrow keys, which the bar doesn't do (Tab moves through it).
        text = read("view.html")
        self.assertIn('<div id="bar" class="view-bar" role="group" aria-label="Controls">', text)
        self.assertNotIn('role="toolbar"', text)

    def test_orientation_is_not_locked(self):
        self.assertNotIn("orientation", read("manifest.json"))
        self.assertNotIn("orientation", read("style.css"))

    def test_tap_to_play_has_a_real_button(self):
        # The "Tap to play" screen was a clickable box: a keyboard couldn't press it.
        self.assertRegex(read("view.html"), r'<button id="play" type="button" class="primary" hidden>Play</button>')
        self.assertIn("$('play').hidden = !screen.tap;", read("view.js"))

    def test_quality_menu_keeps_keyboard_focus(self):
        # Arrow keys change a closed menu at once; dropping the focus then stranded keyboard users.
        script = read("view.js")
        self.assertNotRegex(script, r"\n  \$\('quality'\)\.blur\(\);")
        self.assertIn("if (!usingKeys) $('quality').blur();", script)

    def test_viewer_keeps_keys_off_hidden_controls(self):
        script = read("view.js")
        idle = re.search(r"\nfunction idle\(\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("bar.contains(document.activeElement)", idle)
        self.assertIn(".blur()", idle)
        self.assertIn("bar.addEventListener('focusin'", script)

    def test_focus_lands_somewhere_sensible(self):
        text, script = read("share.html"), read("share.js")
        self.assertIn('<h2 id="viewers-title" tabindex="-1">Watching</h2>', text)
        self.assertIn("item.tabIndex = -1;", script)
        self.assertIn("item.dataset.id = viewer.id;", script)
        for call in ("focusAfterChange(", "replacementFocus(", "const focusBefore = focusPlace();", "restoreFocus(focusBefore);"):
            self.assertIn(call, script)
        # A disabled button loses the focus in Chrome: Record (disabled while it starts) gets it back.
        start = re.search(r"\nasync function startRecording\(\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("const hadFocus = document.activeElement === $('record');", start)
        self.assertIn("focusOn('record')", start)
        # The mini window's Allow and Deny name the device, and its focus never strands on a hidden part.
        self.assertIn("mini.allow.setAttribute('aria-label', `Allow ${rowName(first)}`)", script)
        self.assertIn("mini.deny.setAttribute('aria-label', `Deny ${rowName(first)}`)", script)


if __name__ == "__main__":
    unittest.main()


class ThemeBootTest(unittest.TestCase):
    def test_saved_theme_is_applied_before_the_page_paints(self):
        html = read("share.html")
        head = html[: html.index("</head>")]
        self.assertIn('<script src="/static/theme-boot.js"></script>', head)
        self.assertLess(head.index("theme-boot.js"), head.index("style.css"))
        boot = read("theme-boot.js")
        self.assertIn("porthole-theme", boot)
        self.assertNotIn("import ", boot)  # a classic script, so it runs before first paint
