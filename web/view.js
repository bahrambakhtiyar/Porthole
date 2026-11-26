import { Signal, createPeer, retrier, retryOnce } from '/static/signal.js';
import { receiverSample, isWeak, weakBadge } from '/static/quality.js';
import { contentRect, clampView, zoomAt, panBy, actualSizeScale, viewForKey } from '/static/zoom.js';
import { keepViewerId, keptViewerId, pictureName, spokenState, wasRestarted } from '/static/view-helpers.js';

const $ = (id) => document.getElementById(id);
const video = $('video');
const stage = $('stage');
const overlay = $('overlay');
const bar = $('bar');
const room = decodeURIComponent(location.pathname.split('/')[2] || '');

// Short states: Waiting, Connecting, Watching (no screen at all), Can't connect, Ended.
// `busy` shows the spinner; `final` offers Try again.
const SCREENS = {
  waiting: { title: 'Waiting', busy: true },
  approval: { title: 'Waiting', detail: 'Ask the sharer to let you in.', busy: true },
  connecting: { title: 'Connecting', busy: true },
  stopped: { title: 'Waiting', detail: 'Sharing stopped.' },
  lost: { title: "Can't connect", detail: 'Trying again.', busy: true },
  ended: { title: 'Ended', detail: 'The sharer closed Porthole.', final: true },
  restarted: { title: 'Ended', detail: 'Porthole was restarted. Use its new link, or try again.', final: true },
  denied: { title: 'Ended', detail: "The sharer didn't let you in.", final: true },
  removed: { title: 'Ended', detail: 'The sharer removed you.', final: true },
  tabs: { title: "Can't connect", detail: 'Too many Porthole tabs are open on this device. Close one and try again.', final: true },
  tries: { title: "Can't connect", detail: 'Too many tries. Wait a minute, then try again.', final: true },
  tap: { title: 'Tap to play', tap: true },
};

const canFullscreen = Boolean(stage.requestFullscreen || stage.webkitRequestFullscreen || video.webkitEnterFullscreen);
const canPip =
  (document.pictureInPictureEnabled === true && typeof video.requestPictureInPicture === 'function') ||
  (typeof video.webkitSetPresentationMode === 'function' &&
    typeof video.webkitSupportsPresentationMode === 'function' &&
    video.webkitSupportsPresentationMode('picture-in-picture'));

// ---------- remembered on this device ----------

function randomToken() {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b & 63]).join('');
}

// A random token so the sharer recognises this device when it reconnects (approval stays).
function deviceToken() {
  try {
    let token = localStorage.getItem('porthole-client');
    if (!token || !/^[A-Za-z0-9_-]{8,64}$/.test(token)) {
      token = randomToken();
      localStorage.setItem('porthole-client', token);
    }
    return token;
  } catch {
    return randomToken();
  }
}

// This tab's own storage (sessionStorage), for the viewer id kept across a reload; null where it is refused.
function tabStorage() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
const kept = tabStorage();

function loadQuality() {
  try {
    const saved = localStorage.getItem('porthole-quality');
    return ['auto', 'high', 'saver'].includes(saved) ? saved : 'auto';
  } catch {
    return 'auto';
  }
}

// ---------- connection ----------

let link = null;
let gone = false; // denied, removed, refused, or Porthole closed: stop trying
let code = null; // this connection's check code, also shown on the sharer's list
let badge = weakBadge(); // the Weak signal badge's state
let statsPrev = null;
let playing = false;
let sharerPaused = false; // the sharer paused: a small "Paused" note shows over the picture
let screenShown = SCREENS.connecting; // the screen over the picture, or null
let usingKeys = false; // the last input was a key, not a touch or a click
let lastPointerAt = -Infinity; // when the last touch or click began
let clickFocus = false; // focus in the bar came from a touch or click, not from a key or a screen reader
let mode = loadQuality();
// Keep asking the sharer for a fresh connection until one arrives (a single request can be lost),
// but never pile up requests while one is still on its way.
let readyInFlight = false;
const retry = retrier(
  () => {
    if (readyInFlight) return;
    readyInFlight = true;
    signal.send('sharer', { type: 'ready' }).finally(() => {
      readyInFlight = false;
    });
  },
  { max: 10000 },
);

