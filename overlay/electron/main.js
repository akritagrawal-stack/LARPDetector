const {
  app,
  BrowserWindow,
  globalShortcut,
  desktopCapturer,
  screen,
  clipboard,
  shell,
  ipcMain,
  Menu,
  systemPreferences,
} = require('electron');
const path = require('path');
const os = require('os');
const fs = require('fs');
const http = require('http');
const { spawn, spawnSync } = require('child_process');
const {
  buildActiveUrlScript,
  ACTIVE_URL_TIMEOUT_MS,
  normalizeCapturedUrl,
  looksLikeTargetUrl,
  looksLikeLinkedInProfileUrl,
} = require('./activeUrl');
const {
  buildMacActiveUrlScript,
  MAC_ACTIVE_URL_TIMEOUT_MS,
  MAC_PERMISSION_PROMPT_TIMEOUT_MS,
  MAC_AUTOMATION_DENIED_SENTINEL,
} = require('./macActiveUrl');
const { computeMaxHeight } = require('./sizing');
const { summonShortcutForPlatform } = require('./shortcuts');
const {
  COMPANION_HISTORY_URL_AGE_SECONDS,
  getRecentMacBrowserHistoryUrl
} = require('./macBrowserHistory');
const { isBrowserCompanionInstalled } = require('./browserCompanion');

// Warm-start hint: the HWND (decimal string) of the browser window that won the
// last active-URL read. Passed as a HINT to the next call so a stable browser
// window is tried first, before even the foreground fast path. Correctness
// never depends on it (the Z-order enumeration runs regardless); it is a pure
// latency optimization, refreshed from every successful read.
let lastBrowserForegroundHwnd = null;

// Default (initial) panel width. The window is user-resizable now, so this
// only seeds the first launch; MIN/MAX_WIDTH bound what the user can drag.
const PANEL_WIDTH = 460;
const MARGIN = 16;
const MIN_WIDTH = 380;
const MAX_WIDTH = 860;

// RESIZE OWNERSHIP: content owns the height INITIALLY (the auto-fit engine
// below), and the user can take over at any time by grabbing a window edge.
// The first real manual resize flips userResized, which permanently (for the
// session) hands the size to the user on both sides: main ignores further
// panel-resize reports, and the renderer is told (user-resized IPC) to fill
// the chosen window size and scroll its content internally instead. This is
// the honest reconciliation: two owners fighting over one height is worse
// than a clean handoff, so an explicit user size always wins once expressed.
// MIN_HEIGHT is the window's initial height before the first content report
// and the auto-fit low clamp (kept in sync with src/App.jsx). The auto-fit
// HIGH clamp is the smaller of the final-output envelope and the available
// screen height (see computeMaxHeight in ./sizing.js and currentMaxHeight
// below). Overflow past that cap scrolls internally while content owns the
// height. The renderer mirrors the same formula from
// window.screen.availHeight (src/App.jsx), so the two sides agree closely and
// the renderer never asks for more height than main will grant.
const MIN_HEIGHT = 64;

// The live auto-fit height cap, computed against whichever display the window
// currently sits on (consistent with boundsForHeight's nearest-display anchor).
// Read per resize rather than cached so moving the panel to a shorter display
// re-clamps correctly.
function currentMaxHeight() {
  const nearest = screen.getDisplayNearestPoint({ x: winX, y: winY });
  return computeMaxHeight(nearest.workArea.height);
}

const IS_MAC = process.platform === 'darwin';

// ---------- Windows "Transparency effects" guard ----------
// Windows acrylic only actually renders as a live blur when the user's
// system-wide "Transparency effects" setting is on (Settings > Personalization
// > Colors). When it is off, Windows silently falls back to a flat, opaque
// panel for every app that requests acrylic, ours included, with no error and
// nothing for Electron to react to. The registry value below is the only
// reliable signal, read once at launch, never written without the user
// clicking "Enable" in the in-app prompt (see checkWindowsTransparencyEnabled
// and enableWindowsTransparency further down).
const TRANSPARENCY_REG_KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize';
const TRANSPARENCY_REG_VALUE = 'EnableTransparency';

const isDev = !app.isPackaged && process.env.VITE_DEV_SERVER === '1';
const DEV_SERVER_URL = 'http://localhost:5173';

let win = null;

// The window's intended top-left, in screen coordinates. This is the anchor
// the panel grows DOWN from. It is set once from the primary display's WORK
// area (so the panel lands below the macOS menu bar and clear of the Windows
// taskbar), then kept in sync with wherever the user drags the panel (the
// `moved` listener below). The old build computed this once and never
// updated it, which is what teleported the window back to the corner on any
// content-height change. It is updated on every real user move now.
let winX = MARGIN;
let winY = MARGIN;

// The window's current width. Seeded from PANEL_WIDTH, updated whenever the
// user resizes, so a content-driven setBounds can never snap the width back.
let winW = PANEL_WIDTH;

// Ignore the `move`/`moved` events that our OWN setBounds calls emit (an
// off-screen-guard shift changes the displayed Y, and we must not mistake
// that for the user relocating the panel). A short time window is enough:
// content-driven resizes never overlap an active user drag.
let suppressMoveUntil = 0;

// Same trick for resize: our OWN setBounds calls emit resize events too, and
// those must never be mistaken for the user grabbing a window edge.
let suppressResizeUntil = 0;

// Flipped true on the first REAL user resize, never back (this session).
// While true, the content auto-fit is off: panel-resize reports are ignored
// and the renderer fills the user's chosen size (see onUserResize below).
let userResized = false;

// Screen recording invisibility (Cluely's signature trick): when on, the
// overlay is excluded from screenshots and screen-share/recording capture
// on both platforms. Defaults to on. Toggled from the renderer via IPC.
let contentProtectionEnabled = true;

// ---------- Live glass preset cycler ----------
// A dial-in tool for the "glass look": the composited backdrop (DWM acrylic,
// the raw blur-behind API, or the CSS faux-glass) can only be judged by a live
// human eye on a real screen, never from a screenshot (the OS material is not
// captured). So the owner cycles these presets on the running window with a
// global shortcut (Ctrl+Shift+G) and picks a winner live. Each preset holds
// EVERY parameter needed to reproduce its look, in this one array, so BAKING a
// winner is a single-line edit (see DEFAULT_PRESET_ID below).
//
// Per-preset fields:
//   id           stable key, also what DEFAULT_PRESET_ID selects.
//   label        short name shown in the on-screen toast when cycled.
//   material     what win.setBackgroundMaterial() is set to: 'acrylic' (the
//                DWM material) or 'none' (so a different backdrop owns it).
//   composition  null, or { alpha } to apply the raw Win32
//                SetWindowCompositionAttribute blur-behind (focus-INDEPENDENT,
//                the whole point for an always-unfocused overlay). alpha is the
//                gradient tint's alpha byte (higher = darker/more tinted).
//   dataGlass    which CSS token set the renderer stamps on <html data-glass>:
//                'acrylic' (light CSS tint, the OS material carries the blur)
//                or 'css' (heavy backdrop-filter + strong specular, self-owned).
//   tintOverride whether to push an inline --glass-tint override to the CSS. The
//                baseline is false so the shipped CSS (incl. the unfocused-acrylic
//                compensation in styles.css) governs it untouched; blur-behind /
//                css presets are true because a fixed inline tint is exactly what
//                a focus-independent backdrop wants.
//   cssTintAlpha the alpha for that inline override; also the SEED value the
//                Ctrl+Shift+Up/Down nudge starts from even when tintOverride is
//                false (so nudging the baseline begins from its real tint).
//   tintRGB      the base color the inline --glass-tint override is built from.
const PRESETS = [
  {
    // 1. Baseline: the current shipped behavior, kept so the owner can A/B
    // against it. DWM acrylic material + the acrylic CSS tokens + the shipped
    // focus-compensation. tintOverride:false means cycling BACK to this preset
    // restores pure CSS control (the renderer removes any inline tint), so the
    // baseline the owner compares against is the exact look that ships.
    id: 'dwm-acrylic',
    label: 'DWM acrylic (baseline)',
    material: 'acrylic',
    composition: null,
    dataGlass: 'acrylic',
    tintOverride: false,
    cssTintAlpha: 0.22, // matches the acrylic token; a seed for nudging only
    tintRGB: [15, 16, 21]
  },
  {
    // 2. Raw blur-behind, MEDIUM tint. DWM material off, the focus-independent
    // ACCENT_ENABLE_ACRYLICBLURBEHIND on. Light CSS tint since the OS blur
    // carries the look.
    id: 'blur-behind',
    label: 'Blur-behind (medium)',
    material: 'none',
    composition: { alpha: 0x99 },
    dataGlass: 'acrylic',
    tintOverride: true,
    cssTintAlpha: 0.14,
    tintRGB: [15, 16, 21]
  },
  {
    // 3. Same raw blur-behind, LIGHTER tint alpha for a clearer, shinier glass.
    id: 'blur-behind-light',
    label: 'Blur-behind (light)',
    material: 'none',
    composition: { alpha: 0x66 },
    dataGlass: 'acrylic',
    tintOverride: true,
    cssTintAlpha: 0.10,
    tintRGB: [15, 16, 21]
  },
  {
    // 4. Self-contained CSS faux-glass: heavy backdrop-filter + strong specular
    // highlights (styles.css data-glass="css"), the reliable fallback that looks
    // good regardless of OS compositor state. No OS material at all.
    id: 'css-max-shine',
    label: 'CSS max shine',
    material: 'none',
    composition: null,
    dataGlass: 'css',
    tintOverride: true,
    cssTintAlpha: 0.80,
    tintRGB: [16, 17, 22]
  },
  {
    // 5. Cluely-exact: a fully TRANSPARENT window with NO OS backdrop at all
    // (material none, composition null), so the glass is drawn entirely in CSS
    // (a 16px-radius dark translucent panel with its own soft shadow inside a
    // transparent margin). This is what kills the "outside square": there is no
    // full-rect OS blur/tint to leak past the rounded CSS corners, only the
    // rounded panel and its shadow paint anything. Verified (see
    // CLUELY_MATCH_PLAN.md) as exactly how Cluely itself renders on Windows: no
    // true desktop blur, tint carries the glass. This is the shipped default.
    id: 'cluely',
    label: 'Cluely (CSS glass, rounded)',
    material: 'none',
    composition: null,
    dataGlass: 'cluely',
    tintOverride: false,
    cssTintAlpha: 0.68,
    tintRGB: [16, 18, 24]
  }
];

