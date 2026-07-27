"""Posts / activity extraction and post-claim decomposition tests.

Two surfaces, both offline (no network, no LinkedIn login):

  PART A: parse_posts_html(html) is a PURE function, same discipline as
          parse_experience_html: it turns an activity/posts HTML block into
          raw_profile["posts"] = [{"text": ..., "url": ...}], captures the
          AUTHOR's own posts, skips bare reshares that carry no commentary,
          and never raises on missing/broken markup (degrades to []).

  PART B: mechanical_decompose scans raw_profile["posts"] and turns CHECKABLE
          quantitative boasts in post text into Claim objects (a user_count /
          revenue_metric carrying the number, with POST provenance), reusing
          the same traction machinery experience descriptions use. A post that
          only carries incidental numbers ("5 lessons from 3 years") produces
          NO magnitude claim. These are still only CLAIMS the person made:
          UNVERIFIED by default, never auto-true.

Also covers the extended checkable-claim extraction from experience
DESCRIPTIONS (owner-flagged: a "$4.8M managed" plus a named certification were
in the description, not the title): money-managed / AUM claims carrying the
dollar amount, and named certification claims. No em dashes (house rule).
"""

from __future__ import annotations

from pathlib import Path

from detective.extract_linkedin import parse_posts_html
from detective.llm import mechanical_decompose
from detective.dossier import parse_quantity

FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITY_FIXTURE = FIXTURES / "activity_posts_sample.html"
# Synthetic fixture matching the stable container and commentary markers from
# LinkedIn's authenticated activity DOM, plus one nested bare reshare.
LIVE_ACTIVITY_FIXTURE = FIXTURES / "activity_live_sample.html"


def _posts():
    html = ACTIVITY_FIXTURE.read_text(encoding="utf-8")
    return parse_posts_html(html)


def test_multi_affiliation_headline_does_not_create_fake_post_product_claim():
    profile = {
        "identity": {
            "name": "Riley Morgan",
            "headline": "Returning SDE Intern @ AWS, Jane Street AMP | CS @ Texas A&M",
            "current_company": "",
        },
        "experience": [
            {
                "title": "Software Engineering Intern",
                "company": "Amazon Web Services (AWS)",
                "start_date": "May 2025",
                "end_date": "Aug 2025",
            }
        ],
        "posts": [
            {
                "text": "We reached 80 users this week.",
                "url": "https://www.linkedin.com/feed/update/example",
            }
        ],
    }

    claims = mechanical_decompose(profile)

    assert not any(
        claim.type == "user_count"
        and "AWS, Jane Street AMP" in (claim.employer or "")
        for claim in claims
    )


def _live_posts():
    html = LIVE_ACTIVITY_FIXTURE.read_text(encoding="utf-8")
    return parse_posts_html(html)


# ---------------------------------------------------------------------------
# PART A: parse_posts_html
# ---------------------------------------------------------------------------


def test_activity_fixture_exists():
    assert ACTIVITY_FIXTURE.exists(), "activity posts fixture is missing"


def test_parse_posts_captures_own_post_text():
    posts = _posts()
    texts = [p.get("text", "") for p in posts]
    assert any("Organize Campus just crossed 50,000 users" in t for t in texts)
    assert any("5 lessons from 3 years" in t for t in texts)


def test_parse_posts_skips_bare_reshare_without_commentary():
    # The nested original post (someone else's "$10M Series A") must NOT be
    # captured as the author's own post.
    posts = _posts()
    texts = " ".join(p.get("text", "") for p in posts)
    assert "Series A" not in texts
    # Exactly the two authored posts, nothing else.
    assert len(posts) == 2


def test_parse_posts_live_dom_extracts_author_commentary():
    # Real LinkedIn activity DOM: the author's own commentary lives in
    # update-components-text / update-components-update-v2__commentary inside a
    # feed-shared-update-v2 root. The parser must pull both real posts out of
    # the live markup (this is the regression the parser previously returned []).
    posts = _live_posts()
    texts = [p.get("text", "") for p in posts]
    assert any("accelerator interview" in t for t in texts)
    assert any("Founder Workshop" in t for t in texts)


