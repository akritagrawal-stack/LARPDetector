// Active-browser-URL capture: the PowerShell + Win32/UIA script generator, in
// its own CommonJS module so it can be unit-tested from plain Node (no Electron
// runtime) per the plan's Task 1 verification, and so main.js stays focused on
// window/IPC wiring. main.js requires buildActiveUrlScript from here.
//
// STRATEGY (see docs/plans/deep-extraction.plan.md, Task 1). The old fallback
// keyed off Get-Process MainWindowHandle, which is one arbitrary handle per
// process, unrelated to which browser window the user is actually viewing, so
// with multiple windows it read the wrong omnibox (an Instagram tab) or
// nothing. This replaces it with Z-ORDER enumeration: EnumWindows returns
// top-level windows top-to-bottom in Z order, and the browser window the user
// was just looking at is the topmost VISIBLE, non-minimized, non-cloaked window
// owned by a known-browser process, sitting directly under the always-on-top
// overlay. That is a property of what the user is doing, not of process
// bookkeeping, so it generalizes to any browser, any window count, any monitor.
//
// The whole EnumWindows/DWM/pid enumeration lives INSIDE the C# Add-Type block
// (GetZOrderedWindows returns IntPtr[]), which dodges the classic PowerShell
// delegate-GC pitfall (the callback is only ever invoked synchronously inside
// GetZOrderedWindows, so it is alive for the whole EnumWindows call) and keeps
// the PowerShell side to filtering + the UIA omnibox read.
//
// Fallback chain inside the script, in order:
//   0. Hint hwnd (warm start): the winning hwnd from the previous call, tried
//      first if it still passes the known-browser/visible/not-cloaked filter.
//   1. Foreground fast path: GetForegroundWindow() if it is a known browser
//      (covers the hotkey path, where the read happens before the overlay shows).
//   2. Z-order enumeration: the topmost known-browser window with a TARGET URL
//      (LinkedIn/company/crunchbase), else the topmost with ANY non-empty URL.
// Output line shape: "URL<TAB>HWND<TAB>LAYER" so main.js can warm-start the next
// call and log which layer won. Every failure path prints nothing (caller -> null).
//
// No em dashes anywhere in this file (house rule).

const ACTIVE_URL_TIMEOUT_MS = 4000;
const ACTIVE_URL_KNOWN_BROWSERS = ['chrome', 'msedge', 'brave', 'arc', 'comet', 'opera', 'vivaldi'];

// The scan-target URL preference. A target-looking URL in a lower window is
// deliberately preferred over a non-target URL in the topmost window: the user
// pressed "scan this profile", so a visible LinkedIn window is the intended
// target even if some other browser window sits on top of it. Kept identical to
// the historical regex. Backslashes are doubled for the template literal below.
const TARGET_URL_REGEX_SOURCE = 'linkedin\\.com/in/|linkedin\\.com|crunchbase\\.com|\\.com/company';

