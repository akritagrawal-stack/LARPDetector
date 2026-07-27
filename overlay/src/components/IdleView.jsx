// The scan shortcut used to hide inside the input placeholder, where the
// owner reported it as invisible. It now gets its own bright keycap hint line
// under the input (see idle__shortcut-hint). The symbol matches the platform
// the app is actually running on (window.overlay.platform, set by preload.js
// from process.platform, is undefined in a plain browser tab).
function shortcutLabel() {
  const platform = typeof window !== 'undefined' ? window.overlay?.platform : undefined;
  return platform === 'darwin' ? '⌃Space' : 'Ctrl+Space';
}

// main.js owns the Python engine child process and reports its lifecycle
// over IPC. This just turns that into a dot color: gray when there is
// nothing to report (plain browser tab, or Electron hasn't heard back
// yet), amber while the engine is starting, green once it answers its
// health check, red if it never came up (missing Python, missing deps, or
// it crashed).
function engineDotClass(engineStatus) {
  if (!engineStatus) return '';
  if (engineStatus.state === 'starting') return ' idle__status-dot--starting';
  if (engineStatus.state === 'ready') return ' idle__status-dot--ready';
  if (engineStatus.state === 'error') return ' idle__status-dot--error';
  return '';
}

// The Go button's own label swaps to a short "reading" state while
// runGoScan is in flight (checking the active tab, then, if needed,
// capturing and sending a screenshot for the engine to read), so a click
// that takes a beat never reads as unresponsive.
function goLabel(goBusy) {
  return goBusy ? 'Reading your screen...' : 'Scan this profile';
}

import { useEffect, useRef, useState } from 'react';

