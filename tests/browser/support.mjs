// Helpers for the browser-test runner (run.mjs) and the screenshot tool (tools/screenshots.mjs).
// The first part differs between Linux, macOS and Windows: tests/js/browser-support.test.mjs checks it for
// every system, so it takes the system as an argument. The second part starts Porthole and headless Chrome,
// drives Chrome over the DevTools protocol (Node 22 or newer, no packages) and stops everything again.

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const CHROME_NAMES = ['google-chrome', 'chromium', 'chromium-browser'];
// Windows: Chrome (for everyone or for one user), then Chromium, under these folders.
const WINDOWS_CHROMES = [
  ['Google', 'Chrome', 'Application', 'chrome.exe'],
  ['Chromium', 'Application', 'chrome.exe'],
];
const WINDOWS_FOLDERS = [
  ['PROGRAMFILES', 'C:\\Program Files'],
  ['PROGRAMFILES(X86)', 'C:\\Program Files (x86)'],
  ['LOCALAPPDATA', null],
];
const MAC_CHROMES = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
];

export function isFile(file) {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

// Windows variable names ignore case (Path, PATH; ProgramFiles, PROGRAMFILES).
function upperEnv(env) {
  return Object.fromEntries(Object.entries(env).map(([key, value]) => [key.toUpperCase(), value]));
}

// Where Chrome is installed when it isn't on the PATH.
export function chromeInstallPaths(platform, env) {
  if (platform === 'win32') {
    const upper = upperEnv(env);
    const folders = WINDOWS_FOLDERS.map(([name, fallback]) => upper[name] || fallback).filter(Boolean);
    return WINDOWS_CHROMES.flatMap((inside) => folders.map((folder) => path.win32.join(folder, ...inside)));
  }
  if (platform === 'darwin') return [...MAC_CHROMES];
  return [];
}

// CHROME_PATH (and only that, when set), else Chrome or Chromium on the PATH, else in its install folder.
export function findChrome(env = process.env, platform = process.platform, fileExists = isFile) {
  if (env.CHROME_PATH) return fileExists(env.CHROME_PATH) ? env.CHROME_PATH : null;
  const windows = platform === 'win32';
  const paths = windows ? path.win32 : path.posix;
  const dirs = (upperEnv(env).PATH || '').split(windows ? ';' : ':').filter(Boolean);
  const names = windows ? ['chrome.exe', ...CHROME_NAMES.map((name) => `${name}.exe`)] : CHROME_NAMES;
  for (const name of names) {
    for (const dir of dirs) {
      const candidate = paths.join(dir, name);
      if (fileExists(candidate)) return candidate;
    }
  }
  return chromeInstallPaths(platform, env).find(fileExists) || null;
}

// The command that stops a process and everything it started, where a process group can't be signalled:
// Windows has taskkill /T. Elsewhere null (the caller signals the process group).
export function stopTreeCommand(pid, platform = process.platform) {
  return platform === 'win32' ? ['taskkill', ['/PID', String(pid), '/T', '/F']] : null;
}

export function defaultPython(env = process.env, platform = process.platform) {
  return env.PYTHON || (platform === 'win32' ? 'python' : 'python3');
}

// ---------- running Porthole and Chrome ----------

// A cold Chrome on a busy CI machine has taken over 20 s to start.
const CHROME_START_MS = 60_000;

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

const children = new Set(); // still running
export const started = []; // everything started, for the failure report
const tempDirs = [];
const GROUPS = process.platform !== 'win32'; // Chrome gets its own process group, to stop its helpers too

// `group`: Chrome, with its helper processes. Linux and macOS stop its process group (`kill -<pid>`);
// Windows has no process groups to signal and stops the process tree with taskkill instead, which also
// reaches the real Python behind a launcher such as a virtual environment's python.exe (`tree`).
function track(child, name, { group = false, tree = false } = {}) {
  child.label = name;
  child.group = group && GROUPS;
  child.tree = (group || tree) && !GROUPS;
  child.output = '';
  children.add(child);
  started.push(child);
  const keep = (chunk) => {
    child.output = (child.output + chunk.toString()).slice(-20000);
  };
  child.stdout?.on('data', keep);
  child.stderr?.on('data', keep);
  child.exited = new Promise((resolve) => child.once('exit', (code, signal) => resolve({ code, signal })));
  child.exited.then(() => children.delete(child));
  return child;
}

export function makeTempDir(name) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `porthole-${name}-`));
  tempDirs.push(dir);
  return dir;
}

