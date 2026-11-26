// Makes the Snap Store pictures with headless Chrome (Node 22 or newer, no packages):
//
//   node tools/store_images.mjs
//
// Needs Chrome or Chromium and Python 3 (found as in tests/browser/run.mjs: CHROME_PATH or the usual places;
// PYTHON overrides the Python command). Run it again after changing the pages, so the store shows Porthole as it is.
//
// Like tools/screenshots.mjs, it starts Porthole on a free port above 8800 (8801 to 8899 when one is free, so
// the link stays short) with the example address 192.168.1.20 in the viewer link and the easy address saved as
// off (nothing is announced on the real network). The sharer page runs as in normal use, not in test mode (so no
// test note and no test picture): only the browser's screen picker is replaced, by a window showing the banner's
// slide (a title, a bar chart and a ring chart) with silent sound, so the PC, the phone and the banner show the
// same picture. It lets one phone-sized viewer in, pressing Allow next to its code. On the way it captures the
// real pages: the sharer page with the phone waiting and with it watching, in light and dark (picked with the
// page's own theme switch), and the phone waiting with its code, watching on its side, and watching under a
// browser's address bar. Then it quits Porthole and lays the captures out in HTML templates (a browser window, as
// the snap opens the sharer page in the default browser; a phone frame; a calm background and one short caption
// each), which Chrome renders at the store's exact sizes into packaging/snapstore/:
//   banner.png         1920 × 640 (3:1): the ring logo, "Porthole", the tagline and a PC and a phone showing
//                      the same slide, kept clear of the outer edges, which the store may crop
//   screenshot-1.png   1920 × 1080: "Share your screen with any phone", the sharer page sharing the slide and a
//                      phone on its side showing the same slide
//   screenshot-2.png   1920 × 1080: "Phones just scan the QR code", the Join part and a phone that opened the link
//   screenshot-3.png   1920 × 1080: "You decide who watches", a phone waiting with its code, Allow and Deny on the PC
//   screenshot-4.png   1920 × 1080: "Light or dark, your choice", the sharer page in both themes
// and, in packaging/github/:
//   social-preview.png 1280 × 640 (2:1): the banner's design for GitHub's social preview (the picture shown when
//                      the repository's link is shared). `--social-only` makes just this one, without Porthole.
// Each window ends on one of the page's section lines, inside the picture, so no row is cut through. Each
// picture must stay under 2 MB, the store's limit; the tool checks the sizes and stops if one is over. What to
// upload where is in packaging/snapstore/listing.md. The words use Inter where it is installed, else Noto Sans or
// the system's own sans-serif: nothing is downloaded.
//
// For working on the layouts: `--save-pages <folder>` also keeps the page captures there, and
// `--from-pages <folder>` lays out those saved captures again without starting Porthole.

import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

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
const OUT = path.join(ROOT, 'packaging', 'snapstore');
const EXAMPLE_HOST = '192.168.1.20';
const MAX_BYTES = 2_000_000; // the Snap Store's limit for the banner and each screenshot
const SHOT = { width: 1920, height: 1080 };
const BANNER = { width: 1920, height: 640 };
const SOCIAL = { width: 1280, height: 640 }; // GitHub's social preview (Settings > General): 2:1, under 1 MB
const SOCIAL_OUT = path.join(ROOT, 'packaging', 'github');
const SOCIAL_MAX_BYTES = 1_000_000;
// The sharer page as on a PC (twice the pixels, so it stays sharp when shown larger), and the viewer on a phone.
const PC = { width: 900, height: 1000, deviceScaleFactor: 2, mobile: false };
const PHONE = { width: 390, height: 844, deviceScaleFactor: 2, mobile: true };
const PHONE_SIDEWAYS = {
  width: 844,
  height: 390,
  deviceScaleFactor: 2,
  mobile: true,
  screenOrientation: { type: 'landscapePrimary', angle: 90 },
};
// CSS px at the top of the phone's screen taken by a browser's address bar in screenshot 2 (clear of the camera).
const URL_BAR = 84;
const PHONE_BROWSER = { ...PHONE, height: PHONE.height - URL_BAR };
const EASY_OFF = `localStorage.setItem('porthole-settings', ${JSON.stringify(JSON.stringify({ ask: true, smooth: false, easy: false }))});`;
// An Android phone's browser, so the sharer page lists the viewer as "Android phone".
const PHONE_AGENT =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36';
const DEADLINE_MS = 180_000;
// The shared window's size: the slide drawn at 16:9. Its background is a pale blue rather than white, so it stands
// out from the white sharer page in the light theme.
const SLIDE = { width: 1280, height: 720, background: [237, 242, 253] };
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const log = (text) => console.log(text);
cleanUpOnExit();

// ---------- the port ----------

function portFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.listen(port, '0.0.0.0', () => server.close(() => resolve(true)));
  });
}

