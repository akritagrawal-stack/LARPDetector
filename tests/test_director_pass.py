"""Offline tests for the director / planning pass (detective.dossier + llm).

The director pass is a bounded, opt-in reasoning step inserted between the
broad aggregate gather and the mechanical mismatch detectors: the provider
proposes targeted follow-up web queries for thin CHECKABLE claims, each query
is executed with a bounded, never-raise web search, and the results are
attached as director_followup evidence BEFORE the detectors and the scorer
run. The director only ADDS evidence and PROPOSES where to look; it never sets
tiers, the score, or the verdict, and a followup that finds nothing is an
absence, never a DISPROVEN.

All tests are offline: verify.gather_evidence and search.web_search are patched
with deterministic fakes. No network, never a live fetch. No em dashes (house
rule).
"""

from __future__ import annotations

import json

import pytest

from detective import verify
from detective import search
from detective import dossier as dossier_mod
from detective.dossier import build_dossier
from detective.llm import (
    FollowupQuery,
    LLMProvider,
    ManualProvider,
    compute_founder_score,
    mechanical_decompose,
)
from detective.models import Claim, Dossier, EvidenceTier


# ---------------------------------------------------------------------------
# Base provider no-op and the FollowupQuery type
# ---------------------------------------------------------------------------


def test_base_plan_followups_returns_empty():
    """The base LLMProvider.plan_followups is a no-op ([]), so the pass is
    opt-in and nothing breaks for a provider that does not implement it."""
    assert LLMProvider().plan_followups([], {}) == []
    claims = [Claim(type="identity", assertion="A real person exists.")]
    assert LLMProvider().plan_followups(claims, {"name": "X"}) == []


def test_followup_query_from_dict_is_defensive():
    fq = FollowupQuery.from_dict(
        {"claim_index": 2, "query": "q", "rationale": "r", "kind": "web"}
    )
    assert fq.claim_index == 2 and fq.query == "q"
    assert fq.rationale == "r" and fq.kind == "web"
    # Coerces a stringy index, defaults kind to "web" and rationale to "".
    fq2 = FollowupQuery.from_dict({"claim_index": "3", "query": "q2"})
    assert fq2.claim_index == 3 and fq2.kind == "web" and fq2.rationale == ""
    # A bad/missing index degrades to a skippable sentinel, never raises.
    fq3 = FollowupQuery.from_dict({"query": "q3"})
    assert isinstance(fq3.claim_index, int) and fq3.claim_index < 0


# ---------------------------------------------------------------------------
# Offline fakes + a planning stub provider
# ---------------------------------------------------------------------------


def _fake_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    """Deterministic offline gather: every claim comes back with one generic,
    uncorroborating web hit (thin), so the director has something to enrich."""
    claim.evidence = [
        {"source_url": "https://g.test", "snippet": "generic result, no corroboration"}
    ]
    return claim


@pytest.fixture
def patch_gather(monkeypatch):
    monkeypatch.setattr(verify, "gather_evidence", _fake_gather)


def _person_raw():
    # No experience descriptions, so decomposition is exactly:
    #   claim 0 = identity, claim 1 = employment (no traction claims).
    return {
        "profile_url": "https://www.linkedin.com/in/test-person/",
        "scan_type": "person",
        "identity": {"name": "Test Person", "headline": "Analyst", "current_company": "Acme"},
        "experience": [
            {"title": "Analyst", "company": "Acme", "start_date": "Jan 2019", "end_date": "Dec 2021"},
        ],
        "education": [],
    }


class PlanningStubProvider(LLMProvider):
    """In-process stand-in for a director-capable provider. plan_followups
    returns a fixed list; assign_tiers_and_verdict records which source_names
    it saw (to prove director evidence was attached before scoring) and marks
    everything UNVERIFIED (never invents a contradiction)."""

    def __init__(self, followups):
        self._followups = list(followups)
        self.saw_source_names = None

    def decompose_claims(self, raw_profile: dict) -> list[Claim]:
        return mechanical_decompose(raw_profile)

    def plan_followups(self, dossier_or_claims, identity=None):
        return list(self._followups)

    def assign_tiers_and_verdict(self, dossier: Dossier) -> Dossier:
        seen = set()
        for c in dossier.claims:
            for e in c.evidence or []:
                seen.add(e.get("source_name") or "")
            c.tier = EvidenceTier.UNVERIFIED
        self.saw_source_names = seen
        dossier.larp_score = compute_founder_score(dossier.claims)
        dossier.verdict = "stub verdict"
        return dossier


# ---------------------------------------------------------------------------
# (b) build_dossier unchanged when no followups are planned
# ---------------------------------------------------------------------------