// ---- BAKE POINT ----
// To ship a preset as the permanent default, change ONLY this one line to the
// winner's id (e.g. 'blur-behind'). It drives both the OS-side backdrop applied
// at startup AND the window-creation shape + the renderer's first-paint
// data-glass, so the next launch comes up already looking like the chosen
// preset with no cycling. (See the createWindow acrylic branch, which reads the
// default preset's `material` to decide transparent-vs-opaque.)
const DEFAULT_PRESET_ID = 'cluely';

function presetById(id) {
  return PRESETS.find((p) => p.id === id) || PRESETS[0];
}

// Live cycler state. activePresetId gates the OS-acrylic-specific behaviors
// (height staging, focus re-apply) so they run only while the DWM material is
// actually the active backdrop, never for a blur-behind or CSS preset.
let activePresetId = DEFAULT_PRESET_ID;
let currentPresetIndex = Math.max(0, PRESETS.findIndex((p) => p.id === DEFAULT_PRESET_ID));
let currentTintAlpha = presetById(DEFAULT_PRESET_ID).cssTintAlpha;
let tintOverrideActive = presetById(DEFAULT_PRESET_ID).tintOverride;

// The OS glass CAPABILITY of this machine (independent of which preset is
// active): 'vibrancy' on macOS, 'win11' where DWM acrylic / blur-behind are
// available (Windows 11 build >= 22621), else 'css'. LARP_GLASS=css forces the
// pure-CSS path everywhere.
function computeGlassCapability() {
  if ((process.env.LARP_GLASS || '').toLowerCase() === 'css') return 'css';
  if (IS_MAC) return 'vibrancy';
  if (process.platform === 'win32') {
    const build = parseInt(os.release().split('.')[2] || '0', 10);
    if (build >= 22621) return 'win11';
  }
  return 'css';
}

const glassCapability = computeGlassCapability();

// The glass mode that drives window creation and the renderer's FIRST-PAINT
// <html data-glass> (passed synchronously via additionalArguments). On a
// win11-capable machine this is the DEFAULT preset's dataGlass, so baking a new
// default (one-line DEFAULT_PRESET_ID change) also flips the initial look with
// no IPC round-trip. macOS is always vibrancy; everything else is css.
function computeGlassMode() {
  if (glassCapability === 'vibrancy') return 'vibrancy';
  if (glassCapability === 'win11') return presetById(DEFAULT_PRESET_ID).dataGlass;
  return 'css';
}

const glassMode = computeGlassMode();

// True only while the real DWM acrylic material is the active backdrop: the
// machine must be win11-capable AND the active preset must use the 'acrylic'
// material. This is the correct gate for the acrylic-only behaviors below
// (height staging, blur/focus re-apply, the startup paint nudge), replacing the
// old `glassMode === 'acrylic'` checks, which no longer capture "is the DWM
// material actually on" once a blur-behind preset can be active.
function osAcrylicActive() {
  return glassCapability === 'win11' && presetById(activePresetId).material === 'acrylic';
}

// Returns true (on), false (confirmed off), or null (unknown: not Windows,
// the key/value does not exist yet, or `reg query` failed for any reason).
// Only a confirmed `false` ever triggers the in-app nudge below: a missing
// key or a parse miss both read as "leave it alone, say nothing", never as
// "off", so a quirky environment can never nag a user we have no real signal
// about.
function checkWindowsTransparencyEnabled() {
  if (process.platform !== 'win32') return null;
  try {
    const result = spawnSync(
      'reg',
      ['query', TRANSPARENCY_REG_KEY, '/v', TRANSPARENCY_REG_VALUE],
      { windowsHide: true, encoding: 'utf8' }
    );
    if (!result || result.status !== 0 || !result.stdout) return null;
    const match = result.stdout.match(/EnableTransparency\s+REG_DWORD\s+0x([0-9a-fA-F]+)/i);
    if (!match) return null;
    return parseInt(match[1], 16) !== 0;
  } catch {
    return null;
  }
}

// Writes the value on. Spawned async, only ever in response to the user
// clicking "Enable" in the in-app prompt (see the enable-transparency IPC
// handler below), never automatically. Resolves true only on a clean exit
// code from `reg add`. HKCU needs no elevation, so this is a normal,
// non-privileged write.
function enableWindowsTransparency() {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(
        'reg',
        ['add', TRANSPARENCY_REG_KEY, '/v', TRANSPARENCY_REG_VALUE, '/t', 'REG_DWORD', '/d', '1', '/f'],
        { windowsHide: true }
      );
    } catch {
      resolve(false);
      return;
    }
    child.once('error', () => resolve(false));
    child.once('exit', (code) => resolve(code === 0));
  });
}

// Set once at startup (see app.whenReady below) and pushed to the renderer on
// did-finish-load. Only ever { enabled: false }, or null when there is
// nothing to report (on, or unknown), so the renderer only ever shows the
// banner on a confirmed off, never on a guess.
let transparencyState = null;

// ---------- Scan engine (Python service) lifecycle ----------
// The overlay is meant to be one self-contained app: the user launches it
// and never starts a server or opens a localhost page themselves. main.js
// spawns the Python scan engine (service_run.py, a plain uvicorn/FastAPI
// app living one level up from overlay/, at the LARPDetector repo root) as
// a hidden child process, waits for it to answer its /health endpoint, and
// kills it again when the app quits. Nothing here edits engine files, it
// only launches the existing, unmodified entry point.
const ENGINE_ROOT = path.join(__dirname, '..', '..');
const ENGINE_ENTRY = 'service_run.py';
const LINKEDIN_LOGIN_ENTRY = path.join(ENGINE_ROOT, 'scripts', 'login_linkedin_macos.py');
const ENGINE_HOST = '127.0.0.1';
const ENGINE_PORT = 8756;
const MAC_URL_HELPER_APP = path.join(ENGINE_ROOT, '.runtime', 'LARP URL Helper.app');
const BROWSER_EXTENSION_DIR = path.join(ENGINE_ROOT, 'browser-extension');
const DEFAULT_LINKEDIN_PROFILE_DIR = path.join(
  os.homedir(),
  'Library',
  'Application Support',
  'LARP Detector',
  'linkedin-profile'
);
const DEFAULT_CODEX_CLI = '/Applications/ChatGPT.app/Contents/Resources/codex';
const PROJECT_GITHUB_CLI = path.join(ENGINE_ROOT, '.runtime', 'github-cli', 'gh');
const ENGINE_READY_TIMEOUT_MS = 20000;
const ENGINE_POLL_INTERVAL_MS = 400;

// Windows almost never has `python3` on PATH, only `python`; macOS/Linux is
// the reverse (bare `python` is frequently missing or points at Python 2 on
// older systems). Try the platform's usual name first, then the other, so
// one missing alias does not sink the whole thing.
const LOCAL_VENV_PYTHON = process.platform === 'win32'
  ? path.join(ENGINE_ROOT, '.venv', 'Scripts', 'python.exe')
  : path.join(ENGINE_ROOT, '.venv', 'bin', 'python');
const PYTHON_CANDIDATES = IS_MAC || process.platform === 'linux'
  ? [LOCAL_VENV_PYTHON, 'python3', 'python']
  : [LOCAL_VENV_PYTHON, 'python', 'py'];

let engineProcess = null;
let engineStatusState = { state: 'starting', message: 'Starting the scan engine...' };
let appQuitting = false;
let linkedinLoginProcess = null;
let summonShortcutRegistered = false;
let macAutomationStatus = 'unknown';
let githubAuthCache = { checkedAt: 0, authenticated: false };

function sendEngineStatus(status) {
  engineStatusState = status;
  if (win) win.webContents.send('engine-status', status);
}