// Never one Porthole uses by itself (8765 to 8790): 8801 to 8899 when one is free, else any free port above 8800.
async function storePort() {
  for (let port = 8801; port < 8900; port += 1) if (await portFree(port)) return port;
  for (let i = 0; i < 20; i += 1) {
    const port = await freePort();
    if (port > 8800) return port;
  }
  throw new Error('no free port above 8800');
}

// ---------- capturing the real pages ----------

async function setMedia(page, theme) {
  await page.send('Emulation.setEmulatedMedia', {
    features: [
      { name: 'prefers-color-scheme', value: theme },
      { name: 'prefers-reduced-motion', value: 'reduce' },
    ],
  });
}

async function shot(page, clip) {
  const { data } = await page.send('Page.captureScreenshot', { format: 'png', clip: { ...clip, scale: 1 }, captureBeyondViewport: true });
  return Buffer.from(data, 'base64');
}

// The whole sharer page, and where its parts are (CSS px from the top of the page).
async function sharerShot(page) {
  const layout = await page.eval(`(() => {
    const top = (element) => element ? Math.round(element.getBoundingClientRect().top + window.scrollY) : null;
    const section = (id) => document.getElementById(id) && document.getElementById(id).closest('section');
    return {
      width: document.documentElement.clientWidth,
      height: Math.ceil(document.documentElement.scrollHeight),
      status: top(document.getElementById('status-line')),
      waiting: top(document.getElementById('waiting').hidden ? null : document.getElementById('waiting')),
      join: top(section('join-title')),
      watching: top(document.getElementById('watching')),
      settings: top(section('settings-title')),
    };
  })()`);
  const png = await shot(page, { x: 0, y: 0, width: layout.width, height: layout.height });
  return { png, ...layout };
}

async function phoneShot(page, size) {
  return { png: await shot(page, { x: 0, y: 0, width: size.width, height: size.height }), width: size.width, height: size.height };
}

async function setPhone(page, size) {
  await page.send('Emulation.setDeviceMetricsOverride', size);
  await page.waitFor(`innerWidth === ${size.width} && innerHeight === ${size.height}`, `the phone at ${size.width} × ${size.height}`);
  await sleep(400); // the picture laid out again at the new size
}

async function expectFrames(page, id) {
  await page.waitFor(`document.getElementById('${id}').videoWidth > 0`, `video in #${id}`);
  const first = await page.eval(`document.getElementById('${id}').currentTime`);
  await page.waitFor(`document.getElementById('${id}').currentTime > ${first} + 0.5`, `#${id} to keep playing`, 10000);
}

// Stands in for the browser's screen picker on the sharer page (the page itself runs as in normal use): Start
// sharing gets a window showing the banner's slide, as a canvas, with a silent sound track, like a window or tab
// shared with its sound. So the page shows what it shows for a real share: Change, and no note about missing
// sound or about notifications on a whole screen.
function fakeScreen(svg) {
  return `(() => {
    const image = new Image();
    image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(${JSON.stringify(svg)});
    navigator.mediaDevices.getDisplayMedia = async () => {
      await image.decode();
      const canvas = document.createElement('canvas');
      canvas.width = ${SLIDE.width};
      canvas.height = ${SLIDE.height};
      const ctx = canvas.getContext('2d');
      const draw = () => ctx.drawImage(image, 0, 0, ${SLIDE.width}, ${SLIDE.height});
      draw();
      const stream = canvas.captureStream(30);
      setInterval(draw, 100); // a still window: the same picture, a fresh frame now and then
      const sound = new AudioContext().createMediaStreamDestination(); // silence
      for (const track of sound.stream.getAudioTracks()) stream.addTrack(track);
      return stream;
    };
  })();`;
}

// Waits until the video in #id shows the slide: its background, right of its title.
function slideShown(page, id) {
  return page.waitFor(`(() => {
    const video = document.getElementById('${id}');
    if (!video.videoWidth) return false;
    const canvas = document.createElement('canvas');
    canvas.width = 160;
    canvas.height = 90;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(video, 0, 0, 160, 90);
    const pixel = ctx.getImageData(100, 10, 1, 1).data;
    return ${JSON.stringify(SLIDE.background)}.every((value, i) => Math.abs(pixel[i] - value) < 12);
  })()`, `the slide in #${id}`, 10000);
}

// A finger tap in the middle of the picture brings back the viewer's button bar, which hides after 3 s.
async function showBar(page, size) {
  const point = { x: size.width / 2, y: size.height / 2 };
  await page.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
  await sleep(50);
  await page.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitFor(`getComputedStyle(document.getElementById('bar')).visibility === 'visible'`, 'the button bar', 3000);
}

async function hideBar(page) {
  await page.waitFor(`getComputedStyle(document.getElementById('bar')).visibility === 'hidden'`, 'the button bar to hide', 8000);
}

