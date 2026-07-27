"""Aggregate-then-mismatch detection path (the A/B alternative to pipeline.run).

Design in one paragraph. The existing engine (detective.pipeline.run) verifies
each decomposed claim SERIALLY: one claim, a targeted gather, a tier. This
module instead gathers a BROAD, bounded, parallel dossier of the whole subject
in one pass, then cross-references the CLAIMED facts against the DISCOVERED
evidence to surface three kinds of mismatch:

    CONTRADICTION : claimed X, the evidence actively says not-X (the DISPROVEN
                    driver: adverse findings such as "no record of", "admitted
                    fabricating", "did not attend").
    INFLATION     : a claimed NUMBER far exceeds a DISCOVERED reality (the
                    generalized App-Store "50k users vs 12 ratings" check, and
                    "$10M raised" vs a Form D showing a tiny raise).
    GAP           : a claimed NOTABLE thing with zero corroborating trace where
                    one would be expected (SUS only, never DISPROVEN: absence is
                    not a lie, same discipline the whole engine already keeps).
                    Corroboration of ANY kind suppresses it, including a strong
                    web/news snippet that talks about the claim's subject, not
                    only structured-connector hits.
    AUTONOMY      : a claimed-autonomy / proprietary-AI assertion contradicted
                    by humans-in-the-loop evidence anywhere in the dossier (the
                    AI-washing / wizard-of-oz class: Amazon Just Walk Out,
                    Builder.ai). A REAL contradiction shape, so it may
                    legitimately resolve to DISPROVEN, which routes the company
                    score into the top band.

    Plus a TIMELINE consistency pass (overlapping full-time roles, a founding
    year that predates the domain registration, a credential that postdates the
    role it enabled).

Crucial reuse decision (so this is not a fork). The mismatch detectors are
MECHANICAL and never set a tier; they only produce CANDIDATES. Each candidate
is injected onto the relevant claim(s) as one clearly-labeled synthetic
evidence record (source_name "mismatch_*"), so it flows into the SAME
provider.assign_tiers_and_verdict reasoning step the current engine already
uses (ManualProvider operator OR ApiProvider/Gemini, unchanged), and into the
SAME code-computed scorers (llm.compute_founder_score / compute_company_score,
unchanged). The provider's tiers ARE the contradiction/gap emission, and every
defamation guard baked into those instructions and scorers carries over for
free: DISPROVEN still needs a real contradiction, GAP/absence still cannot
reach the top band, and the verdict tone rules still forbid the liar/fraud
vocabulary without a DISPROVEN claim. The genuinely new code here is the broad
parallel aggregation, the five mechanical detectors, and the typed-findings
surfacing; the brain and the score model are reused verbatim.

A/B parity. build_dossier(raw_profile, provider, ...) returns a scored Dossier
with the SAME score fields (founder_larp_score / company_larp_score), verdict,
and larp_score semantics as pipeline.run(url, provider, raw_profile=...), so a
harness can run BOTH on the same injected raw_profile with the same provider
and compare score + verdict directly. build_dossier deliberately has NO live
fetch path (it takes an already-fetched raw_profile only), so it can never
trip a live LinkedIn fetch; callers that need a fetch use pipeline's gated
fetchers and pass the result in.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .models import Buildability, Claim, Dossier, EvidenceTier
from .audit import AttemptLedger, ledger_for
from .retrieval_quality import claim_search_completed
from .llm import (
    FollowupQuery,
    LLMProvider,
    ManualProvider,
    _claim_has_confirmation_basis,
    build_metric_breakdown,
    compute_company_score,
    compute_founder_score,
    normalize_expected_footprints,
    sync_buildability_metric,
)
from . import verify
from . import search
from . import pitchbook
from .sources import product_site

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, object], None]

# ---------------------------------------------------------------------------
# Tunables. All bounded on purpose: the owner wants quick AND accurate, so we
# gather broadly but never infinitely (short worker pool, a wall-clock guard
# per claim, a wider-but-still-finite evidence cap).
# ---------------------------------------------------------------------------

# Claim-level parallelism. Small on purpose: web_search shares ONE
# rate-limited backend with a process-global cooldown (see search.py), and the
# ~12 per-claim connectors are already threaded inside gather_evidence, so a
# modest fan-out over claims is where the wall-clock win is without thrashing
# the shared search backend.
_DOSSIER_MAX_WORKERS = 4

# Wider than the per-claim pipeline cap of 8: a BROAD dossier. Still finite so
# gathering is bounded, not "gather forever".
_DOSSIER_MAX_EVIDENCE = 16

# Per-claim wall-clock guard. A claim whose whole gather (web + connectors)
# somehow exceeds this proceeds with whatever it has rather than hanging the
# run. Generous by default (connectors have their own short timeouts); it is
# the hard ceiling on "diminishing returns", not the common case.
_DOSSIER_PER_CLAIM_TIMEOUT_S = 30.0

# Director / planning pass bounds. The director proposes targeted follow-up
# web queries for thin CHECKABLE claims (see build_dossier stage 2.5); these
# cap it so it stays quick AND never runs away. All never-raise.
#   _DIRECTOR_MAX_FOLLOWUPS  : hard cap on how many follow-up queries run,
#                              regardless of how many a provider proposes.
#   _DIRECTOR_WALL_CLOCK_S   : total wall-clock ceiling across all follow-ups;
#                              once exceeded, remaining follow-ups are skipped.
#   _DIRECTOR_SEARCH_COUNT   : results requested per follow-up web query.
#   _DIRECTOR_RESULTS_PER_FOLLOWUP : how many of those results are attached as
#                              evidence (the top few; the rest are noise).
_DIRECTOR_MAX_FOLLOWUPS = 6
_DIRECTOR_WALL_CLOCK_S = 30.0
_DIRECTOR_SEARCH_COUNT = 10
_DIRECTOR_RESULTS_PER_FOLLOWUP = 3
_PUBLIC_ROLE_TOKENS = frozenset(
    {
        "founder", "cofounder", "ceo", "cto", "cfo", "coo", "president",
        "director", "partner", "principal", "head", "chief", "vice", "vp",
    }
)

# INFLATION fires only when the claimed number is at least this many times the
# discovered reality. Below it is normal startup rounding/optimism (a "100k"
# that is really 80k), which the whole engine is designed NOT to punish.
_INFLATION_MIN_RATIO = 10.0

# Severity scaling for an inflation: severity = min(1, log10(ratio) / K). With
# K = 4, a 10x gap is ~0.25, a 100x gap ~0.5, a 10,000x gap saturates at 1.0.
# Matches the company rubric's "score by the log gap" intent.
_INFLATION_SEVERITY_K = 4.0

# Claim types whose truthful version is high-footprint enough that a total
# absence of any corroborating trace is worth flagging as a GAP (SUS only).
# A junior/obscure/private role is intentionally NOT here: we never punish a
# legitimately low-footprint person for being hard to verify.
_GAP_NOTABLE_TYPES = frozenset(
    {
        "employment", "education", "company_overview", "funding", "identity",
        # A big managed-money claim (AUM/portfolio, e.g. "managed $4.8M") and a
        # named certification are BOTH the should-be-findable-if-real kind: a
        # junior person managing millions or holding a named credential leaves a
        # public trace when it is real, so an uncorroborated one is a GAP (SUS),
        # never a DISPROVEN. There is no public AUM registry to cross-check a
        # number against, so these ride the GAP/absence path, not inflation.
        "money_managed", "certification",
    }
)

# Numeric claim types the inflation detector inspects.
_NUMERIC_CLAIM_TYPES = frozenset(
    # money_managed is BOTH GAP-notable and (when a counter-number is actually
    # discovered) inflation-eligible: with no discovered figure it stays on the
    # absence path exactly as before, so absence never masquerades as a
    # measurement here.
    {"user_count", "revenue_metric", "funding", "headcount", "money_managed"}
)

# Adverse-finding phrases that mechanically mark a claim's evidence as carrying
# a CONTRADICTION signal. Mirrors the CORROBORATION DISCIPLINE wording in
# llm._SOURCE_WEIGHTING_INSTRUCTIONS. The detector only SURFACES these (as a
# candidate); the provider still makes the DISPROVEN call under the full
# discipline (never off one low-confidence hit, never off a bare absence).
_CONTRADICTION_PHRASES = (
    "no record of",
    "no record for",
    "did not attend",
    "never attended",
    "never enrolled",
    "never worked",
    "admitted lying",
    "admits to fabricating",
    "admitted fabricating",
    "admitted to fabricating",
    "convicted of",
    "pleaded guilty",
    "found to have fabricated",
    "fabricated the",
    "falsely claimed",
    "no such degree",
    "no such company",
    "does not exist",
    "was fabricated",
    "debunked",
)

# High-confidence connector source_names whose presence for the RIGHT claim
# type counts as a real corroborating trace (so its ABSENCE is what a GAP is).
_CORROBORATING_SOURCES = frozenset(
    {
        "github",
        "packages",
        # The org's OWN published roster listing the person: genuinely
        # role-speaking evidence (the mechanical half of the confirmation bar).
        # Its ABSENCE record is stamped match_confidence "low" by the connector
        # (org_roster._build_absence_record), so a documented absence can never
        # corroborate anything through this set.
        "org_roster",
        "sec_edgar_form_d",
        "wayback_machine",
        "uspto_patents_trademarks",
        "arxiv",
        "openalex",
        "app_store_play_store_reviews",
        "accelerator_badges",
        "domain_rdap_whois",
    }
)

# Tokens too generic to identify a claim subject on their own (legal suffixes,
# org-shaped nouns, utterance verbs). Used by the snippet-corroboration check
# so "University of Pennsylvania" matches on "pennsylvania", never "university".
_GENERIC_SUBJECT_TOKENS = frozenset(
    {
        "the", "and", "for", "with", "from", "inc", "llc", "ltd", "corp",
        "corporation", "company", "companies", "group", "global", "holdings",
        "university", "school", "college", "institute", "technologies",
        "technology", "systems", "system", "labs", "studio", "studios",
        "claimed", "said", "worked", "studied", "founder", "cofounder",
        "founded", "self", "employed",
    }
)

# Snippet words that mark a hit as speaking to a ROLE or IMPACT rather than
# mere co-occurrence. Broad on purpose: any one of these in the SAME snippet
# as the name and employer anchors suffices (news apposition like "Jane Doe,
# chief executive of Acme" always carries one), so real coverage of real
# people keeps clearing while "X and Y attended an event" stops clearing.
# Matched against the snippet's WORD TOKENS, never as bare substrings, so
# "led" cannot match inside "settled" and "president" cannot match "present".
# Additions to this set only ever LOOSEN corroboration (more things clear),
# so they can never newly accuse anyone; removals require re-running the
# principle suite (tests/test_judgment_principles.py).
_ROLE_EVIDENCE_TOKENS = frozenset({
    "founder", "cofounder", "co-founder", "founded", "ceo", "cto", "cfo",
    "coo", "chief", "president", "vice", "director", "head", "lead", "leads",
    "led", "principal", "partner", "officer", "manager", "engineer",
    "developer", "scientist", "researcher", "analyst", "intern", "professor",
    "joined", "joins", "hired", "appointed", "promoted", "serves", "served",
    "works", "worked", "working", "employee", "team", "staff", "role",
    "position", "alumni", "alumnus", "graduated", "graduate", "student",
    "degree", "enrolled", "class", "phd", "mba",
})

# Snippet words that mark a record as speaking to MANAGED MONEY or a real
# CREDENTIAL (the substance of a money_managed / certification claim, the way
# _FUNDING_CONTEXT_TOKENS is the substance of a funding claim). Multi-word
# entries are matched as phrases, single words as tokens.
_CREDENTIAL_CONTEXT_TOKENS = frozenset(
    {
        "aum", "under management", "assets", "certified", "charterholder",
        "license", "credential",
    }
)

# Snippet words that mark a record as actually talking about a FUNDING event
# (the funding-claim corroboration context; a bare company mention is not
# corroboration of a raise).
_FUNDING_CONTEXT_TOKENS = frozenset(
    {
        "raised", "raise", "funding", "round", "series", "seed", "million",
        "billion", "investor", "investors", "valuation", "venture",
    }
)

# Autonomy / proprietary-AI overstatement markers in a CLAIM's own assertion.
# This is the AI-washing / wizard-of-oz LARP class (Amazon Just Walk Out,
# Builder.ai): a loud "fully autonomous / no humans / our AI does it" pitch.
_AUTONOMY_MARKERS = (
    "fully autonomous",
    "fully automatic",
    "fully automatically",
    "no humans",
    "no human involvement",
    "without human",
    "without any human",
    "no cashiers",
    "no checkout",
    "zero human",
    "no manual",
    "automatically detect",
    "automatically detects",
    "automatically handle",
    "automatically handles",
    "our ai handles",
    "ai handles the rest",
    "ai does the rest",
    "end-to-end ai",
    "autonomously",
    "powered entirely by ai",
    "100% automated",
    "fully automated",
)

# Humans-in-the-loop exposE language in EVIDENCE snippets that contradicts a
# claimed-autonomy assertion. Mirrors the wizard-of-oz hardening already in
# verify._proprietary_tech_queries (which is what gathers this evidence).
# Deliberately exposE-shaped: generic AI discourse ("human fallback", "human
# drivers" said about some OTHER product) is NOT in this list, so a genuinely
# autonomous product surrounded by industry chatter does not trip it.
_HUMANS_IN_LOOP_MARKERS = (
    "actually humans",
    "actually human",
    "human engineers",
    "human workers",
    "human reviewers",
    "human contractors",
    "human operators",
    "human labelers",
    "humans behind",
    "humans in the loop",
    "humans review",
    "humans watching",
    "humans pretending",
    "relied on humans",
    "reviewed by humans",
    "powered by humans",
    "workers in india",
    "indian workers",
    "low-paid workers",
    "low-paid indian",
    "outsourced engineers",
    "outsourced to",
    "manual review of",
    "manually reviewed",
    "wizard of oz",
    "ai washing",
    "not really ai",
    "fake ai",
)


# ---------------------------------------------------------------------------
# Extraction depth: the honesty gate. A scored SUS verdict is only legitimate
# when the tool actually LOOKED, which means a full extraction (a live scrape
# that parsed real experience, or a live company fetch). Everything else is
# "shallow" and must not accrue absence-based suspicion. This is a pure read of
# the extraction manifest that fetch_profile / fetch_company stamp (see
# extract_linkedin.fetch_profile and pipeline.run's injected-profile branding).
# ---------------------------------------------------------------------------


def scan_depth(raw_profile: Optional[dict]) -> str:
    """Classify a raw profile as "full" or "shallow" from its _extraction
    manifest alone (never from intent). Never raises.

    A PERSON scan is "full" iff the manifest says method == "live_scrape" AND at
    least one experience entry was parsed (experience_count > 0): a live scrape
    that hit a login wall or a broken layout and parsed zero experience is NOT a
    full check, it is shallow. A COMPANY scan is "full" iff method ==
    "live_company_fetch". Anything else, including an injected raw_profile with
    no manifest at all (every _*_blind_scan.py / eval fixture), is "shallow", so
    a hand-built profile can never masquerade as a real scan.
    """
    rp = raw_profile or {}
    ext = rp.get("_extraction") or {}
    method = str(ext.get("method", "")).strip().lower()
    stype = rp.get("scan_type") or "person"
    if stype == "company_app":
        return "full" if method == "live_company_fetch" else "shallow"
    if method == "live_scrape":
        try:
            exp_count = int(ext.get("experience_count", 0) or 0)
        except (TypeError, ValueError):
            exp_count = 0
        if exp_count > 0:
            return "full"
    return "shallow"


# ---------------------------------------------------------------------------
# Typed finding surfaced on the Dossier (deliverable). Never feeds the score;
# the score is the provider's tiers folded by the reused code-computed scorers.
# ---------------------------------------------------------------------------


@dataclass
class MismatchFinding:
    """One cross-reference finding, for dossier.mismatches (the overlay surface).

    kind          : "CONTRADICTION" | "INFLATION" | "GAP" | "TIMELINE".
    claim_indices : the 0-based claim index/indices this finding spans (a
                    timeline overlap spans two).
    label         : short human label (e.g. "user_count vs app-store ratings").
    claimed       : the claimed side, as text.
    discovered    : the discovered side, as text ("" when the point IS the
                    absence, i.e. a GAP).
    severity      : 0..1 mechanical severity (log gap for inflation, fixed
                    bands otherwise). Advisory; the score is the provider's.
    detail        : one line explaining the mismatch.
    resolved_tier : the tier the provider ultimately assigned the anchor claim
                    ("" until resolve_findings runs; "n/a" for company-metric
                    inflations that map to a metric row, not a claim tier).
    basis         : how the DISCOVERED side was obtained: "structured" (a
                    connector-parsed measurement), "web" (a counter-number
                    parsed out of a web/news snippet, weaker), or "" for kinds
                    that do not use it.
    """

    kind: str
    claim_indices: list[int] = field(default_factory=list)
    label: str = ""
    claimed: str = ""
    discovered: str = ""
    severity: float = 0.0
    detail: str = ""
    resolved_tier: str = ""
    basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Small robust parsers (pure, unit-tested directly).
# ---------------------------------------------------------------------------

_SUFFIX_MULT = {
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "mm": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
}

_QUANTITY_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|mm|m|million|bn|b|billion)?\b",
    re.IGNORECASE,
)


def parse_quantity(text: str) -> Optional[float]:
    """Parse the FIRST human-written magnitude in `text`.

    Handles "50k", "1.2M", "$10 million", "50,000", "2.5B", plain "12". Returns
    a float, or None when there is no parseable number. Deliberately takes the
    first match (a claim like "50k users" leads with the number that matters);
    the connector-side discovered parsers below target their own known shapes.
    """
    if not text:
        return None
    m = _QUANTITY_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix:
        value *= _SUFFIX_MULT.get(suffix, 1)
    return value


_RATING_COUNT_RE = re.compile(
    r"(\d[\d,]*)\s*(?:total\s+)?(?:ratings?|reviews?)\b", re.IGNORECASE
)
# App-store FOOTPRINT count specifically: the "rating(s)" word only (optionally
# preceded by "total"), NEVER "reviews". This is the fix behind the traction
# cross-check: the app_store listing record surfaces the true userRatingCount
# as "N total rating(s)" / "N rating(s)", while the SEPARATE reviews-activity
# record says "N review(s) fetched" (a Customer-Reviews-RSS FEED CAP of <= 50,
# not a footprint) and uses the word "star", never "rating". Matching on
# "rating" alone makes _discovered_number_for read the real store footprint and
# ignore the <=50 feed-fetch artifact, so a healthy app (say 88k ratings) is
# never falsely inflated against 50, and a genuinely tiny listing (12 ratings)
# still fires the inflation check.
_APP_STORE_RATING_RE = re.compile(
    r"(\d[\d,]*)\s*(?:total\s+)?ratings?\b", re.IGNORECASE
)
_DOLLAR_RE = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|mm|m|million|bn|b|billion)?",
    re.IGNORECASE,
)


def parse_rating_count(snippet: str) -> Optional[float]:
    """Pull an app-store rating/review COUNT ("12 ratings", "1,204 reviews",
    "88542 total rating(s)") out of a snippet. The count, not the star average,
    is the reach tell. General helper (matches ratings OR reviews); the
    footprint cross-check uses the rating-specific parser below.
    """
    if not snippet:
        return None
    m = _RATING_COUNT_RE.search(snippet)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_app_store_rating_count(snippet: str) -> Optional[float]:
    """Pull the app-store userRatingCount ("N total rating(s)", "N ratings")
    out of a LISTING snippet, matching the "rating" word only so a reviews-
    activity record ("N review(s) fetched", a <=50 feed cap that says "star",
    never "rating") returns None here. This is the authoritative store-footprint
    number the inflation cross-check compares a claimed user count against.
    """
    if not snippet:
        return None
    m = _APP_STORE_RATING_RE.search(snippet)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_dollar_amount(snippet: str) -> Optional[float]:
    """Pull the LARGEST dollar magnitude out of a snippet (a Form D offering
    amount, an audited revenue figure). Largest, because a filing snippet may
    also mention small incidental figures; the headline number is the biggest.
    """
    if not snippet:
        return None
    best: Optional[float] = None
    for m in _DOLLAR_RE.finditer(snippet):
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        suffix = (m.group(2) or "").lower()
        if suffix:
            value *= _SUFFIX_MULT.get(suffix, 1)
        if best is None or value > best:
            best = value
    return best


_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_month_year(text: str) -> Optional[tuple[int, int]]:
    """Parse a displayed date into a (year, month) tuple for ordering.

    "Jan 2020" -> (2020, 1); "2016" -> (2016, 1); "" / "Present" / "Current"
    -> None (an open end, handled by the caller). Month defaults to 1 when only
    a year is present, which is fine for the coarse overlap/ordering checks the
    timeline detector makes.
    """
    if not text:
        return None
    low = text.strip().lower()
    if low in ("present", "current", "now", "ongoing"):
        return None
    ym = _YEAR_RE.search(text)
    if not ym:
        return None
    year = int(ym.group(1))
    month = 1
    for token, num in _MONTHS.items():
        if re.search(r"\b" + token, low):
            month = num
            break
    return (year, month)


def _months(ym: tuple[int, int]) -> int:
    return ym[0] * 12 + (ym[1] - 1)


# ---------------------------------------------------------------------------
# CLAIMED / DISCOVERED structured views (for the detectors and the deliverable)
# ---------------------------------------------------------------------------


def build_claimed_set(claims: list[Claim]) -> list[dict[str, Any]]:
    """A compact structured view of what the PROFILE asserts: one row per claim
    with its type, employer/title, dates, and any numeric magnitude embedded in
    the assertion. Pure; reads only already-decomposed claims.
    """
    rows: list[dict[str, Any]] = []
    for i, c in enumerate(claims):
        rows.append(
            {
                "index": i,
                "type": c.type,
                "employer": c.employer,
                "title": c.title,
                "start": c.start,
                "end": c.end,
                "assertion": c.assertion,
                "claimed_quantity": (
                    parse_quantity(c.assertion) if c.type in _NUMERIC_CLAIM_TYPES else None
                ),
            }
        )
    return rows


def build_discovered_set(claims: list[Claim]) -> list[dict[str, Any]]:
    """A compact structured view of what the SOURCES returned across every
    claim: one row per evidence record, carrying source_name / weight /
    match_confidence when present. Pure; reads only claim.evidence.
    """
    rows: list[dict[str, Any]] = []
    for i, c in enumerate(claims):
        for e in c.evidence or []:
            rows.append(
                {
                    "claim_index": i,
                    "source_url": e.get("source_url", ""),
                    "snippet": e.get("snippet", ""),
                    "source_name": e.get("source_name", ""),
                    "match_confidence": e.get("match_confidence", ""),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# The four mechanical detectors. Each is PURE (takes claims already carrying
# evidence, returns candidates) and NEVER sets a tier. Each returned candidate
# carries an "inject" dict: the synthetic evidence record to attach so the
# reused provider notices the signal.
# ---------------------------------------------------------------------------


def _synthetic(source_name: str, snippet: str, match_confidence: str, weight: float) -> dict:
    """One synthetic evidence record, shaped exactly like a real connector hit
    (source_url/snippet/source_name/weight/match_confidence) so both
    _claims_prompt_block (ApiProvider) and a human operator consume it
    naturally, and _rank_and_cap-style ranking treats it consistently.
    """
    return {
        "source_url": f"internal://mismatch/{source_name}",
        "snippet": snippet,
        "source_name": source_name,
        "weight": weight,
        "match_confidence": match_confidence,
    }


def detect_contradiction(claims: list[Claim]) -> list[MismatchFinding]:
    """Surface claims whose gathered evidence contains an adverse-finding
    phrase (a real contradiction signal, e.g. "no record of him", "admitted
    fabricating"). CANDIDATE only: the provider still decides DISPROVEN under
    the full source-weighting discipline. Never fires on a bare absence (there
    is no snippet to match) and never on a courtlistener-only hit alone (the
    phrase must be in the snippet text, which name-only litigation hits lack).
    """
    findings: list[MismatchFinding] = []
    for i, c in enumerate(claims):
        subject_tokens = _significant_tokens(c.employer)
        if not subject_tokens:
            subject_tokens = _significant_tokens(c.title) | _significant_tokens(c.assertion)
        for e in c.evidence or []:
            if not _snippet_record_usable(e):
                continue
            snippet = (e.get("snippet") or "").lower()
            if subject_tokens and not any(token in snippet for token in subject_tokens):
                continue
            hit = next((p for p in _CONTRADICTION_PHRASES if p in snippet), None)
            if hit is None:
                continue
            # A courtlistener record is name-only and never carries these
            # phrases as an established finding about THIS subject; skip it as
            # the sole basis (mirrors the COURTLISTENER SPECIAL CASE rule).
            if (e.get("source_name") or "") == "courtlistener":
                continue
            findings.append(
                MismatchFinding(
                    kind="CONTRADICTION",
                    claim_indices=[i],
                    label=f"adverse finding on {c.type or 'claim'}",
                    claimed=c.assertion or c.title or c.employer,
                    discovered=(e.get("snippet") or "").strip()[:240],
                    severity=0.9,
                    detail=f'evidence contains "{hit}" contradicting the claim',
                )
            )
            break  # one contradiction candidate per claim is enough
    return findings


# Per-claim-type WEB counter-number patterns: the unit words that make a
# number in a news/web snippet a measurement OF THIS KIND of claim. Per-type on
# purpose, so a headcount snippet can never be read against a user_count claim.
_WEB_COUNTER_PATTERNS = {
    "headcount": re.compile(
        r"(\d[\d,]*)\s*(?:\+\s*)?(?:employees|staff|team members|people)\b",
        re.IGNORECASE,
    ),
    "user_count": re.compile(
        r"(\d[\d,]*)\s*(?:\+\s*)?(?:users|customers|subscribers|downloads|installs)\b",
        re.IGNORECASE,
    ),
}

# Snippet context words a DOLLAR figure needs before it counts as a discovered
# measurement for these claim types (a random dollar amount never counts).
_WEB_DOLLAR_CONTEXT = {
    "revenue_metric": frozenset({"revenue", "arr", "sales", "gmv"}),
    "money_managed": frozenset({"aum", "under management", "assets", "portfolio"}),
}

# Marker / synthetic source names that are never measurements (the two search
# markers plus every injected mismatch_* record, matched by prefix below).
_NON_MEASUREMENT_SOURCES = frozenset({"searched_no_results", "search_unavailable"})


def _web_counter_number(claim: Claim, snippet: str) -> Optional[float]:
    """A counter-number for this claim parsed out of a plain web/news snippet,
    or None. Per-type units only; funding/revenue/money_managed additionally
    require their own context word so a stray dollar figure never counts."""
    ctype = claim.type
    pattern = _WEB_COUNTER_PATTERNS.get(ctype)
    if pattern is not None:
        m = pattern.search(snippet)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    low = snippet.lower()
    tokens = _snippet_tokens(low)
    if ctype == "funding":
        if not _context_hit(low, tokens, _FUNDING_CONTEXT_TOKENS):
            return None
        return parse_dollar_amount(snippet)
    context = _WEB_DOLLAR_CONTEXT.get(ctype)
    if context and _context_hit(low, tokens, context):
        return parse_dollar_amount(snippet)
    return None


def _discovered_number_for(claim: Claim) -> Optional[tuple[float, str, str, str]]:
    """The DISCOVERED reality for a numeric claim, as (value, source_name,
    snippet, basis), or None if nothing measurable was found. Maps each numeric
    claim type to the connector whose record IS the independent measurement:
      user_count      -> app-store userRatingCount (the reach/footprint tell),
                         parsed rating-specifically so the <=50 reviews-feed
                         fetch cap is never mistaken for the footprint
      funding/revenue -> a dollar amount in a Form D / filing snippet
    Plus a WEB fallback (basis "web") used ONLY when no structured measurement
    exists: a per-type counter-number parsed out of a plain web/news snippet
    that actually names this subject. It is weaker than a registry measurement
    and is injected accordingly (medium confidence, the SUS path), never as a
    proven adverse measurement. Bases are never mixed.

    PULLED / DELISTED GUARD: a not-found or removed app yields NO app_store
    evidence at all (verify_app_store returns []), so there is no discovered
    number, so detect_inflation cannot fire and the claim falls through to the
    UNVERIFIED path, never an auto-penalty. Low store visibility that is
    explainable by delisting is treated as unverified, not disproven.
    """
    ctype = claim.type
    subject_tokens = _significant_tokens(claim.employer) or _significant_tokens(
        claim.assertion
    )
    best_structured: Optional[tuple[float, str, str, str]] = None
    best_web: Optional[tuple[float, str, str, str]] = None
    for e in claim.evidence or []:
        sn = (e.get("source_name") or "")
        snippet = e.get("snippet") or ""
        if _is_registry_absent(e):
            # A COMPLETED but empty registry lookup carries no measurement; a
            # pulled/delisted app must never manufacture an inflation.
            continue
        value: Optional[float] = None
        if ctype in ("funding", "revenue_metric") and sn == "sec_edgar_form_d":
            value = parse_dollar_amount(snippet)
        if value is not None:
            # Keep the SMALLEST discovered measurement (the most conservative
            # footprint), so inflation is only flagged against the strongest
            # deflating evidence, never cherry-picked upward.
            if best_structured is None or value < best_structured[0]:
                best_structured = (value, sn, snippet.strip()[:240], "structured")
            continue
        if best_structured is not None:
            continue  # a registry measurement always wins; do not mix bases
        # WEB counter-number path: only a usable, non-marker record whose
        # snippet actually names THIS subject (the misattribution guard: a
        # number about some other company never counts).
        if not _snippet_record_usable(e):
            continue
        if sn.startswith("mismatch_") or sn in _NON_MEASUREMENT_SOURCES:
            continue
        low = snippet.lower()
        if not any(t in low for t in subject_tokens):
            continue
        value = _web_counter_number(claim, snippet)
        if value is None:
            continue
        if best_web is None or value < best_web[0]:
            best_web = (value, sn or "web", snippet.strip()[:240], "web")
    return best_structured or best_web


def detect_inflation(claims: list[Claim]) -> list[MismatchFinding]:
    """Flag a claimed NUMBER that far exceeds a DISCOVERED measurement.

    The generalized "50k users vs 12 app-store ratings" check: it needs BOTH a
    claimed magnitude AND an independent discovered measurement; a big claim
    with NO discovered number is a GAP (see detect_gap), not an inflation, so
    absence never masquerades as a proven overstatement here. Severity scales
    with the log of the ratio. Fires only above _INFLATION_MIN_RATIO so normal
    rounding/optimism is ignored.
    """
    findings: list[MismatchFinding] = []
    for i, c in enumerate(claims):
        if c.type not in _NUMERIC_CLAIM_TYPES:
            continue
        claimed = parse_quantity(c.assertion)
        if claimed is None or claimed <= 0:
            continue
        discovered = _discovered_number_for(c)
        if discovered is None:
            continue
        disc_value, disc_source, disc_snippet, basis = discovered
        if disc_value <= 0:
            # A literal zero footprint is still a real measurement; treat as 1
            # to keep the ratio finite and the gap maximal.
            disc_value = 1.0
        ratio = claimed / disc_value
        if ratio < _INFLATION_MIN_RATIO:
            continue
        severity = min(1.0, math.log10(ratio) / _INFLATION_SEVERITY_K)
        detail = (
            f"claimed {int(claimed):,} but discovered ~{int(disc_value):,} "
            f"({ratio:.0f}x gap): {disc_snippet}"
        )
        if basis == "web":
            # A snippet-derived counter-number is weaker than a registry
            # measurement, so it leans the operator SUS but must never read as a
            # proven adverse measurement (injected "medium", see inject_candidates).
            detail += (
                " | counter-number parsed from a web/news snippet, weaker than a "
                "registry measurement; supports SUS, not DISPROVEN on its own."
            )
        findings.append(
            MismatchFinding(
                kind="INFLATION",
                claim_indices=[i],
                label=f"{c.type} vs discovered footprint",
                claimed=f"{int(claimed):,}",
                discovered=f"{int(disc_value):,} (via {disc_source})",
                severity=round(severity, 3),
                detail=detail,
                basis=basis,
            )
        )
    return findings


# A claim INVOKES a registry when its own text names it. Only then does a
# completed empty lookup of that registry become a targeted negative result.
# Maps registry key -> (claim text markers, evidence source_name). Play Store /
# Android claims are deliberately absent: the connector queries Apple's catalog
# only. Techstars is deliberately absent: its reachable surface is a fixed
# highlight widget, not the portfolio, so a miss there never means not-backed
# (see accelerators.py) and it must never be read as checked-absent.
_REGISTRY_INVOCATIONS = {
    "y_combinator": (
        ("y combinator", "ycombinator", "yc-backed", "yc backed",
         "backed by yc", "yc s2", "yc w2", "yc f2", "(yc "),
        "accelerator_badges",
    ),
    "apple_app_store": (
        ("app store", "ios app", "iphone app", "ipad app", "on the app store"),
        "app_store_play_store_reviews",
    ),
}


def detect_registry_absence(claims: list[Claim]) -> list[MismatchFinding]:
    """Flag a claim that INVOKES a specific authoritative registry when a
    COMPLETED lookup of that registry came back empty. A positive mismatch (a
    targeted negative result), not generic absence: it fires ONLY off a
    checked-absent evidence record (registry_check == "absent", match_confidence
    "high") emitted by the registry's own connector after a successful query;
    never off an empty evidence set, a search marker, or a failed lookup.

    AUTHORITATIVE registries only (see _REGISTRY_INVOCATIONS and the connector
    authority table): Y Combinator's own public directory and Apple's own App
    Store catalog. NOT authoritative and never read here: Techstars (a fixed
    highlight widget, not the portfolio), org rosters, Form D, arxiv/openalex,
    and generic web search.

    SUS-strength, HARD-CAPPED: this leads the operator to UNVERIFIED + high
    footprint. Registry absence caps at SUS UNCONDITIONALLY and NEVER escalates
    to DISPROVEN: registries have real coverage gaps (renames, very recent
    batches, region limits), and absence-as-disproof is the one line this
    project does not cross. DISPROVEN stays impossible off this signal.
    """
    findings: list[MismatchFinding] = []
    for i, c in enumerate(claims):
        haystack = f"{c.assertion or ''} {c.title or ''} {c.employer or ''}".lower()
        for registry, (markers, source_name) in _REGISTRY_INVOCATIONS.items():
            if not any(m in haystack for m in markers):
                continue
            record = None
            for e in c.evidence or []:
                if (e.get("source_name") or "") != source_name:
                    continue
                if not _is_registry_absent(e):
                    continue
                if (e.get("match_confidence") or "").lower() != "high":
                    continue
                record = e
                break
            if record is None:
                continue
            caveat = (record.get("snippet") or "").strip()[:240]
            findings.append(
                MismatchFinding(
                    kind="REGISTRY_ABSENCE",
                    claim_indices=[i],
                    label=f"claimed {registry} listing not found in the registry itself",
                    claimed=c.assertion or c.employer or c.type,
                    discovered="completed lookup of the invoked registry: not listed",
                    severity=0.6,
                    detail=(
                        f"{caveat} | supports SUS (UNVERIFIED + high footprint). "
                        "HARD RULE: registry absence caps at SUS UNCONDITIONALLY "
                        "and can NEVER reach DISPROVEN, not even after ruling out "
                        "rename/recency gaps: registries have real coverage gaps "
                        "and absence is not contradiction."
                    ),
                )
            )
            break  # one finding per claim; the first invoked registry wins
    return findings


def _significant_tokens(text: str) -> set[str]:
    """Lowercased tokens of length >= 3 that are distinctive enough to identify
    a subject (generic org nouns and stopwords filtered out)."""
    if not text:
        return set()
    return {
        t
        for t in re.split(r"[^a-z0-9]+", text.lower())
        if len(t) >= 3 and t not in _GENERIC_SUBJECT_TOKENS
    }


def _snippet_record_usable(e: dict) -> bool:
    """A record whose snippet may count toward corroboration: never a synthetic
    mismatch record, and never a connector hit the connector itself already
    judged a low-confidence namesake."""
    sn = e.get("source_name") or ""
    if sn.startswith("mismatch_"):
        return False
    mc = (e.get("match_confidence") or "").lower()
    if sn and mc == "low":
        return False
    if sn == "github" and mc != "high":
        return False
    if (e.get("relationship") or "") == "subject_controlled":
        return False
    if (e.get("source_class") or "") == "republication":
        return False
    return True


def _snippet_tokens(snippet_low: str) -> set[str]:
    """The snippet's word tokens, for whole-word context matching (so "led"
    never matches inside "settled" and "president" never matches "present")."""
    return {t for t in re.split(r"[^a-z0-9]+", snippet_low) if t}


def _context_hit(snippet_low: str, tokens: set[str], phrases) -> bool:
    """True when a snippet carries one of `phrases`: single words are matched
    against the snippet's WORD TOKENS, multi-word entries as phrases."""
    for p in phrases:
        if " " in p:
            if p in snippet_low:
                return True
        elif p in tokens:
            return True
    return False


