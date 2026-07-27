// Pure, Electron-free height math shared by the resize logic and its test.
//
// The auto-fit engine grows the overlay window to FIT the scan content, but it
// must never grow past either the visible screen or the designed final-output
// envelope. A scan is a compact overlay, not a second browser window. Search
// content that exceeds the final-output envelope scrolls inside its bounded
// regions instead of making the whole overlay taller than the result it will
// eventually show.
//
// This is kept as a pure function (workAreaHeight in, cap out) so a plain-node
// test can pin the behavior, and so the renderer can mirror the exact formula
// from window.screen.availHeight (see src/App.jsx). No em dashes (house rule).

const FINAL_OUTPUT_MAX_HEIGHT = 640;
const SCREEN_FRACTION = 0.92;

// Given the display work-area height (DIP), return the auto-fit height cap.
// A missing / non-finite / non-positive input falls back to the final-output
// envelope rather than collapsing the window.
function computeMaxHeight(workAreaHeight) {
  const h = Number(workAreaHeight);
  if (!Number.isFinite(h) || h <= 0) return FINAL_OUTPUT_MAX_HEIGHT;
  return Math.min(FINAL_OUTPUT_MAX_HEIGHT, Math.round(h * SCREEN_FRACTION));
}

module.exports = { computeMaxHeight, FINAL_OUTPUT_MAX_HEIGHT, SCREEN_FRACTION };
