"""Thin web-search wrapper.

Adapted from the ATLAS search client (SearXNG plus Brave fallback). That
client is news-only (SearXNG categories=news + Brave news endpoint). Claim
verification needs GENERAL web results ("does this company exist", "is this
person associated with it"), so this copy switches to SearXNG categories=general
and the Brave WEB endpoint. Optional Tavily and Exa backends support their free
allowances, while an ordered no-key DDGS fallback supports local macOS use
without Docker or another account.

Public surface:
    web_search(query, count=8) -> list[{title, url, snippet}]

Environment variables (all optional; graceful degradation to []):
    SEARXNG_URL   -- e.g. http://localhost:8080 ; skipped if unset/unreachable
    TAVILY_API_KEY -- optional free-tier Tavily search key
    EXA_API_KEY    -- optional free-credit Exa search key
    BRAVE_API_KEY -- Brave Search API key ; skipped if unset
    DDGS_ENABLED  -- 1/true/on enables the no-key DDGS fallback
    DDGS_BACKEND  -- ordered comma list; defaults to bing,yahoo,duckduckgo

No em dashes in this file (house rule).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

_BRAVE_WEB_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
_EXA_SEARCH_ENDPOINT = "https://api.exa.ai/search"
_REQUEST_TIMEOUT = 10
_BRAVE_COOLDOWN_SECS = 3600
_TAVILY_COOLDOWN_SECS = 3600
_EXA_COOLDOWN_SECS = 3600
_SEARXNG_COOLDOWN_SECS = 300
_DDGS_COOLDOWN_SECS = 300
_DDGS_MIN_INTERVAL_S = 0.35

# Simple in-process cooldown flags (no SQLite here; the caller runs briefly).
# _brave_exhausted_until: Brave is dark (quota/auth) until this wall-clock time.
# _searxng_dead_until: the SearXNG instance just failed (connection/non-200) and
# is treated as dark until this time. Both are the liveness signal search_healthy
# reads to tell "could not look" apart from "looked and found nothing".
_brave_exhausted_until: float = 0.0
_tavily_exhausted_until: float = 0.0
_exa_exhausted_until: float = 0.0
_searxng_dead_until: float = 0.0
_ddgs_dead_until: float = 0.0
_ddgs_last_request_at: float = 0.0
_ddgs_lock = threading.Lock()


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _extract_snippet(*candidates: Optional[str]) -> str:
    for c in candidates:
        if c:
            return c.strip()
    return ""


_SEARCH_REDIRECT_HOSTS = (
    "startpage.com",
    "duckduckgo.com",
    "google.com",
)
_REDIRECT_QUERY_KEYS = ("url", "uddg", "target", "q")
_TRACKING_QUERY_KEYS = frozenset(
    {"gclid", "fbclid", "ref", "referrer", "source", "campaign"}
)


def _canonical_result_url(raw_url: str) -> str:
    """Unwrap known search redirects and remove common tracking parameters."""
    url = (raw_url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = (parsed.hostname or "").lower()
    if any(host == item or host.endswith("." + item) for item in _SEARCH_REDIRECT_HOSTS):
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key in _REDIRECT_QUERY_KEYS:
            candidate = (params.get(key) or "").strip()
            for _ in range(2):
                candidate = unquote(candidate)
            if candidate.startswith(("http://", "https://")):
                url = candidate
                break
    try:
        parsed = urlparse(url)
        kept = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower.startswith("utm_") or key_lower in _TRACKING_QUERY_KEYS:
                continue
            kept.append((key, value))
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(kept, doseq=True),
                parsed.fragment,
            )
        )
    except Exception:
        return url


def _canonicalize_results(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        item = dict(row)
        item["url"] = _canonical_result_url(item.get("url") or "")
        if item["url"]:
            out.append(item)
    return out


def _searxng_search(base_url: str, query: str, count: int) -> list[dict]:
    """Query a SearXNG instance for GENERAL web results."""
    global _searxng_dead_until
    try:
        import requests  # lazy
    except ImportError:
        logger.warning("search: 'requests' not installed; cannot query SearXNG")
        return []

    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "pageno": 1,
    }
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/search", params=params, timeout=_REQUEST_TIMEOUT
        )
    except Exception:
        # A connection error means the instance is unreachable: mark it dark so
        # an empty result reads as "could not look", then re-raise into
        # web_search's existing catch (control flow unchanged).
        _searxng_dead_until = time.time() + _SEARXNG_COOLDOWN_SECS
        raise
    if resp.status_code != 200:
        logger.warning("search: SearXNG HTTP %d for %r", resp.status_code, query[:60])
        _searxng_dead_until = time.time() + _SEARXNG_COOLDOWN_SECS
        return []
    # A live 200 clears the dark mark: a recovered instance is trusted again.
    _searxng_dead_until = 0.0
    data = resp.json()
    out = []
    for r in data.get("results", [])[:count]:
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "url": r.get("url", ""),
                "snippet": _extract_snippet(r.get("content"), r.get("snippet")),
            }
        )
    return out


def _brave_search(api_key: str, query: str, count: int) -> list[dict]:
    """Query the Brave WEB Search API (not the news endpoint)."""
    global _brave_exhausted_until
    try:
        import requests  # lazy
    except ImportError:
        logger.warning("search: 'requests' not installed; cannot query Brave")
        return []

    if time.time() < _brave_exhausted_until:
        logger.debug("search: Brave in cooldown; skipping")
        return []

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(count, 20)}
    try:
        resp = requests.get(
            _BRAVE_WEB_ENDPOINT, params=params, headers=headers, timeout=_REQUEST_TIMEOUT
        )
    except Exception as exc:
        _brave_exhausted_until = time.time() + _BRAVE_COOLDOWN_SECS
        logger.warning("search: Brave request error: %s", exc)
        return []

    if resp.status_code in (401, 402, 403, 429):
        # 402/429 = quota exhausted; 401/403 = revoked/invalid key. A dead key is
        # exactly as dark as an exhausted one and must not keep reporting the
        # channel usable, so both cases enter the cooldown.
        logger.warning("search: Brave unusable (HTTP %d)", resp.status_code)
        _brave_exhausted_until = time.time() + _BRAVE_COOLDOWN_SECS
        return []
    if resp.status_code != 200:
        logger.warning("search: Brave HTTP %d for %r", resp.status_code, query[:60])
        if resp.status_code >= 500:
            _brave_exhausted_until = time.time() + _BRAVE_COOLDOWN_SECS
        return []

    data = resp.json()
    # Brave web results live under data["web"]["results"].
    results = (data.get("web") or {}).get("results", [])[:count]
    out = []
    for r in results:
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "url": r.get("url", ""),
                "snippet": _extract_snippet(
                    r.get("description"),
                    (r.get("extra_snippets") or [""])[0] if r.get("extra_snippets") else "",
                ),
            }
        )
    return out


def _tavily_search(api_key: str, query: str, count: int) -> list[dict]:
    """Query Tavily's general search endpoint."""
    global _tavily_exhausted_until
    try:
        import requests  # lazy
    except ImportError:
        logger.warning("search: 'requests' not installed; cannot query Tavily")
        return []

    if time.time() < _tavily_exhausted_until:
        return []
    try:
        resp = requests.post(
            _TAVILY_SEARCH_ENDPOINT,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max(1, min(count, 20)),
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as exc:
        _tavily_exhausted_until = time.time() + _TAVILY_COOLDOWN_SECS
        logger.warning("search: Tavily request error: %s", exc)
        return []
    if resp.status_code in (401, 402, 403, 429):
        _tavily_exhausted_until = time.time() + _TAVILY_COOLDOWN_SECS
        logger.warning("search: Tavily unusable (HTTP %d)", resp.status_code)
        return []
    if resp.status_code != 200:
        if resp.status_code >= 500:
            _tavily_exhausted_until = time.time() + _TAVILY_COOLDOWN_SECS
        logger.warning("search: Tavily HTTP %d for %r", resp.status_code, query[:60])
        return []
    _tavily_exhausted_until = 0.0
    return [
        {
            "title": (row.get("title") or "").strip(),
            "url": row.get("url") or "",
            "snippet": _extract_snippet(row.get("content")),
        }
        for row in (resp.json().get("results") or [])[:count]
    ]


def _exa_search(api_key: str, query: str, count: int) -> list[dict]:
    """Query Exa search with a short text extract for evidence snippets."""
    global _exa_exhausted_until
    try:
        import requests  # lazy
    except ImportError:
        logger.warning("search: 'requests' not installed; cannot query Exa")
        return []

    if time.time() < _exa_exhausted_until:
        return []
    try:
        resp = requests.post(
            _EXA_SEARCH_ENDPOINT,
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "type": "auto",
                "numResults": max(1, min(count, 20)),
                "contents": {"text": {"maxCharacters": 1000}},
            },
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as exc:
        _exa_exhausted_until = time.time() + _EXA_COOLDOWN_SECS
        logger.warning("search: Exa request error: %s", exc)
        return []
    if resp.status_code in (401, 402, 403, 429):
        _exa_exhausted_until = time.time() + _EXA_COOLDOWN_SECS
        logger.warning("search: Exa unusable (HTTP %d)", resp.status_code)
        return []
    if resp.status_code != 200:
        if resp.status_code >= 500:
            _exa_exhausted_until = time.time() + _EXA_COOLDOWN_SECS
        logger.warning("search: Exa HTTP %d for %r", resp.status_code, query[:60])
        return []
    _exa_exhausted_until = 0.0
    return [
        {
            "title": (row.get("title") or "").strip(),
            "url": row.get("url") or "",
            "snippet": _extract_snippet(row.get("text"), row.get("summary")),
        }
        for row in (resp.json().get("results") or [])[:count]
    ]


def _ddgs_search(query: str, count: int) -> list[dict]:
    """Query DDGS with no API key.

    DDGS talks to public search frontends, so it is less operationally stable
    than a self-hosted SearXNG instance or a paid API. Calls are serialized and
    lightly paced to reduce throttling. Any raised failure is handled by
    web_search(), which marks this backend dark before returning [].
    """
    global _ddgs_last_request_at
    with _ddgs_lock:
        wait_s = _DDGS_MIN_INTERVAL_S - (time.monotonic() - _ddgs_last_request_at)
        if wait_s > 0:
            time.sleep(wait_s)
        backend_spec = (
            os.environ.get("DDGS_BACKEND", "bing,yahoo,duckduckgo").strip()
            or "bing,yahoo,duckduckgo"
        )
        backends = [item.strip() for item in backend_spec.split(",") if item.strip()]
        rows = []
        completed_empty = False
        failures: list[Exception] = []
        for backend in backends:
            try:
                rows = _ddgs_backend_search(query, count, backend)
            except Exception as exc:
                if _ddgs_exception_means_empty(exc):
                    completed_empty = True
                    continue
                failures.append(exc)
                continue
            if rows:
                break
            completed_empty = True
        _ddgs_last_request_at = time.monotonic()
        if not rows and failures and not completed_empty:
            raise failures[-1]

    out = []
    for row in list(rows or [])[:count]:
        out.append(
            {
                "title": (row.get("title") or "").strip(),
                "url": row.get("href") or row.get("url") or "",
                "snippet": _extract_snippet(row.get("body"), row.get("snippet")),
            }
        )
    return out


def _ddgs_backend_search(query: str, count: int, backend: str) -> list[dict]:
    """One injectable DDGS engine attempt."""
    from ddgs import DDGS  # lazy: the backend is opt-in

    return DDGS(timeout=_REQUEST_TIMEOUT).text(
        query,
        max_results=max(1, min(count, 20)),
        backend=backend,
    )


def _ddgs_exception_means_empty(exc: Exception) -> bool:
    """True when DDGS reports a completed query with zero matches as an error."""
    text = str(exc or "").strip().lower()
    return text in {"no results", "no results found", "no results found."}


def search_available() -> bool:
    """True when at least one web-search backend is CONFIGURED at all (SEARXNG_URL
    or BRAVE_API_KEY set and non-empty). This is a deployment "is it even set up"
    probe; it does NOT know whether the configured backend is currently reachable.
    SUS-labeling callers that need to tell "searched and found nothing" apart from
    "could not search" must use search_healthy() instead, which also accounts for
    a dark (quota-exhausted, dead-key, unreachable) backend. Read from the
    environment each call so a test or a mid-run config change is honored; never
    raises.
    """
    if os.environ.get("SEARXNG_URL", "").strip():
        return True
    if os.environ.get("BRAVE_API_KEY", "").strip():
        return True
    if os.environ.get("TAVILY_API_KEY", "").strip():
        return True
    if os.environ.get("EXA_API_KEY", "").strip():
        return True
    if _env_true("DDGS_ENABLED"):
        return True
    return False


def search_healthy() -> bool:
    """True when at least one web-search backend is BOTH configured and not
    known-dead right now. This is the liveness signal the dossier labeling
    uses to tell "searched and found nothing" (a real SUS input) apart from
    "could not search" (no signal): a Brave key sitting in its quota/auth
    cooldown, or a SearXNG instance that just failed, is configured but
    dark, and an empty result under a dark channel must never read as
    proof-of-search. Implies search_available(); never raises.

    Optimistic by design: a configured backend that has not yet failed this
    process reads healthy. That is safe for the labeling call site because
    _aggregate labels a claim only AFTER its gather completed, and a gather
    that came back empty because Brave 402ed has already set the cooldown by
    then. No network probe here: the labeling path stays offline-cheap and
    deterministic in tests.
    """
    now = time.time()
    searxng_url = os.environ.get("SEARXNG_URL", "").strip()
    if searxng_url and now >= _searxng_dead_until:
        return True
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if brave_key and now >= _brave_exhausted_until:
        return True
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key and now >= _tavily_exhausted_until:
        return True
    exa_key = os.environ.get("EXA_API_KEY", "").strip()
    if exa_key and now >= _exa_exhausted_until:
        return True
    if _env_true("DDGS_ENABLED") and now >= _ddgs_dead_until:
        return True
    return False


def web_search(query: str, count: int = 8) -> list[dict]:
    """Run one web query. Returns [{title, url, snippet}] (never raises).

    SearXNG first, then optional free-allowance API backends, Brave, and the
    no-key DDGS chain. Returns [] if no backend is configured or reachable.
    """
    global _ddgs_dead_until
    searxng_url = os.environ.get("SEARXNG_URL", "").strip()
    if searxng_url:
        try:
            res = _searxng_search(searxng_url, query, count)
            if res:
                return _canonicalize_results(res)
        except Exception as exc:
            logger.warning("search: SearXNG failed (%s); trying Brave", exc)

    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()

    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            tavily_results = _tavily_search(tavily_key, query, count)
            if tavily_results:
                return _canonicalize_results(tavily_results)
        except Exception as exc:
            logger.warning("search: Tavily failed (%s)", exc)

    exa_key = os.environ.get("EXA_API_KEY", "").strip()
    if exa_key:
        try:
            exa_results = _exa_search(exa_key, query, count)
            if exa_results:
                return _canonicalize_results(exa_results)
        except Exception as exc:
            logger.warning("search: Exa failed (%s)", exc)

    if brave_key:
        try:
            brave_results = _brave_search(brave_key, query, count)
            if brave_results:
                return _canonicalize_results(brave_results)
        except Exception as exc:
            logger.warning("search: Brave failed (%s)", exc)

    if _env_true("DDGS_ENABLED"):
        if time.time() < _ddgs_dead_until:
            logger.debug("search: DDGS in cooldown; skipping")
        else:
            try:
                ddgs_results = _ddgs_search(query, count)
                _ddgs_dead_until = 0.0
                return _canonicalize_results(ddgs_results)
            except Exception as exc:
                if _ddgs_exception_means_empty(exc):
                    _ddgs_dead_until = 0.0
                    return []
                _ddgs_dead_until = time.time() + _DDGS_COOLDOWN_SECS
                logger.warning("search: DDGS failed (%s)", exc)

    logger.debug("search: no usable search backend; returning []")

    return []
