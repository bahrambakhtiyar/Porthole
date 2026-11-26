import { test } from 'node:test';
import assert from 'node:assert/strict';
import { nextStep, retryMs } from '../../web/retry-page.js';

test('the page keeps waiting quietly while nothing is shared', () => {
  assert.equal(nextStep(503), 'wait');
});

test('anything else is shown by going there', () => {
  // 200 after the redirect to the share; 404 once the easy address is off ("Nothing here").
  for (const status of [200, 400, 500]) assert.equal(nextStep(status), 'go', String(status));
  assert.equal(nextStep(404, 3), 'go');
});

test('a 404 or two in a row is waited out', () => {
  // The easy address is off for a moment while Porthole starts and when the PC's address changes.
  assert.equal(nextStep(404, 1), 'wait');
  assert.equal(nextStep(404, 2), 'wait');
});

test('the wait between checks comes from the page', () => {
  assert.equal(retryMs('5'), 5000);
  assert.equal(retryMs('2'), 2000);
  for (const odd of [undefined, '', 'soon', '0', '-3']) assert.equal(retryMs(odd), 5000, String(odd));
});
