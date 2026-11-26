import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  nextNumber,
  renameKey,
  rowName,
  clockTime,
  joinTime,
  rowDetails,
  waitingNoteStep,
  waitingTitle,
  isWholeScreen,
  statusText,
  rowState,
  rowTooltip,
  withArticle,
  refusalLine,
  newestRefusal,
  inAppWindow,
  browserHintShown,
  audioHint,
  newPickGuard,
  endTracks,
  waitingNews,
  focusAfterChange,
  replacementFocus,
  rowLabel,
  newsWriter,
  lineNews,
  recordingOutcome,
  keptRecording,
  askToNotify,
  busyText,
  soundPlace,
  pickError,
  answeringRun,
  approvalKey,
  denialKey,
} from '../../web/share-helpers.js';

test('the first device of a kind has no number, later ones are numbered', () => {
  const list = [];
  for (const device of ['Android phone', 'iPhone', 'Android phone', 'Android phone']) {
    list.push({ device, number: nextNumber(list, device) });
  }
  assert.deepEqual(list.map(rowName), ['Android phone', 'iPhone', 'Android phone 2', 'Android phone 3']);
});

test('numbers stay with their row and a free number is used again', () => {
  const list = [
    { device: 'iPhone', number: 1 },
    { device: 'iPhone', number: 2 },
    { device: 'iPhone', number: 3 },
  ];
  const left = list.filter((v) => v.number !== 2); // "iPhone 2" left
  assert.deepEqual(left.map(rowName), ['iPhone', 'iPhone 3']);
  assert.equal(nextNumber(left, 'iPhone'), 2);
  assert.equal(nextNumber(left, 'Mac'), 1);
});

test('clock time is HH:MM, 24-hour, local', () => {
  assert.equal(clockTime(new Date(2026, 10, 21, 9, 5, 59).getTime()), '09:05');
  assert.equal(clockTime(new Date(2026, 10, 21, 14, 30).getTime()), '14:30');
  assert.equal(clockTime(new Date(2026, 10, 21, 0, 0).getTime()), '00:00');
});

test('the join time comes from the server, so a page reload keeps it', () => {
  const joined = new Date(2026, 10, 21, 14, 2).getTime();
  const now = new Date(2026, 10, 21, 14, 20).getTime();
  assert.equal(joinTime(joined, now), joined);
  assert.equal(clockTime(joinTime(joined, now)), '14:02');
});

test('without a server join time the page uses its own clock', () => {
  const now = new Date(2026, 10, 21, 14, 20).getTime();
  for (const missing of [undefined, null, 0, -5, NaN, Infinity, '1763733720000', 1.5e300]) {
    assert.equal(joinTime(missing, now), now, String(missing));
  }
});

test('row details show the check code and the join time', () => {
  const joined = new Date(2026, 10, 21, 14, 5).getTime();
  assert.equal(rowDetails({ code: 482, joined }), 'Code 482 · joined 14:05');
  assert.equal(rowDetails({ joined }), 'Joined 14:05'); // an older server without codes
});

test('the waiting notification appears when someone new waits while the page is away', () => {
  assert.equal(waitingNoteStep({ before: 0, count: 1, showing: false, away: true }), 'show');
  assert.equal(waitingNoteStep({ before: 0, count: 1, showing: false, away: false }), 'none');
  assert.equal(waitingNoteStep({ before: 1, count: 2, showing: false, away: true }), 'show');
});

test('a shown notification follows the count and closes at zero', () => {
  assert.equal(waitingNoteStep({ before: 2, count: 3, showing: true, away: false }), 'show');
  assert.equal(waitingNoteStep({ before: 3, count: 1, showing: true, away: false }), 'show'); // allowed or denied
  assert.equal(waitingNoteStep({ before: 1, count: 0, showing: true, away: true }), 'close'); // Allow, Deny or leave
  assert.equal(waitingNoteStep({ before: 2, count: 2, showing: true, away: true }), 'none');
  assert.equal(waitingNoteStep({ before: 2, count: 1, showing: false, away: true }), 'none'); // fewer: no new alert
  assert.equal(waitingNoteStep({ before: 1, count: 0, showing: false, away: true }), 'none');
  assert.equal(waitingTitle(1), '1 waiting');
  assert.equal(waitingTitle(4), '4 waiting');
});