function signalChild(child, signal) {
  try {
    child.kill(signal);
  } catch {
    // already gone
  }
}

// Chrome's helper processes (e.g. its network service) can outlive the main one for a moment and
// would write into the profile folder after it was removed: stop the whole group (or tree), once.
function killGroup(child) {
  if (!(child.group || child.tree) || child.groupKilled) return;
  child.groupKilled = true;
  const command = child.tree && stopTreeCommand(child.pid);
  if (command) {
    spawnSync(command[0], command[1], { stdio: 'ignore', windowsHide: true, timeout: 20000 });
    return;
  }
  try {
    process.kill(-child.pid, 'SIGKILL');
  } catch {
    // the group is gone already
  }
}

export function killAll() {
  for (const child of started) {
    killGroup(child);
    if (children.has(child)) signalChild(child, 'SIGKILL');
  }
}

export function removeTempDirs() {
  for (const dir of tempDirs.splice(0)) {
    try {
      fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    } catch (err) {
      console.log(`  (couldn't remove ${dir}: ${err.message})`);
    }
  }
}

export async function stopChild(child, graceMs = 5000) {
  if (child.tree && children.has(child)) {
    // Windows: taskkill /T follows the parent links, so stop the tree while its top process still runs.
    killGroup(child);
    await Promise.race([child.exited, sleep(graceMs)]);
  }
  if (children.has(child)) {
    signalChild(child, 'SIGTERM');
    const done = await Promise.race([child.exited, sleep(graceMs).then(() => null)]);
    if (!done) {
      signalChild(child, 'SIGKILL');
      await child.exited;
    }
  }
  if ((child.group || child.tree) && !child.groupKilled) {
    killGroup(child);
    await sleep(300); // let the helpers go before their folder is removed
  }
}

// Whatever happens, nothing started is left running and no temporary folder is left behind.
export function cleanUpOnExit() {
  process.on('exit', () => {
    killAll();
    removeTempDirs();
  });
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => {
      killAll();
      removeTempDirs();
      process.exit(1);
    });
  }
}

// Starts Porthole (`python start.py <args>` in `root`) and waits until it answers on `base`.
export async function startPorthole({ python, root, args, base }) {
  const server = track(
    spawn(python, ['start.py', ...args], {
      cwd: root,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    }),
    'server',
    { tree: true },
  );
  for (let i = 0; i < 100; i += 1) {
    if (!children.has(server)) throw new Error(`the server stopped at start:\n${server.output}`);
    try {
      const res = await fetch(`${base}/api/ping`);
      if (res.ok && (await res.json()).app === 'porthole') return server;
    } catch {
      // not listening yet
    }
    await sleep(100);
  }
  throw new Error(`the server didn't start:\n${server.output}`);
}

// Starts headless Chrome with its own profile in `profileDir`; returns it and a DevTools connection.
export async function startChrome(chrome, profileDir) {
  const browser = track(
    spawn(
      chrome,
      [
        '--headless=new',
        '--remote-debugging-port=0',
        `--user-data-dir=${profileDir}`,
        '--autoplay-policy=no-user-gesture-required',
        '--use-fake-ui-for-media-stream',
        '--no-first-run',
        '--no-default-browser-check',
        // macOS: a fresh profile would otherwise ask the Keychain for a password store (other systems ignore it).
        '--use-mock-keychain',
        'about:blank',
      ],
      { stdio: ['ignore', 'ignore', 'pipe'], detached: GROUPS },
    ),
    'chrome',
    { group: true },
  );
  const wsUrl = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Chrome didn't report its DevTools address:\n${browser.output}`)), CHROME_START_MS);
    const look = () => {
      const match = /DevTools listening on (ws:\/\/\S+)/.exec(browser.output);
      if (match) {
        clearTimeout(timer);
        resolve(match[1]);
      }
    };
    browser.stderr.on('data', look);
    browser.exited.then(() => reject(new Error(`Chrome stopped:\n${browser.output}`)));
    look();
  });
  return { browser, cdp: await Cdp.connect(wsUrl) };
}

