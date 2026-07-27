"""Code-enforced safety tests for API and manual operator judgment."""

from __future__ import annotations

from detective.llm import compute_company_score, enforce_reasoning_safety
from detective.models import Claim, Dossier, EvidenceTier, MetricEntry


def test_unsupported_disproven_is_downgraded():
    dossier = Dossier(
        profile_url="https://example.com",
        claims=[
            Claim(
                type="employment",
                employer="Acme",
                title="Founder",
                assertion="Worked at Acme.",
                tier=EvidenceTier.DISPROVEN,
                evidence=[
                    {
                        "source_url": "https://example.com",
                        "snippet": "No mention of this person.",
                    }
                ],
            )
        ],
        larp_score=99,
        verdict="This person is a liar and a fraudster.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.claims[0].tier == EvidenceTier.UNVERIFIED
    assert dossier.larp_score < 66
    assert "No claim was actively disproven" in dossier.verdict


def test_self_controlled_role_evidence_cannot_keep_confirmed():
    claim = Claim(
        type="employment",
        employer="Fern",
        title="Founder",
        assertion="Worked as Founder at Fern.",
        tier=EvidenceTier.CONFIRMED,
        evidence=[
            {
                "source_url": "https://jordan-rivera.example",
                "relationship": "subject_controlled",
                "source_class": "search_index",
                "claim_relevance": "substantive",
                "snippet": "Jordan Rivera previously built Fern.",
            },
            {
                "source_url": "https://trytalkr.example",
                "source_name": "techstack",
                "runtime_app_hint": "interaction_verified",
                "snippet": "A live web application exists.",
            },
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        claims=[claim],
        verdict="Every role was confirmed.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.claims[0].tier == EvidenceTier.UNVERIFIED
    assert "unsupported CONFIRMED" in dossier.claims[0].notes


def test_aggregator_role_record_cannot_keep_confirmed():
    claim = Claim(
        type="employment",
        employer="Ditto",
        title="Head of Growth",
        assertion="Worked as Head of Growth at Ditto.",
        tier=EvidenceTier.CONFIRMED,
        evidence=[
            {
                "source_url": "https://rocketreach.co/example",
                "relationship": "third_party",
                "source_class": "republication",
                "claim_relevance": "substantive",
                "snippet": "Elena Chen is Head of Growth at Ditto.",
            }
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        claims=[claim],
        verdict="Every role was confirmed.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.claims[0].tier == EvidenceTier.UNVERIFIED


def test_independent_role_binding_can_keep_confirmed():
    claim = Claim(
        type="employment",
        employer="Ditto",
        title="Head of Growth",
        assertion="Worked as Head of Growth at Ditto.",
        tier=EvidenceTier.CONFIRMED,
        evidence=[
            {
                "source_url": "https://news.example/ditto-profile",
                "relationship": "third_party",
                "source_class": "search_index",
                "claim_relevance": "substantive",
                "snippet": "Elena Chen, Head of Growth at Ditto, discussed the product.",
            }
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        claims=[claim],
        verdict="The Ditto role was independently confirmed.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.claims[0].tier == EvidenceTier.CONFIRMED


def test_high_confidence_mismatch_can_support_disproven():
    dossier = Dossier(
        profile_url="https://example.com",
        claims=[
            Claim(
                type="employment",
                employer="Acme",
                title="Founder",
                assertion="Worked at Acme.",
                tier=EvidenceTier.DISPROVEN,
                evidence=[
                    {
                        "source_url": "internal://mismatch/mismatch_contradiction",
                        "source_name": "mismatch_contradiction",
                        "match_confidence": "high",
                        "snippet": "Acme says no such role existed.",
                    }
                ],
            )
        ],
        larp_score=99,
        verdict="A claim was disproven.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.claims[0].tier == EvidenceTier.DISPROVEN
    assert dossier.larp_score >= 66


def test_search_unavailable_cannot_be_described_as_completed_absence():
    claim = Claim(
        type="employment",
        employer="Acme Research",
        title="Researcher",
        assertion="Worked as a researcher at Acme Research.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=[
            {
                "source_url": "internal://search-unavailable",
                "source_name": "search_unavailable",
                "snippet": "The search channel was unavailable.",
                "match_confidence": "low",
                "weight": 0.0,
            }
        ],
        notes="A targeted follow-up found no roster or announcement trace.",
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        claims=[claim],
        verdict="Acme Research went looking for receipts and found only fog.",
    )

    enforce_reasoning_safety(dossier)

    assert "unavailable" in dossier.claims[0].notes.lower()
    assert "found no" not in dossier.claims[0].notes.lower()
    assert "no claim was actively disproven" in dossier.verdict.lower()


def test_search_unavailable_with_product_evidence_neutralizes_missing_receipt_claim():
    claim = Claim(
        type="employment",
        employer="Vercel",
        title="CEO",
        assertion="Worked as CEO at Vercel.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=[
            {
                "source_url": "https://vercel.com",
                "source_name": "product_site",
                "snippet": "The product exists, but this does not prove the person's role.",
            },
            {
                "source_url": "internal://search-unavailable",
                "source_name": "search_unavailable",
                "snippet": "The claim-specific web lookup was unavailable.",
                "weight": 0.0,
            },
        ],
        notes="The product is real, but the role is missing a receipt.",
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        claims=[claim],
        larp_score=35,
        verdict="The Vercel CEO role is missing the one receipt that matters.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.larp_score == 0
    assert "unavailable" in dossier.claims[0].notes.lower()
    assert "no claim was actively disproven" in dossier.verdict.lower()


def test_unsupported_disproven_downgrade_always_rewrites_the_verdict():
    claim = Claim(
        type="employment",
        employer="Acme",
        assertion="Founder at Acme.",
        tier=EvidenceTier.DISPROVEN,
        evidence=[
            {
                "source_url": "https://example.com/acme",
                "snippet": "A generic result about Acme.",
                "match_confidence": "medium",
            }
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        claims=[claim],
        verdict="The whole claim is pure bullshit.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.claims[0].tier == EvidenceTier.UNVERIFIED
    assert dossier.verdict != "The whole claim is pure bullshit."
    assert "no claim was actively disproven" in dossier.verdict.lower()


def test_reasoning_safety_recomputes_person_score_with_scan_depth():
    claim = Claim(
        type="employment",
        employer="Acme",
        title="Founder",
        assertion="Founder at Acme.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=[
            {
                "source_url": "internal://search/no-results",
                "source_name": "searched_no_results",
                "snippet": "A completed search returned no results.",
            }
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        scan_depth="shallow",
        claims=[claim],
        larp_score=35,
    )

    enforce_reasoning_safety(dossier)

    assert dossier.larp_score == 0


def test_completed_high_footprint_absence_keeps_sharp_suspicious_verdict():
    claim = Claim(
        type="employment",
        employer="Acme",
        title="Founder",
        assertion="Founder at Acme.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=[
            {
                "source_url": "internal://search/no-results",
                "source_name": "searched_no_results",
                "snippet": "A completed, claim-specific search returned no results.",
            }
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        scan_depth="full",
        claims=[claim],
        larp_score=35,
        verdict=(
            "This looks like bullshit: a supposedly public founder role left "
            "no corroborating trace after the search actually completed."
        ),
    )

    enforce_reasoning_safety(dossier)

    assert dossier.larp_score > 0
    assert dossier.verdict.startswith("This looks like bullshit")


def test_low_footprint_internship_cannot_keep_a_void_roast():
    claim = Claim(
        type="employment",
        employer="Example Cloud",
        title="Software Engineering Intern",
        assertion="Interned at Example Cloud.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=[
            {
                "source_url": "internal://search/coverage",
                "source_name": "search_coverage",
                "verification_state": "completed",
                "raw_count": 5,
                "relevant_count": 0,
            }
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        scan_depth="full",
        claims=[claim],
        larp_score=37,
        verdict="The internship left no trace and the receipts are missing.",
    )

    enforce_reasoning_safety(dossier)

    assert dossier.claims[0].expected_footprint == "low"
    assert dossier.larp_score <= 33
    assert "no claim was actively disproven" in dossier.verdict.lower()


def test_browser_verified_product_cannot_be_called_traceless():
    claim = Claim(
        type="employment",
        employer="Fern",
        title="Founder",
        assertion="Founder at Fern.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        product_url="https://trytalkr.example",
        evidence=[
            {
                "source_url": "https://trytalkr.example",
                "source_name": "product_site",
                "resolution": "resolved",
                "snippet": "The linked web product exists.",
            },
            {
                "source_url": "https://trytalkr.example/auth",
                "source_name": "techstack",
                "runtime_app_hint": "interaction_verified",
                "snippet": "Browser interaction_verified on a real auth surface.",
            },
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        scan_type="person",
        scan_depth="full",
        claims=[claim],
        verdict="Fern left no public smoke trail. The founder title is all fireworks.",
    )

    enforce_reasoning_safety(dossier)

    assert "live, browser-verified application surface" in dossier.verdict
    assert "suspicious gap is the claimed Founder attribution" in dossier.verdict
    assert "all fireworks" in dossier.verdict


def test_runtime_unavailable_does_not_invent_a_browser_verified_app():
    claim = Claim(
        type="employment",
        employer="Fern",
        title="Founder",
        assertion="Founder at Fern.",
        tier=EvidenceTier.UNVERIFIED,
        product_url="https://trytalkr.example",
        evidence=[
            {
                "source_url": "https://trytalkr.example",
                "source_name": "product_site",
                "resolution": "resolved",
            },
            {
                "source_url": "https://trytalkr.example",
                "source_name": "techstack",
                "runtime_app_hint": "unavailable",
            },
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/profile",
        claims=[claim],
        verdict="The claimed founder role has no independent receipt.",
    )

    enforce_reasoning_safety(dossier)

    assert "browser-verified application surface" not in dossier.verdict


def test_company_metrics_cannot_reach_larp_band_without_disproof():
    metrics = [
        MetricEntry(name="product_realness", weight=3, active=True, score_0_10=10),
        MetricEntry(name="zombie_liveness", weight=2, active=True, score_0_10=10),
        MetricEntry(name="buildability", weight=1, active=True, score_0_10=10),
    ]
    claims = [
        Claim(
            type="company_overview",
            assertion="Acme exists.",
            tier=EvidenceTier.UNVERIFIED,
        )
    ]

    assert compute_company_score(metrics, claims=claims) == 65
