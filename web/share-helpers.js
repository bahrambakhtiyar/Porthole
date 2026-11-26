// Sharer page helpers for the status line, the viewer list, refusals and the waiting notification,
// kept free of the DOM so they can be unit-tested.

import { isWeak, describe } from './quality.js';

// The lowest number no current viewer with the same device label has. A row keeps its number.
export function nextNumber(viewers, device) {
  const used = new Set();
  for (const viewer of viewers) if (viewer.device === device) used.add(viewer.number);
  let number = 1;
  while (used.has(number)) number += 1;
  return number;
}

// The entry under `oldKey` moved to `newKey` (holding `value`), in the same place among the others: a device that
// reconnected keeps its row where it was. Changes `map` itself and returns it; nothing happens if `oldKey` is missing.
export function renameKey(map, oldKey, newKey, value) {
  if (!map.has(oldKey)) return map;
  const entries = [...map].map(([key, entry]) => (key === oldKey ? [newKey, value] : [key, entry]));
  map.clear();
  for (const [key, entry] of entries) map.set(key, entry);
  return map;
}

// "Android phone", then "Android phone 2", "Android phone 3", ...
export function rowName(viewer) {
  return viewer.number > 1 ? `${viewer.device} ${viewer.number}` : viewer.device;
}

// "09:05": 24-hour local time.
export function clockTime(ms) {
  const date = new Date(ms);
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

// When a viewer joined (epoch ms): the server's time, so a page reload keeps it; `now` if the
// server didn't say (an older server) or sent something unusable.
export function joinTime(joined, now) {
  return Number.isFinite(joined) && joined > 0 && joined <= 8.64e15 ? joined : now; // 8.64e15: the last valid Date
}

// "Code 482 · joined 14:05": the code matches the one on the viewer's waiting screen.
export function rowDetails(viewer) {
  const time = clockTime(viewer.joined);
  return viewer.code ? `Code ${viewer.code} · joined ${time}` : `Joined ${time}`;
}

// The one "N waiting" notification, when the waiting count goes from `before` to `count`:
// 'show' (create it, or replace it with the new number), 'close', or 'none'.
export function waitingNoteStep({ before, count, showing, away }) {
  if (count === before) return 'none';
  if (count === 0) return showing ? 'close' : 'none';
  if (showing) return 'show';
  return count > before && away ? 'show' : 'none';
}

export function waitingTitle(count) {
  return `${count} waiting`;
}

// True when a whole screen is shared, so viewers can see pop-up notifications too.
export function isWholeScreen(track) {
  try {
    return Boolean(track) && typeof track.getSettings === 'function' && track.getSettings().displaySurface === 'monitor';
  } catch {
    return false;
  }
}

// The status line under the header. `elsewhere`: while another window or tab is the sharer, whether it shares (true)
// or is only open (false); null or undefined otherwise. "Not sharing" there made Use this one look harmless, though it
// cuts everyone watching the other page.
export function statusText({ sharing, paused, recording, elsewhere = null }) {
  if (elsewhere === true) return 'Sharing in another window or tab';
  if (elsewhere === false) return 'Open in another window or tab';
  if (!sharing) return 'Not sharing';
  if (paused) return 'Paused';
  return recording ? 'Sharing · recording' : 'Sharing';
}

const ROW_STATES = {
  pending: 'Wants to watch',
  waiting: 'Waiting',
  connecting: 'Connecting',
  watching: 'Watching',
  failed: "Can't connect",
};

// A viewer row's short state; `cls` picks its colour. Nothing to watch means Waiting.
export function rowState(viewer, sharing) {
  let state = viewer.state in ROW_STATES ? viewer.state : 'waiting';
  if (!sharing && state !== 'pending') state = 'waiting';
  if (state === 'watching' && viewer.sample && isWeak(viewer.sample)) return { text: 'Weak signal', cls: 'weak' };
  return { text: ROW_STATES[state], cls: state };
}

// The live numbers, for the row's tooltip ("1280×720 · 30 fps · 4.1 Mbps").
export function rowTooltip(viewer) {
  if (viewer.state !== 'watching' || !viewer.sample) return '';
  const numbers = describe(viewer.sample);
  if (!numbers) return '';
  return viewer.mode === 'saver' ? `${numbers} · data saver` : numbers;
}

// "An Android phone", "A Mac", "A device".
export function withArticle(device) {
  if (!device || device === 'Device') return 'A device';
  return /^[AEIOUaeiou]|^i[A-Z]/.test(device) ? `An ${device}` : `A ${device}`;
}

const REFUSAL_REASONS = { 'too-many-tabs': 'too many tabs', 'too-many-tries': 'too many tries' };

// "An Android phone couldn't join: too many tabs (14:05)".
export function refusalLine(refusal) {
  if (!refusal || typeof refusal !== 'object') return '';
  const reason = REFUSAL_REASONS[refusal.reason];
  const time = Number.isFinite(refusal.time) && refusal.time > 0 ? ` (${clockTime(refusal.time)})` : '';
  return `${withArticle(refusal.device)} couldn't join${reason ? `: ${reason}` : ''}${time}`;
}

// The server lists refusals oldest first.
export function newestRefusal(list) {
  if (!Array.isArray(list)) return null;
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (list[i] && typeof list[i] === 'object') return list[i];
  }
  return null;
}

