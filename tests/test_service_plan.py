"""Tests for wiring the director / planning round trip into the live scan
flow (detective/service.py).

Two layers:

  1. Unit tests for _PlanWaitingManualProvider, the service-level ManualProvider
     that gives ONLY the plan round trip a bounded, blocking wait (mirroring
     tests/test_service_vision_extract.py's direct coverage of the vision-queue
     watch loop). These are deterministic and never touch the websocket, so the
     never-hang guarantee is proven without the full server dance.

  2. End-to-end tests through FastAPI's TestClient that drive a real (non-demo)
     ManualProvider scan: pipeline.run is monkeypatched to call the REAL
     build_dossier over a fixed raw_profile (so no live LinkedIn fetch happens),
     while verify.gather_evidence and search.web_search are patched with
     deterministic offline fakes. A stand-in operator thread fills first the
     plan job and then the scoring job, and the test asserts director_followup
     evidence was attached to the dossier BEFORE scoring, and that an unfilled
     plan job degrades to no follow-ups without hanging.

Offline only: no network, never a live fetch. No em dashes (house rule).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

import detective.pipeline as pipeline_module
import detective.service as service_module
from detective import search, verify
from detective.dossier import build_dossier
from detective.llm import (
    FollowupQuery,
    ManualProvider,
    QUEUE_DIR,
    mechanical_decompose,
)
from detective.service import (
    _PlanWaitingManualProvider,
    _manual_provider_for_scan,
    app,
)

_MAX_EVENTS = 500  # safety cap so a regression cannot hang the test suite


# ---------------------------------------------------------------------------
# Shared offline fixtures (mirrors tests/test_director_pass.py)
# ---------------------------------------------------------------------------


def _person_raw() -> dict:
    # No experience descriptions, so decomposition is exactly:
    #   claim 0 = identity, claim 1 = employment.
    return {
        "profile_url": "https://www.linkedin.com/in/test-person/",
        "scan_type": "person",
        "identity": {"name": "Test Person", "headline": "Analyst", "current_company": "Acme"},
        "experience": [
            {"title": "Analyst", "company": "Acme", "start_date": "Jan 2019", "end_date": "Dec 2021"},
        ],
        "education": [],
    }


def _fake_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    """Every claim comes back with one generic, uncorroborating web hit (thin),
    so the director has something to enrich and no live fetch ever happens."""
    claim.evidence = [
        {"source_url": "https://g.test", "snippet": "generic result, no corroboration"}
    ]
    return claim


def _cleanup_job_files(job_id: str) -> None:
    for suffix in (".json", "_plan.json", "_vision.json", "_dossier.json"):
        path = Path(QUEUE_DIR) / f"{job_id}{suffix}"
        if path.exists():
            path.unlink()


def _drain(client: TestClient, job_id: str) -> list[dict]:
    events: list[dict] = []
    with client.websocket_connect(f"/events/{job_id}") as ws:
        for _ in range(_MAX_EVENTS):
            msg = ws.receive_json()
            events.append(msg)
            if msg.get("type") == "done":
                break
        else:
            raise AssertionError("websocket never sent a done event")
    return events


# ---------------------------------------------------------------------------
# 1. Unit tests: _PlanWaitingManualProvider (bounded plan-queue watch)
# ---------------------------------------------------------------------------


def test_manual_provider_for_scan_demo_is_plain_never_the_wrapper():
    # The demo path must NOT get the wrapper: it never fills a plan job, so a
    # blocking plan wait would hang the demo. Plain ManualProvider returns []
    # from plan_followups immediately.
    provider = _manual_provider_for_scan("job_demo_plain", is_demo=True)
    assert type(provider) is ManualProvider
    assert not isinstance(provider, _PlanWaitingManualProvider)


def test_manual_provider_for_scan_real_is_the_plan_waiting_wrapper():
    provider = _manual_provider_for_scan("job_real_wrapped", is_demo=False)
    assert isinstance(provider, _PlanWaitingManualProvider)


def test_plan_waiting_provider_reads_already_completed_immediately(tmp_path, monkeypatch):
    monkeypatch.delenv("MANUAL_QUEUE_TIMEOUT_S", raising=False)
    provider = _PlanWaitingManualProvider(
        queue_dir=tmp_path, job_id="job_pw_ready", plan_timeout_s=30.0
    )
    plan_path = tmp_path / "job_pw_ready_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "job_id": "job_pw_ready",
                "status": "completed",
                "kind": "plan",
                "result": {"followups": [{"claim_index": 1, "query": "x", "kind": "web"}]},
            }
        ),
        encoding="utf-8",
    )
    claims = mechanical_decompose(_person_raw())

    started = time.monotonic()
    out = provider.plan_followups(claims, {"name": "Test Person"})
    elapsed = time.monotonic() - started

    assert len(out) == 1 and isinstance(out[0], FollowupQuery)
    assert out[0].query == "x"
    # An already-completed job is read back at once, never polled.
    assert elapsed < 1.0


def test_plan_waiting_provider_returns_empty_on_timeout_when_unfilled(tmp_path, monkeypatch):
    monkeypatch.delenv("MANUAL_QUEUE_TIMEOUT_S", raising=False)
    provider = _PlanWaitingManualProvider(
        queue_dir=tmp_path, job_id="job_pw_timeout", plan_timeout_s=0.3
    )
    claims = mechanical_decompose(_person_raw())

    started = time.monotonic()
    out = provider.plan_followups(claims, {"name": "Test Person"})
    elapsed = time.monotonic() - started

    # Bounded: proceeds with NO follow-ups rather than hanging.
    assert out == []
    assert elapsed < 3.0
    # The pending plan job was still written, for an operator who shows up late.
    assert (tmp_path / "job_pw_timeout_plan.json").exists()


def test_plan_waiting_provider_picks_up_completion_mid_poll(tmp_path, monkeypatch):
    # The realistic case this wiring targets: nothing completed on disk when
    # the wait starts, and the plan file is filled a little into the poll loop
    # by another actor (here a concurrent thread standing in for the operator).
    monkeypatch.delenv("MANUAL_QUEUE_TIMEOUT_S", raising=False)
    provider = _PlanWaitingManualProvider(
        queue_dir=tmp_path, job_id="job_pw_mid", plan_timeout_s=5.0
    )
    claims = mechanical_decompose(_person_raw())
    result_box: dict = {}

    def _call() -> None:
        result_box["out"] = provider.plan_followups(claims, {"name": "Test Person"})

    worker = threading.Thread(target=_call)
    worker.start()

    plan_path = tmp_path / "job_pw_mid_plan.json"
    deadline = time.time() + 3.0
    while time.time() < deadline and not plan_path.exists():
        time.sleep(0.02)
    assert plan_path.exists(), "plan job file was never written"

    data = json.loads(plan_path.read_text(encoding="utf-8"))
    data["status"] = "completed"
    data["result"] = {
        "followups": [{"claim_index": 1, "query": "verify Acme", "rationale": "thin", "kind": "web"}]
    }
    plan_path.write_text(json.dumps(data), encoding="utf-8")

    worker.join(timeout=5.0)
    assert not worker.is_alive(), "plan wait did not return after completion"

    out = result_box["out"]
    assert len(out) == 1 and isinstance(out[0], FollowupQuery)
    assert out[0].claim_index == 1 and out[0].query == "verify Acme" and out[0].kind == "web"


# ---------------------------------------------------------------------------
# 2. End-to-end: a live ManualProvider scan round-trips the plan job
# ---------------------------------------------------------------------------


def _install_offline_dossier_run(monkeypatch, captured: dict):
    """Monkeypatch pipeline.run to call the REAL build_dossier over a fixed
    person raw_profile (no live fetch), capturing the provider the service
    handed in. verify.gather_evidence and search.web_search are patched offline.
    _verdict_image_events is stubbed so the verdict step never hits the network
    on a real (non-demo) scan.
    """
    monkeypatch.setattr(verify, "gather_evidence", _fake_gather)
    monkeypatch.setattr(service_module, "_verdict_image_events", lambda *a, **k: [])

    def fake_search(query, count=8):
        captured.setdefault("search_queries", []).append(query)
        return [
            {
                "title": "coverage",
                "url": "https://found.test/1",
                "snippet": "Test Person was an Analyst at Acme, confirmed by the record.",
            }
        ]

    monkeypatch.setattr(search, "web_search", fake_search)

    def fake_run(url, provider=None, live=False, progress=None, raw_profile=None,
                 scan_type="person", engine="per_claim"):
        captured["provider"] = provider
        captured["live"] = live
        return build_dossier(
            _person_raw(), provider=provider, emit=progress, scan_type="person"
        )

    monkeypatch.setattr(pipeline_module, "run", fake_run)


def _operator_fill_plan_then_score(job_id: str, followups, fill_score: bool, stop: threading.Event):
    """Stand-in operator: wait for the plan job to appear and (optionally) fill
    it, then wait for the scoring job and complete it so the scan can finish.
    Runs in a background thread while the test drains the websocket.
    """
    plan_path = Path(QUEUE_DIR) / f"{job_id}_plan.json"
    score_path = Path(QUEUE_DIR) / f"{job_id}.json"

    if followups is not None:
        deadline = time.time() + 15.0
        while time.time() < deadline and not stop.is_set():
            if plan_path.exists():
                data = json.loads(plan_path.read_text(encoding="utf-8"))
                if data.get("status") != "completed":
                    data["status"] = "completed"
                    data["result"] = {"followups": followups}
                    plan_path.write_text(json.dumps(data), encoding="utf-8")
                break
            time.sleep(0.05)

    if fill_score:
        deadline = time.time() + 15.0
        while time.time() < deadline and not stop.is_set():
            if score_path.exists():
                data = json.loads(score_path.read_text(encoding="utf-8"))
                if data.get("status") != "completed":
                    # Reuse the demo auto-scorer: it fills tiers/score/verdict and
                    # PRESERVES each claim's evidence, so director_followup records
                    # written before scoring survive into the completed file.
                    service_module._auto_complete_demo_job(score_path)
                break
            time.sleep(0.05)


def test_live_scan_director_plan_roundtrip_attaches_followup_before_scoring(monkeypatch):
    # A generous but bounded plan wait; the operator thread fills it fast.
    monkeypatch.setattr(service_module, "_PLAN_QUEUE_TIMEOUT_S", 8.0)
    captured: dict = {}
    _install_offline_dossier_run(monkeypatch, captured)

    followups = [
        {"claim_index": 1, "query": "verify Acme employment", "rationale": "thin claim", "kind": "web"}
    ]
    stop = threading.Event()

    with TestClient(app) as client:
        resp = client.post("/scan", json={"url": "https://www.linkedin.com/in/test-person/", "scan_type": "person"})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        operator = threading.Thread(
            target=_operator_fill_plan_then_score, args=(job_id, followups, True, stop)
        )
        operator.start()
        try:
            events = _drain(client, job_id)
        finally:
            stop.set()
            operator.join(timeout=5.0)
            score_path = Path(QUEUE_DIR) / f"{job_id}.json"
            scored = json.loads(score_path.read_text(encoding="utf-8")) if score_path.exists() else {}
            _cleanup_job_files(job_id)

    # The service handed build_dossier the plan-waiting wrapper, not a plain
    # ManualProvider (this is what makes the round trip happen at all).
    assert type(captured["provider"]).__name__ == "_PlanWaitingManualProvider"
    assert captured["live"] is True

    # The planned follow-up query actually ran (its evidence is what we assert on).
    assert "verify Acme employment" in captured.get("search_queries", [])

    types = [e["type"] for e in events]
    assert "error" not in types
    assert types[-3:] == ["scores", "verdict", "done"]

    # The director_followup evidence was attached to the dossier BEFORE scoring:
    # the scoring queue file (written by assign_tiers_and_verdict over the
    # enriched claims) carries it.
    claims = (scored.get("dossier", {}) or {}).get("claims", [])
    director = [
        e
        for c in claims
        for e in (c.get("evidence") or [])
        if e.get("source_name") == "director_followup"
    ]
    assert director, "expected director_followup evidence attached before scoring"
    assert director[0]["source_url"] == "https://found.test/1"

    # Scoring ran over the enriched evidence and produced a real score.
    scores_event = next(e for e in events if e["type"] == "scores")
    assert scores_event["founder_larp_score"] is not None


def test_live_scan_unfilled_plan_degrades_to_no_followups_without_hanging(monkeypatch):
    # A short plan timeout so an UNFILLED plan job degrades quickly. The
    # operator thread fills ONLY the scoring job, never the plan job.
    monkeypatch.setattr(service_module, "_PLAN_QUEUE_TIMEOUT_S", 0.5)
    captured: dict = {}
    _install_offline_dossier_run(monkeypatch, captured)

    stop = threading.Event()

    with TestClient(app) as client:
        resp = client.post("/scan", json={"url": "https://www.linkedin.com/in/test-person/", "scan_type": "person"})
        job_id = resp.json()["job_id"]

        operator = threading.Thread(
            target=_operator_fill_plan_then_score, args=(job_id, None, True, stop)
        )
        operator.start()
        started = time.monotonic()
        try:
            events = _drain(client, job_id)
        finally:
            stop.set()
            operator.join(timeout=5.0)
            elapsed = time.monotonic() - started
            score_path = Path(QUEUE_DIR) / f"{job_id}.json"
            scored = json.loads(score_path.read_text(encoding="utf-8")) if score_path.exists() else {}
            _cleanup_job_files(job_id)

    types = [e["type"] for e in events]
    assert "error" not in types
    assert types[-3:] == ["scores", "verdict", "done"]

    # No follow-ups ran: web_search was never called for a director query, and
    # no director_followup evidence appears in the scored dossier.
    assert not captured.get("search_queries")
    claims = (scored.get("dossier", {}) or {}).get("claims", [])
    assert not any(
        e.get("source_name") == "director_followup"
        for c in claims
        for e in (c.get("evidence") or [])
    )

    # Bounded: the unfilled plan wait (0.5s) plus the offline scan finished
    # quickly, nowhere near a hang.
    assert elapsed < 30.0
