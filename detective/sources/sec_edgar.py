"""SEC EDGAR connector: Form D filings (the exempt private-offering form
most funded startups actually file, if they file anything).

Free (efts.sec.gov full-text search + data.sec.gov / www.sec.gov Archives
for the filing itself). SEC's fair-access policy requires a descriptive
User-Agent identifying the requester; set SEC_EDGAR_CONTACT to embed your own
contact string, otherwise a generic placeholder is sent. Rate-limited to
roughly 10 requests/second; this module never issues more than two requests
per verify_sec call (one search, one document fetch), throttled between them.

ABSENCE IS WEAK EVIDENCE, not proof of nothing: most exempt private raises
are unfiled, filed late, or filed under a slightly different legal entity
name than the pitch-deck company name. A missing Form D must never be read
as "the raise did not happen."

Public surface:
    verify_sec(company_name, claimed_amount=None) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_FULL_TEXT_SEARCH = "https://efts.sec.gov/LATEST/search-index"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
_TIMEOUT = 10
_THROTTLE_SECS = 0.2  # comfortably under the ~10 req/s SEC fair-access limit
_SOURCE_NAME = "sec_edgar_form_d"
# SEC's fair-access bot detection requires a User-Agent that actually
# contains a contactable email address (confirmed live: a UA without an "@"
# email gets a flat HTTP 403 on www.sec.gov/Archives, even though the
# efts.sec.gov search endpoint tolerates it). SEC_EDGAR_CONTACT overrides
# this with a real address; the placeholder below uses the .example TLD
# (RFC 2606, reserved for documentation, never a live mailbox) purely to
# satisfy that "@"-shaped requirement out of the box.
_DEFAULT_UA = "LARPDetector-research/1.0 (contact: research@larpdetector.example, set SEC_EDGAR_CONTACT)"


def _user_agent() -> str:
    contact = os.environ.get("SEC_EDGAR_CONTACT", "").strip()
    if contact:
        return f"LARPDetector-research/1.0 ({contact})"
    return _DEFAULT_UA


def _headers() -> dict:
    return {"User-Agent": _user_agent(), "Accept": "application/json"}


def _search_form_d(company_name: str) -> dict:
    import requests  # lazy: keeps offline paths import-free

    params = {"q": f'"{company_name}"', "forms": "D"}
    resp = requests.get(_FULL_TEXT_SEARCH, params=params, headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code != 200:
        logger.warning(
            "sec_edgar: full text search HTTP %d for %r", resp.status_code, company_name
        )
        return {}
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("sec_edgar: non-JSON search response for %r: %s", company_name, exc)
        return {}


def _fetch_xml(doc_url: str) -> Optional[str]:
    import requests  # lazy

    resp = requests.get(doc_url, headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code != 200:
        logger.warning("sec_edgar: doc fetch HTTP %d for %r", resp.status_code, doc_url)
        return None
    return resp.text


def _top_hit(search_data: dict) -> Optional[dict]:
    hits = ((search_data or {}).get("hits") or {}).get("hits") or []
    return hits[0] if hits else None


def _hit_doc_url(hit: dict) -> Optional[str]:
    """Build the primary_doc.xml Archives URL from one full-text-search hit.

    hit["_id"] is "{accession-with-dashes}:{filename}"; hit["_source"]["ciks"]
    carries the filer's CIK(s). Returns None if either piece is missing (a
    schema change on SEC's side), so the caller falls back to search-only
    evidence rather than crashing.
    """
    source = hit.get("_source", {}) or {}
    ciks = source.get("ciks") or []
    hit_id = hit.get("_id", "")
    if not ciks or ":" not in hit_id:
        return None
    accession, filename = hit_id.split(":", 1)
    cik = ciks[0].lstrip("0") or "0"
    accession_nodash = accession.replace("-", "")
    return f"{_ARCHIVES_BASE}/{cik}/{accession_nodash}/{filename}"


def _local_tag(elem) -> str:
    tag = elem.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def _find_element(root, tag_name: str):
    """First element anywhere in the tree with this local tag name,
    tolerant of any XML namespace. None if not present.
    """
    for elem in root.iter():
        if _local_tag(elem) == tag_name:
            return elem
    return None


def _element_or_child_text(elem) -> str:
    """Text of `elem` itself, or of its first child that has text.

    Form D's XML nests some values one level down (e.g. dateOfFirstSale's
    actual date sits on a <value> child), and this is deliberately tolerant
    of either shape rather than hardcoding one exact nesting.
    """
    if elem is None:
        return ""
    if (elem.text or "").strip():
        return elem.text.strip()
    for child in elem:
        if (child.text or "").strip():
            return child.text.strip()
    return ""


def _parse_related_persons(root) -> list[str]:
    """Named executives/directors/promoters off a Form D's relatedPersonsList.

    Some related "persons" are actually entities (a fund's manager, an LLC),
    which the schema represents by leaving firstName as the literal string
    "N/A" and putting the entity's full name in lastName; that "N/A" is
    dropped here so the snippet reads "MyAsiaVC, LLC" rather than the
    confusing "N/A MyAsiaVC, LLC".
    """
    names = []
    for elem in root.iter():
        if _local_tag(elem) != "relatedPersonInfo":
            continue
        first = ""
        last = ""
        for child in elem.iter():
            if _local_tag(child) == "firstName":
                first = (child.text or "").strip()
            elif _local_tag(child) == "lastName":
                last = (child.text or "").strip()
        if first.strip().upper() == "N/A":
            first = ""
        full = f"{first} {last}".strip()
        if full:
            names.append(full)
    return names


def parse_form_d_xml(xml_text: str) -> dict:
    """Pure parse of a Form D primary_doc.xml into a flat dict.

    Tolerant of missing fields or an unparsable document (returns ""/[]
    rather than raising) so a schema variation degrades gracefully instead
    of losing the whole record.
    """
    result = {
        "entity_name": "",
        "total_offering_amount": "",
        "total_amount_sold": "",
        "date_of_first_sale": "",
        "related_persons": [],
    }
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        logger.warning("sec_edgar: could not parse Form D XML: %s", exc)
        return result

    result["entity_name"] = _element_or_child_text(_find_element(root, "entityName"))
    result["total_offering_amount"] = _element_or_child_text(
        _find_element(root, "totalOfferingAmount")
    )
    result["total_amount_sold"] = _element_or_child_text(
        _find_element(root, "totalAmountSold")
    )
    result["date_of_first_sale"] = _element_or_child_text(
        _find_element(root, "dateOfFirstSale")
    )
    result["related_persons"] = _parse_related_persons(root)
    return result


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_matches(company_name: str, entity_name: str) -> bool:
    """True only when one normalized name is a PREFIX of the other (e.g.
    "Example Corp" vs "Example Corp Inc"), never a bare substring match.

    A bare "contains" check is too loose: it flags "OpenAI" as matching
    "MAV OpenAI Fund I, a series of MAV Alternate Investments, LP", an
    unrelated investment vehicle that merely mentions OpenAI in its own
    name (confirmed live, see the sources.sec_edgar report). That is
    exactly the cross-entity mismatch pitchbook.py's _mentions_company
    guard exists to prevent for PitchBook; startswith keeps the common real
    case (a legal-suffix difference, "Example Corp" vs "Example Corp Inc.")
    while rejecting an unrelated entity that happens to embed the queried
    name as a substring.
    """
    a, b = _norm(company_name), _norm(entity_name)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _build_snippet(
    entity_name: str, file_date: str, parsed: dict, claimed_amount: Optional[str]
) -> str:
    parts = [f"SEC Form D on file for {entity_name} (filed {file_date or 'date unknown'})."]
    amount = parsed.get("total_offering_amount", "")
    sold = parsed.get("total_amount_sold", "")
    first_sale = parsed.get("date_of_first_sale", "")
    execs = parsed.get("related_persons", [])
    if amount:
        parts.append(f"Total offering amount: ${amount}.")
    if sold:
        parts.append(f"Total amount sold: ${sold}.")
    if first_sale:
        parts.append(f"Date of first sale: {first_sale}.")
    if execs:
        parts.append("Named executives/directors: " + ", ".join(execs[:5]) + ".")
    if claimed_amount:
        parts.append(f"(Claim under review: {claimed_amount}.)")
    return " ".join(parts)


def verify_sec(company_name: str, claimed_amount: Optional[str] = None) -> list[dict]:
    """Look up a Form D filing for a company by name.

    Returns a single-record list ({"source_url", "snippet", "source_name",
    "weight", "match_confidence"}) if a filing was found, or [] if no
    filing was found or any network/parse step failed. Never raises.

    match_confidence is "high" when the filing's own entity name matches the
    queried company name, else "medium" (a plausible full-text hit whose
    exact entity name could not be confirmed to match). This is never
    "low": full-text search only returns filings that literally mention the
    quoted company name, so a wrong-company false positive is unlikely but
    not zero, hence "medium" rather than "high" for an unconfirmed name.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    try:
        search_data = _search_form_d(company_name)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("sec_edgar: search failed for %r: %s", company_name, exc)
        return []

    hit = _top_hit(search_data)
    if hit is None:
        logger.info(
            "sec_edgar: no Form D filing found for %r (weak evidence only; many "
            "exempt raises are unfiled or filed under a different legal name)",
            company_name,
        )
        return []

    source = hit.get("_source", {}) or {}
    display_names = source.get("display_names") or []
    file_date = source.get("file_date", "")
    doc_url = _hit_doc_url(hit)

    parsed: dict = {}
    if doc_url:
        time.sleep(_THROTTLE_SECS)
        try:
            xml_text = _fetch_xml(doc_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sec_edgar: doc fetch failed for %r: %s", doc_url, exc)
            xml_text = None
        if xml_text:
            parsed = parse_form_d_xml(xml_text)

    entity_name = parsed.get("entity_name") or (display_names[0] if display_names else company_name)
    confidence = "high" if _name_matches(company_name, entity_name) else "medium"

    return [
        {
            "source_url": doc_url or "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
            "snippet": _build_snippet(entity_name, file_date, parsed, claimed_amount),
            "source_name": _SOURCE_NAME,
            "weight": weight_for(_SOURCE_NAME),
            "match_confidence": confidence,
        }
    ]
