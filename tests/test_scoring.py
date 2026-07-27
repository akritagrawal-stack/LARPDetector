"""Tests for the two-score LARP scoring system: founder_larp_score (person
scans, from claim tiers) and company_larp_score (company scans, a weighted
composite over metric_breakdown). Offline, no network. No em dashes (house
rule).
"""

from __future__ import annotations

import json
from pathlib import Path

from detective.llm import (
    build_metric_breakdown,
    compute_company_score,
    compute_founder_score,
    normalize_expected_footprints,
    sync_buildability_metric,
)
from detective.models import Buildability, Claim, Dossier, EvidenceTier, MetricEntry

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# compute_founder_score
# ---------------------------------------------------------------------------


def test_founder_score_none_with_no_claims():
    assert compute_founder_score([]) is None


def test_founder_score_all_confirmed_is_zero():
    claims = [
        Claim(type="identity", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Cluely", title="Founder and CEO", tier=EvidenceTier.CONFIRMED),
        Claim(type="education", employer="Columbia University", title="Computer Science", tier=EvidenceTier.CONFIRMED),
    ]
    assert compute_founder_score(claims) == 0


def test_founder_score_one_specific_lie_is_not_diluted_by_confirmed_claims():
    """A single DISPROVEN employment claim (employer + title both given) must
    drive the score up hard, regardless of how many CONFIRMED claims sit
    alongside it: a noisy-OR combination, not a plain average that dilutes.
    """
    few_confirmed = [
        Claim(type="identity", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Cluely", title="Founder and CEO", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Cluely", title="ARR claim", tier=EvidenceTier.DISPROVEN),
    ]
    many_confirmed = few_confirmed + [
        Claim(type="employment", employer="X", title="Advisor", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Y", title="Advisor", tier=EvidenceTier.CONFIRMED),
        Claim(type="education", employer="Columbia University", title="CS", tier=EvidenceTier.CONFIRMED),
    ]
    score_few = compute_founder_score(few_confirmed)
    score_many = compute_founder_score(many_confirmed)
    assert score_few is not None and score_many is not None
    # Piling on more CONFIRMED claims must not meaningfully dilute the one
    # real lie's contribution (CONFIRMED claims carry factor exactly 0).
    assert abs(score_few - score_many) <= 1
    assert score_few >= 70  # one clean job/ARR-style fabrication drives it up hard


def test_public_footprint_normalization_separates_interns_from_officers():
    intern = Claim(
        type="employment",
        employer="Amazon Web Services (AWS)",
        title="Software Engineering Intern",
        expected_footprint="high",
    )
    analyst = Claim(
        type="employment",
        employer="Jane Street",
        title="Academy of Mathematics and Programming",
        expected_footprint="high",
    )
    president = Claim(
        type="employment",
        employer="Coding For Medicine",
        title="President",
        expected_footprint="high",
    )

    normalize_expected_footprints([intern, analyst, president])

    assert intern.expected_footprint == "low"
    assert analyst.expected_footprint == "low"
    assert president.expected_footprint == "high"


def test_founder_score_fuzzy_vague_lie_scores_lower_than_specific_lie():
    specific = [
        Claim(type="employment", employer="Acme", title="VP Engineering", tier=EvidenceTier.DISPROVEN),
    ]
    vague = [
        Claim(type="employment", employer="", title="", tier=EvidenceTier.DISPROVEN),
    ]
    assert compute_founder_score(specific) > compute_founder_score(vague)


def test_founder_score_unverified_contributes_little_and_does_not_compound():
    """A resume that is merely unconfirmed (never contradicted) must stay low
    no matter how many claims it has: the flat cap must hold even as claim
    count grows, i.e. no noisy-OR blow-up on UNVERIFIED.
    """
    small = [Claim(type="employment", employer="Acme", title="Engineer", tier=EvidenceTier.UNVERIFIED)]
    large = small * 1 + [
        Claim(type="employment", employer=f"Co{i}", title="Engineer", tier=EvidenceTier.UNVERIFIED)
        for i in range(20)
    ]
    score_large = compute_founder_score(large)
    assert score_large is not None
    assert score_large <= 15  # _FOUNDER_UNVERIFIED_MAX_BUMP cap


def test_founder_score_george_santos_lands_very_high():
    # Mostly-fabricated resume: identity real, everything else disproven.
    claims = [
        Claim(type="identity", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Goldman Sachs", title="Analyst", tier=EvidenceTier.DISPROVEN),
        Claim(type="employment", employer="Citigroup", title="Analyst", tier=EvidenceTier.DISPROVEN),
        Claim(type="education", employer="Baruch College", title="", tier=EvidenceTier.DISPROVEN),
    ]
    score = compute_founder_score(claims)
    assert score is not None
    assert score >= 85


def test_founder_score_roy_lee_moderate_single_lie():
    # Real founder, real company, real seed round, real education; one
    # disproven public ARR claim. Should land meaningfully high (a real,
    # documented lie) but well below a total fabricator like Santos.
    claims = [
        Claim(type="identity", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Cluely", title="Founder and CEO", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Cluely", title="Raised 5.3 million dollar seed round for Cluely", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Cluely", title="Publicly stated 7 million dollar ARR for Cluely", tier=EvidenceTier.DISPROVEN),
        Claim(type="education", employer="Columbia University", title="Computer Science (suspended 2025)", tier=EvidenceTier.CONFIRMED),
    ]
    santos_claims = [
        Claim(type="identity", tier=EvidenceTier.CONFIRMED),
        Claim(type="employment", employer="Goldman Sachs", title="Analyst", tier=EvidenceTier.DISPROVEN),
        Claim(type="employment", employer="Citigroup", title="Analyst", tier=EvidenceTier.DISPROVEN),
        Claim(type="education", employer="Baruch College", title="", tier=EvidenceTier.DISPROVEN),
    ]
    roy_score = compute_founder_score(claims)
    santos_score = compute_founder_score(santos_claims)
    assert roy_score is not None and santos_score is not None
    assert 0 < roy_score < santos_score


# ---------------------------------------------------------------------------
# Change A: high-expected-footprint UNVERIFIED claims read SUS, not CLEAR
# ---------------------------------------------------------------------------


def _load_all_unverified_fixture() -> Dossier:
    """The synthetic all-UNVERIFIED notable-claims dossier (a fictional person
    claiming Google / McKinsey / Stripe / Stanford, none corroborated). Loaded
    from tests/fixtures so the same file feeds this offline test and the Gemini
    eval cache. Claims arrive tier=UNVERIFIED with non-corroborating evidence
    present and expected_footprint unset; each test sets footprint itself.
    """
    raw = json.loads((_FIXTURES_DIR / "all_unverified_notable.json").read_text(encoding="utf-8"))
    return Dossier.from_dict(raw)


def _set_footprint(dossier: Dossier, value: str) -> None:
    for c in dossier.claims:
        c.expected_footprint = value


def test_all_unverified_high_footprint_lands_in_sus_band():
    """The headline Change A case: a person whose every NOTABLE claim came back
    uncorroborable must read SUS (34 to 65), not CLEAR. This is the exact
    real-world miss the owner hit (scanned someone, could not verify any of his
    work history, and it still read clear).
    """
    dossier = _load_all_unverified_fixture()
    # Sanity: the fixture really is all-unverified, with evidence present.
    assert all(c.tier == EvidenceTier.UNVERIFIED for c in dossier.claims)
    assert all(c.evidence for c in dossier.claims)

    _set_footprint(dossier, "high")
    score = compute_founder_score(dossier.claims)
    assert score is not None
    assert 34 <= score <= 65, f"expected SUS band, got {score}"
    # And never the top LARP band: only DISPROVEN evidence earns that.
    assert score < 66


def test_all_unverified_low_footprint_stays_clear():
    """Same fixture, same UNVERIFIED tiers, but marked low expected footprint:
    a legitimately low-footprint person must NOT be pushed to SUS just for
    being hard to verify. Proves the clear-before / sus-after split hinges on
    footprint, not on the mere absence of corroboration.
    """
    dossier = _load_all_unverified_fixture()
    _set_footprint(dossier, "low")
    score = compute_founder_score(dossier.claims)
    assert score is not None
    assert score <= 33, f"expected CLEAR band, got {score}"


def test_all_unverified_high_footprint_but_no_search_stays_clear():
    """Guardrail: the SUS contribution only fires when evidence gathering
    actually RAN. Strip the evidence (simulating "we never searched") and even
    high-footprint unverified claims must stay CLEAR: absence of a search is
    not absence of a trace.
    """
    dossier = _load_all_unverified_fixture()
    _set_footprint(dossier, "high")
    for c in dossier.claims:
        c.evidence = []
    score = compute_founder_score(dossier.claims)
    assert score is not None
    assert score <= 33, f"expected CLEAR band when no search ran, got {score}"


def test_one_high_footprint_notable_unverified_reaches_sus():
    """Calibration change (owner priority): a SINGLE UNVERIFIED claim at a
    NOTABLE employer (high expected_footprint, e.g. "Data Analyst at Southwest
    Airlines") is a real yellow flag (should be verifiable, is not) and must
    now reach the SUS band ON ITS OWN (~34 to 45), catching resume-padding.
    Two such claims sit deeper in SUS. Neither ever reaches the LARP band.
    """
    ev = [{"source_url": "http://x", "snippet": "the company exists; no mention of this person"}]
    one = [Claim(type="employment", employer="Southwest Airlines", title="Data Analyst",
                 tier=EvidenceTier.UNVERIFIED, expected_footprint="high", evidence=ev)]
    two = one + [Claim(type="employment", employer="Google", title="Engineer",
                       tier=EvidenceTier.UNVERIFIED, expected_footprint="high", evidence=ev)]
    score_one = compute_founder_score(one)
    score_two = compute_founder_score(two)
    assert score_one is not None and 34 <= score_one <= 45, score_one
    assert score_two is not None and 34 <= score_two < 66, score_two
    assert score_two > score_one  # a second notable-unverifiable deepens SUS


def test_one_low_footprint_unverified_stays_clear():
    """The false-positive guard that MUST survive the calibration change: an
    ordinary thin-footprint / obscure-role person (low expected_footprint) with
    an uncorroborated claim STAYS CLEAR (<=33). The notable-vs-obscure split
    (expected_footprint), not the mere absence of corroboration, is what decides
    SUS: we never flag a legitimately low-footprint person for being hard to
    verify.
    """
    ev = [{"source_url": "http://x", "snippet": "no public trace, obscure tiny shop"}]
    one_low = [Claim(type="employment", employer="Corner Coffee LLC", title="Barista",
                     tier=EvidenceTier.UNVERIFIED, expected_footprint="low", evidence=ev)]
    two_low = one_low + [Claim(type="employment", employer="Local Handyman", title="Helper",
                               tier=EvidenceTier.UNVERIFIED, expected_footprint="low", evidence=ev)]
    assert compute_founder_score(one_low) <= 33
    assert compute_founder_score(two_low) <= 33


def test_unverified_alone_can_never_reach_larp_band():
    """Hard cap: no matter how many high-footprint claims come back empty,
    unverifiability alone can never reach the top LARP band (>=66). That band
    stays reserved for DISPROVEN (proven-false) claims.
    """
    ev = [{"source_url": "http://x", "snippet": "the company exists; no mention of this person"}]
    many = [
        Claim(type="employment", employer=f"BigCo{i}", title="VP",
              tier=EvidenceTier.UNVERIFIED, expected_footprint="high", evidence=ev)
        for i in range(40)
    ]
    score = compute_founder_score(many)
    assert score is not None
    assert score < 66


def test_fully_confirmed_profile_still_clear_with_footprint_set():
    """A fully-CONFIRMED profile must still score ~0 (CLEAR) even when every
    claim is marked high expected footprint: footprint only ever matters for
    UNVERIFIED claims, never CONFIRMED ones.
    """
    claims = [
        Claim(type="identity", tier=EvidenceTier.CONFIRMED, expected_footprint="high"),
        Claim(type="employment", employer="Cluely", title="Founder and CEO",
              tier=EvidenceTier.CONFIRMED, expected_footprint="high"),
        Claim(type="education", employer="Columbia University", title="CS",
              tier=EvidenceTier.CONFIRMED, expected_footprint="high"),
    ]
    assert compute_founder_score(claims) == 0


# ---------------------------------------------------------------------------
# build_metric_breakdown: active-flag logic
# ---------------------------------------------------------------------------


def test_metric_breakdown_has_exactly_8_rows_no_backer_authenticity():
    breakdown = build_metric_breakdown([])
    names = [m.name for m in breakdown]
    assert len(names) == 8
    assert "backer_authenticity" not in names
    assert names == [
        "raise_inflation",
        "reach_vs_footprint",
        "product_realness",
        "headcount_inflation",
        "proprietary_ai_gap",
        "zombie_liveness",
        "key_role_coverage",
        "buildability",
    ]


def test_metric_breakdown_always_active_rows():
    breakdown = build_metric_breakdown([])
    by_name = {m.name: m for m in breakdown}
    assert by_name["product_realness"].active is True
    assert by_name["zombie_liveness"].active is True
    assert by_name["buildability"].active is True


def test_metric_breakdown_conditional_rows_inactive_with_no_signal():
    breakdown = build_metric_breakdown([])
    by_name = {m.name: m for m in breakdown}
    assert by_name["raise_inflation"].active is False
    assert by_name["headcount_inflation"].active is False
    assert by_name["proprietary_ai_gap"].active is False
    assert by_name["key_role_coverage"].active is False
    # No user_count claim at all: nothing to judge reach on.
    assert by_name["reach_vs_footprint"].active is False


def test_reach_vs_footprint_active_on_consumer_scale_claim():
    claims = [Claim(type="user_count", employer="X", title="users", assertion="X claims 100k users.")]
    breakdown = build_metric_breakdown(claims)
    by_name = {m.name: m for m in breakdown}
    assert by_name["reach_vs_footprint"].active is True


def test_reach_vs_footprint_inactive_on_pure_b2b_claim():
    """Regression guard: a B2B seat/team count must NOT fire reach_vs_footprint
    just because the mechanical phrasing always says "claims N users" in the
    assertion text. The real unit lives on claim.title.
    """
    claims = [Claim(type="user_count", employer="X", title="companies", assertion="X claims 500 users (source text: \"500 companies\").")]
    breakdown = build_metric_breakdown(claims)
    by_name = {m.name: m for m in breakdown}
    assert by_name["reach_vs_footprint"].active is False


def test_raise_inflation_active_only_with_funding_claim():
    claims = [Claim(type="funding", employer="X", assertion="X claims to have raised $2 million.")]
    breakdown = build_metric_breakdown(claims)
    by_name = {m.name: m for m in breakdown}
    assert by_name["raise_inflation"].active is True


def test_headcount_and_ai_gated_rows_activate_on_their_claim_types():
    claims = [
        Claim(type="headcount", employer="X", assertion="X claims a team of 12."),
        Claim(type="proprietary_tech", employer="X", assertion="X claims proprietary AI."),
    ]
    breakdown = build_metric_breakdown(claims)
    by_name = {m.name: m for m in breakdown}
    assert by_name["headcount_inflation"].active is True
    assert by_name["proprietary_ai_gap"].active is True
    assert by_name["key_role_coverage"].active is True


# ---------------------------------------------------------------------------
# sync_buildability_metric
# ---------------------------------------------------------------------------


def test_sync_buildability_metric_maps_tier_to_score():
    breakdown = build_metric_breakdown([])
    sync_buildability_metric(breakdown, Buildability(tier="TRIVIAL", note="thin wrapper"))
    by_name = {m.name: m for m in breakdown}
    assert by_name["buildability"].score_0_10 == 3
    assert by_name["buildability"].note == "thin wrapper"


def test_sync_buildability_metric_unfilled_tier_stays_none():
    breakdown = build_metric_breakdown([])
    sync_buildability_metric(breakdown, Buildability())
    by_name = {m.name: m for m in breakdown}
    assert by_name["buildability"].score_0_10 is None


# ---------------------------------------------------------------------------
# compute_company_score: composite math, redistribution, buildability cap
# ---------------------------------------------------------------------------


def test_company_score_none_when_empty():
    assert compute_company_score([]) is None


def test_company_score_none_until_active_scores_filled():
    breakdown = build_metric_breakdown([Claim(type="funding", employer="X")])
    # raise_inflation is active but score_0_10 is still None -> whole thing
    # stays None, never a false "clean" 0.
    assert compute_company_score(breakdown) is None


def test_company_score_all_zero_active_scores_is_zero():
    breakdown = [
        MetricEntry(name="product_realness", weight=3, score_0_10=0, active=True),
        MetricEntry(name="zombie_liveness", weight=2, score_0_10=0, active=True),
        MetricEntry(name="buildability", weight=1, score_0_10=0, active=True),
    ]
    assert compute_company_score(breakdown) == 0


def test_company_score_all_max_active_scores_is_100():
    breakdown = [
        MetricEntry(name="product_realness", weight=3, score_0_10=10, active=True),
        MetricEntry(name="zombie_liveness", weight=2, score_0_10=10, active=True),
        MetricEntry(name="buildability", weight=1, score_0_10=10, active=True),
    ]
    assert compute_company_score(breakdown) == 100


def test_company_score_inactive_rows_are_excluded_not_zero_dragged():
    with_inactive = [
        MetricEntry(name="product_realness", weight=3, score_0_10=10, active=True),
        MetricEntry(name="raise_inflation", weight=3, score_0_10=None, active=False),
        MetricEntry(name="buildability", weight=1, score_0_10=10, active=True),
    ]
    without_inactive = [
        MetricEntry(name="product_realness", weight=3, score_0_10=10, active=True),
        MetricEntry(name="buildability", weight=1, score_0_10=10, active=True),
    ]
    assert compute_company_score(with_inactive) == compute_company_score(without_inactive) == 100


def test_company_score_buildability_alone_cannot_push_to_high_larp():
    """LOW weight, hard-capped: a maxed-out buildability score next to two
    HIGH-weight zero scores must not meaningfully move the composite.
    """
    breakdown = [
        MetricEntry(name="raise_inflation", weight=3, score_0_10=0, active=True),
        MetricEntry(name="product_realness", weight=3, score_0_10=0, active=True),
        MetricEntry(name="buildability", weight=1, score_0_10=10, active=True),
    ]
    score = compute_company_score(breakdown)
    assert score is not None
    assert score <= 15  # a real product/funding story dominates; buildability only nudges


def test_company_score_buildability_cap_binds_with_few_active_metrics():
    """With only buildability + 2 others active, buildability's raw weight
    (1 out of 6 = 16.7%) exceeds the ~15% cap and must be scaled down, not
    left at its raw share.
    """
    breakdown = [
        MetricEntry(name="product_realness", weight=3, score_0_10=0, active=True),
        MetricEntry(name="zombie_liveness", weight=2, score_0_10=0, active=True),
        MetricEntry(name="buildability", weight=1, score_0_10=10, active=True),
    ]
    # Uncapped, buildability's raw share would be 1/6 = 16.7% of a max-10
    # score, i.e. round(10 * (1/6) * 10) = round(16.7) = 17. Capped at ~15%
    # share, the composite must land at or below that uncapped figure.
    score = compute_company_score(breakdown)
    assert score is not None
    uncapped_share = 1.0 / 6.0
    uncapped_score = round(100 * uncapped_share)
    assert score < uncapped_score


def test_company_score_headcount_partial_low_score_does_not_spike_composite():
    breakdown = [
        MetricEntry(name="product_realness", weight=3, score_0_10=0, active=True),
        MetricEntry(name="headcount_inflation", weight=2, score_0_10=1, active=True, note="uncorroborated, flag for review"),
        MetricEntry(name="buildability", weight=1, score_0_10=0, active=True),
    ]
    score = compute_company_score(breakdown)
    assert score is not None
    assert score <= 15