def _is_registry_absent(e: dict) -> bool:
    """True for a CHECKED-ABSENT record: a completed registry/catalog lookup
    that came back empty. It is the opposite of corroboration, so it must
    never suppress a GAP and never yield a discovered measurement."""
    return (e.get("registry_check") or "").strip().lower() == "absent"


def _web_corroboration_level(
    claim: Claim, name_anchors: set[str], context_tokens: set[str]
) -> str:
    """How strongly the gathered web snippets speak to this claim.

    Returns "substantive" (evidence speaks to the ROLE/IMPACT/SCALE, the only
    level that suppresses a GAP), "association" (the entity is real and the
    name co-occurs with it, but nothing addresses the claimed role), or
    "none". Existence and association are deliberately NOT substantiation:
    real LARP is a real company plus an inflated role.

    Per-type rules (the strongest level across the whole evidence set wins, so
    ONE qualifying snippet anywhere clears the claim):
      - employment/education: a person-name anchor AND a distinctive
        employer/school token in the SAME snippet, PLUS something that speaks
        to the role itself (a distinctive token from the claimed title, or any
        _ROLE_EVIDENCE_TOKENS member: news apposition like "Jane Doe, chief
        executive of Acme" always carries one). Name plus employer with no
        role-speaking token is "association".
      - identity: UNCHANGED (name anchor + profile-context token is
        "substantive"). DELIBERATE EXCEPTION: an identity claim is
        existence-shaped by nature ("this person is who they say they are"),
        which is exactly what association evidence answers, so there is no
        association middle state here.
      - company_overview: a distinctive company token AND a distinctive token
        from the claim's own assertion that is NOT part of the company name
        (evidence the DESCRIPTION matches). A bare company mention is
        "association": it-exists is not good enough.
      - funding: UNCHANGED (company token + funding-context word), because the
        funding context IS the substance; no association middle state.
      - money_managed/certification: a subject token plus a management or
        credential context word, else "none" (same shape as funding).
    A generic hit ("Google is a company", careers pages) matches none of
    these, so a fabricated notable claim still reads as a plain GAP.
    """
    subject_tokens = _significant_tokens(claim.employer)
    if not subject_tokens:
        subject_tokens = _significant_tokens(claim.title) | _significant_tokens(
            claim.assertion
        )
    title_tokens = _significant_tokens(claim.title)
    # Tokens of the claim's own assertion that are NOT just the company name:
    # the DESCRIPTION half of a company_overview claim.
    description_tokens = _significant_tokens(claim.assertion) - _significant_tokens(
        claim.employer
    )

    best = "none"
    for e in claim.evidence or []:
        if not _snippet_record_usable(e):
            continue
        if _is_registry_absent(e):
            # A completed negative lookup describes the subject only to say it
            # was not found; it can never corroborate anything.
            continue
        snip = (e.get("snippet") or "").lower()
        if not snip:
            continue
        tokens = _snippet_tokens(snip)
        if claim.type == "identity":
            if name_anchors and any(a in snip for a in name_anchors) and any(
                t in snip for t in context_tokens
            ):
                return "substantive"
        elif claim.type in ("employment", "education"):
            if (
                name_anchors
                and any(a in snip for a in name_anchors)
                and any(t in snip for t in subject_tokens)
            ):
                if any(t in tokens for t in title_tokens) or _context_hit(
                    snip, tokens, _ROLE_EVIDENCE_TOKENS
                ):
                    return "substantive"
                best = "association"
        elif claim.type == "company_overview":
            if any(t in snip for t in subject_tokens):
                if any(t in snip for t in description_tokens):
                    return "substantive"
                best = "association"
        elif claim.type == "funding":
            if any(t in snip for t in subject_tokens) and _context_hit(
                snip, tokens, _FUNDING_CONTEXT_TOKENS
            ):
                return "substantive"
        elif claim.type in ("money_managed", "certification"):
            if any(t in snip for t in subject_tokens) and _context_hit(
                snip, tokens, _CREDENTIAL_CONTEXT_TOKENS
            ):
                return "substantive"
    return best