async function capturePages(chrome) {
  const port = await storePort();
  const base = `http://127.0.0.1:${port}`;
  log(`Porthole on port ${port}`);
  const server = await startPorthole({
    python: defaultPython(),
    root: ROOT,
    args: ['--no-browser', '--port', String(port), '--host', EXAMPLE_HOST, '--record-dir', makeTempDir('recordings')],
    base,
  });
  const { browser, cdp } = await startChrome(chrome, makeTempDir('chrome'));
  const pages = {};
  try {
    const sharerUrl = `${base}/share`;
    const sharer = await openPage(cdp, 'sharer', sharerUrl, {
      beforeLoad: async (page) => {
        await page.send('Emulation.setDeviceMetricsOverride', PC);
        await page.send('Emulation.setScrollbarsHidden', { hidden: true });
        await setMedia(page, 'light');
        // The easy address saved as off, so the page never asks Porthole to announce porthole.local on the real
        // network (Porthole never does it by itself). Ask stays on, as by default.
        await page.send('Page.addScriptToEvaluateOnNewDocument', { source: EASY_OFF });
        await page.send('Page.addScriptToEvaluateOnNewDocument', { source: fakeScreen(slide(SLIDE.width)) });
      },
    });
    await sharer.waitFor(`document.getElementById('link').textContent.startsWith('http')`, 'the viewer link');
    const link = await sharer.text('link');
    if (new URL(link).hostname !== EXAMPLE_HOST) throw new Error(`the viewer link doesn't use ${EXAMPLE_HOST}: ${link}`);
    await sharer.waitFor(`!document.getElementById('start').disabled && document.getElementById('busy').hidden`, 'the page to connect');
    if (await sharer.eval(`document.getElementById('easy').checked`)) throw new Error('the easy address is on');
    await sharer.click('start');
    await sharer.waitFor(`document.getElementById('state').textContent.startsWith('Sharing')`, 'sharing to start');
    await expectFrames(sharer, 'preview');
    await slideShown(sharer, 'preview');
    if (await sharer.eval(`!document.getElementById('test-note').hidden || document.getElementById('change').hidden`)) {
      throw new Error('the sharer page is in test mode');
    }
    const notes = await sharer.eval(`['audio-hint', 'monitor-hint', 'notice', 'error']
      .filter((id) => !document.getElementById(id).hidden).join(', ')`);
    if (notes) throw new Error(`the sharer page shows a note while sharing: ${notes}`);
    log('sharing the slide');

    // The example address doesn't exist here: open the same viewer link on this PC, as a phone.
    const { pathname, search, hash } = new URL(link);
    const viewer = await openPage(cdp, 'viewer', `${base}${pathname}${search}${hash}`, {
      ownContext: true,
      beforeLoad: async (page) => {
        await page.send('Emulation.setDeviceMetricsOverride', PHONE);
        await page.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
        await page.send('Emulation.setUserAgentOverride', { userAgent: PHONE_AGENT, platform: 'Android' });
        await setMedia(page, 'light');
      },
    });
    // "Ask before someone watches" is on, as by default: the phone waits with a code.
    const code = await viewer.waitFor(`(() => {
      const code = document.getElementById('code');
      const found = /(\\d{3})/.exec(code.hidden ? '' : code.textContent);
      return found ? found[1] : '';
    })()`, 'the phone to wait with a code');
    await sharer.waitFor(`document.getElementById('allow-${code}') !== null`, `the waiting row with code ${code}`);
    await sleep(500);
    log(`a phone waiting with code ${code}`);
    pages.sharerWaiting = await sharerShot(sharer);
    pages.phoneWaiting = await phoneShot(viewer, PHONE);
    if (pages.sharerWaiting.waiting === null) throw new Error('the Waiting section is hidden');

    await sharer.click(`allow-${code}`);
    await expectFrames(viewer, 'video');
    await slideShown(viewer, 'video');
    await sharer.waitFor(`document.getElementById('viewers').textContent.includes('Watching')`, 'the phone to show as watching');
    log('the phone watching');

    // Screenshot 1: the phone on its side, the picture without its buttons, at the same moment as the PC.
    await setPhone(viewer, PHONE_SIDEWAYS);
    await hideBar(viewer);
    [pages.sharerWatching, pages.phoneSideways] = await Promise.all([sharerShot(sharer), phoneShot(viewer, PHONE_SIDEWAYS)]);

    // Screenshot 2: the phone upright under a browser's address bar, with its buttons.
    await setPhone(viewer, PHONE_BROWSER);
    await slideShown(viewer, 'video');
    await showBar(viewer, PHONE_BROWSER);
    pages.phoneBrowser = await phoneShot(viewer, PHONE_BROWSER);

    // Screenshot 4: the sharer page in each theme, picked with its own switch.
    for (const theme of ['light', 'dark']) {
      await sharer.click(`theme-${theme}`);
      await sharer.waitFor(`document.documentElement.dataset.theme === '${theme}'`, `the ${theme} theme`);
      await sleep(600); // a frame or two in the new colours
      pages[`sharer${theme === 'light' ? 'Light' : 'Dark'}`] = await sharerShot(sharer);
    }
    log('both themes');

    for (const page of [sharer, viewer]) {
      if (page.errors.length) throw new Error(`${page.name} had script errors: ${page.errors.join(' | ')}`);
    }
    pages.link = link;
    pages.code = code;
    pages.sharerUrl = sharerUrl;

    // Quit as the sharer page would: a same-origin request from this PC.
    const res = await fetch(`${base}/api/quit`, { method: 'POST', headers: { Origin: base } });
    if (!res.ok) throw new Error(`Quit was refused: ${res.status}`);
    const ended = await Promise.race([server.exited, sleep(10000).then(() => null)]);
    if (!ended) throw new Error('Porthole is still running 10 s after Quit');
    return { browser, cdp, pages };
  } catch (err) {
    await stopChrome(browser, cdp);
    throw err;
  } finally {
    await stopChild(server);
  }
}

