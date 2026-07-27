"""Domain-age connector: RDAP first (rdap.org bootstrap, redirects to the
authoritative registry), raw-socket WHOIS (port 43) fallback if RDAP has no
usable registration event.

Free, no auth. RDAP is the modern, structured-JSON replacement for WHOIS;
some registries still only answer WHOIS, hence the fallback.

NOTE ON WHAT THIS PROVES: creation date is reliable (registries do not
backdate registrations), but registrant identity is usually privacy-masked,
so this module never asserts WHO registered the domain, only WHEN. A FRESH
registration is the stronger LARP signal (a claimed decade-old company with
a month-old domain is a real flag); an OLD domain is weaker evidence on its
own, since an aged domain can simply have been bought/transferred.

Public surface:
    verify_domain_age(domain) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

match_confidence is always "high": this connector is bound directly to the
exact domain being checked, no name-resolution ambiguity.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import re
import socket
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_RDAP_URL_TEMPLATE = "https://rdap.org/domain/{domain}"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (RDAP domain-age lookup)"
_SOURCE_NAME = "domain_rdap_whois"

_WHOIS_IANA_SERVER = "whois.iana.org"
_WHOIS_FALLBACK_SERVER = "whois.verisign-grs.com"
_WHOIS_CREATED_RE = re.compile(
    r"(?:Creation Date|created(?:\s+on)?|Registered on)\s*:\s*(.+)", re.IGNORECASE
)


def _normalize_domain(raw: str) -> str:
    domain = (raw or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/", 1)[0]
    domain = domain.split(":", 1)[0]  # drop a stray port
    if domain.startswith("www."):
        domain = domain[len("www."):]
    return domain


def _rdap_lookup(domain: str) -> Optional[dict]:
    import requests  # lazy: keeps offline paths import-free

    try:
        resp = requests.get(
            _RDAP_URL_TEMPLATE.format(domain=domain),
            headers={"Accept": "application/rdap+json", "User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("domain_age: RDAP request error for %r: %s", domain, exc)
        return None
    if resp.status_code != 200:
        logger.warning("domain_age: RDAP HTTP %d for %r", resp.status_code, domain)
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("domain_age: RDAP non-JSON response for %r: %s", domain, exc)
        return None


def _extract_registration_event(rdap_data: dict) -> str:
    """Pure parse: the "registration" event's date out of an RDAP response's
    events[] list. "" if absent (some registries only report last-changed).
    """
    for ev in (rdap_data or {}).get("events", []) or []:
        if (ev.get("eventAction") or "").strip().lower() == "registration":
            return ev.get("eventDate", "")
    return ""


def _raw_whois(server: str, domain: str) -> str:
    with socket.create_connection((server, 43), timeout=_TIMEOUT) as sock:
        sock.sendall((domain + "\r\n").encode("ascii", errors="ignore"))
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode(errors="replace")


def _whois_query(domain: str) -> str:
    """Raw-socket WHOIS via the IANA referral chain: ask IANA which registry
    is authoritative for this TLD, then query that server directly. Falls
    back to a generic gTLD server if IANA gives no referral. "" on any
    connection error.
    """
    try:
        iana_resp = _raw_whois(_WHOIS_IANA_SERVER, domain)
    except Exception as exc:
        logger.warning("domain_age: WHOIS IANA referral failed for %r: %s", domain, exc)
        return ""

    server = _WHOIS_FALLBACK_SERVER
    for line in iana_resp.splitlines():
        if line.lower().startswith("refer:"):
            server = line.split(":", 1)[1].strip()
            break

    try:
        return _raw_whois(server, domain)
    except Exception as exc:
        logger.warning("domain_age: WHOIS query to %r failed for %r: %s", server, domain, exc)
        return ""


def _extract_whois_creation(text: str) -> str:
    """Pure parse: the first "Creation Date"/"created"/"Registered on" line
    out of raw WHOIS text. "" if none of those labels are present (format
    varies a lot by registry).
    """
    m = _WHOIS_CREATED_RE.search(text or "")
    return m.group(1).strip() if m else ""


def verify_domain_age(domain: str) -> list[dict]:
    """Domain creation date via RDAP, falling back to WHOIS.

    Returns a single-record list, or [] if the domain is blank or neither
    RDAP nor WHOIS yielded a creation date. Never raises.
    """
    domain = _normalize_domain(domain)
    if not domain:
        return []

    creation_date = ""
    method = ""

    try:
        rdap_data = _rdap_lookup(domain)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("domain_age: unexpected RDAP error for %r: %s", domain, exc)
        rdap_data = None

    if rdap_data:
        creation_date = _extract_registration_event(rdap_data)
        if creation_date:
            method = "RDAP"

    if not creation_date:
        try:
            whois_text = _whois_query(domain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("domain_age: unexpected WHOIS error for %r: %s", domain, exc)
            whois_text = ""
        creation_date = _extract_whois_creation(whois_text)
        if creation_date:
            method = "WHOIS"

    if not creation_date:
        logger.info("domain_age: no creation date found for %r via RDAP or WHOIS", domain)
        return []

    snippet = (
        f"Domain {domain!r} registered/created {creation_date} (via {method}). "
        "Registrant identity is usually privacy-masked, so this speaks only to "
        "the registration TIMELINE, not who owns it: a fresh registration is "
        "the stronger LARP signal, an aged domain can still have been bought."
    )
    return [
        {
            "source_url": _RDAP_URL_TEMPLATE.format(domain=domain),
            "snippet": snippet,
            "source_name": _SOURCE_NAME,
            "weight": weight_for(_SOURCE_NAME),
            "match_confidence": "high",
        }
    ]