def test_build_dossier_unchanged_when_no_followups(patch_gather, monkeypatch):
    """With plan_followups -> [] the director pass is a pure no-op: web_search
    is never called and no director_followup evidence appears anywhere."""

    def boom(*a, **k):
        raise AssertionError("web_search must not be called when nothing is planned")

    monkeypatch.setattr(search, "web_search", boom)
    d = build_dossier(_person_raw(), provider=PlanningStubProvider([]), emit=lambda *a: None)
    assert d.founder_larp_score is not None
    assert not any(
        (e.get("source_name") == "director_followup")
        for c in d.claims
        for e in c.evidence or []
    )


# ---------------------------------------------------------------------------
# (c) a planned followup is executed and attached BEFORE scoring
# ---------------------------------------------------------------------------


def test_director_followup_attaches_evidence_before_scoring(patch_gather, monkeypatch):
    calls = []

    def fake_search(query, count=8):
        calls.append(query)
        return [
            {
                "title": "coverage",
                "url": "https://found.test/1",
                "snippet": "Test Person was an Analyst at Acme, confirmed by the record.",
            }
        ]

    monkeypatch.setattr(search, "web_search", fake_search)
    fq = FollowupQuery(claim_index=1, query="Test Person Analyst Acme", rationale="verify employer", kind="web")
    provider = PlanningStubProvider([fq])
    d = build_dossier(_person_raw(), provider=provider, emit=lambda *a: None)

    assert calls == ["Test Person Analyst Acme"]
    director = [e for e in d.claims[1].evidence if e.get("source_name") == "director_followup"]
    assert len(director) == 1
    assert director[0]["source_url"] == "https://found.test/1"
    assert "verify employer" in director[0]["snippet"]
    # Attached BEFORE scoring: the provider saw the director record.
    assert "director_followup" in provider.saw_source_names
    # The director only ADDS evidence, it never sets a tier.
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


def test_director_followup_rejects_generic_or_namesake_results(
    patch_gather, monkeypatch
):
    monkeypatch.setattr(
        search,
        "web_search",
        lambda query, count=8: [
            {
                "url": "https://news.test/generic",
                "title": "Acme announces earnings",
                "snippet": "Acme reported quarterly earnings.",
            },
            {
                "url": "https://news.test/namesake",
                "title": "Another Test Person",
                "snippet": "Test Person joined a different company as an analyst.",
            },
        ],
    )
    monkeypatch.setattr(search, "search_healthy", lambda: True)
    fq = FollowupQuery(
        claim_index=1,
        query='"Test Person" "Acme" Analyst',
        rationale="verify employer",
        kind="web",
    )

    d = build_dossier(
        _person_raw(),
        provider=PlanningStubProvider([fq]),
        emit=lambda *a: None,
    )

    assert not any(
        e.get("source_name") == "director_followup"
        for e in d.claims[1].evidence
    )
    assert any(
        e.get("source_name") == "searched_no_results"
        for e in d.claims[1].evidence
    )


def test_director_followup_records_provenance_and_role_binding(
    patch_gather, monkeypatch
):
    monkeypatch.setattr(
        search,
        "web_search",
        lambda query, count=8: [
            {
                "url": "https://news.test/profile",
                "title": "Test Person at Acme",
                "snippet": "Test Person was an Analyst at Acme.",
            }
        ],
    )
    fq = FollowupQuery(
        claim_index=1,
        query='"Test Person" "Acme" Analyst',
        rationale="verify employer",
        kind="web",
    )

    d = build_dossier(
        _person_raw(),
        provider=PlanningStubProvider([fq]),
        emit=lambda *a: None,
    )

    record = next(
        e for e in d.claims[1].evidence
        if e.get("source_name") == "director_followup"
    )
    assert record["claim_relevance"] == "substantive"
    assert record["relationship"] == "third_party"
    assert record["source_class"] == "search_index"


def test_public_role_gets_required_followup_when_planner_skips_it(
    monkeypatch,
):
    def thin_gather(
        claim,
        identity=None,
        pb_budget=None,
        company_url=None,
        *,
        max_evidence=8,
    ):
        if claim.type == "employment":
            claim.evidence = [
                {
                    "source_url": "https://www.linkedin.com/in/elena-chen/",
                    "snippet": "Elena Chen is Head of Growth at Ditto.",
                    "relationship": "subject_controlled",
                    "source_class": "search_index",
                    "claim_relevance": "substantive",
                }
            ]
        else:
            claim.evidence = []
        return claim

    searches = []

    def fake_search(query, count=8):
        searches.append(query)
        return [
            {
                "url": "https://news.test/ditto",
                "title": "Ditto profile",
                "snippet": "Elena Chen is Head of Growth at Ditto.",
            }
        ]

    monkeypatch.setattr(verify, "gather_evidence", thin_gather)
    monkeypatch.setattr(search, "web_search", fake_search)
    raw = {
        "profile_url": "https://www.linkedin.com/in/elena-chen/",
        "scan_type": "person",
        "identity": {
            "name": "Elena Chen",
            "headline": "Head of Growth",
            "current_company": "Ditto",
        },
        "experience": [
            {
                "title": "Head of Growth",
                "company": "Ditto",
                "start_date": "May 2025",
                "end_date": "Present",
            }
        ],
        "education": [],
    }

    d = build_dossier(
        raw,
        provider=PlanningStubProvider([]),
        emit=lambda *a: None,
    )

    assert any(q.endswith('"Head of Growth" interview') for q in searches)
    assert any(
        e.get("source_name") == "director_followup"
        and e.get("claim_relevance") == "substantive"
        for e in d.claims[1].evidence
    )


