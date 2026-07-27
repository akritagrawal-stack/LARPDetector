"""Offline tests for the corroboration-discipline addendum in
detective/llm.py's operator instructions.

Root cause this addendum targets: Gemini was reading a press article that
REPORTS someone made a claim ("Roy Lee says $7M ARR") as if it CORROBORATES
the claim, even when the claim was later self-admitted false or a court
found it false. These tests only check the prompt TEXT (no network, no
Gemini call): the actual tier-flip behavior is graded by evals/run_eval.py
against real Gemini, documented in evals/RESULTS.md.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

from detective.llm import (
    _COMPANY_OPERATOR_INSTRUCTIONS,
    _OPERATOR_INSTRUCTIONS,
    _SOURCE_WEIGHTING_INSTRUCTIONS,
)


def test_source_weighting_instructions_teach_reporting_is_not_corroboration():
    text = _SOURCE_WEIGHTING_INSTRUCTIONS
    assert "CORROBORATION DISCIPLINE" in text
    assert "REPORTING that someone MADE a claim" in text
    assert "is not evidence the claim is TRUE" in text


def test_source_weighting_instructions_teach_self_admission_is_disproven():
    text = _SOURCE_WEIGHTING_INSTRUCTIONS
    assert "ADMITTED false" in text
    assert "DISPROVEN" in text
    # The self-admission line must explicitly rule out the observed bug
    # (an admitted-false claim scored CONFIRMED).
    assert "never CONFIRMED" in text


def test_source_weighting_instructions_list_adverse_coverage_phrases():
    text = _SOURCE_WEIGHTING_INSTRUCTIONS
    for phrase in ("admitted\nlying", "convicted of", "no record of", "did not attend"):
        assert phrase in text, f"missing adverse-coverage cue: {phrase!r}"


def test_source_weighting_instructions_keep_existing_false_accusation_discipline():
    """The new corroboration-discipline paragraph must not weaken the
    existing anti-false-accusation rules: absence alone is still not
    DISPROVEN, and a single low-confidence hit still cannot flip a tier.
    """
    text = _SOURCE_WEIGHTING_INSTRUCTIONS
    assert "NEVER set\nDISPROVEN off a single low-confidence hit" in text
    assert "NEVER set DISPROVEN off\na bare absence of a record" in text
    assert "still never DISPROVEN off one low-confidence hit alone" in text
    assert "still never DISPROVEN off a bare absence of a record" in text


def test_person_and_company_instructions_both_include_corroboration_discipline():
    """The addendum lives in the shared _SOURCE_WEIGHTING_INSTRUCTIONS block,
    so both the person-scan and company-scan operator instructions inherit
    it without duplicating the text (keeps the prompt from bloating).
    """
    assert "CORROBORATION DISCIPLINE" in _OPERATOR_INSTRUCTIONS
    assert "CORROBORATION DISCIPLINE" in _COMPANY_OPERATOR_INSTRUCTIONS


def test_source_weighting_instructions_teach_embedded_utterance_discipline():
    """The Holmes false negative: a fabrication encoded as a "Claimed X" title
    decomposes into an assertion about the ACT of claiming, which is literally
    true. The instructions must tell the brain to judge the embedded FACT X,
    not whether the person uttered it.
    """
    text = _SOURCE_WEIGHTING_INSTRUCTIONS
    assert "EMBEDDED-UTTERANCE DISCIPLINE" in text
    assert "judge the TRUTH of the embedded fact" in text
    # The act of claiming being literally true must NOT read as CONFIRMED.
    assert "NOT grounds for CONFIRMED" in text
    # And an embedded fact found false is DISPROVEN even though it was said.
    assert "is DISPROVEN even though the person did in" in text


def test_embedded_utterance_discipline_does_not_weaken_false_accusation_guards():
    """The new paragraph must not lower the bar: it restates that absence and
    a single low-confidence hit still cannot flip a tier to DISPROVEN.
    """
    text = _SOURCE_WEIGHTING_INSTRUCTIONS
    assert "still never\nDISPROVEN off one low-confidence hit alone" in text
    assert "still never DISPROVEN\noff a bare absence of a record" in text


def test_person_and_company_instructions_both_include_embedded_utterance_discipline():
    assert "EMBEDDED-UTTERANCE DISCIPLINE" in _OPERATOR_INSTRUCTIONS
    assert "EMBEDDED-UTTERANCE DISCIPLINE" in _COMPANY_OPERATOR_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Workstream 5: the confirmation bar, the new mismatch bullets, and the roast
# gate. Text-only pins (no network, no Gemini call): the actual tier-flip
# behavior is graded by the principle suite and the evals.
# ---------------------------------------------------------------------------

from detective.llm import _VERDICT_TONE_INSTRUCTIONS


def test_confirmation_bar_present():
    text = _OPERATOR_INSTRUCTIONS
    assert "CONFIRMATION BAR" in text
    assert "ASSOCIATION, not confirmation" in text
    # The search_unavailable void must never be treated as suspicious.
    assert "must not be treated as suspicious" in text
    assert "search_unavailable" in text


def test_mismatch_bullets_present():
    text = _OPERATOR_INSTRUCTIONS
    assert "mismatch_tech_substance" in text
    assert "mismatch_registry_absence" in text
    # Both resolve to UNVERIFIED + high expected footprint (the SUS path)...
    assert 'expected_footprint "high"' in text or "expected_footprint 'high'" in text
    # ...with the never-DISPROVEN-alone discipline spelled out.
    assert "NEVER DISPROVEN" in text or "never DISPROVEN" in text
    # Registry absence is capped at SUS unconditionally (owner decision 2).
    assert "registry" in text.lower()


def test_roast_kept_and_gated():
    text = _VERDICT_TONE_INSTRUCTIONS
    # The Tier 2 roast material stays (a stable phrase from the examples).
    assert "cannot find a single shred" in text
    # And it is now gated on a real search.
    assert "GATE THE ROAST ON A REAL SEARCH" in text
    assert "search_unavailable" in text


def test_gap_bullet_defines_corroboration_by_substance():
    text = _OPERATOR_INSTRUCTIONS
    assert "speaks to the role/impact/scale" in text
    assert "at a real entity" in text
