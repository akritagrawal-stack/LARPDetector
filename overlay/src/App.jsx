import { useCallback, useEffect, useRef, useState } from 'react';
import IdleView from './components/IdleView.jsx';
import SearchingView from './components/SearchingView.jsx';
import VerdictView from './components/VerdictView.jsx';
import TransparencyBanner from './components/TransparencyBanner.jsx';
import {
  MOCK_EVENTS,
  MOCK_ERROR_ENGINE,
  MOCK_ERROR_LONG,
  MOCK_BROKEN_EVENTS,
  MOCK_SCORING_EVENTS
} from './mock/mockEvents.js';

const SCAN_ENDPOINT = 'http://127.0.0.1:8756/scan';
const EVENTS_WS_BASE = 'ws://127.0.0.1:8756/events/';
const MAC_AUTOMATION_DENIED_SENTINEL = '__LARP_AUTOMATION_DENIED__';
const MAC_AUTOMATION_NOTICE =
  'Allow Electron to control your browser under Privacy & Security > Automation, then retry. ' +
  'You can also copy or paste the complete LinkedIn profile URL.';
const MAC_COMPANION_NOTICE =
  'The browser companion is installed but has not sent this LinkedIn profile yet. ' +
  'Refresh the profile tab once, then retry, or paste the complete profile URL.';

// Height clamp, single source of truth on the renderer side (mirrors the
// clamp in main.js). Content owns the height; overflow past MAX scrolls
// internally via each phase's own max-height.
// MIN_HEIGHT is a comfortable floor, not the resting size: content still owns
// the real height (idle is ~130px), this only keeps the very first frame and
// any tiny transient state from opening cramped. Kept in sync with the same
// constant in electron/main.js (the window's initial height and its clamp).
const MIN_HEIGHT = 64;

// The auto-fit HIGH clamp is the final-output envelope. The searching state
// never gets to become a taller window than the result it will eventually
// show. On a short display the screen-relative limit can still be smaller.
// This mirrors electron/sizing.js exactly.
const FINAL_OUTPUT_MAX_HEIGHT = 640;
const MAX_HEIGHT = (() => {
  const avail =
    (typeof window !== 'undefined' && window.screen && window.screen.availHeight) || 900;
  return Math.min(FINAL_OUTPUT_MAX_HEIGHT, Math.round(avail * 0.92));
})();

// The glass mode is passed synchronously from main.js via preload, so it is
// known on the first paint. In a plain browser tab there is no overlay, so it
// falls back to 'web' (same layer stack and CSS blur as 'css'). The panel's
// own CSS margin is 8px in css/web (the CSS shadow needs the room) and 0 in
// vibrancy/acrylic (the window rect IS the panel), so the outer window height
// the renderer reports is the measured content height plus that margin.
const GLASS_MODE = (typeof window !== 'undefined' && window.overlay?.glassMode) || 'web';

// The panel's own CSS margin depends on the LIVE glass preset, not just the
// launch mode: the Ctrl+Shift+G cycler restamps <html data-glass> at runtime
// (see onGlassPreset), and 'css'/'web'/blur presets carry an 8px margin (16px
// vertical) while acrylic/vibrancy carry 0 (the window rect IS the panel). The
// height report must read the margin from whatever preset is live at report
// time, so this is a function of data-glass rather than a load-time constant.
function marginForGlass(glass) {
  if (glass === 'vibrancy' || glass === 'acrylic') return 0;
  // cluely draws a rounded panel inside a transparent margin (12px top + 24px
  // bottom) so its CSS shadow has room to fade; the outer window must include
  // that vertical margin or the shadow gets clipped and reads as a square edge.
  if (glass === 'cluely') return 36;
  return 16;
}

