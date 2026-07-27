"""Best-effort image helpers for the rich scan stream (hero + thumbnails).

Three honest image sources feed the overlay's image-first searching UI, and
every one of them is BOUNDED and FAILURE-SILENT: a miss returns None (or an
empty string) and the overlay degrades to a monogram, never an error. None of
these ever raise to the caller.

    1. proxy_image_as_data_uri(url)   the LinkedIn identity photo, fetched
        SERVER-SIDE with browser-like headers (+ a linkedin.com Referer, +
        session cookies when available) and returned as a `data:` URI, so the
        overlay does not hotlink media.licdn.com directly (which 403s).
    2. clearbit_logo_url / guess_company_domain   a company logo URL from a
        best-effort domain (no auth, no fetch: Clearbit 404s harmlessly for an
        unknown domain, which the overlay degrades to a monogram).
    2b. favicon_logo_url   a SECOND, independently-reliable company image
        source, used by service.py as a client-side fallback when the
        Clearbit URL fails to load. Confirmed by hand (curl + nslookup, not
        just a code review) that the public logo.clearbit.com host currently
        returns NXDOMAIN, so relying on Clearbit alone silently starves the
        overlay of every company/employer image and leaves only a monogram.
        Google's public, no-key s2/favicons service still resolves and is
        requested here at sz=256, the largest size that service honors (a
        bigger sz does not buy a higher-resolution source; Google just pads
        or upscales its own small icon to fill the request). A favicon is
        STILL a favicon: even at 256 it is inherently a small, low-detail
        mark, not a photograph. Requesting the largest size only gives the
        overlay the least-bad source to downscale from; it does not make a
        favicon safe to stretch across a big hero tile. The overlay side
        (SearchingView.jsx) is responsible for rendering any favicon/logo
        source CONTAINED and capped small, never object-fit: cover'd to fill
        a large tile, which is what produces a blurry stretched blob.
    3. fetch_og_image(url)   an article/company thumbnail: the page's og:image
        (or twitter:image) meta and its title, one short bounded fetch.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# A plain, current desktop-Chrome User-Agent. media.licdn.com's hotlink
# protection 403s a bare requests default UA / missing Referer, so both are
# always sent on the profile-photo proxy below.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_LINKEDIN_REFERER = "https://www.linkedin.com/"

# Never treat these as a company's OWN domain when slug-guessing a logo.
_EMPLOYER_SUFFIX_WORDS = ("incorporated", "inc", "llc", "corp", "co", "ai", "labs")

# Bound the amount of arbitrary external HTML we parse for an og:image meta.
_MAX_OG_HTML_CHARS = 200_000
# Bound the proxied-photo download so a mis-served huge body cannot blow up.
_MAX_PHOTO_BYTES = 5_000_000


def slugify_company(name: str) -> str:
    """Lowercase, alphanumeric-only company slug with a trailing legal/suffix
    word stripped ("Acme Labs, Inc." -> "acme"). "" for an empty name.

    Best-effort only: this is the base for a GUESSED logo domain, and a wrong
    guess is harmless (Clearbit 404 -> overlay monogram).
    """
    slug = "".join(ch for ch in (name or "").lower() if ch.isalnum() or ch == " ")
    parts = [p for p in slug.split() if p]
    # Strip a trailing legal/marketing suffix word ("inc", "llc", "labs", ...)
    # but never strip the whole thing away (a one-word "Labs" stays "labs").
    while len(parts) > 1 and parts[-1] in _EMPLOYER_SUFFIX_WORDS:
        parts.pop()
    return "".join(parts)


def guess_company_domain(name: str) -> str:
    """Best-effort <slug>.com guess for a company with no known website. "" for
    an empty/uslugifiable name. Never authoritative: only ever used to build a
    Clearbit logo URL, which 404s harmlessly on a wrong guess.
    """
    slug = slugify_company(name)
    return f"{slug}.com" if slug else ""


def clearbit_logo_url(domain: str) -> str:
    """The Clearbit logo URL for a domain. https, no auth, loads fine in the
    overlay; returns "" for an empty domain so no bogus URL is ever emitted.
    """
    domain = (domain or "").strip().lower()
    if domain.startswith("http://"):
        domain = domain[7:]
    elif domain.startswith("https://"):
        domain = domain[8:]
    if domain.startswith("www."):
        domain = domain[4:]
    domain = domain.split("/")[0].strip()
    return f"https://logo.clearbit.com/{domain}" if domain else ""


def favicon_logo_url(domain: str, size: int = 256) -> str:
    """A second, independently-reliable "company image" URL for a domain,
    used as the client-side fallback when clearbit_logo_url's host fails to
    load (see the module docstring: logo.clearbit.com currently returns
    NXDOMAIN). Google's public, no-auth s2/favicons service, requested at
    sz=256: the practical ceiling for that endpoint (a larger sz does not
    return a higher-resolution source, it just has Google pad/upscale its own
    small icon further). Even at the largest size Google offers, this is
    still a FAVICON, i.e. inherently low-resolution: the overlay must render
    it small and contained (object-fit: contain, capped near its native
    size), never stretched to fill a big hero/thumbnail tile, or it blurs
    into an upscaled blob. "" for an empty domain, so no bogus URL is ever
    emitted; never raises.
    """
    domain = (domain or "").strip().lower()
    if domain.startswith("http://"):
        domain = domain[7:]
    elif domain.startswith("https://"):
        domain = domain[8:]
    if domain.startswith("www."):
        domain = domain[4:]
    domain = domain.split("/")[0].strip()
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?domain={domain}&sz={size}"


def load_linkedin_cookies() -> Optional[dict]:
    """Best-effort {name: value} of the linkedin.com cookies from the same
    Playwright storage_state file extract_linkedin bridges from
    (LINKEDIN_STATE_PATH). None on any miss (unset/missing/unreadable/no
    linkedin cookies). Never raises, never logs cookie VALUES (only a count).
    """
    state_path = os.environ.get("LINKEDIN_STATE_PATH", "").strip()
    if not state_path:
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        raw = state.get("cookies", []) if isinstance(state, dict) else []
        jar = {
            c["name"]: c["value"]
            for c in raw
            if "linkedin" in (c.get("domain") or "") and c.get("name") and c.get("value") is not None
        }
        if not jar:
            return None
        logger.info("images: loaded %d linkedin cookie(s) for the photo proxy", len(jar))
        return jar
    except Exception:
        logger.info("images: no usable linkedin cookies for the photo proxy (falling back to headers only)")
        return None


def proxy_image_as_data_uri(
    url: str,
    cookies: Optional[dict] = None,
    timeout: float = 5.0,
) -> Optional[str]:
    """Fetch an image SERVER-SIDE and return it as a `data:<mime>;base64,...`
    URI, so the overlay never hotlinks a media.licdn.com URL that 403s.

    Sends a browser-like User-Agent AND a linkedin.com Referer (the two things
    licdn's hotlink protection checks), plus session cookies when supplied.
    Returns None on ANY failure or non-image response (a miss is graceful: the
    overlay already has a monogram fallback). Never raises.
    """
    if not url or not url.strip():
        return None
    try:
        headers = {
            "User-Agent": _BROWSER_UA,
            "Referer": _LINKEDIN_REFERER,
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
        }
        resp = requests.get(url.strip(), headers=headers, cookies=cookies, timeout=timeout)
        if resp.status_code != 200:
            return None
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            return None
        data = resp.content or b""
        if not data or len(data) > _MAX_PHOTO_BYTES:
            return None
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{content_type};base64,{b64}"
    except Exception:
        # Silent on purpose: a photo miss must never look like a scan error.
        logger.info("images: profile-photo proxy failed for a url (graceful monogram fallback)")
        return None


def extract_og_image(html: str, base_url: str = "") -> Optional[str]:
    """The page's og:image (preferred) or twitter:image meta content, absolute
    when a base_url is given, else None. Uses beautifulsoup4 (already a dep).
    Never raises.
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup  # lazy: keeps import cost off cold paths

        soup = BeautifulSoup(html[:_MAX_OG_HTML_CHARS], "html.parser")
        for attr, key in (
            ("property", "og:image"),
            ("property", "og:image:url"),
            ("name", "twitter:image"),
            ("name", "twitter:image:src"),
        ):
            tag = soup.find("meta", attrs={attr: key})
            content = (tag.get("content") if tag else "") or ""
            content = content.strip()
            if content:
                if base_url and content.startswith("/"):
                    from urllib.parse import urljoin

                    return urljoin(base_url, content)
                return content
        return None
    except Exception:
        logger.info("images: og:image extraction failed for a page (skipped)")
        return None


def extract_og_title(html: str) -> Optional[str]:
    """The page's og:title (preferred) or <title> text, trimmed, else None.
    Never raises.
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html[:_MAX_OG_HTML_CHARS], "html.parser")
        tag = soup.find("meta", attrs={"property": "og:title"})
        content = ((tag.get("content") if tag else "") or "").strip()
        if content:
            return content
        if soup.title and soup.title.string:
            return soup.title.string.strip() or None
        return None
    except Exception:
        return None


def fetch_og_image(url: str, timeout: float = 4.0) -> Optional[Tuple[str, str]]:
    """Fetch a page and return (og_image_url, page_title) or None.

    Bounded: one short-timeout GET, at most _MAX_OG_HTML_CHARS of HTML parsed.
    Skips obvious non-HTML responses. Returns None on any failure or when the
    page has no og:image/twitter:image. Never raises.
    """
    if not url or not url.strip():
        return None
    try:
        headers = {
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
        }
        resp = requests.get(url.strip(), headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and "html" not in content_type:
            return None
        html = resp.text or ""
        og_image = extract_og_image(html, base_url=url.strip())
        if not og_image:
            return None
        title = extract_og_title(html) or url.strip()
        return (og_image, title)
    except Exception:
        logger.info("images: og:image fetch failed for a page (skipped)")
        return None
