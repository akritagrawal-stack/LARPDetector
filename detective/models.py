"""Data models for the LARP detector Dossier.

All models are plain dataclasses that round-trip to and from JSON via
to_dict / from_dict. No em dashes anywhere in this file (house rule).

Dossier.scan_type distinguishes a person scan (default, existing behavior)
from a company/app scan ("company_app"). Company scans additionally carry a
Buildability block (tier plus a one-line note): null on person scans. That
tier is a FACTOR the provider folds into larp_score (a trivially
vibecodeable product sold at a premium scores higher on LARP), surfaced as
its own meter alongside the claim-based score, never a separate rebuild plan.

Serialization rules:
  - EvidenceTier serializes to its string value ("CONFIRMED", ...).
  - datetimes serialize to ISO 8601 strings.
  - from_dict tolerates missing / extra keys so old queue files still load.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


class EvidenceTier(str, enum.Enum):
    """Confidence level assigned to a claim by the reasoning provider.

    DISPROVEN   : evidence actively contradicts the claim (fabrication signal).
    UNVERIFIED  : no corroborating evidence found either way (neutral).
    CONFIRMED   : independent evidence supports the claim.

    Note: tier is set ONLY by an LLMProvider reasoning step, never by the
    evidence-gathering code in verify.py.
    """

    DISPROVEN = "DISPROVEN"
    UNVERIFIED = "UNVERIFIED"
    CONFIRMED = "CONFIRMED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The only two meaningful values for Claim.expected_footprint; anything else
# (a typo, a stray enum, an old file with no such key) degrades to "", which
# the score treats exactly like "low" (contributes nothing). Same defensive
# from_dict discipline as a bad tier degrading to UNVERIFIED: garbage never
# becomes a silent accusation.
_VALID_FOOTPRINTS = {"high", "low"}


def _clamp_footprint(raw: Any) -> str:
    """Normalize an expected_footprint value to "high", "low", or "" (unknown).
    Never raises; any unexpected value becomes "" (the safe, no-effect default).
    """
    try:
        value = str(raw).strip().lower()
    except (TypeError, ValueError):
        return ""
    return value if value in _VALID_FOOTPRINTS else ""


# Mid-weight fallback for an evidence record that predates the weighted
# source registry (detective/sources/registry.py): plain web-search evidence
# carries no "weight" key at all. Neither the ceiling nor the floor, so an
# un-weighted hit is not silently trusted more, or dismissed, versus a
# weighted one. Kept here (not in detective/sources/registry.py) so models.py
# has no dependency on the sources package; the two constants are meant to
# stay equal (see detective/sources/registry.py's own DEFAULT_WEIGHT).
DEFAULT_EVIDENCE_WEIGHT = 0.5


def evidence_weight(evidence: dict[str, Any]) -> float:
    """The weight to use for one evidence record when a reasoning provider
    is combining evidence by source strength: evidence["weight"] if present,
    else DEFAULT_EVIDENCE_WEIGHT for older/plain web-search evidence that
    predates this field. Never raises on a malformed value.
    """
    raw = evidence.get("weight")
    if raw is None:
        return DEFAULT_EVIDENCE_WEIGHT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_EVIDENCE_WEIGHT


@dataclass
class Claim:
    """A single verifiable assertion decomposed from a profile.

    type      : claim category, e.g. "employment", "education", "identity"
                (person scans), or "user_count", "revenue_metric",
                "proprietary_tech", "funding", "pricing" (company/app scans).
                Just a string, no enum: adding a type is never a schema break.
    employer  : company or institution the claim is about (may be ""); for a
                company/app scan this holds the product/company name.
    title     : role or degree title (may be "").
    start     : start date as displayed, e.g. "Jan 2020" (may be "").
    end       : end date as displayed, e.g. "Present" (may be "").
    assertion : one-line human-readable statement of the claim.
    tier      : EvidenceTier, assigned by the provider (default UNVERIFIED).
    evidence  : list of dicts gathered by verify.py. Always carries
                {source_url, snippet}. May additionally carry, from the
                weighted source connectors in detective/sources/:
                  source_name      : registry key, e.g. "github", "wayback_machine"
                  weight           : float 0 to 1.0 (see detective/sources/registry.py)
                  match_confidence : "high" | "medium" | "low"
                Plain web-search evidence has none of the three extra keys;
                treat that as a default mid weight (see evidence_weight /
                DEFAULT_EVIDENCE_WEIGHT above), never as unweighted-therefore-
                untrustworthy or unweighted-therefore-authoritative.
    notes     : free-form reasoning notes from the provider.
    expected_footprint : optional "high" | "low" | "" (unknown/default),
                assigned by the reasoning provider alongside tier. It answers
                a SEPARATE question from tier: if a truthful version of this
                claim were real, would it normally leave a verifiable PUBLIC
                trace? A notable employer, a known school, or a senior/public
                role is "high" (should be corroborable); an obscure tiny
                company or a junior private role is "low" (reasonably may not
                be). Only UNVERIFIED claims marked "high" lift the founder
                score into the SUS band (see llm.compute_founder_score): we do
                not punish a legitimately low-footprint person for being hard
                to verify. Default "" behaves exactly like "low" (contributes
                nothing), so an older file or a provider that never sets it is
                the safe, no-effect case. from_dict clamps any other value to
                "" the same way a bad tier degrades to UNVERIFIED.
    """

    type: str
    employer: str = ""
    title: str = ""
    start: str = ""
    end: str = ""
    assertion: str = ""
    tier: EvidenceTier = EvidenceTier.UNVERIFIED
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    expected_footprint: str = ""
    # The claimed product's real website, once the resolution stage has decided
    # WHICH site that is (see llm.SiteResolution). Empty unless a resolution
    # came back "resolved" at high or medium confidence. This is what lets the
    # URL-keyed connectors (wayback, domain_age, techstack) run on a PERSON
    # scan, where there is no company_url at all. A resolved site substantiates
    # that the product exists, never the person's role in it.
    product_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Claim":
        tier_raw = d.get("tier", EvidenceTier.UNVERIFIED.value)
        if isinstance(tier_raw, EvidenceTier):
            tier = tier_raw
        else:
            try:
                tier = EvidenceTier(str(tier_raw).upper())
            except ValueError:
                tier = EvidenceTier.UNVERIFIED
        return cls(
            type=d.get("type", ""),
            employer=d.get("employer", ""),
            title=d.get("title", ""),
            start=d.get("start", ""),
            end=d.get("end", ""),
            assertion=d.get("assertion", ""),
            tier=tier,
            evidence=list(d.get("evidence", []) or []),
            notes=d.get("notes", ""),
            expected_footprint=_clamp_footprint(d.get("expected_footprint", "")),
            product_url=d.get("product_url", "") or "",
        )


@dataclass
class Buildability:
    """The buildability meter, attached to a company/app scan only.

    A compact factor, not a rebuild plan: no build steps, no stack, no
    run-cost estimate. It exists so the reasoning provider can fold "this is
    a trivially vibecodeable wrapper sold at a premium" into larp_score as a
    real LARP signal (overcharging IS the LARP for an app scan), while
    keeping the same disproven-vs-unverified honesty discipline as
    EvidenceTier: verify.py only gathers thin-wrapper signals (see
    verify.py's proprietary_tech queries), it never decides buildability, and
    a genuinely hard product must land MODERATE/HARD, never TRIVIAL by
    default.

    tier : one of TRIVIAL, MODERATE, HARD. Set by the reasoning provider.
        TRIVIAL  = an LLM API call plus a landing page and Stripe; thin
                   wrapper signals in the evidence support this call.
        MODERATE = real integration work, data pipelines, or non-trivial
                   UX/infra; not a moonshot but not a weekend wrapper either.
        HARD     = real infra, novel models/training, hard distribution, or
                   a regulatory/data moat.
    note : one short line of reasoning for the tier, grounded in the
        gathered evidence (e.g. "evidence shows only an OpenAI API call, no
        proprietary model" or "evidence shows custom infra, not a wrapper").
    """

    tier: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Buildability":
        return cls(
            tier=d.get("tier", ""),
            note=d.get("note", ""),
        )


@dataclass
class MetricEntry:
    """One row of a company scan's metric_breakdown (see Dossier).

    name       : metric identifier, one of the 8 company-LARP metrics, e.g.
                 "raise_inflation", "reach_vs_footprint", "product_realness",
                 "headcount_inflation", "proprietary_ai_gap",
                 "zombie_liveness", "key_role_coverage", "buildability".
    weight     : HIGH = 3, MED = 2, LOW = 1. The "buildability" row is always
                 weight 1 and is additionally hard-capped at compute time
                 (see llm.compute_company_score) so it can only nudge the
                 composite, never push it to high-LARP by itself.
    score_0_10 : 0 to 10 contribution to the composite. None until filled:
                 by the operator for most rows, or derived deterministically
                 from Dossier.buildability.tier for the "buildability" row
                 (see llm.sync_buildability_metric). A None score on an
                 active row blocks the whole composite (company_larp_score
                 stays None) rather than defaulting to 0, so an unscored
                 metric can never silently read as "clean".
    active     : whether this metric applies to this company scan, decided
                 by code from the decomposed claims (e.g. reach_vs_footprint
                 only fires on a consumer-scale user_count claim, never on a
                 pure B2B seat count). An inactive metric's weight is left
                 out of the composite entirely rather than dragging it down,
                 i.e. the remaining active metrics' weight is what the
                 composite normalizes against.
    note       : one line of reasoning, filled by the operator (or by code
                 for the derived "buildability" row).
    """

    name: str
    weight: int = 0
    score_0_10: Optional[int] = None
    active: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetricEntry":
        # score_0_10 is the ManualProvider queue-file field an operator
        # (human or fresh Codex reviewer) literally types by hand per
        # _COMPANY_OPERATOR_INSTRUCTIONS ("score_0_10: integer 0 to 10"), so
        # it needs the same guarded-conversion discipline every sibling
        # parser in this file already has (Claim.from_dict falls back on a
        # bad tier, evidence_weight falls back on a bad weight, and
        # llm.py's ApiProvider._apply_result clamps its own score the same
        # way). Garbage input degrades to None (unscored), never a crash and
        # never a silently-wrong number: compute_company_score already
        # blocks the composite on a None score in an active row rather than
        # reading it as "clean".
        raw_score = d.get("score_0_10")
        score_0_10: Optional[int] = None
        if raw_score is not None:
            try:
                score_0_10 = max(0, min(10, round(float(raw_score))))
            except (TypeError, ValueError):
                score_0_10 = None
        return cls(
            name=d.get("name", ""),
            weight=int(d.get("weight", 0) or 0),
            score_0_10=score_0_10,
            active=bool(d.get("active", False)),
            note=d.get("note", ""),
        )


@dataclass
class Dossier:
    """The full evidence file produced for one profile or company/app scan.

    profile_url    : the URL that was analyzed.
    scan_type      : "person" (default, existing behavior) or "company_app".
    scan_depth     : "full" (default) or "shallow". Computed from the extraction
                     manifest (see dossier.scan_depth), never from intent. A
                     "shallow" scan (injected profile, or a live scrape that
                     parsed zero experience) cannot accrue absence-based
                     suspicion: GAP findings are suppressed and the scorer adds
                     no unverified/SUS points, so a degraded scan can never
                     masquerade as a real SUS verdict. Old files without this
                     key load as "full" (the pre-existing behavior).
    identity       : {name, headline, current_company, location} for a person
                     scan; for a company scan this holds the product's own
                     identity fields (name, tagline/headline, company, "").
    raw_experience : list of raw experience entry dicts (unparsed by the brain).
    claims         : list[Claim] decomposed and verified.
    larp_score     : 0 to 100, higher means more likely fabricated. None until
                     scored. Legacy free-form score, kept for backward
                     compatibility; superseded in prominence by the two
                     formalized scores below.
    verdict        : free-form summary string. None until scored. For a
                     company/app scan this may call out overcharging
                     consumers, but only when buildability supports it.
    buildability   : Buildability, only populated for scan_type "company_app".
                     None on person scans.
    founder_larp_score : 0 to 100, person scans only. Deterministically
                     derived (see llm.compute_founder_score) from the claim
                     tiers: DISPROVEN claims drive it up hard, UNVERIFIED
                     contributes little, CONFIRMED contributes nothing. None
                     until the operator has assigned tiers.
    company_larp_score : 0 to 100, company/app scans only. Deterministically
                     derived (see llm.compute_company_score) as a weighted
                     composite over metric_breakdown's active rows. None
                     until every active metric's score_0_10 is filled.
    metric_breakdown : list[MetricEntry], company/app scans only. The 8
                     company-LARP metrics (see MetricEntry), built by
                     llm.build_metric_breakdown from the decomposed claims
                     and filled in by the operator. Empty on person scans.
    mismatches     : list of typed cross-reference findings (dicts), produced
                     ONLY by the aggregate-then-mismatch path
                     (detective.dossier.build_dossier); always empty for the
                     per-claim detective.pipeline.run path. Each entry is a
                     MismatchFinding.to_dict() (kind CONTRADICTION / INFLATION
                     / GAP / TIMELINE, plus claimed/discovered/severity and the
                     final resolved tier). Purely additive and surfaced for the
                     overlay; it never feeds the score (the score is still
                     computed by llm.compute_founder_score /
                     compute_company_score off the claim tiers / metric rows the
                     provider set), so an old file without this key loads fine.
    generated_at   : ISO 8601 timestamp of creation.
    """

    profile_url: str
    scan_type: str = "person"
    scan_depth: str = "full"
    identity: dict[str, str] = field(default_factory=dict)
    raw_experience: list[dict[str, Any]] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    larp_score: Optional[int] = None
    verdict: Optional[str] = None
    buildability: Optional[Buildability] = None
    founder_larp_score: Optional[int] = None
    company_larp_score: Optional[int] = None
    metric_breakdown: list[MetricEntry] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    # Execution provenance. Each row records a bounded connector or reasoning
    # attempt, its terminal status, result count, timing, and target/query.
    # This describes what the scanner did, never what the subject did.
    attempt_ledger: list[dict[str, Any]] = field(default_factory=list)
    # Non-empty when most claims were never actually looked up (the lookup
    # channel was dark). The score STILL stands: unsearched claims contribute
    # nothing, a dark scan lands CLEAR rather than suspicious, and a genuine
    # contradiction must still reach the fraud band. What this carries is the
    # COVERAGE the number alone hides, so a reader can tell a checked-and-clean
    # profile apart from one we simply could not check.
    coverage_warning: str = ""
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_url": self.profile_url,
            "scan_type": self.scan_type,
            "scan_depth": self.scan_depth,
            "identity": dict(self.identity),
            "raw_experience": list(self.raw_experience),
            "claims": [c.to_dict() for c in self.claims],
            "larp_score": self.larp_score,
            "verdict": self.verdict,
            "buildability": (
                self.buildability.to_dict() if self.buildability is not None else None
            ),
            "founder_larp_score": self.founder_larp_score,
            "company_larp_score": self.company_larp_score,
            "metric_breakdown": [m.to_dict() for m in self.metric_breakdown],
            "mismatches": [dict(m) for m in self.mismatches],
            "attempt_ledger": [dict(item) for item in self.attempt_ledger],
            "coverage_warning": self.coverage_warning,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Dossier":
        buildability_raw = d.get("buildability")
        buildability = (
            Buildability.from_dict(buildability_raw) if buildability_raw else None
        )
        return cls(
            profile_url=d.get("profile_url", ""),
            scan_type=d.get("scan_type", "person"),
            scan_depth=d.get("scan_depth", "full"),
            identity=dict(d.get("identity", {}) or {}),
            raw_experience=list(d.get("raw_experience", []) or []),
            claims=[Claim.from_dict(c) for c in d.get("claims", []) or []],
            larp_score=d.get("larp_score"),
            verdict=d.get("verdict"),
            buildability=buildability,
            founder_larp_score=d.get("founder_larp_score"),
            company_larp_score=d.get("company_larp_score"),
            metric_breakdown=[
                MetricEntry.from_dict(m) for m in d.get("metric_breakdown", []) or []
            ],
            mismatches=[dict(m) for m in d.get("mismatches", []) or [] if isinstance(m, dict)],
            attempt_ledger=[
                dict(item)
                for item in d.get("attempt_ledger", []) or []
                if isinstance(item, dict)
            ],
            coverage_warning=d.get("coverage_warning", "") or "",
            generated_at=d.get("generated_at", _now_iso()),
        )
