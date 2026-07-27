"""Offline tests for detective.sources.domain_age. No network: the internal
_rdap_lookup / _whois_query functions are monkeypatched with realistic
sample RDAP JSON and raw WHOIS text.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.domain_age as domain_age


# A realistic RDAP response (trimmed to the fields this module reads).
_SAMPLE_RDAP_RESPONSE = {
    "objectClassName": "domain",
    "ldhName": "EXAMPLE.COM",
    "events": [
        {"eventAction": "registration", "eventDate": "2015-06-01T04:00:00Z"},
        {"eventAction": "last changed", "eventDate": "2024-05-01T04:00:00Z"},
        {"eventAction": "expiration", "eventDate": "2026-06-01T04:00:00Z"},
    ],
}

_SAMPLE_WHOIS_TEXT = """Domain Name: EXAMPLE.COM
Registry Domain ID: 123456_DOMAIN_COM-VRSN
Registrar WHOIS Server: whois.example-registrar.com
Creation Date: 2015-06-01T04:00:00Z
Registry Expiry Date: 2026-06-01T04:00:00Z
Registrant Organization: REDACTED FOR PRIVACY
"""

_SAMPLE_IANA_REFERRAL = "refer:        whois.example-registrar.com\n"


# ---------------------------------------------------------------------------
# _normalize_domain
# ---------------------------------------------------------------------------


def test_normalize_domain_strips_scheme_and_path():
    assert domain_age._normalize_domain("https://example.com/about") == "example.com"


def test_normalize_domain_strips_www():
    assert domain_age._normalize_domain("http://www.example.com") == "example.com"


def test_normalize_domain_strips_port():
    assert domain_age._normalize_domain("example.com:8080") == "example.com"


def test_normalize_domain_lowercases():
    assert domain_age._normalize_domain("EXAMPLE.COM") == "example.com"


# ---------------------------------------------------------------------------
# _extract_registration_event: pure parse of the RDAP events[] shape
# ---------------------------------------------------------------------------


def test_extract_registration_event_finds_registration_action():
    assert (
        domain_age._extract_registration_event(_SAMPLE_RDAP_RESPONSE)
        == "2015-06-01T04:00:00Z"
    )


def test_extract_registration_event_empty_when_no_registration_action():
    data = {"events": [{"eventAction": "last changed", "eventDate": "2024-01-01T00:00:00Z"}]}
    assert domain_age._extract_registration_event(data) == ""


def test_extract_registration_event_empty_for_empty_dict():
    assert domain_age._extract_registration_event({}) == ""


# ---------------------------------------------------------------------------
# _extract_whois_creation: pure parse of raw WHOIS text
# ---------------------------------------------------------------------------


def test_extract_whois_creation_finds_creation_date_label():
    assert domain_age._extract_whois_creation(_SAMPLE_WHOIS_TEXT) == "2015-06-01T04:00:00Z"


def test_extract_whois_creation_empty_when_label_absent():
    assert domain_age._extract_whois_creation("Domain Name: EXAMPLE.COM\n") == ""


def test_extract_whois_creation_empty_for_empty_text():
    assert domain_age._extract_whois_creation("") == ""


# ---------------------------------------------------------------------------
# verify_domain_age: end-to-end with the network internals monkeypatched
# ---------------------------------------------------------------------------


def test_verify_domain_age_empty_domain_returns_empty():
    assert domain_age.verify_domain_age("") == []


def test_verify_domain_age_rdap_failure_falls_back_to_whois(monkeypatch):
    monkeypatch.setattr(domain_age, "_rdap_lookup", lambda domain: None)
    monkeypatch.setattr(domain_age, "_whois_query", lambda domain: _SAMPLE_WHOIS_TEXT)

    evidence = domain_age.verify_domain_age("example.com")
    assert len(evidence) == 1
    assert "via WHOIS" in evidence[0]["snippet"]


def test_verify_domain_age_no_data_from_either_source_returns_empty(monkeypatch):
    monkeypatch.setattr(domain_age, "_rdap_lookup", lambda domain: None)
    monkeypatch.setattr(domain_age, "_whois_query", lambda domain: "")

    assert domain_age.verify_domain_age("example.com") == []


def test_verify_domain_age_rdap_lookup_raises_falls_back_to_whois(monkeypatch):
    def boom(domain):
        raise RuntimeError("network down")

    monkeypatch.setattr(domain_age, "_rdap_lookup", boom)
    monkeypatch.setattr(domain_age, "_whois_query", lambda domain: _SAMPLE_WHOIS_TEXT)

    evidence = domain_age.verify_domain_age("example.com")
    assert len(evidence) == 1
    assert "via WHOIS" in evidence[0]["snippet"]


def test_verify_domain_age_happy_path_evidence_shape(monkeypatch):
    monkeypatch.setattr(domain_age, "_rdap_lookup", lambda domain: _SAMPLE_RDAP_RESPONSE)

    evidence = domain_age.verify_domain_age("example.com")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "domain_rdap_whois"
    assert record["weight"] == 0.64
    assert record["match_confidence"] == "high"
    assert "2015-06-01T04:00:00Z" in record["snippet"]
    assert "via RDAP" in record["snippet"]
    assert "privacy-masked" in record["snippet"]


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_domain_age_openai_com():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real RDAP/WHOIS lookup")

    evidence = domain_age.verify_domain_age("openai.com")
    assert evidence, "expected a creation date for a well-known domain"
