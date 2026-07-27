"""News-coverage connector: is there genuine third-party press coverage of the
subject (a person or a company), as opposed to only the subject's own
announcements republished?

WHAT THIS CATCHES (and the discipline it must keep): real editorial coverage
from an independent outlet is corroborating FOOTPRINT: a stranger with no
stake wrote about the subject, so the subject exists and is at least somewhat
notable. But the corroboration discipline this whole engine lives by
(reporting a claim is NOT confirming it) means a source that merely REPRINTS
the subject's own press release is not corroboration of anything the release
asserts. A "$10M ARR" number is exactly as unverified when a PR wire carries
it as when the company's own blog does; the wire just retypes the company.

So this connector classifies each search hit by where it came from:
  - a recognized independent outlet (Reuters, TechCrunch, NYT, ...) whose hit
    is NOT a syndicated press release -> COVERAGE, corroborating,
    match_confidence "medium" (the honest ceiling: a search snippet cannot
    prove the article is about THIS same-named subject, so this never reaches
    "high").
  - a known PR wire / self-publish host (PR Newswire, Business Wire,
    GlobeNewswire, ...), OR any hit whose URL/snippet carries a press-release
    marker even on an otherwise-recognized domain (a syndicated release) ->
    REPRINT, NOT corroboration, surfaced but marked "low" and explicitly
    flagged as the subject restating itself.
  - anything else (an unrecognized blog/domain) -> skipped, so the connector
    never manufactures a corroboration signal it cannot stand behind.

It only GATHERS; it never sets a tier or a verdict (see verify.py).

Public surface:
    verify_news(subject, is_company=False) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging

from ..search import web_search
from .registry import weight_for

logger = logging.getLogger(__name__)

_SOURCE_NAME = "news_coverage"
_MAX_SEARCH_RESULTS = 8
_MAX_RECORDS = 4

# Recognized independent outlets. A hit on one of these (and NOT carrying a
# press-release marker, see below) is treated as genuine editorial coverage.
# Deliberately broad but still an allowlist: an outlet not listed here is
# skipped rather than counted, which misses smaller/local coverage (a known,
# documented limitation) in exchange for never over-crediting an unknown blog.
_NEWS_DOMAINS = (
    "reuters.com", "apnews.com", "nytimes.com", "washingtonpost.com", "wsj.com",
    "bloomberg.com", "forbes.com", "cnbc.com", "cnn.com", "bbc.com", "bbc.co.uk",
    "npr.org", "theguardian.com", "ft.com", "economist.com", "politico.com",
    "axios.com", "propublica.org", "businessinsider.com", "theverge.com",
    "techcrunch.com", "wired.com", "arstechnica.com", "engadget.com",
    "venturebeat.com", "theinformation.com", "fortune.com", "fastcompany.com",
    "cbsnews.com", "nbcnews.com", "abcnews.go.com", "usatoday.com",
    "latimes.com", "wikipedia.org",
)

# Known press-release wires and self-publish hosts: a hit here is the subject
# (or its agency) restating itself, never independent coverage.
_PRESS_RELEASE_DOMAINS = (
    "prnewswire.com", "businesswire.com", "globenewswire.com", "einpresswire.com",
    "prweb.com", "accesswire.com", "newswire.com", "openpr.com", "prlog.org",
    "issuewire.com", "pr.com", "24-7pressrelease.com", "presswire.com",
    "medium.com", "substack.com", "prunderground.com", "send2press.com",
)

# Markers that flag a hit as a syndicated press release even when it lands on
# an otherwise-recognized outlet domain (a wire piece republished onto a
# finance/affiliate page). Matched against the URL and the snippet text.
_PRESS_RELEASE_MARKERS = (
    "prnewswire", "businesswire", "globenewswire", "einpresswire", "accesswire",
    "/press-release", "press-releases", "/pressrelease", "prweb", "/prnewswire",
)


def _search_news(subject: str) -> list[dict]:
    """Targeted web search for third-party coverage of the subject. Returns
    the raw web_search result dicts ({title, url, snippet}); the caller
    classifies them by domain. Reuses the shared web_search primitive
    (SearXNG/Brave), the same one verify.py uses, rather than inventing a new
    HTTP client.
    """
    query = f'"{subject}" (news OR reported OR interview OR coverage OR profile)'
    return web_search(query, count=_MAX_SEARCH_RESULTS) or []


def _domain_of(url: str) -> str:
    return (url or "").split("//")[-1].split("/")[0].lower()


def _domain_matches(domain: str, needles: tuple[str, ...]) -> bool:
    # Suffix-aware so "finance.yahoo.com" or "www.reuters.com" both match.
    return any(domain == n or domain.endswith("." + n) for n in needles)


def _looks_like_press_release(url: str, snippet: str) -> bool:
    blob = f"{(url or '').lower()} {(snippet or '').lower()}"
    return any(m in blob for m in _PRESS_RELEASE_MARKERS)


def _classify(url: str, snippet: str) -> str:
    """One of "coverage", "reprint", or "skip" for a single hit.

    A press-release wire domain, or ANY hit carrying a press-release marker
    (even on a recognized outlet, i.e. a syndicated release), is a "reprint".
    A recognized outlet with no such marker is "coverage". Everything else is
    "skip".
    """
    domain = _domain_of(url)
    if not domain:
        return "skip"
    if _domain_matches(domain, _PRESS_RELEASE_DOMAINS):
        return "reprint"
    if _looks_like_press_release(url, snippet):
        return "reprint"
    if _domain_matches(domain, _NEWS_DOMAINS):
        return "coverage"
    return "skip"


def _build_coverage_record(subject: str, url: str, snippet: str) -> dict:
    text = (snippet or "").strip()
    body = (
        f"Independent press coverage of {subject!r} from {_domain_of(url)}: "
        f"\"{text}\" This is genuine third-party editorial footprint (an outside "
        "outlet chose to write about the subject), corroborating that the subject "
        "exists and is at least somewhat notable. It is not, by itself, confirmation "
        "of any specific numeric or role claim, and a snippet cannot prove the "
        "article is about this exact (possibly same-named) subject, so confidence "
        "stays medium."
    )
    return {
        "source_url": url,
        "snippet": body,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "medium",
    }


def _build_reprint_record(subject: str, url: str, snippet: str) -> dict:
    text = (snippet or "").strip()
    body = (
        f"Press release / reprint about {subject!r} at {url}: \"{text}\" This is the "
        "subject restating its own announcement (a PR wire or a syndicated release), "
        "NOT independent coverage: reporting or reprinting a claim is not confirming "
        "it, so this does not corroborate the claim's truth. Surfaced only so the "
        "absence of genuine third-party coverage is visible; low confidence."
    )
    return {
        "source_url": url,
        "snippet": body,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "low",
    }


def verify_news(subject: str, is_company: bool = False) -> list[dict]:
    """Search for genuine third-party news/press coverage of the subject.

    Returns up to a few evidence records: "coverage" records (recognized
    outlet, corroborating, match_confidence "medium") and "reprint" records (a
    PR wire or a syndicated press release, NOT corroboration, match_confidence
    "low", flagged as the subject restating itself). Unrecognized domains are
    skipped. Returns [] when the subject is blank, nothing was found, or every
    path failed. Never raises.

    is_company is accepted so the caller can gate this on person vs company
    scans; the search and classification are the same either way (the subject
    string is what differs), and it is reserved for future outlet-weighting
    without changing the call sites.
    """
    subject = (subject or "").strip()
    if not subject:
        return []

    try:
        results = _search_news(subject)
    except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
        logger.warning("news: search failed for %r: %s", subject, exc)
        return []
    if not results:
        return []

    evidence: list[dict] = []
    seen_urls: set[str] = set()
    for r in results:
        url = (r.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        snippet = r.get("snippet") or r.get("title") or ""
        kind = _classify(url, snippet)
        if kind == "coverage":
            evidence.append(_build_coverage_record(subject, url, snippet))
        elif kind == "reprint":
            evidence.append(_build_reprint_record(subject, url, snippet))
        else:
            continue
        seen_urls.add(url)
        if len(evidence) >= _MAX_RECORDS:
            break

    return evidence