def detect_gap(
    claims: list[Claim],
    identity: Optional[dict] = None,
    skip_indices: Optional[frozenset[int]] = None,
) -> list[MismatchFinding]:
    """Flag a claimed NOTABLE thing with ZERO corroborating trace where a
    truthful version should have left one. SUS ONLY: the finding maps to
    UNVERIFIED-high-footprint downstream, never DISPROVEN, because absence is
    not a contradiction. A claim is a GAP when it is a notable type, a broad
    search actually RAN for it (evidence records exist), and nothing
    SUBSTANTIATES it: neither a qualifying structured-connector hit NOR a
    web/news snippet that speaks to the claimed role/impact/scale (see
    _web_corroboration_level). EXISTENCE DOES NOT CLEAR: evidence that merely
    shows the entity is real, or that the name co-occurs with it, leaves the
    claim unsubstantiated and yields the "at a real entity" GAP instead. Only
    role-speaking corroboration suppresses the finding, so a legit person
    verified through real news coverage still never trips it.

    identity: the profile identity dict (name/headline/current_company), used
    to anchor the web-snippet corroboration check. Without it the snippet
    check falls back to connector-only corroboration for person claims.
    skip_indices: claim indices already covered by a CONTRADICTION or AUTONOMY
    finding; a contradicted claim is not an absence, so it is never also a GAP.
    """
    findings: list[MismatchFinding] = []
    identity = identity or {}
    skip = skip_indices or frozenset()

    name_anchors = _significant_tokens(identity.get("name") or "")
    full_name = (identity.get("name") or "").strip().lower()
    if full_name:
        name_anchors.add(full_name)
    # Profile-match context for identity claims: the profile's own employers,
    # headline, and current company.
    context_tokens: set[str] = set()
    for c in claims:
        context_tokens |= _significant_tokens(c.employer)
    context_tokens |= _significant_tokens(identity.get("headline") or "")
    context_tokens |= _significant_tokens(identity.get("current_company") or "")

    for i, c in enumerate(claims):
        if c.type not in _GAP_NOTABLE_TYPES:
            continue
        if c.expected_footprint == "low":
            continue
        if i in skip:
            # Already covered by a contradiction-shaped finding: the claim is
            # contested, not silent, and a GAP would only muddy the signal.
            continue
        evidence = c.evidence or []
        if not _claim_was_searched(c):
            # No search ran (empty evidence, or only a search_unavailable
            # marker: the channel was not configured). "We did not / could not
            # look" is never SUS, same discipline as compute_founder_score's
            # evidence gate.
            continue
        corroborated = False
        for e in evidence:
            sn = (e.get("source_name") or "")
            mc = (e.get("match_confidence") or "").lower()
            if sn not in _CORROBORATING_SOURCES or mc not in ("high", "medium"):
                continue
            if _is_registry_absent(e):
                # A COMPLETED negative registry lookup (the invoked registry
                # says "not listed") is the opposite of corroboration.
                continue
            if sn == "github":
                # GITHUB SPECIAL RULE: a "medium" record is a name-handle
                # pattern match (an account with a matching name merely
                # exists), and a confirmed-but-thin account substantiates no
                # role either. Only a CONFIRMED account whose code footprint
                # reads substantial speaks to a claimed role.
                if mc != "high":
                    continue
                if "authenticity read: substantial" not in (
                    e.get("snippet") or ""
                ).lower():
                    continue
            corroborated = True
            break
        level = "substantive" if corroborated else _web_corroboration_level(
            c, name_anchors, context_tokens
        )
        if level == "substantive":
            continue
        subject = c.employer or c.title or (c.assertion[:60] if c.assertion else c.type)
        if level == "association":
            # The entity is real and the name co-occurs with it, but nothing
            # speaks to the claimed role. A REAL gap (same kind, same injected
            # meta, same scoring consequence); the distinct label lets the
            # deliverable say the sharper true thing.
            label = f"unsubstantiated {c.type} at a real entity"
            severity = 0.35
            discovered = "entity exists; nothing speaks to the claimed role"
            detail = (
                f"the entity is real and the name co-occurs with it, but none of "
                f"{len(evidence)} record(s) speaks to the claimed role/impact/"
                f"scale; existence is not substantiation"
            )
        else:
            label = f"unverifiable {c.type}"
            severity = 0.4
            discovered = ""
            detail = (
                f"searched {len(evidence)} record(s); none independently "
                f"corroborates this notable {c.type}"
            )
        findings.append(
            MismatchFinding(
                kind="GAP",
                claim_indices=[i],
                label=label,
                claimed=c.assertion or subject,
                discovered=discovered,
                severity=severity,
                detail=detail,
            )
        )
    return findings