// Writes only a change: screen readers may read a live region out again on every write.
function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

// Screen readers hear the state through one live region that is always on the page (#say): the words
// of the screen shown, or Watching / Weak signal while the picture plays. Written only on a change.
function speak() {
  const text = spokenState({
    screen: overlay.hidden ? null : screenShown,
    code: $('code').hidden ? null : code,
    playing,
    weak: !$('weak').hidden,
    paused: sharerPaused,
  });
  if (text) setText($('say'), text);
}

// The sharer paused (or resumed). The card says "Paused" only in its middle, so zoomed in on a corner a viewer saw a
// plain dark picture: a small note over the picture says it whatever the zoom, and "Paused" is said once.
function showPaused(on) {
  sharerPaused = on;
  $('paused').hidden = !(sharerPaused && playing);
  speak();
}

// `withCode`: the check code to show under the words (while waiting to be let in).
function show(name, withCode = null) {
  const screen = name ? SCREENS[name] : null;
  screenShown = screen;
  overlay.hidden = !screen;
  if (screen) {
    overlay.classList.toggle('tap', Boolean(screen.tap));
    $('status').textContent = screen.title;
    $('detail').textContent = screen.detail || '';
    $('detail').hidden = !screen.detail;
    $('spinner').hidden = !screen.busy;
    $('code').textContent = withCode ? `Your code: ${withCode}` : '';
    $('code').hidden = !withCode;
    $('play').hidden = !screen.tap;
    $('retry').hidden = !screen.final;
  }
  speak();
}

// Waiting to be let in: show the code, so the sharer can tell this device from others.
function showApproval() {
  show('approval', code);
}

// The buttons that need a picture appear only once video plays.
function setPlaying(on) {
  const focused = document.activeElement;
  playing = on;
  $('fullscreen').hidden = !on || !canFullscreen;
  $('pip').hidden = !on || !canPip;
  $('save').hidden = !on;
  showPaused(sharerPaused); // the note only over a picture
  updateZoomButtons();
  keepBarFocus(focused);
  speak();
}

function updateSound() {
  const incoming = video.srcObject;
  const hasAudio = Boolean(incoming && incoming.getAudioTracks && incoming.getAudioTracks().length);
  $('unmute').hidden = !hasAudio;
  $('unmute').setAttribute('aria-pressed', String(!video.muted));
}

function closeLink() {
  retry.stop();
  if (link) link.close();
  link = null;
  statsPrev = null;
  badge = weakBadge();
  showPaused(false); // a new connection starts without it: the sharer says so again right after its offer
  video.srcObject = null;
  setPlaying(false);
  updateSound();
  $('weak').hidden = true;
}

function play() {
  video.play().catch(() => {
    if (video.srcObject) show('tap'); // the browser wants a tap first
  });
}

function sendQuality() {
  signal.send('sharer', { type: 'quality', mode });
}

async function answerOn(peer, msg) {
  await peer.pc.setRemoteDescription({ type: 'offer', sdp: msg.sdp });
  await peer.remoteSet();
  await peer.pc.setLocalDescription(await peer.pc.createAnswer());
  if (link !== peer) return;
  const answer = { type: 'answer', sdp: peer.pc.localDescription.sdp, n: msg.n };
  // A lost answer leaves the sharer waiting: try once more, unless a newer connection took over.
  await retryOnce(() => signal.send('sharer', answer), () => link === peer);
}

async function onOffer(msg) {
  // An ICE restart keeps the same connection (and the picture) after a network blip.
  if (msg.restart && link && link.pc.signalingState === 'stable') {
    const peer = link;
    try {
      await answerOn(peer, msg);
    } catch {
      if (link === peer) retry.start(500); // ask for a completely new connection instead
    }
    return;
  }
  closeLink();
  show('connecting');
  const mine = createPeer((candidate) => signal.send('sharer', { type: 'candidate', candidate }));
  link = mine;
  mine.pc.ontrack = (event) => {
    const incoming = event.streams[0] || new MediaStream([event.track]);
    if (video.srcObject !== incoming) video.srcObject = incoming;
    updateSound();
    play();
  };
  mine.pc.onconnectionstatechange = () => {
    if (link !== mine) return;
    const state = mine.pc.connectionState;
    if (state === 'connected') {
      retry.stop();
      // Back after a blip: a picture the browser wouldn't play yet still needs its tap.
      if (video.readyState >= 2) show(video.paused && video.srcObject ? 'tap' : null);
    } else if (state === 'failed') {
      show('lost');
      retry.start(20000); // the sharer repairs or rebuilds first (about 10 s); this is the last resort
    } else if (state === 'disconnected') {
      show('lost');
      retry.start(25000);
    }
  };
  try {
    await answerOn(mine, msg);
    sendQuality();
  } catch {
    if (link === mine) {
      show('lost');
      retry.start(1000);
    }
  }
}

