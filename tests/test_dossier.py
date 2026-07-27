"""Offline tests for the aggregate-then-mismatch path (detective.dossier).

All tests are pure/offline: the mechanical detectors run on hand-built Claim
objects, and the build_dossier end-to-end tests patch verify.gather_evidence
with a deterministic offline fake and use an in-process stub provider. No
network, and NEVER a live LinkedIn fetch. No em dashes (house rule).
"""

from __future__ import annotations

import pytest

from detective import verify
from detective import dossier as dossier_mod
from detective.dossier import (
    MismatchFinding,
    build_claimed_set,
    build_dossier,
    detect_contradiction,
    detect_gap,
    detect_inflation,
    detect_technical_authenticity,
    detect_timeline,
    inject_candidates,
    parse_app_store_rating_count,
    parse_dollar_amount,
    parse_month_year,
    parse_quantity,
    parse_rating_count,
    run_detectors,
)
from detective.llm import LLMProvider, compute_founder_score, mechanical_decompose, mechanical_decompose_company
from detective.models import Claim, Dossier, EvidenceTier
from detective import pipeline


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("50k users", 50_000),
        ("1.2M downloads", 1_200_000),
        ("raised $10 million", 10_000_000),
        ("50,000", 50_000),
        ("2.5B", 2_500_000_000),
        ("a team of 12", 12),
        ("no number here", None),
        ("", None),
    ],
)
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


def test_parse_rating_count():
    assert parse_rating_count("The app has 12 ratings on the App Store.") == 12
    assert parse_rating_count("1,204 reviews and counting") == 1204
    assert parse_rating_count("no counts here") is None


def test_parse_dollar_amount_takes_largest():
    snippet = "Form D: offering amount $2,000,000; a $50 filing fee applied."
    assert parse_dollar_amount(snippet) == 2_000_000
    assert parse_dollar_amount("raised $5 million in a seed round") == 5_000_000
    assert parse_dollar_amount("no dollars") is None


def test_parse_month_year():
    assert parse_month_year("Jan 2020") == (2020, 1)
    assert parse_month_year("2016") == (2016, 1)
    assert parse_month_year("Present") is None
    assert parse_month_year("") is None
    assert parse_month_year("Dec 2019") == (2019, 12)


# ---------------------------------------------------------------------------
# CONTRADICTION detector
# ---------------------------------------------------------------------------


def test_detect_contradiction_flags_adverse_finding():
    claim = Claim(
        type="employment",
        employer="Goldman Sachs",
        title="Analyst",
        assertion="Worked as Analyst at Goldman Sachs.",
        evidence=[
            {
                "source_url": "https://news.test/a",
                "snippet": "Goldman Sachs has no record of him ever being employed there.",
                "source_name": "",
                "match_confidence": "medium",
            }
        ],
    )
    findings = detect_contradiction([claim])
    assert len(findings) == 1
    assert findings[0].kind == "CONTRADICTION"
    assert findings[0].claim_indices == [0]


def test_detect_contradiction_ignores_courtlistener_only():
    claim = Claim(
        type="identity",
        assertion="A real person named Jane Doe exists.",
        evidence=[
            {
                "source_url": "https://courtlistener.test/x",
                "snippet": "Docket: no record of a settlement in this matter.",
                "source_name": "courtlistener",
                "match_confidence": "low",
            }
        ],
    )
    # The phrase "no record of" appears, but a courtlistener-only hit must not
    # be surfaced as a contradiction candidate.
    assert detect_contradiction([claim]) == []


def test_detect_contradiction_silent_on_clean_evidence():
    claim = Claim(
        type="employment",
        employer="Acme",
        assertion="Worked at Acme.",
        evidence=[{"source_url": "https://acme.test", "snippet": "Acme is a real company."}],
    )
    assert detect_contradiction([claim]) == []


# ---------------------------------------------------------------------------
# INFLATION detector
# ---------------------------------------------------------------------------


def test_app_store_ratings_are_not_treated_as_measured_users():
    claim = Claim(
        type="user_count",
        employer="MyApp",
        title="users",
        assertion="MyApp claims 50,000 users.",
        evidence=[
            {
                "source_url": "https://itunes.test/app",
                "snippet": "MyApp has 12 ratings on the App Store.",
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
            }
        ],
    )
    findings = detect_inflation([claim])
    assert findings == []


def test_detect_inflation_funding_vs_form_d():
    claim = Claim(
        type="funding",
        employer="MyCo",
        assertion="MyCo claims to have raised $50 million.",
        evidence=[
            {
                "source_url": "https://sec.test/formd",
                "snippet": "Form D offering amount $500,000 sold to 3 investors.",
                "source_name": "sec_edgar_form_d",
                "match_confidence": "high",
            }
        ],
    )
    findings = detect_inflation([claim])
    assert len(findings) == 1
    assert findings[0].kind == "INFLATION"  # 50M vs 500k = 100x gap


def test_detect_inflation_no_discovered_number_is_not_inflation():
    # A big claim with NO discovered measurement is a GAP, not an inflation:
    # absence must never masquerade as a proven overstatement.
    claim = Claim(
        type="user_count",
        employer="MyApp",
        title="users",
        assertion="MyApp claims 50,000 users.",
        evidence=[{"source_url": "https://g.test", "snippet": "generic result, no app store data"}],
    )
    assert detect_inflation([claim]) == []


def test_detect_inflation_normal_rounding_not_flagged():
    # 100k claimed vs 80k discovered is < 10x: normal optimism, not flagged.
    claim = Claim(
        type="user_count",
        employer="MyApp",
        title="users",
        assertion="MyApp claims 100,000 users.",
        evidence=[
            {
                "source_url": "https://itunes.test/app",
                "snippet": "MyApp has 80000 ratings on the App Store.",
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
            }
        ],
    )
    assert detect_inflation([claim]) == []


# ---------------------------------------------------------------------------
# Feature 1: App Store TRACTION cross-check reliability + no-false-positive
# ---------------------------------------------------------------------------


def test_parse_app_store_rating_count_reads_listing_ignores_reviews_fetched():
    """The rating-specific parser reads the real userRatingCount out of a
    LISTING snippet ("N total rating(s)" / "N ratings") but returns None for a
    reviews-activity record ("N review(s) fetched", a <=50 feed cap that says
    "star", never "rating") so the feed cap is never mistaken for a footprint.
    """
    assert parse_app_store_rating_count("88542 total rating(s), average 4.78 stars") == 88542
    assert parse_app_store_rating_count("The app has 12 ratings on the App Store.") == 12
    # The reviews-activity record: NO "rating" word -> not a footprint number.
    assert parse_app_store_rating_count(
        "Customer Reviews RSS for 'X': 50 review(s) fetched from the feed, 3 star review"
    ) is None
    assert parse_app_store_rating_count("no counts here") is None


def test_detect_inflation_uses_listing_rating_not_reviews_fetched():
    """The core Feature 1 fix: with BOTH a listing record (real 88542 rating
    count, the store footprint) and a reviews record ("50 review(s) fetched",
    the feed cap), the cross-check must use 88542, not 50, so a healthy app
    claiming 50,000 users does NOT falsely read as inflated (50000/88542 < 1).
    Before the fix the listing rating count was unparseable and the check fell
    to the 50-cap, computing a bogus 1000x inflation.
    """
    claim = Claim(
        type="user_count",
        employer="Notion",
        title="users",
        assertion="Notion claims 50,000 users.",
        evidence=[
            {
                "source_url": "https://apps.apple.com/app/id1",
                "snippet": (
                    "App Store listing for 'Notion': 88542 total rating(s), average 4.78 stars. "
                    "TRACTION SIGNAL: userRatingCount is 88542 rating(s)."
                ),
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
            },
            {
                "source_url": "https://itunes.apple.com/us/rss/customerreviews/id1",
                "snippet": "Customer Reviews RSS for 'Notion': 50 review(s) fetched from the feed, 40 within the last 90 days. 5 star.",
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
            },
        ],
    )
    assert detect_inflation([claim]) == []


def test_tiny_rating_footprint_is_not_a_user_count_measurement():
    """Ratings are a weak reach proxy, not a measured count of users."""
    claim = Claim(
        type="user_count",
        employer="GhostApp",
        title="users",
        assertion="GhostApp claims 50,000 users.",
        evidence=[
            {
                "source_url": "https://apps.apple.com/app/id2",
                "snippet": (
                    "App Store listing for 'GhostApp': 12 total rating(s), average 5.00 stars. "
                    "TRACTION SIGNAL: userRatingCount is 12 rating(s)."
                ),
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
            }
        ],
    )
    findings = detect_inflation([claim])
    assert findings == []