def detect_timeline(claims: list[Claim]) -> list[MismatchFinding]:
    """Assemble a chronology and flag impossibilities.

    Three checks, all conservative (low false-positive):
      1. Two employment roles with IDENTICAL full-time date ranges (you cannot
         hold two identical-span full-time jobs at two employers).
      2. An intra-claim reversal: end date strictly before start date.
      3. A founding/role year that predates the domain registration year found
         in the claim's own domain_rdap_whois evidence ("founded 2015" on a
         domain first registered 2023). Also: a credential (education) that
         ENDS after the start of a role whose title requires it (Dr/PhD/MD).
    """
    findings: list[MismatchFinding] = []

    employments = [
        (i, c) for i, c in enumerate(claims) if c.type == "employment"
    ]

    # Check 1: identical full-time spans across two different employers.
    seen: dict[tuple, tuple[int, Claim]] = {}
    for i, c in employments:
        s = parse_month_year(c.start)
        e = parse_month_year(c.end)
        if s is None or e is None:
            continue  # need both bounds to call it a full identical span
        key = (s, e)
        if key in seen:
            j, other = seen[key]
            if (other.employer or "").strip().lower() != (c.employer or "").strip().lower():
                findings.append(
                    MismatchFinding(
                        kind="TIMELINE",
                        claim_indices=[j, i],
                        label="overlapping full-time roles",
                        claimed=(
                            f"{other.employer} ({other.start} to {other.end}) and "
                            f"{c.employer} ({c.start} to {c.end})"
                        ),
                        discovered="",
                        severity=0.6,
                        detail="two full-time roles claimed over the exact same dates",
                    )
                )
        else:
            seen[key] = (i, c)

    # Check 2: end before start, within one claim.
    for i, c in enumerate(claims):
        s = parse_month_year(c.start)
        e = parse_month_year(c.end)
        if s is None or e is None:
            continue
        if _months(e) < _months(s):
            findings.append(
                MismatchFinding(
                    kind="TIMELINE",
                    claim_indices=[i],
                    label="reversed dates",
                    claimed=f"{c.employer or c.title}: {c.start} to {c.end}",
                    discovered="",
                    severity=0.5,
                    detail="claimed end date precedes the start date",
                )
            )

    # Check 3a: founding/role year predates the domain registration year.
    for i, c in enumerate(claims):
        if any(
            (e.get("source_name") or "") == "product_site"
            and (e.get("product_name_alignment") or "") == "first_party_alias"
            for e in c.evidence or []
        ):
            # A subject-declared rename or rebrand can legitimately use a
            # newer domain than the original product or role. Until the alias
            # timeline is independently established, domain age is not a
            # contradiction.
            continue
        claim_year = None
        s = parse_month_year(c.start)
        if s is not None:
            claim_year = s[0]
        else:
            ym = _YEAR_RE.search(c.assertion or "")
            claim_year = int(ym.group(1)) if ym else None
        if claim_year is None:
            continue
        for e in c.evidence or []:
            if (e.get("source_name") or "") != "domain_rdap_whois":
                continue
            reg = _YEAR_RE.search(e.get("snippet") or "")
            if not reg:
                continue
            reg_year = int(reg.group(1))
            if claim_year < reg_year:
                findings.append(
                    MismatchFinding(
                        kind="TIMELINE",
                        claim_indices=[i],
                        label="founding predates domain",
                        claimed=f"dated {claim_year}",
                        discovered=f"domain registered {reg_year}",
                        severity=0.55,
                        detail=(
                            f"claim dated {claim_year} but the domain was first "
                            f"registered {reg_year}, {reg_year - claim_year} year(s) later"
                        ),
                    )
                )
                break

    # Check 3b: a credential that postdates a role that required it.
    _CREDENTIAL_TITLES = ("phd", "ph.d", "dr.", "m.d", "md,", "doctor", "postdoc")
    edu_ends = [
        (i, parse_month_year(c.end))
        for i, c in enumerate(claims)
        if c.type == "education"
    ]
    for i, c in enumerate(claims):
        if c.type != "employment":
            continue
        title_low = (c.title or "").lower()
        if not any(t in title_low for t in _CREDENTIAL_TITLES):
            continue
        role_start = parse_month_year(c.start)
        if role_start is None:
            continue
        for j, edu_end in edu_ends:
            if edu_end is None:
                continue
            if _months(edu_end) > _months(role_start):
                findings.append(
                    MismatchFinding(
                        kind="TIMELINE",
                        claim_indices=[j, i],
                        label="credential postdates role",
                        claimed=(
                            f"role '{c.title}' from {c.start}"
                        ),
                        discovered=f"required credential completed {claims[j].end}",
                        severity=0.5,
                        detail=(
                            "a role requiring the credential starts before the "
                            "credential was completed"
                        ),
                    )
                )
                break

    return findings


def detect_autonomy_overstatement(claims: list[Claim]) -> list[MismatchFinding]:
    """Surface a claimed-autonomy / proprietary-AI assertion CONTRADICTED by
    humans-in-the-loop evidence anywhere in the aggregated dossier.

    This is the AI-washing / wizard-of-oz LARP class (Amazon Just Walk Out,
    Builder.ai): the profile says "fully automatic / no humans / our AI does
    it" while the gathered evidence says humans (outsourced workers, manual
    review) actually did the work. CROSS-CLAIM on purpose: the exposE evidence
    usually lands on the company_overview claim's gather, not on the
    proprietary_tech claim itself, so a per-claim check would miss it.

    Two guards keep a genuinely autonomous product safe:
      1. It needs BOTH sides: a strong autonomy marker in the claim's own
         assertion AND exposE-shaped humans-in-the-loop language in a usable
         evidence record. Either alone is silent.
      2. The marker list is exposE-shaped (see _HUMANS_IN_LOOP_MARKERS):
         generic industry chatter about humans and AI does not match, and
         low-confidence connector namesakes are excluded.

    CANDIDATE only: the provider still makes the DISPROVEN call. But unlike a
    GAP this is a REAL contradiction shape (evidence of humans doing the
    claimed-autonomous work), so a provider that accepts it may legitimately
    mark the autonomy claim DISPROVEN, which is what routes the company score
    into the top band (see llm.compute_company_score's disproven path).
    """
    contradicting: list[tuple[int, str, str, str]] = []
    for source_index, c in enumerate(claims):
        for e in c.evidence or []:
            if not _snippet_record_usable(e):
                continue
            snip = e.get("snippet") or ""
            low = snip.lower()
            marker = next((m for m in _HUMANS_IN_LOOP_MARKERS if m in low), None)
            if marker is None:
                continue
            contradicting.append(
                (source_index, snip.strip()[:240], e.get("source_name") or "web", marker)
            )
    if not contradicting:
        return []

    findings: list[MismatchFinding] = []
    for i, c in enumerate(claims):
        if c.type != "proprietary_tech":
            continue
        low = (c.assertion or "").lower()
        hit = next((m for m in _AUTONOMY_MARKERS if m in low), None)
        if hit is None:
            continue
        subject_tokens = _significant_tokens(c.employer)
        if not subject_tokens:
            subject_tokens = _significant_tokens(c.assertion)
        relevant = next(
            (
                item
                for item in contradicting
                if item[0] == i
                or (
                    subject_tokens
                    and any(token in item[1].lower() for token in subject_tokens)
                )
            ),
            None,
        )
        if relevant is None:
            continue
        _, snip, src, marker = relevant
        findings.append(
            MismatchFinding(
                kind="AUTONOMY",
                claim_indices=[i],
                label="claimed autonomy vs humans-in-the-loop evidence",
                claimed=(c.assertion or "")[:240],
                discovered=f"{snip} (via {src})",
                severity=0.9,
                detail=(
                    f'claims autonomy ("{hit}") but gathered evidence carries '
                    f'humans-in-the-loop language ("{marker}")'
                ),
            )
        )
    return findings


