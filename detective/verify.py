"""Per-claim evidence gathering.

This module ONLY gathers evidence and attaches it to claims. It never sets
claim.tier, larp_score, or the verdict. That reasoning is the exclusive job of
an LLMProvider (see llm.py). Keeping the judgment out of here on purpose:
mechanical "not found -> DISPROVEN" rules produce false accusations, which is
exactly what a LARP detector must avoid.

Query design (each claim gets its OWN targeted queries, not a generic dump):
  - identity claims  : anchor the bare name with a disambiguator (current
                        company and/or headline) so same-name strangers
                        (e.g. a film producer sharing an entrepreneur's name)
                        do not pollute the evidence.
  - employment/education claims : one corroboration query built from the
                        claim's own fields (person, title, employer), so a
                        role claim ("Founder and CEO") and a metric claim
                        ("publicly stated 7 million dollar ARR") search
                        differently instead of collapsing to the same
                        generic "person + company" lookup, plus one
                        adversarial query aimed at surfacing DISPROVING
                        evidence ("no record", fired, lied, fake, fraud,
                        "did not attend", left). The adversarial query is
                        what actually finds fabrication reporting.
  - user_count / revenue_metric (company scans) : a corroboration query, a
                        footprint query (social following / app-store
                        reviews, so the reasoning provider can judge a huge
                        claim against a tiny public footprint), and an
                        adversarial query. Still gathers only; never scores
                        plausibility itself.
  - proprietary_tech (company scans) : queries aimed at surfacing thin-wrapper
                        evidence ("built on OpenAI/Claude/GPT", "no-code").
  - funding (company scans) : web search plus the same PitchBook path used
                        for funding-flavored employment claims.
  - pricing (company scans) : the product's own pricing page plus an
                        adversarial "overpriced / hidden fees" query.
  - headcount (company scans) : a query aimed at surfacing a corroborated
                        team-size signal (LinkedIn company page, "how many
                        employees"), for the headcount_inflation company-LARP
                        metric. Gathering only: the code has no company-
                        LinkedIn member-count fetch yet, so this is a plain
                        web search, and the operator marks the metric PARTIAL
                        when nothing corroborating turns up (see llm.py's
                        company operator instructions).
  - company_overview (company scans) : one claim per company scan (added by
                        llm.mechanical_decompose_company, same pattern as the
                        person-scan identity claim) anchoring product-
                        liveness/app-store-footprint, recency, and technical-
                        team queries. Backs the product_realness and
                        zombie_liveness company-LARP metrics, neither of
                        which is tied to one specific pricing/metric/tech
                        claim.
  - anything else    : fall back to searching the claim's own assertion text.

Independent source connectors (detective/sources/), additive on top of the
web/PitchBook evidence above, gated so each only fires when it has the input
it needs:
  - github      : identity claims (the once-per-profile founder claim), and
                   any employment claim whose title reads founder/technical
                   (see _looks_technical_or_founder). Needs a person name.
  - sec_edgar   : a company-scan "funding" claim, or a person-scan employment
                   claim that reads funding/metric flavored (see
                   _is_funding_or_metric_claim). Needs claim.employer.
  - wayback /
    domain_age  : the once-per-profile company_overview claim (company scans
                   only), keyed off the company's own profile_url. Needs
                   company_url, threaded in from pipeline.run.
  - uspto        : a company-scan "proprietary_tech" claim (checked against
                   the company/product name as assignee/applicant), or a
                   person-scan employment/education/identity claim whose
                   text reads patent-flavored (see
                   _looks_patent_or_invention_flavored). Needs a company
                   name or a person name respectively.
  - arxiv /
    openalex     : a person-scan employment/education/identity claim whose
                   text reads research/credential-flavored (see
                   _looks_research_credential_flavored). Needs a person
                   name; NEVER fired on a company-scan proprietary_tech
                   claim, since both APIs search PEOPLE (authors), not
                   companies, and firing a person-name lookup on a bare
                   product name would only manufacture noise, exactly what
                   this module's honesty discipline exists to avoid.
  - packages    : a proprietary_tech or company_overview claim whose text
                   references an SDK/package/library/open-source (see
                   _looks_package_flavored). Needs the company/product name
                   (claim.employer).
  - app_store   : a user_count, revenue_metric, or company_overview claim
                   (traction / product-realness for a consumer app). Needs
                   the product name (claim.employer).
  - accelerators: the once-per-profile company_overview claim, OR any claim
                   whose own text reads accelerator/badge-flavored (see
                   _looks_accelerator_flavored, e.g. "YC-backed", "Techstars
                   portfolio"). Needs the company/product name
                   (claim.employer); "not listed" is never proof the
                   company was not backed by some OTHER accelerator.
  - hackernews  : the once-per-profile company_overview claim (searched by
                   product name), or a person-scan identity claim (searched
                   by the person's disambiguator company/headline, with the
                   person's name passed through as a candidate HN username,
                   see hackernews.py's own caveat that a username is never
                   itself a confirmed legal identity).
  - techstack   : the once-per-profile company_overview claim ONLY (company
                   scans), keyed off company_url exactly like wayback /
                   domain_age above. Deliberately NOT also fired on every
                   proprietary_tech claim (a company scan can carry several
                   of those): the fingerprint is a property of the URL, not
                   of any one claim, and firing per-claim would refetch the
                   same page repeatedly for no new signal, which is exactly
                   what "keep network calls reasonable" rules out. The
                   single company_overview fetch already backs the
                   buildability read (see techstack.py's module docstring
                   and llm.py's _COMPANY_OPERATOR_INSTRUCTIONS).
  - courtlistener: a person-scan identity claim (searched by the person's
                   own name, is_company=False), or the once-per-profile
                   company_overview claim (searched by the company/product
                   name, is_company=True, an adverse-record check). Needs a
                   person name or company name respectively. Highest
                   same-name false-positive risk of any source in this
                   registry; see courtlistener.py's own module docstring for
                   why match_confidence never reaches "high" here and a hit
                   must be corroborated before the reasoning provider treats
                   it as real.
Every one of these is wrapped so a network failure logs one line and
contributes [] rather than breaking evidence gathering for the claim.

Every claim is capped at _MAX_EVIDENCE_PER_CLAIM records after deduping by
source_url, preferring news/reference domains over bare company landing pages
when trimming, so downstream reasoning is not drowned in redundant hits.

Public surface:
    gather_evidence(claim, identity) -> Claim  (mutates claim.evidence in place)

No em dashes in this file (house rule).
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

from .models import Claim, evidence_weight
from .audit import ledger_for
from .search import web_search
from . import search as search_backend
from . import pitchbook
from .sources import github as github_source
from .sources import sec_edgar as sec_edgar_source
from .sources import wayback as wayback_source
from .sources import domain_age as domain_age_source
from .sources import uspto as uspto_source
from .sources import arxiv as arxiv_source
from .sources import openalex as openalex_source
from .sources import packages as packages_source
from .sources import app_store as app_store_source
from .sources import accelerators as accelerators_source
from .sources import hackernews as hackernews_source
from .sources import techstack as techstack_source
from .sources import courtlistener as courtlistener_source
from .sources import org_roster as org_roster_source
from .sources import news as news_source

logger = logging.getLogger(__name__)

_MAX_EVIDENCE_PER_QUERY = 4
_MAX_EVIDENCE_PER_CLAIM = 8

# Bounded thread pool for the independent source connectors. Each connector
# hits a DISTINCT host and does its own network I/O with `requests` (which
# releases the GIL during the call), so running them concurrently within one
# claim collapses the connector wall-clock from sum(t_i) to ~max(t_i). Kept
# per-claim (created inside gather_evidence, never at module import) so the
# module stays import-safe: no threads are spawned just by importing verify.
# web_search and pitchbook are deliberately NOT run in this pool (see
# gather_evidence): they share process-global rate-limit state (search's
# Brave cooldown) or shared mutable budget (pitchbook), and each connector's
# own internal fan-out stays serial, so no single host ever sees two
# concurrent requests from one claim.
_CONNECTOR_MAX_WORKERS = 8

# Query-role tags. The evidence a query produces is tagged with the ROLE of
# the query that found it so the per-claim cap (see _rank_and_cap) can protect
# the fabrication-finding signal classes that a plain news/landing sort would
# otherwise drop:
#   corroboration : a claim-specific "does this hold up" lookup.
#   adversarial   : a disproving-evidence lookup ("no record", fired, lied,
#                   fraud, thin-wrapper, overpriced). This is what actually
#                   surfaces fabrication reporting (see the module docstring),
#                   so it must not be crowded out by generic corroboration hits.
#   footprint     : a plausibility-footprint lookup (social following, app
#                   reviews, liveness/recency), so the reasoning provider can
#                   judge a huge claim against a tiny public footprint.
# Only web-search records carry a query_role; weighted source-connector records
# never do (they carry source_name/weight/match_confidence instead).
_ROLE_CORROBORATION = "corroboration"
_ROLE_ADVERSARIAL = "adversarial"
_ROLE_FOOTPRINT = "footprint"
_PROTECTED_ROLES = frozenset({_ROLE_ADVERSARIAL, _ROLE_FOOTPRINT})

# Per-claim cap quotas (see _rank_and_cap). Reserving guaranteed slots for the
# weighted high/medium-confidence connector records and for the adversarial/
# footprint web records is what fixes the old cap defect: a plain news/landing
# sort systematically dropped exactly those two high-value classes in favor of
# generic corroboration web hits. The two reserves must sum to <= the cap so a
# fill remainder always exists.
_CONNECTOR_RESERVE = 4
_ADVERSARIAL_RESERVE = 2

# A sane length cap on any assembled query text before it is ever issued to
# a search backend. Without this, a claim built from a very long title or
# assertion (e.g. a verbose role/description string folded into the query)
# produced a URL long enough to trip Brave's search endpoint with an HTTP
# 422 ("URL too long"), silently losing evidence for that claim. Cut on a
# word boundary, never mid-token, so the truncated query still reads as
# real search terms.
_MAX_QUERY_LEN = 380

_ADVERSARIAL_HINT = (
    '("no record" OR fired OR lied OR fake OR fraud OR "did not attend" OR left)'
)

# Keywords that mark a claim as funding / exit / metric flavored (as opposed to
# a plain role claim). PitchBook adds real value on these (rounds, valuation,
# acquisitions) that web search often misses or gets wrong. Kept as a light
# keyword check on purpose: this is a gate, not a classifier, and false
# positives just cost one extra (cheap, capped) PitchBook lookup.
_FUNDING_OR_METRIC_KEYWORDS = (
    "raised", "raise", "round", "seed", "series a", "series b", "series c",
    "series d", "funding", "valuation", "arr", "revenue", "acquired",
    "acquisition", "exit", "ipo", "million dollar", "billion dollar", "$",
)

_NEWS_OR_REFERENCE_HINTS = (
    "wikipedia.org", "reuters.com", "apnews.com", "nytimes.com", "techcrunch.com",
    "bloomberg.com", "forbes.com", "wsj.com", "washingtonpost.com", "cnn.com",
    "bbc.com", "bbc.co.uk", "npr.org", "theverge.com", "businessinsider.com",
    "axios.com", "politico.com", "cnbc.com", "propublica.org", "abcnews.go.com",
    "nbcnews.com", "cbsnews.com",
)


def _cap_query_length(query: str, max_len: int = _MAX_QUERY_LEN) -> str:
    """Truncate an assembled query to at most `max_len` characters, cutting
    on a word boundary (never mid-word), so an over-length query can never
    reach the search backend. Queries already within the cap pass through
    unchanged.
    """
    q = (query or "").strip()
    if len(q) <= max_len:
        return q
    truncated = q[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.strip()


_QUERY_STOPWORDS = frozenset(
    {
        "and", "the", "for", "with", "from", "this", "that", "full", "time",
        "company", "corporation", "inc", "llc", "university", "school",
        "international", "club", "academy", "program", "degree",
    }
)
_SUBJECT_CONTROLLED_HOSTS = ("medium.com", "substack.com")
_REPUBLICATION_HOSTS = (
    "rocketreach.co",
    "signalhire.com",
    "zoominfo.com",
    "contactout.com",
    "crunchbase.com",
)


def _query_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if len(token) >= 2 and token not in _QUERY_STOPWORDS
    }


def _org_aliases(value: str) -> list[str]:
    """Return exact and compact organization names for retrieval queries."""
    raw = " ".join((value or "").split()).strip()
    if not raw:
        return []
    aliases = [raw]
    without_parenthetical = re.sub(r"\s*\([^)]{1,40}\)\s*", " ", raw).strip()
    if without_parenthetical and without_parenthetical not in aliases:
        aliases.append(without_parenthetical)
    for acronym in re.findall(r"\(([A-Za-z][A-Za-z0-9&.-]{1,12})\)", raw):
        if acronym not in aliases:
            aliases.append(acronym)
    words = without_parenthetical.split()
    compact = [
        word for word in words
        if word.lower().strip(".,") not in {"international", "club", "inc", "llc"}
    ]
    compact_name = " ".join(compact).strip()
    if compact_name and compact_name not in aliases:
        aliases.append(compact_name)
    return aliases[:3]


def _host_matches_person(host: str, person: str) -> bool:
    """Return whether a host is plausibly the named person's own domain."""
    labels = host.lower().split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    host_label = (labels[0] if labels else "").replace("-", "").replace("_", "")
    name_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", (person or "").lower())
        if len(token) >= 2
    ]
    return bool(name_tokens) and all(token in host_label for token in name_tokens)


