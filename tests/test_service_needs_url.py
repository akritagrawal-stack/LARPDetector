"""Tests for the needs_url honesty states in service.py.

Covers the identity-confirmation gate (a name-resolved URL whose scraped name
does not match what the operator was viewing must NOT score, it must ask for
the exact URL), the "could not read a profile" path routing to needs_url
instead of a dead error card, and the name-match truth table.

Offline only: monkeypatches pipeline.run and the provider so no live fetch or
real web search happens. No em dashes (house rule).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import detective.pipeline as pipeline_module
import detective.service as service_module
from detective.models import Claim, Dossier, EvidenceTier
from detective.service import app, _names_plausibly_match

_MAX_EVENTS = 500
_FAKE_SCREENSHOT_B64 = "ZmFrZQ=="


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


class _StubVisionProvider:
    def __init__(self, vision_result: dict):
        self._vision_result = vision_result

    def vision_extract(self, screenshot_b64: str) -> dict:
        return dict(self._vision_result)


def _scored_dossier(url: str, name: str) -> Dossier:
    d = Dossier(profile_url=url, scan_type="person", identity={"name": name})
    d.claims = [Claim(type="identity", assertion=f"{name} exists.", tier=EvidenceTier.CONFIRMED)]
    d.larp_score = 3
    d.founder_larp_score = 3
    d.verdict = "test verdict"
    return d


# ---------------------------------------------------------------------------
# name-match truth table
# ---------------------------------------------------------------------------


def test_names_match_exact():
    assert _names_plausibly_match("John Smith", "John Smith") is True


def test_names_match_reordered():
    assert _names_plausibly_match("John Smith", "Smith John") is True


def test_names_match_middle_name_superset():
    assert _names_plausibly_match("John Smith", "John Michael Smith") is True


def test_names_match_punctuation_and_case():
    assert _names_plausibly_match("john  smith", "John Smith.") is True


def test_names_no_match_different_person():
    assert _names_plausibly_match("John Smith", "Jane Doe") is False


def test_names_match_inconclusive_when_empty():
    # Cannot assert a mismatch with no tokens: do not block the scan.
    assert _names_plausibly_match("", "John Smith") is True
    assert _names_plausibly_match("John Smith", "") is True


# ---------------------------------------------------------------------------
# identity gate on a name-resolved URL
# ---------------------------------------------------------------------------


def test_name_resolved_mismatch_emits_needs_url_not_verdict(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "_select_provider",
        lambda job_id, is_demo: _StubVisionProvider(
            {"profile_url": None, "name": "Jane Searchable", "headline": "Eng", "company": "Acme"}
        ),
    )
    monkeypatch.setattr(
        service_module.search,
        "web_search",
        lambda query, count=8: [
            {"title": "x", "url": "https://www.linkedin.com/in/someone-else/", "snippet": ""}
        ],
    )

    # pipeline.run "scrapes" a DIFFERENT person than the one on screen.
    def fake_run(url, provider=None, live=False, progress=None, raw_profile=None, scan_type="person", engine="per_claim"):
        return _scored_dossier(url, "Bob Different")

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "extract_from_screenshot": True, "scan_type": "person"},
        )
        events = _drain(client, resp.json()["job_id"])

    types = [e["type"] for e in events]
    assert "needs_url" in types
    assert "verdict" not in types
    assert "scores" not in types
    assert types[-1] == "done"


def test_name_resolved_match_proceeds_to_verdict(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "_select_provider",
        lambda job_id, is_demo: _StubVisionProvider(
            {"profile_url": None, "name": "Jane Searchable", "headline": "Eng", "company": "Acme"}
        ),
    )
    monkeypatch.setattr(
        service_module.search,
        "web_search",
        lambda query, count=8: [
            {"title": "x", "url": "https://www.linkedin.com/in/janesearchable/", "snippet": ""}
        ],
    )

    def fake_run(url, provider=None, live=False, progress=None, raw_profile=None, scan_type="person", engine="per_claim"):
        return _scored_dossier(url, "Jane Searchable")

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "extract_from_screenshot": True, "scan_type": "person"},
        )
        events = _drain(client, resp.json()["job_id"])

    types = [e["type"] for e in events]
    assert "needs_url" not in types
    assert types[-3:] == ["scores", "verdict", "done"]


def test_exact_vision_url_skips_identity_gate(monkeypatch):
    # A vision result that yields a profile_url is treated as exact (the
    # operator read/typed a real URL): the identity gate must NOT fire even if
    # the scraped name differs from a vision name field.
    monkeypatch.setattr(
        service_module,
        "_select_provider",
        lambda job_id, is_demo: _StubVisionProvider(
            {"profile_url": "https://www.linkedin.com/in/exact/", "name": "On Screen", "headline": None, "company": None}
        ),
    )

    def fake_run(url, provider=None, live=False, progress=None, raw_profile=None, scan_type="person", engine="per_claim"):
        return _scored_dossier(url, "Totally Different Scraped")

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "extract_from_screenshot": True, "scan_type": "person"},
        )
        events = _drain(client, resp.json()["job_id"])

    types = [e["type"] for e in events]
    assert "needs_url" not in types
    assert types[-3:] == ["scores", "verdict", "done"]


def test_nothing_extracted_emits_needs_url_not_error(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "_select_provider",
        lambda job_id, is_demo: _StubVisionProvider(
            {"profile_url": None, "name": None, "headline": None, "company": None}
        ),
    )

    def fake_run(*a, **k):
        raise AssertionError("pipeline.run must not run when nothing was extracted")

    monkeypatch.setattr(pipeline_module, "run", fake_run)

    with TestClient(app) as client:
        resp = client.post(
            "/scan",
            json={"screenshot_b64": _FAKE_SCREENSHOT_B64, "extract_from_screenshot": True, "scan_type": "person"},
        )
        events = _drain(client, resp.json()["job_id"])

    types = [e["type"] for e in events]
    assert "needs_url" in types
    assert "error" not in types
    assert types[-1] == "done"