def test_detect_inflation_pulled_app_no_penalty():
    """Pulled / delisted guard: a not-found or removed app yields NO app_store
    evidence at all, so a big user_count claim produces no discovered number
    and NO inflation finding (it falls through to unverified, never an
    auto-penalty). Low visibility explainable by delisting is not a lie.
    """
    claim = Claim(
        type="user_count",
        employer="PulledApp",
        title="users",
        assertion="PulledApp claims 500,000 users.",
        evidence=[
            {"source_url": "https://g.test", "snippet": "generic web result, no app store listing found"}
        ],
    )
    assert detect_inflation([claim]) == []


# ---------------------------------------------------------------------------
# Feature 2: technical authenticity detector (SUS only, hard-gated)
# ---------------------------------------------------------------------------


def _github_ev(match_confidence: str, read: str) -> dict:
    return {
        "source_url": "https://github.com/someone",
        "snippet": (
            f"GitHub account 'someone' created 2024-01-01T00:00:00Z, 1 public repo(s). "
            f"Technical authenticity read: {read} (0 original repo(s), 1 fork(s), 0 star(s) "
            f"across originals, languages: none detected, account 1.0y old)."
        ),
        "source_name": "github",
        "weight": 1.0,
        "match_confidence": match_confidence,
    }


def test_tech_authenticity_fires_on_loud_claim_with_thin_github():
    """A LOUD technical/builder claim (proprietary AI, built by a technical
    cofounder) with NO confirmed substantial code footprint is a SUS tell: the
    detector must fire, as an absence-shaped finding (never DISPROVEN)."""
    claim = Claim(
        type="employment",
        employer="StartupX",
        title="Technical Cofounder",
        assertion="Built the proprietary AI engine as technical cofounder at StartupX.",
        evidence=[_github_ev("low", "thin-or-absent")],
    )
    findings = detect_technical_authenticity([claim])
    assert len(findings) == 1
    assert findings[0].kind == "TECH_AUTHENTICITY"
    assert findings[0].claim_indices == [0]


def test_tech_authenticity_silent_on_non_technical_ceo():
    """HARD GATE: a non-technical CEO/founder who never claimed to code is NEVER
    touched by this signal, even with a thin github namesake in evidence."""
    claim = Claim(
        type="employment",
        employer="StartupX",
        title="Chief Executive Officer",
        assertion="Worked as Chief Executive Officer at StartupX.",
        evidence=[_github_ev("low", "thin-or-absent")],
    )
    assert detect_technical_authenticity([claim]) == []


def test_tech_authenticity_cleared_by_confirmed_substantial_github():
    """A CONFIRMED-substantial GitHub account (high match_confidence,
    authenticity 'substantial') clears the tell: a real engineer's claim checks
    out, no finding."""
    claim = Claim(
        type="employment",
        employer="StartupX",
        title="Founding Engineer",
        assertion="I built the entire backend as founding engineer at StartupX.",
        evidence=[_github_ev("high", "substantial")],
    )
    assert detect_technical_authenticity([claim]) == []


def test_tech_authenticity_namesake_substantial_does_not_clear():
    """MATCH DISCIPLINE: a namesake (low match_confidence) 'substantial' account
    does NOT clear the tell (its repos are not confirmably this person's), so a
    loud technical claim with only a namesake still fires."""
    claim = Claim(
        type="employment",
        employer="StartupX",
        title="Technical Cofounder",
        assertion="Built our proprietary AI as technical cofounder.",
        evidence=[_github_ev("low", "substantial")],
    )
    findings = detect_technical_authenticity([claim])
    assert len(findings) == 1 and findings[0].kind == "TECH_AUTHENTICITY"


def test_tech_authenticity_silent_when_no_search_ran():
    """"We did not look" is never a tell: a loud technical claim with no
    evidence at all produces no finding."""
    claim = Claim(
        type="employment",
        employer="StartupX",
        title="Technical Cofounder",
        assertion="Built the proprietary AI engine.",
        evidence=[],
    )
    assert detect_technical_authenticity([claim]) == []


def test_tech_authenticity_injected_record_is_absence_shaped_sus_only():
    """The injected record must be an ABSENCE flag: low confidence, low weight,
    carrying the 'can never support DISPROVEN' disclaimer, appended (not
    prepended)."""
    claim = Claim(
        type="employment",
        employer="StartupX",
        title="Technical Cofounder",
        assertion="Built the proprietary AI engine.",
        evidence=[_github_ev("low", "thin-or-absent")],
    )
    findings = detect_technical_authenticity([claim])
    inject_candidates([claim], findings)
    injected = [e for e in claim.evidence if (e.get("source_name") or "") == "mismatch_tech_authenticity"]
    assert len(injected) == 1
    rec = injected[0]
    assert rec["match_confidence"] == "low"
    assert rec["weight"] <= 0.2
    assert "can NEVER support" in rec["snippet"]
    # Appended, not prepended: the real github record stays first.
    assert claim.evidence[0]["source_name"] == "github"


def test_tech_authenticity_lift_rides_feature3_sus():
    """Demo (b) end-to-end (deterministic): a loud technical claim marked
    UNVERIFIED + high footprint (what a disciplined provider does on the
    injected tech-authenticity record) reaches SUS on its own, while a
    non-technical CEO's low-footprint UNVERIFIED claim stays CLEAR. This proves
    the Feature 2 lift rides Feature 3's SUS formula with no scorer change."""
    ev = [{"source_url": "http://x", "snippet": "no confirmed substantial code footprint"}]
    tech_claim = [
        Claim(type="employment", employer="StartupX", title="Technical Cofounder",
              assertion="Built the proprietary AI engine.",
              tier=EvidenceTier.UNVERIFIED, expected_footprint="high", evidence=ev)
    ]
    ceo_claim = [
        Claim(type="employment", employer="StartupX", title="CEO",
              assertion="Worked as CEO at StartupX.",
              tier=EvidenceTier.UNVERIFIED, expected_footprint="low", evidence=ev)
    ]
    assert compute_founder_score(tech_claim) >= 34
    assert compute_founder_score(ceo_claim) <= 33


# ---------------------------------------------------------------------------
# GAP detector (SUS only, never DISPROVEN)
# ---------------------------------------------------------------------------


def test_detect_gap_flags_uncorroborated_notable_claim():
    claim = Claim(
        type="employment",
        employer="Google",
        title="VP of Engineering",
        assertion="Worked as VP of Engineering at Google.",
        evidence=[
            {"source_url": "https://en.wikipedia.org/wiki/Google", "snippet": "Google is a company."},
            {"source_url": "https://careers.google.com", "snippet": "Generic careers page, no names."},
        ],
    )
    findings = detect_gap([claim])
    assert len(findings) == 1
    assert findings[0].kind == "GAP"
    # A GAP is an absence: no discovered side, and it must not carry a
    # contradiction/inflation kind that could reach DISPROVEN downstream.
    assert findings[0].discovered == ""


def test_detect_gap_silent_when_corroborated():
    claim = Claim(
        type="employment",
        employer="Google",
        assertion="Worked at Google.",
        evidence=[
            {
                "source_url": "https://github.com/x",
                # A CONFIRMED account whose code footprint reads substantial:
                # the only github shape that speaks to a claimed role (a
                # "medium" name-handle match or a confirmed-but-thin account
                # substantiates nothing, see the workstream 1 tests below).
                "snippet": (
                    "Real aged GitHub account, company field says Google. "
                    "Technical authenticity read: substantial (14 original repo(s))."
                ),
                "source_name": "github",
                "match_confidence": "high",
            }
        ],
    )
    assert detect_gap([claim]) == []


def test_detect_gap_silent_when_no_search_ran():
    # Empty evidence means we never looked: "did not search" must never read as
    # SUS (same discipline as compute_founder_score's evidence gate).
    claim = Claim(type="employment", employer="Google", assertion="Worked at Google.", evidence=[])
    assert detect_gap([claim]) == []


