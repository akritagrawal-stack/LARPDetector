"""Tests for search-channel HEALTH (liveness), not just config.

search_available() answers "is any backend configured at all". search_healthy()
answers the sharper question the dossier labeling actually needs: "is any backend
configured AND not known-dark right now". A Brave key sitting in its quota/auth
cooldown, or a SearXNG instance that just failed, is configured but dark, so an
empty search result under it must read as "could not look", never as "looked and
found nothing". These tests pin that distinction and the dead-marking / recovery
behavior of the two backends.

The cooldown globals are process state; an autouse fixture resets all of them at
setup and teardown so a test that flips
one through a real web_search() call (test 5) cannot leak into other test files.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import time

import pytest

from detective import search


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    # Reset inbound AND outbound: test 5 mutates _searxng_dead_until through a
    # real web_search() (a genuine assignment inside _searxng_search, not a
    # monkeypatch), so setup-only reset would still leak it downstream.
    search._brave_exhausted_until = 0.0
    search._tavily_exhausted_until = 0.0
    search._exa_exhausted_until = 0.0
    search._searxng_dead_until = 0.0
    search._ddgs_dead_until = 0.0
    search._ddgs_last_request_at = 0.0
    yield
    search._brave_exhausted_until = 0.0
    search._tavily_exhausted_until = 0.0
    search._exa_exhausted_until = 0.0
    search._searxng_dead_until = 0.0
    search._ddgs_dead_until = 0.0
    search._ddgs_last_request_at = 0.0


class _FakeResp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# search_healthy: configuration + liveness
# ---------------------------------------------------------------------------


def test_search_healthy_false_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("DDGS_ENABLED", raising=False)
    assert search.search_healthy() is False


def test_empty_searxng_url_is_not_configured(monkeypatch):
    # The live .env failure mode: SEARXNG_URL is SET but empty. It must count as
    # unconfigured for BOTH predicates, forever.
    monkeypatch.setenv("SEARXNG_URL", "")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("DDGS_ENABLED", raising=False)
    assert search.search_available() is False
    assert search.search_healthy() is False


def test_brave_in_cooldown_is_unhealthy_but_available(monkeypatch):
    # The void leak itself: Brave is CONFIGURED (available True) but sitting in
    # its quota/auth cooldown (healthy False). An empty result here is "could
    # not look", not "looked and found nothing".
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "abc")
    search._brave_exhausted_until = time.time() + 100
    assert search.search_available() is True
    assert search.search_healthy() is False


def test_brave_configured_and_warm_is_healthy(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "abc")
    search._brave_exhausted_until = 0.0
    assert search.search_healthy() is True


def test_searxng_dead_marking_and_recovery(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    import requests

    # First: SearXNG answers HTTP 500. web_search must mark it dead so a later
    # health check reads False.
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(500))
    assert search.web_search("q") == []
    assert search.search_healthy() is False

    # Then: SearXNG recovers with a 200 + empty results payload. The dead mark
    # clears and health reads True again, even though the search itself is [].
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200, {"results": []}))
    assert search.web_search("q") == []
    assert search.search_healthy() is True


def test_brave_401_and_403_trigger_cooldown(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "abc")

    import requests

    # A revoked/invalid key (401) is as dark as an exhausted one: it must set
    # the cooldown, not keep reporting the channel usable.
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(401))
    assert search.web_search("q") == []
    assert search._brave_exhausted_until > time.time()
    assert search.search_healthy() is False

    # Same for a forbidden (403) response.
    search._brave_exhausted_until = 0.0
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(403))
    assert search.web_search("q") == []
    assert search._brave_exhausted_until > time.time()
    assert search.search_healthy() is False


def test_ddgs_opt_in_is_available_and_healthy(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("DDGS_ENABLED", "1")

    assert search.search_available() is True
    assert search.search_healthy() is True


def test_optional_free_allowance_backends_are_configured_independently(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("DDGS_ENABLED", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert search.search_available() is True
    assert search.search_healthy() is True

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("EXA_API_KEY", "exa-test")
    assert search.search_available() is True
    assert search.search_healthy() is True


def test_tavily_and_exa_result_shapes(monkeypatch):
    import requests

    responses = [
        _FakeResp(
            200,
            {
                "results": [
                    {
                        "title": "Tavily result",
                        "url": "https://example.com/tavily",
                        "content": "Tavily snippet",
                    }
                ]
            },
        ),
        _FakeResp(
            200,
            {
                "results": [
                    {
                        "title": "Exa result",
                        "url": "https://example.com/exa",
                        "text": "Exa snippet",
                    }
                ]
            },
        ),
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **k: responses.pop(0))

    assert search._tavily_search("key", "query", 2)[0]["snippet"] == "Tavily snippet"
    assert search._exa_search("key", "query", 2)[0]["snippet"] == "Exa snippet"


def test_ddgs_tries_ordered_backends_until_one_returns_results(monkeypatch):
    attempted = []

    def fake_backend_search(query, count, backend):
        attempted.append(backend)
        if backend == "bing":
            raise RuntimeError("rate limited")
        return [
            {
                "title": "Exact role result",
                "href": "https://example.com/role",
                "body": "Person worked in the claimed role.",
            }
        ]

    monkeypatch.setenv("DDGS_BACKEND", "bing,yahoo,duckduckgo")
    monkeypatch.setattr(search, "_ddgs_backend_search", fake_backend_search)

    rows = search._ddgs_search("person role", 3)

    assert attempted == ["bing", "yahoo"]
    assert rows[0]["url"] == "https://example.com/role"


def test_ddgs_result_shape_and_fallback(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("DDGS_ENABLED", "true")
    monkeypatch.setattr(
        search,
        "_ddgs_search",
        lambda query, count: [
            {"title": "Acme", "url": "https://example.com/acme", "snippet": "Independent result"}
        ],
    )

    assert search.web_search("acme", count=3) == [
        {"title": "Acme", "url": "https://example.com/acme", "snippet": "Independent result"}
    ]


def test_ddgs_failure_marks_backend_dark(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("DDGS_ENABLED", "on")

    def boom(query, count):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(search, "_ddgs_search", boom)
    assert search.web_search("acme") == []
    assert search._ddgs_dead_until > time.time()
    assert search.search_available() is True
    assert search.search_healthy() is False


def test_ddgs_no_results_is_completed_empty_not_an_outage(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("DDGS_ENABLED", "on")

    def empty(query, count):
        raise RuntimeError("No results found.")

    monkeypatch.setattr(search, "_ddgs_search", empty)

    assert search.web_search("obscure exact query") == []
    assert search._ddgs_dead_until == 0.0
    assert search.search_healthy() is True


def test_search_redirect_is_canonicalized_and_tracking_is_removed(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("DDGS_ENABLED", "1")
    monkeypatch.setattr(
        search,
        "_ddgs_search",
        lambda query, count: [
            {
                "title": "Acme",
                "url": (
                    "https://www.startpage.com/do/dsearch?"
                    "url=https%3A%2F%2Facme.example%2Fapp%3Futm_source%3Dsearch%26ref%3Dresults"
                    "&cat=web&pl=opensearch"
                ),
                "snippet": "Acme app",
            }
        ],
    )

    rows = search.web_search("acme", count=1)

    assert rows[0]["url"] == "https://acme.example/app"
