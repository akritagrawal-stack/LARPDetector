import { useEffect, useRef, useState } from 'react';

const ANIM_MS = 900;
const STAGGER_MS = 140; // second meter's needle lands after the first

function prefersReducedMotion() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false;
}

// Mirrors --ok / --warn / --bad in styles.css so the numeric readout is the
// same color as the point on the track it corresponds to.
function valueColor(value) {
  if (value <= 33) return '#34D06B';
  if (value <= 66) return '#FFAA33';
  return '#FF5C4D';
}

// Horizontal LARP meter: a full-scale gradient track (green at low, red at
// high) revealed by clip-path, a soft glow underlay, a bright needle, zone
// ticks at 33/66, and an rAF count-up on the number so it and the bar arrive
// together. `index` staggers the second meter after the first.
export default function MeterBar({ label, value, index = 0 }) {
  const clamped = Math.max(0, Math.min(100, value));
  const reduced = prefersReducedMotion();
  const delay = index * STAGGER_MS;

  // The bar/needle animate via CSS transition: render at 0% for one painted
  // frame, then flip to the real value so there is a "from" state to sweep
  // away from. The number counts up over the same window via rAF.
  const [barVal, setBarVal] = useState(reduced ? clamped : 0);
  const [display, setDisplay] = useState(reduced ? Math.round(clamped) : 0);
  const [swept, setSwept] = useState(false);

  useEffect(() => {
    if (reduced) {
      setBarVal(clamped);
      setDisplay(Math.round(clamped));
      setSwept(false);
      return undefined;
    }

    setBarVal(0);
    setDisplay(0);
    setSwept(false);

    let raf1 = null;
    let raf2 = null;
    let countRaf = null;
    let sweepTimer = null;
    let start = null;

    // Double rAF guarantees the 0% frame paints before the target applies.
    raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => setBarVal(clamped));
    });

    const step = (now) => {
      if (start == null) start = now;
      const t = Math.min(1, Math.max(0, (now - start - delay) / ANIM_MS));
      // Cubic ease-out, matches the bar's --ease-out-expo closely enough.
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplay(Math.round(clamped * eased));
      if (t < 1) countRaf = requestAnimationFrame(step);
      else setDisplay(Math.round(clamped));
    };
    countRaf = requestAnimationFrame(step);

    // Fire the one-shot track sweep once the fill has settled.
    sweepTimer = setTimeout(() => setSwept(true), ANIM_MS + delay + 40);

    return () => {
      if (raf1) cancelAnimationFrame(raf1);
      if (raf2) cancelAnimationFrame(raf2);
      if (countRaf) cancelAnimationFrame(countRaf);
      if (sweepTimer) clearTimeout(sweepTimer);
    };
  }, [clamped, delay, reduced]);

  const trackStyle = { '--val': barVal + '%', '--meter-delay': delay + 'ms' };

  return (
    <div className="meter">
      <div className="meter__top">
        <span className="meter__name">{label}</span>
        <span className="meter__value" style={{ color: valueColor(clamped) }}>
          {display}
        </span>
      </div>
      <div
        className={'meter__track' + (swept ? ' meter__track--swept' : '')}
        style={trackStyle}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={clamped}
        aria-label={label}
      >
        <div className="meter__glow" aria-hidden="true" />
        <div className="meter__fill" />
        <span className="meter__tick" style={{ left: '33%' }} aria-hidden="true" />
        <span className="meter__tick" style={{ left: '66%' }} aria-hidden="true" />
        <div className="meter__marker" style={{ left: barVal + '%' }} />
      </div>
      <div className="meter__zones" aria-hidden="true">
        <span>CLEAR</span>
        <span>SUS</span>
        <span>LARP</span>
      </div>
    </div>
  );
}
