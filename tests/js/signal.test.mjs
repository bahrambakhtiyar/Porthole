import { test } from 'node:test';
import assert from 'node:assert/strict';

globalThis.EventSource = class {
  static CLOSED = 2;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
  }
  close() {
    this.closed = true;
  }
};
const { Signal } = await import('../../web/signal.js');
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test('sends go out one after another, in order', async () => {
  const started = [];
  const release = [];
  globalThis.fetch = (url, options) => {
    started.push(JSON.parse(options.body).n);
    return new Promise((resolve) => release.push(() => resolve({ ok: true })));
  };
  const signal = new Signal({ room: 'r', role: 'viewer', onMessage() {} });
  signal.id = 'v-1';
  const first = signal.send('sharer', { type: 'candidate', n: 1 });
  const second = signal.send('sharer', { type: 'candidate', n: 2 });
  await tick();
  assert.deepEqual(started, [1]);
  release.shift()();
  assert.equal(await first, true);
  await tick();
  assert.deepEqual(started, [1, 2]);
  release.shift()();
  assert.equal(await second, true);
});

test('a failed send resolves false and does not block the next one', async () => {
  let calls = 0;
  globalThis.fetch = () => {
    calls += 1;
    return calls === 1 ? Promise.reject(new Error('offline')) : Promise.resolve({ ok: true });
  };
  const signal = new Signal({ room: 'r', role: 'viewer', onMessage() {} });
  signal.id = 'v-1';
  assert.equal(await signal.send('sharer', { type: 'ready' }), false);
  assert.equal(await signal.send('sharer', { type: 'ready' }), true);
});

test('the device token goes into the events address', () => {
  const signal = new Signal({ room: 'r', role: 'viewer', client: 'Abc_12345', onMessage() {} });
  assert.match(signal.source.url, /[?&]client=Abc_12345/);
  const plain = new Signal({ room: 'r', role: 'sharer', onMessage() {} });
  assert.doesNotMatch(plain.source.url, /client=/);
});

test('extra details go into the events address, and only those that are set', () => {
  const signal = new Signal({ room: 'r', role: 'sharer', params: { page: 'p_123456789', window: 1, touch: 0 }, onMessage() {} });
  assert.match(signal.source.url, /&page=p_123456789&window=1$/);
  const plain = new Signal({ room: 'r', role: 'viewer', params: { touch: false }, onMessage() {} });
  assert.equal(plain.source.url, '/events?room=r&role=viewer');
});

test('a hung request gives up after the timeout so later messages still go out', async () => {
  let calls = 0;
  globalThis.fetch = (url, options) => {
    calls += 1;
    if (calls === 1) {
      return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    }
    return Promise.resolve({ ok: true });
  };
  const signal = new Signal({ room: 'r', role: 'viewer', onMessage() {}, timeoutMs: 50 });
  signal.id = 'v-1';
  const first = signal.send('sharer', { type: 'ready' });
  const second = signal.send('sharer', { type: 'ready' });
  assert.equal(await first, false);
  assert.equal(await second, true);
});

test('messages queued under an old session are dropped', async () => {
  const sent = [];
  const release = [];
  globalThis.fetch = (url) => {
    sent.push(url);
    return new Promise((resolve) => release.push(() => resolve({ ok: true })));
  };
  const signal = new Signal({ room: 'r', role: 'viewer', onMessage() {} });
  signal.id = 'v-1';
  const first = signal.send('sharer', { type: 'answer', sdp: 'x' });
  const stale = signal.send('sharer', { type: 'candidate', candidate: {} });
  await tick();
  signal.id = 'v-2'; // the event stream reconnected with a new id
  release.shift()();
  assert.equal(await first, true);
  assert.equal(await stale, false);
  assert.equal(sent.length, 1);
});