def test_gap_resolves_to_sus_not_disproven_end_to_end():
    """A pure gap (notable claim, broad search, no corroboration) must land in
    the SUS band, never the top LARP band, and no claim may be DISPROVEN.
    """
    claims = [
        Claim(
            type="identity",
            assertion="A real person named Ghost McPhantom exists.",
            expected_footprint="high",
            tier=EvidenceTier.UNVERIFIED,
            evidence=[{"source_url": "https://g", "snippet": "no matching public profile"}],
        ),
        Claim(
            type="employment",
            employer="Google",
            title="VP Engineering",
            assertion="Worked as VP Engineering at Google.",
            expected_footprint="high",
            tier=EvidenceTier.UNVERIFIED,
            evidence=[{"source_url": "https://g2", "snippet": "generic, no corroboration"}],
        ),
        Claim(
            type="employment",
            employer="Meta",
            title="Director",
            assertion="Worked as Director at Meta.",
            expected_footprint="high",
            tier=EvidenceTier.UNVERIFIED,
            evidence=[{"source_url": "https://g3", "snippet": "generic, no corroboration"}],
        ),
    ]
    score = compute_founder_score(claims)
    assert score is not None
    assert 0 < score < 66  # SUS band, never the DISPROVEN-only top band
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in claims)


def test_detect_gap_suppressed_by_strong_web_hit():
    """The GAP false-positive fix: a claim corroborated by a strong web/news
    snippet (name + employer co-occurring, no source_name at all) must NOT be
    flagged, even though no structured connector confirmed it. This is the
    Musk/Altman class: legit people verified primarily through news coverage.
    """
    claim = Claim(
        type="employment",
        employer="Tesla",
        title="Chief Executive Officer",
        assertion="Worked as Chief Executive Officer at Tesla (2008 to Present).",
        evidence=[
            {
                "source_url": "https://en.wikipedia.org/wiki/Elon_Musk",
                "snippet": "Musk became CEO of Tesla in 2008 and remains its chief executive.",
            }
        ],
    )
    identity = {"name": "Elon Musk", "headline": "CEO", "current_company": "Tesla"}
    assert detect_gap([claim], identity=identity) == []


def test_detect_gap_still_fires_when_web_hits_are_generic():
    """A generic hit that mentions the employer but never the person is not
    corroboration: the fabricated-notable-claim signal must survive the fix."""
    claim = Claim(
        type="employment",
        employer="Google",
        title="VP of Engineering",
        assertion="Worked as VP of Engineering at Google.",
        evidence=[
            {"source_url": "https://en.wikipedia.org/wiki/Google", "snippet": "Google is a company."},
            {"source_url": "https://careers.google.com", "snippet": "Generic careers page, no names."},
        ],
    )
    identity = {"name": "Ghost McPhantom", "headline": "VP", "current_company": "Google"}
    findings = detect_gap([claim], identity=identity)
    assert len(findings) == 1 and findings[0].kind == "GAP"


def test_detect_gap_skips_contested_claims():
    """A claim already covered by a contradiction-shaped finding is contested,
    not silent: run_detectors must not also emit a GAP for it."""
    claim = Claim(
        type="employment",
        employer="Goldman Sachs",
        title="Analyst",
        assertion="Worked as Analyst at Goldman Sachs.",
        evidence=[
            {
                "source_url": "https://news.test/a",
                "snippet": "Goldman Sachs has no record of him ever being employed there.",
                "match_confidence": "medium",
            }
        ],
    )
    findings = run_detectors([claim])
    kinds = [f.kind for f in findings]
    assert "CONTRADICTION" in kinds
    assert "GAP" not in kinds


def test_gap_record_injected_weak_appended_and_self_disarming():
    """The injected GAP record must be too weak to escalate a corroborated
    claim: appended (never displacing real evidence), low confidence, low
    weight, and carrying the absence-only disclaimer in its own text."""
    claim = Claim(
        type="employment",
        employer="Google",
        title="VP",
        assertion="VP at Google.",
        evidence=[{"source_url": "https://g.test", "snippet": "generic result"}],
    )
    findings = detect_gap([claim])
    assert findings
    from detective.dossier import inject_candidates

    inject_candidates([claim], findings)
    rec = claim.evidence[-1]  # appended, not prepended
    assert rec["source_name"] == "mismatch_gap"
    assert rec["match_confidence"] == "low"
    assert rec["weight"] <= 0.2
    assert "ABSENCE ONLY" in rec["snippet"]
    assert "NEVER support DISPROVEN" in rec["snippet"]
    # The real evidence stays first.
    assert claim.evidence[0]["source_url"] == "https://g.test"


# ---------------------------------------------------------------------------
# AUTONOMY detector (AI-washing / wizard-of-oz)
# ---------------------------------------------------------------------------


def _autonomy_claims(overview_snippet: str, mc: str = "medium", source: str = "hackernews"):
    return [
        Claim(
            type="company_overview",
            employer="ShopMagic",
            assertion="ShopMagic is an actively operating, real product.",
            evidence=[
                {
                    "source_url": "https://news.ycombinator.com/item?id=1",
                    "snippet": overview_snippet,
                    "source_name": source,
                    "match_confidence": mc,
                }
            ],
        ),
        Claim(
            type="proprietary_tech",
            employer="ShopMagic",
            assertion='ShopMagic claims: "our AI handles the rest fully automatically, no humans involved."',
            evidence=[],
        ),
    ]


def test_detect_autonomy_fires_cross_claim():
    """The Amazon Just Walk Out shape: the humans-in-the-loop exposE landed on
    the company_overview claim's gather, while the autonomy claim itself
    gathered nothing. The detector must cross-reference them."""
    from detective.dossier import detect_autonomy_overstatement

    claims = _autonomy_claims(
        "ShopMagic's checkout tech was powered by low-paid Indian workers: report"
    )
    findings = detect_autonomy_overstatement(claims)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "AUTONOMY"
    assert f.claim_indices == [1]  # anchored on the autonomy claim
    assert "indian workers" in f.detail or "low-paid" in f.detail


def test_detect_autonomy_silent_on_genuinely_autonomous_product():
    """A real autonomous product (autonomy claim, clean evidence, generic AI
    discourse about OTHER products) must not trip the detector."""
    from detective.dossier import detect_autonomy_overstatement

    claims = _autonomy_claims(
        "Every AI product has some human fallback somewhere; Waymo has human drivers on standby."
    )
    assert detect_autonomy_overstatement(claims) == []


def test_detect_autonomy_silent_without_autonomy_language():
    from detective.dossier import detect_autonomy_overstatement

    claims = _autonomy_claims(
        "ShopMagic was actually humans behind the curtain, human engineers did the work."
    )
    claims[1].assertion = 'ShopMagic claims: "we use machine learning for recommendations."'
    assert detect_autonomy_overstatement(claims) == []


def test_detect_autonomy_ignores_low_confidence_namesake():
    from detective.dossier import detect_autonomy_overstatement

    claims = _autonomy_claims(
        "Some other outfit was powered by human engineers pretending to be AI.",
        mc="low",
        source="courtlistener",
    )
    assert detect_autonomy_overstatement(claims) == []


# ---------------------------------------------------------------------------
# Company DISPROVEN score routing
# ---------------------------------------------------------------------------


def test_company_score_disproven_autonomy_reaches_top_band():
    """A DISPROVEN proprietary_tech (autonomy) claim must floor the company
    composite into the top band even when the metric rows sit low; without the
    claims argument (frozen-fixture recompute) the pure composite is returned;
    with claims but nothing DISPROVEN, nothing changes."""
    from detective.llm import build_metric_breakdown, compute_company_score
    from detective.models import EvidenceTier

    claims = [
        Claim(type="company_overview", employer="X", assertion="X is real."),
        Claim(type="proprietary_tech", employer="X",
              assertion='X claims: "fully autonomous, no humans."'),
    ]
    breakdown = build_metric_breakdown(claims)
    for row in breakdown:
        if row.active:
            row.score_0_10 = 1  # everything reads near-clean
    base = compute_company_score(breakdown)
    assert base is not None and base < 30

    assert compute_company_score(breakdown, claims=claims) == base  # no DISPROVEN

    claims[1].tier = EvidenceTier.DISPROVEN
    floored = compute_company_score(breakdown, claims=claims)
    assert floored is not None and floored >= 66, floored
    # And the frozen-fixture call shape is untouched.
    assert compute_company_score(breakdown) == base


def test_company_score_unverified_claims_never_reach_top_band():
    """Absence discipline: with zero DISPROVEN claims the claims argument must
    never lift the composite (UNVERIFIED is not a contradiction)."""
    from detective.llm import build_metric_breakdown, compute_company_score

    claims = [
        Claim(type="company_overview", employer="X", assertion="X is real."),
        Claim(type="proprietary_tech", employer="X",
              assertion='X claims: "fully autonomous."'),
    ]
    breakdown = build_metric_breakdown(claims)
    for row in breakdown:
        if row.active:
            row.score_0_10 = 5
    assert compute_company_score(breakdown, claims=claims) == compute_company_score(breakdown)


# ---------------------------------------------------------------------------
# TIMELINE detector
# ---------------------------------------------------------------------------


