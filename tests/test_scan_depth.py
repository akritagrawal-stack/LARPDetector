"""Tests for the extraction-depth honesty layer.

Covers the pure scan_depth classifier, the search-channel availability
predicate, and the shallow-scan absence-suppression gates. These are the
engine-side guarantees behind "no silent shallow scans": a scored SUS verdict
may only ever come from a FULL extraction with working evidence channels.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import pytest

from detective import dossier as D
from detective import search
from detective import verify
from detective.llm import compute_founder_score
from detective.models import Claim, EvidenceTier


# ---------------------------------------------------------------------------
# scan_depth classifier (pure)
# ---------------------------------------------------------------------------


def _live_person(exp_count: int = 2, with_desc: int = 1) -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/x/",
        "scan_type": "person",
        "identity": {"name": "X Y"},
        "experience": [{"title": "Analyst", "company": "Acme"}] * exp_count,
        "_extraction": {
            "method": "live_scrape",
            "experience_count": exp_count,
            "with_description_count": with_desc,
            "posts_count": 0,
            "details_page_loaded": True,
        },
    }


def test_scan_depth_full_for_live_person_with_experience():
    assert D.scan_depth(_live_person(exp_count=2)) == "full"


def test_scan_depth_shallow_for_injected_person_no_manifest():
    raw = _live_person()
    raw.pop("_extraction")
    assert D.scan_depth(raw) == "shallow"


def test_scan_depth_shallow_for_live_scrape_zero_experience():
    # A live scrape that parsed zero experience (login wall, layout break) is
    # NOT a full scan, even though the fetch method is live_scrape.
    assert D.scan_depth(_live_person(exp_count=0)) == "shallow"


def test_scan_depth_full_for_live_company_fetch():
    raw = {
        "profile_url": "https://app.test/",
        "scan_type": "company_app",
        "_extraction": {"method": "live_company_fetch"},
    }
    assert D.scan_depth(raw) == "full"


def test_scan_depth_shallow_for_injected_company():
    raw = {"profile_url": "https://app.test/", "scan_type": "company_app"}
    assert D.scan_depth(raw) == "shallow"


def test_scan_depth_shallow_for_method_injected():
    raw = _live_person()
    raw["_extraction"] = {"method": "injected"}
    assert D.scan_depth(raw) == "shallow"


# ---------------------------------------------------------------------------
# search-channel availability (per-channel honesty)
# ---------------------------------------------------------------------------


def test_search_available_false_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert search.search_available() is False


def test_search_available_true_with_brave(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "abc")
    assert search.search_available() is True


def test_search_available_true_with_searxng(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert search.search_available() is True


# ---------------------------------------------------------------------------
# search_unavailable marker: not "looked", so no GAP, no absence score
# ---------------------------------------------------------------------------


def _notable_claim_with_unavailable() -> Claim:
    c = Claim(
        type="employment",
        employer="Goldman Sachs",
        title="VP",
        assertion="VP at Goldman Sachs",
        expected_footprint="high",
    )
    c.evidence = [D._search_unavailable_record(c)]
    return c


def test_search_unavailable_record_shape():
    c = Claim(type="employment", employer="Acme")
    rec = D._search_unavailable_record(c)
    assert rec["source_name"] == "search_unavailable"
    assert rec["weight"] == 0.0


def test_detect_gap_skips_search_unavailable_only_claim():
    # A notable claim whose ONLY evidence is the search_unavailable marker was
    # never really looked at: it must NOT become a GAP.
    claims = [_notable_claim_with_unavailable()]
    findings = D.detect_gap(claims)
    assert all(f.kind != "GAP" for f in findings)


def test_search_unavailable_not_counted_as_searched_in_score():
    # A high-footprint UNVERIFIED claim whose only evidence is search_unavailable
    # must NOT accrue the SUS contribution (we could not look).
    c = _notable_claim_with_unavailable()
    c.tier = EvidenceTier.UNVERIFIED
    score = compute_founder_score([c])
    assert score == 0 or score is None


def test_search_unavailable_with_product_evidence_still_does_not_score_role_gap():
    c = _notable_claim_with_unavailable()
    c.evidence.insert(
        0,
        {
            "source_url": "https://example.test",
            "source_name": "product_site",
            "snippet": "The product exists, but this does not prove the person's role.",
            "match_confidence": "high",
        },
    )
    c.tier = EvidenceTier.UNVERIFIED
    score = compute_founder_score([c])
    assert score == 0 or score is None
    assert all(f.kind != "GAP" for f in D.detect_gap([c]))


# ---------------------------------------------------------------------------
# _aggregate labeling: an empty completed gather stamps the RIGHT marker based
# on search HEALTH (liveness), not just search config. This is the void-leak
# fix: a configured-but-dark backend must stamp search_unavailable, never
# searched_no_results (the Vedant false-SUS root cause).
# ---------------------------------------------------------------------------


def _empty_notable_claim() -> Claim:
    return Claim(
        type="employment",
        employer="Goldman Sachs",
        title="VP",
        assertion="VP at Goldman Sachs",
        expected_footprint="high",
    )


def test_aggregate_labels_search_unavailable_when_dark(monkeypatch):
    # Reproduces the Vedant mislabel: the gather completes but returns nothing
    # because every channel is DARK (Brave configured but quota-exhausted, no
    # SearXNG). search_available() is True but search_healthy() is False, so the
    # claim must be stamped search_unavailable ("could not look"), not
    # searched_no_results ("looked and found nothing").
    claim = _empty_notable_claim()
    monkeypatch.setattr(D.verify, "gather_evidence", lambda *a, **k: None)
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: False)

    D._aggregate([claim], {}, None, None, 1, 8, 5.0, lambda *a: None)

    assert len(claim.evidence) == 1
    assert claim.evidence[0]["source_name"] == "search_unavailable"


def test_aggregate_labels_searched_no_results_when_healthy(monkeypatch):
    # Anti-over-suppression guard: when search is HEALTHY and a notable claim
    # genuinely comes back empty, it must still be stamped searched_no_results,
    # keeping the real-fraud GAP path alive.
    claim = _empty_notable_claim()
    monkeypatch.setattr(D.verify, "gather_evidence", lambda *a, **k: None)
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: True)

    D._aggregate([claim], {}, None, None, 1, 8, 5.0, lambda *a: None)

    assert len(claim.evidence) == 1
    assert claim.evidence[0]["source_name"] == "searched_no_results"


def test_aggregate_preserves_connector_evidence_but_marks_dark_search(monkeypatch):
    claim = _empty_notable_claim()

    def gather_with_product_evidence(*args, **kwargs):
        claim.evidence = [
            {
                "source_url": "https://example.test",
                "source_name": "product_site",
                "snippet": "The product exists, but the role is not corroborated.",
            }
        ]
        claim._web_search_unavailable = True

    monkeypatch.setattr(D.verify, "gather_evidence", gather_with_product_evidence)
    monkeypatch.setattr(search, "search_healthy", lambda: False)

    D._aggregate([claim], {}, None, None, 1, 8, 5.0, lambda *a: None)

    assert any(e.get("source_name") == "product_site" for e in claim.evidence)
    assert any(e.get("source_name") == "search_unavailable" for e in claim.evidence)


# ---------------------------------------------------------------------------
# compute_founder_score shallow suppression (absence never scores on shallow)
# ---------------------------------------------------------------------------


def _high_footprint_unverified_looked() -> Claim:
    c = Claim(
        type="employment",
        employer="Goldman Sachs",
        title="VP",
        assertion="VP at Goldman Sachs",
        expected_footprint="high",
        tier=EvidenceTier.UNVERIFIED,
    )
    c.evidence = [{"source_url": "https://g.test", "snippet": "generic, no corroboration"}]
    return c


def test_shallow_scan_suppresses_sus_contribution():
    claims = [_high_footprint_unverified_looked()]
    full = compute_founder_score(claims, scan_depth="full")
    shallow = compute_founder_score(claims, scan_depth="shallow")
    assert full is not None and full >= 30  # a notable unverified is SUS on full
    assert shallow == 0  # never SUS on a shallow scan


# ---------------------------------------------------------------------------
# pipeline.run threads scan_depth from the extraction manifest (both engines)
# ---------------------------------------------------------------------------


def _fake_gather_generic(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    claim.evidence = [{"source_url": "https://g.test", "snippet": "generic, no corroboration"}]
    return claim


def _injected_person_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/x/",
        "scan_type": "person",
        "identity": {"name": "Test Person", "headline": "Analyst", "current_company": "Acme"},
        "experience": [{"title": "Analyst", "company": "Acme", "start_date": "2019", "end_date": "2021"}],
        "education": [],
    }


def test_pipeline_run_injected_profile_is_shallow(monkeypatch):
    from detective import pipeline, verify

    monkeypatch.setattr(verify, "gather_evidence", _fake_gather_generic)
    d = pipeline.run("https://x/", raw_profile=_injected_person_raw(), engine="per_claim")
    assert d.scan_depth == "shallow"


def test_pipeline_run_live_manifest_is_full(monkeypatch):
    from detective import pipeline, verify

    monkeypatch.setattr(verify, "gather_evidence", _fake_gather_generic)
    raw = _injected_person_raw()
    raw["_extraction"] = {"method": "live_scrape", "experience_count": 1}
    d = pipeline.run("https://x/", raw_profile=raw, engine="per_claim")
    assert d.scan_depth == "full"


def test_pipeline_run_dossier_engine_brands_injected_shallow(monkeypatch):
    from detective import pipeline, verify

    monkeypatch.setattr(verify, "gather_evidence", _fake_gather_generic)
    d = pipeline.run("https://x/", raw_profile=_injected_person_raw(), engine="dossier")
    assert d.scan_depth == "shallow"


def test_finalize_scores_honors_shallow_depth():
    # The ManualProvider queue-completion scorer must also suppress absence on a
    # shallow scan (a shallow dossier read back from a completed queue file).
    from detective.models import Dossier
    from detective.service import _finalize_scores

    c = _high_footprint_unverified_looked()
    d = Dossier(profile_url="x", scan_type="person", scan_depth="shallow", claims=[c])
    d.larp_score = 0  # operator finished tiering (the "job done" signal)
    _finalize_scores(d)
    assert d.founder_larp_score == 0

    c2 = _high_footprint_unverified_looked()
    d2 = Dossier(profile_url="x", scan_type="person", scan_depth="full", claims=[c2])
    d2.larp_score = 0
    _finalize_scores(d2)
    assert d2.founder_larp_score >= 30


def test_shallow_scan_keeps_disproven_contribution():
    c = Claim(
        type="employment",
        employer="Goldman Sachs",
        title="VP",
        assertion="VP at Goldman Sachs",
        expected_footprint="high",
        tier=EvidenceTier.DISPROVEN,
    )
    c.evidence = [{"source_url": "https://g.test", "snippet": "no record of this person"}]
    shallow = compute_founder_score([c], scan_depth="shallow")
    assert shallow is not None and shallow >= 66  # contradiction still scores
