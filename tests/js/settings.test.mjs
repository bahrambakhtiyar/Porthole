import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULTS, loadSettings, withEasy, withAsk, easyLine, easyView } from '../../web/settings.js';

test('defaults: ask on, smooth off, easy address on', () => {
  assert.deepEqual(DEFAULTS, { ask: true, smooth: false, easy: true });
  assert.deepEqual(loadSettings(null), DEFAULTS);
  assert.deepEqual(loadSettings(''), DEFAULTS);
});

test('broken or odd saved settings fall back to the defaults', () => {
  assert.deepEqual(loadSettings('{not json'), DEFAULTS);
  assert.deepEqual(loadSettings('[1, 2]'), DEFAULTS);
  assert.deepEqual(loadSettings('"text"'), DEFAULTS);
  assert.deepEqual(loadSettings('null'), DEFAULTS);
});

test('settings saved before the easy address turn ask on', () => {
  assert.deepEqual(loadSettings(JSON.stringify({ ask: false, smooth: true })), { ask: true, smooth: true, easy: true });
});

test('settings saved with the easy key load as saved', () => {
  const saved = { ask: false, smooth: true, easy: false };
  assert.deepEqual(loadSettings(JSON.stringify(saved)), saved);
});

test('easy address on never loads with ask off', () => {
  assert.deepEqual(loadSettings(JSON.stringify({ ask: false, smooth: false, easy: true })), { ask: true, smooth: false, easy: true });
});

test('only true or false is taken from saved settings', () => {
  assert.deepEqual(loadSettings(JSON.stringify({ ask: 'no', smooth: 1, easy: false })), { ask: true, smooth: false, easy: false });
});

test('turning easy address on also turns ask on', () => {
  assert.deepEqual(withEasy({ ask: false, smooth: true, easy: false }, true), { ask: true, smooth: true, easy: true });
});

test('turning easy address off leaves ask as it was', () => {
  assert.deepEqual(withEasy({ ask: true, smooth: false, easy: true }, false), { ask: true, smooth: false, easy: false });
});

test('turning ask off also turns easy address off', () => {
  assert.deepEqual(withAsk({ ask: true, smooth: false, easy: true }, false), { ask: false, smooth: false, easy: false });
});

test('turning ask on leaves easy address as it was', () => {
  assert.deepEqual(withAsk({ ask: false, smooth: false, easy: false }, true), { ask: true, smooth: false, easy: false });
});

test('the coupling never changes the object it was given', () => {
  const settings = { ask: true, smooth: false, easy: true };
  withAsk(settings, false);
  withEasy(settings, false);
  assert.deepEqual(settings, { ask: true, smooth: false, easy: true });
});

test('easyLine shows the address people can type', () => {
  const state = { enabled: true, available: true, host: 'porthole.local', url: 'http://porthole.local:8765', short_url: null, error: '' };
  assert.equal(easyLine(state), 'porthole.local:8765');
  assert.equal(easyLine({ ...state, short_url: 'http://porthole.local' }), 'porthole.local');
});

test('easyLine is empty while the name is not announced', () => {
  const state = { enabled: false, available: true, host: 'porthole.local', url: 'http://porthole.local:8765', short_url: null, error: '' };
  assert.equal(easyLine(state), '');
  assert.equal(easyLine({ ...state, enabled: true, available: false }), '');
  assert.equal(easyLine(null), '');
});

test('easyView: a start problem shows as an error while the switch is on', () => {
  const failed = { enabled: false, available: false, blocked: false, error: "Easy address couldn't start on this network." };
  assert.deepEqual(easyView({ easy: true }, failed), { checked: true, disabled: false, error: failed.error, note: '' });
  assert.deepEqual(easyView({ easy: false }, failed), { checked: false, disabled: false, error: '', note: '' });
  assert.deepEqual(easyView({ easy: true }, null), { checked: true, disabled: false, error: '', note: '' });
});

test('easyView: turned off for this run (--no-discovery) is a plain note, with the switch off and greyed out', () => {
  const blocked = { enabled: false, available: false, blocked: true, error: 'Easy address is turned off for this run.' };
  const view = { checked: false, disabled: true, error: '', note: blocked.error };
  assert.deepEqual(easyView({ easy: true }, blocked), view); // the saved choice stays for later runs
  assert.deepEqual(easyView({ easy: false }, blocked), view);
});