function leave(name) {
  gone = true;
  const hadFocus = bar.contains(document.activeElement);
  closeLink();
  show(name);
  bar.hidden = true; // nothing left to control
  if (hadFocus && !$('retry').hidden) $('retry').focus(); // the bar has gone: Try again takes its place
  signal.close();
}

function onMessage(msg) {
  if (gone) return;
  switch (msg.type) {
    case 'hello':
      code = msg.code || null;
      keepViewerId(kept, room, msg.id); // a reload names this connection, so a quick one may keep this device's row
      closeLink();
      unzoom();
      show('waiting');
      break;
    case 'waiting':
      closeLink();
      unzoom();
      if (msg.reason === 'approval') showApproval();
      else show('waiting');
      break;
    case 'offer':
      onOffer(msg);
      break;
    case 'candidate':
      if (link) link.addCandidate(msg.candidate);
      break;
    case 'paused':
    case 'resumed':
      showPaused(msg.type === 'paused');
      break;
    case 'stopped':
    case 'sharer-left':
      // A device not let in yet (or waiting for a share) keeps its screen and code: nothing changed for it.
      if (msg.type === 'sharer-left' && !link && (screenShown === SCREENS.approval || screenShown === SCREENS.waiting)) break;
      closeLink();
      unzoom(); // the next share starts at fit, not magnified on one corner of a different screen
      show('stopped');
      break;
    case 'denied':
    case 'removed':
      // Turned away: that row is never coming back, so Try again joins afresh. (Only here: a page being reloaded hears
      // its stream close too, and the new page needs the id.)
      keepViewerId(kept, room, '');
      leave(msg.type);
      break;
    case 'ended':
      leave('ended');
      break;
    case 'refused': // the server turned this connection away: say why, and stop retrying
      leave(msg.reason === 'too-many-tries' ? 'tries' : 'tabs');
      break;
  }
}

function onStatus(status) {
  if (gone) return;
  if (status === 'reconnecting') {
    closeLink();
    show('lost');
  } else if (status === 'closed') {
    onClosed();
  }
}

// The stream was refused, so this page's run of Porthole has ended. If a new run answers for this page's address
// (Porthole was killed and started again), say so: its new link, or Try again, which reloads this page (and with the
// easy address on reaches the new share). Nothing answering, or the same run: "The sharer closed Porthole." as before.
async function onClosed() {
  gone = true; // nothing more comes on this stream
  signal.close();
  let answer = null;
  try {
    const res = await fetch(location.href, { cache: 'no-store' });
    answer = { status: res.status, url: res.url };
  } catch {
    // nothing answers
  }
  if (answer && answer.status === 400) {
    // This device may no longer use the address it came by (Easy address went off and its wait is over), while the
    // share goes on: the page itself says why, "Use the link".
    location.reload();
    return;
  }
  const restarted = wasRestarted(answer, location.pathname);
  leave(restarted ? 'restarted' : 'ended');
}

// An iPad's browser says it is a Mac: its touch screen tells the sharer it is an iPad. After a reload, the connection
// before is named, so Porthole hands this device's row to the new one (Signal does the same on every reconnect).
const signal = new Signal({
  room,
  role: 'viewer',
  client: deviceToken(),
  previous: keptViewerId(kept, room),
  params: { touch: navigator.maxTouchPoints > 1 ? 1 : 0 },
  onMessage,
  onStatus,
});

