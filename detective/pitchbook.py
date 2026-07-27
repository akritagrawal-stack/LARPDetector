"""PitchBook cross-verification source. ADDITIVE ONLY, never a replacement.

PitchBook is strong exactly where web search is weak: does a company exist,
how much did it raise and from whom, was it acquired or did it fold, and is a
person actually listed in an exec/founder/board capacity at a given company.
This module wires that up as a second evidence source behind verify.py's
gating (see gather_evidence there), never as the primary path.

Attribution: the request and authentication structure is adapted from an
earlier private curl_cffi PitchBook client owned by this project's author.
It is copied rather than imported because that client belongs to a separate
application with unrelated configuration and database dependencies. This
copy is trimmed down hard: no contact information, email or phone extraction,
screener, or daily-lookup database. A LARP detector only needs public
existence, funding, and role facts. The small per-profile budget enforced by
the caller is the relevant protection here.

Auth: cookies are read from a JSON file at the path in the PITCHBOOK_COOKIES_PATH
env var (same shape as the VC-emails .pitchbook_storage.json export:
{"cookies": [{"name", "value", "domain"}, ...]}). No path is hardcoded and no
cookie value is ever logged. If the env var is unset, the file is missing, or
the file carries no SESSION cookie, PitchBook is treated as unavailable and
every public function below returns [] without raising.

Real cap: PitchBook's institutional account returns HTTP 402
(PROFILE_LIMIT_REACHED) at roughly 250 profile views (confirmed in the
VC-emails runbook at docs/superpowers/operator-startup-sourcing-runbook.md).
This module does not track that budget itself: the caller's per-profile cap
(PitchBookBudget, default 3 lookups) is what protects it, and a 402 here is
handled the same as any other PitchBook error (log a one-line warning, return
[], never raise).

Public surface:
    is_enabled() -> bool
    PitchBookBudget(max_lookups=3)
    verify_company(name, budget=None) -> list[{"source_url", "snippet"}]
    verify_person_role(person, company, budget=None) -> list[{"source_url", "snippet"}]

No em dashes in this file (house rule).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://my.pitchbook.com/web-api"
_REQUEST_TIMEOUT = 20
_THROTTLE_SECS = 2.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

_DEFAULT_MAX_LOOKUPS_PER_PROFILE = 3

# Lazy import: curl_cffi is required to pass PitchBook's Cloudflare TLS check
# (plain requests gets a 403). Missing it just means PitchBook is unavailable,
# never a hard crash.
try:
    from curl_cffi import requests as _cffi_requests

    _CFFI_AVAILABLE = True
except ImportError:
    _cffi_requests = None  # type: ignore
    _CFFI_AVAILABLE = False


class PitchBookUnavailable(Exception):
    """Raised internally for any condition that should end in [] to the caller:
    missing config, missing/stale auth, HTTP 401/402/403, or a network error.
    Never escapes verify_company / verify_person_role.
    """


def is_enabled() -> bool:
    """True only if PITCHBOOK_ENABLED is explicitly set to a truthy value.

    Default is false (opt-in), per the gating requirement: PitchBook must
    never run unless a human turned it on.
    """
    return os.environ.get("PITCHBOOK_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _norm(s: str) -> str:
    return (s or "").strip().lower()


class PitchBookBudget:
    """Per-profile lookup budget, shared across every claim in one pipeline run.

    Caps total PitchBook lookups (company or person) at max_lookups so a
    single profile can never eat more than a small slice of the roughly
    250-view daily cap. Also caches by (normalized) name so three claims
    about the same employer (e.g. "Cluely" appearing in a role claim, a
    funding claim, and an ARR claim) resolve that company once and reuse the
    cached evidence for the other two, at zero extra budget cost.
    """

    def __init__(self, max_lookups: int = _DEFAULT_MAX_LOOKUPS_PER_PROFILE):
        self.max_lookups = max_lookups
        self.used = 0
        self._company_cache: dict[str, list[dict]] = {}
        self._person_cache: dict[tuple, list[dict]] = {}

    def cached_company(self, name: str) -> Optional[list[dict]]:
        return self._company_cache.get(_norm(name))

    def cache_company(self, name: str, evidence: list[dict]) -> None:
        self._company_cache[_norm(name)] = evidence

    def cached_person(self, person: str, company: str) -> Optional[list[dict]]:
        return self._person_cache.get((_norm(person), _norm(company)))

    def cache_person(self, person: str, company: str, evidence: list[dict]) -> None:
        self._person_cache[(_norm(person), _norm(company))] = evidence

    def try_consume(self) -> bool:
        """Reserve one lookup slot. Returns False (and reserves nothing) once
        max_lookups is reached; the caller must then skip the network call.
        """
        if self.used >= self.max_lookups:
            return False
        self.used += 1
        return True


# ── Auth / session ──────────────────────────────────────────────────────────


def _cookies_path() -> Optional[Path]:
    raw = os.environ.get("PITCHBOOK_COOKIES_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def _load_session():
    """Build a curl_cffi Session from the cookie file at PITCHBOOK_COOKIES_PATH.

    Raises PitchBookUnavailable for every failure mode (missing curl_cffi,
    missing/unreadable file, no SESSION cookie). Never raises anything else.
    """
    if not _CFFI_AVAILABLE:
        raise PitchBookUnavailable("curl_cffi is not installed; PitchBook path unavailable")

    path = _cookies_path()
    if path is None:
        raise PitchBookUnavailable(
            "PITCHBOOK_COOKIES_PATH is unset or the file does not exist"
        )

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PitchBookUnavailable(f"could not read/parse cookie file: {exc}") from exc

    cookies = state.get("cookies", []) if isinstance(state, dict) else []
    pb_cookies: dict[str, str] = {}
    for c in cookies:
        domain = (c.get("domain") or "").lower()
        name = c.get("name")
        value = c.get("value")
        if name and value is not None and "pitchbook" in domain:
            pb_cookies[name] = value

    if not pb_cookies.get("SESSION"):
        raise PitchBookUnavailable(
            "no SESSION cookie in cookie file; PitchBook auth is stale or missing"
        )

    session = _cffi_requests.Session(impersonate="chrome146")
    for name, value in pb_cookies.items():
        session.cookies.set(name, value, domain="my.pitchbook.com")
    session.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://my.pitchbook.com/",
            "x-requested-with": "XMLHttpRequest",
        }
    )
    return session


def _raise_for_status(resp) -> None:
    if resp.status_code == 402:
        raise PitchBookUnavailable("HTTP 402 PROFILE_LIMIT_REACHED (PitchBook view cap hit)")
    if resp.status_code in (401, 403):
        raise PitchBookUnavailable(
            f"HTTP {resp.status_code} (auth expired, blocked, or stale cookies)"
        )
    if resp.status_code != 200:
        raise PitchBookUnavailable(f"HTTP {resp.status_code} from PitchBook")


def _post(session, path: str, body: dict) -> dict:
    try:
        resp = session.post(f"{BASE_URL}{path}", json=body, timeout=_REQUEST_TIMEOUT)
    except Exception as exc:
        raise PitchBookUnavailable(f"request error: {exc}") from exc
    _raise_for_status(resp)
    try:
        return resp.json()
    except Exception as exc:
        raise PitchBookUnavailable(f"non-JSON response: {exc}") from exc


def _get(session, path: str, params: Optional[dict] = None) -> dict:
    try:
        resp = session.get(f"{BASE_URL}{path}", params=params, timeout=_REQUEST_TIMEOUT)
    except Exception as exc:
        raise PitchBookUnavailable(f"request error: {exc}") from exc
    _raise_for_status(resp)
    try:
        return resp.json()
    except Exception as exc:
        raise PitchBookUnavailable(f"non-JSON response: {exc}") from exc


def _throttle() -> None:
    time.sleep(_THROTTLE_SECS)


# ── Search ───────────────────────────────────────────────────────────────────


def _search_mixed(session, query: str, limit: int = 8) -> dict:
    body = {
        "savedConferenceSearchAllowed": False,
        "searchRequest": {"limit": limit, "offset": 0, "query": query},
        "timeZoneOffset": "-05:00",
    }
    return _post(session, "/general-search/search/mixed", body)


def _first_item_of_type(data: dict, type_name: str) -> Optional[dict]:
    for item in (data or {}).get("items", []) or []:
        if item.get("type") == type_name:
            return item
    return None


def _profile_url(pb_id: Optional[str], kind: str) -> str:
    if not pb_id:
        return "https://my.pitchbook.com/"
    # Confirmed pattern for the investor case (see pitchbook_browser.py docstring
    # in the VC-emails project): /profile/{id}/investor/profile. The company
    # equivalent is inferred by the same convention; it is only used here as a
    # citation URL, never re-fetched, so an inexact suffix carries no risk.
    return f"https://my.pitchbook.com/profile/{pb_id}/{kind}/profile"


# ── Public API ───────────────────────────────────────────────────────────────


def verify_company(name: str, budget: Optional["PitchBookBudget"] = None) -> list[dict]:
    """Look up a company on PitchBook: existence, description, and (best
    effort) a short funding/investor snippet if the follow-up call succeeds.

    Returns a list of {"source_url", "snippet"} evidence records, or []
    if PitchBook is disabled, unavailable, over budget, or the company was
    not found. Never raises.
    """
    name = (name or "").strip()
    if not name or not is_enabled():
        return []

    budget = budget or PitchBookBudget(max_lookups=1)

    cached = budget.cached_company(name)
    if cached is not None:
        return cached

    if not budget.try_consume():
        logger.info(
            "pitchbook: budget exhausted (%d/%d), skipping company lookup for %r",
            budget.used, budget.max_lookups, name,
        )
        return []

    logger.info(
        "pitchbook: company lookup %d/%d for %r", budget.used, budget.max_lookups, name
    )

    try:
        session = _load_session()
        data = _search_mixed(session, name)
        item = _first_item_of_type(data, "COMPANY")
        if item is None:
            evidence: list[dict] = []
            budget.cache_company(name, evidence)
            return evidence

        val = item.get("value", {}) or {}
        pr = val.get("profileResult", {}) or {}
        pb_id = pr.get("id")
        pb_name = (pr.get("name") or name).strip()
        description = (pr.get("description") or "").strip()

        parts = [f"PitchBook lists a company profile for {pb_name}."]
        if description:
            parts.append(description[:400])

        # Best-effort funding/investor snippet. Failure here must not drop the
        # existence/description evidence already gathered above.
        if pb_id:
            try:
                _throttle()
                inv_data = _get(
                    session,
                    f"/profile-platform-bff/profiles/{pb_id}/company/investors/ACTIVE",
                    params={"page": 1, "pageSize": 10},
                )
                inv_snippet = _summarize_investors(inv_data)
                if inv_snippet:
                    parts.append(inv_snippet)
            except Exception as exc:  # noqa: BLE001 - best effort only
                logger.debug("pitchbook: company investors lookup failed for %r: %s", name, exc)

        evidence = [
            {"source_url": _profile_url(pb_id, "company"), "snippet": " ".join(parts)}
        ]
        budget.cache_company(name, evidence)
        return evidence

    except Exception as exc:  # noqa: BLE001 - PitchBook must never crash the pipeline
        logger.warning("PitchBook unavailable (cap or auth), falling back to web search")
        logger.debug("pitchbook: verify_company(%r) error detail: %s", name, exc)
        evidence = []
        budget.cache_company(name, evidence)
        return evidence


def _summarize_investors(inv_data: dict) -> str:
    """Best-effort one-line investor/round summary from a company investors
    response. Returns "" on any unexpected shape (no fields are invented).
    """
    investors = []
    for item in (inv_data or {}).get("investors", []) or []:
        inv = (item.get("investor") or {})
        inv_name = (inv.get("name") or "").strip()
        if not inv_name:
            continue
        rounds = item.get("rounds") or []
        round_info = (rounds[0].get("round") if rounds else {}) or {}
        deal_type = round_info.get("primaryTypeWithSeries", "")
        amount = ((round_info.get("amount") or {}).get("amount"))
        tag = inv_name
        if deal_type or amount:
            tag += f" ({deal_type}{', ' + str(amount) if amount else ''})".strip()
        investors.append(tag)
        if len(investors) >= 5:
            break
    if not investors:
        return ""
    return "PitchBook-listed investors: " + "; ".join(investors) + "."


def _mentions_company(company: str, related_company: str, description: str) -> bool:
    """True if `company` plausibly appears in the PitchBook record's own
    associated-company field or bio text.

    This is the guard that keeps a role claim for one employer from ever
    being silently paired with PitchBook evidence about a DIFFERENT
    employer. PitchBook's mixed search ranks on the whole query string but
    still returns a person's single top-ranked profile (usually their
    current/primary role) regardless of which company we asked about, so
    without this check every employment claim for a person would attach the
    same snippet, and an old claim (e.g. "worked at NVIDIA") could end up
    citing a bio that is actually about a different, unrelated employer
    (e.g. a Red Barn Robotics or Relativity Space bio). No mention, no
    evidence: the caller then falls back to web search evidence only.
    """
    target = _norm(company)
    if not target:
        return True
    return target in _norm(related_company) or target in _norm(description)


def verify_person_role(
    person: str, company: str, budget: Optional["PitchBookBudget"] = None
) -> list[dict]:
    """Look up whether a person has a PitchBook-listed exec/founder/board
    association with the given company.

    The search query includes the company (not just the person's name) so a
    lookup for "Ilya Kelner at NVIDIA" is not issued identically to one for
    "Ilya Kelner at Relativity Space". Even so, PitchBook's mixed search can
    still return the same top-ranked person profile regardless of the query
    wording, so the returned record is additionally checked (see
    _mentions_company) to actually mention the given company before it is
    attached as evidence for THIS claim; a match against a different company
    is discarded rather than attached, so distinct claims about the same
    person never collapse onto one shared, possibly-wrong snippet.

    Returns a list of {"source_url", "snippet"} evidence records, or [] if
    PitchBook is disabled, unavailable, over budget, no match was found, or
    the match found could not be tied to this company. Never raises.
    """
    person = (person or "").strip()
    company = (company or "").strip()
    if not person or not is_enabled():
        return []

    budget = budget or PitchBookBudget(max_lookups=1)

    cached = budget.cached_person(person, company)
    if cached is not None:
        return cached

    if not budget.try_consume():
        logger.info(
            "pitchbook: budget exhausted (%d/%d), skipping person-role lookup for %r",
            budget.used, budget.max_lookups, person,
        )
        return []

    logger.info(
        "pitchbook: person-role lookup %d/%d for %r at %r",
        budget.used, budget.max_lookups, person, company,
    )

    try:
        session = _load_session()
        query = f"{person} {company}".strip() if company else person
        data = _search_mixed(session, query)
        item = _first_item_of_type(data, "INVESTOR") or _first_item_of_type(data, "PERSON")
        if item is None:
            evidence: list[dict] = []
            budget.cache_person(person, company, evidence)
            return evidence

        val = item.get("value", {}) or {}
        pr = val.get("profileResult", {}) or {}
        related = val.get("relatedPerson", {}) or {}
        pb_id = pr.get("id")
        pb_name = (pr.get("name") or person).strip()
        description = (pr.get("description") or "").strip()
        related_company = (related.get("companyName") or "").strip()

        if not _mentions_company(company, related_company, description):
            logger.info(
                "pitchbook: match for %r does not mention %r (associated company %r); "
                "not attaching as evidence for this claim",
                person, company, related_company,
            )
            evidence = []
            budget.cache_person(person, company, evidence)
            return evidence

        parts = [f"PitchBook lists a profile for {pb_name}."]
        if related_company:
            parts.append(f"PitchBook-associated company: {related_company}.")
        if description:
            parts.append(description[:300])

        evidence = [
            {"source_url": _profile_url(pb_id, "investor"), "snippet": " ".join(parts)}
        ]
        budget.cache_person(person, company, evidence)
        return evidence

    except Exception as exc:  # noqa: BLE001 - PitchBook must never crash the pipeline
        logger.warning("PitchBook unavailable (cap or auth), falling back to web search")
        logger.debug(
            "pitchbook: verify_person_role(%r, %r) error detail: %s", person, company, exc
        )
        evidence = []
        budget.cache_person(person, company, evidence)
        return evidence
