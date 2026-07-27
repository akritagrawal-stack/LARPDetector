import { useState } from 'react';
import MeterBar from './MeterBar.jsx';

const TIER_CLASS = {
  DISPROVEN: 'receipt__dot--disproven',
  UNVERIFIED: 'receipt__dot--unverified',
  CONFIRMED: 'receipt__dot--confirmed'
};

// Highest-signal claims first: a disproven claim is the sharpest receipt, a
// confirmed one next, an unverified one last. The verdict shows its work.
const TIER_PRIORITY = { DISPROVEN: 0, CONFIRMED: 1, UNVERIFIED: 2 };

// The verdict word for the code-computed overall score.
function verdictTier(score) {
  if (score <= 33) return { word: 'CLEAR', mod: 'clear' };
  if (score <= 66) return { word: 'SUS', mod: 'sus' };
  return { word: 'LARP', mod: 'larp' };
}

function topReceipts(claims) {
  return [...claims]
    .sort((a, b) => (TIER_PRIORITY[a.tier] ?? 3) - (TIER_PRIORITY[b.tier] ?? 3))
    .slice(0, 3);
}

export default function VerdictView({
  founderScore,
  companyScore,
  overallScore,
  companyAssessments = [],
  verdictText,
  error,
  claims = [],
  sourceCount = 0,
  scanTarget,
  scanDepth = 'full',
  onReset
}) {
  const isShallow = scanDepth === 'shallow';
  const hasFounder = typeof founderScore === 'number';
  const hasCompany = typeof companyScore === 'number';
  const hasOverall = typeof overallScore === 'number';
  // A shallow scan's NUMBER is exactly the artifact the owner does not want
  // screenshotted as a real finding: suppress the dial/chip and show a badge
  // instead. The verdict text and receipts still render (a contradiction found
  // on a shallow scan is still real), but the score is never shown.
  const showScores = (hasOverall || hasFounder || hasCompany) && !isShallow;
  const hasScores = hasOverall || hasFounder || hasCompany;
  const effectiveOverall = hasOverall
    ? overallScore
    : Math.max(hasFounder ? founderScore : 0, hasCompany ? companyScore : 0);
  const tier = verdictTier(effectiveOverall);
  const receipts = topReceipts(claims);

  const [copied, setCopied] = useState(false);

  const copyVerdict = () => {
    const lines = [];
    lines.push('LARP Detector verdict' + (scanTarget ? ' for ' + scanTarget : ''));
    // An error verdict has no scores or verdict text, so without this the copy
    // would be just the header line and silently drop the actual failure.
    if (error) lines.push('', error);
    if (isShallow) {
      // Never let a shallow scan's number be copied out as a real score.
      lines.push('', 'SHALLOW SCAN (not a full check): score withheld.');
    } else {
      if (hasOverall) lines.push('Overall LARP: ' + Math.round(overallScore) + '/100');
      if (hasFounder) lines.push('Founder LARP: ' + Math.round(founderScore) + '/100');
      if (hasCompany) lines.push('Company LARP: ' + Math.round(companyScore) + '/100');
      if (companyAssessments.length) {
        lines.push('Company checks:');
        companyAssessments.forEach((assessment) => {
          const score =
            typeof assessment.larp_score === 'number'
              ? Math.round(assessment.larp_score) + '/100'
              : 'unscored';
          lines.push(
            '- ' +
              assessment.company_name +
              ': ' +
              score +
              ' (' +
              assessment.relationship +
              (assessment.affects_overall ? ', affects overall' : ', context only') +
              ')'
          );
        });
      }
      if (hasScores) lines.push('Verdict band: ' + tier.word);
    }
    if (verdictText) lines.push('', verdictText);
    if (receipts.length) {
      lines.push('', 'Receipts:');
      receipts.forEach((r) => lines.push('- [' + r.tier + '] ' + r.assertion));
    }
    if (sourceCount) lines.push('', sourceCount + ' sources checked');
    const text = lines.join('\n');

    if (window.overlay?.copyText) window.overlay.copyText(text);
    else if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 400);
  };

  return (
    <div className="verdict">
      <div className="verdict__header">
        <span className="verdict__label">VERDICT</span>
        <div className="verdict__header-actions">
          {showScores && (
            <span className={'verdict__chip verdict__chip--' + tier.mod}>{tier.word}</span>
          )}
          {isShallow && (
            <span className="verdict__chip verdict__chip--shallow">SHALLOW</span>
          )}
          <button
            className={'verdict__copy panel__interactive' + (copied ? ' verdict__copy--done' : '')}
            onClick={copyVerdict}
            title="Copy verdict summary"
          >
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button className="verdict__reset panel__interactive" onClick={onReset}>
            New scan
          </button>
        </div>
      </div>

      {showScores && (
        <div className="meters">
          <MeterBar label="Overall LARP" value={effectiveOverall} index={0} />
          <div className="score-breakdown" aria-label="Overall score breakdown">
            {hasFounder && (
              <div className="score-breakdown__item">
                <span>Founder</span>
                <strong>{Math.round(founderScore)}</strong>
              </div>
            )}
            {hasCompany && (
              <div className="score-breakdown__item">
                <span>Company</span>
                <strong>{Math.round(companyScore)}</strong>
              </div>
            )}
          </div>
          {companyAssessments.length > 1 && (
            <div className="company-breakdown">
              {companyAssessments.map((assessment) => (
                <span
                  className={
                    'company-breakdown__item' +
                    (assessment.affects_overall
                      ? ' company-breakdown__item--weighted'
                      : '')
                  }
                  key={assessment.company_name}
                  title={
                    assessment.affects_overall
                      ? 'Included in the company component'
                      : 'Checked for context, not included in the overall score'
                  }
                >
                  {assessment.company_name}{' '}
                  {typeof assessment.larp_score === 'number'
                    ? Math.round(assessment.larp_score)
                    : 'n/a'}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {isShallow && (
        <div className="verdict__shallow">
          <span className="verdict__shallow-title">SHALLOW SCAN, not a full check</span>
          <span className="verdict__shallow-why">
            The profile could not be fully read (no confirmed URL, an injected
            profile, or zero experience parsed), so no score is shown. Paste the
            exact profile URL and run New scan for a full check.
          </span>
        </div>
      )}

      {error ? (
        <div className="verdict__error">{error}</div>
      ) : (
        verdictText && <div className="verdict__text">{verdictText}</div>
      )}

      {!error && receipts.length > 0 && (
        <div className="receipts">
          <div className="receipts__head">
            <span>RECEIPTS</span>
            {sourceCount > 0 && (
              <span className="receipts__count">{sourceCount} sources checked</span>
            )}
          </div>
          {receipts.map((r, i) => (
            <div className="receipt" key={i}>
              <span className={'receipt__dot ' + (TIER_CLASS[r.tier] || 'receipt__dot--unverified')} />
              <span>{r.assertion}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
