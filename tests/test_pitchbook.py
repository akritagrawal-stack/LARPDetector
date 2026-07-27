"""Offline tests for detective.pitchbook. No network, no real cookies.

Bug 2 (from the live stress test): verify_person_role(person, company)
searched PitchBook by person name only, and attached whatever the single
top-ranked hit was as evidence for EVERY employment claim about that
person, regardless of which company the claim was actually about. Observed
damage: an NVIDIA employment claim ended up citing a Northwind Robotics /
Relativity Space bio snippet that never mentions NVIDIA at all.

These tests monkeypatch the network-touching internals (_load_session,
_search_mixed) so the fix (company folded into the query, plus a
mentions-company guard before attaching evidence) is exercised without any
real PitchBook auth or request. No em dashes in this file (house rule).
"""

from __future__ import annotations

import detective.pitchbook as pitchbook
from detective.pitchbook import PitchBookBudget, _mentions_company, verify_person_role


def _enable(monkeypatch):
    monkeypatch.setattr(pitchbook, "is_enabled", lambda: True)
    monkeypatch.setattr(pitchbook, "_load_session", lambda: object())
    monkeypatch.setattr(pitchbook, "_throttle", lambda: None)


def _investor_item(name: str, related_company: str, description: str) -> dict:
    return {
        "type": "INVESTOR",
        "value": {
            "profileResult": {"id": "999-AB", "name": name, "description": description},
            "relatedPerson": {"companyName": related_company},
        },
    }


# ---------------------------------------------------------------------------
# _mentions_company: the guard itself
# ---------------------------------------------------------------------------


def test_mentions_company_true_via_related_company():
    assert _mentions_company("Northwind Robotics", "Northwind Robotics", "") is True


def test_mentions_company_true_via_description():
    assert _mentions_company(
        "Relativity Space", "", "Previously worked on Relativity Space flight software."
    ) is True


def test_mentions_company_false_when_neither_mentions_it():
    assert _mentions_company("NVIDIA", "Northwind Robotics", "Co-Founder & CTO bio.") is False


def test_mentions_company_true_when_company_blank():
    # No company to check against: never block (identity-style callers).
    assert _mentions_company("", "Anything", "Anything") is True


# ---------------------------------------------------------------------------
# verify_person_role: disabled / no-op paths untouched
# ---------------------------------------------------------------------------


def test_verify_person_role_disabled_by_default(monkeypatch):
    monkeypatch.setattr(pitchbook, "is_enabled", lambda: False)
    assert verify_person_role("Jordan Rivera", "NVIDIA") == []


def test_verify_person_role_empty_person_returns_empty(monkeypatch):
    _enable(monkeypatch)
    assert verify_person_role("", "NVIDIA") == []


# ---------------------------------------------------------------------------
# The actual bug: company must be in the query, and a mismatched result must
# not be attached as evidence.
# ---------------------------------------------------------------------------


def test_verify_person_role_query_includes_company(monkeypatch):
    _enable(monkeypatch)
    seen_queries = []

    def fake_search_mixed(session, query, limit=8):
        seen_queries.append(query)
        return {"items": [_investor_item("Jordan Rivera", "Northwind Robotics", "CTO bio.")]}

    monkeypatch.setattr(pitchbook, "_search_mixed", fake_search_mixed)
    verify_person_role("Jordan Rivera", "Northwind Robotics", budget=PitchBookBudget())
    assert seen_queries, "verify_person_role never called _search_mixed"
    assert "Jordan Rivera" in seen_queries[0]
    assert "Northwind Robotics" in seen_queries[0]


def test_verify_person_role_does_not_attach_mismatched_company(monkeypatch):
    # The single top-ranked PitchBook hit for this person is their CURRENT
    # role (Northwind Robotics / Relativity Space bio); it says nothing
    # about NVIDIA. The NVIDIA employment claim must get NO evidence rather
    # than this unrelated snippet.
    _enable(monkeypatch)

    def fake_search_mixed(session, query, limit=8):
        return {
            "items": [
                _investor_item(
                    "Jordan Rivera",
                    "Northwind Robotics",
                    "Previously worked on Flight software at Relativity Space.",
                )
            ]
        }

    monkeypatch.setattr(pitchbook, "_search_mixed", fake_search_mixed)
    evidence = verify_person_role("Jordan Rivera", "NVIDIA", budget=PitchBookBudget())
    assert evidence == []


def test_verify_person_role_attaches_when_company_matches(monkeypatch):
    _enable(monkeypatch)

    def fake_search_mixed(session, query, limit=8):
        return {
            "items": [
                _investor_item(
                    "Jordan Rivera",
                    "Northwind Robotics",
                    "Mr. Jordan Rivera is a Co-Founder & serves as Chief Technology Officer "
                    "at Northwind Robotics.",
                )
            ]
        }

    monkeypatch.setattr(pitchbook, "_search_mixed", fake_search_mixed)
    evidence = verify_person_role("Jordan Rivera", "Northwind Robotics", budget=PitchBookBudget())
    assert len(evidence) == 1
    assert "Northwind Robotics" in evidence[0]["snippet"]


def test_verify_person_role_distinct_claims_get_distinct_evidence(monkeypatch):
    # Two different employment claims about the same person (their real
    # current employer, and an unrelated past employer with no PitchBook
    # trace of it) must not collapse onto the same shared snippet.
    _enable(monkeypatch)

    def fake_search_mixed(session, query, limit=8):
        return {
            "items": [
                _investor_item(
                    "Casey Lin",
                    "Northwind Robotics",
                    "Mr. Casey Lin is a Co-Founder & serves as COO at Northwind Robotics.",
                )
            ]
        }

    monkeypatch.setattr(pitchbook, "_search_mixed", fake_search_mixed)
    budget = PitchBookBudget(max_lookups=5)
    northwind_evidence = verify_person_role("Casey Lin", "Northwind Robotics", budget=budget)
    amazon_evidence = verify_person_role("Casey Lin", "Amazon", budget=budget)

    assert northwind_evidence != amazon_evidence
    assert len(northwind_evidence) == 1
    assert amazon_evidence == []