video.addEventListener('playing', () => {
  show(null);
  setPlaying(true);
});
// Closing picture-in-picture, headphones going, a media key: the browser pauses the picture by itself. Without this
// it would sit frozen, with nothing to tap; "Tap to play" brings it back.
video.addEventListener('pause', () => {
  if (gone || !playing || !video.srcObject) return;
  setPlaying(false);
  show('tap');
});
overlay.addEventListener('click', () => {
  if (overlay.classList.contains('tap')) {
    show(null);
    play();
  }
});
// Try again reloads once Porthole answers: a reload while it is still down would show the browser's own error page,
// a dead end in a home-screen app with no reload button.
async function tryAgain() {
  show('connecting');
  $('retry').hidden = true;
  let notFound = 0;
  for (;;) {
    try {
      const res = await fetch(location.href, { cache: 'no-store' });
      // A 404 or two in a row is waited out: while Porthole starts, the easy address is off for a moment and an old
      // link answers "expired" (the "Nothing shared yet" page does the same).
      notFound = res.status === 404 ? notFound + 1 : 0;
      if (notFound === 0 || notFound >= 3) {
        location.reload();
        return;
      }
    } catch {
      // Porthole isn't answering yet
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}
$('retry').addEventListener('click', tryAgain);

// "Weak signal" badge from this device's own receive stats.
setInterval(async () => {
  const mine = link;
  if (!mine || mine.pc.connectionState !== 'connected') return;
  try {
    const stats = await mine.pc.getStats();
    if (link !== mine) return;
    statsPrev = receiverSample(statsPrev, stats);
    badge = weakBadge(badge, isWeak(statsPrev));
    if ($('weak').hidden === badge.shown) {
      $('weak').hidden = !badge.shown;
      speak();
    }
  } catch {
    // stats unavailable this round
  }
}, 2000);

// ---------- buttons ----------

$('quality').value = mode;
$('quality').addEventListener('change', () => {
  mode = $('quality').value;
  try {
    localStorage.setItem('porthole-quality', mode);
  } catch {
    // remembered for this page only
  }
  if (link) sendQuality();
  // After a touch or a click, so the bar can hide again. Not after a key: arrow keys change a closed
  // menu at once, and dropping the focus would strand the person using the keyboard.
  if (!usingKeys) $('quality').blur();
});

// The iPhone's own full-screen player and Firefox's picture-in-picture have mute buttons of their own.
video.addEventListener('volumechange', updateSound);
$('unmute').addEventListener('click', () => {
  video.muted = !video.muted;
  if (!video.muted) play();
  updateSound();
});

function toggleFullscreen() {
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    return;
  }
  if (stage.requestFullscreen) stage.requestFullscreen().catch(() => {});
  else if (stage.webkitRequestFullscreen) stage.webkitRequestFullscreen();
  else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen(); // iPhone
}
$('fullscreen').addEventListener('click', toggleFullscreen);

// Pop out: the picture in its own small window that stays on top.
async function togglePip() {
  try {
    if (document.pictureInPictureEnabled && typeof video.requestPictureInPicture === 'function') {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await video.requestPictureInPicture();
    } else if (typeof video.webkitSetPresentationMode === 'function') {
      if (video.webkitPresentationMode === 'picture-in-picture') video.webkitSetPresentationMode('inline');
      else video.webkitSetPresentationMode('picture-in-picture'); // Safari
    }
  } catch {
    // refused, e.g. while the picture is still loading
  }
}
$('pip').addEventListener('click', togglePip);

// Full screen and Pop out undo themselves when pressed again: say when they are on, as Sound and 1:1 do.
function updateModes() {
  const full = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
  const popped = Boolean(document.pictureInPictureElement) || video.webkitPresentationMode === 'picture-in-picture';
  $('fullscreen').setAttribute('aria-pressed', String(full));
  $('pip').setAttribute('aria-pressed', String(popped));
}
for (const event of ['fullscreenchange', 'webkitfullscreenchange']) document.addEventListener(event, updateModes);
for (const event of ['enterpictureinpicture', 'leavepictureinpicture', 'webkitpresentationmodechanged']) {
  video.addEventListener(event, updateModes);
}
updateModes();

// Save: the current frame as a PNG file.
function savePicture() {
  if (!video.videoWidth || !video.videoHeight) return;
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => {
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = pictureName(new Date());
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }, 'image/png');
}
$('save').addEventListener('click', savePicture);