# LOUD technical/builder markers in a claim's own title/assertion. Deliberately
# the LOUD end only (proprietary AI, "I built it", technical cofounder, founding
# engineer, CTO), NOT a bare "software engineer" or "works in tech": a strong
# builder claim normally leaves a public CODE/artifact trace, and it is that
# loud end that is worth flagging when no real code footprint backs it. Gating
# here IS the false-positive control: an ordinary non-technical CEO/founder who
# never claimed to code is never matched, so this signal never touches them.
_TECHNICAL_CLAIM_MARKERS = (
    "proprietary ai",
    "proprietary model",
    "proprietary algorithm",
    "proprietary tech",
    "proprietary technology",
    "our own ai",
    "our own model",
    "built the entire",
    "built it myself",
    "i built",
    "i personally built",
    "i wrote the",
    "single-handedly built",
    "architected the",
    "technical cofounder",
    "technical co-founder",
    "founding engineer",
    "principal engineer",
    "staff engineer",
    "chief technology officer",
    " cto",
    "cto ",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "deep learning",
)

# Person claim types the technical-authenticity signal may fire on. It never
# fires on company-scan claim types (proprietary_tech / company_overview): those
# are handled by the AUTONOMY detector and the proprietary_ai_gap / buildability
# company metrics, which is where the company path feeds this same judgment.
_TECH_CLAIM_TYPES = frozenset({"employment", "identity"})


def _asserts_technical_ability(claim: Claim) -> bool:
    """True only when a PERSON claim makes a LOUD technical/builder assertion
    (see _TECHNICAL_CLAIM_MARKERS): a claimed engineer / technical cofounder /
    "I built it" / "proprietary AI". The gate that keeps a non-technical
    CEO/founder who never claimed to code entirely out of this signal.
    """
    if claim.type not in _TECH_CLAIM_TYPES:
        return False
    haystack = f"{claim.title or ''} {claim.assertion or ''}".lower()
    return any(m in haystack for m in _TECHNICAL_CLAIM_MARKERS)


def _github_authenticity_clears(claim: Claim) -> bool:
    """True when the claim carries a CONFIRMED-substantial GitHub record: a
    high-match-confidence github hit whose technical-authenticity read is
    "substantial". Only that clears the tell. A namesake (low-confidence)
    account NEVER clears it (its repos are not confirmably this person's), and
    a matched-but-thin account does not clear it either.
    """
    for e in claim.evidence or []:
        if (e.get("source_name") or "") != "github":
            continue
        if (e.get("match_confidence") or "").lower() != "high":
            continue
        if "authenticity read: substantial" in (e.get("snippet") or "").lower():
            return True
    return False


def _github_thin_matched(claim: Claim) -> bool:
    """True when a HIGH-confidence (confirmed) github account for this person
    reads thin-or-absent: the strongest version of the tell (it IS them, and
    the code is thin), stronger than a mere absence of any match."""
    for e in claim.evidence or []:
        if (e.get("source_name") or "") != "github":
            continue
        if (e.get("match_confidence") or "").lower() != "high":
            continue
        if "authenticity read: thin-or-absent" in (e.get("snippet") or "").lower():
            return True
    return False


def detect_technical_authenticity(
    claims: list[Claim], identity: Optional[dict] = None
) -> list[MismatchFinding]:
    """Surface a LOUD technical/builder claim (proprietary AI, "I built it",
    technical cofounder, founding engineer, CTO) that is NOT backed by a real,
    confirmed public code footprint. A should-be-real-engineer with no real
    code is a LARP tell.

    SUS ONLY, never DISPROVEN: absence (or thinness) of public code is not a
    contradiction, so this maps downstream to UNVERIFIED + high expected
    footprint (the SUS band), exactly like a GAP, and the injected record
    carries the same "can never support DISPROVEN" disclaimer. Two hard gates
    keep it off legit people:
      1. It only fires on a LOUD technical claim (_asserts_technical_ability);
         a non-technical CEO/founder who never claimed to code is untouched.
      2. It is CLEARED by any CONFIRMED-substantial GitHub account
         (_github_authenticity_clears); a namesake never clears it, but a
         namesake's thin repos never deepen it either (match discipline).
    It only fires when a search actually RAN (the claim has evidence), same
    "we did not look is not SUS" discipline as the GAP detector and the score.
    """
    findings: list[MismatchFinding] = []
    for i, c in enumerate(claims):
        if not _asserts_technical_ability(c):
            continue
        if not _claim_was_searched(c):
            # No search ran (or only a search_unavailable marker); "we did not /
            # could not look" is never a tell.
            continue
        if _github_authenticity_clears(c):
            # A confirmed, substantial engineer footprint: the claim checks out.
            continue
        matched_thin = _github_thin_matched(c)
        subject = c.title or c.employer or (c.assertion[:60] if c.assertion else c.type)
        if matched_thin:
            # RESOLVED-AND-THIN: the identity is high-confidence (the account IS
            # theirs) and it reads thin-or-absent behind a loud technical claim.
            # A POSITIVE undershoot, stronger than a bare void, still SUS-only.
            findings.append(
                MismatchFinding(
                    kind="TECH_SUBSTANCE_MISMATCH",
                    claim_indices=[i],
                    label=f"claimed technical role vs resolved code footprint on {c.type}",
                    claimed=c.assertion or subject,
                    discovered="confirmed GitHub account reads thin-or-absent",
                    severity=0.6,
                    detail=(
                        "the claimed technical/leadership role is undershot by the "
                        "person's own resolved code footprint (identity is "
                        "high-confidence, the account is theirs, and it reads "
                        "thin-or-absent). UNDERSHOOT, NOT PROOF: employer code is "
                        "often private, so this supports SUS (UNVERIFIED + high "
                        "expected footprint) and can NEVER support DISPROVEN on "
                        "its own."
                    ),
                )
            )
            continue
        findings.append(
            MismatchFinding(
                kind="TECH_AUTHENTICITY",
                claim_indices=[i],
                label=f"technical authenticity gap on {c.type}",
                claimed=c.assertion or subject,
                discovered="",
                severity=0.45,
                detail=(
                    "loud technical/builder claim with no confirmed substantial "
                    "public code footprint found"
                ),
            )
        )
    return findings


def run_detectors(
    claims: list[Claim],
    identity: Optional[dict] = None,
    depth: str = "full",
) -> list[MismatchFinding]:
    """Run all six mechanical detectors over the aggregated claims.

    Contradiction-shaped detectors (CONTRADICTION, AUTONOMY) run first; the
    GAP detector then skips any claim they already cover, because a contested
    claim is not a silent one. identity (optional) anchors the GAP detector's
    web-snippet corroboration check. The TECH_AUTHENTICITY detector is
    independent (a loud builder claim with no real code footprint), SUS-only.

    depth: "full" (default) or "shallow". On a SHALLOW scan the tool did not
    actually look (an injected profile, a zero-experience scrape), so
    ABSENCE-kind findings (GAP, TECH_AUTHENTICITY) are suppressed entirely: an
    absence of evidence when nothing was really searched is not a signal.
    CONTRADICTION / AUTONOMY / INFLATION / TIMELINE are real cross-references
    that stand on discovered evidence, so they remain allowed even on a shallow
    scan (only absence is illegitimate).
    """
    contradictions = detect_contradiction(claims)
    autonomy = detect_autonomy_overstatement(claims)
    contested = frozenset(
        i for f in (contradictions + autonomy) for i in f.claim_indices
    )
    findings: list[MismatchFinding] = []
    findings.extend(contradictions)
    findings.extend(autonomy)
    findings.extend(detect_inflation(claims))
    # Registry absence stands on discovered evidence (a completed negative
    # lookup), so it runs OUTSIDE the shallow gate like inflation. A claim
    # already covered by a contradiction-shaped finding is contested, not
    # silent, so it is skipped (reuse the same contested set).
    findings.extend(
        f
        for f in detect_registry_absence(claims)
        if not (set(f.claim_indices) & contested)
    )
    if depth != "shallow":
        findings.extend(detect_gap(claims, identity=identity, skip_indices=contested))
        findings.extend(detect_technical_authenticity(claims, identity=identity))
    findings.extend(detect_timeline(claims))
    return findings


# ---------------------------------------------------------------------------
# Candidate injection: turn each finding into a synthetic evidence record on
# the relevant claim(s), so the REUSED provider reasoning step sees the signal.
# match_confidence is chosen so the discipline lands correctly:
#   - CONTRADICTION: "high" (a real adverse finding the provider can act on),
#     but still one record among the set, so the provider's "never DISPROVEN
#     off a single low-confidence hit" and "read the whole set" rules apply.
#   - INFLATION: "high" when a real discovered measurement backs it.
#   - GAP: "low" (it is an ABSENCE, must never push toward DISPROVEN).
#   - TIMELINE: "medium" (a structural inconsistency worth weighing).
# ---------------------------------------------------------------------------

_INJECT_META = {
    "CONTRADICTION": ("mismatch_contradiction", "high", 0.8),
    "AUTONOMY": ("mismatch_autonomy", "high", 0.8),
    "INFLATION": ("mismatch_inflation", "high", 0.8),
    # GAP is an ABSENCE: injected weak on purpose (low confidence, low weight)
    # so even an operator who trusts it blindly cannot escalate a corroborated
    # claim off it; the record's own text also disarms it (see below).
    "GAP": ("mismatch_gap", "low", 0.2),
    # TECH_AUTHENTICITY is absence/thinness-shaped like GAP (a loud builder
    # claim with no real code footprint): injected weak and SUS-only, with the
    # same "can never support DISPROVEN" disclaimer, so it lifts a strong
    # technical claim into the SUS band (UNVERIFIED + high footprint) but can
    # never become a false accusation of fabrication.
    "TECH_AUTHENTICITY": ("mismatch_tech_authenticity", "low", 0.2),
    # TECH_SUBSTANCE_MISMATCH is the RESOLVED undershoot (the person's own
    # confirmed account reads thin behind a loud builder claim), so it is a
    # real signal rather than an absence flag: injected at medium confidence
    # and prepended. Still SUS-only: the "UNDERSHOOT, NOT PROOF" discipline
    # travels inside the snippet via the finding's own detail text.
    "TECH_SUBSTANCE_MISMATCH": ("mismatch_tech_substance", "medium", 0.5),
    # REGISTRY_ABSENCE is a completed negative lookup of the registry the claim
    # itself invokes: a strong STRUCTURAL signal, injected at medium confidence
    # and deliberately below the 0.8 contradiction weight so it can never read
    # as a proven adverse finding. Prepended (a real signal, not an absence
    # flag); the "caps at SUS, never DISPROVEN" discipline travels in the detail.
    "REGISTRY_ABSENCE": ("mismatch_registry_absence", "medium", 0.64),
    "TIMELINE": ("mismatch_timeline", "medium", 0.64),
}

# Kinds whose injected record is an ABSENCE flag: appended (never allowed to
# crowd out real evidence) and stamped with the disclaimer below.
_ABSENCE_KINDS = frozenset({"GAP", "TECH_AUTHENTICITY"})

# Appended verbatim to every injected GAP record so the discipline travels
# WITH the record: a careless operator who reads only this snippet still gets
# told it can never justify DISPROVEN and is void next to real corroboration.
_GAP_RECORD_DISCLAIMER = (
    " | ABSENCE ONLY: this is not evidence of falsehood and can NEVER support "
    "DISPROVEN. If any other record in this evidence set corroborates the "
    "claim (including a strong news/reference hit), disregard this record "
    "entirely: real corroboration always outranks an injected absence flag."
    " ASSOCIATION IS NOT CORROBORATION: if the other records only show the "
    "entity exists or the name near the employer, that does NOT clear this "
    "claim; corroboration means evidence speaking to the role, impact, or "
    "scale itself."
)


