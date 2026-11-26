// The "Nothing shared yet" page: checks quietly every few seconds and opens the share once it starts.
// It replaced a page reload every 5 s, which made screen readers read the whole page out again each
// time. Browsers without scripts still get that reload (the page's <noscript> refresh).

const WAITING = 503; // the server's answer while nothing is shared yet
const NOT_FOUND = 404; // "Nothing here": the easy address is off ...
const NOT_FOUND_CHECKS = 3; // ... but it is off for a moment while Porthole starts and when the PC's address changes
const DEFAULT_MS = 5000;

// Keep waiting while the server still says "nothing shared yet", and through a 404 or two in a row
// (`notFoundInARow` counts this one); anything else (the share, after its redirect, or "Nothing here" once the
// easy address is off for good) is shown by going there.
export function nextStep(status, notFoundInARow = 1) {
  if (status === WAITING) return 'wait';
  return status === NOT_FOUND && notFoundInARow < NOT_FOUND_CHECKS ? 'wait' : 'go';
}

// The wait between checks, from the page's data-retry (seconds).
export function retryMs(seconds) {
  const value = Number(seconds);
  return Number.isFinite(value) && value >= 1 ? value * 1000 : DEFAULT_MS;
}

function startChecking() {
  const wait = retryMs(document.body.dataset.retry);
  let notFound = 0;
  const check = async () => {
    try {
      const res = await fetch(location.href, { cache: 'no-store' });
      notFound = res.status === NOT_FOUND ? notFound + 1 : 0;
      if (nextStep(res.status, notFound) === 'go') {
        location.replace(res.url || location.href);
        return;
      }
    } catch {
      // Porthole is out of reach for a moment: keep trying
    }
    setTimeout(check, wait);
  };
  setTimeout(check, wait);
}

if (typeof document !== 'undefined' && document.body) startChecking();