// Idle: after 3 s with no touch the bar is hidden (not faded), so a tap can't press an
// invisible button. Any tap or mouse move brings it back.
// On touch the browser picks the click target when the finger lifts, which is after the bar is
// back: so after a tap that woke the bar, its buttons ignore taps ("waking") until 400 ms after
// that finger lifts. A tap that lands on a button still showing (the bar was fading) is kept.
let idleTimer = null;
let wakingTimer = null;
function endWaking(delay) {
  clearTimeout(wakingTimer);
  wakingTimer = setTimeout(() => bar.classList.remove('waking'), delay);
}
// The bar stays while the quality menu may be open, and while focus that a key or a screen reader
// moved is in it. Focus left by a touch or click doesn't keep it.
function keepBar() {
  if (document.activeElement === $('quality')) return true;
  return bar.contains(document.activeElement) && !clickFocus;
}
function idle() {
  if (keepBar()) {
    idleTimer = setTimeout(idle, 3000);
    return;
  }
  // A click leaves focus on its button: a hidden button must not keep it, or a key would press it.
  if (bar.contains(document.activeElement)) document.activeElement.blur();
  bar.classList.add('idle');
}
function wake(event) {
  if (event && event.type === 'pointerdown' && bar.classList.contains('idle') && !bar.contains(event.target)) {
    bar.classList.add('waking');
    endWaking(2000); // in case the lift is never reported
  }
  bar.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(idle, 3000);
}
for (const type of ['pointermove', 'pointerdown', 'keydown']) document.addEventListener(type, wake);
// Whatever moves focus into the bar shows it.
bar.addEventListener('focusin', () => {
  clickFocus = !usingKeys && performance.now() - lastPointerAt < 1000;
  wake();
});
document.addEventListener('keydown', () => (usingKeys = true), true);
document.addEventListener(
  'pointerdown',
  () => {
    usingKeys = false;
    lastPointerAt = performance.now();
  },
  true,
);
for (const type of ['pointerup', 'pointercancel']) {
  document.addEventListener(type, () => {
    if (bar.classList.contains('waking')) endWaking(400);
  });
}
wake();

// ---------- pinch-zoom and pan ----------

let view = { scale: 1, x: 0, y: 0 };
const pointers = new Map(); // pointer id -> last position
let pinch = null;
let moved = false;
let lastTap = { time: 0, x: 0, y: 0 };

function geometry() {
  const box = stage.getBoundingClientRect();
  const size = { w: box.width, h: box.height };
  return { box, size, content: contentRect(box.width, box.height, video.videoWidth, video.videoHeight) };
}

// "1:1": one video pixel per device pixel.
function actualScale() {
  const { content } = geometry();
  return actualSizeScale(video.videoWidth, content.w, window.devicePixelRatio || 1);
}

function atActualSize() {
  const target = actualScale();
  return target > 1.001 && Math.abs(view.scale - target) < 0.01;
}

// A focused bar button that hides itself (Fit once the picture fits, the picture buttons when it
// stops) hands the focus to the nearest control still shown, so it doesn't fall back to the top of
// the page. Focus a click left there is moved too; the idle bar takes it away as before.
function keepBarFocus(focused) {
  if (!focused || !focused.hidden || !bar.contains(focused)) return;
  const controls = [...bar.querySelectorAll('button, select')];
  const at = controls.indexOf(focused);
  const shown = (control) => !control.hidden && !control.disabled;
  const next = controls.slice(at + 1).find(shown) || controls.slice(0, at).reverse().find(shown);
  if (next) next.focus({ preventScroll: true });
}

function updateZoomButtons() {
  const focused = document.activeElement;
  $('fit').hidden = !playing || view.scale <= 1.001; // like 1:1, only over a picture
  // Hidden when the picture already shows every pixel at fit: 1:1 would do nothing.
  $('actual').hidden = !playing || actualScale() <= 1.001;
  $('actual').setAttribute('aria-pressed', String(playing && atActualSize()));
  keepBarFocus(focused);
}

function applyView(next) {
  view = next;
  video.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  updateZoomButtons();
}

function fit() {
  const { size, content } = geometry();
  applyView(clampView({ scale: 1, x: 0, y: 0 }, size, content));
}

