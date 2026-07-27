"""Test for service.py's ApiProvider -> ManualProvider fallback wiring.

Offline only: monkeypatches detective.pipeline.run itself (not just the
Gemini call), so this test never triggers a real live fetch or a real
Gemini call. It only exercises _run_job's provider-selection and
except ApiProviderError fallback path. No em dashes (house rule).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import detective.pipeline as pipeline_module
from detective.llm import ApiProvider, ApiProviderError, ManualProvider
from detective.models import Claim, Dossier, EvidenceTier
from detective.service import app

_MAX_EVENTS = 500


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


def test_api_provider_failure_falls_back_to_manual_and_still_completes(monkeypatch):
    monkeypatch.setenv("LARP_SERVICE_PROVIDER", "api")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls: list[str] = []

    def fake_run(url, provider=None, live=False, progress=None, raw_profile=None, scan_type="person", engine="per_claim"):
        if isinstance(provider, ApiProvider):
            calls.append("api")
            raise ApiProviderError("simulated gemini quota exhausted")
        assert isinstance(provider, ManualProvider)
        calls.append("manual")
        # Stand in for "an operator already completed this job": build an
        # already-scored dossier directly, so this test exercises the
        # fallback wiring itself, not the real queue-file round trip
        # (covered by test_service.py's demo-path tests already).
        dossier = Dossier(profile_url=url, scan_type=scan_type, identity={"name": "Fallback Person"})
        dossier.claims = [
            Claim(
                type="identity",
                assertion="A real person named Fallback Person exists.",
                tier=EvidenceTier.CONFIRMED,
            )
        ]
        dossier.larp_score = 5
        dossier.founder_larp_score = 5
        dossier.verdict = "fallback test verdict"
        return dossier

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"url": "https://www.linkedin.com/in/someone-fallback-test/", "scan_type": "person"},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        events = _drain(client, job_id)

    assert calls == ["api", "manual"]  # ApiProvider tried first, then ManualProvider

    types = [e["type"] for e in events]
    assert "error" not in types  # falls back cleanly, never surfaces as an error
    assert types[-3:] == ["scores", "verdict", "done"]

    fallback_status = [e for e in events if e["type"] == "status" and "falling back" in e.get("text", "")]
    assert fallback_status, "expected a status event announcing the fallback"

    scores_event = next(e for e in events if e["type"] == "scores")
    assert scores_event["founder_larp_score"] == 5

    verdict_event = next(e for e in events if e["type"] == "verdict")
    assert verdict_event["text"] == "fallback test verdict"
