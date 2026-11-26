// Takes the README screenshots with headless Chrome (Node 22 or newer, no packages):
//
//   node tools/screenshots.mjs
//
// Needs Chrome or Chromium and Python 3 (found as in tests/browser/run.mjs: CHROME_PATH or the usual
// places; PYTHON overrides the Python command). Run it again after changing the pages.
//
// Starts Porthole on a free port with the example address 192.168.1.20 in the viewer link (so the
// pictures never show this PC's real address), shares the built-in moving test picture (test mode),
// lets one phone-sized viewer in, and saves in docs/screenshots/:
//   sharer-light.png, sharer-dark.png   the sharer page at a desktop size, sharing, one viewer watching
//   viewer-light.png, viewer-dark.png   the viewer page on a phone, showing the test picture
// Then it quits Porthole, stops Chrome and removes its temporary folders.

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
} from '../tests/browser/support.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'docs', 'screenshots');
const EXAMPLE_HOST = '192.168.1.20';
const DESKTOP = { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false };
const PHONE = { width: 390, height: 844, deviceScaleFactor: 2, mobile: true };
// An Android phone's browser, so the sharer page lists the viewer as "Android phone".
const EASY_OFF = `localStorage.setItem('porthole-settings', ${JSON.stringify(JSON.stringify({ ask: true, smooth: false, easy: false }))});`;
const PHONE_AGENT =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36';
const DEADLINE_MS = 120_000;

const log = (text) => console.log(text);
cleanUpOnExit();

async function expectFrames(page, id) {
  await page.waitFor(`document.getElementById('${id}').videoWidth > 0`, `video in #${id}`);
  const first = await page.eval(`document.getElementById('${id}').currentTime`);
  await page.waitFor(`document.getElementById('${id}').currentTime > ${first} + 0.5`, `#${id} to keep playing`, 10000);
}

async function save(page, name, clip) {
  const { data } = await page.send('Page.captureScreenshot', { format: 'png', clip: { ...clip, scale: 1 }, captureBeyondViewport: true });
  const file = path.join(OUT, name);
  fs.writeFileSync(file, Buffer.from(data, 'base64'));
  log(`  ${path.relative(ROOT, file)} (${Math.round(fs.statSync(file).size / 1024)} KB)`);
}

// Waits until the test picture's moving blue box is near the middle of the video in #id (it crosses
// the picture in about 5 s), so it isn't caught half off the edge.
function boxInMiddle(page, id) {
  return page.waitFor(`(() => {
    const video = document.getElementById('${id}');
    if (!video.videoWidth) return false;
    const canvas = document.createElement('canvas');
    canvas.width = 160;
    canvas.height = 90;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(video, 0, 0, 160, 90);
    const row = ctx.getImageData(0, 45, 160, 1).data; // across the box (y 300 to 420 of 720)
    const blue = [];
    for (let x = 0; x < 160; x += 1) if (row[x * 4 + 2] > 200 && row[x * 4] < 150) blue.push(x);
    if (!blue.length) return false;
    const middle = (blue[0] + blue[blue.length - 1]) / 2 / 160;
    return middle > 0.35 && middle < 0.55;
  })()`, `the test picture's box in the middle of #${id}`, 10000);
}

// A finger tap in the middle of the picture brings back the viewer's button bar, which hides after 3 s.
async function showBar(page) {
  const point = { x: PHONE.width / 2, y: PHONE.height / 2 };
  await page.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
  await sleep(50);
  await page.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  // No fade-in: the pages turn it off for reduced motion, which the screenshots ask for.
  await page.waitFor(`getComputedStyle(document.getElementById('bar')).visibility === 'visible'`, 'the button bar', 3000);
}