test('whole screen means displaySurface monitor', () => {
  const track = (settings) => ({ getSettings: () => settings });
  assert.equal(isWholeScreen(track({ displaySurface: 'monitor' })), true);
  assert.equal(isWholeScreen(track({ displaySurface: 'window' })), false);
  assert.equal(isWholeScreen(track({ displaySurface: 'browser' })), false);
  assert.equal(isWholeScreen(track({})), false); // Firefox and the test picture don't say
  assert.equal(isWholeScreen(null), false);
  assert.equal(isWholeScreen({}), false);
  assert.equal(isWholeScreen({ getSettings: () => { throw new Error('ended'); } }), false);
});

test('the status line says what is going on in one or two words', () => {
  assert.equal(statusText({ sharing: false, paused: false, recording: false }), 'Not sharing');
  assert.equal(statusText({ sharing: true, paused: false, recording: false }), 'Sharing');
  assert.equal(statusText({ sharing: true, paused: false, recording: true }), 'Sharing · recording');
  assert.equal(statusText({ sharing: true, paused: true, recording: true }), 'Paused');
  assert.equal(statusText({ sharing: true, paused: true, recording: false }), 'Paused');
  assert.equal(statusText({ sharing: false, paused: true, recording: true }), 'Not sharing'); // nothing to pause
});

test('a page waiting while another one is the sharer says so in its status line', () => {
  // "Not sharing" made Use this one look harmless, though it cuts everyone watching the other page.
  assert.equal(statusText({ sharing: false, paused: false, recording: false, elsewhere: true }), 'Sharing in another window or tab');
  assert.equal(statusText({ sharing: false, paused: false, recording: false, elsewhere: false }), 'Open in another window or tab');
  // Not waiting: as before.
  assert.equal(statusText({ sharing: false, paused: false, recording: false, elsewhere: null }), 'Not sharing');
  assert.equal(statusText({ sharing: true, paused: false, recording: false, elsewhere: undefined }), 'Sharing');
});

test('viewer rows use short states', () => {
  const sharing = true;
  assert.deepEqual(rowState({ state: 'waiting' }, sharing), { text: 'Waiting', cls: 'waiting' });
  assert.deepEqual(rowState({ state: 'connecting' }, sharing), { text: 'Connecting', cls: 'connecting' });
  assert.deepEqual(rowState({ state: 'watching', sample: null }, sharing), { text: 'Watching', cls: 'watching' });
  assert.deepEqual(rowState({ state: 'failed' }, sharing), { text: "Can't connect", cls: 'failed' });
  assert.deepEqual(rowState({ state: 'pending' }, sharing), { text: 'Wants to watch', cls: 'pending' });
});

test('a weak connection shows as Weak signal, and nothing but Waiting while not sharing', () => {
  const weak = { loss: 0.2, rtt: 0.4 };
  assert.deepEqual(rowState({ state: 'watching', sample: weak }, true), { text: 'Weak signal', cls: 'weak' });
  assert.deepEqual(rowState({ state: 'watching', sample: weak }, false), { text: 'Waiting', cls: 'waiting' });
  assert.deepEqual(rowState({ state: 'failed' }, false), { text: 'Waiting', cls: 'waiting' });
  assert.deepEqual(rowState({ state: 'pending' }, false), { text: 'Wants to watch', cls: 'pending' });
});

test('the numbers go in the row tooltip, not in the row', () => {
  const sample = { width: 1280, height: 720, fps: 30, bitrate: 4_100_000, loss: 0, rtt: 0.01 };
  assert.equal(rowTooltip({ state: 'watching', sample, mode: 'auto' }), '1280×720 · 30 fps · 4.1 Mbps');
  assert.equal(rowTooltip({ state: 'watching', sample, mode: 'saver' }), '1280×720 · 30 fps · 4.1 Mbps · data saver');
  assert.equal(rowTooltip({ state: 'watching', sample: null, mode: 'auto' }), '');
  assert.equal(rowTooltip({ state: 'connecting', sample, mode: 'auto' }), '');
});

