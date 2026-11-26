import { Signal, createPeer } from '/static/signal.js';
import { LEVELS, newTuner, decide, senderSample } from '/static/quality.js';
import { loadSettings, withEasy, withAsk, easyLine, easyView } from '/static/settings.js';
import {
  nextNumber,
  renameKey,
  rowName,
  rowDetails,
  joinTime,
  waitingNoteStep,
  waitingTitle,
  isWholeScreen,
  statusText,
  rowState,
  rowTooltip,
  refusalLine,
  newestRefusal,
  inAppWindow,
  browserHintShown,
  audioHint,
  newPickGuard,
  waitingNews,
  focusAfterChange,
  replacementFocus,
  newsWriter,
  lineNews,
  rowLabel,
  recordingOutcome,
  keptRecording,
  askToNotify,
  busyText,
  pickError,
  answeringRun,
  approvalKey,
  denialKey,
} from '/static/share-helpers.js';
import { readTheme, saveTheme, applyTheme, resolveTheme } from '/static/theme.js';

const $ = (id) => document.getElementById(id);
const testMode = new URLSearchParams(location.search).has('test');
const appWindow = inAppWindow(location.search); // Porthole's own window, not a browser tab
const canShare = testMode || Boolean(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
const CANT_SHARE = "This browser can't share the screen. Use Chrome, Edge or Firefox on this PC.";
const TITLE = 'Porthole';
const GONE = "Porthole isn't running any more. You can close this page.";
const RESTARTED = 'Porthole was restarted. Use its new page, or reload this one.';
// This page, as long as it is open: its event stream comes back with it after a network blip, and its recording
// belongs to it (a reloaded page has a new one, so Porthole closes a recording it can't finish).
const PAGE = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, '0')).join('');
const miniSupported = 'documentPictureInPicture' in window;

let settings = loadSettings(readSavedSettings());
let easyState = null; // the server's easy address state, as last reported
let serverQueue = Promise.resolve();
let room = '';
let signal = null;
let stream = null; // what is being shared right now
let paused = null; // { track, preview, stop } while paused: viewers get a "Paused" card instead
let stopTestPicture = null;
let inactive = false;
let controlsOn = true; // off while another window or tab shares, and once this page has let go
let elsewhere = null; // while another window or tab is the sharer: whether it shares (true) or is only open (false)
let approved = new Set(); // device keys allowed in this run
let denied = new Set(); // device keys denied or removed in this run
let recording = null;
let startingRecording = false;
let lastSaved = null; // Porthole's answer when the last recording ended: the file saved, or `empty`
let joinUrls = null; // the viewer links shown, as JSON, to notice when the PC's address changes
let currentLink = '';
let waitingNote = null; // the one "N waiting" notification
let waitingBefore = 0;
let notifyAsked = false; // this page asked for notifications once: a prompt closed without an answer isn't shown again
let connecting = false; // reading this run's choices before connecting ("Use this one" pressed twice connects once)
let checkingGone = false; // checking whether Porthole is still there
let retaking = false; // connecting again after a refusal that found no sharer page (once, until the stream opens)
let runEnded = false; // the page ended because Porthole stopped or went away: it watches for a new run (started again)
let theme = 'auto';
let mini = null; // the mini controls window, where the browser has one
let announcedWaiting = new Set(); // waiting devices already announced to screen readers
let hintsShown = {}; // the notes that showed at the last redraw, so a new one is announced once
let announcedSaved = null; // the saved recording last announced
let refusalDrawn = false; // the first refusal line comes with the page, not as news
const easyLines = {}; // the Easy address error and note as last drawn (none before the first draw)
let noNetworkDrawn; // the "no network" line as last drawn (undefined before the first draw)
const viewers = new Map(); // viewer id -> viewer
const picks = newPickGuard(); // a stop while the screen picker is open wins over what is picked

// ---------- small helpers ----------

// Writes only a change: screen readers may read a live line out again on every write.
function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

// Screen readers: one quiet live region (#say), always on the page, given only news. It is cleared and
// written a moment later, so the same news again (a note back after Stop and Start) is read again.
const announce = newsWriter(
  (text) => ($('say').textContent = text),
  (step) => setTimeout(step, 100),
);

// Whether the last input was a click or a touch. Focus moved after one (Allow clicked, its row
// moved to Watching) doesn't scroll the page: a mouse user keeps their place, while a keyboard user
// is shown where the focus went.
let pointerLast = false;
document.addEventListener('pointerdown', () => (pointerLast = true), true);
document.addEventListener('keydown', () => (pointerLast = false), true);

// Moves keyboard focus to the element with this id, if it can take it.
function focusOn(id) {
  const target = id && $(id);
  if (target && !target.disabled && !target.closest('[hidden]')) target.focus({ preventScroll: pointerLast });
}

function storage() {
  try {
    return window.localStorage;
  } catch {
    return null; // blocked: nothing is remembered
  }
}

function readSavedSettings() {
  try {
    return localStorage.getItem('porthole-settings');
  } catch {
    return null; // private window: defaults
  }
}

// `change` is what was changed here (Ask and Easy address go together), done to this browser's own saved settings
// too: after taking over this run's from Porthole, a switch changed here is saved without the ones taken over.
function saveSettings(change) {
  try {
    localStorage.setItem('porthole-settings', JSON.stringify(change(loadSettings(readSavedSettings()))));
  } catch {
    // private window: Porthole still keeps them for this run
  }
  keepChoices();
}

function loadSet(name) {
  try {
    return new Set(JSON.parse(localStorage.getItem(`${name}-${room}`) || '[]'));
  } catch {
    return new Set();
  }
}

// This run's approvals and switches as Porthole keeps them (the page that shares reports each change): a page that
// takes over, also one in another browser after Open in my browser, starts from those instead of what its own browser
// saved. Used for this run only: the browser's own saved settings stay as they were, and a switch changed here is
// saved there on its own (saveSettings), without the ones taken over.
// False when Porthole has none yet (no page has shared in this run).
function useChoices(choices) {
  if (!choices || typeof choices !== 'object') return false;
  settings = loadSettings(JSON.stringify(choices.settings));
  approved = new Set(Array.isArray(choices.approved) ? choices.approved : []);
  denied = new Set(Array.isArray(choices.denied) ? choices.denied : []);
  return true;
}

// Before connecting, and so before "Use this one" takes over: the other page may have allowed, denied or removed
// devices and changed the switches while this one waited. Porthole's copy first, else what this browser saved.
async function readChoicesAgain() {
  const info = await readInfo();
  if (!(info && info.room === room && useChoices(info.choices)) && storage()) {
    settings = loadSettings(readSavedSettings());
    approved = loadSet('porthole-approved');
    denied = loadSet('porthole-denied');
  }
  render();
}

// Porthole keeps this run's approvals and switches for a page that takes over. Only the page that shares reports
// them (Porthole checks its id): a waiting or replaced page must not undo what it allowed or denied.
function keepChoices() {
  if (!signal || !signal.id || inactive || !controlsOn) return;
  tellServer('/api/choices', { id: signal.id, settings: { ...settings }, approved: [...approved], denied: [...denied] });
}

function saveSets() {
  try {
    localStorage.setItem(`porthole-approved-${room}`, JSON.stringify([...approved]));
    localStorage.setItem(`porthole-denied-${room}`, JSON.stringify([...denied]));
  } catch {
    // keep them in memory (and with Porthole) only
  }
  keepChoices();
}


// Written only on a change: the error line is an alert, read out on every write.
function showError(text) {
  setText($('error'), text);
  $('error').hidden = !text;
}

// Normal endings (quit, moved to another window or tab) are plain notes, not errors.
function showNotice(text) {
  setText($('notice'), text);
  $('notice').hidden = !text;
  announce(text);
}

async function api(path, body = '', type = 'application/json') {
  // The room says which run of Porthole this page belongs to: a page left from an earlier run can't drive this one.
  const headers = { 'Content-Type': type, ...(room ? { 'X-Porthole-Room': room } : {}) };
  const res = await fetch(path, { method: 'POST', headers, body });
  let data = null;
  try {
    data = await res.json();
  } catch {
    // 204 has no body
  }
  return { ok: res.ok, status: res.status, data };
}

function apiError(result) {
  return (result.data && result.data.error) || `error ${result.status}`;
}

