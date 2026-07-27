// Hardcoded event sequence used by MOCK MODE. Mirrors the exact wire
// contract the real server streams over ws://127.0.0.1:8756/events/{job_id},
// so this file is the single thing standing in for a live backend.
// Every "delay" is milliseconds to wait AFTER the previous event before
// emitting this one, which is what drives the typewriter and reveal pacing.
//
// Includes the two forward-compatible event kinds the live-search feed
// understands (`website`, `thought`), so the demo sequence exercises the
// same picture-plus-website-card-plus-reasoning layout the real engine will
// use once it starts sending them. One website event below omits `favicon`
// on purpose, to exercise the letter-monogram fallback in SearchingView.

// All demo art is generated inline as SVG data URIs: bundled, offline-safe,
// zero network fetches, and bright enough to read as an actual picture on
// the dark glass (the old flat dark-navy tiles read as empty gray boxes).
function svgDataUri(svg) {
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}

// A portrait-style avatar "photo": warm sky gradient, soft vignette, light
// silhouette with shoulders. Reads as a LinkedIn headshot at thumbnail size.
const PROFILE_THUMB = svgDataUri(
  '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">' +
  '<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">' +
  '<stop offset="0" stop-color="#7FA8E8"/><stop offset="0.55" stop-color="#4A6FB0"/><stop offset="1" stop-color="#27406E"/>' +
  '</linearGradient></defs>' +
  '<rect width="120" height="120" fill="url(#sky)"/>' +
  '<circle cx="88" cy="20" r="26" fill="rgba(255,236,200,0.35)"/>' +
  '<circle cx="60" cy="46" r="20" fill="#F2E5D8"/>' +
  '<path d="M60 70c24 0 38 13 38 26v24H22V96c0-13 14-26 38-26Z" fill="#2E4A7A"/>' +
  '<path d="M60 70c24 0 38 13 38 26v24H60Z" fill="#243D66"/>' +
  '</svg>'
);

// A registration-filing "document scan": bright paper on a desk, text lines,
// a blue official seal. Unmistakably a picture, not a gray box.
const COMPANY_THUMB = svgDataUri(
  '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">' +
  '<defs><linearGradient id="desk" x1="0" y1="0" x2="1" y2="1">' +
  '<stop offset="0" stop-color="#3A4A63"/><stop offset="1" stop-color="#1E2A3E"/>' +
  '</linearGradient></defs>' +
  '<rect width="120" height="120" fill="url(#desk)"/>' +
  '<rect x="26" y="14" width="68" height="92" rx="4" fill="#F4F6FA"/>' +
  '<rect x="34" y="26" width="40" height="6" rx="2" fill="#33415C"/>' +
  '<rect x="34" y="40" width="52" height="4" rx="2" fill="#9AA7BC"/>' +
  '<rect x="34" y="50" width="52" height="4" rx="2" fill="#9AA7BC"/>' +
  '<rect x="34" y="60" width="44" height="4" rx="2" fill="#9AA7BC"/>' +
  '<rect x="34" y="70" width="52" height="4" rx="2" fill="#B9C3D4"/>' +
  '<circle cx="76" cy="90" r="10" fill="none" stroke="#2F5FD0" stroke-width="3"/>' +
  '<path d="M71 90l4 4 7-8" fill="none" stroke="#2F5FD0" stroke-width="2.4"/>' +
  '</svg>'
);

// Favicon-style site marks: rounded square + bold monogram, like the real
// 16px favicons a live scan shows.
function faviconSvg(bg, letter) {
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">' +
    '<rect width="32" height="32" rx="7" fill="' + bg + '"/>' +
    '<text x="16" y="22.5" text-anchor="middle" font-family="Arial, sans-serif" font-size="17" font-weight="700" fill="#FFFFFF">' + letter + '</text>' +
    '</svg>';
  return svgDataUri(svg);
}

