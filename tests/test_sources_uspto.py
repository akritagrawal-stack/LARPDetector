"""Offline tests for detective.sources.uspto. No network: the internal
_odp_search / _google_patents_search functions are monkeypatched with
realistic sample USPTO ODP and Google Patents response shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.uspto as uspto


# ---------------------------------------------------------------------------
# verify_uspto: basic gating
# ---------------------------------------------------------------------------


def test_verify_uspto_empty_name_returns_empty():
    assert uspto.verify_uspto("") == []
    assert uspto.verify_uspto(None) == []


def test_verify_uspto_search_failure_returns_empty(monkeypatch):
    def boom(name, is_company):
        raise RuntimeError("network down")

    monkeypatch.setattr(uspto, "_google_patents_search", boom)
    assert uspto.verify_uspto("Nobody Findable Inc", is_company=True) == []


def test_verify_uspto_no_hits_returns_empty(monkeypatch):
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: [])
    assert uspto.verify_uspto("Totally Unpatented LLC", is_company=True) == []


# ---------------------------------------------------------------------------
# Grant-vs-pending bucketing (the key LARP tell this connector exists for)
# ---------------------------------------------------------------------------


def _google_patent(
    publication_number: str,
    assignee: str = "",
    inventor: str = "",
    grant_date: str = "",
    filing_date: str = "2022-01-01",
    title: str = "Some invention",
):
    return {
        "publication_number": publication_number,
        "title": title,
        "assignee": assignee,
        "inventor": inventor,
        "filing_date": filing_date,
        "grant_date": grant_date,
    }


def test_company_query_buckets_granted_and_pending(monkeypatch):
    hits = [
        _google_patent("US11111111B2", assignee="Acme Corp", grant_date="2023-05-01"),
        _google_patent("US20240012345A1", assignee="Acme Corp", grant_date=""),
        _google_patent("US99999999B1", assignee="Unrelated Inc", grant_date="2021-01-01"),
    ]
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: hits)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    assert len(evidence) == 2  # one granted-summary record, one pending-summary record

    snippets = " ".join(e["snippet"] for e in evidence)
    assert "US11111111B2" in snippets
    assert "US20240012345A1" in snippets
    # The unrelated assignee's patent must not be counted (name-match filter).
    assert "US99999999B1" not in snippets


def test_pending_only_snippet_flags_the_larp_tell(monkeypatch):
    hits = [_google_patent("US20240099999A1", assignee="Acme Corp", grant_date="")]
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: hits)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    assert len(evidence) == 1
    assert "pending application" in evidence[0]["snippet"]
    assert "LARP tell" in evidence[0]["snippet"]


def test_granted_only_no_pending_record(monkeypatch):
    hits = [_google_patent("US11111111B2", assignee="Acme Corp", grant_date="2023-05-01")]
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: hits)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    assert len(evidence) == 1
    assert "pending" not in evidence[0]["snippet"].lower()


# ---------------------------------------------------------------------------
# match_confidence: only a field-level assignee/inventor match earns "high"
# ---------------------------------------------------------------------------


def test_match_confidence_high_when_assignee_field_matches(monkeypatch):
    hits = [_google_patent("US11111111B2", assignee="Acme Corp", grant_date="2023-05-01")]
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: hits)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    assert evidence[0]["match_confidence"] == "high"


def test_match_confidence_low_when_no_assignee_field_matches(monkeypatch):
    # The search returned hits (Google Patents' fuzzy ranking), but none of
    # their own assignee fields actually match the queried company name.
    hits = [_google_patent("US99999999B1", assignee="Completely Different Co", grant_date="2021-01-01")]
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: hits)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    assert len(evidence) == 1
    assert evidence[0]["match_confidence"] == "low"


def test_person_query_uses_inventor_field(monkeypatch):
    captured = {}

    def fake_search(name, is_company):
        captured["is_company"] = is_company
        return [_google_patent("US11111111B2", inventor="Jane Doe", grant_date="2023-05-01")]

    monkeypatch.setattr(uspto, "_google_patents_search", fake_search)
    evidence = uspto.verify_uspto("Jane Doe", is_company=False)
    assert captured["is_company"] is False
    assert evidence[0]["match_confidence"] == "high"


# ---------------------------------------------------------------------------
# Evidence record shape + registry weight
# ---------------------------------------------------------------------------


def test_evidence_record_shape_and_weight(monkeypatch):
    hits = [_google_patent("US11111111B2", assignee="Acme Corp", grant_date="2023-05-01")]
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: hits)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "uspto_patents_trademarks"
    assert record["weight"] == 0.8


# ---------------------------------------------------------------------------
# ODP-key-present-but-empty falls through to the Google Patents fallback
# ---------------------------------------------------------------------------


def test_odp_key_present_but_empty_falls_through_to_google_patents(monkeypatch):
    monkeypatch.setenv("USPTO_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(uspto, "_odp_search", lambda name, is_company: [])

    fallback_hits = [_google_patent("US11111111B2", assignee="Acme Corp", grant_date="2023-05-01")]
    monkeypatch.setattr(uspto, "_google_patents_search", lambda name, is_company: fallback_hits)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    assert len(evidence) == 1
    assert evidence[0]["match_confidence"] == "high"


def test_odp_key_present_and_returns_data_does_not_call_fallback(monkeypatch):
    monkeypatch.setenv("USPTO_API_KEY", "fake-key-for-test")
    odp_item = {
        "applicationNumberText": "17123456",
        "applicationMetaData": {
            "applicationStatusDescriptionText": "Patented Case",
            "inventionTitle": "Widget improvement",
            "filingDate": "2021-01-01",
            "grantDate": "2023-06-01",
            "inventorBag": [],
            "applicantBag": [{"applicantNameText": "Acme Corp"}],
        },
    }
    monkeypatch.setattr(uspto, "_odp_search", lambda name, is_company: [odp_item])

    def fallback_should_not_be_called(name, is_company):
        raise AssertionError("Google Patents fallback must not fire when ODP returned data")

    monkeypatch.setattr(uspto, "_google_patents_search", fallback_should_not_be_called)

    evidence = uspto.verify_uspto("Acme Corp", is_company=True)
    assert len(evidence) == 1
    assert evidence[0]["match_confidence"] == "high"


# ---------------------------------------------------------------------------
# ODP (keyed) parse path: pure parse function
# ---------------------------------------------------------------------------


def test_odp_item_to_record_granted():
    item = {
        "applicationNumberText": "17123456",
        "applicationMetaData": {
            "applicationStatusDescriptionText": "Patented Case",
            "inventionTitle": "Widget improvement",
            "filingDate": "2021-01-01",
            "grantDate": "2023-06-01",
            "patentNumber": "11234567",
            "inventorBag": [{"inventorNameText": "Jane Doe"}],
            "applicantBag": [{"applicantNameText": "Acme Corp"}],
        },
    }
    record = uspto._odp_item_to_record(item)
    assert record["granted"] is True
    assert record["grant_date"] == "2023-06-01"
    assert record["inventors"] == ["Jane Doe"]
    assert record["applicants"] == ["Acme Corp"]


def test_odp_item_to_record_pending():
    item = {
        "applicationNumberText": "17999999",
        "applicationMetaData": {
            "applicationStatusDescriptionText": "Non Final Action Mailed",
            "inventionTitle": "Widget improvement v2",
            "filingDate": "2024-01-01",
            "inventorBag": [],
            "applicantBag": [{"applicantNameText": "Acme Corp"}],
        },
    }
    record = uspto._odp_item_to_record(item)
    assert record["granted"] is False
    assert record["grant_date"] == ""


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_uspto_nvidia():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real USPTO/Google Patents call")

    evidence = uspto.verify_uspto("Nvidia Corporation", is_company=True)
    assert isinstance(evidence, list)