function checkEngineHealth() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: ENGINE_HOST, port: ENGINE_PORT, path: '/health', timeout: 1500 },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          if (body.length < 8192) body += chunk;
        });
        res.on('end', () => {
          if (res.statusCode !== 200) {
            resolve(false);
            return;
          }
          try {
            const health = JSON.parse(body);
            resolve(
              health.status === 'ok' &&
              path.resolve(health.project_root || '') === path.resolve(ENGINE_ROOT)
            );
          } catch {
            resolve(false);
          }
        });
      }
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForEngineReady(maxWaitMs) {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (await checkEngineHealth()) return true;
    if (Date.now() - start >= maxWaitMs) return false;
    await new Promise((resolve) => setTimeout(resolve, ENGINE_POLL_INTERVAL_MS));
  }
}

// Tries each interpreter name in order and resolves with the first child
// process that actually starts (no immediate spawn error, e.g. ENOENT for
// "not on PATH"). Resolves null if every candidate fails to start.
function trySpawnPython(candidates) {
  return new Promise((resolve) => {
    if (candidates.length === 0) {
      resolve(null);
      return;
    }
    const [cmd, ...rest] = candidates;
    const childEnv = { ...process.env };
    if (!childEnv.LARP_LINKEDIN_PROFILE_DIR && IS_MAC) {
      childEnv.LARP_LINKEDIN_PROFILE_DIR = DEFAULT_LINKEDIN_PROFILE_DIR;
    }
    if (!childEnv.LARP_CODEX_CLI && fs.existsSync(DEFAULT_CODEX_CLI)) {
      childEnv.LARP_CODEX_CLI = DEFAULT_CODEX_CLI;
    }
    if (!childEnv.LARP_SERVICE_PROVIDER && childEnv.LARP_CODEX_CLI) {
      childEnv.LARP_SERVICE_PROVIDER = 'codex';
    }
    let settled = false;
    let child;
    try {
      child = spawn(cmd, [ENGINE_ENTRY], {
        cwd: ENGINE_ROOT,
        env: childEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true
      });
    } catch {
      resolve(trySpawnPython(rest));
      return;
    }

    child.once('error', () => {
      if (settled) return;
      settled = true;
      resolve(trySpawnPython(rest));
    });

    setImmediate(() => {
      if (!settled) {
        settled = true;
        resolve(child);
      }
    });
  });
}

async function spawnEngine() {
  sendEngineStatus({ state: 'starting', message: 'Starting the scan engine...' });

  // If an engine is ALREADY listening on the port (started manually, or a prior
  // overlay instance, or an operator session), adopt it instead of spawning a
  // duplicate. A second uvicorn cannot bind 8756, so spawning one only produces
  // an immediate exit-code-1 death and the "scan engine stopped unexpectedly"
  // banner, which is exactly the red error seen on New scan. Reuse, don't fight.
  if (await checkEngineHealth()) {
    sendEngineStatus({ state: 'ready', message: 'Scan engine ready.' });
    return;
  }

  const child = await trySpawnPython(PYTHON_CANDIDATES);
  if (!child) {
    sendEngineStatus({
      state: 'error',
      message:
        'Python was not found on PATH. Install Python 3.10+ (from python.org, or ' +
        '`brew install python` on macOS) so `python` or `python3` runs from a terminal, ' +
        'then restart the app.'
    });
    return;
  }

  engineProcess = child;
  let stderrTail = '';
  child.stdout.on('data', (chunk) => process.stdout.write('[engine] ' + chunk));
  child.stderr.on('data', (chunk) => {
    const text = chunk.toString();
    stderrTail = (stderrTail + text).slice(-2000);
    process.stderr.write('[engine] ' + text);
  });

  child.once('exit', (code) => {
    if (engineProcess === child) engineProcess = null;
    if (appQuitting) return;
    sendEngineStatus({
      state: 'error',
      message:
        'The scan engine stopped unexpectedly' + (code != null ? ' (exit code ' + code + ')' : '') + '. ' +
        (stderrTail.trim()
          ? 'Last output: ' + stderrTail.trim().slice(-300)
          : 'Make sure dependencies are installed: pip install -r requirements.txt in ' + ENGINE_ROOT + '.')
    });
  });

  const ready = await waitForEngineReady(ENGINE_READY_TIMEOUT_MS);
  if (ready) {
    sendEngineStatus({ state: 'ready', message: 'Scan engine ready.' });
  } else {
    sendEngineStatus({
      state: 'error',
      message:
        'The scan engine did not become ready in time. Make sure dependencies are ' +
        'installed (pip install -r requirements.txt in ' + ENGINE_ROOT + '), then restart the app.'
    });
  }
}

function stopEngine() {
  appQuitting = true;
  if (!engineProcess) return;
  const pid = engineProcess.pid;
  try {
    if (process.platform === 'win32') {
      // Synchronous on purpose: app quit is a one-time, short-lived event,
      // and an async spawn() here was observed to lose the race against
      // Electron's own process teardown, leaving the engine orphaned
      // (Electron can finish exiting before an async child process gets
      // a turn to run). spawnSync blocks the few milliseconds it takes
      // taskkill to run, which guarantees the kill completes first. This
      // targets the exact PID we spawned (/pid), never an image name, and
      // /T stops that one process's tree only.
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F']);
    } else {
      engineProcess.kill('SIGTERM');
    }
  } catch {
    // Best-effort cleanup during shutdown, nothing else to do if it fails.
  }
  engineProcess = null;
}

function sqliteHasLinkedInSession(cookiePath) {
  return new Promise((resolve) => {
    if (!fs.existsSync(cookiePath)) {
      resolve(false);
      return;
    }
    let stdout = '';
    let child;
    try {
      child = spawn(
        '/usr/bin/sqlite3',
        [
          '-readonly',
          cookiePath,
          "select 1 from cookies where name='li_at' and host_key like '%linkedin.com' limit 1;"
        ],
        { stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true }
      );
    } catch {
      resolve(false);
      return;
    }
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // Best-effort only.
      }
      resolve(false);
    }, 1200);
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    child.once('error', () => {
      clearTimeout(timer);
      resolve(false);
    });
    child.once('exit', () => {
      clearTimeout(timer);
      resolve(stdout.trim() === '1');
    });
  });
}

async function hasLinkedInSession() {
  const cookiePaths = [
    path.join(DEFAULT_LINKEDIN_PROFILE_DIR, 'Default', 'Network', 'Cookies'),
    path.join(DEFAULT_LINKEDIN_PROFILE_DIR, 'Default', 'Cookies')
  ];
  for (const cookiePath of cookiePaths) {
    if (await sqliteHasLinkedInSession(cookiePath)) return true;
  }
  return false;
}

function hasGitHubAuth() {
  const now = Date.now();
  if (now - githubAuthCache.checkedAt < 30000) {
    return githubAuthCache.authenticated;
  }
  const cli = fs.existsSync(PROJECT_GITHUB_CLI) ? PROJECT_GITHUB_CLI : 'gh';
  let authenticated = false;
  try {
    const result = spawnSync(
      cli,
      ['auth', 'status', '--hostname', 'github.com'],
      {
        env: { ...process.env, GH_PROMPT_DISABLED: '1' },
        timeout: 1500,
        windowsHide: true
      }
    );
    authenticated = result.status === 0;
  } catch {
    authenticated = false;
  }
  githubAuthCache = { checkedAt: now, authenticated };
  return authenticated;
}

async function getSetupStatus() {
  let screenRecording = 'not-applicable';
  if (IS_MAC) {
    try {
      screenRecording = systemPreferences.getMediaAccessStatus('screen');
    } catch {
      screenRecording = 'unknown';
    }
  }
  const browserCompanionConnected = await getBrowserCompanionPresence();
  const browserCompanionInstalled =
    browserCompanionConnected
    || (IS_MAC && isBrowserCompanionInstalled(os.homedir(), BROWSER_EXTENSION_DIR));
  return {
    platform: process.platform,
    linkedin_authenticated: await hasLinkedInSession(),
    linkedin_login_running: !!(linkedinLoginProcess && linkedinLoginProcess.exitCode == null),
    codex_ready: fs.existsSync(DEFAULT_CODEX_CLI),
    github_authenticated: hasGitHubAuth(),
    screen_recording: screenRecording,
    screen_recording_optional: true,
    browser_companion_connected: browserCompanionConnected,
    browser_companion_installed: browserCompanionInstalled,
    automation: IS_MAC ? macAutomationStatus : 'not-applicable',
    shortcut: summonShortcutForPlatform(process.platform),
    shortcut_ready: summonShortcutRegistered
  };
}

function getBrowserCompanionPresence() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: ENGINE_HOST, port: ENGINE_PORT, path: '/browser-companion', timeout: 700 },
      (res) => {
        let body = '';
        res.on('data', (chunk) => {
          body += chunk.toString();
        });
        res.on('end', () => {
          try {
            resolve(!!JSON.parse(body).connected);
          } catch {
            resolve(false);
          }
        });
      }
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  }).then((connected) => {
    // Backwards compatibility for an older loaded extension: publishing an
    // active LinkedIn profile still proves the companion is present.
    if (connected) return true;
    return getBrowserCompanionTab().then((url) => !!url);
  });
}

