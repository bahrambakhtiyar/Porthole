// Sharer page settings, kept free of the DOM so they can be unit-tested.
// Easy address and Ask are coupled: anyone on the Wi-Fi can find an easy address, so it needs Ask.

export const DEFAULTS = Object.freeze({ ask: true, smooth: false, easy: true });

// `raw` is the saved JSON text (or null). Settings saved before the easy address existed turn Ask on.
export function loadSettings(raw) {
  let saved = {};
  try {
    const parsed = JSON.parse(raw || '{}');
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) saved = parsed;
  } catch {
    // broken: use the defaults
  }
  const settings = { ...DEFAULTS };
  for (const key of Object.keys(DEFAULTS)) {
    if (typeof saved[key] === 'boolean') settings[key] = saved[key];
  }
  // Saved before the easy address existed, or Easy on without Ask: Ask goes on.
  if (typeof saved.easy !== 'boolean' || settings.easy) settings.ask = true;
  return settings;
}

export function withEasy(settings, on) {
  return { ...settings, easy: on, ask: on ? true : settings.ask };
}

export function withAsk(settings, on) {
  return { ...settings, ask: on, easy: on ? settings.easy : false };
}

// The address to show under the QR code ("porthole.local:8765"), or '' while it isn't announced.
export function easyLine(state) {
  if (!state || !state.enabled || !state.available) return '';
  return (state.short_url || state.url || '').replace(/^http:\/\//, '');
}

// How the Easy address switch looks. Turned off for this run (--no-discovery): off and greyed out, with a
// plain note, whatever was saved (the saved choice stays for later runs). A start problem shows as an error
// while the switch is on.
export function easyView(settings, state) {
  if (state && state.blocked) return { checked: false, disabled: true, error: '', note: state.error || '' };
  const error = settings.easy && state && !state.available ? state.error || '' : '';
  return { checked: Boolean(settings.easy), disabled: false, error, note: '' };
}
