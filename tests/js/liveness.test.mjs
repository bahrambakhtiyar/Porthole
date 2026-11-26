// Signal notices a dead event stream (40 s of silence, or "unknown peer") and opens a new one.
import { test, mock, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';

const sources = [];
globalThis.EventSource = class {
  static CLOSED = 2;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    sources.push(this);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(msg) {
    this.onmessage({ data: JSON.stringify(msg) });
  }
};
const { Signal, retryOnce } = await import('../../web/signal.js');
const flush = () => new Promise((resolve) => setImmediate(resolve));

function make(extra = {}) {
  const got = { messages: [], statuses: [] };
  const signal = new Signal({
    room: 'r',
    role: 'viewer',
    client: 'Abc_12345',
    onMessage: (msg) => got.messages.push(msg),
    onStatus: (status) => got.statuses.push(status),
    ...extra,
  });
  return { signal, got };
}

beforeEach(() => {
  sources.length = 0;
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: 1_000_000 });
});
afterEach(() => mock.timers.reset());

test('ping events count as life but never reach the page', () => {
  const { signal, got } = make();
  sources[0].emit({ type: 'hello', id: 'v-1', code: 482 });
  for (let i = 0; i < 6; i += 1) {
    mock.timers.tick(15000);
    sources[0].emit({ type: 'ping' });
  }
  assert.equal(sources.length, 1); // 90 s, but never 40 s of silence
  assert.deepEqual(got.messages, [{ type: 'hello', id: 'v-1', code: 482 }]);
  assert.equal(signal.lastMessageAt, Date.now());
  signal.close();
});

test('40 s of silence closes the stream and opens a new one', () => {
  const { signal, got } = make();
  const first = sources[0];
  first.emit({ type: 'hello', id: 'v-1' });
  mock.timers.tick(39000);
  assert.equal(sources.length, 1);
  mock.timers.tick(2000);
  assert.equal(first.closed, true);
  assert.equal(sources.length, 2);
  assert.equal(sources[1].url, `${first.url}&prev=v-1`); // same room, role and device token, and the connection before
  assert.equal(signal.id, null);
  assert.deepEqual(got.statuses, ['open', 'reconnecting']);
  sources[1].emit({ type: 'hello', id: 'v-2' }); // the page resets on the next hello, as today
  assert.equal(signal.id, 'v-2');
  assert.deepEqual(got.statuses, ['open', 'reconnecting', 'open']);
  signal.close();
});

test('a stream that never says anything is reopened too', () => {
  const { signal } = make();
  mock.timers.tick(41000);
  assert.equal(sources.length, 2);
  mock.timers.tick(41000); // still nothing: try again
  assert.equal(sources.length, 3);
  signal.close();
});

test('messages from the old stream are ignored after a reopen', () => {
  const { signal, got } = make();
  const first = sources[0];
  mock.timers.tick(41000);
  first.emit({ type: 'hello', id: 'v-old' });
  first.onerror();
  assert.equal(signal.id, null);
  assert.deepEqual(got.messages, []);
  assert.deepEqual(got.statuses, ['reconnecting']);
  signal.close();
});

test('close stops watching for silence', () => {
  const { signal } = make();
  signal.close();
  mock.timers.tick(120000);
  assert.equal(sources.length, 1);
  assert.equal(sources[0].closed, true);
});

test('silenceMs 0 turns the silence check off', () => {
  const { signal } = make({ silenceMs: 0 });
  mock.timers.tick(120000);
  assert.equal(sources.length, 1);
  signal.close();
});

function reply(status, body) {
  return Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(body) });
}

test("a 404 'unknown peer' reopens the stream", async () => {
  const { signal, got } = make();
  sources[0].emit({ type: 'hello', id: 'v-1' });
  globalThis.fetch = () => reply(404, { error: 'unknown peer' });
  assert.equal(await signal.send('sharer', { type: 'ready' }), false);
  assert.equal(sources.length, 2);
  assert.equal(sources[0].closed, true);
  assert.deepEqual(got.statuses, ['open', 'reconnecting']);
  signal.close();
});