// Porthole's local actions (Quit, Record, Open folder, Open in my browser). A request that got no answer at all (the
// browser's "Failed to fetch") may mean Porthole has gone, crashed or killed: then the page ends and this gives null.
// If Porthole is still there, the failure is an error like any other.
async function act(path, body = '') {
  try {
    return await api(path, body);
  } catch (err) {
    if (await portholeGone()) return null;
    return { ok: false, status: 0, data: { error: err.message } };
  }
}

// Whether Porthole has gone: no answer now, nor a second later (so a moment's hiccup doesn't end the page). Once gone,
// the page has ended; true then.
async function portholeGone() {
  if (!inactive && ((await readInfo()) || (await later(1000, readInfo)))) return false;
  endGone();
  return true;
}

// Lost contact: checked at once (one check at a time), instead of "Retrying..." for ever after a crash.
async function noticeGone() {
  if (checkingGone || inactive) return;
  checkingGone = true;
  try {
    await portholeGone();
  } finally {
    checkingGone = false;
  }
}

// Sharing state and the easy address go to the server one at a time, in order.
function tellServer(path, body) {
  const result = serverQueue.then(() => api(path, JSON.stringify(body))).catch(() => null);
  serverQueue = result;
  return result;
}

// "/" sends people to the share only while something is shared, so the server needs to know.
function reportSharing() {
  if (!signal || !signal.id) return; // not the sharer (yet): the hello reports it once this page is
  tellServer('/api/status', { sharing: Boolean(stream), id: signal ? signal.id : null });
}

async function applyEasy() {
  if (inactive || !controlsOn) return;
  const result = await tellServer('/api/discovery', { enabled: settings.easy });
  if (!result) return;
  if (result.ok) easyState = result.data;
  else if (result.status === 503) easyState = null;
  render();
}

// ---------- theme ----------

const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function setTheme(choice, save = true) {
  theme = applyTheme(document.documentElement, choice);
  if (save) saveTheme(storage(), theme);
  for (const input of document.querySelectorAll('input[name="theme"]')) input.checked = input.value === theme;
  // The browser's own bar (and an app window's title bar) matches the page.
  const dark = resolveTheme(theme, Boolean(darkQuery && darkQuery.matches)) === 'dark';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? '#131416' : '#ffffff';
  if (mini) applyTheme(mini.win.document.documentElement, theme);
}

setTheme(readTheme(storage()), false); // before anything else draws

// ---------- what to share ----------

function testPicture() {
  const canvas = document.createElement('canvas');
  canvas.width = 1280;
  canvas.height = 720;
  const ctx = canvas.getContext('2d');
  const picture = canvas.captureStream(30); // first: if it fails, no timer is left drawing a picture nobody can stop
  const started = performance.now();
  const timer = setInterval(() => {
    const t = (performance.now() - started) / 1000;
    ctx.fillStyle = '#1d2433';
    ctx.fillRect(0, 0, 1280, 720);
    ctx.fillStyle = '#4f8cff';
    ctx.fillRect(((t * 300) % 1480) - 200, 300, 200, 120);
    ctx.fillStyle = '#ffffff';
    ctx.font = '600 64px system-ui, sans-serif';
    ctx.fillText('Test picture', 60, 120);
    ctx.font = '48px ui-monospace, monospace';
    ctx.fillText(new Date().toLocaleTimeString(), 60, 620);
  }, 33);
  stopTestPicture = () => clearInterval(timer);
  return picture;
}

async function pickStream() {
  if (testMode) return testPicture();
  const fps = settings.smooth ? 60 : 30;
  return navigator.mediaDevices.getDisplayMedia({
    video: { frameRate: { ideal: fps, max: fps } },
    audio: true,
    // Chrome: "Share this tab instead" swaps the source on the same track, so viewers and the
    // recording carry on; this tab stays out of the list. Other browsers ignore keys they don't know.
    surfaceSwitching: 'include',
    selfBrowserSurface: 'exclude',
    preferCurrentTab: false,
    systemAudio: 'include',
    monitorTypeSurfaces: 'include',
  });
}

function useStream(next) {
  stream = next;
  const video = next.getVideoTracks()[0];
  if (video) {
    // The browser's own "Stop sharing" bar ends the track.
    video.addEventListener('ended', () => {
      if (stream === next) stop();
    });
  }
  if (paused) for (const track of next.getAudioTracks()) track.enabled = false;
  applySmooth();
  showPreview();
}

// The preview shows what viewers see: the Paused card while paused.
function showPreview() {
  $('preview').srcObject = paused ? paused.preview : stream;
}

function applySmooth() {
  const video = stream && stream.getVideoTracks()[0];
  if (video) {
    if ('contentHint' in video) video.contentHint = settings.smooth ? 'motion' : 'detail';
    if (!testMode) {
      const fps = settings.smooth ? 60 : 30;
      video.applyConstraints({ frameRate: { ideal: fps, max: fps } }).catch(() => {});
    }
  }
  for (const viewer of viewers.values()) applyLevel(viewer);
}

async function start() {
  showError('');
  showNotice('');
  const pick = picks.begin();
  let next;
  try {
    const picking = pickStream(); // the screen picker needs this click, so it opens first
    askForNotifications(); // in the same click: some browsers ask only then
    next = await picking;
  } catch (err) {
    const problem = pickError("Couldn't start sharing", err); // nothing when the picker was only cancelled
    if (problem) showError(problem);
    return;
  }
  if (!pick.keep(next)) {
    // Porthole stopped, or sharing moved to another window or tab, while the picker was open: share nothing.
    if (testMode && stopTestPicture) stopTestPicture();
    stopTestPicture = null;
    return;
  }
  useStream(next);
  reportSharing();
  render();
  for (const viewer of viewers.values()) if (viewer.state !== 'pending') offerTo(viewer.id);
}

async function change() {
  showError('');
  const pick = picks.begin();
  let next;
  try {
    next = await pickStream();
  } catch (err) {
    // What was picked can't be captured (or the picker was only cancelled): the current share goes on as it was.
    const problem = pickError("Couldn't change what's shared", err);
    if (problem) showError(problem);
    return;
  }
  if (!pick.keep(next)) return; // Stop was pressed (in the mini window) while the picker was open
  const wasRecording = Boolean(recording);
  if (wasRecording) await stopRecording();
  const earlier = lastSaved; // the part before Change, as Porthole saved it
  if (!pick.keep(next)) return; // stopped while the old recording was closing
  const old = stream;
  const kinds = (s) => s.getTracks().map((t) => t.kind).sort().join(',');
  const sameKinds = old && kinds(old) === kinds(next);
  useStream(next);
  for (const [id, viewer] of viewers) {
    if (viewer.state === 'pending') continue;
    if (sameKinds && viewer.link) {
      for (const sender of viewer.link.pc.getSenders()) {
        if (paused && sender.track && sender.track.kind === 'video') continue; // keeps the Paused card
        const track = sender.track && next.getTracks().find((t) => t.kind === sender.track.kind);
        if (track) sender.replaceTrack(track).catch(() => offerTo(id));
      }
    } else {
      offerTo(id);
    }
  }
  if (old) old.getTracks().forEach((t) => t.stop());
  if (wasRecording) {
    await startRecording();
    if (recording) {
      recording.note = 'Continues in a new file.';
      recording.earlier = earlier; // shown again if the new file ends with nothing in it (paused until the end)
    }
  }
  render();
}

function stop() {
  picks.stopped(); // also while Start's picker is open and nothing is shared yet
  if (!stream) return;
  if (recording) stopRecording();
  stream.getTracks().forEach((t) => t.stop());
  stream = null;
  if (paused) paused.stop();
  paused = null;
  reportSharing();
  if (stopTestPicture) stopTestPicture();
  stopTestPicture = null;
  showPreview();
  for (const [id, viewer] of viewers) {
    closeLink(viewer);
    if (viewer.state !== 'pending') {
      viewer.state = 'waiting';
      signal.send(id, { type: 'stopped' });
    }
  }
  render();
}

// ---------- pause ----------