def test_detect_timeline_identical_fulltime_spans():
    claims = [
        Claim(type="employment", employer="Acme", title="CEO", start="Jan 2020", end="Dec 2022",
              assertion="CEO at Acme (Jan 2020 to Dec 2022)."),
        Claim(type="employment", employer="Globex", title="CTO", start="Jan 2020", end="Dec 2022",
              assertion="CTO at Globex (Jan 2020 to Dec 2022)."),
    ]
    findings = detect_timeline(claims)
    assert any(f.kind == "TIMELINE" and f.label == "overlapping full-time roles" for f in findings)


def test_detect_timeline_founding_predates_domain():
    claim = Claim(
        type="employment",
        employer="OldCo",
        title="Founder",
        start="2015",
        assertion="Founded OldCo in 2015.",
        evidence=[
            {
                "source_url": "https://rdap.test",
                "snippet": "Domain oldco.com first registered 2023-04-01.",
                "source_name": "domain_rdap_whois",
                "match_confidence": "high",
            }
        ],
    )
    findings = detect_timeline([claim])
    assert any(f.kind == "TIMELINE" and f.label == "founding predates domain" for f in findings)


def test_detect_timeline_does_not_compare_alias_domain_to_original_name():
    claim = Claim(
        type="employment",
        employer="Fern",
        title="Founder",
        start="2025",
        assertion="Founded Fern in 2025.",
        evidence=[
            {
                "source_url": "https://trytalkr.example",
                "source_name": "product_site",
                "resolution": "resolved",
                "product_name_alignment": "first_party_alias",
            },
            {
                "source_url": "https://rdap.test",
                "snippet": "Domain trytalkr.example first registered 2026-04-01.",
                "source_name": "domain_rdap_whois",
                "match_confidence": "high",
            },
        ],
    )

    findings = detect_timeline([claim])

    assert not any(f.label == "founding predates domain" for f in findings)


def test_detect_timeline_reversed_dates():
    claims = [
        Claim(type="employment", employer="Acme", start="Jan 2022", end="Jan 2020",
              assertion="Acme role with reversed dates.")
    ]
    findings = detect_timeline(claims)
    assert any(f.kind == "TIMELINE" and f.label == "reversed dates" for f in findings)


def test_detect_timeline_clean_history_no_flags():
    claims = [
        Claim(type="employment", employer="Acme", start="Jan 2018", end="Dec 2020",
              assertion="Acme."),
        Claim(type="employment", employer="Globex", start="Jan 2021", end="Present",
              assertion="Globex."),
    ]
    assert detect_timeline(claims) == []


# ---------------------------------------------------------------------------
# claimed-set helper
# ---------------------------------------------------------------------------


def test_build_claimed_set_extracts_numeric_magnitude():
    claims = [
        Claim(type="user_count", employer="A", title="users", assertion="A claims 50,000 users."),
        Claim(type="employment", employer="Acme", assertion="Worked at Acme."),
    ]
    rows = build_claimed_set(claims)
    assert rows[0]["claimed_quantity"] == 50_000
    assert rows[1]["claimed_quantity"] is None


def test_build_discovered_set_flattens_evidence():
    from detective.dossier import build_discovered_set

    claims = [
        Claim(type="user_count", employer="A", assertion="A claims 50,000 users.",
              evidence=[{"source_url": "u", "snippet": "12 ratings", "source_name": "app_store_play_store_reviews", "match_confidence": "high"}]),
        Claim(type="employment", employer="Acme", assertion="Worked at Acme.", evidence=[]),
    ]
    rows = build_discovered_set(claims)
    assert len(rows) == 1  # only the record-bearing claim contributes rows
    assert rows[0]["claim_index"] == 0
    assert rows[0]["source_name"] == "app_store_play_store_reviews"


# ---------------------------------------------------------------------------
# End-to-end build_dossier with a stub provider + patched offline gather.
# ---------------------------------------------------------------------------


class DisciplinedStubProvider(LLMProvider):
    """An in-process stand-in for a human operator / Gemini that faithfully
    applies the engine's discipline over the (now mismatch-augmented) evidence:
      - a high-confidence CONTRADICTION candidate -> DISPROVEN (a real,
        proven falsehood in the evidence),
      - a GAP candidate -> UNVERIFIED + high expected_footprint (SUS, never
        DISPROVEN off an absence),
      - otherwise UNVERIFIED.
    For a company scan it also fills buildability and the active metric rows,
    pushing reach_vs_footprint high when an inflation candidate is present.
    This is a TEST DOUBLE for the reused provider step, not new engine code.
    """

    def decompose_claims(self, raw_profile: dict) -> list[Claim]:
        if (raw_profile.get("scan_type") or "person") == "company_app":
            return mechanical_decompose_company(raw_profile)
        return mechanical_decompose(raw_profile)

    def assign_tiers_and_verdict(self, dossier: Dossier) -> Dossier:
        has_disproven = False
        has_inflation = False
        for c in dossier.claims:
            names = {(e.get("source_name") or "") for e in c.evidence}
            if "mismatch_contradiction" in names or "mismatch_autonomy" in names:
                # Both are contradiction-shaped candidates quoting real
                # adverse / humans-in-the-loop evidence; a disciplined
                # operator who accepts them marks the claim DISPROVEN.
                c.tier = EvidenceTier.DISPROVEN
                c.expected_footprint = "high"
                has_disproven = True
            elif names & {
                "mismatch_gap",
                # The two positive mismatches added by the judgment-layer
                # tightening: both resolve to UNVERIFIED + high footprint (SUS),
                # never DISPROVEN, mirroring the operator instructions.
                "mismatch_tech_substance",
                "mismatch_registry_absence",
            }:
                c.tier = EvidenceTier.UNVERIFIED
                c.expected_footprint = "high"
            else:
                c.tier = EvidenceTier.UNVERIFIED
            if "mismatch_inflation" in names:
                has_inflation = True

        if dossier.scan_type == "company_app":
            if dossier.buildability is not None:
                dossier.buildability.tier = "MODERATE"
                dossier.buildability.note = "stub"
            for row in dossier.metric_breakdown:
                if not row.active or row.name == "buildability":
                    continue
                if row.name == "reach_vs_footprint" and has_inflation:
                    row.score_0_10 = 9
                else:
                    row.score_0_10 = 1
                row.note = "stub"
            dossier.verdict = "stub company verdict"
        else:
            # Same completion-gate discipline as ApiProvider._apply_result:
            # set larp_score off the tiers so pipeline.run/build_dossier will
            # compute founder_larp_score.
            from detective.llm import compute_founder_score as _cfs

            dossier.larp_score = _cfs(dossier.claims)
            dossier.verdict = (
                "stub verdict: proven falsehood" if has_disproven else "stub verdict: unverified"
            )
        return dossier


def _fake_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    """Deterministic offline evidence keyed off claim shape. Never touches the
    network; stands in for verify.gather_evidence in both pipeline.run and
    build_dossier (both look the function up on the verify module at call time).
    """
    if claim.type == "user_count":
        claim.evidence = [
            {
                "source_url": "https://itunes.test/app",
                "snippet": "The app has 12 ratings on the App Store.",
                "source_name": "app_store_play_store_reviews",
                "weight": 0.8,
                "match_confidence": "high",
            }
        ]
    elif claim.type == "employment" and "goldman" in (claim.employer or "").lower():
        claim.evidence = [
            {
                "source_url": "https://news.test/a",
                "snippet": "Goldman Sachs has no record of this person ever working there.",
                "source_name": "",
                "weight": 0.5,
                "match_confidence": "medium",
            }
        ]
    else:
        claim.evidence = [
            {"source_url": "https://g.test", "snippet": "generic result, no corroboration"}
        ]
    return claim


@pytest.fixture
def patch_gather(monkeypatch):
    monkeypatch.setattr(verify, "gather_evidence", _fake_gather)


def _person_raw():
    return {
        "profile_url": "https://www.linkedin.com/in/test-person/",
        "scan_type": "person",
        "identity": {"name": "Test Person", "headline": "Analyst", "current_company": "Goldman Sachs"},
        "experience": [
            {"title": "Analyst", "company": "Goldman Sachs", "start_date": "Jan 2019", "end_date": "Dec 2021"},
            {"title": "VP", "company": "Obscure Startup", "start_date": "Jan 2022", "end_date": "Present"},
        ],
        "education": [],
    }


