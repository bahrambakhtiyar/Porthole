// The browser-test runner's helpers (tests/browser/support.mjs): finding Chrome and stopping what it started,
// on Linux, macOS and Windows.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { chromeInstallPaths, defaultPython, findChrome, stopTreeCommand } from '../browser/support.mjs';

const WINDOWS_ENV = {
  ProgramFiles: 'C:\\Program Files',
  'ProgramFiles(x86)': 'C:\\Program Files (x86)',
  LOCALAPPDATA: 'C:\\Users\\runner\\AppData\\Local',
  PATH: 'C:\\Windows\\system32',
};
const present = (files) => (file) => files.includes(file);

test('CHROME_PATH wins, and only if it exists', () => {
  assert.equal(findChrome({ CHROME_PATH: '/opt/chrome' }, 'linux', present(['/opt/chrome'])), '/opt/chrome');
  assert.equal(findChrome({ CHROME_PATH: '/opt/chrome' }, 'linux', present(['/usr/bin/chromium'])), null);
});

test('Linux looks on the PATH', () => {
  const env = { PATH: '/usr/local/bin:/usr/bin' };
  assert.equal(findChrome(env, 'linux', present(['/usr/bin/chromium'])), '/usr/bin/chromium');
  assert.equal(findChrome(env, 'linux', present(['/usr/bin/google-chrome', '/usr/bin/chromium'])), '/usr/bin/google-chrome');
  assert.equal(findChrome(env, 'linux', present([])), null);
});

test('Windows looks in the install folders, Chrome before Chromium', () => {
  for (const file of [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Users\\runner\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Users\\runner\\AppData\\Local\\Chromium\\Application\\chrome.exe',
  ]) {
    assert.equal(findChrome(WINDOWS_ENV, 'win32', present([file])), file);
  }
  const chromium = 'C:\\Program Files\\Chromium\\Application\\chrome.exe';
  const chrome = 'C:\\Users\\runner\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe';
  assert.equal(findChrome(WINDOWS_ENV, 'win32', present([chromium, chrome])), chrome);
});

test('Windows variable names ignore case and have defaults', () => {
  const chrome = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
  assert.equal(findChrome({}, 'win32', present([chrome])), chrome);
  const custom = 'D:\\Apps\\Google\\Chrome\\Application\\chrome.exe';
  assert.equal(findChrome({ PROGRAMFILES: 'D:\\Apps' }, 'win32', present([custom])), custom);
});

test('Windows also finds chrome.exe on the PATH', () => {
  const env = { Path: 'C:\\Tools;C:\\Windows' };
  assert.equal(findChrome(env, 'win32', present(['C:\\Tools\\chrome.exe'])), 'C:\\Tools\\chrome.exe');
});

test('macOS looks in Applications', () => {
  const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  assert.equal(findChrome({}, 'darwin', present([chrome])), chrome);
  assert.deepEqual(chromeInstallPaths('linux', {}), []);
});

test('stopping a process tree', () => {
  assert.deepEqual(stopTreeCommand(4242, 'win32'), ['taskkill', ['/PID', '4242', '/T', '/F']]);
  assert.equal(stopTreeCommand(4242, 'linux'), null); // a process group is signalled instead
});

test('the default Python command', () => {
  assert.equal(defaultPython({ PYTHON: 'C:\\Python312\\python.exe' }, 'win32'), 'C:\\Python312\\python.exe');
  assert.equal(defaultPython({}, 'win32'), 'python');
  assert.equal(defaultPython({}, 'linux'), 'python3');
});
