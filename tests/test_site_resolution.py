"""Offline tests for the product-site RESOLUTION step (the reasoning call).

Which candidate site IS the claimed product is a judgment, not a string match:
"Cognition" hits thousands of sites and a wrong-site match is the defamation
path (it must never confirm AND never condemn). So resolution is a provider
method, alongside decompose / assign_tiers / plan_followups / vision_extract.

Covered here:
  - SiteResolution.from_dict is defensive (operator-edited JSON, model JSON)
  - the base provider is a pure no-op, so the stage is backwards compatible
  - ManualProvider does the queue-file round trip on its OWN file
  - ApiProvider ACTUALLY IMPLEMENTS IT. plan_followups is silently missing on
    ApiProvider today, which makes the director pass a no-op in API mode; this
    suite exists partly so the same bug cannot be repeated here.

No network, no API keys: _gemini_generate is monkeypatched. Synthetic names.
No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import json
import os

import pytest

from detective import llm as llm_module
from detective.llm import ApiProvider, LLMProvider, ManualProvider, SiteResolution


def _request(claim_index=0, product="Acme Widgets"):
    return {
        "claim_index": claim_index,
        "product_name": product,
        "role_text": "Founder at Acme Widgets",
        "context": ["Shipped v2 of Acme Widgets today"],
        "candidates": [
            {
                "url": "https://acmewidgets.example",
                "title": "Acme Widgets",
                "description": "Inventory software for small shops",
                "status": 200,
                "parked": False,
                "source": "post_link",
            }
        ],
    }


# ---------------------------------------------------------------------------
# SiteResolution
# ---------------------------------------------------------------------------


def test_site_resolution_round_trips():
    r = SiteResolution(
        claim_index=2, url="https://acmewidgets.example", confidence="high",
        outcome="resolved", rationale="the founder's own post links it",
    )
    assert SiteResolution.from_dict(r.to_dict()) == r


def test_site_resolution_from_dict_is_defensive():
    r = SiteResolution.from_dict({"claim_index": "not a number"})
    assert r.claim_index == -1  # skippable sentinel, never an exception
    assert r.outcome == "ambiguous"
    assert r.url == ""


def test_resolved_without_a_url_degrades_to_ambiguous():
    # A model that says "resolved" but names no site has not resolved anything.
    r = SiteResolution.from_dict({"claim_index": 0, "outcome": "resolved", "confidence": "high"})
    assert r.outcome == "ambiguous"


def test_resolved_with_low_confidence_degrades_to_ambiguous():
    # THE defamation guard. Low-confidence resolution stays UNVERIFIED: it must
    # never confirm the product and never condemn the person. Ambiguity is not
    # absence, so it contributes zero rather than becoming a SUS input.
    r = SiteResolution.from_dict(
        {"claim_index": 0, "outcome": "resolved", "confidence": "low", "url": "https://x.example"}
    )
    assert r.outcome == "ambiguous"


def test_unknown_outcome_degrades_to_ambiguous():
    r = SiteResolution.from_dict({"claim_index": 0, "outcome": "banana", "url": "https://x.example"})
    assert r.outcome == "ambiguous"


def test_not_found_survives_without_a_url():
    # "We looked properly and nothing credible exists" is a legitimate outcome
    # and carries no URL by definition.
    r = SiteResolution.from_dict({"claim_index": 1, "outcome": "not_found"})
    assert r.outcome == "not_found"
    assert r.claim_index == 1


# ---------------------------------------------------------------------------
# Base provider: pure no-op
# ---------------------------------------------------------------------------


def test_base_provider_resolves_nothing():
    assert LLMProvider().resolve_product_site([_request()], {}) == []


# ---------------------------------------------------------------------------
# ManualProvider queue round trip
# ---------------------------------------------------------------------------


def test_manual_provider_writes_its_own_resolve_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_QUEUE_TIMEOUT_S", "0")
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_test")
    out = provider.resolve_product_site([_request()], {"name": "Jane Doe"})

    # Non-blocking: a scan NEVER hangs waiting on the operator.
    assert out == []
    path = tmp_path / "job_test_resolve.json"
    assert path.exists()
    # Its OWN file: never collides with the scoring job or the plan job.
    assert not (tmp_path / "job_test.json").exists()
    assert not (tmp_path / "job_test_plan.json").exists()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "resolve"
    assert data["status"] == "pending"
    assert data["identity"] == {"name": "Jane Doe"}
    assert data["requests"][0]["product_name"] == "Acme Widgets"
    assert data["requests"][0]["candidates"][0]["url"] == "https://acmewidgets.example"
    assert data["result"] == {"resolutions": []}


def test_manual_provider_reads_back_a_completed_resolve_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_QUEUE_TIMEOUT_S", "0")
    path = tmp_path / "job_test_resolve.json"
    path.write_text(
        json.dumps(
            {
                "job_id": "job_test",
                "status": "completed",
                "kind": "resolve",
                "result": {
                    "resolutions": [
                        {
                            "claim_index": 0,
                            "url": "https://acmewidgets.example",
                            "confidence": "high",
                            "outcome": "resolved",
                            "rationale": "the profile's own contact link",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_test")
    out = provider.resolve_product_site([_request()], {})
    assert len(out) == 1
    assert out[0].url == "https://acmewidgets.example"
    assert out[0].outcome == "resolved"


def test_manual_provider_ignores_a_pending_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_QUEUE_TIMEOUT_S", "0")
    path = tmp_path / "job_test_resolve.json"
    path.write_text(json.dumps({"status": "pending", "result": {"resolutions": [{"claim_index": 0}]}}), encoding="utf-8")
    assert ManualProvider(queue_dir=tmp_path, job_id="job_test").resolve_product_site([_request()], {}) == []


def test_manual_provider_never_raises_on_a_corrupt_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_QUEUE_TIMEOUT_S", "0")
    (tmp_path / "job_test_resolve.json").write_text("{not json", encoding="utf-8")
    assert ManualProvider(queue_dir=tmp_path, job_id="job_test").resolve_product_site([_request()], {}) == []


def test_manual_provider_skips_the_job_entirely_when_nothing_to_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_QUEUE_TIMEOUT_S", "0")
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_test")
    assert provider.resolve_product_site([], {}) == []
    assert not (tmp_path / "job_test_resolve.json").exists()


def test_resolve_instructions_forbid_guessing():
    # The operator/model text is load-bearing: an unsure pick is the wrong-site
    # match that gets a real person accused.
    text = llm_module._RESOLVE_INSTRUCTIONS.lower()
    assert "ambiguous" in text
    assert "guess" in text
    # And it must say plainly that a resolved site is not the role claim.
    assert "role" in text


# ---------------------------------------------------------------------------
# ApiProvider parity: the feature must actually exist in API mode
# ---------------------------------------------------------------------------


def test_api_provider_overrides_the_base_no_op():
    assert ApiProvider.resolve_product_site is not LLMProvider.resolve_product_site


def test_api_provider_resolves_via_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    captured = {}

    def fake_generate(prompt, key, model):
        captured["prompt"] = prompt
        return json.dumps(
            {
                "resolutions": [
                    {
                        "claim_index": 0,
                        "url": "https://acmewidgets.example",
                        "confidence": "high",
                        "outcome": "resolved",
                        "rationale": "title matches the claimed product and the post links it",
                    }
                ]
            }
        )

    monkeypatch.setattr(llm_module, "_gemini_generate", fake_generate)
    out = ApiProvider().resolve_product_site([_request()], {"name": "Jane Doe"})

    assert len(out) == 1
    assert out[0].outcome == "resolved"
    assert out[0].url == "https://acmewidgets.example"
    # The candidates and the claimed product must actually reach the model, or
    # it is picking blind.
    assert "Acme Widgets" in captured["prompt"]
    assert "acmewidgets.example" in captured["prompt"]


def test_api_provider_with_nothing_to_resolve_makes_no_call(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*a, **k):
        raise AssertionError("no API call should be made for an empty request list")

    monkeypatch.setattr(llm_module, "_gemini_generate", boom)
    assert ApiProvider().resolve_product_site([], {}) == []


# ---------------------------------------------------------------------------
# The service-level bounded wait.
#
# .env sets MANUAL_QUEUE_TIMEOUT_S=1200 for the live engine. Stage 1.5 runs in
# FRONT of the whole evidence gather, so inheriting that would stall a live scan
# for up to 20 minutes before it gathered anything, with nothing on screen.
# ---------------------------------------------------------------------------


def test_a_long_global_queue_timeout_never_stalls_the_resolve_stage(tmp_path, monkeypatch):
    from detective import service

    monkeypatch.setenv("MANUAL_QUEUE_TIMEOUT_S", "1200")
    monkeypatch.setattr(service, "_QUEUE_POLL_INTERVAL_S", 0.01, raising=False)

    provider = service._PlanWaitingManualProvider(
        queue_dir=tmp_path, job_id="job_test", plan_timeout_s=0.0, resolve_timeout_s=0.05
    )
    out = provider.resolve_product_site([_request()], {})

    # Bounded by resolve_timeout_s (0.05s here), NOT by the 1200s global.
    assert out == []
    # The job was still WRITTEN, so an operator can fill it and a re-run uses it.
    assert (tmp_path / "job_test_resolve.json").exists()


def test_the_service_provider_uses_a_completed_resolve_job(tmp_path, monkeypatch):
    from detective import service

    monkeypatch.setenv("MANUAL_QUEUE_TIMEOUT_S", "1200")
    (tmp_path / "job_test_resolve.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "kind": "resolve",
                "result": {
                    "resolutions": [
                        {
                            "claim_index": 0,
                            "url": "https://acmewidgets.example",
                            "confidence": "high",
                            "outcome": "resolved",
                            "rationale": "operator filled it",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    provider = service._PlanWaitingManualProvider(
        queue_dir=tmp_path, job_id="job_test", plan_timeout_s=0.0, resolve_timeout_s=30.0
    )
    out = provider.resolve_product_site([_request()], {})
    assert len(out) == 1 and out[0].outcome == "resolved"


def test_api_provider_unparseable_response_raises_api_provider_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_module, "_gemini_generate", lambda *a, **k: "not json at all")
    with pytest.raises(llm_module.ApiProviderError):
        ApiProvider().resolve_product_site([_request()], {})