// ---------- saving and loading the captures ----------

const PICTURES = ['sharerWaiting', 'phoneWaiting', 'sharerWatching', 'phoneSideways', 'phoneBrowser', 'sharerLight', 'sharerDark'];

function savePages(pages, dir) {
  fs.mkdirSync(dir, { recursive: true });
  const info = { link: pages.link, code: pages.code, sharerUrl: pages.sharerUrl };
  for (const name of PICTURES) {
    const { png, ...rest } = pages[name];
    fs.writeFileSync(path.join(dir, `${name}.png`), png);
    info[name] = rest;
  }
  fs.writeFileSync(path.join(dir, 'pages.json'), `${JSON.stringify(info, null, 2)}\n`);
}

function loadPages(dir) {
  const info = JSON.parse(fs.readFileSync(path.join(dir, 'pages.json'), 'utf8'));
  for (const name of PICTURES) info[name].png = fs.readFileSync(path.join(dir, `${name}.png`));
  return info;
}

// ---------- the layouts ----------

const FONT = `Inter, "Noto Sans", Cantarell, Ubuntu, system-ui, sans-serif`;
// The browser window's tab strip and toolbar, in px of the picture.
const TAB_STRIP = 42;
const TOOLBAR = 46;
const INK = '#0f1b3d';
const BLUE = '#2563eb'; // the logo's blue

const BASE_CSS = `
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body { position: relative; overflow: hidden; font-family: ${FONT}; -webkit-font-smoothing: antialiased; }
  img { display: block; }
  .caption { position: absolute; left: 0; right: 0; top: 68px; margin: 0; text-align: center; font-size: 64px;
    font-weight: 700; line-height: 1.1; letter-spacing: -0.02em; }
  .ring { position: absolute; border-radius: 50%; border-style: solid; }
  .window { position: absolute; border-radius: 12px; overflow: hidden; }
  .tabs { position: relative; height: ${TAB_STRIP}px; display: flex; align-items: flex-end; padding-left: 10px; }
  .tab { width: 250px; height: 34px; display: flex; align-items: center; gap: 9px; padding: 0 10px 0 14px;
    border-radius: 10px 10px 0 0; font-size: 14px; }
  .tab .icon { width: 16px; height: 16px; flex: none; }
  .tab .title { flex: 1; white-space: nowrap; overflow: hidden; }
  .tab svg, .toolbar svg, .tabs svg { display: block; flex: none; }
  .newtab { width: 34px; height: 34px; display: grid; place-items: center; }
  .tabs .buttons { position: absolute; right: 14px; top: 0; bottom: 0; display: flex; align-items: center; gap: 10px; }
  .tabs .buttons span { width: 24px; height: 24px; border-radius: 50%; display: grid; place-items: center; }
  .toolbar { height: ${TOOLBAR}px; display: flex; align-items: center; gap: 6px; padding: 0 12px 0 10px; }
  .toolbar .tool { width: 32px; height: 32px; display: grid; place-items: center; }
  .omnibox { flex: 1; height: 32px; margin-left: 6px; display: flex; align-items: center; gap: 10px; padding: 0 14px;
    border-radius: 16px; font-size: 15px; white-space: nowrap; overflow: hidden; }
  .omnibox .info { flex: none; width: 17px; height: 17px; border-radius: 50%; border: 1.8px solid currentColor;
    display: grid; place-items: center; font-size: 11px; font-weight: 700; line-height: 1; opacity: 0.75; }
  .content { position: relative; overflow: hidden; }
  .content img { position: absolute; left: 50%; transform: translateX(-50%); }
  .phone { position: absolute; background: #121418; box-shadow: inset 0 0 0 2px #2d3138, 0 40px 90px rgba(15, 27, 61, 0.35); }
  .screen { position: relative; width: 100%; height: 100%; overflow: hidden; background: #000; }
  .screen img { width: 100%; }
  .camera { position: absolute; width: 14px; height: 14px; border-radius: 50%; background: #0b0c0f;
    box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.06); }
  .urlbar { display: flex; align-items: flex-end; background: #f1f3f4; border-bottom: 1px solid #dadce0; }
  .urlbar .pill { flex: 1; display: flex; align-items: center; gap: 0.6em; background: #e3e6ea; color: #202124;
    border-radius: 999px; white-space: nowrap; overflow: hidden; }
  .urlbar .info { flex: none; width: 1.15em; height: 1.15em; border-radius: 50%; border: 0.12em solid #5f6368;
    display: grid; place-items: center; font-size: 0.85em; font-weight: 700; color: #5f6368; line-height: 1; }
`;