test('device names get the right article', () => {
  assert.equal(withArticle('Android phone'), 'An Android phone');
  assert.equal(withArticle('iPhone'), 'An iPhone');
  assert.equal(withArticle('iPad'), 'An iPad');
  assert.equal(withArticle('Windows PC'), 'A Windows PC');
  assert.equal(withArticle('Mac'), 'A Mac');
  assert.equal(withArticle('Linux PC'), 'A Linux PC');
  assert.equal(withArticle('Device'), 'A device');
  assert.equal(withArticle(''), 'A device');
  assert.equal(withArticle(undefined), 'A device');
});

test('one plain line for a refused device', () => {
  const time = new Date(2026, 10, 21, 14, 5).getTime();
  assert.equal(
    refusalLine({ time, device: 'Android phone', reason: 'too-many-tabs' }),
    "An Android phone couldn't join: too many tabs (14:05)",
  );
  assert.equal(refusalLine({ time, device: 'Mac', reason: 'too-many-tries' }), "A Mac couldn't join: too many tries (14:05)");
  assert.equal(refusalLine({ time, device: 'iPhone', reason: 'something-new' }), "An iPhone couldn't join (14:05)");
  assert.equal(refusalLine(null), '');
  assert.equal(refusalLine({ device: 'Mac', reason: 'too-many-tabs' }), "A Mac couldn't join: too many tabs");
});

test('the newest refusal is the last one the server lists', () => {
  const a = { time: 1, device: 'Mac', reason: 'too-many-tabs' };
  const b = { time: 2, device: 'iPhone', reason: 'too-many-tries' };
  assert.equal(newestRefusal([a, b]), b);
  assert.equal(newestRefusal([]), null);
  assert.equal(newestRefusal(undefined), null);
  assert.equal(newestRefusal('junk'), null);
  assert.equal(newestRefusal([a, 'junk']), a);
});

test("Porthole's own window is the page opened with ?window", () => {
  assert.equal(inAppWindow('?window'), true);
  assert.equal(inAppWindow('?test=1&window'), true);
  assert.equal(inAppWindow('?window=1'), true);
  assert.equal(inAppWindow(''), false);
  assert.equal(inAppWindow('?test=1'), false);
  assert.equal(inAppWindow('?windows'), false);
});

test('"Open in my browser" shows only in Porthole\'s own window, before sharing', () => {
  assert.equal(browserHintShown({ appWindow: true, sharing: false, inactive: false }), true);
  assert.equal(browserHintShown({ appWindow: false, sharing: false, inactive: false }), false);
  assert.equal(browserHintShown({ appWindow: true, sharing: true, inactive: false }), false);
  assert.equal(browserHintShown({ appWindow: true, sharing: false, inactive: true }), false);
});

// User agents of the browsers a sharer page runs in (the app window is always Chrome, Edge, Chromium or Brave).
const UA = {
  chromeLinux: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
  edgeLinux: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0',
  chromeWindows: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
  edgeWindows: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0',
  chromeMac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
  chromeOs: 'Mozilla/5.0 (X11; CrOS x86_64 16181.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
  firefoxLinux: 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0',
  firefoxWindows: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0',
  safari: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Safari/605.1.15',
  gnomeWeb: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
};

test('the browser and system are told apart as far as sound goes', () => {
  assert.deepEqual(soundPlace(UA.chromeLinux), { browser: 'chrome', system: 'linux' });
  assert.deepEqual(soundPlace(UA.edgeWindows), { browser: 'chrome', system: 'windows' });
  assert.deepEqual(soundPlace(UA.chromeMac), { browser: 'chrome', system: 'mac' });
  assert.deepEqual(soundPlace(UA.chromeOs), { browser: 'chrome', system: '' }); // says X11, but isn't a Linux desktop
  assert.deepEqual(soundPlace(UA.firefoxLinux), { browser: 'firefox', system: 'linux' });
  assert.deepEqual(soundPlace(UA.safari), { browser: 'safari', system: 'mac' });
  assert.deepEqual(soundPlace(UA.gnomeWeb), { browser: 'safari', system: 'linux' }); // WebKit, like Safari
  assert.deepEqual(soundPlace(''), { browser: '', system: '' });
  assert.deepEqual(soundPlace(undefined), { browser: '', system: '' });
});

