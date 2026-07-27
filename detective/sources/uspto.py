"""USPTO connector: granted patents vs pending (unexamined) applications.

THE KEY LARP TELL this connector exists for: a founder claiming "patented
technology" when all that actually exists is a filed, PENDING application.
Filing an application costs a few hundred dollars and requires nothing be
examined or allowed; a GRANT means the USPTO actually examined and allowed
claims. This module always buckets its findings into "granted" vs "pending
application" and never blurs the two into one undifferentiated "patent"
count.

Primary path: the USPTO Open Data Portal (ODP) Patent File Wrapper Search
API, https://api.uspto.gov/api/v1/patent/applications/search, header
X-API-KEY. As of March 2026 this replaced the old PatentsView
search.patentsview.org API (confirmed live: search.patentsview.org no
longer resolves in DNS; api.patentsview.org now redirects to an HTML
portal page instead of returning JSON). ODP requires a free but
identity-verified USPTO.gov account, so USPTO_API_KEY is commonly unset.

Fallback path (no key, or the ODP call fails): Google Patents' public,
unauthenticated XHR search endpoint (patents.google.com/xhr/query). This
endpoint's "assignee:" / "inventor:" query operators are NOT a strict field
filter, they are a fuzzy relevance search (confirmed live: an
assignee:"Nvidia Corporation" query surfaces unrelated assignees like "Sas
Institute Inc." and "Prince Sultan University" mixed in with real Nvidia
hits). This module therefore never trusts the search ranking alone: it
independently checks each hit's own assignee/inventor field against the
queried name (see _name_field_matches) before counting it as a real match.
grant-vs-pending is read directly off Google Patents' own grant_date field
(present = granted, absent = a published, not-yet-granted application),
which is simpler and more reliable than parsing the US kind-code suffix.

match_confidence: "high" only when at least one returned record's own
assignee (is_company=True) or inventor (is_company=False) field actually
contains the queried name, "low" for a bare-name query that produced no
field-level match (the caller must not treat "low" as identity-confirming;
a common person name in particular can span many real inventors with no
way for this connector to tell them apart from name text alone).

Public surface:
    verify_uspto(name, is_company=False) -> list[dict]

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

_ODP_SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
_GOOGLE_PATENTS_URL = "https://patents.google.com/xhr/query"
_TIMEOUT = 12
_USER_AGENT = "LARPDetector-research/1.0 (USPTO grant-vs-application check)"
_SOURCE_NAME = "uspto_patents_trademarks"
_MAX_RESULTS_TO_SCAN = 20
_MAX_EXAMPLES_PER_BUCKET = 3


def _odp_headers() -> dict:
    key = os.environ.get("USPTO_API_KEY", "").strip()
    return {"X-API-KEY": key, "Accept": "application/json"}


def _odp_search(name: str, is_company: bool) -> list[dict]:
    """One ODP Patent File Wrapper search. Returns the raw
    patentFileWrapperDataBag list, or [] on any non-200 or malformed
    response. Only called when USPTO_API_KEY is set (see verify_uspto).
    """
    import requests  # lazy: keeps offline paths import-free

    field = (
        "applicationMetaData.applicantBag.applicantNameText"
        if is_company
        else "applicationMetaData.inventorBag.inventorNameText"
    )
    resp = requests.get(
        _ODP_SEARCH_URL,
        params={"q": f'{field}:"{name}"'},
        headers=_odp_headers(),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("uspto: ODP search HTTP %d for %r", resp.status_code, name)
        return []
    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("uspto: ODP non-JSON response for %r: %s", name, exc)
        return []
    return data.get("patentFileWrapperDataBag", []) or []


def _odp_item_to_record(item: dict) -> Optional[dict]:
    """Pure parse: one ODP patentFileWrapperDataBag item into this module's
    flat record shape. Tolerant of missing sub-fields (schema drift on
    USPTO's side degrades to "" rather than raising).
    """
    meta = item.get("applicationMetaData", {}) or {}
    status = (meta.get("applicationStatusDescriptionText") or "").strip()
    granted = status.lower() == "patented case"
    inventors = [
        (inv.get("inventorNameText") or "").strip()
        for inv in (meta.get("inventorBag") or [])
        if (inv.get("inventorNameText") or "").strip()
    ]
    applicants = [
        (app.get("applicantNameText") or "").strip()
        for app in (meta.get("applicantBag") or [])
        if (app.get("applicantNameText") or "").strip()
    ]
    return {
        "number": item.get("applicationNumberText", "") or meta.get("patentNumber", ""),
        "title": meta.get("inventionTitle", ""),
        "granted": granted,
        "status": status,
        "filing_date": meta.get("filingDate", ""),
        "grant_date": meta.get("grantDate", "") if granted else "",
        "inventors": inventors,
        "applicants": applicants,
    }


def _google_patents_search(name: str, is_company: bool) -> list[dict]:
    """One Google Patents XHR search. Returns the raw list of "patent" dicts
    from the first result cluster, or [] on any non-200, malformed, or
    query-error response.
    """
    import requests  # lazy

    field = "assignee" if is_company else "inventor"
    resp = requests.get(
        _GOOGLE_PATENTS_URL,
        params={"url": f'q={field}:"{name}" country:US', "exp": ""},
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("uspto: Google Patents HTTP %d for %r", resp.status_code, name)
        return []
    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("uspto: Google Patents non-JSON response for %r: %s", name, exc)
        return []
    results = data.get("results") or {}
    if results.get("user_error"):
        logger.warning(
            "uspto: Google Patents query error for %r: %s", name, results.get("user_error")
        )
        return []
    cluster = results.get("cluster") or []
    items = cluster[0].get("result", []) if cluster else []
    return [it.get("patent") or {} for it in items[:_MAX_RESULTS_TO_SCAN]]


def _google_item_to_record(patent: dict) -> dict:
    grant_date = (patent.get("grant_date") or "").strip()
    inventor = (patent.get("inventor") or "").strip()
    assignee = (patent.get("assignee") or "").strip()
    return {
        "number": patent.get("publication_number", ""),
        "title": (patent.get("title") or "").strip(),
        "granted": bool(grant_date),
        "status": "granted" if grant_date else "pending / published application",
        "filing_date": (patent.get("filing_date") or "").strip(),
        "grant_date": grant_date,
        "inventors": [inventor] if inventor else [],
        "applicants": [assignee] if assignee else [],
    }


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_field_matches(field_values: list[str], name: str) -> bool:
    """True when at least one of this record's own assignee/inventor field
    values contains (or is contained by) the normalized queried name. This
    is the only thing that raises confidence out of "low"; see the module
    docstring for why the search results themselves cannot be trusted at
    face value.
    """
    target = _norm(name)
    if not target:
        return False
    for value in field_values:
        v = _norm(value)
        if v and (target in v or v in target):
            return True
    return False


def _build_record(name: str, items: list[dict], bucket_label: str, confidence: str) -> dict:
    examples = items[:_MAX_EXAMPLES_PER_BUCKET]
    lines = []
    for r in examples:
        who = ", ".join(r.get("applicants") or r.get("inventors") or []) or "unnamed assignee/inventor"
        if r["granted"] and r.get("grant_date"):
            date_bit = f"granted {r['grant_date']}"
        else:
            date_bit = f"filed {r.get('filing_date') or 'unknown date'}"
        lines.append(f"{r.get('number') or 'no number on record'} ({date_bit}, {who})")

    snippet = (
        f"USPTO records for {name!r}: {len(items)} {bucket_label} patent record(s) found. "
        + "; ".join(lines)
        + "."
    )
    if bucket_label == "pending application":
        snippet += (
            " Pending means examined/allowed status has NOT been reached: a claim of "
            "\"patented technology\" resting only on a pending application, not a grant, "
            "is exactly the LARP tell this connector exists to catch."
        )

    return {
        "source_url": "https://patents.google.com/?q=" + name.replace(" ", "+"),
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def verify_uspto(name: str, is_company: bool = False) -> list[dict]:
    """Granted-vs-pending patent check for one company (is_company=True,
    searched by assignee/applicant name) or person (is_company=False,
    searched by inventor name).

    Returns up to 2 evidence records (one "granted" summary, one "pending
    application" summary; either or both may be absent), or [] if nothing
    was found or every network path failed. Never raises.

    match_confidence is "high" only when a returned record's own
    assignee/inventor field actually matches the queried name, else "low".
    Never assume a "low" record identifies the claimed person/company; see
    the module docstring.
    """
    name = (name or "").strip()
    if not name:
        return []

    api_key = os.environ.get("USPTO_API_KEY", "").strip()
    records: list[dict] = []
    if api_key:
        try:
            raw_items = _odp_search(name, is_company)
            records = [r for r in (_odp_item_to_record(item) for item in raw_items) if r]
        except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
            logger.warning("uspto: ODP search failed for %r: %s", name, exc)
            records = []
        if not records:
            # The ODP path is a keyed API this module cannot fully exercise
            # without a verified USPTO.gov account (field names are best-
            # effort per USPTO's docs); fall through to the free Google
            # Patents path rather than surfacing a silent [] just because
            # the keyed call came back empty (a real miss looks identical
            # to a schema mismatch otherwise).
            api_key = ""

    if not api_key:
        try:
            raw_patents = _google_patents_search(name, is_company)
            records = [_google_item_to_record(p) for p in raw_patents]
        except Exception as exc:  # noqa: BLE001
            logger.warning("uspto: Google Patents fallback failed for %r: %s", name, exc)
            return []

    if not records:
        return []

    field_key = "applicants" if is_company else "inventors"
    matched_records = [r for r in records if _name_field_matches(r.get(field_key, []), name)]
    confidence = "high" if matched_records else "low"
    pool = matched_records or records

    granted = [r for r in pool if r["granted"]]
    pending = [r for r in pool if not r["granted"]]

    evidence = []
    if granted:
        evidence.append(_build_record(name, granted, "granted", confidence))
    if pending:
        evidence.append(_build_record(name, pending, "pending application", confidence))
    return evidence