export default function IdleView({
  url,
  onUrlChange,
  onTrigger,
  onGoTrigger,
  goBusy,
  onMockTrigger,
  canTrigger,
  protectionEnabled,
  onToggleProtection,
  engineStatus,
  notice,
  setupStatus,
  onStartLinkedInLogin,
  onOpenScreenSettings,
  onOpenBrowserCompanionSetup
}) {
  const inputRef = useRef(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const setupNeedsAttention =
    !!setupStatus &&
    (!setupStatus.linkedin_authenticated ||
      !setupStatus.codex_ready ||
      (setupStatus.platform === 'darwin' &&
        !setupStatus.browser_companion_installed &&
        !setupStatus.browser_companion_connected));

  // When a needs_url notice lands (a URL could not be captured, or the Go
  // button exhausted every automatic layer), pull focus to the paste field so
  // the operator is literally one paste away from a full scan.
  useEffect(() => {
    if (notice && inputRef.current && !settingsOpen) inputRef.current.focus();
  }, [notice, settingsOpen]);

  useEffect(() => {
    if (!settingsOpen) return undefined;
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') setSettingsOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [settingsOpen]);

  function handleKeyDown(e) {
    if (e.key === 'Enter' && canTrigger) onTrigger();
  }

  if (settingsOpen) {
    return (
      <div className="idle idle--settings">
        <div className="idle__settings-header">
          <div>
            <div className="idle__settings-title">Settings</div>
            <div className="idle__settings-subtitle">Connections and macOS permissions</div>
          </div>
          <button
            type="button"
            className="idle__settings-close panel__interactive"
            onClick={() => setSettingsOpen(false)}
            title="Close settings"
            aria-label="Close settings"
          >
            &#10005;
          </button>
        </div>

        <div className="idle__setup panel__interactive" role="status">
          {!setupStatus && (
            <div className="idle__setup-row">
              <span>Setup status</span>
              <span className="idle__setup-warn">Checking...</span>
            </div>
          )}
          {setupStatus && (
            <>
              <div className="idle__setup-row">
                <span>LinkedIn session</span>
                {setupStatus.linkedin_authenticated ? (
                  <span className="idle__setup-ok">Connected</span>
                ) : (
                  <button
                    className="idle__setup-action"
                    onClick={onStartLinkedInLogin}
                    disabled={setupStatus.linkedin_login_running}
                  >
                    {setupStatus.linkedin_login_running ? 'Waiting for login...' : 'Connect'}
                  </button>
                )}
              </div>
              <div className="idle__setup-row">
                <span>Codex reviewer</span>
                <span className={setupStatus.codex_ready ? 'idle__setup-ok' : 'idle__setup-warn'}>
                  {setupStatus.codex_ready ? 'Ready' : 'ChatGPT app required'}
                </span>
              </div>
              <div className="idle__setup-row">
                <span>GitHub evidence</span>
                <span
                  className={
                    setupStatus.github_authenticated ? 'idle__setup-ok' : 'idle__setup-warn'
                  }
                >
                  {setupStatus.github_authenticated ? 'Connected' : 'Public-only'}
                </span>
              </div>
              {setupStatus.platform === 'darwin' && (
                <>
                  <div className="idle__setup-row">
                    <span>Browser companion</span>
                    {setupStatus.browser_companion_connected ? (
                      <span className="idle__setup-ok">Connected</span>
                    ) : setupStatus.browser_companion_installed ? (
                      <span className="idle__setup-ok">Installed</span>
                    ) : (
                      <button
                        className="idle__setup-action"
                        onClick={onOpenBrowserCompanionSetup}
                      >
                        Install extension
                      </button>
                    )}
                  </div>
                  <div className="idle__setup-row">
                    <span>Screen capture fallback</span>
                    {setupStatus.screen_recording === 'granted' ? (
                      <span className="idle__setup-ok">Granted</span>
                    ) : (
                      <button className="idle__setup-action" onClick={onOpenScreenSettings}>
                        Optional
                      </button>
                    )}
                  </div>
                </>
              )}
            </>
          )}
        </div>

        <div className="idle__settings-footnote">
          The companion shares only active LinkedIn profile links. Screen capture remains optional.
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="idle">
        {notice && (
          <div className="idle__notice idle__notice--needs-url" role="status">
            {notice}
          </div>
        )}
        <div className="idle__primary panel__interactive">
          <span
            className={'idle__status-dot' + engineDotClass(engineStatus)}
            title={(engineStatus && engineStatus.message) || ''}
            aria-hidden="true"
          />
          <button
            className="idle__scan-btn"
            onClick={onGoTrigger}
            disabled={goBusy}
            title="Scan the LinkedIn profile you're currently viewing, no paste needed"
            aria-label="Scan the profile you're currently viewing"
          >
            {goBusy && <span className="idle__scan-btn-spinner" aria-hidden="true" />}
            {goLabel(goBusy)}
          </button>
          <button
            type="button"
            className="idle__settings-button"
            onClick={() => setSettingsOpen(true)}
            title="Open settings"
            aria-label={
              setupNeedsAttention ? 'Open settings, setup needs attention' : 'Open settings'
            }
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path
                d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Zm8 3.5-.09-1.13 2.02-1.57-2-3.46-2.48 1a8.5 8.5 0 0 0-1.96-1.13L15.1 3h-4l-.39 2.71a8.5 8.5 0 0 0-1.96 1.13l-2.48-1-2 3.46 2.02 1.57L6.2 12l.09 1.13-2.02 1.57 2 3.46 2.48-1a8.5 8.5 0 0 0 1.96 1.13L11.1 21h4l.39-2.71a8.5 8.5 0 0 0 1.96-1.13l2.48 1 2-3.46-2.02-1.57L20 12Z"
              />
            </svg>
            {setupNeedsAttention && <span className="idle__settings-badge" aria-hidden="true" />}
          </button>
        </div>

        <div className="idle__secondary">
          <div className="idle__field panel__interactive">
            <input
              ref={inputRef}
              className="idle__input"
              type="text"
              placeholder="Or paste a profile URL"
              value={url}
              onChange={(e) => onUrlChange(e.target.value)}
              onKeyDown={handleKeyDown}
              spellCheck={false}
            />
            <button
              className="idle__go"
              onClick={onTrigger}
              disabled={!canTrigger}
              title="Run scan on the pasted URL"
              aria-label="Run scan on the pasted URL"
            >
              &#8594;
            </button>
          </div>
          <div className="idle__hint panel__interactive">
            <button
              className={'idle__chip' + (protectionEnabled ? ' idle__chip--on' : '')}
              onClick={onToggleProtection}
              title={
                protectionEnabled
                  ? 'Hidden from screen shares and recordings, click to reveal'
                  : 'Visible to screen shares and recordings, click to hide'
              }
              aria-label="Toggle screen recording visibility"
              aria-pressed={protectionEnabled}
            >
              {protectionEnabled ? 'HIDDEN' : 'VISIBLE'}
            </button>
            <button
              className="idle__chip"
              onClick={onMockTrigger}
              title="Play demo sequence (mock mode)"
              aria-label="Play demo sequence"
            >
              DEMO
            </button>
          </div>
        </div>

        <div className="idle__shortcut-hint">
          <kbd className="idle__kbd">{shortcutLabel()}</kbd>
          <span>opens LARP Detector and scans the LinkedIn profile you are viewing</span>
        </div>
      </div>

      {engineStatus && engineStatus.state === 'error' && (
        <div className="idle__notice">{engineStatus.message}</div>
      )}
    </>
  );
}
