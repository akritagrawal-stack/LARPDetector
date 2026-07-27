"""Company/app landing-page extraction.

Two layers, kept strictly separate so the parser is testable offline (same
shape as extract_linkedin.py):

  1. parse_company_page(html, url)  -- PURE function. Takes the HTML of a
     product landing page and returns a normalized raw company profile: the
     product name, tagline, pricing tiers, claimed metrics (user counts,
     revenue), proprietary-tech language, and integrations. No network. This
     is the unit-tested surface.

  2. fetch_company(url, live=False) -- the live orchestration. Refuses to run
     unless live=True (same protection pattern as extract_linkedin.fetch_profile
     so a stray test run can never make a real HTTP request), then fetches the
     page with `requests` and hands the HTML to the pure parser.

Normalized raw company profile shape (mirrors the person raw_profile shape so
pipeline.run and llm.mechanical_decompose_company can key off the same
identity dict):
    {
        "profile_url": str,
        "scan_type":   "company_app",
        "identity": {
            "name":            str,   # product name
            "headline":        str,   # tagline
            "current_company": str,   # company name (often == product name)
            "location":        "",
        },
        "pricing": {
            "tiers": [{"name": str, "price": str, "period": str}, ...],
        },
        "metrics": [
            {"type": "user_count" | "revenue_metric" | "funding" | "headcount",
             "text": str, "value": str, "unit": str (user_count only)},
            ...
        ],
        "tech_claims": [
            {"type": "proprietary_tech", "text": str},
            ...
        ],
        "integrations": [str, ...],
    }

House rule: no em dashes anywhere in this file.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# --- regex patterns (kept simple and readable, not a full NLP pass) ---------

_USER_COUNT_RE = re.compile(
    r"(?P<value>[\d][\d,]*(?:\.\d+)?)(?P<scale>[kmb])?\+?\s*"
    r"(?P<unit>users|customers|subscribers|downloads|companies|teams|"
    r"stars|developers|installs)",
    re.IGNORECASE,
)

_REVENUE_RE = re.compile(
    r"\$(?P<amount>[\d][\d,\.]*)\s*(?P<scale>million|billion|[kmb])?\s*"
    r"(?P<label>ARR|in annual recurring revenue|in revenue|revenue)",
    re.IGNORECASE,
)

_PRICE_RE = re.compile(
    r"\$(?P<amount>[\d][\d,]*(?:\.\d{1,2})?)\s*(?:/\s*(?P<period>mo|month|yr|year))?",
    re.IGNORECASE,
)

# Fallback-only pricing pattern (whole-page scan, no "price"-classed node to
# anchor on): the period unit is REQUIRED here, unlike _PRICE_RE above, so a
# bare "$N" mention (funding raised, ARR, a "$0" callout inside prose) is
# never mistaken for an actual price. Used only by the whole-page fallback in
# _extract_pricing, never for nodes whose class already contains "price".
_PRICE_FALLBACK_RE = re.compile(
    r"\$(?P<amount>[\d][\d,]*(?:\.\d{1,2})?)\s*/\s*(?P<period>mo|month|yr|year)\b",
    re.IGNORECASE,
)

# Funding/revenue context words that, when found immediately before a
# fallback dollar-amount match, mean the match is a raised/ARR/revenue
# figure, not a pricing tier, and must be excluded even if it happens to
# carry a period-looking suffix nearby in free text.
_MONEY_CONTEXT_RE = re.compile(
    r"\b(?:raised|closed|ARR|revenue|funding|million|billion)\b",
    re.IGNORECASE,
)

_HEADCOUNT_RE = re.compile(
    r"team\s+of\s+(?P<value>[\d][\d,]*)\+?",
    re.IGNORECASE,
)

_FUNDING_RE = re.compile(
    r"(?:raised|closed)\s+\$(?P<amount>[\d][\d,\.]*)\s*"
    r"(?P<scale>million|billion|[kmb])?\s*(?:in\s+)?"
    r"(?P<round>pre-seed|seed|series\s+[a-d])?",
    re.IGNORECASE,
)

_PROPRIETARY_RE = re.compile(r"([^.]*\bproprietary\b[^.]*\.)", re.IGNORECASE)
_AI_POWERED_RE = re.compile(
    r"([^.]*\bpowered by\b[^.]*\bAI\b[^.]*\.)", re.IGNORECASE
)
_AI_TAG_RE = re.compile(r"([^.]*\bAI[\s-]powered\b[^.]*\.)", re.IGNORECASE)

# Bug 5 (coverage): the three patterns above only fire on the literal words
# "proprietary" or "AI-powered"/"powered by AI", so real copy for a company
# that ships an SDK/library, or that claims a custom (not necessarily
# labeled "proprietary") model, produced no tech_claims at all, silently
# leaving the proprietary_ai_gap / key_role_coverage company-LARP metrics
# inactive. Two more patterns, each still conservative:
#   - _SDK_PACKAGE_RE: fires only on an explicit SDK/package-distribution
#     phrase ("our SDK", "open-source package/library", "pip install",
#     "npm install", "available on npm/pypi"), never on generic "we believe
#     in openness" marketing copy that never names a package.
#   - _CUSTOM_MODEL_RE: fires only when "custom" (or "our own") sits next to
#     an AI/ML-flavored model word ("custom AI model", "custom-trained
#     foundation model"), so a generic "custom pricing model" line is never
#     mistaken for a tech claim.
_SDK_PACKAGE_RE = re.compile(
    r"([^.]*\b(?:our SDK|open[\s-]source\s+(?:package|library|SDK|project)|"
    r"npm install|pip install|available on\s+(?:npm|pypi))\b[^.]*\.)",
    re.IGNORECASE,
)
_CUSTOM_MODEL_RE = re.compile(
    r"([^.]*\b(?:custom|our own)[\s-]*(?:built|trained)?\s*"
    r"(?:AI|ML|foundation|LLM)\s+models?\b[^.]*\.)",
    re.IGNORECASE,
)

_INTEGRATIONS_LEAD_RE = re.compile(
    r"integrat(?:es|ion)\s+with\s+(?P<list>[^.]+)\.", re.IGNORECASE
)

# --- product-name extraction: source priority + tagline detection ----------
#
# Bug 1: the old extractor took the hero <h1> as the product name first,
# falling back to <title> only if there was no h1. On a real landing page
# (Browser Use / Gregor Zunic, YC W25) the h1 is a marketing TAGLINE ("The
# Way AI uses the web.") and the real product name ("Browser Use") only
# appears in <title>. That mis-parse poisoned every downstream evidence
# query (they searched "The Way AI uses the web." and pulled in unrelated
# results like xAI/Grok/Opera). Fixed priority, most reliable first:
#   1. og:site_name meta tag (machine-readable, never a tagline)
#   2. <title>, with a trailing " | ..." / " - ..." tagline suffix stripped
#   3. a logo image/svg's alt or aria-label ("Browser Use logo")
#   4. the hero <h1>, but ONLY if it does not read like a tagline sentence
#      (see _looks_like_tagline); a tagline sitting in h1 position must
#      never be promoted to the name just because nothing else was found.

# Splits a title on a separator that has whitespace on both sides (pipe, en
# dash, em dash, or a bare hyphen), matched by unicode escape so no literal
# long dash is ever typed in this source file (house rule): a hyphenated
# product name with no surrounding spaces ("Ever-Green") is never cut
# mid-word.
_TITLE_DASH_CLASS = "-" + chr(0x2010) + chr(0x2011) + chr(0x2013) + chr(0x2014)
_TITLE_SPLIT_RE = re.compile(r"\s+(?:\||[" + _TITLE_DASH_CLASS + r"])\s+")

# A short, deliberately small list of common imperative-verb tagline openers
# ("Automate your workflow", "Build the future of..."). Only used as a THIRD
# signal alongside trailing-period and word-count; a name that happens to
# start with one of these words but is short and unpunctuated still needs
# one of the other two signals to be flagged, keeping this conservative.
_TAGLINE_VERB_STARTS = (
    "build", "automate", "generate", "create", "manage", "get", "find",
    "search", "discover", "unlock", "simplify", "streamline", "power",
    "transform", "turn", "make", "ship", "launch", "run", "scale", "grow",
    "book", "close", "save", "boost", "supercharge", "meet", "browse", "use",
)


def _looks_like_tagline(text: str) -> bool:
    """True when `text` reads like a marketing sentence rather than a bare
    product name: it ends in a period, it runs more than about 5 words, or
    it opens with a common lowercase imperative verb.
    """
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("."):
        return True
    words = t.split()
    if len(words) > 5:
        return True
    first = words[0].lower() if words else ""
    return first in _TAGLINE_VERB_STARTS


def _strip_title_suffix(title: str) -> str:
    """'Browser Use - The Way AI uses the web.' -> 'Browser Use'.

    Only cuts at the FIRST separator, so a longer trailing tagline (which may
    itself contain a dash) is dropped wholesale along with everything after
    the product name.
    """
    t = (title or "").strip()
    if not t:
        return ""
    parts = _TITLE_SPLIT_RE.split(t, maxsplit=1)
    return parts[0].strip() if parts else t


def _empty_profile(url: str) -> dict[str, Any]:
    return {
        "profile_url": url,
        "scan_type": "company_app",
        "identity": {"name": "", "headline": "", "current_company": "", "location": ""},
        "pricing": {"tiers": []},
        "metrics": [],
        "tech_claims": [],
        "integrations": [],
    }


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_product_name(soup) -> str:
    """See the module-level comment above _looks_like_tagline for the bug
    this fixes and the source priority (og:site_name, then title, then a
    logo alt/aria-label, then a non-tagline-looking h1).
    """
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og is not None:
        text = _clean(og.get("content", ""))
        if text:
            return text

    if soup.title is not None:
        raw_title = _clean(soup.title.get_text())
        text = _strip_title_suffix(raw_title)
        if text and not _looks_like_tagline(text):
            return text

    logo = soup.find(attrs={"alt": re.compile(r"logo", re.IGNORECASE)})
    if logo is None:
        logo = soup.find(attrs={"aria-label": re.compile(r"logo", re.IGNORECASE)})
    if logo is not None:
        raw = logo.get("alt") or logo.get("aria-label") or ""
        text = _clean(re.sub(r"\blogo\b", "", raw, flags=re.IGNORECASE))
        if text:
            return text

    h1 = soup.find("h1")
    if h1 is not None:
        text = _clean(h1.get_text(" "))
        if text and not _looks_like_tagline(text):
            return text

    return ""


def _extract_tagline(soup) -> str:
    tag = soup.find(class_=lambda c: bool(c) and "tagline" in c)
    if tag is not None:
        text = _clean(tag.get_text(" "))
        if text:
            return text
    meta = soup.find("meta", attrs={"name": "description"})
    if meta is not None:
        text = _clean(meta.get("content", ""))
        if text:
            return text
    # A hero h1 that reads like a marketing sentence (see
    # _looks_like_tagline) IS the tagline, just misplaced in name position;
    # capture it here rather than losing it entirely.
    h1 = soup.find("h1")
    if h1 is not None:
        text = _clean(h1.get_text(" "))
        if text and _looks_like_tagline(text):
            return text
    p = soup.find("p")
    if p is not None:
        return _clean(p.get_text(" "))
    return ""


def _extract_pricing(soup, full_text: str) -> dict[str, Any]:
    tiers: list[dict[str, str]] = []
    for node in soup.find_all(class_=lambda c: bool(c) and "price" in c):
        text = _clean(node.get_text(" "))
        m = _PRICE_RE.search(text)
        if not m:
            continue
        # Best-effort tier name: nearest preceding heading, else "".
        name = ""
        heading = node.find_previous(["h1", "h2", "h3", "h4"])
        if heading is not None:
            name = _clean(heading.get_text(" "))
        tiers.append(
            {
                "name": name,
                "price": f"${m.group('amount')}",
                "period": (m.group("period") or "").lower(),
            }
        )

    if not tiers:
        # Fall back to scanning the whole page text for a "$X/mo" or "$X/yr"
        # price. Requires an explicit period unit (_PRICE_FALLBACK_RE), and
        # skips a match sitting right after funding/revenue context words
        # ("raised $12 million", "$10M ARR"), so a dollar amount from that
        # kind of copy is never mistaken for a pricing tier.
        for m in _PRICE_FALLBACK_RE.finditer(full_text):
            preceding = full_text[max(0, m.start() - 40) : m.start()]
            if _MONEY_CONTEXT_RE.search(preceding):
                continue
            tiers.append(
                {
                    "name": "",
                    "price": f"${m.group('amount')}",
                    "period": (m.group("period") or "").lower(),
                }
            )
            break

    return {"tiers": tiers}


def _extract_metrics(full_text: str) -> list[dict[str, str]]:
    metrics: list[dict[str, str]] = []
    seen_values: set[str] = set()

    for m in _USER_COUNT_RE.finditer(full_text):
        # Fold a "k"/"m"/"b" scale suffix (e.g. the "k" in "100k+ stars")
        # into the stored value so a GitHub-star-count-style claim keeps its
        # real magnitude instead of silently reporting the bare "100".
        value = m.group("value") + (m.group("scale") or "")
        key = f"user_count:{value}"
        if key in seen_values:
            continue
        seen_values.add(key)
        metrics.append(
            {
                "type": "user_count",
                "text": _clean(m.group(0)),
                "value": value,
                # "unit" is the raw noun the claim was counted in (users,
                # customers, downloads, ... vs companies, teams). Carried
                # through onto the Claim (see llm.mechanical_decompose_company)
                # so the company-LARP reach_vs_footprint metric can tell a
                # consumer-scale claim apart from a B2B seat/team count
                # without re-parsing the claim's free-form assertion text.
                "unit": (m.group("unit") or "").strip().lower(),
            }
        )

    # "team of N" / "team of N+" headcount phrase, for the headcount_inflation
    # company-LARP metric (see llm.mechanical_decompose_company /
    # llm.build_metric_breakdown). Deliberately narrow (just this one common
    # landing-page phrasing), same discipline as the other extractors here:
    # a miss just means the headcount_inflation metric stays inactive, never
    # a fabricated number.
    for m in _HEADCOUNT_RE.finditer(full_text):
        value = m.group("value")
        key = f"headcount:{value}"
        if key in seen_values:
            continue
        seen_values.add(key)
        metrics.append(
            {
                "type": "headcount",
                "text": _clean(m.group(0)),
                "value": value,
            }
        )

    for m in _REVENUE_RE.finditer(full_text):
        amount = m.group("amount")
        key = f"revenue:{amount}"
        if key in seen_values:
            continue
        seen_values.add(key)
        metrics.append(
            {
                "type": "revenue_metric",
                "text": _clean(m.group(0)),
                "value": f"${amount}{m.group('scale') or ''}".strip(),
            }
        )

    for m in _FUNDING_RE.finditer(full_text):
        amount = m.group("amount")
        key = f"funding:{amount}"
        if key in seen_values:
            continue
        seen_values.add(key)
        scale = m.group("scale") or ""
        round_name = m.group("round") or ""
        value = f"${amount}{scale}" + (f" {round_name}" if round_name else "")
        metrics.append(
            {
                "type": "funding",
                "text": _clean(m.group(0)),
                "value": value.strip(),
            }
        )

    return metrics


def _extract_tech_claims(full_text: str) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    seen: set[str] = set()
    for pattern in (
        _PROPRIETARY_RE,
        _AI_POWERED_RE,
        _AI_TAG_RE,
        _SDK_PACKAGE_RE,
        _CUSTOM_MODEL_RE,
    ):
        for m in pattern.finditer(full_text):
            text = _clean(m.group(1))
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            claims.append({"type": "proprietary_tech", "text": text})
    return claims


def _extract_integrations(full_text: str) -> list[str]:
    m = _INTEGRATIONS_LEAD_RE.search(full_text)
    if not m:
        return []
    raw_list = m.group("list")
    parts = re.split(r",|\band\b", raw_list)
    return [p.strip() for p in parts if p.strip()]


def parse_company_page(html: str, url: str = "") -> dict[str, Any]:
    """Parse a product landing page HTML string into a raw company profile.

    Defensive by design (same discipline as parse_experience_html): malformed
    or unexpected markup degrades to partial data (missing fields left empty)
    rather than raising. Returns an empty-but-valid profile shape for blank
    input or if BeautifulSoup is unavailable.
    """
    try:
        from bs4 import BeautifulSoup  # lazy: not needed to import this module
    except ImportError:
        raise RuntimeError(
            "beautifulsoup4 is required to parse a company landing page "
            "(pip install beautifulsoup4)"
        )

    if not html or not html.strip():
        return _empty_profile(url)

    try:
        soup = BeautifulSoup(html, "html.parser")
        full_text = soup.get_text(" ")
    except Exception:
        return _empty_profile(url)

    profile = _empty_profile(url)
    try:
        name = _extract_product_name(soup)
        tagline = _extract_tagline(soup)
        profile["identity"] = {
            "name": name,
            "headline": tagline,
            "current_company": name,
            "location": "",
        }
    except Exception:
        pass

    try:
        profile["pricing"] = _extract_pricing(soup, full_text)
    except Exception:
        pass

    try:
        profile["metrics"] = _extract_metrics(full_text)
    except Exception:
        pass

    try:
        profile["tech_claims"] = _extract_tech_claims(full_text)
    except Exception:
        pass

    try:
        profile["integrations"] = _extract_integrations(full_text)
    except Exception:
        pass

    return profile


# ---------------------------------------------------------------------------
# Live fetch (gated, same protection pattern as extract_linkedin.fetch_profile)
# ---------------------------------------------------------------------------


def fetch_company(url: str, live: bool = False) -> dict[str, Any]:
    """Fetch a product landing page and parse it into a raw company profile.

    live=False (default) refuses to touch the network and raises, so tests
    and offline runs never accidentally make a real HTTP request. Pass
    live=True (wired to the CLI --company flag) to actually fetch.
    """
    if not live:
        raise RuntimeError(
            "fetch_company called with live=False. Live fetches are gated "
            "behind the --company flag. Use parse_company_page(html, url) "
            "for offline work, or --company-file for an offline raw profile."
        )

    try:
        import requests  # lazy
    except ImportError as exc:
        raise RuntimeError(
            "the 'requests' package is required for a live company fetch"
        ) from exc

    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    profile = parse_company_page(resp.text, url)
    # Extraction manifest: stamp the LIVE fetch (not parse_company_page itself,
    # which is also the offline / test path) so dossier.scan_depth classifies a
    # real company fetch as "full". An offline / --company-file profile carries
    # no manifest and is branded "injected" -> shallow by pipeline.run, exactly
    # like an injected person profile.
    profile["_extraction"] = {"method": "live_company_fetch"}
    return profile