function getBrowserCompanionTab() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: ENGINE_HOST, port: ENGINE_PORT, path: '/browser-tab', timeout: 700 },
      (res) => {
        let body = '';
        res.on('data', (chunk) => {
          body += chunk.toString();
        });
        res.on('end', () => {
          try {
            const data = JSON.parse(body);
            resolve(data.connected && looksLikeLinkedInProfileUrl(data.url) ? data.url : null);
          } catch {
            resolve(null);
          }
        });
      }
    );
    req.on('error', () => resolve(null));
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
  });
}

function startLinkedInLogin() {
  if (!IS_MAC || !fs.existsSync(LINKEDIN_LOGIN_ENTRY)) return false;
  if (linkedinLoginProcess && linkedinLoginProcess.exitCode == null) return true;
  const python = fs.existsSync(LOCAL_VENV_PYTHON) ? LOCAL_VENV_PYTHON : 'python3';
  try {
    linkedinLoginProcess = spawn(python, [LINKEDIN_LOGIN_ENTRY], {
      cwd: ENGINE_ROOT,
      env: {
        ...process.env,
        LARP_LINKEDIN_PROFILE_DIR: DEFAULT_LINKEDIN_PROFILE_DIR
      },
      stdio: 'ignore',
      windowsHide: true
    });
  } catch {
    linkedinLoginProcess = null;
    return false;
  }
  linkedinLoginProcess.once('error', () => {
    linkedinLoginProcess = null;
  });
  linkedinLoginProcess.once('exit', () => {
    linkedinLoginProcess = null;
    if (win && !win.isDestroyed()) {
      getSetupStatus().then((status) => win.webContents.send('setup-status', status));
    }
  });
  return true;
}

// Where the panel currently lives, clamped so a tall panel never grows off
// the bottom of the work area. The user-chosen top-left (winX/winY) is the
// anchor; only the DISPLAYED y is shifted up when needed, and it snaps back
// to winY the moment the panel shrinks enough to fit again.
function boundsForHeight(height) {
  const nearest = screen.getDisplayNearestPoint({ x: winX, y: winY });
  const workArea = nearest.workArea;
  let y = winY;
  const bottom = workArea.y + workArea.height;
  if (y + height > bottom) {
    y = Math.max(workArea.y, bottom - height);
  }
  return { x: winX, y, width: winW, height };
}

function applyWindowHeight(target) {
  if (!win) return;
  suppressMoveUntil = Date.now() + 300;
  suppressResizeUntil = Date.now() + 300;
  if (osAcrylicActive()) {
    // Acrylic-mode caveat: the window rect itself is the visible material,
    // so a single instant grow can read as a one-frame "material pop". Split
    // the change across ~3 frames (60 / 90 / 100%), which reads as smooth at
    // these small deltas. Every other mode grows in a single step because
    // the newly exposed region is transparent (nothing to pop).
    const [, currentHeight] = win.getSize();
    const fracs = [0.6, 0.9, 1.0];
    fracs.forEach((f, i) => {
      const h = Math.round(currentHeight + (target - currentHeight) * f);
      setTimeout(() => {
        if (win) win.setBounds(boundsForHeight(h));
      }, i * 16);
    });
  } else {
    win.setBounds(boundsForHeight(target));
  }
}

// A one-time tiny resize, the same trick used on first show below, giving a
// live acrylic material a chance to notice a material or setting change
// without the user having to relaunch the app. Cheap and harmless to call
// speculatively; a no-op in every other glass mode or once the window is gone.
function nudgeAcrylicMaterial() {
  if (!win || !osAcrylicActive()) return;
  nudgeWindowRepaint();
}

// The bare 1px-grow-and-shrink primitive: forces the frameless window to
// repaint so a freshly (re)attached DWM material shows without a manual resize.
// Ungated on purpose (callers decide when it applies), so the preset cycler can
// use it directly even when the module-level default is a css/blur mode.
function nudgeWindowRepaint() {
  if (!win) return;
  suppressResizeUntil = Date.now() + 300;
  const [w, h] = win.getSize();
  win.setBounds({ x: winX, y: winY, width: w, height: h + 1 });
  setTimeout(() => {
    if (win) win.setBounds({ x: winX, y: winY, width: w, height: h });
  }, 16);
}

// ---------- Raw Win32 blur-behind (SetWindowCompositionAttribute) ----------
// The older, focus-INDEPENDENT acrylic: unlike backgroundMaterial:'acrylic'
// (which DWM dims when the window is unfocused, the common state for this
// overlay), ACCENT_ENABLE_ACRYLICBLURBEHIND stays lit while backgrounded. This
// is the raw OS API, applied by P/Invoking user32.dll from a short PowerShell
// helper (same spawn shape as getActiveBrowserUrl below). It degrades to a safe
// no-op on any failure (non-Windows, missing API, bad HWND): it never throws,
// logs a single line at most, and never blocks the app.
const BLUR_BEHIND_TIMEOUT_MS = 3000;

// Builds the P/Invoke script with the HWND, accent state, and tint bytes
// interpolated as literals (matching the existing ACTIVE_URL_PS_SCRIPT pattern),
// so nothing has to be threaded through $args under -Command. accentState is 4
// (ACCENT_ENABLE_ACRYLICBLURBEHIND) to turn the blur ON, or 0 (ACCENT_DISABLED)
// to CLEAR a previously applied policy so it does not persist under the next
// preset (OS accent state survives independently of setBackgroundMaterial).
function buildAccentPolicyScript(hwndDec, accentState, alpha, r, g, b) {
  return `
$ErrorActionPreference = 'Stop'
try {
  Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class LarpDetectorAcrylic {
  [StructLayout(LayoutKind.Sequential)]
  public struct AccentPolicy {
    public int AccentState;
    public int AccentFlags;
    public int GradientColor;
    public int AnimationId;
  }
  [StructLayout(LayoutKind.Sequential)]
  public struct WindowCompositionAttribData {
    public int Attribute;
    public IntPtr Data;
    public int SizeOfData;
  }
  [DllImport("user32.dll")]
  public static extern int SetWindowCompositionAttribute(IntPtr hwnd, ref WindowCompositionAttribData data);
}
"@

  # HWND: read as an unsigned 64-bit value on the JS side, so parse it the same
  # way here (uint64 -> int64 -> IntPtr) to stay correct on x64.
  $hwnd = [IntPtr]([int64]([uint64]${hwndDec}))
  if ($hwnd -eq [IntPtr]::Zero) { exit 0 }

  # GradientColor is 0xAABBGGRR. Build it in uint32 space (a high alpha byte
  # overflows a signed int), then reinterpret the exact bits as int32 for the
  # struct's int GradientColor field.
  $u = ([uint32]${alpha} -shl 24) -bor ([uint32]${b} -shl 16) -bor ([uint32]${g} -shl 8) -bor [uint32]${r}
  $gradient = [System.BitConverter]::ToInt32([System.BitConverter]::GetBytes($u), 0)

  $accent = New-Object LarpDetectorAcrylic+AccentPolicy
  $accent.AccentState = ${accentState}   # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND (focus-independent); 0 = ACCENT_DISABLED (clear)
  $accent.AccentFlags = 0   # 0 = no border; 2 would draw borders
  $accent.GradientColor = $gradient
  $accent.AnimationId = 0

  # SizeOfData is the marshalled size of the AccentPolicy struct. It is declared
  # int per the classic signature; on x64 the native field is SIZE_T, but the
  # CLR zero-inits the following padding so the high dword reads 0. If the blur
  # ever fails to appear on x64, this narrowing is the first thing to suspect.
  $size = [System.Runtime.InteropServices.Marshal]::SizeOf($accent)
  $ptr = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($size)
  try {
    [System.Runtime.InteropServices.Marshal]::StructureToPtr($accent, $ptr, $false)
    $data = New-Object LarpDetectorAcrylic+WindowCompositionAttribData
    $data.Attribute = 19   # WCA_ACCENT_POLICY
    $data.Data = $ptr
    $data.SizeOfData = $size
    [void][LarpDetectorAcrylic]::SetWindowCompositionAttribute($hwnd, [ref]$data)
    Write-Output 'LARP_ACRYLIC_OK'
  } finally {
    [System.Runtime.InteropServices.Marshal]::FreeHGlobal($ptr)
  }
} catch {
  exit 0
}
`;
}

// Sets the window's accent policy: accentState 4 (with a gradient alpha byte)
// turns ACCENT_ENABLE_ACRYLICBLURBEHIND on; accentState 0 CLEARS any prior
// policy (alphaByte ignored, sent as 0). Resolves true only on the helper's
// explicit OK token, false on every other path. Never rejects, never throws.
function applyAccentPolicy(accentState, alphaByte) {
  return new Promise((resolve) => {
    if (process.platform !== 'win32' || !win) {
      resolve(false);
      return;
    }

    let hwndDec;
    try {
      const buf = win.getNativeWindowHandle();
      // x64: an 8-byte little-endian HWND. Fall back to 4 bytes on a 32-bit
      // build. Read UNSIGNED so the decimal string handed to PowerShell is
      // never a spurious negative.
      hwndDec = buf.length >= 8 ? buf.readBigUInt64LE(0).toString() : String(buf.readUInt32LE(0));
    } catch {
      resolve(false);
      return;
    }

    const script = buildAccentPolicyScript(hwndDec, accentState, alphaByte, 0x14, 0x18, 0x20);

    let child;
    try {
      child = spawn(
        'powershell',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
        { windowsHide: true }
      );
    } catch {
      resolve(false);
      return;
    }

    let settled = false;
    let stdout = '';
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // best-effort only
      }
      finish(false);
    }, BLUR_BEHIND_TIMEOUT_MS);

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    child.on('error', () => finish(false));
    child.on('exit', () => finish(stdout.includes('LARP_ACRYLIC_OK')));
  });
}

