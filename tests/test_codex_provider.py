import json
from pathlib import Path
from types import SimpleNamespace

from detective import llm
from detective.llm import CodexProvider
from detective.models import Claim, Dossier, EvidenceTier


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
