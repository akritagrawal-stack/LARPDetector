"""OpenAlex connector: works count, citation count, top affiliations, and
publication-year span for a claimed researcher. Used instead of Google
Scholar (no stable free API).

Free JSON API (api.openalex.org), no key. A "mailto" query param is sent on
every request to join OpenAlex's polite pool (faster, more reliable
service); override the contact address with OPENALEX_MAILTO, otherwise a
placeholder .example address is sent (see sec_edgar.py for the same
pattern).

IDENTITY RESOLUTION: OpenAlex's author search is a text index, not an
author-ID lookup, so a common name can return several unrelated authors
under one umbrella "display_name" (confirmed live: searching "Geoffrey
Hinton" returns 16 candidate author records). This module only raises
match_confidence out of "low" when a concrete disambiguator lines up:
    - "high"   : the caller-supplied `institution` string matches one of
                 THIS candidate's own recorded affiliations.
    - "medium" : no institution was supplied, but the candidate carries an
                 ORCID (a real third-party-verified personal identifier,
                 meaning OpenAlex itself resolved this to one specific
                 person rather than a merged namesake cluster) AND the
                 search returned only a small number of total candidates.
    - "low"    : anything else, including any bare-name search with no
                 institution and no ORCID. Never treat a "low" record as
                 confirming this is the claimed person.

Public surface:
    verify_openalex(person_name, institution=None) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_API_URL = "https://api.openalex.org/authors"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (OpenAlex author lookup)"
_SOURCE_NAME = "openalex"
_MAX_CANDIDATES = 2
_DEFAULT_MAILTO = "research@larpdetector.example"


def _mailto() -> str:
    return os.environ.get("OPENALEX_MAILTO", "").strip() or _DEFAULT_MAILTO


def _search_authors(person_name: str) -> dict:
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(
        _API_URL,
        params={"search": person_name, "mailto": _mailto(), "per_page": _MAX_CANDIDATES},
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("openalex: HTTP %d for %r", resp.status_code, person_name)
        return {}
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("openalex: non-JSON response for %r: %s", person_name, exc)
        return {}


def _top_institution_names(author: dict, limit: int = 3) -> list[str]:
    names = []
    for aff in (author.get("affiliations") or [])[:limit]:
        inst_name = (aff.get("institution") or {}).get("display_name")
        if inst_name:
            names.append(inst_name)
    return names


def _pub_year_range(author: dict) -> tuple[str, str]:
    years = [
        c.get("year")
        for c in (author.get("counts_by_year") or [])
        if c.get("works_count")
    ]
    if not years:
        return "", ""
    return str(min(years)), str(max(years))


def _institution_matches(author: dict, institution: str) -> bool:
    target = (institution or "").strip().lower()
    if not target:
        return False
    for name in _top_institution_names(author, limit=10):
        name_l = name.lower()
        if target in name_l or name_l in target:
            return True
    return False


def _build_record(
    author: dict, person_name: str, institution: Optional[str], total_candidates: int
) -> dict:
    works = author.get("works_count", 0) or 0
    cites = author.get("cited_by_count", 0) or 0
    top_insts = _top_institution_names(author)
    first_year, last_year = _pub_year_range(author)
    has_orcid = bool((author.get("ids") or {}).get("orcid"))

    matched_institution = _institution_matches(author, institution or "")
    if matched_institution:
        confidence = "high"
    elif not institution and has_orcid and total_candidates <= _MAX_CANDIDATES:
        confidence = "medium"
    else:
        confidence = "low"

    parts = [
        f"OpenAlex author {author.get('display_name', person_name)!r}: {works} work(s), "
        f"{cites} citation(s)."
    ]
    if top_insts:
        parts.append("Top affiliation(s): " + ", ".join(top_insts) + ".")
    if first_year:
        parts.append(f"Active {first_year} to {last_year}.")
    if institution and not matched_institution:
        parts.append(
            f"(Claimed institution {institution!r} not found among this author's recorded affiliations.)"
        )
    if confidence == "low":
        parts.append(
            "Common-name collision risk: no institution/ORCID disambiguator confirmed this is "
            "the same person."
        )

    source_url = (author.get("ids") or {}).get("openalex") or author.get("id") or "https://openalex.org"
    return {
        "source_url": source_url,
        "snippet": " ".join(parts),
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def verify_openalex(person_name: str, institution: Optional[str] = None) -> list[dict]:
    """Works/citations/affiliation lookup for one claimed researcher.

    Returns up to _MAX_CANDIDATES evidence records (one per plausible author
    match), or [] if the name is blank, the API call failed, or no author
    was found. Never raises.

    match_confidence policy: see module docstring. Passing `institution`
    (e.g. the person's claimed current company/university) is the strongest
    way to raise confidence out of "low"; without it, only an ORCID-bearing
    candidate from a small result set reaches "medium".
    """
    person_name = (person_name or "").strip()
    if not person_name:
        return []

    try:
        data = _search_authors(person_name)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("openalex: search failed for %r: %s", person_name, exc)
        return []

    results = data.get("results") or []
    if not results:
        return []

    total_candidates = (data.get("meta") or {}).get("count", len(results))

    return [
        _build_record(author, person_name, institution, total_candidates)
        for author in results[:_MAX_CANDIDATES]
    ]