// A GitHub-org-avatar style tile: dark card, rounded identicon block, and a
// contribution graph that is conspicuously sparse. Reads as "we pulled their
// GitHub" at hero size.
const GITHUB_THUMB = svgDataUri(
  '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="120">' +
  '<rect width="160" height="120" fill="#171B26"/>' +
  '<rect x="14" y="14" width="44" height="44" rx="8" fill="#232A3C"/>' +
  '<rect x="22" y="22" width="12" height="12" fill="#8AA6FF"/>' +
  '<rect x="38" y="22" width="12" height="12" fill="#3D4A6B"/>' +
  '<rect x="22" y="38" width="12" height="12" fill="#3D4A6B"/>' +
  '<rect x="38" y="38" width="12" height="12" fill="#8AA6FF"/>' +
  '<rect x="68" y="18" width="60" height="8" rx="3" fill="#93A1BC"/>' +
  '<rect x="68" y="34" width="42" height="6" rx="3" fill="#4A566E"/>' +
  '<g>' +
  '<rect x="14" y="76" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="28" y="76" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="42" y="76" width="10" height="10" rx="2" fill="#2EA05A"/>' +
  '<rect x="56" y="76" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="70" y="76" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="84" y="76" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="98" y="76" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="112" y="76" width="10" height="10" rx="2" fill="#2EA05A" opacity="0.5"/>' +
  '<rect x="126" y="76" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="14" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="28" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="42" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="56" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="70" y="92" width="10" height="10" rx="2" fill="#2EA05A" opacity="0.35"/>' +
  '<rect x="84" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="98" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="112" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '<rect x="126" y="92" width="10" height="10" rx="2" fill="#22304A"/>' +
  '</g>' +
  '</svg>'
);

// A company "team page" screenshot: header bar, two real avatar cards, and a
// row of dashed empty slots where the other 38 people should be.
const TEAM_THUMB = svgDataUri(
  '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="120">' +
  '<rect width="160" height="120" fill="#F0F2F7"/>' +
  '<rect width="160" height="22" fill="#22304A"/>' +
  '<rect x="10" y="7" width="34" height="8" rx="3" fill="#8AA6FF"/>' +
  '<rect x="12" y="34" width="52" height="8" rx="3" fill="#33415C"/>' +
  '<circle cx="34" cy="70" r="14" fill="#7FA8E8"/>' +
  '<rect x="20" y="88" width="28" height="5" rx="2" fill="#6B7893"/>' +
  '<circle cx="80" cy="70" r="14" fill="#E8A87F"/>' +
  '<rect x="66" y="88" width="28" height="5" rx="2" fill="#6B7893"/>' +
  '<circle cx="126" cy="70" r="14" fill="none" stroke="#B7C0D2" stroke-width="2" stroke-dasharray="4 4"/>' +
  '<rect x="112" y="88" width="28" height="5" rx="2" fill="#D4DAE5"/>' +
  '</svg>'
);

const LINKEDIN_FAVICON = faviconSvg('#0A66C2', 'in');
const CRUNCHBASE_FAVICON = faviconSvg('#146AFF', 'cb');
const HN_FAVICON = faviconSvg('#FF6600', 'Y');

export const MOCK_JOB_ID = 'mock-job-0001';

