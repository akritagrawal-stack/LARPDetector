"""Offline tests for detective.sources.news. No network: the internal
_search_news (web-search layer) function is monkeypatched with realistic
sample result shapes.

The discipline this connector must hold:
  - genuine third-party editorial coverage (a recognized outlet) is
    corroborating footprint (match_confidence "medium", the honest ceiling,
    since a same-named subject cannot be verified from a snippet alone).
  - a source merely REPRINTING the subject's own announcement (a PR wire /
    self-published press release, even syndicated onto an allowlisted
    domain) is NOT corroboration: reporting a claim is not confirming it.
    Such a hit is surfaced but marked "low" and flagged as a reprint.
  - any network failure degrades to [] and never raises.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.news as news


def _result(url: str, title: str = "", snippet: str = "") -> dict:
    return {"url": url, "title": title, "snippet": snippet}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def test_blank_subject_returns_empty():
    assert news.verify_news("") == []
    assert news.verify_news(None) == []


def test_no_results_returns_empty(monkeypatch):
    monkeypatch.setattr(news, "_search_news", lambda subject: [])
    assert news.verify_news("Acme Corp", is_company=True) == []


# ---------------------------------------------------------------------------
# Genuine third-party coverage: corroboration
# ---------------------------------------------------------------------------


def test_third_party_coverage_yields_corroborating_record(monkeypatch):
    coverage_text = (
        "Reuters reviewed internal documents and interviewed five former "
        "employees who described Acme Corp's rapid expansion into Europe."
    )
    monkeypatch.setattr(
        news,
        "_search_news",
        lambda subject: [
            _result(
                "https://www.reuters.com/technology/acme-corp-expands-2026",
                "Acme Corp expands into Europe",
                coverage_text,
            )
        ],
    )

    evidence = news.verify_news("Acme Corp", is_company=True)
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "news_coverage"
    assert record["weight"] == 0.48
    # Genuine coverage is corroborating: medium is the honest ceiling here.
    assert record["match_confidence"] == "medium"
    # The snippet must be the ACTUAL coverage text, not a synthesized summary.
    assert "interviewed five former" in record["snippet"]


def test_coverage_confidence_never_reaches_high(monkeypatch):
    monkeypatch.setattr(
        news,
        "_search_news",
        lambda subject: [
            _result("https://www.nytimes.com/2026/acme", "Acme profile", "A long profile of Acme Corp.")
        ],
    )
    evidence = news.verify_news("Acme Corp", is_company=True)
    assert evidence[0]["match_confidence"] != "high"


# ---------------------------------------------------------------------------
# Reprint of the subject's own claim: NOT confirmation
# ---------------------------------------------------------------------------


def test_press_release_reprint_is_not_treated_as_confirmation(monkeypatch):
    monkeypatch.setattr(
        news,
        "_search_news",
        lambda subject: [
            _result(
                "https://www.prnewswire.com/news-releases/acme-corp-announces-10m-arr",
                "Acme Corp Announces $10M ARR",
                "SAN FRANCISCO, Acme Corp today announced it has surpassed $10M in ARR.",
            )
        ],
    )

    evidence = news.verify_news("Acme Corp", is_company=True)
    assert len(evidence) == 1
    record = evidence[0]
    # A reprint of the subject's own announcement is never corroboration.
    assert record["match_confidence"] == "low"
    lowered = record["snippet"].lower()
    assert "press release" in lowered or "reprint" in lowered or "not independent" in lowered


def test_syndicated_press_release_on_allowlisted_domain_is_downgraded(monkeypatch):
    # A PR-wire piece republished onto an otherwise-allowlisted outlet domain
    # must still be caught as a reprint via URL/snippet markers, not counted
    # as independent editorial coverage.
    monkeypatch.setattr(
        news,
        "_search_news",
        lambda subject: [
            # techcrunch.com IS an allowlisted outlet, yet this URL carries a
            # press-release marker: the marker check must PRECEDE the allowlist
            # check so a syndicated release is still caught as a reprint.
            _result(
                "https://techcrunch.com/press-release/acme-corp-announces-10m-arr-prnewswire",
                "Acme Corp Announces $10M ARR (PRNewswire)",
                "PRNewswire -- Acme Corp today announced it has surpassed $10M in ARR.",
            )
        ],
    )

    evidence = news.verify_news("Acme Corp", is_company=True)
    assert len(evidence) == 1
    assert evidence[0]["match_confidence"] == "low"


def test_mixed_results_only_real_coverage_is_corroborating(monkeypatch):
    monkeypatch.setattr(
        news,
        "_search_news",
        lambda subject: [
            _result(
                "https://www.prnewswire.com/news-releases/acme-corp-10m",
                "Acme Corp Announces $10M ARR",
                "Acme Corp today announced $10M ARR.",
            ),
            _result(
                "https://techcrunch.com/2026/acme-corp-raises",
                "Acme Corp raises Series B",
                "TechCrunch has learned Acme Corp closed a Series B led by an outside firm.",
            ),
        ],
    )

    evidence = news.verify_news("Acme Corp", is_company=True)
    confidences = {e["source_url"]: e["match_confidence"] for e in evidence}
    assert confidences["https://www.prnewswire.com/news-releases/acme-corp-10m"] == "low"
    assert confidences["https://techcrunch.com/2026/acme-corp-raises"] == "medium"


def test_unrecognized_domain_is_skipped(monkeypatch):
    # An unknown blog is neither a recognized outlet nor a known PR wire; the
    # connector does not manufacture a corroboration signal from it.
    monkeypatch.setattr(
        news,
        "_search_news",
        lambda subject: [_result("https://random-blog.example/post", "Acme thoughts", "some musings")],
    )
    assert news.verify_news("Acme Corp", is_company=True) == []


# ---------------------------------------------------------------------------
# Never raises on a network error
# ---------------------------------------------------------------------------


def test_search_layer_error_returns_empty_and_never_raises(monkeypatch):
    def boom(subject):
        raise RuntimeError("network down")

    monkeypatch.setattr(news, "_search_news", boom)
    assert news.verify_news("Acme Corp", is_company=True) == []


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_news_smoke():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real news web search")

    news.verify_news("OpenAI", is_company=True)