// Applies a preset's OS-side backdrop (DWM material + optional blur-behind) and
// records it as the active preset. Does NOT touch the renderer (see
// applyPresetAndNotify for the full cycle); the startup path uses this alone so
// the shipped CSS + focus-compensation are left exactly as-is on first paint.
function applyPresetOS(preset) {
  if (!win) return;
  activePresetId = preset.id;

  // First set the DWM material so a preset that wants the raw blur-behind does
  // not have both acrylics fighting (setBackgroundMaterial('none') first).
  try {
    if (process.platform === 'win32') {
      win.setBackgroundMaterial(preset.material === 'acrylic' ? 'acrylic' : 'none');
    }
  } catch {
    // setBackgroundMaterial is Windows-only and version-gated; a throw here is
    // a safe no-op, the CSS layers still render.
  }

  // Accent policy: on a win11-capable machine ALWAYS reconcile it, so a preset
  // WITHOUT a composition clears any blur-behind left over from a previous one
  // (OS accent state persists independently of setBackgroundMaterial; without
  // this, cycling blur-behind -> css/dwm would composite two backdrops). State 4
  // enables the blur for composition presets; state 0 (ACCENT_DISABLED) clears.
  if (glassCapability === 'win11') {
    const accentState = preset.composition ? 4 : 0;
    const accentAlpha = preset.composition ? preset.composition.alpha : 0;
    applyAccentPolicy(accentState, accentAlpha).then((ok) => {
      if (!ok && preset.composition) {
        console.log('[glass] blur-behind composition did not apply (safe no-op).');
      }
    });
  }

  // Force a repaint so a just-(re)attached DWM acrylic material shows without a
  // manual resize. Inline (not via nudgeAcrylicMaterial, whose osAcrylicActive
  // gate could no-op here after a css/blur default is baked). Only meaningful
  // for the acrylic material on a win11-capable machine.
  if (preset.material === 'acrylic' && glassCapability === 'win11') {
    nudgeWindowRepaint();
  }
}

// Pushes the active preset's look to the renderer: the data-glass token set and
// either an inline --glass-tint override (blur-behind / css / any nudged preset)
// or a null tint that tells the renderer to REMOVE the override and hand control
// back to the shipped CSS (the baseline). Also carries the label + alpha for the
// on-screen toast.
function sendGlassPreset() {
  if (!win) return;
  const preset = PRESETS[currentPresetIndex];
  win.webContents.send('glass-preset', {
    id: preset.id,
    label: preset.label,
    dataGlass: preset.dataGlass,
    tint: tintOverrideActive
      ? { r: preset.tintRGB[0], g: preset.tintRGB[1], b: preset.tintRGB[2], a: currentTintAlpha }
      : null
  });
}

// Ctrl+Shift+G: advance to the next preset, apply it OS-side, reset the tint
// state to that preset's own values, and notify the renderer.
function cycleGlassPreset() {
  if (!win) return;
  currentPresetIndex = (currentPresetIndex + 1) % PRESETS.length;
  const preset = PRESETS[currentPresetIndex];
  applyPresetOS(preset);
  currentTintAlpha = preset.cssTintAlpha;
  tintOverrideActive = preset.tintOverride;
  sendGlassPreset();
}

// Ctrl+Shift+Up / Ctrl+Shift+Down: nudge the CSS --glass-tint alpha live for
// fine legibility tuning on whichever preset is active. Nudging always engages
// an explicit override (even on the baseline, which then leaves its pure-CSS
// state), clamped to 0.10..0.90.
function nudgeGlassTint(delta) {
  if (!win) return;
  const next = Math.min(0.9, Math.max(0.1, Math.round((currentTintAlpha + delta) * 100) / 100));
  currentTintAlpha = next;
  tintOverrideActive = true;
  sendGlassPreset();
}

function createWindow() {
  const workArea = screen.getPrimaryDisplay().workArea;
  winX = workArea.x + MARGIN;
  winY = workArea.y + MARGIN;

  const windowOptions = {
    width: PANEL_WIDTH,
    height: MIN_HEIGHT,
    x: winX,
    y: winY,
    frame: false,
    // User-resizable with sane bounds; content still owns the INITIAL height
    // (see RESIZE OWNERSHIP above). NOTE: Electron has historically limited
    // resizing transparent frameless windows on some platforms; on the
    // Windows 11 builds this app targets it works, and if an environment
    // refuses it the app simply behaves like the old fixed-size build.
    resizable: true,
    minWidth: MIN_WIDTH,
    maxWidth: MAX_WIDTH,
    minHeight: MIN_HEIGHT,
    movable: true,
    alwaysOnTop: true,
    skipTaskbar: true, // pure overlay, matches the Cluely behavior; hotkey is the recovery route
    show: false, // shown on ready-to-show to avoid a first-paint flash
    fullscreenable: false,
    minimizable: false,
    maximizable: false, // also sidesteps the frameless-acrylic maximize/restore bugs
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      // Race-free: the renderer reads the glass mode synchronously from argv
      // (see preload.js), so <html data-glass> and the height-offset math are
      // correct on the very first paint, no IPC round-trip to wait on.
      additionalArguments: ['--glass-mode=' + glassMode]
    }
  };

  if (glassMode === 'vibrancy') {
    // Frosted, translucent chrome behind the (also translucent) CSS panel.
    // 'hud' is Apple's dark, floating-panel material. With the window rect ==
    // the panel (margin 0 in vibrancy mode), roundedCorners clips the
    // material to the native window radius and hasShadow draws a real shadow.
    windowOptions.transparent = true;
    windowOptions.vibrancy = 'hud';
    windowOptions.visualEffectState = 'active';
    windowOptions.roundedCorners = true;
    windowOptions.hasShadow = true;
  } else if (glassMode === 'acrylic') {
    // win11-capable, an acrylic-token default. Two window SHAPES share this
    // branch, chosen by the default preset's material so a one-line
    // DEFAULT_PRESET_ID change sets up the right window next launch:
    if (presetById(DEFAULT_PRESET_ID).material === 'none') {
      // A baked blur-behind default: the raw ACCENT_ENABLE_ACRYLICBLURBEHIND
      // (applied at startup below) needs a transparent client area to blur the
      // desktop behind it, and must NOT also carry a DWM backgroundMaterial (the
      // two acrylics would fight). So create the window transparent with no DWM
      // material. NOTE: this is why live-cycling to blur-behind from an OPAQUE
      // dwm-acrylic default under-renders it (a created-opaque window cannot
      // become transparent at runtime); to judge blur-behind on a real screen,
      // bake it as the default and relaunch.
      windowOptions.transparent = true;
      windowOptions.hasShadow = true;
    } else {
      // dwm-acrylic default: real DWM acrylic backdrop. Do NOT set
      // transparent:true here: the window stays "opaque" to the compositor,
      // which keeps native shadows, DWM rounded corners, and smooth resize,
      // while acrylic provides the desktop blur. backgroundColor alpha 0 clears
      // Chromium's own buffer so the DWM material shows through.
      windowOptions.backgroundMaterial = 'acrylic';
      windowOptions.backgroundColor = '#00000000';
      windowOptions.hasShadow = true;
    }
  } else {
    // css mode (Windows 10, or forced via LARP_GLASS=css): no DWM backdrop
    // and no DWM rounding, so the window must be transparent and the CSS
    // draws its own shadow and rounded corners.
    windowOptions.transparent = true;
    windowOptions.hasShadow = false;
  }

  win = new BrowserWindow(windowOptions);

  // Creation and first-show can emit resize noise (min-size clamping, DPI
  // scaling); none of it is the user grabbing an edge.
  suppressResizeUntil = Date.now() + 1500;

  // 'screen-saver' level keeps the panel above fullscreen apps and other
  // always-on-top windows, which is what a live overlay needs.
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setContentProtection(contentProtectionEnabled);

  win.once('ready-to-show', () => {
    win.show();
    // Apply the DEFAULT preset's OS-side backdrop once the window is up: for
    // dwm-acrylic this (re)attaches the DWM material and does the 1px paint
    // nudge for the known Electron frameless-acrylic bug class (issues 39959 /
    // 41824 / 42393 / 46753); for a baked blur-behind default it applies the
    // raw ACCENT_ENABLE_ACRYLICBLURBEHIND. Deliberately OS-side only (no
    // renderer tint override), so the shipped CSS + focus-compensation govern
    // the first paint exactly as before.
    applyPresetOS(presetById(DEFAULT_PRESET_ID));
  });

  if (IS_MAC && app.dock) {
    // Pure overlay, no app window to switch to: hiding the Dock icon keeps
    // Cmd+Tab and the Dock clean. The panel is still reachable via the hotkey.
    app.dock.hide();
  }

  win.webContents.on('did-finish-load', () => {
    win.webContents.send('protection-state', contentProtectionEnabled);
    win.webContents.send('engine-status', engineStatusState);
    win.webContents.send('window-focus-state', win.isFocused());
    // Only ever sent on a confirmed off (see checkWindowsTransparencyEnabled).
    // Nothing is sent when it is on or unknown, so the renderer never shows a
    // banner it has no real signal for.
    if (transparencyState) win.webContents.send('transparency-status', transparencyState);
    // A dev-server reload loses renderer state: re-tell it the user owns the
    // size, or it would silently resume auto-fitting a user-sized window.
    if (userResized) win.webContents.send('user-resized', true);
  });

  // Task: DWM dims/flattens the acrylic material on a window that is not the
  // foreground window, which is the COMMON state for this overlay (the user
  // is almost always focused on the page being checked, not the panel). Two
  // things happen on every blur/focus: (1) a best-effort re-apply of the same
  // backdrop material, on the chance DWM re-evaluates it fresh (this is not
  // expected to fully win, see the comment below); (2) the real focus state
  // is forwarded to the renderer, which is the actual mitigation, see the
  // [data-focused="false"] block in styles.css.
  win.on('blur', () => {
    if (osAcrylicActive()) {
      // Best-effort only: Windows ties the inactive-window dimming to window
      // activation state at the compositor level, not to a paint glitch, so
      // re-applying the same material is a cheap try, not a guaranteed fix.
      // Gated on osAcrylicActive so a blur-behind / css preset (whose backdrop
      // is focus-independent or self-owned) is never clobbered with acrylic.
      try {
        win.setBackgroundMaterial('acrylic');
      } catch {
        // Non-fatal, the CSS-side mitigation is the real fix either way.
      }
    }
    if (win) win.webContents.send('window-focus-state', false);
  });
  win.on('focus', () => {
    if (osAcrylicActive()) {
      try {
        win.setBackgroundMaterial('acrylic');
      } catch {
        // Non-fatal, see the blur handler above.
      }
    }
    if (win) win.webContents.send('window-focus-state', true);
  });

  // Keep the growth anchor in sync with wherever the user drags the panel.
  // Ignore the moves our own off-screen-guard setBounds emits (see
  // suppressMoveUntil), so an auto-shift never gets mistaken for a user move.
  const onMoved = () => {
    if (!win) return;
    if (Date.now() < suppressMoveUntil) return;
    const [x, y] = win.getPosition();
    winX = x;
    winY = y;
  };
  win.on('moved', onMoved);
  win.on('move', onMoved); // some platforms emit 'move' only

  // The first REAL user resize (not our own setBounds echo, which is inside
  // suppressResizeUntil) hands size ownership to the user for the session:
  // auto-fit stops and the renderer is told to fill the window and scroll
  // internally. Width and position are re-read on every user resize because
  // dragging a top/left edge changes both.
  const onUserResize = () => {
    if (!win) return;
    if (Date.now() < suppressResizeUntil) return;
    const [w] = win.getSize();
    const [x, y] = win.getPosition();
    winW = w;
    winX = x;
    winY = y;
    if (!userResized) {
      userResized = true;
      win.webContents.send('user-resized', true);
    }
  };
  win.on('resize', onUserResize);
  win.on('resized', onUserResize);

  if (isDev) {
    win.loadURL(DEV_SERVER_URL);
  } else {
    win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
  }

  // Closing the overlay on macOS hides it instead of destroying it. This
  // leaves the lightweight Electron process available for Control+Space to
  // summon instantly. Cmd+Q still quits normally because before-quit sets
  // appQuitting before BrowserWindow receives its close event.
  win.on('close', (event) => {
    if (IS_MAC && !appQuitting) {
      event.preventDefault();
      win.hide();
    }
  });

  win.on('closed', () => {
    win = null;
  });
}