// Delays tuned brisk (roughly 300 to 700ms between arrivals) so the
// progressive reveal in SearchingView (an image pops in, then the next
// website card or thought line slides and types in under it) plays at a
// pace that reads as "actively working" rather than sluggish, while still
// leaving enough of a beat between arrivals to prove, screenshot to
// screenshot, that items are staged in one or two at a time, not dumped in
// all at once.
export const MOCK_EVENTS = [
  { delay: 300, event: { type: 'status', text: 'Reading LinkedIn profile...' } },
  {
    delay: 450,
    event: { type: 'website', url: 'https://www.linkedin.com/in/example', title: 'Alex Rivera - Founder & CEO', favicon: LINKEDIN_FAVICON }
  },
  {
    delay: 600,
    event: { type: 'image', url: PROFILE_THUMB, caption: "LinkedIn banner: 'Ex-Google, Stealth Founder'" }
  },
  {
    delay: 600,
    event: { type: 'thought', text: 'Profile reads polished. Cross-checking the headline claims against public records.' }
  },
  { delay: 550, event: { type: 'status', text: 'Checking Crunchbase funding history...' } },
  {
    delay: 500,
    event: { type: 'website', url: 'https://www.crunchbase.com/organization/example-inc', title: 'Example Inc. - Crunchbase', favicon: CRUNCHBASE_FAVICON }
  },
  {
    // Lands ~60ms after the row above (a burst): exercises the cascade
    // stagger in SearchingView, where near-simultaneous rows fan in instead
    // of thudding in together.
    delay: 60,
    event: { type: 'website', url: 'https://news.ycombinator.com/item?id=example', title: 'Show HN: Example Inc (2 points, 0 comments)', favicon: HN_FAVICON }
  },
  {
    // Second image: the hero updates and the profile photo recedes into the
    // thumbnail strip (the "going through photos" cascade).
    delay: 500,
    event: { type: 'image', url: GITHUB_THUMB, caption: 'GitHub org: 2 public repos, 3 commits total' }
  },
  {
    delay: 350,
    event: { type: 'claim', assertion: "Claims a $2M pre-seed led by 'a Sequoia scout'", tier: 'UNVERIFIED' }
  },
  {
    delay: 650,
    event: { type: 'claim', assertion: "'Ex-Google SWE': no Google email, badge, or commit history found", tier: 'DISPROVEN' }
  },
  {
    delay: 600,
    event: { type: 'image', url: COMPANY_THUMB, caption: 'Company registration filing' }
  },
  {
    delay: 600,
    event: { type: 'thought', text: 'Filing itself is genuine. Team headcount looks inflated next to what is publicly findable.' }
  },
  { delay: 600, event: { type: 'status', text: 'Scanning GitHub contribution graph...' } },
  {
    // No favicon on purpose: exercises the letter-monogram fallback chip.
    delay: 500,
    event: { type: 'website', url: 'https://github.com/example-inc' }
  },
  {
    // Fourth image: by now the strip shows three earlier finds under the hero.
    delay: 550,
    event: { type: 'image', url: TEAM_THUMB, caption: "Team page: 2 employees findable, not '40'" }
  },
  {
    delay: 500,
    event: { type: 'claim', assertion: 'Delaware C-corp registered 3 weeks ago, filing is genuine', tier: 'CONFIRMED' }
  },
  {
    delay: 650,
    event: { type: 'claim', assertion: "'40 person team': 2 employees findable on LinkedIn", tier: 'DISPROVEN' }
  },
  { delay: 600, event: { type: 'status', text: 'Weighing evidence, compiling verdict...' } },
  {
    delay: 650,
    event: {
      type: 'scores',
      founder_larp_score: 82,
      company_larp_score: 61,
      overall_larp_score: 74,
      company_assessments: [
        {
          company_name: 'Acme',
          relationship: 'founder',
          affects_overall: true,
          larp_score: 61
        }
      ]
    }
  },
  {
    delay: 500,
    event: {
      type: 'verdict',
      text:
        "This founder's LinkedIn is doing more startup theater than the startup is. " +
        "The funding round is real, the halo painted around it is not, and the " +
        "'40 person team' is two guys and a Canva deck. Company checks out on paper. " +
        "The man in front of it is mostly vibes."
    }
  },
  { delay: 300, event: { type: 'done' } }
];

// ---- Error-state fixtures (capture harness / manual QA) ----------------------
// Driven directly idle -> verdict (no searching phase first), the exact path a
// Go-button failure takes, so the verdict error sizing is proven from the
// default state.

// The verbatim string the engine streams on a keyless / unreadable screenshot
// scan. Passing this through the app proves the friendlyError mapping rewrites
// it into an actionable, on-screen-affordance message (see App.friendlyError).
export const MOCK_ERROR_ENGINE =
  'could not read a LinkedIn profile from your screen; paste the profile URL ' +
  'instead, or try again once the queued screenshot job is completed.';

// A deliberately long error that does NOT match the mapping, so it renders
// verbatim, and it embeds one very long unbreakable token (a pasted URL) to
// prove overflow-wrap:anywhere breaks it inside the glass and the max-height
// cap scrolls rather than overflowing or clipping the panel.
export const MOCK_ERROR_LONG =
  'Lost connection to the scan service while reading the profile, then the ' +
  'retry also failed before any evidence came back. The engine was still ' +
  'waiting on this source when the socket dropped: ' +
  'https://www.linkedin.com/in/this-is-a-deliberately-long-unbreakable-token-to-exercise-overflow-wrap-anywhere-and-the-internal-scroll-cap-0123456789abcdefghijklmnopqrstuvwxyz ' +
  'Click New scan and paste the profile URL to try the check again.';

