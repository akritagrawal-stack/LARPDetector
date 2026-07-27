"""Offline tests for detective.sources.accelerators. No network: the
internal _yc_hits / _fetch_techstars_html functions are monkeypatched with
realistic sample YC Algolia and Techstars-widget response shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.accelerators as accelerators


def _yc_hit(name: str, batch: str = "Winter 2009", status: str = "Public", slug: str = "", one_liner: str = ""):
    return {
        "name": name,
        "batch": batch,
        "status": status,
        "slug": slug or name.lower().replace(" ", "-"),
        "one_liner": one_liner,
    }


def _techstars_html_for(name: str, stage: str = "ACQUIRED", session_year: str = "2009") -> str:
    """Build a minimal string reproducing the escaped JSON shape this
    module's Techstars regex is built to parse (see accelerators.py's
    _TECHSTARS_RECORD_RE), without needing the full real page.
    """
    return (
        r'...\"id\":\"abc-123\",\"name\":\"' + name + r'\",\"vertical\":null,\"stage\":\"'
        + stage + r'\",\"note\":\"Some note\",\"description\":\"Some description.\",'
        r'\"valuation\":null,\"session_year\":' + session_year + r'},{\"id\":\"next\"...'
    )


# ---------------------------------------------------------------------------
# verify_accelerator: basic gating
# ---------------------------------------------------------------------------


def test_verify_accelerator_empty_name_returns_empty():
    assert accelerators.verify_accelerator("") == []
    assert accelerators.verify_accelerator(None) == []


def test_yc_lookup_failure_does_not_block_techstars(monkeypatch):
    def boom(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(accelerators, "_yc_hits", boom)
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: _techstars_html_for("Acme Corp"))

    evidence = accelerators.verify_accelerator("Acme Corp")
    assert len(evidence) == 1
    assert "Techstars" in evidence[0]["snippet"]


def test_techstars_fetch_failure_does_not_block_yc(monkeypatch):
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: [_yc_hit("Acme Corp")])

    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(accelerators, "_fetch_techstars_html", boom)

    evidence = accelerators.verify_accelerator("Acme Corp")
    assert len(evidence) == 1
    assert "Y Combinator" in evidence[0]["snippet"]


def test_neither_program_has_a_match_emits_only_yc_checked_absent(monkeypatch):
    # MIGRATED: a SUCCESSFUL but empty YC query is now a COMPLETED directory
    # lookup (a checked-absent read), while a Techstars widget miss still emits
    # nothing (its widget is not authoritative). So "not in either" yields
    # exactly the YC checked-absent record, not [].
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: [])
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: "")
    evidence = accelerators.verify_accelerator("Totally Unbacked LLC")
    assert len(evidence) == 1
    assert evidence[0]["registry_check"] == "absent"
    assert evidence[0]["source_name"] == "accelerator_badges"


# ---------------------------------------------------------------------------
# YC: exact match high, fuzzy-unrelated-hit absent, ambiguous multi-match low
# ---------------------------------------------------------------------------


def test_yc_exact_match_is_high_confidence(monkeypatch):
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: [_yc_hit("Airbnb", batch="Winter 2009", status="Public")])
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: "")

    evidence = accelerators.verify_accelerator("Airbnb")
    assert len(evidence) == 1
    record = evidence[0]
    assert record["match_confidence"] == "high"
    assert "Winter 2009" in record["snippet"]
    assert "Public" in record["snippet"]


def test_yc_unrelated_fuzzy_hit_is_dropped_not_shown_as_low(monkeypatch):
    # Algolia's typo-tolerant search returns SOMETHING even for a
    # non-YC-backed company (confirmed live: "Cluely" -> "Hyperspell"); the
    # misleading positive suggestion must never be surfaced. MIGRATED: the
    # completed query with no name-match is now a checked-absent read, but it
    # still must not surface the unrelated "Hyperspell" listing.
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: [_yc_hit("Hyperspell", batch="Summer 2023")])
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: "")

    evidence = accelerators.verify_accelerator("Cluely")
    assert len(evidence) == 1
    assert evidence[0]["registry_check"] == "absent"
    assert "Hyperspell" not in evidence[0]["snippet"]


def test_yc_multiple_containment_matches_is_low_confidence(monkeypatch):
    monkeypatch.setattr(
        accelerators,
        "_yc_hits",
        lambda name: [_yc_hit("Acme Robotics"), _yc_hit("Acme Health")],
    )
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: "")

    evidence = accelerators.verify_accelerator("Acme")
    assert len(evidence) == 2
    assert all(e["match_confidence"] == "low" for e in evidence)


def test_yc_success_empty_returns_checked_absent(monkeypatch):
    # A SUCCESSFUL query whose hits all fail name containment (here: zero hits)
    # is a COMPLETED directory lookup, so it emits exactly one checked-absent
    # record with the completed-lookup snippet. (Was
    # test_yc_no_hits_at_all_returns_empty, migrated.)
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: [])
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: "")
    evidence = accelerators.verify_accelerator("Nobody Findable Inc")
    assert len(evidence) == 1
    rec = evidence[0]
    assert rec["registry_check"] == "absent"
    assert rec["match_confidence"] == "high"
    assert "COMPLETED directory lookup" in rec["snippet"]


def test_yc_network_failure_returns_nothing(monkeypatch):
    # The load-bearing failure/empty distinction: when the query path FAILS
    # (_yc_hits returns None, no HTTP 200 payload), verify_accelerator emits NO
    # absence record ("we could not look" is never a checked-absent read).
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: None)
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: "")
    assert accelerators.verify_accelerator("Nobody Findable Inc") == []


def test_techstars_never_emits_absence(monkeypatch):
    # Techstars' widget is not authoritative: a miss yields no absence record,
    # ever (only YC's own directory can emit a checked-absent read).
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: None)
    monkeypatch.setattr(
        accelerators, "_fetch_techstars_html", lambda: _techstars_html_for("SomeOtherCo")
    )
    evidence = accelerators.verify_accelerator("Widget Miss Inc")
    assert all(e.get("registry_check") != "absent" for e in evidence)
    assert evidence == []


# ---------------------------------------------------------------------------
# Techstars: static widget parse + stage-to-status mapping
# ---------------------------------------------------------------------------


def test_techstars_match_maps_acquired_stage(monkeypatch):
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: None)
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: _techstars_html_for("SendGrid", stage="ACQUIRED", session_year="2009"))

    evidence = accelerators.verify_accelerator("SendGrid")
    assert len(evidence) == 1
    record = evidence[0]
    assert record["match_confidence"] == "high"
    assert "acquired" in record["snippet"]
    assert "2009" in record["snippet"]


def test_techstars_match_maps_private_stage_to_active(monkeypatch):
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: None)
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: _techstars_html_for("Sendbird", stage="PRIVATE", session_year="2015"))

    evidence = accelerators.verify_accelerator("Sendbird")
    assert len(evidence) == 1
    assert "active" in evidence[0]["snippet"]


def test_techstars_no_match_in_widget_returns_empty(monkeypatch):
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: None)
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: _techstars_html_for("SendGrid"))

    evidence = accelerators.verify_accelerator("Some Random Startup Not In Widget")
    assert evidence == []


def test_parse_techstars_widget_pure_function():
    html = _techstars_html_for("SendGrid", stage="ACQUIRED", session_year="2009")
    companies = accelerators._parse_techstars_widget(html)
    assert len(companies) == 1
    assert companies[0]["name"] == "SendGrid"
    assert companies[0]["stage"] == "ACQUIRED"
    assert companies[0]["session_year"] == "2009"


# ---------------------------------------------------------------------------
# Evidence record shape + registry weight
# ---------------------------------------------------------------------------


def test_yc_evidence_record_shape_and_weight(monkeypatch):
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: [_yc_hit("Airbnb")])
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: "")

    evidence = accelerators.verify_accelerator("Airbnb")
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "accelerator_badges"
    assert record["weight"] == 0.8


def test_both_programs_return_two_records(monkeypatch):
    monkeypatch.setattr(accelerators, "_yc_hits", lambda name: [_yc_hit("Acme Corp")])
    monkeypatch.setattr(accelerators, "_fetch_techstars_html", lambda: _techstars_html_for("Acme Corp"))

    evidence = accelerators.verify_accelerator("Acme Corp")
    assert len(evidence) == 2


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_accelerators_airbnb():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real YC Algolia / Techstars calls")

    evidence = accelerators.verify_accelerator("Airbnb")
    assert evidence, "expected the YC directory to find the real Airbnb (Winter 2009 batch)"