// A plain "Paused" card (1280×720, about 2 frames a second) that viewers see instead of the screen.
function pausedCard() {
  const canvas = document.createElement('canvas');
  canvas.width = 1280;
  canvas.height = 720;
  const ctx = canvas.getContext('2d');
  const draw = () => {
    ctx.fillStyle = '#131416';
    ctx.fillRect(0, 0, 1280, 720);
    ctx.fillStyle = '#e9eaec';
    ctx.font = '600 72px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Paused', 640, 360);
  };
  draw();
  const track = canvas.captureStream(2).getVideoTracks()[0];
  if ('contentHint' in track) track.contentHint = 'detail';
  const timer = setInterval(draw, 500); // keeps frames coming, so a viewer who joins now gets a picture
  return {
    track,
    preview: new MediaStream([track]),
    stop() {
      clearInterval(timer);
      track.stop();
    },
  };
}

// Viewers hear about a pause too: their page shows a small "Paused" note over the picture, since the card says it only
// in its middle (zoomed in on a corner, a viewer saw a plain dark picture). Only devices that get the picture.
function tellViewers(message) {
  for (const viewer of viewers.values()) if (viewer.link) signal.send(viewer.id, message);
}

function pause() {
  if (!stream || paused) return;
  paused = pausedCard();
  for (const track of stream.getAudioTracks()) track.enabled = false; // silence, not a dropped track
  for (const viewer of viewers.values()) {
    const sender = videoSender(viewer);
    if (sender) sender.replaceTrack(paused.track).catch(() => offerTo(viewer.id));
  }
  tellViewers({ type: 'paused' });
  pauseRecording(); // the paused part is left out of the recording
  showPreview();
  render();
}

async function resume() {
  if (!paused) return;
  const card = paused;
  paused = null;
  if (stream) {
    for (const track of stream.getAudioTracks()) track.enabled = true;
    const real = stream.getVideoTracks()[0];
    const swaps = [];
    for (const viewer of viewers.values()) {
      const sender = videoSender(viewer);
      if (sender && real) swaps.push(sender.replaceTrack(real).catch(() => offerTo(viewer.id)));
    }
    tellViewers({ type: 'resumed' });
    resumeRecording();
    showPreview();
    render();
    await Promise.allSettled(swaps);
  }
  card.stop();
  render();
}

function togglePause() {
  if (paused) resume();
  else pause();
}

// ---------- viewers: approval ----------

// `number`: "Android phone 2" when there are two.
function newViewer(id, device, client, code, joined, number, address) {
  return {
    id,
    device,
    number,
    address: typeof address === 'string' ? address : '', // where it connects from: an approval is for this address too
    code: Number.isInteger(code) ? code : null, // matches the code on the viewer's waiting screen
    joined: joinTime(joined, Date.now()), // the server's time: a reload or reconnect keeps it
    client: client || '',
    link: null,
    state: 'waiting',
    offerN: 0,
    mode: 'auto',
    tuner: newTuner(),
    sample: null,
    appliedLevel: -1,
    restartAt: 0,
    recoverTimer: null,
  };
}

function addViewer(id, device, client, code, joined, address) {
  const viewer = newViewer(id, device, client, code, joined, nextNumber(viewers.values(), device), address);
  viewers.set(id, viewer);
  admit(viewer);
}

// The same device reconnected (a locked phone, a Wi-Fi drop, a reload) and Porthole handed its row to the new
// connection (only for the same device: its token, its address, a row never turned away; see hub.py). The row keeps
// its name, code, join time and place, and its focus; a waiting row isn't announced again. Whether it waits or is let
// in is decided as for any device (admit: by its key, so an approval stays). The old connection is closed by Porthole.
function replaceViewer(old, msg) {
  closeLink(old);
  const viewer = newViewer(msg.id, old.device, msg.client, msg.code, msg.joined, old.number, msg.address); // the same name
  renameKey(viewers, old.id, msg.id, viewer);
  const row = rows.get(old.id);
  if (row) {
    renameKey(rows, old.id, msg.id, row);
    row.item.dataset.id = msg.id;
    for (const button of row.actions.querySelectorAll('button')) button.dataset.id = msg.id;
  }
  if (announcedWaiting.has(old.id)) announcedWaiting.add(msg.id);
  admit(viewer);
}

function admit(viewer) {
  if (settings.ask && !approved.has(approvalKey(viewer))) {
    if (denied.has(denialKey(viewer))) {
      signal.send(viewer.id, { type: 'denied' });
      viewers.delete(viewer.id);
      return;
    }
    viewer.state = 'pending';
    signal.send(viewer.id, { type: 'waiting', reason: 'approval' });
    return;
  }
  letIn(viewer);
}

function letIn(viewer) {
  viewer.state = 'waiting'; // no longer pending, so offerTo will connect it
  if (stream) offerTo(viewer.id);
  else signal.send(viewer.id, { type: 'waiting', reason: 'screen' });
}

// Its other tabs: allowed with it from the same address, sent away with it wherever they are.
const sameKey = (viewer, key) => [...viewers.values()].filter((v) => key(v) === key(viewer));

function allow(viewer) {
  approved.add(approvalKey(viewer));
  denied.delete(denialKey(viewer));
  saveSets();
  for (const v of sameKey(viewer, approvalKey)) if (v.state === 'pending') letIn(v);
}

// A device sent away loses its approvals from every address.
function forgetApprovals(viewer) {
  const mine = approvalKey(viewer);
  const prefix = viewer.client ? `${viewer.client}@` : null;
  for (const key of [...approved]) if (key === mine || (prefix && key.startsWith(prefix))) approved.delete(key);
}

function sendAway(viewer, type) {
  denied.add(denialKey(viewer));
  forgetApprovals(viewer);
  saveSets();
  for (const v of sameKey(viewer, denialKey)) {
    closeLink(v);
    signal.send(v.id, { type });
    viewers.delete(v.id);
  }
}

function setAsk(on) {
  const easyWasOn = settings.easy;
  settings = withAsk(settings, on); // Ask off also turns Easy address off
  saveSettings((saved) => withAsk(saved, on));
  if (on) {
    for (const viewer of viewers.values()) if (viewer.state !== 'pending') approved.add(approvalKey(viewer));
    saveSets();
    askForNotifications();
  } else {
    for (const viewer of viewers.values()) if (viewer.state === 'pending') letIn(viewer);
  }
  if (easyWasOn && !settings.easy) applyEasy();
  render();
}

function setEasy(on) {
  if (on && !settings.ask) setAsk(true); // anyone on the Wi-Fi can find the share now
  settings = withEasy(settings, on);
  saveSettings((saved) => withEasy(saved, on));
  applyEasy();
  render();
}

function denyAll() {
  for (const viewer of [...viewers.values()]) {
    if (viewer.state === 'pending' && viewers.has(viewer.id)) sendAway(viewer, 'denied');
  }
}

// With Ask on, "2 waiting" can show while the page is in the background, if the browser allows it: asked when Start
// sharing is pressed or Ask is turned on, while the browser has no answer, once per page (askToNotify).
function askForNotifications() {
  const permission = 'Notification' in window ? Notification.permission : '';
  if (!askToNotify({ ask: settings.ask, permission, askedBefore: notifyAsked })) return;
  notifyAsked = true;
  try {
    Promise.resolve(Notification.requestPermission()).catch(() => {});
  } catch {
    // an old browser that wants a callback: no notifications then
  }
}

// One notification for everyone waiting ("2 waiting"), kept up to date and closed when nobody waits.
function updateWaitingNote(count) {
  const away = document.hidden || !document.hasFocus();
  const step = waitingNoteStep({ before: waitingBefore, count, showing: Boolean(waitingNote), away });
  const grew = count > waitingBefore;
  waitingBefore = count;
  if (step === 'close') {
    const note = waitingNote;
    waitingNote = null;
    note.close();
    return;
  }
  if (step !== 'show' || !('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    const note = new Notification(waitingTitle(count), {
      body: 'Open Porthole to allow or deny.',
      tag: 'porthole-waiting', // replaces the previous one instead of adding another
      renotify: grew,
      requireInteraction: true,
    });
    note.onclick = () => {
      window.focus();
      note.close();
    };
    note.onclose = () => {
      if (waitingNote === note) waitingNote = null;
    };
    waitingNote = note;
  } catch {
    // notifications unavailable
  }
}

// ---------- viewers: connections ----------

function closeLink(viewer) {
  clearTimeout(viewer.recoverTimer);
  if (viewer.link) viewer.link.close();
  viewer.link = null;
}

function videoSender(viewer) {
  return viewer.link && viewer.link.pc.getSenders().find((s) => s.track && s.track.kind === 'video');
}

