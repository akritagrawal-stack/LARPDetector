"""Offline tests for detective.verify. No network (web_search is
monkeypatched everywhere it would otherwise be called).

Bug 4 (from the live stress test): an assembled query built from a very
long claim title/assertion had no length cap, which produced a URL long
enough to trip Brave's search endpoint with an HTTP 422 ("URL too long"),
silently losing evidence for that claim. Fixed with _cap_query_length,
applied to every query right before it is issued.

No em dashes in this file (house rule).
"""

from __future__ import annotations

import time

import detective.verify as verify
from detective.models import Claim
from detective.verify import (
    _cap_query_length,
    _dedup,
    _disambiguator,
    _employment_queries,
    _relationship_for_url,
    _result_relevance,
    _source_class_for_url,
    _to_evidence,
    _rank_and_cap,
    gather_evidence,
    _MAX_EVIDENCE_PER_CLAIM,
    _MAX_QUERY_LEN,
)


def test_multi_affiliation_headline_does_not_poison_employment_queries():
    identity = {
        "name": "Riley Morgan",
        "headline": "Returning SDE Intern @ AWS, Jane Street AMP | CS @ Texas A&M",
        "current_company": "",
    }
    claim = Claim(
        type="employment",
        employer="Amazon Web Services (AWS)",
        title="Software Engineering Intern",
    )

    disambiguator = _disambiguator(identity)
    queries = [query for query, _role in _employment_queries(
        identity["name"], claim, disambiguator
    )]

    assert disambiguator == ""
    assert all("Jane Street" not in query for query in queries)
    assert any("Amazon Web Services" in query for query in queries)


def test_compound_role_queries_preserve_real_title_facets():
    claim = Claim(
        type="employment",
        employer="Ditto",
        title="Head of Growth & Creative Director",
    )

    queries = [
        query
        for query, role in _employment_queries("Elena Chen", claim, "")
        if role == "corroboration"
    ]

    assert '"Head of Growth"' in queries[0]
    assert any('"Creative Director"' in query for query in queries)
    assert all("Head Growth Creative Director" not in query for query in queries)


def test_subject_personal_site_and_linkedin_paths_are_classified_by_owner():
    assert (
        _relationship_for_url("https://elena-chen.example/about", person="Elena Chen")
        == "subject_controlled"
    )
    assert (
        _relationship_for_url(
            "https://www.linkedin.com/in/elena-chen/",
            person="Elena Chen",
        )
        == "subject_controlled"
    )
    assert (
        _relationship_for_url(
            "https://www.linkedin.com/posts/samuellacote_ditto-activity-1",
            person="Elena Chen",
        )
        == "third_party"
    )
    assert (
        _relationship_for_url(
            "https://www.linkedin.com/company/ditto-ai/",
            person="Elena Chen",
        )
        == "first_party_org"
    )
    assert (
        _relationship_for_url(
            "https://www.linkedin.com/in/jordan-rivera-synthetic/",
            person="Jordan Rivera",
        )
        == "subject_controlled"
    )
    assert (
        _relationship_for_url(
            "https://www.jordan-rivera.example/",
            person="Jordan Rivera",
        )
        == "subject_controlled"
    )


def test_people_data_aggregators_are_republication_not_independent_reporting():
    assert _source_class_for_url("https://rocketreach.co/elena-chen-email") == "republication"
    assert _source_class_for_url("https://www.signalhire.com/profiles/jordan-rivera") == "republication"


def test_role_search_keeps_deeper_independent_result_over_top_aggregators():
    claim = Claim(
        type="employment",
        employer="Ditto",
        title="Head of Growth",
    )
    results = [
        {
            "url": "https://elena-chen.example",
            "snippet": "Elena Chen is Head of Growth at Ditto.",
        },
        {
            "url": "https://www.linkedin.com/in/elena-chen",
            "snippet": "Elena Chen is Head of Growth at Ditto.",
        },
        {
            "url": "https://rocketreach.co/elena",
            "snippet": "Elena Chen is Head of Growth at Ditto.",
        },
        {
            "url": "https://signalhire.com/elena",
            "snippet": "Elena Chen is Head of Growth at Ditto.",
        },
        {
            "url": "https://interviews.example/elena",
            "snippet": "Elena Chen, Head of Growth at Ditto, discussed the company.",
        },
    ]

    records = _to_evidence(
        results,
        claim=claim,
        person="Elena Chen",
    )

    assert records[0]["source_url"] == "https://interviews.example/elena"
    assert any(r["source_url"] == "https://interviews.example/elena" for r in records)


def test_resolved_product_domain_is_first_party_org_role_evidence():
    claim = Claim(
        type="employment",
        employer="Fern",
        title="Founder",
        product_url="https://trytalkr.com",
    )

    records = _to_evidence(
        [
            {
                "url": "https://www.trytalkr.com/about",
                "snippet": "Jordan Rivera, Founder. TalkR by Fern.",
            }
        ],
        claim=claim,
        person="Jordan Rivera",
    )

    assert records[0]["relationship"] == "first_party_org"
    assert records[0]["claim_relevance"] == "substantive"


