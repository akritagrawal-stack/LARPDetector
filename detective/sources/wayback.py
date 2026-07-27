"""Wayback Machine connector: first-capture date (site-age truth) plus a
small sample of capture timestamps so a later step can diff a company's own
claims against what its site actually said over time.

Free CDX API (web.archive.org/cdx/search/cdx?output=json). No auth, no
documented hard rate limit, but this module still sends a descriptive
User-Agent and issues exactly one request per verify_wayback call.

Public surface:
    verify_wayback(url) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

match_confidence is always "high": unlike github.py's name search, this
connector is bound directly to the exact URL being checked, so there is no
identity-resolution ambiguity, only "did the CDX API return captures or
not."

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (Wayback CDX lookup)"
_SOURCE_NAME = "wayback_machine"
_MAX_SAMPLE_TIMESTAMPS = 6


def _cdx_query(url: str) -> list:
    import requests  # lazy: keeps offline paths import-free

    params = {"url": url, "output": "json", "collapse": "timestamp:8", "limit": 200}
    resp = requests.get(
        _CDX_ENDPOINT, params=params, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT
    )
    if resp.status_code != 200:
        logger.warning("wayback: CDX HTTP %d for %r", resp.status_code, url)
        return []
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("wayback: CDX non-JSON response for %r: %s", url, exc)
        return []


def _rows_to_timestamps(rows: list) -> list[str]:
    """Pure parse of a CDX JSON response (list of lists, first row is the
    header) into a sorted list of "timestamp" column values. [] for an
    empty, malformed, or header-only response.
    """
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    try:
        ts_idx = header.index("timestamp")
    except ValueError:
        return []
    timestamps = [row[ts_idx] for row in rows[1:] if len(row) > ts_idx]
    return sorted(timestamps)


def _sample_timestamps(timestamps: list[str], count: int) -> list[str]:
    """Evenly spaced sample of at most `count` timestamps (oldest first),
    so a later diff step sees the capture history spread across time
    rather than clustered at one end.
    """
    if not timestamps:
        return []
    if len(timestamps) <= count:
        return timestamps
    step = len(timestamps) / count
    return [timestamps[int(i * step)] for i in range(count)]


def _fmt_ts(ts: str) -> str:
    if len(ts) >= 8:
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
    return ts


def verify_wayback(url: str) -> list[dict]:
    """First-capture date and a small capture-timestamp sample for one URL.

    Returns a single-record list, or [] if the URL is blank, the CDX API
    call failed, or no captures exist for it. Never raises.
    """
    url = (url or "").strip()
    if not url:
        return []

    try:
        rows = _cdx_query(url)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("wayback: CDX request failed for %r: %s", url, exc)
        return []

    timestamps = _rows_to_timestamps(rows)
    if not timestamps:
        logger.info("wayback: no captures found for %r", url)
        return []

    first_capture = timestamps[0]
    sample = _sample_timestamps(timestamps, _MAX_SAMPLE_TIMESTAMPS)

    snippet = (
        f"First Wayback Machine capture of {url!r}: {_fmt_ts(first_capture)}. "
        f"{len(timestamps)} total capture date(s) on record, sample: "
        f"{', '.join(_fmt_ts(t) for t in sample)}."
    )
    return [
        {
            "source_url": f"https://web.archive.org/web/{first_capture}/{url}",
            "snippet": snippet,
            "source_name": _SOURCE_NAME,
            "weight": weight_for(_SOURCE_NAME),
            "match_confidence": "high",
        }
    ]
