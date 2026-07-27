// Plain-node unit tests for the compact auto-fit height cap.
//
// It pins computeMaxHeight, the pure helper that prevents live search content
// from making the overlay taller than its final-output envelope. The renderer
// mirrors this formula from window.screen.availHeight (see src/App.jsx), so
// keeping the behavior nailed down keeps the two sides in agreement.
//
// No em dashes anywhere (house rule).

const assert = require('assert');
const { computeMaxHeight, FINAL_OUTPUT_MAX_HEIGHT } = require('./sizing');

let passed = 0;
function check(label, cond) {
  assert.strictEqual(cond, true, 'FAILED: ' + label);
  passed += 1;
  console.log('  ok  ' + label);
}

console.log('computeMaxHeight:');
// Normal laptop and desktop displays share the same compact envelope as the
// final output. Search content must scroll rather than expanding beyond it.
check('1040 work area -> final-output cap', computeMaxHeight(1040) === FINAL_OUTPUT_MAX_HEIGHT);
check('1080 -> final-output cap', computeMaxHeight(1080) === FINAL_OUTPUT_MAX_HEIGHT);
check('2160 -> final-output cap', computeMaxHeight(2160) === FINAL_OUTPUT_MAX_HEIGHT);

// A short laptop work area produces a smaller cap that still fits its screen.
check('720 work area still uses the 640 cap', computeMaxHeight(720) === FINAL_OUTPUT_MAX_HEIGHT);
check('600 work area -> 552', computeMaxHeight(600) === 552);

// Bad or missing reads fall back to the final-output cap.
check('0 -> final-output cap', computeMaxHeight(0) === FINAL_OUTPUT_MAX_HEIGHT);
check('negative -> final-output cap', computeMaxHeight(-5) === FINAL_OUTPUT_MAX_HEIGHT);
check('NaN -> final-output cap', computeMaxHeight(NaN) === FINAL_OUTPUT_MAX_HEIGHT);
check('null -> final-output cap', computeMaxHeight(null) === FINAL_OUTPUT_MAX_HEIGHT);
check('undefined -> final-output cap', computeMaxHeight(undefined) === FINAL_OUTPUT_MAX_HEIGHT);

console.log('\nAll ' + passed + ' assertions passed.');