def test_director_evidence_record_shape_is_usable_web_evidence():
    """The director_followup record is shaped as plain, usable web evidence
    (source_name set, no match_confidence "low"), and is deliberately NOT a
    structured corroborating source, so GAP suppression flows through the
    snippet-corroboration path, not the connector allowlist."""
    rec = {"source_url": "u", "snippet": "s", "source_name": "director_followup"}
    assert dossier_mod._snippet_record_usable(rec) is True
    assert "director_followup" not in dossier_mod._CORROBORATING_SOURCES


def test_director_followup_can_suppress_gap(patch_gather, monkeypatch):
    """Because the director runs BEFORE the detectors, a followup that returns
    a strong snippet actually talking about the subject suppresses the GAP for
    that notable claim (the whole point: found a trace where the broad gather
    had not)."""

    def fake_search(query, count=8):
        return [
            {
                "url": "https://news.test/x",
                "title": "announcement",
                "snippet": "Test Person is VP of Engineering at Google, per the announcement.",
            }
        ]

    monkeypatch.setattr(search, "web_search", fake_search)
    raw = _person_raw()
    raw["experience"] = [
        {"title": "VP of Engineering", "company": "Google", "start_date": "Jan 2019", "end_date": "Present"}
    ]
    fq = FollowupQuery(claim_index=1, query="Test Person Google VP", rationale="find coverage", kind="web")
    d = build_dossier(raw, provider=PlanningStubProvider([fq]), emit=lambda *a: None)
    claim1_kinds = [m["kind"] for m in d.mismatches if 1 in m["claim_indices"]]
    assert "GAP" not in claim1_kinds


# ---------------------------------------------------------------------------
# (d) never-raise: a followup that errors or finds nothing is a benign absence
# ---------------------------------------------------------------------------


def test_followup_search_raising_never_breaks_scan(patch_gather, monkeypatch):
    def boom(query, count=8):
        raise RuntimeError("search backend down")

    monkeypatch.setattr(search, "web_search", boom)
    fq = FollowupQuery(claim_index=1, query="q", rationale="r", kind="web")
    d = build_dossier(_person_raw(), provider=PlanningStubProvider([fq]), emit=lambda *a: None)
    assert d.founder_larp_score is not None
    assert not any(
        e.get("source_name") == "director_followup" for c in d.claims for e in c.evidence or []
    )
    assert not any(
        e.get("source_url") == "internal://searched/director-followup"
        for c in d.claims
        for e in c.evidence or []
    )
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


def test_followup_finds_nothing_is_absence_not_disproven(patch_gather, monkeypatch):
    monkeypatch.setattr(search, "web_search", lambda query, count=8: [])
    monkeypatch.setattr(search, "search_healthy", lambda: True)
    fq = FollowupQuery(claim_index=1, query="q", rationale="r", kind="web")
    d = build_dossier(_person_raw(), provider=PlanningStubProvider([fq]), emit=lambda *a: None)
    assert not any(
        e.get("source_name") == "director_followup" for c in d.claims for e in c.evidence or []
    )
    markers = [
        e
        for e in d.claims[1].evidence
        if e.get("source_url") == "internal://searched/director-followup"
    ]
    assert len(markers) == 1
    assert markers[0]["source_name"] == "searched_no_results"
    assert "zero results" in markers[0]["snippet"]
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


def test_site_scoped_followup_drops_backend_results_from_other_domains(
    patch_gather, monkeypatch
):
    monkeypatch.setattr(
        search,
        "web_search",
        lambda query, count=8: [
            {
                "url": "https://unrelated.example/profile",
                "title": "Namesake",
                "snippet": "The search backend ignored the site operator.",
            }
        ],
    )
    monkeypatch.setattr(search, "search_healthy", lambda: True)
    fq = FollowupQuery(
        claim_index=1,
        query='site:acme.example "Test Person"',
        rationale="check employer roster",
        kind="web",
    )

    d = build_dossier(
        _person_raw(),
        provider=PlanningStubProvider([fq]),
        emit=lambda *a: None,
    )

    assert not any(
        e.get("source_url") == "https://unrelated.example/profile"
        for e in d.claims[1].evidence
    )
    assert any(
        e.get("source_name") == "searched_no_results"
        for e in d.claims[1].evidence
    )