// What the page says once a recording has ended, from Porthole's answer: the line under the buttons (the Saved line
// with Open folder, or "Nothing was recorded." when the file had nothing in it and was deleted) and, when a write
// error (a full disk) cut it short, an error that says why ("the disk is full") and that what came before is kept.
export function recordingOutcome(result) {
  if (!result) return { line: '', folder: false, error: '' };
  const stopped = result.stopped ? `The recording stopped: ${result.stopped}.` : '';
  if (result.empty) return { line: 'Nothing was recorded.', folder: false, error: stopped };
  return {
    line: `Saved "${result.name}".`,
    folder: true,
    error: stopped && `${stopped} What was recorded until then is saved.`,
  };
}

// What a recording that continues another one (Change while recording) leaves on the page once it ends. With nothing
// in it (paused from Change until Stop recording), Porthole deleted its file, but the part before Change was saved:
// that part's Saved line and Open folder show again, as a new answer so it is said again.
export function keptRecording(answer, earlier) {
  if (!answer || !answer.empty || !earlier || earlier.empty) return answer;
  const { stopped, ...file } = earlier;
  return answer.stopped ? { ...file, stopped: answer.stopped } : file;
}

// Porthole opens its own window at /share?window. That window has its own browser profile, so the
// browser's tabs can't be picked there.
export function inAppWindow(search) {
  return new URLSearchParams(search || '').has('window');
}

// "Open in my browser" is offered in Porthole's own window before sharing starts.
export function browserHintShown({ appWindow, sharing, inactive }) {
  return Boolean(appWindow) && !sharing && !inactive;
}

