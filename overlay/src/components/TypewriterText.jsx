import { useEffect, useState } from 'react';

function prefersReducedMotion() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false;
}

// Reveals `text` character by character. Used for the single most recent
// status line, and for each website-card title / thought line in the
// evidence feed, so the panel reads like it is actively typing out what it
// just found, rather than a plain list appearing all at once. Renders the
// full text immediately, with no per-character reveal, when the system
// asks for reduced motion.
export default function TypewriterText({ text, speedMs = 10, maxDurationMs = 900 }) {
  const [shown, setShown] = useState(prefersReducedMotion() ? text || '' : '');

  useEffect(() => {
    if (prefersReducedMotion()) {
      setShown(text || '');
      return undefined;
    }

    setShown('');
    if (!text) return undefined;

    const stepMs = Math.max(4, Math.min(speedMs, maxDurationMs / Math.max(1, text.length)));
    let i = 0;
    const id = setInterval(() => {
      i += 1;
      setShown(text.slice(0, i));
      if (i >= text.length) clearInterval(id);
    }, stepMs);

    return () => clearInterval(id);
  }, [text, speedMs, maxDurationMs]);

  const done = shown.length >= (text || '').length;

  return (
    <>
      {shown}
      {!done && <span className="log__cursor" aria-hidden="true" />}
    </>
  );
}