def _live_person_raw():
    """The same person fixture, but stamped with a live-scrape extraction
    manifest so it classifies as a FULL scan (the regression pair partner of
    the injected, no-manifest _person_raw, which is shallow)."""
    raw = _person_raw()
    raw["_extraction"] = {
        "method": "live_scrape",
        "experience_count": 2,
        "with_description_count": 1,
        "posts_count": 0,
        "details_page_loaded": True,
    }
    return raw


def test_build_dossier_shallow_suppresses_absence(patch_gather):
    # An injected profile (no extraction manifest) is a SHALLOW scan: the tool
    # did not really look, so absence findings (GAP) are suppressed entirely and
    # the score cannot accrue absence-based suspicion. The Goldman "no record"
    # CONTRADICTION is a real cross-reference and still stands.
    d = build_dossier(_person_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None)
    assert d.scan_depth == "shallow"
    kinds = {m["kind"] for m in d.mismatches}
    assert "GAP" not in kinds
    assert "TECH_AUTHENTICITY" not in kinds


def test_build_dossier_full_surfaces_absence(patch_gather):
    # The SAME fixture stamped as a live scrape is a FULL scan: the notable but
    # uncorroborated employment/identity claims surface as GAP findings (the
    # regression pair partner of the shallow case above).
    d = build_dossier(_live_person_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None)
    assert d.scan_depth == "full"
    kinds = {m["kind"] for m in d.mismatches}
    assert "GAP" in kinds


def test_build_dossier_person_end_to_end(patch_gather):
    d = build_dossier(_person_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None)
    assert isinstance(d, Dossier)
    assert d.scan_type == "person"
    assert d.founder_larp_score is not None
    # The Goldman "no record" contradiction should have driven a DISPROVEN
    # claim and landed the score in the top band.
    assert any(c.tier is EvidenceTier.DISPROVEN for c in d.claims)
    assert d.founder_larp_score >= 66
    assert d.mismatches, "expected typed mismatch findings to be surfaced"
    kinds = {m["kind"] for m in d.mismatches}
    assert "CONTRADICTION" in kinds
    # The resolved contradiction finding carries the final tier.
    contra = next(m for m in d.mismatches if m["kind"] == "CONTRADICTION")
    assert contra["resolved_tier"] == "DISPROVEN"


def _company_raw():
    return {
        "profile_url": "https://myapp.test/",
        "scan_type": "company_app",
        "identity": {"name": "MyApp", "current_company": "MyApp"},
        "metrics": [
            {"type": "user_count", "value": "50000", "unit": "users", "text": "50,000 users"},
        ],
        "tech_claims": [{"text": "proprietary AI engine"}],
        "pricing": {"tiers": []},
    }


def test_build_dossier_company_ratings_do_not_create_inflation(patch_gather):
    d = build_dossier(_company_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None)
    assert d.scan_type == "company_app"
    assert d.company_larp_score is not None
    kinds = {m["kind"] for m in d.mismatches}
    assert "INFLATION" not in kinds
    assert d.company_larp_score < 20


def _fake_gather_ai_washing(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    """Offline gather for the AI-washing shape: the exposE lands on the
    company_overview claim (as it does in the real Amazon JWO cache), while
    the autonomy claim itself gathers nothing."""
    if claim.type == "company_overview":
        claim.evidence = [
            {
                "source_url": "https://news.ycombinator.com/item?id=9",
                "snippet": "GrabGo's checkout was powered by low-paid Indian workers reviewing video: report",
                "source_name": "hackernews",
                "weight": 0.64,
                "match_confidence": "medium",
            }
        ]
    else:
        claim.evidence = []
    return claim


def test_build_dossier_company_autonomy_end_to_end(monkeypatch):
    """The Amazon Just Walk Out class end to end: a claimed-autonomy company
    whose gathered evidence shows humans in the loop must land in the top
    band via the AUTONOMY candidate -> DISPROVEN -> company-score floor."""
    monkeypatch.setattr(verify, "gather_evidence", _fake_gather_ai_washing)
    raw = {
        "profile_url": "https://grabgo.test/",
        "scan_type": "company_app",
        "identity": {"name": "GrabGo", "current_company": "GrabGo"},
        "metrics": [],
        "tech_claims": [
            {"text": "Our AI handles the rest fully automatically, no humans involved."}
        ],
        "pricing": {"tiers": []},
    }
    d = build_dossier(raw, provider=DisciplinedStubProvider(), emit=lambda *a: None)
    assert d.scan_type == "company_app"
    kinds = {m["kind"] for m in d.mismatches}
    assert "AUTONOMY" in kinds
    auto = next(m for m in d.mismatches if m["kind"] == "AUTONOMY")
    assert auto["resolved_tier"] == "DISPROVEN"
    assert d.company_larp_score is not None and d.company_larp_score >= 66


def test_build_dossier_company_autonomous_control_stays_low(monkeypatch):
    """The guard: the SAME autonomy claim with clean evidence (no humans-in-
    the-loop exposE) must stay low: no AUTONOMY finding, no DISPROVEN, no floor."""

    def _clean_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = [
            {
                "source_url": "https://news.test/clean",
                "snippet": "GrabGo's system uses computer vision; reviewers praised the seamless experience.",
            }
        ]
        return claim

    monkeypatch.setattr(verify, "gather_evidence", _clean_gather)
    raw = {
        "profile_url": "https://grabgo.test/",
        "scan_type": "company_app",
        "identity": {"name": "GrabGo", "current_company": "GrabGo"},
        "metrics": [],
        "tech_claims": [
            {"text": "Our AI handles the rest fully automatically, no humans involved."}
        ],
        "pricing": {"tiers": []},
    }
    d = build_dossier(raw, provider=DisciplinedStubProvider(), emit=lambda *a: None)
    kinds = {m["kind"] for m in d.mismatches}
    assert "AUTONOMY" not in kinds
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)
    assert d.company_larp_score is not None and d.company_larp_score < 40


def test_build_dossier_ab_comparable_to_pipeline_run(patch_gather):
    """Both paths, same injected raw_profile and same provider, must each
    return a scored Dossier with the same score fields, so an A/B harness can
    compare them directly.
    """
    raw = _person_raw()
    d_pipeline = pipeline.run(
        raw["profile_url"], provider=DisciplinedStubProvider(), raw_profile=dict(raw), progress=lambda *a: None
    )
    d_dossier = build_dossier(dict(raw), provider=DisciplinedStubProvider(), emit=lambda *a: None)

    for d in (d_pipeline, d_dossier):
        assert isinstance(d, Dossier)
        assert d.founder_larp_score is not None
        assert d.verdict
    # The per-claim path has no mismatch findings; the aggregate path does.
    assert d_pipeline.mismatches == []
    assert d_dossier.mismatches
    # The aggregate path, seeing the injected contradiction, should score at
    # least as high as the per-claim path on the same fabricated resume.
    assert d_dossier.founder_larp_score >= d_pipeline.founder_larp_score


class LowFootprintStubProvider(DisciplinedStubProvider):
    """A provider that follows the "when unsure, choose low footprint"
    discipline: it never invents a contradiction, and it marks uncorroborated
    claims UNVERIFIED with LOW expected_footprint. This is the calibration
    control: a legit, hard-to-verify person must NOT be pushed into SUS just
    for being obscure.
    """

    def assign_tiers_and_verdict(self, dossier: Dossier) -> Dossier:
        for c in dossier.claims:
            names = {(e.get("source_name") or "") for e in c.evidence}
            # No contradiction present here; everything is merely unconfirmed,
            # and a disciplined operator calls an obscure claim low-footprint.
            c.tier = EvidenceTier.UNVERIFIED
            c.expected_footprint = "low"
            _ = names
        from detective.llm import compute_founder_score as _cfs

        dossier.larp_score = _cfs(dossier.claims)
        dossier.verdict = "stub verdict: could not verify, nothing disproven"
        return dossier


def _obscure_person_raw():
    return {
        "profile_url": "https://www.linkedin.com/in/obscure-person/",
        "scan_type": "person",
        "identity": {"name": "Quiet Individual", "headline": "Freelancer", "current_company": "Self"},
        "experience": [
            {"title": "Freelance Designer", "company": "Self-employed", "start_date": "Jan 2019", "end_date": "Present"},
        ],
        "education": [],
    }


def test_clean_low_footprint_person_stays_clear(patch_gather):
    """Calibration control (guard e): a legit low-footprint person whose claims
    merely could not be corroborated must land CLEAR, never SUS or LARP, and no
    claim may be DISPROVEN. The gap detector fires (its candidates are injected)
    but a disciplined provider marks footprint low, so the SUS band is not lifted.
    """
    d = build_dossier(_obscure_person_raw(), provider=LowFootprintStubProvider(), emit=lambda *a: None)
    assert d.founder_larp_score is not None
    assert d.founder_larp_score < 30  # CLEAR band
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


