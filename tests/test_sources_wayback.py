"""Offline tests for detective.sources.wayback. No network: the internal
_cdx_query function is monkeypatched with a realistic sample CDX JSON
response shape.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.wayback as wayback


# A realistic CDX API response (list of lists, header row first).
_SAMPLE_CDX_ROWS = [
    ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    ["com,example)/", "20150601000000", "https://example.com/", "text/html", "200", "ABC123", "1000"],
    ["com,example)/", "20180101000000", "https://example.com/", "text/html", "200", "DEF456", "1100"],
    ["com,example)/", "20210101000000", "https://example.com/", "text/html", "200", "GHI789", "1200"],
    ["com,example)/", "20230601000000", "https://example.com/", "text/html", "200", "JKL012", "1300"],
]


# ---------------------------------------------------------------------------
# _rows_to_timestamps: pure parse of the CDX JSON shape
# ---------------------------------------------------------------------------


def test_rows_to_timestamps_extracts_and_sorts():
    timestamps = wayback._rows_to_timestamps(_SAMPLE_CDX_ROWS)
    assert timestamps == ["20150601000000", "20180101000000", "20210101000000", "20230601000000"]


def test_rows_to_timestamps_empty_for_header_only():
    assert wayback._rows_to_timestamps([_SAMPLE_CDX_ROWS[0]]) == []


def test_rows_to_timestamps_empty_for_empty_input():
    assert wayback._rows_to_timestamps([]) == []


def test_rows_to_timestamps_empty_when_no_timestamp_column():
    assert wayback._rows_to_timestamps([["urlkey", "original"], ["x", "y"]]) == []


# ---------------------------------------------------------------------------
# _sample_timestamps
# ---------------------------------------------------------------------------


def test_sample_timestamps_returns_all_when_fewer_than_count():
    ts = ["a", "b", "c"]
    assert wayback._sample_timestamps(ts, 6) == ts


def test_sample_timestamps_caps_at_count():
    ts = [str(i) for i in range(50)]
    sample = wayback._sample_timestamps(ts, 6)
    assert len(sample) == 6


# ---------------------------------------------------------------------------
# _fmt_ts
# ---------------------------------------------------------------------------


def test_fmt_ts_formats_yyyymmdd():
    assert wayback._fmt_ts("20150601000000") == "2015-06-01"


def test_fmt_ts_passes_through_short_strings():
    assert wayback._fmt_ts("2015") == "2015"


# ---------------------------------------------------------------------------
# verify_wayback: end-to-end with _cdx_query monkeypatched
# ---------------------------------------------------------------------------


def test_verify_wayback_empty_url_returns_empty():
    assert wayback.verify_wayback("") == []
    assert wayback.verify_wayback(None) == []


def test_verify_wayback_no_captures_returns_empty(monkeypatch):
    monkeypatch.setattr(wayback, "_cdx_query", lambda url: [])
    assert wayback.verify_wayback("https://never-archived.example") == []


def test_verify_wayback_query_failure_returns_empty(monkeypatch):
    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(wayback, "_cdx_query", boom)
    assert wayback.verify_wayback("https://example.com") == []


def test_verify_wayback_happy_path_evidence_shape(monkeypatch):
    monkeypatch.setattr(wayback, "_cdx_query", lambda url: _SAMPLE_CDX_ROWS)

    evidence = wayback.verify_wayback("https://example.com")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "wayback_machine"
    assert record["weight"] == 0.8
    # URL-bound: always high confidence.
    assert record["match_confidence"] == "high"
    assert "2015-06-01" in record["snippet"]
    assert "20150601000000/https://example.com" in record["source_url"]


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_wayback_openai():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real Wayback CDX API call")

    evidence = wayback.verify_wayback("https://openai.com")
    assert evidence, "expected at least one capture for a well-known domain"