async function applyLevel(viewer) {
  const sender = videoSender(viewer);
  if (!sender) return;
  const level = LEVELS[viewer.tuner.level];
  viewer.appliedLevel = viewer.tuner.level;
  try {
    const params = sender.getParameters();
    if (!params.encodings || !params.encodings.length) return;
    params.encodings[0].maxBitrate = level.maxBitrate;
    params.encodings[0].scaleResolutionDownBy = level.scale;
    await sender.setParameters(params);
  } catch {
    viewer.appliedLevel = -1; // try again on the next round
  }
  try {
    const params = sender.getParameters();
    params.degradationPreference = settings.smooth ? 'maintain-framerate' : 'maintain-resolution';
    await sender.setParameters(params);
  } catch {
    // not supported in every browser
  }
}

function watchLink(viewer, link) {
  link.pc.onconnectionstatechange = () => {
    if (viewer.link !== link) return;
    const state = link.pc.connectionState;
    clearTimeout(viewer.recoverTimer);
    if (state === 'connected') {
      viewer.state = 'watching';
    } else if (state === 'disconnected') {
      viewer.state = 'connecting';
      viewer.recoverTimer = setTimeout(() => recover(viewer, link), 3000);
    } else if (state === 'failed') {
      viewer.state = 'failed';
      recover(viewer, link);
    } else if (state === 'connecting' || state === 'new') {
      viewer.state = 'connecting';
    }
    render();
  };
}

// First try an ICE restart on the same connection; if that doesn't reconnect in 10 s, start over.
async function recover(viewer, link) {
  if (viewer.link !== link || !stream || !viewers.has(viewer.id)) return;
  if (Date.now() - viewer.restartAt < 10000) return; // a restart is already under way
  viewer.restartAt = Date.now();
  setTimeout(() => {
    if (viewer.link === link && link.pc.connectionState !== 'connected') offerTo(viewer.id);
  }, 10000);
  try {
    const n = ++viewer.offerN;
    await link.pc.setLocalDescription(await link.pc.createOffer({ iceRestart: true }));
    if (viewer.link !== link) return;
    await signal.send(viewer.id, { type: 'offer', sdp: link.pc.localDescription.sdp, n, restart: true });
  } catch {
    if (viewer.link === link) offerTo(viewer.id);
  }
}

async function offerTo(id) {
  const viewer = viewers.get(id);
  if (!viewer || !stream || viewer.state === 'pending') return;
  closeLink(viewer);
  const link = createPeer((candidate) => signal.send(id, { type: 'candidate', candidate }));
  viewer.link = link;
  viewer.state = 'connecting';
  viewer.sample = null;
  viewer.appliedLevel = -1;
  viewer.restartAt = 0;
  watchLink(viewer, link);
  // While paused, someone who joins gets the Paused card (and silence) too.
  for (const track of stream.getTracks()) link.pc.addTrack(paused && track.kind === 'video' ? paused.track : track, stream);
  render();
  try {
    const n = ++viewer.offerN;
    await link.pc.setLocalDescription(await link.pc.createOffer());
    if (viewer.link !== link) return;
    const sent = signal.send(id, { type: 'offer', sdp: link.pc.localDescription.sdp, n });
    // Right after the offer, in order (the viewer's page starts each new connection without the note): a device that
    // joins while paused shows the "Paused" note too. A pause or Resume from now on reaches it on its own.
    if (paused) signal.send(id, { type: 'paused' });
    await sent;
  } catch {
    if (viewer.link !== link) return; // a newer connection replaced this one
    viewer.state = 'failed';
    render();
  }
}

async function onAnswer(msg) {
  const viewer = viewers.get(msg.from);
  const link = viewer && viewer.link;
  // Only the answer to the latest offer counts; an older one would break the new connection.
  if (!link || link.pc.signalingState !== 'have-local-offer' || msg.n !== viewer.offerN) return;
  try {
    await link.pc.setRemoteDescription({ type: 'answer', sdp: msg.sdp });
    await link.remoteSet();
    applyLevel(viewer);
  } catch {
    if (viewer.link !== link) return;
    viewer.state = 'failed';
    render();
  }
}

// Every 2 s: read each connection's stats, pick a quality level, show live numbers.
async function tune() {
  for (const viewer of viewers.values()) {
    const link = viewer.link;
    if (!link || link.pc.connectionState !== 'connected') continue;
    try {
      const stats = await link.pc.getStats();
      if (viewer.link !== link) continue;
      viewer.sample = senderSample(viewer.sample, stats, performance.now());
      viewer.tuner = decide(viewer.tuner, viewer.sample, viewer.mode);
      if (viewer.appliedLevel !== viewer.tuner.level) applyLevel(viewer);
    } catch {
      // stats unavailable this round
    }
  }
  render();
}

// ---------- recording ----------

// H.264 in MP4 plays almost everywhere; WebM is the fallback.
function pickMime(hasAudio) {
  if (!window.MediaRecorder) return '';
  const options = hasAudio
    ? ['video/mp4;codecs=avc1,mp4a.40.2', 'video/mp4;codecs=avc1,opus', 'video/webm;codecs=vp9,opus', 'video/webm']
    : ['video/mp4;codecs=avc1', 'video/webm;codecs=vp9', 'video/webm'];
  return options.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

async function startRecording() {
  if (!stream || recording || startingRecording) return;
  startingRecording = true;
  const hadFocus = document.activeElement === $('record'); // a disabled button loses the focus
  $('record').disabled = true;
  try {
    await beginRecording();
  } finally {
    startingRecording = false;
    $('record').disabled = inactive || !controlsOn; // stays off if the page ended (Porthole gone) or let go meanwhile
    if (hadFocus && !inactive && document.activeElement === document.body) focusOn('record');
  }
}

async function beginRecording() {
  showError('');
  const mimeType = pickMime(stream.getAudioTracks().length > 0);
  if (!mimeType) {
    showError("This browser can't record.");
    return;
  }
  const result = await act('/api/recording/start', JSON.stringify({ ext: mimeType.startsWith('video/mp4') ? 'mp4' : 'webm', page: PAGE }));
  if (!result) return; // Porthole has gone: the page has ended
  if (!result.ok) {
    showError(`Couldn't start recording: ${apiError(result)}`);
    return;
  }
  const rec = {
    ...result.data,
    seq: 0,
    bytes: 0,
    started: Date.now(),
    pausedAt: 0, // when the current pause began
    pausedFor: 0, // paused time so far, left out of the length shown
    chain: Promise.resolve(),
    failed: false,
    note: '',
  };
  try {
    // A key frame every 2 s, so MP4 pieces are written out steadily instead of piling up.
    rec.recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 4_000_000, videoKeyFrameIntervalDuration: 2000 });
  } catch (err) {
    await api(`/api/recording/stop?id=${encodeURIComponent(rec.id)}`);
    showError(`Couldn't start recording: ${err.message}`);
    return;
  }
  rec.recorder.ondataavailable = (event) => {
    if (event.data && event.data.size) rec.chain = rec.chain.then(() => upload(rec, event.data));
  };
  // 'stop' comes after the last piece, whoever stopped the recorder (us, or the shared tab closing).
  rec.stopped = new Promise((resolve) => rec.recorder.addEventListener('stop', resolve, { once: true }));
  rec.recorder.onerror = () => stopRecording('The recording stopped because of a browser error.', rec);
  recording = rec;
  lastSaved = null;
  rec.recorder.start(2000); // a piece every 2 s, so nothing large waits in memory
  rec.recorded = !paused; // whether any picture went in: stopped before Resume, the file has none and is deleted
  if (paused) pauseRecording(rec); // started while paused: nothing is recorded until Resume
  render();
}

function pauseRecording(rec = recording) {
  if (!rec || rec.recorder.state !== 'recording') return;
  rec.recorder.pause();
  rec.pausedAt = Date.now();
}

function resumeRecording(rec = recording) {
  if (!rec || rec.recorder.state !== 'paused') return;
  rec.recorder.resume();
  rec.recorded = true;
  rec.pausedFor += Date.now() - rec.pausedAt;
  rec.pausedAt = 0;
}

