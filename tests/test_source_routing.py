"""The reasoning layer must be told WHERE to look, per claim shape.

THE INCIDENT (live, 2026-07-24): the director was asked to plan follow-ups for a
profile whose student-org role, ambassador program, university research role and
professional certification had all come back empty. It proposed nothing for any
of them, reasoning that "a student/ambassador role is plausibly invisible online
for a genuine person". Two things were wrong with that:

  1. Those four claims had never been searched at all. The web-search channel
     was dark, so their only evidence was the search_unavailable marker.
  2. Every one of those shapes has an obvious public source. Member-publishing
     organizations exist to advertise their members, and credential bodies run
     public registers. Unchecked was being reported as unremarkable.

The instructions had licensed exactly this: "do not propose a follow-up for an
obscure/low-footprint claim that a genuine person could plausibly have with no
public trace", with no catalogue of sources to check that guess against.

The fix is by claim SHAPE, never by naming an organization: hardcoding "Texas
A&M" or any specific org would fix one profile and no others.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import re

from detective import llm as llm_module


def _flat(text: str) -> str:
    """Normalized text: the source wraps these strings mid-sentence."""
    return re.sub(r"\s+", " ", text).lower()


PLAN = _flat(llm_module._PLAN_INSTRUCTIONS)
PERSON = _flat(llm_module._OPERATOR_INSTRUCTIONS)
COMPANY = _flat(llm_module._COMPANY_OPERATOR_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# The excuse is gone
# ---------------------------------------------------------------------------


def test_the_invisibility_license_is_gone_from_the_planner():
    # The exact sentence the director quoted back when it skipped four claims.
    assert "plausibly have with no public trace" not in PLAN


def test_the_planner_forbids_excusing_a_claim_without_checking_the_map():
    assert "do not excuse a claim as untraceable" in PLAN


def test_junior_sounding_roles_are_explicitly_high_footprint():
    # The standing rule: recognition IS the product these orgs offer, so they
    # publish their people. Junior-sounding is not the same as untraceable.
    assert "high-footprint claims" in PLAN
    assert "ambassador" in PLAN
    assert "student org" in PLAN


def test_a_never_searched_claim_is_the_best_follow_up_not_a_skippable_one():
    assert "never searched" in PLAN
    assert "search_unavailable" in PLAN
    assert "best follow-up candidate" in PLAN


# ---------------------------------------------------------------------------
# The source map is present, and reaches every reasoning surface
# ---------------------------------------------------------------------------


def test_the_catalogue_covers_the_shapes_that_were_missed():
    cat = _flat(llm_module._SOURCE_CATALOGUE)
    # Member-publishing orgs: the shape that got waved off.
    assert "roster" in cat and "members" in cat
    # University research roles.
    assert "lab" in cat and "department directory" in cat
    # Certifications: check the ISSUER's register, not a mention.
    assert "issuing body" in cat and "register" in cat
    # And the founder/product shape the web-app path feeds.
    assert "product's own website" in cat


def test_the_catalogue_routes_by_shape_not_by_named_organizations():
    # A hardcoded org list would fix one profile and no others. No specific
    # organization, school or credential may appear in the shared map.
    cat = llm_module._SOURCE_CATALOGUE
    for specific in ("Texas A&M", "Superagent", "CFA", "LinkedIn", "Cognition", "Y Combinator"):
        assert specific not in cat, f"catalogue hardcodes {specific!r}"


def test_the_planner_carries_the_catalogue():
    assert "where to look, by claim shape" in PLAN


def test_both_scoring_prompts_carry_the_catalogue():
    # The director proposes lookups and the scoring step judges what came back.
    # If only one of them has the map they disagree about what was checkable.
    assert "where to look, by claim shape" in PERSON
    assert "where to look, by claim shape" in COMPANY


def test_the_catalogue_states_that_unchecked_is_neither_clean_nor_suspicious():
    cat = _flat(llm_module._SOURCE_CATALOGUE)
    assert "unchecked as clean" in cat