test('the no-sound note names only what this browser offers on this system', () => {
  const cant = "No sound is shared. This browser can't share sound.";
  const tab = 'No sound is shared. Use Change, pick a tab and turn on "Also share tab audio".';
  const system = 'No sound is shared. Use Change, pick a screen and turn on "Also share system audio".';
  const inBrowser = 'No sound is shared. To share a tab and its sound, stop sharing and use Open in my browser.';
  // Firefox and Safari can't share sound at all.
  for (const ua of [UA.firefoxLinux, UA.firefoxWindows, UA.safari, UA.gnomeWeb]) assert.equal(audioHint(false, ua), cant, ua);
  // Chrome and Edge share a tab's sound on Linux and a Mac (no system sound there); the whole system's on Windows.
  for (const ua of [UA.chromeLinux, UA.edgeLinux, UA.chromeMac]) assert.equal(audioHint(false, ua), tab, ua);
  for (const ua of [UA.chromeWindows, UA.edgeWindows]) assert.equal(audioHint(false, ua), system, ua);
  // Porthole's own window can't see the browser's tabs: on Linux and a Mac their sound needs the page in the browser,
  // while on Windows the system's sound works right there.
  for (const ua of [UA.chromeLinux, UA.edgeLinux, UA.chromeMac]) assert.equal(audioHint(true, ua), inBrowser, ua);
  for (const ua of [UA.chromeWindows, UA.edgeWindows]) assert.equal(audioHint(true, ua), system, ua);
  // Anything else: both of Chrome's options, as before.
  const both = 'No sound is shared. Use Change and turn on "Also share tab audio" or "Also share system audio".';
  assert.equal(audioHint(false, UA.chromeOs), both);
  assert.equal(audioHint(false, ''), both);
  assert.equal(audioHint(true, ''), inBrowser);
  for (const ua of Object.values(UA)) {
    for (const appWindow of [false, true]) {
      const note = audioHint(appWindow, ua);
      assert.ok(note.length <= 100, `too long: ${note}`);
      assert.doesNotMatch(note, /"Share tab audio"|"Share system audio"/, 'Chrome says "Also share ..."');
    }
  }
});

test('notifications are asked for once, with Ask on, while the browser has no answer', () => {
  assert.equal(askToNotify({ ask: true, permission: 'default', askedBefore: false }), true);
  // Ask off: nobody waits, so nothing to tell about.
  assert.equal(askToNotify({ ask: false, permission: 'default', askedBefore: false }), false);
  // Answered already: never asked again after a refusal, and no need after a yes.
  assert.equal(askToNotify({ ask: true, permission: 'denied', askedBefore: false }), false);
  assert.equal(askToNotify({ ask: true, permission: 'granted', askedBefore: false }), false);
  // Asked on this page already: a prompt closed without an answer isn't shown again on each Start sharing.
  assert.equal(askToNotify({ ask: true, permission: 'default', askedBefore: true }), false);
  // No notifications in this browser.
  assert.equal(askToNotify({ ask: true, permission: '', askedBefore: false }), false);
});

test('a picker that was only cancelled says nothing', () => {
  for (const name of ['NotAllowedError', 'AbortError']) {
    assert.equal(pickError("Couldn't start sharing", { name, message: 'Permission denied' }), '', name);
    assert.equal(pickError("Couldn't change what's shared", { name, message: 'Permission denied' }), '', name);
  }
});

test('Start sharing and Change give the same cause when the picked screen or window cannot be captured', () => {
  const failed = { name: 'NotReadableError', message: 'Could not start video source' };
  assert.equal(pickError("Couldn't start sharing", failed), "Couldn't start sharing: Could not start video source");
  assert.equal(pickError("Couldn't change what's shared", failed), "Couldn't change what's shared: Could not start video source");
  // No words from the browser: still a plain line, never a bare colon.
  assert.equal(pickError("Couldn't change what's shared", { name: 'NotReadableError', message: '' }), "Couldn't change what's shared: something went wrong");
  assert.equal(pickError("Couldn't change what's shared", null), "Couldn't change what's shared: something went wrong");
});

