"""Regression contracts for retrieval quality before absence scoring.

These tests intentionally isolate evidence quality from the reasoning model.
An incomplete or namesake-only retrieval must not be treated as a completed,
claim-specific search and must not create a repeatable mid-SUS score.
"""

from __future__ import annotations

from detective import dossier
from detective import llm
from detective.models import Claim, EvidenceTier


def _incomplete_employment_evidence(employer: str) -> list[dict]:
    """Evidence that mentions the employer, but never the claimed role."""
    return [
        {
            "source_url": "https://news.example/company",
            "source_name": "news_coverage",
            "match_confidence": "medium",
            "snippet": f"Independent coverage says {employer} reported quarterly earnings.",
        },
        {
            "source_url": "https://safety.example/advisory",
            "query_role": "adversarial",
            "snippet": f"How to recognize phishing messages that impersonate {employer}.",
        },
    ]


def _notable_role(employer: str) -> Claim:
    return Claim(
        type="employment",
        employer=employer,
        title="Software Engineering Intern",
        assertion=f"Worked as Software Engineering Intern at {employer}.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=_incomplete_employment_evidence(employer),
    )


def test_unrelated_employer_news_and_adversarial_hits_are_not_completed_role_search():
    claim = _notable_role("Example Cloud")

    assert not dossier._claim_was_searched(claim)
    assert not llm._claim_was_searched(claim)
    assert llm.compute_founder_score([claim]) <= 33


def test_namesake_identity_evidence_cannot_confirm_the_profile_subject():
    claim = Claim(
        type="identity",
        assertion="A real person named Jordan Sample exists and matches this profile.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=[
            {
                "source_url": "https://github.example/JordanSample99",
                "source_name": "github",
                "match_confidence": "medium",
                "snippet": (
                    "GitHub account JordanSample99 is a name-pattern match only and is not "
                    "confirmed to be the claimed person. Jordan Sample lists Example Cloud "
                    "in a bio."
                ),
            }
        ],
    )
    identity = {"name": "Jordan Sample", "current_company": "Example Cloud"}

    findings = dossier.detect_gap([claim], identity=identity)

    assert len(findings) == 1
    assert findings[0].kind == "GAP"


def test_incomplete_high_footprint_searches_do_not_saturate_at_58():
    claims = [
        _notable_role("Example Cloud"),
        _notable_role("Example Trading"),
        _notable_role("Example University"),
        _notable_role("Example Foundation"),
    ]

    score = llm.compute_founder_score(claims)

    assert score is not None
    assert score <= 33


def test_people_data_republication_cannot_suppress_role_gap():
    claim = Claim(
        type="employment",
        employer="Ditto",
        title="Head of Growth",
        assertion="Worked as Head of Growth at Ditto.",
        tier=EvidenceTier.UNVERIFIED,
        expected_footprint="high",
        evidence=[
            {
                "source_url": "https://rocketreach.co/example",
                "source_class": "republication",
                "relationship": "third_party",
                "snippet": "Elena Chen is Head of Growth at Ditto.",
            }
        ],
    )

    findings = dossier.detect_gap(
        [claim],
        identity={"name": "Elena Chen", "current_company": "Ditto"},
    )

    assert len(findings) == 1
    assert findings[0].kind == "GAP"
