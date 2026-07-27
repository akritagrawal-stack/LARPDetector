"""Offline regression tests for utterance-framed title decomposition.

Root cause these target (the Elizabeth Holmes false negative): a fabrication
encoded as a LinkedIn experience-entry TITLE ("Claimed Theranos technology
could run 200 or more blood tests from a single finger prick blood drop") was
slotted by mechanical_decompose into "Worked as {title} at {company}",
producing a META-claim about the ACT of claiming. That meta-claim is literally
TRUE (she did claim it), so the reasoning brain confirmed it and the fabricated
CAPABILITY was never judged.

The fix (detective.llm._reframe_utterance_title) makes decomposition assert the
EMBEDDED FACT directly when a title is an utterance-framed claim, so the thing
that gets verified is the capability, not the saying of it. These tests are
pure offline (no LLM, no network); the real tier-flip is graded by
evals/run_eval.py against Gemini and documented in evals/RESULTS.md.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import json
from pathlib import Path

from detective.llm import _reframe_utterance_title, mechanical_decompose

CASES_DIR = Path(__file__).resolve().parent / "cases"


# ---------------------------------------------------------------------------
# _reframe_utterance_title: the conservative trigger itself
# ---------------------------------------------------------------------------


def test_reframe_strips_leading_utterance_verb():
    fact = _reframe_utterance_title(
        "Claimed the device could run 200 blood tests from a single drop"
    )
    assert fact == "The device could run 200 blood tests from a single drop"


def test_reframe_strips_optional_that_after_verb():
    fact = _reframe_utterance_title(
        "Promised that revenue would exceed 100 million dollars by 2024"
    )
    assert fact == "Revenue would exceed 100 million dollars by 2024"


def test_reframe_does_not_fire_on_short_proper_noun_titles():
    """A real name or role that merely STARTS with an utterance word must be
    left alone: the length guard (_UTTERANCE_MIN_WORDS) is what protects it.
    """
    for legit in (
        "Promised Land Realty",
        "Stated Preference Research Lead",
        "Founder and CEO",
        "Said Studio Creative Director",
        "Claimed X",
    ):
        assert _reframe_utterance_title(legit) is None, legit


def test_reframe_requires_the_verb_to_LEAD_the_title():
    # A long title whose utterance verb is buried mid-phrase is NOT reframed;
    # only a leading verb signals the whole title is a restated claim.
    assert (
        _reframe_utterance_title(
            "Led a large team that claimed record growth for the company"
        )
        is None
    )


# ---------------------------------------------------------------------------
# mechanical_decompose end to end, on the regression fixture
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    return json.loads((CASES_DIR / name).read_text(encoding="utf-8"))


def test_fabricated_title_decomposes_to_embedded_fact_not_act_of_claiming():
    raw = _load_fixture("fabricated_title_larp.json")
    claims = mechanical_decompose(raw)

    # The claim decomposed from the utterance-framed title must assert the
    # CAPABILITY itself; find it by an embedded-fact keyword.
    fab = next(c for c in claims if "selfie" in c.assertion.lower())

    assert not fab.assertion.lower().startswith("worked as claimed")
    assert "claimed" not in fab.assertion.lower()
    # It must assert the actual capability that has to be verified.
    assert "diagnose" in fab.assertion.lower()
    assert "50 diseases" in fab.assertion.lower()


def test_fabricated_title_leaves_the_plain_role_claim_untouched():
    raw = _load_fixture("fabricated_title_larp.json")
    claims = mechanical_decompose(raw)

    role = next(c for c in claims if c.title == "Founder and CEO")
    # A normal (non-utterance-framed) title stays in the standard employment
    # phrasing, so the reframe is surgical, not a blanket rewrite.
    assert role.assertion == "Worked as Founder and CEO at NimbusHealth (2019 to Present)."


def test_holmes_fixture_capability_no_longer_wrapped_in_worked_as_claimed():
    """The exact case the fix exists for: the real Holmes fixture must now
    decompose its fabricated-capability title into a direct capability
    assertion, not the meta-claim that read as CONFIRMED before.
    """
    raw = _load_fixture("elizabeth_holmes.json")
    claims = mechanical_decompose(raw)

    cap = next(c for c in claims if "200 or more blood tests" in c.assertion)
    assert not cap.assertion.lower().startswith("worked as claimed")
    assert cap.assertion.lower().startswith("theranos technology could run")
