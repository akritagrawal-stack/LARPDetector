const assert = require('assert');
const test = require('node:test');
const {
  COMPANION_HISTORY_URL_AGE_SECONDS,
  MAX_HISTORY_URL_AGE_SECONDS,
  parseHistoryRow,
} = require('./macBrowserHistory');

test('parses a fresh LinkedIn profile history row', () => {
  assert.deepStrictEqual(
    parseHistoryRow('https://www.linkedin.com/in/example-person/\t42\n', 'safari-history'),
    {
      url: 'https://www.linkedin.com/in/example-person/',
      ageSeconds: 42,
      source: 'safari-history',
    }
  );
});

test('rejects non-profile and malformed history rows', () => {
  assert.strictEqual(parseHistoryRow('https://www.linkedin.com/company/example\t4', 'safari'), null);
  assert.strictEqual(parseHistoryRow('https://example.com/in/person\t4', 'safari'), null);
  assert.strictEqual(parseHistoryRow('not-a-row', 'safari'), null);
  assert.strictEqual(parseHistoryRow('https://linkedin.com/in/person\told', 'safari'), null);
});

test('history fallback has a bounded freshness window', () => {
  assert.strictEqual(MAX_HISTORY_URL_AGE_SECONDS, 7200);
  assert.strictEqual(COMPANION_HISTORY_URL_AGE_SECONDS, 900);
});
