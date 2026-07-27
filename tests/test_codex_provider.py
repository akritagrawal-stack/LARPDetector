import json
from pathlib import Path
from types import SimpleNamespace

from detective import llm
from detective.llm import CodexProvider
from detective.models import (
    Buildability,
    Claim,
    CompanyAssessment,
    Dossier,
    EvidenceTier,
    MetricEntry,
)


def _fake_run_with(payload, seen):
    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["prompt"] = kwargs["input"]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_codex_plan_is_ephemeral_read_only_and_schema_constrained(monkeypatch):
    seen = {}
    payload = {
        "followups": [
            {
                "claim_index": 0,
                "query": '"Jane Doe" "Acme" CTO',
                "rationale": "Checks the thin executive role.",
                "kind": "web",
            }
        ]
    }
    monkeypatch.setattr(llm.subprocess, "run", _fake_run_with(payload, seen))
    provider = CodexProvider(cli_path="/fake/codex")
    claims = [
        Claim(
            type="employment",
            employer="Acme",
            title="CTO",
            assertion="Worked as CTO at Acme",
        )
    ]

    out = provider.plan_followups(claims, {"name": "Jane Doe"})

    assert out[0].claim_index == 0
    assert "--ephemeral" in seen["command"]
    assert "--ignore-user-config" in seen["command"]
    assert "--ignore-rules" in seen["command"]
    assert seen["command"][seen["command"].index("--sandbox") + 1] == "read-only"
    assert seen["command"].index("--ask-for-approval") < seen["command"].index("exec")
    assert "untrusted data" in seen["prompt"]


def test_codex_scoring_uses_existing_safety_and_deterministic_score(monkeypatch):
    seen = {}
    payload = {
        "claims": [
            {
                "index": 0,
                "tier": "UNVERIFIED",
                "expected_footprint": "high",
                "notes": "Only a completed empty search was available.",
            }
        ],
        "verdict": "The public role has no independent receipts.",
    }
    monkeypatch.setattr(llm.subprocess, "run", _fake_run_with(payload, seen))
    claim = Claim(
        type="employment",
        employer="Acme",
        title="CTO",
        assertion="Worked as CTO at Acme",
        evidence=[
            {
                "source_url": "internal://searched",
                "snippet": "Search completed with no results.",
                "source_name": "searched_no_results",
                "weight": 0.0,
                "match_confidence": "low",
            }
        ],
    )
    dossier = Dossier(
        profile_url="https://example.com/jane",
        identity={"name": "Jane Doe"},
        claims=[claim],
        scan_type="person",
    )

    out = CodexProvider(cli_path="/fake/codex").assign_tiers_and_verdict(dossier)

    assert out.claims[0].tier is EvidenceTier.UNVERIFIED
    assert out.claims[0].expected_footprint == "high"
    assert isinstance(out.larp_score, int)
    assert out.verdict == "The public role has no independent receipts."


def test_codex_person_scan_scores_company_assessment_in_same_reasoning_pass(
    monkeypatch,
):
    seen = {}
    payload = {
        "claims": [
            {
                "index": 0,
                "tier": "CONFIRMED",
                "expected_footprint": "high",
                "notes": "Independent role evidence exists.",
            }
        ],
        "company_assessments": [
            {
                "company_name": "Acme",
                "buildability": {"tier": "MODERATE", "note": "Real integration."},
                "metric_breakdown": [
                    {
                        "name": "product_realness",
                        "score_0_10": 2,
                        "note": "Live product.",
                    },
                    {
                        "name": "zombie_liveness",
                        "score_0_10": 1,
                        "note": "Recently active.",
                    },
                ],
            }
        ],
        "verdict": "The role and product have independent receipts.",
    }
    monkeypatch.setattr(llm.subprocess, "run", _fake_run_with(payload, seen))
    dossier = Dossier(
        profile_url="https://example.test/jane",
        claims=[
            Claim(
                type="employment",
                employer="Acme",
                title="Founder",
                assertion="Founder at Acme",
            )
        ],
        company_assessments=[
            CompanyAssessment(
                company_name="Acme",
                claim_indices=[0],
                relationship="founder",
                affects_overall=True,
                buildability=Buildability(),
                metric_breakdown=[
                    MetricEntry(
                        name="product_realness", weight=3, active=True
                    ),
                    MetricEntry(
                        name="zombie_liveness", weight=2, active=True
                    ),
                    MetricEntry(name="buildability", weight=1, active=True),
                ],
            )
        ],
    )

    out = CodexProvider(cli_path="/fake/codex").assign_tiers_and_verdict(dossier)

    assert "company_assessments" in seen["prompt"]
    assert out.company_assessments[0].buildability.tier == "MODERATE"
    assert out.company_assessments[0].metric_breakdown[0].score_0_10 == 2
