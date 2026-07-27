"""Offline tests for detective.sources.arxiv. No network: the internal
_query function is monkeypatched with realistic sample arXiv Atom XML
feeds.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.arxiv as arxiv


def _feed(total_results: int, entries: list[tuple[str, str]]) -> str:
    """Build a minimal but realistic arXiv Atom feed. entries is a list of
    (title, published_iso_datetime) tuples.
    """
    entry_xml = "".join(
        f"""
  <entry>
    <id>http://arxiv.org/abs/{idx}v1</id>
    <title>{title}</title>
    <published>{published}</published>
  </entry>"""
        for idx, (title, published) in enumerate(entries)
    )
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns="http://www.w3.org/2005/Atom">
  <opensearch:totalResults>{total_results}</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>{entry_xml}
</feed>"""


# ---------------------------------------------------------------------------
# verify_arxiv: basic gating
# ---------------------------------------------------------------------------


def test_verify_arxiv_empty_name_returns_empty():
    assert arxiv.verify_arxiv("") == []
    assert arxiv.verify_arxiv(None) == []


def test_verify_arxiv_request_failure_returns_empty(monkeypatch):
    def boom(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(arxiv, "_query", boom)
    assert arxiv.verify_arxiv("Someone") == []


def test_verify_arxiv_empty_response_returns_empty(monkeypatch):
    monkeypatch.setattr(arxiv, "_query", lambda name: "")
    assert arxiv.verify_arxiv("Someone") == []


def test_verify_arxiv_no_entries_returns_empty(monkeypatch):
    monkeypatch.setattr(arxiv, "_query", lambda name: _feed(0, []))
    assert arxiv.verify_arxiv("Totally Unpublished Person") == []


# ---------------------------------------------------------------------------
# Evidence record shape + weight
# ---------------------------------------------------------------------------


def test_evidence_record_shape_and_weight(monkeypatch):
    monkeypatch.setattr(
        arxiv,
        "_query",
        lambda name: _feed(1, [("A Novel Widget Approach", "2023-04-01T10:00:00Z")]),
    )
    evidence = arxiv.verify_arxiv("Jane Rare Doe")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "arxiv"
    assert record["weight"] == 0.8
    assert "A Novel Widget Approach" in record["snippet"]
    assert "2023-04-01" in record["snippet"]
    assert "NOT peer-reviewed" in record["snippet"]


# ---------------------------------------------------------------------------
# match_confidence: never "high", "medium" for small result counts, "low"
# for large (likely-namesake-collision) result counts.
# ---------------------------------------------------------------------------


def test_match_confidence_medium_for_small_result_count(monkeypatch):
    monkeypatch.setattr(
        arxiv,
        "_query",
        lambda name: _feed(3, [("Paper One", "2023-01-01T00:00:00Z")]),
    )
    evidence = arxiv.verify_arxiv("Distinctive Name")
    assert evidence[0]["match_confidence"] == "medium"


def test_match_confidence_low_for_large_result_count(monkeypatch):
    monkeypatch.setattr(
        arxiv,
        "_query",
        lambda name: _feed(500, [("Paper One", "2023-01-01T00:00:00Z")]),
    )
    evidence = arxiv.verify_arxiv("John Smith")
    assert evidence[0]["match_confidence"] == "low"
    assert "likely spans multiple different people" in evidence[0]["snippet"]


def test_match_confidence_never_high(monkeypatch):
    monkeypatch.setattr(
        arxiv,
        "_query",
        lambda name: _feed(1, [("Only Paper", "2023-01-01T00:00:00Z")]),
    )
    evidence = arxiv.verify_arxiv("Uniquely Named Person")
    assert evidence[0]["match_confidence"] in {"medium", "low"}


# ---------------------------------------------------------------------------
# _parse_feed: pure parsing
# ---------------------------------------------------------------------------


def test_parse_feed_malformed_xml_returns_zero_and_empty():
    total, papers = arxiv._parse_feed("not xml at all <<<")
    assert total == 0
    assert papers == []


def test_parse_feed_extracts_title_and_date():
    xml_text = _feed(2, [("First Paper", "2020-05-01T12:00:00Z"), ("Second Paper", "2021-06-01T00:00:00Z")])
    total, papers = arxiv._parse_feed(xml_text)
    assert total == 2
    assert papers[0]["title"] == "First Paper"
    assert papers[0]["submitted"] == "2020-05-01"
    assert papers[1]["title"] == "Second Paper"


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_arxiv_yann_lecun():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real arXiv API call")

    evidence = arxiv.verify_arxiv("Yann LeCun")
    assert evidence, "expected at least one paper for a well-known arXiv author"
