// Plain-node tests for the macOS JXA active-URL capture script.
// Run with: node --test electron/macActiveUrl.test.js
//
// No em dashes anywhere (house rule).

const assert = require('assert');
const test = require('node:test');
const {
  MAC_ACTIVE_URL_TIMEOUT_MS,
  MAC_PERMISSION_PROMPT_TIMEOUT_MS,
  MAC_AUTOMATION_DENIED_SENTINEL,
  MAC_BROWSER_CANDIDATES,
  buildMacActiveUrlScript,
} = require('./macActiveUrl');

test('mac capture includes Safari and major Chromium browsers', () => {
  const names = MAC_BROWSER_CANDIDATES.map((item) => item.name);
  assert(names.includes('Safari'));
  assert(names.includes('Google Chrome'));
  assert(names.includes('Brave Browser'));
  assert(names.includes('Microsoft Edge'));
  assert(names.includes('Arc'));
  assert(names.includes('Comet'));
});

test('mac capture prefers a LinkedIn profile and keeps a fallback', () => {
  const script = buildMacActiveUrlScript();
  assert(script.includes('linkedin\\.com\\/in\\/'));
  assert(script.includes('if (!fallback) fallback = url'));
  assert(script.includes('return fallback'));
});

test('mac capture is bounded and browser failures are isolated', () => {
  const script = buildMacActiveUrlScript();
  assert(MAC_ACTIVE_URL_TIMEOUT_MS > 0);
  assert(MAC_PERMISSION_PROMPT_TIMEOUT_MS >= 30000);
  assert(MAC_PERMISSION_PROMPT_TIMEOUT_MS > MAC_ACTIVE_URL_TIMEOUT_MS);
  assert(script.includes('catch (error)'));
  assert(script.includes("if (!browser.running()) return ''"));
});

test('mac capture reports Automation denial instead of hiding it', () => {
  const script = buildMacActiveUrlScript();
  assert(script.includes('-1743'));
  assert(script.includes(MAC_AUTOMATION_DENIED_SENTINEL));
  assert(script.includes('automationDenied = true'));
});
