import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { retrier } from '../../web/signal.js';

test('keeps retrying with doubling delay up to the cap until stopped', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  let calls = 0;
  const retry = retrier(() => { calls += 1; }, { max: 10000 });
  retry.start(1000);
  mock.timers.tick(999);
  assert.equal(calls, 0);
  mock.timers.tick(1);
  assert.equal(calls, 1);
  mock.timers.tick(2000);
  assert.equal(calls, 2);
  mock.timers.tick(4000);
  assert.equal(calls, 3);
  mock.timers.tick(8000);
  assert.equal(calls, 4);
  mock.timers.tick(10000);
  assert.equal(calls, 5);
  retry.stop();
  mock.timers.tick(60000);
  assert.equal(calls, 5);
  mock.timers.reset();
});

test('start while running keeps the current schedule', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  let calls = 0;
  const retry = retrier(() => { calls += 1; });
  retry.start(1000);
  mock.timers.tick(900);
  retry.start(5000);
  mock.timers.tick(100);
  assert.equal(calls, 1);
  assert.equal(retry.running, true);
  retry.stop();
  assert.equal(retry.running, false);
  mock.timers.reset();
});

test('start after stop begins again from the first delay', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  let calls = 0;
  const retry = retrier(() => { calls += 1; });
  retry.start(1000);
  mock.timers.tick(3000);
  retry.stop();
  const before = calls;
  retry.start(500);
  mock.timers.tick(500);
  assert.equal(calls, before + 1);
  retry.stop();
  mock.timers.reset();
});
