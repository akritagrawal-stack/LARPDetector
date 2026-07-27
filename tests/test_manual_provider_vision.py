"""Tests for ManualProvider.vision_extract (detective/llm.py's queue-file
based screenshot reading, the "Go" button's ManualProvider fallback path).

Offline only: no network, no real screenshot decoding beyond base64, and the
queue file lives in a tmp_path directory (never the repo's own queue/), so
this suite never touches or depends on a real operator. No em dashes (house
rule).
"""

from __future__ import annotations

import base64
import json

from detective.llm import ManualProvider

_FAKE_PNG_B64 = base64.b64encode(b"not a real png, just test bytes").decode("ascii")


def test_vision_extract_writes_job_and_screenshot_then_returns_empty_by_default(tmp_path):
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_vision_test1")

    result = provider.vision_extract(_FAKE_PNG_B64)

    # MANUAL_QUEUE_TIMEOUT_S defaults to 0: non-blocking, returns an empty
    # (all-None) result immediately, mirroring assign_tiers_and_verdict.
    assert result == {"profile_url": None, "name": None, "headline": None, "company": None}

    job_path = tmp_path / "job_vision_test1_vision.json"
    assert job_path.exists()
    data = json.loads(job_path.read_text(encoding="utf-8"))
    assert data["status"] == "pending"
    assert data["kind"] == "vision_extract"
    assert data["job_id"] == "job_vision_test1"
    assert "instructions" in data and data["instructions"]
    assert data["result"] == {"profile_url": None, "name": None, "headline": None, "company": None}

    screenshot_path = tmp_path / "job_vision_test1_screenshot.png"
    assert screenshot_path.exists()
    assert screenshot_path.read_bytes() == base64.b64decode(_FAKE_PNG_B64)
    assert data["screenshot_path"] == str(screenshot_path)


def test_vision_extract_reads_back_a_completed_job(tmp_path):
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_vision_test2")

    # First call queues the job (pending).
    provider.vision_extract(_FAKE_PNG_B64)

    # Simulate the operator (human or Claude Code) filling in the file.
    job_path = tmp_path / "job_vision_test2_vision.json"
    data = json.loads(job_path.read_text(encoding="utf-8"))
    data["status"] = "completed"
    data["result"] = {
        "profile_url": "https://www.linkedin.com/in/janedoe/",
        "name": "Jane Doe",
        "headline": "Engineer",
        "company": "Acme Corp",
    }
    job_path.write_text(json.dumps(data), encoding="utf-8")

    # Idempotent: a fresh call for the same job_id reads the completed
    # result back, same discipline as assign_tiers_and_verdict.
    result = provider.vision_extract(_FAKE_PNG_B64)
    assert result == {
        "profile_url": "https://www.linkedin.com/in/janedoe/",
        "name": "Jane Doe",
        "headline": "Engineer",
        "company": "Acme Corp",
    }


def test_vision_extract_never_collides_with_the_scoring_job_file(tmp_path):
    """The vision job and the scoring job for the SAME job_id must live in
    two separate files (queue/<job_id>_vision.json vs queue/<job_id>.json),
    never overwrite each other.
    """
    provider = ManualProvider(queue_dir=tmp_path, job_id="job_vision_test3")
    provider.vision_extract(_FAKE_PNG_B64)

    assert (tmp_path / "job_vision_test3_vision.json").exists()
    assert not (tmp_path / "job_vision_test3.json").exists()
