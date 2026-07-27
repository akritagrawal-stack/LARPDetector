"""Consolidated principle regression suite for the judgment layer.

This file is a single high-level lock over the substance-verification contract
(docs/plans/substance-verification.plan.md, sections 8 and 9). Each of the seven
principles below is asserted with one or more synthetic-only tests. Nothing here
encodes a real person's name or a person-specific expected verdict: the suite
pins RULES, never an answer about anyone.

The seven principles (task contract):
  1. EXISTENCE MUST NOT CLEAR: name-plus-employer co-occurrence, or a bare
     company mention, leaves a claim UNVERIFIED and SUS-eligible. It downgrades
     to an "at a real entity" GAP; it does not clear.
  2. ROLE-SPEAKING EVIDENCE STILL CLEARS (anti-over-correction guard): role
     apposition, a title token, or a high-confidence org roster listing clears.
  3. ABSENCE ONLY ACCUSES IF WE LOOKED: a genuinely searched notable claim
     (searched_no_results) is SUS-eligible; a claim the tool could not look up
     (search_unavailable, dark backend) contributes zero and never gaps.
  4. ABSENCE NEVER REACHES LARP: an absence pile stays sub-66; only a DISPROVEN
     contradiction reaches the fraud band.
  5. POSITIVE MISMATCH ACCUSES: a magnitude undershoot and a role-vs-substance
     undershoot each raise an accusation and land the profile in SUS.
  6. REGISTRY ABSENCE CAPS AT SUS: an authoritative-registry absence is
     SUS-strength and NEVER DISPROVEN.
  7. NO NAMESAKE ACCUSATION: a github "medium" name-handle match neither clears
     a GAP nor fires a substance mismatch.

All tests are pure/offline: the mechanical detectors run on hand-built Claim
objects, and the build_dossier end-to-end tests patch verify.gather_evidence
with a deterministic offline fake and use an in-process stub provider. No
network, and NEVER a live LinkedIn fetch. Score bands throughout: CLEAR is
below 33, SUS is 33 to 66 (exclusive of 66), LARP is 66 and above.

No em dashes or en dashes anywhere in this file (house rule): commas, periods,
colons, parentheses, or "to" for ranges instead.
"""

from __future__ import annotations

import pytest

from detective import search
from detective import verify
from detective.dossier import (
    build_dossier,
    detect_gap,
    detect_registry_absence,
    detect_technical_authenticity,
    inject_candidates,
)
from detective.llm import (
    LLMProvider,
    compute_founder_score,
    mechanical_decompose,
    mechanical_decompose_company,
)
from detective.models import Claim, Dossier, EvidenceTier


# ---------------------------------------------------------------------------
# Shared harness, mirroring tests/test_dossier.py and
# tests/test_void_leak_regression.py exactly (the redefine-per-file idiom).
# ---------------------------------------------------------------------------


