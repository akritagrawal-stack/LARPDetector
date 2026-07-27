"""Offline regression tests for the blind-test subjects (documented real-world
LARPers plus Sam Altman / Elon Musk calibration controls).

These lock in three things so they never silently regress, all WITHOUT a live
Gemini call or any network (the free-tier daily cap makes re-scoring on demand
impossible, so we freeze the LLM's tier assignments and re-derive the composite
scores from them):

  1. The wizard-of-oz hardening in verify._proprietary_tech_queries: a loud
     proprietary-AI claim must be probed for BOTH the thin-wrapper shape and
     the humans-behind-the-curtain shape.
  2. The company metric-activation logic: proprietary_ai_gap and
     key_role_coverage must activate on a proprietary_tech claim.
  3. The scored dossiers captured by evals/run_eval.py (evals/cache/
     *_scored.json): re-running compute_founder_score / compute_company_score
     on their frozen tiers must keep frauds in the LARP band and calibration
     controls out of it (proportionality: one exaggeration must not nuke a
     mostly-legit founder).

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detective import verify
from detective.llm import (
    build_metric_breakdown,
    compute_company_score,
    compute_founder_score,
    mechanical_decompose_company,
)
from detective.models import Claim, Dossier, EvidenceTier

ROOT = Path(__file__).resolve().parent.parent
EVAL_CASES_DIR = ROOT / "evals" / "cases"
# The scored dossiers are committed here (public-figure public-web evidence) so
# these regression tests always run on a fresh checkout. evals/cache/ is
# gitignored (it holds gathered scan evidence), so it is only the fallback for a
# freshly re-run eval that has not been copied across yet.
COMMITTED_SCORED_DIR = ROOT / "tests" / "fixtures" / "blind_scored"
CACHE_DIR = ROOT / "evals" / "cache"


# ---------------------------------------------------------------------------
# 1. verify.py hardening: wizard-of-oz query variant
# ---------------------------------------------------------------------------


def test_proprietary_tech_queries_probe_both_larp_shapes():
    claim = Claim(
        type="proprietary_tech",
        employer="Builder.ai",
        assertion='Builder.ai claims: "Natasha builds your app with AI."',
    )
    queries = verify._proprietary_tech_queries("Builder.ai", claim)
    texts = [q for q, _role in queries]
    joined = " ".join(texts).lower()

    # The thin-wrapper probe (an API call to an existing model) must survive.
    assert any("built on" in t.lower() for t in texts), texts
    # The wizard-of-oz probe (humans behind the curtain) is the hardening: a
    # humans-pretending-to-be-AI story never contains "built on OpenAI", so a
    # second adversarial query with exposE language is required to surface it.
    assert any(
        marker in joined
        for marker in ("human engineers", "actually humans", "ai washing", "wizard of oz")
    ), texts
    # Two adversarial probes plus one corroboration probe.
    adversarial = [role for _q, role in queries if role == verify._ROLE_ADVERSARIAL]
    assert len(adversarial) >= 2, queries


# ---------------------------------------------------------------------------
# 2. Metric activation: the wizard-of-oz metrics fire on a proprietary AI claim
# ---------------------------------------------------------------------------

_COMPANY_CASES_WITH_PROP_TECH = [
    "larp_builder_ai",
    "larp_amazon_jwo",
    "wrapper_synthetic_founder",
    "control_devin",
]


@pytest.mark.parametrize("case_name", _COMPANY_CASES_WITH_PROP_TECH)
def test_wizard_of_oz_metrics_active_on_company_cases(case_name):
    raw = json.loads((EVAL_CASES_DIR / f"{case_name}.json").read_text(encoding="utf-8"))
    raw.pop("_expected", None)
    claims = mechanical_decompose_company(raw)
    assert any(c.type == "proprietary_tech" for c in claims), case_name

    breakdown = build_metric_breakdown(claims)
    active = {m.name for m in breakdown if m.active}
    # These two metrics are the engine's wizard-of-oz / thin-wrapper surface.
    assert "proprietary_ai_gap" in active, (case_name, active)
    assert "key_role_coverage" in active, (case_name, active)
    assert "product_realness" in active and "buildability" in active


# ---------------------------------------------------------------------------
# 3. Frozen scored dossiers: frauds stay high, controls stay proportionate
# ---------------------------------------------------------------------------


def _load_scored(case_name: str) -> Dossier:
    committed = COMMITTED_SCORED_DIR / f"{case_name}_scored.json"
    fallback = CACHE_DIR / f"{case_name}_scored.json"
    path = committed if committed.exists() else fallback
    if not path.exists():
        pytest.skip(f"scored dossier not present: {case_name} (run evals/run_eval.py)")
    return Dossier.from_dict(json.loads(path.read_text(encoding="utf-8")))


# name -> (kind, assertion). kind picks the score function; the lambda encodes
# the ground-truth band a correct engine must land in when the frozen tiers are
# re-scored. Frauds must sit in the LARP band; calibration controls must NOT be
# nuked; the honest Amazon miss is asserted only to stay computed + activated,
# not to score high (documented gap, not rigged).
_FRAUD_PERSON = ["larp_trevor_milton", "larp_charlie_javice", "larp_sbf"]
_CONTROL_PERSON = {
    "control_sam_altman": lambda s: s is not None and s <= 20,
    "control_elon_musk": lambda s: s is not None and s <= 45,  # NOT nuked
}
_COMPANY_BANDS = {
    "larp_builder_ai": lambda s: s is not None and s >= 60,        # wizard-of-oz caught
    "wrapper_synthetic_founder": lambda s: s is not None and s >= 45,  # wrapper caught
    "control_devin": lambda s: s is not None and s <= 50,          # overstated-but-real, not nuked
    "larp_amazon_jwo": lambda s: s is not None,                    # honest miss: just stays computed
}


@pytest.mark.parametrize("case_name", _FRAUD_PERSON)
def test_fraud_founders_land_in_larp_band(case_name):
    d = _load_scored(case_name)
    score = compute_founder_score(d.claims)
    assert score is not None and score >= 66, (case_name, score)
    # A fraud must carry at least one DISPROVEN claim (the thing that earns the
    # top band); this guards the DISPROVEN discipline, not just the number.
    assert any(c.tier == EvidenceTier.DISPROVEN for c in d.claims), case_name


@pytest.mark.parametrize("case_name", list(_CONTROL_PERSON))
def test_calibration_controls_not_nuked(case_name):
    d = _load_scored(case_name)
    score = compute_founder_score(d.claims)
    assert _CONTROL_PERSON[case_name](score), (case_name, score)


@pytest.mark.parametrize("case_name", list(_COMPANY_BANDS))
def test_company_scores_in_band(case_name):
    d = _load_scored(case_name)
    score = compute_company_score(d.metric_breakdown)
    assert _COMPANY_BANDS[case_name](score), (case_name, score)


# ---------------------------------------------------------------------------
# 4. Aggregate-then-mismatch hardening locks (detective.dossier), on the same
# frozen cached evidence: the GAP detector must be silent on web-verified
# controls even for a careless operator, and the autonomy-contradiction signal
# must make the AI-washing case (Amazon JWO) catchable, without regressing the
# frauds or the controls.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", ["control_elon_musk", "control_sam_altman"])
def test_gap_detector_silent_on_web_verified_controls(case_name):
    """Regression for the pilot false-positive: Musk tripped 7 GAP flags and
    Altman 4 off news-verified claims (no connector source_name). After the
    corroboration fix the detectors must produce ZERO gap findings here, so
    even a naive operator who trusts every injected gap record cannot drift a
    web-verified legit person toward SUS."""
    import copy

    from detective import dossier as dz

    d = _load_scored(case_name)
    baseline = compute_founder_score(d.claims)

    claims = copy.deepcopy(d.claims)
    findings = dz.run_detectors(claims, identity=d.identity)
    assert [f for f in findings if f.kind == "GAP"] == [], (
        case_name,
        [f.label for f in findings],
    )

    # Naive-operator drill: inject whatever fired and blindly escalate every
    # gap-flagged claim to UNVERIFIED + high footprint. The score must not move.
    dz.inject_candidates(claims, findings)
    for c in claims:
        names = {(e.get("source_name") or "") for e in c.evidence}
        if "mismatch_gap" in names:
            c.tier = EvidenceTier.UNVERIFIED
            c.expected_footprint = "high"
    assert compute_founder_score(claims) == baseline, case_name


def test_amazon_jwo_autonomy_contradiction_makes_it_catchable():
    """The pilot's detection gap: Amazon Just Walk Out (AI-washing, humans in
    the loop) scored ~4 with zero findings. The AUTONOMY detector must fire on
    the cached humans-in-the-loop evidence, and a disciplined operator who
    accepts that real contradiction (DISPROVEN on the autonomy claim) must land
    the company in the top band via the claims-aware score routing."""
    import copy

    from detective import dossier as dz

    d = _load_scored("larp_amazon_jwo")
    claims = copy.deepcopy(d.claims)
    findings = dz.run_detectors(claims, identity=d.identity)
    autonomy = [f for f in findings if f.kind == "AUTONOMY"]
    assert autonomy, [f.kind for f in findings]
    # Anchored on a proprietary_tech (claimed-autonomy) claim, quoting the
    # humans-in-the-loop evidence.
    anchor = autonomy[0].claim_indices[0]
    assert claims[anchor].type == "proprietary_tech"

    # Frozen recompute without claims stays the honest pure-metric composite.
    base = compute_company_score(d.metric_breakdown)
    assert base is not None and base < 20

    # Disciplined operator accepts the real contradiction on the anchor claim.
    claims[anchor].tier = EvidenceTier.DISPROVEN
    after = compute_company_score(d.metric_breakdown, claims=claims)
    assert after is not None and after >= 66, (base, after)


def test_devin_control_no_autonomy_false_positive():
    """The guard side: a real (if hyped) autonomous product with no humans-in-
    the-loop exposE in its evidence must produce NO autonomy finding, and its
    score must be unchanged by the claims-aware routing (nothing DISPROVEN)."""
    from detective import dossier as dz

    d = _load_scored("control_devin")
    findings = dz.run_detectors(d.claims, identity=d.identity)
    assert [f for f in findings if f.kind == "AUTONOMY"] == []
    assert compute_company_score(d.metric_breakdown, claims=d.claims) == compute_company_score(
        d.metric_breakdown
    )


@pytest.mark.parametrize("case_name", _FRAUD_PERSON)
def test_fraud_contradictions_survive_gap_hardening(case_name):
    """The frauds' CONTRADICTION candidates must still fire on the cached
    evidence after the GAP hardening (the detection side must not regress)."""
    from detective import dossier as dz

    d = _load_scored(case_name)
    findings = dz.run_detectors(d.claims, identity=d.identity)
    assert any(f.kind == "CONTRADICTION" for f in findings), case_name


def test_wrapper_flagged_without_false_accusation():
    """The synthetic wrapper (a subject with NO real-world exposE) must be
    flagged via buildability/proprietary_ai_gap, but with NO DISPROVEN claim:
    there is nothing external to disprove, so a DISPROVEN here would be a
    false accusation. This locks the false-accusation guard on the wrapper path.
    """
    d = _load_scored("wrapper_synthetic_founder")
    assert not any(c.tier == EvidenceTier.DISPROVEN for c in d.claims), (
        "synthetic wrapper must not earn a DISPROVEN (nothing external to disprove)"
    )
    assert d.buildability is not None and d.buildability.tier == "TRIVIAL"