def inject_candidates(claims: list[Claim], findings: list[MismatchFinding]) -> None:
    """Attach one synthetic evidence record per finding to each claim it spans.
    Mutates claims in place. Idempotent-ish: a second call would append again,
    so build_dossier calls it exactly once.

    Contradiction-shaped records are PREPENDED (the signal must never be
    crowded out of a capped evidence set); GAP records are APPENDED so an
    absence flag never displaces or outranks the real evidence above it.
    """
    for f in findings:
        source_name, mc, weight = _INJECT_META.get(
            f.kind, ("mismatch_other", "low", 0.2)
        )
        if f.kind == "INFLATION" and f.basis == "web":
            # A web/news counter-number is not a registry-grade measurement:
            # inject it at medium confidence / medium weight so it can lean the
            # operator SUS but can never read as a proven adverse measurement.
            source_name, mc, weight = ("mismatch_inflation", "medium", 0.5)
        snippet = f"[{f.kind}] {f.detail}"
        if f.claimed:
            snippet += f" | claimed: {f.claimed}"
        if f.discovered:
            snippet += f" | discovered: {f.discovered}"
        if f.kind in _ABSENCE_KINDS:
            snippet += _GAP_RECORD_DISCLAIMER
        record = _synthetic(source_name, snippet, mc, weight)
        for idx in f.claim_indices:
            if 0 <= idx < len(claims):
                if f.kind in _ABSENCE_KINDS:
                    claims[idx].evidence.append(dict(record))
                else:
                    # Prepend so the mismatch signal is never crowded out of a
                    # capped evidence set the provider actually reads.
                    claims[idx].evidence.insert(0, dict(record))


def resolve_findings(
    dossier: Dossier, findings: list[MismatchFinding]
) -> list[MismatchFinding]:
    """After the provider has assigned tiers, stamp each finding with the tier
    the provider ultimately gave its anchor claim, so the deliverable shows how
    the mechanical candidate RESOLVED (a CONTRADICTION the provider accepted is
    now a DISPROVEN claim; a GAP stays UNVERIFIED; an INFLATION on a company
    metric maps to a metric row rather than a claim tier).
    """
    for f in findings:
        if f.kind == "INFLATION" and dossier.scan_type == "company_app":
            # Company inflations drive the reach_vs_footprint / raise_inflation
            # metric rows (the score), not a claim tier.
            f.resolved_tier = "n/a (metric row)"
            continue
        idx = f.claim_indices[0] if f.claim_indices else None
        if idx is not None and 0 <= idx < len(dossier.claims):
            f.resolved_tier = dossier.claims[idx].tier.value
    return findings


# ---------------------------------------------------------------------------
# Aggregation: broad, parallel, bounded gather over all claims at once.
# ---------------------------------------------------------------------------


def _searched_no_results_record(claim: Claim) -> dict:
    """A marker that a broad search RAN for this claim and returned nothing.

    Distinct from an absent evidence[] (which means the search never ran /
    was cut short). It is not corroboration and not a contradiction: it is
    explicit proof-of-search, so a NOTABLE claim that comes back empty reads
    as SUS (unverifiable-where-it-should-be-verifiable) instead of silently
    CLEAR, while an obscure/low-footprint claim stays clear because the
    operator's expected_footprint, not this record, gates the score.
    """
    return {
        "source_url": "internal://searched",
        "snippet": (
            "Searched all connectors and web for this claim and found no "
            "records. This is proof a search ran and returned nothing, not a "
            "contradiction: absence is never DISPROVEN."
        ),
        "source_name": "searched_no_results",
        "weight": 0.0,
        "match_confidence": "low",
    }


# Source name of the search_unavailable marker. A claim whose only "evidence" is
# a record of this name was NOT searched (the web-search channel was not
# configured); it must be treated exactly like an empty evidence set (never
# looked), never as a searched-and-absent SUS signal.
_SEARCH_UNAVAILABLE_SOURCE = "search_unavailable"


def _search_unavailable_record(claim: Claim) -> dict:
    """A marker that the web-search channel was UNAVAILABLE (unconfigured, or
    configured but dark: quota-exhausted, dead key, unreachable SearXNG) when
    this claim came back empty, so nothing could be looked up.

    Distinct from _searched_no_results_record (which means a search actually RAN
    and returned nothing, a legitimate SUS signal for a notable claim). This
    record carries NO signal at all: downstream (detect_gap, the SUS evidence
    gate in compute_founder_score) treats a claim whose only evidence is this
    marker as never-looked, so an unconfigured search backend can never make a
    real person read as SUS. Weight 0.0, mirroring the searched_no_results shape.
    """
    return {
        "source_url": "internal://search-unavailable",
        "snippet": (
            "The web-search channel was not configured or was unreachable/"
            "quota-exhausted, so this claim could not be looked up. This is NOT "
            "a search that returned nothing and is never a signal: no GAP, no "
            "absence scoring, never DISPROVEN."
        ),
        "source_name": _SEARCH_UNAVAILABLE_SOURCE,
        "weight": 0.0,
        "match_confidence": "low",
    }


def _claim_was_searched(claim: Claim) -> bool:
    """Compatibility wrapper around the shared relevance-qualified rule."""
    return claim_search_completed(claim)


# A scan is BLIND when this share or more of its claims were never actually
# looked up. Not a tunable nicety: below this line the number stops describing
# the person and starts describing our own outage.
_BLIND_SCAN_UNSEARCHED_RATIO = 0.5


def blind_scan_reason(claims: list[Claim]) -> str:
    """How much of this scan was never actually looked up, or "" when coverage
    was fine.

    NOT a scoring gate. The per-claim guards already stop a dark channel from
    accusing anyone (see _search_unavailable_record), a fully dark scan lands
    CLEAR, and a real contradiction must still reach the fraud band even under a
    dark backend. Suppressing the score would break both rules and, worse, would
    bury a genuine disproof behind an outage.

    What was missing is COVERAGE. A reader shown "42" cannot tell a profile we
    checked thoroughly from one where the backend was down and half the claims
    were never touched. Both produced a number; only one of them means anything.

    Deliberately counted from the EVIDENCE, not from a health probe: what
    matters is whether the claims on this profile actually got looked up, which
    survives a backend that dies or recovers halfway through a scan. A profile
    with no checkable claims at all is not blind, it is just empty.
    """
    checkable = [c for c in claims or [] if (c.evidence or [])]
    if not checkable:
        return ""
    unsearched = [c for c in checkable if not _claim_was_searched(c)]
    if not unsearched:
        return ""
    ratio = len(unsearched) / len(checkable)
    if ratio < _BLIND_SCAN_UNSEARCHED_RATIO:
        return ""
    return (
        f"LIMITED COVERAGE: {len(unsearched)} of {len(checkable)} claims were "
        f"never looked up, because the web-search channel was unreachable or out "
        f"of quota. Nothing here is evidence against this person: those claims "
        f"were not checked, not found wanting. Restore the search backend and "
        f"re-run before treating this scan as a result."
    )