class DisciplinedStubProvider(LLMProvider):
    """In-process stand-in for a disciplined operator, matching the double in
    tests/test_dossier.py: a high-confidence contradiction becomes DISPROVEN,
    a GAP or either positive mismatch (tech-substance, registry absence) becomes
    UNVERIFIED plus high expected_footprint (SUS, never DISPROVEN off absence),
    otherwise UNVERIFIED. Sets larp_score off the tiers so build_dossier
    computes founder_larp_score, exactly like the real providers.
    """

    def decompose_claims(self, raw_profile: dict) -> list[Claim]:
        if (raw_profile.get("scan_type") or "person") == "company_app":
            return mechanical_decompose_company(raw_profile)
        return mechanical_decompose(raw_profile)

    def assign_tiers_and_verdict(self, dossier: Dossier) -> Dossier:
        has_disproven = False
        has_inflation = False
        for c in dossier.claims:
            names = {(e.get("source_name") or "") for e in c.evidence}
            if "mismatch_contradiction" in names or "mismatch_autonomy" in names:
                c.tier = EvidenceTier.DISPROVEN
                c.expected_footprint = "high"
                has_disproven = True
            elif names & {
                "mismatch_gap",
                "mismatch_tech_substance",
                "mismatch_registry_absence",
            }:
                c.tier = EvidenceTier.UNVERIFIED
                c.expected_footprint = "high"
            else:
                c.tier = EvidenceTier.UNVERIFIED
            if "mismatch_inflation" in names:
                has_inflation = True

        if dossier.scan_type == "company_app":
            if dossier.buildability is not None:
                dossier.buildability.tier = "MODERATE"
                dossier.buildability.note = "stub"
            for row in dossier.metric_breakdown:
                if not row.active or row.name == "buildability":
                    continue
                if row.name == "reach_vs_footprint" and has_inflation:
                    row.score_0_10 = 9
                else:
                    row.score_0_10 = 1
                row.note = "stub"
            dossier.verdict = "stub company verdict"
        else:
            dossier.larp_score = compute_founder_score(dossier.claims)
            dossier.verdict = (
                "stub verdict: proven falsehood" if has_disproven else "stub verdict: unverified"
            )
        return dossier


def _github_ev(match_confidence: str, read: str) -> dict:
    return {
        "source_url": "https://github.com/someone",
        "snippet": (
            f"GitHub account 'someone' created 2024-01-01T00:00:00Z, 1 public repo(s). "
            f"Technical authenticity read: {read} (0 original repo(s), 1 fork(s), 0 star(s) "
            f"across originals, languages: none detected, account 1.0y old)."
        ),
        "source_name": "github",
        "weight": 1.0,
        "match_confidence": match_confidence,
    }


def _org_roster_ev(match_confidence: str, found: bool) -> dict:
    body = (
        "Public roster/team page for 'Acme Robotics' lists 'Jane Sample'."
        if found
        else (
            "Public roster/team page for 'Acme Robotics' was fetched, but the name "
            "'Jane Sample' was NOT found in its visible text. This is a documented "
            "ABSENCE, not disproof."
        )
    )
    return {
        "source_url": "https://acme.test/team",
        "snippet": body,
        "source_name": "org_roster",
        "weight": 0.64,
        "match_confidence": match_confidence,
    }


def _yc_absent_record() -> dict:
    return {
        "source_url": "https://www.ycombinator.com/companies?query=Acme Robotics",
        "snippet": (
            "Queried Y Combinator's own public companies directory for 'Acme Robotics'; "
            "no matching company is listed. This is a COMPLETED directory lookup."
        ),
        "source_name": "accelerator_badges",
        "weight": 0.8,
        "match_confidence": "high",
        "registry_check": "absent",
    }


_WS1_IDENTITY = {
    "name": "Jane Sample",
    "headline": "Chief Technology Officer",
    "current_company": "Acme Robotics",
}


def _employment_claim(snippet: str, **overrides) -> Claim:
    evidence = overrides.pop("evidence", None) or [
        {"source_url": "https://news.test/a", "snippet": snippet}
    ]
    kwargs = {
        "type": "employment",
        "employer": "Acme Robotics",
        "title": "Chief Technology Officer",
        "assertion": "Worked as Chief Technology Officer at Acme Robotics.",
        "evidence": evidence,
    }
    kwargs.update(overrides)
    return Claim(**kwargs)


def _full_scan_raw(headline: str, title: str) -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jane-sample/",
        "scan_type": "person",
        "identity": {
            "name": "Jane Sample",
            "headline": headline,
            "current_company": "Acme Robotics",
        },
        "experience": [
            {
                "title": title,
                "company": "Acme Robotics",
                "start_date": "Jan 2020",
                "end_date": "Present",
            }
        ],
        "education": [],
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }


def _snippet_gather(snippet: str):
    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = [{"source_url": "https://news.test/c", "snippet": snippet}]
        return claim

    return _gather


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    # Keep search_healthy deterministic for the tests that consult it (mirrors
    # tests/test_void_leak_regression.py).
    search._brave_exhausted_until = 0.0
    search._searxng_dead_until = 0.0
    yield
    search._brave_exhausted_until = 0.0
    search._searxng_dead_until = 0.0


# ---------------------------------------------------------------------------
# Principle 1: EXISTENCE MUST NOT CLEAR.
# ---------------------------------------------------------------------------


def test_p1_association_employment_does_not_clear():
    """Name-plus-employer co-occurrence with nothing about the role surfaces an
    "at a real entity" GAP instead of silently clearing."""
    claim = _employment_claim(
        "Jane Sample and Acme Robotics were both at the Austin startup mixer."
    )
    findings = detect_gap([claim], identity=_WS1_IDENTITY)
    assert len(findings) == 1, findings
    f = findings[0]
    assert f.kind == "GAP"
    assert "at a real entity" in f.label
    assert f.severity == 0.35
    assert "existence is not substantiation" in f.detail
    inject_candidates([claim], findings)
    assert claim.evidence[-1]["source_name"] == "mismatch_gap"


def test_p1_company_overview_bare_mention_does_not_clear():
    """It-exists is not substantiation: a bare company mention downgrades a
    company_overview claim to the association GAP, while a snippet that speaks
    to the DESCRIPTION still clears it."""

    def _claim(snippet: str) -> Claim:
        return Claim(
            type="company_overview",
            employer="Acme Robotics",
            assertion="Acme Robotics builds warehouse automation robots.",
            evidence=[{"source_url": "https://news.test/b", "snippet": snippet}],
        )

    bare = detect_gap([_claim("Acme Robotics appeared on a list of Texas startups.")])
    assert len(bare) == 1 and "at a real entity" in bare[0].label

    # Twin guard: the description itself is corroborated, so it clears.
    substantive = detect_gap(
        [_claim("Acme Robotics builds warehouse automation robots for grocers.")]
    )
    assert substantive == []


def test_p1_association_employment_scores_sus_end_to_end(monkeypatch):
    """The core deliverable, end to end: a notable role whose ONLY evidence is
    co-occurrence lands in the SUS band, never CLEAR."""
    monkeypatch.setattr(
        verify,
        "gather_evidence",
        _snippet_gather(
            "Jane Sample and Acme Robotics were both at the Austin startup mixer."
        ),
    )
    d = build_dossier(
        _full_scan_raw("Vice President of Operations", "Vice President of Operations"),
        provider=DisciplinedStubProvider(),
        emit=lambda *a: None,
    )
    assert d.scan_depth == "full"
    assert any("at a real entity" in m["label"] for m in d.mismatches), d.mismatches
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


# ---------------------------------------------------------------------------
# Principle 2: ROLE-SPEAKING EVIDENCE STILL CLEARS (anti-over-correction guard).
# ---------------------------------------------------------------------------


def test_p2_role_apposition_clears():
    """Ordinary news apposition speaks to the role and suppresses the GAP."""
    claim = _employment_claim(
        "Jane Sample, chief technology officer of Acme Robotics, announced the raise."
    )
    assert detect_gap([claim], identity=_WS1_IDENTITY) == []


def test_p2_title_token_clears():
    """A role/title token next to the name and the employer is role-speaking."""
    claim = _employment_claim(
        "Acme Robotics engineer Jane Sample presented at PyCon."
    )
    assert detect_gap([claim], identity=_WS1_IDENTITY) == []


def test_p2_org_roster_high_clears():
    """The org's OWN roster listing the person is role-speaking corroboration
    and clears the GAP (a high-confidence org_roster listing)."""
    listed = _employment_claim("", evidence=[_org_roster_ev("high", True)])
    assert detect_gap([listed], identity=_WS1_IDENTITY) == []


