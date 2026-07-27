"""Offline tests for detective.sources.registry: the weighted source table.

No network. No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

from detective.sources.registry import (
    DEFAULT_WEIGHT,
    SOURCES,
    get_source,
    weight_for,
)


def test_github_weight_reflects_subject_controlled_content():
    assert weight_for("github") == 0.48


def test_sec_edgar_weight_is_0_8():
    assert weight_for("sec_edgar_form_d") == 0.8


def test_wayback_weight_is_0_8():
    assert weight_for("wayback_machine") == 0.8


def test_domain_age_weight_is_0_64():
    assert weight_for("domain_rdap_whois") == 0.64


def test_uspto_weight_is_0_8():
    assert weight_for("uspto_patents_trademarks") == 0.8


def test_arxiv_weight_is_0_8():
    assert weight_for("arxiv") == 0.8


def test_openalex_weight_is_0_64():
    assert weight_for("openalex") == 0.64


def test_packages_weight_is_1_0():
    assert weight_for("packages") == 1.0


def test_app_store_weight_is_0_8():
    assert weight_for("app_store_play_store_reviews") == 0.8


def test_accelerator_badges_weight_is_0_8():
    assert weight_for("accelerator_badges") == 0.8


def test_hackernews_weight_is_0_64():
    assert weight_for("hackernews") == 0.64


def test_techstack_weight_is_0_384():
    assert weight_for("techstack") == 0.384


def test_courtlistener_weight_is_0_6():
    assert weight_for("courtlistener") == 0.6


def test_weight_formula_matches_credibility_parsability_independence():
    for source in SOURCES:
        expected = round(
            (source.credibility * source.parsability * source.independence) / 125.0, 4
        )
        assert source.weight == expected, source.name


def test_unknown_source_name_returns_default_weight():
    assert weight_for("some_source_that_does_not_exist") == DEFAULT_WEIGHT


def test_get_source_returns_none_for_unknown_name():
    assert get_source("nope") is None


def test_get_source_returns_the_def_for_a_known_name():
    source = get_source("github")
    assert source is not None
    assert source.implemented is True
    assert source.connector == "detective.sources.github.verify_github"


def test_sixteen_implemented_connectors_present():
    implemented_names = {s.name for s in SOURCES if s.implemented}
    assert implemented_names == {
        "product_site",
        "github",
        "sec_edgar_form_d",
        "wayback_machine",
        "domain_rdap_whois",
        "uspto_patents_trademarks",
        "arxiv",
        "openalex",
        "packages",
        "app_store_play_store_reviews",
        "accelerator_badges",
        "hackernews",
        "techstack",
        "courtlistener",
        "org_roster",
        "news_coverage",
    }


def test_seeded_reference_sources_have_no_connector():
    for source in SOURCES:
        if not source.implemented:
            assert source.connector is None


def test_all_weights_in_0_to_1_range():
    for source in SOURCES:
        assert 0 < source.weight <= 1.0, source.name
