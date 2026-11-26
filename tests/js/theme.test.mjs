import { test } from 'node:test';
import assert from 'node:assert/strict';
import { THEMES, THEME_KEY, pickTheme, readTheme, saveTheme, resolveTheme, applyTheme } from '../../web/theme.js';

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => {
      data[key] = String(value);
    },
  };
}

const brokenStorage = {
  getItem() {
    throw new Error('blocked');
  },
  setItem() {
    throw new Error('blocked');
  },
};

test('three choices, auto first, stored under porthole-theme', () => {
  assert.deepEqual(THEMES, ['auto', 'light', 'dark']);
  assert.equal(THEME_KEY, 'porthole-theme');
  assert.ok(Object.isFrozen(THEMES));
});

test('anything that is not a known choice means auto', () => {
  for (const choice of THEMES) assert.equal(pickTheme(choice), choice);
  for (const odd of [null, undefined, '', 'Dark', 'blue', 1, {}, ['dark']]) assert.equal(pickTheme(odd), 'auto', String(odd));
});

test('the saved choice is read back, and auto when nothing or junk is saved', () => {
  assert.equal(readTheme(memoryStorage({ 'porthole-theme': 'dark' })), 'dark');
  assert.equal(readTheme(memoryStorage({ 'porthole-theme': 'light' })), 'light');
  assert.equal(readTheme(memoryStorage()), 'auto');
  assert.equal(readTheme(memoryStorage({ 'porthole-theme': 'purple' })), 'auto');
});

test('storage that throws (private window) or is missing never breaks the page', () => {
  assert.equal(readTheme(brokenStorage), 'auto');
  assert.equal(readTheme(null), 'auto');
  assert.equal(saveTheme(brokenStorage, 'dark'), false);
  assert.equal(saveTheme(null, 'dark'), false);
});

test('saving keeps only known choices', () => {
  const storage = memoryStorage();
  assert.equal(saveTheme(storage, 'light'), true);
  assert.equal(storage.data['porthole-theme'], 'light');
  saveTheme(storage, 'neon');
  assert.equal(storage.data['porthole-theme'], 'auto');
});

test('auto follows the system, light and dark ignore it', () => {
  assert.equal(resolveTheme('auto', true), 'dark');
  assert.equal(resolveTheme('auto', false), 'light');
  assert.equal(resolveTheme('light', true), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
  assert.equal(resolveTheme('junk', true), 'dark'); // junk is auto
});

test('the choice is set as data-theme on the root element', () => {
  const attributes = {};
  const root = { setAttribute: (name, value) => (attributes[name] = value) };
  assert.equal(applyTheme(root, 'dark'), 'dark');
  assert.equal(attributes['data-theme'], 'dark');
  assert.equal(applyTheme(root, 'nope'), 'auto');
  assert.equal(attributes['data-theme'], 'auto');
});
