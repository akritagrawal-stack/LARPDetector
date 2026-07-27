"""Model round-trip tests. Offline, no network. No em dashes (house rule)."""

from __future__ import annotations

import json

from detective.models import Buildability, Claim, Dossier, EvidenceTier, MetricEntry


def test_claim_roundtrip_serializes_tier_to_string():
    c = Claim(
        type="employment",
        employer="Google",
        title="Engineer",
        start="Jan 2020",
        end="Present",
        assertion="Worked as Engineer at Google.",
        tier=EvidenceTier.CONFIRMED,
        evidence=[{"source_url": "https://x.test", "snippet": "hit"}],
        notes="looks real",
    )
    d = c.to_dict()
    # Tier must be a plain string in the dict, JSON-serializable.
    assert d["tier"] == "CONFIRMED"
    assert isinstance(d["tier"], str)
    json.dumps(d)  # must not raise

    back = Claim.from_dict(d)
    assert back == c
    assert back.tier is EvidenceTier.CONFIRMED


def test_claim_from_dict_tolerates_bad_tier():
    c = Claim.from_dict({"type": "employment", "tier": "nonsense"})
    assert c.tier is EvidenceTier.UNVERIFIED


def test_dossier_roundtrip():
    dossier = Dossier(
        profile_url="https://www.linkedin.com/in/x/",
        identity={"name": "Jane Doe", "headline": "CEO", "current_company": "Acme", "location": "NYC"},
        raw_experience=[{"title": "CEO", "company": "Acme"}],
        claims=[
            Claim(type="identity", assertion="Jane Doe exists.", tier=EvidenceTier.UNVERIFIED),
            Claim(type="employment", employer="Acme", tier=EvidenceTier.DISPROVEN),
        ],
        larp_score=72,
        verdict="Mostly vapor.",
        attempt_ledger=[
            {
                "sequence": 1,
                "stage": "evidence",
                "connector": "web_search",
                "status": "completed_empty",
            }
        ],
    )
    d = dossier.to_dict()
    s = json.dumps(d)  # full dossier must be JSON-serializable
    assert '"tier": "DISPROVEN"' in s

    back = Dossier.from_dict(json.loads(s))
    assert back.profile_url == dossier.profile_url
    assert back.larp_score == 72
    assert back.verdict == "Mostly vapor."
    assert back.attempt_ledger[0]["status"] == "completed_empty"
    assert len(back.claims) == 2
    assert back.claims[1].tier is EvidenceTier.DISPROVEN
    # generated_at survives the round trip.
    assert back.generated_at == dossier.generated_at


def test_dossier_defaults_unscored():
    dossier = Dossier(profile_url="https://x.test")
    assert dossier.larp_score is None
    assert dossier.verdict is None
    assert dossier.claims == []
    assert dossier.generated_at  # auto-populated ISO string


def test_dossier_defaults_person_scan_no_buildability():
    """Backward compatibility: a bare Dossier is a person scan with no
    buildability block, and old queue files with neither key still load.
    """
    dossier = Dossier(profile_url="https://x.test")
    assert dossier.scan_type == "person"
    assert dossier.buildability is None

    d = dossier.to_dict()
    assert d["scan_type"] == "person"
    assert d["buildability"] is None
    json.dumps(d)  # must not raise

    # Old queue files (pre-dating scan_type / buildability) have neither key.
    old_shape = dict(d)
    del old_shape["scan_type"]
    del old_shape["buildability"]
    back = Dossier.from_dict(old_shape)
    assert back.scan_type == "person"
    assert back.buildability is None


def test_dossier_company_scan_roundtrip():
    dossier = Dossier(
        profile_url="https://resumegenie.example/",
        scan_type="company_app",
        identity={"name": "ResumeGenie AI", "headline": "AI resume optimizer"},
        claims=[
            Claim(type="pricing", employer="ResumeGenie AI", assertion="$49/mo."),
            Claim(type="user_count", employer="ResumeGenie AI", assertion="100k users."),
        ],
        larp_score=81,
        verdict="Thin wrapper sold at a premium.",
        buildability=Buildability(
            tier="TRIVIAL",
            note="Evidence shows only an OpenAI API call, no proprietary model.",
        ),
    )
    d = dossier.to_dict()
    s = json.dumps(d)  # must be JSON-serializable
    assert '"tier": "TRIVIAL"' in s

    back = Dossier.from_dict(json.loads(s))
    assert back.scan_type == "company_app"
    assert back.larp_score == 81
    assert back.buildability is not None
    assert back.buildability.tier == "TRIVIAL"
    assert "OpenAI" in back.buildability.note
    assert len(back.claims) == 2
    assert back.claims[0].type == "pricing"
    assert back.claims[1].type == "user_count"


def test_metric_entry_roundtrip():
    m = MetricEntry(name="raise_inflation", weight=3, score_0_10=7, active=True, note="log gap on funding.")
    d = m.to_dict()
    json.dumps(d)  # must be JSON-serializable
    back = MetricEntry.from_dict(d)
    assert back == m


def test_metric_entry_from_dict_tolerates_missing_keys():
    m = MetricEntry.from_dict({"name": "buildability"})
    assert m.weight == 0
    assert m.score_0_10 is None
    assert m.active is False
    assert m.note == ""


def test_dossier_new_score_fields_default_none_and_empty():
    dossier = Dossier(profile_url="https://x.test")
    assert dossier.founder_larp_score is None
    assert dossier.company_larp_score is None
    assert dossier.metric_breakdown == []


def test_dossier_company_scan_with_metric_breakdown_roundtrip():
    dossier = Dossier(
        profile_url="https://resumegenie.example/",
        scan_type="company_app",
        identity={"name": "ResumeGenie AI"},
        buildability=Buildability(tier="TRIVIAL", note="thin wrapper"),
        company_larp_score=62,
        metric_breakdown=[
            MetricEntry(name="product_realness", weight=3, score_0_10=8, active=True, note="waitlist only"),
            MetricEntry(name="raise_inflation", weight=3, score_0_10=None, active=False, note=""),
        ],
    )
    d = dossier.to_dict()
    s = json.dumps(d)  # must be JSON-serializable

    back = Dossier.from_dict(json.loads(s))
    assert back.company_larp_score == 62
    assert len(back.metric_breakdown) == 2
    assert back.metric_breakdown[0].name == "product_realness"
    assert back.metric_breakdown[0].active is True
    assert back.metric_breakdown[1].active is False
    assert back.metric_breakdown[1].score_0_10 is None


def test_dossier_backward_compat_old_queue_file_missing_new_fields():
    """A queue file written before this feature existed has neither
    founder_larp_score, company_larp_score, nor metric_breakdown. It must
    still load cleanly, with the new fields defaulting sanely.
    """
    dossier = Dossier(profile_url="https://x.test", larp_score=40, verdict="Mostly real.")
    d = dossier.to_dict()
    old_shape = dict(d)
    del old_shape["founder_larp_score"]
    del old_shape["company_larp_score"]
    del old_shape["metric_breakdown"]

    back = Dossier.from_dict(old_shape)
    assert back.larp_score == 40
    assert back.verdict == "Mostly real."
    assert back.founder_larp_score is None
    assert back.company_larp_score is None
    assert back.metric_breakdown == []