def _aggregate(
    claims: list[Claim],
    identity: dict,
    company_url: Optional[str],
    pb_budget: Optional["pitchbook.PitchBookBudget"],
    max_workers: int,
    max_evidence: int,
    per_claim_timeout_s: float,
    emit: ProgressFn,
) -> None:
    """Gather evidence for EVERY claim in one bounded parallel pass, mutating
    each claim.evidence in place. Reuses verify.gather_evidence verbatim (same
    connectors, same web search, same dedup/rank), only wider (max_evidence)
    and fanned out across claims (bounded pool + per-claim wall-clock guard).
    """
    if not claims:
        return

    def _one(claim: Claim) -> None:
        verify.gather_evidence(
            claim,
            identity,
            pb_budget=pb_budget,
            company_url=company_url,
            max_evidence=max_evidence,
        )

    workers = max(1, min(max_workers, len(claims)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_one, c): c for c in claims}
        for fut, claim in futures.items():
            completed = False
            try:
                fut.result(timeout=per_claim_timeout_s)
                completed = True
            except FuturesTimeoutError:
                logger.warning(
                    "dossier: gather for claim %r exceeded %.0fs; proceeding with "
                    "partial evidence",
                    claim.assertion or claim.type,
                    per_claim_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 - a source must never break the run
                logger.warning("dossier: gather raised for a claim: %s", exc)
            # A claim whose search COMPLETED but returned nothing is a real
            # "we looked and found zero" signal, distinct from a claim that
            # timed out (genuinely unfinished). Record the completed-empty case
            # explicitly so downstream (detect_gap, the SUS evidence gate in
            # compute_founder_score) can tell "searched, nothing there" apart
            # from "never searched": only the former is a candidate SUS gap.
            # Never inject on a timeout: unfinished is not the same as absent.
            if completed and not (claim.evidence or []):
                # Distinguish "searched and found nothing" (a real SUS signal
                # for a notable claim) from "could not search at all" (the web
                # channel is unconfigured OR dark: quota-exhausted, dead key,
                # unreachable SearXNG): the latter must never let a dark backend
                # brand a real person SUS. Use search_healthy(), not
                # search_available(): a configured-but-dark channel that returned
                # [] never looked. See risk 9.4: _aggregate labels a claim only
                # AFTER its own gather completed, so any failing Brave call has
                # already set the cooldown by the time this liveness check runs.
                if search.search_healthy():
                    claim.evidence = [_searched_no_results_record(claim)]
                else:
                    claim.evidence = [_search_unavailable_record(claim)]
            elif completed and bool(
                getattr(claim, "_web_search_unavailable", False)
            ):
                evidence = list(claim.evidence or [])
                already_marked = any(
                    (record.get("source_name") or "") == _SEARCH_UNAVAILABLE_SOURCE
                    for record in evidence
                )
                if not already_marked:
                    claim.evidence = evidence + [_search_unavailable_record(claim)]
            emit("claim", claim)


# ---------------------------------------------------------------------------
# Director / planning pass: execute the provider's proposed follow-ups.
# ---------------------------------------------------------------------------


def _followup_field(fq: object, name: str, default=None):
    """Read a field off a FollowupQuery OR a plain dict, so the executor works
    whether a provider returned typed objects or raw dicts."""
    if isinstance(fq, dict):
        return fq.get(name, default)
    return getattr(fq, name, default)


def _director_evidence_record(
    result: dict,
    rationale: str,
    claim: Claim,
    person: str,
) -> Optional[dict]:
    """One director follow-up evidence record. Shaped as PLAIN, usable web
    evidence (source_url + snippet + source_name), with NO match_confidence and
    NO weight, so:
      - it is treated as default mid-weight web evidence (evidence_weight),
      - _snippet_record_usable returns True for it (it can suppress a GAP via
        the snippet-corroboration path), and
      - it is NOT in _CORROBORATING_SOURCES, so it never counts as a structured
        connector hit, only as what it is: a targeted web result.
    The rationale is carried into the snippet so the reasoning travels with the
    record for the operator/provider that reads it.
    """
    relevance = verify._result_relevance(result, claim, person)
    if relevance == "irrelevant":
        return None
    url = (result.get("url") or "").strip()
    snippet = (result.get("snippet") or result.get("title") or "").strip()
    prefix = f"[director followup: {rationale}] " if rationale else "[director followup] "
    return {
        "source_url": url,
        "snippet": (prefix + (snippet or "")).strip(),
        "source_name": "director_followup",
        "claim_relevance": relevance,
        "relationship": verify._relationship_for_url(url, person=person),
        "source_class": verify._source_class_for_url(url),
    }


def _director_no_results_record(query: str, rationale: str) -> dict:
    """Proof that one targeted director search completed with zero results.

    This deliberately uses the existing searched_no_results source class so
    scoring can distinguish a real empty lookup from a dark search backend.
    The exact query and rationale travel with the marker for operator review.
    """
    why = f" Rationale: {rationale}" if rationale else ""
    return {
        "source_url": "internal://searched/director-followup",
        "snippet": (
            f"Targeted director search completed with zero results: {query!r}.{why} "
            "This is an absence signal only and can never support DISPROVEN."
        ),
        "source_name": "searched_no_results",
        "weight": 0.0,
        "match_confidence": "low",
    }


def _required_role_followups(
    claims: list[Claim],
    identity: dict,
    proposed: list,
) -> list[FollowupQuery]:
    """Add one role-binding lookup when a public role still lacks one."""
    person = (identity.get("name") or "").strip()
    if not person:
        return []
    planned_indices = {
        int(_followup_field(item, "claim_index", -1))
        for item in proposed or []
        if str(_followup_field(item, "claim_index", -1)).lstrip("-").isdigit()
    }
    required: list[FollowupQuery] = []
    for index, claim in enumerate(claims):
        if index in planned_indices or claim.type != "employment":
            continue
        if not (verify._query_tokens(claim.title) & _PUBLIC_ROLE_TOKENS):
            continue
        if _claim_has_confirmation_basis(claim):
            continue
        role_phrases = verify._role_query_phrases(claim.title)
        role = role_phrases[0] if role_phrases else claim.title
        suffix = (
            "event"
            if verify._query_tokens(claim.title) & {"founder", "cofounder"}
            else "interview"
        )
        query = f'"{person}" "{claim.employer}" "{role}" {suffix}'.strip()
        required.append(
            FollowupQuery(
                claim_index=index,
                query=query,
                rationale=(
                    "Public role has only self-controlled, republished, or "
                    "association evidence. Find a source that binds person, "
                    "organization, and role."
                ),
                kind="web",
            )
        )
    return required


# ---------------------------------------------------------------------------
# Stage 1.5: product-site resolution.
#
# Runs BETWEEN decompose and the aggregate gather, because its whole purpose is
# to hand the URL-keyed connectors (wayback, domain_age, techstack) a URL. On a
# person scan a founder claim carries only the product NAME, so before this
# stage those three connectors could never fire and a claimed WEB product could
# be neither confirmed nor properly flagged (the App-Store-or-nothing hole).
#
# Same discipline as the director pass: bounded, never raises, no-op unless the
# provider opts in, and it only ADDS evidence. It never sets a tier, a score or
# the verdict. Candidate harvesting is deliberately LinkedIn-first (the profile
# usually declares the site itself), with a name search only as a fallback,
# because a name search is where namesake ambiguity comes from.
# ---------------------------------------------------------------------------

_RESOLVE_MAX_CLAIMS = 5
_RESOLVE_MAX_CANDIDATES = 4
_RESOLVE_WALL_CLOCK_S = 25.0
_RESOLVE_SEARCH_COUNT = 4
_RESOLVE_CONTEXT_CHARS = 400


def _is_product_claim(claim: Claim) -> bool:
    """True for a claim whose weight rests on a PRODUCT actually existing: a
    metric claim, or a founder-flavored employment claim. A degree or a plain
    non-founder job is not a product and gets no probes spent on it."""
    if claim.type in ("user_count", "revenue_metric"):
        return True
    if claim.type == "employment":
        try:
            return bool(verify._looks_founder_flavored(claim))
        except Exception:  # noqa: BLE001 - never break a scan on a gating helper
            return False
    return False


def _resolution_inputs(claim: Claim, raw_profile: dict) -> tuple[list[str], list[str]]:
    """Harvest (candidate_urls, context_lines) for one product claim.

    Ordered cheapest and most trustworthy first, which is also least ambiguous
    first: links the PERSON published (contact info, their own posts about
    shipping the thing, the role description) beat anything a name search can
    return. The experience row's own /company/ href is context, never a
    candidate: it identifies WHICH company, but you cannot point wayback or a
    tech-stack fingerprint at linkedin.com and learn anything about the product.
    """
    product = (claim.employer or "").strip()
    identity = raw_profile.get("identity") or {}
    hints = identity.get("hints")
    ledger = ledger_for(claim)
    claim_index = getattr(claim, "_claim_index", None)
    if not isinstance(hints, dict):
        hints = {}
        identity["hints"] = hints

    urls: list[str] = []
    context: list[str] = []

    role_line = " ".join(x for x in [claim.title, "at", product] if x).strip()
    if role_line:
        context.append(f"Claimed role: {role_line}")

    declared_sites = [
        (site or "").strip() for site in (hints.get("websites") or [])
        if (site or "").strip()
    ]
    for site in declared_sites:
        named_links = product_site.extract_named_product_links(site, product)
        for target in named_links:
            if target not in urls:
                urls.append(target)
            context.append(
                f"Profile-declared site {site} links {product} to {target}"
            )
        person_norm = re.sub(
            r"[^a-z0-9]", "", (identity.get("name") or "").lower()
        )
        site_host_norm = re.sub(
            r"[^a-z0-9]", "", (urlparse(site).hostname or "").lower()
        )
        subject_named_site = bool(person_norm and person_norm in site_host_norm)
        if not named_links and not subject_named_site and site not in urls:
            urls.append(site)

    lowered = product.lower()
    for row in raw_profile.get("experience") or []:
        if not isinstance(row, dict):
            continue
        if (row.get("company") or "").strip().lower() != lowered:
            continue
        company_page = (row.get("company_url") or "").strip()
        if company_page:
            context.append(f"LinkedIn company page for this row: {company_page}")
        description = (row.get("description") or "").strip()
        if description:
            context.append(f"Role description: {description[:_RESOLVE_CONTEXT_CHARS]}")
            for url in product_site.urls_in_text(description):
                if url not in urls:
                    urls.append(url)

    for post in raw_profile.get("posts") or []:
        if not isinstance(post, dict):
            continue
        text = (post.get("text") or "").strip()
        if not text or (lowered and lowered not in text.lower()):
            continue
        context.append(f"Own post: {text[:_RESOLVE_CONTEXT_CHARS]}")
        for url in product_site.urls_in_text(text):
            if url not in urls:
                urls.append(url)

    # Recovery path: LinkedIn's contact overlay sometimes fails even though a
    # name-matched personal site is indexed. Search for the person plus product,
    # but only crawl a result whose host itself carries both name tokens. From
    # that page harvest only links explicitly labeled with the product name.
    # The resolver still decides the target, so this path never confirms a role.
    if not urls and product:
        person = ((raw_profile.get("identity") or {}).get("name") or "").strip()
        if person:
            try:
                query = f'"{person}" "{product}"'
                attempt = (
                    ledger.attempt(
                        "resolve",
                        "subject_product_search",
                        claim_index=claim_index,
                        query=query,
                    )
                    if ledger
                    else None
                )
                subject_results = search.web_search(query, count=_RESOLVE_SEARCH_COUNT)
                if attempt:
                    attempt.finish(
                        "completed" if subject_results else (
                            "completed_empty"
                            if search.search_healthy()
                            else "unavailable"
                        ),
                        result_count=len(subject_results),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dossier: subject-product search raised for %r: %s",
                    product[:60],
                    exc,
                )
                subject_results = []
                if attempt:
                    attempt.finish("error", error=f"{type(exc).__name__}: {exc}")
            person_tokens = [
                re.sub(r"[^a-z0-9]", "", token.lower())
                for token in person.split()
                if re.sub(r"[^a-z0-9]", "", token.lower())
            ]
            for result in subject_results or []:
                page_url = (result.get("url") or "").strip()
                try:
                    host_norm = re.sub(
                        r"[^a-z0-9]", "",
                        (urlparse(page_url).hostname or "").lower(),
                    )
                except Exception:
                    host_norm = ""
                if (
                    not page_url
                    or len(person_tokens) < 2
                    or not all(token in host_norm for token in person_tokens[:2])
                ):
                    continue
                named_links = product_site.extract_named_product_links(
                    page_url, product
                )
                recovered_hints = product_site.extract_subject_identity_hints(
                    page_url
                )
                if recovered_hints:
                    hints.update(recovered_hints)
                for target in named_links:
                    if target not in urls:
                        urls.append(target)
                    context.append(
                        f"First-party subject page {page_url} links "
                        f"{product} to {target}"
                    )
                if urls:
                    break

    # Last fallback: nothing the person published or their subject-matched site
    # points anywhere, so ask the general web. This is ambiguous by construction.
    if not urls and product:
        try:
            query = f"{product} official site"
            attempt = (
                ledger.attempt(
                    "resolve",
                    "official_site_search",
                    claim_index=claim_index,
                    query=query,
                )
                if ledger
                else None
            )
            results = search.web_search(query, count=_RESOLVE_SEARCH_COUNT)
            if attempt:
                attempt.finish(
                    "completed" if results else (
                        "completed_empty" if search.search_healthy() else "unavailable"
                    ),
                    result_count=len(results),
                )
        except Exception as exc:  # noqa: BLE001 - a lookup must never break the run
            logger.warning("dossier: product-site search raised for %r: %s", product[:60], exc)
            results = []
            if attempt:
                attempt.finish("error", error=f"{type(exc).__name__}: {exc}")
        for r in results or []:
            url = (r.get("url") or "").strip()
            if url and url not in urls:
                urls.append(url)

    return urls, context


def _resolve_product_sites(
    claims: list[Claim],
    raw_profile: dict,
    identity: dict,
    provider,
    emit: ProgressFn,
    depth: str = "full",
    max_claims: int = _RESOLVE_MAX_CLAIMS,
    max_candidates: int = _RESOLVE_MAX_CANDIDATES,
    wall_clock_s: float = _RESOLVE_WALL_CLOCK_S,
) -> dict[int, dict]:
    """Resolve claimed products to their real sites and set claim.product_url.

    Returns {claim_index: evidence_record} for the records to attach AFTER the
    aggregate gather (the gather overwrites claim.evidence wholesale, so a
    record attached before it would be silently thrown away).

    Outcomes, and this is the whole contract:
      resolved    -> product_url set, one record. Existence of the PRODUCT only.
      not_found   -> no URL, one weight-0 record. SUS at most, never DISPROVEN.
      ambiguous   -> nothing at all. Ambiguity is NOT absence.
      unavailable -> nothing at all. We could not look.
    """
    import time as _time

    deadline = _time.time() + max(0.0, wall_clock_s)

    requests_: list[dict] = []
    for index, claim in enumerate(claims):
        if len(requests_) >= max_claims:
            break
        if not _is_product_claim(claim) or not (claim.employer or "").strip():
            continue
        urls, context = _resolution_inputs(claim, raw_profile)
        if not urls:
            continue
        ledger = ledger_for(claim)
        attempt = (
            ledger.attempt(
                "resolve",
                "product_site_probe",
                claim_index=index,
                target=", ".join(urls[:max_candidates]),
            )
            if ledger
            else None
        )
        probes = product_site.probe_candidates(
            urls, max_candidates=max_candidates, deadline=deadline
        )
        if attempt:
            attempt.finish(
                "completed" if probes else "completed_empty",
                result_count=len(probes),
            )
        if not probes:
            continue
        requests_.append(
            {
                "claim_index": index,
                "product_name": (claim.employer or "").strip(),
                "role_text": claim.assertion or f"{claim.title} at {claim.employer}",
                "context": context,
                "candidates": probes,
            }
        )

    if not requests_:
        return {}

    emit("status", f"resolving {len(requests_)} claimed product site(s)")
    shared_ledger = ledger_for(claims[requests_[0]["claim_index"]])
    provider_attempt = (
        shared_ledger.attempt(
            "resolve",
            type(provider).__name__,
            metadata={"request_count": len(requests_)},
        )
        if shared_ledger
        else None
    )
    try:
        resolutions = provider.resolve_product_site(requests_, identity)
        if provider_attempt:
            provider_attempt.finish(
                "completed", result_count=len(resolutions or [])
            )
    except Exception as exc:
        if provider_attempt:
            provider_attempt.finish(
                "error", error=f"{type(exc).__name__}: {exc}"
            )
        raise

    by_index = {r["claim_index"]: r for r in requests_}
    records: dict[int, dict] = {}
    for resolution in resolutions or []:
        index = getattr(resolution, "claim_index", -1)
        outcome = getattr(resolution, "outcome", "ambiguous")
        request = by_index.get(index)
        if request is None or not (0 <= index < len(claims)):
            continue  # a resolution for a claim we never asked about
        product = request["product_name"]

        if outcome == "resolved":
            url = (getattr(resolution, "url", "") or "").strip()
            probe = next(
                (p for p in request["candidates"] if p.get("url") == url or p.get("final_url") == url),
                None,
            )
            if not url or probe is None:
                continue  # picked something we never probed: treat as ambiguous
            claims[index].product_url = url
            candidate_urls = {
                (probe.get("url") or "").strip(),
                (probe.get("final_url") or "").strip(),
                url,
            }
            first_party_mapping = any(
                (
                    line.startswith("Profile-declared site ")
                    or line.startswith("First-party subject page ")
                )
                and f"links {product} to " in line
                and any(candidate and candidate in line for candidate in candidate_urls)
                for line in request.get("context") or []
            )
            records[index] = product_site.resolved_record(
                product,
                probe,
                confidence=getattr(resolution, "confidence", "medium"),
                rationale=getattr(resolution, "rationale", ""),
                first_party_mapping=first_party_mapping,
            )
        elif outcome == "not_found":
            # A shallow/injected profile must never accrue absence-based
            # suspicion (the depth rule the detectors already obey). A positive
            # resolution is still allowed through above: finding something is
            # never the thing depth protects against.
            if depth != "full":
                continue
            records[index] = product_site.not_found_record(
                product, candidates_seen=len(request["candidates"])
            )
        # ambiguous / unavailable: nothing recorded, on purpose.

    return records


def _execute_followups(
    claims: list[Claim],
    followups: list,
    emit: ProgressFn,
    max_followups: int,
    wall_clock_s: float,
    identity: Optional[dict] = None,
) -> int:
    """Run each proposed follow-up query with a bounded, never-raise web search
    and attach its results as director_followup evidence on the named claim.
    Returns how many follow-ups actually ran. Bounds:
      - at most max_followups queries run (extra proposals are dropped),
      - a total wall-clock ceiling of wall_clock_s across all follow-ups,
      - only kind == "web" and an in-range claim_index are executed.
    The director ONLY adds evidence: it never sets a tier, a score, or the
    verdict. A completed follow-up that finds nothing adds an explicit
    searched_no_results marker, while a failed or dark lookup adds nothing.
    Either way, absence can never become DISPROVEN. Any error in one follow-up
    is swallowed so a single bad lookup never breaks the scan.
    """
    if not followups:
        return 0

    import time as _time

    deadline = _time.time() + max(0.0, wall_clock_s)
    person = ((identity or {}).get("name") or "").strip()
    ran = 0
    for fq in followups:
        if ran >= max_followups:
            break
        if _time.time() > deadline:
            logger.warning("dossier: director follow-ups hit the wall-clock ceiling; stopping")
            break

        kind = str(_followup_field(fq, "kind", "web") or "web").strip().lower()
        if kind != "web":
            continue
        try:
            idx = int(_followup_field(fq, "claim_index", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(claims)):
            continue
        query = str(_followup_field(fq, "query", "") or "").strip()
        if not query:
            continue
        rationale = str(_followup_field(fq, "rationale", "") or "").strip()

        ran += 1
        search_completed = False
        ledger = ledger_for(claims[idx])
        audit_attempt = (
            ledger.attempt(
                "director",
                "web_search",
                claim_index=idx,
                query=query,
                metadata={"rationale": rationale[:300]},
            )
            if ledger is not None
            else None
        )
        try:
            results = search.web_search(query, count=_DIRECTOR_SEARCH_COUNT)
            search_completed = search.search_healthy()
        except Exception as exc:  # noqa: BLE001 - a follow-up must never break the run
            logger.warning("dossier: director follow-up search raised for %r: %s", query[:60], exc)
            results = []
            if audit_attempt is not None:
                audit_attempt.finish(
                    "error", error=f"{type(exc).__name__}: {exc}"
                )

        attached = 0
        site_match = re.search(
            r"(?:^|\s)site:([A-Za-z0-9.-]+)", query, re.IGNORECASE
        )
        required_host = site_match.group(1).lower().strip(".") if site_match else ""
        candidate_records = []
        for r in results or []:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            if required_host:
                result_host = (urlparse(url).hostname or "").lower()
                if not (
                    result_host == required_host
                    or result_host.endswith("." + required_host)
                ):
                    continue
            record = _director_evidence_record(r, rationale, claims[idx], person)
            if record is None:
                continue
            candidate_records.append(record)
        candidate_records.sort(key=verify._web_record_priority, reverse=True)
        for record in candidate_records[:_DIRECTOR_RESULTS_PER_FOLLOWUP]:
            claims[idx].evidence.append(record)
            attached += 1
        if attached == 0 and search_completed:
            claims[idx].evidence.append(
                _director_no_results_record(query, rationale)
            )
        if audit_attempt is not None and not audit_attempt.finished:
            audit_attempt.finish(
                "completed" if search_completed else "unavailable",
                result_count=attached,
                metadata={"search_completed": search_completed},
            )
        outcome = f"{attached} hit(s)" if search_completed else "search unavailable"
        emit(
            "status",
            f"director follow-up for claim {idx} ({outcome}): {query[:60]}",
        )
    return ran


# ---------------------------------------------------------------------------
# The public entry point.
# ---------------------------------------------------------------------------


def _default_progress(event: str, payload: object) -> None:
    from .pipeline import safe_print  # reuse the narrow-console-safe printer

    if event == "status":
        safe_print(f"[dossier] {payload}")
    elif event == "claim":
        safe_print(f"[dossier]   claim: {getattr(payload, 'assertion', payload)}")
    elif event == "verdict":
        d = payload
        score = getattr(d, "founder_larp_score", None)
        if score is None:
            score = getattr(d, "company_larp_score", None)
        safe_print(f"[dossier] verdict ready (score={score})")


def build_dossier(
    raw_profile: dict,
    provider: Optional[LLMProvider] = None,
    emit: Optional[ProgressFn] = None,
    scan_type: Optional[str] = None,
    *,
    max_workers: int = _DOSSIER_MAX_WORKERS,
    max_evidence: int = _DOSSIER_MAX_EVIDENCE,
    per_claim_timeout_s: float = _DOSSIER_PER_CLAIM_TIMEOUT_S,
    director_max_followups: int = _DIRECTOR_MAX_FOLLOWUPS,
    director_wall_clock_s: float = _DIRECTOR_WALL_CLOCK_S,
    allow_network: bool = True,
    attempt_ledger: Optional[AttemptLedger] = None,
) -> Dossier:
    """Aggregate-then-mismatch scan. A/B-comparable to detective.pipeline.run.

    Args:
        raw_profile: an ALREADY-FETCHED profile dict (person) or company page
            dict (company_app). REQUIRED: this path never fetches, so it can
            never trip a live LinkedIn fetch. Its own "scan_type" key wins over
            the scan_type arg, exactly like pipeline.run.
        provider: the reasoning brain (defaults to ManualProvider), used
            UNCHANGED: build_dossier calls provider.decompose_claims and
            provider.assign_tiers_and_verdict, so both ManualProvider (human
            or fresh Codex reviewer) and ApiProvider (Gemini) work with no new provider
            code. The mismatch candidates reach the brain as synthetic evidence.
        emit: optional (event, payload) progress callback; defaults to printing.
        scan_type: "person" (default) or "company_app"; only consulted when
            raw_profile has no "scan_type" of its own.
        max_workers / max_evidence / per_claim_timeout_s: the bounded-gather
            tunables (see module constants). Kept as args so a harness or the
            overlay can trade breadth for latency.

    Returns:
        A scored Dossier with the SAME score fields as pipeline.run
        (founder_larp_score OR company_larp_score, verdict, larp_score), PLUS
        dossier.mismatches: the typed CONTRADICTION / INFLATION / GAP / TIMELINE
        findings with their resolved tiers. Unscored (scores None) if a
        ManualProvider job is still pending, same as pipeline.run.
    """
    provider = provider or ManualProvider()
    emit = emit or _default_progress
    ledger = attempt_ledger or AttemptLedger(
        getattr(provider, "_audit_job_id", "")
        or getattr(provider, "job_id", "")
    )

    effective_scan_type = raw_profile.get("scan_type") or scan_type or "person"
    raw_profile = dict(raw_profile)
    raw_profile["scan_type"] = effective_scan_type
    identity = raw_profile.get("identity", {}) or {}

    # Depth is read from the extraction manifest ALONE (not from intent): an
    # injected/hand-built profile or a zero-experience scrape is "shallow" and
    # must not accrue absence-based suspicion (no GAP findings, no SUS score).
    depth = scan_depth(raw_profile)

    # 1. Decompose (mechanical, via the provider, same as pipeline.run).
    emit("status", "decomposing claims")
    with ledger.attempt("decompose", type(provider).__name__) as attempt:
        claims = provider.decompose_claims(raw_profile)
        attempt.finish("completed", result_count=len(claims))
    for index, claim in enumerate(claims):
        claim._attempt_ledger = ledger
        claim._claim_index = index

    # 1.5 RESOLVE: decide which website each claimed product actually IS, before
    # the gather, so claim.product_url can point the URL-keyed connectors
    # (wayback, domain_age, techstack) at a real web product on a PERSON scan.
    # Bounded and wrapped: a broken or slow resolver degrades to "no product URL
    # resolved" and the scan proceeds exactly as it did before this stage
    # existed. The records it produces are attached AFTER the gather below,
    # because the gather replaces claim.evidence wholesale.
    # Skipped entirely on a company/app scan: the operator already handed us the
    # company's own landing page as profile_url, which is authoritative, and a
    # company profile carries no declared-website hints, so resolution there
    # would fall straight to the ambiguous name-search branch and spend probes
    # to maybe contradict a URL we were given.
    try:
        product_site_records = (
            {}
            if effective_scan_type == "company_app" or not allow_network
            else _resolve_product_sites(
                claims, raw_profile, identity, provider, emit, depth=depth
            )
        )
    except Exception as exc:  # noqa: BLE001 - resolution must never break the run
        logger.warning(
            "dossier: product-site resolution raised; proceeding unresolved: %s", exc
        )
        product_site_records = {}

    # 2. AGGREGATE: one broad, bounded, parallel gather over ALL claims.
    pb_budget = pitchbook.PitchBookBudget() if pitchbook.is_enabled() else None
    company_url = (
        raw_profile.get("profile_url") if effective_scan_type == "company_app" else None
    )
    emit("status", f"aggregating a broad dossier for {len(claims)} claim(s)")
    if allow_network:
        _aggregate(
            claims,
            identity,
            company_url,
            pb_budget,
            max_workers,
            max_evidence,
            per_claim_timeout_s,
            emit,
        )
    else:
        emit("status", "offline mode: skipping web and connector requests")
        for claim in claims:
            claim.evidence = []
            emit("claim", claim)

    # 2.2 Attach the product-site records the resolution stage produced. This
    # happens HERE, after the gather, because verify.gather_evidence assigns
    # claim.evidence wholesale and would have discarded anything attached
    # earlier. One record per resolved-or-searched claim, deliberately appended
    # past the per-claim cap: it is the answer to "does this product exist",
    # which must not lose a coin flip against a generic web hit.
    for index, record in (product_site_records or {}).items():
        if 0 <= index < len(claims):
            runtime_record = next(
                (
                    item
                    for item in claims[index].evidence or []
                    if (item.get("source_name") or "") == "techstack"
                ),
                None,
            )
            runtime_status = (
                (runtime_record or {}).get("runtime_app_hint") or "unavailable"
            )
            record["web_app_check_status"] = runtime_status
            if runtime_record is None:
                record["web_app_check_note"] = (
                    "The runtime web-app check returned no evidence. This is "
                    "missing coverage, not evidence that no app exists."
                )
            claims[index].evidence = list(claims[index].evidence or []) + [record]

    # 2.5 DIRECT / PLAN: an OPTIONAL, bounded director pass inserted BETWEEN the
    # aggregate gather and the mechanical detectors. The provider reasons over
    # the now-aggregated dossier and proposes targeted follow-up web queries for
    # the thin CHECKABLE claims; each is executed with a bounded, never-raise
    # web search and attached as director_followup evidence, so the EXISTING
    # detectors (stage 3) and scorer (stage 4/5) then run over the enriched
    # evidence, UNCHANGED. The base provider proposes nothing, so this stage is
    # a pure no-op unless a provider opts in (backwards compatible). The
    # director only ADDS evidence and PROPOSES where to look: it never sets
    # tiers, the score, or the verdict (brain proposes, math disposes), and a
    # follow-up that finds nothing is an absence, never a DISPROVEN. Wrapped so
    # a broken planner can never break the scan.
    try:
        if allow_network:
            with ledger.attempt(
                "director_plan", type(provider).__name__
            ) as attempt:
                followups = provider.plan_followups(claims, identity)
                attempt.finish("completed", result_count=len(followups or []))
        else:
            followups = []
    except Exception as exc:  # noqa: BLE001 - planning must never break the run
        logger.warning(
            "dossier: provider.plan_followups raised; skipping director pass: %s", exc
        )
        followups = []
    if allow_network:
        required_followups = _required_role_followups(
            claims, identity, list(followups or [])
        )
        followups = required_followups + list(followups or [])
    if followups:
        emit("status", f"director proposed {len(followups)} follow-up(s)")
        _execute_followups(
            claims,
            followups,
            emit,
            director_max_followups,
            director_wall_clock_s,
            identity,
        )

    # 3. CROSS-REFERENCE: run the five mechanical mismatch detectors and inject
    # each finding as a synthetic evidence record the reused brain will read.
    emit("status", "cross-referencing claimed vs discovered")
    normalize_expected_footprints(claims)
    with ledger.attempt("cross_reference", "mechanical_detectors") as attempt:
        findings = run_detectors(claims, identity=identity, depth=depth)
        inject_candidates(claims, findings)
        attempt.finish("completed", result_count=len(findings))

    dossier = Dossier(
        profile_url=raw_profile.get("profile_url", ""),
        scan_type=effective_scan_type,
        identity=identity,
        raw_experience=raw_profile.get("experience", []) or [],
        claims=claims,
        scan_depth=depth,
        attempt_ledger=ledger.snapshot(),
    )
    if effective_scan_type == "company_app":
        dossier.buildability = Buildability()
        dossier.metric_breakdown = build_metric_breakdown(claims)

    # 3.5 COVERAGE DISCLOSURE. Scoring continues UNCHANGED: unsearched claims
    # already contribute nothing, a dark scan already lands CLEAR rather than
    # suspicious, and a genuine contradiction must still reach the fraud band
    # (see tests/test_judgment_principles.py P3/P4). Suppressing the score would
    # break both and would hide real disproofs. What was missing is the coverage
    # the number alone hides: a reader cannot tell a profile we checked and
    # found clean from one we could not check at all. This states which it was.
    coverage = blind_scan_reason(claims)
    if coverage:
        logger.warning("dossier: %s", coverage)
        dossier.coverage_warning = coverage
        emit("status", coverage)

    # 4. REASONING: the SAME provider step the current engine uses. The
    # provider sets tiers/verdict (and, for a company, buildability + metric
    # scores) reasoning over the evidence set that now includes the mismatch
    # candidates. Every defamation guard lives in that step, unchanged.
    emit("status", "assigning tiers and verdict")
    with ledger.attempt("reasoning", type(provider).__name__) as attempt:
        dossier = provider.assign_tiers_and_verdict(dossier)
        attempt.finish("completed", result_count=len(dossier.claims))

    # 5. SCORE: the SAME code-computed scorers, unchanged, so the number is
    # A/B-comparable to pipeline.run and every scoring guard (only DISPROVEN
    # reaches the top band, GAP/absence capped below it) carries over.
    if dossier.scan_type == "company_app":
        if dossier.buildability is not None and dossier.metric_breakdown:
            sync_buildability_metric(dossier.metric_breakdown, dossier.buildability)
        dossier.company_larp_score = compute_company_score(
            dossier.metric_breakdown, claims=dossier.claims
        )
    else:
        if dossier.larp_score is not None:
            final_score = compute_founder_score(
                dossier.claims, scan_depth=depth
            )
            dossier.larp_score = final_score
            dossier.founder_larp_score = final_score

    # 6. SURFACE the typed findings (deliverable only, never scored here).
    resolve_findings(dossier, findings)
    dossier.mismatches = [f.to_dict() for f in findings]
    dossier.attempt_ledger = ledger.snapshot()

    emit("verdict", dossier)
    return dossier
