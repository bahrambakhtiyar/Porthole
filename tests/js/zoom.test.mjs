import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MAX_ZOOM, KEY_ZOOM, contentRect, clampView, zoomAt, panBy, actualSizeScale, viewForKey } from '../../web/zoom.js';

const near = (a, b, msg) => assert.ok(Math.abs(a - b) < 0.01, `${msg}: ${a} != ${b}`);
const stage = { w: 1000, h: 500 };
const content = contentRect(1000, 500, 1920, 1080);

test('contentRect fits a wide video into a wider stage by height', () => {
  near(content.h, 500, 'h');
  near(content.w, 888.89, 'w');
  near(content.x, 55.56, 'x');
  near(content.y, 0, 'y');
});

test('contentRect fits a wide video into a tall stage by width', () => {
  const r = contentRect(400, 800, 1920, 1080);
  near(r.w, 400, 'w');
  near(r.h, 225, 'h');
  near(r.y, 287.5, 'y');
});

test('contentRect uses the whole stage before the video size is known', () => {
  assert.deepEqual(contentRect(400, 800, 0, 0), { x: 0, y: 0, w: 400, h: 800 });
});

test('zoomAt keeps the point under the fingers in place', () => {
  const view = zoomAt({ scale: 1, x: 0, y: 0 }, 2, 500, 250, stage, content);
  near(view.scale, 2, 'scale');
  near(view.x, -500, 'x');
  near(view.y, -250, 'y');
  near((500 - view.x) / view.scale, 500, 'anchor x');
});

test('zoom is limited to fit and the maximum', () => {
  assert.equal(MAX_ZOOM, 5);
  assert.equal(zoomAt({ scale: 1, x: 0, y: 0 }, 100, 500, 250, stage, content).scale, 5);
  assert.deepEqual(zoomAt({ scale: 2, x: -500, y: -250 }, 0.1, 500, 250, stage, content), { scale: 1, x: 0, y: 0 });
});

test('panBy stops at the picture edges', () => {
  const view = { scale: 2, x: -500, y: -250 };
  near(panBy(view, 10000, 0, stage, content).x, -111.11, 'left edge');
  near(panBy(view, -10000, 0, stage, content).x, -888.89, 'right edge');
  near(panBy(view, 0, 10000, stage, content).y, 0, 'top edge');
  near(panBy(view, 0, -10000, stage, content).y, -500, 'bottom edge');
});

test('clampView re-centres at fit', () => {
  assert.deepEqual(clampView({ scale: 1, x: 30, y: -20 }, stage, content), { scale: 1, x: 0, y: 0 });
});

test('actual size: one video pixel per device pixel', () => {
  // A 1920 px wide picture shown 400 css px wide on a 3x phone screen (1200 device px): zoom 1.6.
  near(actualSizeScale(1920, 400, 3), 1.6, '3x phone');
  // The same picture 960 css px wide on a 1x screen: zoom 2.
  near(actualSizeScale(1920, 960, 1), 2, '1x PC');
});

test('actual size never shrinks below fit and never passes the maximum', () => {
  assert.equal(actualSizeScale(1280, 1280, 2), 1); // already more device pixels than video pixels
  assert.equal(actualSizeScale(3840, 100, 1), MAX_ZOOM);
});

test('actual size falls back to fit while sizes are unknown or odd', () => {
  for (const [w, c, d] of [[0, 400, 2], [1920, 0, 2], [1920, 400, 0], [NaN, 400, 2], [1920, -5, 2], [1920, 400, undefined]]) {
    const scale = actualSizeScale(w, c, d);
    assert.ok(Number.isFinite(scale) && scale >= 1 && scale <= MAX_ZOOM, `${w} ${c} ${d}: ${scale}`);
  }
  assert.equal(actualSizeScale(0, 400, 2), 1);
  near(actualSizeScale(1920, 960, undefined), 2, 'no devicePixelRatio means 1');
});

// ---------- keys, for people without a mouse or a touch screen ----------

test('+ and - zoom around the middle of the window', () => {
  const zoomedIn = viewForKey('+', { scale: 1, x: 0, y: 0 }, stage, content);
  near(zoomedIn.scale, KEY_ZOOM, 'scale');
  near((stage.w / 2 - zoomedIn.x) / zoomedIn.scale, stage.w / 2, 'the middle stays put (x)');
  near((stage.h / 2 - zoomedIn.y) / zoomedIn.scale, stage.h / 2, 'the middle stays put (y)');
  assert.deepEqual(viewForKey('=', { scale: 1, x: 0, y: 0 }, stage, content), zoomedIn, '= is + without Shift');
  const back = viewForKey('-', zoomedIn, stage, content);
  assert.deepEqual(back, { scale: 1, x: 0, y: 0 });
});

test('zoom keys stop at fit and at the maximum, and still count as used', () => {
  assert.deepEqual(viewForKey('-', { scale: 1, x: 0, y: 0 }, stage, content), { scale: 1, x: 0, y: 0 });
  assert.equal(viewForKey('+', { scale: MAX_ZOOM, x: -2000, y: -1000 }, stage, content).scale, MAX_ZOOM);
});

test('the arrow keys move a zoomed picture a tenth of the window, like scrolling', () => {
  const view = { scale: 2, x: -500, y: -250 };
  // Right shows more of the right-hand side: the picture moves left.
  near(viewForKey('ArrowRight', view, stage, content).x, -500 - stage.w / 10, 'right');
  near(viewForKey('ArrowLeft', view, stage, content).x, -500 + stage.w / 10, 'left');
  near(viewForKey('ArrowDown', view, stage, content).y, -250 - stage.h / 10, 'down');
  near(viewForKey('ArrowUp', view, stage, content).y, -250 + stage.h / 10, 'up');
  assert.equal(viewForKey('ArrowRight', view, stage, content).scale, 2);
});

test('the arrow keys stop at the edges of the picture', () => {
  let view = { scale: 2, x: -500, y: -250 };
  for (let i = 0; i < 50; i += 1) view = viewForKey('ArrowLeft', view, stage, content);
  near(view.x, -111.11, 'left edge');
  for (let i = 0; i < 50; i += 1) view = viewForKey('ArrowUp', view, stage, content);
  near(view.y, 0, 'top edge');
});

test('at fit the arrow keys are left alone, and so is every other key', () => {
  const fitted = { scale: 1, x: 0, y: 0 };
  for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) assert.equal(viewForKey(key, fitted, stage, content), null, key);
  for (const key of ['Tab', 'Enter', ' ', 'a', 'Escape', 'Home', 'PageDown']) {
    assert.equal(viewForKey(key, { scale: 2, x: -500, y: -250 }, stage, content), null, key);
  }
});