test('which run of Porthole answers: this one, another one (killed and started again), or none', () => {
  assert.equal(answeringRun({ room: 'abc', sharer_active: true }, 'abc'), 'this');
  assert.equal(answeringRun({ room: 'xyz', sharer_active: false }, 'abc'), 'another');
  // Nothing answered, or not Porthole (a page that isn't its answer, a server that isn't Porthole).
  for (const info of [null, undefined, 'text', {}, { room: '' }, { room: 7 }, []]) {
    assert.equal(answeringRun(info, 'abc'), 'none', JSON.stringify(info));
  }
});

test('a page waiting on another says whether that one shares or is only open', () => {
  assert.equal(busyText(true), 'Sharing is on in another window or tab.');
  assert.equal(busyText(false), 'Porthole is open in another window or tab.');
  assert.equal(busyText(undefined), 'Porthole is open in another window or tab.');
});

// A stand-in stream: tracks that remember being stopped.
function fakeStream(kinds = ['video', 'audio']) {
  const tracks = kinds.map((kind) => ({ kind, stopped: false, stop() { this.stopped = true; } }));
  return { tracks, getTracks: () => tracks.slice() };
}

// A picker that stays open until the test answers it, like a person still choosing.
function openPicker() {
  let answer;
  const promise = new Promise((resolve) => { answer = resolve; });
  return { promise, answer };
}

test('a pick with no stop meanwhile is kept', async () => {
  const picks = newPickGuard();
  const picker = openPicker();
  const pick = picks.begin();
  const stream = fakeStream();
  picker.answer(stream);
  assert.equal(pick.keep(await picker.promise), stream);
  assert.equal(pick.current(), true);
  assert.deepEqual(stream.tracks.map((t) => t.stopped), [false, false]);
});

test('Stop while the picker is open wins: the picked stream is ended, not shared', async () => {
  const picks = newPickGuard();
  const picker = openPicker();
  const pick = picks.begin(); // Change opened the picker
  picks.stopped(); // the mini window's Stop, while the picker is still open
  const stream = fakeStream();
  picker.answer(stream); // the person picks a screen after all
  assert.equal(pick.keep(await picker.promise), null);
  assert.equal(pick.current(), false);
  assert.deepEqual(stream.tracks.map((t) => t.stopped), [true, true]);
});

test('a stop after the pick came back still wins (Change waits for the recording to close)', async () => {
  const picks = newPickGuard();
  const pick = picks.begin();
  const stream = fakeStream();
  assert.equal(pick.keep(stream), stream);
  picks.stopped(); // while Change waits for the old recording to finish
  assert.equal(pick.keep(stream), null);
  assert.deepEqual(stream.tracks.map((t) => t.stopped), [true, true]);
});

test('a stop before a pick began does not touch that pick', async () => {
  const picks = newPickGuard();
  picks.stopped();
  const pick = picks.begin(); // Start sharing again later
  const stream = fakeStream(['video']);
  assert.equal(pick.keep(stream), stream);
  assert.equal(stream.tracks[0].stopped, false);
});

test('each pick has its own check', () => {
  const picks = newPickGuard();
  const first = picks.begin();
  picks.stopped();
  const second = picks.begin();
  assert.equal(first.current(), false);
  assert.equal(second.current(), true);
});

test('ending tracks never throws', () => {
  assert.doesNotThrow(() => endTracks(null));
  assert.doesNotThrow(() => endTracks({}));
  const after = { stopped: false, stop() { this.stopped = true; } };
  assert.doesNotThrow(() => endTracks({ getTracks: () => [{ stop() { throw new Error('gone'); } }, after] }));
  assert.equal(after.stopped, true); // one track that fails doesn't keep the others running
  const stream = fakeStream();
  endTracks(stream);
  assert.deepEqual(stream.tracks.map((t) => t.stopped), [true, true]);
});

// ---------- screen readers ----------