// INTENTIONAL DUPLICATE of overlay/electron/activeUrl.js's normalizeCapturedUrl.
// The renderer is a Vite/React bundle that cannot import the main-process
// CommonJS module across the Electron boundary, so this tiny pure helper is
// copied verbatim (keep the two in sync). Chromium browsers elide "https://" in
// the address bar, so a captured or user-pasted URL can arrive schemeless; this
// prepends the scheme for a real domain/path and returns "" for junk, so the
// value sent to the backend /scan is always a schemed URL (or an untouched
// non-URL that the caller's own checks reject).
function normalizeCapturedUrl(text) {
  let cleaned = (text == null ? '' : String(text)).trim();
  if (!cleaned) return '';
  cleaned = cleaned.replace(/^[`"'“”‘’]+|[`"'“”‘’]+$/g, '').trim();
  if (/^\/\//.test(cleaned)) cleaned = 'https:' + cleaned;
  cleaned = cleaned.replace(/^(https?)\s*\/\/\s*/i, '$1://');
  cleaned = cleaned.replace(/^(https?):\s*\/(?!\/)\s*/i, '$1://');
  cleaned = cleaned.replace(/^(https?):(?!\/\/)\s*/i, '$1://');
  if (/linkedin\.com/i.test(cleaned)) {
    cleaned = cleaned.replace(/\s*\/\s*/g, '/');
  }
  if (/^[a-z][a-z0-9+.\-]*:\/\//i.test(cleaned)) return cleaned;
  if (/^[^\s/]+\.[^\s/]+/.test(cleaned)) return 'https://' + cleaned;
  return cleaned;
}

function derivePlatformAndType(rawUrl) {
  const url = (rawUrl || '').toLowerCase();
  if (url.includes('linkedin.com')) return { platform: 'linkedin', scan_type: 'person' };
  if (url.includes('crunchbase.com')) return { platform: 'crunchbase', scan_type: 'company_app' };
  return { platform: 'web', scan_type: 'company_app' };
}

// Turn a raw engine/scan error into something the user can act on. The common
// dead end (the engine's vision extract found no profile: ManualProvider with
// no Gemini key, or the overlay itself was focused so the active-tab read got
// nothing) arrives as "could not read a LinkedIn profile from your screen...".
// That is a real dead end unless the user pastes the URL, but the verdict state
// has no input box, so the message must point at the affordance that IS on
// screen: the "New scan" button, which returns to idle where the paste field
// lives. Action first, then the why. Anything we do not recognize passes
// through verbatim, never swallowed.
function friendlyError(text) {
  const raw = (text == null ? '' : String(text)).trim();
  if (
    /profile not seeded|linkedin_human returned none|linkedin session|login required|challenge|captcha/i.test(
      raw
    )
  ) {
    return (
      'The saved LinkedIn session needs attention. Click New scan, open Settings, ' +
      'then connect LinkedIn again.'
    );
  }
  if (/could not read a linkedin profile/i.test(raw)) {
    return (
      "Couldn't read a LinkedIn profile from your screen. Click New scan, then " +
      'paste the profile URL to check it. (Auto-read needs the profile open in a ' +
      "foreground browser tab, or the engine's Gemini vision key configured.)"
    );
  }
  return raw || 'Something went wrong running that scan. Click New scan and try again.';
}

// The verdict retints the whole pane's accent bleed to the tier color of the
// worse of the two scores: the panel subtly "runs hot" on a bad verdict.
function bleedForScore(score) {
  if (score <= 33) return 'rgba(52, 208, 107, 0.10)';
  if (score <= 66) return 'rgba(255, 170, 51, 0.10)';
  return 'rgba(255, 92, 77, 0.12)';
}

export default function App() {
  const [phase, setPhase] = useState('idle'); // 'idle' | 'searching' | 'verdict'
  const [url, setUrl] = useState('');
  const [scanTarget, setScanTarget] = useState('');
  const [statuses, setStatuses] = useState([]);
  // Unified, ordered "live search" feed: images, website source cards, and
  // thought/reasoning snippets, in the order the engine actually found them.
  const [feed, setFeed] = useState([]);
  const [claims, setClaims] = useState([]);
  const [founderScore, setFounderScore] = useState(null);
  const [companyScore, setCompanyScore] = useState(null);
  const [verdictText, setVerdictText] = useState('');
  const [error, setError] = useState(null);
  // Set when the service emits needs_url (a URL could not be confirmed): an
  // inline notice shown on the idle paste field. Distinct from `error` (a dead
  // verdict card): needs_url lands the user one paste away from a full scan.
  const [needsUrlNotice, setNeedsUrlNotice] = useState(null);
  // "full" or "shallow", carried on the scores event. A shallow verdict is
  // branded and its number de-emphasized so a degraded scan can never be
  // screenshotted as a real finding.
  const [scanDepth, setScanDepth] = useState('full');
  const [protectionEnabled, setProtectionEnabled] = useState(true);
  const [engineStatus, setEngineStatus] = useState(null);
  const [setupStatus, setSetupStatus] = useState(null);

  // Task 1: Windows "Transparency effects" guard. main.js reads the registry
  // once at launch and only ever tells the renderer when it is confirmed
  // off (see onTransparencyStatus in preload.js). Dismissal is persisted so
  // a user who has already seen and dismissed this is not nagged again on
  // every relaunch.
  const [transparencyPrompt, setTransparencyPrompt] = useState(null);
  const [transparencyDismissed, setTransparencyDismissed] = useState(() => {
    try {
      return localStorage.getItem('larp-transparency-dismissed') === '1';
    } catch {
      return false;
    }
  });

  // Live glass preset cycler: the brief on-screen toast shown when the owner
  // cycles a preset (Ctrl+Shift+G) or nudges the tint (Ctrl+Shift+Up/Down), so
  // they know which preset + tint they are currently looking at. null when
  // nothing is showing. Absent entirely in a plain browser (no cycler).
  const [glassToast, setGlassToast] = useState(null);

  // Task 2: whether the OS window currently has focus (main.js forwards
  // win.on('blur'/'focus') over IPC). Defaults to false, the common case for
  // this always-on-top overlay: the user is normally focused on the page
  // being checked, not the panel, so the panel starts in its compensated
  // "dimmed acrylic" look rather than flashing focused -> dimmed moments
  // after paint.
  const [windowFocused, setWindowFocused] = useState(false);

  // Phase crossfade: the outgoing view stays mounted for 140ms while it fades,
  // then the rendered phase swaps and the incoming view fades/slides in,
  // coordinated with the height tween so the glass appears to stretch and
  // refill. `sweeping` fires the one-shot glass light sweep on each change.
  const [renderedPhase, setRenderedPhase] = useState('idle');
  const [leaving, setLeaving] = useState(false);
  const [sweeping, setSweeping] = useState(false);

  const wsRef = useRef(null);
  const mockTimersRef = useRef([]);
  const panelRef = useRef(null);
  const measureRef = useRef(null);
  const feedIdRef = useRef(0);

  const clearMockTimers = () => {
    mockTimersRef.current.forEach((id) => clearTimeout(id));
    mockTimersRef.current = [];
  };

  const closeSocket = () => {
    if (wsRef.current) {
      const ws = wsRef.current;
      wsRef.current = null;
      // Detach the handlers FIRST, synchronously, before close(). A frame the
      // browser already queued to dispatch (received just before the user
      // clicked "New scan", which is common: a real scan keeps streaming
      // status/thought/claim events for a beat after the verdict lands) will
      // otherwise still fire ws.onmessage even after close() runs, since
      // close() does not retroactively cancel an already-queued dispatch.
      // That stale dispatch would call applyEvent with the CURRENT setPhase,
      // flipping the UI straight back to 'verdict' right after resetToIdle
      // just set it to 'idle', which reads as "the New scan button does
      // nothing" (this is exactly what it was doing).
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      try {
        ws.close();
      } catch {
        // socket may already be closed, nothing to do
      }
    }
  };

  const resetToIdle = useCallback(() => {
    clearMockTimers();
    closeSocket();
    setPhase('idle');
    setStatuses([]);
    setFeed([]);
    setClaims([]);
    setFounderScore(null);
    setCompanyScore(null);
    setVerdictText('');
    setError(null);
    setNeedsUrlNotice(null);
    setScanDepth('full');
  }, []);

  const pushFeedItem = useCallback((item) => {
    feedIdRef.current += 1;
    // `t` (arrival time) lets SearchingView cascade a burst of rows that land
    // within ~90ms of each other instead of thudding in all together.
    setFeed((prev) => [...prev, { id: feedIdRef.current, t: Date.now(), ...item }]);
  }, []);

  const applyEvent = useCallback(
    (evt) => {
      switch (evt.type) {
        case 'status':
          setStatuses((prev) => [...prev, evt.text]);
          break;
        case 'image':
          // `fallback` is optional: a second, independently-reliable image
          // URL the engine sends alongside a Clearbit logo (see
          // detective/service.py), since logo.clearbit.com's public host is
          // confirmed dead. SearchingView swaps to it on a load error
          // instead of degrading straight to a bare monogram letter.
          //
          // `imageKind` ('logo' | 'photo') and `isHero` carry the engine's
          // rendering hints through to SearchingView (see the "image" event
          // in detective/service.py's module docstring): a favicon/Clearbit
          // "logo" is inherently low-res and must render small+contained,
          // never stretched to fill the hero (that stretch is what used to
          // blur a company/employer logo into an upscaled blob); a "photo"
          // (proxied LinkedIn photo, og:image) is a real image and may
          // cover-fill. `isHero` false means this image can only ever be a
          // thumbnail, never the big hero, which is what stops the hero from
          // flickering between a logo/og-thumbnail and the profile photo.
          // Named `imageKind`/`isHero` (not `kind`) so they never collide
          // with this feed item's own `kind` ('image' vs 'website' etc).
          // Both default to the pre-existing behavior when the engine omits
          // them (mock mode's fixtures: every image is a real photo and is
          // meant to become the hero in turn), so nothing here is a breaking
          // change for the mock/demo sequences.
          pushFeedItem({
            kind: 'image',
            url: evt.url,
            caption: evt.caption,
            fallback: evt.fallback,
            imageKind: evt.kind === 'logo' ? 'logo' : 'photo',
            isHero: evt.hero !== false
          });
          break;
        case 'website':
          pushFeedItem({ kind: 'website', url: evt.url, title: evt.title, favicon: evt.favicon });
          break;
        case 'thought':
          pushFeedItem({ kind: 'thought', text: evt.text });
          break;
        case 'claim':
          setClaims((prev) => [...prev, { assertion: evt.assertion, tier: evt.tier }]);
          break;
        case 'scores':
          setFounderScore(
            typeof evt.founder_larp_score === 'number' ? evt.founder_larp_score : null
          );
          setCompanyScore(
            typeof evt.company_larp_score === 'number' ? evt.company_larp_score : null
          );
          setScanDepth(evt.scan_depth === 'shallow' ? 'shallow' : 'full');
          setPhase('verdict');
          break;
        case 'verdict':
          setVerdictText(evt.text);
          setPhase('verdict');
          break;
        case 'needs_url':
          // Not a verdict and not a dead error: the service could not confirm a
          // profile URL. Return to the idle paste field with an inline notice,
          // never the verdict phase, so the user is one paste from a full scan.
          closeSocket();
          setStatuses([]);
          setFeed([]);
          setClaims([]);
          setFounderScore(null);
          setCompanyScore(null);
          setVerdictText('');
          setError(null);
          setScanDepth('full');
          setNeedsUrlNotice(
            (evt.text && String(evt.text)) ||
              'Could not capture the profile URL. Paste it to run a full scan.'
          );
          setPhase('idle');
          break;
        case 'error':
          if (
            /profile not seeded|linkedin_human returned none|linkedin session|login required|challenge|captcha/i.test(
              String(evt.text || '')
            )
          ) {
            setSetupStatus((prev) =>
              prev ? { ...prev, linkedin_authenticated: false } : prev
            );
          }
          setError(friendlyError(evt.text));
          setPhase('verdict');
          break;
        case 'done':
        default:
          break;
      }
    },
    [pushFeedItem]
  );

  const runMockSequence = useCallback(() => {
    resetToIdle();
    setPhase('searching');
    setScanTarget(url.trim() || 'demo target (linkedin.com/in/example)');

    let elapsed = 0;
    MOCK_EVENTS.forEach(({ delay, event }) => {
      elapsed += delay;
      const id = setTimeout(() => applyEvent(event), elapsed);
      mockTimersRef.current.push(id);
    });
  }, [applyEvent, resetToIdle, url]);

  // Mock error: drive a single error event straight from idle to verdict (no
  // searching phase), the exact shape of a Go-button failure, so the verdict
  // error sizing/wrapping is provable from the default state. 'engine' feeds
  // the verbatim engine string (proves friendlyError rewrites it); anything
  // else feeds the long unbreakable-token message (proves wrap + scroll cap).
  const runMockError = useCallback(
    (kind) => {
      resetToIdle();
      const text = kind === 'engine' ? MOCK_ERROR_ENGINE : MOCK_ERROR_LONG;
      const id = setTimeout(() => applyEvent({ type: 'error', text }), 400);
      mockTimersRef.current.push(id);
    },
    [applyEvent, resetToIdle]
  );

  // Mock broken media: a short searching sequence whose images/favicons all
  // fail or are missing, to prove FeedVisual always renders a monogram tile,
  // never an empty box.
  const runMockBroken = useCallback(() => {
    resetToIdle();
    setPhase('searching');
    setScanTarget('demo target (broken-media fallback)');
    let elapsed = 0;
    MOCK_BROKEN_EVENTS.forEach(({ delay, event }) => {
      elapsed += delay;
      const id = setTimeout(() => applyEvent(event), elapsed);
      mockTimersRef.current.push(id);
    });
  }, [applyEvent, resetToIdle]);

  // Mock scoring wait: a compressed evidence burst, a claim-heavy list (so the
  // claims box overflows and proves the newest claim auto-scrolls into view
  // while the sticky header stays put), then a LONG pause on the "weighing
  // evidence" step before scores land, so the harness can capture the
  // persistent "Scoring the evidence" status that covers the operator-scoring
  // wait. Kept as its own mode so it never perturbs the shared MOCK_EVENTS
  // timeline the other capture shots key off.
  const runMockScoring = useCallback(() => {
    resetToIdle();
    setPhase('searching');
    setScanTarget('demo target (scoring wait)');
    let elapsed = 0;
    MOCK_SCORING_EVENTS.forEach(({ delay, event }) => {
      elapsed += delay;
      const id = setTimeout(() => applyEvent(event), elapsed);
      mockTimersRef.current.push(id);
    });
  }, [applyEvent, resetToIdle]);

  // Shared POST /scan -> open websocket -> stream events plumbing, used by
  // every scan entry point (manual URL, the Go button's exact-URL happy
  // path, and the screenshot/vision fallback below), so each of those only
  // has to build its own request body and target label.
  const startScan = useCallback(
    async (body, targetLabel) => {
      resetToIdle();
      setPhase('searching');
      setScanTarget(targetLabel);

      try {
        const res = await fetch(SCAN_ENDPOINT, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });

        if (!res.ok) throw new Error('Scan request failed (' + res.status + ')');
        const data = await res.json();
        if (!data.job_id) throw new Error('Scan service did not return a job id');

        const ws = new WebSocket(EVENTS_WS_BASE + data.job_id);
        wsRef.current = ws;

        ws.onmessage = (message) => {
          // Second line of defense (closeSocket also nulls these handlers
          // directly): if this socket is no longer the active one, a stray
          // dispatch must never resurrect a phase it no longer owns.
          if (wsRef.current !== ws) return;
          try {
            applyEvent(JSON.parse(message.data));
          } catch {
            // ignore a single malformed frame, the stream continues
          }
        };
        ws.onerror = () => {
          if (wsRef.current !== ws) return;
          setError('Lost connection to the scan service.');
          setPhase('verdict');
        };
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        const friendly =
          message.includes('Failed to fetch') || message.includes('NetworkError')
            ? "Can't reach the scan service on 127.0.0.1:8756. Make sure it's running, then try again."
            : message;
        setError(friendly);
        setPhase('verdict');
      }
    },
    [applyEvent, resetToIdle]
  );

  const runRealScan = useCallback(
    (targetUrl, screenshotB64) => {
      if (window.overlay && setupStatus && !setupStatus.linkedin_authenticated) {
        setNeedsUrlNotice(
          'Open Settings with the gear, then connect LinkedIn before running a live profile scan.'
        );
        setPhase('idle');
        return Promise.resolve(false);
      }
      if (window.overlay && setupStatus && !setupStatus.codex_ready) {
        setNeedsUrlNotice(
          'The ChatGPT desktop app and its Codex login are required for automatic scoring.'
        );
        setPhase('idle');
        return Promise.resolve(false);
      }
      // Single chokepoint for every scan entry point (manual paste, Go button,
      // hotkey, clipboard): normalize to a schemed URL before it is classified
      // or sent to the backend, so a schemeless Chromium capture or a
      // scheme-less paste is never shipped to /scan as-is. Falls back to the
      // raw value if normalization yields nothing (should not happen for a real
      // URL, but never send an empty string in its place).
      const normalizedUrl = normalizeCapturedUrl(targetUrl) || targetUrl;
      const { platform, scan_type } = derivePlatformAndType(normalizedUrl);
      return startScan(
        { url: normalizedUrl, screenshot_b64: screenshotB64 || null, scan_type, platform },
        normalizedUrl
      );
    },
    [setupStatus, startScan]
  );

  // Layer 2 fallback: no exact URL was found (UI-Automation came back
  // empty), so the engine reads the screenshot itself (see llm.vision_extract
  // and service.py's extract_from_screenshot routing) to find the profile.
  const runScreenshotScan = useCallback(
    (screenshotB64) =>
      startScan(
        {
          url: null,
          screenshot_b64: screenshotB64,
          extract_from_screenshot: true,
          scan_type: 'person',
          platform: 'linkedin'
        },
        'Reading the profile on your screen'
      ),
    [startScan]
  );

  // The "Go" button: no paste needed. Tries the three layers in priority
  // order (see overlay/electron/main.js's getActiveBrowserUrl docstring and
  // detective/service.py's module docstring for the full contract):
  //   1. Native active-tab URL (Windows UI Automation or macOS Automation).
  //   2. Screenshot + engine-side vision extraction.
  //   3. Whatever is already in the manual URL input.
  // Always degrades to a clear error rather than hanging: if nothing usable
  // is found by any layer, the user is told to paste the URL themselves.
  const [goBusy, setGoBusy] = useState(false);

  const runGoScan = useCallback(async () => {
    if (goBusy) return;
    setGoBusy(true);
    try {
      if (!window.overlay) {
        // Plain browser tab, no Electron: layers 1 and 2 do not exist here.
        if (url.trim()) {
          await runRealScan(url.trim(), null);
        } else {
          setNeedsUrlNotice('No URL to scan yet. Paste a profile URL (auto-detect needs the desktop app).');
          setPhase('idle');
        }
        return;
      }

      let activeUrl = null;
      let automationDenied = false;
      try {
        activeUrl = await window.overlay.getActiveBrowserUrl();
        automationDenied = activeUrl === MAC_AUTOMATION_DENIED_SENTINEL;
        if (automationDenied) activeUrl = null;
      } catch {
        activeUrl = null;
      }

      if (activeUrl && /linkedin\.com\/in\//i.test(activeUrl)) {
        await runRealScan(activeUrl.trim(), null);
        return;
      }

      // Clipboard layer (before vision): a user who just copied the profile
      // link should never be routed through the screenshot/vision fallback.
      if (window.overlay.readClipboardText) {
        let clip = '';
        try {
          clip = (await window.overlay.readClipboardText()) || '';
        } catch {
          clip = '';
        }
        if (clip && /linkedin\.com\/in\//i.test(clip)) {
          await runRealScan(clip.trim(), null);
          return;
        }
      }

      if (automationDenied) {
        const companionInstalled = !!setupStatus?.browser_companion_installed;
        if (!companionInstalled && window.overlay.openAutomationSettings) {
          try {
            await window.overlay.openAutomationSettings();
          } catch {
            // The actionable notice remains even if System Settings did not open.
          }
        }
        setNeedsUrlNotice(
          companionInstalled ? MAC_COMPANION_NOTICE : MAC_AUTOMATION_NOTICE
        );
        setPhase('idle');
        return;
      }

      let shot = null;
      try {
        shot = await window.overlay.captureScreenshot();
      } catch {
        shot = null;
      }

      if (shot) {
        await runScreenshotScan(shot);
        return;
      }

      if (url.trim()) {
        await runRealScan(url.trim(), null);
        return;
      }

      setNeedsUrlNotice("Couldn't detect the profile you're viewing. Paste the LinkedIn profile URL to check it.");
      setPhase('idle');
    } finally {
      setGoBusy(false);
    }
  }, [goBusy, url, runRealScan, runScreenshotScan]);

  // Wire the Electron scan shortcut. Absent in a plain browser, so
  // every call is optional-chained.
  useEffect(() => {
    if (!window.overlay) return undefined;

    const unsubscribeScan = window.overlay.onHotkeyScan((payload) => {
      // Same three-layer priority as the Go button (see runGoScan): the
      // active-tab URL first, then screenshot vision, then whatever the
      // user already typed or had copied.
      const activeUrl = payload && payload.active_url;
      if (activeUrl) {
        runRealScan(activeUrl, null);
        return;
      }
      if (payload && payload.capture_error === 'automation_denied') {
        const clipboardUrl = payload.clipboard_url;
        if (clipboardUrl) {
          runRealScan(clipboardUrl, null);
          return;
        }
        const companionInstalled = !!setupStatus?.browser_companion_installed;
        if (!companionInstalled && window.overlay.openAutomationSettings) {
          window.overlay.openAutomationSettings().catch(() => {});
        }
        setNeedsUrlNotice(
          companionInstalled ? MAC_COMPANION_NOTICE : MAC_AUTOMATION_NOTICE
        );
        setPhase('idle');
        return;
      }
      if (payload && payload.screenshot_b64) {
        runScreenshotScan(payload.screenshot_b64);
        return;
      }
      const targetUrl = url.trim() || (payload && payload.clipboard_url) || '';
      if (!targetUrl) {
        setError('No URL to scan yet. Click New scan and paste a profile URL, or copy a profile link first.');
        setPhase('verdict');
        return;
      }
      runRealScan(targetUrl, null);
    });

    const unsubscribeError = window.overlay.onHotkeyError((payload) => {
      setError((payload && payload.message) || 'Screenshot capture failed.');
      setPhase('verdict');
    });

    return () => {
      unsubscribeScan();
      unsubscribeError();
    };
  }, [url, runRealScan, runScreenshotScan, setupStatus]);

  // Browser-only fallback so the hotkey can be exercised without Electron.
  useEffect(() => {
    if (window.overlay) return undefined;
    const handler = (e) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'L' || e.key === 'l')) {
        e.preventDefault();
        if (url.trim()) runRealScan(url.trim(), null);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [url, runRealScan]);

  // Auto-run mock mode when opened as .../index.html?mock=1. Also supports
  // ?transparency=off, a test-only trigger that mirrors the real
  // transparency-status IPC payload so the in-app prompt can be captured by
  // a screenshot harness that never touches the registry or Electron's main
  // process (see overlay/capture.cjs).
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const mockParam = params.get('mock');
    if (mockParam === '1') runMockSequence();
    else if (mockParam === 'error') runMockError('long');
    else if (mockParam === 'error-engine') runMockError('engine');
    else if (mockParam === 'broken') runMockBroken();
    else if (mockParam === 'scoring') runMockScoring();
    if (params.get('transparency') === 'off') setTransparencyPrompt({ enabled: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Content-owned auto-height. The ResizeObserver watches the INNER measured
  // content node, never the height-animated .panel, so setting --panel-h can
  // never feed back into the measurement. Reports are quantized to 4px and
  // debounced 80ms so per-character typewriter reflow collapses into one
  // settled report. Growing: the OS window grows first (report), then the CSS
  // panel animates up to fill it. Shrinking: the CSS panel animates down
  // first, then the OS window shrinks once the tween has run. This keeps the
  // window a pure follower and never teleports it (main.js anchors at the
  // current position).
  const [panelH, setPanelH] = useState(null);
  const lastReportedRef = useRef(0);

  // Once the user manually resizes the OS window (user-resized IPC from
  // main.js, one-way for the session), content stops owning the height: the
  // panel fills whatever size the user chose (tracked live via window resize
  // events below) and the content column scrolls internally instead. The ref
  // mirrors the state so the settle() closure can check it without being
  // re-created.
  const [userSized, setUserSized] = useState(false);
  const userSizedRef = useRef(false);
  // The floor the auto-grow may never shrink below: the user's own window
  // height at takeover, kept in sync as they later drag. Grow-only auto-resize
  // (see settle) reads this so streaming verdict content can still push the
  // window TALLER to fit, but never snaps it below the size the user chose.
  const userFloorRef = useRef(0);
  // Populated by the measure effect below so the live glass-preset cycler can
  // force an immediate re-measure/re-report after it restamps data-glass (the
  // margin, and therefore the reported outer height, changes with the preset).
  const settleRef = useRef(null);

  // Bumped each time the user clicks the "Auto-resize" toggle to re-engage auto
  // sizing. Passed down as a signal so the active view scrolls its content box
  // to the newest update the instant auto is re-engaged (see SearchingView).
  const [reengageNonce, setReengageNonce] = useState(0);

  useEffect(() => {
    const el = measureRef.current;
    if (!el) return undefined;

    let debounceId = null;
    let shrinkId = null;

    const settle = () => {
      debounceId = null;
      // Read the margin from the LIVE preset every time, not a load-time
      // constant: cycling to/from acrylic changes it, and a stale margin would
      // report the outer window height 16px short of the panel's real box.
      const margin = marginForGlass(document.documentElement.dataset.glass);
      const raw = el.getBoundingClientRect().height;
      const quantized = Math.round(raw / 4) * 4;
      const target = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, quantized + margin));
      const cssH = target - margin;

      if (userSizedRef.current) {
        // GROW-ONLY after a manual resize. The user's chosen size is the floor:
        // never auto-shrink below it (that is what read as clipped/latched
        // before), but DO grow to fit taller streaming content, capped at
        // MAX_HEIGHT (content beyond that scrolls internally). panelH stays
        // owned by the fill effect (single owner, so the two-owners fight the
        // old latch prevented never returns), and lastReportedRef belongs to
        // the content-owned mode, so neither is touched on this path.
        if (target <= window.innerHeight) return;
        window.overlay?.reportSize(Math.min(target, MAX_HEIGHT));
        return;
      }

      if (target === lastReportedRef.current) {
        // No change: leave any pending shrink toward THIS same target intact
        // (cancelling it here would strand the window tall, never resized).
        setPanelH(cssH);
        return;
      }

      // A settle to a genuinely NEW target supersedes a shrink that has not
      // fired yet. Without this, a shrink scheduled 300ms out (e.g. the
      // searching -> verdict handoff) could still fire AFTER later content grew
      // the panel, snapping the OS window smaller than the now-taller .panel and
      // clipping the bottom (overflow:hidden). Cancel the stale shrink first.
      if (shrinkId) {
        clearTimeout(shrinkId);
        shrinkId = null;
      }

      if (target > lastReportedRef.current) {
        // Grow: window first, then the panel animates up into the new room.
        window.overlay?.reportSize(target);
        setPanelH(cssH);
      } else {
        // Shrink: panel animates down first, window follows after the tween.
        setPanelH(cssH);
        shrinkId = setTimeout(() => {
          shrinkId = null;
          // Belt and braces: if the user took over size ownership between
          // scheduling and firing, a late content-mode shrink must be a no-op
          // (the grow-only branch above now owns resizing).
          if (userSizedRef.current) return;
          window.overlay?.reportSize(target);
        }, 300);
      }
      lastReportedRef.current = target;
    };

    const onResize = () => {
      if (debounceId) clearTimeout(debounceId);
      debounceId = setTimeout(settle, 80);
    };

    const ro = new ResizeObserver(onResize);
    ro.observe(el);
    settleRef.current = settle;
    settle();

    return () => {
      ro.disconnect();
      if (debounceId) clearTimeout(debounceId);
      if (shrinkId) clearTimeout(shrinkId);
      settleRef.current = null;
    };
  }, []);

  // The user-resize takeover: after the first manual resize, the CSS panel
  // fills the OS window exactly (window inner height minus the live glass
  // margin) and keeps tracking it through the renderer's own resize events,
  // which fire continuously during an edge drag.
  useEffect(() => {
    if (!window.overlay?.onUserResized) return undefined;
    return window.overlay.onUserResized(() => {
      userSizedRef.current = true;
      // The floor starts at the window height the user just settled on.
      userFloorRef.current = window.innerHeight;
      setUserSized(true);
    });
  }, []);

  useEffect(() => {
    if (!userSized) return undefined;
    const fill = () => {
      const margin = marginForGlass(document.documentElement.dataset.glass);
      setPanelH(Math.max(0, window.innerHeight - margin));
      // Track the floor with the user: a later manual grow or shrink moves it,
      // so the grow-only auto-resize always measures against their latest size.
      userFloorRef.current = window.innerHeight;
    };
    fill();
    window.addEventListener('resize', fill);
    return () => window.removeEventListener('resize', fill);
  }, [userSized]);

  // The "Auto-resize" toggle. Auto is ACTIVE whenever the user has not taken
  // over sizing (!userSized). When they have (after a manual drag), this button
  // is the visible affordance to turn auto back ON, which RE-ENGAGES both sides
  // of the two-flag handoff and re-fits the window to the current content:
  //   1. Renderer: drop the takeover flags (state + refs) so the content-owned
  //      measure effect governs height again, and zero lastReportedRef so the
  //      next settle() is guaranteed to re-report the fitted height.
  //   2. Main: clear its own userResized flag (via the preload bridge) so it
  //      honors that report in full (grow AND shrink) instead of grow-only.
  //   3. Re-fit now (rAF settle) and signal the active view to scroll to its
  //      newest update (see reengageNonce -> SearchingView).
  // A no-op-ish click while auto is already ON simply re-fits and re-scrolls,
  // which can never break anything. The invoke is fired BEFORE the rAF settle so
  // main has cleared userResized by the time the re-report arrives.
  const reengageAutoResize = useCallback(() => {
    userSizedRef.current = false;
    userFloorRef.current = 0;
    lastReportedRef.current = 0;
    setUserSized(false);
    window.overlay?.reengageAutoResize?.();
    requestAnimationFrame(() => settleRef.current?.());
    setReengageNonce((n) => n + 1);
  }, []);

  useEffect(() => clearMockTimers, []);

  // Stamp the glass mode on the root element so styles.css picks per-mode
  // tint / blur / radius / margin. Falls back to 'web' in a plain browser.
  useEffect(() => {
    document.documentElement.dataset.glass = GLASS_MODE;
    document.documentElement.dataset.platform = window.overlay?.platform || 'web';
  }, []);

  // Drive the crossfade + one-shot glass sweep on every phase change.
  useEffect(() => {
    if (phase === renderedPhase) return undefined;
    setSweeping(false);
    const raf = requestAnimationFrame(() => setSweeping(true));
    setLeaving(true);
    const swap = setTimeout(() => {
      setRenderedPhase(phase);
      setLeaving(false);
    }, 140);
    const endSweep = setTimeout(() => setSweeping(false), 1100);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(swap);
      clearTimeout(endSweep);
    };
  }, [phase, renderedPhase]);

  // Sync the screen-recording invisibility toggle with main.js.
  useEffect(() => {
    if (!window.overlay) return undefined;
    return window.overlay.onProtectionState((enabled) => setProtectionEnabled(!!enabled));
  }, []);

  const toggleProtection = useCallback(() => {
    setProtectionEnabled((prev) => {
      const next = !prev;
      window.overlay?.setContentProtection(next);
      return next;
    });
  }, []);

  // Explicit drag-to-resize from the bottom-right grip. Transparent frameless
  // windows on Windows 11 are unreliable to grab at the native edge, so the
  // grip streams pointer deltas (in SCREEN pixels, DPI-correct and unaffected
  // by the window moving under the cursor) to main.js, which setBounds() the OS
  // window and flips it into user-sized mode. Pointer capture keeps the drag
  // alive even if the pointer briefly leaves the 16px grip. No-op in a plain
  // browser / the capture harness (no window.overlay): the grip still renders,
  // it just does not resize, which is all the harness needs to prove.
  const onResizeStart = useCallback((e) => {
    if (!window.overlay?.resizeWindowBy) return;
    e.preventDefault();
    e.stopPropagation();
    const grip = e.currentTarget;
    let lastX = e.screenX;
    let lastY = e.screenY;
    try {
      grip.setPointerCapture(e.pointerId);
    } catch {
      // capture is best-effort; the listeners below still track the drag
    }
    const onMove = (ev) => {
      const dx = ev.screenX - lastX;
      const dy = ev.screenY - lastY;
      lastX = ev.screenX;
      lastY = ev.screenY;
      if (dx || dy) window.overlay.resizeWindowBy(dx, dy);
    };
    const onUp = (ev) => {
      grip.removeEventListener('pointermove', onMove);
      grip.removeEventListener('pointerup', onUp);
      grip.removeEventListener('pointercancel', onUp);
      try {
        grip.releasePointerCapture(ev.pointerId);
      } catch {
        // already released
      }
    };
    grip.addEventListener('pointermove', onMove);
    grip.addEventListener('pointerup', onUp);
    grip.addEventListener('pointercancel', onUp);
  }, []);

  useEffect(() => {
    if (!window.overlay) return undefined;
    return window.overlay.onEngineStatus((status) => setEngineStatus(status));
  }, []);

  useEffect(() => {
    if (!window.overlay?.getSetupStatus) return undefined;
    let mounted = true;
    const refresh = async () => {
      try {
        const status = await window.overlay.getSetupStatus();
        if (mounted) setSetupStatus(status);
      } catch {
        // Setup status is advisory. Scanning still exposes actionable errors.
      }
    };
    refresh();
    const interval = setInterval(refresh, 5000);
    const unsubscribe = window.overlay.onSetupStatus
      ? window.overlay.onSetupStatus((status) => mounted && setSetupStatus(status))
      : () => {};
    return () => {
      mounted = false;
      clearInterval(interval);
      unsubscribe();
    };
  }, []);

  const startLinkedInSetup = useCallback(async () => {
    if (!window.overlay?.startLinkedInLogin) return;
    await window.overlay.startLinkedInLogin();
    const status = await window.overlay.getSetupStatus();
    setSetupStatus(status);
  }, []);

  const openScreenRecordingSetup = useCallback(() => {
    window.overlay?.openScreenRecordingSettings?.();
  }, []);

  const openBrowserCompanionSetup = useCallback(() => {
    window.overlay?.openBrowserCompanionSetup?.();
  }, []);

  // Task 1: show the banner only on a confirmed off, clear it on a confirmed
  // on (the result of the user clicking Enable and the registry write
  // succeeding).
  useEffect(() => {
    if (!window.overlay) return undefined;
    return window.overlay.onTransparencyStatus((status) => {
      setTransparencyPrompt(status && status.enabled === false ? status : null);
    });
  }, []);

  const enableTransparency = useCallback(() => {
    window.overlay?.enableTransparency();
  }, []);

  const dismissTransparencyPrompt = useCallback(() => {
    setTransparencyDismissed(true);
    try {
      localStorage.setItem('larp-transparency-dismissed', '1');
    } catch {
      // Best-effort only: a plain browser tab or a locked-down profile can
      // refuse localStorage, the banner just reappears next launch then.
    }
  }, []);

  // Task 2: real OS focus state, forwarded from main.js. Stamped on the root
  // element so styles.css can apply the unfocused-acrylic compensation.
  useEffect(() => {
    if (!window.overlay) return undefined;
    return window.overlay.onWindowFocusState((focused) => setWindowFocused(!!focused));
  }, []);

  useEffect(() => {
    document.documentElement.dataset.focused = windowFocused ? 'true' : 'false';
  }, [windowFocused]);

  // Live glass preset cycler. Restamp <html data-glass> to the preset's token
  // set, then either set an inline --glass-tint override (blur-behind / css /
  // any nudged preset) or REMOVE it so the shipped CSS + focus-compensation own
  // the baseline again. An inline var on documentElement (:root) beats the
  // stylesheet's [data-glass]/[data-focused] rules, which is exactly what a
  // focus-independent backdrop wants. Also raise the toast.
  useEffect(() => {
    if (!window.overlay?.onGlassPreset) return undefined;
    return window.overlay.onGlassPreset((preset) => {
      if (!preset) return;
      document.documentElement.dataset.glass = preset.dataGlass;
      const root = document.documentElement;
      if (preset.tint) {
        const { r, g, b, a } = preset.tint;
        root.style.setProperty('--glass-tint', 'rgba(' + r + ', ' + g + ', ' + b + ', ' + a + ')');
      } else {
        root.style.removeProperty('--glass-tint');
      }
      // The new preset may carry a different panel margin (acrylic/vibrancy 0
      // vs css/web 16px vertical), so re-measure and re-report the outer height
      // against the now-live data-glass. rAF lets the restamped CSS var settle
      // into layout first.
      requestAnimationFrame(() => settleRef.current?.());
      setGlassToast({
        id: preset.id,
        label: preset.label,
        alpha: preset.tint ? preset.tint.a : null,
        // A changing nonce so re-cycling the same preset still re-fires the
        // auto-hide timer and the entrance animation.
        nonce: Date.now()
      });
    });
  }, []);

  // Auto-hide the preset toast a beat after the last change.
  useEffect(() => {
    if (!glassToast) return undefined;
    const id = setTimeout(() => setGlassToast(null), 1600);
    return () => clearTimeout(id);
  }, [glassToast]);

  const latestStatus = statuses.length > 0 ? statuses[statuses.length - 1] : '';
  const hasFounder = typeof founderScore === 'number';
  const hasCompany = typeof companyScore === 'number';
  const sourceCount = feed.filter((f) => f.kind === 'website').length;

  const panelStyle = { '--panel-h': panelH != null ? panelH + 'px' : undefined };
  if (renderedPhase === 'verdict' && (hasFounder || hasCompany)) {
    const worst = Math.max(hasFounder ? founderScore : 0, hasCompany ? companyScore : 0);
    panelStyle['--accent-bleed'] = bleedForScore(worst);
  }

  const panelClass =
    'panel panel--' +
    renderedPhase +
    (sweeping ? ' panel--sweeping' : '') +
    (userSized ? ' panel--user-sized' : '');

  return (
    <div className={panelClass} ref={panelRef} style={panelStyle}>
      <div className="panel__grain" aria-hidden="true" />

      <div className="panel__measure" ref={measureRef}>
        <div className="panel__handle">
          <span className="panel__grip" aria-hidden="true" />
          {/* Auto-resize toggle. A no-drag child INSIDE the drag handle (the
              canonical custom-titlebar carve-out), so it stays clickable while
              the rest of the bar moves the window. Reflects whether auto sizing
              is active (!userSized): "on" when content owns the height, the
              affordance to re-engage after a manual drag when off. */}
          <button
            type="button"
            className={
              'panel__autoresize panel__interactive' +
              (userSized ? '' : ' panel__autoresize--on')
            }
            onClick={reengageAutoResize}
            aria-pressed={!userSized}
            title={
              userSized
                ? 'Auto-resize is off (you resized manually). Click to re-fit the panel to the scan and follow the newest update.'
                : 'Auto-resize is on: the panel fits the scan and follows the newest update.'
            }
          >
            <span className="panel__autoresize-dot" aria-hidden="true" />
            Auto-resize
          </button>
        </div>

        {transparencyPrompt && !transparencyDismissed && (
          <TransparencyBanner onEnable={enableTransparency} onDismiss={dismissTransparencyPrompt} />
        )}

        <div
          className={'panel__view' + (leaving ? ' panel__view--leaving' : '')}
          key={renderedPhase}
        >
          {renderedPhase === 'idle' && (
            <IdleView
              url={url}
              onUrlChange={setUrl}
              onTrigger={() => url.trim() && runRealScan(url.trim(), null)}
              onGoTrigger={runGoScan}
              goBusy={goBusy}
              onMockTrigger={runMockSequence}
              canTrigger={url.trim().length > 0}
              protectionEnabled={protectionEnabled}
              onToggleProtection={toggleProtection}
              engineStatus={engineStatus}
              notice={needsUrlNotice}
              setupStatus={setupStatus}
              onStartLinkedInLogin={startLinkedInSetup}
              onOpenScreenSettings={openScreenRecordingSetup}
              onOpenBrowserCompanionSetup={openBrowserCompanionSetup}
            />
          )}

          {renderedPhase === 'searching' && (
            <SearchingView
              target={scanTarget}
              latestStatus={latestStatus}
              feed={feed}
              claims={claims}
              onStop={resetToIdle}
              reengageSignal={reengageNonce}
            />
          )}

          {renderedPhase === 'verdict' && (
            <VerdictView
              founderScore={founderScore}
              companyScore={companyScore}
              verdictText={verdictText}
              error={error}
              claims={claims}
              sourceCount={sourceCount}
              scanTarget={scanTarget}
              scanDepth={scanDepth}
              onReset={resetToIdle}
            />
          )}
        </div>
      </div>

      <div className="panel__sweep" aria-hidden="true" />

      {/* Always-present drag-to-resize grip (bottom-right). panel__interactive
          opts it out of the window drag region so the pointer resizes instead
          of moving the window. */}
      <button
        type="button"
        className="panel__resize panel__interactive"
        onPointerDown={onResizeStart}
        aria-label="Resize the overlay"
        title="Drag to resize"
      />

      {glassToast && (
        <div className="glass-toast" key={glassToast.nonce} aria-hidden="true">
          <span className="glass-toast__label">{glassToast.label}</span>
          {glassToast.alpha != null && (
            <span className="glass-toast__alpha">tint {glassToast.alpha.toFixed(2)}</span>
          )}
        </div>
      )}
    </div>
  );
}
