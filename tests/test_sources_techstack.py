"""Offline tests for detective.sources.techstack. No network: the internal
_fetch function is monkeypatched with realistic sample HTML + header shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.techstack as techstack


def _html_page(body: str) -> str:
    return f"<!doctype html><html><head><title>Test</title></head><body>{body}</body></html>"


_WEBFLOW_HTML = """<!doctype html>
<html data-wf-page="abc123" data-wf-site="def456">
<head>
<link rel="stylesheet" href="https://cdn.prod.website-files.com/abc/def.css">
</head>
<body>
<h1>Welcome to our real product</h1>
<p>We are a fast growing startup with lots of substance and real content here that goes on
for a while so this page reads as a genuine static marketing site rather than an empty shell.</p>
<script src="https://assets.website-files.com/abc/webflow.js"></script>
</body>
</html>
"""

_BUBBLE_HTML = """<!doctype html>
<html>
<head><meta name="generator" content="Bubble"></head>
<body>
<div id="app"></div>
<script>window.bubble_fn_init = function() {};</script>
<img src="https://s3.amazonaws.com/appforest_uf/f123/logo.png">
<p>Some marketing copy about our platform that has enough words in the body to not look
like an empty client-rendered shell when this heuristic strips the tags and counts characters.</p>
</body>
</html>
"""

_WIX_HTML = """<!doctype html>
<html>
<head></head>
<body>
<script src="https://static.parastorage.com/services/wix-thunderbolt/dist/main.js"></script>
<img src="https://static.wixstatic.com/media/abc123.jpg">
<p>Real marketing copy describing our company in enough detail that the visible text length
comfortably clears the minimum threshold used to detect an empty client-rendered shell page.</p>
</body>
</html>
"""

_WORDPRESS_ELEMENTOR_HTML = """<!doctype html>
<html>
<head><meta name="generator" content="WordPress 6.4"></head>
<body class="elementor-page">
<link rel="stylesheet" href="/wp-content/plugins/elementor/assets/css/frontend.min.css">
<script src="/wp-includes/js/jquery/jquery.min.js"></script>
<p>A WordPress site built with the Elementor page builder, with plenty of visible marketing
copy in the body so it clears the minimum-visible-text threshold for a real static page.</p>
</body>
</html>
"""

_PLAIN_WORDPRESS_HTML = """<!doctype html>
<html>
<head><meta name="generator" content="WordPress 6.4"></head>
<body>
<script src="/wp-includes/js/jquery/jquery.min.js"></script>
<p>A hand-coded WordPress theme with no visual page builder plugin markers anywhere on this
page, just plain WordPress core asset paths and plenty of real marketing copy in the body.</p>
</body>
</html>
"""

_LLM_WRAPPER_HTML = """<!doctype html>
<html>
<head></head>
<body>
<h1>AI Assistant</h1>
<p>Our proprietary AI helps you write faster with cutting-edge technology built in-house
by our world class research team, so this reads as a substantial static marketing page.</p>
<script>
async function ask(prompt) {
  const resp = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {"Authorization": "Bearer sk-xxx"},
    body: JSON.stringify({model: "gpt-4", messages: [{role: "user", content: prompt}]})
  });
  return resp.json();
}
</script>
</body>
</html>
"""

_CUSTOM_STACK_HTML = """<!doctype html>
<html>
<head></head>
<body>
<h1>Our Real Product</h1>
<p>This is a genuinely custom-built application with a hand rolled backend, its own design
system, and substantial marketing copy describing years of engineering work that went into
building this product from scratch without relying on any third party no-code platform or
directly embedding a call to a well known large language model API endpoint anywhere here.</p>
</body>
</html>
"""

_EMPTY_SHELL_HTML = """<!doctype html>
<html>
<head><title>App</title></head>
<body>
<div id="root"></div>
<script src="/static/js/bundle.js"></script>
</body>
</html>
"""

_REAL_WEB_APP_HTML = """<!doctype html>
<html>
<head><title>Acme App</title><link rel="manifest" href="/manifest.json"></head>
<body>
<h1>Sign in to Acme</h1>
<form action="/api/session" method="post">
  <input type="email" name="email">
  <input type="password" name="password">
  <button type="submit">Sign in</button>