def test_run_detectors_returns_all_kinds():
    claims = [
        Claim(type="funding", employer="A", assertion="A claims to have raised $50 million.",
              evidence=[{"source_url": "u", "snippet": "A Form D offering amount was $500,000.", "source_name": "sec_edgar_form_d", "match_confidence": "high"}]),
        Claim(type="employment", employer="Goldman Sachs", assertion="Worked at Goldman Sachs.",
              evidence=[{"source_url": "n", "snippet": "Goldman Sachs has no record of him at the firm", "match_confidence": "medium"}]),
        Claim(type="employment", employer="Google", title="VP", assertion="VP at Google.",
              evidence=[{"source_url": "g", "snippet": "generic"}]),
    ]
    findings = run_detectors(claims)
    kinds = {f.kind for f in findings}
    assert {"INFLATION", "CONTRADICTION", "GAP"} <= kinds


def test_traction_in_description_becomes_user_count_without_rating_math():
    """A founder's OWN traction boast in an experience description ("2,000+
    users") must decompose into a user_count claim. App Store ratings remain a
    reach clue, but they are not converted into a measured user count."""
    prof = {
        "identity": {"name": "Mann Bellani"},
        "experience": [
            {
                "title": "Founder",
                "company": "Organize Campus",
                "start_date": "Jan 2026",
                "end_date": "Jun 2026",
                "description": "Built Organize Campus, a campus events app that grew to 2,000+ users.",
            },
            {
                "title": "Data Analyst",
                "company": "Southwest Airlines",
                "description": "Presented findings to a team of 5.",
            },
        ],
    }
    claims = mechanical_decompose(prof)
    user_counts = [c for c in claims if c.type == "user_count"]
    assert len(user_counts) == 1
    assert "2,000+ users" in user_counts[0].assertion
    # An incidental number ("team of 5") must NOT become a magnitude claim.
    assert not any("team of 5" in (c.assertion or "") for c in claims)

    user_counts[0].evidence = [
        {
            "source_name": "app_store_play_store_reviews",
            "match_confidence": "high",
            "snippet": "App Store listing for 'Organize Campus': 13 total rating(s), average 3.62 stars.",
        }
    ]
    findings = dossier_mod.detect_inflation(claims)
    assert findings == []


# ---------------------------------------------------------------------------
# Workstream 1: corroboration must speak to the ROLE, not merely to existence.
# Association ("the entity is real and the name co-occurs with it") no longer
# clears a GAP; it downgrades to an "at a real entity" GAP. Every tightening
# below ships with its paired guard test proving legitimate coverage still
# clears (apposition, title tokens, org rosters, structured connectors).
# ---------------------------------------------------------------------------

_WS1_IDENTITY = {
    "name": "Jane Sample",
    "headline": "Chief Technology Officer",
    "current_company": "Acme Robotics",
}


def _ws1_employment_claim(snippet: str, **overrides) -> Claim:
    evidence = overrides.pop("evidence", None) or [
        {"source_url": "https://news.test/a", "snippet": snippet}
    ]
    kwargs = {
        "type": "employment",
        "employer": "Acme Robotics",
        "title": "Chief Technology Officer",
        "assertion": "Worked as Chief Technology Officer at Acme Robotics.",
        "evidence": evidence,
    }
    kwargs.update(overrides)
    return Claim(**kwargs)


def test_association_only_snippet_no_longer_clears_employment():
    """Bare co-occurrence (the name and the employer in one snippet, nothing
    about the role) proves association, not the ROLE. It must now surface an
    "at a real entity" GAP instead of silently clearing."""
    claim = _ws1_employment_claim(
        "Jane Sample and Acme Robotics were both at the Austin startup mixer."
    )
    findings = detect_gap([claim], identity=_WS1_IDENTITY)
    assert len(findings) == 1, findings
    f = findings[0]
    assert f.kind == "GAP"
    assert "at a real entity" in f.label
    assert f.severity == 0.35
    assert "existence is not substantiation" in f.detail
    inject_candidates([claim], findings)
    assert claim.evidence[-1]["source_name"] == "mismatch_gap"


def test_role_apposition_still_clears():
    """ANTI-OVER-CORRECTION GUARD: ordinary news apposition ("Jane Sample,
    chief technology officer of Acme Robotics") speaks to the role and must
    keep suppressing the GAP entirely."""
    claim = _ws1_employment_claim(
        "Jane Sample, chief technology officer of Acme Robotics, announced the raise."
    )
    assert detect_gap([claim], identity=_WS1_IDENTITY) == []


def test_title_token_clears():
    """ANTI-OVER-CORRECTION GUARD: a snippet carrying a role/title token next
    to the name and the employer is role-speaking evidence."""
    claim = _ws1_employment_claim(
        "Acme Robotics engineer Jane Sample presented at PyCon."
    )
    assert detect_gap([claim], identity=_WS1_IDENTITY) == []


def test_company_overview_bare_mention_downgraded():
    """It-exists is not substantiation: a bare company mention downgrades a
    company_overview claim to the association GAP, while a snippet that speaks
    to the DESCRIPTION still clears it."""

    def _claim(snippet: str) -> Claim:
        return Claim(
            type="company_overview",
            employer="Acme Robotics",
            assertion="Acme Robotics builds warehouse automation robots.",
            evidence=[{"source_url": "https://news.test/b", "snippet": snippet}],
        )

    bare = detect_gap([_claim("Acme Robotics appeared on a list of Texas startups.")])
    assert len(bare) == 1 and "at a real entity" in bare[0].label

    # Twin (guard): the description itself is corroborated.
    substantive = detect_gap(
        [_claim("Acme Robotics builds warehouse automation robots for grocers.")]
    )
    assert substantive == []


def test_github_medium_does_not_clear_gap():
    """A "medium" github record is a name-pattern match: an account that merely
    EXISTS somewhere. It must never clear a role claim on its own."""
    claim = _ws1_employment_claim("", evidence=[_github_ev("medium", "substantial")])
    findings = detect_gap([claim], identity=_WS1_IDENTITY)
    assert len(findings) == 1 and findings[0].kind == "GAP"


def test_github_high_substantial_clears_gap():
    """GUARD: a CONFIRMED account with a substantial code footprint is real
    role-speaking evidence and still suppresses the GAP."""
    claim = _ws1_employment_claim("", evidence=[_github_ev("high", "substantial")])
    assert detect_gap([claim], identity=_WS1_IDENTITY) == []


def test_github_high_thin_does_not_clear_gap():
    """A confirmed-but-thin account substantiates no role: it clears nothing
    (the substance detector owns the accusation side)."""
    claim = _ws1_employment_claim("", evidence=[_github_ev("high", "thin-or-absent")])
    findings = detect_gap([claim], identity=_WS1_IDENTITY)
    assert len(findings) == 1 and findings[0].kind == "GAP"


def _org_roster_ev(match_confidence: str, found: bool) -> dict:
    body = (
        "Public roster/team page for 'Acme Robotics' lists 'Jane Sample'."
        if found
        else (
            "Public roster/team page for 'Acme Robotics' was fetched, but the name "
            "'Jane Sample' was NOT found in its visible text. This is a documented "
            "ABSENCE, not disproof."
        )
    )
    return {
        "source_url": "https://acme.test/team",
        "snippet": body,
        "source_name": "org_roster",
        "weight": 0.64,
        "match_confidence": match_confidence,
    }


def test_org_roster_high_clears_and_low_absence_does_not():
    """GUARD both ways: the org's OWN roster listing the person is role-speaking
    corroboration and clears the GAP; its low-confidence ABSENCE record can
    never corroborate anything."""
    listed = _ws1_employment_claim("", evidence=[_org_roster_ev("high", True)])
    assert detect_gap([listed], identity=_WS1_IDENTITY) == []

    absent = _ws1_employment_claim("", evidence=[_org_roster_ev("low", False)])
    findings = detect_gap([absent], identity=_WS1_IDENTITY)
    assert len(findings) == 1 and findings[0].kind == "GAP"


def test_registry_absent_record_never_corroborates():
    """A COMPLETED negative registry lookup is the opposite of corroboration:
    a checked-absent record must never suppress a GAP, however confident."""
    claim = _ws1_employment_claim(
        "",
        evidence=[
            {
                "source_url": "https://apps.apple.com",
                "snippet": (
                    "Searched Apple's App Store catalog for 'Acme Robotics'; no app "
                    "with a matching name is listed. This is a completed catalog lookup."
                ),
                "source_name": "app_store_play_store_reviews",
                "weight": 0.8,
                "match_confidence": "high",
                "registry_check": "absent",
            }
        ],
    )
    findings = detect_gap([claim], identity=_WS1_IDENTITY)
    assert len(findings) == 1 and findings[0].kind == "GAP"


