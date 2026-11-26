// Browser tests: drive headless Chrome over the DevTools protocol (Node only, no packages).
//
//   node tests/browser/run.mjs
//
// Starts Porthole on a free port with a temporary recordings folder, opens the sharer page in test
// mode (a moving test picture instead of the screen) and viewer pages, and checks:
//   (a) the theme switch sets data-theme, and a viewer gets moving video (a pause from outside offers Tap to play),
//   (b) with Ask on, a new viewer waits with a code until Allow; Deny all sends the others away,
//   (c) a 3 s recording ends up as a file with something in it,
//   (d) the viewer's idle button bar is hidden (not faded) and a finger tap brings it back without
//       pressing the button under the finger, while the next tap does press it; Pause shows
//       viewers (and the sharer's preview) the Paused card while their video keeps playing, with a small "Paused"
//       note over the picture (also zoomed in), said once, and a device that joins while paused gets both; a
//       recording started while paused and stopped
//       before Resume says "Nothing was recorded." and leaves no file; Resume brings the picture back; Record, Pause,
//       Change and Stop recording before Resume shows the part before Change as saved and leaves only that file;
//       a Change whose pick can't be captured says why and leaves the share running,
//   (e) too many new connections from one device get a clear refusal,
//   (e2) a second page, in another browser (its own saved settings), waits, says whether the other page shares (in
//       its status line too, with no Watching list), asks before its Quit cuts the share, and takes over with "Use
//       this one" from the approvals and switches Porthole keeps for this run; a switch changed there is saved in its
//       browser on its own,
//   (f) Quit stops the server, and a tab still waiting notices,
//   (g) with Porthole gone (killed), the page says so and ends, also when Quit gets no answer; killed and started
//       again on the same port, the page and a viewer of the run before say it was restarted (also a page that had
//       found Porthole gone, once the new run answers),
//   (h) a device that reconnects while its old connection is still open (a locked phone, a Wi-Fi drop) keeps its one
//       row, with its code, and isn't announced again; a reload names the connection before and takes its row back,
//       which Porthole keeps a moment after its stream closes; a denied row stays denied.
// Start sharing with Ask on asks for notifications once, and never again after a refusal. The link comes back by
// itself after "no network". Along the way it checks accessibility: names in Chrome's accessibility tree (also of a focused
// viewer row, which must not read its live numbers), where keyboard focus lands after Start sharing,
// Allow and Deny all, that each page's announcements are made once (not on every redraw), that lines
// appearing later ("no network", an Easy address error) are said, and said again when they come back,
// that Tab brings the viewer's hidden bar back and keys never press a hidden control, that keys zoom
// and move the viewer's picture, that a pressed button keeps its colours under the pointer, targets
// of at least 24 px, and the sharer page at 320 px and a viewer screen at 320 × 256 with wider text
// spacing (applied through DevTools: the page's security rules refuse a <style> a script adds).
// Exit code 0 means everything passed. Chrome is found from CHROME_PATH, or google-chrome, chromium or
// chromium-browser on the PATH, or in its install folder on Windows and macOS (support.mjs). PYTHON
// overrides the Python command (default python3, or python on Windows).

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  cleanUpOnExit,
  defaultPython,
  findChrome,
  freePort,
  killAll,
  makeTempDir,
  openPage,
  removeTempDirs,
  sleep,
  startChrome,
  started,
  startPorthole,
  stopChild,
  stopChrome,
  stopTreeCommand,
} from './support.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const PYTHON = defaultPython();
const DEADLINE_MS = 160_000; // the Python wrapper gives up at 180 s

const log = (text) => console.log(text);
cleanUpOnExit();

// ---------- the checks ----------

async function expectFrames(page) {
  await page.waitFor(`document.getElementById('video').videoWidth > 0`, 'video frames');
  const first = await page.eval(`document.getElementById('video').currentTime`);
  await page.waitFor(`document.getElementById('video').currentTime > ${first} + 0.5`, 'the video to keep playing', 10000);
  const size = await page.eval(`(() => { const v = document.getElementById('video'); return v.videoWidth + 'x' + v.videoHeight; })()`);
  log(`  ${page.name}: moving picture, ${size}`);
}

// The average colour of the picture's top-left corner, where the test picture is plain background
// (#1d2433) and the Paused card is #131416: blue tells them apart.
function cornerBlue(page, id = 'video') {
  return page.eval(`(() => {
    const video = document.getElementById(${JSON.stringify(id)});
    if (!video.videoWidth) return -1;
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 36;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, 64, 36);
    const data = ctx.getImageData(0, 0, 3, 3).data;
    let blue = 0;
    for (let i = 2; i < data.length; i += 4) blue += data[i];
    return Math.round(blue / (data.length / 4));
  })()`);
}

// A real finger tap at the middle of an element (touch emulation must be on). Unlike a made-up
// event, the browser itself works out which element gets the click.
async function tap(page, id) {
  const point = await page.eval(`(() => {
    const box = document.getElementById(${JSON.stringify(id)}).getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  })()`);
  await page.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
  await sleep(50);
  await page.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}

async function waitForCorner(page, test, what, id = 'video') {
  const deadline = Date.now() + 15000;
  let blue = -1;
  while (Date.now() < deadline) {
    blue = await cornerBlue(page, id);
    if (blue >= 0 && test(blue)) return blue;
    await sleep(200);
  }
  throw new Error(`${page.name}: timed out waiting for ${what} (corner blue ${blue})`);
}

async function waitingCode(page) {
  await page.waitFor(page.visible('code'), 'the waiting screen with a code');
  const text = await page.text('code');
  const match = /^Your code: (\d{3})$/.exec(text);
  if (!match) throw new Error(`${page.name}: unexpected code line "${text}"`);
  return match[1];
}

function check(condition, message) {
  if (!condition) throw new Error(message);
}

// ---------- accessibility helpers ----------

// Records every text the page's live region (#say) is given, from the first one on: set before the
// page's own scripts run (readystatechange to "interactive" comes before module scripts).
const RECORD_SAID = `document.addEventListener('readystatechange', () => {
  const say = document.getElementById('say');
  if (document.readyState !== 'interactive' || !say) return;
  window.saidLog = [];
  new MutationObserver(() => window.saidLog.push(say.textContent)).observe(say, { childList: true, characterData: true, subtree: true });
});`;
const recordSaid = (page) => page.send('Page.addScriptToEvaluateOnNewDocument', { source: RECORD_SAID });
const saidLog = (page) => page.eval(`window.saidLog || []`);

// Counts the page's requests for leave to show notifications, before its own scripts run. The first one is refused,
// as a person saying no would, so the page must never ask again.
const RECORD_NOTIFY_ASKS = `(() => {
  window.notifyAsks = 0;
  let permission = 'default';
  Object.defineProperty(Notification, 'permission', { get: () => permission, configurable: true });
  Notification.requestPermission = () => {
    window.notifyAsks += 1;
    permission = 'denied';
    return Promise.resolve(permission);
  };
})();`;
const recordNotifyAsks = (page) => page.send('Page.addScriptToEvaluateOnNewDocument', { source: RECORD_NOTIFY_ASKS });

// Ends Porthole at once, as a crash would: its pages hear no "ended". Windows stops the process tree, which also
// reaches the real Python behind a launcher.
function crash(child) {
  const command = stopTreeCommand(child.pid);
  if (command) spawnSync(command[0], command[1], { stdio: 'ignore', windowsHide: true, timeout: 20000 });
  else child.kill('SIGKILL');
  return child.exited;
}

// Every request of the page that matches one of `patterns` fails as if nothing answered (the browser's "Failed to
// fetch"), until the returned function is called.
async function failRequests(page, patterns) {
  const listener = (message) => {
    if (message.sessionId !== page.sessionId || message.method !== 'Fetch.requestPaused') return;
    page.send('Fetch.failRequest', { requestId: message.params.requestId, errorReason: 'ConnectionRefused' }).catch(() => {});
  };
  page.cdp.listeners.add(listener);
  await page.send('Fetch.enable', { patterns: patterns.map((urlPattern) => ({ urlPattern, requestStage: 'Request' })) });
  return async () => {
    page.cdp.listeners.delete(listener);
    await page.send('Fetch.disable');
  };
}

// Every request of the page that matches one of `patterns` waits, unanswered, until the returned function is called;
// then each goes on to whatever answers by then (a slow PC, as the page sees it).
async function holdRequests(page, patterns) {
  const held = [];
  let holding = true;
  const listener = (message) => {
    if (message.sessionId !== page.sessionId || message.method !== 'Fetch.requestPaused') return;
    if (holding) held.push(message.params.requestId);
    else page.send('Fetch.continueRequest', { requestId: message.params.requestId }).catch(() => {});
  };
  page.cdp.listeners.add(listener);
  await page.send('Fetch.enable', { patterns: patterns.map((urlPattern) => ({ urlPattern, requestStage: 'Request' })) });
  return async () => {
    holding = false;
    for (const requestId of held.splice(0)) await page.send('Fetch.continueRequest', { requestId }).catch(() => {});
    await page.send('Fetch.disable');
    page.cdp.listeners.delete(listener);
  };
}

const KEYS = {
  Tab: { key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 },
  Enter: { key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r' },
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', windowsVirtualKeyCode: 38 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39 },
  ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', windowsVirtualKeyCode: 40 },
  Plus: { key: '+', code: 'Equal', windowsVirtualKeyCode: 187, text: '+' }, // with Shift
  Minus: { key: '-', code: 'Minus', windowsVirtualKeyCode: 189, text: '-' },
};
const SHIFT = 8;

// A real key press, as a keyboard sends it (the browser moves focus or presses the focused button).
async function press(page, name, modifiers = 0) {
  const key = KEYS[name];
  await page.send('Input.dispatchKeyEvent', { type: 'keyDown', modifiers, ...key });
  await page.send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers, key: key.key, code: key.code, windowsVirtualKeyCode: key.windowsVirtualKeyCode });
  await sleep(100);
}

const activeId = (page) => page.eval(`(document.activeElement && document.activeElement !== document.body && document.activeElement.id) || ''`);