// Build the PowerShell script. hintHwnd is a decimal HWND string/number to try
// first (or null/0 for none); overlayPid is this Electron process's pid, so no
// overlay-owned window (including devtools) is ever read even if a future build
// puts an Edit control in it.
function buildActiveUrlScript(hintHwnd, overlayPid) {
  const hint = Number.parseInt(hintHwnd, 10);
  const hintLiteral = Number.isFinite(hint) && hint > 0 ? String(hint) : '0';
  const pid = Number.parseInt(overlayPid, 10);
  const pidLiteral = Number.isFinite(pid) && pid > 0 ? String(pid) : '0';
  const knownLiteral = ACTIVE_URL_KNOWN_BROWSERS.map((n) => `'${n}'`).join(', ');

  return `
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes

  Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class LarpDetectorWin32 {
  [DllImport("user32.dll")]
  public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")]
  public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("dwmapi.dll")]
  public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out int val, int size);

  private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")]
  private static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);

  // EnumWindows yields top-level windows in Z order, topmost first. The
  // callback is only ever called synchronously during this method, so it can
  // never be GC'd mid-enumeration (the classic Add-Type delegate pitfall).
  public static IntPtr[] GetZOrderedWindows() {
    List<IntPtr> handles = new List<IntPtr>();
    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) { handles.Add(hWnd); return true; }, IntPtr.Zero);
    return handles.ToArray();
  }

  public static uint PidForWindow(IntPtr hWnd) {
    uint pid = 0;
    GetWindowThreadProcessId(hWnd, out pid);
    return pid;
  }

  // DWMWA_CLOAKED = 14. A cloaked window is on another virtual desktop or is a
  // suspended UWP shell: exactly the "reads a window the user is not looking
  // at" class. A failed DWM call is treated as not-cloaked (best-effort).
  public static bool IsCloaked(IntPtr hWnd) {
    int cloaked = 0;
    int hr = DwmGetWindowAttribute(hWnd, 14, out cloaked, sizeof(int));
    if (hr != 0) return false;
    return cloaked != 0;
  }
}
"@

  $known = @(${knownLiteral})
  $overlayPid = ${pidLiteral}
  $hint = ${hintLiteral}

  # Read the address-bar (omnibox) URL out of one window handle, or $null.
  # Hardened: a value that is empty or does not look URL-ish (no dot, e.g. an
  # in-progress omnibox edit or a page search box) returns $null so the caller
  # continues to the next window instead of returning junk.
  function Read-OmniboxUrl($hwnd) {
    if ($hwnd -eq [IntPtr]::Zero) { return $null }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    if ($null -eq $root) { return $null }
    $omniboxCond = New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::AutomationIdProperty, "omnibox"
    )
    # Each FindFirst is wrapped: some Chromium builds (Comet observed) throw
    # RPC_E_SERVERFAULT on the AutomationId search while their UIA tree is busy.
    # With ErrorActionPreference Stop that would abort the whole read, so a fault
    # on the omnibox lookup must fall through to the generic Edit lookup, and a
    # fault there returns null so the caller moves to the next window.
    $edit = $null
    try { $edit = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $omniboxCond) } catch { $edit = $null }
    if ($null -eq $edit) {
      $editTypeCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
      )
      try { $edit = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $editTypeCond) } catch { $edit = $null }
    }
    if ($null -eq $edit) { return $null }
    $vp = $null
    if (-not $edit.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { return $null }
    $val = $vp.Current.Value
    if (-not $val) { return $null }
    if ($val.IndexOf('.') -lt 0) { return $null }
    return $val
  }

  # A window we may read: visible, not minimized, not cloaked, owned by a known
  # browser process, and never the overlay's own process (devtools included).
  function Test-KnownBrowserWindow($hwnd) {
    if ($hwnd -eq [IntPtr]::Zero) { return $false }
    if (-not [LarpDetectorWin32]::IsWindowVisible($hwnd)) { return $false }
    if ([LarpDetectorWin32]::IsIconic($hwnd)) { return $false }
    if ([LarpDetectorWin32]::IsCloaked($hwnd)) { return $false }
    $wpid = [LarpDetectorWin32]::PidForWindow($hwnd)
    if ($wpid -eq 0) { return $false }
    if ($overlayPid -ne 0 -and $wpid -eq $overlayPid) { return $false }
    $proc = Get-Process -Id $wpid -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return $false }
    return ($known -contains $proc.ProcessName.ToLowerInvariant())
  }

  # Print "URL<TAB>HWND<TAB>LAYER" and exit. The HWND lets the caller warm-start
  # the next call; the LAYER lets it log which path won.
  function Write-Winner($url, $hwnd, $layer) {
    Write-Output ($url + "\`t" + $hwnd.ToString() + "\`t" + $layer)
  }

  # A non-target URL found by the hint or foreground layer. Remembered, NOT
  # returned immediately: see the target-guard note on layer 0 below.
  $fallback = $null; $fallbackH = $null; $fallbackLayer = $null

  # 0. HINT: the previous winner, if it still passes the filter (warm start).
  #
  # TARGET-GUARDED. This layer used to return ANY non-empty URL and exit, which
  # made the warm start actively harmful with more than one browser window open:
  # the previous winner might be a docs tab or an unrelated app, and returning
  # its URL short-circuited the z-order search that would have found the
  # LinkedIn window one layer down. main.js then correctly rejected the non-
  # profile URL, and the scan fell all the way through to screenshot vision,
  # which cannot recover a URL at all because Chromium renders the omnibox
  # elided ("linkedin.com / Jordan Rivera"). Net effect: a profile that was
  # plainly on screen scanned as "no URL found". Now a non-target hit is kept
  # only as a last-resort fallback and the search continues.
  if ($hint -ne 0) {
    $hintH = [IntPtr]$hint
    if (Test-KnownBrowserWindow $hintH) {
      $u = Read-OmniboxUrl $hintH
      if ($u) {
        if ($u -match '${TARGET_URL_REGEX_SOURCE}') { Write-Winner $u $hintH 'hint'; exit 0 }
        if ($null -eq $fallback) { $fallback = $u; $fallbackH = $hintH; $fallbackLayer = 'hint' }
      }
    }
  }

  # 1. FOREGROUND FAST PATH: the foreground window if it is a known browser.
  # Covers the hotkey path (read happens before the overlay shows) with zero
  # extra work. When the overlay is foreground this is skipped and we enumerate.
  # TARGET-GUARDED for the same reason as layer 0: the foreground browser window
  # is often NOT the one holding the profile (the user is looking at the overlay,
  # a terminal, or another tab when they press scan), so a non-target foreground
  # URL must not short-circuit the z-order search.
  $fg = [LarpDetectorWin32]::GetForegroundWindow()
  if ((Test-KnownBrowserWindow $fg)) {
    $u = Read-OmniboxUrl $fg
    if ($u) {
      if ($u -match '${TARGET_URL_REGEX_SOURCE}') { Write-Winner $u $fg 'fast'; exit 0 }
      if ($null -eq $fallback) { $fallback = $u; $fallbackH = $fg; $fallbackLayer = 'fast' }
    }
  }

  # 2. Z-ORDER ENUMERATION: walk top-level windows topmost-first, keeping only
  # known-browser windows, and read each omnibox. Prefer the FIRST (topmost)
  # target-looking URL; else the topmost window with ANY non-empty URL.
  $target = $null; $targetH = $null
  $firstAny = $null; $firstAnyH = $null
  foreach ($h in [LarpDetectorWin32]::GetZOrderedWindows()) {
    if (-not (Test-KnownBrowserWindow $h)) { continue }
    $u = Read-OmniboxUrl $h
    if (-not $u) { continue }
    if ($null -eq $firstAny) { $firstAny = $u; $firstAnyH = $h }
    if ($u -match '${TARGET_URL_REGEX_SOURCE}') { $target = $u; $targetH = $h; break }
  }
  if ($target) { Write-Winner $target $targetH 'enum'; exit 0 }
  if ($firstAny) { Write-Winner $firstAny $firstAnyH 'enum'; exit 0 }
  # Last resort: no window anywhere had a target URL, so hand back whatever the
  # hint or foreground layer saw. This preserves the old behavior EXACTLY for the
  # single-window case; it is only reached once the target search has failed.
  if ($fallback) { Write-Winner $fallback $fallbackH $fallbackLayer; exit 0 }
} catch {
  exit 0
}
`;
}

