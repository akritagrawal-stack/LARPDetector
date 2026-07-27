"""Offline tests for detective.sources.hackernews. No network: the internal
_search_stories / _search_show_hn_count / _search_comments / _get_user /
_oldest_reachable_post_date functions are monkeypatched with realistic
sample HN Algolia response shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.hackernews as hackernews


def _story_hit(title: str, points: int = 10, num_comments: int = 5, created_at: str = "2026-03-05T22:50:14Z", object_id: str = "47268351"):
    return {
        "title": title,
        "points": points,
        "num_comments": num_comments,
        "created_at": created_at,
        "objectID": object_id,
    }


def _comment_hit(comment_text: str, created_at: str = "2026-04-10T00:00:00Z", object_id: str = "47717339", author: str = "someone"):
    return {"comment_text": comment_text, "created_at": created_at, "objectID": object_id, "author": author}


# ---------------------------------------------------------------------------
# verify_hackernews: basic gating
# ---------------------------------------------------------------------------


def test_verify_hackernews_blank_query_and_no_person_returns_empty():
    assert hackernews.verify_hackernews("") == []
    assert hackernews.verify_hackernews(None, person=None) == []


def test_story_search_failure_does_not_block_comment_search(monkeypatch):
    def boom(query):
        raise RuntimeError("network down")

    monkeypatch.setattr(hackernews, "_search_stories", boom)
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [_comment_hit("this is a wrapper around GPT")])

    evidence = hackernews.verify_hackernews("Cluely")
    assert len(evidence) == 1
    assert "wrapper" in evidence[0]["snippet"]


def test_no_story_hits_and_no_critical_comments_returns_empty(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])
    assert hackernews.verify_hackernews("Totally Obscure Product Zzz") == []


# ---------------------------------------------------------------------------
# Thread evidence: Show HN count + top threads
# ---------------------------------------------------------------------------


def test_thread_record_shape_and_confidence(monkeypatch):
    hits = [_story_hit("Cluely CEO admits to publicly lying about revenue numbers")]
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (514, hits))
    monkeypatch.setattr(hackernews, "_search_show_hn_count", lambda query: 33)
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])

    evidence = hackernews.verify_hackernews("Cluely")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "hackernews"
    assert record["weight"] == 0.64
    assert record["match_confidence"] == "medium"
    assert "514 stor" in record["snippet"]
    assert "33" in record["snippet"]
    assert "lying about revenue" in record["snippet"]


def test_thread_confidence_never_reaches_high(monkeypatch):
    hits = [_story_hit("Some thread about the product")]
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (1, hits))
    monkeypatch.setattr(hackernews, "_search_show_hn_count", lambda query: 0)
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])

    evidence = hackernews.verify_hackernews("Some Product")
    assert evidence[0]["match_confidence"] != "high"


# ---------------------------------------------------------------------------
# Critical comment evidence: the wrapper/vibecoded/scam signal
# ---------------------------------------------------------------------------


def test_critical_comment_keywords_are_surfaced(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    hits = [
        _comment_hit("this is just a thin wrapper around an OpenAI call"),
        _comment_hit("I love this product, works great"),
    ]
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: hits)

    evidence = hackernews.verify_hackernews("Acme AI")
    assert len(evidence) == 1
    assert "wrapper" in evidence[0]["snippet"]
    assert "I love this product" not in evidence[0]["snippet"]


def test_no_critical_comments_yields_no_comment_record(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    hits = [_comment_hit("I love this product, works great")]
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: hits)

    evidence = hackernews.verify_hackernews("Acme AI")
    assert evidence == []


def test_html_is_stripped_from_comment_snippets(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    hits = [_comment_hit("this looks like a <i>scam</i> to me, total <b>grift</b>")]
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: hits)

    evidence = hackernews.verify_hackernews("Acme AI")
    assert "<i>" not in evidence[0]["snippet"]
    assert "<b>" not in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# Account-age evidence: username != legal name, bio corroboration
# ---------------------------------------------------------------------------


def test_person_lookup_not_found_returns_no_account_record(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])
    monkeypatch.setattr(hackernews, "_get_user", lambda username: None)

    evidence = hackernews.verify_hackernews("Acme AI", person="janedoe")
    assert evidence == []


def test_person_lookup_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])

    def boom(username):
        raise RuntimeError("network down")

    monkeypatch.setattr(hackernews, "_get_user", boom)

    evidence = hackernews.verify_hackernews("Acme AI", person="janedoe")
    assert evidence == []


def test_account_record_low_confidence_without_bio_match(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])
    monkeypatch.setattr(hackernews, "_get_user", lambda username: {"karma": 500, "about": "I like bugs."})
    monkeypatch.setattr(hackernews, "_oldest_reachable_post_date", lambda username: "2020-01-01")

    evidence = hackernews.verify_hackernews("Acme AI", person="janedoe")
    assert len(evidence) == 1
    record = evidence[0]
    assert record["match_confidence"] == "low"
    assert record["source_name"] == "hackernews"
    assert "2020-01-01" in record["snippet"]


def test_account_record_medium_confidence_with_bio_match(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])
    monkeypatch.setattr(
        hackernews, "_get_user", lambda username: {"karma": 500, "about": "Founder of Acme AI, builder."}
    )
    monkeypatch.setattr(hackernews, "_oldest_reachable_post_date", lambda username: "2020-01-01")

    evidence = hackernews.verify_hackernews("Acme AI", person="janedoe")
    assert evidence[0]["match_confidence"] == "medium"


def test_account_record_never_reaches_high(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])
    monkeypatch.setattr(
        hackernews, "_get_user", lambda username: {"karma": 99999, "about": "Founder of Acme AI."}
    )
    monkeypatch.setattr(hackernews, "_oldest_reachable_post_date", lambda username: "2010-01-01")

    evidence = hackernews.verify_hackernews("Acme AI", person="janedoe")
    assert all(e["match_confidence"] != "high" for e in evidence)


def test_no_person_given_skips_account_lookup_entirely(monkeypatch):
    monkeypatch.setattr(hackernews, "_search_stories", lambda query: (0, []))
    monkeypatch.setattr(hackernews, "_search_comments", lambda query: [])

    called = {"get_user": False}

    def fake_get_user(username):
        called["get_user"] = True
        return None

    monkeypatch.setattr(hackernews, "_get_user", fake_get_user)

    hackernews.verify_hackernews("Acme AI", person=None)
    assert called["get_user"] is False


# ---------------------------------------------------------------------------
# _looks_critical / _strip_html: pure functions
# ---------------------------------------------------------------------------


def test_looks_critical_matches_keywords():
    assert hackernews._looks_critical("this is a total scam")
    assert hackernews._looks_critical("just vibecoded garbage")
    assert not hackernews._looks_critical("great product, love it")


def test_strip_html_removes_tags():
    assert hackernews._strip_html("hello <b>world</b>") == "hello world"
    assert hackernews._strip_html("") == ""
    assert hackernews._strip_html(None) == ""


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_hackernews_cluely():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real HN Algolia API calls")

    evidence = hackernews.verify_hackernews("Cluely")
    assert evidence, "expected HN Algolia to find real Cluely-related threads/comments"