// The browser and the system a page runs in, as far as sound goes, from its user agent: `browser` is 'firefox',
// 'chrome' (Chrome, Edge, Chromium, Brave, Opera) or 'safari' (also other WebKit browsers, such as GNOME Web), else
// ''; `system` is 'windows', 'mac' or 'linux', else '' (ChromeOS says X11 but isn't a Linux desktop).
export function soundPlace(userAgent) {
  const ua = typeof userAgent === 'string' ? userAgent : '';
  let browser = '';
  if (/Firefox\//.test(ua)) browser = 'firefox';
  else if (/Chrome\/|Chromium\//.test(ua)) browser = 'chrome';
  else if (/AppleWebKit\//.test(ua) && /Safari\//.test(ua)) browser = 'safari';
  let system = '';
  if (/Windows/.test(ua)) system = 'windows';
  else if (/Macintosh|Mac OS X/.test(ua)) system = 'mac';
  else if (/CrOS/.test(ua)) system = '';
  else if (/Linux|X11/.test(ua)) system = 'linux';
  return { browser, system };
}

// The note when nothing can be heard, naming only what this browser offers on this system. Firefox and Safari
// can't share sound at all. Chrome and its relatives share a tab's sound everywhere ("Also share tab audio"), and
// the whole system's sound only on Windows ("Also share system audio", offered for a whole screen). Porthole's own
// window (always Chrome or a relative) can't see the browser's tabs.
export function audioHint(appWindow, userAgent) {
  const { browser, system } = soundPlace(userAgent);
  if (browser === 'firefox' || browser === 'safari') return "No sound is shared. This browser can't share sound.";
  if (system === 'windows') return 'No sound is shared. Use Change, pick a screen and turn on "Also share system audio".';
  if (appWindow) return 'No sound is shared. To share a tab and its sound, stop sharing and use Open in my browser.';
  if (browser === 'chrome' && (system === 'linux' || system === 'mac')) {
    return 'No sound is shared. Use Change, pick a tab and turn on "Also share tab audio".';
  }
  return 'No sound is shared. Use Change and turn on "Also share tab audio" or "Also share system audio".';
}

// Whether to ask the browser now for leave to show the "2 waiting" notification: only with Ask on (devices wait for
// an answer then), only while the browser has none ('default': never again after a refusal), and once per page, so
// a prompt closed without an answer isn't shown again on every Start sharing.
export function askToNotify({ ask, permission, askedBefore }) {
  return Boolean(ask) && permission === 'default' && !askedBefore;
}

// What the page says when the screen picker fails, after `doing` ("Couldn't start sharing", "Couldn't change what's
// shared"): '' when it was only cancelled (the picker closed, or Cancel), else the browser's own words for the cause,
// the same for Start sharing and Change.
export function pickError(doing, err) {
  const name = err && err.name;
  if (name === 'NotAllowedError' || name === 'AbortError') return '';
  const cause = err && typeof err.message === 'string' && err.message ? err.message : 'something went wrong';
  return `${doing}: ${cause}`;
}

// Which run of Porthole answered /api/info, for a page of the run `room`: 'this', 'another' (Porthole was killed and
// started again, with a new room) or 'none' (nothing answered, or something that isn't Porthole).
export function answeringRun(info, room) {
  if (!info || typeof info !== 'object' || typeof info.room !== 'string' || !info.room) return 'none';
  return info.room === room ? 'this' : 'another';
}

// The line on a page that waits while another page is the sharer: whether that one shares, or is only open.
export function busyText(sharing) {
  return sharing ? 'Sharing is on in another window or tab.' : 'Porthole is open in another window or tab.';
}

// Ends every track of a stream that won't be shared. Never throws.
export function endTracks(stream) {
  let tracks = [];
  try {
    tracks = stream.getTracks();
  } catch {
    return;
  }
  for (const track of tracks) {
    try {
      track.stop();
    } catch {
      // already gone
    }
  }
}

// Stop wins over a screen picker that is still open. The mini window's Stop can come while Change's
// picker covers the page, and Porthole stopping (or sharing moving away) while Start's or Change's
// does; a pick that comes back after that is ended, never shared. Call stopped() on every stop,
// sharing or not.
export function newPickGuard() {
  let stops = 0;
  return {
    stopped() {
      stops += 1;
    },
    // One pick: current() is true until the next stop; keep(stream) returns the stream while
    // current, else ends its tracks and returns null.
    begin() {
      const at = stops;
      const current = () => at === stops;
      return {
        current,
        keep(stream) {
          if (current()) return stream;
          endTracks(stream);
          return null;
        },
      };
    },
  };
}

// ---------- screen readers and keyboard focus ----------

// Each device that starts waiting is announced once ("Android phone wants to watch. Code 482."), not
// again on every redraw. `announced` holds the ids already told; returns the words for new ones and
// the ids to remember next time (only those still waiting).
export function waitingNews(announced, waitingList) {
  const list = Array.isArray(waitingList) ? waitingList : [];
  const fresh = list.filter((viewer) => !announced.has(viewer.id));
  const text = fresh
    .map((viewer) => (viewer.code ? `${rowName(viewer)} wants to watch. Code ${viewer.code}.` : `${rowName(viewer)} wants to watch.`))
    .join(' ');
  return { text, announced: new Set(list.map((viewer) => viewer.id)) };
}

// The page's one live region, written only with news. A live region given the words it already holds
// says nothing, so the same news twice in a row (a note that shows again after Stop and Start) would
// be silent: each time, the region is cleared first and the news written a moment later. News given
// within that moment is said together. `write(text)` sets the region's text; `later(step)` runs the
// step a moment later (a short timer in the page). Returns say(text).
export function newsWriter(write, later) {
  let queue = [];
  return (text) => {
    if (!text) return;
    queue.push(text);
    if (queue.length > 1) return; // already on its way
    write('');
    later(() => {
      const all = queue.join(' ');
      queue = [];
      write(all);
    });
  };
}

// A line (an error, a note) is news when it appears or changes after the page drew it once; what comes
// with the page is read with the page. `before` is undefined until the first draw. Returns the words to
// say, or ''.
export function lineNews(before, now) {
  return before !== undefined && now && now !== before ? now : '';
}

// What a focused row reads ("Linux PC 2, code 717, Watching"): the device, its code and its state.
// Not its buttons, and not the live numbers of its tooltip, which change every 2 s.
export function rowLabel(viewer, sharing) {
  const code = viewer.code ? `code ${viewer.code}, ` : '';
  return `${rowName(viewer)}, ${code}${rowState(viewer, sharing).text}`;
}

// Where keyboard focus goes when the viewer row that had it (or Deny all) leaves its list or loses the
// focused button. `list` is 'waiting' or 'viewers', `index` the row's old place. In order: the same row
// if it is still in that list; the row now in its place (or the last one); after Allow, the allowed row
// under Watching; else the Watching heading. Never a button: a held Enter key would press it too.
export function focusAfterChange({ list, index, viewerId, waitingIds, watchingIds }) {
  const same = list === 'waiting' ? waitingIds : watchingIds;
  const other = list === 'waiting' ? watchingIds : waitingIds;
  if (viewerId && same.includes(viewerId)) return { kind: 'row', id: viewerId };
  if (same.length) return { kind: 'row', id: same[Math.min(Math.max(index, 0), same.length - 1)] };
  if (viewerId && other.includes(viewerId)) return { kind: 'row', id: viewerId };
  return { kind: 'heading' };
}

// A button that hides while it has keyboard focus hands it to the one that takes its place:
// Start sharing to Stop sharing, the sharing controls (and "Use this one") to Start sharing.
const REPLACEMENTS = { start: 'stop', pause: 'start', change: 'start', record: 'start', stop: 'start', mini: 'start', takeover: 'start' };

export function replacementFocus(id) {
  return Object.prototype.hasOwnProperty.call(REPLACEMENTS, id) ? REPLACEMENTS[id] : null;
}

// A viewer's keys in the approved and denied lists. An approval is for its browser token from the address it was
// allowed from, so the same token from a new address is asked again (a reconnect keeps its row only with both too).
// A denial is for its token wherever it comes from, so a new address doesn't get it back in. A viewer without a token
// (no storage) is known by its connection only.
export function approvalKey(viewer) {
  return viewer.client ? `${viewer.client}@${viewer.address || ''}` : viewer.id;
}

export function denialKey(viewer) {
  return viewer.client || viewer.id;
}