// Normalize a captured omnibox string into a schemed URL, or "" for junk.
//
// WHY THIS EXISTS: Chromium browsers (Comet, Chrome, Edge, Brave) display the
// address-bar URL WITHOUT the scheme, eliding "https://". The UIA read
// therefore returns a schemeless string like "linkedin.com/in/jordan-rivera-synthetic/",
// which every "https?://" validator would reject, dropping the scan to a
// fallback that cannot recover the URL. Normalizing here, at the shared helper
// both main.js (source + validators) and the renderer use, fixes it once.
//
// Rules:
//   - null/undefined/empty (after trim) -> "" (null-safe: call sites pass the
//     possibly-null capture result straight in).
//   - already has a scheme (http://, https://, anything scheme-shaped) ->
//     returned trimmed, untouched.
//   - looks like a bare domain/path (a dot before any slash or space, e.g.
//     "linkedin.com/in/x") -> "https://" prepended.
//   - anything else (a no-dot word, a search phrase like "reading your screen")
//     -> returned unchanged, so garbage stays garbage and is never coerced into
//     a bogus URL.
function normalizeCapturedUrl(text) {
  let cleaned = (text == null ? '' : String(text)).trim();
  if (!cleaned) return '';

  // Address bars, OCR, and accessibility APIs have each been observed
  // returning a real URL with a damaged or elided scheme. Repair only the
  // narrow leading forms, before the normal URL gate:
  //   linkedin.com/in/x       -> https://linkedin.com/in/x
  //   //linkedin.com/in/x     -> https://linkedin.com/in/x
  //   https//linkedin.com/... -> https://linkedin.com/...
  //   https:/linkedin.com/... -> https://linkedin.com/...
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

// True for a target-looking URL (LinkedIn, Crunchbase, a /company path, a .io
// or .ai domain). Scheme-tolerant: a schemeless capture is normalized first, so
// "linkedin.com/in/x" passes while a bare non-URL word still fails.
function looksLikeTargetUrl(text) {
  const normalized = normalizeCapturedUrl(text);
  if (!normalized) return false;
  return /linkedin\.com|crunchbase\.com|\.com\/company|\.io\b|\.ai\b/i.test(normalized);
}

// True only for a LinkedIn member profile URL (linkedin.com/in/<slug>).
// Scheme-tolerant via normalizeCapturedUrl, so a schemeless Chromium omnibox
// value is accepted; a /company path or a bare word is not.
function looksLikeLinkedInProfileUrl(text) {
  const normalized = normalizeCapturedUrl(text);
  if (!normalized) return false;
  return /^https?:\/\/([a-z]{2,3}\.)?linkedin\.com\/in\/[^/?#]+/i.test(normalized);
}

module.exports = {
  buildActiveUrlScript,
  ACTIVE_URL_TIMEOUT_MS,
  ACTIVE_URL_KNOWN_BROWSERS,
  TARGET_URL_REGEX_SOURCE,
  normalizeCapturedUrl,
  looksLikeTargetUrl,
  looksLikeLinkedInProfileUrl,
};
