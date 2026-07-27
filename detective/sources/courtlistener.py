"""CourtListener connector: fraud suits, SEC/FTC actions, IP/patent disputes,
breach-of-contract, and bankruptcy records for a claimed person or company.

Free REST API (https://www.courtlistener.com/api/rest/v4/search/), JSON, no
key required for basic use; set COURTLISTENER_TOKEN for a higher rate limit
(sent as "Authorization: Token <token>", CourtListener's documented DRF
token-auth header format). No token is ever logged.

HIGHEST SAME-NAME FALSE-POSITIVE RISK OF ANY SOURCE IN THIS REGISTRY (read
before using this evidence): "John Smith" or "Acme Inc" litigation is
extremely common and a full-text hit on a bare name proves only that SOME
party with that name was in SOME case, not that it was the claimed person or
company. Every evidence record from this module therefore defaults
match_confidence to "low" and raises it to "medium" ONLY when a corroborating
identifier is present that this connector can actually check from a
name-only search: for a company query (is_company=True), the matched case
caption itself carries a legal-entity suffix (Inc, LLC, Corp, Corporation,
Ltd, Co, LP, LLP, PLC, Company) consistent with a real company being a named
party, not just a bare name coincidence. This is NEVER raised to "high" on a
bare name match, full stop; a filed docket is a real record, but WHO exactly
it is filed against is not something a name-only search can confirm on its
own.

Jurisdiction-fit and counsel-match corroboration (mentioned as possible
disambiguators) are NOT checked here: this connector's signature is
name-only, with no caller-supplied company jurisdiction/counsel context to
compare against. That corroboration is the reasoning provider's job: treat
every "low" record here as a maybe, and even a "medium" record as needing
independent confirmation before it is treated as a real adverse hit against
the claimed subject, never as settled fact from this connector alone.

Searches both RECAP dockets (type=r, the PACER-sourced federal case records
most fraud/SEC/FTC/bankruptcy suits actually live in) and case-law opinions
(type=o, appellate/published decisions), at most one request per type, so
this module never issues more than two requests per verify_courtlistener
call.

Public surface:
    verify_courtlistener(name, is_company=False) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
_BASE_URL = "https://www.courtlistener.com"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (CourtListener adverse-record check)"
_SOURCE_NAME = "courtlistener"
_MAX_RESULTS_PER_TYPE = 3
_MAX_TOTAL_RESULTS = 5

_COMPANY_SUFFIX_TOKENS = (
    "inc", "incorporated", "llc", "corp", "corporation", "ltd", "co",
    "lp", "llp", "plc", "company",
)


def _headers() -> dict:
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Token {token}"
    return headers


def _search(name: str, result_type: str) -> list[dict]:
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(
        _SEARCH_URL,
        params={"q": f'"{name}"', "type": result_type},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning(
            "courtlistener: search HTTP %d for %r (type=%s)", resp.status_code, name, result_type
        )
        return []
    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("courtlistener: non-JSON search response for %r: %s", name, exc)
        return []
    return (data.get("results") or [])[:_MAX_RESULTS_PER_TYPE]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower())


def _looks_like_company_entity(case_name: str) -> bool:
    """True when the matched case's own caption carries a legal-entity
    suffix (Inc, LLC, Corp, ...), i.e. a real company name shape, not just a
    bare personal name that happens to share words with the query. This is
    the ONE corroborating identifier this name-only connector can check for
    a company query; see module docstring for why jurisdiction/counsel are
    out of scope here.
    """
    tokens = set(_norm(case_name).split())
    return any(suffix in tokens for suffix in _COMPANY_SUFFIX_TOKENS)


def _case_url(hit: dict) -> str:
    """Confirmed live against a real v4 search response: an opinion hit
    (type=o) carries its own "absolute_url", but a docket hit (type=r)
    carries that same page under "docket_absolute_url" instead (its
    "absolute_url" key, when present at all, is not the docket page).
    Falls back to a bare /docket/{id}/ URL if neither field is present.
    """
    absolute_url = hit.get("absolute_url") or hit.get("docket_absolute_url") or ""
    if absolute_url:
        return f"{_BASE_URL}{absolute_url}"
    docket_id = hit.get("docket_id")
    if docket_id:
        return f"{_BASE_URL}/docket/{docket_id}/"
    return _BASE_URL


def _case_summary(hit: dict, kind: str) -> str:
    case_name = hit.get("caseName") or hit.get("case_name") or "unknown caption"
    court = hit.get("court") or hit.get("court_id") or "unknown court"
    date_filed = hit.get("dateFiled") or hit.get("date_filed") or "date unknown"
    docket_number = hit.get("docketNumber") or hit.get("docket_number") or ""
    docket_str = f", docket {docket_number}" if docket_number else ""
    return f"[{kind}] {case_name} ({court}, filed {date_filed}{docket_str})"


def _build_record(name: str, is_company: bool, hit: dict, kind: str) -> dict:
    case_name = hit.get("caseName") or hit.get("case_name") or ""
    confidence = "low"
    if is_company and _looks_like_company_entity(case_name):
        confidence = "medium"

    snippet = (
        f"CourtListener {kind} record possibly involving {name!r}: {_case_summary(hit, kind)}. "
        "SAME-NAME MATCHES ARE THE HIGHEST FALSE-POSITIVE RISK OF ANY SOURCE HERE: this is "
        "not confirmation the claimed "
        + ("company" if is_company else "person")
        + " was actually a party in this case unless corroborated independently "
        "(matching entity type, a jurisdiction that fits, or counsel); treat this as a lead "
        "to verify, never as a settled adverse finding on its own."
    )

    return {
        "source_url": _case_url(hit),
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def verify_courtlistener(name: str, is_company: bool = False) -> list[dict]:
    """Search CourtListener's RECAP dockets and case-law opinions for a
    claimed person or company name.

    Returns up to _MAX_TOTAL_RESULTS evidence records (one per matched
    case), or [] if the name is blank or both searches found nothing / every
    network path failed. Never raises.

    match_confidence is "low" for every record by default (a bare name
    match on litigation is the weakest, highest-false-positive-risk evidence
    in this registry) and is raised to "medium" ONLY when is_company=True
    AND the matched case's own caption carries a legal-entity suffix
    consistent with a real company party. NEVER "high": a name-only search
    can never confirm identity on its own, see module docstring.
    """
    name = (name or "").strip()
    if not name:
        return []

    docket_hits: list[dict] = []
    try:
        docket_hits = _search(name, "r")
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("courtlistener: docket search failed for %r: %s", name, exc)

    opinion_hits: list[dict] = []
    try:
        opinion_hits = _search(name, "o")
    except Exception as exc:  # noqa: BLE001
        logger.warning("courtlistener: opinion search failed for %r: %s", name, exc)

    evidence: list[dict] = []
    for hit in docket_hits:
        evidence.append(_build_record(name, is_company, hit, "docket"))
    for hit in opinion_hits:
        evidence.append(_build_record(name, is_company, hit, "opinion"))

    return evidence[:_MAX_TOTAL_RESULTS]
