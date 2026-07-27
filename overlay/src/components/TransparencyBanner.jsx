// The in-app nudge for Windows' system-wide "Transparency effects" setting
// (see electron/main.js: checkWindowsTransparencyEnabled). Rendered only
// when main.js has confirmed via the registry that the setting is off, or,
// for the screenshot harness, when ?transparency=off is in the URL (see
// App.jsx), and only until the user dismisses it or successfully turns it
// on. It never appears on a guess, only on a real signal.
export default function TransparencyBanner({ onEnable, onDismiss }) {
  return (
    <div className="transparency-banner panel__interactive" role="status">
      <span className="transparency-banner__text">
        Windows transparency is off. Turn it on for the full glass look.
      </span>
      <div className="transparency-banner__actions">
        <button type="button" className="transparency-banner__enable" onClick={onEnable}>
          Enable
        </button>
        <button
          type="button"
          className="transparency-banner__dismiss"
          onClick={onDismiss}
          aria-label="Dismiss"
          title="Dismiss"
        >
          &#10005;
        </button>
      </div>
    </div>
  );
}
