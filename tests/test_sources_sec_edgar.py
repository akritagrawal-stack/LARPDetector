"""Offline tests for detective.sources.sec_edgar. No network: the internal
_search_form_d / _fetch_xml functions are monkeypatched with a realistic
sample full-text-search response and a realistic sample Form D XML document.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.sec_edgar as sec_edgar


_SAMPLE_SEARCH_RESPONSE = {
    "hits": {
        "total": {"value": 1, "relation": "eq"},
        "hits": [
            {
                "_id": "0001193125-21-123456:primary_doc.xml",
                "_source": {
                    "root_forms": ["D"],
                    "file_date": "2021-05-03",
                    "display_names": ["EXAMPLE CORP (CIK 0001234567)"],
                    "ciks": ["0001234567"],
                },
            }
        ],
    }
}

# A realistic Form D primary_doc.xml (trimmed to the fields this module reads).
_SAMPLE_FORM_D_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission>
  <schemaVersion>X0708</schemaVersion>
  <primaryIssuer>
    <cik>0001234567</cik>
    <entityName>Example Corp</entityName>
  </primaryIssuer>
  <offeringData>
    <typeOfFiling>
      <dateOfFirstSale>
        <value>2021-05-01</value>
      </dateOfFirstSale>
    </typeOfFiling>
    <offeringSalesAmounts>
      <totalOfferingAmount>5000000</totalOfferingAmount>
      <totalAmountSold>5000000</totalAmountSold>
      <totalRemaining>0</totalRemaining>
    </offeringSalesAmounts>
  </offeringData>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>Jane</firstName>
        <lastName>Doe</lastName>
      </relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Executive Officer</relationship>
        <relationship>Director</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
</edgarSubmission>
"""


# ---------------------------------------------------------------------------
# parse_form_d_xml: pure parsing
# ---------------------------------------------------------------------------


def test_parse_form_d_xml_extracts_entity_name():
    parsed = sec_edgar.parse_form_d_xml(_SAMPLE_FORM_D_XML)
    assert parsed["entity_name"] == "Example Corp"


def test_parse_form_d_xml_extracts_offering_amounts():
    parsed = sec_edgar.parse_form_d_xml(_SAMPLE_FORM_D_XML)
    assert parsed["total_offering_amount"] == "5000000"
    assert parsed["total_amount_sold"] == "5000000"


def test_parse_form_d_xml_extracts_date_of_first_sale_nested_value():
    parsed = sec_edgar.parse_form_d_xml(_SAMPLE_FORM_D_XML)
    assert parsed["date_of_first_sale"] == "2021-05-01"


def test_parse_form_d_xml_extracts_related_persons():
    parsed = sec_edgar.parse_form_d_xml(_SAMPLE_FORM_D_XML)
    assert parsed["related_persons"] == ["Jane Doe"]


def test_parse_form_d_xml_malformed_returns_empty_shape():
    parsed = sec_edgar.parse_form_d_xml("not xml at all <<<")
    assert parsed["entity_name"] == ""
    assert parsed["related_persons"] == []


# ---------------------------------------------------------------------------
# _hit_doc_url: builds the Archives URL from a search hit
# ---------------------------------------------------------------------------


def test_hit_doc_url_builds_archives_path():
    hit = _SAMPLE_SEARCH_RESPONSE["hits"]["hits"][0]
    url = sec_edgar._hit_doc_url(hit)
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1234567/000119312521123456/primary_doc.xml"
    )


def test_hit_doc_url_returns_none_without_cik():
    assert sec_edgar._hit_doc_url({"_id": "abc:primary_doc.xml", "_source": {}}) is None


# ---------------------------------------------------------------------------
# verify_sec: end-to-end with the network internals monkeypatched
# ---------------------------------------------------------------------------


def test_verify_sec_empty_name_returns_empty():
    assert sec_edgar.verify_sec("") == []


def test_verify_sec_no_filing_found_returns_empty(monkeypatch):
    monkeypatch.setattr(sec_edgar, "_search_form_d", lambda name: {"hits": {"hits": []}})
    assert sec_edgar.verify_sec("Totally Unfiled Inc") == []


def test_verify_sec_search_failure_returns_empty(monkeypatch):
    def boom(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(sec_edgar, "_search_form_d", boom)
    assert sec_edgar.verify_sec("Example Corp") == []


def test_verify_sec_happy_path_evidence_shape(monkeypatch):
    monkeypatch.setattr(sec_edgar, "_search_form_d", lambda name: _SAMPLE_SEARCH_RESPONSE)
    monkeypatch.setattr(sec_edgar, "_fetch_xml", lambda url: _SAMPLE_FORM_D_XML)

    evidence = sec_edgar.verify_sec("Example Corp", claimed_amount="$50 million raised")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "sec_edgar_form_d"
    assert record["weight"] == 0.8
    assert record["match_confidence"] == "high"  # "Example Corp" matches the filing's entity name
    assert "5000000" in record["snippet"]
    assert "$50 million raised" in record["snippet"]


def test_verify_sec_medium_confidence_when_entity_name_does_not_match(monkeypatch):
    monkeypatch.setattr(sec_edgar, "_search_form_d", lambda name: _SAMPLE_SEARCH_RESPONSE)
    monkeypatch.setattr(sec_edgar, "_fetch_xml", lambda url: _SAMPLE_FORM_D_XML)

    # Query a name unrelated to "Example Corp" (the filing's own entity name).
    evidence = sec_edgar.verify_sec("Totally Different Name LLC")
    assert evidence[0]["match_confidence"] == "medium"


def test_verify_sec_doc_fetch_failure_still_returns_search_level_evidence(monkeypatch):
    monkeypatch.setattr(sec_edgar, "_search_form_d", lambda name: _SAMPLE_SEARCH_RESPONSE)
    monkeypatch.setattr(sec_edgar, "_fetch_xml", lambda url: None)

    evidence = sec_edgar.verify_sec("Example Corp")
    assert len(evidence) == 1
    assert "EXAMPLE CORP" in evidence[0]["snippet"] or "Example Corp" in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_sec_openai():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real SEC EDGAR API call")

    evidence = sec_edgar.verify_sec("OpenAI")
    assert isinstance(evidence, list)
