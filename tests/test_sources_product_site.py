"""Offline tests for the product-site probe (detective/sources/product_site.py).

This module is deliberately DUMB. It fetches a candidate URL and reports facts
(final URL, status, title, description, parked-ness). It never decides which
candidate is the claimed product: that is a reasoning call, because prefix and
token matching on a name is exactly the namesake bug the App Store connector
already had ("Cognition" hits thousands of sites). Code harvests and probes,
the brain picks.

No network: the module's private `_get` is monkeypatched, same discipline as
tests/test_sources_app_store.py. Synthetic names throughout.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

from detective.sources import product_site


class _Resp:
    def __init__(self, text="", status=200, url="https://acmewidgets.example/"):
        self.text = text
        self.status_code = status
        self.url = url


def _page(title="Acme Widgets", description="Inventory software for small shops"):
    return f"""
    <html><head>
      <title>{title}</title>
      <meta name="description" content="{description}">
      <meta property="og:site_name" content="{title}">
    </head><body><p>hello</p></body></html>
    """


# ---------------------------------------------------------------------------
# urls_in_text: the post-mining harvester
# ---------------------------------------------------------------------------


def test_urls_in_text_finds_links_in_a_post():
    text = "Shipped v2 today. Try it at https://acmewidgets.example/pricing and tell me."
    assert product_site.urls_in_text(text) == ["https://acmewidgets.example/pricing"]


def test_urls_in_text_strips_trailing_punctuation():
    # Prose links end in sentence punctuation far more often than not.
    text = "we launched (https://acmewidgets.example), finally!"
    assert product_site.urls_in_text(text) == ["https://acmewidgets.example"]


def test_urls_in_text_dedupes_and_drops_socials_and_linkedin():
    text = (
        "https://acmewidgets.example https://acmewidgets.example "
        "https://www.linkedin.com/in/someone https://lnkd.in/abc "
        "https://twitter.com/someone"
    )
    # Own-platform and social links are never a product site; keeping them
    # would just hand the resolver noise to disambiguate against.
    assert product_site.urls_in_text(text) == ["https://acmewidgets.example"]


def test_urls_in_text_is_empty_for_nothing():
    assert product_site.urls_in_text("") == []
    assert product_site.urls_in_text(None) == []


def test_extract_named_product_links_reads_exact_labeled_anchors(monkeypatch):
    html = """
    <html><body>
      <a href="https://trytalkr.example/app">Fern</a>
      <a href="https://unrelated.example/">Consulting</a>
      <a href="/articles/fern">Read about Fern</a>
    </body></html>
    """
    monkeypatch.setattr(
        product_site,
        "_get",
        lambda url: _Resp(
            text=html,
            status=200,
            url="https://janedoe.example/",
        ),
    )

    links = product_site.extract_named_product_links(
        "https://janedoe.example/", "Fern"
    )

    assert links == [
        "https://trytalkr.example/app",
        "https://janedoe.example/articles/fern",
    ]


def test_extract_subject_identity_hints_recovers_declared_github(monkeypatch):
    html = """
    <html><body>
      <a href="https://github.com/janedoe">GitHub</a>
      <a href="https://x.com/janedoe">X</a>
    </body></html>
    """
    monkeypatch.setattr(
        product_site,
        "_get",
        lambda url: _Resp(
            text=html,
            status=200,
            url="https://janedoe.example/",
        ),
    )

    hints = product_site.extract_subject_identity_hints(
        "https://janedoe.example/"
    )

    assert hints["github_login"] == "janedoe"
    assert hints["personal_site"] == "https://janedoe.example/"
    assert hints["domain"] == "janedoe.example"


# ---------------------------------------------------------------------------
# probe_site: facts only
# ---------------------------------------------------------------------------


def test_probe_site_reports_title_and_description(monkeypatch):
    monkeypatch.setattr(product_site, "_get", lambda url: _Resp(_page()))
    probe = product_site.probe_site("https://acmewidgets.example")
    assert probe["title"] == "Acme Widgets"
    assert probe["description"] == "Inventory software for small shops"
    assert probe["status"] == 200
    assert probe["domain"] == "acmewidgets.example"
    assert probe["parked"] is False


def test_probe_site_follows_to_the_final_url(monkeypatch):
    monkeypatch.setattr(
        product_site,
        "_get",
        lambda url: _Resp(_page(), url="https://www.acmewidgets.example/home"),
    )
    probe = product_site.probe_site("http://acmewidgets.example")
    assert probe["final_url"] == "https://www.acmewidgets.example/home"
    # The domain is taken from where we LANDED, since that is what wayback and
    # domain_age will be pointed at.
    assert probe["domain"] == "acmewidgets.example"


def test_probe_site_unreachable_is_none_not_a_verdict(monkeypatch):
    # "Could not look" must be distinguishable from "looked and found nothing".
    # None means the former, and the caller contributes ZERO for it.
    def boom(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(product_site, "_get", boom)
    assert product_site.probe_site("https://acmewidgets.example") is None


def test_probe_site_dead_domain_is_a_fact_not_an_exception(monkeypatch):
    monkeypatch.setattr(product_site, "_get", lambda url: _Resp("", status=404))
    probe = product_site.probe_site("https://acmewidgets.example")
    assert probe["status"] == 404
    assert probe["title"] == ""


def test_probe_site_flags_a_parked_domain(monkeypatch):
    monkeypatch.setattr(
        product_site,
        "_get",
        lambda url: _Resp(_page(title="acmewidgets.example is for sale", description="Buy this domain")),
    )
    probe = product_site.probe_site("https://acmewidgets.example")
    assert probe["parked"] is True


def test_probe_candidates_is_bounded_and_never_raises(monkeypatch):
    seen = []

    def flaky(url):
        seen.append(url)
        if "bad" in url:
            raise RuntimeError("nope")
        return _Resp(_page())

    monkeypatch.setattr(product_site, "_get", flaky)
    urls = [f"https://site{i}.example" for i in range(10)] + ["https://bad.example"]
    probes = product_site.probe_candidates(urls, max_candidates=3)
    # Capped: a resolver pass must never turn into an unbounded crawl.
    assert len(seen) == 3
    assert len(probes) == 3
    assert all(p["title"] == "Acme Widgets" for p in probes)


def test_probe_candidates_drops_the_unreachable_ones(monkeypatch):
    def only_second(url):
        if "one" in url:
            raise RuntimeError("dead")
        return _Resp(_page())

    monkeypatch.setattr(product_site, "_get", only_second)
    probes = product_site.probe_candidates(
        ["https://one.example", "https://two.example"], max_candidates=5
    )
    assert [p["url"] for p in probes] == ["https://two.example"]


# ---------------------------------------------------------------------------
# Evidence records
# ---------------------------------------------------------------------------


def test_resolved_record_shape_and_source_name():
    probe = {
        "url": "https://acmewidgets.example",
        "final_url": "https://acmewidgets.example/",
        "domain": "acmewidgets.example",
        "title": "Acme Widgets",
        "description": "Inventory software",
        "status": 200,
        "parked": False,
    }
    rec = product_site.resolved_record("Acme Widgets", probe, confidence="high", rationale="the post links it")
    assert rec["source_name"] == "product_site"
    assert rec["source_url"] == "https://acmewidgets.example/"
    assert rec["match_confidence"] == "high"
    assert rec["resolution"] == "resolved"
    assert "Acme Widgets" in rec["snippet"]
    assert "the post links it" in rec["snippet"]
    assert isinstance(rec["weight"], (int, float))


def test_resolved_record_says_existence_is_not_the_role_claim():
    # The single most important line in this module. A live, well-built site
    # proves the PRODUCT exists; it says nothing about who built it or how many
    # users it has. The snippet must carry that to the brain explicitly, since
    # "CONFIRMED web product" otherwise reads as "claim cleared".
    probe = {
        "url": "https://acmewidgets.example",
        "final_url": "https://acmewidgets.example/",
        "domain": "acmewidgets.example",
        "title": "Acme Widgets",
        "description": "",
        "status": 200,
        "parked": False,
    }
    rec = product_site.resolved_record("Acme Widgets", probe, confidence="high", rationale="x")
    snippet = rec["snippet"].lower()
    assert "does not" in snippet
    assert "role" in snippet or "who" in snippet


def test_not_found_record_is_a_searched_absence_capped_at_sus():
    rec = product_site.not_found_record("Acme Widgets", candidates_seen=4)
    assert rec["source_name"] == "product_site"
    assert rec["resolution"] == "not_found"
    assert rec["weight"] == 0.0
    snippet = rec["snippet"].lower()
    # Absence language must be explicit about its ceiling, mirroring the
    # authoritative-registry rule: SUS at most, never DISPROVEN.
    assert "sus" in snippet
    assert "disproven" in snippet


def test_records_carry_no_registry_check_key():
    # detect_registry_absence fires off registry_check == "absent" for
    # AUTHORITATIVE registries (Apple's catalog, YC's directory). The open web
    # is not one, so a not-found web product must never be able to trip it.
    probe = {
        "url": "https://a.example", "final_url": "https://a.example/", "domain": "a.example",
        "title": "T", "description": "", "status": 200, "parked": False,
    }
    assert "registry_check" not in product_site.resolved_record("A", probe, confidence="high", rationale="x")
    assert "registry_check" not in product_site.not_found_record("A", candidates_seen=0)