const phone = (id, number, code) => ({ id, device: 'Android phone', number, code });

test('a device that starts waiting is announced once, with its code', () => {
  let news = waitingNews(new Set(), [phone('a', 1, 482)]);
  assert.equal(news.text, 'Android phone wants to watch. Code 482.');
  // The page redraws every 2 s: the same device is not announced again.
  news = waitingNews(news.announced, [phone('a', 1, 482)]);
  assert.equal(news.text, '');
  news = waitingNews(news.announced, [phone('a', 1, 482), phone('b', 2, 517)]);
  assert.equal(news.text, 'Android phone 2 wants to watch. Code 517.');
  news = waitingNews(news.announced, [phone('a', 1, 482), phone('b', 2, 517)]);
  assert.equal(news.text, '');
});

test('devices that start waiting together are announced together', () => {
  const news = waitingNews(new Set(), [phone('a', 1, 482), { id: 'c', device: 'iPhone', number: 1, code: null }]);
  assert.equal(news.text, 'Android phone wants to watch. Code 482. iPhone wants to watch.');
  assert.deepEqual([...news.announced].sort(), ['a', 'c']);
});

test('devices that no longer wait are forgotten', () => {
  let news = waitingNews(new Set(), [phone('a', 1, 482)]);
  news = waitingNews(news.announced, []); // allowed, denied or gone
  assert.equal(news.text, '');
  assert.equal(news.announced.size, 0);
  assert.equal(waitingNews(new Set(['x']), 'not a list').text, '');
});

test('after Allow, focus goes to the next waiting device, then to the allowed row', () => {
  // Two were waiting; the first was allowed.
  assert.deepEqual(
    focusAfterChange({ list: 'waiting', index: 0, viewerId: 'a', waitingIds: ['b'], watchingIds: ['x', 'a'] }),
    { kind: 'row', id: 'b' },
  );
  // The last one waiting was allowed: its row, now under Watching.
  assert.deepEqual(
    focusAfterChange({ list: 'waiting', index: 0, viewerId: 'a', waitingIds: [], watchingIds: ['x', 'a'] }),
    { kind: 'row', id: 'a' },
  );
});

test('after Deny or Remove, focus goes to the row now in that place, else the one before', () => {
  assert.deepEqual(
    focusAfterChange({ list: 'viewers', index: 1, viewerId: 'b', waitingIds: [], watchingIds: ['a', 'c'] }),
    { kind: 'row', id: 'c' },
  );
  assert.deepEqual(
    focusAfterChange({ list: 'viewers', index: 2, viewerId: 'c', waitingIds: [], watchingIds: ['a', 'b'] }),
    { kind: 'row', id: 'b' },
  );
  // The list is empty now (Deny all, or the last one removed): the Watching heading.
  assert.deepEqual(focusAfterChange({ list: 'waiting', index: 0, viewerId: null, waitingIds: [], watchingIds: ['a'] }), { kind: 'heading' });
  assert.deepEqual(focusAfterChange({ list: 'viewers', index: 0, viewerId: 'a', waitingIds: [], watchingIds: [] }), { kind: 'heading' });
});

test('a row that is still in its list keeps the focus', () => {
  assert.deepEqual(
    focusAfterChange({ list: 'viewers', index: 0, viewerId: 'b', waitingIds: [], watchingIds: ['a', 'b'] }),
    { kind: 'row', id: 'b' },
  );
});

test('a button that hides passes the focus to the one that takes its place', () => {
  assert.equal(replacementFocus('start'), 'stop');
  for (const id of ['pause', 'change', 'record', 'stop', 'mini', 'takeover']) assert.equal(replacementFocus(id), 'start', id);
  for (const id of ['copy', 'quit', '', undefined]) assert.equal(replacementFocus(id), null, String(id));
});

// ---------- what a focused row reads, and news said again ----------