def _linkedin_owner_slug(url: str) -> tuple[str, str]:
    """Return the LinkedIn surface type and its visible owner slug."""
    parsed = urlparse(url or "")
    parts = [part for part in parsed.path.lower().split("/") if part]
    if len(parts) >= 2 and parts[0] in {"in", "company"}:
        return parts[0], parts[1]
    if len(parts) >= 2 and parts[0] == "posts":
        return "posts", parts[1].split("_", 1)[0]
    return "", ""


def _relationship_for_url(url: str, person: str = "") -> str:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if any(host == item or host.endswith("." + item) for item in _SUBJECT_CONTROLLED_HOSTS):
        return "subject_controlled"
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        surface, owner = _linkedin_owner_slug(url)
        if surface == "company":
            return "first_party_org"
        person_slug = "-".join(
            token
            for token in re.findall(r"[a-z0-9]+", (person or "").lower())
            if len(token) >= 2
        )
        owner_compact = re.sub(r"[^a-z0-9]+", "", owner)
        person_compact = re.sub(r"[^a-z0-9]+", "", person_slug)
        if owner and person_slug and (
            owner == person_slug
            or owner.startswith(person_slug + "-")
            or person_slug.startswith(owner + "-")
            or owner_compact.startswith(person_compact)
        ):
            return "subject_controlled"
        if owner:
            return "third_party"
        return "subject_controlled"
    if person and _host_matches_person(host, person):
        return "subject_controlled"
    if host.endswith("github.com") or host.endswith("devpost.com"):
        return "platform_artifact"
    return "third_party"


def _source_class_for_url(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if any(host == item or host.endswith("." + item) for item in _REPUBLICATION_HOSTS):
        return "republication"
    return "search_index"


def _person_matches_blob(person: str, blob: str) -> bool:
    """Match a full name or LinkedIn's common first-name plus last-initial form."""
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", (person or "").lower())
        if len(token) >= 2
    ]
    if not tokens:
        return False
    if all(token in blob for token in tokens):
        return True
    if len(tokens) < 2 or tokens[0] not in blob:
        return False
    first = re.escape(tokens[0])
    last_initial = re.escape(tokens[-1][0])
    return bool(
        re.search(rf"\b{first}\s+{last_initial}\b", blob)
        or re.search(rf"\b{first}[-_/]{last_initial}(?:\b|[/_.-])", blob)
    )


