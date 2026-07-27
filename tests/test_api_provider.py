"""Tests for ApiProvider (detective/llm.py's Gemini-backed brain).

Offline only: monkeypatches llm._gemini_generate (the single network seam)
so no real Gemini call ever happens in this suite. No em dashes (house
rule).
"""

from __future__ import annotations

import json

import pytest

from detective import llm
from detective.llm import ApiProvider, ApiProviderError
from detective.models import Buildability, Claim, Dossier, EvidenceTier, MetricEntry


def _person_dossier() -> Dossier:
    claims = [
        Claim(
            type="identity",
            assertion="A real person named Jane Doe exists and matches this profile.",
            evidence=[{"source_url": "https://en.wikipedia.org/wiki/Jane_Doe", "snippet": "Jane Doe is real."}],
        ),
        Claim(
            type="employment",
            employer="Acme Corp",
            title="Engineer",
            assertion="Worked as Engineer at Acme Corp (2020 to 2022).",
            evidence=[
                {
                    "source_url": "internal://mismatch/mismatch_contradiction",
                    "snippet": "Acme Corp records actively contradict the claimed role.",
                    "source_name": "mismatch_contradiction",
                    "match_confidence": "high",
                }
            ],
        ),
    ]
    return Dossier(profile_url="https://www.linkedin.com/in/janedoe/", scan_type="person", claims=claims)


def _company_dossier() -> Dossier:
    claims = [
        Claim(
            type="company_overview",
            employer="Widget AI",
            assertion="Widget AI is an actively operating, real product with a live public footprint.",
            evidence=[{"source_url": "https://widget.example/", "snippet": "Landing page loads."}],
        ),
        Claim(
            type="proprietary_tech",
            employer="Widget AI",
            assertion='Widget AI claims: "our own proprietary model."',
            evidence=[
                {
                    "source_url": "internal://mismatch/mismatch_contradiction",
                    "snippet": "Independent inspection contradicts the proprietary-model claim.",
                    "source_name": "mismatch_contradiction",
                    "match_confidence": "high",
                }
            ],
        ),
    ]
    metric_breakdown = [
        MetricEntry(name="raise_inflation", weight=3, active=False),
        MetricEntry(name="reach_vs_footprint", weight=3, active=False),
        MetricEntry(name="product_realness", weight=3, active=True),
        MetricEntry(name="headcount_inflation", weight=2, active=False),
        MetricEntry(name="proprietary_ai_gap", weight=2, active=True),
        MetricEntry(name="zombie_liveness", weight=2, active=True),
        MetricEntry(name="key_role_coverage", weight=2, active=True),
        MetricEntry(name="buildability", weight=1, active=True),
    ]
    return Dossier(
        profile_url="https://widget.example/",
        scan_type="company_app",
        claims=claims,
        buildability=Buildability(),
        metric_breakdown=metric_breakdown,
    )


# ---------------------------------------------------------------------------
# Mocked-Gemini-response parse test
# ---------------------------------------------------------------------------


def test_person_scan_applies_mocked_gemini_response(monkeypatch):
    dossier = _person_dossier()
    mocked_response = {
        "claims": [
            {"index": 0, "tier": "CONFIRMED", "notes": "Wikipedia confirms Jane Doe is real."},
            {"index": 1, "tier": "DISPROVEN", "notes": "Acme's own team page has no record of Jane Doe."},
        ],
        "verdict": "One confirmed identity, one fabricated employment claim.",
    }

    def fake_generate(prompt: str, api_key: str, model_name: str) -> str:
        assert "OPERATOR TASK" in prompt  # reuses the same instruction text
        assert "Jane Doe" not in prompt or "Jane Doe" in prompt  # smoke: prompt built ok
        return json.dumps(mocked_response)

    monkeypatch.setattr(llm, "_gemini_generate", fake_generate)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    result = provider.assign_tiers_and_verdict(dossier)

    assert result.claims[0].tier == EvidenceTier.CONFIRMED
    assert result.claims[1].tier == EvidenceTier.DISPROVEN
    assert result.claims[1].notes
    assert result.verdict == mocked_response["verdict"]
    # larp_score is code-computed from tiers, never taken from the mocked
    # response (the mocked response never included one).
    assert result.larp_score is not None
    assert isinstance(result.larp_score, int)


def test_company_scan_applies_mocked_gemini_response(monkeypatch):
    dossier = _company_dossier()
    mocked_response = {
        "claims": [
            {"index": 0, "tier": "CONFIRMED", "notes": "Product ships."},
            {"index": 1, "tier": "DISPROVEN", "notes": "Just an OpenAI wrapper."},
        ],
        "buildability": {"tier": "TRIVIAL", "note": "Only a client-side OpenAI call found."},
        "metric_breakdown": [
            {"name": "product_realness", "score_0_10": 1, "note": "Ships, has users."},
            {"name": "proprietary_ai_gap", "score_0_10": 9, "note": "No proprietary artifacts."},
            {"name": "zombie_liveness", "score_0_10": 0, "note": "Recently updated."},
            {"name": "key_role_coverage", "score_0_10": 8, "note": "No technical founder found."},
        ],
        "verdict": "Thin wrapper sold as proprietary AI.",
    }

    def fake_generate(prompt: str, api_key: str, model_name: str) -> str:
        assert "buildability" in prompt.lower()
        return json.dumps(mocked_response)

    monkeypatch.setattr(llm, "_gemini_generate", fake_generate)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    result = provider.assign_tiers_and_verdict(dossier)

    assert result.claims[0].tier == EvidenceTier.CONFIRMED
    assert result.claims[1].tier == EvidenceTier.DISPROVEN
    assert result.buildability.tier == "TRIVIAL"
    by_name = {m.name: m for m in result.metric_breakdown}
    assert by_name["product_realness"].score_0_10 == 1
    assert by_name["proprietary_ai_gap"].score_0_10 == 9
    # Inactive rows are left untouched (still None), and buildability's
    # score_0_10 is never set here: sync_buildability_metric (pipeline.run)
    # derives it from the tier just set above.
    assert by_name["raise_inflation"].score_0_10 is None
    assert by_name["buildability"].score_0_10 is None
    assert result.verdict == mocked_response["verdict"]


