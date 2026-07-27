"""Orchestration: fetch -> decompose -> verify -> score -> dossier.

Progress is emitted through a simple callback so the same shape maps onto a
future websocket event stream. Events emitted:
    ("status", str)          coarse stage updates
    ("claim",  Claim)        one per claim after evidence gathering
    ("verdict", Dossier)     final scored dossier

No em dashes in this file (house rule).
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

from .models import Buildability, Dossier
from .audit import AttemptLedger
from .llm import (
    LLMProvider,
    ManualProvider,
    build_metric_breakdown,
    build_person_company_assessments,
    finalize_dossier_scores,
)
from . import verify
from . import pitchbook

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, object], None]


def safe_print(*args, **kwargs) -> None:
    """print(), but never raises UnicodeEncodeError on a narrow console
    codepage (e.g. Windows cp1252 printing an accented name like "Gregor
    Zunic"). Bug 2: a profile's own text (names, headlines, assertions)
    reaches the console verbatim in _default_progress below and in
    __main__._print_dossier, and a non-ASCII character in that text used to
    crash the whole run. __main__.main() also reconfigures stdout/stderr to
    UTF-8 at CLI entry (belt and suspenders); this fallback covers any other
    caller of these print helpers, and any stream reconfigure() cannot touch
    (e.g. one that predates Python 3.7 or was swapped out by a test/host).
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        stream = kwargs.get("file") or sys.stdout
        encoding = getattr(stream, "encoding", None) or "ascii"
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args) + end
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe_text)


def _default_progress(event: str, payload: object) -> None:
    if event == "status":
        safe_print(f"[pipeline] {payload}")
    elif event == "claim":
        c = payload
        safe_print(f"[pipeline]   claim: {getattr(c, 'assertion', c)}")
    elif event == "verdict":
        d = payload
        safe_print(f"[pipeline] verdict ready (larp_score={getattr(d, 'larp_score', None)})")