// Focuses a button as Tab would (so the browser counts it as keyboard focus), then presses Enter.
async function pressButton(page, id) {
  await page.eval(`document.getElementById(${JSON.stringify(id)}).focus(), true`);
  await press(page, 'Enter');
}

// What Chrome tells screen readers: the nodes that aren't ignored, with role, name and description.
async function axNodes(page) {
  await page.send('Accessibility.enable');
  const { nodes } = await page.send('Accessibility.getFullAXTree');
  return nodes
    .filter((node) => !node.ignored)
    .map((node) => ({ role: node.role && node.role.value, name: (node.name && node.name.value) || '', description: (node.description && node.description.value) || '' }));
}

// What Chrome tells screen readers about one element (found with a CSS selector).
async function axNodeOf(page, selector) {
  await page.send('DOM.enable');
  await page.send('Accessibility.enable');
  const { root } = await page.send('DOM.getDocument', { depth: 0 });
  const { nodeId } = await page.send('DOM.querySelector', { nodeId: root.nodeId, selector });
  check(nodeId, `${page.name}: nothing matches ${selector}`);
  const { nodes } = await page.send('Accessibility.getPartialAXTree', { nodeId, fetchRelatives: false });
  const node = nodes[0];
  return { role: node.role && node.role.value, name: (node.name && node.name.value) || '', description: (node.description && node.description.value) || '' };
}

// The colours an element shows in a forced state (CSS.forcePseudoState, e.g. ['hover']), so the check
// doesn't depend on where the mouse is.
async function forcedColours(page, id, states) {
  await page.send('DOM.enable');
  await page.send('CSS.enable');
  const { root } = await page.send('DOM.getDocument', { depth: 0 });
  const { nodeId } = await page.send('DOM.querySelector', { nodeId: root.nodeId, selector: `#${id}` });
  await page.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: states });
  try {
    return await page.eval(`(() => {
      const style = getComputedStyle(document.getElementById(${JSON.stringify(id)}));
      return { background: style.backgroundColor, color: style.color, ring: style.outlineColor };
    })()`);
  } finally {
    await page.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: [] });
  }
}

// Answers the page's next request that matches `pattern` (a DevTools URL pattern) itself, with the JSON
// that `change` returns: at the 'Request' stage before it reaches Porthole (`change(null)`), at the
// 'Response' stage from Porthole's own answer (`change(answer)`). Every other request goes through as usual.
function answerOnce(page, pattern, requestStage, change) {
  return new Promise((resolve, reject) => {
    const listener = async (message) => {
      if (message.sessionId !== page.sessionId || message.method !== 'Fetch.requestPaused') return;
      page.cdp.listeners.delete(listener);
      try {
        const { requestId } = message.params;
        let answer = null;
        if (requestStage === 'Response') {
          const { body, base64Encoded } = await page.send('Fetch.getResponseBody', { requestId });
          answer = JSON.parse(base64Encoded ? Buffer.from(body, 'base64').toString('utf8') : body);
        }
        const body = Buffer.from(JSON.stringify(change(answer))).toString('base64');
        await page.send('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }], body });
        await page.send('Fetch.disable');
        resolve();
      } catch (err) {
        reject(err);
      }
    };
    page.cdp.listeners.add(listener);
    page.send('Fetch.enable', { patterns: [{ urlPattern: pattern, requestStage }] }).catch(reject);
  });
}

// The zoom of the viewer's picture, read from its transform.
const viewOf = (page) =>
  page.eval(`(() => {
    const m = /translate\\((-?[\\d.e-]+)px, (-?[\\d.e-]+)px\\) scale\\(([\\d.e-]+)\\)/.exec(document.getElementById('video').style.transform);
    return m ? { x: Number(m[1]), y: Number(m[2]), scale: Number(m[3]) } : { x: 0, y: 0, scale: 1 };
  })()`);

// Every visible control is at least 24 by 24 CSS pixels (WCAG 2.5.8).
async function checkTargets(page) {
  const small = await page.eval(`[...document.querySelectorAll('button, select, summary, input')]
    .filter((e) => e.checkVisibility({ visibilityProperty: true }))
    .map((e) => ({ id: e.id || e.textContent.trim() || e.className, box: e.getBoundingClientRect() }))
    .filter(({ box }) => box.width < 24 || box.height < 24)
    .map(({ id, box }) => id + ' ' + Math.round(box.width) + 'x' + Math.round(box.height))`);
  check(!small.length, `${page.name}: targets smaller than 24 px: ${small.join(', ')}`);
}

// A mouse click at the middle of an element (scrolled into view first).
async function mouseClick(page, id) {
  const point = await page.eval(`(() => {
    const element = document.getElementById(${JSON.stringify(id)});
    element.scrollIntoView({ block: 'center' });
    const box = element.getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  })()`);
  await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', ...point });
  await page.send('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', clickCount: 1, ...point });
  await page.send('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', clickCount: 1, ...point });
  await sleep(100);
}

const TEXT_SPACING = '* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }';

// Wider text spacing on or off, added the way a reader's own style sheet or extension would be:
// through the DevTools protocol, since the page's security rules refuse a <style> a script adds.
// Checks that it really took effect, so the measurements after it mean something.
async function setTextSpacing(page, on) {
  if (!page.spacingSheet) {
    await page.send('DOM.enable');
    await page.send('CSS.enable');
    const { frameTree } = await page.send('Page.getFrameTree');
    page.spacingSheet = (await page.send('CSS.createStyleSheet', { frameId: frameTree.frame.id })).styleSheetId;
  }
  await page.send('CSS.setStyleSheetText', { styleSheetId: page.spacingSheet, text: on ? TEXT_SPACING : '' });
  const spacing = await page.eval(`getComputedStyle(document.querySelector('h1')).letterSpacing`);
  check(on ? parseFloat(spacing) > 0 : spacing === 'normal', `${page.name}: wider text spacing is ${on ? 'not applied' : 'still on'} (letter spacing ${spacing})`);
}

async function reload(page) {
  await page.eval(`window.beforeReload = true`);
  await page.send('Page.reload');
  await page.waitFor(`!window.beforeReload && document.readyState === 'complete'`, 'the page to load again');
}

// Opens a bare viewer event stream and returns its first event: the hello, or the refusal.
async function firstEvent(url) {
  const stop = new AbortController();
  const res = await fetch(url, { signal: stop.signal });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let text = '';
  try {
    while (!text.includes('\n\n')) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
    }
  } finally {
    stop.abort();
  }
  const match = /^data: (.*)$/m.exec(text);
  return match ? JSON.parse(match[1]) : null;
}

// Opens a bare viewer event stream and keeps it open, as a phone's connection that died without Porthole noticing
// yet would stay: `events` collects what arrives, `ended` turns true once Porthole closes it.
function holdStream(url, headers = {}) {
  const stream = { events: [], ended: false, stop: new AbortController() };
  stream.done = (async () => {
    try {
      const res = await fetch(url, { headers, signal: stream.stop.signal });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let text = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        text += decoder.decode(value, { stream: true });
        for (let cut = text.indexOf('\n\n'); cut >= 0; cut = text.indexOf('\n\n')) {
          const data = /^data: (.*)$/m.exec(text.slice(0, cut));
          text = text.slice(cut + 2);
          if (data) stream.events.push(JSON.parse(data[1]));
        }
      }
    } catch {
      // stopped by the test
    }
    stream.ended = true;
  })();
  return stream;
}

async function streamEvent(stream, type, what, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const event = stream.events.find((e) => e.type === type);
    if (event) return event;
    await sleep(50);
  }
  throw new Error(`timed out waiting for ${what} (got ${JSON.stringify(stream.events)})`);
}

