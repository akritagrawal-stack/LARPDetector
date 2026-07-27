"""Apple App Store connector: bundle existence, rating volume/average,
current version, and last-update date via the iTunes Lookup/Search API,
plus recent review count and dates via the Customer Reviews RSS feed.

Google Play is P1, not this batch: this module is Apple-only.

THE KEY LARP TELL this connector exists for: a "10k users" claim sitting next
to an App Store listing with a dozen total ratings and no review activity in
months. Rating COUNT and review RECENCY are what matter here, not the star
average: a handful of friends can 5-star anything, so a high average on a
near-zero rating count proves almost nothing on its own.

Two free, unauthenticated, unkeyed JSON endpoints:
  - iTunes Search/Lookup. Confirmed live that itunes.apple.com/lookup only
    accepts an id/bundleId/amgArtistId/upc/isbn lookup key, NOT a free-text
    "term" (a term= query against /lookup returns resultCount 0 even for a
    real app name); a name search therefore goes through
    itunes.apple.com/search?term=..&entity=software, and an id-known lookup
    goes through itunes.apple.com/lookup?id=... This module uses whichever
    path matches what the caller supplied.
  - Customer Reviews RSS
    (itunes.apple.com/{country}/rss/customerreviews/id={id}/sortBy=mostRecent/json).
    Confirmed live and real (a WhatsApp lookup, id 310633997, returned 50
    real entries), but some high-profile apps returned zero entries in the
    same live check even though they obviously have reviews, so Apple
    appears to be winding this feed down per-app rather than it being
    globally broken. Treat an empty feed as "no data available from this
    feed for this app", never as "this app has zero reviews".

VERSION HISTORY IS NOT PUBLICLY EXPOSED: unlike npm/PyPI (see packages.py),
the iTunes API has no endpoint for a full version-history list or count,
only the CURRENT version string and its release date. This module reports
that honestly rather than inventing a version count it cannot obtain.

match_confidence: "high" only when a caller-supplied app_id was looked up
directly (an id is unambiguous). A name-only term match is always "low":
marketplace titles collide constantly, and a matching title does not connect
the listing to the claimed person or product. A term search that returns
candidates but NONE cleanly name-matches emits a checked-absent catalog record,
never an unrelated listing.

Public surface:
    verify_app_store(product_name, app_id=None) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .registry import weight_for

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://itunes.apple.com/search"
_LOOKUP_URL = "https://itunes.apple.com/lookup"
_REVIEWS_URL = "https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostRecent/json"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (iTunes App Store existence/footprint check)"
_SOURCE_NAME = "app_store_play_store_reviews"
_MAX_SEARCH_RESULTS = 5
_RECENT_WINDOW_DAYS = 90
_DEFAULT_COUNTRY = "us"
# A store listing whose last update is older than this reads as stale (a real
# "we're actively scaling" claim next to a year-dead listing is a liveness
# tell). Wider than the review-recency window: an app can legitimately go a
# quarter between releases, but not a year while "growing fast".
_LISTING_STALE_DAYS = 365


def _fetch_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(
        url, params=params, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}, timeout=_TIMEOUT
    )
    if resp.status_code != 200:
        logger.warning("app_store: HTTP %d for %r", resp.status_code, url)
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.warning("app_store: non-JSON response for %r: %s", url, exc)
        return None


def _search(term: str) -> Optional[list[dict]]:
    """Query the iTunes catalog. Returns None when the lookup FAILED (no HTTP
    200 JSON payload): "we could not look", which must never become a
    checked-absent read. Returns a (possibly empty) results list when the
    search SUCCEEDED, so a genuine empty catalog result stays distinguishable
    from a network failure.
    """
    data = _fetch_json(
        _SEARCH_URL,
        params={"term": term, "entity": "software", "country": _DEFAULT_COUNTRY, "limit": _MAX_SEARCH_RESULTS},
    )
    if data is None:
        return None
    return data.get("results") or []


def _lookup_by_id(app_id: str) -> Optional[dict]:
    data = _fetch_json(_LOOKUP_URL, params={"id": app_id, "country": _DEFAULT_COUNTRY})
    if not data:
        return None
    results = data.get("results") or []
    return results[0] if results else None


def _reviews(app_id: str) -> list[dict]:
    url = _REVIEWS_URL.format(country=_DEFAULT_COUNTRY, app_id=app_id)
    data = _fetch_json(url)
    if not data:
        return []
    return _parse_reviews_feed(data)


def _parse_reviews_feed(data: dict) -> list[dict]:
    """Pure parse of one Customer Reviews RSS JSON payload into a flat list
    of {rating, title, content, updated}. Some feeds carry a non-review
    "entry" (app summary, no im:rating) as the first item; those are
    skipped rather than counted as a review. Tolerant of a single-review
    feed where "entry" is a dict rather than a list.
    """
    feed = data.get("feed", {}) or {}
    raw_entries = feed.get("entry", []) or []
    if isinstance(raw_entries, dict):
        raw_entries = [raw_entries]

    parsed = []
    for entry in raw_entries:
        rating_label = ((entry.get("im:rating") or {}).get("label") or "").strip()
        if not rating_label:
            continue
        parsed.append(
            {
                "rating": rating_label,
                "title": ((entry.get("title") or {}).get("label") or "").strip(),
                "content": ((entry.get("content") or {}).get("label") or "").strip(),
                "updated": ((entry.get("updated") or {}).get("label") or "")[:10],
            }
        )
    return parsed


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_matches(product_name: str, track_name: str) -> bool:
    """True only when one normalized name is a PREFIX of the other (e.g.
    "Notion" vs "Notion: Notes, Tasks, AI"), same discipline as
    sec_edgar._name_matches: a bare substring-anywhere check is too loose
    for a marketplace full of near-duplicate app names.
    """
    a, b = _norm(product_name), _norm(track_name)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _is_recent(date_str: str, days: int = _RECENT_WINDOW_DAYS) -> bool:
    if not date_str:
        return False
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return (datetime.now(timezone.utc) - parsed).days <= days


def _build_listing_record(app: dict, confidence: str) -> dict:
    name = app.get("trackName", "")
    seller = app.get("sellerName", "")
    bundle_id = app.get("bundleId", "")
    track_id = app.get("trackId")
    rating_count = app.get("userRatingCount", 0) or 0
    avg_rating = app.get("averageUserRating")
    version = app.get("version", "")
    last_update = (app.get("currentVersionReleaseDate") or "")[:10]
    release_date = (app.get("releaseDate") or "")[:10]

    avg_rating_text = f"{avg_rating:.2f}" if isinstance(avg_rating, (int, float)) else "unknown"
    # TRACTION is the reason this connector exists: the rating COUNT
    # (userRatingCount) is the store's own popularity/footprint measure and the
    # number a claimed user/traction figure should be cross-checked against (the
    # star AVERAGE is not, a handful of friends can 5-star anything). Surfaced
    # first and named explicitly so both the reasoning step and the inflation
    # detector (dossier._discovered_number_for) read it reliably.
    stale = bool(last_update) and not _is_recent(last_update, _LISTING_STALE_DAYS)
    staleness_note = (
        f" The listing has not been updated in over {_LISTING_STALE_DAYS} days, a real "
        "staleness/liveness signal against any actively-growing claim."
        if stale
        else ""
    )
    name_only_note = (
        "NAME-ONLY MATCH: this listing shares the queried product name but is "
        "not tied to the claimed person, seller, or product without an app ID "
        "or another first-party link. "
        if confidence == "low"
        else ""
    )
    snippet = (
        name_only_note
        +
        f"App Store listing for {name!r} (seller: {seller or 'unknown'}, bundle {bundle_id or 'unknown'}): "
        f"{rating_count} total rating(s), average {avg_rating_text} stars. "
        f"TRACTION SIGNAL: userRatingCount is {rating_count} rating(s) (the store's own "
        "popularity/footprint measure; cross-check a claimed user/traction number against this "
        "COUNT, not the star average). Current version "
        f"{version or 'unknown'}, last updated {last_update or 'unknown'} "
        f"(first released {release_date or 'unknown'}).{staleness_note} Apple does not publish a public "
        "version-history count, only the current version, so that figure is not available here."
    )

    source_url = f"https://apps.apple.com/app/id{track_id}" if track_id else "https://apps.apple.com"
    return {
        "source_url": source_url,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def _build_reviews_record(app: dict, reviews: list[dict], confidence: str) -> dict:
    name = app.get("trackName", "")
    track_id = app.get("trackId")
    recent = [r for r in reviews if _is_recent(r["updated"])]

    lines = [
        f"{r['rating']} star, {r['updated'] or 'date unknown'}: {r['title'] or 'untitled'}"
        for r in reviews[:3]
    ]
    name_only_note = (
        "NAME-ONLY MATCH: these reviews must not be attributed to the claimed "
        "product without an app ID or another first-party link. "
        if confidence == "low"
        else ""
    )
    snippet = (
        name_only_note
        +
        f"Customer Reviews RSS for {name!r}: {len(reviews)} review(s) fetched from the feed, "
        f"{len(recent)} within the last {_RECENT_WINDOW_DAYS} days. " + "; ".join(lines) + "."
    )
    if not recent:
        snippet += (
            f" No review activity in the last {_RECENT_WINDOW_DAYS} days is itself a real "
            "footprint signal: a claimed actively-growing user base with no recent reviews is "
            "worth flagging against the claim."
        )

    source_url = (
        f"https://itunes.apple.com/{_DEFAULT_COUNTRY}/rss/customerreviews/id={track_id}/sortBy=mostRecent/json"
        if track_id
        else "https://itunes.apple.com"
    )
    return {
        "source_url": source_url,
        "snippet": snippet,
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": confidence,
    }


def _checked_absent_record(product_name: str) -> dict:
    """A CHECKED-ABSENT record: a COMPLETED search of Apple's own catalog that
    returned candidates but none whose name matches the queried product. A
    targeted negative result, not generic absence and not a failed search. The
    known-coverage caveats travel in the snippet so a downstream brain can
    never escalate it to DISPROVEN without ruling out renames/region limits.
    No unrelated listing is surfaced (the Lumosity-never-appears guarantee).
    """
    return {
        "source_url": "https://apps.apple.com",
        "snippet": (
            f"Searched Apple's App Store catalog for {product_name!r}; no app with a "
            "matching name is listed. This is a completed catalog lookup, not a failed "
            "search. Caveats: a renamed app can miss on name, and region-restricted "
            "listings may not appear in this catalog query."
        ),
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "high",
        "registry_check": "absent",
    }


def verify_app_store(product_name: str, app_id: Optional[str] = None) -> list[dict]:
    """iTunes listing + recent-review-activity check for one claimed app.

    Returns up to 2 evidence records (one listing-facts record, one
    review-activity record if the Customer Reviews RSS feed returned any
    entries for this app), or [] if nothing was found or every network path
    failed. Never raises.

    Pass app_id when it is already known (an unambiguous id-based lookup,
    match_confidence "high"); otherwise this searches by product_name and
    keeps every name-only match at match_confidence "low".
    """
    product_name = (product_name or "").strip()
    app_id = (str(app_id).strip() if app_id else "") or None
    if not product_name and not app_id:
        return []

    candidate: Optional[dict] = None
    confidence = "low"

    if app_id:
        try:
            candidate = _lookup_by_id(app_id)
        except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
            logger.warning("app_store: lookup by id failed for %r: %s", app_id, exc)
            return []
        if not candidate:
            return []
        confidence = "high"
    else:
        try:
            results = _search(product_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("app_store: search failed for %r: %s", product_name, exc)
            return []
        if results is None:
            # The search FAILED (no catalog payload): "we could not look",
            # never a checked-absent read.
            return []
        if not results:
            # A completed search whose catalog returned nothing at all: keep the
            # existing silent behavior (no checked-absent record for an empty
            # catalog result; the checked-absent read is reserved for the
            # results-present-but-none-match branch below).
            return []

        matches = [r for r in results if _name_matches(product_name, r.get("trackName", ""))]
        if len(matches) == 1:
            candidate = matches[0]
            confidence = "low"
        elif len(matches) > 1:
            candidate = matches[0]
            confidence = "low"
        else:
            # Candidates came back but NONE cleanly name-matches the queried
            # product. This is a COMPLETED catalog lookup that found no matching
            # app: emit an explicit checked-absent record (registry_check
            # "absent") rather than nothing, so a claim that itself invokes the
            # App Store can be read as a targeted negative result. An unrelated
            # listing is still never surfaced (the misleading-footprint guard),
            # and _reviews is never called for it.
            return [_checked_absent_record(product_name)]

    if candidate is None:
        return []

    evidence = [_build_listing_record(candidate, confidence)]

    track_id = candidate.get("trackId")
    if track_id:
        try:
            reviews = _reviews(str(track_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("app_store: reviews fetch failed for id %r: %s", track_id, exc)
            reviews = []
        if reviews:
            evidence.append(_build_reviews_record(candidate, reviews, confidence))

    return evidence
