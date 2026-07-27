# LARP Detector overlay

A small, semi-transparent, always-on-top desktop panel pinned to the top-left
of the screen (cross-platform: Windows and macOS). Press a hotkey, it grabs a
screenshot and a target URL, sends both to the scan engine, and streams back
what it finds as it works: pictures, the website sources it is checking,
short reasoning snippets, claims, and finally two LARP score meters plus a
verdict.

This is one self-contained app. The Electron shell spawns and manages the
Python scan engine itself as a hidden background process when it launches,
you never start a server or open a localhost page yourself.

## Run it

```
npm install
npm start
```

`npm start` builds the renderer once (`vite build`) and launches Electron
against the built files. On launch, Electron's main process spawns the
Python engine (`service_run.py`, one level up at the repo root) as a hidden
child process, waits for it to answer its `/health` endpoint, and only then
considers it ready. You do not run anything else yourself.

The panel appears in the top-left corner of your primary display, below the
macOS menu bar where relevant: a slim bar by default, transparent everywhere
except the panel itself, a shiny frosted-glass surface behind it. The blur is
real where the OS provides it (macOS vibrancy, Windows 11 acrylic) and a
self-contained CSS faux-glass everywhere else (see "Glass modes" below).

The Python side needs its own dependencies installed once, from the repository
root, one level up from `overlay/`:

```
pip install -r requirements.txt
```

