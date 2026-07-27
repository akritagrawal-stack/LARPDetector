// macOS active-browser URL capture. The overlay becomes the foreground app when
// its Go button is clicked, so reading only the frontmost process is wrong.
// Instead, inspect each supported running browser and prefer a LinkedIn profile
// URL over an unrelated active tab. The caller still validates the result.
//
// The generated JXA script runs through macOS's built-in osascript command. It
// may trigger an Automation permission prompt the first time it asks a browser
// for its active tab. Every browser-specific failure is isolated and returns
// nothing, so unsupported browsers safely fall through to screenshot capture.
//
// No em dashes anywhere in this file (house rule).

const MAC_ACTIVE_URL_TIMEOUT_MS = 4000;
const MAC_PERMISSION_PROMPT_TIMEOUT_MS = 60000;
const MAC_AUTOMATION_DENIED_SENTINEL = '__LARP_AUTOMATION_DENIED__';

const MAC_BROWSER_CANDIDATES = [
  { name: 'Safari', mode: 'safari' },
  { name: 'Google Chrome', mode: 'chromium' },
  { name: 'Brave Browser', mode: 'chromium' },
  { name: 'Microsoft Edge', mode: 'chromium' },
  { name: 'Arc', mode: 'chromium' },
  { name: 'Comet', mode: 'chromium' },
  { name: 'Chromium', mode: 'chromium' },
  { name: 'Vivaldi', mode: 'chromium' },
  { name: 'Opera', mode: 'chromium' },
];

function buildMacActiveUrlScript() {
  const candidates = JSON.stringify(MAC_BROWSER_CANDIDATES);
  const deniedSentinel = JSON.stringify(MAC_AUTOMATION_DENIED_SENTINEL);
  return `
let automationDenied = false;

function readUrl(spec) {
  try {
    const browser = Application(spec.name);
    if (!browser.running()) return '';
    const windows = browser.windows();
    if (!windows || windows.length === 0) return '';
    const tab = spec.mode === 'safari'
      ? windows[0].currentTab()
      : windows[0].activeTab();
    return String(tab.url() || '').trim();
  } catch (error) {
    // JXA often strips the underlying Apple Event error number and exposes
    // only "An error occurred." Once browser.running() was true, any failure
    // reading its windows/tabs means browser automation is unavailable for
    // this process, most commonly TCC error -1743.
    automationDenied = true;
    return '';
  }
}

function run() {
  const candidates = ${candidates};
  let fallback = '';
  for (const spec of candidates) {
    const url = readUrl(spec);
    if (!url) continue;
    if (/linkedin\\.com\\/in\\//i.test(url)) return url;
    if (!fallback) fallback = url;
  }
  if (fallback) return fallback;
  return automationDenied ? ${deniedSentinel} : '';
}
`.trim();
}

module.exports = {
  MAC_ACTIVE_URL_TIMEOUT_MS,
  MAC_PERMISSION_PROMPT_TIMEOUT_MS,
  MAC_AUTOMATION_DENIED_SENTINEL,
  MAC_BROWSER_CANDIDATES,
  buildMacActiveUrlScript,
};
