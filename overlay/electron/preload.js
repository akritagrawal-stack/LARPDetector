const { contextBridge, ipcRenderer } = require('electron');

// Glass mode is passed synchronously from main.js as a launch argument
// (webPreferences.additionalArguments), so the renderer knows it on the very
// first paint with no IPC round-trip. Falls back to 'css' if absent.
function readGlassMode() {
  const arg = process.argv.find((a) => a.startsWith('--glass-mode='));
  return arg ? arg.split('=')[1] : 'css';
}

// Minimal, explicit surface exposed to the renderer. No node access,
// no raw ipcRenderer, just the handful of calls the overlay UI needs.
contextBridge.exposeInMainWorld('overlay', {
  isElectron: true,
  platform: process.platform,
  // 'vibrancy' (macOS) | 'acrylic' (Windows 11) | 'css' (Windows 10 / forced).
  glassMode: readGlassMode(),

  onHotkeyScan(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('hotkey-scan', listener);
    return () => ipcRenderer.removeListener('hotkey-scan', listener);
  },

  // "Go" button, layers 1 and 2, called on demand (not just on the hotkey).
  // getActiveBrowserUrl resolves to a URL string, or null if no browser is
  // available, or the native platform read failed for any reason
  // (see main.js's getActiveBrowserUrl for the honest reliability caveats).
  // captureScreenshot resolves to a raw base64 PNG string, or null if the
  // screenshot itself could not be captured (e.g. macOS Screen Recording
  // permission not granted).
  getActiveBrowserUrl() {
    return ipcRenderer.invoke('get-active-browser-url');
  },

  openAutomationSettings() {
    return ipcRenderer.invoke('open-automation-settings');
  },

  requestAutomationPermission() {
    return ipcRenderer.invoke('request-automation-permission');
  },

  openBrowserCompanionSetup() {
    return ipcRenderer.invoke('open-browser-companion-setup');
  },

  captureScreenshot() {
    return ipcRenderer.invoke('capture-screenshot');
  },

  // Reads the system clipboard's plain text (for the Go button's clipboard
  // fallback layer: a user who just copied a profile link should never be
  // routed through vision). Resolves to a string (possibly empty); never
  // rejects (main.js swallows any failure to "").
  readClipboardText() {
    return ipcRenderer.invoke('read-clipboard-text');
  },

  getSetupStatus() {
    return ipcRenderer.invoke('get-setup-status');
  },

  startLinkedInLogin() {
    return ipcRenderer.invoke('start-linkedin-login');
  },

  openScreenRecordingSettings() {
    return ipcRenderer.invoke('open-screen-recording-settings');
  },

  onSetupStatus(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('setup-status', listener);
    return () => ipcRenderer.removeListener('setup-status', listener);
  },

  onHotkeyError(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('hotkey-error', listener);
    return () => ipcRenderer.removeListener('hotkey-error', listener);
  },

  // Reports the exact outer window height the renderer wants (measured
  // content height plus the panel's own CSS margin, computed per glass mode
  // on the renderer side). The window is a pure follower of this value now.
  reportSize(height) {
    ipcRenderer.send('panel-resize', height);
  },

  // Drag-to-resize from the in-app bottom-right grip. The renderer streams
  // pointer deltas (screen pixels) and main.js applies them to the window's
  // bounds, clamped to the min/max size, anchored at the current top-left. This
  // is the reliable resize path for the transparent frameless overlay, whose
  // native edge-grab is unreliable on Windows 11. The first such call also
  // hands size ownership to the user (same as a native resize).
  resizeWindowBy(dx, dy) {
    ipcRenderer.send('user-resize-by', { dx, dy });
  },

  // Fired when the user first manually resizes the window (and re-sent on a
  // reload): the renderer must stop auto-fitting height and instead fill the
  // user's chosen window size, scrolling its content internally.
  onUserResized(callback) {
    const listener = () => callback();
    ipcRenderer.on('user-resized', listener);
    return () => ipcRenderer.removeListener('user-resized', listener);
  },

  // The "Auto-resize" toggle: clears main's user-resized takeover so the content
  // auto-fit engine owns the window height again (grow AND shrink to fit the
  // scan). The renderer clears its own takeover flags and re-reports the fitted
  // height in the same handler, so this only resets main's side of the handoff.
  // Resolves once main has reset (a plain boolean); safe to fire-and-forget.
  reengageAutoResize() {
    return ipcRenderer.invoke('reengage-auto-resize');
  },

  // Screen-recording invisibility toggle (on by default, set in main.js).
  setContentProtection(enabled) {
    ipcRenderer.send('set-content-protection', !!enabled);
  },

  onProtectionState(callback) {
    const listener = (_event, enabled) => callback(enabled);
    ipcRenderer.on('protection-state', listener);
    return () => ipcRenderer.removeListener('protection-state', listener);
  },

  // Open a source page (a clickable evidence row) in the user's real browser.
  openExternal(url) {
    ipcRenderer.send('open-external', url);
  },

  // Copy a compact verdict summary to the system clipboard (copy-verdict).
  copyText(text) {
    ipcRenderer.send('copy-text', text);
  },

  // Reports the Python scan engine's lifecycle: main.js spawns it as a
  // hidden child process on launch, so the renderer needs a way to know
  // whether it is starting, ready, or failed (missing Python, missing
  // deps, crashed), without the user ever running a server themselves.
  onEngineStatus(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('engine-status', listener);
    return () => ipcRenderer.removeListener('engine-status', listener);
  },

  // Task 1: Windows "Transparency effects" guard. main.js checks the
  // registry once at launch and only ever sends this when it is confirmed
  // off, so the renderer only shows its dismissible banner on a real signal,
  // never a guess.
  onTransparencyStatus(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('transparency-status', listener);
    return () => ipcRenderer.removeListener('transparency-status', listener);
  },

  // The banner's "Enable" button: asks main.js to write the registry value
  // on. Never called except by that explicit click.
  enableTransparency() {
    ipcRenderer.send('enable-transparency');
  },

  // Task 2: the unfocused-acrylic mitigation. main.js forwards the OS
  // window's real blur/focus state so styles.css can compensate the glass
  // tint, rim, and sheen while the panel is backgrounded (the common state
  // for this always-on-top overlay).
  onWindowFocusState(callback) {
    const listener = (_event, focused) => callback(focused);
    ipcRenderer.on('window-focus-state', listener);
    return () => ipcRenderer.removeListener('window-focus-state', listener);
  },

  // Live glass preset cycler. main.js sends this whenever the owner cycles a
  // preset (Ctrl+Shift+G) or nudges the tint (Ctrl+Shift+Up/Down). The payload
  // is { id, label, dataGlass, tint }, where tint is either
  // { r, g, b, a } (an inline --glass-tint override) or null (remove the
  // override and hand control back to the shipped CSS). The renderer restamps
  // <html data-glass> and shows a brief on-screen toast of the preset name.
  onGlassPreset(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('glass-preset', listener);
    return () => ipcRenderer.removeListener('glass-preset', listener);
  }
});