// The sharer page's column with a margin around it, as tall as the page.
function sharerClip(page) {
  return page.eval(`(() => {
    const box = document.querySelector('main').getBoundingClientRect();
    const margin = 32;
    const x = Math.max(0, Math.floor(box.left - margin));
    const right = Math.min(document.documentElement.clientWidth, Math.ceil(box.right + margin));
    return { x, y: 0, width: right - x, height: Math.ceil(document.documentElement.scrollHeight) };
  })()`);
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
  fs.mkdirSync(OUT, { recursive: true });

  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  log(`Porthole on port ${port}`);
  const server = await startPorthole({
    python: defaultPython(),
    root: ROOT,
    args: ['--no-browser', '--port', String(port), '--host', EXAMPLE_HOST, '--record-dir', makeTempDir('recordings')],
    base,
  });
  const { browser, cdp } = await startChrome(chrome, makeTempDir('chrome'));

  try {
    const sharer = await openPage(cdp, 'sharer', `${base}/share?test=1`, {
      beforeLoad: async (page) => {
        await page.send('Emulation.setDeviceMetricsOverride', DESKTOP);
        // The easy address saved as off, so the page never asks Porthole to announce porthole.local on the real
        // network (Porthole never does it by itself). Ask stays on, as by default.
        await page.send('Page.addScriptToEvaluateOnNewDocument', { source: EASY_OFF });
      },
    });
    await sharer.waitFor(`document.getElementById('link').textContent.startsWith('http')`, 'the viewer link');
    const link = await sharer.text('link');
    if (new URL(link).hostname !== EXAMPLE_HOST) throw new Error(`the viewer link doesn't use ${EXAMPLE_HOST}: ${link}`);
    await sharer.waitFor(`!document.getElementById('start').disabled && document.getElementById('busy').hidden`, 'the page to connect');
    if (await sharer.eval(`document.getElementById('easy').checked`)) throw new Error('the easy address is on');
    await sharer.click('start');
    await sharer.waitFor(`document.getElementById('state').textContent.startsWith('Sharing')`, 'sharing to start');
    log('sharing the test picture');

    // The example address doesn't exist here: open the same viewer link on this PC.
    const { pathname, search, hash } = new URL(link);
    const viewer = await openPage(cdp, 'viewer', `${base}${pathname}${search}${hash}`, {
      ownContext: true,
      beforeLoad: async (page) => {
        await page.send('Emulation.setDeviceMetricsOverride', PHONE);
        await page.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
        await page.send('Emulation.setUserAgentOverride', { userAgent: PHONE_AGENT, platform: 'Android' });
      },
    });
    // With "Ask before someone watches" on (the default), the phone waits until it is allowed.
    const waiting = await viewer.waitFor(`(() => {
      if (document.getElementById('video').videoWidth > 0) return 'in';
      const code = /(\\d{3})/.exec(document.getElementById('code').hidden ? '' : document.getElementById('code').textContent);
      return code ? code[1] : '';
    })()`, 'the viewer to get in or wait with a code');
    if (waiting !== 'in') {
      await sharer.waitFor(`document.getElementById('allow-${waiting}') !== null`, `the waiting row with code ${waiting}`);
      await sharer.click(`allow-${waiting}`);
    }
    await expectFrames(viewer, 'video');
    await sharer.waitFor(`document.getElementById('viewers').textContent.includes('Watching')`, 'the viewer to show as watching');
    await expectFrames(sharer, 'preview');
    log('one viewer watching');

    for (const theme of ['light', 'dark']) {
      log(theme);
      for (const page of [sharer, viewer]) {
        await page.send('Emulation.setEmulatedMedia', {
          features: [
            { name: 'prefers-color-scheme', value: theme },
            { name: 'prefers-reduced-motion', value: 'reduce' },
          ],
        });
      }
      await sleep(800); // a frame or two in the new colours
      await boxInMiddle(sharer, 'preview');
      await save(sharer, `sharer-${theme}.png`, await sharerClip(sharer));
      await boxInMiddle(viewer, 'video');
      await showBar(viewer);
      await save(viewer, `viewer-${theme}.png`, { x: 0, y: 0, width: PHONE.width, height: PHONE.height });
    }
    for (const page of [sharer, viewer]) {
      if (page.errors.length) throw new Error(`${page.name} had script errors: ${page.errors.join(' | ')}`);
    }

    // Quit as the sharer page would: a same-origin request from this PC.
    const res = await fetch(`${base}/api/quit`, { method: 'POST', headers: { Origin: base } });
    if (!res.ok) throw new Error(`Quit was refused: ${res.status}`);
    const ended = await Promise.race([server.exited, sleep(10000).then(() => null)]);
    if (!ended) throw new Error('Porthole is still running 10 s after Quit');
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
    if (code === 0) log(`Screenshots saved in ${path.relative(ROOT, OUT)}.`);
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
