"""Product-site probe: the web-application half of "does this thing exist".

The catalog connectors (app_store, packages, accelerators) answer that question
for apps and libraries. Most claimed products are WEB applications, and for
those we had nothing: on a person scan a founder claim carries only the product
NAME, while the three connectors that can actually assess a live product
(wayback, domain_age, techstack) are URL-keyed and never fired. This module is
the missing bridge: it turns a candidate URL into FACTS a reasoning step can
judge, and builds the evidence records for the outcome.

WHAT THIS MODULE DOES NOT DO: decide which candidate is the claimed product.
Name matching does not survive contact with reality ("Cognition" hits thousands
of sites), and a wrong-site match is the classic defamation path: it must never
confirm and never condemn. So code harvests and probes; the brain picks (see
LLMProvider.resolve_product_site).

WHAT A RESOLVED SITE PROVES: that the PRODUCT exists, and roughly how built-out
it is. NOT that the person founded it, held the role they claim, or has the
users they claim. Existence does not clear a role claim, which is why these
records are deliberately NOT in dossier._CORROBORATING_SOURCES and why the
snippet says so in words the reasoning step reads.

Public surface:
    urls_in_text(text) -> list[str]
    probe_site(url) -> Optional[dict]
    probe_candidates(urls, max_candidates) -> list[dict]
    resolved_record(product_name, probe, confidence, rationale) -> dict
    not_found_record(product_name, candidates_seen) -> dict

Evidence record shape (matches every other connector):
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}
plus "resolution" ("resolved" | "not_found") so downstream code can tell a
positive hit from a searched absence without parsing prose.

Deliberately absent: "registry_check". detect_registry_absence fires only off
AUTHORITATIVE registries (Apple's own catalog, YC's own directory). The open web
is not one, and a web miss must never be able to trip a registry-absence
finding.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

from .registry import weight_for

logger = logging.getLogger(__name__)

_SOURCE_NAME = "product_site"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (product-site existence check)"
_MAX_CANDIDATES = 6
_SNIPPET_CAP = 300

# Hosts that are never a product's own site. LinkedIn's own wrappers and the
# social platforms: keeping them would just hand the resolver noise to
# disambiguate against, and none of them can be pointed at wayback/techstack
# as "the product".
_NEVER_A_PRODUCT_HOST = (
    "linkedin.com", "lnkd.in", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "medium.com",
    "substack.com", "notion.site", "docs.google.com", "drive.google.com",
    "calendly.com", "t.co", "bit.ly",
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# Trailing punctuation that belongs to the sentence, not the URL.
_TRAILING_JUNK = ").,;:!?'\"]}>"

# A parked/for-sale placeholder is NOT a product. Flagged as a fact so the
# reasoning step can read "the domain resolves but nothing is there", which is
# a very different signal from a real thin site.
_PARKED_MARKERS = (
    "is for sale", "buy this domain", "domain for sale", "parked domain",
    "this domain is parked", "domain parking", "coming soon",
    "under construction", "godaddy.com/domainsearch", "sedoparking",
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r"<meta[^>]+name=[\"']description[\"'][^>]*content=[\"'](.*?)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_OG_DESC_RE = re.compile(
    r"<meta[^>]+property=[\"']og:description[\"'][^>]*content=[\"'](.*?)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _host_of(url: str) -> str:
    try:
        netloc = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":", 1)[0]


def _is_never_a_product(url: str) -> bool:
    host = _host_of(url)
    if not host:
        return True
    return any(host == h or host.endswith("." + h) for h in _NEVER_A_PRODUCT_HOST)


def urls_in_text(text: Optional[str]) -> list[str]:
    """Every plausible product URL written in a post, in order, deduped.

    The owner's point: a founder's own posts usually link the thing they built,
    and that link is far cheaper and far less ambiguous than a name search.
    Own-platform and social links are dropped (see _NEVER_A_PRODUCT_HOST).
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(text):
        url = raw.rstrip(_TRAILING_JUNK)
        if not url or _is_never_a_product(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


class _NamedLinkParser(HTMLParser):
    """Collect links whose visible label names one claimed product."""

    def __init__(self, base_url: str, product_name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.product_norm = _norm_name(product_name)
        self.current: Optional[dict] = None
        self.matches: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if (tag or "").lower() != "a":
            return
        values = {str(key).lower(): (value or "") for key, value in attrs}
        self.current = {
            "href": values.get("href", ""),
            "label": " ".join(
                values.get(key, "") for key in ("aria-label", "title")
            ),
            "text": [],
        }

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["text"].append(data or "")

    def handle_endtag(self, tag: str) -> None:
        if (tag or "").lower() != "a" or self.current is None:
            return
        item = self.current
        self.current = None
        label = f"{item['label']} {' '.join(item['text'])}"
        if not self.product_norm or self.product_norm not in _norm_name(label):
            return
        href = (item["href"] or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            return
        url = urljoin(self.base_url, href)
        if _is_never_a_product(url):
            return
        if url not in self.matches:
            self.matches.append(url)


class _AllLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if (tag or "").lower() != "a":
            return
        values = {str(key).lower(): (value or "") for key, value in attrs}
        href = (values.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            return
        url = urljoin(self.base_url, href)
        if url not in self.links:
            self.links.append(url)


def extract_subject_identity_hints(page_url: str) -> dict:
    """Recover identity links from a subject-matched personal website."""
    page_url = (page_url or "").strip()
    if not page_url:
        return {}
    try:
        resp = _get(page_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("product_site: could not inspect identity links on %r: %s", page_url, exc)
        return {}
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0
    if status < 200 or status >= 400:
        return {}
    final_url = (getattr(resp, "url", "") or page_url).strip() or page_url
    parser = _AllLinkParser(final_url)
    try:
        parser.feed(getattr(resp, "text", "") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("product_site: could not parse identity links on %r: %s", page_url, exc)
        return {}

    hints = {
        "personal_site": final_url,
        "website": final_url,
        "domain": _host_of(final_url),
        "websites": [final_url],
    }
    for link in parser.links:
        try:
            parsed = urlparse(link)
        except Exception:
            continue
        host = (parsed.hostname or "").lower()
        if host == "github.com" or host.endswith(".github.com"):
            login = (parsed.path or "").strip("/").split("/", 1)[0]
            if login and login not in {"features", "marketplace", "orgs", "topics"}:
                hints["github_login"] = login
                break
    return {key: value for key, value in hints.items() if value}


def extract_named_product_links(
    page_url: str, product_name: str, max_links: int = 4
) -> list[str]:
    """Fetch one subject page and return links explicitly labeled as a product.

    This is a bounded bridge for a common LinkedIn extraction failure: the
    contact overlay can miss a personal website, while a subject-matched search
    result still finds that site and the site itself links each shipped product.
    Code only harvests exact labeled links. The resolver still decides whether
    the target is the claimed product, so this never confirms a role.
    """
    page_url = (page_url or "").strip()
    product_name = (product_name or "").strip()
    if not page_url or not product_name:
        return []
    try:
        resp = _get(page_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("product_site: could not inspect links on %r: %s", page_url, exc)
        return []
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0
    if status < 200 or status >= 400:
        return []
    final_url = (getattr(resp, "url", "") or page_url).strip() or page_url
    parser = _NamedLinkParser(final_url, product_name)
    try:
        parser.feed(getattr(resp, "text", "") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("product_site: could not parse links on %r: %s", page_url, exc)
        return []
    return parser.matches[: max(0, max_links)]


def _clean(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _get(url: str):
    """The one network call, isolated so tests can monkeypatch it (repo style)."""
    import requests  # lazy: keeps offline paths import-free

    return requests.get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
        timeout=_TIMEOUT,
        allow_redirects=True,
    )


def probe_site(url: str) -> Optional[dict]:
    """Fetch one candidate and report FACTS, or None if we could not look.

    None is load-bearing: "could not look" (timeout, DNS failure, connection
    reset) must never be read as "does not exist". A 404 or a parked page IS a
    fact and comes back as a dict, because we did look.
    """
    url = (url or "").strip()
    if not url:
        return None
    try:
        resp = _get(url)
    except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
        logger.warning("product_site: could not fetch %r: %s", url, exc)
        return None

    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0
    final_url = (getattr(resp, "url", "") or url) or url
    html = getattr(resp, "text", "") or ""

    title = ""
    m = _TITLE_RE.search(html)
    if m:
        title = _clean(m.group(1))[:200]
    description = ""
    for pattern in (_META_DESC_RE, _OG_DESC_RE):
        m = pattern.search(html)
        if m:
            description = _clean(m.group(1))[:400]
            break

    haystack = f"{title} {description}".lower()
    parked = any(marker in haystack for marker in _PARKED_MARKERS)

    return {
        "url": url,
        "final_url": final_url,
        "domain": _host_of(final_url) or _host_of(url),
        "status": status,
        "title": title,
        "description": description,
        "parked": parked,
    }


def probe_candidates(
    urls: list[str],
    max_candidates: int = _MAX_CANDIDATES,
    deadline: Optional[float] = None,
) -> list[dict]:
    """Probe up to max_candidates URLs, dropping the unreachable ones.

    Bounded on purpose: a resolution pass sits in front of the whole evidence
    gather, so it must never become an unbounded crawl. `deadline` is an
    optional time.time() ceiling checked before each fetch, so a run of slow
    hosts cannot stretch a scan past its budget. Never raises.
    """
    import time as _time

    out: list[dict] = []
    seen: set[str] = set()
    attempted = 0
    for url in urls or []:
        if attempted >= max(0, max_candidates):
            break
        if deadline is not None and _time.time() > deadline:
            logger.warning("product_site: hit the probe wall-clock ceiling; stopping")
            break
        url = (url or "").strip()
        if not url or url in seen or _is_never_a_product(url):
            continue
        seen.add(url)
        attempted += 1
        try:
            probe = probe_site(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("product_site: probe raised for %r: %s", url, exc)
            continue
        if probe is not None:
            out.append(probe)
    return out


def resolved_record(
    product_name: str,
    probe: dict,
    confidence: str = "medium",
    rationale: str = "",
    first_party_mapping: bool = False,
) -> dict:
    """Evidence record for a candidate the reasoning step accepted as the
    claimed product's real site.

    The snippet states, in words, that existence is not the role claim. Without
    that line a CONFIRMED web product reads to the brain as "claim cleared",
    which is precisely the hole the judgment-layer block closed.
    """
    probe = probe or {}
    status = probe.get("status")
    title = (probe.get("title") or "").strip()
    description = (probe.get("description") or "").strip()
    parked = bool(probe.get("parked"))
    destination = probe.get("final_url") or probe.get("url") or ""
    visible_identity = " ".join(
        [title, description, _host_of(destination)]
    )
    name_visible = bool(
        _norm_name(product_name)
        and _norm_name(product_name) in _norm_name(visible_identity)
    )
    if name_visible:
        name_alignment = "visible_match"
        mapping_basis = "destination_identity"
    elif first_party_mapping:
        name_alignment = "first_party_alias"
        mapping_basis = "subject_site_link"
    else:
        name_alignment = "resolver_selected"
        mapping_basis = "resolver_judgment"

    state = "live" if status and 200 <= int(status) < 400 and not parked else (
        "parked placeholder" if parked else f"HTTP {status}"
    )
    detail = f' Site title: "{title}".' if title else ""
    if description:
        detail += f' Description: "{description[:160]}".'
    why = f" Resolved because: {rationale.strip()}." if rationale else ""
    identity_note = ""
    if name_alignment == "first_party_alias":
        identity_note = (
            f" FIRST-PARTY ALIAS MAPPING: the subject's own site labels this "
            f"destination {product_name!r}, but the destination identifies "
            f"itself as {title or _host_of(destination)!r}. This may be a "
            f"rename or rebrand. It verifies the linked app exists, but the "
            f"name equivalence is not independently established."
        )

    snippet = (
        f"WEB PRODUCT RESOLVED: the claimed product {product_name!r} resolves to "
        f"{destination} ({state})."
        f"{detail}{why}{identity_note} This substantiates that the PRODUCT exists and is "
        f"reachable. It does NOT substantiate the person's ROLE, seniority, "
        f"ownership, or any user/revenue number: who built it is a separate "
        f"question needing its own evidence."
    )
    effective_confidence = (confidence or "medium").lower()
    if name_alignment == "first_party_alias" and effective_confidence == "high":
        effective_confidence = "medium"
    return {
        "source_url": destination,
        "snippet": snippet[:_SNIPPET_CAP * 2],
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": effective_confidence,
        "resolution": "resolved",
        "product_name_alignment": name_alignment,
        "mapping_basis": mapping_basis,
    }


def not_found_record(product_name: str, candidates_seen: int = 0) -> dict:
    """Evidence record for "a real search ran and no credible site exists".

    Weight 0.0: this is an ABSENCE, and it earns its place by describing its own
    ceiling. Absence of a web presence is SUS-strength at most and can never
    reach DISPROVEN. The web has no authoritative index, a product can be
    renamed, unlaunched, internal, or behind a login, and absence-as-disproof is
    the one line this project does not cross.

    NOT emitted for an ambiguous resolution. "Several candidates, could not tell
    which" means we could not look properly, so it contributes ZERO and no
    record is written at all (see the resolver stage).
    """
    return {
        "source_url": "",
        "snippet": (
            f"NO WEB PRODUCT FOUND: a real search ran for the claimed product "
            f"{product_name!r} ({candidates_seen} candidate site(s) checked) and "
            f"none of them is credibly that product. Supports SUS (UNVERIFIED "
            f"plus high expected footprint) for a claim that leans on the product "
            f"being real. HARD RULE: this can NEVER reach DISPROVEN. The open web "
            f"is not an authoritative registry, and a product can be renamed, "
            f"pre-launch, internal, or login-walled."
        ),
        "source_name": _SOURCE_NAME,
        "weight": 0.0,
        "match_confidence": "low",
        "resolution": "not_found",
    }