const ICON = `<svg class="icon" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="${BLUE}"/>` +
  `<circle cx="32" cy="32" r="14.45" fill="none" stroke="#fff" stroke-width="8.5"/></svg>`;

const GLYPH = (path, color, size = 12, width = 2.4) =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true">` +
  `<path d="${path}" stroke="${color}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>`;
const GLYPHS = {
  minimize: 'M5 12h14',
  maximize: 'M6 6h12v12H6z',
  close: 'M6 6l12 12M18 6L6 18',
  plus: 'M12 5v14M5 12h14',
  back: 'M19 12H5M11 6l-6 6 6 6',
  forward: 'M5 12h14M13 6l6 6-6 6',
  reload: 'M19 12a7 7 0 1 1-2.05-4.95M19 4v4h-4',
};

// The height in px of a browser window showing the sharer capture pages[name] from `from` to `to` at `scale`.
function windowHeight(pages, name, { scale, from = 0, to = null }) {
  return TAB_STRIP + TOOLBAR + Math.round(((to === null ? pages[name].height : to) - from) * scale);
}

// A browser window, as the snap opens the sharer page in the default browser: one tab titled Porthole (the page's
// title, with its ring icon) and the sharer page's address. It shows the sharer capture pages[name] at `scale`, from
// `from` to `to` (CSS px from the top of the page; `to` is best a section's top, so the window ends on the space
// under a section, before its line). Its height follows from those.
function browserWindow(pages, name, { x, y, width, scale, from = 0, to = null, dark = false }) {
  const page = pages[name];
  const height = windowHeight(pages, name, { scale, from, to });
  const contentHeight = height - TAB_STRIP - TOOLBAR;
  const colours = dark
    ? { page: '#131416', strip: '#1f2023', toolbar: '#35363a', omnibox: '#202124', line: '#46484d', text: '#e8eaed',
        dim: '#9aa0a6', button: '#3c3d41', frame: '0 0 0 1px rgba(255, 255, 255, 0.14), 0 40px 100px rgba(0, 0, 0, 0.55)' }
    : { page: '#fff', strip: '#dee1e6', toolbar: '#fff', omnibox: '#f1f3f4', line: '#dadce0', text: '#202124',
        dim: '#9aa0a6', button: '#c9ccd2', frame: '0 0 0 1px rgba(15, 27, 61, 0.12), 0 40px 100px rgba(15, 27, 61, 0.22)' };
  const buttons = ['minimize', 'maximize', 'close']
    .map((button) => `<span style="background:${colours.button}">${GLYPH(GLYPHS[button], colours.text)}</span>`)
    .join('');
  const tool = (glyph, colour) => `<span class="tool">${GLYPH(GLYPHS[glyph], colour, 18, 2)}</span>`;
  const address = new URL(pages.sharerUrl).host + new URL(pages.sharerUrl).pathname; // as a browser shows it
  return `
  <div class="window" style="left:${x}px; top:${y}px; width:${width}px; height:${height}px;
    background:${colours.page}; box-shadow:${colours.frame};">
    <div class="tabs" style="background:${colours.strip};">
      <div class="tab" style="background:${colours.toolbar}; color:${colours.text};">
        ${ICON}<span class="title">Porthole</span>${GLYPH(GLYPHS.close, colours.text, 10)}
      </div>
      <span class="newtab">${GLYPH(GLYPHS.plus, colours.text, 14, 2)}</span>
      <div class="buttons">${buttons}</div>
    </div>
    <div class="toolbar" style="background:${colours.toolbar}; border-bottom: 1px solid ${colours.line};">
      ${tool('back', colours.text)}${tool('forward', colours.dim)}${tool('reload', colours.text)}
      <div class="omnibox" style="background:${colours.omnibox}; color:${colours.text};">
        <span class="info">i</span><span>${address}</span>
      </div>
    </div>
    <div class="content" style="height:${contentHeight}px;">
      <img src="${name}.png" style="width:${page.width * scale}px; top:${-from * scale}px;" alt="">
    </div>
  </div>`;
}

