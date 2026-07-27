import { useEffect, useRef, useState } from 'react';
import TypewriterText from './TypewriterText.jsx';

const TIER_CLASS = {
  DISPROVEN: 'claim__dot--disproven',
  UNVERIFIED: 'claim__dot--unverified',
  CONFIRMED: 'claim__dot--confirmed'
};

function domainFromUrl(rawUrl) {
  if (!rawUrl) return '';
  try {
    return new URL(rawUrl).hostname.replace(/^www\./, '');
  } catch {
    return rawUrl;
  }
}

// A real, loadable src is a non-empty trimmed string. Empty string, null,
// whitespace, or a non-string all count as "no image" so the monogram
// placeholder shows instead of an <img> that would render as a broken tile.
function hasSrc(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

// First readable letter for a monogram placeholder: prefer the caption, fall
// back to the scan target with any protocol/www prefix stripped.
function monogramFor(text, fallback) {
  const cleaned = (text || '').replace(/^https?:\/\/(www\.)?/i, '');
  const match = cleaned.match(/[a-z0-9]/i);
  return (match ? match[0] : fallback || 'S').toUpperCase();
}

// Which src an image feed item should actually load right now: its primary
// url normally, its `fallback` (a second, independently-reliable image for
// the same subject, e.g. a favicon-based logo riding along a Clearbit URL
// that may be dead; see detective/service.py) once the primary has errored,
// or '' once both have failed (the caller then shows the monogram). Never
// leaves an <img> pointed at a URL already known to be bad, which is what
// produced the odd cut-off broken-image-icon-plus-alt-text artifact: a plain
// <img src> with no live fallback sits there showing the browser's own
// broken-image glyph until React's onError round-trip catches up.
function pickSrc(item, triedFallback) {
  if (!item) return '';
  if (!triedFallback && hasSrc(item.url)) return item.url;
  if (hasSrc(item.fallback)) return item.fallback;
  return '';
}

// A favicon/Clearbit "logo" image is inherently low-res (see
// detective/images.py's favicon_logo_url: even at its largest requested
// size, 256, it is still a small icon, not a photograph). Stretching one to
// fill the hero's full-width 16:9 box via object-fit: cover is exactly what
// used to render it as a blurry upscaled blob. Applied as an inline style
// (not a stylesheet class) since overlay/src/styles.css is out of scope for
// this fix: this style OVERRIDES the .hero__img stylesheet rule's
// width/height:100%; object-fit:cover, centering the logo at a small,
// native-ish size on the hero's own card background instead. A "photo" kind
// (a real captured photo or og:image) gets no override and keeps the
// existing full-bleed cover treatment, which is safe for an actual photo.
const HERO_LOGO_IMG_STYLE = {
  position: 'absolute',
  top: '50%',
  left: '50%',
  transform: 'translate(-50%, -50%)',
  width: 'auto',
  height: 'auto',
  maxWidth: '88px',
  maxHeight: '88px',
  objectFit: 'contain'
};

// Same discipline for the small thumbnail strip: `.hero-strip__thumb` is
// already a flex-centered box in styles.css, so a "logo" thumbnail just
// needs to stop being forced to 100%/cover and instead sit at a capped,
// contained native-ish size within that box.
const THUMB_LOGO_IMG_STYLE = {
  width: 'auto',
  height: 'auto',
  maxWidth: '32px',
  maxHeight: '32px',
  objectFit: 'contain'
};

// The big cinematic stage: the current hero image event, shown large. Keyed
// by the image's feed id in the parent so each new arrival re-runs the
// spring pop. A failed or missing src (and a failed fallback) degrades to a
// glass monogram tile, never an empty box or a stray broken-image icon. The
// looping scan band on top keeps the "actively working" feel.
function HeroStage({ image, target, triedFallback, broken, onError }) {
  const src = image ? pickSrc(image, triedFallback) : '';
  const showImg = Boolean(src) && !broken;
  const isLogo = Boolean(image) && image.imageKind === 'logo';
  return (
    <div className="hero">
      {showImg ? (
        <img
          className="hero__img"
          src={src}
          alt=""
          onError={onError}
          style={isLogo ? HERO_LOGO_IMG_STYLE : undefined}
        />
      ) : (
        <div className="hero__placeholder">
          <span className="hero__monogram">
            {monogramFor(image ? image.caption : '', monogramFor(target, 'S'))}
          </span>
        </div>
      )}
      <div className="hero__scan" aria-hidden="true" />
      {image && image.caption && <div className="hero__caption">{image.caption}</div>}
    </div>
  );
}

// One small clickable source chip: the site's favicon (or letter monogram) in
// a squared glass tile. Opens the source in the real browser.
function SourceChip({ item, broken, onBroken }) {
  const domain = domainFromUrl(item.url);
  const openSource = () => {
    if (window.overlay?.openExternal) window.overlay.openExternal(item.url);
    else if (item.url) window.open(item.url, '_blank', 'noopener');
  };
  return (
    <button
      type="button"
      className="source-chip panel__interactive"
      onClick={openSource}
      title={item.title || item.url}
    >
      {hasSrc(item.favicon) && !broken ? (
        <img src={item.favicon} alt="" onError={onBroken} />
      ) : (
        <span className="source-chip__mono">{(domain[0] || '?').toUpperCase()}</span>
      )}
    </button>
  );
}

// Image-first live search. The newest image event is the big hero; earlier
// images recede into a small thumbnail strip beneath it, so the scan visibly
// "goes through photos". Only two text lines live under the stage (the latest
// status and the latest reasoning line); older reasoning is replaced rather
// than accumulated, so this never becomes a wall of bullets. Claims stay, but
// compact and secondary. Nothing here ever scrolls an ancestor: the claims
// list pins itself via its own scrollTop (scrollIntoView is banned in this
// view, it was what shoved the header off the top of the clipped panel).
export default function SearchingView({ target, latestStatus, feed, claims, onStop, reengageSignal }) {
  // Feed item ids whose PRIMARY url has already errored once: those retry
  // with `fallback` (see pickSrc above) instead of jumping straight to a
  // monogram. Feed item ids whose fallback ALSO errored (or that never had
  // one) land in brokenIds and finally degrade to a monogram.
  const [fallbackIds, setFallbackIds] = useState(() => new Set());
  const [brokenIds, setBrokenIds] = useState(() => new Set());
  const markBroken = (id) =>
    setBrokenIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  const markFallback = (id) =>
    setFallbackIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  // One onError handler for any image feed item: first failure tries the
  // fallback src (if any), second failure (or no fallback to try) degrades
  // to the monogram. Never leaves an <img> retrying an already-bad url.
  const onImageError = (item) => {
    if (!fallbackIds.has(item.id) && hasSrc(item.fallback)) {
      markFallback(item.id);
    } else {
      markBroken(item.id);
    }
  };

  const images = feed.filter((f) => f.kind === 'image');
  const sites = feed.filter((f) => f.kind === 'website');

  let lastThought = null;
  feed.forEach((f) => {
    if (f.kind === 'thought') lastThought = f;
  });

  // Only an image event the engine explicitly marked hero-eligible
  // (isHero !== false; see App.jsx's applyEvent) can become the big hero.
  // A thumbnail-only source (a per-employer logo, a source favicon, an
  // og:image) never competes for the hero slot, so it can only ever land in
  // the strip below. This is what stops the hero from flickering between a
  // company/employer logo and the profile photo as those thumbnail-only
  // events stream in: the hero only ever changes on a genuine new hero
  // event, never on an unrelated thumbnail arriving after it.
  const heroImages = images.filter((img) => img.isHero !== false);
  const hero = heroImages.length > 0 ? heroImages[heroImages.length - 1] : null;
  // Up to four most recent images that are NOT the current hero, newest
  // first, as the strip (thumbnail-only events plus any earlier heroes).
  const thumbs = images.filter((img) => img !== hero).slice(-4).reverse();

  // Keep the newest claim visible by scrolling ONLY the claims box itself
  // (element.scrollTop = element.scrollHeight, never scrollIntoView, which
  // would drag the sticky header/ancestors and clip the top). Runs on every
  // claim arrival so the latest evidence is always in view as it streams, and
  // again whenever the user re-engages auto-resize (reengageSignal bumps), so
  // the newest update is snapped back into view the instant auto sizing
  // resumes after a manual drag.
  const claimsRef = useRef(null);
  useEffect(() => {
    const el = claimsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [claims.length, reengageSignal]);

  // "Still working" phase cue. The searching view is only mounted while the
  // scan is live (it unmounts the instant a verdict/score/error flips the
  // phase), so a persistent status row here inherently means "not done yet".
  // Its label escalates to the scoring/reasoning state once the live feed goes
  // QUIET: after evidence gathering, the engine hands off to the reasoning step
  // (in operator mode it waits for the Claude operator to score, which can take
  // many seconds), during which nothing streams and the panel would otherwise
  // look frozen. We detect that lull purely from activity going idle, so it is
  // robust to the exact status wording. The threshold is generous so the normal
  // sub-second gaps between evidence arrivals never false-trigger "scoring".
  const activityKey = feed.length + '|' + claims.length + '|' + (latestStatus || '');
  const [scoring, setScoring] = useState(false);
  useEffect(() => {
    setScoring(false);
    const id = setTimeout(() => setScoring(true), 1400);
    return () => clearTimeout(id);
  }, [activityKey]);

  return (
    <div className="searching">
      <div className="searching__header">
        <span className="scan-dot" aria-hidden="true" />
        <span className="searching__target">{target || 'Scanning'}</span>
        <button
          type="button"
          className="searching__stop panel__interactive"
          onClick={onStop}
          aria-label="Stop this scan"
          title="Stop this scan"
        >
          <span className="searching__stop-icon" aria-hidden="true" />
          Stop
        </button>
      </div>

      <div className="searching__progress" aria-hidden="true" />

      <HeroStage
        key={hero ? hero.id : 'hero-placeholder'}
        image={hero}
        target={target}
        triedFallback={hero ? fallbackIds.has(hero.id) : false}
        broken={hero ? brokenIds.has(hero.id) : false}
        onError={hero ? () => onImageError(hero) : undefined}
      />

      {thumbs.length > 0 && (
        <div className="hero-strip">
          {thumbs.map((t) => {
            const thumbSrc = pickSrc(t, fallbackIds.has(t.id));
            const isLogo = t.imageKind === 'logo';
            return (
              <div className="hero-strip__thumb" key={t.id} title={t.caption || ''}>
                {thumbSrc && !brokenIds.has(t.id) ? (
                  <img
                    src={thumbSrc}
                    alt=""
                    onError={() => onImageError(t)}
                    style={isLogo ? THUMB_LOGO_IMG_STYLE : undefined}
                  />
                ) : (
                  <span className="hero-strip__mono">{monogramFor(t.caption, 'E')}</span>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="searching__lines">
        {latestStatus && (
          <div className="searching__ticker">
            <span className="log__caret">&gt;</span>
            <TypewriterText key={latestStatus} text={latestStatus} />
          </div>
        )}
        {lastThought && (
          <div className="searching__thought" key={lastThought.id}>
            <TypewriterText text={lastThought.text} maxDurationMs={620} />
          </div>
        )}
      </div>

      {sites.length > 0 && (
        <div className="source-strip">
          {sites.slice(-6).map((s) => (
            <SourceChip
              key={s.id}
              item={s}
              broken={brokenIds.has(s.id)}
              onBroken={() => markBroken(s.id)}
            />
          ))}
          <span className="source-strip__count">
            {sites.length} source{sites.length === 1 ? '' : 's'}
          </span>
        </div>
      )}

      {claims.length > 0 && (
        <div className="claims" ref={claimsRef}>
          {claims.map((c, i) => (
            <div className="claim" key={i}>
              <span className={'claim__dot ' + (TIER_CLASS[c.tier] || 'claim__dot--unverified')} />
              <span className="claim__text">{c.assertion}</span>
            </div>
          ))}
        </div>
      )}

      {/* Persistent "still working" status, always after the feed and OUTSIDE
          the claims scroll box, so it stays visible at MAX_HEIGHT. The label
          escalates to the scoring/reasoning wait once the feed goes quiet. */}
      <div
        className={'scan-status' + (scoring ? ' scan-status--scoring' : '')}
        role="status"
        aria-live="polite"
      >
        <span className="scan-status__spinner" aria-hidden="true" />
        <span className="scan-status__label">
          {scoring ? 'Scoring the evidence' : 'Gathering evidence'}
          <span className="scan-status__dots" aria-hidden="true" />
        </span>
      </div>
    </div>
  );
}
