// Makes the README's short looping demo with headless Chrome and ffmpeg (Node 22 or newer, no packages):
//
//   node tools/demo.mjs
//
// Needs Chrome or Chromium and Python 3 (found as in tools/screenshots.mjs), and ffmpeg (FFMPEG overrides
// the command). Run it again after changing the sharer page.
//
// Like tools/screenshots.mjs, it starts Porthole on a free port above 8800 with the example address
// 192.168.1.20 in the viewer link and the easy address saved as off (nothing is announced on the real
// network), and shares the built-in moving test picture (test mode). It films the sharer page in four
// scenes: Not sharing; Sharing; a phone waiting with Allow and Deny; the phone watching. It does this
// once in light and once in dark, and ffmpeg makes two looping GIFs in docs/screenshots/ (GIF, because
// every browser and GitHub's apps show it; the page has few colours, so 128 of them and no dithering keep
// it sharp and small):
//   demo-light.gif, demo-dark.gif   the sharer page, about 8 seconds
// After each run it quits Porthole and stops Chrome; at the end it removes its temporary folders.

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
} from '../tests/browser/support.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'docs', 'screenshots');
const EXAMPLE_HOST = '192.168.1.20';
const FFMPEG = process.env.FFMPEG || 'ffmpeg';
const DESKTOP = { width: 1200, height: 1100, deviceScaleFactor: 1, mobile: false };
const PHONE = { width: 390, height: 844, deviceScaleFactor: 2, mobile: true };
const EASY_OFF = `localStorage.setItem('porthole-settings', ${JSON.stringify(JSON.stringify({ ask: true, smooth: false, easy: false }))});`;
// An Android phone's browser, so the sharer page lists the viewer as "Android phone".
const PHONE_AGENT =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36';
const THEMES = ['light', 'dark'];
const FRAME_MS = 125; // about 8 frames a second
const SCENES = { off: 1600, sharing: 1600, waiting: 2400, watching: 2800 }; // how long each scene shows, in ms
const DEADLINE_MS = 240_000;
const MAX_BYTES = 2_000_000; // per file; a reader's browser loads one of the two

const log = (text) => console.log(text);
cleanUpOnExit();

// freePort() gives a port the system picks; never one Porthole uses by itself (8765 to 8790).
async function portAbove8800() {
  for (let i = 0; i < 20; i += 1) {
    const port = await freePort();
    if (port > 8800) return port;
  }
  throw new Error('no free port above 8800');
}

async function setTheme(page, theme) {
  await page.send('Emulation.setEmulatedMedia', {
    features: [
      { name: 'prefers-color-scheme', value: theme },
      { name: 'prefers-reduced-motion', value: 'reduce' },
    ],
  });
}

// The sharer page's column with a margin around it, from the top of the window.
function columnClip(page) {
  return page.eval(`(() => {
    const box = document.querySelector('main').getBoundingClientRect();
    const margin = 32;
    const x = Math.max(0, Math.floor(box.left - margin));
    const right = Math.min(document.documentElement.clientWidth, Math.ceil(box.right + margin));
    return { x, y: 0, width: right - x, height: ${DESKTOP.height} };
  })()`);
}

// Where the Watching section ends, so the film can be cut just below it.
function watchingBottom(page) {
  return page.eval(`Math.ceil(document.getElementById('watching').getBoundingClientRect().bottom)`);
}

// Films one scene: a frame about every FRAME_MS until `ms` have passed.
async function film(page, frames, clip, dir, ms) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    const at = Date.now();
    const { data } = await page.send('Page.captureScreenshot', { format: 'png', clip: { ...clip, scale: 1 } });
    fs.writeFileSync(path.join(dir, frameName(frames.length)), Buffer.from(data, 'base64'));
    frames.push({ index: frames.length, at });
    const wait = FRAME_MS - (Date.now() - at);
    if (wait > 0) await sleep(wait);
  }
  // The scene's last frame stays until the scene's time is up.
  frames[frames.length - 1].end = start + ms;
}

function frameName(index) {
  return `frame-${String(index).padStart(4, '0')}.png`;
}

// Each frame shows until the next one was taken (the last one of a scene until the scene's end).
function durations(frames) {
  return frames.map((frame, i) => {
    const end = frame.end ?? frames[i + 1].at;
    return Math.max(40, end - frame.at) / 1000;
  });
}

