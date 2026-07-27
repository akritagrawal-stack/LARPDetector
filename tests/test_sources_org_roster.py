"""Offline tests for detective.sources.org_roster. No network: the internal
_search_roster_pages (web-search layer) and _fetch_page (page-fetch layer)
functions are monkeypatched with realistic sample shapes.

The discipline this connector must hold:
  - a name found on the org's own public roster is real corroboration
    (match_confidence medium/high, never "low").
  - the name NOT found on a fetched roster is a documented ABSENCE, never
    disproof (match_confidence "low", snippet must say so in words), because
    rosters are incomplete and routinely omit past/junior members.
  - any network failure degrades to [] and never raises.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.org_roster as org_roster


def _result(url: str, title: str = "", snippet: str = "") -> dict:
    return {"url": url, "title": title, "snippet": snippet}


_ROSTER_HTML_WITH_NAME = """
<html><head><title>Acme Robotics - Our Team</title></head>
<body>
<h1>Acme Robotics Team</h1>
<ul>
  <li>Jane Doe, Co-founder and CTO</li>
  <li>John Smith, Head of Engineering</li>
</ul>
</body></html>
"""

_ROSTER_HTML_WITHOUT_NAME = """
<html><head><title>Acme Robotics - Our Team</title></head>
<body>
<h1>Acme Robotics Team</h1>
<ul>
  <li>John Smith, Head of Engineering</li>
  <li>Mary Major, Designer</li>
</ul>
</body></html>
"""


# ---------------------------------------------------------------------------
# Gating and inputs
# ---------------------------------------------------------------------------


def test_blank_person_or_org_returns_empty():
    assert org_roster.verify_org_roster("", "Acme Robotics") == []
    assert org_roster.verify_org_roster("Jane Doe", "") == []
    assert org_roster.verify_org_roster(None, None) == []


def test_no_candidate_roster_page_found_returns_empty(monkeypatch):
    monkeypatch.setattr(org_roster, "_search_roster_pages", lambda org: [])
    assert org_roster.verify_org_roster("Jane Doe", "Acme Robotics") == []


# ---------------------------------------------------------------------------
# Name match on the roster: corroboration
# ---------------------------------------------------------------------------


def test_name_match_on_roster_yields_corroborating_evidence(monkeypatch):
    monkeypatch.setattr(
        org_roster,
        "_search_roster_pages",
        lambda org: [_result("https://acmerobotics.com/team", "Acme Robotics - Our Team")],
    )
    monkeypatch.setattr(org_roster, "_fetch_page", lambda url: _ROSTER_HTML_WITH_NAME)

    evidence = org_roster.verify_org_roster("Jane Doe", "Acme Robotics")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "org_roster"
    assert record["weight"] == 0.288
    # A found name on the org's own roster is corroboration, never "low".
    assert record["match_confidence"] in ("medium", "high")
    assert "Jane Doe" in record["snippet"]
    assert record["source_url"] == "https://acmerobotics.com/team"


def test_org_confirmed_on_page_grades_high(monkeypatch):
    # The org name appears clearly on the fetched page AND the person name
    # matched cleanly, so the match is graded "high".
    monkeypatch.setattr(
        org_roster,
        "_search_roster_pages",
        lambda org: [_result("https://acmerobotics.com/team", "Acme Robotics - Our Team")],
    )
    monkeypatch.setattr(org_roster, "_fetch_page", lambda url: _ROSTER_HTML_WITH_NAME)

    evidence = org_roster.verify_org_roster("Jane Doe", "Acme Robotics")
    assert evidence[0]["match_confidence"] == "high"


def test_org_not_confirmed_on_page_grades_medium(monkeypatch):
    # Person name is present but nothing on the page/URL confirms this is the
    # claimed org, so the corroboration is only "medium".
    html = "<html><body><ul><li>Jane Doe, engineer</li></ul></body></html>"
    monkeypatch.setattr(
        org_roster,
        "_search_roster_pages",
        lambda org: [_result("https://some-aggregator.example/list", "People directory")],
    )
    monkeypatch.setattr(org_roster, "_fetch_page", lambda url: html)

    evidence = org_roster.verify_org_roster("Jane Doe", "Acme Robotics")
    assert len(evidence) == 1
    assert evidence[0]["match_confidence"] == "medium"


# ---------------------------------------------------------------------------
# Name NOT on the roster: documented ABSENCE, never disproof
# ---------------------------------------------------------------------------


def test_name_absent_from_roster_yields_absence_never_disproven(monkeypatch):
    monkeypatch.setattr(
        org_roster,
        "_search_roster_pages",
        lambda org: [_result("https://acmerobotics.com/team", "Acme Robotics - Our Team")],
    )
    monkeypatch.setattr(org_roster, "_fetch_page", lambda url: _ROSTER_HTML_WITHOUT_NAME)

    evidence = org_roster.verify_org_roster("Jane Doe", "Acme Robotics")
    assert len(evidence) == 1
    record = evidence[0]
    assert record["source_name"] == "org_roster"
    # An absence is the weakest possible signal and must NEVER read as disproof.
    assert record["match_confidence"] == "low"
    lowered = record["snippet"].lower()
    assert "does not disprove" in lowered
    # The connector must never itself pronounce a verdict word like DISPROVEN.
    assert "disproven" not in lowered


def test_page_fetch_returns_none_yields_empty_not_absence(monkeypatch):
    # If no roster page could actually be fetched, there is no roster to be
    # absent FROM, so this is [] (no evidence), not a manufactured absence.
    monkeypatch.setattr(
        org_roster,
        "_search_roster_pages",
        lambda org: [_result("https://acmerobotics.com/team", "Acme Robotics - Our Team")],
    )
    monkeypatch.setattr(org_roster, "_fetch_page", lambda url: None)

    assert org_roster.verify_org_roster("Jane Doe", "Acme Robotics") == []


# ---------------------------------------------------------------------------
# Never raises on a network error
# ---------------------------------------------------------------------------


def test_search_layer_error_returns_empty_and_never_raises(monkeypatch):
    def boom(org):
        raise RuntimeError("network down")

    monkeypatch.setattr(org_roster, "_search_roster_pages", boom)
    assert org_roster.verify_org_roster("Jane Doe", "Acme Robotics") == []


def test_fetch_layer_error_returns_empty_and_never_raises(monkeypatch):
    monkeypatch.setattr(
        org_roster,
        "_search_roster_pages",
        lambda org: [_result("https://acmerobotics.com/team", "Acme Robotics - Our Team")],
    )

    def boom(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(org_roster, "_fetch_page", boom)
    assert org_roster.verify_org_roster("Jane Doe", "Acme Robotics") == []


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_org_roster_smoke():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real roster discovery/fetch")

    # Best-effort: this only asserts it never raises; roster discovery is not
    # guaranteed to find a page for any given org.
    org_roster.verify_org_roster("Guido van Rossum", "Python Software Foundation")
