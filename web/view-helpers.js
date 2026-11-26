// Viewer page helpers, kept free of the DOM so they can be unit-tested.

const two = (n) => String(n).padStart(2, '0');

// "Porthole 2026-11-21 14-05-03.png", in local time; no characters that file systems refuse.
export function pictureName(date = new Date()) {
  const when = Number.isFinite(date.getTime()) ? date : new Date();
  const day = `${when.getFullYear()}-${two(when.getMonth() + 1)}-${two(when.getDate())}`;
  const time = `${two(when.getHours())}-${two(when.getMinutes())}-${two(when.getSeconds())}`;
  return `Porthole ${day} ${time}.png`;
}

// What screen readers hear about the viewer's state: the words of the screen shown, in short sentences
// ("Waiting. Ask the sharer to let you in. Your code: 482"); while the picture plays and nothing covers
// it, Paused (the sharer paused), Weak signal or Watching; '' in between, so nothing is said.
export function spokenState({ screen = null, code = null, playing = false, weak = false, paused = false } = {}) {
  if (screen) {
    const parts = [screen.title, screen.detail, code ? `Your code: ${code}` : ''].filter(Boolean);
    return parts.map((part, i) => (i < parts.length - 1 && !/[.!?]$/.test(part) ? `${part}.` : part)).join(' ');
  }
  if (playing) return paused ? 'Paused' : weak ? 'Weak signal' : 'Watching';
  return '';
}

// After the event stream was refused: whether Porthole was restarted, from what the page's own address answers now
// (`answer`: the status and the final address after any redirect; null when nothing answered). In the same run this
// page's address always gives the page itself. A new run answers for it with "This link doesn't work" (404), or, with
// the easy address on, sends it to the new room or to "Nothing shared yet" (503). Anything else, such as "Use the link"
// (400), isn't a restart.
export function wasRestarted(answer, path) {
  if (!answer) return false;
  if (answer.status === 404 || answer.status === 503) return true;
  if (answer.status !== 200) return false;
  let now;
  try {
    now = new URL(answer.url).pathname;
  } catch {
    return false;
  }
  return now.startsWith('/v/') && now !== path;
}

// The id of this tab's viewer connection, kept for a reload (sessionStorage: this tab only, gone when it closes), so
// the reloaded page names its connection before and Porthole can keep its row instead of listing the device twice (only
// if the page is back before Porthole notices the old stream closed, up to a second later, and drops the row). Per
// run (`room`): an id from another run means nothing. Storage may be missing or refuse (a private window): then
// nothing is kept. Only something shaped like a viewer id ("v-" and 8 hex digits, as Porthole makes them) comes back.
const keptKey = (room) => `porthole-viewer-${room}`;

export function keptViewerId(storage, room) {
  try {
    const id = storage.getItem(keptKey(room));
    return typeof id === 'string' && /^v-[0-9a-f]{8}$/.test(id) ? id : '';
  } catch {
    return '';
  }
}

// `id` '' forgets it (the device was turned away, or Porthole ended): a reload then joins afresh.
export function keepViewerId(storage, room, id) {
  try {
    if (id) storage.setItem(keptKey(room), id);
    else storage.removeItem(keptKey(room));
  } catch {
    // nothing kept: a reload joins as a new device, as before
  }
}
