import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LEVELS, newTuner, decide, senderSample, receiverSample, isWeak, describe, weakBadge } from '../../web/quality.js';

const bad = { loss: 0.1, rtt: 0.05, available: 0, fps: 30 };
const good = { loss: 0, rtt: 0.02, available: 0, fps: 30 };
const run = (tuner, samples, mode = 'auto') => samples.reduce((t, s) => decide(t, s, mode), tuner);
const times = (sample, n) => Array(n).fill(sample);

test("the viewer's Weak signal badge doesn't blink when loss hovers around the limit", () => {
  const after = (samples) => samples.reduce((badge, weak) => weakBadge(badge, weak), undefined);
  assert.equal(after([true]).shown, false); // one weak sample isn't enough
  assert.equal(after([true, true]).shown, true);
  assert.equal(after([true, false, true, false, true]).shown, false); // hovering: never shown
  assert.equal(after([true, true, false, false]).shown, true); // shown: two clean samples aren't enough
  assert.equal(after([true, true, false, false, false]).shown, false);
  assert.equal(after([true, true, false, true, false, false, false]).shown, false);
  assert.equal(after([true, true, false, true, false, false]).shown, true); // the clean run started again
});

test('there are four levels from full to data saver', () => {
  assert.equal(LEVELS.length, 4);
  assert.deepEqual(LEVELS.map((l) => l.maxBitrate), [8_000_000, 4_000_000, 2_000_000, 1_000_000]);
  assert.deepEqual(LEVELS.map((l) => l.scale), [1, 4 / 3, 2, 2]);
});

test('two bad samples in a row step down one level', () => {
  assert.equal(run(newTuner(), [bad]).level, 0);
  assert.equal(run(newTuner(), [bad, bad]).level, 1);
});

test('a good sample resets the bad count', () => {
  assert.equal(run(newTuner(), [bad, good, bad]).level, 0);
});

test('nothing changes for three samples after a step', () => {
  assert.equal(run(newTuner(), times(bad, 5)).level, 1);
  assert.equal(run(newTuner(), times(bad, 7)).level, 2);
});

test('five good samples step up one level', () => {
  const low = { ...newTuner(), level: 2 };
  assert.equal(run(low, times(good, 4)).level, 2);
  assert.equal(run(low, times(good, 5)).level, 1);
});

test('levels stay within bounds', () => {
  assert.equal(run(newTuner(), times(bad, 40)).level, 3);
  assert.equal(run(newTuner(), times(good, 40)).level, 0);
});

test('high pins full quality and data saver pins the lowest level', () => {
  assert.equal(run(newTuner(), times(bad, 10), 'high').level, 0);
  assert.equal(run(newTuner(), [good], 'saver').level, 3);
});

test('little spare bandwidth counts as bad only when the encoder is held back by it', () => {
  const quiet = { loss: 0, rtt: 0.02, available: 3_000_000, fps: 2, limited: 'none' };
  assert.equal(run(newTuner(), [quiet, quiet, quiet]).level, 0); // a still screen just has little to send
  const tight = { loss: 0, rtt: 0.02, available: 3_000_000, fps: 30, limited: 'bandwidth' };
  assert.equal(run(newTuner(), [tight, tight]).level, 1);
  const atLevel1 = { ...newTuner(), level: 1 };
  assert.equal(run(atLevel1, [tight, tight]).level, 1);
});

test('senderSample reads stats and computes bitrate from the byte delta', () => {
  const stats = new Map([
    ['o', { type: 'outbound-rtp', kind: 'video', bytesSent: 1_000_000, framesPerSecond: 30, frameWidth: 1280, frameHeight: 720, qualityLimitationReason: 'bandwidth' }],
    ['a', { type: 'outbound-rtp', kind: 'audio', bytesSent: 99_999_999 }],
    ['r', { type: 'remote-inbound-rtp', kind: 'video', fractionLost: 0.02, roundTripTime: 0.01 }],
    ['c', { type: 'candidate-pair', nominated: true, state: 'succeeded', availableOutgoingBitrate: 5e6, currentRoundTripTime: 0.012 }],
  ]);
  const first = senderSample(null, stats, 0);
  assert.equal(first.bitrate, 0);
  const sample = senderSample({ time: 0, bytes: 500_000 }, stats, 2000);
  assert.equal(sample.bitrate, 2_000_000);
  assert.equal(sample.fps, 30);
  assert.equal(sample.width, 1280);
  assert.equal(sample.loss, 0.02);
  assert.equal(sample.rtt, 0.01);
  assert.equal(sample.available, 5e6);
  assert.equal(sample.bytes, 1_000_000);
  assert.equal(sample.limited, 'bandwidth');
});

test('isWeak flags loss, delay and a bandwidth-limited encoder, not a still screen', () => {
  assert.equal(isWeak(good), false);
  assert.equal(isWeak(bad), true);
  assert.equal(isWeak({ ...good, rtt: 0.5 }), true);
  assert.equal(isWeak({ ...good, limited: 'bandwidth' }), true);
  assert.equal(isWeak({ ...good, fps: 2 }), false);
  assert.equal(isWeak({ loss: 0, fps: 3 }), false); // viewer sample of a quiet screen
});

test('describe formats resolution, frame rate and bitrate', () => {
  assert.equal(describe({ width: 1280, height: 720, fps: 29.6, bitrate: 4_100_000 }), '1280×720 · 30 fps · 4.1 Mbps');
  assert.equal(describe({ width: 640, height: 360, fps: 15, bitrate: 850_000 }), '640×360 · 15 fps · 850 kbps');
  assert.equal(describe({ width: 0, height: 0, fps: 0, bitrate: 0 }), '');
});

test('receiverSample computes loss from packet deltas', () => {
  const stats = new Map([['i', { type: 'inbound-rtp', kind: 'video', packetsLost: 10, packetsReceived: 190, framesPerSecond: 25 }]]);
  const sample = receiverSample({ packetsLost: 0, packetsReceived: 100 }, stats);
  assert.ok(Math.abs(sample.loss - 0.1) < 1e-9);
  assert.equal(sample.fps, 25);
  assert.equal(receiverSample(null, stats).loss, 0);
});
