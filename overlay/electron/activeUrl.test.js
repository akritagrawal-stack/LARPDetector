// Plain-node unit tests for the schemeless-omnibox fix. No test framework is
// installed in overlay/ (package.json has no "test" script and no jest/vitest),
// so this runs via: node overlay/electron/activeUrl.test.js
//
// It exercises the pure helpers that gate the active-browser-URL capture:
// normalizeCapturedUrl (prepends the scheme Chromium elides) and the two
// validators (looksLikeTargetUrl, looksLikeLinkedInProfileUrl), which were the
// site of the regression that threw away schemeless captures.
//
// No em dashes anywhere (house rule).

const assert = require('assert');
const {
  normalizeCapturedUrl,
  looksLikeTargetUrl,
  looksLikeLinkedInProfileUrl,
} = require('./activeUrl');

let passed = 0;
function check(label, cond) {
  assert.strictEqual(cond, true, 'FAILED: ' + label);
  passed += 1;
  console.log('  ok  ' + label);
}

console.log('normalizeCapturedUrl:');
// THE bug: a schemeless Chromium capture gets the scheme prepended.
check(
  'schemeless linkedin -> https:// prepended',
  normalizeCapturedUrl('linkedin.com/in/jordan-rivera-synthetic/') ===
    'https://linkedin.com/in/jordan-rivera-synthetic/'
);
// A value that already has a scheme is returned unchanged.
check(
  'already-schemed URL is unchanged',
  normalizeCapturedUrl('https://www.linkedin.com/in/x') === 'https://www.linkedin.com/in/x'
);
// Empty stays empty.
check('empty string -> ""', normalizeCapturedUrl('') === '');
// Null-safe: call sites pass the possibly-null capture straight in.
check('null -> ""', normalizeCapturedUrl(null) === '');
check('undefined -> ""', normalizeCapturedUrl(undefined) === '');
// Junk (no dot, a search phrase) is NOT turned into a URL.
check(
  'no-dot phrase is returned unchanged (not a URL)',
  normalizeCapturedUrl('reading your screen') === 'reading your screen'
);
// Leading/trailing whitespace is trimmed and scheme still prepended.
check(
  'whitespace trimmed then schemed',
  normalizeCapturedUrl('  linkedin.com/in/x  ') === 'https://linkedin.com/in/x'
);
check(
  'missing scheme colon is repaired',
  normalizeCapturedUrl('https//www.linkedin.com/in/x') ===
    'https://www.linkedin.com/in/x'
);
check(
  'single scheme slash is repaired',
  normalizeCapturedUrl('https:/www.linkedin.com/in/x') ===
    'https://www.linkedin.com/in/x'
);
check(
  'scheme-relative profile is repaired',
  normalizeCapturedUrl('//www.linkedin.com/in/x') ===
    'https://www.linkedin.com/in/x'
);
check(
  'quoted profile with spaced path separators is repaired',
  normalizeCapturedUrl('"linkedin.com / in / x"') ===
    'https://linkedin.com/in/x'
);

console.log('looksLikeLinkedInProfileUrl:');
// THE regression: a schemeless /in/ profile is now accepted.
check(
  'schemeless linkedin profile -> true',
  looksLikeLinkedInProfileUrl('linkedin.com/in/jordan-rivera-synthetic') === true
);
check(
  'schemed www linkedin profile -> true (still)',
  looksLikeLinkedInProfileUrl('https://www.linkedin.com/in/x') === true
);
check(
  'linkedin company page -> false (not a /in/ profile)',
  looksLikeLinkedInProfileUrl('linkedin.com/company/acme') === false
);
check('null -> false', looksLikeLinkedInProfileUrl(null) === false);
check('bare word -> false', looksLikeLinkedInProfileUrl('vedant') === false);

console.log('looksLikeTargetUrl:');
check(
  'schemeless linkedin profile -> true',
  looksLikeTargetUrl('linkedin.com/in/x') === true
);
check('bare word -> false', looksLikeTargetUrl('hello') === false);
check('null -> false', looksLikeTargetUrl(null) === false);
check(
  'schemeless crunchbase -> true',
  looksLikeTargetUrl('crunchbase.com/organization/acme') === true
);
check('schemeless .ai domain -> true', looksLikeTargetUrl('foo.ai/team') === true);

// ---------------------------------------------------------------------------
// buildActiveUrlScript: the hint and foreground layers must be TARGET-GUARDED.
//
// THE BUG this pins (reproduced live 2026-07-24, two browser windows open): both
// early layers returned ANY non-empty URL and exited, so a stale warm-start hwnd
// or a foreground window sitting on an unrelated tab short-circuited the z-order
// search that would have found the LinkedIn window one layer down. main.js then
// correctly rejected the non-profile URL, the scan fell through to screenshot
// vision, and vision cannot recover a URL at all because Chromium renders the
// omnibox elided ("linkedin.com / Jordan Rivera"). A profile plainly on screen
// scanned as "no URL found". String assertions are the most this plain-node
// harness can do (the real logic is PowerShell), so they check the guard is
// present and that no early layer can exit on an unguarded URL.
// ---------------------------------------------------------------------------

const { buildActiveUrlScript } = require('./activeUrl');

console.log('buildActiveUrlScript layer guards:');
const script = buildActiveUrlScript(12345, 999);

// Every Write-Winner for the two early layers sits behind a target-regex match.
const earlyWinners = script
  .split('\n')
  .filter((line) => /Write-Winner \$u \$(hintH|fg) '(hint|fast)'/.test(line));
check('both early layers still emit a winner line', earlyWinners.length === 2);
check(
  'no early layer exits without a target check on the same line',
  earlyWinners.every((line) => line.includes('-match'))
);

// A non-target early hit is remembered, not returned, and only used once the
// target search has failed. This keeps single-window behavior identical.
check('a non-target early hit is stashed as a fallback', /\$fallback = \$u/.test(script));
check(
  'the fallback is only emitted after both enum results',
  script.indexOf("Write-Winner $firstAny") < script.indexOf('Write-Winner $fallback')
);

console.log('\nAll ' + passed + ' assertions passed.');