async function main() {
  const chrome = findChrome();
  if (!chrome) {
    log('Chrome or Chromium not found (set CHROME_PATH).');
    return 2;
  }
  if (typeof WebSocket !== 'function') {
    log('This Node has no built-in WebSocket (needs Node 22 or newer).');
    return 2;
  }

  const port = await freePort();
  const recordDir = makeTempDir('recordings');
  const profileDir = makeTempDir('chrome');
  const base = `http://127.0.0.1:${port}`;

  log(`Porthole on port ${port}`);
  const server = await startPorthole({
    python: PYTHON,
    root: ROOT,
    args: ['--no-browser', '--port', String(port), '--record-dir', recordDir, '--no-discovery'],
    base,
  });
  const { browser, cdp } = await startChrome(chrome, profileDir);

  try {
    // (a) Sharing in test mode reaches a viewer.
    log('(a) frames arrive');
    const sharer = await openPage(cdp, 'sharer', `${base}/share?test=1`, {
      beforeLoad: async (page) => {
        await recordSaid(page);
        await recordNotifyAsks(page);
      },
    });
    await sharer.waitFor(`document.getElementById('link').textContent.startsWith('http')`, 'the viewer link');
    const sharerTree = await axNodes(sharer);
    const askSwitch = sharerTree.find((node) => node.role === 'switch' && node.name.startsWith('Ask'));
    check(askSwitch && askSwitch.name === 'Ask before someone watches', `the Ask switch is named "${askSwitch && askSwitch.name}"`);
    check(askSwitch.description === 'New devices wait until you allow them.', `the Ask switch is described as "${askSwitch.description}"`);
    check(sharerTree.some((node) => node.role === 'image' && node.name === 'QR code to open the link on a phone'), 'the QR code has no name');
    check(!sharerTree.some((node) => node.role === 'image' && !node.name), 'an image without a name (the QR picture read twice?)');
    check(sharerTree.some((node) => node.role === 'main'), 'the sharer page has no main landmark');
    check((await sharer.eval(`document.title`)) === 'Porthole', `the sharer page title is "${await sharer.eval('document.title')}"`);
    log('  the switches, the QR code and the title read well');
    check((await sharer.eval(`document.documentElement.dataset.theme`)) === 'auto', 'the page does not start on Auto');
    await sharer.click('theme-dark');
    check((await sharer.eval(`document.documentElement.dataset.theme`)) === 'dark', 'Dark did not set data-theme');
    check((await sharer.eval(`localStorage.getItem('porthole-theme')`)) === 'dark', 'Dark was not remembered');
    await sharer.click('theme-light');
    check((await sharer.eval(`document.documentElement.dataset.theme`)) === 'light', 'Light did not set data-theme');
    await sharer.click('theme-auto');
    check((await sharer.eval(`document.documentElement.dataset.theme`)) === 'auto', 'Auto did not set data-theme');
    log('  the theme switch sets data-theme and is remembered');
    await sharer.waitFor(`!document.getElementById('start').disabled && document.getElementById('busy').hidden`, 'the page to connect');
    check(await sharer.eval(`document.getElementById('ask').checked`), 'Ask is not on from the start');
    check((await sharer.eval(`window.notifyAsks`)) === 0, 'the page asked for notifications before Start sharing');
    await pressButton(sharer, 'start'); // with the keyboard: Start sharing hides, so focus must go somewhere
    await sharer.waitFor(`document.getElementById('state').textContent.startsWith('Sharing')`, 'sharing to start');
    await sharer.waitFor(`document.activeElement === document.getElementById('stop')`, 'focus to move from Start sharing to Stop sharing', 3000);
    log('  Start sharing by keyboard: focus moves to Stop sharing');
    // Ask is on from the start: Start sharing asks for leave to show "2 waiting" while the page is in the background.
    check((await sharer.eval(`window.notifyAsks`)) === 1, `Start sharing with Ask on asked for notifications ${await sharer.eval('window.notifyAsks')} times`);
    log('  Start sharing with Ask on asks for notifications');
    await sharer.click('ask'); // let the first viewer straight in
    // The status line is read out by screen readers when it changes, so the page must not rewrite
    // the same text on every redraw (a viewer joining, the 2 s check).
    await sharer.eval(`(() => {
      window.stateWrites = 0;
      new MutationObserver((records) => { window.stateWrites += records.length; })
        .observe(document.getElementById('state'), { childList: true, characterData: true, subtree: true });
      return true;
    })()`);
    const watchedFrom = Date.now();
    const info = await (await fetch(`${base}/api/info`)).json();
    // macOS 15 lets only allowed apps reach the local network (Local Network privacy) and nobody can answer
    // that question on a CI Mac, so there the viewer uses the loopback address; elsewhere the real viewer link.
    const viewerUrl = (process.platform === 'darwin' ? null : info.viewer_urls[0]) || `${base}/v/${info.room}`;
    const viewer1 = await openPage(cdp, 'viewer 1', viewerUrl, { ownContext: true, beforeLoad: recordSaid });
    await expectFrames(viewer1);
    await viewer1.waitFor(viewer1.visible('fullscreen'), 'Full screen once video plays', 5000);
    await viewer1.waitFor(`document.getElementById('say').textContent === 'Watching'`, 'viewer 1 to say Watching', 5000);
    await sleep(Math.max(0, watchedFrom + 4500 - Date.now())); // at least two of the 2 s checks
    check((await sharer.text('state')) === 'Sharing', 'the status line changed while sharing');
    const writes = await sharer.eval(`window.stateWrites`);
    check(writes === 0, `the unchanged status line was rewritten ${writes} times`);
    const watching = (await saidLog(viewer1)).filter((text) => text === 'Watching').length;
    check(watching === 1, `viewer 1 said Watching ${watching} times`);
    const viewerTree = await axNodes(viewer1);
    check(viewerTree.some((node) => node.role === 'Video' && node.name === 'Shared screen'), 'the viewer\'s picture is not named "Shared screen"');
    check(viewerTree.some((node) => node.role === 'main'), 'the viewer page has no main landmark');
    log('  the status lines are only written when they change; the viewer says Watching once');
    // The browser can pause the picture by itself (picture-in-picture closed, headphones gone, a media key): it must
    // not sit frozen with nothing to tap.
    await viewer1.eval(`(document.getElementById('video').pause(), true)`);
    await viewer1.waitFor(`${viewer1.visible('play')} && document.getElementById('fullscreen').hidden`, 'Tap to play once the browser paused the picture', 5000);
    await viewer1.click('play');
    await viewer1.waitFor(`document.getElementById('overlay').hidden && !document.getElementById('video').paused`, 'the picture to play again', 5000);
    log('  a picture the browser paused offers Tap to play, which brings it back');

    // (b) Ask on: a new device waits, shows a code, and gets in after Allow.
    log('(b) ask, allow, deny all');
    await sharer.click('ask');
    check(await sharer.eval(`document.getElementById('ask').checked`), 'Ask did not turn on');
    check((await sharer.eval(`window.notifyAsks`)) === 1, 'Ask turned on asked for notifications again after a refusal');
    const viewer2 = await openPage(cdp, 'viewer 2', viewerUrl, { ownContext: true, beforeLoad: recordSaid });
    const code2 = await waitingCode(viewer2);
    const said2 = await viewer2.text('say');
    check(said2 === `Waiting. Ask the sharer to let you in. Your code: ${code2}`, `viewer 2 says "${said2}"`);
    await sharer.waitFor(`document.getElementById('allow-${code2}') !== null`, `a waiting row with code ${code2}`);
    await sleep(2500); // a 2 s redraw, which must not announce the device again
    const row = await sharer.text(`viewer-${code2}`);
    check(row.includes(`Code ${code2} · joined `) && /joined \d\d:\d\d/.test(row), `the row doesn't show the code and time: "${row}"`);
    const name = await sharer.eval(`document.getElementById('viewer-${code2}').querySelector('.viewer-name').textContent`);
    check(name.endsWith(' 2'), `the second device of the same kind isn't numbered: "${name}"`);
    check(await sharer.eval(`document.getElementById('deny-all').hidden`), 'Deny all shows for only one waiting device');
    check((await sharer.eval(`document.title`)) === '(1) Porthole', `with one device waiting the title is "${await sharer.eval('document.title')}"`);
    const announced = (await saidLog(sharer)).filter((text) => text.includes(`Code ${code2}.`));
    check(announced.length === 1 && announced[0] === `${name} wants to watch. Code ${code2}.`, `the waiting device was announced as ${JSON.stringify(announced)}`);
    log(`  viewer 2 waits with code ${code2}, said on both pages, once`);
    await pressButton(sharer, `allow-${code2}`); // the row moves to Watching: focus follows it
    await sharer.waitFor(`document.activeElement === document.getElementById('viewer-${code2}') && document.activeElement.parentElement.id === 'viewers'`, 'focus on the allowed row', 3000);
    // The focused row reads the device, its code and its state: not its buttons, and not the live
    // numbers of its tooltip, which change every 2 s and would be read out again and again.
    const quietRow = (node) => !/Remove|Allow|fps|kbps|Mbps/.test(`${node.name} ${node.description}`);
    const allowedRow = await axNodeOf(sharer, `#viewer-${code2}`);
    check(allowedRow.name.startsWith(`${name}, code ${code2}, `) && quietRow(allowedRow), `the focused row reads ${JSON.stringify(allowedRow)}`);
    await expectFrames(viewer2);
    await sharer.waitFor(`document.getElementById('viewer-${code2}').textContent.includes('Watching')`, 'viewer 2 to show as watching');
    await sharer.waitFor(
      `(() => { const row = document.getElementById('viewer-${code2}'); return [row, ...row.querySelectorAll('[title]')].some((e) => /fps/.test(e.title || '')); })()`,
      'the live numbers in the row\'s tooltip',
      10000,
    );
    check((await activeId(sharer)) === `viewer-${code2}`, `focus left the allowed row for "${await activeId(sharer)}"`);
    const watchingRow = await axNodeOf(sharer, `#viewer-${code2}`);
    check(new RegExp(`^${name}, code ${code2}, (Watching|Weak signal)$`).test(watchingRow.name) && quietRow(watchingRow), `the focused watching row reads ${JSON.stringify(watchingRow)}`);
    log(`  Allow by keyboard: focus moves to the allowed row, which reads "${watchingRow.name}"`);

    // The mini controls window (Document Picture-in-Picture), where this Chrome can open one: it needs a real click.
    const miniPart = (name) => `window.documentPictureInPicture.window.document.querySelector('[data-mini="${name}"]')`;
    let miniOpen = false;
    if (await sharer.eval(sharer.visible('mini'))) {
      await mouseClick(sharer, 'mini');
      miniOpen = await sharer.waitFor(`Boolean(window.documentPictureInPicture.window && ${miniPart('say')})`, 'the mini window', 5000).catch(() => false);
    }
    if (!miniOpen) log('  (the mini window could not be opened here: its checks are skipped)');

    const viewer3 = await openPage(cdp, 'viewer 3', viewerUrl, { ownContext: true });
    const viewer4 = await openPage(cdp, 'viewer 4', viewerUrl, { ownContext: true });
    const code3 = await waitingCode(viewer3);
    const code4 = await waitingCode(viewer4);
    check(new Set([code2, code3, code4]).size === 3, `codes repeat: ${code2} ${code3} ${code4}`);
    await sharer.waitFor(sharer.visible('deny-all'), 'Deny all with two waiting');
    check((await sharer.eval(`document.title`)) === '(2) Porthole', `with two devices waiting the title is "${await sharer.eval('document.title')}"`);
    if (miniOpen) {
      await sharer.waitFor(`!${miniPart('waiting')}.hidden`, 'the mini window to show a waiting device', 5000);
      const mini = await sharer.eval(`({
        first: document.querySelector('#waiting-list > li .viewer-name').textContent,
        allow: ${miniPart('allow')}.getAttribute('aria-label'),
        deny: ${miniPart('deny')}.getAttribute('aria-label'),
        said: ${miniPart('say')}.textContent,
        title: window.documentPictureInPicture.window.document.title,
      })`);
      check(mini.allow === `Allow ${mini.first}` && mini.deny === `Deny ${mini.first}`, `the mini window's buttons are named ${JSON.stringify(mini)}`);
      check(mini.said.includes(`Code ${code4}.`) || mini.said.includes(`Code ${code3}.`), `the mini window said "${mini.said}"`);
      check(mini.title === 'Mini controls · Porthole', `the mini window's title is "${mini.title}"`);
      await sharer.eval(`${miniPart('deny')}.focus(), true`); // its focus must not stay on the part that hides
    }
    await sharer.eval(`document.getElementById('other').hidden = false, true`); // folded, but there when there are more addresses
    await checkTargets(sharer);
    await sharer.eval(`document.getElementById('other').hidden = document.getElementById('other-links').children.length === 0, true`);
    await pressButton(sharer, 'deny-all');
    for (const page of [viewer3, viewer4]) {
      await page.waitFor(`document.getElementById('detail').textContent === "The sharer didn't let you in." && document.getElementById('status').textContent === 'Ended'`, 'the denied message');
    }
    await sharer.waitFor(`document.getElementById('deny-all').hidden && !document.getElementById('viewer-${code3}')`, 'the denied rows to go');
    check((await activeId(sharer)) === 'viewers-title', `after Deny all, focus is on "${await activeId(sharer)}"`);
    log('  viewers 3 and 4 were denied; focus moved to the Watching heading');
    if (miniOpen) {
      await sharer.waitFor(`${miniPart('waiting')}.hidden && window.documentPictureInPicture.window.document.activeElement === ${miniPart('pause')}`, 'the mini window\'s focus to move to Pause', 3000);
      log('  the mini window names the device, says it is waiting, and keeps its focus on a shown button');
    }

    // One column at 320 px (400% zoom) and at 600 px (200% zoom), also with wider text spacing.
    for (const width of [600, 320]) {
      await sharer.send('Emulation.setDeviceMetricsOverride', { width, height: 800, deviceScaleFactor: 1, mobile: false });
      for (const spacing of [false, true]) {
        await setTextSpacing(sharer, spacing);
        const fits = await sharer.eval(`document.documentElement.scrollWidth <= document.documentElement.clientWidth`);
        check(fits, `the sharer page scrolls sideways at ${width} px${spacing ? ' with wider text spacing' : ''}`);
      }
    }
    await setTextSpacing(sharer, false);
    await sharer.send('Emulation.clearDeviceMetricsOverride');
    log('  the page fits 320 and 600 px, also with wider text spacing');

    // (c) A short recording becomes a file.
    log('(c) recording');
    await pressButton(sharer, 'record'); // with the keyboard: Record is briefly disabled while it starts
    await sharer.waitFor(sharer.visible('record-status'), 'the recording to start');
    await sharer.waitFor(`document.activeElement === document.getElementById('record')`, 'focus to stay on Record', 3000);
    await sleep(3000);
    await pressButton(sharer, 'record');
    await sharer.waitFor(sharer.visible('saved'), 'the recording to be saved', 15000);
    // News is written a moment after the region is cleared (so the same news twice is read twice).
    await sharer.waitFor(`document.getElementById('say').textContent.startsWith('Saved "Screen share ')`, 'the saved file to be announced', 3000);
    const savedText = await sharer.text('saved-text');
    const files = fs.readdirSync(recordDir).filter((name) => name.startsWith('Screen share '));
    check(files.length === 1, `expected one recording, found: ${files.join(', ') || 'none'}`);
    const size = fs.statSync(path.join(recordDir, files[0])).size;
    check(size > 0, `${files[0]} is empty`);
    check(savedText.includes(files[0]), `the page names another file: ${savedText}`);
    log(`  saved ${files[0]} (${size} bytes)`);

    // (d) The idle bar is hidden, not faded, and the finger tap that brings it back doesn't press the
    // button that appears under the finger (on touch the browser picks the click target at lift-off).
    log('(d) idle bar, pause');
    const barShown = `getComputedStyle(document.getElementById('bar')).visibility === 'visible'`;
    const actualPressed = `document.getElementById('actual').getAttribute('aria-pressed') === 'true'`;
    // The keyboard: Tab brings the hidden bar back and lands on a control that can be seen; the bar
    // stays while keyboard focus is in it.
    await viewer1.waitFor(viewer1.visible('actual'), '1:1 once video plays', 5000);
    await viewer1.waitFor(`!(${barShown})`, 'the idle bar to be hidden', 8000);
    check(!(await axNodes(viewer1)).some((node) => node.name === 'Full screen'), 'the hidden bar is still offered to screen readers');
    await press(viewer1, 'Tab');
    await viewer1.waitFor(barShown, 'Tab to bring the bar back', 3000);
    const tabbed = await viewer1.eval(`(() => {
      const active = document.activeElement;
      return document.getElementById('bar').contains(active) && active.checkVisibility({ visibilityProperty: true }) && active.matches(':focus-visible') ? active.id : '';
    })()`);
    check(tabbed, 'Tab did not land on a control in the bar that can be seen');
    await checkTargets(viewer1);
    await sleep(3500);
    check(await viewer1.eval(barShown), 'the bar hid while keyboard focus was in it');
    log(`  Tab brings the bar back (focus on ${tabbed}) and it stays while focus is there`);
    // Keys zoom and move the picture: 1:1 by keyboard zooms around the middle, and the arrow keys
    // reach the rest; + and - zoom. The quality menu keeps its own arrow keys.
    await pressButton(viewer1, 'actual');
    await viewer1.waitFor(actualPressed, 'Enter to press 1:1', 3000);
    const atActual = await viewOf(viewer1);
    await press(viewer1, 'ArrowRight');
    await press(viewer1, 'ArrowDown');
    const panned = await viewOf(viewer1);
    check(panned.scale === atActual.scale && panned.x < atActual.x && panned.y < atActual.y, `the arrow keys didn't move the zoomed picture: ${JSON.stringify(atActual)} -> ${JSON.stringify(panned)}`);
    await press(viewer1, 'Plus', SHIFT);
    const zoomedIn = await viewOf(viewer1);
    check(Math.abs(zoomedIn.scale - panned.scale * 1.25) < 0.01, `+ didn't zoom in: ${panned.scale} -> ${zoomedIn.scale}`);
    await press(viewer1, 'Minus');
    const zoomedOut = await viewOf(viewer1);
    check(Math.abs(zoomedOut.scale - panned.scale) < 0.01, `- didn't zoom out: ${zoomedIn.scale} -> ${zoomedOut.scale}`);
    await viewer1.eval(`document.getElementById('quality').focus(), true`);
    await press(viewer1, 'ArrowUp'); // Auto is the first choice, so the menu itself stays as it is
    check(JSON.stringify(await viewOf(viewer1)) === JSON.stringify(zoomedOut), 'an arrow key on the quality menu moved the picture');
    check((await viewer1.eval(`document.getElementById('quality').value`)) === 'auto', 'the arrow key changed the quality');
    await pressButton(viewer1, 'actual');
    await viewer1.waitFor(`!(${actualPressed}) && document.getElementById('fit').hidden`, 'Enter on 1:1 to fit again', 3000);
    // Fit hides itself once the picture fits: the focus goes to a control that is still shown, not
    // back to the top of the page.
    await press(viewer1, 'Plus', SHIFT);
    await viewer1.waitFor(viewer1.visible('fit'), '+ to zoom in and show Fit', 3000);
    await pressButton(viewer1, 'fit');
    await viewer1.waitFor(`document.getElementById('fit').hidden`, 'Enter on Fit to fit the picture', 3000);
    const afterFit = await viewer1.eval(`(() => {
      const active = document.activeElement;
      return document.getElementById('bar').contains(active) && !active.hidden ? active.id : '';
    })()`);
    check(afterFit === 'actual', `after Fit hid itself, focus is on "${afterFit || (await activeId(viewer1))}"`);
    log(`  keys zoom and move the picture (1:1 at ${atActual.scale}x, the arrows moved it to ${panned.x}, ${panned.y}); Fit hands the focus to 1:1`);
    await viewer1.eval(`document.activeElement.blur(), true`);
    // A mouse click leaves focus on the button. Once the bar hides, keys must not press that hidden button.
    await mouseClick(viewer1, 'actual');
    await viewer1.waitFor(actualPressed, 'a click to press 1:1', 3000);
    // The pointer is still over the button it pressed (and phones keep hover on the button last
    // tapped): it must keep its pressed look, black on white, and so must its focus ring.
    const hovered = await forcedColours(viewer1, 'actual', ['hover', 'focus-visible']);
    check(
      hovered.background === 'rgb(255, 255, 255)' && hovered.color === 'rgb(0, 0, 0)' && hovered.ring === 'rgb(0, 0, 0)',
      `a pressed button with the pointer over it shows ${JSON.stringify(hovered)}`,
    );
    // Chrome leaves the focus on a clicked button; a browser that doesn't has nothing for a key to press here.
    const clickFocused = (await activeId(viewer1)) === 'actual';
    await viewer1.waitFor(`!(${barShown})`, 'the bar to hide after the click', 8000);
    check(!(await viewer1.eval(`document.getElementById('bar').contains(document.activeElement)`)), 'focus stayed on a hidden control');
    await press(viewer1, 'Enter');
    await viewer1.waitFor(barShown, 'a key to bring the bar back', 3000);
    check(await viewer1.eval(actualPressed), 'Enter pressed the hidden 1:1 button');
    await mouseClick(viewer1, 'actual');
    await viewer1.waitFor(`!(${actualPressed})`, 'a second click to fit again', 3000);
    log(`  keys never press a hidden control${clickFocused ? ' (the click had left focus on 1:1)' : ''}`);

    await viewer1.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
    await viewer1.eval(`window.barClicks = 0; document.getElementById('bar').addEventListener('click', () => { window.barClicks += 1; }, true), true`);
    await viewer1.waitFor(`!(${barShown})`, 'the idle bar to be hidden', 8000);
    await tap(viewer1, 'actual');
    await viewer1.waitFor(barShown, 'a tap to bring the bar back', 3000);
    await sleep(500); // time for a click to land
    check((await viewer1.eval(`window.barClicks`)) === 0, 'the tap that brought the bar back also pressed a button');
    check(!(await viewer1.eval(actualPressed)), 'the tap that brought the bar back pressed 1:1');
    await sleep(800);
    await tap(viewer1, 'actual');
    await viewer1.waitFor(actualPressed, 'a tap on the shown bar to press 1:1', 3000);
    await tap(viewer1, 'actual');
    await viewer1.waitFor(`!(${actualPressed})`, 'a second tap on 1:1 to fit again', 3000);
    await viewer1.send('Emulation.setTouchEmulationEnabled', { enabled: false });
    log('  the idle bar is hidden; the tap that brings it back presses nothing, the next one does');

    // Pause: viewers see the Paused card, their video keeps playing; Resume brings the picture back.
    const before = await waitForCorner(viewer1, (blue) => blue > 42, 'the test picture');
    await sharer.click('pause');
    await sharer.waitFor(`document.getElementById('state').textContent === 'Paused'`, 'the Paused status');
    const pausedBlue = await waitForCorner(viewer1, (blue) => blue < 37, 'the Paused card');
    await waitForCorner(sharer, (blue) => blue < 37, 'the Paused card in the preview', 'preview');
    const pausedAt = await viewer1.eval(`document.getElementById('video').currentTime`);
    await viewer1.waitFor(`document.getElementById('video').currentTime > ${pausedAt} + 0.5`, 'the video to keep playing while paused', 10000);
    check(await viewer1.eval(`document.getElementById('overlay').hidden`), 'the viewer shows a screen over the Paused card');
    check((await sharer.text('pause')) === 'Resume', 'the Pause button does not say Resume');
    log(`  viewer 1 sees the Paused card (corner blue ${before} -> ${pausedBlue}) and keeps playing`);
    // Viewers hear about the pause: a small "Paused" note over the picture, said once. Zoomed in on a corner, where the
    // card is plain dark, the note still shows, inside the window and on top.
    const pausedNote = (page) => `${page.visible('paused')} && document.getElementById('say').textContent === 'Paused'`;
    await viewer1.waitFor(pausedNote(viewer1), 'the Paused note over the picture, and "Paused" said', 5000);
    await press(viewer1, 'Plus', SHIFT);
    await press(viewer1, 'Plus', SHIFT);
    const zoomedNote = await viewer1.eval(`(() => {
      const note = document.getElementById('paused');
      const r = note.getBoundingClientRect();
      const inside = r.width > 0 && r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;
      return { inside, onTop: document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) === note, text: note.textContent };
    })()`);
    const noteScale = (await viewOf(viewer1)).scale;
    check(noteScale > 1.5 && zoomedNote.inside && zoomedNote.onTop && zoomedNote.text === 'Paused', `zoomed in ${noteScale}x, the Paused note is ${JSON.stringify(zoomedNote)}`);
    await press(viewer1, 'Minus');
    await press(viewer1, 'Minus');
    log(`  viewer 1 shows a "Paused" note, also zoomed in (${noteScale}x), and says it`);
    // A device that joins while paused gets the card on its new connection (the check above covers
    // connections that were open already). Viewer 2 was allowed in (b), so its reload doesn't wait.
    await reload(viewer2);
    const joinedBlue = await waitForCorner(viewer2, (blue) => blue < 37, 'the Paused card after joining while paused');
    const joinedAt = await viewer2.eval(`document.getElementById('video').currentTime`);
    await viewer2.waitFor(`document.getElementById('video').currentTime > ${joinedAt} + 0.5`, 'the video to keep playing after joining while paused', 10000);
    check(await viewer2.eval(`document.getElementById('overlay').hidden`), 'viewer 2 shows a screen over the Paused card');
    check(await viewer2.eval(`document.getElementById('code').hidden`), 'viewer 2 had to wait again after its reload');
    await viewer2.waitFor(pausedNote(viewer2), 'the Paused note after joining while paused', 5000);
    log(`  viewer 2 joined again while paused and sees the Paused card (corner blue ${joinedBlue}) and the note`);
    // A recording started while paused and stopped before Resume has no picture in it: Porthole deletes the file
    // and the page says so, with no Saved line and no Open folder.
    await sharer.click('record');
    await sharer.waitFor(`${sharer.visible('record-status')} && document.getElementById('record').textContent === 'Stop recording'`, 'a recording to start while paused');
    await sleep(2500); // longer than one piece (2 s): whatever the browser sends meanwhile reaches Porthole
    await sharer.click('record');
    await sharer.waitFor(`${sharer.visible('saved')} && document.getElementById('saved-text').textContent === 'Nothing was recorded.'`, '"Nothing was recorded."', 15000);
    check(!(await sharer.eval(sharer.visible('open-folder'))), 'Open folder shows for a recording with nothing in it');
    check(!(await sharer.eval(sharer.visible('error'))), `an error shows after a recording with nothing in it: ${await sharer.text('error')}`);
    await sharer.waitFor(`document.getElementById('say').textContent === 'Nothing was recorded.'`, '"Nothing was recorded." to be announced', 3000);
    const afterPaused = fs.readdirSync(recordDir).filter((name) => name.startsWith('Screen share '));
    check(afterPaused.length === 1, `a recording with nothing in it was kept: ${afterPaused.join(', ')}`);
    log('  a recording started while paused and stopped before Resume says "Nothing was recorded." and leaves no file');
    await sharer.click('pause');
    await sharer.waitFor(`document.getElementById('state').textContent === 'Sharing'`, 'the Sharing status after Resume');
    const resumedBlue = await waitForCorner(viewer1, (blue) => blue > 42, 'the test picture after Resume');
    await waitForCorner(viewer2, (blue) => blue > 42, 'the test picture after Resume on viewer 2');
    await waitForCorner(sharer, (blue) => blue > 42, 'the test picture in the preview after Resume', 'preview');
    for (const page of [viewer1, viewer2]) {
      await page.waitFor(`document.getElementById('paused').hidden && /^(Watching|Weak signal)$/.test(document.getElementById('say').textContent)`, 'the Paused note to go with Resume', 5000);
    }
    const pausedSaid = (await saidLog(viewer1)).filter((text) => text === 'Paused').length;
    check(pausedSaid === 1, `viewer 1 said "Paused" ${pausedSaid} times`);
    log(`  Resume brings the picture back (corner blue ${resumedBlue}), also for viewer 2, and takes the note away`);
    // Record, Pause, Change: the recording continues in a new file, which has nothing in it until Resume. Stopped
    // before Resume, that file goes, and the page shows the part saved before Change, not "Nothing was recorded."
    const filesBefore = fs.readdirSync(recordDir).filter((name) => name.startsWith('Screen share '));
    await sharer.click('record');
    await sharer.waitFor(`${sharer.visible('record-status')} && document.getElementById('record').textContent === 'Stop recording'`, 'a recording to start');
    await sleep(2500); // a piece reaches Porthole
    await sharer.click('pause');
    await sharer.waitFor(`document.getElementById('state').textContent === 'Paused'`, 'the Paused status');
    await sharer.click('change'); // test mode: a new test picture, no picker
    await sharer.waitFor(`${sharer.visible('record-status')} && document.getElementById('record-status').textContent.endsWith('Continues in a new file.')`, 'the recording to continue in a new file', 15000);
    const firstPart = fs.readdirSync(recordDir).filter((name) => name.startsWith('Screen share ') && !filesBefore.includes(name)).sort()[0];
    const firstSaved = `Saved "${firstPart}".`;
    await sharer.click('record');
    await sharer.waitFor(`${sharer.visible('saved')} && document.getElementById('record-status').hidden && document.getElementById('record').textContent === 'Record'`, 'the recording to end', 15000);
    await sleep(300); // the news is written a moment after
    check((await sharer.text('saved-text')) === firstSaved, `after Pause, Change and Stop recording the page says "${await sharer.text('saved-text')}", not "${firstSaved}"`);
    check(await sharer.eval(sharer.visible('open-folder')), 'Open folder is missing after Pause, Change and Stop recording');
    check(!(await sharer.eval(sharer.visible('error'))), `an error shows after Pause, Change and Stop recording: ${await sharer.text('error')}`);
    const firstSaid = (await saidLog(sharer)).filter((text) => text === firstSaved).length;
    check(firstSaid === 2, `the part saved before Change was said ${firstSaid} times (at Change, then at Stop recording)`);
    const afterChange = fs.readdirSync(recordDir).filter((name) => name.startsWith('Screen share '));
    check(afterChange.length === filesBefore.length + 1, `after Pause, Change and Stop recording the folder has ${afterChange.join(', ')}`);
    await sharer.click('pause');
    await sharer.waitFor(`document.getElementById('state').textContent === 'Sharing'`, 'the Sharing status after Resume');
    log('  Record, Pause, Change, Stop recording: the part before Change shows as saved, and the empty new file goes');
    // Change, when what was picked can't be captured: the page says why, as Start sharing does, and the share goes on.
    // In test mode Change makes a new test picture instead of opening the picker: here it can't, once.
    await sharer.eval(`(() => {
      const real = HTMLCanvasElement.prototype.captureStream;
      HTMLCanvasElement.prototype.captureStream = function () {
        HTMLCanvasElement.prototype.captureStream = real;
        throw new DOMException('Could not start video source', 'NotReadableError');
      };
      return true;
    })()`);
    await sharer.click('change');
    const changeFailed = "Couldn't change what's shared: Could not start video source";
    await sharer.waitFor(`${sharer.visible('error')} && document.getElementById('error').textContent === ${JSON.stringify(changeFailed)}`, 'Change to say why it failed', 5000);
    check((await sharer.text('state')) === 'Sharing', `after a Change that failed the page says "${await sharer.text('state')}"`);
    await expectFrames(viewer2);
    await waitForCorner(viewer2, (blue) => blue > 42, 'the test picture, still shared after a Change that failed');
    log(`  a Change that fails says "${changeFailed}", and the share goes on`);

    // (e) Joining again and again from one device is refused, honestly, after 6 new connections in a
    // minute. Viewers 1 to 4 and viewer 2's reload were five, so the second bare try should be
    // refused; on a slow machine the first joins may be a minute old already, so try until refused.
    log('(e) too many tries');
    const eventsUrl = `${new URL(viewerUrl).origin}/events?room=${encodeURIComponent(info.room)}&role=viewer`;
    let tries = 0;
    let event = null;
    while (tries < 7) {
      tries += 1;
      event = await firstEvent(eventsUrl);
      if (!event || event.type !== 'hello') break;
    }
    check(event && event.type === 'refused' && event.reason === 'too-many-tries', `no refusal after ${tries} tries: ${JSON.stringify(event)}`);
    check(tries >= 2, 'the 6th new connection within a minute was refused');
    const viewer5 = await openPage(cdp, 'viewer 5', viewerUrl, { ownContext: true });
    await viewer5.waitFor(`document.getElementById('detail').textContent === 'Too many tries. Wait a minute, then try again.'`, 'the refusal message');
    check(await viewer5.eval(viewer5.visible('retry')), 'the refused viewer has no Try again button');
    log(`  bare try ${tries} was refused, and a viewer page says so plainly`);
    // The viewer's screens fit a small window (320 × 256: 400% zoom of 1280 × 1024), also with wider
    // text spacing: nothing is cut off, since the screen can't scroll. This one, and then the same
    // screen with the longest words any viewer screen has (read from view.js).
    const details = [...fs.readFileSync(path.join(ROOT, 'web', 'view.js'), 'utf8').matchAll(/detail: (?:'([^']*)'|"([^"]*)")/g)].map((m) => m[1] ?? m[2]);
    const longest = details.reduce((a, b) => (b.length > a.length ? b : a), '');
    check(longest.length > 40, `the viewer's screen texts weren't found in view.js: ${JSON.stringify(details)}`);
    await viewer5.send('Emulation.setDeviceMetricsOverride', { width: 320, height: 256, deviceScaleFactor: 1, mobile: false });
    for (const words of [await viewer5.text('detail'), longest]) {
      await viewer5.eval(`document.getElementById('detail').textContent = ${JSON.stringify(words)}, true`);
      for (const spacing of [false, true]) {
        await setTextSpacing(viewer5, spacing);
        const cut = await viewer5.eval(`[...document.getElementById('overlay').children]
          .filter((e) => !e.hidden)
          .map((e) => [e.id || e.tagName, e.getBoundingClientRect()])
          .filter(([, r]) => r.left < 0 || r.top < 0 || r.right > innerWidth || r.bottom > innerHeight)
          .map(([id, r]) => id + ' ' + Math.round(r.left) + ',' + Math.round(r.top) + ' to ' + Math.round(r.right) + ',' + Math.round(r.bottom))`);
        check(!cut.length, `the viewer's screen ("${words}") is cut off at 320 x 256${spacing ? ' with wider text spacing' : ''}: ${cut.join('; ')}`);
      }
    }
    log('  its screen fits 320 x 256, also with the longest words and wider text spacing');

    // Lines that appear after the page was drawn are said, not only shown. The PC drops off the
    // network: the page's next address check (every 10 s) is answered with no addresses, and with the
    // easy address no longer turned off for this run (this run uses --no-discovery).
    const said = (text) => `document.getElementById('say').textContent === ${JSON.stringify(text)}`;
    await answerOnce(sharer, '*/api/info*', 'Response', (answer) => ({
      ...answer,
      viewer_urls: [],
      easy: { enabled: false, available: true, blocked: false, host: 'porthole.local', url: 'http://porthole.local:1', short_url: null, error: '' },
    }));
    const noNetwork = await sharer.text('no-network');
    check(noNetwork === "This PC has no network. Connect it to Wi-Fi or a network cable; the link shows here once it's connected.", `the no-network line says "${noNetwork}"`);
    // Said with whatever else that check brought (the refusal from (e) may come with it).
    await sharer.waitFor(
      `${sharer.visible('no-network')} && document.getElementById('say').textContent.includes(${JSON.stringify(noNetwork)})`,
      '"no network" to be shown and said',
      15000,
    );
    // Easy address turned on while the name can't be announced: the error comes a moment after the
    // switch was read as on. Porthole never sees this request, so nothing is announced on the network.
    const easyError = "Easy address couldn't start on this network.";
    const refused = answerOnce(sharer, '*/api/discovery*', 'Request', () => ({ enabled: true, available: false, blocked: false, host: null, url: null, short_url: null, error: easyError }));
    await sharer.click('easy');
    await refused;
    await sharer.waitFor(`${sharer.visible('easy-error')} && ${said(easyError)}`, 'the Easy address error to be shown and said', 5000);
    // Off and on again, with nothing said in between: the same error is news again, and must be
    // written again (a live region given the words it already holds says nothing).
    const turnedOff = answerOnce(sharer, '*/api/discovery*', 'Request', () => ({ enabled: false, available: true, blocked: false, host: null, url: null, short_url: null, error: '' }));
    await sharer.click('easy');
    await turnedOff;
    await sharer.waitFor(`document.getElementById('easy-error').hidden`, 'the error to go with the switch off', 3000);
    const refusedAgain = answerOnce(sharer, '*/api/discovery*', 'Request', () => ({ enabled: true, available: false, blocked: false, host: null, url: null, short_url: null, error: easyError }));
    await sharer.click('easy');
    await refusedAgain;
    await sharer.waitFor(sharer.visible('easy-error'), 'the Easy address error to show again', 3000);
    await sleep(500);
    const errorSaid = (await saidLog(sharer)).filter((text) => text === easyError).length;
    check(errorSaid === 2, `the same Easy address error, shown twice, was written to the live region ${errorSaid} times`);
    log('  "no network" and an Easy address error are said when they appear, and said again when they come back');
    // The next address check (every 10 s) finds the network again: the link and the QR code come back by themselves.
    await sharer.waitFor(
      `!${sharer.visible('no-network')} && ${sharer.visible('join')} && document.getElementById('link').textContent === ${JSON.stringify(info.viewer_urls[0] || '')} && document.querySelector('#qr svg') !== null`,
      'the link to come back after "no network"',
      15000,
    );
    log('  the link and the QR code come back by themselves');

    // A row's button keeps the focus when a device above it leaves: moving the focused row in the
    // list would drop the focus to the top of the page (or, at best, onto the row).
    const rowsBefore = await sharer.eval(`document.querySelectorAll('#viewers > li').length`);
    const removeId = await sharer.eval(`(() => {
      const button = document.querySelector('#viewers > li:last-child button[data-action="remove"]');
      button.focus();
      return document.activeElement === button ? button.id : '';
    })()`);
    check(rowsBefore === 2 && removeId, `expected viewers 1 and 2 under Watching with focus on a Remove button: ${rowsBefore} rows, focus "${removeId}"`);
    await viewer1.send('Page.navigate', { url: 'about:blank' }); // viewer 1, above it, leaves
    await sharer.waitFor(`document.querySelectorAll('#viewers > li').length === 1`, 'viewer 1 to leave the Watching list', 10000);
    await sleep(300);
    check((await activeId(sharer)) === removeId, `when viewer 1 left, focus moved from ${removeId} to "${await activeId(sharer)}"`);
    log('  a row keeps its focus when a device above it leaves');

    // (e2) A second page in another browser (its own saved settings, like the browser tab that Open in my browser
    // opens) starts from this run's approvals and switches as Porthole keeps them, not from its own browser's.
    log('(e2) use this one, in another browser');
    await sharer.click('smooth'); // a switch this run has that the other browser's defaults don't
    const sharer2 = await openPage(cdp, 'sharer 2', `${base}/share?test=1`, { ownContext: true, beforeLoad: recordSaid });
    // It knows at once that another page shares: while it gives that page a moment to let go (about 3 s), its
    // controls are off, and Quit, which has no viewers of its own to go by, asks before cutting that page's share.
    await sharer2.waitFor(`document.getElementById('start').disabled && document.getElementById('busy').hidden`, 'the second page to wait with its controls off', 2500);
    await sharer2.eval(`(window.realConfirm = window.confirm, window.confirm = (text) => { window.asked = text; return false; }, true)`);
    await sharer2.click('quit');
    await sharer2.waitFor(`window.asked === 'Stop sharing and quit Porthole?'`, 'Quit on the waiting page to ask first', 3000);
    check(!(await Promise.race([server.exited, sleep(700).then(() => null)])), 'Quit on the waiting page stopped Porthole when told not to');
    check((await sharer.text('state')) === 'Sharing', `after a cancelled Quit in the waiting page the first page says "${await sharer.text('state')}"`);
    await sharer2.eval(`(window.confirm = window.realConfirm, true)`);
    log('  the waiting page keeps its controls off, and its Quit asks first');
    await sharer2.waitFor(sharer2.visible('busy'), 'the second page to say the other one shares', 10000);
    const busyShared = await sharer2.text('busy-text');
    check(busyShared === 'Sharing is on in another window or tab.', `while the other page shares, the waiting page says "${busyShared}"`);
    // Its status line says where sharing is (not "Not sharing"), and there is no Watching list ("No one yet") to make
    // Use this one look harmless. The status line is read out by itself: the waiting line isn't said a second time.
    await sharer2.waitFor(`document.getElementById('state').textContent === 'Sharing in another window or tab'`, 'the status line to say the other page shares', 3000);
    check(!(await sharer2.eval(sharer2.visible('watching'))), 'the waiting page shows a Watching list');
    await sleep(300); // news is written a moment after the region is cleared
    check(!(await saidLog(sharer2)).includes(busyShared), 'the waiting line was said besides the status line');
    check(
      (await sharer2.eval(`document.getElementById('smooth').checked && document.getElementById('ask').checked`)) &&
        (await sharer2.eval(`localStorage.getItem('porthole-settings')`)) === null,
      'the page in the other browser does not show the switches Porthole keeps for this run (or saved them as its own)',
    );
    log(`  it says "${busyShared}", its status line "Sharing in another window or tab", and shows this run's switches from Porthole`);
    // This browser's own saved switches, unlike this run's: taking over uses the run's, and a switch changed there
    // saves only that switch in this browser (checked below).
    const runSwitches = (await (await fetch(`${base}/api/info`)).json()).choices.settings;
    const ownSwitches = { ask: true, easy: !runSwitches.easy, smooth: false };
    await sharer2.eval(`(localStorage.setItem('porthole-settings', ${JSON.stringify(JSON.stringify(ownSwitches))}), true)`);
    await sharer.click('smooth'); // changed while the second page waits: taking over starts from the newer choice
    await sharer2.click('takeover');
    await sharer.waitFor(`document.getElementById('state').textContent === 'Moved'`, 'the first page to let go', 5000);
    check(await sharer.eval(`document.querySelectorAll('#viewers > li, #waiting-list > li').length === 0`), 'the page that let go still lists devices');
    await sharer2.waitFor(`document.querySelectorAll('#viewers > li').length === 1`, 'viewer 2 under Watching in the second page', 5000);
    check(await sharer2.eval(`document.getElementById('waiting').hidden`), 'viewer 2 was asked about again after Use this one in another browser');
    check(await sharer2.eval(`${sharer2.visible('watching')} && document.getElementById('state').textContent === 'Not sharing'`), 'after Use this one the page has no Watching list, or a status line about another page');
    check(!(await sharer2.eval(`document.getElementById('smooth').checked`)), 'Use this one did not read the switches again');
    // Its own report to Porthole keeps the devices the first page denied: it took them over too.
    await sharer2.click('smooth');
    const kept = await sharer2.waitFor(
      `fetch('/api/info').then((r) => r.json()).then((info) => info.choices && info.choices.settings.smooth === true && info.choices)`,
      'the second page to report its switches',
      5000,
    );
    check(kept.denied.length === 2 && kept.approved.length >= 1 && kept.settings.ask, `after taking over, Porthole keeps ${JSON.stringify(kept)}`);
    check(kept.settings.easy === runSwitches.easy, `after taking over, Porthole keeps Easy address ${kept.settings.easy}, not this run's ${runSwitches.easy}`);
    // Smooth motion was changed there: only it is saved in that browser, not the run's Easy address it took over.
    const ownNow = JSON.parse(await sharer2.eval(`localStorage.getItem('porthole-settings')`));
    const ownWanted = { ...ownSwitches, smooth: true };
    check(
      ownNow && ['ask', 'easy', 'smooth'].every((name) => ownNow[name] === ownWanted[name]),
      `after Smooth motion changed in the page that took over, its browser saved ${JSON.stringify(ownNow)}, not ${JSON.stringify(ownWanted)}`,
    );
    log('  the page in the other browser took over with viewer 2 still allowed and viewers 3 and 4 still denied');

    // A third tab waits while the second one is the sharer.
    const sharer3 = await openPage(cdp, 'sharer 3', `${base}/share?test=1`);
    await sharer3.waitFor(sharer3.visible('busy'), 'a third tab to wait', 10000);
    // The second page took over but shares nothing yet: it is only open.
    const busyOpen = await sharer3.text('busy-text');
    check(busyOpen === 'Porthole is open in another window or tab.', `while the other page only is open, the waiting page says "${busyOpen}"`);
    await sharer3.waitFor(`document.getElementById('state').textContent === 'Open in another window or tab'`, 'the status line to say the other page is open', 3000);
    check(!(await sharer3.eval(sharer3.visible('watching'))), 'the third tab shows a Watching list');
    log(`  a third tab waits and says "${busyOpen}"`);
    // When the second page closes, the third takes the free slot, with this run's approvals: viewer 2 stays allowed.
    await sharer2.send('Page.navigate', { url: 'about:blank' });
    await sharer3.waitFor(`document.getElementById('busy').hidden && !document.getElementById('start').disabled`, 'the third tab to take the free slot', 25000);
    await sharer3.waitFor(`document.querySelectorAll('#viewers > li').length === 1`, 'viewer 2 under Watching in the third tab', 5000);
    check(await sharer3.eval(`document.getElementById('waiting').hidden`), 'viewer 2 was asked about again when the third tab took the free slot');
    log('  a waiting tab took the free slot with viewer 2 still allowed');
    // A fourth tab waits while the third one shares: it has no stream to hear Quit, so it notices by itself.
    const sharer4 = await openPage(cdp, 'sharer 4', `${base}/share?test=1`);
    await sharer4.waitFor(sharer4.visible('busy'), 'a fourth tab to wait', 10000);

    // (f) Quit stops the server and tells viewers.
    log('(f) quit');
    await sharer3.click('quit'); // the "are you sure" dialog is accepted by the page helper
    const ended = await Promise.race([server.exited, sleep(10000).then(() => null)]);
    check(ended, 'the server is still running 10 s after Quit');
    check(ended.code === 0, `the server ended with code ${ended.code} (signal ${ended.signal}):\n${server.output}`);
    await viewer2.waitFor(`document.getElementById('detail').textContent.startsWith('The sharer closed Porthole')`, 'the ended message', 5000);
    check(await viewer2.eval(viewer2.visible('retry')), 'the ended screen has no Try again button');
    check(await viewer2.eval(`document.getElementById('bar').hidden`), 'the button bar still shows after the end');
    check((await viewer2.text('say')) === 'Ended. The sharer closed Porthole.', `viewer 2 says "${await viewer2.text('say')}" at the end`);
    log('  the server stopped and viewers were told');
    await sharer4.waitFor(`document.getElementById('state').textContent === 'Stopped' && document.getElementById('busy').hidden`, 'the waiting tab to notice Porthole stopped', 15000);
    log('  the waiting tab noticed Porthole stopped');
    for (const page of [sharer, sharer2, sharer3, sharer4, viewer1, viewer2]) {
      check(!page.errors.length, `${page.name} had script errors: ${page.errors.join(' | ')}`);
    }

    // (g) Porthole gone without a word (crashed or killed). A second Porthole for this: first Quit gets no answer at
    // all (made so here, while Porthole runs), then that Porthole is really killed while its page shares.
    log('(g) porthole gone');
    const gonePort = await freePort();
    const goneBase = `http://127.0.0.1:${gonePort}`;
    const goneArgs = ['--no-browser', '--port', String(gonePort), '--record-dir', makeTempDir('recordings2'), '--no-discovery'];
    const server2 = await startPorthole({ python: PYTHON, root: ROOT, args: goneArgs, base: goneBase });
    const gone = "Porthole isn't running any more. You can close this page.";
    const goneSaid = `document.getElementById('notice').textContent === ${JSON.stringify(gone)} && document.getElementById('state').textContent === 'Stopped'`;
    const allOff = `[...document.querySelectorAll('button')].every((button) => button.disabled)`;
    const lone = await openPage(cdp, 'lone sharer', `${goneBase}/share?test=1`);
    await lone.waitFor(`!document.getElementById('start').disabled`, 'the page to connect');
    await lone.click('start');
    await lone.waitFor(`document.getElementById('state').textContent === 'Sharing'`, 'sharing to start');
    const unfail = await failRequests(lone, ['*/api/quit*', '*/api/info*']);
    await lone.click('quit');
    await lone.waitFor(goneSaid, `"${gone}" after a Quit with no answer`, 5000);
    check(await lone.eval(allOff), 'controls are still on after Porthole was found gone');
    check(!(await lone.eval(lone.visible('error'))), `an error shows after Quit found Porthole gone: ${await lone.text('error')}`);
    await unfail();
    check(!(await Promise.race([server2.exited, sleep(300).then(() => null)])), 'the Quit that got no answer still reached Porthole');
    log('  Quit with no answer ends the page and says Porthole isn\'t running, not "Failed to fetch"');
    const shareAgain = async () => {
      await reload(lone);
      await lone.waitFor(`!document.getElementById('start').disabled`, 'the page to connect again', 10000);
      await lone.click('start');
      await lone.waitFor(`document.getElementById('state').textContent === 'Sharing'`, 'sharing to start again');
    };
    await shareAgain();
    const unfailRecord = await failRequests(lone, ['*/api/recording/start*', '*/api/info*']);
    await lone.click('record');
    await lone.waitFor(goneSaid, `"${gone}" after Record got no answer`, 5000);
    await sleep(300); // Record is turned back on after it starts: it must stay off once the page has ended
    check(await lone.eval(allOff), 'controls are still on after Record found Porthole gone');
    check(!(await lone.eval(lone.visible('record-status'))), 'a recording shows as started after Record found Porthole gone');
    await unfailRecord();
    log('  Record with no answer does the same');
    // Killed and started again on the same port: the new run answers, so the page of the run before says Porthole was
    // restarted, and so does a viewer of that run (waiting to be let in, its stream open). Until the new run is up, the
    // page's checks get no answer yet (held, as on a slow PC), so it doesn't find Porthole gone first.
    await shareAgain();
    const oldRoom = (await (await fetch(`${goneBase}/api/info`)).json()).room;
    const oldViewer = await openPage(cdp, 'viewer of the run before', `${goneBase}/v/${encodeURIComponent(oldRoom)}`, { ownContext: true });
    await waitingCode(oldViewer);
    const release = await holdRequests(lone, ['*/api/info*']);
    await crash(server2);
    const server2b = await startPorthole({ python: PYTHON, root: ROOT, args: goneArgs, base: goneBase });
    await release();
    const restarted = 'Porthole was restarted. Use its new page, or reload this one.';
    await lone.waitFor(
      `document.getElementById('notice').textContent === ${JSON.stringify(restarted)} && document.getElementById('state').textContent === 'Stopped'`,
      `"${restarted}"`,
      15000,
    );
    check(await lone.eval(allOff), 'controls are still on after Porthole was restarted');
    check(!(await lone.eval(lone.visible('error'))), `an error shows after Porthole was restarted: ${await lone.text('error')}`);
    const viewerRestarted = 'Porthole was restarted. Use its new link, or try again.';
    await oldViewer.waitFor(`document.getElementById('detail').textContent === ${JSON.stringify(viewerRestarted)}`, 'the viewer to say Porthole was restarted', 15000);
    check(await oldViewer.eval(oldViewer.visible('retry')), 'the viewer that says Porthole was restarted has no Try again');
    check((await oldViewer.text('say')) === `Ended. ${viewerRestarted}`, `the viewer of the run before says "${await oldViewer.text('say')}"`);
    check(!lone.errors.length && !oldViewer.errors.length, `script errors: ${[...lone.errors, ...oldViewer.errors].join(' | ')}`);
    log('  killed and started again: the page and a viewer of the run before say Porthole was restarted');
    await shareAgain();
    await crash(server2b);
    await lone.waitFor(goneSaid, `"${gone}" once Porthole was killed`, 15000);
    check(await lone.eval(allOff), 'controls are still on after Porthole was killed');
    check(!(await lone.eval(lone.visible('error'))), `an error shows once Porthole was killed: ${await lone.text('error')}`);
    log('  with Porthole killed, the page notices, says so and ends');
    // Started again some seconds later: the page that found Porthole gone says it was restarted once the new run
    // answers (its next 10-second check).
    const server2c = await startPorthole({ python: PYTHON, root: ROOT, args: goneArgs, base: goneBase });
    await lone.waitFor(`document.getElementById('notice').textContent === ${JSON.stringify(restarted)}`, 'the page that found Porthole gone to say it was restarted', 15000);
    check(!(await lone.eval(lone.visible('error'))), `an error shows once Porthole was started again: ${await lone.text('error')}`);
    check(!lone.errors.length, `the page had script errors: ${lone.errors.join(' | ')}`);
    log('  started again, the page that found it gone says Porthole was restarted');
    await stopChild(server2c);

    // (h) A device that reconnects. A Porthole of its own, so its viewers' joins don't count against the others'
    // (6 a minute per address; on a Mac every viewer here comes from the loopback address).
    {
      log('(h) a device that reconnects');
      const backPort = await freePort();
      const backBase = `http://127.0.0.1:${backPort}`;
      const server3 = await startPorthole({
        python: PYTHON,
        root: ROOT,
        args: ['--no-browser', '--port', String(backPort), '--record-dir', makeTempDir('recordings3'), '--no-discovery'],
        base: backBase,
      });
      const host = await openPage(cdp, 'reconnect sharer', `${backBase}/share?test=1`, { beforeLoad: recordSaid });
      await host.waitFor(`!document.getElementById('start').disabled && document.getElementById('busy').hidden`, 'the page to connect');
      await host.click('start');
      await host.waitFor(`document.getElementById('state').textContent === 'Sharing'`, 'sharing to start');
      check(await host.eval(`document.getElementById('ask').checked`), 'Ask is off on a new page');
      const backRoom = (await (await fetch(`${backBase}/api/info`)).json()).room;
      const userAgent = await host.eval('navigator.userAgent'); // the phone's browser, so its row has the same name
      const eventsAt = `${backBase}/events?room=${encodeURIComponent(backRoom)}&role=viewer`;
      // The phone's connection before: open (Porthole hasn't noticed it died), waiting to be let in.
      const token = 'Rcnct_0123456789';
      const earlier = holdStream(`${eventsAt}&client=${token}`, { 'User-Agent': userAgent });
      const firstHello = await streamEvent(earlier, 'hello', 'the hello of the connection before');
      const waitingRows = `document.querySelectorAll('#waiting-list > li').length`;
      await host.waitFor(`${waitingRows} === 1 && document.getElementById('viewer-${firstHello.code}')`, 'the waiting row');
      await host.waitFor(`(window.saidLog || []).some((text) => text.includes('Code ${firstHello.code}.'))`, 'the waiting device to be announced', 3000);
      // The phone is back: its page (same browser token, same address) names that connection, as after a reload; the
      // browser's own reconnects send the same id as Last-Event-ID (tests/test_webserver.py).
      const seed = `if (location.origin === ${JSON.stringify(backBase)} && !sessionStorage.getItem('seeded')) {
        sessionStorage.setItem('seeded', '1');
        localStorage.setItem('porthole-client', ${JSON.stringify(token)});
        sessionStorage.setItem(${JSON.stringify(`porthole-viewer-${backRoom}`)}, ${JSON.stringify(firstHello.id)});
      }`;
      const phone = await openPage(cdp, 'reconnecting phone', `${backBase}/v/${encodeURIComponent(backRoom)}`, {
        ownContext: true,
        beforeLoad: (page) => page.send('Page.addScriptToEvaluateOnNewDocument', { source: seed }),
      });
      check((await waitingCode(phone)) === String(firstHello.code), 'the phone came back with a new code');
      await streamEvent(earlier, 'replaced', 'the connection before to be told it was replaced');
      const closeDeadline = Date.now() + 5000;
      while (!earlier.ended && Date.now() < closeDeadline) await sleep(50);
      check(earlier.ended, 'Porthole kept the connection before open');
      await sleep(500); // time for a second row or announcement to show up, if one were coming
      check((await host.eval(waitingRows)) === 1, `the phone is listed ${await host.eval(waitingRows)} times`);
      check((await host.eval('document.title')) === '(1) Porthole', `the sharer page's title is "${await host.eval('document.title')}"`);
      const announced = (await saidLog(host)).filter((text) => text.includes('wants to watch')).length;
      check(announced === 1, `the waiting phone was announced ${announced} times`);
      log(`  the phone came back to its own row (code ${firstHello.code}), announced once; the connection before was closed`);
      await host.click(`allow-${firstHello.code}`);
      await expectFrames(phone);
      // A reload names this tab's connection before (kept in sessionStorage). Its old stream closes cleanly, and
      // Porthole keeps the row of a closed connection a moment (2 s), so the reloaded page takes it back: the same
      // row and code, let in without waiting again, and not announced. The new stream is held 1.1 s, as on a phone
      // whose reload takes longer: past the second Porthole takes to notice the old stream closed, inside the 2 s.
      const keptId = await phone.eval(`sessionStorage.getItem(${JSON.stringify(`porthole-viewer-${backRoom}`)})`);
      check(/^v-[0-9a-f]{8}$/.test(keptId || '') && keptId !== firstHello.id, `the phone kept "${keptId}" as its id`);
      const announcedBefore = (await saidLog(host)).filter((text) => text.includes('wants to watch')).length;
      const asked = new Promise((resolve) => {
        const listener = (message) => {
          if (message.sessionId !== phone.sessionId || message.method !== 'Fetch.requestPaused') return;
          phone.cdp.listeners.delete(listener);
          resolve(message.params.request.url);
          setTimeout(() => {
            phone.send('Fetch.continueRequest', { requestId: message.params.requestId }).then(() => phone.send('Fetch.disable')).catch(() => {});
          }, 1100);
        };
        phone.cdp.listeners.add(listener);
      });
      await phone.send('Fetch.enable', { patterns: [{ urlPattern: '*/events?*', requestStage: 'Request' }] });
      await reload(phone);
      const reloadUrl = await Promise.race([asked, sleep(5000).then(() => '')]);
      check(new URL(reloadUrl || 'http://x/').searchParams.get('prev') === keptId, `the reloaded page asked for ${reloadUrl}`);
      await expectFrames(phone);
      check(await phone.eval(`document.getElementById('code').hidden`), 'the phone had to wait again after its reload');
      await host.waitFor(`document.querySelectorAll('#viewers > li').length === 1 && ${waitingRows} === 0`, 'one row for the phone after its reload', 5000);
      check(await host.eval(`Boolean(document.getElementById('viewer-${firstHello.code}'))`), `the reloaded phone lost its row with code ${firstHello.code}`);
      await sleep(2500); // past the wait for a reload: no second row, and the row stays
      check((await host.eval(`document.querySelectorAll('#viewers > li').length`)) === 1, 'the reloaded phone is listed twice, or not at all');
      const announcedAfter = (await saidLog(host)).filter((text) => text.includes('wants to watch')).length;
      check(announcedAfter === announcedBefore, `the phone was announced again after its reload (${announcedAfter - announcedBefore} times)`);
      log(`  a reload names the connection before and takes its row back (code ${firstHello.code}), let in, not announced`);
      // A denied row is never taken over: the same device back with its id joins afresh, and is turned away again.
      const deniedToken = 'Denied_0123456789';
      const turnedAway = holdStream(`${eventsAt}&client=${deniedToken}`, { 'User-Agent': userAgent });
      const deniedHello = await streamEvent(turnedAway, 'hello', 'the hello of a device to deny');
      await host.waitFor(`document.getElementById('deny-${deniedHello.code}')`, 'the row to deny');
      await host.click(`deny-${deniedHello.code}`);
      await streamEvent(turnedAway, 'denied', 'the denial');
      const backAgain = holdStream(`${eventsAt}&client=${deniedToken}&prev=${deniedHello.id}`, { 'User-Agent': userAgent });
      const againHello = await streamEvent(backAgain, 'hello', 'the hello of the denied device back');
      check(againHello.code !== deniedHello.code, 'a denied row was taken over');
      await streamEvent(backAgain, 'denied', 'the denied device to be turned away again');
      for (const stream of [turnedAway, backAgain]) stream.stop.abort();
      log('  a denied device back with its old id joins afresh and is turned away again');
      for (const page of [host, phone]) check(!page.errors.length, `${page.name} had script errors: ${page.errors.join(' | ')}`);
      await stopChild(server3);
    }
  } finally {
    await stopChrome(browser, cdp);
    await stopChild(server);
  }
  return 0;
}

const deadline = setTimeout(() => {
  log(`FAILED: timed out after ${DEADLINE_MS / 1000} s`);
  killAll();
  removeTempDirs();
  process.exit(1);
}, DEADLINE_MS);

main()
  .then((code) => {
    clearTimeout(deadline);
    removeTempDirs();
    if (code === 0) log('All browser checks passed.');
    process.exit(code);
  })
  .catch((err) => {
    clearTimeout(deadline);
    log(`FAILED: ${err.message}`);
    for (const child of started) if (child.output) log(`--- ${child.label} output ---\n${child.output.slice(-3000)}`);
    killAll();
    removeTempDirs();
    process.exit(1);
  });