def test_search_unavailable_still_never_gaps():
    """REGRESSION PIN (unchanged behavior): a claim the tool could not look up
    contributes nothing. Absence accuses only if we actually looked."""
    claim = _ws1_employment_claim(
        "",
        evidence=[
            {
                "source_url": "internal://search-unavailable",
                "snippet": "The web-search channel was not configured.",
                "source_name": "search_unavailable",
                "weight": 0.0,
                "match_confidence": "low",
            }
        ],
    )
    assert detect_gap([claim], identity=_WS1_IDENTITY) == []


def _ws1_full_scan_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jane-sample/",
        "scan_type": "person",
        "identity": {
            "name": "Jane Sample",
            "headline": "Vice President of Operations",
            "current_company": "Acme Robotics",
        },
        "experience": [
            {
                "title": "Vice President of Operations",
                "company": "Acme Robotics",
                "start_date": "Jan 2020",
                "end_date": "Present",
            }
        ],
        "education": [],
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }


def _ws1_snippet_gather(snippet: str):
    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = [{"source_url": "https://news.test/c", "snippet": snippet}]
        return claim

    return _gather


def test_association_gap_scores_sus_end_to_end(monkeypatch):
    """The core deliverable, end to end: a notable role whose ONLY evidence is
    co-occurrence lands in the SUS band, while the same profile with
    role-speaking coverage stays CLEAR."""
    monkeypatch.setattr(
        verify,
        "gather_evidence",
        _ws1_snippet_gather(
            "Jane Sample and Acme Robotics were both at the Austin startup mixer."
        ),
    )
    d = build_dossier(
        _ws1_full_scan_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None
    )
    assert d.scan_depth == "full"
    assert any("at a real entity" in m["label"] for m in d.mismatches), d.mismatches
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score

    # Twin (guard): role-speaking apposition coverage stays CLEAR.
    monkeypatch.setattr(
        verify,
        "gather_evidence",
        _ws1_snippet_gather(
            "Jane Sample, vice president of operations at Acme Robotics, announced "
            "the expansion."
        ),
    )
    clear = build_dossier(
        _ws1_full_scan_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None
    )
    assert all(m["kind"] != "GAP" for m in clear.mismatches), clear.mismatches
    assert clear.founder_larp_score is not None and clear.founder_larp_score < 33


# ---------------------------------------------------------------------------
# Workstream 2: role-vs-substance mismatch (the undershoot detector). A claimed
# technical/leadership role whose RESOLVED evidence undershoots it is a POSITIVE
# mismatch, stronger than a bare void, and still SUS-only (never DISPROVEN:
# employer code is often private).
# ---------------------------------------------------------------------------


def _cto_claim(evidence: list[dict]) -> Claim:
    return Claim(
        type="employment",
        employer="Acme Robotics",
        title="Chief Technology Officer",
        assertion="Worked as Chief Technology Officer at Acme Robotics.",
        evidence=evidence,
    )


def test_confirmed_thin_cto_is_substance_mismatch():
    """The person's OWN confirmed GitHub account reading thin-or-absent behind a
    loud technical claim is a resolved undershoot, not a bare absence."""
    claim = _cto_claim([_github_ev("high", "thin-or-absent")])
    findings = detect_technical_authenticity([claim])
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "TECH_SUBSTANCE_MISMATCH"
    assert f.severity == 0.6
    assert "UNDERSHOOT, NOT PROOF" in f.detail
    inject_candidates([claim], findings)
    injected = [
        e for e in claim.evidence if (e.get("source_name") or "") == "mismatch_tech_substance"
    ]
    assert len(injected) == 1
    assert injected[0]["match_confidence"] == "medium"
    # A real signal, not an absence flag: prepended so it is never crowded out.
    assert claim.evidence[0]["source_name"] == "mismatch_tech_substance"


def test_medium_name_match_never_fires_substance_mismatch():
    """MATCH DISCIPLINE: a "medium" name-pattern account is not confirmably this
    person's, so it can never produce the resolved-undershoot mismatch. The
    ordinary void branch may still fire, as an absence-shaped record."""
    claim = _cto_claim([_github_ev("medium", "thin-or-absent")])
    findings = detect_technical_authenticity([claim])
    assert all(f.kind != "TECH_SUBSTANCE_MISMATCH" for f in findings), findings
    assert [f.kind for f in findings] == ["TECH_AUTHENTICITY"]
    inject_candidates([claim], findings)
    assert any(
        (e.get("source_name") or "") == "mismatch_tech_authenticity" for e in claim.evidence
    )


def test_quiet_internal_role_never_fires():
    """ANTI-OVER-CORRECTION GUARD: a quiet rank-and-file internal role never
    made a loud builder claim, so the loud-claim gate keeps it untouched even
    with a confirmed thin account in evidence."""
    claim = Claim(
        type="employment",
        employer="Sample State University",
        title="IT Department",
        assertion="Works in the IT department at a large university.",
        evidence=[_github_ev("high", "thin-or-absent")],
    )
    assert detect_technical_authenticity([claim]) == []


def test_substantial_account_clears_both_branches():
    """GUARD: a confirmed substantial account clears the whole detector, even
    when a second, thin confirmed account sits alongside it."""
    claim = _cto_claim([_github_ev("high", "thin-or-absent"), _github_ev("high", "substantial")])
    assert detect_technical_authenticity([claim]) == []


def _ws2_full_scan_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jane-sample/",
        "scan_type": "person",
        "identity": {
            "name": "Jane Sample",
            "headline": "Chief Technology Officer",
            "current_company": "Acme Robotics",
        },
        "experience": [
            {
                "title": "Chief Technology Officer",
                "company": "Acme Robotics",
                "start_date": "Jan 2020",
                "end_date": "Present",
            }
        ],
        "education": [],
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }


def test_substance_mismatch_scores_sus_end_to_end(monkeypatch):
    """End to end: a loud technical claim undershot by the person's own
    confirmed code footprint lands in the SUS band, never the LARP band."""

    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = [_github_ev("high", "thin-or-absent")]
        return claim

    monkeypatch.setattr(verify, "gather_evidence", _gather)
    d = build_dossier(
        _ws2_full_scan_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None
    )
    kinds = {m["kind"] for m in d.mismatches}
    assert "TECH_SUBSTANCE_MISMATCH" in kinds, d.mismatches
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


# ---------------------------------------------------------------------------
# Workstream 3: magnitude/scale contradiction beyond app-store users. A
# counter-number parsed from a web/news snippet is weaker than a registry
# measurement, so it is injected at "medium" (SUS), never as a proven adverse
# measurement, and it must be about THIS subject (the misattribution guard).
# ---------------------------------------------------------------------------


def _web_ev(snippet: str) -> dict:
    return {"source_url": "https://news.test/w", "snippet": snippet}


def test_headcount_inflation_from_web_snippet():
    claim = Claim(
        type="headcount",
        employer="Acme Robotics",
        title="team",
        assertion="Acme Robotics is a team of 500 people.",
        evidence=[_web_ev("Acme Robotics, a startup with 12 employees, opened a new office.")],
    )
    findings = detect_inflation([claim])
    assert len(findings) == 1, findings
    f = findings[0]
    assert f.kind == "INFLATION"
    assert f.basis == "web"
    assert "weaker than a registry measurement" in f.detail
    inject_candidates([claim], findings)
    injected = [e for e in claim.evidence if (e.get("source_name") or "") == "mismatch_inflation"]
    assert len(injected) == 1 and injected[0]["match_confidence"] == "medium"


def test_revenue_inflation_from_web_snippet():
    claim = Claim(
        type="revenue_metric",
        employer="Acme Robotics",
        assertion="Acme Robotics is at $10M ARR.",
        evidence=[_web_ev("Acme Robotics reported roughly $150k in revenue last year.")],
    )
    findings = detect_inflation([claim])
    assert len(findings) == 1 and findings[0].basis == "web"