// A phone frame around the viewer capture pages[name], shown `screenWidth` px wide, with its top and height in px.
// `urlBar`: the address that a browser's address bar above the page shows.
function phoneFrame(pages, name, { x, y, screenWidth, urlBar = null }) {
  const page = pages[name];
  const scale = screenWidth / page.width;
  const bezel = 14;
  const radius = 46;
  const barHeight = urlBar ? URL_BAR * scale : 0;
  const screenHeight = page.height * scale + barHeight;
  const camera = page.width > page.height // on its side: the camera on the left
    ? `left:${bezel / 2 + 16}px; top:50%; transform: translateY(-50%);`
    : `top:${bezel + 12}px; left:50%; transform: translateX(-50%);`;
  const bar = urlBar
    ? `<div class="urlbar" style="height:${barHeight}px; padding: 0 ${12 * scale}px ${10 * scale}px; font-size:${15 * scale}px;">
         <div class="pill" style="height:${38 * scale}px; padding: 0 ${14 * scale}px;">
           <span class="info">i</span><span>${urlBar}</span>
         </div>
       </div>`
    : '';
  const height = screenHeight + 2 * bezel;
  const html = `
  <div class="phone" style="left:${x}px; top:${y}px; width:${screenWidth + 2 * bezel}px;
    height:${height}px; padding:${bezel}px; border-radius:${radius + bezel}px;">
    <div class="screen" style="border-radius:${radius}px;">${bar}<img src="${name}.png" alt=""></div>
    <div class="camera" style="${camera}"></div>
  </div>`;
  return { html, top: y, height };
}

// The top for a window `height` px tall, so it is centred on the phone's height.
function besidePhone(phone, height) {
  return Math.round(phone.top + (phone.height - height) / 2);
}

// Big, faint rings, the logo's shape, behind everything: [x, y, size, line width] each.
function rings(colour, list) {
  return list
    .map(([x, y, size, width]) => `<div class="ring" style="left:${x - size / 2}px; top:${y - size / 2}px;
      width:${size}px; height:${size}px; border-width:${width}px; border-color:${colour};"></div>`)
    .join('');
}

function document_(size, background, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><style>${BASE_CSS}
  body { width:${size.width}px; height:${size.height}px; background:${background}; }</style></head><body>${body}</body></html>`;
}

const CORNER_RINGS = [[90, 960, 520, 70], [1830, 150, 380, 52]];
function lightScreenshot(caption, body) {
  return document_(SHOT, 'linear-gradient(180deg, #f3f6ff 0%, #dfe8fd 100%)', `${rings('rgba(37, 99, 235, 0.07)', CORNER_RINGS)}
    <h1 class="caption" style="color:${INK}">${caption}</h1>${body}`);
}

// The viewer link without "http://", as a phone's address bar shows it.
function shortLink(link) {
  const url = new URL(link);
  return `${url.host}${url.pathname}`;
}

// The window from the top of the page to the line above Join: sharing, with the slide in the preview. The phone on
// its side, in front of the window's empty right side (between Quit and the buttons), shows the same slide.
function screenshot1(p) {
  const pc = browserWindow(p, 'sharerWatching', { x: 170, y: 250, width: 1100, scale: 1.45, to: p.sharerWatching.join });
  return lightScreenshot('Share your screen with any phone', `${pc}
    ${phoneFrame(p, 'phoneSideways', { x: 1110, y: 495, screenWidth: 600 }).html}`);
}

// The window from just below Join's line to the line above Settings, with the phone watching beside it. The windows
// in screenshots 2 and 3 are centred on the phone's height.
function screenshot2(p) {
  const page = p.sharerWatching;
  const phone = phoneFrame(p, 'phoneBrowser', { x: 1262, y: 180, screenWidth: 390, urlBar: shortLink(p.link) });
  const view = { x: 230, width: 1000, scale: 1.4, from: page.join + 1, to: page.settings };
  const pc = browserWindow(p, 'sharerWatching', { ...view, y: besidePhone(phone, windowHeight(p, 'sharerWatching', view)) });
  return lightScreenshot('Phones just scan the QR code', `${pc}${phone.html}`);
}

// The window from the status line to the line above Join, so Waiting with Allow and Deny is in view.
function screenshot3(p) {
  const page = p.sharerWaiting;
  const phone = phoneFrame(p, 'phoneWaiting', { x: 1262, y: 180, screenWidth: 390 });
  const view = { x: 230, width: 1000, scale: 1.3, from: page.status, to: page.join };
  const pc = browserWindow(p, 'sharerWaiting', { ...view, y: besidePhone(phone, windowHeight(p, 'sharerWaiting', view)) });
  return lightScreenshot('You decide who watches', `${pc}${phone.html}`);
}

// Both windows from the top of the page to the line above Watching.
function screenshot4(p) {
  const light = browserWindow(p, 'sharerLight', { x: 170, y: 215, width: 800, scale: 1.05, to: p.sharerLight.watching });
  const dark = browserWindow(p, 'sharerDark', { x: 950, y: 275, width: 800, scale: 1.05, to: p.sharerDark.watching, dark: true });
  return document_(SHOT, 'linear-gradient(180deg, #172447 0%, #0b1226 100%)', `${rings('rgba(127, 168, 255, 0.08)', CORNER_RINGS)}
    <h1 class="caption" style="color:#fff">Light or dark, your choice</h1>
    ${light}
    ${dark}`);
}

// The one picture shared everywhere: on the PC and the phone in the banner, and (drawn on a canvas by fakeScreen) as
// the shared window in the screenshots. A slide with a title, a bar chart and a ring chart.
function slide(width) {
  const bars = [18, 28, 22, 36]
    .map((h, i) => `<rect x="${14 + i * 16}" y="${76 - h}" width="11" height="${h}" rx="2" fill="${i === 3 ? BLUE : '#93b4fb'}"/>`)
    .join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 90" width="${width}" height="${(width * 9) / 16}" aria-hidden="true">
    <rect width="160" height="90" fill="rgb(${SLIDE.background.join(', ')})"/>
    <rect x="14" y="13" width="72" height="9" rx="4.5" fill="#1e3a8a"/>
    <rect x="14" y="27" width="46" height="5" rx="2.5" fill="#c7d6fd"/>
    ${bars}
    <circle cx="122" cy="55" r="16" fill="none" stroke="#c7d6fd" stroke-width="8"/>
    <circle cx="122" cy="55" r="16" fill="none" stroke="${BLUE}" stroke-width="8" stroke-dasharray="65 101" transform="rotate(-90 122 55)"/>
  </svg>`;
}

