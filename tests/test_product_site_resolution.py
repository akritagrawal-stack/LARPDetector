"""Stage 1.5: resolving a claimed WEB product to its real site.

Runs between decompose and the aggregate gather, because the connectors that
can actually assess a live product (wayback, domain_age, techstack) are
URL-keyed and a person-scan founder claim carries only a NAME.

The contract this suite pins, in order of how badly each one hurts if it breaks:

  1. A resolved site NEVER clears the role claim. It substantiates that the
     PRODUCT exists, nothing more.
  2. Ambiguous contributes ZERO. It is not absence, and it must not become a
     SUS input, because an unsure pick is the wrong-site match that gets a real
     person accused.
  3. not_found is a searched absence: SUS at most, never DISPROVEN, and it must
     never be able to trip the authoritative-registry detector.
  4. The stage can never break a scan.

No network: probes and web search are monkeypatched. Synthetic names only.
No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import pytest

from detective import dossier as dossier_module
from detective.dossier import build_dossier
from detective.llm import LLMProvider, SiteResolution
from detective.models import Claim


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _StubProvider(LLMProvider):
    """Decomposes to one founder claim, records what it was asked to resolve,
    and returns whatever resolutions the test dictates."""

    def __init__(self, resolutions=None, claims=None, raises=False):
        self.resolutions = resolutions or []
        self._claims = claims
        self.raises = raises
        self.seen_requests = None

    def decompose_claims(self, raw_profile: dict) -> list[Claim]:
        if self._claims is not None:
            return [Claim(**c) if isinstance(c, dict) else c for c in self._claims]
        return [
            Claim(
                type="employment",
                employer="Acme Widgets",
                title="Founder",
                assertion="Founder at Acme Widgets",
            )
        ]

    def assign_tiers_and_verdict(self, dossier):
        return dossier

    def resolve_product_site(self, requests_, identity=None):
        self.seen_requests = requests_
        if self.raises:
            raise RuntimeError("resolver exploded")
        return self.resolutions


def _profile(**overrides):
    profile = {
        "profile_url": "https://www.linkedin.com/in/jane-doe",
        "scan_type": "person",
        "identity": {
            "name": "Jane Doe",
            "headline": "Founder at Acme Widgets",
            "hints": {"websites": ["https://acmewidgets.example"]},
        },
        "experience": [
            {
                "title": "Founder",
                "company": "Acme Widgets",
                "start_date": "2021",
                "end_date": "Present",
                "company_url": "https://www.linkedin.com/company/acme-widgets/",
            }
        ],
        "posts": [],
        # A real live scrape, so scan_depth is "full". Absence-based records are
        # withheld at shallow depth (see the shallow tests below), so the
        # default fixture has to be a genuine scan or those paths never run.
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }
    profile.update(overrides)
    return profile


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Neutralize the gather and every outbound call the stage could make."""

    def _fake_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = list(claim.evidence or [])
        return claim

    monkeypatch.setattr(dossier_module.verify, "gather_evidence", _fake_gather)
    monkeypatch.setattr(
        dossier_module.search, "web_search", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(
        dossier_module.product_site,
        "probe_site",
        lambda url: {
            "url": url,
            "final_url": url,
            "domain": "acmewidgets.example",
            "status": 200,
            "title": "Acme Widgets",
            "description": "Inventory software",
            "parked": False,
        },
    )


def _product_site_records(dossier):
    return [
        e
        for c in dossier.claims
        for e in (c.evidence or [])
        if (e.get("source_name") or "") == "product_site"
    ]


# ---------------------------------------------------------------------------
# Candidate harvest: LinkedIn's own signals first
# ---------------------------------------------------------------------------


def test_declared_websites_and_post_links_are_candidates():
    profile = _profile(
        posts=[{"text": "Shipped Acme Widgets v2 at https://acme.example/app today"}]
    )
    provider = _StubProvider()
    build_dossier(profile, provider=provider)

    urls = [c["url"] for c in provider.seen_requests[0]["candidates"]]
    assert "https://acmewidgets.example" in urls  # contact-info declared site
    assert "https://acme.example/app" in urls  # link the founder posted


def test_subject_site_product_link_repairs_missing_contact_hints(monkeypatch):
    profile = _profile(
        identity={
            "name": "Jane Doe",
            "headline": "Building software",
            "hints": {},
        }
    )
    monkeypatch.setattr(
        dossier_module.search,
        "web_search",
        lambda query, count=4: [
            {
                "title": "Jane Doe",
                "url": "https://janedoe.example/",
                "snippet": "Jane Doe previously built Acme Widgets.",
            }
        ]
        if '"Jane Doe"' in query and '"Acme Widgets"' in query
        else [],
    )
    monkeypatch.setattr(
        dossier_module.product_site,
        "extract_named_product_links",
        lambda url, product_name: ["https://app.acmewidgets.example"]
        if url == "https://janedoe.example/" and product_name == "Acme Widgets"
        else [],
    )
    monkeypatch.setattr(
        dossier_module.product_site,
        "extract_subject_identity_hints",
        lambda url: {
            "personal_site": url,
            "website": url,
            "domain": "janedoe.example",
            "websites": [url],
            "github_login": "janedoe",
        },
    )
    provider = _StubProvider()

    build_dossier(profile, provider=provider)

    request = provider.seen_requests[0]
    urls = [item["url"] for item in request["candidates"]]
    assert urls[0] == "https://app.acmewidgets.example"
    assert any(
        "first-party subject page" in line.lower()
        and "https://janedoe.example/" in line
        for line in request["context"]
    )
    assert profile["identity"]["hints"]["github_login"] == "janedoe"


def test_linkedin_company_page_is_context_never_a_candidate_site():
    # The experience row's /company/ href identifies WHICH company, which is
    # exactly the disambiguation signal the brain needs. It is NOT the product's
    # website: you cannot point wayback or a tech-stack fingerprint at
    # linkedin.com and learn anything about the product.
    provider = _StubProvider()
    build_dossier(_profile(), provider=provider)

    request = provider.seen_requests[0]
    assert all("linkedin.com" not in c["url"] for c in request["candidates"])
    assert any("linkedin.com/company/acme-widgets" in line for line in request["context"])


def test_the_claimed_product_and_role_reach_the_resolver():
    provider = _StubProvider()
    build_dossier(_profile(), provider=provider)
    request = provider.seen_requests[0]
    assert request["product_name"] == "Acme Widgets"
    assert "Founder" in request["role_text"]


def test_non_product_claims_are_not_resolved():
    provider = _StubProvider(
        claims=[
            Claim(type="education", employer="State University", assertion="BS, State University"),
        ]
    )
    build_dossier(_profile(), provider=provider)
    # A degree is not a product. No resolution request, so no wasted probes.
    assert provider.seen_requests in (None, [])


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_resolved_sets_product_url_and_attaches_one_record():
    provider = _StubProvider(
        resolutions=[
            SiteResolution(
                claim_index=0,
                url="https://acmewidgets.example",
                confidence="high",
                outcome="resolved",
                rationale="the profile's own contact link and the post both point here",
            )
        ]
    )
    dossier = build_dossier(_profile(), provider=provider)

    assert dossier.claims[0].product_url == "https://acmewidgets.example"
    records = _product_site_records(dossier)
    assert len(records) == 1
    assert records[0]["resolution"] == "resolved"


def test_first_party_link_to_differently_named_app_is_marked_as_alias(monkeypatch):
    target = "https://trytalkr.example"
    profile = _profile(
        identity={
            "name": "Jane Doe",
            "headline": "Founder at Fern",
            "hints": {"websites": ["https://janedoe.example"]},
        },
        experience=[
            {
                "title": "Founder",
                "company": "Fern",
                "start_date": "2025",
                "end_date": "Present",
            }
        ],
    )
    monkeypatch.setattr(
        dossier_module.product_site,
        "extract_named_product_links",
        lambda page_url, product_name: [target]
        if page_url == "https://janedoe.example" and product_name == "Fern"
        else [],
    )
    monkeypatch.setattr(
        dossier_module.product_site,
        "probe_site",
        lambda url: {
            "url": url,
            "final_url": url,
            "domain": "trytalkr.example",
            "status": 200,
            "title": "TalkR",
            "description": "Communication software",
            "parked": False,
        },
    )
    provider = _StubProvider(
        claims=[
            Claim(
                type="employment",
                employer="Fern",
                title="Founder",
                assertion="Founder at Fern",
            )
        ],
        resolutions=[
            SiteResolution(
                claim_index=0,
                url=target,
                confidence="high",
                outcome="resolved",
                rationale="the subject site links Fern to this destination",
            )
        ],
    )

    dossier = build_dossier(profile, provider=provider)

    record = _product_site_records(dossier)[0]
    assert record["product_name_alignment"] == "first_party_alias"
    assert record["mapping_basis"] == "subject_site_link"
    assert record["match_confidence"] == "medium"
    assert record["web_app_check_status"] == "unavailable"
    assert "rename or rebrand" in record["snippet"].lower()


def test_a_resolved_product_does_not_clear_the_role_claim():
    # THE regression that matters. A live, well-built product site behind a loud
    # founder claim must leave that claim UNVERIFIED and still SUS-eligible.
    # Two independent guards: the record is not a corroborating source, and it
    # says so in words the reasoning step reads.
    provider = _StubProvider(
        resolutions=[
            SiteResolution(
                claim_index=0, url="https://acmewidgets.example",
                confidence="high", outcome="resolved", rationale="x",
            )
        ]
    )
    dossier = build_dossier(_profile(), provider=provider)

    assert "product_site" not in dossier_module._CORROBORATING_SOURCES
    snippet = _product_site_records(dossier)[0]["snippet"].lower()
    assert "does not" in snippet and "role" in snippet
    # Existence must not have quietly upgraded the claim.
    assert dossier.claims[0].tier.value == "UNVERIFIED"


def test_not_found_attaches_a_searched_absence_that_caps_at_sus():
    provider = _StubProvider(
        resolutions=[SiteResolution(claim_index=0, outcome="not_found", confidence="high")]
    )
    dossier = build_dossier(_profile(), provider=provider)

    assert dossier.claims[0].product_url == ""
    records = _product_site_records(dossier)
    assert len(records) == 1
    assert records[0]["resolution"] == "not_found"
    assert records[0]["weight"] == 0.0
    snippet = records[0]["snippet"].lower()
    assert "sus" in snippet and "never reach disproven" in snippet


def test_not_found_can_never_trip_the_registry_absence_detector():
    # detect_registry_absence fires only off AUTHORITATIVE registries (Apple's
    # catalog, YC's directory) via registry_check == "absent". The open web is
    # not one of them.
    provider = _StubProvider(
        resolutions=[SiteResolution(claim_index=0, outcome="not_found", confidence="high")]
    )
    dossier = build_dossier(_profile(), provider=provider)
    assert all("registry_check" not in r for r in _product_site_records(dossier))
    assert not [m for m in dossier.mismatches if m.get("kind") == "REGISTRY_ABSENCE"]


def test_ambiguous_contributes_absolutely_nothing():
    # Ambiguity is NOT absence. "Several plausible Cognitions, cannot tell which"
    # must leave no URL, no evidence, and no suspicion whatsoever.
    provider = _StubProvider(
        resolutions=[
            SiteResolution(claim_index=0, outcome="ambiguous", confidence="low", rationale="four namesakes")
        ]
    )
    dossier = build_dossier(_profile(), provider=provider)
    assert dossier.claims[0].product_url == ""
    assert _product_site_records(dossier) == []


def test_unavailable_contributes_absolutely_nothing():
    provider = _StubProvider(
        resolutions=[SiteResolution(claim_index=0, outcome="unavailable", confidence="low")]
    )
    dossier = build_dossier(_profile(), provider=provider)
    assert dossier.claims[0].product_url == ""
    assert _product_site_records(dossier) == []


def test_a_resolution_for_an_out_of_range_claim_is_ignored():
    provider = _StubProvider(
        resolutions=[
            SiteResolution(claim_index=99, url="https://x.example", confidence="high", outcome="resolved")
        ]
    )
    dossier = build_dossier(_profile(), provider=provider)
    assert all(c.product_url == "" for c in dossier.claims)
    assert _product_site_records(dossier) == []


# ---------------------------------------------------------------------------
# The stage can never break a scan
# ---------------------------------------------------------------------------


def test_a_broken_resolver_never_breaks_the_scan():
    provider = _StubProvider(raises=True)
    dossier = build_dossier(_profile(), provider=provider)
    assert dossier.claims  # the scan still produced its claims
    assert _product_site_records(dossier) == []


def test_a_probe_that_raises_never_breaks_the_scan(monkeypatch):
    # Patched at the probe_candidates level on purpose: probe_site failures are
    # swallowed inside that helper, so patching there would exercise nothing
    # and this test would pass without the dossier-level guard existing.
    def boom(*a, **k):
        raise RuntimeError("network on fire")

    monkeypatch.setattr(dossier_module.product_site, "probe_candidates", boom)
    dossier = build_dossier(_profile(), provider=_StubProvider())
    assert dossier.claims
    assert _product_site_records(dossier) == []


# ---------------------------------------------------------------------------
# Scope guards: where this stage must NOT reach.
# ---------------------------------------------------------------------------


def test_a_company_scan_never_resolves_and_keeps_its_given_url(monkeypatch):
    # A company scan was handed the real landing page by the operator. Resolving
    # a name and pointing wayback/techstack at the winner instead would trade an
    # authoritative URL for a guess. The stage does not run at all there.
    captured = {}

    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        captured.setdefault("company_url", company_url)
        claim.evidence = list(claim.evidence or [])
        return claim

    monkeypatch.setattr(dossier_module.verify, "gather_evidence", _gather)

    provider = _StubProvider(
        claims=[Claim(type="user_count", employer="Acme Widgets", assertion="50,000 users.")],
        resolutions=[
            SiteResolution(
                claim_index=0, url="https://a-namesake.example",
                confidence="high", outcome="resolved", rationale="x",
            )
        ],
    )
    profile = _profile(scan_type="company_app", profile_url="https://acmewidgets.example")
    dossier = build_dossier(profile, provider=provider)

    assert provider.seen_requests is None  # the stage never ran
    assert captured["company_url"] == "https://acmewidgets.example"
    assert all(c.product_url == "" for c in dossier.claims)


def test_an_operator_supplied_company_url_outranks_a_resolved_one():
    # Belt and braces at the gate itself, independent of the stage skip above.
    from detective.verify import _checkable_site_url

    claim = Claim(
        type="company_overview", employer="Acme Widgets",
        product_url="https://a-namesake.example",
    )
    assert _checkable_site_url(claim, "https://acmewidgets.example") == "https://acmewidgets.example"


def test_a_shallow_profile_never_accrues_a_web_absence():
    # The depth rule from the judgment-layer block: an injected or thin profile
    # must not accrue absence-based suspicion. A "no web product found" record
    # is absence-based suspicion, so it is withheld at shallow depth.
    provider = _StubProvider(
        resolutions=[SiteResolution(claim_index=0, outcome="not_found", confidence="high")]
    )
    shallow = _profile(_extraction={"method": "injected"})
    dossier = build_dossier(shallow, provider=provider)

    assert dossier.scan_depth == "shallow"
    assert _product_site_records(dossier) == []


def test_a_shallow_profile_can_still_resolve_a_real_product():
    # Depth protects against ACCUSATION from absence, not against finding
    # something real. A positive resolution still lands.
    provider = _StubProvider(
        resolutions=[
            SiteResolution(
                claim_index=0, url="https://acmewidgets.example",
                confidence="high", outcome="resolved", rationale="own contact link",
            )
        ]
    )
    dossier = build_dossier(_profile(_extraction={"method": "injected"}), provider=provider)
    assert dossier.claims[0].product_url == "https://acmewidgets.example"
    assert len(_product_site_records(dossier)) == 1


def test_a_provider_without_the_method_changes_nothing():
    # Backwards compatibility: the base provider resolves nothing, so the stage
    # is a pure no-op and build_dossier behaves exactly as it did before.
    class _Old(LLMProvider):
        def decompose_claims(self, raw_profile):
            return [Claim(type="employment", employer="Acme Widgets", title="Founder")]

        def assign_tiers_and_verdict(self, dossier):
            return dossier

    dossier = build_dossier(_profile(), provider=_Old())
    assert _product_site_records(dossier) == []
    assert all(c.product_url == "" for c in dossier.claims)


# ---------------------------------------------------------------------------
# The prompt half of the contract. The records are only as safe as the
# instructions that tell the brain how to read them.
# ---------------------------------------------------------------------------


def test_operator_instructions_teach_both_flavors_and_their_ceilings():
    import re

    from detective import llm as llm_module

    # Assert against the ASSEMBLED operator prompt, not just the fragment: that
    # is what proves the guidance actually reaches the brain. Whitespace is
    # normalized because the source wraps these lines mid-sentence.
    text = re.sub(r"\s+", " ", llm_module._OPERATOR_INSTRUCTIONS).lower()
    assert "product_site" in text
    # Resolved: existence, explicitly not the role.
    assert "does not substantiate the person's role" in text
    assert "interaction_verified means" in text
    assert "unavailable means missing runtime coverage" in text
    # not_found: SUS at most, never DISPROVEN.
    assert "caps at sus, never disproven" in text
    # Ambiguous writes nothing, and its absence must not be read as a signal.
    assert "ambiguous resolution writes no record" in text


def test_corroborating_sources_still_excludes_product_site():
    # Belt and braces alongside the behavioral test above: adding this source to
    # the corroboration set would silently let a live landing page clear a
    # founder claim, which is the exact hole the judgment-layer block closed.
    assert "product_site" not in dossier_module._CORROBORATING_SOURCES
