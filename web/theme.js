// Light, dark or follow the system: kept free of the DOM so it can be unit-tested.
// The choice is set as data-theme="auto|light|dark" on <html>; style.css does the rest.

export const THEMES = Object.freeze(['auto', 'light', 'dark']);
export const THEME_KEY = 'porthole-theme';

// Anything that isn't a known choice means auto.
export function pickTheme(value) {
  return THEMES.includes(value) ? value : 'auto';
}

// `storage` is localStorage, which can be missing or throw in a private window.
export function readTheme(storage) {
  try {
    return pickTheme(storage ? storage.getItem(THEME_KEY) : null);
  } catch {
    return 'auto';
  }
}

export function saveTheme(storage, choice) {
  try {
    if (!storage) return false;
    storage.setItem(THEME_KEY, pickTheme(choice));
    return true;
  } catch {
    return false; // remembered for this page only
  }
}

// What the page actually looks like: 'light' or 'dark'.
export function resolveTheme(choice, systemDark) {
  const picked = pickTheme(choice);
  if (picked !== 'auto') return picked;
  return systemDark ? 'dark' : 'light';
}

export function applyTheme(root, choice) {
  const picked = pickTheme(choice);
  root.setAttribute('data-theme', picked);
  return picked;
}