def _result_relevance(
    result: dict, claim: Claim, person: str, disambiguator: str = ""
) -> str:
    """Classify a raw web hit as substantive, association, or irrelevant.

    A result about an employer but not the named person is not employment
    evidence. A namesake result without the profile anchor is not identity
    evidence. This filter is intentionally mechanical and conservative. The
    reasoning provider still decides whether a retained record confirms a
    claim.
    """
    blob = " ".join(
        str(result.get(key) or "") for key in ("title", "snippet", "url")
    ).lower()
    if not blob:
        return "irrelevant"

    if claim.type in ("employment", "education", "identity"):
        person_matches = _person_matches_blob(person, blob)
        first_party_org = (
            _relationship_for_url(result.get("url") or "", person=person)
            == "first_party_org"
        )
        if not person_matches and not (
            claim.type in ("employment", "education") and first_party_org
        ):
            return "irrelevant"

    if claim.type == "identity":
        anchor_tokens = _query_tokens(disambiguator)
        if anchor_tokens and not any(token in blob for token in anchor_tokens):
            return "irrelevant"
        return "association"

    if claim.type in ("employment", "education"):
        aliases = _org_aliases(claim.employer)
        alias_match = any(
            all(token in blob for token in _query_tokens(alias))
            for alias in aliases
            if _query_tokens(alias)
        )
        if not alias_match:
            return "irrelevant"
        title_tokens = _query_tokens(claim.title) - _query_tokens(claim.employer)
        role_match = bool(title_tokens and any(token in blob for token in title_tokens))
        if "software" in title_tokens and "engineering" in title_tokens and "sde" in blob:
            role_match = True
        if {"mathematics", "programming"} <= title_tokens and "amp" in blob:
            role_match = True
        if role_match and person_matches:
            return "substantive"
        return "association"

    return "substantive"


def _to_evidence(
    results: list[dict],
    role: str = _ROLE_CORROBORATION,
    *,
    claim: Optional[Claim] = None,
    person: str = "",
    disambiguator: str = "",
) -> list[dict]:
    """Normalize search results into claim-evidence records.

    `role` is the query-role of the query that produced these results (see the
    _ROLE_* constants). Adversarial/footprint records are tagged with a
    "query_role" key so the per-claim cap can reserve slots for them; plain
    corroboration records are left untagged so the common case adds no extra
    key to the evidence dict.
    """
    ev = []
    for r in results:
        url = r.get("url", "")
        if not url:
            continue
        relevance = (
            _result_relevance(r, claim, person, disambiguator)
            if claim is not None
            else "substantive"
        )
        if relevance == "irrelevant":
            continue
        relationship = _relationship_for_url(url, person=person)
        if claim is not None and claim.product_url:
            result_host = (urlparse(url).hostname or "").lower()
            product_host = (urlparse(claim.product_url).hostname or "").lower()
            if (
                result_host
                and product_host
                and (
                    result_host == product_host
                    or result_host.endswith("." + product_host)
                    or product_host.endswith("." + result_host)
                )
            ):
                relationship = "first_party_org"
        record = {
            "source_url": url,
            "snippet": (r.get("snippet") or r.get("title") or "").strip(),
            "query_role": role,
            "claim_relevance": relevance,
            "relationship": relationship,
            "source_class": _source_class_for_url(url),
        }
        ev.append(record)
    return sorted(ev, key=_web_record_priority, reverse=True)[:_MAX_EVIDENCE_PER_QUERY]


def _web_record_priority(record: dict) -> tuple[int, int]:
    """Prefer role binding and independent provenance over search rank."""
    relevance_rank = {
        "substantive": 2,
        "association": 1,
    }.get((record.get("claim_relevance") or "").lower(), 0)
    relationship = (record.get("relationship") or "").lower()
    source_class = (record.get("source_class") or "").lower()
    if source_class == "republication":
        provenance_rank = 0
    else:
        provenance_rank = {
            "third_party": 4,
            "first_party_org": 3,
            "platform_artifact": 2,
            "subject_controlled": 1,
        }.get(relationship, 2)
    return relevance_rank, provenance_rank


_MATCH_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _evidence_rank(e: dict) -> tuple:
    """Higher tuple sorts as better. Used by _dedup to pick which of two
    records sharing a URL survives:
      1. a record carrying source_name/weight (a weighted source connector)
         always outranks a plain web hit, which carries neither.
      2. among two weighted records, higher match_confidence wins.
      3. then higher weight wins.
    See detective.models.evidence_weight / DEFAULT_EVIDENCE_WEIGHT: plain
    web-search evidence has no "weight" key at all, so it is never confused
    for a low-weight connector hit here.
    """
    is_weighted = 1 if (e.get("source_name") or e.get("weight") is not None) else 0
    confidence_rank = _MATCH_CONFIDENCE_RANK.get((e.get("match_confidence") or "").lower(), 0)
    return (is_weighted, confidence_rank, evidence_weight(e))


