const assert = require('assert');
const test = require('node:test');
const { chromiumSettingsHaveCompanion } = require('./browserCompanion');

test('detects the unpacked companion by its registered source path', () => {
  const settings = {
    abc: {
      path: '/Users/test/LARPDetector/browser-extension',
      location: 4
    }
  };
  assert.strictEqual(
    chromiumSettingsHaveCompanion(
      settings,
      '/Users/test/LARPDetector/browser-extension'
    ),
    true
  );
});

test('does not mistake an unrelated extension for the companion', () => {
  const settings = {
    abc: {
      path: '/Users/test/other-extension',
      location: 4
    }
  };
  assert.strictEqual(
    chromiumSettingsHaveCompanion(
      settings,
      '/Users/test/LARPDetector/browser-extension'
    ),
    false
  );
});
