"""End-to-end regression lock for the void leak (the dark-backend false-SUS).

The invariant: a real person whose notable-but-private roles return no web trace
while the search backend is DARK (configured but unreachable/quota-exhausted)
cannot be driven into the LARP band, or even into GAP-driven SUS, by absence
alone. The protection must come from HONEST LABELING (search_unavailable), not
from the shallow-scan gate: these fixtures are FULL scans, the dangerous case
the shallow gate does not cover.

Three tests, mirrored so the fix cannot silently over-suppress:
  1. dark search + full scan -> not LARP, not even GAP-SUS (the private-roles shape).
  2. HEALTHY search, same shape -> GAP + SUS still reachable (real-fake guard).
  3. dark search but a real DISPROVEN contradiction -> still reaches LARP (a dark
     backend suppresses absence-suspicion only, never contradiction scoring).

Test-only: no production change is expected here (workstream A already landed
the labeling fix). Reuses the DisciplinedStubProvider pattern from
tests/test_dossier.py but emits FIXED notable claims so GAP eligibility does not
depend on the mechanical decomposer.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import pytest

from detective import search
from detective import verify
from detective.dossier import build_dossier
from detective.llm import LLMProvider, compute_founder_score
from detective.models import Claim, EvidenceTier


# ---------------------------------------------------------------------------
# Fixtures: a FULL live scrape with notable private roles (all synthetic).
# ---------------------------------------------------------------------------


def _private_roles_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jordan-sample/",
        "scan_type": "person",
        "identity": {"name": "Jordan Sample", "current_company": "Acme Robotics"},
        "experience": [
            {"title": "IT Department", "company": "Sample State University"},
            {"title": "Founder", "company": "Acme Robotics"},
            {"title": "Software Engineer", "company": "Initech"},
        ],
        # A live scrape that parsed three roles: scan_depth == "full". This is
        # the dangerous case (the shallow gate does NOT protect it).
        "_extraction": {"method": "live_scrape", "experience_count": 3},
    }


class _PrivateRolesStub(LLMProvider):
    """Emits three FIXED notable claims (types in the GAP-notable set, high
    expected_footprint) and applies the operator discipline over the injected
    mismatch records: a contradiction -> DISPROVEN, a GAP -> UNVERIFIED + high
    footprint (SUS), otherwise UNVERIFIED. Sets larp_score off the tiers so
    build_dossier computes founder_larp_score, exactly like the real providers.
    """

    def decompose_claims(self, raw_profile: dict) -> list[Claim]:
        return [
            Claim(
                type="employment",
                employer="Sample State University",
                title="IT Department",
                assertion="Works in the IT department at a large university IT department.",
                expected_footprint="high",
            ),
            Claim(
                type="employment",
                employer="Acme Robotics",
                title="Founder",
                assertion="Founder who earned about $100k from Acme Robotics.",
                expected_footprint="high",
            ),
            Claim(
                type="identity",
                assertion="Jordan Sample is a software engineer who ships real code for Initech.",
                expected_footprint="high",
            ),
        ]

    def assign_tiers_and_verdict(self, dossier):
        has_disproven = False
        for c in dossier.claims:
            names = {(e.get("source_name") or "") for e in c.evidence}
            if "mismatch_contradiction" in names or "mismatch_autonomy" in names:
                c.tier = EvidenceTier.DISPROVEN
                c.expected_footprint = "high"
                has_disproven = True
            elif "mismatch_gap" in names:
                c.tier = EvidenceTier.UNVERIFIED
                c.expected_footprint = "high"
            else:
                c.tier = EvidenceTier.UNVERIFIED
        dossier.larp_score = compute_founder_score(dossier.claims)
        dossier.verdict = "stub verdict"
        return dossier


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    search._brave_exhausted_until = 0.0
    search._searxng_dead_until = 0.0
    yield
    search._brave_exhausted_until = 0.0
    search._searxng_dead_until = 0.0


def _dark_search(monkeypatch):
    # Configured (available) but dark (not healthy): the reported environment.
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: False)


def _healthy_search(monkeypatch):
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: True)


def _gather_leaves_empty(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    # Dark web + dark connectors: every claim comes back with no evidence.
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dark_search_full_scan_not_larp(monkeypatch):
    monkeypatch.setattr(verify, "gather_evidence", _gather_leaves_empty)
    _dark_search(monkeypatch)

    d = build_dossier(_private_roles_raw(), provider=_PrivateRolesStub(), emit=lambda *a: None)

    # Every claim was stamped "could not look", not "looked and found nothing".
    for c in d.claims:
        assert len(c.evidence) == 1
        assert c.evidence[0]["source_name"] == "search_unavailable"
    # No GAP could fire off an unsearched claim.
    assert all(m["kind"] != "GAP" for m in d.mismatches)
    # The protection came from labeling, not the shallow gate.
    assert d.scan_depth == "full"
    # No disproven, no countable absence: CLEAR band (computes to 0).
    assert d.founder_larp_score is not None
    assert d.founder_larp_score < 33


def test_healthy_search_same_shape_still_susable(monkeypatch):
    monkeypatch.setattr(verify, "gather_evidence", _gather_leaves_empty)
    _healthy_search(monkeypatch)

    d = build_dossier(_private_roles_raw(), provider=_PrivateRolesStub(), emit=lambda *a: None)

    # A healthy search that genuinely found nothing on a notable claim STILL
    # stamps searched_no_results and remains a legitimate GAP candidate.
    for c in d.claims:
        assert c.evidence[0]["source_name"] == "searched_no_results"
    assert any(m["kind"] == "GAP" for m in d.mismatches)
    # SUS reachable, LARP not (absence never reaches the fraud band).
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66


def test_disproven_still_reaches_larp_under_dark_search(monkeypatch):
    def _gather_one_contradiction(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        # One real adverse finding from a connector (evidence present) on the
        # Acme Robotics employment claim; every other claim stays dark/empty.
        if (claim.employer or "") == "Acme Robotics":
            claim.evidence = [
                {
                    "source_url": "https://news.test/acme",
                    "snippet": "Acme Robotics has no record of this person ever being employed there.",
                    "source_name": "",
                    "match_confidence": "medium",
                }
            ]
        return None

    monkeypatch.setattr(verify, "gather_evidence", _gather_one_contradiction)
    _dark_search(monkeypatch)

    d = build_dossier(_private_roles_raw(), provider=_PrivateRolesStub(), emit=lambda *a: None)

    assert any(c.tier is EvidenceTier.DISPROVEN for c in d.claims)
    # Darkness suppresses absence-suspicion only, never contradiction scoring.
    assert d.founder_larp_score is not None
    assert d.founder_larp_score >= 66