// Renderer reports the exact OUTER window height it wants (its measured
// content height plus its own CSS margin, computed on the renderer side per
// glass mode). The window grows/shrinks to match, anchored at the current
// top-left so it never teleports, with an off-screen guard on the bottom
// edge. This engine runs only until the user's first manual resize, after
// which their explicit size owns the window (see RESIZE OWNERSHIP up top).
ipcMain.on('panel-resize', (_event, requestedHeight) => {
  if (!win) return;
  const target = Math.max(MIN_HEIGHT, Math.min(currentMaxHeight(), Math.round(requestedHeight)));
  const [, currentHeight] = win.getSize();
  // Once the user has manually resized, their size is the FLOOR: a shrink
  // report is ignored (their size wins), but a GROW report still passes through
  // so streaming verdict content can push the window taller to fit rather than
  // clipping inside the dragged height (grow-only, see RESIZE OWNERSHIP up top).
  // applyWindowHeight sets suppressResizeUntil, so this programmatic grow is not
  // mistaken for a fresh user grab and does not re-fire the user-resized IPC.
  if (userResized && target <= currentHeight) return;
  if (currentHeight === target) return;
  applyWindowHeight(target);
});

// Drag-to-resize from the renderer's bottom-right grip. Native edge-resize is
// unreliable on a transparent frameless window on Windows 11, so the grip
// streams pointer deltas (screen pixels) and we apply them to the window's
// bounds here. Anchored at the current top-left (winX/winY) so the top-left
// stays put while the bottom-right corner follows the cursor. Width is clamped
// to [MIN_WIDTH, MAX_WIDTH]; height to [MIN_HEIGHT, work-area height] (NOT the
// content MAX_HEIGHT cap: a manual resize is the user overriding auto-fit, so
// they may make it as tall as the screen allows). The first such resize flips
// userResized, which stops content auto-fit and tells the renderer to fill the
// chosen size and scroll internally (the same handoff a native resize makes).
ipcMain.on('user-resize-by', (_event, delta) => {
  if (!win) return;
  const dx = Math.round((delta && delta.dx) || 0);
  const dy = Math.round((delta && delta.dy) || 0);
  if (!dx && !dy) return;
  const nearest = screen.getDisplayNearestPoint({ x: winX, y: winY });
  const maxHeight = Math.max(MIN_HEIGHT, nearest.workArea.height - MARGIN);
  const [w, h] = win.getSize();
  const nextW = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, w + dx));
  const nextH = Math.max(MIN_HEIGHT, Math.min(maxHeight, h + dy));
  if (nextW === w && nextH === h) return;
  winW = nextW;
  // Our own setBounds echoes back as resize/move events; do not mistake them
  // for a fresh user grab or an off-screen auto-shift.
  suppressResizeUntil = Date.now() + 300;
  suppressMoveUntil = Date.now() + 300;
  win.setBounds({ x: winX, y: winY, width: nextW, height: nextH });
  if (!userResized) {
    userResized = true;
    win.webContents.send('user-resized', true);
  }
});

// The renderer's "Auto-resize" toggle: the user manually resized (which flipped
// userResized and put main into grow-only/ignore-shrink mode), then decided
// they would rather the panel go back to fitting the scan content. Clearing
// userResized hands height ownership back to the content auto-fit engine, so the
// next panel-resize report is honored in full (grow AND shrink) again. The
// renderer clears its OWN takeover flags in parallel and immediately re-reports
// the fitted height (see App.jsx reengageAutoResize), so this only has to reset
// main's side of the two-flag handoff.
ipcMain.handle('reengage-auto-resize', () => {
  userResized = false;
  return true;
});

// Renderer-driven toggle for screen-recording invisibility.
ipcMain.on('set-content-protection', (_event, enabled) => {
  contentProtectionEnabled = !!enabled;
  if (win) win.setContentProtection(contentProtectionEnabled);
});

// The in-app transparency prompt's "Enable" button. Runs only in response to
// this explicit click, never automatically. On success, re-query rather than
// assume, so the banner reflects reality, and give a live acrylic window a
// chance to notice. On failure (permissions, an unexpected environment),
// fall back to opening the Windows colors settings page so the user can flip
// it by hand.
ipcMain.on('enable-transparency', async () => {
  const ok = await enableWindowsTransparency();
  if (ok) {
    transparencyState = checkWindowsTransparencyEnabled() === false ? { enabled: false } : { enabled: true };
    if (win) win.webContents.send('transparency-status', transparencyState);
    nudgeAcrylicMaterial();
  } else {
    shell.openExternal('ms-settings:personalization-colors');
  }
});