def test_parse_posts_live_dom_builds_permalink_from_data_urn():
    # The live activity DOM carries no per-post anchor href, so the permalink is
    # built from the post root's data-urn (urn:li:activity:...).
    posts = _live_posts()
    urls = [p.get("url", "") for p in posts]
    assert any(
        u == "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000001/"
        for u in urls
    )


def test_parse_posts_live_dom_skips_bare_reshare():
    # The bare reshare (nested original, no author commentary) carries only
    # someone else's "$25M Series B" words; they must never enter this subject's
    # posts, and only the two authored posts survive.
    posts = _live_posts()
    joined = " ".join(p.get("text", "") for p in posts)
    assert "Series B" not in joined
    assert len(posts) == 2


def test_parse_posts_shape_is_list_of_text_dicts():
    posts = _posts()
    assert isinstance(posts, list)
    for p in posts:
        assert isinstance(p, dict)
        assert isinstance(p.get("text", ""), str)
        assert p.get("text", "").strip()


def test_parse_posts_empty_html_returns_empty_never_raises():
    assert parse_posts_html("") == []
    assert parse_posts_html("   ") == []


def test_parse_posts_broken_markup_returns_empty_never_raises():
    # No posts section at all: degrade to [] rather than raise.
    assert parse_posts_html("<html><body><div>nothing here</div></body></html>") == []
    assert parse_posts_html("<<<not even valid>>>") == []


# ---------------------------------------------------------------------------
# PART B: mechanical_decompose over posts
# ---------------------------------------------------------------------------


def test_post_traction_becomes_user_count_claim_with_number_and_provenance():
    raw = {
        "identity": {"name": "Jane Founder", "current_company": "Organize Campus"},
        "experience": [],
        "posts": [
            {"text": "Update: Organize Campus just crossed 50,000 users!"},
        ],
    }
    claims = mechanical_decompose(raw)
    user_counts = [c for c in claims if c.type == "user_count"]
    assert len(user_counts) == 1
    c = user_counts[0]
    # The number must be machine-readable by the SAME parser detect_inflation
    # uses, else the cross-check silently never fires.
    assert parse_quantity(c.assertion) == 50000
    # Employer/product set to the subject's current product so the App Store
    # connector cross-checks the RIGHT product's real footprint.
    assert c.employer == "Organize Campus"
    # Provenance: it must be discoverable that this came from a POST (in the
    # assertion or the notes), so downstream weighs "said in a post", not fact.
    prov = (c.assertion + " " + (c.notes or "")).lower()
    assert "post" in prov
    # Discipline: a post claim is UNVERIFIED by default, never auto-true.
    from detective.models import EvidenceTier

    assert c.tier == EvidenceTier.UNVERIFIED


def test_post_with_only_incidental_numbers_makes_no_magnitude_claim():
    raw = {
        "identity": {"name": "Jane Founder", "current_company": "Organize Campus"},
        "experience": [],
        "posts": [
            {"text": "5 lessons from 3 years of building."},
        ],
    }
    claims = mechanical_decompose(raw)
    assert not any(c.type in ("user_count", "revenue_metric", "money_managed", "funding") for c in claims)


def test_post_rejects_currency_prefixed_customer_count_and_bare_revenue_noise():
    raw = {
        "identity": {"name": "Jane Founder", "current_company": "Organize Campus"},
        "experience": [],
        "posts": [
            {
                "text": (
                    "Planning 2026 revenue while working with $10k+ customers. "
                    "Here are 4 revenue lessons."
                )
            },
        ],
    }
    claims = mechanical_decompose(raw)
    assert not any(c.type in ("user_count", "revenue_metric") for c in claims)