def test_linkedin_last_initial_result_binds_exact_role_as_subject_controlled():
    claim = Claim(
        type="employment",
        employer="Amazon Web Services (AWS)",
        title="Software Engineering Intern",
    )
    result = {
        "title": "Zarik K. - Returning SDE Intern @ AWS - LinkedIn",
        "url": "https://www.linkedin.com/in/riley-morgan",
        "snippet": "Experience: Amazon Web Services (AWS). Returning SDE Intern.",
    }

    assert _result_relevance(result, claim, "Riley Morgan") == "substantive"


def _stub_out_source_connectors(monkeypatch):
    """Stub the independent source connectors (github, sec_edgar, wayback,
    domain_age, uspto, arxiv, openalex, packages, app_store, accelerators,
    hackernews, techstack, courtlistener) so gather_evidence tests stay
    offline and fast even though these claims (founder/technical titles,
    employer set) would otherwise gate real connector calls. See
    verify._gather_github_evidence / _gather_sec_evidence /
    _gather_site_history_evidence / _gather_uspto_evidence /
    _gather_arxiv_evidence / _gather_openalex_evidence /
    _gather_packages_evidence / _gather_app_store_evidence /
    _gather_accelerators_evidence / _gather_hackernews_evidence /
    _gather_techstack_evidence / _gather_courtlistener_evidence for the
    gating.
    """
    monkeypatch.setattr(verify.github_source, "verify_github", lambda *a, **k: [])
    monkeypatch.setattr(verify.sec_edgar_source, "verify_sec", lambda *a, **k: [])
    monkeypatch.setattr(verify.wayback_source, "verify_wayback", lambda *a, **k: [])
    monkeypatch.setattr(verify.domain_age_source, "verify_domain_age", lambda *a, **k: [])
    monkeypatch.setattr(verify.uspto_source, "verify_uspto", lambda *a, **k: [])
    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", lambda *a, **k: [])
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", lambda *a, **k: [])
    monkeypatch.setattr(verify.packages_source, "verify_packages", lambda *a, **k: [])
    monkeypatch.setattr(verify.app_store_source, "verify_app_store", lambda *a, **k: [])
    monkeypatch.setattr(verify.accelerators_source, "verify_accelerator", lambda *a, **k: [])
    monkeypatch.setattr(verify.hackernews_source, "verify_hackernews", lambda *a, **k: [])
    monkeypatch.setattr(verify.techstack_source, "verify_techstack", lambda *a, **k: [])
    monkeypatch.setattr(verify.courtlistener_source, "verify_courtlistener", lambda *a, **k: [])


# ---------------------------------------------------------------------------
# _cap_query_length: the guard itself
# ---------------------------------------------------------------------------


def test_cap_query_length_leaves_short_query_untouched():
    q = '"Casey Lin" Amazon Sr. Product Manager'
    assert _cap_query_length(q) == q


def test_cap_query_length_truncates_on_word_boundary():
    q = ("word " * 200).strip()  # way over the cap, all short tokens
    capped = _cap_query_length(q)
    assert len(capped) <= _MAX_QUERY_LEN
    assert not capped.endswith("wor")  # never cut mid-word
    assert capped == capped.strip()


def test_cap_query_length_handles_empty_and_none():
    assert _cap_query_length("") == ""
    assert _cap_query_length(None) == ""


# ---------------------------------------------------------------------------
# gather_evidence: the cap must actually apply before any query is issued.
# ---------------------------------------------------------------------------


def test_gather_evidence_never_issues_an_over_length_query(monkeypatch):
    seen_queries = []

    def fake_web_search(query, count=5):
        seen_queries.append(query)
        return []

    monkeypatch.setattr(verify, "web_search", fake_web_search)
    _stub_out_source_connectors(monkeypatch)

    very_long_title = (
        "Founder, CEO, and Chief Everything Officer of a company that also does "
        * 10
    )
    claim = Claim(
        type="employment",
        employer="Cluely",
        title=very_long_title,
        start="Jan 2023",
        end="Present",
        assertion=f"Worked as {very_long_title} at Cluely (Jan 2023 to Present).",
    )
    gather_evidence(claim, identity={"name": "Someone Founder"})

    assert seen_queries, "gather_evidence never issued a query"
    for q in seen_queries:
        assert len(q) <= _MAX_QUERY_LEN


def test_gather_evidence_normal_length_queries_unaffected(monkeypatch):
    seen_queries = []

    def fake_web_search(query, count=5):
        seen_queries.append(query)
        return []

    monkeypatch.setattr(verify, "web_search", fake_web_search)
    _stub_out_source_connectors(monkeypatch)

    claim = Claim(
        type="employment",
        employer="Northwind Robotics",
        title="Co-founder, COO",
        start="Jul 2024",
        end="Present",
        assertion="Worked as Co-founder, COO at Northwind Robotics (Jul 2024 to Present).",
    )
    gather_evidence(claim, identity={"name": "Casey Lin", "current_company": "Northwind Robotics"})

    assert seen_queries
    for q in seen_queries:
        assert "Casey Lin" in q


# ---------------------------------------------------------------------------
# Batch 2 gating: uspto, arxiv, openalex, packages. Each fires only when the
# claim type AND claim text warrant it (see verify._gather_uspto_evidence /
# _gather_arxiv_evidence / _gather_openalex_evidence / _gather_packages_evidence).
# ---------------------------------------------------------------------------


