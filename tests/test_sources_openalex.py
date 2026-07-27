"""Offline tests for detective.sources.openalex. No network: the internal
_search_authors function is monkeypatched with realistic sample OpenAlex
/authors response shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.openalex as openalex


def _author(
    display_name: str = "Jane Doe",
    works_count: int = 42,
    cited_by_count: int = 1000,
    institutions: list[str] = None,
    orcid: str = None,
    first_year: int = 2010,
    last_year: int = 2024,
) -> dict:
    institutions = institutions or []
    return {
        "id": "https://openalex.org/A1234567890",
        "display_name": display_name,
        "works_count": works_count,
        "cited_by_count": cited_by_count,
        "ids": {"openalex": "https://openalex.org/A1234567890", "orcid": orcid},
        "affiliations": [
            {"institution": {"display_name": name}} for name in institutions
        ],
        "counts_by_year": [
            {"year": first_year, "works_count": 1},
            {"year": last_year, "works_count": 1},
        ],
    }


def _search_response(authors: list[dict], count: int = None) -> dict:
    return {"meta": {"count": count if count is not None else len(authors)}, "results": authors}


# ---------------------------------------------------------------------------
# verify_openalex: basic gating
# ---------------------------------------------------------------------------


def test_verify_openalex_empty_name_returns_empty():
    assert openalex.verify_openalex("") == []
    assert openalex.verify_openalex(None) == []


def test_verify_openalex_search_failure_returns_empty(monkeypatch):
    def boom(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(openalex, "_search_authors", boom)
    assert openalex.verify_openalex("Someone") == []


def test_verify_openalex_no_results_returns_empty(monkeypatch):
    monkeypatch.setattr(openalex, "_search_authors", lambda name: _search_response([]))
    assert openalex.verify_openalex("Totally Unpublished Person") == []


# ---------------------------------------------------------------------------
# Evidence record shape + weight
# ---------------------------------------------------------------------------


def test_evidence_record_shape_and_weight(monkeypatch):
    author = _author(institutions=["MIT"])
    monkeypatch.setattr(openalex, "_search_authors", lambda name: _search_response([author]))

    evidence = openalex.verify_openalex("Jane Doe")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "openalex"
    assert record["weight"] == 0.64
    assert "42 work(s)" in record["snippet"]
    assert "1000 citation(s)" in record["snippet"]
    assert "MIT" in record["snippet"]
    assert "2010 to 2024" in record["snippet"]


# ---------------------------------------------------------------------------
# match_confidence policy
# ---------------------------------------------------------------------------


def test_match_confidence_high_when_institution_matches(monkeypatch):
    author = _author(institutions=["Massachusetts Institute of Technology"])
    monkeypatch.setattr(openalex, "_search_authors", lambda name: _search_response([author]))

    evidence = openalex.verify_openalex("Jane Doe", institution="Massachusetts Institute of Technology")
    assert evidence[0]["match_confidence"] == "high"


def test_match_confidence_low_when_institution_does_not_match(monkeypatch):
    author = _author(institutions=["Some Other University"])
    monkeypatch.setattr(openalex, "_search_authors", lambda name: _search_response([author]))

    evidence = openalex.verify_openalex("Jane Doe", institution="Stanford University")
    assert evidence[0]["match_confidence"] == "low"
    assert "not found among this author's recorded affiliations" in evidence[0]["snippet"]


def test_match_confidence_medium_when_orcid_present_and_result_set_small(monkeypatch):
    author = _author(orcid="https://orcid.org/0000-0001-2345-6789")
    monkeypatch.setattr(
        openalex, "_search_authors", lambda name: _search_response([author], count=1)
    )

    evidence = openalex.verify_openalex("Jane Doe")  # no institution supplied
    assert evidence[0]["match_confidence"] == "medium"


def test_match_confidence_low_bare_name_no_institution_no_orcid(monkeypatch):
    author = _author(orcid=None)
    monkeypatch.setattr(
        openalex, "_search_authors", lambda name: _search_response([author], count=16)
    )

    evidence = openalex.verify_openalex("Common Name")
    assert evidence[0]["match_confidence"] == "low"
    assert "Common-name collision risk" in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_openalex_geoffrey_hinton():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real OpenAlex API call")

    evidence = openalex.verify_openalex("Geoffrey Hinton")
    assert evidence, "expected at least one candidate for a well-known researcher"
