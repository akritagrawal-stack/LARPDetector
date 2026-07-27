const assert = require('assert');
const { summonShortcutForPlatform } = require('./shortcuts');

assert.strictEqual(summonShortcutForPlatform('darwin'), 'Control+Space');
assert.strictEqual(summonShortcutForPlatform('win32'), 'CommandOrControl+Space');
assert.strictEqual(summonShortcutForPlatform('linux'), 'CommandOrControl+Space');

console.log('All 3 shortcut assertions passed.');
