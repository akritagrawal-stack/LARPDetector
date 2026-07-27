"""Hacker News connector: Show HN / Launch HN threads about a claimed
product, critical comment signal, and a founder's HN account age (when a
username is already known).

Free API (hn.algolia.com/api/v1), no key, no documented hard rate limit for
this low a call volume.

WHAT THIS CATCHES: a "wrapper", "vibecoded", or "scam" reputation that
already exists in public discussion the company's own marketing will never
surface, plus outright fabrication callouts ("Cluely CEO admits to publicly
lying about revenue numbers" is a real live HN story title, found by this
exact query during development).

IDENTITY AND BIAS CAVEATS (read before trusting this evidence):
  - A bare HN username is NOT a legal name. This module never assumes a
    username lookup identifies the claimed person; it only raises
    confidence when the account's own "about" bio text mentions the
    claimed company/product (the same disambiguator discipline github.py
    uses for a bio/company-field match).
  - HN's own account API (hn.algolia.com/api/v1/users/{username}) no longer
    exposes a created_at field (confirmed live against real accounts "pg"
    and "dang": the response now contains only about/karma/username). This
    module instead pages through that account's own post history via the
    search_by_date endpoint (sorted newest-first) to its last reachable
    page, and reports the OLDEST POST REACHABLE that way as a floor on
    account age ("at least this old"), never as the true join date.
    Algolia caps pagination at 1000 pages, so for a very high-volume
    account this floor undershoots the account's true age; it never
    overshoots it.
  - HN's community skews toward technical, contrarian, and occasionally
    needlessly harsh commentary. A critical comment is a real public
    reputation signal, not a verdict: this module never treats HN snark as
    proof of fabrication on its own.

match_confidence is "medium" at best for thread/comment evidence (a
full-text hit on the product name is real and grounded, but HN's
commentary culture and the lack of any moderation vetting keep this off
"high"), and "low" by default for account-age evidence unless the account's
own bio corroborates the claimed company (never "high": a username is not a
verified legal identity, full stop).

Public surface:
    verify_hackernews(query, person=None) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
_SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
_USER_URL = "https://hn.algolia.com/api/v1/users/{username}"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (Hacker News thread/comment/account check)"
_SOURCE_NAME = "hackernews"
_MAX_THREADS = 3
_MAX_COMMENTS = 3
_MAX_ALGOLIA_PAGE = 999  # Algolia caps nbPages at 1000 (indices 0..999)

_CRITICAL_COMMENT_KEYWORDS = (
    "wrapper", "vibecoded", "vibe-coded", "scam", "grift", "fraud", "fake",
    "does not do what it claims", "doesn't do what it claims", "snake oil",
    "overhyped", "exaggerat",
)


def _fetch_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(url, params=params, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        logger.warning("hackernews: HTTP %d for %r", resp.status_code, url)
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("hackernews: non-JSON response for %r: %s", url, exc)
        return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _search_stories(query: str) -> tuple[int, list[dict]]:
    data = _fetch_json(_SEARCH_URL, params={"query": query, "tags": "story", "hitsPerPage": _MAX_THREADS})
    if not data:
        return 0, []
    return data.get("nbHits", 0) or 0, data.get("hits") or []


def _search_show_hn_count(query: str) -> int:
    data = _fetch_json(_SEARCH_URL, params={"query": query, "tags": "show_hn", "hitsPerPage": 0})
    if not data:
        return 0
    return data.get("nbHits", 0) or 0


def _search_comments(query: str) -> list[dict]:
    data = _fetch_json(_SEARCH_URL, params={"query": query, "tags": "comment", "hitsPerPage": 20})
    if not data:
        return []
    return data.get("hits") or []


def _looks_critical(comment_text: str) -> bool:
    t = _strip_html(comment_text).lower()
    return any(kw in t for kw in _CRITICAL_COMMENT_KEYWORDS)


def _build_thread_record(query: str, total: int, show_hn_count: int, hits: list[dict]) -> Optional[dict]:
    if not hits:
        return None
    lines = [
        f"{h.get('title', 'untitled')!r} ({h.get('points', 0) or 0} points, "
        f"{h.get('num_comments', 0) or 0} comments, {(h.get('created_at') or '')[:10] or 'date unknown'})"
        for h in hits[:_MAX_THREADS]
    ]
    snippet = (
        f"Hacker News: {total} stor(y/ies) mention {query!r} ({show_hn_count} of them tagged Show HN). "
        "Top thread(s): " + "; ".join(lines) + ". "
        "HN's commentary skews technical/contrarian; treat this as a real public discussion "
        "signal, not a verdict."
    )
    top_id = hits[0].get("objectID")
    source_url = f"https://news.ycombinator.com/item?id={top_id}" if top_id else "https://hn.algolia.com"
    return {
        "source_url": source_url,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "medium",
    }


def _build_comment_record(query: str, critical_hits: list[dict]) -> Optional[dict]:
    if not critical_hits:
        return None
    lines = []
    for h in critical_hits[:_MAX_COMMENTS]:
        text = _strip_html(h.get("comment_text", ""))
        snippet_text = text[:200] + ("..." if len(text) > 200 else "")
        lines.append(f"{(h.get('created_at') or '')[:10] or 'date unknown'}: {snippet_text!r}")
    snippet = (
        f"Hacker News comments mentioning {query!r} include wrapper/scam/exaggeration-flavored "
        "language: " + "; ".join(lines) + ". HN skews contrarian and unmoderated; a critical "
        "comment is a real reputation signal, never proof of fabrication on its own."
    )
    top_id = critical_hits[0].get("objectID")
    source_url = f"https://news.ycombinator.com/item?id={top_id}" if top_id else "https://hn.algolia.com"
    return {
        "source_url": source_url,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "medium",
    }


def _get_user(username: str) -> Optional[dict]:
    return _fetch_json(_USER_URL.format(username=username))


def _oldest_reachable_post_date(username: str) -> str:
    """Best-effort account-age floor: the oldest post reachable by paging
    search_by_date (newest-first) to its last available page. Returns "" on
    any failure or if the account has no posts. See module docstring for
    why this is a floor ("at least this old"), never the true join date.
    """
    count_data = _fetch_json(_SEARCH_BY_DATE_URL, params={"tags": f"author_{username}", "hitsPerPage": 1})
    if not count_data:
        return ""
    total = count_data.get("nbHits", 0) or 0
    if total <= 0:
        return ""

    last_page = min(total - 1, _MAX_ALGOLIA_PAGE)
    page_data = _fetch_json(
        _SEARCH_BY_DATE_URL, params={"tags": f"author_{username}", "hitsPerPage": 1, "page": last_page}
    )
    if not page_data:
        return ""
    hits = page_data.get("hits") or []
    if not hits:
        return ""
    return (hits[0].get("created_at") or "")[:10]


def _build_account_record(username: str, user: dict, oldest_date: str, company_hint: str) -> dict:
    karma = user.get("karma", 0) or 0
    about = (user.get("about") or "")
    about_text = _strip_html(about)

    bio_matches = bool(company_hint) and company_hint.strip().lower() in about_text.lower()
    confidence = "medium" if bio_matches else "low"

    parts = [f"Hacker News account {username!r}: {karma} karma."]
    if oldest_date:
        parts.append(
            f"Oldest post reachable via search: {oldest_date} (a floor on account age; the true "
            "join date may be earlier, never later)."
        )
    if bio_matches:
        parts.append(f"Bio mentions {company_hint!r}, corroborating this is likely the same person.")
    else:
        parts.append(
            "A HN username is not a verified legal identity; nothing here confirms this account "
            "belongs to the claimed person unless the bio corroborates it."
        )

    return {
        "source_url": f"https://news.ycombinator.com/user?id={username}",
        "snippet": " ".join(parts),
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def _gather_account_evidence(person: str, company_hint: str) -> list[dict]:
    username = (person or "").strip()
    if not username:
        return []
    try:
        user = _get_user(username)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("hackernews: user lookup failed for %r: %s", username, exc)
        return []
    if not user:
        return []
    try:
        oldest_date = _oldest_reachable_post_date(username)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hackernews: post-history lookup failed for %r: %s", username, exc)
        oldest_date = ""
    return [_build_account_record(username, user, oldest_date, company_hint)]


def verify_hackernews(query: str, person: Optional[str] = None) -> list[dict]:
    """Show HN / Launch HN thread check, critical-comment scan, and (when
    `person` is already a known HN username) account-age check for one
    claimed product.

    Returns up to 3 evidence records (thread summary, critical-comment
    summary, account-age summary), or [] if the query is blank and no
    person is given, or every path found nothing / failed. Never raises.

    `person`, if passed, must already be a plausible HN username (HN has no
    real-name lookup); match_confidence for that record is "low" unless the
    account's own bio mentions the queried product, and "medium" is the
    ceiling for every other record this connector returns (see module
    docstring on why this never reaches "high").
    """
    query = (query or "").strip()
    evidence: list[dict] = []

    if query:
        try:
            total, story_hits = _search_stories(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hackernews: story search failed for %r: %s", query, exc)
            total, story_hits = 0, []

        if story_hits:
            try:
                show_hn_count = _search_show_hn_count(query)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hackernews: show_hn count failed for %r: %s", query, exc)
                show_hn_count = 0
            record = _build_thread_record(query, total, show_hn_count, story_hits)
            if record:
                evidence.append(record)

        try:
            comment_hits = _search_comments(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hackernews: comment search failed for %r: %s", query, exc)
            comment_hits = []

        critical_hits = [h for h in comment_hits if _looks_critical(h.get("comment_text", ""))]
        record = _build_comment_record(query, critical_hits)
        if record:
            evidence.append(record)

    if person:
        evidence.extend(_gather_account_evidence(person, query))

    return evidence