async function upload(rec, blob) {
  if (rec.failed) return;
  const path = `/api/recording/chunk?id=${encodeURIComponent(rec.id)}&seq=${rec.seq}`;
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    let result;
    try {
      result = await api(path, blob, 'application/octet-stream');
    } catch (err) {
      result = { ok: false, status: 0, data: { error: err.message } };
    }
    // 409 with the next number means an earlier try did arrive.
    if (result.ok || (result.status === 409 && result.data && result.data.expected === rec.seq + 1)) {
      rec.seq += 1;
      rec.bytes += blob.size;
      return;
    }
    if (attempt === 4 || result.status === 404 || result.status === 507) {
      rec.failed = true;
      // A write error (a full disk) stopped it: what came before is kept, and Porthole's answer at the end says why.
      const stoppedByDisk = result.status === 507 && result.data && result.data.stopped;
      stopRecording(stoppedByDisk ? '' : `Couldn't save the recording: ${apiError(result)}`, rec);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

function stopRecording(errorText = '', rec = recording) {
  if (!rec || rec.finishing) return Promise.resolve();
  rec.finishing = true;
  if (recording === rec) recording = null;
  render();
  return new Promise((resolve) => {
    const finish = () => {
      rec.chain = rec.chain.then(async () => {
        let problem = errorText;
        try {
          const nothing = rec.recorded ? '' : '&recorded=0'; // no picture went in: Porthole deletes the file
          const result = await api(`/api/recording/stop?id=${encodeURIComponent(rec.id)}${nothing}`);
          if (result.ok && !problem) {
            lastSaved = keptRecording(result.data, rec.earlier); // empty after Change: the part before it is kept
            problem = recordingOutcome(lastSaved).error; // why a full disk (say) stopped it early
          } else if (!result.ok) problem = problem || `Couldn't finish the recording: ${apiError(result)}`;
        } catch (err) {
          // No answer at all: if Porthole has gone, the page has ended and says so, with nothing more to add.
          problem = (await portholeGone()) ? '' : problem || `Couldn't finish the recording: ${err.message}`;
        }
        if (problem) showError(problem);
        render();
        resolve();
      });
    };
    if (rec.recorder.state !== 'inactive') rec.recorder.stop(); // sends the last piece, then 'stop'
    const safety = new Promise((done) => setTimeout(done, 5000));
    Promise.race([rec.stopped, safety]).then(finish);
  });
}

function formatDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

function renderRecording() {
  const status = $('record-status');
  status.hidden = !recording;
  if (recording) {
    const now = Date.now();
    const length = now - recording.started - recording.pausedFor - (recording.pausedAt ? now - recording.pausedAt : 0);
    const size =
      recording.bytes >= 1_000_000
        ? `${(recording.bytes / 1_000_000).toFixed(1)} MB`
        : `${Math.round(recording.bytes / 1000)} KB`;
    const note = recording.note ? ` · ${recording.note}` : '';
    status.textContent = `${recording.pausedAt ? 'Recording paused' : 'Recording'} ${formatDuration(length)} · ${size}${note}`;
  }
  // The Saved line with Open folder, or "Nothing was recorded." (the file had nothing in it and was deleted).
  const outcome = recordingOutcome(lastSaved);
  $('saved').hidden = !outcome.line || Boolean(recording);
  if (outcome.line) setText($('saved-text'), outcome.line);
  $('open-folder').hidden = !outcome.folder;
  if (lastSaved && lastSaved !== announcedSaved && !recording) {
    announcedSaved = lastSaved;
    announce(outcome.line);
  }
}

// ---------- quit ----------

function endPage(state) {
  inactive = true;
  $('busy').hidden = true; // nothing left to take over
  elsewhere = null;
  // Its devices have been told it ended, or belong to the page that took over: none is listed here any more.
  for (const viewer of viewers.values()) closeLink(viewer);
  viewers.clear();
  picks.stopped(); // a picker still open shares nothing once the page has let go
  if (signal) signal.close();
  setControlsEnabled(false);
  for (const button of document.querySelectorAll('button')) button.disabled = true;
  setText($('state'), state);
  showError('');
  if (mini) mini.win.close();
}

async function quit() {
  // A page waiting while another window or tab shares has no viewers or recording of its own: it asks Porthole.
  const live = controlsOn ? liveHere() : await sharingElsewhere();
  if (live && !window.confirm('Stop sharing and quit Porthole?')) return;
  await stopRecording();
  const result = await act('/api/quit');
  if (!result) return; // Porthole had gone already: the page has ended and says so
  if (!result.ok) {
    showError(`Couldn't quit Porthole: ${apiError(result)}`);
    return;
  }
  if (inactive) return; // "ended" got here first and said it already
  endStopped();
}

// Quitting would cut something: someone watching what is shared, or a recording. Devices only waiting see nothing.
function liveHere() {
  return Boolean(recording) || (Boolean(stream) && [...viewers.values()].some((v) => v.state !== 'pending'));
}

async function sharingElsewhere() {
  try {
    const info = await (await fetch('/api/info')).json();
    return Boolean(info.sharing);
  } catch {
    return false; // Porthole is unreachable: quitting can't cut anything
  }
}

// Porthole has stopped (Quit, Ctrl+C, its window closed): the page lets go and says so; its own window closes.
function endStopped() {
  stop();
  endPage('Stopped');
  runEnded = true;
  showNotice('Porthole has stopped. You can close this page.');
  closeOwnWindow();
}

// Porthole has gone without a word (crashed or killed): no "ended" came. The page lets go as when Porthole stops and
// says so. Porthole's own window stays open, so the note can be read: it didn't close because someone quit.
function endGone() {
  if (inactive) return;
  stop();
  endPage('Stopped');
  runEnded = true;
  showNotice(GONE);
}

// Porthole was killed and started again: its new run answers, with a new room. This page belongs to the run before and
// lets go; the new run has a page of its own, and reloading this one gets that. Porthole's own window closes as with
// every other ending: its browser would keep the new run's window from being followed.
function endRestarted() {
  stop();
  endPage('Stopped');
  showNotice(RESTARTED);
  closeOwnWindow();
}

// Porthole's own window goes away with Porthole; a browser tab stays open with the note.
function closeOwnWindow() {
  if (appWindow) window.close();
}

// Porthole's own window can't see the browser's tabs: open the page in a browser tab instead. That tab may be in
// another browser, with other saved settings: it takes this run's approvals and switches from Porthole.
async function openInBrowser() {
  await serverQueue; // this run's approvals and switches reach Porthole before the browser tab reads them
  const result = await act('/api/open-in-browser');
  if (!result) return;
  if (!result.ok) {
    showError(`Couldn't open your browser: ${apiError(result)}`);
    return;
  }
  endPage('Opened in your browser'); // lets go at once, so the tab doesn't have to ask
  showNotice('Porthole is open in your browser. You can close this window.');
  window.close();
}

// ---------- messages from the server ----------

function onMessage(msg) {
  if (inactive) return;
  const viewer = msg.from ? viewers.get(msg.from) : viewers.get(msg.id);
  switch (msg.type) {
    case 'hello':
      for (const v of viewers.values()) closeLink(v);
      viewers.clear();
      for (const v of msg.viewers) addViewer(v.id, v.device, v.client, v.code, v.joined, v.address);
      applyEasy(); // the saved setting: the server never announces the name on its own
      reportSharing();
      keepChoices(); // also when nothing changes here: a page that takes over gets this page's choices
      break;
    case 'viewer-joined':
      if (msg.replaces && viewers.has(msg.replaces)) replaceViewer(viewers.get(msg.replaces), msg);
      else addViewer(msg.id, msg.device, msg.client, msg.code, msg.joined, msg.address);
      break;
    case 'viewer-left':
      if (viewer) closeLink(viewer);
      viewers.delete(msg.id);
      break;
    case 'answer':
      onAnswer(msg);
      break;
    case 'candidate':
      if (viewer && viewer.link) viewer.link.addCandidate(msg.candidate);
      break;
    case 'ready':
      if (stream && viewer && viewer.state !== 'pending') offerTo(msg.from);
      break;
    case 'quality':
      if (viewer) {
        viewer.mode = msg.mode;
        viewer.tuner = msg.mode === 'auto' ? newTuner() : decide(viewer.tuner, { loss: 0, rtt: 0 }, msg.mode);
        applyLevel(viewer);
      }
      break;
    case 'ended': // Porthole was stopped (Ctrl+C or Quit): a normal ending, not an error
      signal.close();
      stopRecording();
      endStopped();
      break;
    case 'replaced':
      signal.close(); // stop the browser reconnecting and taking the role back
      // Porthole's own window lets go when a browser tab took over, so closing it, as the note says, leaves the new
      // share running. Another Porthole window lives in the same browser: then the tie stays, and closing the last
      // window still stops Porthole.
      if (appWindow && !msg.window) api('/api/release-window').catch(() => {});
      stopRecording();
      stop();
      endPage('Moved');
      $('open-folder').disabled = false; // Porthole still runs: a recording just saved can still be found
      showNotice('Sharing moved to another window or tab. You can close this one.');
      break;
  }
  render();
}

// Up to 3 s for the page that is the sharer to let go (a reloaded page's old connection takes about a second to
// close). `free` once it has; else `sharing` says whether that page shares (or is only open).
async function sharerSlotFrees() {
  let info = null;
  for (let i = 0; i < 6; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    info = await readInfo();
    if (!info) return { free: false, sharing: false };
    if (!info.sharer_active) return { free: true, sharing: false };
  }
  return { free: false, sharing: Boolean(info.sharing) };
}

async function connect(takeover) {
  const focused = $('busy').contains(document.activeElement) ? document.activeElement.id : '';
  if (connecting) return;
  connecting = true;
  try {
    await readChoicesAgain(); // what the other page allowed, denied or changed while this one waited
  } finally {
    connecting = false;
  }
  if (inactive) return; // Porthole stopped meanwhile
  $('busy').hidden = true;
  elsewhere = null; // the status line and the Watching list are this page's own again
  setControlsEnabled(true);
  render();
  if (focused) focusOn(replacementFocus(focused)); // "Use this one" has gone: Start sharing takes its place
  if (signal) signal.close();
  // No silence check here: reopening would race this page's own old connection and look like
  // "sharing in another window or tab". A 404 "unknown peer" still reopens it.
  signal = new Signal({
    room,
    role: 'sharer',
    takeover,
    params: { page: PAGE, window: appWindow ? 1 : 0 },
    onMessage,
    onStatus,
    silenceMs: 0,
  });
}

function setControlsEnabled(on) {
  controlsOn = on;
  for (const id of ['start', 'pause', 'change', 'record', 'stop', 'mini', 'ask', 'smooth', 'easy', 'open-browser']) {
    $(id).disabled = !on || (id === 'start' && !canShare); // a browser that can't share keeps Start sharing off
  }
}

// Another page is the sharer: this one waits, saying whether that page shares or is only open (`sharing`). The status
// line says so too ("Sharing in another window or tab"), and there is no Watching list here: "Not sharing" and "No one
// yet" made Use this one look harmless, though it cuts everyone watching. The status line is read out by screen
// readers when it changes, so the waiting line isn't said as well (it would be read twice).
function showBusy(sharing) {
  if (signal) signal.close();
  picks.stopped(); // a picker still open shares nothing: this page isn't the sharer
  const hadFocus = document.activeElement && document.activeElement !== document.body;
  elsewhere = Boolean(sharing);
  setText($('busy-text'), busyText(sharing));
  $('busy').hidden = false;
  setControlsEnabled(false);
  render();
  // A turned-off or hidden control loses the focus: some browsers drop it at once, Chrome a moment later.
  const active = document.activeElement;
  const lost = !active || active === document.body || active.disabled || Boolean(active.closest('[hidden]'));
  if (hadFocus && lost) focusOn('takeover');
}

// While this page waits, the other one starts or stops sharing: the lines follow. The status line is written only on a
// change, so the change is said once.
function redrawBusy(sharing) {
  elsewhere = Boolean(sharing);
  setText($('busy-text'), busyText(sharing));
  render();
}

async function onStatus(status) {
  if (inactive) return;
  if (status === 'reconnecting') {
    showError('Lost contact with Porthole. Retrying…');
    noticeGone(); // crashed or killed: no "ended" ever comes, and the browser would retry for ever
  } else if (status === 'closed') {
    // Refused: another window or tab is the sharer, or Porthole was restarted (a new run, with a new room), or stopped.
    const info = await readInfo();
    if (inactive) return;
    const run = answeringRun(info, room);
    if (run === 'this' && info.sharer_active) {
      showError('');
      showBusy(info.sharing);
      return;
    }
    if (run === 'this' && !retaking) {
      // The page that held the slot has just gone: this one takes it. Once: refused again with no sharer page, it
      // ends below.
      retaking = true;
      showError('');
      connect(false);
      return;
    }
    if (run === 'another') {
      endRestarted(); // the new run answers: use its page, or reload this one
      return;
    }
    // Nothing answers (or this run refused the page again with no sharer page). This page belongs to a run that has
    // ended: it lets go, so nothing on it acts on a Porthole started since. Porthole's own window closes as with every
    // other ending: its browser would keep the new run's window from being followed, so closing that one wouldn't
    // stop Porthole.
    stop();
    endPage('Stopped');
    runEnded = true;
    showError('Porthole has stopped or restarted. Start it again and reload this page.');
    closeOwnWindow();
  } else {
    retaking = false;
    showError(canShare ? '' : CANT_SHARE);
  }
}

// ---------- mini controls (an always-on-top window, where the browser has one) ----------

const MINI_HTML = `<main class="mini">
  <p class="status" data-mini="line" data-state="idle" aria-live="polite"><span class="dot" aria-hidden="true"></span><span data-mini="state">Not sharing</span></p>
  <p class="visually-hidden" role="status" data-mini="say"></p>
  <div class="controls">
    <button type="button" data-mini="pause">Pause</button>
    <button type="button" data-mini="stop">Stop sharing</button>
  </div>
  <div class="mini-waiting" data-mini="waiting" hidden>
    <span class="viewer-name" data-mini="who" tabindex="-1"></span>
    <span class="viewer-actions">
      <button type="button" class="primary small" data-mini="allow">Allow</button>
      <button type="button" class="small" data-mini="deny">Deny</button>
    </span>
  </div>
</main>`;

async function openMini() {
  if (mini) {
    mini.win.focus();
    return;
  }
  let win;
  try {
    win = await window.documentPictureInPicture.requestWindow({ width: 280, height: 180 });
  } catch {
    return; // refused, or already open from another click
  }
  const doc = win.document;
  doc.documentElement.lang = 'en';
  applyTheme(doc.documentElement, theme);
  doc.title = 'Mini controls · Porthole';
  const css = doc.createElement('link');
  css.rel = 'stylesheet';
  css.href = new URL('/static/style.css', location.href).href;
  doc.head.append(css);
  doc.body.innerHTML = MINI_HTML; // fixed markup; names are filled in with textContent
  const part = (name) => doc.querySelector(`[data-mini="${name}"]`);
  const opened = {
    win,
    line: part('line'),
    state: part('state'),
    pause: part('pause'),
    stop: part('stop'),
    waiting: part('waiting'),
    who: part('who'),
    allow: part('allow'),
    deny: part('deny'),
    say: part('say'),
    viewerId: null,
  };
  const firstWaiting = () => viewers.get(opened.viewerId);
  // After Allow or Deny the same buttons stand for the next device: focus goes to its name instead,
  // so a held Enter key can't let it in too.
  const afterChoice = () => {
    if (!opened.waiting.hidden) opened.who.focus();
    else if (!opened.pause.hidden) opened.pause.focus();
  };
  opened.pause.addEventListener('click', togglePause);
  opened.stop.addEventListener('click', stop);
  opened.allow.addEventListener('click', () => {
    const viewer = firstWaiting();
    if (viewer) allow(viewer);
    render();
    afterChoice();
  });
  opened.deny.addEventListener('click', () => {
    const viewer = firstWaiting();
    if (viewer) sendAway(viewer, 'denied');
    render();
    afterChoice();
  });
  win.addEventListener('pagehide', () => {
    if (mini === opened) mini = null;
  });
  mini = opened;
  render();
}

function renderMini(sharing, waitingList, news) {
  if (!mini) return;
  setText(mini.state, $('state').textContent);
  mini.line.dataset.state = $('status-line').dataset.state;
  mini.pause.hidden = !sharing;
  mini.stop.hidden = !sharing;
  mini.pause.textContent = paused ? 'Resume' : 'Pause';
  const first = inactive ? null : waitingList[0];
  mini.viewerId = first ? first.id : null;
  const hadFocus = mini.waiting.contains(mini.win.document.activeElement);
  mini.waiting.hidden = !first;
  if (first) {
    const who = first.code ? `${rowName(first)} · Code ${first.code}` : rowName(first);
    if (mini.who.textContent !== who) mini.who.textContent = who;
    mini.allow.setAttribute('aria-label', `Allow ${rowName(first)}`);
    mini.deny.setAttribute('aria-label', `Deny ${rowName(first)}`);
  } else if (hadFocus && !mini.pause.hidden) {
    mini.pause.focus(); // the waiting part has gone
  }
  if (news) setText(mini.say, news); // the mini window may have the focus, so it says it too
}

// ---------- screen ----------

// Rows are updated in place (never rebuilt), so a button under the pointer survives the 2 s refresh.
const rows = new Map(); // viewer id -> { item, name, meta, tag, tagText, actions, kind }

function makeButton(label, action, viewer, extra = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.dataset.action = action;
  button.dataset.id = viewer.id;
  button.setAttribute('aria-label', `${label} ${rowName(viewer)}`);
  if (viewer.code) button.id = `${action}-${viewer.code}`; // e.g. allow-482: the code the viewer sees
  button.className = `small ${extra}`.trim();
  return button;
}

function makeRow(viewer) {
  const item = document.createElement('li');
  const main = document.createElement('div');
  main.className = 'row-main';
  const name = document.createElement('span');
  name.className = 'viewer-name';
  const meta = document.createElement('span');
  meta.className = 'viewer-meta';
  main.append(name, meta);
  const tag = document.createElement('span');
  const dot = document.createElement('span');
  dot.className = 'dot';
  dot.setAttribute('aria-hidden', 'true');
  const tagText = document.createElement('span');
  tag.append(dot, tagText);
  const actions = document.createElement('div');
  actions.className = 'viewer-actions';
  item.append(main, tag, actions);
  item.dataset.id = viewer.id;
  item.tabIndex = -1; // focus can land here when a button in the row goes away (see restoreFocus)
  if (viewer.code) item.id = `viewer-${viewer.code}`;
  return { item, main, name, meta, tag, tagText, actions, kind: '' };
}

function updateRow(row, viewer) {
  const { text, cls } = rowState(viewer, Boolean(stream));
  const name = rowName(viewer);
  if (row.name.textContent !== name) row.name.textContent = name;
  const meta = rowDetails(viewer);
  if (row.meta.textContent !== meta) row.meta.textContent = meta;
  if (row.tagText.textContent !== text) row.tagText.textContent = text;
  if (row.tag.className !== `tag ${cls}`) row.tag.className = `tag ${cls}`;
  // A focused row reads the device, its code and its state: not its buttons, and not the live numbers.
  const label = rowLabel(viewer, Boolean(stream));
  if (row.item.getAttribute('aria-label') !== label) row.item.setAttribute('aria-label', label);
  // The live numbers stay out of the row itself, in a tooltip on a part that never takes the focus:
  // on the focused row they would be read out again every 2 s.
  const tip = rowTooltip(viewer);
  if ((row.main.getAttribute('title') || '') !== tip) {
    if (tip) row.main.setAttribute('title', tip);
    else row.main.removeAttribute('title');
  }
  const kind = viewer.state === 'pending' ? 'pending' : 'normal';
  if (row.kind !== kind) {
    row.kind = kind;
    row.actions.replaceChildren(
      ...(kind === 'pending'
        ? [makeButton('Allow', 'allow', viewer, 'primary'), makeButton('Deny', 'deny', viewer)]
        : [makeButton('Remove', 'remove', viewer)]),
    );
  }
}

// Draws `list` into `container` in order; a row moves between the lists when it is allowed.
function renderRows(container, list) {
  list.forEach((viewer, index) => {
    let row = rows.get(viewer.id);
    if (!row) {
      row = makeRow(viewer);
      rows.set(viewer.id, row);
    }
    updateRow(row, viewer);
    if (container.children[index] !== row.item) container.insertBefore(row.item, container.children[index] || null);
  });
}

// Returns an error or note that just appeared, for screen readers: both describe the switch too, but a
// changed description isn't read out again (it comes a moment after the switch was turned on).
function renderEasy() {
  const view = easyView(settings, easyState);
  $('easy').checked = view.checked;
  $('easy').disabled = view.disabled || !controlsOn; // redrawn every 2 s: it must stay off with the others
  const line = easyLine(easyState);
  setText($('easy-host'), line);
  $('easy-address').hidden = !line;
  const news = [];
  for (const [id, text] of [['easy-error', view.error], ['easy-note', view.note]]) {
    const fresh = lineNews(easyLines[id], text);
    if (fresh) news.push(fresh);
    easyLines[id] = text;
    setText($(id), text);
    $(id).hidden = !text;
  }
  return news.join(' ');
}

// ---------- keyboard focus ----------

// Where keyboard focus is before a redraw: a viewer row (or Deny all), or a button by its id.
function focusPlace() {
  const active = document.activeElement;
  if (!active || active === document.body) return null;
  const item = active.closest('#waiting-list > li, #viewers > li');
  if (item) {
    const list = item.parentElement;
    return { list: list.id === 'waiting-list' ? 'waiting' : 'viewers', index: [...list.children].indexOf(item), viewerId: item.dataset.id };
  }
  if (active.id === 'deny-all') return { list: 'waiting', index: 0, viewerId: null };
  return { id: active.id };
}

// After a redraw: if the focused element went away or was hidden (Allow, Deny, Remove, Deny all,
// Start sharing, Stop sharing), focus goes where focusAfterChange and replacementFocus say, never
// back to the start of the page.
function restoreFocus(place) {
  const active = document.activeElement;
  const lost = !active || active === document.body || !active.isConnected || Boolean(active.closest('[hidden]'));
  if (!place || !lost) return;
  if (!place.list) {
    focusOn(replacementFocus(place.id));
    return;
  }
  const ids = (list) => [...$(list).children].map((item) => item.dataset.id);
  const next = focusAfterChange({ ...place, waitingIds: ids('waiting-list'), watchingIds: ids('viewers') });
  const row = next.kind === 'row' && rows.get(next.id);
  if (row) row.item.focus({ preventScroll: pointerLast });
  else focusOn('viewers-title');
}

function render() {
  const focusBefore = focusPlace();
  const said = []; // news for screen readers, said together at the end
  const sharing = Boolean(stream);
  if (!inactive) setText($('state'), statusText({ sharing, paused: Boolean(paused), recording: Boolean(recording), elsewhere }));
  $('status-line').dataset.state = inactive || !sharing ? 'idle' : paused ? 'paused' : 'live';
  $('start').hidden = sharing;
  $('controls').hidden = !sharing;
  $('preview').hidden = !sharing;
  $('change').hidden = testMode;
  $('mini').hidden = !miniSupported;
  $('pause').textContent = paused ? 'Resume' : 'Pause';
  $('record').textContent = recording ? 'Stop recording' : 'Record';
  $('audio-hint').hidden = !sharing || testMode || stream.getAudioTracks().length > 0;
  $('browser-hint').hidden = !browserHintShown({ appWindow, sharing, inactive });
  // Checked each time: "Share this tab instead" can change what the same track shows.
  $('monitor-hint').hidden = !sharing || !isWholeScreen(stream.getVideoTracks()[0]);
  for (const id of ['monitor-hint', 'audio-hint']) {
    const shown = !$(id).hidden;
    if (shown && !hintsShown[id]) said.push($(id).textContent); // a note that just appeared
    hintsShown[id] = shown;
  }
  $('ask').checked = settings.ask;
  $('smooth').checked = settings.smooth;

  const list = [...viewers.values()];
  const waitingList = list.filter((v) => v.state === 'pending');
  const watchingList = list.filter((v) => v.state !== 'pending');
  const waitingCount = inactive ? 0 : waitingList.length; // a page that let go can't let anyone in
  document.title = waitingCount ? `(${waitingCount}) ${TITLE}` : TITLE;
  $('waiting').hidden = !waitingList.length || inactive;
  $('waiting-title').textContent = waitingTitle(waitingList.length);
  $('deny-all').hidden = waitingList.length < 2 || inactive;
  $('watching').hidden = elsewhere !== null; // waiting while another page is the sharer: that page's viewers aren't here
  $('viewer-count').textContent = watchingList.length ? String(watchingList.length) : '';
  $('no-viewers').hidden = watchingList.length > 0;
  // Rows of devices that left go first: putting the rest in order would otherwise move a focused
  // row past them, and moving an element drops its focus.
  for (const [id, row] of rows) {
    if (!viewers.has(id)) {
      row.item.remove();
      rows.delete(id);
    }
  }
  renderRows($('waiting-list'), waitingList);
  renderRows($('viewers'), watchingList);
  updateWaitingNote(inactive ? 0 : waitingList.length);
  const news = waitingNews(announcedWaiting, inactive ? [] : waitingList);
  announcedWaiting = news.announced;
  if (news.text) said.push(news.text);
  renderRecording();
  const easyNews = renderEasy();
  if (easyNews) said.push(easyNews);
  renderMini(sharing && !inactive, waitingList, news.text);
  if (said.length) announce(said.join(' '));
  restoreFocus(focusBefore);
}

// The link, the QR code and the other addresses; drawn again only when the addresses change.
function drawJoin(info) {
  const urls = Array.isArray(info.viewer_urls) ? info.viewer_urls : [];
  const key = JSON.stringify(urls);
  if (key === joinUrls) return false;
  joinUrls = key;
  const [first, ...others] = urls;
  currentLink = first || '';
  $('join').hidden = !first;
  $('no-network').hidden = Boolean(first);
  const noNetwork = first ? '' : $('no-network').textContent;
  announce(lineNews(noNetworkDrawn, noNetwork)); // the PC dropped off the network while the page was open
  noNetworkDrawn = noNetwork;
  $('link').textContent = currentLink;
  $('qr').innerHTML = first ? info.qr_svg : ''; // SVG made by Porthole from the link
  const picture = $('qr').querySelector('svg');
  if (picture) picture.setAttribute('aria-hidden', 'true'); // the box around it says what it is
  $('other').hidden = !others.length;
  $('other-links').replaceChildren(
    ...others.map((url) => {
      const item = document.createElement('li');
      item.textContent = url;
      return item;
    }),
  );
  return true;
}

// One line about the newest device that was turned away (too many tabs or tries).
function drawRefusal(info) {
  const line = refusalLine(newestRefusal(info.refusals));
  if ($('refusal').textContent !== line) {
    $('refusal').textContent = line;
    if (refusalDrawn && elsewhere === null) announce(line); // news, unless it came with the page or isn't shown (waiting)
  }
  refusalDrawn = true;
  $('refusal').hidden = !line;
}

async function readInfo() {
  try {
    return await (await fetch('/api/info')).json();
  } catch {
    return null; // Porthole is unreachable
  }
}

const SLOT_GRACE_MS = 4000; // longer than a reloaded sharer page waits for its slot (sharerSlotFrees, 3 s)
const later = (ms, then) => new Promise((resolve) => setTimeout(resolve, ms)).then(then);

async function refreshInfo() {
  if (inactive) {
    // Ended because Porthole stopped or went away: once a new run answers (it was started again, say some seconds after
    // it was killed), the page says so instead. Porthole's own window closes then, so the new run's window is followed.
    if (runEnded && answeringRun(await readInfo(), room) === 'another') {
      runEnded = false;
      showError('');
      showNotice(RESTARTED);
      closeOwnWindow();
    }
    return;
  }
  const waiting = !$('busy').hidden;
  let info = await readInfo();
  // No answer: asked again a second later, so a moment's hiccup doesn't end the page.
  if (!info) info = await later(1000, readInfo);
  if (inactive) return;
  // A page waiting while another window or tab shares has no event stream to hear "ended": it notices itself that
  // Porthole was restarted (a new room) or has gone (no answer twice), and ends the same way.
  if (waiting && answeringRun(info, room) === 'another') {
    endRestarted();
    return;
  }
  if (waiting && answeringRun(info, room) === 'none') {
    endStopped();
    return;
  }
  if (waiting && !info.sharer_active) {
    // The page that shared has closed: this one takes the free slot (nothing live is cut), so a device waiting
    // to be let in has someone to let it in. Not at once: a page that is only reloading gets its slot back first.
    const again = await later(SLOT_GRACE_MS, readInfo);
    if (inactive || $('busy').hidden) return;
    if (again && again.room === room && !again.sharer_active) connect(false);
    return;
  }
  // A page that had its stream: no answer twice, so "ended" never came: Porthole crashed or was killed.
  if (!info) {
    endGone();
    return;
  }
  if (info.room !== room) return; // restarted: the stream's error handling says so
  // The other page shares or is only open: the waiting line follows it.
  if (waiting) redrawBusy(info.sharing);
  drawRefusal(info);
  if (drawJoin(info)) {
    easyState = info.easy; // the easy address moved with the address
    render();
  }
}

async function openFolder() {
  const result = await act('/api/open-folder');
  if (result && !result.ok) showError(apiError(result));
}

async function copyLink() {
  try {
    await navigator.clipboard.writeText(currentLink);
    $('copy').textContent = 'Copied';
    setTimeout(() => ($('copy').textContent = 'Copy'), 1500);
  } catch {
    window.getSelection().selectAllChildren($('link')); // copy it by hand
  }
}

function onViewerButton(event) {
  const button = event.target.closest('button[data-action]');
  const viewer = button && viewers.get(button.dataset.id);
  if (!viewer) return;
  if (button.dataset.action === 'allow') allow(viewer);
  else if (button.dataset.action === 'deny') sendAway(viewer, 'denied');
  else if (button.dataset.action === 'remove') sendAway(viewer, 'removed');
  render();
}

async function init() {
  $('test-note').hidden = !testMode;
  $('audio-hint').textContent = audioHint(appWindow, navigator.userAgent);
  if (!canShare) {
    showError(CANT_SHARE);
    $('start').disabled = true;
  }
  for (const input of document.querySelectorAll('input[name="theme"]')) {
    input.addEventListener('change', () => setTheme(input.value));
  }
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener('change', () => setTheme(theme, false));
  $('start').addEventListener('click', start);
  $('pause').addEventListener('click', togglePause);
  $('change').addEventListener('click', change);
  $('stop').addEventListener('click', stop);
  $('record').addEventListener('click', () => (recording ? stopRecording() : startRecording()));
  $('mini').addEventListener('click', openMini);
  $('quit').addEventListener('click', quit);
  $('open-browser').addEventListener('click', openInBrowser);
  $('open-folder').addEventListener('click', openFolder);
  $('ask').addEventListener('change', (event) => setAsk(event.target.checked));
  $('easy').addEventListener('change', (event) => setEasy(event.target.checked));
  $('smooth').addEventListener('change', (event) => {
    const on = event.target.checked;
    settings.smooth = on;
    saveSettings((saved) => ({ ...saved, smooth: on }));
    applySmooth();
  });
  $('deny-all').addEventListener('click', () => {
    denyAll();
    render();
  });
  $('waiting-list').addEventListener('click', onViewerButton);
  $('viewers').addEventListener('click', onViewerButton);
  $('copy').addEventListener('click', copyLink);

  const info = await (await fetch('/api/info')).json();
  room = info.room;
  easyState = info.easy;
  // This run's approvals and switches, kept by Porthole, else what this browser saved (the first page of a run).
  if (!useChoices(info.choices)) {
    approved = loadSet('porthole-approved');
    denied = loadSet('porthole-denied');
  }
  drawJoin(info);
  drawRefusal(info);
  render(); // the saved switches show at once, even while waiting for an old tab to let go
  setInterval(refreshInfo, 10000); // the PC's address can change, e.g. after moving to another Wi-Fi

  $('takeover').addEventListener('click', () => connect(true));
  // After a reload the old page's connection takes about a second to close: wait for it. Until then this page
  // isn't the sharer, so its controls stay off (connect() turns them on).
  if (info.sharer_active) setControlsEnabled(false);
  const slot = info.sharer_active ? await sharerSlotFrees() : { free: true };
  if (!slot.free) showBusy(slot.sharing);
  else connect(false);
  window.addEventListener('pagehide', () => {
    if (signal) signal.close();
    if (mini) mini.win.close();
  });
  setInterval(tune, 2000);
  setInterval(renderRecording, 1000);
  render();
}

init();
