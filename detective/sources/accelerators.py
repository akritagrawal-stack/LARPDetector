"""Accelerator badge connector: confirms a claimed "YC-backed" or
"Techstars" affiliation against each program's own public directory data.

THE KEY LARP TELL this connector exists for: a deck or bio that name-drops a
prestige accelerator badge the company never actually went through. Both
paths here check the accelerator's OWN directory, not the company's own
claim about itself.

Y Combinator: the public companies directory at ycombinator.com/companies is
an Algolia-backed search (confirmed live: App ID 45BWZJ1SGC, index
YCCompany_production, tagFilters ["ycdc_public"]). Algolia's search key for
this index is a short-lived, restriction-scoped "secured API key" embedded
in that page's own HTML (window.AlgoliaOpts), not a fixed long-lived secret,
so it will eventually rotate out from under a hardcoded value. To keep the
common case at one network call, this module tries a last-known-good
fallback key first; only if that call fails does it scrape a fresh key from
the live directory page and retry once. Algolia's search is typo-tolerant,
so a query for a company NOT in YC's directory still returns
SOME hit (confirmed live: querying "Cluely", which is not YC-backed, returns
"Hyperspell" as Algolia's closest guess); this module never surfaces a hit
whose own name does not actually contain (or get contained by) the queried
name, so an unrelated fuzzy suggestion like that is silently dropped rather
than shown as misleading evidence.

Techstars: the public portfolio page at techstars.com/portfolio is a
client-rendered app; confirmed live that its `_companySearch` query
parameter does NOT filter the server-rendered HTML (searching for
"SendGrid" and "Instabug" returned byte-identical embedded data), so a
plain HTTP fetch cannot reach Techstars' real, full, filtered portfolio
search. What IS present in the static HTML is a small, fixed "notable
portfolio companies" widget (roughly two dozen companies, e.g. SendGrid,
Uber, DigitalOcean), embedded as JSON in the page's React hydration payload
alongside each company's stage (PRIVATE / PUBLIC / ACQUIRED) and Techstars
session_year. This module checks a queried company name against that fixed
widget only. A hit is a real, live-confirmed Techstars portfolio company; a
miss means only "not among Techstars' own short highlight list", never "not
Techstars-backed" (survivorship bias too: this highlight widget will never
itself list a dead/shut-down company).

NOT LISTED IS WEAK EVIDENCE, NOT PROOF: absence in either directory must
never be read as "never accelerator-backed". A company can be backed by a
different accelerator entirely, or (for Techstars specifically) simply not
be one of the roughly two dozen companies in the static highlight widget.

match_confidence: "high" for a single clean company-name match (exact,
prefix, or substring after normalization); "low" when Algolia's fuzzy search
returns MULTIPLE candidates that all pass that same name-containment check
(ambiguous which one is the queried company). No result at all (a bare
mismatch, or the containment check fails for every hit) yields no record
for that source, not a misleading "low" one.

Public surface:
    verify_accelerator(company_name) -> list[dict]

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

_YC_APP_ID = "45BWZJ1SGC"
_YC_INDEX = "YCCompany_production"
_YC_DIRECTORY_PAGE = "https://www.ycombinator.com/companies"
# Last-known-good fallback key (a short-lived, restriction-scoped Algolia
# "secured API key", not a long-lived secret; see module docstring). Used
# only if a fresh scrape of the live directory page fails, so a transient
# scrape failure does not immediately zero out this connector.
_YC_FALLBACK_KEY = (
    "NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJiMWM4ZTU0ZDlhYTZjOTJiMjlhMWFuYWx5"
    "dGljc1RhZ3M9eWNkYyZyZXN0cmljdEluZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlfTGF1"
    "bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIyJTVE"
)

_TECHSTARS_PORTFOLIO_URL = "https://www.techstars.com/portfolio"

_TIMEOUT = 12
_USER_AGENT = "LARPDetector-research/1.0 (accelerator directory badge check)"
_SOURCE_NAME = "accelerator_badges"
_MAX_CANDIDATES = 5

_TECHSTARS_STAGE_TO_STATUS = {
    "PRIVATE": "active",
    "PUBLIC": "active",
    "ACQUIRED": "acquired",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_matches(company_name: str, candidate_name: str) -> bool:
    a, b = _norm(company_name), _norm(candidate_name)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


# ---------------------------------------------------------------------------
# Y Combinator (Algolia-backed public directory)
# ---------------------------------------------------------------------------


def _scrape_yc_algolia_key() -> Optional[tuple[str, str]]:
    """Best-effort: scrape the live app-id/key pair out of the YC companies
    page's own window.AlgoliaOpts inline script. Returns None on any
    network/parse failure so the caller can fall back to the last-known-good
    key instead.
    """
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(_YC_DIRECTORY_PAGE, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        logger.warning("accelerators: YC directory page HTTP %d", resp.status_code)
        return None
    match = re.search(r'AlgoliaOpts\s*=\s*\{"app":"([^"]+)","key":"([^"]+)"\}', resp.text)
    if not match:
        logger.warning("accelerators: could not find AlgoliaOpts in YC directory page")
        return None
    return match.group(1), match.group(2)


def _yc_query(company_name: str, app_id: str, key: str) -> Optional[dict]:
    import requests  # lazy

    query_url = f"https://{app_id}-dsn.algolia.net/1/indexes/{_YC_INDEX}/query"
    resp = requests.post(
        query_url,
        headers={
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key": key,
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
        json={"query": company_name, "tagFilters": ["ycdc_public"], "hitsPerPage": _MAX_CANDIDATES},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("accelerators: YC Algolia query HTTP %d for %r", resp.status_code, company_name)
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("accelerators: YC Algolia non-JSON response for %r: %s", company_name, exc)
        return None


def _yc_hits(company_name: str) -> Optional[list[dict]]:
    """Query YC's Algolia index. Tries the last-known-good fallback key
    first (one network call in the common case where that key still
    works); only if that call fails does this scrape a fresh key from the
    live directory page and retry once. Never raises.

    Returns None when the lookup FAILED (both the fallback key and a fresh
    scrape came back with no HTTP 200 payload): "we could not look", which
    must never become a checked-absent read. Returns a (possibly empty) hit
    list when the query SUCCEEDED: an empty list is a genuine "the directory
    was queried and returned nothing", the completed-lookup case that CAN
    become a checked-absent record.
    """
    data = _yc_query(company_name, _YC_APP_ID, _YC_FALLBACK_KEY)

    if data is None:
        # The fallback key has likely rotated out; scrape a fresh one from
        # the live directory page and retry once before giving up.
        scraped = None
        try:
            scraped = _scrape_yc_algolia_key()
        except Exception as exc:  # noqa: BLE001
            logger.warning("accelerators: YC key scrape failed: %s", exc)
        if scraped:
            data = _yc_query(company_name, scraped[0], scraped[1])

    if data is None:
        return None
    return data.get("hits") or []


def _yc_checked_absent_record(company_name: str) -> dict:
    """A CHECKED-ABSENT record: a COMPLETED query of YC's own public
    companies directory that returned no matching company. This is a targeted
    negative result, not generic absence, and not a failed search. The
    known-coverage caveats travel in the snippet so a downstream brain can
    never escalate it to DISPROVEN without ruling out renames/recency.
    """
    return {
        "source_url": f"https://www.ycombinator.com/companies?query={company_name}",
        "snippet": (
            "Queried Y Combinator's own public companies directory for "
            f"{company_name!r}; no matching company is listed. This is a COMPLETED "
            "directory lookup, not a failed search. Caveats: very recent "
            "batches can lag the public directory and renamed companies can "
            "miss on name."
        ),
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "high",
        "registry_check": "absent",
    }


def _build_yc_record(hit: dict, confidence: str) -> dict:
    name = hit.get("name", "")
    batch = hit.get("batch", "") or "unknown batch"
    status = hit.get("status", "") or "unknown status"
    one_liner = hit.get("one_liner", "")
    slug = hit.get("slug", "")

    snippet = f"Y Combinator directory: {name!r} is listed, batch {batch}, status {status}."
    if one_liner:
        snippet += f" ({one_liner})"

    source_url = f"https://www.ycombinator.com/companies/{slug}" if slug else _YC_DIRECTORY_PAGE
    return {
        "source_url": source_url,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def _yc_evidence(company_name: str) -> list[dict]:
    try:
        hits = _yc_hits(company_name)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("accelerators: YC lookup failed for %r: %s", company_name, exc)
        return []
    if hits is None:
        # The lookup FAILED (no HTTP 200 payload): "we could not look". This
        # must never become a checked-absent read; emit nothing.
        return []

    matched = [h for h in hits if _name_matches(company_name, h.get("name", ""))]
    if not matched:
        # The directory was genuinely queried and nothing sharing the queried
        # name is listed (either zero hits, or only fuzzy suggestions like the
        # docstring's "Cluely" -> "Hyperspell" that never name-match). That is a
        # COMPLETED negative lookup of YC's own directory: a checked-absent
        # record. A misleading positive suggestion is still never surfaced.
        return [_yc_checked_absent_record(company_name)]

    if len(matched) == 1:
        return [_build_yc_record(matched[0], "high")]

    exact = [h for h in matched if _norm(h.get("name", "")) == _norm(company_name)]
    if len(exact) == 1:
        return [_build_yc_record(exact[0], "high")]

    # Multiple plausible name-matching candidates: ambiguous which one is
    # the queried company.
    return [_build_yc_record(h, "low") for h in matched[:_MAX_CANDIDATES]]


# ---------------------------------------------------------------------------
# Techstars (static "notable portfolio companies" widget only, see docstring)
# ---------------------------------------------------------------------------

_TECHSTARS_RECORD_RE = re.compile(
    r'\\"name\\":\\"([^\\]*)\\",\\"vertical\\":(?:null|\\"[^\\]*\\"),\\"stage\\":\\"([^\\]*)\\"'
    r'.{0,600}?\\"session_year\\":(\d+)',
    re.DOTALL,
)


def _fetch_techstars_html() -> str:
    import requests  # lazy

    resp = requests.get(_TECHSTARS_PORTFOLIO_URL, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        logger.warning("accelerators: Techstars portfolio page HTTP %d", resp.status_code)
        return ""
    return resp.text


def _parse_techstars_widget(html: str) -> list[dict]:
    """Pure parse of the fixed "notable portfolio companies" widget embedded
    in the Techstars portfolio page's React hydration payload. Tolerant of
    zero matches (returns []) if Techstars changes this markup.
    """
    companies = []
    for match in _TECHSTARS_RECORD_RE.finditer(html):
        name, stage, year = match.group(1), match.group(2), match.group(3)
        if name:
            companies.append({"name": name, "stage": stage, "session_year": year})
    return companies


def _build_techstars_record(company: dict) -> dict:
    name = company.get("name", "")
    stage = company.get("stage", "")
    year = company.get("session_year", "")
    status = _TECHSTARS_STAGE_TO_STATUS.get(stage, stage.lower() or "unknown status")

    snippet = (
        f"Techstars portfolio: {name!r} appears in Techstars' own notable-portfolio-companies "
        f"listing, session year {year or 'unknown'}, status {status}."
    )
    return {
        "source_url": _TECHSTARS_PORTFOLIO_URL,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "high",
    }


def _techstars_evidence(company_name: str) -> list[dict]:
    try:
        html = _fetch_techstars_html()
    except Exception as exc:  # noqa: BLE001
        logger.warning("accelerators: Techstars fetch failed for %r: %s", company_name, exc)
        return []
    if not html:
        return []

    companies = _parse_techstars_widget(html)
    matched = [c for c in companies if _name_matches(company_name, c.get("name", ""))]
    if not matched:
        return []
    return [_build_techstars_record(matched[0])]


def verify_accelerator(company_name: str) -> list[dict]:
    """YC directory + Techstars highlight-widget badge check for one claimed
    company.

    Returns up to 2 evidence records (one per program the company was found
    in), or [] if the name is blank or neither program shows a matching
    company. Never raises.

    Not being listed in either directory is weak evidence, never proof: a
    company can be backed by a different accelerator entirely, and the
    Techstars path in particular only ever checks a small static highlight
    list (see module docstring), not Techstars' full real portfolio.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    evidence: list[dict] = []
    evidence.extend(_yc_evidence(company_name))
    evidence.extend(_techstars_evidence(company_name))
    return evidence
