// Pinch-zoom and pan maths for the viewer, kept free of the DOM so it can be unit-tested.
// A view {scale, x, y} is applied to the video as `translate(x, y) scale(scale)` with origin 0 0.

export const MAX_ZOOM = 5;

const tidy = (value) => Math.round(value * 1000) / 1000 + 0; // drop float noise and -0

// Where the picture sits inside the stage with object-fit: contain.
export function contentRect(stageW, stageH, videoW, videoH) {
  if (!videoW || !videoH || !stageW || !stageH) return { x: 0, y: 0, w: stageW, h: stageH };
  const fit = Math.min(stageW / videoW, stageH / videoH);
  const w = videoW * fit;
  const h = videoH * fit;
  return { x: (stageW - w) / 2, y: (stageH - h) / 2, w, h };
}

function clampAxis(offset, scale, start, size, stageSize) {
  const scaledSize = scale * size;
  if (scaledSize <= stageSize + 0.5) return (stageSize - scaledSize) / 2 - scale * start; // centre
  const min = stageSize - scaledSize - scale * start; // right/bottom edge touches the stage edge
  const max = -scale * start; // left/top edge touches the stage edge
  return Math.min(max, Math.max(min, offset));
}

export function clampView(view, stage, content) {
  const scale = Math.min(MAX_ZOOM, Math.max(1, view.scale));
  return {
    scale: tidy(scale),
    x: tidy(clampAxis(view.x, scale, content.x, content.w, stage.w)),
    y: tidy(clampAxis(view.y, scale, content.y, content.h, stage.h)),
  };
}

// Zoom by `factor`, keeping the stage point (px, py) over the same spot of the picture.
export function zoomAt(view, factor, px, py, stage, content) {
  const scale = Math.min(MAX_ZOOM, Math.max(1, view.scale * factor));
  const k = scale / view.scale;
  return clampView({ scale, x: px - (px - view.x) * k, y: py - (py - view.y) * k }, stage, content);
}

export function panBy(view, dx, dy, stage, content) {
  return clampView({ scale: view.scale, x: view.x + dx, y: view.y + dy }, stage, content);
}

// Keys, for people without a mouse or a touch screen: + (or =) and - zoom around the middle of the
// window; while zoomed, the arrow keys move the picture a tenth of the window, as scrolling would
// (Right shows more of the right-hand side). Returns the new view, or null for a key left alone.
export const KEY_ZOOM = 1.25;

export function viewForKey(key, view, stage, content) {
  const middle = [stage.w / 2, stage.h / 2];
  if (key === '+' || key === '=') return zoomAt(view, KEY_ZOOM, ...middle, stage, content);
  if (key === '-') return zoomAt(view, 1 / KEY_ZOOM, ...middle, stage, content);
  const step = { ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[key];
  if (!step || view.scale <= 1.001) return null;
  return panBy(view, (step[0] * stage.w) / 10, (step[1] * stage.h) / 10, stage, content);
}

// "1:1": the zoom at which one video pixel covers one device pixel. `contentW` is the picture's
// width at fit, in CSS pixels; `dpr` is window.devicePixelRatio. Never below fit, never past the maximum.
export function actualSizeScale(videoW, contentW, dpr) {
  const ratio = Number.isFinite(dpr) && dpr > 0 ? dpr : 1;
  if (!Number.isFinite(videoW) || videoW <= 0 || !Number.isFinite(contentW) || contentW <= 0) return 1;
  return Math.min(MAX_ZOOM, Math.max(1, videoW / (contentW * ratio)));
}