def run(
    url: str,
    provider: Optional[LLMProvider] = None,
    live: bool = False,
    progress: Optional[ProgressFn] = None,
    raw_profile: Optional[dict] = None,
    scan_type: str = "person",
    engine: str = "per_claim",
    offline: bool = False,
) -> Dossier:
    """Run the full LARP-detection pipeline for one profile or company/app URL.

    Args:
        url: the LinkedIn profile URL (scan_type "person"), or the product
             landing page URL (scan_type "company_app").
        provider: reasoning brain (defaults to ManualProvider).
        live: pass True to allow a real fetch (gated). Ignored if a
              raw_profile is supplied.
        progress: optional (event, payload) callback; defaults to printing.
        raw_profile: optional pre-fetched profile dict (bypasses the fetch
              step, useful for offline demos and tests). If it carries its
              own "scan_type" key (e.g. from --company-file or a live
              fetch_company call), that value wins over the scan_type arg.
        scan_type: "person" (default, existing behavior) or "company_app".
              Only consulted when raw_profile is None (selects which fetcher
              to use) or when raw_profile has no "scan_type" key of its own.
        engine: which DETECTION engine runs after the fetch step.
              "per_claim" (default, unchanged behavior): the serial
              decompose -> per-claim gather -> score path implemented below.
              "dossier": the hardened aggregate-then-mismatch engine
              (detective.dossier.build_dossier), run over the SAME fetched (or
              injected) raw_profile. The fetch step, its live gate, and the
              progress event vocabulary (status/claim/verdict) are identical
              either way, so a caller (the overlay service) can A/B or roll
              back purely by flipping this flag. Kept as an explicit param
              (default per_claim) so direct callers and the CLI are untouched;
              the overlay selects the engine via LARP_ENGINE in service.py.

    Returns:
        A scored Dossier (unscored if a ManualProvider job is still pending).
    """
    provider = provider or ManualProvider()
    emit = progress or _default_progress
    ledger = AttemptLedger(
        getattr(provider, "_audit_job_id", "")
        or getattr(provider, "job_id", "")
    )

    # 1. Fetch (or accept an injected raw profile). Person and company scans
    # use different fetchers, gated the same way (refuse without live=True).
    if raw_profile is None:
        if offline:
            raise ValueError("offline mode requires a supplied raw_profile")
        if scan_type == "company_app":
            emit("status", f"fetching company page: {url}")
            from .extract_company import fetch_company  # lazy: keeps offline paths clean

            with ledger.attempt("extract", "company_page", target=url) as attempt:
                raw_profile = fetch_company(url, live=live)
                attempt.finish(
                    "completed",
                    result_count=1,
                    final_url=(raw_profile or {}).get("profile_url", url),
                )
        else:
            emit("status", f"fetching profile: {url}")
            from .extract_linkedin import fetch_profile  # lazy: keeps offline paths clean

            with ledger.attempt("extract", "linkedin_profile", target=url) as attempt:
                raw_profile = fetch_profile(url, live=live)
                attempt.finish(
                    "completed",
                    result_count=len((raw_profile or {}).get("experience", []) or []),
                    final_url=(raw_profile or {}).get("profile_url", url),
                    metadata={
                        "experience_count": len(
                            (raw_profile or {}).get("experience", []) or []
                        ),
                        "posts_count": len((raw_profile or {}).get("posts", []) or []),
                        "company_about_loaded": bool(
                            ((raw_profile or {}).get("_extraction") or {}).get(
                                "company_about_loaded"
                            )
                        ),
                        "company_website_recovered": bool(
                            ((raw_profile or {}).get("_extraction") or {}).get(
                                "company_website_recovered"
                            )
                        ),
                    },
                )
    else:
        emit("status", "using supplied raw profile (no fetch)")
        with ledger.attempt("extract", "supplied_profile", target=url) as attempt:
            attempt.finish(
                "supplied",
                result_count=len(raw_profile.get("experience", []) or []),
            )

    # An injected raw_profile's own scan_type (if present) is authoritative,
    # so a --company-file JSON or a fetch_company() result is routed
    # correctly even if the caller passed the default scan_type arg.
    effective_scan_type = raw_profile.get("scan_type") or scan_type or "person"
    raw_profile["scan_type"] = effective_scan_type

    # Brand an INJECTED raw_profile (supplied by a caller, no extraction
    # manifest of its own: every _*_blind_scan.py / eval fixture, or any future
    # path that skips the real fetch) so it is explicitly recorded as a
    # non-scraped profile. dossier.scan_depth then classifies it "shallow" and
    # the honesty layer (absence suppression, no SUS scoring) engages
    # automatically, with no change needed in those callers. A live fetch above
    # already carries its own "_extraction" (see extract_linkedin.fetch_profile
    # / extract_company.fetch_company), so this only ever stamps the gap.
    if "_extraction" not in raw_profile:
        raw_profile["_extraction"] = {"method": "injected"}

    # Depth from the manifest alone (never from intent), threaded onto the
    # Dossier and into the founder scorer below so a shallow scan can never
    # accrue absence-based suspicion. The dossier engine computes this itself
    # from the same raw_profile; the per-claim path below applies it here.
    from .dossier import scan_depth as _scan_depth  # lazy: avoids import cycle

    depth = _scan_depth(raw_profile)

    # Engine fork. The fetch above (with its live gate) is shared; only the
    # DETECTION path differs. "dossier" hands the already-fetched raw_profile
    # to build_dossier, which does its own decompose/aggregate/cross-reference
    # and emits the SAME (status/claim/verdict) events this function does, so
    # every downstream consumer (the overlay's cinematic layer, the CLI
    # printer, tests) sees an identical stream. build_dossier reuses the same
    # provider verbatim, so ManualProvider's queue flow and ApiProvider's
    # ApiProviderError both propagate out of here exactly as the per-claim path
    # would (build_dossier never swallows them). Imported lazily to avoid any
    # import cycle (dossier.py imports pipeline.safe_print lazily in turn).
    if (engine or "per_claim").strip().lower() == "dossier":
        from .dossier import build_dossier

        return build_dossier(
            raw_profile,
            provider=provider,
            emit=emit,
            scan_type=effective_scan_type,
            allow_network=not offline,
            attempt_ledger=ledger,
        )

    identity = raw_profile.get("identity", {}) or {}

    # 2. Decompose into claims (mechanical). decompose_claims itself branches
    # on raw_profile["scan_type"] to route person vs company claim shapes.
    emit("status", "decomposing claims")
    with ledger.attempt("decompose", type(provider).__name__) as attempt:
        claims = provider.decompose_claims(raw_profile)
        attempt.finish("completed", result_count=len(claims))
    for index, claim in enumerate(claims):
        claim._attempt_ledger = ledger
        claim._claim_index = index
    person_company_assessments = (
        build_person_company_assessments(raw_profile, claims)
        if effective_scan_type == "person"
        else []
    )

    # 3. Gather evidence per claim (no tier set here).
    # One PitchBookBudget is shared across every claim in this profile so the
    # hard per-profile cap on PitchBook lookups is enforced across the whole
    # run, not per claim. None (PitchBook untouched) when PITCHBOOK_ENABLED is
    # not set, so the disabled path never even imports curl_cffi at call time.
    pb_budget = pitchbook.PitchBookBudget() if pitchbook.is_enabled() else None
    # The company/app scan's own profile_url is the one domain the
    # wayback / domain_age source connectors can key off (see
    # verify._gather_site_history_evidence); a person scan has no such URL.
    company_url = (
        raw_profile.get("profile_url", url) if effective_scan_type == "company_app" else None
    )
    emit("status", f"gathering evidence for {len(claims)} claim(s)")
    for claim in claims:
        if not offline:
            verify.gather_evidence(claim, identity, pb_budget=pb_budget, company_url=company_url)
        emit("claim", claim)

    dossier = Dossier(
        profile_url=raw_profile.get("profile_url", url),
        scan_type=effective_scan_type,
        scan_depth=depth,
        identity=identity,
        raw_experience=raw_profile.get("experience", []) or [],
        claims=claims,
        attempt_ledger=ledger.snapshot(),
    )

    # A company/app scan gets an unfilled buildability scaffold (tier, note)
    # and the 8-row metric_breakdown skeleton (active flags decided from the
    # claims just decomposed above) for the reasoning step (human operator
    # or, later, ApiProvider) to complete. Person scans keep
    # dossier.buildability as None and dossier.metric_breakdown empty.
    if effective_scan_type == "company_app":
        dossier.buildability = Buildability()
        dossier.metric_breakdown = build_metric_breakdown(claims)
    else:
        dossier.company_assessments = person_company_assessments

    # 4. Assign tiers + larp_score + verdict (the reasoning step). For a
    # company scan this is also where buildability and each active
    # metric_breakdown row get filled in; larp_score must factor buildability
    # in (a trivially vibecodeable product sold at a premium scores higher on
    # LARP; see llm._COMPANY_OPERATOR_INSTRUCTIONS).
    emit("status", "assigning tiers and verdict")
    with ledger.attempt("reasoning", type(provider).__name__) as attempt:
        dossier = provider.assign_tiers_and_verdict(dossier)
        attempt.finish("completed", result_count=len(dossier.claims))

    # 5. Formalized composite scores, computed here in code (never by the
    # provider) from whatever the reasoning step just filled in.
    finalize_dossier_scores(dossier)

    dossier.attempt_ledger = ledger.snapshot()
    emit("verdict", dossier)
    return dossier
