"""arXiv connector: papers by author, with submission dates.

Free API (export.arxiv.org/api/query), Atom XML, no key, no documented hard
rate limit for this low a call volume. Issues exactly one request per
verify_arxiv call.

WHAT THIS PROVES AND WHAT IT DOES NOT: arXiv is a preprint server. Landing a
paper there requires no peer review at all, only a moderator endorsement
check; presence on arXiv proves the paper EXISTS and WHEN it was submitted
(the submission timestamp is not backdatable), it never proves the work was
vetted, correct, or influential. A "published researcher" claim resting only
on arXiv presence should never be treated as equivalent to a peer-reviewed
publication.

IDENTITY RESOLUTION IS ALSO WEAK: arXiv's au: search field is a fuzzy text
match over author bylines, not an author-ID lookup, so "au:Jane Smith"
returns every paper with any author whose name looks like that, real
namesakes included. This module never returns "high" confidence for that
reason: at best "medium" when the result count is small enough that the
hits plausibly cluster around one person, "low" when the result count is
large enough that a common-name collision is the more likely explanation.

Public surface:
    verify_arxiv(person_name) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_API_URL = "https://export.arxiv.org/api/query"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (arXiv author lookup)"
_SOURCE_NAME = "arxiv"
_MAX_RESULTS = 5
# Above this total-hit count, a two-or-three-token name is more plausibly a
# namesake collision than one person's whole body of work; below it, the
# results plausibly cluster around a single author (still never "high": see
# module docstring on why arXiv's author search cannot confirm identity).
_MANY_RESULTS_THRESHOLD = 20

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_OPENSEARCH_NS = "{http://a9.com/-/spec/opensearch/1.1/}"


def _query(person_name: str) -> str:
    import requests  # lazy: keeps offline paths import-free

    params = {
        "search_query": f'au:"{person_name}"',
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": _MAX_RESULTS,
    }
    resp = requests.get(_API_URL, params=params, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        logger.warning("arxiv: HTTP %d for %r", resp.status_code, person_name)
        return ""
    return resp.text


def _parse_feed(xml_text: str) -> tuple[int, list[dict]]:
    """Pure parse of an arXiv Atom feed into (total_results, [{title,
    submitted, arxiv_id}]). (0, []) on any parse failure or empty feed,
    never raises.
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        logger.warning("arxiv: could not parse Atom feed: %s", exc)
        return 0, []

    total_el = root.find(f"{_OPENSEARCH_NS}totalResults")
    total_text = (total_el.text or "").strip() if total_el is not None else ""
    total = int(total_text) if total_text.isdigit() else 0

    papers = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title_el = entry.find(f"{_ATOM_NS}title")
        published_el = entry.find(f"{_ATOM_NS}published")
        id_el = entry.find(f"{_ATOM_NS}id")
        title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
        published = (published_el.text or "")[:10] if published_el is not None else ""
        arxiv_id = (id_el.text or "").strip() if id_el is not None else ""
        if title:
            papers.append({"title": title, "submitted": published, "arxiv_id": arxiv_id})
    return total, papers


def verify_arxiv(person_name: str) -> list[dict]:
    """Papers-by-author lookup for one claimed researcher.

    Returns a single-record list summarizing up to _MAX_RESULTS most recent
    papers (title + submission date), or [] if the name is blank, the API
    call failed, or no papers were found. Never raises.

    match_confidence is "medium" at best (small result count, plausibly one
    author) or "low" (large result count, likely a namesake collision);
    never "high", since arXiv's author search cannot confirm identity (see
    module docstring).
    """
    person_name = (person_name or "").strip()
    if not person_name:
        return []

    try:
        xml_text = _query(person_name)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("arxiv: request failed for %r: %s", person_name, exc)
        return []
    if not xml_text:
        return []

    total, papers = _parse_feed(xml_text)
    if not papers:
        return []

    confidence = "low" if total > _MANY_RESULTS_THRESHOLD else "medium"

    lines = [f"{p['title']} ({p['submitted'] or 'date unknown'})" for p in papers]
    snippet = (
        f"arXiv lists {total} paper(s) matching author {person_name!r}, most recent first: "
        + "; ".join(lines)
        + ". arXiv is a preprint server, NOT peer-reviewed: this shows the papers exist and "
        "when they were submitted, never that the work has been vetted."
    )
    if confidence == "low":
        snippet += (
            f" {total} total matches is enough that this name likely spans multiple different "
            "people (arXiv's au: search is a text match, not an author-ID lookup)."
        )

    return [
        {
            "source_url": papers[0]["arxiv_id"] or "https://arxiv.org",
            "snippet": snippet,
            "source_name": _SOURCE_NAME,
            "weight": weight_for(_SOURCE_NAME),
            "match_confidence": confidence,
        }
    ]