</form>
<script src="/assets/app.abc123.js"></script>
</body>
</html>
"""


def _fake_fetch(html: str, headers: dict | None = None):
    def _fetch(url: str):
        return html, (headers or {})

    return _fetch


# ---------------------------------------------------------------------------
# verify_techstack: basic gating
# ---------------------------------------------------------------------------


def test_blank_url_returns_empty():
    assert techstack.verify_techstack("") == []
    assert techstack.verify_techstack(None) == []


def test_fetch_failure_returns_empty(monkeypatch):
    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(techstack, "_fetch", boom)
    assert techstack.verify_techstack("https://example.com") == []


def test_fetch_returns_none_yields_empty(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", lambda url: None)
    assert techstack.verify_techstack("https://example.com") == []


# ---------------------------------------------------------------------------
# No-code / low-code builder detection
# ---------------------------------------------------------------------------


def test_webflow_detected(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_WEBFLOW_HTML))
    evidence = techstack.verify_techstack("https://example.webflow.io")
    assert len(evidence) == 1
    record = evidence[0]
    assert record["buildability_hint"] == "no_code_detected"
    assert record["match_confidence"] == "medium"
    assert "Webflow" in record["snippet"]


def test_bubble_detected(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_BUBBLE_HTML))
    evidence = techstack.verify_techstack("https://example.bubbleapps.io")
    record = evidence[0]
    assert record["buildability_hint"] == "no_code_detected"
    assert "Bubble" in record["snippet"]


def test_wix_detected(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_WIX_HTML))
    evidence = techstack.verify_techstack("https://example.wixsite.com/mysite")
    record = evidence[0]
    assert record["buildability_hint"] == "no_code_detected"
    assert "Wix" in record["snippet"]


def test_wordpress_with_page_builder_detected(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_WORDPRESS_ELEMENTOR_HTML))
    evidence = techstack.verify_techstack("https://example.com")
    record = evidence[0]
    assert record["buildability_hint"] == "no_code_detected"
    assert "WordPress page builder" in record["snippet"]


def test_plain_wordpress_without_builder_marker_is_not_flagged_no_code(monkeypatch):
    # Bare WordPress core asset paths alone (no elementor/divi/wpbakery/etc)
    # must NOT trigger "WordPress page builder": that is not the trivially
    # rebuildable signal this connector targets.
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_PLAIN_WORDPRESS_HTML))
    evidence = techstack.verify_techstack("https://example.com")
    record = evidence[0]
    assert record["buildability_hint"] == "custom_stack"


# ---------------------------------------------------------------------------
# LLM-wrapper (thin-wrapper) signal detection
# ---------------------------------------------------------------------------


def test_llm_wrapper_signal_detected(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_LLM_WRAPPER_HTML))
    evidence = techstack.verify_techstack("https://example.com")
    record = evidence[0]
    assert record["buildability_hint"] == "llm_wrapper_signals"
    assert record["match_confidence"] == "medium"
    assert "api.openai.com" in record["snippet"]


def test_no_code_marker_takes_priority_over_llm_wrapper_signal(monkeypatch):
    combined_html = _WEBFLOW_HTML.replace(
        "</body>",
        '<script>fetch("https://api.openai.com/v1/x")</script></body>',
    )
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(combined_html))
    evidence = techstack.verify_techstack("https://example.com")
    assert evidence[0]["buildability_hint"] == "no_code_detected"


# ---------------------------------------------------------------------------
# custom_stack: CRITICAL, never proof of substance
# ---------------------------------------------------------------------------


def test_custom_stack_not_proof_of_substance(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_CUSTOM_STACK_HTML))
    evidence = techstack.verify_techstack("https://example.com")
    record = evidence[0]
    assert record["buildability_hint"] == "custom_stack"
    assert record["match_confidence"] == "low"
    assert "NOT proof" in record["snippet"]
    assert "invisible" in record["snippet"] or "mask" in record["snippet"]


def test_inconclusive_for_near_empty_spa_shell(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_EMPTY_SHELL_HTML))
    evidence = techstack.verify_techstack("https://example.com")
    record = evidence[0]
    assert record["buildability_hint"] == "inconclusive"
    assert record["match_confidence"] == "low"


# ---------------------------------------------------------------------------
# Web-application surface, independent of App Store presence
# ---------------------------------------------------------------------------


def test_real_web_auth_surface_is_reported(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_REAL_WEB_APP_HTML))
    record = techstack.verify_techstack("https://app.example.com")[0]

    assert record["web_app_hint"] == "interactive_surface"
    assert "password/auth form" in record["snippet"]
    assert "application bundle" in record["snippet"]
    assert "does not prove the authenticated workflow works" in record["snippet"]


def test_marketing_page_is_not_misreported_as_a_working_app(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_CUSTOM_STACK_HTML))
    record = techstack.verify_techstack("https://example.com")[0]

    assert record["web_app_hint"] == "marketing_only"
    assert "marketing surface only" in record["snippet"]
    assert "not proof that the product is fake" in record["snippet"]


def test_empty_spa_shell_is_distinct_from_a_verified_workflow(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_EMPTY_SHELL_HTML))
    record = techstack.verify_techstack("https://example.com")[0]

    assert record["web_app_hint"] == "client_shell"
    assert "client application shell" in record["snippet"]
    assert "execution was not verified" in record["snippet"]


def test_browser_runtime_probe_can_verify_rendered_interaction(monkeypatch):
    monkeypatch.setenv("WEB_RUNTIME_ENABLED", "1")
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_EMPTY_SHELL_HTML))
    monkeypatch.setattr(
        techstack,
        "_runtime_probe",
        lambda url: {
            "final_url": url + "/login",
            "status": 200,
            "title": "Acme Login",
            "visible_text_chars": 420,
            "inputs": 2,
            "password_inputs": 1,
            "forms": 1,
            "buttons": 1,
            "app_links": 0,
            "script_count": 3,
            "failed_requests": 0,
        },
    )

    record = techstack.verify_techstack("https://app.example.com")[0]

    assert record["runtime_app_hint"] == "runtime_interactive"
    assert "executed in headless Chromium" in record["snippet"]
    assert "password inputs: 1" in record["snippet"]
    assert "does not submit credentials" in record["snippet"]


def test_browser_runtime_probe_reports_verified_route_interaction(monkeypatch):
    monkeypatch.setenv("WEB_RUNTIME_ENABLED", "1")
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_EMPTY_SHELL_HTML))
    monkeypatch.setattr(
        techstack,
        "_runtime_probe",
        lambda url: {
            "final_url": url,
            "status": 200,
            "title": "Acme",
            "visible_text_chars": 200,
            "inputs": 0,
            "password_inputs": 0,
            "forms": 0,
            "buttons": 0,
            "app_links": 1,
            "script_count": 3,
            "failed_requests": 0,
            "interaction_attempted": True,
            "interaction_succeeded": True,
            "interaction_route": url + "/app",
            "interaction_final_url": url + "/app",
            "interaction_status": 200,
            "interaction_visible_text_chars": 500,
            "interaction_inputs": 1,
            "interaction_buttons": 2,
        },
    )

    record = techstack.verify_techstack("https://app.example.com")[0]

    assert record["runtime_app_hint"] == "interaction_verified"
    assert "Tested route: https://app.example.com/app" in record["snippet"]


def test_runtime_discovers_and_executes_same_site_app_route(monkeypatch):
    monkeypatch.setenv("WEB_RUNTIME_ENABLED", "1")
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_CUSTOM_STACK_HTML))
    monkeypatch.setattr(
        techstack,
        "_discover_public_app_route",
        lambda url: "https://app.example.com/sign-in",
    )

    def probe(url):
        if url == "https://example.com":
            return {
                "final_url": url,
                "status": 200,
                "title": "Marketing",
                "visible_text_chars": 300,
                "inputs": 1,
                "password_inputs": 0,
                "forms": 0,
                "buttons": 3,
                "app_links": 0,
                "script_count": 3,
                "failed_requests": 0,
            }
        return {
            "final_url": url,
            "status": 200,
            "title": "Sign in",
            "visible_text_chars": 120,
            "inputs": 2,
            "password_inputs": 1,
            "forms": 1,
            "buttons": 1,
            "app_links": 0,
            "script_count": 3,
            "failed_requests": 0,
        }

    monkeypatch.setattr(techstack, "_runtime_probe", probe)

    record = techstack.verify_techstack("https://example.com")[0]

    assert record["runtime_app_hint"] == "interaction_verified"
    assert "https://app.example.com/sign-in" in record["snippet"]


def test_browser_runtime_failure_does_not_erase_static_evidence(monkeypatch):
    monkeypatch.setenv("WEB_RUNTIME_ENABLED", "1")
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_REAL_WEB_APP_HTML))
    monkeypatch.setattr(techstack, "_runtime_probe", lambda url: None)

    record = techstack.verify_techstack("https://app.example.com")[0]

    assert record["web_app_hint"] == "interactive_surface"
    assert record["runtime_app_hint"] == "unavailable"


# ---------------------------------------------------------------------------
# Header-based detection: markers can also show up in response headers.
# ---------------------------------------------------------------------------


def test_marker_detected_via_response_headers(monkeypatch):
    monkeypatch.setattr(
        techstack,
        "_fetch",
        _fake_fetch(_CUSTOM_STACK_HTML, headers={"X-Wix-Request-Id": "abc", "Link": "<https://static.wixstatic.com/x>; rel=preload"}),
    )
    evidence = techstack.verify_techstack("https://example.com")
    assert evidence[0]["buildability_hint"] == "no_code_detected"


# ---------------------------------------------------------------------------
# Evidence record shape + registry weight
# ---------------------------------------------------------------------------


def test_evidence_record_shape_and_weight(monkeypatch):
    monkeypatch.setattr(techstack, "_fetch", _fake_fetch(_WEBFLOW_HTML))
    evidence = techstack.verify_techstack("https://example.com")
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
        "buildability_hint",
        "web_app_hint",
        "runtime_app_hint",
    }
    assert record["source_name"] == "techstack"
    assert record["weight"] == 0.384
    assert record["source_url"] == "https://example.com"


def test_never_reaches_high_confidence(monkeypatch):
    for html in (_WEBFLOW_HTML, _BUBBLE_HTML, _WIX_HTML, _LLM_WRAPPER_HTML, _CUSTOM_STACK_HTML, _EMPTY_SHELL_HTML):
        monkeypatch.setattr(techstack, "_fetch", _fake_fetch(html))
        evidence = techstack.verify_techstack("https://example.com")
        assert evidence[0]["match_confidence"] in ("medium", "low")


# ---------------------------------------------------------------------------
# Live smoke tests (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_techstack_webflow_site():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real fetch")

    evidence = techstack.verify_techstack("https://webflow.com")
    assert evidence, "expected the fetch to succeed against a known Webflow-adjacent page"


def test_live_techstack_custom_site():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real fetch")

    evidence = techstack.verify_techstack("https://www.python.org")
    assert evidence, "expected the fetch to succeed against a known custom-stack site"
