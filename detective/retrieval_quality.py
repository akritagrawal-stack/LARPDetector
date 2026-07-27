"""Shared retrieval-quality rules used by gap detection and scoring.

Evidence volume is not coverage. Generic employer news, scam-advice pages, and
unlinked namesakes must not make an employment claim look fully searched.
"""

from __future__ import annotations

from .models import Claim


_UNAVAILABLE = "search_unavailable"
_COMPLETED_MARKERS = {
    "search_coverage",
    "searched_no_results",
    "director_followup",
    "mismatch_gap",
}
_NON_COMPLETING_SOURCES = {
    "",
    "news_coverage",
    "search_unavailable",
}


def claim_search_completed(claim: Claim) -> bool:
    """Whether a claim-specific retrieval completed with usable accounting."""
    evidence = list(claim.evidence or [])
    if any((item.get("source_name") or "") == _UNAVAILABLE for item in evidence):
        return False

    for item in evidence:
        source = (item.get("source_name") or "").strip()
        if source == "search_coverage":
            return (item.get("verification_state") or "") == "completed"
        if source in _COMPLETED_MARKERS:
            return True
        if source not in _NON_COMPLETING_SOURCES:
            # A structured connector record proves that connector ran. This
            # includes negative catalog/roster checks and namesake-only GitHub
            # matches. It does not make the record corroborating evidence.
            return True
        if (
            (item.get("claim_relevance") or "") in {"association", "substantive"}
            and (item.get("query_role") or "") in {
                "corroboration", "adversarial", "footprint"
            }
        ):
            return True
        if (
            not source
            and item.get("source_url")
            and not item.get("query_role")
        ):
            # Compatibility for older dossiers and deterministic fixtures that
            # predate retrieval accounting. New web evidence always carries
            # query_role and claim_relevance, so production searches cannot use
            # this branch to turn unrelated hits into completed coverage.
            return True

    if claim.type == "identity":
        # A namesake-only structured result still proves identity resolution ran.
        # It does not corroborate the identity, but it allows the gap detector to
        # say that the attempted resolution did not bind the profile subject.
        return any(
            (item.get("source_name") or "")
            and (item.get("match_confidence") or "").lower() in {"low", "medium", "high"}
            for item in evidence
        )

    if claim.type not in {"employment", "education"}:
        return any(bool(item) for item in evidence)
    return False