function encode(dir, frames, theme, height) {
  const times = durations(frames);
  const list = path.join(dir, 'frames.txt');
  const lines = [];
  frames.forEach((frame, i) => lines.push(`file '${frameName(frame.index)}'`, `duration ${times[i].toFixed(3)}`));
  // The concat reader ignores the last duration unless the last file is named once more.
  lines.push(`file '${frameName(frames[frames.length - 1].index)}'`);
  fs.writeFileSync(list, `${lines.join('\n')}\n`);
  const out = path.join(OUT, `demo-${theme}.gif`);
  const result = spawnSync(
    FFMPEG,
    [
      '-hide_banner', '-loglevel', 'error', '-y',
      '-f', 'concat', '-safe', '0', '-i', list,
      '-vf',
      `crop=iw:min(ih\\,${height}):0:0,fps=${Math.round(1000 / FRAME_MS)},split[a][b];` +
        '[a]palettegen=stats_mode=full:max_colors=128[p];[b][p]paletteuse=dither=none:diff_mode=rectangle',
      '-loop', '0', '-an',
      out,
    ],
    { cwd: dir, encoding: 'utf8', timeout: 120_000 },
  );
  if (result.error) throw new Error(`couldn't run ${FFMPEG}: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`ffmpeg failed:\n${result.stderr}`);
  const size = fs.statSync(out).size;
  log(`  ${path.relative(ROOT, out)} (${Math.round(size / 1024)} KB)`);
  if (size > MAX_BYTES) throw new Error(`${path.basename(out)} is over ${MAX_BYTES / 1_000_000} MB`);
}

// One run in one theme: starts Porthole and Chrome, films the four scenes, quits Porthole, stops Chrome.
async function filmRun(chrome, theme) {
  const frameDir = makeTempDir(`demo-${theme}`);
  const port = await portAbove8800();
  const base = `http://127.0.0.1:${port}`;
  log(`${theme}: Porthole on port ${port}`);
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
        // No scroll bar: it would come and go with the page's height and move the column sideways.
        await page.send('Emulation.setScrollbarsHidden', { hidden: true });
        await setTheme(page, theme);
        await page.send('Page.addScriptToEvaluateOnNewDocument', { source: EASY_OFF });
      },
    });
    await sharer.waitFor(`document.getElementById('link').textContent.startsWith('http')`, 'the viewer link');
    const link = await sharer.text('link');
    if (new URL(link).hostname !== EXAMPLE_HOST) throw new Error(`the viewer link doesn't use ${EXAMPLE_HOST}: ${link}`);
    await sharer.waitFor(`!document.getElementById('start').disabled && document.getElementById('busy').hidden`, 'the page to connect');
    if (await sharer.eval(`document.getElementById('easy').checked`)) throw new Error('the easy address is on');
    const clip = await columnClip(sharer);
    const frames = [];

    log('  Not sharing');
    await film(sharer, frames, clip, frameDir, SCENES.off);

    await sharer.click('start');
    await sharer.waitFor(`document.getElementById('state').textContent.startsWith('Sharing')`, 'sharing to start');
    await sharer.waitFor(`document.getElementById('preview').videoWidth > 0`, 'the preview');
    await sleep(300); // a few frames in the preview
    log('  Sharing');
    await film(sharer, frames, clip, frameDir, SCENES.sharing);

    // The example address doesn't exist here: open the same viewer link on this PC, as a phone.
    const { pathname, search, hash } = new URL(link);
    const viewer = await openPage(cdp, 'viewer', `${base}${pathname}${search}${hash}`, {
      ownContext: true,
      beforeLoad: async (page) => {
        await page.send('Emulation.setDeviceMetricsOverride', PHONE);
        await page.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
        await page.send('Emulation.setUserAgentOverride', { userAgent: PHONE_AGENT, platform: 'Android' });
      },
    });
    // "Ask before someone watches" is on, as by default: the phone waits with a code.
    const code = await viewer.waitFor(`(() => {
      const code = document.getElementById('code');
      const found = /(\\d{3})/.exec(code.hidden ? '' : code.textContent);
      return found ? found[1] : '';
    })()`, 'the phone to wait with a code');
    await sharer.waitFor(`document.getElementById('allow-${code}') !== null`, `the waiting row with code ${code}`);
    log(`  A phone waiting (code ${code})`);
    await film(sharer, frames, clip, frameDir, SCENES.waiting);

    await sharer.click(`allow-${code}`);
    await viewer.waitFor(`document.getElementById('video').videoWidth > 0`, 'the phone to get the picture');
    await sharer.waitFor(`document.getElementById('viewers').textContent.includes('Watching')`, 'the phone to show as watching');
    log('  The phone watching');
    await film(sharer, frames, clip, frameDir, SCENES.watching);
    // Cut just below the Watching section as it ends: the waiting scene's lower part (Join, then an empty
    // Watching) is left out, and the first scene's switches fit.
    const height = Math.min(clip.height, (await watchingBottom(sharer)) + 24);

    for (const page of [sharer, viewer]) {
      if (page.errors.length) throw new Error(`${page.name} had script errors: ${page.errors.join(' | ')}`);
    }
    log(`  ${frames.length} frames, ${clip.width} × ${height}`);
    encode(frameDir, frames, theme, height);

    // Quit as the sharer page would: a same-origin request from this PC.
    const res = await fetch(`${base}/api/quit`, { method: 'POST', headers: { Origin: base } });
    if (!res.ok) throw new Error(`Quit was refused: ${res.status}`);
    const ended = await Promise.race([server.exited, sleep(10000).then(() => null)]);
    if (!ended) throw new Error('Porthole is still running 10 s after Quit');
  } finally {
    await stopChrome(browser, cdp);
    await stopChild(server);
  }
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
  const check = spawnSync(FFMPEG, ['-hide_banner', '-version'], { encoding: 'utf8', timeout: 30_000 });
  if (check.error || check.status !== 0) {
    log('ffmpeg not found (set FFMPEG).');
    return 2;
  }
  fs.mkdirSync(OUT, { recursive: true });
  for (const theme of THEMES) await filmRun(chrome, theme);
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
    if (code === 0) log(`Demo saved in ${path.relative(ROOT, OUT)}.`);
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
