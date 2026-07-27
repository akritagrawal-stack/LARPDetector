"""Offline tests for detective.sources.packages. No network: the internal
_npm_lookup / _npm_downloads / _pypi_lookup / _pypistats_downloads
functions are monkeypatched with realistic sample npm registry / PyPI JSON
response shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import detective.sources.packages as packages


def _npm_data(
    version_count: int = 100,
    created: str = "2013-05-24T15:33:41.000Z",
    modified: str = "2026-07-01T00:00:00.000Z",
    maintainers: list = None,
    readme: str = "A" * 200,
) -> dict:
    return {
        "time": {"created": created, "modified": modified},
        "versions": {str(i): {} for i in range(version_count)},
        "maintainers": maintainers if maintainers is not None else [{"name": "fb"}],
        "readme": readme,
    }


def _pypi_data(
    version_count: int = 50,
    first_upload: str = "2011-02-14T08:49:42.641660Z",
    last_upload: str = "2026-05-14T19:25:26.443000Z",
    author: str = "Kenneth Reitz",
    summary: str = "Python HTTP for Humans.",
) -> dict:
    releases = {}
    for i in range(version_count):
        upload = first_upload if i == 0 else last_upload
        releases[str(i)] = [{"upload_time_iso_8601": upload}]
    return {"info": {"author": author, "maintainer": None, "summary": summary}, "releases": releases}


# ---------------------------------------------------------------------------
# verify_packages: basic gating
# ---------------------------------------------------------------------------


def test_verify_packages_empty_name_returns_empty():
    assert packages.verify_packages("") == []
    assert packages.verify_packages(None) == []


def test_verify_packages_neither_registry_has_it_returns_empty(monkeypatch):
    monkeypatch.setattr(packages, "_npm_lookup", lambda name: None)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: None)
    assert packages.verify_packages("this-should-not-exist-zzz") == []


def test_verify_packages_npm_lookup_failure_does_not_block_pypi(monkeypatch):
    def boom(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(packages, "_npm_lookup", boom)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: _pypi_data())
    monkeypatch.setattr(packages, "_pypistats_downloads", lambda name: 1000)

    evidence = packages.verify_packages("requests")
    assert len(evidence) == 1
    assert evidence[0]["source_url"].startswith("https://pypi.org")


# ---------------------------------------------------------------------------
# Evidence record shape + weight, both registries
# ---------------------------------------------------------------------------


def test_npm_evidence_shape_and_weight(monkeypatch):
    monkeypatch.setattr(packages, "_npm_lookup", lambda name: _npm_data())
    monkeypatch.setattr(packages, "_npm_downloads", lambda name: 5_000_000)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: None)

    evidence = packages.verify_packages("react")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "packages"
    assert record["weight"] == 1.0
    assert record["match_confidence"] == "high"
    assert "100 version(s)" in record["snippet"]
    assert "2013-05-24" in record["snippet"]
    assert "5000000 download(s)" in record["snippet"]
    assert "inflatable" in record["snippet"]


def test_pypi_evidence_shape_and_weight(monkeypatch):
    monkeypatch.setattr(packages, "_npm_lookup", lambda name: None)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: _pypi_data())
    monkeypatch.setattr(packages, "_pypistats_downloads", lambda name: 1_500_000)

    evidence = packages.verify_packages("requests")
    assert len(evidence) == 1
    record = evidence[0]
    assert record["source_name"] == "packages"
    assert record["weight"] == 1.0
    assert "50 version(s)" in record["snippet"]
    assert "Kenneth Reitz" in record["snippet"]


def test_both_registries_return_two_records(monkeypatch):
    monkeypatch.setattr(packages, "_npm_lookup", lambda name: _npm_data())
    monkeypatch.setattr(packages, "_npm_downloads", lambda name: 100)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: _pypi_data())
    monkeypatch.setattr(packages, "_pypistats_downloads", lambda name: 100)

    evidence = packages.verify_packages("somepackage")
    assert len(evidence) == 2


# ---------------------------------------------------------------------------
# Stub / namespace-squat detection: version count, not mere existence
# ---------------------------------------------------------------------------


def test_npm_single_version_flagged_as_stub(monkeypatch):
    monkeypatch.setattr(
        packages, "_npm_lookup", lambda name: _npm_data(version_count=1, readme="hi")
    )
    monkeypatch.setattr(packages, "_npm_downloads", lambda name: 0)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: None)

    evidence = packages.verify_packages("squatted-name")
    assert "thin stub" in evidence[0]["snippet"] or "namespace squat" in evidence[0]["snippet"]


def test_npm_many_versions_not_flagged_as_stub(monkeypatch):
    monkeypatch.setattr(packages, "_npm_lookup", lambda name: _npm_data(version_count=100))
    monkeypatch.setattr(packages, "_npm_downloads", lambda name: 1000)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: None)

    evidence = packages.verify_packages("react")
    assert "thin stub" not in evidence[0]["snippet"]


def test_pypi_single_version_empty_summary_flagged_as_stub(monkeypatch):
    monkeypatch.setattr(packages, "_npm_lookup", lambda name: None)
    monkeypatch.setattr(
        packages,
        "_pypi_lookup",
        lambda name: _pypi_data(version_count=1, summary=""),
    )
    monkeypatch.setattr(packages, "_pypistats_downloads", lambda name: None)

    evidence = packages.verify_packages("squatted-pypi-name")
    assert "thin stub" in evidence[0]["snippet"] or "namespace squat" in evidence[0]["snippet"]


def test_downloads_unavailable_does_not_crash(monkeypatch):
    monkeypatch.setattr(packages, "_npm_lookup", lambda name: _npm_data())
    monkeypatch.setattr(packages, "_npm_downloads", lambda name: None)
    monkeypatch.setattr(packages, "_pypi_lookup", lambda name: None)

    evidence = packages.verify_packages("react")
    assert len(evidence) == 1
    assert "download(s)" not in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_packages_react_and_requests():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real npm/PyPI API calls")

    react_evidence = packages.verify_packages("react")
    assert react_evidence, "expected npm to find the real 'react' package"

    requests_evidence = packages.verify_packages("requests")
    assert requests_evidence, "expected PyPI to find the real 'requests' package"
