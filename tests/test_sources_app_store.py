"""Offline tests for detective.sources.app_store. No network: the internal
_search / _lookup_by_id / _reviews functions are monkeypatched with
realistic sample iTunes Lookup/Search and Customer Reviews RSS response
shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.app_store as app_store


def _itunes_app(
    track_id: int = 1232780281,
    track_name: str = "Notion: Notes, Tasks, AI",
    seller_name: str = "Notion Labs, Incorporated",
    bundle_id: str = "notion.id",
    user_rating_count: int = 88542,
    average_user_rating: float = 4.77822,
    version: str = "1.7.322",
    current_version_release_date: str = "2026-07-17T07:10:18Z",
    release_date: str = "2017-09-14T19:07:31Z",
) -> dict:
    return {
        "trackId": track_id,
        "trackName": track_name,
        "sellerName": seller_name,
        "bundleId": bundle_id,
        "userRatingCount": user_rating_count,
        "averageUserRating": average_user_rating,
        "version": version,
        "currentVersionReleaseDate": current_version_release_date,
        "releaseDate": release_date,
    }


def _raw_review_entry(rating: str, updated: str, title: str = "Some review", content: str = "Body text"):
    """One review in the RAW Customer Reviews RSS JSON shape (im:rating
    etc.), for testing _parse_reviews_feed itself.
    """
    return {
        "im:rating": {"label": rating},
        "title": {"label": title},
        "content": {"label": content, "attributes": {"type": "text"}},
        "updated": {"label": updated + "T00:00:00-07:00"},
    }


def _parsed_review(rating: str, updated: str, title: str = "Some review", content: str = "Body text") -> dict:
    """One review already in the flat PARSED shape verify_app_store's
    _build_reviews_record consumes (what _reviews() itself returns), for
    tests that monkeypatch _reviews directly rather than going through
    _parse_reviews_feed.
    """
    return {"rating": rating, "title": title, "content": content, "updated": updated}


def _reviews_feed(entries: list[dict]) -> dict:
    return {"feed": {"entry": entries}}


# ---------------------------------------------------------------------------
# verify_app_store: basic gating
# ---------------------------------------------------------------------------


def test_verify_app_store_blank_input_returns_empty():
    assert app_store.verify_app_store("") == []
    assert app_store.verify_app_store(None) == []
    assert app_store.verify_app_store("", app_id=None) == []


def test_search_failure_returns_empty(monkeypatch):
    def boom(term):
        raise RuntimeError("network down")

    monkeypatch.setattr(app_store, "_search", boom)
    assert app_store.verify_app_store("Some Unfindable App") == []


def test_lookup_by_id_failure_returns_empty(monkeypatch):
    def boom(app_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(app_store, "_lookup_by_id", boom)
    assert app_store.verify_app_store("Notion", app_id="1232780281") == []


def test_no_search_results_returns_empty(monkeypatch):
    monkeypatch.setattr(app_store, "_search", lambda term: [])
    assert app_store.verify_app_store("Totally Fictional App Name Zzz") == []


def test_lookup_by_id_not_found_returns_empty(monkeypatch):
    monkeypatch.setattr(app_store, "_lookup_by_id", lambda app_id: None)
    assert app_store.verify_app_store("Notion", app_id="000000") == []


# ---------------------------------------------------------------------------
# match_confidence: only a supplied app id is "high". Every name-only catalog
# match stays "low" because the title alone cannot connect the listing to the
# claimed person or product.
# ---------------------------------------------------------------------------


def test_id_lookup_is_high_confidence(monkeypatch):
    app = _itunes_app()
    monkeypatch.setattr(app_store, "_lookup_by_id", lambda app_id: app)
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    evidence = app_store.verify_app_store("Notion", app_id="1232780281")
    assert evidence[0]["match_confidence"] == "high"


def test_exact_name_search_match_is_low_confidence_without_app_id(monkeypatch):
    results = [
        _itunes_app(track_id=1, track_name="Notion: Notes, Tasks, AI"),
        _itunes_app(track_id=2, track_name="Notion Calendar"),
    ]
    monkeypatch.setattr(app_store, "_search", lambda term: results)
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    evidence = app_store.verify_app_store("Notion: Notes, Tasks, AI")
    assert evidence[0]["match_confidence"] == "low"
    assert "NAME-ONLY MATCH" in evidence[0]["snippet"]


def test_no_clean_match_returns_checked_absent_record(monkeypatch):
    # MIGRATED from test_no_clean_name_match_returns_empty. When a term search
    # returns candidates but NONE cleanly name-matches the queried product, this
    # is now a COMPLETED catalog lookup: it emits one checked-absent record
    # (registry_check "absent"). The misleading-footprint guard is preserved: no
    # unrelated listing (here "Cognition" -> "Lumosity") ever surfaces, and
    # _reviews is never called for it.
    results = [
        _itunes_app(track_id=1, track_name="Lumosity: Brain Training"),
        _itunes_app(track_id=2, track_name="Peak - Brain Games & Training"),
    ]
    monkeypatch.setattr(app_store, "_search", lambda term: results)

    def _reviews_must_not_be_called(app_id):
        raise AssertionError("reviews must not be fetched when there is no match")

    monkeypatch.setattr(app_store, "_reviews", _reviews_must_not_be_called)

    evidence = app_store.verify_app_store("Cognition")
    assert len(evidence) == 1
    rec = evidence[0]
    assert rec["registry_check"] == "absent"
    assert rec["match_confidence"] == "high"
    assert "completed catalog lookup" in rec["snippet"]
    # The unrelated Lumosity/Peak listing must never surface.
    assert "Lumosity" not in rec["snippet"] and "Peak" not in rec["snippet"]


def test_search_failure_still_returns_empty(monkeypatch):
    # The load-bearing failure/empty distinction: when _search FAILS (returns
    # None, no catalog payload), verify_app_store emits NO record at all ("we
    # could not look" is never a checked-absent read).
    monkeypatch.setattr(app_store, "_search", lambda term: None)
    assert app_store.verify_app_store("Some Unfindable App") == []


def test_ambiguous_multi_match_is_low_confidence(monkeypatch):
    # Two candidates both plausibly match the queried name (a prefix tie).
    results = [
        _itunes_app(track_id=1, track_name="Acme"),
        _itunes_app(track_id=2, track_name="Acme Pro"),
    ]
    monkeypatch.setattr(app_store, "_search", lambda term: results)
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    evidence = app_store.verify_app_store("Acme")
    assert evidence[0]["match_confidence"] == "low"


# ---------------------------------------------------------------------------
# Evidence record shape + registry weight
# ---------------------------------------------------------------------------


def test_evidence_record_shape_and_weight(monkeypatch):
    monkeypatch.setattr(app_store, "_search", lambda term: [_itunes_app()])
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    evidence = app_store.verify_app_store("Notion: Notes, Tasks, AI")
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "app_store_play_store_reviews"
    assert record["weight"] == 0.8
    assert "88542 total rating" in record["snippet"]
    assert "version-history count" in record["snippet"]


def test_listing_record_surfaces_traction_signal(monkeypatch):
    """Feature 1: the rating COUNT (userRatingCount) is surfaced prominently as
    the store's popularity/footprint measure, named so the reasoning step and
    the inflation detector read it reliably."""
    monkeypatch.setattr(app_store, "_search", lambda term: [_itunes_app(user_rating_count=88542)])
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    snippet = app_store.verify_app_store("Notion: Notes, Tasks, AI")[0]["snippet"]
    assert "TRACTION SIGNAL" in snippet
    assert "userRatingCount is 88542" in snippet


def test_listing_record_flags_stale_listing(monkeypatch):
    """A store listing not updated in over a year is surfaced as a staleness /
    liveness signal (a real tell against an actively-growing claim)."""
    stale_app = _itunes_app(current_version_release_date="2023-01-01T00:00:00Z")
    monkeypatch.setattr(app_store, "_search", lambda term: [stale_app])
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    snippet = app_store.verify_app_store("Notion: Notes, Tasks, AI")[0]["snippet"]
    assert "not been updated in over" in snippet


def test_listing_record_fresh_listing_not_flagged_stale(monkeypatch):
    """A recently-updated listing must NOT carry the staleness note."""
    monkeypatch.setattr(app_store, "_search", lambda term: [_itunes_app()])
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    snippet = app_store.verify_app_store("Notion: Notes, Tasks, AI")[0]["snippet"]
    assert "not been updated in over" not in snippet


# ---------------------------------------------------------------------------
# Customer Reviews RSS: the core LARP tell (huge claim, tiny/no review
# activity) plus graceful handling of an empty/failed feed.
# ---------------------------------------------------------------------------


def test_reviews_record_appended_when_feed_has_entries(monkeypatch):
    monkeypatch.setattr(app_store, "_search", lambda term: [_itunes_app()])
    reviews = [_parsed_review("5", "2026-07-01"), _parsed_review("1", "2026-06-15")]
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: reviews)

    evidence = app_store.verify_app_store("Notion: Notes, Tasks, AI")
    assert len(evidence) == 2
    assert "review(s) fetched" in evidence[1]["snippet"]
    assert evidence[1]["source_name"] == "app_store_play_store_reviews"


def test_no_recent_reviews_flags_the_larp_tell(monkeypatch):
    monkeypatch.setattr(app_store, "_search", lambda term: [_itunes_app(user_rating_count=12)])
    # All reviews far in the past: none within the recency window.
    old_reviews = [_parsed_review("5", "2018-01-01"), _parsed_review("4", "2017-06-01")]
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: old_reviews)

    evidence = app_store.verify_app_store("Notion: Notes, Tasks, AI")
    assert len(evidence) == 2
    assert "No review activity" in evidence[1]["snippet"]
    assert "12 total rating" in evidence[0]["snippet"]


def test_empty_reviews_feed_does_not_add_second_record(monkeypatch):
    monkeypatch.setattr(app_store, "_search", lambda term: [_itunes_app()])
    monkeypatch.setattr(app_store, "_reviews", lambda app_id: [])

    evidence = app_store.verify_app_store("Notion: Notes, Tasks, AI")
    assert len(evidence) == 1


def test_reviews_fetch_failure_does_not_block_listing_record(monkeypatch):
    monkeypatch.setattr(app_store, "_search", lambda term: [_itunes_app()])

    def boom(app_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(app_store, "_reviews", boom)

    evidence = app_store.verify_app_store("Notion: Notes, Tasks, AI")
    assert len(evidence) == 1
    assert evidence[0]["source_name"] == "app_store_play_store_reviews"


# ---------------------------------------------------------------------------
# _parse_reviews_feed: pure parse function
# ---------------------------------------------------------------------------


def test_parse_reviews_feed_skips_non_review_entries():
    data = {
        "feed": {
            "entry": [
                {"title": {"label": "App summary, no rating"}},  # no im:rating
                _raw_review_entry("3", "2026-05-01", title="Mixed feelings"),
            ]
        }
    }
    parsed = app_store._parse_reviews_feed(data)
    assert len(parsed) == 1
    assert parsed[0]["rating"] == "3"
    assert parsed[0]["title"] == "Mixed feelings"


def test_parse_reviews_feed_handles_single_dict_entry():
    data = {"feed": {"entry": _raw_review_entry("5", "2026-05-01")}}
    parsed = app_store._parse_reviews_feed(data)
    assert len(parsed) == 1


def test_parse_reviews_feed_handles_empty_feed():
    assert app_store._parse_reviews_feed({"feed": {}}) == []
    assert app_store._parse_reviews_feed({}) == []


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_app_store_notion():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real iTunes API calls")

    evidence = app_store.verify_app_store("Notion")
    assert evidence, "expected the iTunes Search API to find the real Notion app"