def _no_web_search(monkeypatch):
    monkeypatch.setattr(verify, "web_search", lambda query, count=5: [])


def test_proprietary_tech_claim_fires_uspto_with_company_name(monkeypatch):
    _no_web_search(monkeypatch)
    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", lambda *a, **k: [])
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", lambda *a, **k: [])
    monkeypatch.setattr(verify.packages_source, "verify_packages", lambda *a, **k: [])

    captured = {}

    def fake_uspto(name, is_company=False):
        captured["name"] = name
        captured["is_company"] = is_company
        return []

    monkeypatch.setattr(verify.uspto_source, "verify_uspto", fake_uspto)

    claim = Claim(
        type="proprietary_tech",
        employer="Acme Widgets",
        assertion="Acme Widgets uses patented proprietary compression technology.",
    )
    gather_evidence(claim, identity={})

    assert captured == {"name": "Acme Widgets", "is_company": True}


def test_proprietary_tech_claim_never_fires_arxiv_or_openalex(monkeypatch):
    _no_web_search(monkeypatch)
    monkeypatch.setattr(verify.uspto_source, "verify_uspto", lambda *a, **k: [])

    fired = {"arxiv": False, "openalex": False}

    def fake_arxiv(*a, **k):
        fired["arxiv"] = True
        return []

    def fake_openalex(*a, **k):
        fired["openalex"] = True
        return []

    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", fake_arxiv)
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", fake_openalex)
    monkeypatch.setattr(verify.packages_source, "verify_packages", lambda *a, **k: [])

    claim = Claim(
        type="proprietary_tech",
        employer="Acme Widgets",
        assertion="Acme Widgets uses proprietary research-grade AI, PhD-built.",
    )
    gather_evidence(claim, identity={"name": "Acme Widgets"})

    # arxiv/openalex search PEOPLE, not companies: must never fire on a
    # company-scan proprietary_tech claim, even one whose text happens to
    # contain research-flavored keywords.
    assert fired == {"arxiv": False, "openalex": False}


def test_patent_flavored_employment_claim_fires_uspto_with_person_name(monkeypatch):
    _no_web_search(monkeypatch)
    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", lambda *a, **k: [])
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", lambda *a, **k: [])
    monkeypatch.setattr(verify.packages_source, "verify_packages", lambda *a, **k: [])

    captured = {}

    def fake_uspto(name, is_company=False):
        captured["name"] = name
        captured["is_company"] = is_company
        return []

    monkeypatch.setattr(verify.uspto_source, "verify_uspto", fake_uspto)

    claim = Claim(
        type="employment",
        employer="Acme Widgets",
        title="Founder, holds 3 patents on the core invention",
        assertion="Founder at Acme Widgets, holds 3 patents on the core invention.",
    )
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert captured == {"name": "Jane Doe", "is_company": False}


def test_plain_employment_claim_does_not_fire_uspto(monkeypatch):
    _no_web_search(monkeypatch)
    fired = {"uspto": False}

    def fake_uspto(*a, **k):
        fired["uspto"] = True
        return []

    monkeypatch.setattr(verify.uspto_source, "verify_uspto", fake_uspto)
    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", lambda *a, **k: [])
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", lambda *a, **k: [])
    monkeypatch.setattr(verify.packages_source, "verify_packages", lambda *a, **k: [])

    claim = Claim(
        type="employment",
        employer="Acme Widgets",
        title="Founder and CEO",
        assertion="Founder and CEO of Acme Widgets.",
    )
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert fired["uspto"] is False


def test_research_credential_claim_fires_arxiv_and_openalex_with_person_name(monkeypatch):
    _no_web_search(monkeypatch)
    monkeypatch.setattr(verify.uspto_source, "verify_uspto", lambda *a, **k: [])
    monkeypatch.setattr(verify.packages_source, "verify_packages", lambda *a, **k: [])

    captured = {}

    def fake_arxiv(person_name):
        captured["arxiv_name"] = person_name
        return []

    def fake_openalex(person_name, institution=None):
        captured["openalex_name"] = person_name
        captured["institution"] = institution
        return []

    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", fake_arxiv)
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", fake_openalex)

    claim = Claim(
        type="employment",
        employer="Acme Labs",
        title="Founder",
        assertion="Founder at Acme Labs; previously a research scientist with a PhD from MIT.",
    )
    gather_evidence(claim, identity={"name": "Jane Doe", "current_company": "Acme Labs"})

    assert captured["arxiv_name"] == "Jane Doe"
    assert captured["openalex_name"] == "Jane Doe"
    assert captured["institution"] == "Acme Labs"


def test_package_flavored_proprietary_tech_claim_fires_packages(monkeypatch):
    _no_web_search(monkeypatch)
    monkeypatch.setattr(verify.uspto_source, "verify_uspto", lambda *a, **k: [])
    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", lambda *a, **k: [])
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", lambda *a, **k: [])

    captured = {}

    def fake_packages(name):
        captured["name"] = name
        return []

    monkeypatch.setattr(verify.packages_source, "verify_packages", fake_packages)

    claim = Claim(
        type="proprietary_tech",
        employer="Acme SDK",
        assertion="Acme SDK is an open-source package available on npm.",
    )
    gather_evidence(claim, identity={})

    assert captured["name"] == "Acme SDK"


