"""Offline parser tests for the company/app landing-page extractor.

MUST pass with no network. Exercises parse_company_page against the synthetic
overpriced-wrapper fixture, which is the parser spec for this scan type.
No em dashes (house rule).
"""

from __future__ import annotations

from pathlib import Path

from detective.extract_company import fetch_company, parse_company_page
from detective.llm import build_metric_breakdown, mechanical_decompose_company

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "company_wrapper_landing.html"


def _profile():
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_company_page(html, "https://resumegenie.example/")


def test_fixture_exists():
    assert FIXTURE.exists(), "company wrapper landing fixture is missing"


def test_product_identity():
    profile = _profile()
    assert profile["scan_type"] == "company_app"
    assert profile["identity"]["name"] == "ResumeGenie AI"
    assert "AI resume optimizer" in profile["identity"]["headline"] or (
        "advanced AI resume optimizer" in profile["identity"]["headline"]
    )


def test_pricing_extracted():
    profile = _profile()
    tiers = profile["pricing"]["tiers"]
    assert len(tiers) >= 1
    assert any(t["price"] == "$49" and t["period"] == "mo" for t in tiers)


def test_user_count_claim_extracted():
    profile = _profile()
    user_metrics = [m for m in profile["metrics"] if m["type"] == "user_count"]
    assert user_metrics, "expected a user_count metric to be extracted"
    assert any(m["value"].replace(",", "") == "100000" for m in user_metrics)


def test_headcount_claim_extracted():
    profile = _profile()
    headcount_metrics = [m for m in profile["metrics"] if m["type"] == "headcount"]
    assert headcount_metrics, "expected a headcount metric to be extracted"
    assert any(m["value"] == "12" for m in headcount_metrics)


def test_user_count_metric_carries_unit():
    profile = _profile()
    user_metrics = [m for m in profile["metrics"] if m["type"] == "user_count"]
    assert any(m.get("unit") == "users" for m in user_metrics)


def test_proprietary_ai_claim_extracted():
    profile = _profile()
    tech_claims = [c for c in profile["tech_claims"] if c["type"] == "proprietary_tech"]
    assert tech_claims, "expected a proprietary-tech claim to be extracted"
    assert any("proprietary" in c["text"].lower() for c in tech_claims)


def test_integrations_extracted():
    profile = _profile()
    assert "LinkedIn" in profile["integrations"]
    assert "Google Docs" in profile["integrations"]
    assert "Notion" in profile["integrations"]


def test_empty_html_degrades_gracefully():
    profile = parse_company_page("", "https://x.test/")
    assert profile["scan_type"] == "company_app"
    assert profile["identity"]["name"] == ""
    assert profile["pricing"]["tiers"] == []
    assert profile["metrics"] == []
    assert profile["tech_claims"] == []
    assert profile["integrations"] == []


def test_fetch_company_refuses_without_live():
    try:
        fetch_company("https://resumegenie.example/", live=False)
        assert False, "fetch_company must refuse when live=False"
    except RuntimeError as exc:
        assert "live=False" in str(exc) or "live" in str(exc).lower()


# ---------------------------------------------------------------------------
# Bug 1: product-name mis-parse. The old extractor took the hero <h1> first,
# so a real landing page (Browser Use / Gregor Zunic, YC W25) whose h1 is a
# marketing tagline ("The Way AI uses the web.") and whose real product name
# ("Browser Use") only appears in <title> got the name wrong, which poisoned
# every downstream evidence query (they searched the tagline sentence and
# pulled in unrelated results like xAI/Grok/Opera).
# ---------------------------------------------------------------------------


def test_browser_use_shaped_fixture_name_comes_from_title_not_tagline_h1():
    html = """
    <html><head>
      <title>Browser Use - The Way AI uses the web.</title>
      <meta name="description" content="Open-source browser automation for AI agents.">
    </head><body>
      <h1>The Way AI uses the web.</h1>
      <p class="tagline">Open-source browser automation for AI agents.</p>
      <section>
        <p>Browser Use is fully open-source. Install our SDK with pip
        install browser-use and get started in minutes.</p>
        <p>100k+ stars on GitHub, with thousands of downloads every week.</p>
      </section>
    </body></html>
    """
    profile = parse_company_page(html, "https://browseruse.example/")
    assert profile["identity"]["name"] == "Browser Use"
    assert profile["identity"]["name"] != "The Way AI uses the web."


