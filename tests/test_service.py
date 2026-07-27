"""Tests for the local overlay service (detective/service.py).

Offline only: exercises the demo path (bundled fixture profile, no network,
no login) end to end through FastAPI's TestClient, POSTing /scan then
draining the /events/{job_id} websocket. No em dashes (house rule).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from detective.llm import QUEUE_DIR
from detective import service
from detective.service import app

_MAX_EVENTS = 500  # safety cap so a regression cannot hang the test suite


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


def _cleanup_job_file(job_id: str) -> None:
    path = Path(QUEUE_DIR) / f"{job_id}.json"
    if path.exists():
        path.unlink()


def test_health_identifies_the_serving_project_root():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "project_root": str(Path(service.__file__).resolve().parent.parent),
    }


def test_browser_companion_accepts_only_linkedin_profile_urls():
    service._active_browser_tab.clear()
    service._browser_companion_presence.clear()
    with TestClient(app) as client:
        rejected = client.post("/browser-tab", json={"url": "https://example.com/not-linkedin"})
        assert rejected.status_code == 200
        assert rejected.json() == {"accepted": False}
        assert client.get("/browser-tab").json()["connected"] is False

        accepted = client.post(
            "/browser-tab",
            json={"url": "https://www.linkedin.com/in/example-person/", "browser": "test"},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"accepted": True}
        status = client.get("/browser-tab").json()

    assert status["connected"] is True
    assert status["url"] == "https://www.linkedin.com/in/example-person/"
    assert status["browser"] == "test"
    service._active_browser_tab.clear()
    service._browser_companion_presence.clear()


def test_browser_companion_presence_does_not_require_a_linkedin_tab():
    service._active_browser_tab.clear()
    service._browser_companion_presence.clear()
    with TestClient(app) as client:
        accepted = client.post(
            "/browser-companion", json={"browser": "test-browser"}
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"accepted": True}

        presence = client.get("/browser-companion").json()
        active_tab = client.get("/browser-tab").json()

    assert presence["connected"] is True
    assert presence["browser"] == "test-browser"
    assert active_tab["connected"] is False
    service._browser_companion_presence.clear()


def test_demo_person_scan_streams_to_scores_verdict_done():
    with TestClient(app) as client:
        resp = client.post("/scan", json={"url": "demo", "scan_type": "person"})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        assert job_id

        try:
            events = _drain(client, job_id)
        finally:
            _cleanup_job_file(job_id)

    types = [e["type"] for e in events]

    # Terminal sequence, in order: scores, verdict, done.
    assert types[-3:] == ["scores", "verdict", "done"]

    # Live progress happened before the final sequence.
    assert "status" in types
    assert "claim" in types

    # Every event type is one of the documented vocabulary.
    allowed = {"status", "image", "thought", "website", "claim", "scores", "verdict", "done", "error"}
    assert set(types) <= allowed

    # No error anywhere in a clean demo run.
    assert "error" not in types

    scores_event = next(e for e in events if e["type"] == "scores")
    assert scores_event["founder_larp_score"] is not None
    assert isinstance(scores_event["founder_larp_score"], int)
    assert isinstance(scores_event["company_larp_score"], int)
    assert isinstance(scores_event["overall_larp_score"], int)
    assert scores_event["company_assessments"]

    verdict_event = next(e for e in events if e["type"] == "verdict")
    assert verdict_event["text"]

    claim_events = [e for e in events if e["type"] == "claim"]
    assert all(c["tier"] in ("DISPROVEN", "UNVERIFIED", "CONFIRMED") for c in claim_events)
    assert all(c["assertion"] for c in claim_events)

    # Rich "showing its work" events: thoughts and website cards, streamed
    # before the terminal scores/verdict/done sequence.
    thought_events = [e for e in events if e["type"] == "thought"]
    assert thought_events, "expected at least one thought event"
    assert all(t["text"] for t in thought_events)

    website_events = [e for e in events if e["type"] == "website"]
    assert website_events, "expected at least one website event"
    for w in website_events:
        assert w["url"]
        assert w["domain"]
        assert w["title"]
        assert w["favicon"].startswith("https://www.google.com/s2/favicons?domain=")
        assert w["domain"] in w["favicon"]

    # Website cards are deduped by domain and capped (~6) for the whole scan.
    domains = [w["domain"] for w in website_events]
    assert len(domains) == len(set(domains))
    assert len(domains) <= 6

    image_events = [e for e in events if e["type"] == "image"]
    assert image_events, "expected at least one image event (hero or thumbnail)"
    assert all(img["url"] for img in image_events)

    # Rich events all happen before the terminal sequence.
    tail_start = len(types) - 3
    assert all(i < tail_start for i, t in enumerate(types) if t in ("thought", "website")), (
        "thought/website events must stream before scores/verdict/done"
    )


def test_demo_company_scan_streams_to_scores_verdict_done():
    with TestClient(app) as client:
        resp = client.post("/scan", json={"demo": True, "scan_type": "company_app"})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        try:
            events = _drain(client, job_id)
        finally:
            _cleanup_job_file(job_id)

    types = [e["type"] for e in events]
    assert types[-3:] == ["scores", "verdict", "done"]
    assert "error" not in types

    scores_event = next(e for e in events if e["type"] == "scores")
    assert scores_event["company_larp_score"] is not None
    assert isinstance(scores_event["company_larp_score"], int)
    assert scores_event["founder_larp_score"] is None

    thought_events = [e for e in events if e["type"] == "thought"]
    assert thought_events

    website_events = [e for e in events if e["type"] == "website"]
    assert website_events
    for w in website_events:
        assert w["favicon"].startswith("https://www.google.com/s2/favicons?domain=")
        assert w["domain"] in w["favicon"]

    image_events = [e for e in events if e["type"] == "image"]
    assert image_events
    # Company hero: a Clearbit logo of the scanned url's own domain.
    assert any(img["url"].startswith("https://logo.clearbit.com/") for img in image_events)


def test_unknown_job_id_gets_error_then_done():
    with TestClient(app) as client:
        events = _drain(client, "job_does_not_exist")

    types = [e["type"] for e in events]
    assert types == ["error", "done"]


def test_missing_input_gets_error_then_done():
    with TestClient(app) as client:
        resp = client.post("/scan", json={"scan_type": "person"})
        job_id = resp.json()["job_id"]

        try:
            events = _drain(client, job_id)
        finally:
            _cleanup_job_file(job_id)

    types = [e["type"] for e in events]
    assert types == ["error", "done"]