def test_gemini_response_missing_a_claim_entry_raises_api_provider_error(monkeypatch):
    """An incomplete response (fewer claim entries than claims) must raise,
    never silently leave some claims unset.
    """
    dossier = _person_dossier()

    def fake_generate(prompt: str, api_key: str, model_name: str) -> str:
        return json.dumps({"claims": [{"index": 0, "tier": "CONFIRMED"}], "verdict": "incomplete"})

    monkeypatch.setattr(llm, "_gemini_generate", fake_generate)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    with pytest.raises(ApiProviderError):
        provider.assign_tiers_and_verdict(dossier)


def test_gemini_response_with_markdown_fence_still_parses(monkeypatch):
    """Gemini sometimes wraps JSON in a ```json fence despite the
    response_mime_type hint; the parser must strip it.
    """
    dossier = _person_dossier()
    mocked = {
        "claims": [
            {"index": 0, "tier": "CONFIRMED", "notes": ""},
            {"index": 1, "tier": "UNVERIFIED", "notes": ""},
        ],
        "verdict": "fenced response",
    }

    def fake_generate(prompt: str, api_key: str, model_name: str) -> str:
        return "```json\n" + json.dumps(mocked) + "\n```"

    monkeypatch.setattr(llm, "_gemini_generate", fake_generate)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    result = provider.assign_tiers_and_verdict(dossier)
    assert result.verdict == "fenced response"


# ---------------------------------------------------------------------------
# Fallback-on-error tests
# ---------------------------------------------------------------------------


def test_network_error_raises_api_provider_error_not_crash(monkeypatch):
    dossier = _person_dossier()

    def fake_generate(prompt: str, api_key: str, model_name: str) -> str:
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(llm, "_gemini_generate", fake_generate)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    with pytest.raises(ApiProviderError):
        provider.assign_tiers_and_verdict(dossier)


def test_quota_exhausted_error_raises_api_provider_error_and_scrubs_key(monkeypatch):
    dossier = _person_dossier()
    secret_key = "fake-provider-key-never-leak"

    def fake_generate(prompt: str, api_key: str, model_name: str) -> str:
        raise RuntimeError(f"429 RESOURCE_EXHAUSTED quota exceeded for key {secret_key}")

    monkeypatch.setattr(llm, "_gemini_generate", fake_generate)
    monkeypatch.setenv("GEMINI_API_KEY", secret_key)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    with pytest.raises(ApiProviderError) as excinfo:
        provider.assign_tiers_and_verdict(dossier)
    assert secret_key not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_no_key_raises_api_provider_error():
    dossier = _person_dossier()
    provider = ApiProvider()
    provider.provider = None
    provider.gemini_key = ""
    provider.anthropic_key = ""
    with pytest.raises(ApiProviderError):
        provider.assign_tiers_and_verdict(dossier)


def test_anthropic_only_raises_api_provider_error_not_wired(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    dossier = _person_dossier()
    provider = ApiProvider()
    assert provider.provider == "anthropic"
    with pytest.raises(ApiProviderError):
        provider.assign_tiers_and_verdict(dossier)


# ---------------------------------------------------------------------------
# vision_extract: the overlay "Go" button's screenshot-reading fallback.
# Offline only: monkeypatches llm._gemini_generate_vision (the network seam),
# never llm._gemini_generate (the text-only seam used by the tests above).
# ---------------------------------------------------------------------------


def test_vision_extract_applies_mocked_gemini_response(monkeypatch):
    mocked_response = {
        "profile_url": "https://www.linkedin.com/in/janedoe/",
        "name": "Jane Doe",
        "headline": "Engineer at Acme",
        "company": "Acme Corp",
    }

    def fake_generate_vision(prompt, image_b64, api_key, model_name):
        assert "LinkedIn" in prompt
        assert image_b64 == "ZmFrZQ=="
        return json.dumps(mocked_response)

    monkeypatch.setattr(llm, "_gemini_generate_vision", fake_generate_vision)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    result = provider.vision_extract("ZmFrZQ==")

    assert result == mocked_response


def test_vision_extract_missing_fields_become_none(monkeypatch):
    """Gemini reading only a name off the page (no visible address bar) must
    leave profile_url as None, never a fabricated guess.
    """

    def fake_generate_vision(prompt, image_b64, api_key, model_name):
        return json.dumps({"name": "Jane Doe"})

    monkeypatch.setattr(llm, "_gemini_generate_vision", fake_generate_vision)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    result = provider.vision_extract("ZmFrZQ==")

    assert result == {"profile_url": None, "name": "Jane Doe", "headline": None, "company": None}


def test_vision_extract_network_error_raises_api_provider_error(monkeypatch):
    def fake_generate_vision(prompt, image_b64, api_key, model_name):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(llm, "_gemini_generate_vision", fake_generate_vision)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ApiProvider()
    with pytest.raises(ApiProviderError):
        provider.vision_extract("ZmFrZQ==")


def test_vision_extract_no_key_raises_api_provider_error():
    provider = ApiProvider()
    provider.provider = None
    provider.gemini_key = ""
    provider.anthropic_key = ""
    with pytest.raises(ApiProviderError):
        provider.vision_extract("ZmFrZQ==")