def test_browser_use_shaped_fixture_stars_metric_captured():
    html = """
    <html><head><title>Browser Use - The Way AI uses the web.</title></head>
    <body><h1>The Way AI uses the web.</h1>
    <p>100k+ stars on GitHub, with thousands of downloads every week.</p>
    </body></html>
    """
    profile = parse_company_page(html, "https://browseruse.example/")
    star_metrics = [m for m in profile["metrics"] if m["type"] == "user_count" and m.get("unit") == "stars"]
    assert star_metrics, "expected a 'stars' user_count metric to be captured"
    assert any(m["value"].lower() == "100k" for m in star_metrics)


def test_og_site_name_takes_priority_over_title_and_h1():
    html = (
        '<html><head><title>Ignored Title Sentence Here.</title>'
        '<meta property="og:site_name" content="RealName"></head>'
        "<body><h1>RealName does the thing you need.</h1></body></html>"
    )
    profile = parse_company_page(html, "https://x.test/")
    assert profile["identity"]["name"] == "RealName"


def test_logo_alt_fallback_when_no_title_or_og_site_name():
    html = (
        '<html><head></head><body><img alt="Acme Corp logo">'
        "<h1>Automate everything you do, every single day.</h1></body></html>"
    )
    profile = parse_company_page(html, "https://x.test/")
    assert profile["identity"]["name"] == "Acme Corp"


def test_tagline_only_h1_never_fabricates_a_name():
    # With no title, no og:site_name, no logo, and an h1 that reads as a
    # marketing sentence, the name must stay empty rather than being
    # promoted from the tagline; the tagline is still captured separately.
    html = "<html><head></head><body><h1>The Way AI uses the web.</h1></body></html>"
    profile = parse_company_page(html, "https://x.test/")
    assert profile["identity"]["name"] == ""
    assert profile["identity"]["headline"] == "The Way AI uses the web."


def test_short_h1_product_name_still_works_with_no_other_source():
    # A short, non-sentence h1 (no trailing period, <=5 words, no verb
    # opener) is still a valid name fallback when nothing else is present.
    html = "<html><head></head><body><h1>Notion</h1></body></html>"
    profile = parse_company_page(html, "https://x.test/")
    assert profile["identity"]["name"] == "Notion"


# ---------------------------------------------------------------------------
# Bug 5 (coverage): unit + keyword gates.
# ---------------------------------------------------------------------------


def test_user_count_regex_recognizes_new_traction_units():
    html = (
        "<html><head><title>Acme</title></head><body>"
        "<p>Used by 5,000 developers and counting, with 2,000 installs "
        "last month alone.</p>"
        "</body></html>"
    )
    profile = parse_company_page(html, "https://x.test/")
    user_metrics = [m for m in profile["metrics"] if m["type"] == "user_count"]
    units = {m["unit"] for m in user_metrics}
    assert "developers" in units
    assert "installs" in units


def test_sdk_package_page_activates_proprietary_ai_gap_metric():
    html = """
    <html><head><title>Browser Use</title></head><body>
    <h1>Automate your browser with AI agents.</h1>
    <p>Browser Use is fully open-source. Install our SDK with pip
    install browser-use and get started in minutes.</p>
    </body></html>
    """
    profile = parse_company_page(html, "https://browseruse.example/")
    tech_claims = [c for c in profile["tech_claims"] if c["type"] == "proprietary_tech"]
    assert tech_claims, "expected an SDK/package tech claim to be extracted"

    claims = mechanical_decompose_company(profile)
    breakdown = {m.name: m for m in build_metric_breakdown(claims)}
    assert breakdown["proprietary_ai_gap"].active is True
    assert breakdown["key_role_coverage"].active is True


def test_custom_ai_model_claim_activates_proprietary_ai_gap_metric():
    html = (
        "<html><head><title>Acme AI</title></head><body>"
        "<h1>Acme AI</h1>"
        "<p>We built a custom AI model trained on our own private data.</p>"
        "</body></html>"
    )
    profile = parse_company_page(html, "https://x.test/")
    tech_claims = [c for c in profile["tech_claims"] if c["type"] == "proprietary_tech"]
    assert tech_claims


def test_plain_marketing_page_does_not_activate_proprietary_ai_gap_metric():
    html = """
    <html><head><title>Acme Corp</title></head><body>
    <h1>Acme Corp</h1>
    <p>We help teams move faster and get more done every day. Our custom
    pricing model fits every team.</p>
    </body></html>
    """
    profile = parse_company_page(html, "https://x.test/")
    tech_claims = [c for c in profile["tech_claims"] if c["type"] == "proprietary_tech"]
    assert not tech_claims, "a plain marketing page must not fire a tech claim"

    claims = mechanical_decompose_company(profile)
    breakdown = {m.name: m for m in build_metric_breakdown(claims)}
    assert breakdown["proprietary_ai_gap"].active is False