function banner() {
  const background = `radial-gradient(1200px 700px at 78% 40%, #3b7bff 0%, rgba(59, 123, 255, 0) 70%),
    linear-gradient(135deg, ${BLUE} 0%, #1d4ed8 55%, #1e3a8a 100%)`;
  const glow = rings('rgba(255, 255, 255, 0.07)', [[1400, 330, 760, 64], [1400, 330, 1180, 40]]);
  return document_(BANNER, background, `${glow}
    <style>
      .brand { position: absolute; left: 250px; top: 168px; width: 760px; color: #fff; }
      .brand .row { display: flex; align-items: center; gap: 36px; }
      .brand .logo { width: 132px; height: 132px; border-radius: 30px;
        box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.35), 0 18px 40px rgba(10, 20, 60, 0.35); }
      .brand .logo svg { width: 100%; height: 100%; display: block; }
      .brand .name { font-size: 124px; font-weight: 800; letter-spacing: -0.03em; line-height: 1; }
      .brand .tagline { margin-top: 34px; max-width: 700px; font-size: 46px; font-weight: 500; line-height: 1.2;
        color: rgba(255, 255, 255, 0.94); }
      .monitor { position: absolute; left: 1050px; top: 122px; width: 500px; }
      .monitor .bezel { background: #0f172a; border-radius: 22px; padding: 16px; box-shadow: 0 30px 70px rgba(10, 20, 60, 0.45); }
      .monitor .bezel svg { display: block; border-radius: 8px; }
      .monitor .neck { width: 90px; height: 44px; margin: 0 auto; background: linear-gradient(#0f172a, #1e293b); }
      .monitor .foot { width: 220px; height: 14px; margin: 0 auto; border-radius: 7px; background: #0f172a; }
      .handset { position: absolute; left: 1515px; top: 226px; width: 150px; height: 290px; padding: 9px;
        background: #0f172a; border-radius: 28px; box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.18), 0 30px 60px rgba(10, 20, 60, 0.5); }
      .handset .glass { width: 100%; height: 100%; display: flex; align-items: center; overflow: hidden;
        border-radius: 20px; background: #000; }
    </style>
    <div class="brand">
      <div class="row"><div class="logo">${ICON.replace('class="icon"', '')}</div><div class="name">Porthole</div></div>
      <div class="tagline">Share your screen with phones on your Wi-Fi</div>
    </div>
    <div class="monitor">
      <div class="bezel">${slide(468)}</div>
      <div class="neck"></div><div class="foot"></div>
    </div>
    <div class="handset"><div class="glass">${slide(132)}</div></div>`);
}

function socialPreview() {
  const background = `radial-gradient(900px 600px at 76% 42%, #3b7bff 0%, rgba(59, 123, 255, 0) 70%),
    linear-gradient(135deg, ${BLUE} 0%, #1d4ed8 55%, #1e3a8a 100%)`;
  const glow = rings('rgba(255, 255, 255, 0.07)', [[930, 330, 560, 48], [930, 330, 860, 30]]);
  return document_(SOCIAL, background, `${glow}
    <style>
      .brand { position: absolute; left: 96px; top: 190px; width: 560px; color: #fff; }
      .brand .row { display: flex; align-items: center; gap: 26px; }
      .brand .logo { width: 104px; height: 104px; border-radius: 24px;
        box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.35), 0 14px 32px rgba(10, 20, 60, 0.35); }
      .brand .logo svg { width: 100%; height: 100%; display: block; }
      .brand .name { font-size: 96px; font-weight: 800; letter-spacing: -0.03em; line-height: 1; }
      .brand .tagline { margin-top: 28px; max-width: 520px; font-size: 38px; font-weight: 500; line-height: 1.2;
        color: rgba(255, 255, 255, 0.94); }
      .monitor { position: absolute; left: 690px; top: 150px; width: 400px; }
      .monitor .bezel { background: #0f172a; border-radius: 18px; padding: 13px; box-shadow: 0 24px 56px rgba(10, 20, 60, 0.45); }
      .monitor .bezel svg { display: block; border-radius: 7px; }
      .monitor .neck { width: 72px; height: 36px; margin: 0 auto; background: linear-gradient(#0f172a, #1e293b); }
      .monitor .foot { width: 176px; height: 12px; margin: 0 auto; border-radius: 6px; background: #0f172a; }
      .handset { position: absolute; left: 1062px; top: 236px; width: 122px; height: 236px; padding: 8px;
        background: #0f172a; border-radius: 24px; box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.18), 0 24px 48px rgba(10, 20, 60, 0.5); }
      .handset .glass { width: 100%; height: 100%; display: flex; align-items: center; overflow: hidden;
        border-radius: 17px; background: #000; }
    </style>
    <div class="brand">
      <div class="row"><div class="logo">${ICON.replace('class="icon"', '')}</div><div class="name">Porthole</div></div>
      <div class="tagline">Share your screen with phones on your Wi-Fi</div>
    </div>
    <div class="monitor">
      <div class="bezel">${slide(374)}</div>
      <div class="neck"></div><div class="foot"></div>
    </div>
    <div class="handset"><div class="glass">${slide(106)}</div></div>`);
}