def test_p2_role_speaking_coverage_scores_clear_end_to_end(monkeypatch):
    """The paired end-to-end guard for P1: the same profile with role-speaking
    apposition coverage stays CLEAR."""
    monkeypatch.setattr(
        verify,
        "gather_evidence",
        _snippet_gather(
            "Jane Sample, vice president of operations at Acme Robotics, announced "
            "the expansion."
        ),
    )
    d = build_dossier(
        _full_scan_raw("Vice President of Operations", "Vice President of Operations"),
        provider=DisciplinedStubProvider(),
        emit=lambda *a: None,
    )
    assert all(m["kind"] != "GAP" for m in d.mismatches), d.mismatches
    assert d.founder_larp_score is not None and d.founder_larp_score < 33


# ---------------------------------------------------------------------------
# Principle 3: ABSENCE ONLY ACCUSES IF WE LOOKED.
# ---------------------------------------------------------------------------


class _NotableVoidStub(LLMProvider):
    """Emits three FIXED notable claims (high expected_footprint) and applies
    the GAP discipline, so GAP eligibility does not depend on the decomposer.
    Mirrors the stub in tests/test_void_leak_regression.py."""

    def decompose_claims(self, raw_profile: dict) -> list[Claim]:
        return [
            Claim(
                type="employment",
                employer="Sample State University",
                title="IT Department",
                assertion="Works in the IT department at a large university IT department.",
                expected_footprint="high",
            ),
            Claim(
                type="employment",
                employer="Acme Robotics",
                title="Founder",
                assertion="Founder who earned about $100k from Acme Robotics.",
                expected_footprint="high",
            ),
            Claim(
                type="identity",
                assertion="Jordan Sample is a software engineer who ships real code for Initech.",
                expected_footprint="high",
            ),
        ]

    def assign_tiers_and_verdict(self, dossier: Dossier) -> Dossier:
        for c in dossier.claims:
            names = {(e.get("source_name") or "") for e in c.evidence}
            if "mismatch_gap" in names:
                c.tier = EvidenceTier.UNVERIFIED
                c.expected_footprint = "high"
            else:
                c.tier = EvidenceTier.UNVERIFIED
        dossier.larp_score = compute_founder_score(dossier.claims)
        dossier.verdict = "stub verdict"
        return dossier


def _void_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jordan-sample/",
        "scan_type": "person",
        "identity": {"name": "Jordan Sample", "current_company": "Acme Robotics"},
        "experience": [
            {"title": "IT Department", "company": "Sample State University"},
            {"title": "Founder", "company": "Acme Robotics"},
            {"title": "Software Engineer", "company": "Initech"},
        ],
        "_extraction": {"method": "live_scrape", "experience_count": 3},
    }


def _gather_empty(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    # Every claim comes back with no evidence (dark web plus dark connectors).
    return None


def test_p3_unsearchable_void_scores_clear(monkeypatch):
    """A dark backend (search_available True but search_healthy False) stamps
    every empty claim search_unavailable: no GAP, CLEAR band. Not automatically
    suspicious when we were not even looking."""
    monkeypatch.setattr(verify, "gather_evidence", _gather_empty)
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: False)

    d = build_dossier(_void_raw(), provider=_NotableVoidStub(), emit=lambda *a: None)

    assert d.scan_depth == "full"
    for c in d.claims:
        assert c.evidence[0]["source_name"] == "search_unavailable"
    assert all(m["kind"] != "GAP" for m in d.mismatches)
    assert d.founder_larp_score is not None
    assert d.founder_larp_score < 33


def test_p3_searched_void_scores_sus(monkeypatch):
    """The paired half: a HEALTHY search that genuinely found nothing on the same
    notable claims stamps searched_no_results, keeps the GAP path alive, and
    lands SUS. A proper search that cannot verify IS a legitimate SUS input."""
    monkeypatch.setattr(verify, "gather_evidence", _gather_empty)
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: True)

    d = build_dossier(_void_raw(), provider=_NotableVoidStub(), emit=lambda *a: None)

    for c in d.claims:
        assert c.evidence[0]["source_name"] == "searched_no_results"
    assert any(m["kind"] == "GAP" for m in d.mismatches)
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66