// Back to fit at once, with no picture to measure: the next one starts whole.
function unzoom() {
  applyView({ scale: 1, x: 0, y: 0 });
}

function refit() {
  const { size, content } = geometry();
  applyView(clampView(view, size, content));
}

// Zoom to 1:1 around the middle of what is on screen now; pressed again, back to fit.
function toggleActualSize() {
  if (atActualSize()) {
    fit();
    return;
  }
  const { size, content } = geometry();
  applyView(zoomAt(view, actualScale() / view.scale, size.w / 2, size.h / 2, size, content));
}

function pinchState() {
  const [a, b] = [...pointers.values()];
  return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2 };
}

function ignored(event) {
  return !overlay.hidden || event.target.closest('.view-bar');
}

stage.addEventListener('pointerdown', (event) => {
  if (ignored(event)) return;
  try {
    stage.setPointerCapture(event.pointerId);
  } catch {
    // the pointer is already gone (or not a real one): carry on without capture
  }
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (pointers.size === 1) moved = false;
  if (pointers.size >= 2) pinch = pinchState(); // always from the two pointers pinchState() measures
});

stage.addEventListener('pointermove', (event) => {
  const point = pointers.get(event.pointerId);
  if (!point) return;
  const { box, size, content } = geometry();
  const dx = event.clientX - point.x;
  const dy = event.clientY - point.y;
  point.x = event.clientX;
  point.y = event.clientY;
  if (pointers.size >= 2 && pinch) {
    const now = pinchState();
    let next = zoomAt(view, now.dist / pinch.dist, now.cx - box.left, now.cy - box.top, size, content);
    next = panBy(next, now.cx - pinch.cx, now.cy - pinch.cy, size, content);
    pinch = now;
    moved = true;
    applyView(next);
  } else if (pointers.size === 1) {
    if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
    if (view.scale > 1) applyView(panBy(view, dx, dy, size, content));
  }
});

function onTap(event) {
  const now = performance.now();
  const isDouble = now - lastTap.time < 300 && Math.hypot(event.clientX - lastTap.x, event.clientY - lastTap.y) < 25;
  if (!isDouble) {
    lastTap = { time: now, x: event.clientX, y: event.clientY };
    return;
  }
  lastTap = { time: 0, x: 0, y: 0 };
  const { box, size, content } = geometry();
  if (view.scale > 1.001) applyView(clampView({ scale: 1, x: 0, y: 0 }, size, content));
  else applyView(zoomAt(view, 2.5, event.clientX - box.left, event.clientY - box.top, size, content));
}

function endPointer(event) {
  if (!pointers.has(event.pointerId)) return;
  pointers.delete(event.pointerId);
  pinch = pointers.size >= 2 ? pinchState() : null; // a third finger lifted: go on from the two that are left
  if (event.type === 'pointerup' && pointers.size === 0 && !moved) onTap(event);
}
stage.addEventListener('pointerup', endPointer);
stage.addEventListener('pointercancel', endPointer);

stage.addEventListener(
  'wheel',
  (event) => {
    if (ignored(event)) return;
    event.preventDefault();
    const { box, size, content } = geometry();
    applyView(zoomAt(view, Math.exp(-event.deltaY * 0.0015), event.clientX - box.left, event.clientY - box.top, size, content));
  },
  { passive: false },
);

// Keys: + and - zoom around the middle, and the arrow keys move a zoomed picture (viewForKey), for
// people without a mouse or a touch screen; 1:1 alone would only ever show the middle. Not while the
// quality menu has the focus (its arrow keys change the quality), not with Ctrl, Cmd or Alt (the
// browser's own zoom and shortcuts), and only while the picture shows.
document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
  if (!playing || !overlay.hidden || (event.target.closest && event.target.closest('select'))) return;
  const { size, content } = geometry();
  const next = viewForKey(event.key, view, size, content);
  if (!next) return;
  event.preventDefault();
  applyView(next);
});

$('fit').addEventListener('click', fit);
$('actual').addEventListener('click', toggleActualSize);

window.addEventListener('resize', refit);
video.addEventListener('loadedmetadata', refit);
video.addEventListener('resize', refit);
document.addEventListener('gesturestart', (event) => event.preventDefault()); // iPhone: no page zoom