def test_followup_empty_while_search_dark_is_unavailable_not_absence(
    patch_gather, monkeypatch
):
    monkeypatch.setattr(search, "web_search", lambda query, count=8: [])
    monkeypatch.setattr(search, "search_healthy", lambda: False)
    fq = FollowupQuery(claim_index=1, query="q", rationale="r", kind="web")
    d = build_dossier(_person_raw(), provider=PlanningStubProvider([fq]), emit=lambda *a: None)
    assert not any(
        e.get("source_url") == "internal://searched/director-followup"
        for c in d.claims
        for e in c.evidence or []
    )


def test_provider_plan_raising_never_breaks_scan(patch_gather, monkeypatch):
    monkeypatch.setattr(search, "web_search", lambda *a, **k: [])

    class Boom(PlanningStubProvider):
        def plan_followups(self, dossier_or_claims, identity=None):
            raise RuntimeError("planner exploded")

    d = build_dossier(_person_raw(), provider=Boom([]), emit=lambda *a: None)
    assert d.founder_larp_score is not None


# ---------------------------------------------------------------------------
# Bounding: total followups capped, non-web / bad-index skipped
# ---------------------------------------------------------------------------


def test_director_caps_total_followups(patch_gather, monkeypatch):
    calls = []
    monkeypatch.setattr(search, "web_search", lambda query, count=8: (calls.append(query), [])[1])
    fqs = [FollowupQuery(claim_index=1, query=f"q{i}", kind="web") for i in range(20)]
    build_dossier(_person_raw(), provider=PlanningStubProvider(fqs), emit=lambda *a: None)
    assert len(calls) == dossier_mod._DIRECTOR_MAX_FOLLOWUPS


def test_director_skips_non_web_and_out_of_range_index(patch_gather, monkeypatch):
    calls = []
    monkeypatch.setattr(search, "web_search", lambda query, count=8: (calls.append(query), [])[1])
    fqs = [
        FollowupQuery(claim_index=1, query="ok", kind="web"),
        FollowupQuery(claim_index=1, query="skip-non-web", kind="lab_roster"),
        FollowupQuery(claim_index=999, query="skip-oor", kind="web"),
        FollowupQuery(claim_index=-1, query="skip-neg", kind="web"),
    ]
    build_dossier(_person_raw(), provider=PlanningStubProvider(fqs), emit=lambda *a: None)
    assert calls == ["ok"]


# ---------------------------------------------------------------------------
# ManualProvider plan job: file structure + idempotent read-back
# ---------------------------------------------------------------------------


def test_manual_provider_plan_job_file_structure(tmp_path, monkeypatch):
    monkeypatch.delenv("MANUAL_QUEUE_TIMEOUT_S", raising=False)
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_plan_1")
    claims = mechanical_decompose(_person_raw())
    out = provider.plan_followups(claims, {"name": "Test Person"})
    # timeout 0 (default): queue and return [] immediately, never hang.
    assert out == []
    job = tmp_path / "job_plan_1_plan.json"
    assert job.exists()
    data = json.loads(job.read_text(encoding="utf-8"))
    assert data["kind"] == "plan"
    assert data["status"] == "pending"
    assert data["result"] == {"followups": []}
    # The operator gets a claims/evidence view to reason over.
    assert isinstance(data.get("claims"), list) and data["claims"]


def test_manual_provider_plan_job_idempotent_readback(tmp_path, monkeypatch):
    monkeypatch.delenv("MANUAL_QUEUE_TIMEOUT_S", raising=False)
    claims = mechanical_decompose(_person_raw())
    ManualProvider(queue_dir=tmp_path, job_id="job_plan_2").plan_followups(claims, {"name": "Test Person"})
    job = tmp_path / "job_plan_2_plan.json"
    data = json.loads(job.read_text(encoding="utf-8"))
    data["status"] = "completed"
    data["result"] = {
        "followups": [{"claim_index": 1, "query": "verify Acme", "rationale": "thin", "kind": "web"}]
    }
    job.write_text(json.dumps(data), encoding="utf-8")
    # A fresh provider with the same job_id reads the completed followups back.
    out = ManualProvider(queue_dir=tmp_path, job_id="job_plan_2").plan_followups(
        claims, {"name": "Test Person"}
    )
    assert len(out) == 1
    assert isinstance(out[0], FollowupQuery)
    assert out[0].claim_index == 1 and out[0].query == "verify Acme" and out[0].kind == "web"