test('a focused row reads the device, its code and its state, and nothing that changes every 2 s', () => {
  const watching = { id: 'a', device: 'Linux PC', number: 2, code: 717, state: 'watching', sample: { width: 1280, height: 720, fps: 30, bitrate: 207000, loss: 0, rtt: 0 } };
  assert.equal(rowLabel(watching, true), 'Linux PC 2, code 717, Watching');
  assert.equal(rowLabel({ ...watching, sample: { ...watching.sample, loss: 0.2 } }, true), 'Linux PC 2, code 717, Weak signal');
  assert.equal(rowLabel({ ...watching, state: 'pending', number: 1 }, true), 'Linux PC, code 717, Wants to watch');
  assert.equal(rowLabel({ ...watching, code: null }, false), 'Linux PC 2, Waiting');
  assert.doesNotMatch(rowLabel(watching, true), /fps|kbps|Mbps|Remove/);
});

// A fake page: write() records what the live region is given, later() keeps the step for flush().
function fakeRegion() {
  const writes = [];
  let pending = [];
  const say = newsWriter(
    (text) => writes.push(text),
    (fn) => pending.push(fn),
  );
  const flush = () => {
    const steps = pending;
    pending = [];
    for (const step of steps) step();
  };
  return { writes, say, flush };
}

test('news is written after the region is cleared, so the same words said again are read again', () => {
  const region = fakeRegion();
  const note = 'Viewers can see your notifications.';
  region.say(note);
  region.flush();
  // Sharing stopped, then started again: the note shows again, with nothing said in between.
  region.say(note);
  region.flush();
  assert.deepEqual(region.writes, ['', note, '', note]);
});

test('news given in the same moment is said together, once', () => {
  const region = fakeRegion();
  region.say('Android phone wants to watch. Code 482.');
  region.say('No sound is shared.');
  region.flush();
  assert.deepEqual(region.writes, ['', 'Android phone wants to watch. Code 482. No sound is shared.']);
});

test('nothing to say writes nothing', () => {
  const region = fakeRegion();
  region.say('');
  region.say(null);
  region.flush();
  assert.deepEqual(region.writes, []);
});

test('a line is news when it appears or changes after the page drew it, not when it comes with the page', () => {
  assert.equal(lineNews(undefined, 'This PC has no network.'), ''); // drawn with the page
  assert.equal(lineNews('', 'This PC has no network.'), 'This PC has no network.');
  assert.equal(lineNews('Name taken.', 'Port busy.'), 'Port busy.');
  assert.equal(lineNews('Port busy.', 'Port busy.'), '');
  assert.equal(lineNews('Port busy.', ''), ''); // gone is not news
});

test('a saved recording names its file and offers the folder', () => {
  assert.deepEqual(recordingOutcome({ name: 'Screen share 2026-11-24 10-00-00.mp4', bytes: 1234 }), {
    line: 'Saved "Screen share 2026-11-24 10-00-00.mp4".',
    folder: true,
    error: '',
  });
});

test('a recording with nothing in it says so, with no file to open', () => {
  // Started while paused and stopped before Resume: Porthole deleted the empty file.
  assert.deepEqual(recordingOutcome({ empty: true, name: 'Screen share 2026-11-24 10-00-00.webm', bytes: 0 }), {
    line: 'Nothing was recorded.',
    folder: false,
    error: '',
  });
});

test('a recording a full disk cut short keeps the Saved line and says why it stopped', () => {
  assert.deepEqual(recordingOutcome({ name: 'Screen share.mp4', bytes: 9, stopped: 'the disk is full' }), {
    line: 'Saved "Screen share.mp4".',
    folder: true,
    error: 'The recording stopped: the disk is full. What was recorded until then is saved.',
  });
  assert.equal(
    recordingOutcome({ name: 'a.webm', bytes: 9, stopped: "the disk can't be written to" }).error,
    "The recording stopped: the disk can't be written to. What was recorded until then is saved.",
  );
  // The disk was full before anything was saved: nothing to keep.
  assert.deepEqual(recordingOutcome({ empty: true, name: 'a.webm', bytes: 0, stopped: 'the disk is full' }), {
    line: 'Nothing was recorded.',
    folder: false,
    error: 'The recording stopped: the disk is full.',
  });
});