def _dedup(evidence: list[dict]) -> list[dict]:
    """Drop records with a duplicate source_url, keeping the BEST one per URL.

    Bug 4: plain web-search evidence is collected before the weighted
    source-connector evidence (see gather_evidence below), so a naive
    first-wins dedup silently discarded the higher-confidence WEIGHTED
    record whenever both produced the same URL, defeating source weighting
    entirely (reproduced live: accelerators.verify_accelerator("Browser
    Use") found a high-confidence YC match that a plain web hit on the same
    URL then threw away before it ever reached the dossier). Ranks by
    _evidence_rank and keeps the best-ranked record per URL, preserving
    first-seen order across distinct URLs.
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    for e in evidence:
        u = e.get("source_url", "")
        if not u:
            continue
        if u not in best:
            best[u] = e
            order.append(u)
        elif _evidence_rank(e) > _evidence_rank(best[u]):
            best[u] = e
    return [best[u] for u in order]


def _domain_of(url: str) -> str:
    return url.split("//")[-1].split("/")[0].lower()


def _is_landing_page(url: str) -> bool:
    """True for bare-root URLs like "https://cluely.com/": generic company
    homepage hits that are the least useful thing to keep when a claim gets
    trimmed down to the cap.
    """
    rest = url.split("//")[-1].split("/", 1)
    path = rest[1] if len(rest) > 1 else ""
    return path.strip("/") == ""


def _is_weighted(e: dict) -> bool:
    """True for a weighted source-connector record (carries source_name/weight);
    False for a plain web-search hit (carries neither). Mirrors _evidence_rank.
    """
    return bool(e.get("source_name")) or e.get("weight") is not None


def _is_weighted_high_medium(e: dict) -> bool:
    """A weighted connector record at high or medium match_confidence: the
    exact signal class the old cap sort dropped in favor of generic web hits.
    """
    return _is_weighted(e) and (e.get("match_confidence") or "").lower() in ("high", "medium")


def _is_protected_web(e: dict) -> bool:
    """A plain web record produced by an adversarial or footprint query (see
    _to_evidence). Only web records ever carry query_role, so this is
    implicitly disjoint from the weighted-connector class above.
    """
    return not _is_weighted(e) and (e.get("query_role") or "") in _PROTECTED_ROLES


def _news_landing_key(e: dict) -> tuple:
    """The original cap preference: news/reference domains first, bare landing
    pages last. Stable sort preserves search-rank order within each tier.
    """
    url = e.get("source_url", "")
    domain = _domain_of(url)
    is_news = any(hint in domain for hint in _NEWS_OR_REFERENCE_HINTS)
    return (0 if is_news else 1, 1 if _is_landing_page(url) else 0)


def _rank_and_cap(evidence: list[dict], cap: int) -> list[dict]:
    """Keep at most `cap` records with a bucketed quota that reserves slots for
    the two high-value signal classes a plain news/landing sort used to drop.

    The old sort keyed only on (is_news, is_landing), ignoring source weight and
    match_confidence, so the cap systematically discarded the highest-value
    connector evidence and the adversarial-query web evidence in favor of
    generic corroboration web hits, defeating the source weighting that _dedup
    was written to preserve. Instead of a pure global re-sort by weight (which
    would just trade the dropped connector signal for a dropped adversarial one,
    since adversarial hits are collected AFTER corroboration hits and a stable
    sort would cut them), this reserves guaranteed slots per class:
      1. up to _CONNECTOR_RESERVE weighted high/medium-confidence connector
         records,
      2. up to _ADVERSARIAL_RESERVE adversarial/footprint web records,
      3. the remainder filled by the original news/landing ranking, with any
         leftover connector/adversarial records ranked ahead of generic hits.
    Bare landing pages, being last in _news_landing_key, are the first thing
    dropped when the cap bites, so connectors never compete with homepage hits.
    """
    if len(evidence) <= cap:
        # Nothing is dropped, so no class can be starved; keep the original
        # news/landing ordering for a stable, deterministic result.
        return sorted(evidence, key=_news_landing_key)

    connector_hm = [e for e in evidence if _is_weighted_high_medium(e)]
    protected_web = [e for e in evidence if _is_protected_web(e)]
    reserved_ids = {id(e) for e in connector_hm} | {id(e) for e in protected_web}
    remainder = sorted(
        (e for e in evidence if id(e) not in reserved_ids), key=_news_landing_key
    )

    conn_quota = min(len(connector_hm), _CONNECTOR_RESERVE)
    adv_quota = min(len(protected_web), _ADVERSARIAL_RESERVE)

    result: list[dict] = []
    result.extend(connector_hm[:conn_quota])
    result.extend(protected_web[:adv_quota])

    # Fill the rest: leftover high-value records first (extra connector, then
    # extra adversarial/footprint), then the news/landing-ranked remainder.
    fill_pool = connector_hm[conn_quota:] + protected_web[adv_quota:] + remainder
    for e in fill_pool:
        if len(result) >= cap:
            break
        result.append(e)
    return result[:cap]


def _disambiguator(identity: dict) -> str:
    """Best available anchor to distinguish this person from a same-named
    stranger. A multi-entity headline is not an organization and must never be
    copied into every claim query.
    """
    current_company = (identity.get("current_company") or "").strip()
    headline = (identity.get("headline") or "").strip()
    for candidate in (current_company, headline):
        if not candidate or len(candidate) > 80:
            continue
        if any(mark in candidate for mark in ("|", ",", ";")):
            continue
        if candidate.count("@") > 1:
            continue
        return candidate
    return ""


def _identity_queries(person: str, disambiguator: str) -> list[tuple[str, str]]:
    # Bare "{name}" alone is exactly what pulls in a same-named stranger; only
    # fall back to it when there is truly no anchor available.
    if disambiguator:
        return [(f'"{person}" {disambiguator}', _ROLE_CORROBORATION)]
    return [(f'"{person}"', _ROLE_CORROBORATION)]


def _role_query_phrases(title: str) -> list[str]:
    """Preserve natural title facets instead of deleting meaningful joiners."""
    cleaned = " ".join((title or "").split()).strip()
    if not cleaned:
        return []
    facets = [
        " ".join(part.split()).strip(" ,")
        for part in re.split(r"\s*(?:&|/|\band\b)\s*", cleaned, flags=re.IGNORECASE)
    ]
    facets = [facet for facet in facets if facet]
    return facets[:2] or [cleaned]


def _employment_queries(person: str, claim: Claim, disambiguator: str) -> list[tuple[str, str]]:
    """Build a corroboration query (claim-specific) and an adversarial query
    (disproving-evidence-specific) for one employment or education claim.
    """
    employer = (claim.employer or "").strip()
    title = (claim.title or "").strip()

    aliases = _org_aliases(employer) or [employer]
    primary_org = aliases[1] if len(aliases) > 1 else aliases[0]
    queries: list[tuple[str, str]] = []
    role_phrases = _role_query_phrases(title)
    for phrase in role_phrases:
        queries.append(
            (
                f'"{person}" "{primary_org}" "{phrase}"'.strip(),
                _ROLE_CORROBORATION,
            )
        )

    compact_org = aliases[-1]
    compact_role = role_phrases[0] if role_phrases else title
    compact_tokens = _query_tokens(compact_role)
    if "software" in compact_tokens and "engineering" in compact_tokens:
        compact_role = "SDE " + (
            "intern" if "intern" in compact_tokens else "engineer"
        )
    elif "academy" in compact_tokens and "mathematics" in compact_tokens:
        compact_role = "AMP"
    elif claim.type == "education" and "computer" in compact_tokens:
        compact_role = "Computer Science"

    compact_query = f'"{person}" "{compact_org}" {compact_role}'.strip()
    if len(queries) < 2 and (not queries or compact_query != queries[0][0]):
        queries.append((compact_query, _ROLE_CORROBORATION))

    high_public_role = any(
        token in _query_tokens(title)
        for token in {
            "founder", "cofounder", "ceo", "cto", "cfo", "coo", "president",
            "director", "partner", "principal", "head", "vice",
        }
    )
    if employer and high_public_role:
        queries.append((f'"{person}" {employer} {_ADVERSARIAL_HINT}', _ROLE_ADVERSARIAL))

    return queries[:3]


def _fallback_queries(claim: Claim, person: str) -> list[tuple[str, str]]:
    if not claim.assertion:
        return []
    if person:
        return [(f'"{person}" {claim.assertion}', _ROLE_CORROBORATION)]
    return [(claim.assertion, _ROLE_CORROBORATION)]


# ---------------------------------------------------------------------------
# Company/app claim queries (user_count, revenue_metric, proprietary_tech,
# funding, pricing). Each claim carries the product name in claim.employer
# (mechanical_decompose_company sets this), so these builders key off that
# rather than the person-scan `identity` dict.
# ---------------------------------------------------------------------------

_THIN_WRAPPER_ADVERSARIAL_HINT = (
    '(fake users OR inflated OR "no evidence" OR fraud OR scam OR exaggerated)'
)


def _company_metric_queries(product: str, claim: Claim) -> list[tuple[str, str]]:
    """Corroboration + footprint (plausibility signal, never a verdict) +
    adversarial queries for a user_count or revenue_metric claim.

    The footprint query gathers evidence about the product's actual social /
    app-store presence so the reasoning provider can judge whether a huge
    claimed user count matches a tiny public footprint. This module only
    gathers that evidence; it never computes a plausibility score or sets a
    tier itself.
    """
    anchor = f'"{product}"' if product else ""
    corroboration = f"{anchor} {claim.assertion}".strip()
    footprint = f"{anchor} (app store reviews OR twitter followers OR reddit mentions)".strip()
    adversarial = f"{anchor} {_THIN_WRAPPER_ADVERSARIAL_HINT}".strip()
    tagged = [
        (corroboration, _ROLE_CORROBORATION),
        (footprint, _ROLE_FOOTPRINT),
        (adversarial, _ROLE_ADVERSARIAL),
    ]
    return [(q, role) for q, role in tagged if q][:3]


def _proprietary_tech_queries(product: str, claim: Claim) -> list[tuple[str, str]]:
    """Queries aimed at surfacing evidence that a loud "proprietary AI /
    proprietary tech" claim is hollow. Two DISTINCT LARP shapes are probed,
    because they leave very different search fingerprints and one query cannot
    catch both:
      1. THIN WRAPPER: the "proprietary AI" is really just an API call to an
         existing model (built on OpenAI/Claude/GPT) or a no-code builder.
      2. WIZARD OF OZ: the "AI" is actually humans behind the curtain
         (outsourced engineers, manual review of every transaction) marketed
         as automation, e.g. Builder.ai's "Natasha" (largely human engineers)
         or Amazon Just Walk Out (workers reviewing video). The wrapper query
         never surfaces this shape: a humans-pretending-to-be-AI story does
         not contain "built on OpenAI", it contains exposE language like
         "actually humans" / "human engineers" / "AI washing". This second
         adversarial query gathers exactly that so the reasoning provider can
         see the human-behind-the-curtain evidence and mark proprietary_ai_gap
         high. It only GATHERS evidence; it never sets a tier or a verdict.
    """
    anchor = f'"{product}"' if product else ""
    wrapper_check = (
        f"{anchor} built on (OpenAI OR Claude OR GPT OR Gemini) OR wrapper OR no-code"
    ).strip()
    wizard_of_oz_check = (
        f'{anchor} ("actually humans" OR "human engineers" OR "wizard of oz" '
        f'OR outsourced OR "not really AI" OR "fake AI" OR "AI washing")'
    ).strip()
    corroboration = f"{anchor} {claim.assertion}".strip()
    tagged = [
        (wrapper_check, _ROLE_ADVERSARIAL),
        (wizard_of_oz_check, _ROLE_ADVERSARIAL),
        (corroboration, _ROLE_CORROBORATION),
    ]
    return [(q, role) for q, role in tagged if q][:3]


def _funding_queries(product: str) -> list[tuple[str, str]]:
    anchor = f'"{product}"' if product else ""
    q = f"{anchor} (raised OR funding OR seed OR series a OR series b) million".strip()
    return [(q, _ROLE_CORROBORATION)] if q else []


def _pricing_queries(product: str, claim: Claim) -> list[tuple[str, str]]:
    anchor = f'"{product}"' if product else ""
    pricing = f"{anchor} pricing {claim.title}".strip()
    complaints = f"{anchor} (overpriced OR \"price increase\" OR \"hidden fees\" OR complaints)".strip()
    tagged = [(pricing, _ROLE_CORROBORATION), (complaints, _ROLE_ADVERSARIAL)]
    return [(q, role) for q, role in tagged if q][:2]


def _headcount_queries(product: str) -> list[tuple[str, str]]:
    """Best-effort team-size corroboration for the headcount_inflation
    metric. The code has no company-LinkedIn member-count fetch yet, so this
    is a plain web search only; see llm.py's company operator instructions
    for the PARTIAL-not-DISPROVEN handling when nothing turns up.
    """
    anchor = f'"{product}"' if product else ""
    q = f"{anchor} (team size OR employees OR headcount OR \"how many employees\" OR LinkedIn company page)".strip()
    return [(q, _ROLE_CORROBORATION)] if q else []


def _company_overview_queries(product: str) -> list[tuple[str, str]]:
    """Product-liveness/app-store-footprint, recency, and technical-team
    queries for the once-per-profile company_overview claim. Backs the
    product_realness, zombie_liveness, and key_role_coverage company-LARP
    metrics, none of which are tied to one specific pricing/metric/tech
    claim (see llm.mechanical_decompose_company).
    """
    anchor = f'"{product}"' if product else ""
    liveness = f"{anchor} (waitlist OR \"coming soon\" OR demo video OR beta OR app store reviews)".strip()
    recency = f"{anchor} (last updated OR \"this week\" OR \"this month\" OR latest news OR recently launched OR shut down)".strip()
    technical_team = f"{anchor} (co-founder CTO OR lead engineer OR chief scientist OR ML engineer)".strip()
    # liveness/recency are footprint/liveness signals (product_realness,
    # zombie_liveness); the technical-team query is a plain corroboration lookup.
    tagged = [
        (liveness, _ROLE_FOOTPRINT),
        (recency, _ROLE_FOOTPRINT),
        (technical_team, _ROLE_CORROBORATION),
    ]
    return [(q, role) for q, role in tagged if q][:3]


def _is_funding_or_metric_claim(claim: Claim) -> bool:
    text = f"{claim.title} {claim.assertion}".lower()
    return any(kw in text for kw in _FUNDING_OR_METRIC_KEYWORDS)


def _gather_pitchbook_evidence(
    claim: Claim, person: str, pb_budget: Optional["pitchbook.PitchBookBudget"]
) -> list[dict]:
    """Additional, strictly additive evidence from PitchBook, gated hard.

    Called for employment claims with a non-empty employer (never education
    or identity, per the task requirement) AND for company-scan "funding"
    claims (also employer-gated). Splits further:
      - funding/exit/metric-flavored employment claims, or any "funding"
        claim (company scan)        -> verify_company (existence + funding
        facts PitchBook is strong on and web search is weak on).
      - plain role/founder claims                -> verify_person_role
        (is this person actually listed at this company in that capacity).
    The per-profile pb_budget cap (shared across every claim in the run) is
    unchanged: this only adds one more claim type that can spend from it,
    never a second budget.
    Never raises: pitchbook.py swallows every internal error and returns [].
    """
    if claim.type not in ("employment", "funding") or not (claim.employer or "").strip():
        return []
    if not pitchbook.is_enabled():
        return []

    if claim.type == "funding":
        return pitchbook.verify_company(claim.employer, budget=pb_budget)
    if _is_funding_or_metric_claim(claim):
        return pitchbook.verify_company(claim.employer, budget=pb_budget)
    if person:
        return pitchbook.verify_person_role(person, claim.employer, budget=pb_budget)
    return []


# ---------------------------------------------------------------------------
# Independent source connectors (detective/sources/): additive, gated, and
# never allowed to raise out of gather_evidence. Each helper below decides
# whether IT has what it needs for THIS claim; verify.py otherwise leaves
# the connector alone rather than issuing a call it cannot ground.
# ---------------------------------------------------------------------------

_TECHNICAL_OR_FOUNDER_TITLE_KEYWORDS = (
    "founder",
    "cto",
    "chief technology",
    "engineer",
    "developer",
    "technical",
    "architect",
)


def _looks_technical_or_founder(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in _TECHNICAL_OR_FOUNDER_TITLE_KEYWORDS)


def _gather_github_evidence(claim: Claim, person: str, identity: dict) -> list[dict]:
    """GitHub is additive, gated to founder/technical claims about a named
    person: the once-per-profile identity claim, or an employment claim
    whose title reads as founder/technical. Never fires without a person
    name; GitHub's user search has no way to search on a bare company.
    """
    if not person:
        return []
    if claim.type == "identity":
        company_hint = _disambiguator(identity)
    elif claim.type == "employment" and _looks_technical_or_founder(claim.title):
        company_hint = (claim.employer or "").strip() or _disambiguator(identity)
    else:
        return []
    try:
        return github_source.verify_github(
            person, company=company_hint, hints=(identity.get("hints") or {})
        )
    except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
        logger.warning("verify: github source unavailable: %s", exc)
        return []


def _gather_sec_evidence(claim: Claim) -> list[dict]:
    """SEC EDGAR Form D is additive, gated to funding-flavored claims with a
    named employer/company: a company-scan "funding" claim, or a
    person-scan employment claim that reads funding/metric flavored (see
    _is_funding_or_metric_claim). Needs claim.employer; a claim with no
    employer has nothing to search SEC's full-text index for.
    """
    employer = (claim.employer or "").strip()
    if not employer:
        return []
    if claim.type != "funding" and not (
        claim.type == "employment" and _is_funding_or_metric_claim(claim)
    ):
        return []
    try:
        return sec_edgar_source.verify_sec(employer, claimed_amount=claim.assertion)
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: sec_edgar source unavailable: %s", exc)
        return []


def _checkable_site_url(claim: Claim, company_url: Optional[str]) -> str:
    """The URL this claim can actually be checked against, or "".

    Two ways a claim gets one, and BOTH halves of the old gate had to widen
    together (a person-scan founder claim is type "employment", so handing it a
    URL alone would still have been rejected by the type check):

    - a company/app scan's own profile_url, for the once-per-profile
      company_overview claim (the original path, unchanged)
    - claim.product_url, set by the resolution stage when the brain decided
      WHICH site is the claimed product. This is what makes a web app a
      first-class product check on a PERSON scan instead of App-Store-or-nothing.

    A resolved product_url is only ever set at high/medium confidence (see
    llm.SiteResolution): an ambiguous resolution leaves it empty, so these
    connectors simply do not fire rather than being pointed at a namesake.

    An operator-supplied company_url WINS over a resolved one. It is the URL a
    human handed us for this exact company scan, so it is authoritative; a
    resolved URL is the brain's best read of a name. Precedence, not preference:
    a company scan must never end up fingerprinting whatever a name search
    turned up instead of the site it was pointed at.
    """
    if claim.type == "company_overview" and company_url:
        return company_url
    product_url = (getattr(claim, "product_url", "") or "").strip()
    if product_url and (
        claim.type in ("company_overview", "user_count", "revenue_metric")
        or (claim.type == "employment" and _looks_founder_flavored(claim))
    ):
        return product_url
    return ""


def _gather_site_history_evidence(claim: Claim, company_url: Optional[str]) -> list[dict]:
    """Wayback + domain age are additive, gated to a claim with a checkable site
    URL (see _checkable_site_url): the once-per-profile company_overview claim on
    a company scan, or any product/founder claim whose product site the
    resolution stage identified. No URL, no domain to check, no fire.
    """
    site_url = _checkable_site_url(claim, company_url)
    if not site_url:
        return []
    collected: list[dict] = []
    try:
        collected.extend(wayback_source.verify_wayback(site_url))
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: wayback source unavailable: %s", exc)
    try:
        domain = _domain_of(site_url)
        collected.extend(domain_age_source.verify_domain_age(domain))
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: domain_age source unavailable: %s", exc)
    return collected


# ---------------------------------------------------------------------------
# Batch 2 (tech/research-substance cluster): uspto, arxiv, openalex,
# packages. Same additive/gated/never-raise discipline as batch 1 above.
# ---------------------------------------------------------------------------

_PATENT_CLAIM_KEYWORDS = (
    "patent", "patented", "patents", "patent-pending", "patent pending",
    "invention", "inventor", "ip portfolio", "intellectual property",
)

_RESEARCH_CREDENTIAL_KEYWORDS = (
    "phd", "ph.d", "doctorate", "postdoc", "post-doc", "research scientist",
    "professor", "published a paper", "publication", "dissertation",
    "thesis", "peer-reviewed", "peer reviewed",
)

_PACKAGE_CLAIM_KEYWORDS = (
    "sdk", "open source", "open-source", "package", "library", "npm",
    "pypi", "pip install", "python package", "javascript library",
)


def _looks_patent_or_invention_flavored(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _PATENT_CLAIM_KEYWORDS)


def _looks_research_credential_flavored(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _RESEARCH_CREDENTIAL_KEYWORDS)


def _looks_package_flavored(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _PACKAGE_CLAIM_KEYWORDS)


def _gather_uspto_evidence(claim: Claim, person: str) -> list[dict]:
    """USPTO is additive, gated two ways:

    - a company-scan "proprietary_tech" claim: always checked (that claim
      type IS exactly what patent evidence backs or undercuts), searched by
      the company/product name as assignee/applicant (is_company=True).
    - a person-scan employment/education/identity claim whose own text
      reads patent-flavored (see _looks_patent_or_invention_flavored):
      searched by person name as inventor (is_company=False). Gated on the
      keyword check so a plain "Founder and CEO" claim does not spam a
      patent search that has nothing to do with it.
    """
    if claim.type == "proprietary_tech":
        company = (claim.employer or "").strip()
        if not company:
            return []
        try:
            return uspto_source.verify_uspto(company, is_company=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: uspto source unavailable: %s", exc)
            return []

    if person and claim.type in ("employment", "education", "identity"):
        text = f"{claim.title} {claim.assertion}"
        if _looks_patent_or_invention_flavored(text):
            try:
                return uspto_source.verify_uspto(person, is_company=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("verify: uspto source unavailable: %s", exc)
                return []
    return []


def _gather_arxiv_evidence(claim: Claim, person: str) -> list[dict]:
    """arXiv is additive, gated to a person-scan employment/education/
    identity claim whose own text reads research-credential-flavored (see
    _looks_research_credential_flavored). Never fires on a company-scan
    proprietary_tech claim: arXiv's au: search is a person-name lookup, and
    searching it on a bare product name would only manufacture noise (see
    this module's top-of-file docstring).
    """
    if not person or claim.type not in ("employment", "education", "identity"):
        return []
    text = f"{claim.title} {claim.assertion}"
    if not _looks_research_credential_flavored(text):
        return []
    try:
        return arxiv_source.verify_arxiv(person)
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: arxiv source unavailable: %s", exc)
        return []


def _gather_openalex_evidence(claim: Claim, person: str, identity: dict) -> list[dict]:
    """OpenAlex is additive, gated identically to arxiv (see
    _gather_arxiv_evidence): a person-scan research-credential-flavored
    claim. The person's current company/headline (the same disambiguator
    github.py uses) is passed as the institution hint so a matching
    affiliation can raise match_confidence out of "low".
    """
    if not person or claim.type not in ("employment", "education", "identity"):
        return []
    text = f"{claim.title} {claim.assertion}"
    if not _looks_research_credential_flavored(text):
        return []
    try:
        return openalex_source.verify_openalex(person, institution=_disambiguator(identity))
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: openalex source unavailable: %s", exc)
        return []


def _gather_packages_evidence(claim: Claim) -> list[dict]:
    """npm/PyPI is additive, gated to a proprietary_tech or company_overview
    claim whose own text references an SDK/package/library/open-source
    (see _looks_package_flavored). Needs claim.employer (the product name);
    a claim with no product name has nothing to look up in either registry.
    """
    if claim.type not in ("proprietary_tech", "company_overview"):
        return []
    product = (claim.employer or "").strip()
    if not product:
        return []
    text = f"{claim.title} {claim.assertion}"
    if not _looks_package_flavored(text):
        return []
    try:
        return packages_source.verify_packages(product)
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: packages source unavailable: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Batch 3 (final P0 batch): app_store, accelerators, hackernews. Same
# additive/gated/never-raise discipline as batches 1 and 2 above.
# ---------------------------------------------------------------------------

_ACCELERATOR_CLAIM_KEYWORDS = (
    "y combinator", "yc-backed", "yc backed", "ycombinator", "techstars",
    "500 startups", "accelerator", "incubator", "backed by",
)


def _looks_accelerator_flavored(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _ACCELERATOR_CLAIM_KEYWORDS)


_FOUNDER_ROLE_KEYWORDS = (
    "founder", "co-founder", "cofounder", "creator", "built", "ceo", "cto",
    "maker", "developer of", "i built", "author of",
)


def _looks_founder_flavored(claim: Claim) -> bool:
    """True when an employment claim is a founder/builder role, i.e. the
    employer name is plausibly a PRODUCT the person shipped and whose App
    Store traction is worth checking (Organize Campus by its founder, etc)."""
    text = f"{claim.title or ''} {claim.assertion or ''}".lower()
    return any(kw in text for kw in _FOUNDER_ROLE_KEYWORDS)


def _gather_app_store_evidence(claim: Claim) -> list[dict]:
    """App Store is additive, gated to a user_count / revenue_metric claim
    (traction) or the once-per-profile company_overview claim (product
    realness for a consumer app), OR a founder/builder employment claim whose
    employer is the product they shipped (person scans: check the founder's
    own app's real store traction). Needs claim.employer (the product name);
    a claim with no product name has nothing to look up on the App Store.
    """
    product_claim = claim.type in ("user_count", "revenue_metric", "company_overview")
    employment_company = (
        claim.type == "employment"
        and (
            _looks_founder_flavored(claim)
            or bool(getattr(claim, "_company_component_relevant", False))
        )
    )
    if not (product_claim or employment_company):
        return []
    product = (claim.employer or "").strip()
    if not product:
        return []
    try:
        return app_store_source.verify_app_store(product)
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: app_store source unavailable: %s", exc)
        return []


def _gather_accelerators_evidence(claim: Claim) -> list[dict]:
    """YC/Techstars is additive, gated two ways:

    - the once-per-profile company_overview claim: always checked, same as
      uspto's always-fire on proprietary_tech (this claim type is exactly
      what an accelerator badge backs or undercuts).
    - any OTHER claim whose own text reads accelerator/badge-flavored (see
      _looks_accelerator_flavored, e.g. an employment claim bragging
      "YC-backed founder"), gated on the keyword check so a plain role
      claim does not spam an accelerator-directory lookup that has nothing
      to do with it.

    Needs claim.employer (the company/product name) either way; "not
    listed" in either directory is never proof the company was not backed
    by some OTHER accelerator (see accelerators.py's own module docstring).
    """
    company = (claim.employer or "").strip()
    if not company:
        return []
    if claim.type == "company_overview":
        fire = True
    else:
        text = f"{claim.title} {claim.assertion}"
        fire = _looks_accelerator_flavored(text)
    if not fire:
        return []
    try:
        return accelerators_source.verify_accelerator(company)
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: accelerators source unavailable: %s", exc)
        return []


def _gather_hackernews_evidence(claim: Claim, person: str, identity: dict) -> list[dict]:
    """Hacker News is additive, gated two ways:

    - the once-per-profile company_overview claim: searched by product name
      (claim.employer), no person hint.
    - a person-scan identity claim: searched only by a concrete current-company
      anchor. A generic headline is not a product identifier, and a legal name
      is not an HN username, so neither is sent to this connector.

    Needs a non-empty product or current-company query. Without one, this source
    is skipped rather than manufacturing generic topic matches.
    """
    if claim.type == "company_overview":
        product = (claim.employer or "").strip()
        if not product:
            return []
        try:
            return hackernews_source.verify_hackernews(product)
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: hackernews source unavailable: %s", exc)
            return []

    if claim.type == "identity" and person:
        query = (identity.get("current_company") or "").strip()
        if not query:
            return []
        try:
            return hackernews_source.verify_hackernews(query, person=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: hackernews source unavailable: %s", exc)
            return []

    return []


# ---------------------------------------------------------------------------
# Batch 4 (first P1 batch): techstack, courtlistener. Same additive/gated/
# never-raise discipline as batches 1 through 3 above.
# ---------------------------------------------------------------------------


def _gather_techstack_evidence(claim: Claim, company_url: Optional[str]) -> list[dict]:
    """Tech-stack fingerprint is additive, gated to the once-per-profile
    company_overview claim on a company scan, OR a product/founder claim whose
    product site the resolution stage identified (see _checkable_site_url), the
    same gate _gather_site_history_evidence uses for wayback / domain_age. No
    URL, no page to fetch. Deliberately NOT also fired on every proprietary_tech
    claim: see this module's top-of-file docstring for why (the fingerprint is a
    property of the URL, not of any one claim, and refiring would just
    refetch the same page for no new signal).
    """
    site_url = _checkable_site_url(claim, company_url)
    if not site_url:
        return []
    try:
        ledger = ledger_for(claim)
        if ledger is None:
            return techstack_source.verify_techstack(site_url)
        return techstack_source.verify_techstack(
            site_url,
            attempt_ledger=ledger,
            claim_index=getattr(claim, "_claim_index", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify: techstack source unavailable: %s", exc)
        return []


def _gather_courtlistener_evidence(claim: Claim, person: str) -> list[dict]:
    """CourtListener is additive, gated two ways:

    - a person-scan identity claim: searched by the person's own name,
      is_company=False.
    - the once-per-profile company_overview claim (company scans): searched
      by the company/product name, is_company=True, an adverse-record check.

    Needs a person name or a company name (claim.employer) respectively;
    absent either, there is nothing to search CourtListener's full-text
    index for. See courtlistener.py's own module docstring: this is the
    highest same-name false-positive risk of any source in this registry,
    so match_confidence never reaches "high" here regardless of gate.
    """
    if claim.type == "identity" and person:
        try:
            return courtlistener_source.verify_courtlistener(person, is_company=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: courtlistener source unavailable: %s", exc)
            return []

    if claim.type == "company_overview":
        company = (claim.employer or "").strip()
        if not company:
            return []
        try:
            return courtlistener_source.verify_courtlistener(company, is_company=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: courtlistener source unavailable: %s", exc)
            return []

    return []


# ---------------------------------------------------------------------------
# Batch 5 (entity-targeted sources): org_roster, news. Same additive/gated/
# never-raise discipline as batches 1 through 4 above. Both connectors call
# the shared web_search primitive (unlike the API-backed connectors above,
# which each hit a distinct host); they run inside the same connector thread
# pool, so on an employment claim (the one claim type that fires BOTH) two
# web_search calls can race search.py's process-global Brave cooldown. That is
# not a crash (a float timestamp under the GIL, and SearXNG has no cooldown)
# and degrades to [] on any error, but it does bend the "no single host sees
# two concurrent requests per claim" note in gather_evidence; documented here
# honestly rather than papered over.
# ---------------------------------------------------------------------------


def _gather_org_roster_evidence(claim: Claim, person: str) -> list[dict]:
    """Org-roster is additive, gated to an employment or education claim with
    BOTH a named person and a named org (claim.employer holds the employer or
    school). Checks whether the person's name appears on the org's own public
    team/members/roster page: a match is real third-party corroboration, and a
    non-match is a documented ABSENCE, never disproof (see org_roster.py).
    Needs both a person name and an org; absent either there is nothing to
    look up.
    """
    if claim.type not in ("employment", "education"):
        return []
    org = (claim.employer or "").strip()
    if not person or not org:
        return []
    public_role_tokens = {
        "founder", "cofounder", "ceo", "cto", "cfo", "coo", "president",
        "director", "partner", "principal", "head", "professor", "researcher",
    }
    if claim.type != "employment" or not (
        _query_tokens(claim.title) & public_role_tokens
    ):
        return []
    try:
        return org_roster_source.verify_org_roster(person, org)
    except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
        logger.warning("verify: org_roster source unavailable: %s", exc)
        return []


def _gather_news_evidence(claim: Claim, person: str) -> list[dict]:
    """News is additive, gated to subjects that news can identify:

    - a person-scan identity claim: searched by the person's own name
      (is_company=False), press coverage of the person.
    - the once-per-profile company_overview claim (company scans): searched by
      the company/product name (is_company=True), press coverage of the company.
    Genuine third-party coverage corroborates footprint; a reprint of the
    subject's own press release does not (see news.py). Generic employer news is
    not gathered for an employment claim because it cannot bind the named
    person to the claimed role.
    """
    if claim.type == "identity" and person:
        try:
            return news_source.verify_news(person, is_company=False)
        except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
            logger.warning("verify: news source unavailable: %s", exc)
            return []

    if claim.type == "company_overview":
        company = (claim.employer or "").strip()
        if not company:
            return []
        try:
            return news_source.verify_news(company, is_company=True)
        except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
            logger.warning("verify: news source unavailable: %s", exc)
            return []

    return []


def _connector_applicable(
    name: str,
    claim: Claim,
    person: str,
    identity: dict,
    company_url: Optional[str],
) -> bool:
    """Mirror connector gates so the audit ledger distinguishes a skip."""
    employer = (claim.employer or "").strip()
    text = f"{claim.title} {claim.assertion}"
    if name == "github":
        return bool(
            person
            and (
                claim.type == "identity"
                or (
                    claim.type == "employment"
                    and _looks_technical_or_founder(claim.title)
                )
            )
        )
    if name == "sec_edgar":
        return bool(
            employer
            and (
                claim.type == "funding"
                or (
                    claim.type == "employment"
                    and _is_funding_or_metric_claim(claim)
                )
            )
        )
    if name in {"site_history", "techstack"}:
        return bool(_checkable_site_url(claim, company_url))
    if name == "uspto":
        return bool(
            (claim.type == "proprietary_tech" and employer)
            or (
                person
                and claim.type in ("employment", "education", "identity")
                and _looks_patent_or_invention_flavored(text)
            )
        )
    if name in {"arxiv", "openalex"}:
        return bool(
            person
            and claim.type in ("employment", "education", "identity")
            and _looks_research_credential_flavored(text)
        )
    if name == "packages":
        return bool(
            employer
            and claim.type in ("proprietary_tech", "company_overview")
            and _looks_package_flavored(text)
        )
    if name == "app_store":
        return bool(
            employer
            and (
                claim.type in ("user_count", "revenue_metric", "company_overview")
                or (
                    claim.type == "employment"
                    and (
                        _looks_founder_flavored(claim)
                        or bool(
                            getattr(claim, "_company_component_relevant", False)
                        )
                    )
                )
            )
        )
    if name == "accelerators":
        return bool(
            employer
            and (
                claim.type == "company_overview"
                or _looks_accelerator_flavored(text)
            )
        )
    if name == "hackernews":
        return bool(
            (claim.type == "company_overview" and employer)
            or (
                claim.type == "identity"
                and person
                and (identity.get("current_company") or "").strip()
            )
        )
    if name == "courtlistener":
        return bool(
            (claim.type == "identity" and person)
            or (claim.type == "company_overview" and employer)
        )
    if name == "org_roster":
        public_role_tokens = {
            "founder", "cofounder", "ceo", "cto", "cfo", "coo", "president",
            "director", "partner", "principal", "head", "professor", "researcher",
        }
        return bool(
            person
            and employer
            and claim.type == "employment"
            and (_query_tokens(claim.title) & public_role_tokens)
        )
    if name == "news":
        return bool(
            (claim.type == "identity" and person)
            or (claim.type == "company_overview" and employer)
        )
    return False


def _search_coverage_record(
    claim: Claim,
    *,
    raw_count: int,
    relevant_count: int,
    query_count: int,
) -> dict:
    """Persist claim-level retrieval accounting without pretending it is proof."""
    binding = (
        "role"
        if claim.type in ("employment", "education")
        else "identity" if claim.type == "identity" else "claim"
    )
    return {
        "source_url": "internal://search/coverage",
        "snippet": (
            f"Targeted claim search completed: {query_count} query or queries, "
            f"{raw_count} raw result(s), {relevant_count} claim-relevant result(s). "
            "Raw result count is not corroboration. Only retained evidence that "
            "binds the named subject to this claim may support a verdict."
        ),
        "source_name": "search_coverage",
        "weight": 0.0,
        "match_confidence": "low",
        "verification_state": "completed",
        "binding": binding,
        "raw_count": max(0, int(raw_count)),
        "relevant_count": max(0, int(relevant_count)),
        "query_count": max(0, int(query_count)),
    }


def gather_evidence(
    claim: Claim,
    identity: Optional[dict] = None,
    pb_budget: Optional["pitchbook.PitchBookBudget"] = None,
    company_url: Optional[str] = None,
    *,
    max_evidence: int = _MAX_EVIDENCE_PER_CLAIM,
) -> Claim:
    """Populate claim.evidence for one claim. Mutates and returns the claim.

    Does NOT touch claim.tier. The evidence set is what the provider reasons over.

    max_evidence: the per-claim record cap applied after dedup/rank. Defaults
    to _MAX_EVIDENCE_PER_CLAIM (8), the value the per-claim pipeline.run path
    has always used, so existing callers and tests are unchanged. The
    aggregate-then-mismatch path (detective.dossier.build_dossier) raises this
    to gather a BROADER picture per subject in one pass; the cap still exists so
    gathering stays bounded (never unbounded), it is just wider there.

    pb_budget: an optional PitchBookBudget shared across every claim in one
    profile (created once by pipeline.run). PitchBook is only ever consulted
    when PITCHBOOK_ENABLED is set; when it is, this still costs at most
    pb_budget.max_lookups real PitchBook calls for the whole profile, and
    PitchBook evidence is appended alongside web evidence, never replacing it.

    company_url: the company/app scan's own profile_url (unset for a person
    scan), threaded in by pipeline.run so the wayback / domain_age connectors
    have a domain to check for the company_overview claim. See
    _gather_site_history_evidence.
    """
    identity = identity or {}
    person = (identity.get("name") or "").strip()
    disambiguator = _disambiguator(identity)
    ledger = ledger_for(claim)
    claim_index = getattr(claim, "_claim_index", None)

    if claim.type in ("employment", "education") and claim.employer:
        if person:
            queries = _employment_queries(person, claim, disambiguator)
        else:
            queries = [(f'"{claim.employer}" {claim.title}'.strip(), _ROLE_CORROBORATION)]
    elif claim.type == "identity" and person:
        queries = _identity_queries(person, disambiguator)
    elif claim.type in ("user_count", "revenue_metric"):
        queries = _company_metric_queries(claim.employer, claim)
    elif claim.type == "proprietary_tech":
        queries = _proprietary_tech_queries(claim.employer, claim)
    elif claim.type == "funding":
        queries = _funding_queries(claim.employer)
    elif claim.type == "pricing":
        queries = _pricing_queries(claim.employer, claim)
    elif claim.type == "headcount":
        queries = _headcount_queries(claim.employer)
    elif claim.type == "company_overview":
        queries = _company_overview_queries(claim.employer)
    else:
        queries = _fallback_queries(claim, person)

    collected: list[dict] = []
    web_evidence_count = 0
    web_raw_count = 0
    completed_query_count = 0
    # Web search stays serial: SearXNG/Brave share ONE rate-limited backend
    # and Brave sets a process-global cooldown on 429/402 (see search.py), so
    # concurrent web queries would race that shared state, not gain throughput.
    for q, role in queries:
        q = _cap_query_length(q)
        if not q:
            continue
        attempt = (
            ledger.attempt(
                "evidence",
                "web_search",
                claim_index=claim_index,
                query=q,
                metadata={"query_role": role},
            )
            if ledger
            else None
        )
        try:
            public_role = bool(
                claim.type == "employment"
                and (
                    _query_tokens(claim.title)
                    & {
                        "founder", "cofounder", "ceo", "cto", "cfo", "coo",
                        "president", "director", "partner", "principal",
                        "head", "chief", "vice", "vp",
                    }
                )
            )
            raw_results = web_search(q, count=10 if public_role else 5)
            healthy = search_backend.search_healthy()
            records = _to_evidence(
                raw_results,
                role,
                claim=claim,
                person=person,
                disambiguator=disambiguator,
            )
            web_raw_count += len(raw_results)
            if healthy:
                completed_query_count += 1
            if attempt:
                attempt.finish(
                    "completed" if records else (
                        "completed_empty" if healthy else "unavailable"
                    ),
                    result_count=len(records),
                    metadata={
                        "raw_count": len(raw_results),
                        "relevant_count": len(records),
                        "rejected_count": max(0, len(raw_results) - len(records)),
                    },
                )
        except Exception as exc:
            if attempt:
                attempt.finish("error", error=f"{type(exc).__name__}: {exc}")
            raise
        web_evidence_count += len(records)
        collected.extend(records)

    if completed_query_count and claim.type in ("identity", "employment", "education"):
        collected.append(
            _search_coverage_record(
                claim,
                raw_count=web_raw_count,
                relevant_count=web_evidence_count,
                query_count=completed_query_count,
            )
        )

    # Private, per-run state consumed by dossier._aggregate. A connector may
    # still return a product or domain record while every claim-specific web
    # query was blocked by an outage. Preserve those useful records, but carry
    # the lookup failure separately so they cannot masquerade as a completed
    # role search. This attribute is intentionally not serialized by Claim.
    claim._web_search_unavailable = bool(queries and completed_query_count == 0)

    # PitchBook is additive only: appended after web evidence, gated to
    # high-value claim shapes, and capped by pb_budget. Disabled by default;
    # any failure (missing auth, 402, network error) yields [] silently.
    # Stays serial (shares the mutable pb_budget) and is wrapped defensively so
    # an unexpected raise cannot break evidence gathering for the whole claim,
    # matching every free connector's own never-raise contract below.
    pb_attempt = (
        ledger.attempt(
            "evidence",
            "pitchbook",
            claim_index=claim_index,
            target=(claim.employer or person),
        )
        if ledger and pb_budget is not None
        else None
    )
    try:
        pb_evidence = _gather_pitchbook_evidence(claim, person, pb_budget)
        if pb_attempt:
            pb_attempt.finish(
                "completed" if pb_evidence else "completed_empty",
                result_count=len(pb_evidence),
            )
    except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
        logger.warning("verify: pitchbook source unavailable: %s", exc)
        pb_evidence = []
        if pb_attempt:
            pb_attempt.finish("error", error=f"{type(exc).__name__}: {exc}")
    if pb_evidence:
        collected.extend(pb_evidence)

    # Free, additive, gated independent-source connectors (see
    # detective/sources/registry.py for the weighted source table). Each hits a
    # DISTINCT host, so they run CONCURRENTLY in a bounded thread pool. Results
    # are reassembled in the SAME fixed order as the serial version below, so
    # the pre-dedup `collected` list is byte-identical regardless of completion
    # order and scores are fully deterministic. Each _gather_* helper already
    # degrades to [] on failure; the try/except around future.result() is
    # belt-and-suspenders for an unexpected raise inside the pool itself.
    connector_calls = [
        ("github", lambda: _gather_github_evidence(claim, person, identity)),
        ("sec_edgar", lambda: _gather_sec_evidence(claim)),
        ("site_history", lambda: _gather_site_history_evidence(claim, company_url)),
        ("uspto", lambda: _gather_uspto_evidence(claim, person)),
        ("arxiv", lambda: _gather_arxiv_evidence(claim, person)),
        ("openalex", lambda: _gather_openalex_evidence(claim, person, identity)),
        ("packages", lambda: _gather_packages_evidence(claim)),
        ("app_store", lambda: _gather_app_store_evidence(claim)),
        ("accelerators", lambda: _gather_accelerators_evidence(claim)),
        ("hackernews", lambda: _gather_hackernews_evidence(claim, person, identity)),
        ("techstack", lambda: _gather_techstack_evidence(claim, company_url)),
        ("courtlistener", lambda: _gather_courtlistener_evidence(claim, person)),
        ("org_roster", lambda: _gather_org_roster_evidence(claim, person)),
        ("news", lambda: _gather_news_evidence(claim, person)),
    ]

    def run_connector(name, fn):
        attempt = (
            ledger.attempt(
                "evidence",
                name,
                claim_index=claim_index,
                target=(claim.product_url or claim.employer or person),
            )
            if ledger
            else None
        )
        applicable = _connector_applicable(
            name, claim, person, identity, company_url
        )
        if not applicable:
            if attempt:
                attempt.finish("not_applicable")
            return []
        try:
            records = fn() or []
            if attempt:
                attempt.finish(
                    "completed" if records else "no_evidence_or_unavailable",
                    result_count=len(records),
                )
            return records
        except Exception as exc:
            if attempt:
                attempt.finish("error", error=f"{type(exc).__name__}: {exc}")
            raise

    with ThreadPoolExecutor(
        max_workers=min(_CONNECTOR_MAX_WORKERS, len(connector_calls))
    ) as executor:
        futures = [
            executor.submit(run_connector, name, fn)
            for name, fn in connector_calls
        ]
        for fut in futures:
            try:
                collected.extend(fut.result() or [])
            except Exception as exc:  # noqa: BLE001 - a source must never break the pipeline
                logger.warning("verify: connector raised in pool: %s", exc)

    claim.evidence = _rank_and_cap(_dedup(collected), max_evidence)
    logger.info(
        "claim %r: issued %d quer(ies) -> %d evidence record(s) (capped at %d)",
        claim.assertion or claim.type,
        len(queries),
        len(claim.evidence),
        max_evidence,
    )
    return claim