def test_plain_proprietary_tech_claim_without_package_keywords_does_not_fire_packages(monkeypatch):
    _no_web_search(monkeypatch)
    monkeypatch.setattr(verify.arxiv_source, "verify_arxiv", lambda *a, **k: [])
    monkeypatch.setattr(verify.openalex_source, "verify_openalex", lambda *a, **k: [])

    fired = {"packages": False}

    def fake_packages(*a, **k):
        fired["packages"] = True
        return []

    monkeypatch.setattr(verify.packages_source, "verify_packages", fake_packages)
    monkeypatch.setattr(verify.uspto_source, "verify_uspto", lambda *a, **k: [])

    claim = Claim(
        type="proprietary_tech",
        employer="Acme Widgets",
        assertion="Acme Widgets built a custom in-house recommendation engine.",
    )
    gather_evidence(claim, identity={})

    assert fired["packages"] is False


# ---------------------------------------------------------------------------
# Batch 3 gating: app_store, accelerators, hackernews. Each fires only when
# the claim type AND (for accelerators) claim text warrant it (see
# verify._gather_app_store_evidence / _gather_accelerators_evidence /
# _gather_hackernews_evidence).
# ---------------------------------------------------------------------------


def test_user_count_claim_fires_app_store_with_product_name(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_app_store(product_name, app_id=None):
        captured["product_name"] = product_name
        return []

    monkeypatch.setattr(verify.app_store_source, "verify_app_store", fake_app_store)

    claim = Claim(
        type="user_count",
        employer="Acme App",
        assertion="Acme App claims 10,000 active users.",
    )
    gather_evidence(claim, identity={})

    assert captured == {"product_name": "Acme App"}


def test_company_overview_claim_fires_app_store_with_product_name(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_app_store(product_name, app_id=None):
        captured["product_name"] = product_name
        return []

    monkeypatch.setattr(verify.app_store_source, "verify_app_store", fake_app_store)

    claim = Claim(type="company_overview", employer="Acme App", assertion="Acme App overview.")
    gather_evidence(claim, identity={})

    assert captured == {"product_name": "Acme App"}


def test_plain_employment_claim_fires_company_app_store_check(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"app_store": False}

    def fake_app_store(*a, **k):
        fired["app_store"] = True
        return []

    monkeypatch.setattr(verify.app_store_source, "verify_app_store", fake_app_store)

    # A current non-founder company gets the company-side footprint check. This
    # evidence says something about the company, never whether the person held
    # the role. Historical non-founder employers keep the general web check.
    claim = Claim(type="employment", employer="Acme Widgets", title="Software Engineer", assertion="Software Engineer at Acme Widgets.")
    claim._company_component_relevant = True
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert fired["app_store"] is True


def test_founder_employment_claim_fires_app_store(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"app_store": False}

    def fake_app_store(*a, **k):
        fired["app_store"] = True
        return []

    monkeypatch.setattr(verify.app_store_source, "verify_app_store", fake_app_store)

    # A founder/builder employment claim DOES fire the App Store lookup: the
    # employer may be the app they shipped, and its real store traction (rating
    # count vs a claimed user number) is exactly the mismatch worth checking on
    # a person scan. The lookup self-filters (returns nothing for a non-app).
    claim = Claim(type="employment", employer="Organize Campus", title="Founder", assertion="Founder at Organize Campus.")
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert fired["app_store"] is True


def test_identity_hackernews_skips_a_generic_headline(monkeypatch):
    fired = {"value": False}
    monkeypatch.setattr(
        verify.hackernews_source,
        "verify_hackernews",
        lambda *args, **kwargs: fired.__setitem__("value", True) or [],
    )
    claim = Claim(
        type="identity",
        assertion="A real person named Jane Doe exists.",
    )

    evidence = verify._gather_hackernews_evidence(
        claim,
        "Jane Doe",
        {"name": "Jane Doe", "headline": "building and teaching AI"},
    )

    assert evidence == []
    assert fired["value"] is False


def test_identity_hackernews_uses_company_not_legal_name_as_username(monkeypatch):
    captured = {}

    def fake_hackernews(query, person=None):
        captured["query"] = query
        captured["person"] = person
        return []

    monkeypatch.setattr(
        verify.hackernews_source, "verify_hackernews", fake_hackernews
    )
    claim = Claim(
        type="identity",
        assertion="A real person named Jane Doe exists.",
    )

    verify._gather_hackernews_evidence(
        claim,
        "Jane Doe",
        {"name": "Jane Doe", "current_company": "Acme Widgets"},
    )

    assert captured == {"query": "Acme Widgets", "person": None}


def test_company_overview_claim_always_fires_accelerators(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_accelerators(company_name):
        captured["company_name"] = company_name
        return []

    monkeypatch.setattr(verify.accelerators_source, "verify_accelerator", fake_accelerators)

    claim = Claim(type="company_overview", employer="Acme Widgets", assertion="Acme Widgets overview.")
    gather_evidence(claim, identity={})

    assert captured == {"company_name": "Acme Widgets"}


def test_accelerator_flavored_employment_claim_fires_accelerators(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_accelerators(company_name):
        captured["company_name"] = company_name
        return []

    monkeypatch.setattr(verify.accelerators_source, "verify_accelerator", fake_accelerators)

    claim = Claim(
        type="employment",
        employer="Acme Widgets",
        title="Founder, YC-backed",
        assertion="Founder of Acme Widgets, a YC-backed company.",
    )
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert captured == {"company_name": "Acme Widgets"}


def test_plain_employment_claim_without_accelerator_keywords_does_not_fire_accelerators(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"accelerators": False}

    def fake_accelerators(*a, **k):
        fired["accelerators"] = True
        return []

    monkeypatch.setattr(verify.accelerators_source, "verify_accelerator", fake_accelerators)

    claim = Claim(type="employment", employer="Acme Widgets", title="Founder and CEO", assertion="Founder and CEO of Acme Widgets.")
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert fired["accelerators"] is False


def test_company_overview_claim_fires_hackernews_with_product_query(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_hackernews(query, person=None):
        captured["query"] = query
        captured["person"] = person
        return []

    monkeypatch.setattr(verify.hackernews_source, "verify_hackernews", fake_hackernews)

    claim = Claim(type="company_overview", employer="Acme Widgets", assertion="Acme Widgets overview.")
    gather_evidence(claim, identity={})

    assert captured == {"query": "Acme Widgets", "person": None}


def test_identity_claim_fires_hackernews_with_company_anchor_only(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_hackernews(query, person=None):
        captured["query"] = query
        captured["person"] = person
        return []

    monkeypatch.setattr(verify.hackernews_source, "verify_hackernews", fake_hackernews)

    claim = Claim(type="identity", assertion="Jane Doe is the founder of Acme Widgets.")
    gather_evidence(claim, identity={"name": "Jane Doe", "current_company": "Acme Widgets"})

    assert captured == {"query": "Acme Widgets", "person": None}


def test_identity_claim_without_disambiguator_does_not_fire_hackernews(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"hackernews": False}

    def fake_hackernews(*a, **k):
        fired["hackernews"] = True
        return []

    monkeypatch.setattr(verify.hackernews_source, "verify_hackernews", fake_hackernews)

    claim = Claim(type="identity", assertion="Jane Doe.")
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert fired["hackernews"] is False


def test_plain_employment_claim_does_not_fire_hackernews(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"hackernews": False}

    def fake_hackernews(*a, **k):
        fired["hackernews"] = True
        return []

    monkeypatch.setattr(verify.hackernews_source, "verify_hackernews", fake_hackernews)

    claim = Claim(type="employment", employer="Acme Widgets", title="Founder and CEO", assertion="Founder and CEO of Acme Widgets.")
    gather_evidence(claim, identity={"name": "Jane Doe", "current_company": "Acme Widgets"})

    assert fired["hackernews"] is False


# ---------------------------------------------------------------------------
# Bug 4: _dedup used to keep whichever record for a URL was added first.
# Plain web-search evidence is gathered before the weighted source-connector
# evidence (see gather_evidence), so a naive first-wins dedup silently
# discarded the WEIGHTED/higher-confidence record whenever both produced the
# same URL, defeating source weighting entirely. Fixed to keep the BEST
# record per URL: weighted beats plain, then higher match_confidence, then
# higher weight.
# ---------------------------------------------------------------------------


def test_dedup_keeps_weighted_record_over_plain_web_hit_on_same_url():
    same_url = "https://www.ycombinator.com/companies/browser-use"
    plain = {"source_url": same_url, "snippet": "a plain web search hit"}
    weighted = {
        "source_url": same_url,
        "snippet": "Y Combinator W25 batch listing",
        "source_name": "accelerators",
        "weight": 0.9,
        "match_confidence": "high",
    }

    out = _dedup([plain, weighted])

    assert len(out) == 1
    assert out[0] is weighted

    # Order of arrival must not matter either.
    out_reversed = _dedup([weighted, plain])
    assert len(out_reversed) == 1
    assert out_reversed[0] is weighted


def test_dedup_prefers_higher_match_confidence_among_weighted_records():
    low = {
        "source_url": "https://x.test/a",
        "source_name": "github",
        "weight": 0.6,
        "match_confidence": "low",
    }
    high = {
        "source_url": "https://x.test/a",
        "source_name": "github",
        "weight": 0.6,
        "match_confidence": "high",
    }

    out = _dedup([low, high])

    assert len(out) == 1
    assert out[0]["match_confidence"] == "high"


def test_dedup_preserves_distinct_urls_and_order():
    a = {"source_url": "https://x.test/a", "snippet": "a"}
    b = {"source_url": "https://x.test/b", "snippet": "b"}
    out = _dedup([a, b])
    assert out == [a, b]


def test_dedup_drops_records_with_no_url():
    a = {"source_url": "", "snippet": "no url"}
    b = {"source_url": "https://x.test/b", "snippet": "b"}
    out = _dedup([a, b])
    assert out == [b]


# ---------------------------------------------------------------------------
# Batch 4 gating: techstack, courtlistener. See
# verify._gather_techstack_evidence / _gather_courtlistener_evidence for the
# gating.
# ---------------------------------------------------------------------------


def test_company_overview_claim_fires_techstack_with_company_url(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_techstack(url):
        captured["url"] = url
        return []

    monkeypatch.setattr(verify.techstack_source, "verify_techstack", fake_techstack)

    claim = Claim(type="company_overview", employer="Acme Widgets", assertion="Acme Widgets overview.")
    gather_evidence(claim, identity={}, company_url="https://acmewidgets.com")

    assert captured == {"url": "https://acmewidgets.com"}


def test_company_overview_claim_without_company_url_does_not_fire_techstack(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"techstack": False}

    def fake_techstack(*a, **k):
        fired["techstack"] = True
        return []

    monkeypatch.setattr(verify.techstack_source, "verify_techstack", fake_techstack)

    claim = Claim(type="company_overview", employer="Acme Widgets", assertion="Acme Widgets overview.")
    gather_evidence(claim, identity={}, company_url=None)

    assert fired["techstack"] is False


# ---------------------------------------------------------------------------
# A RESOLVED product site unlocks the URL-keyed connectors on a PERSON scan.
#
# This is the point of the whole web-app verification block: before it, wayback
# / domain_age / techstack gated on (company_overview AND company_url), and a
# person scan passes company_url=None, so a founder's WEB product could never
# be assessed. Both halves of that gate widen together: a founder claim is
# type "employment", so a URL alone would still have been rejected.
# ---------------------------------------------------------------------------


def test_founder_claim_with_a_resolved_product_url_fires_the_url_connectors(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_techstack(url):
        captured["techstack"] = url
        return []

    def fake_wayback(url):
        captured["wayback"] = url
        return []

    def fake_domain_age(domain):
        captured["domain_age"] = domain
        return []

    monkeypatch.setattr(verify.techstack_source, "verify_techstack", fake_techstack)
    monkeypatch.setattr(verify.wayback_source, "verify_wayback", fake_wayback)
    monkeypatch.setattr(verify.domain_age_source, "verify_domain_age", fake_domain_age)

    claim = Claim(
        type="employment", employer="Acme Widgets", title="Founder",
        assertion="Founder at Acme Widgets.", product_url="https://acmewidgets.example",
    )
    gather_evidence(claim, identity={}, company_url=None)

    assert captured["techstack"] == "https://acmewidgets.example"
    assert captured["wayback"] == "https://acmewidgets.example"
    assert captured["domain_age"] == "acmewidgets.example"


def test_a_plain_non_founder_job_never_fires_them_even_with_a_url(monkeypatch):
    # Website fingerprinting is reserved for company scans and founder-linked
    # product claims. General company and App Store checks still cover every
    # employer without trusting an ambiguous product URL.
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"techstack": False}
    monkeypatch.setattr(
        verify.techstack_source, "verify_techstack",
        lambda url: fired.__setitem__("techstack", True) or [],
    )

    claim = Claim(
        type="employment", employer="Acme Widgets", title="Accounts Payable Clerk",
        assertion="Accounts Payable Clerk at Acme Widgets.",
        product_url="https://acmewidgets.example",
    )
    gather_evidence(claim, identity={}, company_url=None)

    assert fired["techstack"] is False


def test_metric_claim_with_a_resolved_product_url_fires_techstack(monkeypatch):
    # "50k users" leans entirely on the product being real, so it earns the
    # buildability fingerprint once we know which site the product is.
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_techstack(url):
        captured["url"] = url
        return []

    monkeypatch.setattr(verify.techstack_source, "verify_techstack", fake_techstack)

    claim = Claim(
        type="user_count", employer="Acme Widgets",
        assertion="Acme Widgets has 50,000 users.",
        product_url="https://acmewidgets.example",
    )
    gather_evidence(claim, identity={}, company_url=None)

    assert captured["url"] == "https://acmewidgets.example"


def test_proprietary_tech_claim_does_not_fire_techstack(monkeypatch):
    # Deliberate: the fingerprint is a property of the URL, not of any one
    # claim; firing on every proprietary_tech claim too would just refetch
    # the same page for no new signal (see verify.py module docstring).
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"techstack": False}

    def fake_techstack(*a, **k):
        fired["techstack"] = True
        return []

    monkeypatch.setattr(verify.techstack_source, "verify_techstack", fake_techstack)

    claim = Claim(type="proprietary_tech", employer="Acme Widgets", assertion="Acme Widgets uses proprietary AI.")
    gather_evidence(claim, identity={}, company_url="https://acmewidgets.com")

    assert fired["techstack"] is False


def test_identity_claim_fires_courtlistener_with_person_name_not_company(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_courtlistener(name, is_company=False):
        captured["name"] = name
        captured["is_company"] = is_company
        return []

    monkeypatch.setattr(verify.courtlistener_source, "verify_courtlistener", fake_courtlistener)

    claim = Claim(type="identity", assertion="Jane Doe is the founder of Acme Widgets.")
    gather_evidence(claim, identity={"name": "Jane Doe", "current_company": "Acme Widgets"})

    assert captured == {"name": "Jane Doe", "is_company": False}


def test_company_overview_claim_fires_courtlistener_with_company_name_as_company(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    captured = {}

    def fake_courtlistener(name, is_company=False):
        captured["name"] = name
        captured["is_company"] = is_company
        return []

    monkeypatch.setattr(verify.courtlistener_source, "verify_courtlistener", fake_courtlistener)

    claim = Claim(type="company_overview", employer="Acme Widgets", assertion="Acme Widgets overview.")
    gather_evidence(claim, identity={})

    assert captured == {"name": "Acme Widgets", "is_company": True}


def test_plain_employment_claim_does_not_fire_courtlistener(monkeypatch):
    _no_web_search(monkeypatch)
    _stub_out_source_connectors(monkeypatch)

    fired = {"courtlistener": False}

    def fake_courtlistener(*a, **k):
        fired["courtlistener"] = True
        return []

    monkeypatch.setattr(verify.courtlistener_source, "verify_courtlistener", fake_courtlistener)

    claim = Claim(type="employment", employer="Acme Widgets", title="Founder and CEO", assertion="Founder and CEO of Acme Widgets.")
    gather_evidence(claim, identity={"name": "Jane Doe"})

    assert fired["courtlistener"] is False


def test_gather_evidence_keeps_weighted_evidence_over_duplicate_web_hit(monkeypatch):
    # End-to-end reproduction of the live bug: accelerators.verify_accelerator
    # returns a high-confidence YC match on a URL that a plain web_search
    # result also happens to return; the weighted record must be the one
    # that survives into claim.evidence, not the plain one.
    same_url = "https://www.ycombinator.com/companies/browser-use"

    def fake_web_search(query, count=5):
        return [
            {
                "url": same_url,
                "title": "Browser Use - Y Combinator",
                "snippet": "a plain web hit with no source weighting",
            }
        ]

    monkeypatch.setattr(verify, "web_search", fake_web_search)
    _stub_out_source_connectors(monkeypatch)
    monkeypatch.setattr(
        verify.accelerators_source,
        "verify_accelerator",
        lambda name: [
            {
                "source_url": same_url,
                "snippet": "Y Combinator W25 batch",
                "source_name": "accelerators",
                "weight": 0.9,
                "match_confidence": "high",
            }
        ],
    )

    claim = Claim(
        type="company_overview",
        employer="Browser Use",
        assertion="Browser Use overview.",
    )
    gather_evidence(claim, identity={})

    matches = [e for e in claim.evidence if e.get("source_url") == same_url]
    assert len(matches) == 1
    assert matches[0].get("source_name") == "accelerators"
    assert matches[0].get("match_confidence") == "high"


# ---------------------------------------------------------------------------
# Parallelization determinism: the connectors now run in a bounded thread pool
# (see gather_evidence), but their results must be reassembled in the SAME
# fixed declared order regardless of which thread finishes first, so the
# pre-dedup evidence list (and therefore every downstream score) is identical
# to the old serial version.
# ---------------------------------------------------------------------------


def test_connector_results_reassembled_in_fixed_order_not_completion_order(monkeypatch):
    monkeypatch.setattr(verify, "web_search", lambda query, count=5: [])

    # Stub every connector to []; then override the ones that fire for a
    # company_overview scan to each return one distinct, plain (non-news,
    # non-landing) record, few enough to stay under the cap so _rank_and_cap's
    # stable news/landing sort preserves collection order.
    _stub_out_source_connectors(monkeypatch)

    def _rec(url):
        return [{"source_url": url, "snippet": url}]

    # wayback is the FIRST-submitted connector's inner call; make it sleep so
    # it completes LAST. If reassembly used completion order its record would
    # move to the end; the fixed-order reassembly must still place it first.
    def slow_wayback(*a, **k):
        time.sleep(0.05)
        return _rec("https://x.test/site")

    monkeypatch.setattr(verify.wayback_source, "verify_wayback", slow_wayback)
    monkeypatch.setattr(verify.domain_age_source, "verify_domain_age", lambda *a, **k: [])
    monkeypatch.setattr(verify.app_store_source, "verify_app_store", lambda *a, **k: _rec("https://x.test/appstore"))
    monkeypatch.setattr(verify.accelerators_source, "verify_accelerator", lambda *a, **k: _rec("https://x.test/accel"))
    monkeypatch.setattr(verify.hackernews_source, "verify_hackernews", lambda *a, **k: _rec("https://x.test/hn"))
    monkeypatch.setattr(verify.techstack_source, "verify_techstack", lambda *a, **k: _rec("https://x.test/techstack"))
    monkeypatch.setattr(verify.courtlistener_source, "verify_courtlistener", lambda *a, **k: _rec("https://x.test/court"))

    expected = [
        "https://x.test/site",       # site_history (index 2)
        "https://x.test/appstore",   # app_store (index 7)
        "https://x.test/accel",      # accelerators (index 8)
        "https://x.test/hn",         # hackernews (index 9)
        "https://x.test/techstack",  # techstack (index 10)
        "https://x.test/court",      # courtlistener (index 11)
    ]

    for _ in range(5):
        claim = Claim(type="company_overview", employer="Acme Widgets", assertion="Acme Widgets overview.")
        gather_evidence(claim, identity={}, company_url="https://acmewidgets.com")
        assert [e["source_url"] for e in claim.evidence] == expected


# ---------------------------------------------------------------------------
# Finding 1: _rank_and_cap used to sort only on (is_news, is_landing), so the
# per-claim cap systematically dropped the highest-value connector evidence and
# the adversarial-query web evidence in favor of generic corroboration web
# hits. The bucketed quota must now guarantee slots for both classes.
# ---------------------------------------------------------------------------


def test_rank_and_cap_keeps_weighted_connector_record_over_generic_web_hits():
    # cap generic web hits, plus one weighted high-confidence connector record
    # collected LAST (connectors run after web search). The connector record
    # must survive the cap; under the old news/landing-only sort it was tied
    # with the web hits and, being collected last, got dropped.
    web_hits = [
        {"source_url": f"https://web.test/{i}", "snippet": "generic corroboration hit"}
        for i in range(_MAX_EVIDENCE_PER_CLAIM)
    ]
    weighted = {
        "source_url": "https://github.com/realaccount",
        "snippet": "account created 2015, matches claimed company",
        "source_name": "github",
        "weight": 0.9,
        "match_confidence": "high",
    }

    out = _rank_and_cap(web_hits + [weighted], _MAX_EVIDENCE_PER_CLAIM)

    assert len(out) == _MAX_EVIDENCE_PER_CLAIM
    assert weighted in out


def test_rank_and_cap_keeps_adversarial_web_record_collected_after_corroboration():
    # Enough corroboration (untagged) web hits to fill the cap on their own,
    # then one adversarial-query web record collected LAST. The adversarial
    # record is exactly what surfaces fabrication reporting, so the quota must
    # reserve a slot for it rather than let the corroboration hits crowd it out.
    corroboration = [
        {"source_url": f"https://web.test/{i}", "snippet": "corroboration hit"}
        for i in range(_MAX_EVIDENCE_PER_CLAIM)
    ]
    adversarial = {
        "source_url": "https://news.test/fraud-report",
        "snippet": "no record of the claimed role; company disputes it",
        "query_role": "adversarial",
    }

    out = _rank_and_cap(corroboration + [adversarial], _MAX_EVIDENCE_PER_CLAIM)

    assert len(out) == _MAX_EVIDENCE_PER_CLAIM
    assert adversarial in out


def test_rank_and_cap_below_cap_keeps_everything_news_first():
    # Under the cap nothing is dropped; the original news/landing ordering is
    # preserved (news/reference domains first, bare landing pages last).
    landing = {"source_url": "https://acme.com/", "snippet": "homepage"}
    news = {"source_url": "https://techcrunch.com/acme", "snippet": "coverage"}
    plain = {"source_url": "https://blog.test/post", "snippet": "post"}

    out = _rank_and_cap([landing, plain, news], _MAX_EVIDENCE_PER_CLAIM)

    assert len(out) == 3
    assert out[0] is news
    assert out[-1] is landing


# ---------------------------------------------------------------------------
# Workstream C.2: _gather_github_evidence must thread identity["hints"] into
# verify_github, so the profile-declared handle / blog disambiguator can fire.
# ---------------------------------------------------------------------------


def test_gather_github_passes_identity_hints(monkeypatch):
    recorded = {}

    def _recorder(person, company=None, hints=None):
        recorded["person"] = person
        recorded["company"] = company
        recorded["hints"] = hints
        return []

    monkeypatch.setattr(verify.github_source, "verify_github", _recorder)

    claim = Claim(type="identity", assertion="Jordan Rivera is a software engineer.")
    identity = {
        "name": "Jordan Rivera",
        "current_company": "Pillar",
        "hints": {"github_login": "JordanRivera-dev"},
    }
    verify._gather_github_evidence(claim, "Jordan Rivera", identity)
    assert recorded["hints"] == {"github_login": "JordanRivera-dev"}

    # Identity with no hints key: the recorder must receive {} (never None).
    recorded.clear()
    verify._gather_github_evidence(claim, "Jordan Rivera", {"name": "Jordan Rivera"})
    assert recorded["hints"] == {}


def test_gather_records_dark_web_lookup_separately_from_connector_results(monkeypatch):
    monkeypatch.setattr(verify, "web_search", lambda query, count=5: [])
    monkeypatch.setattr(verify.search_backend, "search_healthy", lambda: False)
    _stub_out_source_connectors(monkeypatch)
    monkeypatch.setattr(
        verify.techstack_source,
        "verify_techstack",
        lambda url: [
            {
                "source_url": url,
                "source_name": "techstack",
                "snippet": "A product surface exists, but this does not verify a role.",
            }
        ],
    )

    claim = Claim(
        type="employment",
        employer="Vercel",
        title="CEO",
        assertion="Worked as CEO at Vercel.",
        product_url="https://vercel.com",
    )
    gather_evidence(claim, identity={"name": "Guillermo Rauch"})

    assert claim._web_search_unavailable is True
    assert any(e.get("source_name") == "techstack" for e in claim.evidence)