def test_post_accepts_real_user_and_revenue_magnitudes():
    raw = {
        "identity": {"name": "Jane Founder", "current_company": "Organize Campus"},
        "experience": [],
        "posts": [
            {"text": "We crossed 50,000 users and reached $2M ARR."},
        ],
    }
    claims = mechanical_decompose(raw)
    assert len([c for c in claims if c.type == "user_count"]) == 1
    assert len([c for c in claims if c.type == "revenue_metric"]) == 1


def test_no_posts_key_is_safe_and_changes_nothing():
    # The zero-regression guarantee: a raw_profile with no "posts" key must
    # decompose exactly as before (only the identity claim here).
    raw = {"identity": {"name": "Jane Founder"}, "experience": [], "education": []}
    claims = mechanical_decompose(raw)
    assert [c.type for c in claims] == ["identity"]


# ---------------------------------------------------------------------------
# Extended checkable extraction from experience DESCRIPTIONS
# (owner-flagged: "$4.8M managed" + named cert were in the description).
# ---------------------------------------------------------------------------


def test_description_money_managed_becomes_claim_carrying_dollar_amount():
    raw = {
        "identity": {"name": "Sam Analyst"},
        "experience": [
            {
                "title": "Portfolio Manager",
                "company": "Aggie Investment Fund",
                "description": "Managed a $4.8M equity portfolio, CFA Level II candidate.",
            }
        ],
    }
    claims = mechanical_decompose(raw)
    money = [c for c in claims if c.type == "money_managed"]
    assert len(money) == 1
    assert parse_quantity(money[0].assertion) == 4_800_000


def test_description_named_certification_becomes_certification_claim():
    raw = {
        "identity": {"name": "Sam Analyst"},
        "experience": [
            {
                "title": "Portfolio Manager",
                "company": "Aggie Investment Fund",
                "description": "Managed a $4.8M equity portfolio, CFA Level II candidate.",
            }
        ],
    }
    claims = mechanical_decompose(raw)
    certs = [c for c in claims if c.type == "certification"]
    assert len(certs) == 1
    assert "CFA Level II" in certs[0].assertion


def test_description_incidental_number_makes_no_magnitude_or_money_claim():
    raw = {
        "identity": {"name": "Sam Analyst"},
        "experience": [
            {
                "title": "Advisor",
                "company": "Some Firm",
                "description": "Advised clients over 10 years.",
            }
        ],
    }
    claims = mechanical_decompose(raw)
    assert not any(
        c.type in ("user_count", "revenue_metric", "money_managed", "funding", "certification")
        for c in claims
    )


def test_description_led_team_becomes_headcount_claim():
    raw = {
        "identity": {"name": "Sam Lead"},
        "experience": [
            {
                "title": "Engineering Manager",
                "company": "Some Firm",
                "description": "Led a team of 40 engineers across three time zones.",
            }
        ],
    }
    claims = mechanical_decompose(raw)
    headcounts = [c for c in claims if c.type == "headcount"]
    assert len(headcounts) == 1
    assert parse_quantity(headcounts[0].assertion) == 40


def test_incidental_team_of_5_still_never_becomes_a_claim():
    # Preserves the existing contract (test_dossier): a throwaway "team of 5"
    # with no leadership verb abutting it must NOT become a magnitude claim.
    raw = {
        "identity": {"name": "Sam Analyst"},
        "experience": [
            {
                "title": "Data Analyst",
                "company": "Some Firm",
                "description": "Presented findings to a team of 5.",
            }
        ],
    }
    claims = mechanical_decompose(raw)
    assert not any(c.type == "headcount" for c in claims)
    assert not any("team of 5" in (c.assertion or "") for c in claims)


def test_post_funding_boast_becomes_funding_claim():
    raw = {
        "identity": {"name": "Jane Founder", "current_company": "Organize Campus"},
        "experience": [],
        "posts": [
            {"text": "Thrilled to share we raised $2M to build Organize Campus."},
        ],
    }
    claims = mechanical_decompose(raw)
    funding = [c for c in claims if c.type == "funding"]
    assert len(funding) == 1
    assert parse_quantity(funding[0].assertion) == 2_000_000