// Open a source URL in the user's real browser (clickable evidence rows).
// Validates the scheme so the renderer can never coax the shell into opening
// anything but a web page.
ipcMain.on('open-external', (_event, url) => {
  if (typeof url !== 'string') return;
  if (!/^https?:\/\//i.test(url.trim())) return;
  shell.openExternal(url.trim());
});

// Copy a compact verdict summary to the clipboard (the copy-verdict button).
ipcMain.on('copy-text', (_event, text) => {
  if (typeof text !== 'string') return;
  clipboard.writeText(text);
});

// looksLikeTargetUrl and looksLikeLinkedInProfileUrl are pure, Electron-free
// string validators and now live in ./activeUrl.js (imported above) so a plain
// node test can exercise them and so they share the single normalizeCapturedUrl
// that makes them tolerant of schemeless Chromium omnibox captures.

// ---------- "Go" button, layer 1: native active-tab URL capture ----------
// The primary, no-vision-needed detection layer: read the address bar of
// whatever browser window is currently in the foreground, entirely via
// Windows' own accessibility API (System.Windows.Automation), no browser
// extension and no clipboard involved. Windows-only (guarded below); every
// other platform, and any failure on Windows itself, resolves to null so the
// caller falls back to the screenshot+vision layer, then the manual input.
//
// Reliability, stated honestly: this reads whichever control UIA exposes as
// the address bar's editable text. Chromium browsers (Chrome, Edge, Brave)
// expose this consistently as an Edit control, usually with AutomationId
// "omnibox"; the script below tries that first, then falls back to the
// first Edit control found in the window, which is a weaker heuristic and
// can occasionally grab the wrong control (e.g. a page's own search box, if
// the browser chrome's omnibox is not found first) or an empty value if the
// browser is between navigations. It can also fail to read the address bar
// on a browser build that renders its own custom, non-standard UIA tree
// (rare, but Arc in particular has been reported to vary), on a window in a
// loading/transient state, or if System.Windows.Automation is unavailable
// in the environment (non-Windows, or a stripped-down .NET). All of those
// cases resolve to null, which is a deliberate, safe degrade, never a
// thrown error the caller has to handle specially.
// The full script (fallback chain, Z-order enumeration, DWM/cloaked filtering,
// hardened omnibox read) and the honest reliability caveats live in
// ./activeUrl.js (buildActiveUrlScript). Arc renders a nonstandard UIA tree
// (best-effort, degrades to null unchanged); a window in a loading/transient
// state or an environment without System.Windows.Automation also degrades to
// null, never a thrown error the caller has to handle.

// Returns the active browser's address-bar URL as a string, or null if it is
// not Windows, no browser window could be read, or anything about the read
// failed. Never rejects and never throws: every failure path resolves null.
// hintHwnd (optional) is a decimal HWND string tried first (warm start); the
// script also prints the winning HWND + layer, which we parse to warm-start the
// next call and to log which layer won.
function attemptActiveBrowserUrl(hintHwnd = null) {
  return new Promise((resolve) => {
    const script = buildActiveUrlScript(hintHwnd, process.pid);

    let settled = false;
    let child;
    try {
      child = spawn(
        'powershell',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
        { windowsHide: true }
      );
    } catch {
      resolve(null);
      return;
    }

    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };

    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // best-effort only, nothing else to do if the kill itself fails
      }
      finish(null);
    }, ACTIVE_URL_TIMEOUT_MS);

    let stdout = '';
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    child.on('error', () => finish(null));
    child.on('exit', () => {
      // Script prints "URL<TAB>HWND<TAB>LAYER" on success, or nothing on any
      // failure. Parse the URL, remember the winning HWND for the next call's
      // warm-start hint, and log which layer won so future flakiness is
      // diagnosable from logs alone. Tolerant of a bare-URL line (no tabs).
      const text = stdout.trim();
      if (!text) {
        finish(null);
        return;
      }
      const firstLine = text.split(/\r?\n/)[0];
      const [url, hwnd, layer] = firstLine.split('\t');
      // Normalize AT THE SOURCE so every consumer (hotkey gate, Go-button IPC,
      // clipboard path) receives a schemed URL. Chromium browsers elide
      // "https://" in the address bar, so the UIA read returns a schemeless
      // string; normalizeCapturedUrl prepends the scheme for a real URL and
      // returns "" for junk, so the promise resolves a schemed URL or null.
      const cleanUrl = normalizeCapturedUrl(url);
      if (hwnd && /^\d+$/.test(hwnd.trim())) {
        lastBrowserForegroundHwnd = hwnd.trim();
      }
      if (cleanUrl) {
        console.log(`[active-url] layer=${layer || 'enum'} hwnd=${hwnd || '?'} url=${cleanUrl}`);
      }
      finish(cleanUrl || null);
    });
  });
}

function attemptMacActiveBrowserUrlViaJxa(timeoutMs = MAC_ACTIVE_URL_TIMEOUT_MS) {
  return new Promise((resolve) => {
    let settled = false;
    let child;
    try {
      child = spawn(
        'osascript',
        ['-l', 'JavaScript', '-e', buildMacActiveUrlScript()],
        { windowsHide: true }
      );
    } catch {
      resolve(null);
      return;
    }

    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // Best-effort only.
      }
      finish(null);
    }, timeoutMs);

    let stdout = '';
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    child.on('error', () => finish(null));
    child.on('exit', () => {
      const rawValue = stdout.trim().split(/\r?\n/)[0] || '';
      if (rawValue === MAC_AUTOMATION_DENIED_SENTINEL) {
        macAutomationStatus = 'denied';
        console.warn('[active-url] macOS Automation permission denied');
        finish(MAC_AUTOMATION_DENIED_SENTINEL);
        return;
      }
      const cleanUrl = normalizeCapturedUrl(rawValue);
      if (cleanUrl) macAutomationStatus = 'granted';
      if (cleanUrl) console.log(`[active-url] layer=mac-automation url=${cleanUrl}`);
      finish(cleanUrl || null);
    });
  });
}

function attemptMacActiveBrowserUrlViaHelper(timeoutMs) {
  if (!fs.existsSync(MAC_URL_HELPER_APP)) {
    return Promise.resolve({ available: false, value: null });
  }

  return new Promise((resolve) => {
    const outputPath = path.join(
      os.tmpdir(),
      'larp-url-' + process.pid + '-' + Date.now() + '-' + Math.random().toString(16).slice(2) + '.txt'
    );
    let settled = false;
    let pollTimer = null;

    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutTimer);
      if (pollTimer) clearInterval(pollTimer);
      try {
        fs.unlinkSync(outputPath);
      } catch {
        // The helper may not have created a result file.
      }
      resolve({ available: true, value });
    };

    const timeoutTimer = setTimeout(() => finish(null), timeoutMs);

    let launcher;
    try {
      launcher = spawn(
        'open',
        ['-na', MAC_URL_HELPER_APP, '--args', outputPath],
        { stdio: 'ignore', windowsHide: true }
      );
    } catch {
      finish(null);
      return;
    }

    launcher.once('error', () => finish(null));
    pollTimer = setInterval(() => {
      let rawValue;
      try {
        if (!fs.existsSync(outputPath)) return;
        rawValue = fs.readFileSync(outputPath, 'utf8').trim();
      } catch {
        return;
      }

      if (rawValue === MAC_AUTOMATION_DENIED_SENTINEL) {
        macAutomationStatus = 'denied';
        console.warn('[active-url] macOS URL helper Automation permission denied');
        finish(MAC_AUTOMATION_DENIED_SENTINEL);
        return;
      }

      const cleanUrl = normalizeCapturedUrl(rawValue);
      if (cleanUrl) macAutomationStatus = 'granted';
      if (cleanUrl) console.log('[active-url] layer=mac-url-helper url=' + cleanUrl);
      finish(cleanUrl || null);
    }, 100);
  });
}

async function attemptMacActiveBrowserUrl(timeoutMs = MAC_ACTIVE_URL_TIMEOUT_MS) {
  // The signed helper calls Apple's native
  // AEDeterminePermissionToAutomateTarget API before reading the browser URL.
  // Its build is content-addressed, so the code signature stays stable across
  // launches and macOS can retain the Automation decision.
  const helperResult = await attemptMacActiveBrowserUrlViaHelper(timeoutMs);
  if (helperResult.available) {
    if (helperResult.value === MAC_AUTOMATION_DENIED_SENTINEL) {
      const historyResult = await getRecentMacBrowserHistoryUrl();
      if (historyResult) {
        console.log(
          '[active-url] layer=' + historyResult.source +
            ' age_seconds=' + historyResult.ageSeconds +
            ' url=' + historyResult.url
        );
        return historyResult.url;
      }
    }
    return helperResult.value;
  }

  const jxaResult = await attemptMacActiveBrowserUrlViaJxa(timeoutMs);
  if (jxaResult === MAC_AUTOMATION_DENIED_SENTINEL) {
    const historyResult = await getRecentMacBrowserHistoryUrl();
    if (historyResult) {
      console.log(
        '[active-url] layer=' + historyResult.source +
          ' age_seconds=' + historyResult.ageSeconds +
          ' url=' + historyResult.url
      );
      return historyResult.url;
    }
  }
  return jxaResult;
}

// Public entry point: run the platform capture. On Windows, if the first
// attempt comes back null, retry the spawn once after a short delay. Comet in
// particular occasionally throws RPC_E_SERVERFAULT while its accessibility tree
// is busy, nulling an otherwise-fine read; a single bounded retry recovers that
// transient fault without turning a genuinely empty result into a long hang.
// macOS uses one bounded osascript pass. Never rejects and never throws.
function getActiveBrowserUrl(hintHwnd = null) {
  if (process.platform === 'darwin') {
    return getBrowserCompanionTab().then(async (url) => {
      if (url) return url;
      if (isBrowserCompanionInstalled(os.homedir(), BROWSER_EXTENSION_DIR)) {
        const historyResult = await getRecentMacBrowserHistoryUrl(
          os.homedir(),
          COMPANION_HISTORY_URL_AGE_SECONDS
        );
        if (historyResult) {
          console.log(
            '[active-url] layer=' + historyResult.source +
              ' companion_fallback=true' +
              ' age_seconds=' + historyResult.ageSeconds +
              ' url=' + historyResult.url
          );
          return historyResult.url;
        }
      }
      return attemptMacActiveBrowserUrl();
    });
  }
  if (process.platform !== 'win32') {
    return Promise.resolve(null);
  }
  return attemptActiveBrowserUrl(hintHwnd).then((first) => {
    if (first) return first;
    return new Promise((resolve) => {
      setTimeout(() => {
        attemptActiveBrowserUrl(hintHwnd).then(resolve, () => resolve(null));
      }, 250);
    });
  }, () => null);
}

