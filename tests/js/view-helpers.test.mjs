import { test } from 'node:test';
import assert from 'node:assert/strict';
import { keepViewerId, keptViewerId, pictureName, spokenState, wasRestarted } from '../../web/view-helpers.js';

test('a saved picture is named after the local date and time', () => {
  assert.equal(pictureName(new Date(2026, 10, 21, 14, 5, 3)), 'Porthole 2026-11-21 14-05-03.png');
  assert.equal(pictureName(new Date(2027, 0, 2, 0, 0, 0)), 'Porthole 2027-01-02 00-00-00.png');
  assert.equal(pictureName(new Date(2026, 11, 31, 23, 59, 59)), 'Porthole 2026-12-31 23-59-59.png');
});

test('the name has no characters that file systems refuse', () => {
  assert.doesNotMatch(pictureName(new Date(2026, 10, 21, 9, 8, 7)), /[:/\\<>|?*"]/);
});

test('a broken date still gives a usable name', () => {
  assert.match(pictureName(new Date(NaN)), /^Porthole \d{4}-\d\d-\d\d \d\d-\d\d-\d\d\.png$/);
});

// ---------- what screen readers hear ----------

test('the state is said in short sentences, with the check code while waiting', () => {
  const approval = { title: 'Waiting', detail: 'Ask the sharer to let you in.' };
  assert.equal(spokenState({ screen: approval, code: 482 }), 'Waiting. Ask the sharer to let you in. Your code: 482');
  assert.equal(spokenState({ screen: approval, code: null }), 'Waiting. Ask the sharer to let you in.');
  assert.equal(spokenState({ screen: { title: 'Connecting' } }), 'Connecting');
  assert.equal(spokenState({ screen: { title: "Can't connect", detail: 'Trying again.' } }), "Can't connect. Trying again.");
});

test('while the picture plays: Watching, or Weak signal', () => {
  assert.equal(spokenState({ screen: null, playing: true, weak: false }), 'Watching');
  assert.equal(spokenState({ screen: null, playing: true, weak: true }), 'Weak signal');
});

test('while the sharer has paused: Paused, also over a weak signal', () => {
  assert.equal(spokenState({ screen: null, playing: true, paused: true }), 'Paused');
  assert.equal(spokenState({ screen: null, playing: true, paused: true, weak: true }), 'Paused');
  // Before the picture plays (joining while paused) the screen's words come first, and nothing between states.
  assert.equal(spokenState({ screen: { title: 'Connecting' }, playing: false, paused: true }), 'Connecting');
  assert.equal(spokenState({ screen: null, playing: false, paused: true }), '');
});

test('a screen over the picture wins over Watching', () => {
  assert.equal(spokenState({ screen: { title: 'Tap to play' }, playing: true }), 'Tap to play');
});

test('nothing to say between states', () => {
  assert.equal(spokenState({ screen: null, playing: false }), '');
  assert.equal(spokenState({}), '');
});

// ---------- the viewer id kept for a reload ----------

function memoryStorage() {
  const items = new Map();
  return {
    items,
    getItem: (key) => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => items.set(key, String(value)),
    removeItem: (key) => items.delete(key),
  };
}

test('the id is kept for this run and given back after a reload', () => {
  const storage = memoryStorage();
  assert.equal(keptViewerId(storage, 'room1'), '');
  keepViewerId(storage, 'room1', 'v-1a2b3c4d');
  assert.equal(keptViewerId(storage, 'room1'), 'v-1a2b3c4d');
  assert.equal(keptViewerId(storage, 'room2'), ''); // another run's link: nothing to come back to
});

test('forgetting it leaves nothing for the next load', () => {
  const storage = memoryStorage();
  keepViewerId(storage, 'room1', 'v-1a2b3c4d');
  keepViewerId(storage, 'room1', '');
  assert.equal(keptViewerId(storage, 'room1'), '');
  assert.equal(storage.items.size, 0);
});

test('only something shaped like a viewer id is given back', () => {
  const storage = memoryStorage();
  for (const value of ['s-1a2b3c4d', 'v-1A2B3C4D', 'v-1a2b3c4d\n', 'v-1a2b', '<b>', '']) {
    storage.setItem('porthole-viewer-room1', value);
    assert.equal(keptViewerId(storage, 'room1'), '', value);
  }
});

test('storage that is missing or refuses (a private window) keeps nothing and never throws', () => {
  const refusing = {
    getItem() {
      throw new Error('SecurityError');
    },
    setItem() {
      throw new Error('QuotaExceededError');
    },
    removeItem() {
      throw new Error('SecurityError');
    },
  };
  assert.equal(keptViewerId(refusing, 'room1'), '');
  assert.doesNotThrow(() => keepViewerId(refusing, 'room1', 'v-1a2b3c4d'));
  assert.doesNotThrow(() => keepViewerId(refusing, 'room1', ''));
  assert.equal(keptViewerId(null, 'room1'), '');
  assert.doesNotThrow(() => keepViewerId(undefined, 'room1', 'v-1a2b3c4d'));
});

// ---------- after the event stream was refused ----------

test('Porthole was restarted when its new run answers for this page with another room, or none', () => {
  const path = '/v/oldroom';
  // The link has expired (the easy address off), the share isn't on yet (on), or it sends this page to its room (on).
  assert.equal(wasRestarted({ status: 404, url: 'http://192.168.1.20:8765/v/oldroom' }, path), true);
  assert.equal(wasRestarted({ status: 503, url: 'http://porthole.local:8765/v/oldroom' }, path), true);
  assert.equal(wasRestarted({ status: 200, url: 'http://porthole.local:8765/v/newroom' }, path), true);
});

test('nothing answering, or the same run, is not a restart', () => {
  const path = '/v/oldroom';
  assert.equal(wasRestarted(null, path), false);
  assert.equal(wasRestarted({ status: 200, url: 'http://192.168.1.20:8765/v/oldroom' }, path), false); // this run
  assert.equal(wasRestarted({ status: 400, url: 'http://porthole.local:8765/v/oldroom' }, path), false); // "Use the link"
  assert.equal(wasRestarted({ status: 200, url: 'http://192.168.1.20:8765/share' }, path), false);
  assert.equal(wasRestarted({ status: 200, url: 'not a url' }, path), false);
  assert.equal(wasRestarted({ status: 500, url: 'http://192.168.1.20:8765/v/oldroom' }, path), false);
});
