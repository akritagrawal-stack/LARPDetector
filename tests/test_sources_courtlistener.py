"""Offline tests for detective.sources.courtlistener. No network: the
internal _search function is monkeypatched with realistic sample CourtListener
v4 search-result shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.courtlistener as courtlistener


def _docket_hit(
    case_name: str = "Jane Doe v. Acme Corp",
    court: str = "cand",
    date_filed: str = "2022-03-14",
    docket_number: str = "3:22-cv-01234",
    absolute_url: str = "/docket/12345/jane-doe-v-acme-corp/",
) -> dict:
    return {
        "caseName": case_name,
        "court": court,
        "dateFiled": date_filed,
        "docketNumber": docket_number,
        "absolute_url": absolute_url,
    }


def _opinion_hit(
    case_name: str = "United States v. Smith",
    court: str = "ca9",
    date_filed: str = "2019-06-01",
    absolute_url: str = "/opinion/98765/united-states-v-smith/",
) -> dict:
    return {
        "caseName": case_name,
        "court": court,
        "dateFiled": date_filed,
        "absolute_url": absolute_url,
    }


# ---------------------------------------------------------------------------
# verify_courtlistener: basic gating
# ---------------------------------------------------------------------------


def test_blank_name_returns_empty():
    assert courtlistener.verify_courtlistener("") == []
    assert courtlistener.verify_courtlistener(None) == []


def test_both_searches_failing_returns_empty(monkeypatch):
    def boom(name, result_type):
        raise RuntimeError("network down")

    monkeypatch.setattr(courtlistener, "_search", boom)
    assert courtlistener.verify_courtlistener("Jane Doe") == []


def test_no_hits_in_either_search_returns_empty(monkeypatch):
    monkeypatch.setattr(courtlistener, "_search", lambda name, result_type: [])
    assert courtlistener.verify_courtlistener("Totally Unlitigated Person Zzz") == []


def test_docket_search_failure_does_not_block_opinion_search(monkeypatch):
    def fake_search(name, result_type):
        if result_type == "r":
            raise RuntimeError("docket search down")
        return [_opinion_hit()]

    monkeypatch.setattr(courtlistener, "_search", fake_search)
    evidence = courtlistener.verify_courtlistener("Smith")
    assert len(evidence) == 1
    assert "opinion" in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# Case-parse: docket + opinion hits both produce a well-formed record.
# ---------------------------------------------------------------------------


def test_docket_hit_parsed_into_evidence_record(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit()] if result_type == "r" else [],
    )
    evidence = courtlistener.verify_courtlistener("Jane Doe")
    assert len(evidence) == 1
    record = evidence[0]
    assert "Jane Doe v. Acme Corp" in record["snippet"]
    assert "cand" in record["snippet"]
    assert "2022-03-14" in record["snippet"]
    assert "3:22-cv-01234" in record["snippet"]
    assert record["source_url"] == "https://www.courtlistener.com/docket/12345/jane-doe-v-acme-corp/"


def test_docket_hit_uses_docket_absolute_url_when_absolute_url_missing(monkeypatch):
    # Confirmed live: a real v4 docket (type=r) hit's own "absolute_url" key
    # is either absent or not the docket page; the docket page lives under
    # "docket_absolute_url" instead.
    hit = _docket_hit()
    del hit["absolute_url"]
    hit["docket_absolute_url"] = "/docket/12986436/theranos-inc-v-lee/"
    hit["docket_id"] = 12986436
    monkeypatch.setattr(
        courtlistener, "_search", lambda name, result_type: [hit] if result_type == "r" else []
    )
    evidence = courtlistener.verify_courtlistener("Theranos", is_company=True)
    assert evidence[0]["source_url"] == "https://www.courtlistener.com/docket/12986436/theranos-inc-v-lee/"


def test_opinion_hit_parsed_into_evidence_record(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_opinion_hit()] if result_type == "o" else [],
    )
    evidence = courtlistener.verify_courtlistener("Smith")
    assert len(evidence) == 1
    record = evidence[0]
    assert "United States v. Smith" in record["snippet"]
    assert record["source_url"] == "https://www.courtlistener.com/opinion/98765/united-states-v-smith/"


def test_both_docket_and_opinion_hits_are_combined(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit()] if result_type == "r" else [_opinion_hit()],
    )
    evidence = courtlistener.verify_courtlistener("Acme Corp", is_company=True)
    assert len(evidence) == 2


def test_results_capped_at_max_total(monkeypatch):
    many_dockets = [_docket_hit(case_name=f"Case {i} v. Acme Corp") for i in range(5)]
    many_opinions = [_opinion_hit(case_name=f"Opinion {i}") for i in range(5)]
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: many_dockets if result_type == "r" else many_opinions,
    )
    evidence = courtlistener.verify_courtlistener("Acme Corp", is_company=True)
    assert len(evidence) == courtlistener._MAX_TOTAL_RESULTS


# ---------------------------------------------------------------------------
# match_confidence policy: LOW by default, MEDIUM only for a company query
# whose matched caption carries a legal-entity suffix, NEVER high.
# ---------------------------------------------------------------------------


def test_default_match_confidence_is_low_for_person_query(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit(case_name="Jane Doe v. John Roe")] if result_type == "r" else [],
    )
    evidence = courtlistener.verify_courtlistener("Jane Doe", is_company=False)
    assert evidence[0]["match_confidence"] == "low"


def test_company_query_with_entity_suffix_raises_to_medium(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit(case_name="SEC v. Acme Corp")] if result_type == "r" else [],
    )
    evidence = courtlistener.verify_courtlistener("Acme Corp", is_company=True)
    assert evidence[0]["match_confidence"] == "medium"


def test_company_query_without_entity_suffix_in_caption_stays_low(monkeypatch):
    # The matched caption has no legal-entity suffix token at all (e.g. it
    # matched on a bare surname), so even a company query cannot corroborate.
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit(case_name="Smith v. Jones")] if result_type == "r" else [],
    )
    evidence = courtlistener.verify_courtlistener("Acme", is_company=True)
    assert evidence[0]["match_confidence"] == "low"


def test_person_query_never_raised_to_medium_even_with_entity_suffix_caption(monkeypatch):
    # is_company=False: the corroborator this connector checks is company-
    # entity-type consistency, which only applies to a company query. A
    # person query stays low even if the matched caption happens to name a
    # company too.
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit(case_name="Jane Doe v. Acme Corp")] if result_type == "r" else [],
    )
    evidence = courtlistener.verify_courtlistener("Jane Doe", is_company=False)
    assert evidence[0]["match_confidence"] == "low"


def test_match_confidence_never_reaches_high(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit(case_name="SEC v. Acme Corporation")] if result_type == "r" else [_opinion_hit()],
    )
    for is_company in (True, False):
        evidence = courtlistener.verify_courtlistener("Acme Corporation", is_company=is_company)
        for record in evidence:
            assert record["match_confidence"] in ("low", "medium")


def test_same_name_false_positive_caveat_in_every_snippet(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit()] if result_type == "r" else [],
    )
    evidence = courtlistener.verify_courtlistener("Jane Doe")
    assert "not confirmation" in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# _looks_like_company_entity: the one corroborator this connector can check.
# ---------------------------------------------------------------------------


def test_looks_like_company_entity_true_for_known_suffixes():
    assert courtlistener._looks_like_company_entity("Acme Corp")
    assert courtlistener._looks_like_company_entity("Widget Industries LLC")
    assert courtlistener._looks_like_company_entity("Foo Bar Inc.")


def test_looks_like_company_entity_false_for_bare_personal_name():
    assert not courtlistener._looks_like_company_entity("Jane Doe v. John Roe")


# ---------------------------------------------------------------------------
# Evidence record shape + registry weight
# ---------------------------------------------------------------------------


def test_evidence_record_shape_and_weight(monkeypatch):
    monkeypatch.setattr(
        courtlistener,
        "_search",
        lambda name, result_type: [_docket_hit()] if result_type == "r" else [],
    )
    evidence = courtlistener.verify_courtlistener("Jane Doe")
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "courtlistener"
    assert record["weight"] == 0.6


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_courtlistener_theranos():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real CourtListener API call")

    evidence = courtlistener.verify_courtlistener("Theranos", is_company=True)
    assert evidence, "expected CourtListener to find real litigation involving Theranos"
