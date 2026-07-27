"""Offline tests for detective/images.py.

Every network call is mocked; nothing here ever hits the real network. Covers
the three rich-image sources: the server-side profile-photo proxy (data URI
success + graceful 403 miss), the Clearbit logo domain builder / slug guess,
and the og:image extractor/fetcher.

No em dashes anywhere (house rule).
"""

from __future__ import annotations

import base64
from unittest import mock

from detective import images


class _FakeResp:
    def __init__(self, status_code=200, content=b"", text="", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {}


# ---------------------------------------------------------------------------
# 1. Profile-photo proxy -> data: URI
# ---------------------------------------------------------------------------


def test_proxy_image_returns_data_uri_on_image_bytes():
    fake_bytes = b"\xff\xd8\xff\xe0jpeg-bytes-here"
    resp = _FakeResp(200, content=fake_bytes, headers={"Content-Type": "image/jpeg"})
    with mock.patch.object(images.requests, "get", return_value=resp) as g:
        uri = images.proxy_image_as_data_uri("https://media.licdn.com/dms/image/x.jpg")

    assert uri is not None
    assert uri.startswith("data:image/jpeg;base64,")
    # The bytes round-trip exactly.
    b64 = uri.split(",", 1)[1]
    assert base64.b64decode(b64) == fake_bytes
    # Browser-like headers + linkedin Referer are actually sent (the whole
    # point of the proxy: dodge licdn's hotlink 403).
    _, kwargs = g.call_args
    assert "Chrome" in kwargs["headers"]["User-Agent"]
    assert kwargs["headers"]["Referer"] == "https://www.linkedin.com/"


def test_proxy_image_returns_none_on_403():
    resp = _FakeResp(403, content=b"denied", headers={"Content-Type": "text/html"})
    with mock.patch.object(images.requests, "get", return_value=resp):
        assert images.proxy_image_as_data_uri("https://media.licdn.com/dms/image/x.jpg") is None


def test_proxy_image_returns_none_on_non_image_content_type():
    resp = _FakeResp(200, content=b"<html></html>", headers={"Content-Type": "text/html"})
    with mock.patch.object(images.requests, "get", return_value=resp):
        assert images.proxy_image_as_data_uri("https://example.com/notimage") is None


def test_proxy_image_returns_none_on_network_error():
    with mock.patch.object(images.requests, "get", side_effect=Exception("timeout")):
        assert images.proxy_image_as_data_uri("https://media.licdn.com/x.jpg") is None


def test_proxy_image_returns_none_on_empty_url():
    # No network call at all for an empty url.
    with mock.patch.object(images.requests, "get", side_effect=AssertionError("should not fetch")):
        assert images.proxy_image_as_data_uri("") is None
        assert images.proxy_image_as_data_uri("   ") is None


def test_proxy_image_passes_cookies_through():
    resp = _FakeResp(200, content=b"img", headers={"Content-Type": "image/png"})
    with mock.patch.object(images.requests, "get", return_value=resp) as g:
        images.proxy_image_as_data_uri("https://media.licdn.com/x.png", cookies={"li_at": "secret"})
    _, kwargs = g.call_args
    assert kwargs["cookies"] == {"li_at": "secret"}


# ---------------------------------------------------------------------------
# 2. Clearbit logo builder + domain guess / slugify
# ---------------------------------------------------------------------------


def test_slugify_company_strips_suffixes_and_punctuation():
    assert images.slugify_company("Acme Labs, Inc.") == "acme"
    assert images.slugify_company("Stripe") == "stripe"
    assert images.slugify_company("OpenAI LLC") == "openai"
    assert images.slugify_company("Two Words Co") == "twowords"
    assert images.slugify_company("") == ""


def test_slugify_company_never_strips_whole_name():
    # A one-word company that happens to be a suffix word survives.
    assert images.slugify_company("Labs") == "labs"


def test_guess_company_domain():
    assert images.guess_company_domain("Stripe") == "stripe.com"
    assert images.guess_company_domain("Acme Labs, Inc.") == "acme.com"
    assert images.guess_company_domain("") == ""


def test_clearbit_logo_url_from_domain():
    assert images.clearbit_logo_url("stripe.com") == "https://logo.clearbit.com/stripe.com"


def test_clearbit_logo_url_normalizes_scheme_and_www():
    assert images.clearbit_logo_url("https://www.Stripe.com/careers") == "https://logo.clearbit.com/stripe.com"
    assert images.clearbit_logo_url("") == ""


def test_clearbit_logo_url_from_guessed_domain():
    # The full source #2 path: company name -> domain -> logo url.
    domain = images.guess_company_domain("Stripe")
    assert images.clearbit_logo_url(domain) == "https://logo.clearbit.com/stripe.com"


# ---------------------------------------------------------------------------
# 2b. favicon_logo_url: the fallback source used when Clearbit fails to load
# client-side (logo.clearbit.com is confirmed dead: it does not resolve).
# ---------------------------------------------------------------------------


def test_favicon_logo_url_from_domain():
    # sz=256: the largest size Google's s2/favicons endpoint honors (see the
    # docstring: a bigger sz does not buy a higher-resolution source, it is
    # just the least-bad source for the overlay to downscale a rendered logo
    # from). Regression guard for the blur fix: this used to request sz=128.
    assert images.favicon_logo_url("stripe.com") == "https://www.google.com/s2/favicons?domain=stripe.com&sz=256"


def test_favicon_logo_url_normalizes_scheme_and_www():
    got = images.favicon_logo_url("https://www.Stripe.com/careers")
    assert got == "https://www.google.com/s2/favicons?domain=stripe.com&sz=256"
    assert images.favicon_logo_url("") == ""


def test_favicon_logo_url_accepts_a_custom_size():
    assert images.favicon_logo_url("stripe.com", size=64) == "https://www.google.com/s2/favicons?domain=stripe.com&sz=64"


def test_favicon_logo_url_is_a_different_host_than_clearbit():
    # The whole point: a real, independent second source, not a second URL on
    # the same (confirmed-dead) host.
    domain = "stripe.com"
    assert "logo.clearbit.com" not in images.favicon_logo_url(domain)
    assert images.clearbit_logo_url(domain) != images.favicon_logo_url(domain)


# ---------------------------------------------------------------------------
# 3. og:image extractor + fetcher
# ---------------------------------------------------------------------------

_HTML_WITH_OG = """
<html><head>
  <meta property="og:title" content="A Real Article Title" />
  <meta property="og:image" content="https://cdn.example.com/thumb.jpg" />
</head><body>hi</body></html>
"""

_HTML_TWITTER_ONLY = """
<html><head>
  <meta name="twitter:image" content="https://cdn.example.com/tw.png" />
</head></html>
"""

_HTML_NO_IMAGE = "<html><head><title>Bare Page</title></head><body>nothing</body></html>"


def test_extract_og_image_prefers_og_over_twitter():
    assert images.extract_og_image(_HTML_WITH_OG) == "https://cdn.example.com/thumb.jpg"


def test_extract_og_image_falls_back_to_twitter():
    assert images.extract_og_image(_HTML_TWITTER_ONLY) == "https://cdn.example.com/tw.png"


def test_extract_og_image_returns_none_without_meta():
    assert images.extract_og_image(_HTML_NO_IMAGE) is None
    assert images.extract_og_image("") is None


def test_extract_og_image_resolves_relative_with_base_url():
    html = '<html><head><meta property="og:image" content="/images/hero.png"></head></html>'
    got = images.extract_og_image(html, base_url="https://example.com/article/123")
    assert got == "https://example.com/images/hero.png"


def test_extract_og_title():
    assert images.extract_og_title(_HTML_WITH_OG) == "A Real Article Title"
    assert images.extract_og_title(_HTML_NO_IMAGE) == "Bare Page"
    assert images.extract_og_title("") is None


def test_fetch_og_image_success():
    resp = _FakeResp(200, text=_HTML_WITH_OG, headers={"Content-Type": "text/html; charset=utf-8"})
    with mock.patch.object(images.requests, "get", return_value=resp):
        got = images.fetch_og_image("https://example.com/article")
    assert got == ("https://cdn.example.com/thumb.jpg", "A Real Article Title")


def test_fetch_og_image_none_when_no_meta():
    resp = _FakeResp(200, text=_HTML_NO_IMAGE, headers={"Content-Type": "text/html"})
    with mock.patch.object(images.requests, "get", return_value=resp):
        assert images.fetch_og_image("https://example.com/bare") is None


def test_fetch_og_image_none_on_non_html():
    resp = _FakeResp(200, text="{}", headers={"Content-Type": "application/json"})
    with mock.patch.object(images.requests, "get", return_value=resp):
        assert images.fetch_og_image("https://example.com/api") is None


def test_fetch_og_image_none_on_network_error():
    with mock.patch.object(images.requests, "get", side_effect=Exception("boom")):
        assert images.fetch_og_image("https://example.com/x") is None


def test_fetch_og_image_none_on_empty_url():
    with mock.patch.object(images.requests, "get", side_effect=AssertionError("should not fetch")):
        assert images.fetch_og_image("") is None


# ---------------------------------------------------------------------------
# load_linkedin_cookies (best-effort, env-gated)
# ---------------------------------------------------------------------------


def test_load_linkedin_cookies_none_without_env(monkeypatch):
    monkeypatch.delenv("LINKEDIN_STATE_PATH", raising=False)
    assert images.load_linkedin_cookies() is None


def test_load_linkedin_cookies_reads_state_file(tmp_path, monkeypatch):
    state = {
        "cookies": [
            {"name": "li_at", "value": "abc", "domain": ".www.linkedin.com"},
            {"name": "other", "value": "z", "domain": ".example.com"},
        ]
    }
    p = tmp_path / "state.json"
    p.write_text(__import__("json").dumps(state), encoding="utf-8")
    monkeypatch.setenv("LINKEDIN_STATE_PATH", str(p))
    jar = images.load_linkedin_cookies()
    assert jar == {"li_at": "abc"}


def test_load_linkedin_cookies_none_on_bad_file(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("LINKEDIN_STATE_PATH", str(p))
    assert images.load_linkedin_cookies() is None
