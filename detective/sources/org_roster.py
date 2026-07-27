"""Org-roster connector: does the subject's name actually appear on the
claimed employer/org's OWN public team / members / people / roster page?

WHAT THIS CATCHES: a claimed affiliation ("member of the Smith Lab at MIT",
"on the founding team at Acme Robotics", "officer of the XYZ student org")
that the org's own public roster does not back up, OR (the useful positive
case) a clean name match on the org's own people page, which is real
third-party corroboration the subject is not just self-asserting.

BEST-EFFORT DISCOVERY (read before trusting this evidence): there is no
directory of "the roster page for org X". This connector does a targeted web
search for the org name plus roster-flavored terms ("team", "members", "our
people", "roster", "lab members"), picks the best-looking candidate page(s),
fetches the raw HTML, strips it to visible text, and checks whether the
subject's full name appears. Every step is heuristic:
  - the search may surface the wrong org (a same-named company), a stale
    cached page, or an aggregator that is not the org's own site.
  - a modern roster is often client-rendered (a JS bundle this connector
    never executes), so the name can be genuinely present yet invisible to a
    raw-HTML fetch.
Because of the second point especially, ABSENCE IS NEVER DISPROOF. A name not
found on a fetched roster is recorded as a documented absence at "low"
confidence with an explicit "this does not disprove the affiliation" note,
never as a contradiction. Rosters routinely omit past members, junior/
contract staff, and students; the connector only GATHERS, it never sets a
tier or a verdict (that is the reasoning provider's job, see verify.py).

match_confidence:
  "high"   : the subject's full name was found on a page that also clearly
             identifies the claimed org (org name on the page or in its
             domain), i.e. a clean org-and-name match.
  "medium" : the subject's full name was found on a fetched candidate page,
             but nothing on that page/URL confirms it is the claimed org (it
             could be a same-named org or an aggregator).
  "low"    : the subject's name was NOT found on the fetched roster (a
             documented absence, never disproof).

Public surface:
    verify_org_roster(person, org) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..search import web_search
from .registry import weight_for

logger = logging.getLogger(__name__)

_SOURCE_NAME = "org_roster"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (public org roster / team-page check)"

# How many candidate roster pages to actually fetch. Kept small: page
# discovery is best-effort and each fetch is a real network call, so this
# tries the two best-looking candidates and stops at the first name match.
_MAX_CANDIDATES_TO_FETCH = 2
_MAX_SEARCH_RESULTS = 6

# Terms that make a search result LOOK like an org's own roster page, used
# both to build the discovery query and to score which returned result is the
# most roster-like candidate to fetch.
_ROSTER_QUERY_TERMS = (
    "team OR members OR \"our people\" OR roster OR \"lab members\" OR people OR staff"
)
_ROSTER_URL_HINTS = (
    "team", "members", "member", "people", "roster", "staff", "our-team",
    "about", "who-we-are", "leadership", "lab", "group", "faculty", "officers",
)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def _search_roster_pages(org: str) -> list[dict]:
    """Targeted web search for the org's own roster/team/people page. Returns
    the raw web_search result dicts ({title, url, snippet}); the caller ranks
    and fetches them. Reuses the shared web_search primitive (SearXNG/Brave),
    the same one verify.py uses, rather than inventing a new HTTP client.
    """
    query = f'"{org}" ({_ROSTER_QUERY_TERMS})'
    return web_search(query, count=_MAX_SEARCH_RESULTS) or []


def _fetch_page(url: str) -> Optional[str]:
    """Raw HTML for one candidate page, or None on a non-200 / non-text
    response or any error. The only page-fetch network call this module
    makes.
    """
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
        timeout=_TIMEOUT,
        allow_redirects=True,
    )
    if resp.status_code != 200:
        logger.warning("org_roster: HTTP %d for %r", resp.status_code, url)
        return None
    return resp.text or ""


def _visible_text(html: str) -> str:
    without_scripts = _SCRIPT_RE.sub(" ", html or "")
    without_styles = _STYLE_RE.sub(" ", without_scripts)
    text_only = _TAG_RE.sub(" ", without_styles)
    return _WHITESPACE_RE.sub(" ", text_only).strip()


def _norm(s: str) -> str:
    """Lowercase and collapse to single spaces, for a whitespace-tolerant
    substring match (so "Jane   Doe" in the HTML still matches "Jane Doe").
    """
    return _WHITESPACE_RE.sub(" ", (s or "").lower()).strip()


def _candidate_score(result: dict) -> int:
    """Higher is a more roster-like candidate. Counts roster hint tokens in
    the URL and title so a "/team" or "Our People" page outranks a bare
    homepage or a press article.
    """
    url = (result.get("url") or "").lower()
    title = (result.get("title") or "").lower()
    return sum(1 for h in _ROSTER_URL_HINTS if h in url or h in title)


def _rank_candidates(results: list[dict]) -> list[dict]:
    scored = [r for r in results if (r.get("url") or "").strip()]
    # Stable sort by descending roster-likeness; ties keep search-rank order.
    return sorted(scored, key=_candidate_score, reverse=True)


def _name_on_page(person: str, text: str) -> bool:
    """True when the subject's full name appears in the page's visible text.

    Requires the whole normalized name to appear as a substring (so a bare
    common first name never triggers a match); also accepts a first+last
    fallback for a three-plus-token name (middle name/initial dropped).
    """
    ntext = _norm(text)
    if not ntext:
        return False
    nperson = _norm(person)
    if nperson and nperson in ntext:
        return True
    tokens = nperson.split()
    if len(tokens) >= 3:
        first_last = f"{tokens[0]} {tokens[-1]}"
        if first_last in ntext:
            return True
    return False


def _org_confirmed(org: str, url: str, text: str) -> bool:
    """True when the claimed org is plausibly confirmed on the fetched page:
    the normalized org name appears in the page text or in the URL. Used to
    grade a name match "high" (org + name) vs "medium" (name only).
    """
    norg = _norm(org)
    if not norg:
        return False
    if norg in _norm(text):
        return True
    # Compare against the URL with separators flattened, so "acme robotics"
    # matches an "acmerobotics.com" or "acme-robotics.org" host.
    url_flat = re.sub(r"[^a-z0-9]", "", (url or "").lower())
    org_flat = re.sub(r"[^a-z0-9]", "", norg)
    return bool(org_flat) and org_flat in url_flat


def _build_match_record(person: str, org: str, url: str, confidence: str) -> dict:
    if confidence == "high":
        detail = (
            f"and the page/URL also identifies {org!r}, a clean org-and-name match. "
            "The org's own public roster listing the person is real third-party "
            "corroboration of the affiliation (not the subject self-asserting)."
        )
    else:
        detail = (
            f"but nothing on the page/URL confirms it is the claimed org {org!r} "
            "(it could be a same-named org or an aggregator), so this is medium "
            "corroboration, not a clean match."
        )
    snippet = (
        f"Public roster/team page at {url}: the name {person!r} appears in the page's "
        f"visible text {detail} Roster-page discovery is best-effort (heuristic web "
        "search plus a single raw-HTML fetch), so treat this as corroborating footprint, "
        "not proof of the specific role."
    )
    return {
        "source_url": url,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def _build_absence_record(person: str, org: str, url: str) -> dict:
    snippet = (
        f"Public roster/team page at {url} for {org!r} was fetched, but the name "
        f"{person!r} was NOT found in its visible text. This is a documented ABSENCE, "
        "not disproof: it does not disprove the claimed affiliation, because rosters "
        "routinely omit past, junior, and contract members, and a client-rendered "
        "people page can hide names from a raw-HTML fetch entirely. A low-weight "
        "absence signal only."
    )
    return {
        "source_url": url,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "low",
    }


def verify_org_roster(person: str, org: str) -> list[dict]:
    """Look for the subject's name on the claimed org's own public roster.

    Returns a single-record list: a corroborating match record (confidence
    "high" if the page also identifies the org, else "medium") when the name
    is found on a fetched candidate page, OR a documented-absence record
    (confidence "low", never disproof) when a roster page was fetched but the
    name was not on it. Returns [] when person or org is blank, no candidate
    roster page could be found, no candidate page could be fetched, or any
    network path failed. Never raises.
    """
    person = (person or "").strip()
    org = (org or "").strip()
    if not person or not org:
        return []

    try:
        results = _search_roster_pages(org)
    except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
        logger.warning("org_roster: roster search failed for %r: %s", org, exc)
        return []
    if not results:
        return []

    candidates = _rank_candidates(results)[:_MAX_CANDIDATES_TO_FETCH]
    if not candidates:
        return []

    last_fetched_url = ""
    for candidate in candidates:
        url = (candidate.get("url") or "").strip()
        if not url:
            continue
        try:
            html = _fetch_page(url)
        except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
            logger.warning("org_roster: page fetch failed for %r: %s", url, exc)
            continue
        if html is None:
            continue
        text = _visible_text(html)
        last_fetched_url = url
        if _name_on_page(person, text):
            confidence = "high" if _org_confirmed(org, url, text) else "medium"
            return [_build_match_record(person, org, url, confidence)]

    # A candidate page was fetched but the name was not on any of them: a
    # documented absence (never disproof). If NOTHING fetched, [] (there is no
    # roster to be absent from).
    if last_fetched_url:
        return [_build_absence_record(person, org, last_fetched_url)]
    return []
