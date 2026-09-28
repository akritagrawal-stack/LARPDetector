import json
from types import SimpleNamespace

from detective import llm
from detective.llm import ClaudeProvider


def _fake_run_with(envelope, seen):
    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["prompt"] = kwargs["input"]
        seen["cwd"] = kwargs["cwd"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

    return fake_run


def test_claude_plan_is_tool_less_and_reads_structured_output(monkeypatch):
    seen = {}
    envelope = {
        "is_error": False,
        "structured_output": {
            "followups": [
                {
                    "claim_index": 0,
                    "query": '"Jane Doe" "Acme" CTO',
                    "rationale": "Checks the thin executive role.",
                    "kind": "web",
                }
            ]
        },
    }
    monkeypatch.setattr(llm.subprocess, "run", _fake_run_with(envelope, seen))
    claims = [
        llm.Claim(
            type="employment",
            employer="Acme",
            title="CTO",
            assertion="Worked as CTO at Acme",
        )
    ]

    out = ClaudeProvider(cli_path="/fake/claude").plan_followups(claims)

    command = seen["command"]
    assert out[0].claim_index == 0
    assert "-p" in command
    assert "--safe-mode" in command
    assert "--no-session-persistence" in command
    assert command[command.index("--tools") + 1] == ""
    assert "followups" in json.loads(command[command.index("--json-schema") + 1])["properties"]
    assert "larp-claude-" in seen["cwd"]
    assert "untrusted data" in seen["prompt"]


def test_claude_vision_only_gets_read_tool(monkeypatch):
    seen = {}
    envelope = {
        "is_error": False,
        "structured_output": {
            "profile_url": "https://www.linkedin.com/in/jane/",
            "name": "Jane Doe",
            "headline": None,
            "company": None,
        },
    }
    monkeypatch.setattr(llm.subprocess, "run", _fake_run_with(envelope, seen))

    out = ClaudeProvider(cli_path="/fake/claude").vision_extract("aGVsbG8=")

    assert out["name"] == "Jane Doe"
    assert seen["command"][seen["command"].index("--tools") + 1] == "Read"
    assert "screen.png" in seen["prompt"]


def test_claude_error_envelope_raises(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        llm.subprocess, "run", _fake_run_with({"is_error": True, "result": "x"}, seen)
    )
    try:
        ClaudeProvider(cli_path="/fake/claude").plan_followups([])
    except llm.ApiProviderError:
        return
    raise AssertionError("expected ApiProviderError")