# ---------------------------------------------------------------------------
# Principle 4: ABSENCE NEVER REACHES LARP.
# ---------------------------------------------------------------------------


class _PileStub(LLMProvider):
    """Emits four FIXED claims that gather routes to an association gap, a
    tech-substance undershoot, a registry absence, and a searched void. Applies
    the same discipline as DisciplinedStubProvider: absence and positive
    mismatches resolve to UNVERIFIED plus high footprint, never DISPROVEN."""

    def decompose_claims(self, raw_profile: dict) -> list[Claim]:
        return [
            Claim(
                type="employment",
                employer="Acme Robotics",
                title="Vice President of Operations",
                assertion="Worked as Vice President of Operations at Acme Robotics.",
                expected_footprint="high",
            ),
            Claim(
                type="employment",
                employer="Globex Systems",
                title="Chief Technology Officer",
                assertion="Worked as Chief Technology Officer at Globex Systems.",
                expected_footprint="high",
            ),
            Claim(
                type="employment",
                employer="Initech",
                title="Founder",
                assertion="Founder of Initech (YC S24).",
                expected_footprint="high",
            ),
            Claim(
                type="identity",
                assertion="Jordan Sample is a widely recognized operator.",
                expected_footprint="high",
            ),
        ]

    def assign_tiers_and_verdict(self, dossier: Dossier) -> Dossier:
        for c in dossier.claims:
            names = {(e.get("source_name") or "") for e in c.evidence}
            if "mismatch_contradiction" in names:
                c.tier = EvidenceTier.DISPROVEN
                c.expected_footprint = "high"
            elif names & {
                "mismatch_gap",
                "mismatch_tech_substance",
                "mismatch_registry_absence",
            }:
                c.tier = EvidenceTier.UNVERIFIED
                c.expected_footprint = "high"
            else:
                c.tier = EvidenceTier.UNVERIFIED
        dossier.larp_score = compute_founder_score(dossier.claims)
        dossier.verdict = "stub verdict"
        return dossier


def _pile_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    if claim.title == "Vice President of Operations":
        claim.evidence = [
            {
                "source_url": "https://news.test/a",
                "snippet": "Jordan Sample and Acme Robotics were both at the Austin startup mixer.",
            }
        ]
        return claim
    if claim.title == "Chief Technology Officer":
        claim.evidence = [_github_ev("high", "thin-or-absent")]
        return claim
    if "YC S24" in (claim.assertion or ""):
        rec = _yc_absent_record()
        rec["source_url"] = "https://www.ycombinator.com/companies?query=Initech"
        rec["snippet"] = (
            "Queried Y Combinator's own public companies directory for 'Initech'; "
            "no matching company is listed. This is a COMPLETED directory lookup."
        )
        claim.evidence = [rec]
        return claim
    return None  # identity: a searched void


def test_p4_absence_pile_never_reaches_larp(monkeypatch):
    """A profile stacking a searched void, an association gap, a substance
    undershoot, and a registry absence, with zero DISPROVEN, stays sub-66: the
    hard cap holds against the full pile."""
    monkeypatch.setattr(verify, "gather_evidence", _pile_gather)
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: True)

    d = build_dossier(_void_raw(), provider=_PileStub(), emit=lambda *a: None)

    assert d.mismatches
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score


def _contradiction_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    if (claim.employer or "") == "Acme Robotics":
        claim.evidence = [
            {
                "source_url": "https://news.test/acme",
                "snippet": "Acme Robotics has no record of this person ever being employed there.",
                "source_name": "",
                "match_confidence": "medium",
            }
        ]
        return claim
    return None