If Python is missing, or those dependencies were never installed, the app
still launches. It shows a friendly message in the panel instead of
crashing or silently failing the first time you try to scan (see "Engine
status" below).

For active development, run the renderer and Electron separately so you get
hot reload (the engine still auto-spawns from Electron's main process either
way):

```
npm run dev            # Vite dev server on http://127.0.0.1:5173
VITE_DEV_SERVER=1 npm run electron   # in a second terminal
```

(On Windows PowerShell: `$env:VITE_DEV_SERVER=1; npm run electron`)

## Using it

- Type a LinkedIn or company URL into the bar, then press Enter or click the
  arrow, to run a scan.
- Or press `Control+Space` anywhere on the system. When the panel is hidden,
  this opens it and starts scanning the LinkedIn profile in the active browser.
  The app prefers the exact browser URL, then a copied profile link, and only
  then the optional screenshot fallback. Press `Control+Space` again while the
  panel is visible to hide it.
- Click "New scan" on the verdict card to reset back to idle.

**Moving (the panel sizes itself).** The window has no title bar. Drag it by
the thin grip at the very top of the panel, or by any other part of the panel
background that is not an input or a button, to move it anywhere on screen.
The panel is not user-resizable, and it does not need to be: its height is
owned entirely by its content (a slim bar when idle, taller once evidence and
a verdict render), and its width is fixed. It grows downward from wherever you
put it and never teleports back to the corner; if it would grow off the bottom
of the screen it shifts up just enough to stay on-screen, then returns to your
chosen spot when it shrinks again. Content past the maximum height scrolls
inside the panel rather than pushing it taller.

**Screen recording invisibility.** The "HIDDEN" / "VISIBLE" chip next to the
hotkey hint toggles content protection: when hidden (the default), the panel
is excluded from screenshots and from screen shares or recordings on both
platforms (Electron's `setContentProtection`), the same trick Cluely uses.
Click it to make the panel visible in captures again, e.g. to screenshot the
app itself.

**Engine status.** The small dot at the left of the idle bar reflects the
Python engine's state: gray means nothing to report yet, amber pulses while
it is starting, green means it answered its health check and is ready, red
means it did not come up (hover it, or read the message that appears under
the bar, for why: usually a missing Python interpreter or missing `pip`
dependencies).

## Mock mode (no backend required)

The panel ships with a hardcoded event sequence that exercises all three UI
states, including the live-search feed (pictures, website cards, reasoning
snippets), without any server running. Two ways to trigger it:

1. Click the small "DEMO" button next to the hotkey hint in the idle bar.
2. Load the renderer with `?mock=1` in the URL (for example
   `http://127.0.0.1:5173/?mock=1`), which auto-plays the sequence on load.
   Useful for driving the app headlessly, e.g. from a screenshot script.

The mock sequence lives in `src/mock/mockEvents.js`: a plain array of
`{ delay, event }` pairs using the exact same event shapes the real server
sends over the WebSocket, so it is a faithful stand-in.

## What talks to what

- `POST http://127.0.0.1:8756/scan` with
  `{ url, screenshot_b64, scan_type, platform }`, returns `{ job_id }`.
  `screenshot_b64` is raw base64 (no `data:image/png;base64,` prefix), since
  the field name says "b64".
- `ws://127.0.0.1:8756/events/{job_id}` streams JSON frames: `status`,
  `image`, `claim`, `scores`, `verdict`, `done`, `error`, plus two
  forward-compatible kinds the live-search feed already understands and
  degrades gracefully without: `website` (`{ url, title, favicon }`, a
  source page the engine is checking) and `thought` (`{ text }`, a short
  reasoning snippet). Both render alongside `image` events in one ordered
  feed, pictures and website cards paired with the reasoning beside them,
  today the feed just has fewer rows in it until the engine starts sending
  them.

The renderer (`src/App.jsx`) owns this flow and works with or without
Electron: `window.overlay` (exposed by the preload script) is always
optional-chained, so the same build runs standalone in a browser (mock mode,
or a real scan with no screenshot) or inside Electron (real scan with a
screenshot, plus the global hotkey, the self-spawned engine, and the
auto-resizing window).

## macOS notes

- **Window chrome**: `frame:false` + `transparent:true` + `vibrancy:'hud'`
  give a native frosted-glass panel with no title bar and no traffic-light
  window controls (frameless windows never show them on macOS, nothing
  extra to hide). The panel is pinned using the primary display's *work
  area* (`screen.getPrimaryDisplay().workArea`), which already excludes the
  menu bar and the Dock, so it never lands underneath either.
- **Dock icon**: hidden on launch (`app.dock.hide()`). This is a pure
  hotkey-driven overlay with nothing to switch to, so a Dock icon and a
  Cmd+Tab entry would just be clutter. The panel is still reachable at any
  time via the global hotkey.
- **One-time setup**: the idle panel reports LinkedIn login, Codex reviewer,
  browser-link access, and the optional screenshot fallback. LinkedIn login
  opens a dedicated Chrome profile once and saves the resulting session.
- **Browser companion**: Settings > Install extension opens the unpacked
  Chromium extension folder and the browser extension manager. The companion
  shares only the active LinkedIn profile URL with the local service, avoiding
  macOS Automation permission and the unreliable TCC behavior of ad-hoc signed
  development builds.
- **Screen Recording permission**: optional. The app requests a screenshot
  only when browser URL and clipboard detection both fail. The setup card can
  open System Settings if you want this fallback.
- **Engine env vars**: the default live LinkedIn path is self-contained and
  uses the dedicated browser profile created by the Settings login action.
  An operator who already has a compatible external `HumanSession` module can
  opt into that legacy adapter explicitly:
  ```
  export LINKEDIN_HUMAN_DIR="$HOME/path/to/compatible/scrapers"
  ```
  Normal setup and offline demo mode do not need this variable.
- **Shortcut**: `Control+Space` is used because macOS reserves `Cmd+Space` for
  Spotlight.

## Windows notes

- **Transparency effects requirement**: Windows 11 acrylic (the `acrylic`
  glass mode above) only renders as a live blur when the OS-wide
  "Transparency effects" setting is on (Settings > Personalization > Colors,
  or the registry value
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize\EnableTransparency`).
  When it is off, Windows silently falls back to a flat, more opaque panel
  for every app that requests acrylic, ours included, with no error and no
  event for the app to react to. `main.js` reads that one registry value
  once at launch (`reg query`, never a write) and, only when it is confirmed
  off, shows a small dismissible banner in the panel: "Windows transparency
  is off. Turn it on for the full glass look." with an Enable button and a
  dismiss button. Enable writes the value on (`reg add ... /f`, an HKCU
  write, no elevation needed) and, only if that write fails, falls back to
  opening the Windows colors settings page (`ms-settings:personalization-colors`)
  so the user can flip it by hand. The registry is never written without the
  user clicking Enable, and the banner never appears when the setting is
  already on or when the check itself is inconclusive (missing key, `reg`
  unavailable, anything short of a confirmed "off").
- **Acrylic looks flatter while the panel is not focused**: this is DWM
  behavior, not a bug in this app. Windows dims and flattens acrylic (and
  Mica) material on any window that is not the foreground window, the same
  behavior native Fluent apps like Windows Terminal show. Because this
  overlay is always-on-top and the user is normally focused on the page
  being checked (LinkedIn, Crunchbase, ...), not the panel, it spends nearly
  all of its time in that dimmed state, so this is the common case, not an
  edge case. `main.js` makes a best-effort attempt to keep the live material
  vivid by re-applying `setBackgroundMaterial('acrylic')` on every
  blur/focus, but this is not expected to fully win: DWM ties the dimming to
  window activation state at the compositor level, which is outside what a
  re-applied material call can override. The real fix is in `styles.css`:
  the renderer is told the actual focus state over IPC and stamps it as
  `[data-focused]` on the root element, and the acrylic mode's tint,
  specular rim, and inner highlights are boosted in the unfocused state so
  the panel still reads as intentional glass with depth and shine, not a
  dead flat rectangle, while backgrounded. This CSS compensation is what can
  be verified from a screenshot: `App.jsx` defaults to the unfocused,
  compensated look whenever there is no Electron main process to report a
  real focus state (exactly the case in a plain browser tab, or under
  `overlay/capture.cjs`), so a capture of the CSS-fallback panel is already
  showing that state. Whether the live DWM acrylic material itself stays
  visibly livelier after the blur/focus re-apply attempt needs a real
  Windows 11 desktop to judge; `capturePage()` reads Chromium's own paint
  buffer, not DWM's composited output, so no screenshot from this repo can
  show real acrylic vividness at all, focused or dimmed.

## File tree

```
overlay/
  package.json
  vite.config.js
  index.html
  electron/
    main.js       Window creation, engine spawn/health-check/cleanup, global
                  shortcut, screenshot capture, resize IPC, content protection,
                  Windows transparency-effects guard, acrylic focus handling
    preload.js     contextBridge surface: glassMode, onHotkeyScan,
                  onHotkeyError, reportSize, setContentProtection,
                  onProtectionState, openExternal, copyText, onEngineStatus,
                  onTransparencyStatus, enableTransparency, onWindowFocusState
  src/
    main.jsx        React root
    App.jsx         State machine: idle -> searching -> verdict, real + mock flows
    styles.css       All styling (self-contained, no external fonts or CDNs)
    components/
      IdleView.jsx      Idle bar: input, hotkey chip, protection toggle, DEMO
      SearchingView.jsx  Live-search feed (pictures, website cards, thoughts) + claims
      VerdictView.jsx
      MeterBar.jsx
      TypewriterText.jsx
      TransparencyBanner.jsx  Dismissible Windows transparency-effects nudge
    mock/
      mockEvents.js  Hardcoded event sequence for mock mode
```

## Glass modes

The chrome renders through one of three glass modes, chosen at launch by
`main.js` and stamped on the root element (`<html data-glass="...">`) so a
single CSS layer stack can adapt per mode. `main.js` passes the mode to the
renderer synchronously as a launch argument, so it is correct on the first
paint.

- **`vibrancy`** (macOS): real `NSVisualEffectView` `hud` material behind the
  window, native rounded corners and shadow. The window rect is the panel (no
  CSS margin), and the CSS tint is light so the material does the work.
- **`acrylic`** (Windows 11, build 22621 or newer): a real DWM acrylic
  backdrop via `backgroundMaterial: 'acrylic'`. The window stays non
  transparent to the compositor (so it keeps native shadows, DWM rounded
  corners, and smooth resize) while acrylic provides the desktop blur. A
  one-time 1px resize nudge on show works around a known Electron bug where
  the material can fail to attach on frameless windows.
- **`css`** (Windows 10, or anywhere the above are unavailable): a self
  contained faux glass. The window is transparent and the CSS draws its own
  blur (`backdrop-filter`), rounded corners, layered fill, specular rim, and
  shadow.

**Escape hatch:** set the environment variable `LARP_GLASS=css` to force the
CSS mode on any platform (useful if a driver/DWM combination fails to show
acrylic, or to verify the fallback look). A plain browser tab (no Electron)
uses an equivalent `web` mode.

## Design notes

The look is a shiny "liquid glass" floating panel: soft rounded corners
(per-mode radius: 14px in CSS mode, 8px acrylic, 10px vibrancy), a six-layer
shine stack (base tint, frost lift and depth shade, real or simulated blur, a
masked specular rim highlight, a static sheen plus SVG grain, and a one-shot
light sweep on phase changes), and a small motion vocabulary (staged evidence
reveal with per-row light sweeps, meter count-up with glow and needle, phase
crossfades). It expands downward from a slim bar into a calm panel for the
live-search feed and the verdict.

Type is split: **Inter** (bundled OFL woff2, weights 400/500/600) for UI text,
**JetBrains Mono** (bundled OFL) reserved for numeric and technical readouts:
the score numbers, the status ticker, domains, the shortcut chip, and the
VERDICT eyebrow. One accent plus a semantic trio (green / amber / red) carry
the two LARP meters, the claim tier dots, and the overall CLEAR / SUS / LARP
verdict chip. No CDNs or external requests: both font families ship inside the
app as woff2, and the only images are inline SVG data URIs.

The verdict card goes slightly beyond a plain answer: an overall verdict chip
that retints the whole pane's accent to the tier color, meter zone ticks and
labels, an evidence "receipts" strip (the highest-signal claims plus a sources
checked count), clickable source rows that open in your real browser, and a
copy-verdict button.