// A viewer names its connection before when it opens a new stream, so Porthole can hand this device's row to the new
// connection (same name, code and approval) instead of listing it twice for half a minute. Porthole checks the rest.
test('a viewer names its connection before in every new stream', () => {
  const { signal } = make();
  const first = sources[0];
  assert.doesNotMatch(first.url, /prev=/); // nothing before
  first.emit({ type: 'hello', id: 'v-1a2b3c4d' });
  mock.timers.tick(41000);
  assert.equal(sources[1].url, '/events?room=r&role=viewer&client=Abc_12345&prev=v-1a2b3c4d');
  sources[1].emit({ type: 'hello', id: 'v-5e6f7a8b' }); // Porthole gave the row a new id
  mock.timers.tick(41000);
  assert.match(sources[2].url, /&prev=v-5e6f7a8b$/);
  mock.timers.tick(41000); // no hello this time: still the last id it had
  assert.match(sources[3].url, /&prev=v-5e6f7a8b$/);
  signal.close();
});

test('an id from before a reload goes into the first address', () => {
  const { signal } = make({ previous: 'v-0a0b0c0d' });
  assert.equal(sources[0].url, '/events?room=r&role=viewer&client=Abc_12345&prev=v-0a0b0c0d');
  signal.close();
});

test('a hello from a stream already given up on is not remembered', () => {
  const { signal } = make({ previous: 'v-0a0b0c0d' });
  const first = sources[0];
  mock.timers.tick(41000);
  first.emit({ type: 'hello', id: 'v-old00000' });
  mock.timers.tick(41000);
  assert.match(sources[2].url, /&prev=v-0a0b0c0d$/);
  signal.close();
});

test("the stream reopened after 'unknown peer' names the connection before", async () => {
  const { signal } = make();
  sources[0].emit({ type: 'hello', id: 'v-1a2b3c4d' });
  globalThis.fetch = () => reply(404, { error: 'unknown peer' });
  await signal.send('sharer', { type: 'ready' });
  assert.match(sources[1].url, /&prev=v-1a2b3c4d$/);
  signal.close();
});

test('a sharer page never names one', () => {
  const { signal } = make({ role: 'sharer', client: '', previous: 'v-0a0b0c0d' });
  sources[0].emit({ type: 'hello', id: 's-1', viewers: [] });
  mock.timers.tick(41000);
  for (const source of sources) assert.doesNotMatch(source.url, /prev=/);
  signal.close();
});

test('other 404s keep the stream', async () => {
  const { signal } = make({ role: 'sharer' });
  sources[0].emit({ type: 'hello', id: 's-1', viewers: [] });
  globalThis.fetch = () => reply(404, { error: 'viewer has left' });
  assert.equal(await signal.send('v-9', { type: 'stopped' }), false);
  globalThis.fetch = () => Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(new Error('no body')) });
  assert.equal(await signal.send('v-9', { type: 'stopped' }), false);
  assert.equal(sources.length, 1);
  signal.close();
});

test("a late 'unknown peer' from an older session does nothing", async () => {
  const { signal } = make();
  sources[0].emit({ type: 'hello', id: 'v-1' });
  let release;
  globalThis.fetch = () => new Promise((resolve) => (release = () => resolve({ ok: false, status: 404, json: () => Promise.resolve({ error: 'unknown peer' }) })));
  const sent = signal.send('sharer', { type: 'ready' });
  await flush();
  sources[0].emit({ type: 'hello', id: 'v-2' }); // reconnected meanwhile
  release();
  assert.equal(await sent, false);
  assert.equal(sources.length, 1);
  signal.close();
});

test('retryOnce sends again after 2 s when the first try failed', async () => {
  const results = [false, true];
  let calls = 0;
  const done = retryOnce(() => Promise.resolve(results[calls++]), () => true);
  await flush();
  assert.equal(calls, 1);
  mock.timers.tick(1999);
  await flush();
  assert.equal(calls, 1);
  mock.timers.tick(1);
  await flush();
  assert.equal(calls, 2);
  assert.equal(await done, true);
});

test('retryOnce does not retry a success, or a connection that was replaced', async () => {
  let calls = 0;
  assert.equal(await retryOnce(() => Promise.resolve((calls += 1) > 0), () => true), true);
  assert.equal(calls, 1);
  calls = 0;
  let current = true;
  const done = retryOnce(() => Promise.resolve(!(calls += 1)), () => current);
  await flush();
  current = false; // a newer connection replaced this one
  mock.timers.tick(2000);
  assert.equal(await done, false);
  assert.equal(calls, 1);
});

test('retryOnce tries at most twice', async () => {
  let calls = 0;
  const done = retryOnce(() => Promise.resolve(Boolean((calls += 1) > 5)), () => true, 500);
  await flush();
  mock.timers.tick(500);
  assert.equal(await done, false);
  mock.timers.tick(5000);
  await flush();
  assert.equal(calls, 2);
});