// ---------- rendering ----------

function pngSize(data) {
  if (!data.subarray(0, 8).equals(PNG_SIGNATURE)) throw new Error('not a PNG');
  return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) };
}

async function render(cdp, dir, name, size, html, { out = OUT, maxBytes = MAX_BYTES } = {}) {
  const file = path.join(dir, `${name}.html`);
  fs.writeFileSync(file, html);
  const page = await openPage(cdp, name, pathToFileURL(file).href, {
    beforeLoad: (p) => p.send('Emulation.setDeviceMetricsOverride', { ...size, deviceScaleFactor: 1, mobile: false }),
  });
  await page.eval(`Promise.all([document.fonts.ready, ...[...document.images].map((image) => image.decode())]).then(() => true)`);
  const { data } = await page.send('Page.captureScreenshot', { format: 'png', clip: { x: 0, y: 0, ...size, scale: 1 } });
  await page.send('Page.close').catch(() => {});
  const png = Buffer.from(data, 'base64');
  const got = pngSize(png);
  if (got.width !== size.width || got.height !== size.height) {
    throw new Error(`${name} is ${got.width} × ${got.height}, not ${size.width} × ${size.height}`);
  }
  if (png.length > maxBytes) throw new Error(`${name}.png is ${png.length} bytes, over the limit of ${maxBytes}`);
  fs.mkdirSync(out, { recursive: true });
  const file_ = path.join(out, `${name}.png`);
  fs.writeFileSync(file_, png);
  log(`  ${path.relative(ROOT, file_)} (${size.width} × ${size.height}, ${Math.round(png.length / 1024)} KB)`);
}

function option(name) {
  const at = process.argv.indexOf(name);
  if (at === -1) return null;
  const value = process.argv[at + 1];
  if (!value || value.startsWith('--')) throw new Error(`${name} needs a folder`);
  return path.resolve(value);
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
  if (process.argv.includes('--social-only')) {
    const { browser, cdp } = await startChrome(chrome, makeTempDir('chrome'));
    try {
      await render(cdp, makeTempDir('social'), 'social-preview', SOCIAL, socialPreview(), { out: SOCIAL_OUT, maxBytes: SOCIAL_MAX_BYTES });
    } finally {
      await stopChrome(browser, cdp);
    }
    return 0;
  }
  const saveTo = option('--save-pages');
  const from = option('--from-pages');
  let browser, cdp, pages;
  if (from) {
    pages = loadPages(from);
    ({ browser, cdp } = await startChrome(chrome, makeTempDir('chrome')));
    log(`using the pages saved in ${from}`);
  } else {
    ({ browser, cdp, pages } = await capturePages(chrome));
    if (saveTo) savePages(pages, saveTo);
  }
  try {
    const dir = makeTempDir('store');
    for (const name of PICTURES) fs.writeFileSync(path.join(dir, `${name}.png`), pages[name].png);
    fs.mkdirSync(OUT, { recursive: true });
    log('laying out');
    await render(cdp, dir, 'banner', BANNER, banner());
    await render(cdp, dir, 'screenshot-1', SHOT, screenshot1(pages));
    await render(cdp, dir, 'screenshot-2', SHOT, screenshot2(pages));
    await render(cdp, dir, 'screenshot-3', SHOT, screenshot3(pages));
    await render(cdp, dir, 'screenshot-4', SHOT, screenshot4(pages));
    await render(cdp, dir, 'social-preview', SOCIAL, socialPreview(), { out: SOCIAL_OUT, maxBytes: SOCIAL_MAX_BYTES });
  } finally {
    await stopChrome(browser, cdp);
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
    if (code === 0 && !process.argv.includes('--social-only')) log(`Store pictures saved in ${path.relative(ROOT, OUT)}.`);
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