def test_p4_contradiction_reaches_larp(monkeypatch):
    """The fraud band still requires an affirmative contradiction: one claim
    with an adverse "no record" record becomes DISPROVEN and reaches LARP."""
    monkeypatch.setattr(verify, "gather_evidence", _contradiction_gather)
    monkeypatch.setattr(search, "search_available", lambda: True)
    monkeypatch.setattr(search, "search_healthy", lambda: False)

    # Even under a DARK backend (absence-suspicion suppressed), a real
    # contradiction still reaches the fraud band.
    d = build_dossier(_void_raw(), provider=_ContradictionStub(), emit=lambda *a: None)
    assert any(c.tier is EvidenceTier.DISPROVEN for c in d.claims)
    assert d.founder_larp_score is not None
    assert d.founder_larp_score >= 66


class _ContradictionStub(_NotableVoidStub):
    """The void stub, extended to escalate a real contradiction to DISPROVEN,
    matching the operator discipline in DisciplinedStubProvider."""

    def assign_tiers_and_verdict(self, dossier: Dossier) -> Dossier:
        for c in dossier.claims:
            names = {(e.get("source_name") or "") for e in c.evidence}
            if "mismatch_contradiction" in names:
                c.tier = EvidenceTier.DISPROVEN
                c.expected_footprint = "high"
            elif "mismatch_gap" in names:
                c.tier = EvidenceTier.UNVERIFIED
                c.expected_footprint = "high"
            else:
                c.tier = EvidenceTier.UNVERIFIED
        dossier.larp_score = compute_founder_score(dossier.claims)
        dossier.verdict = "stub verdict"
        return dossier


# ---------------------------------------------------------------------------
# Principle 5: POSITIVE MISMATCH ACCUSES.
# ---------------------------------------------------------------------------


def _magnitude_gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
    if claim.type == "user_count":
        claim.evidence = [
            {
                "source_url": "https://apps.apple.com/app/id3",
                "snippet": "App Store listing for 'Acme Robotics': 13 total rating(s), average 3.62 stars.",
                "source_name": "app_store_play_store_reviews",
                "match_confidence": "high",
            }
        ]
    else:
        claim.evidence = [
            {"source_url": "https://g.test", "snippet": "generic result, no corroboration"}
        ]
    return claim


def _magnitude_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jane-sample/",
        "scan_type": "person",
        "identity": {"name": "Jane Sample", "headline": "Founder", "current_company": "Acme Robotics"},
        "experience": [
            {
                "title": "Founder",
                "company": "Acme Robotics",
                "start_date": "Jan 2024",
                "end_date": "Present",
                "description": "Built Acme Robotics, a campus events app that grew to 2,000+ users.",
            }
        ],
        "education": [],
        "_extraction": {"method": "live_scrape", "experience_count": 1, "with_description_count": 1},
    }


def test_p5_rating_undershoot_stays_sus_without_fake_user_math(monkeypatch):
    """A tiny rating footprint can leave a notable traction claim unverified,
    but ratings are not converted into users or called measured inflation."""
    monkeypatch.setattr(verify, "gather_evidence", _magnitude_gather)
    d = build_dossier(_magnitude_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None)
    kinds = {m["kind"] for m in d.mismatches}
    assert "INFLATION" not in kinds, d.mismatches
    assert "GAP" in kinds, d.mismatches
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


def _cto_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jane-sample/",
        "scan_type": "person",
        "identity": {
            "name": "Jane Sample",
            "headline": "Chief Technology Officer",
            "current_company": "Acme Robotics",
        },
        "experience": [
            {
                "title": "Chief Technology Officer",
                "company": "Acme Robotics",
                "start_date": "Jan 2020",
                "end_date": "Present",
            }
        ],
        "education": [],
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }


def test_p5_role_vs_substance_undershoot_scores_sus(monkeypatch):
    """A loud technical claim undershot by the person's OWN confirmed code
    footprint (high-confidence github reading thin-or-absent) is a resolved
    undershoot that lands SUS, never DISPROVEN (employer code is often
    private)."""

    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = [_github_ev("high", "thin-or-absent")]
        return claim

    monkeypatch.setattr(verify, "gather_evidence", _gather)
    d = build_dossier(_cto_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None)
    kinds = {m["kind"] for m in d.mismatches}
    assert "TECH_SUBSTANCE_MISMATCH" in kinds, d.mismatches
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


# ---------------------------------------------------------------------------
# Principle 6: REGISTRY ABSENCE CAPS AT SUS (never DISPROVEN).
# ---------------------------------------------------------------------------


def _yc_raw() -> dict:
    return {
        "profile_url": "https://www.linkedin.com/in/jane-sample/",
        "scan_type": "person",
        "identity": {
            "name": "Jane Sample",
            "headline": "Founder at Acme Robotics",
            "current_company": "Acme Robotics",
        },
        "experience": [
            {
                "title": "Founder (YC S24)",
                "company": "Acme Robotics",
                "start_date": "Jan 2024",
                "end_date": "Present",
            }
        ],
        "education": [],
        "_extraction": {"method": "live_scrape", "experience_count": 1},
    }


def test_p6_registry_absence_scores_sus(monkeypatch):
    """A YC-invoking claim with a completed empty accelerator lookup lands in the
    SUS band, never DISPROVEN."""

    def _gather(claim, identity=None, pb_budget=None, company_url=None, *, max_evidence=8):
        claim.evidence = [_yc_absent_record()]
        return claim

    monkeypatch.setattr(verify, "gather_evidence", _gather)
    d = build_dossier(_yc_raw(), provider=DisciplinedStubProvider(), emit=lambda *a: None)
    kinds = {m["kind"] for m in d.mismatches}
    assert "REGISTRY_ABSENCE" in kinds, d.mismatches
    assert d.founder_larp_score is not None
    assert 33 <= d.founder_larp_score < 66, d.founder_larp_score
    assert all(c.tier is not EvidenceTier.DISPROVEN for c in d.claims)


def test_p6_registry_absence_never_disproven():
    """The injected finding must say, in its own text, that a registry absence
    caps at SUS unconditionally, so no operator can escalate it to DISPROVEN."""
    claim = Claim(
        type="employment",
        employer="Acme Robotics",
        title="Founder",
        assertion="Founder of Acme Robotics (YC S24).",
        evidence=[_yc_absent_record()],
    )
    findings = detect_registry_absence([claim])
    assert len(findings) == 1
    assert findings[0].kind == "REGISTRY_ABSENCE"
    assert "NEVER reach DISPROVEN" in findings[0].detail
    assert "UNCONDITIONALLY" in findings[0].detail


# ---------------------------------------------------------------------------
# Principle 7: NO NAMESAKE ACCUSATION.
# ---------------------------------------------------------------------------


def test_p7_github_medium_neither_clears_gap_nor_fires_substance_mismatch():
    """A "medium" github record is a name-pattern match: an account that merely
    EXISTS somewhere. It clears no role claim (a GAP still fires) and it can
    never fire the resolved-undershoot substance mismatch (only the ordinary
    absence-shaped void branch may)."""
    # It does not clear the GAP.
    gap_claim = _employment_claim("", evidence=[_github_ev("medium", "substantial")])
    gap_findings = detect_gap([gap_claim], identity=_WS1_IDENTITY)
    assert len(gap_findings) == 1 and gap_findings[0].kind == "GAP"

    # It does not fire a substance mismatch (only the absence-shaped void may).
    tech_claim = _employment_claim("", evidence=[_github_ev("medium", "thin-or-absent")])
    tech_findings = detect_technical_authenticity([tech_claim])
    assert all(f.kind != "TECH_SUBSTANCE_MISMATCH" for f in tech_findings), tech_findings
    assert [f.kind for f in tech_findings] == ["TECH_AUTHENTICITY"]