// A scoring-wait fixture: a brisk evidence burst, then a CLAIM-HEAVY list (nine
// claims, several two-line, so the claims box overflows its max-height and the
// newest-claim auto-scroll is exercised while the sticky header stays put),
// then a long pause on "Weighing evidence" before scores land. That pause
// stands in for the operator-scoring wait (many seconds in operator mode), so
// the harness can capture the persistent "Scoring the evidence" status that
// proves the panel is not frozen during the reasoning handoff. Its own mode
// (?mock=scoring) so it never perturbs the shared MOCK_EVENTS timeline.
export const MOCK_SCORING_EVENTS = [
  { delay: 300, event: { type: 'status', text: 'Reading LinkedIn profile...' } },
  { delay: 400, event: { type: 'image', url: PROFILE_THUMB, caption: "LinkedIn banner: 'Ex-Google, Stealth Founder'" } },
  { delay: 400, event: { type: 'website', url: 'https://www.linkedin.com/in/example', title: 'Alex Rivera - Founder & CEO', favicon: LINKEDIN_FAVICON } },
  { delay: 350, event: { type: 'claim', assertion: "Claims a $2M pre-seed led by 'a Sequoia scout'", tier: 'UNVERIFIED' } },
  { delay: 350, event: { type: 'claim', assertion: "'Ex-Google SWE': no Google email, badge, or commit history found", tier: 'DISPROVEN' } },
  { delay: 350, event: { type: 'claim', assertion: 'Delaware C-corp registered 3 weeks ago, filing is genuine', tier: 'CONFIRMED' } },
  { delay: 400, event: { type: 'website', url: 'https://www.crunchbase.com/organization/example-inc', title: 'Example Inc. - Crunchbase', favicon: CRUNCHBASE_FAVICON } },
  { delay: 350, event: { type: 'claim', assertion: "'40 person team': 2 employees findable on LinkedIn", tier: 'DISPROVEN' } },
  { delay: 350, event: { type: 'image', url: GITHUB_THUMB, caption: 'GitHub org: 2 public repos, 3 commits total' } },
  { delay: 350, event: { type: 'claim', assertion: "'YC S21' badge on the site does not match any public YC batch listing", tier: 'DISPROVEN' } },
  { delay: 350, event: { type: 'claim', assertion: 'Domain registered same week as the C-corp, WHOIS privacy on', tier: 'UNVERIFIED' } },
  { delay: 350, event: { type: 'claim', assertion: "'Featured in TechCrunch' link points to a paid press-release wire, not editorial", tier: 'DISPROVEN' } },
  { delay: 350, event: { type: 'claim', assertion: 'Two co-founder LinkedIn profiles created within the last month', tier: 'UNVERIFIED' } },
  { delay: 350, event: { type: 'claim', assertion: 'Product demo video is a Figma prototype recording, not a live app', tier: 'DISPROVEN' } },
  { delay: 500, event: { type: 'status', text: 'Weighing evidence, compiling verdict...' } },
  // The long scoring/reasoning wait: no events stream for ~9s, which is what
  // drives the "Scoring the evidence" status the scoring shot captures.
  {
    delay: 9000,
    event: {
      type: 'scores',
      founder_larp_score: 84,
      company_larp_score: 63,
      overall_larp_score: 76,
      company_assessments: [
        {
          company_name: 'Acme',
          relationship: 'founder',
          affects_overall: true,
          larp_score: 63
        }
      ]
    }
  },
  {
    delay: 500,
    event: {
      type: 'verdict',
      text:
        "This founder's LinkedIn is doing more startup theater than the startup is. " +
        'The C-corp is real; almost everything painted around it is not.'
    }
  },
  { delay: 300, event: { type: 'done' } }
];

// A searching sequence whose media all fails to load or is missing, to prove
// FeedVisual never renders an empty tile: every image/website degrades to a
// letter monogram (image load error, website favicon load error, empty url,
// and missing favicon). The bad hosts do not resolve, firing onError; the
// http one also proves the widened img-src CSP does not hard-block http.
export const MOCK_BROKEN_EVENTS = [
  { delay: 250, event: { type: 'status', text: 'Reading LinkedIn profile...' } },
  {
    delay: 350,
    event: { type: 'image', url: 'https://media.licdn.com/dms/image/does-not-resolve.jpg', caption: 'profile photo' }
  },
  {
    delay: 350,
    event: { type: 'website', url: 'https://www.crunchbase.com/organization/example-inc', title: 'Example Inc. - Crunchbase', favicon: 'http://invalid.example.invalid/favicon.ico' }
  },
  {
    delay: 350,
    event: { type: 'image', url: '', caption: 'Evidence thumbnail (no url)' }
  },
  {
    delay: 350,
    event: { type: 'website', url: 'https://news.ycombinator.com/item?id=example', title: 'Show HN: Example Inc' }
  }
];
