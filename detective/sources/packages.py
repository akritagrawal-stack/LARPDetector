"""Package registry connector: npm + PyPI existence, version cadence,
maintainer identity, and download volume.

Free JSON APIs, no key:
    npm registry     : registry.npmjs.org/{name}
    npm downloads    : api.npmjs.org/downloads/point/last-month/{name}
    PyPI             : pypi.org/pypi/{name}/json
    PyPI downloads   : pypistats.org/api/packages/{name}/recent

WHAT THIS CATCHES: a "proprietary SDK" or "open-source library" claim that
is actually an empty stub or a namespace squat. The tell is the VERSION
COUNT and the README/description, not mere existence: a single-version
package with a near-empty README published once and never touched again is
a strong stub/squat signal, published existence alone is not (a brand-new
legitimate package also starts at one version). Mere existence of the name
proves nothing on its own.

DOWNLOADS ARE INFLATABLE: both npm and PyPI download counters count CI
installs, mirrors, and bots, not real distinct users. This module surfaces
the raw number as a coarse floor only ("at least this many install events
happened"), and every snippet says so explicitly; it is never treated as a
user-count or popularity proof.

match_confidence is always "high": a package-registry lookup is bound to
the exact package name string, the same registry-name identity reasoning
wayback.py and domain_age.py use for an exact URL/domain (no bare-name
search ambiguity here, unlike github.py or arxiv.py).

Public surface:
    verify_packages(name) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_NPM_REGISTRY_URL = "https://registry.npmjs.org/{name}"
_NPM_DOWNLOADS_URL = "https://api.npmjs.org/downloads/point/last-month/{name}"
_PYPI_URL = "https://pypi.org/pypi/{name}/json"
_PYPISTATS_URL = "https://pypistats.org/api/packages/{name}/recent"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (npm/PyPI package existence check)"
_SOURCE_NAME = "packages"

# <= 1 version, or a near-empty README/summary, is the stub/squat signal
# this module is built to catch (see module docstring).
_MIN_REAL_README_LEN = 40
_MIN_REAL_SUMMARY_LEN = 10


def _get_json(url: str) -> Optional[dict]:
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        logger.warning("packages: HTTP %d for %r", resp.status_code, url)
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("packages: non-JSON response for %r: %s", url, exc)
        return None


def _npm_lookup(name: str) -> Optional[dict]:
    return _get_json(_NPM_REGISTRY_URL.format(name=name))


def _npm_downloads(name: str) -> Optional[int]:
    data = _get_json(_NPM_DOWNLOADS_URL.format(name=name))
    if not data:
        return None
    return data.get("downloads")


def _pypi_lookup(name: str) -> Optional[dict]:
    return _get_json(_PYPI_URL.format(name=name))


def _pypistats_downloads(name: str) -> Optional[int]:
    data = _get_json(_PYPISTATS_URL.format(name=name))
    if not data:
        return None
    return (data.get("data") or {}).get("last_month")


def _npm_record(name: str, data: dict) -> dict:
    time_map = data.get("time", {}) or {}
    versions = data.get("versions", {}) or {}
    created = (time_map.get("created") or "")[:10]
    modified = (time_map.get("modified") or "")[:10]
    version_count = len(versions)
    maintainers = [m.get("name", "") for m in (data.get("maintainers") or []) if m.get("name")]

    downloads = None
    try:
        downloads = _npm_downloads(name)
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.warning("packages: npm downloads lookup failed for %r: %s", name, exc)

    readme_len = len((data.get("readme") or "").strip())
    is_stub = version_count <= 1 or readme_len < _MIN_REAL_README_LEN

    parts = [
        f"npm package {name!r}: {version_count} version(s) published, first {created or 'unknown'}, "
        f"last {modified or 'unknown'}."
    ]
    if maintainers:
        parts.append("Maintainer(s): " + ", ".join(maintainers) + ".")
    if downloads is not None:
        parts.append(
            f"~{downloads} download(s) in the last month (downloads are inflatable via CI/mirrors; "
            "treat as a coarse floor, not proof of real usage)."
        )
    if is_stub:
        parts.append(
            "Looks like a thin stub / possible namespace squat: very few versions and/or a "
            "near-empty README."
        )

    return {
        "source_url": f"https://www.npmjs.com/package/{name}",
        "snippet": " ".join(parts),
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "high",
    }


def _pypi_record(name: str, data: dict) -> dict:
    info = data.get("info", {}) or {}
    releases = data.get("releases", {}) or {}
    version_count = len(releases)

    upload_times = sorted(
        f.get("upload_time_iso_8601", "")
        for files in releases.values()
        for f in files[:1]
        if f.get("upload_time_iso_8601")
    )
    first_pub = upload_times[0][:10] if upload_times else ""
    last_pub = upload_times[-1][:10] if upload_times else ""
    maintainer = (info.get("maintainer") or info.get("author") or "").strip()

    downloads = None
    try:
        downloads = _pypistats_downloads(name)
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.warning("packages: pypistats lookup failed for %r: %s", name, exc)

    summary_len = len((info.get("summary") or "").strip())
    is_stub = version_count <= 1 or summary_len < _MIN_REAL_SUMMARY_LEN

    parts = [
        f"PyPI package {name!r}: {version_count} version(s) published, first {first_pub or 'unknown'}, "
        f"last {last_pub or 'unknown'}."
    ]
    if maintainer:
        parts.append(f"Maintainer/author: {maintainer}.")
    if downloads is not None:
        parts.append(
            f"~{downloads} download(s) in the last month (downloads are inflatable via CI/mirrors; "
            "treat as a coarse floor, not proof of real usage)."
        )
    if is_stub:
        parts.append(
            "Looks like a thin stub / possible namespace squat: very few versions and/or almost "
            "no description."
        )

    return {
        "source_url": f"https://pypi.org/project/{name}/",
        "snippet": " ".join(parts),
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "high",
    }


def verify_packages(name: str) -> list[dict]:
    """npm + PyPI existence/cadence/stub check for one claimed package name.

    Returns up to 2 evidence records (one per registry the name was found
    in), or [] if the name is blank or it exists in neither registry. Never
    raises: a failure on one registry does not block the other.
    """
    name = (name or "").strip()
    if not name:
        return []

    evidence: list[dict] = []

    try:
        npm_data = _npm_lookup(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("packages: npm lookup failed for %r: %s", name, exc)
        npm_data = None
    if npm_data:
        try:
            evidence.append(_npm_record(name, npm_data))
        except Exception as exc:  # noqa: BLE001
            logger.warning("packages: npm record build failed for %r: %s", name, exc)

    try:
        pypi_data = _pypi_lookup(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("packages: pypi lookup failed for %r: %s", name, exc)
        pypi_data = None
    if pypi_data:
        try:
            evidence.append(_pypi_record(name, pypi_data))
        except Exception as exc:  # noqa: BLE001
            logger.warning("packages: pypi record build failed for %r: %s", name, exc)

    return evidence