test('a continued recording with nothing in it shows the part saved before Change', () => {
  // Record, Pause, Change (continues in a new file), Stop recording before Resume: the new file had nothing in it
  // and was deleted, but the part before Change was saved.
  const earlier = { name: 'Screen share 1.mp4', bytes: 900 };
  const kept = keptRecording({ empty: true, name: 'Screen share 2.mp4', bytes: 0 }, earlier);
  assert.deepEqual(kept, earlier);
  assert.notEqual(kept, earlier); // a new answer, so it is said again
  assert.deepEqual(recordingOutcome(kept), { line: 'Saved "Screen share 1.mp4".', folder: true, error: '' });
  // A full disk stopped the new file before anything went in: why, and that what came before is saved.
  assert.equal(
    recordingOutcome(keptRecording({ empty: true, name: 'b.mp4', bytes: 0, stopped: 'the disk is full' }, earlier)).error,
    'The recording stopped: the disk is full. What was recorded until then is saved.',
  );
  assert.deepEqual(keptRecording({ empty: true, name: 'b.mp4', bytes: 0 }, { ...earlier, stopped: 'the disk is full' }), earlier);
  // Something in the new file: that file is the one shown.
  const own = { name: 'Screen share 2.mp4', bytes: 50 };
  assert.equal(keptRecording(own, earlier), own);
  // Nothing before it either (no earlier part, or one with nothing in it): nothing was recorded.
  const empty = { empty: true, name: 'c.mp4', bytes: 0 };
  assert.equal(keptRecording(empty, null), empty);
  assert.equal(keptRecording(empty, undefined), empty);
  assert.equal(keptRecording(empty, { empty: true, name: 'd.mp4', bytes: 0 }), empty);
  assert.equal(keptRecording(null, earlier), null);
});

test('no recording yet: nothing to say', () => {
  assert.deepEqual(recordingOutcome(null), { line: '', folder: false, error: '' });
});

// ---------- a device's row handed to its new connection ----------

test('a renamed entry keeps its place among the others', () => {
  const map = new Map([['v-1', 'a'], ['v-2', 'b'], ['v-3', 'c']]);
  assert.equal(renameKey(map, 'v-2', 'v-9', 'B'), map); // the same map, changed in place
  assert.deepEqual([...map], [['v-1', 'a'], ['v-9', 'B'], ['v-3', 'c']]);
});

test('renaming a key that is not there changes nothing', () => {
  const map = new Map([['v-1', 'a']]);
  renameKey(map, 'v-7', 'v-9', 'x');
  assert.deepEqual([...map], [['v-1', 'a']]);
});

test("the new connection keeps the row's number, so its name stays", () => {
  // "iPhone 2" stays "iPhone 2"; before, the reconnected phone came back as another row, "iPhone 3".
  const viewers = new Map([
    ['v-1', { id: 'v-1', device: 'iPhone', number: 1 }],
    ['v-2', { id: 'v-2', device: 'iPhone', number: 2 }],
  ]);
  const old = viewers.get('v-2');
  renameKey(viewers, 'v-2', 'v-5', { id: 'v-5', device: 'iPhone', number: old.number });
  assert.deepEqual([...viewers.values()].map(rowName), ['iPhone', 'iPhone 2']);
  assert.equal(nextNumber(viewers.values(), 'iPhone'), 3);
});

test('an approval is for the token from the address it was allowed from; a denial for the token wherever it is', () => {
  const phone = { id: 'v-0a1b2c3d', client: 'Xy7_client-01', address: '192.168.1.30' };
  assert.equal(approvalKey(phone), 'Xy7_client-01@192.168.1.30');
  assert.notEqual(approvalKey({ ...phone, address: '192.168.1.31' }), approvalKey(phone)); // a new address: asked again
  assert.equal(denialKey(phone), 'Xy7_client-01');
  assert.equal(denialKey({ ...phone, address: '192.168.1.31' }), denialKey(phone)); // denied wherever it comes from
  const noToken = { id: 'v-0a1b2c3d', client: '', address: '192.168.1.30' }; // no storage: its connection only
  assert.equal(approvalKey(noToken), 'v-0a1b2c3d');
  assert.equal(denialKey(noToken), 'v-0a1b2c3d');
});