// Closes Chrome politely, then makes sure it and its helpers are gone.
export async function stopChrome(browser, cdp) {
  try {
    await Promise.race([cdp.send('Browser.close'), sleep(3000)]);
  } catch {
    // Chrome already gone
  }
  cdp.close();
  await stopChild(browser);
}

// ---------- the DevTools protocol ----------

export class Cdp {
  static connect(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.addEventListener('open', () => resolve(new Cdp(ws)), { once: true });
      ws.addEventListener('error', () => reject(new Error(`can't connect to ${url}`)), { once: true });
    });
  }

  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    ws.addEventListener('message', (event) => this.receive(JSON.parse(event.data)));
    ws.addEventListener('close', () => {
      for (const { reject, method } of this.pending.values()) reject(new Error(`${method}: connection closed`));
      this.pending.clear();
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      this.ws.send(JSON.stringify(message));
    });
  }

  receive(message) {
    if (message.id) {
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(`${waiter.method}: ${message.error.message}`));
      else waiter.resolve(message.result);
      return;
    }
    for (const listener of this.listeners) listener(message);
  }

  close() {
    try {
      this.ws.close();
    } catch {
      // already closed
    }
  }
}

export class Page {
  constructor(cdp, name, sessionId) {
    this.cdp = cdp;
    this.name = name;
    this.sessionId = sessionId;
    this.errors = [];
    cdp.listeners.add((message) => {
      if (message.sessionId !== sessionId) return;
      if (message.method === 'Runtime.exceptionThrown') {
        const details = message.params.exceptionDetails;
        this.errors.push((details.exception && details.exception.description) || details.text);
      } else if (message.method === 'Page.javascriptDialogOpening') {
        this.send('Page.handleJavaScriptDialog', { accept: true }).catch(() => {}); // Quit asks first
      }
    });
  }

  send(method, params = {}) {
    return this.cdp.send(method, params, this.sessionId);
  }

  async eval(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) {
      const details = result.exceptionDetails;
      throw new Error(`${this.name}: ${(details.exception && details.exception.description) || details.text}`);
    }
    return result.result.value;
  }

  async waitFor(expression, what, timeoutMs = 20000) {
    const deadline = Date.now() + timeoutMs;
    let last = null;
    while (Date.now() < deadline) {
      try {
        const value = await this.eval(expression);
        if (value) return value;
      } catch (err) {
        last = err;
      }
      await sleep(200);
    }
    const errors = this.errors.length ? ` Page errors: ${this.errors.join(' | ')}` : '';
    throw new Error(`${this.name}: timed out waiting for ${what}.${last ? ` Last error: ${last.message}` : ''}${errors}`);
  }

  // Elements are always found by id.
  click(id) {
    return this.eval(`(() => {
      const element = document.getElementById(${JSON.stringify(id)});
      if (!element) throw new Error('no element #' + ${JSON.stringify(id)});
      if (element.disabled) throw new Error('#' + ${JSON.stringify(id)} + ' is disabled');
      element.click();
      return true;
    })()`);
  }

  text(id) {
    return this.eval(`(document.getElementById(${JSON.stringify(id)}) || {}).textContent || ''`);
  }

  visible(id) {
    return `(() => { const e = document.getElementById(${JSON.stringify(id)}); return Boolean(e) && !e.hidden; })()`;
  }
}

// Each viewer gets its own browser context (like a separate device): its own device token.
// `beforeLoad(page)` runs before the page is opened, e.g. to set the screen size.
export async function openPage(cdp, name, url, { ownContext = false, beforeLoad = null } = {}) {
  const params = { url: 'about:blank', newWindow: true };
  if (ownContext) params.browserContextId = (await cdp.send('Target.createBrowserContext')).browserContextId;
  const { targetId } = await cdp.send('Target.createTarget', params);
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  const page = new Page(cdp, name, sessionId);
  await page.send('Page.enable');
  await page.send('Runtime.enable');
  if (beforeLoad) await beforeLoad(page);
  await page.send('Page.navigate', { url });
  await page.waitFor(`document.readyState === 'complete'`, 'the page to load');
  return page;
}
