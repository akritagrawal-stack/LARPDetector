"""Tests for service.py's extract_from_screenshot routing (the overlay's
"Go" button, layer 2: no exact URL found, so the screenshot is read via the
provider's vision_extract, and the resolved profile_url is fed into the
normal live scrape -> verify -> verdict pipeline).

Offline only: monkeypatches detective.pipeline.run itself (same pattern as
test_service_provider_fallback.py) so this test never triggers a real live
LinkedIn fetch, and monkeypatches detective.service._select_provider /
detective.service.search.web_search so no real Gemini call or real web
search ever happens. No em dashes (house rule).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import detective.pipeline as pipeline_module
import detective.service as service_module
from detective.models import Claim, Dossier, EvidenceTier
from detective.service import app

_MAX_EVENTS = 500
_FAKE_SCREENSHOT_B64 = "ZmFrZQ=="  # base64 of "fake"


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


def _stub_dossier(url: str, scan_type: str) -> Dossier:
    dossier = Dossier(profile_url=url, scan_type=scan_type, identity={"name": "Screenshot Person"})
    dossier.claims = [
        Claim(
            type="identity",
            assertion="A real person named Screenshot Person exists.",
            tier=EvidenceTier.CONFIRMED,
        )
    ]
    dossier.larp_score = 3
    dossier.founder_larp_score = 3
    dossier.verdict = "screenshot-routed test verdict"
    return dossier


class _StubVisionProvider:
    """Minimal provider stub: returns a fixed vision_extract result, and is
    used for both the vision call and the (unused in this test) reasoning
    step, since the pipeline.run call itself is monkeypatched.
    """

    def __init__(self, vision_result: dict):
        self._vision_result = vision_result

    def vision_extract(self, screenshot_b64: str) -> dict:
        return dict(self._vision_result)


def test_screenshot_with_profile_url_flows_into_pipeline(monkeypatch):
    calls: dict = {}

    monkeypatch.setattr(
        service_module,
        "_select_provider",
        lambda job_id, is_demo: _StubVisionProvider(
            {
                "profile_url": "https://www.linkedin.com/in/screenshot-person/",
                "name": None,
                "headline": None,
                "company": None,
            }
        ),
    )

    def fake_run(url, provider=None, live=False, progress=None, raw_profile=None, scan_type="person", engine="per_claim"):
        calls["url"] = url
        calls["live"] = live
        calls["raw_profile"] = raw_profile
        calls["scan_type"] = scan_type
        return _stub_dossier(url, scan_type)

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "extract_from_screenshot": True, "scan_type": "person"},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        events = _drain(client, job_id)

    # The vision-extracted profile_url reached pipeline.run as a normal live
    # scan (raw_profile=None so it fetches), never a screenshot passthrough.
    assert calls["url"] == "https://www.linkedin.com/in/screenshot-person/"
    assert calls["live"] is True
    assert calls["raw_profile"] is None
    assert calls["scan_type"] == "person"

    types = [e["type"] for e in events]
    assert "error" not in types
    assert types[-3:] == ["scores", "verdict", "done"]
    assert any("found profile from your screen" in e.get("text", "") for e in events if e["type"] == "status")


def test_screenshot_with_only_name_resolves_via_web_search(monkeypatch):
    calls: dict = {}

    monkeypatch.setattr(
        service_module,
        "_select_provider",
        lambda job_id, is_demo: _StubVisionProvider(
            {"profile_url": None, "name": "Jane Searchable", "headline": "Engineer", "company": "Acme Corp"}
        ),
    )

    def fake_web_search(query, count=8):
        assert "Jane Searchable" in query
        assert "Acme Corp" in query
        return [{"title": "Jane Searchable | LinkedIn", "url": "https://www.linkedin.com/in/janesearchable/", "snippet": ""}]

    monkeypatch.setattr(service_module.search, "web_search", fake_web_search)

    def fake_run(url, provider=None, live=False, progress=None, raw_profile=None, scan_type="person", engine="per_claim"):
        calls["url"] = url
        # The scraped identity must match the vision name "Jane Searchable" or
        # the post-scrape identity gate would (correctly) route to needs_url.
        d = _stub_dossier(url, scan_type)
        d.identity = {"name": "Jane Searchable"}
        return d

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "extract_from_screenshot": True, "scan_type": "person"},
        )
        job_id = resp.json()["job_id"]
        events = _drain(client, job_id)

    assert calls["url"] == "https://www.linkedin.com/in/janesearchable/"
    types = [e["type"] for e in events]
    assert "error" not in types
    assert types[-3:] == ["scores", "verdict", "done"]
    thought_texts = [e["text"] for e in events if e["type"] == "thought"]
    assert any("Searching for Jane Searchable" in t for t in thought_texts)


def test_screenshot_with_nothing_extracted_emits_error_never_calls_pipeline(monkeypatch):
    pipeline_called = {"value": False}

    monkeypatch.setattr(
        service_module,
        "_select_provider",
        lambda job_id, is_demo: _StubVisionProvider(
            {"profile_url": None, "name": None, "headline": None, "company": None}
        ),
    )

    def fake_run(*args, **kwargs):
        pipeline_called["value"] = True
        raise AssertionError("pipeline.run must not be called when nothing could be extracted")

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "extract_from_screenshot": True, "scan_type": "person"},
        )
        job_id = resp.json()["job_id"]
        events = _drain(client, job_id)

    assert pipeline_called["value"] is False
    types = [e["type"] for e in events]
    # Nothing could be read: route to needs_url (return to the paste field),
    # never a dead error card and never a scored verdict off thin data.
    assert types == ["status", "needs_url", "done"]


def test_screenshot_without_extract_flag_is_rejected_cleanly():
    with TestClient(app) as client:
        resp = client.post("/scan", json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "scan_type": "person"})
        job_id = resp.json()["job_id"]
        events = _drain(client, job_id)

    types = [e["type"] for e in events]
    assert types == ["error", "done"]


# -----------------------------------------------------------------------
# Direct coverage of _watch_vision_queue_and_finish (the recovery loop that
# actually lets a completed vision queue file get picked back up; see the
# module docstring's EXTRACT_FROM_SCREENSHOT section). The tests above all
# use _StubVisionProvider, which is never a ManualProvider, so the service's
# isinstance(vision_provider, ManualProvider) guard never triggers this loop
# in an end-to-end run; these tests exercise the loop directly instead, with
# no server involved. No em dashes (house rule).
# -----------------------------------------------------------------------

import asyncio
import json
import time as time_module

from detective.llm import ManualProvider
from detective.service import _vision_result_is_empty, _watch_vision_queue_and_finish

_WATCHED_RESULT = {
    "profile_url": "https://www.linkedin.com/in/watched/",
    "name": None,
    "headline": None,
    "company": None,
}
_EMPTY_RESULT = {"profile_url": None, "name": None, "headline": None, "company": None}


def _write_vision_job(provider: ManualProvider, status: str, result: dict) -> None:
    provider._vision_job_path().write_text(
        json.dumps({"job_id": provider.job_id, "status": status, "result": result}),
        encoding="utf-8",
    )


def test_vision_result_is_empty_true_for_all_none_fields():
    assert _vision_result_is_empty(dict(_EMPTY_RESULT))
    assert _vision_result_is_empty({})


def test_vision_result_is_empty_false_when_any_field_is_set():
    assert not _vision_result_is_empty(dict(_WATCHED_RESULT))


def test_watch_vision_queue_returns_immediately_when_already_completed(tmp_path):
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_watch_1")
    _write_vision_job(provider, "completed", _WATCHED_RESULT)

    result = asyncio.run(_watch_vision_queue_and_finish(provider, lambda _msg: None, timeout_s=5.0))
    assert result == _WATCHED_RESULT


def test_watch_vision_queue_returns_none_on_timeout_when_never_completed(tmp_path):
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_watch_2")
    # Only the "pending" file ManualProvider.vision_extract itself would
    # already have written, never completed.
    _write_vision_job(provider, "pending", {})

    started = time_module.monotonic()
    result = asyncio.run(_watch_vision_queue_and_finish(provider, lambda _msg: None, timeout_s=0.1))
    elapsed = time_module.monotonic() - started

    assert result is None
    assert elapsed < 2.0  # bounded, not a hang past the poll interval


def test_watch_vision_queue_returns_completed_dict_even_when_all_fields_none(tmp_path):
    # A completed-but-genuinely-empty result (an operator looked and saw
    # nothing) must still come back as a dict, not None, so the caller falls
    # through to its normal "nothing extracted" error path rather than
    # treating a real completion the same as a timeout.
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_watch_3")
    _write_vision_job(provider, "completed", _EMPTY_RESULT)

    result = asyncio.run(_watch_vision_queue_and_finish(provider, lambda _msg: None, timeout_s=5.0))
    assert result == _EMPTY_RESULT
    assert result is not None


def test_watch_vision_queue_picks_up_completion_that_happens_mid_poll(tmp_path):
    # The realistic case this fix targets: nothing usable on disk when the
    # watch starts (mirrors vision_extract's immediate empty return under
    # MANUAL_QUEUE_TIMEOUT_S=0), and the file is completed a little while
    # into the poll loop by another actor (here, a concurrent asyncio task
    # standing in for the human/Claude Code operator).
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_watch_4")
    _write_vision_job(provider, "pending", {})

    async def _complete_after_delay() -> None:
        await asyncio.sleep(0.3)
        _write_vision_job(provider, "completed", _WATCHED_RESULT)

    async def _run():
        watch_task = asyncio.create_task(
            _watch_vision_queue_and_finish(provider, lambda _msg: None, timeout_s=5.0)
        )
        complete_task = asyncio.create_task(_complete_after_delay())
        result, _ = await asyncio.gather(watch_task, complete_task)
        return result

    result = asyncio.run(_run())
    assert result == _WATCHED_RESULT