// macOS requires the Screen Recording permission (System Settings > Privacy
// & Security > Screen Recording) before desktopCapturer will return any
// screen sources. Electron does not throw for a missing permission there,
// it just returns an empty source list, so an empty list on darwin is
// treated as "permission not granted" and surfaced as a friendly, specific
// message instead of a silent no-op. On other platforms an empty list falls
// back to no screenshot, same as before.
async function captureActiveDisplayScreenshot() {
  const cursorPoint = screen.getCursorScreenPoint();
  const activeDisplay = screen.getDisplayNearestPoint(cursorPoint);

  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: { width: 1920, height: 1080 }
  });

  if (sources.length === 0) {
    if (IS_MAC) {
      throw new Error(
        'Screen Recording permission is needed for screenshots. Enable it in ' +
          'System Settings > Privacy & Security > Screen Recording, then restart the app.'
      );
    }
    return null;
  }

  const match =
    sources.find((s) => s.display_id && String(activeDisplay.id) === s.display_id) || sources[0];

  // The wire contract's field is "screenshot_b64", so send raw base64,
  // not a "data:image/png;base64,..." URL.
  const dataUrl = match.thumbnail.toDataURL();
  return dataUrl.replace(/^data:image\/\w+;base64,/, '');
}

// "Go" button on-demand IPC: the renderer calls these directly (via
// ipcRenderer.invoke in preload.js) rather than waiting on the hotkey event,
// so the idle bar's Go button and the hotkey share the exact same two
// detection layers implemented above.
ipcMain.handle('get-active-browser-url', async () => {
  try {
    // Pass the last winning HWND as a warm-start hint (correctness never
    // depends on it: the Z-order enumeration runs regardless).
    return await getActiveBrowserUrl(lastBrowserForegroundHwnd);
  } catch {
    return null;
  }
});

ipcMain.handle('open-automation-settings', async () => {
  if (!IS_MAC) return false;
  try {
    await shell.openExternal(
      'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation'
    );
    return true;
  } catch {
    return false;
  }
});

// Actively send a browser Apple Event from the foreground LARP Detector app.
// Merely opening Privacy & Security > Automation does not create an entry for
// an app. macOS creates that entry, and displays its native consent prompt,
// only when the app first attempts to automate a running target browser.
// If access was denied previously, macOS will not show the prompt again, so
// open the correct settings pane after the failed request for manual recovery.
ipcMain.handle('request-automation-permission', async () => {
  if (!IS_MAC) return getSetupStatus();

  const helperResult = await attemptMacActiveBrowserUrlViaHelper(
    MAC_PERMISSION_PROMPT_TIMEOUT_MS
  );
  if (!helperResult.available) {
    await attemptMacActiveBrowserUrlViaJxa(MAC_PERMISSION_PROMPT_TIMEOUT_MS);
  }

  if (macAutomationStatus === 'denied') {
    try {
      await shell.openExternal(
        'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation'
      );
    } catch {
      // The status still tells the renderer access was denied.
    }
  }

  const status = await getSetupStatus();
  if (win && !win.isDestroyed()) {
    win.webContents.send('setup-status', status);
  }
  return status;
});

ipcMain.handle('open-browser-companion-setup', async () => {
  if (!fs.existsSync(BROWSER_EXTENSION_DIR)) return false;
  try {
    shell.showItemInFolder(path.join(BROWSER_EXTENSION_DIR, 'manifest.json'));
    await shell.openPath(path.join(BROWSER_EXTENSION_DIR, 'INSTALL.html'));
    return true;
  } catch {
    return false;
  }
});

ipcMain.handle('capture-screenshot', async () => {
  try {
    return await captureActiveDisplayScreenshot();
  } catch {
    return null;
  }
});

// Clipboard read for the Go button's clipboard fallback layer (see App.jsx
// runGoScan). Returns "" on any failure so the renderer never has to handle a
// throw; the renderer decides whether the text looks like a target URL.
ipcMain.handle('read-clipboard-text', () => {
  try {
    return clipboard.readText();
  } catch {
    return '';
  }
});

ipcMain.handle('get-setup-status', async () => getSetupStatus());

ipcMain.handle('start-linkedin-login', async () => {
  const started = startLinkedInLogin();
  if (started && win && !win.isDestroyed()) {
    setTimeout(() => {
      getSetupStatus().then((status) => win.webContents.send('setup-status', status));
    }, 300);
  }
  return started;
});

ipcMain.handle('open-screen-recording-settings', async () => {
  if (!IS_MAC) return false;
  try {
    await shell.openExternal(
      'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'
    );
    return true;
  } catch {
    return false;
  }
});

async function scanCurrentProfileFromHotkey() {
  if (!win || win.isDestroyed()) return;
  try {
    if (!win.isVisible()) win.showInactive();
    // Capture before focusing the overlay so the browser remains the active app.
    const rawActiveUrl = await getActiveBrowserUrl();
    const automationDenied = rawActiveUrl === MAC_AUTOMATION_DENIED_SENTINEL;
    const activeUrl = looksLikeLinkedInProfileUrl(rawActiveUrl) ? rawActiveUrl.trim() : null;
    const clipboardText = clipboard.readText();
    const clipboardUrl = looksLikeTargetUrl(clipboardText) ? clipboardText.trim() : null;

    // Screen Recording is an optional fallback. A valid active URL or copied
    // profile link must never trigger a screenshot permission dependency.
    let screenshotDataUrl = null;
    if (!activeUrl && !clipboardUrl && !automationDenied) {
      try {
        screenshotDataUrl = await captureActiveDisplayScreenshot();
      } catch {
        screenshotDataUrl = null;
      }
    }

    win.webContents.send('hotkey-scan', {
      active_url: activeUrl,
      screenshot_b64: screenshotDataUrl,
      clipboard_url: clipboardUrl,
      capture_error: automationDenied ? 'automation_denied' : null
    });
    win.show();
    win.focus();
  } catch (err) {
    win.webContents.send('hotkey-error', {
      message: String(err && err.message ? err.message : err)
    });
    win.show();
    win.focus();
  }
}

function registerHotkey() {
  const ok = globalShortcut.register('CommandOrControl+Shift+L', scanCurrentProfileFromHotkey);

  if (!ok) {
    console.error('Failed to register the global shortcut (Cmd/Ctrl+Shift+L), it may be bound by another app.');
  }

  // Control+Space summons or hides the panel on macOS. CommandOrControl+Space
  // resolves to Cmd+Space there, which Spotlight owns, so macOS needs an
  // explicit Control modifier. Other platforms retain the original binding.
  const summonShortcut = summonShortcutForPlatform(process.platform);
  const okToggle = globalShortcut.register(summonShortcut, () => {
    if (!win || win.isDestroyed()) {
      createWindow();
      return;
    }
    if (win.isVisible()) {
      win.hide();
    } else {
      scanCurrentProfileFromHotkey();
    }
  });

  summonShortcutRegistered = okToggle;
  if (!okToggle) {
    console.error('[shortcut] summon ' + summonShortcut + ': unavailable');
  } else {
    console.log('[shortcut] summon ' + summonShortcut + ': registered');
  }

  // Live glass preset cycler (see the PRESETS array up top). Ctrl+Shift+G steps
  // through the presets on the running window so the owner can judge the glass
  // look with their own eyes on a real screen; Ctrl+Shift+Up/Down fine-tune the
  // CSS tint alpha on whichever preset is active. All three are no-ops if the
  // window is gone, and every failure resolves safely (see applyAccentPolicy).
  const okCycle = globalShortcut.register('CommandOrControl+Shift+G', cycleGlassPreset);
  if (!okCycle) {
    console.error('Failed to register the glass-cycler shortcut (Ctrl/Cmd+Shift+G), it may be bound by another app.');
  }

  const okTintUp = globalShortcut.register('CommandOrControl+Shift+Up', () => nudgeGlassTint(0.04));
  if (!okTintUp) {
    console.error('Failed to register the tint-up shortcut (Ctrl/Cmd+Shift+Up), it may be bound by another app.');
  }

  const okTintDown = globalShortcut.register('CommandOrControl+Shift+Down', () => nudgeGlassTint(-0.04));
  if (!okTintDown) {
    console.error('Failed to register the tint-down shortcut (Ctrl/Cmd+Shift+Down), it may be bound by another app.');
  }
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  if (checkWindowsTransparencyEnabled() === false) {
    transparencyState = { enabled: false };
  }
  createWindow();
  registerHotkey();
  // Fire-and-forget: the window shows immediately, engine-status IPC
  // updates the idle indicator once the engine is actually reachable.
  spawnEngine();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('before-quit', () => {
  stopEngine();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
