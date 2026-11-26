// Applies the saved theme before the page paints, so a Dark choice doesn't flash white first.
// A classic (non-module) script on purpose: modules run only after the page is drawn.
try {
  const saved = localStorage.getItem('porthole-theme');
  if (saved === 'light' || saved === 'dark' || saved === 'auto') document.documentElement.dataset.theme = saved;
} catch {
  // storage blocked: Auto
}
