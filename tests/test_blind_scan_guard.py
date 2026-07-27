"""A scan that never looked must SAY SO, without suppressing its score.

THE INCIDENT (live, 2026-07-24): Docker was down so SearXNG refused connections
and the Brave key was at HTTP 402. Four of nine claims carried ONLY the
search_unavailable marker, org_roster never fired, and the scan shipped
"42, SUS" with a roast. Every per-claim guard worked (nothing DISPROVEN,
absence never accused), and the 42 came from a claim that WAS searched. The
defect was not the number, it was that nothing told the reader half the profile
had never been checked.

WHY NOT JUST REFUSE TO SCORE: tried, and the existing contract tests rejected it
for good reasons (tests/test_judgment_principles.py P3/P4, and
tests/test_void_leak_regression.py):
  - a dark backend must land CLEAR, not suspicious: "not automatically
    suspicious when we were not even looking"
  - a genuine contradiction must still reach the fraud band EVEN under a dark
    backend, so suppressing the score would bury a real disproof behind an
    outage
So coverage is disclosed alongside the score, never in place of it.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

from detective import dossier as dossier_module
from detective.dossier import blind_scan_reason, build_dossier
from detective.llm import LLMProvider
from detective.models import Claim


def _searched(assertion="Founder at Acme Widgets"):
    return Claim(
        type="employment", employer="Acme Widgets", title="Founder", assertion=assertion,
        evidence=[{"source_url": "https://example.test/a", "snippet": "a real hit"}],
    )


def _never_looked(assertion="Founder at Northwind Labs"):
    return Claim(
        type="employment", employer="Northwind Labs", title="Founder", assertion=assertion,
        evidence=[{"source_name": "search_unavailable", "snippet": "channel dark", "weight": 0.0}],
    )


# ---------------------------------------------------------------------------
# blind_scan_reason: when is coverage bad enough to disclose
# ---------------------------------------------------------------------------


def test_all_claims_unsearched_is_disclosed():
    reason = blind_scan_reason([_never_looked(), _never_looked("x"), _never_looked("y")])
    assert reason
    assert "3 of 3" in reason
    # The wording must not read as an accusation: unchecked is not adverse.
    assert "not checked, not found wanting" in reason


def test_the_incident_ratio_is_disclosed():
    claims = [_never_looked(f"n{i}") for i in range(5)] + [_searched(f"s{i}") for i in range(5)]
    assert blind_scan_reason(claims)


def test_a_fully_searched_scan_says_nothing():
    assert blind_scan_reason([_searched("a"), _searched("b"), _searched("c")]) == ""


def test_a_minority_of_unsearched_claims_says_nothing():
    # One dark lookup among many real ones is normal. The per-claim marker
    # already neutralizes it, so a banner here would be noise.
    claims = [_searched(f"s{i}") for i in range(5)] + [_never_looked()]
    assert blind_scan_reason(claims) == ""


def test_a_profile_with_no_evidence_at_all_says_nothing():
    # Empty is not dark. A profile with nothing checkable is a different,
    # already-handled condition and must not be reported as an outage.
    assert blind_scan_reason([Claim(type="identity")]) == ""
    assert blind_scan_reason([]) == ""


# ---------------------------------------------------------------------------
# build_dossier: disclosure rides ALONGSIDE the score, never replaces it
# ---------------------------------------------------------------------------


class _ScoringProvider(LLMProvider):
    def __init__(self, claims):
        self._claims = claims
        self.assign_calls = 0

    def decompose_claims(self, raw_profile):
        return list(self._claims)

    def assign_tiers_and_verdict(self, dossier):
        self.assign_calls += 1
        dossier.larp_score = 42
        dossier.verdict = "a verdict"
        return dossier


def _profile():
    return {
        "profile_url": "https://www.linkedin.com/in/jane-doe",
        "scan_type": "person",
        "identity": {"name": "Jane Doe"},
        "experience": [{"title": "Founder", "company": "Acme Widgets"}],
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }


def _run(claims, monkeypatch):
    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        return claim  # evidence already set on the fixtures

    monkeypatch.setattr(dossier_module.verify, "gather_evidence", _gather)
    provider = _ScoringProvider(claims)
    return build_dossier(_profile(), provider=provider), provider


def test_a_blind_scan_still_runs_reasoning_then_uses_deterministic_score(monkeypatch):
    # The rules this protects: a dark scan lands CLEAR rather than suspicious,
    # and a real contradiction must still be able to reach the fraud band. Both
    # require the scoring path to run normally.
    dossier, provider = _run([_never_looked("a"), _never_looked("b")], monkeypatch)
    assert provider.assign_calls == 1
    assert dossier.larp_score == 0


def test_a_blind_scan_discloses_its_coverage(monkeypatch):
    dossier, _ = _run([_never_looked("a"), _never_looked("b")], monkeypatch)
    assert dossier.coverage_warning
    assert "LIMITED COVERAGE" in dossier.coverage_warning
    assert "2 of 2" in dossier.coverage_warning


def test_the_disclosure_does_not_overwrite_the_verdict(monkeypatch):
    # Coverage is a separate channel: it must not clobber what the reasoning
    # step concluded, or the two get confused for each other downstream.
    dossier, _ = _run([_never_looked("a"), _never_looked("b")], monkeypatch)
    assert dossier.verdict == "a verdict"


def test_a_well_covered_scan_carries_no_warning(monkeypatch):
    dossier, provider = _run([_searched("a"), _searched("b")], monkeypatch)
    assert dossier.coverage_warning == ""
    assert provider.assign_calls == 1


def test_the_warning_survives_a_serialization_round_trip(monkeypatch):
    # It has to reach the overlay and the queue job file, not just live in
    # memory on the engine side.
    from detective.models import Dossier

    dossier, _ = _run([_never_looked("a"), _never_looked("b")], monkeypatch)
    restored = Dossier.from_dict(dossier.to_dict())
    assert restored.coverage_warning == dossier.coverage_warning