def test_money_managed_inflation():
    """money_managed is newly numeric: a managed-money boast against a
    discovered AUM figure is an inflation. Twin: with NO counter-number the
    claim stays on the GAP path (absence never masquerades as measurement)."""
    claim = Claim(
        type="money_managed",
        employer="Acme Investment Group",
        assertion="Managed $4.8M for the fund.",
        evidence=[
            _web_ev("The Acme student fund reports $60k in assets under management.")
        ],
    )
    findings = detect_inflation([claim])
    assert len(findings) == 1 and findings[0].basis == "web"

    silent = Claim(
        type="money_managed",
        employer="Acme Investment Group",
        assertion="Managed $4.8M for the fund.",
        evidence=[_web_ev("The Acme student fund held its spring social.")],
    )
    assert detect_inflation([silent]) == []
    assert [f.kind for f in detect_gap([silent])] == ["GAP"]


def test_web_counter_requires_subject_token():
    """MISATTRIBUTION GUARD: a number about some OTHER company never counts."""
    claim = Claim(
        type="headcount",
        employer="Acme Robotics",
        title="team",
        assertion="Acme Robotics is a team of 500 people.",
        evidence=[_web_ev("Initech, a startup with 12 employees, opened a new office.")],
    )
    assert detect_inflation([claim]) == []


def test_app_store_rating_does_not_override_web_user_measurement():
    """Ratings are ignored as user counts; a same-subject web count stays weak."""
    claim = Claim(
        type="user_count",
        employer="Acme Robotics",
        title="users",
        assertion="Acme Robotics claims 2,000 users.",
        evidence=[
            {
                "source_url": "https://apps.apple.com/app/id3",
                "snippet": "App Store listing for 'Acme Robotics': 13 total rating(s).",
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
            },
            _web_ev("Acme Robotics says it has 9 users in the pilot."),
        ],
    )
    findings = detect_inflation([claim])
    assert len(findings) == 1
    f = findings[0]
    assert f.basis == "web"
    assert "9" in f.discovered
    inject_candidates([claim], findings)
    injected = [e for e in claim.evidence if (e.get("source_name") or "") == "mismatch_inflation"]
    assert injected[0]["match_confidence"] == "medium"


def test_marker_records_never_yield_numbers():
    """Search markers and injected synthetic records are not measurements."""
    claim = Claim(
        type="headcount",
        employer="Acme Robotics",
        title="team",
        assertion="Acme Robotics is a team of 500 people.",
        evidence=[
            {
                "source_url": "internal://searched",
                "snippet": "Searched all connectors and web for this claim and found no records.",
                "source_name": "searched_no_results",
                "match_confidence": "low",
            },
            {
                "source_url": "internal://mismatch/mismatch_gap",
                "snippet": "[GAP] searched 1 record(s); 12 employees is not a real number here.",
                "source_name": "mismatch_gap",
                "match_confidence": "low",
            },
        ],
    )
    assert detect_inflation([claim]) == []


def test_checked_absent_record_yields_no_number():
    """A completed-but-empty catalog lookup contains no measurement: inflation
    can never fire from a registry absence."""
    claim = Claim(
        type="user_count",
        employer="Acme Robotics",
        title="users",
        assertion="Acme Robotics claims 2,000 users.",
        evidence=[
            {
                "source_url": "https://apps.apple.com",
                "snippet": (
                    "Searched Apple's App Store catalog for 'Acme Robotics'; no app with "
                    "a matching name is listed. 0 total rating(s) are available."
                ),
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
                "registry_check": "absent",
            }
        ],
    )
    assert detect_inflation([claim]) == []


# ---------------------------------------------------------------------------
# Workstream 4: authoritative-registry absence. A COMPLETED lookup of the
# registry a claim itself invokes, coming back empty, is a positive near-
# contradiction. Gated hard: fires only off a checked-absent "high" record from
# the invoked registry's own connector, and caps at SUS UNCONDITIONALLY (never
# DISPROVEN).
# ---------------------------------------------------------------------------

from detective.dossier import detect_registry_absence


def _yc_absent_record() -> dict:
    return {
        "source_url": "https://www.ycombinator.com/companies?query=Acme Robotics",
        "snippet": (
            "Queried Y Combinator's own public companies directory for 'Acme Robotics'; "
            "no matching company is listed. This is a COMPLETED directory lookup."
        ),
        "source_name": "accelerator_badges",
        "weight": 0.8,
        "match_confidence": "high",
        "registry_check": "absent",
    }


def test_registry_absence_fires_on_invoked_registry():
    claim = Claim(
        type="employment",
        employer="Acme Robotics",
        title="Founder",
        assertion="Founder of Acme Robotics (YC S24).",
        evidence=[_yc_absent_record()],
    )
    findings = detect_registry_absence([claim])
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "REGISTRY_ABSENCE"
    assert f.claim_indices == [0]
    inject_candidates([claim], findings)
    injected = [
        e for e in claim.evidence if (e.get("source_name") or "") == "mismatch_registry_absence"
    ]
    assert len(injected) == 1 and injected[0]["match_confidence"] == "medium"


def test_registry_absence_never_disproven():
    """Owner decision 2, pinned: registry absence caps at SUS UNCONDITIONALLY.
    The injected record must say so in its own text so no operator can escalate
    it to DISPROVEN."""
    claim = Claim(
        type="employment",
        employer="Acme Robotics",
        title="Founder",
        assertion="Founder of Acme Robotics (YC S24).",
        evidence=[_yc_absent_record()],
    )
    findings = detect_registry_absence([claim])
    assert "NEVER reach DISPROVEN" in findings[0].detail
    assert "UNCONDITIONALLY" in findings[0].detail


def test_no_marker_no_finding():
    """A checked-absent record on a claim that never invokes YC does not fire:
    vague prestige-dropping stays on the ordinary GAP path."""
    claim = Claim(
        type="employment",
        employer="Acme Robotics",
        title="Founder",
        assertion="Founder of Acme Robotics, a warehouse robotics startup.",
        evidence=[_yc_absent_record()],
    )
    assert detect_registry_absence([claim]) == []


def test_marker_without_record_no_finding():
    """A YC-invoking claim whose evidence is only a searched_no_results marker
    (no completed registry lookup) is an ordinary GAP candidate, not a registry
    absence: the detector consumes only completed lookups."""
    claim = Claim(
        type="employment",
        employer="Acme Robotics",
        title="Founder",
        assertion="Founder of Acme Robotics (YC S24).",
        evidence=[
            {
                "source_url": "internal://searched",
                "snippet": "Searched all connectors and web for this claim and found no records.",
                "source_name": "searched_no_results",
                "match_confidence": "low",
            }
        ],
    )
    assert detect_registry_absence([claim]) == []


def test_registry_absence_requires_high_confidence():
    """A non-high checked-absent record (should not occur from the connectors,
    which stamp "high") never fires the detector."""
    rec = _yc_absent_record()
    rec["match_confidence"] = "low"
    claim = Claim(
        type="employment",
        employer="Acme Robotics",
        title="Founder",
        assertion="Founder of Acme Robotics (YC S24).",
        evidence=[rec],
    )
    assert detect_registry_absence([claim]) == []


def _ws4_full_scan_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jane-sample/",
        "scan_type": "person",
        "identity": {
            "name": "Jane Sample",
            "headline": "Founder at Acme Robotics",
            "current_company": "Acme Robotics",
        },
        "experience": [
            {
                "title": "Founder (YC S24)",
                "company": "Acme Robotics",
                "start_date": "Jan 2024",
                "end_date": "Present",
            }
        ],
        "education": [],
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }


def test_registry_absence_scores_sus_end_to_end(monkeypatch):
    """End to end: a YC-invoking claim with a checked-absent accelerator record
    lands in the SUS band, never DISPROVEN. Twin: the operator CONFIRMING it
    (the rename case) drops the contribution to CLEAR."""

    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = [_yc_absent_record()]
        return claim

    monkeypatch.setattr(verify, "gather_evidence", _gather)
    d = build_dossier(
        _ws4_full_scan_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None
    )
    kinds = {m["kind"] for m in d.mismatches}
    assert "REGISTRY_ABSENCE" in kinds, d.mismatches
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)

    # Twin: an operator who CONFIRMS the claim (the company is in YC under
    # another name) drops the contribution.
    class _ConfirmingStub(DisciplinedStubProvider):
        def assign_tiers_and_verdict(self, dossier):
            for c in dossier.claims:
                c.tier = EvidenceTier.CONFIRMED
            from detective.llm import compute_founder_score as _cfs

            dossier.larp_score = _cfs(dossier.claims)
            dossier.verdict = "stub verdict: confirmed under another name"
            return dossier

    monkeypatch.setattr(verify, "gather_evidence", _gather)
    confirmed = build_dossier(
        _ws4_full_scan_raw(), provider=_ConfirmingStub(), emit=lambda *a: None
    )
    assert confirmed.founder_larp_score is not None
    assert confirmed.founder_larp_score < 33
