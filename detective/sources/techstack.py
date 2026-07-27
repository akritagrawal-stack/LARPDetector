"""Tech-stack fingerprint connector: is the product's front end built on a
no-code/low-code platform, or does it call a third-party LLM API directly
from the client, versus a genuinely custom stack.

THE "IS IT VIBECODEABLE" SIGNAL this connector exists for: a company charging
a premium price for "proprietary AI" whose landing page is actually a Bubble
app, or whose only "AI" is a client-side fetch to api.openai.com, is a real
buildability tell. This module fetches ONE page (the claimed company URL) and
fingerprints it. When WEB_RUNTIME_ENABLED=1 it also executes that same public
page once in bundled headless Chromium, without submitting forms or entering
credentials. There is no crawl of linked JS bundles and no APK
decompilation of any mobile-wrapped version of the product (that would need a
binary the pipeline never has access to; out of scope for this connector,
flagged here as future work, not attempted).

CRITICAL FRAMING (read before using this evidence): a "no-code detected"
result is an escalate-flag toward TRIVIAL buildability, a real and useful
signal. The REVERSE IS NOT TRUE. "custom stack, nothing detected" is NEVER
proof of real technical substance: the backend is completely invisible to a
front-end fetch, and a CDN or reverse proxy in front of a genuinely thin
wrapper can mask the true origin entirely. Treat a "custom_stack" or
"inconclusive" result as silence, not vindication.

Detection markers (single-page fetch, HTML + response headers only):
  - No-code / low-code builders: Bubble (bubble.io, appforest_uf asset
    bucket), Webflow (cdn.prod.website-files.com,
    assets.website-files.com, data-wf-page/data-wf-site attributes), Wix
    (parastorage.com, wixstatic.com, wix.com), Softr (softr.io,
    static.softr-files.com), Framer (framerusercontent.com), Carrd
    (carrd.co), Glide (glideapps.com), Adalo (adalo.com), Retool
    (retool.com, tryretool.com), and WordPress-with-a-visual-page-builder
    (WordPress itself, wp-content/wp-includes, PLUS a specific builder
    marker like elementor/divi/wpbakery/beaver-builder/oxygen/bricks; a
    bare hand-coded WordPress theme with no such marker is not flagged
    here, since that is not the "trivially rebuildable" signal this
    connector targets). A <meta name="generator" content="..."> tag, when
    present, is folded into the same combined search text as the asset
    paths above.
  - Thin-wrapper signal: a client-side (inline <script>) reference to a
    known LLM API endpoint (api.openai.com, api.anthropic.com,
    generativelanguage.googleapis.com, api.cohere.ai, api.mistral.ai,
    api.stability.ai, api.together.xyz, api.groq.com, openrouter.ai/api),
    found on a page with no no-code builder markers.

buildability_hint, one of:
  "no_code_detected"     : a known no-code/low-code builder marker matched.
                            Escalate toward TRIVIAL; match_confidence "medium".
  "llm_wrapper_signals"   : no builder marker, but a client-side LLM endpoint
                            reference was found. match_confidence "medium".
  "custom_stack"          : the page fetched real static content but no
                            marker of either kind matched. NEVER treat as
                            proof of substance (see CRITICAL FRAMING above).
                            match_confidence "low" (an absence-based read).
  "inconclusive"          : the fetched HTML is a near-empty client-rendered
                            shell (e.g. a bare SPA root div), so this
                            heuristic could not read anything meaningful from
                            the raw HTML at all. match_confidence "low".

match_confidence is "medium" at best for every record this connector
returns: this is a front-end heuristic over one fetched page, not a verified
identity or filing check, so it never reaches "high".

Public surface:
    verify_techstack(url) -> list[dict]

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence",
     "buildability_hint", "web_app_hint", "runtime_app_hint"}
    Both hints are additive to the standard 5-key shape the other connectors
    use; dedup/ranking in verify.py reads records via .get().

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import os
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

from detective.audit import AttemptLedger
from .registry import weight_for

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (tech-stack fingerprint check)"
_SOURCE_NAME = "techstack"
_MIN_VISIBLE_TEXT_CHARS = 100

_NO_CODE_MARKERS: dict[str, tuple[str, ...]] = {
    "Bubble": ("bubble.io", "appforest_uf", "window.bubble_fn", "meta.bubble"),
    "Webflow": (
        "cdn.prod.website-files.com",
        "assets.website-files.com",
        "data-wf-page",
        "data-wf-site",
    ),
    "Wix": ("parastorage.com", "wixstatic.com", "_wixcidx"),
    "Softr": ("softr.io", "static.softr-files.com"),
    "Framer": ("framerusercontent.com",),
    "Carrd": ("carrd.co",),
    "Glide": ("glideapps.com", "go.glideapps.com"),
    "Adalo": ("adalo.com", "adaloapp.com"),
    "Retool": ("retool.com", "tryretool.com"),
}

_WORDPRESS_MARKERS = ("wp-content", "wp-includes")
_PAGE_BUILDER_MARKERS = (
    "elementor",
    "wpbakery",
    "js_composer",
    "et-builder",
    "divi",
    "beaver-builder",
    "fl-builder",
    "bricks-builder",
    "oxygen-builder",
)

_LLM_ENDPOINT_MARKERS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.cohere.ai",
    "api.mistral.ai",
    "api.stability.ai",
    "api.together.xyz",
    "api.groq.com",
    "openrouter.ai/api",
)

_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_SCRIPT_SRC_RE = re.compile(
    r"<script[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE
)
_PASSWORD_INPUT_RE = re.compile(
    r"<input[^>]+type=[\"']password[\"']", re.IGNORECASE
)
_AUTH_FORM_RE = re.compile(
    r"<form[^>]+(?:action=[\"'][^\"']*(?:login|signin|session|auth)[^\"']*[\"']|"
    r"id=[\"'][^\"']*(?:login|signin|auth)[^\"']*[\"'])",
    re.IGNORECASE,
)
_APP_ROUTE_RE = re.compile(
    r"(?:href|action)=[\"']/"
    r"(?:app|login|signin|signup|dashboard|console|workspace|auth|session)(?:[/#?\"']|$)",
    re.IGNORECASE,
)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_APP_RUNTIME_MARKERS = (
    "/api/", "/graphql", "websocket", "wss://", "auth0", "clerk",
    "supabase", "firebase", "cognito", "next-auth", "oauth",
)
_PWA_MARKERS = (
    'rel="manifest"', "rel='manifest'", "serviceworker", "service-worker",
)


def _same_site_host(first: str, second: str) -> bool:
    """Conservative same-site check for a root domain and its subdomains."""
    first = (first or "").lower().strip(".")
    second = (second or "").lower().strip(".")
    if not first or not second:
        return False
    if first == second or first.endswith("." + second) or second.endswith("." + first):
        return True
    first_parts = first.split(".")
    second_parts = second.split(".")
    return (
        len(first_parts) >= 2
        and len(second_parts) >= 2
        and first_parts[-2:] == second_parts[-2:]
    )


class _SurfaceParser(HTMLParser):
    """Tolerant extraction of app-surface markers, including unquoted HTML."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.password_form = False
        self.auth_form = False
        self.app_route = False
        self.script_sources: list[str] = []
        self.pwa = False

    def _route_is_app(self, value: str) -> bool:
        try:
            parsed = urlparse(urljoin(self.base_url, value or ""))
            base = urlparse(self.base_url)
            if parsed.netloc and base.netloc and not _same_site_host(
                parsed.hostname or "", base.hostname or ""
            ):
                return False
            path = (parsed.path or "").lower()
        except Exception:
            path = ""
        first = path.strip("/").split("/", 1)[0]
        return first in {
            "app", "login", "signin", "signup", "dashboard", "console",
            "sign-in", "sign-up", "workspace", "auth", "session",
        }
    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {str(key).lower(): (value or "") for key, value in attrs}
        tag = (tag or "").lower()
        if tag == "input" and attrs_dict.get("type", "").lower() == "password":
            self.password_form = True
        elif tag == "form":
            form_identity = f"{attrs_dict.get('action', '')} {attrs_dict.get('id', '')}".lower()
            if any(token in form_identity for token in ("login", "signin", "session", "auth")):
                self.auth_form = True
            if self._route_is_app(attrs_dict.get("action", "")):
                self.app_route = True
        elif tag == "a" and self._route_is_app(attrs_dict.get("href", "")):
            self.app_route = True
        elif tag == "script" and attrs_dict.get("src"):
            self.script_sources.append(attrs_dict["src"])
        elif tag == "link" and "manifest" in attrs_dict.get("rel", "").lower():
            self.pwa = True


def _fetch(url: str) -> Optional[tuple[str, dict]]:
    """Raw HTML + response headers for one URL, or None on a non-200 or a
    non-text response. The only network call this module makes.
    """
    import requests  # lazy: keeps offline paths import-free

    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
        timeout=_TIMEOUT,
        allow_redirects=True,
    )
    if resp.status_code != 200:
        logger.warning("techstack: HTTP %d for %r", resp.status_code, url)
        return None
    return resp.text or "", dict(resp.headers)


def _combined_text(html: str, headers: dict) -> str:
    header_text = " ".join(f"{k}:{v}" for k, v in (headers or {}).items())
    return f"{html} {header_text}".lower()


def _detect_no_code_platforms(text: str) -> list[str]:
    hits = [name for name, markers in _NO_CODE_MARKERS.items() if any(m in text for m in markers)]
    if any(m in text for m in _WORDPRESS_MARKERS) and any(m in text for m in _PAGE_BUILDER_MARKERS):
        hits.append("WordPress page builder")
    return hits


def _detect_llm_endpoints(text: str) -> list[str]:
    return [ep for ep in _LLM_ENDPOINT_MARKERS if ep in text]


def _looks_like_empty_shell(html: str) -> bool:
    """True when the fetched HTML carries almost no visible static text
    (script/style tags stripped): a bare client-rendered SPA shell (e.g. a
    single <div id="root"></div>) that this heuristic simply cannot read
    anything meaningful from, since the real content only exists after a JS
    bundle this module never fetches or executes.
    """
    without_scripts = _SCRIPT_RE.sub(" ", html)
    without_styles = _STYLE_RE.sub(" ", without_scripts)
    text_only = _TAG_RE.sub(" ", without_styles)
    visible = _WHITESPACE_RE.sub(" ", text_only).strip()
    return len(visible) < _MIN_VISIBLE_TEXT_CHARS


def _web_app_surface(html: str, base_url: str = "") -> tuple[str, list[str]]:
    """Classify only the public application surface visible in raw HTML.

    This never claims the authenticated workflow or backend works. It separates
    a marketing page, a client shell, and a concrete authentication/application
    surface so a web-only product is not judged by App Store presence.
    """
    html = html or ""
    lower = html.lower()
    signals: list[str] = []
    parser = _SurfaceParser(base_url)
    try:
        parser.feed(html)
    except Exception:
        # Keep the regex fallbacks below useful for malformed pages.
        pass
    password_form = parser.password_form or bool(_PASSWORD_INPUT_RE.search(html))
    auth_form = parser.auth_form or bool(_AUTH_FORM_RE.search(html))
    app_route = parser.app_route or bool(_APP_ROUTE_RE.search(html))
    script_sources = parser.script_sources or _SCRIPT_SRC_RE.findall(html)
    app_bundle = any(
        marker in src.lower()
        for src in script_sources
        for marker in ("/assets/", "/static/js/", "/_next/static/", ".js")
    )
    runtime_marker = any(marker in lower for marker in _APP_RUNTIME_MARKERS)
    pwa = parser.pwa or any(marker in lower for marker in _PWA_MARKERS)

    if password_form:
        signals.append("password/auth form")
    elif auth_form:
        signals.append("authentication form")
    if app_route:
        signals.append("application route")
    if app_bundle:
        signals.append("application bundle")
    if runtime_marker:
        signals.append("API/auth runtime marker")
    if pwa:
        signals.append("PWA manifest/service-worker marker")

    if password_form or auth_form or (app_route and (app_bundle or runtime_marker)):
        return "interactive_surface", signals
    if _looks_like_empty_shell(html) and app_bundle:
        return "client_shell", signals
    if runtime_marker and app_bundle:
        return "interactive_surface", signals
    return "marketing_only", signals


def _web_app_snippet(url: str, hint: str, signals: list[str]) -> str:
    signal_text = ", ".join(signals) if signals else "no application markers"
    if hint == "interactive_surface":
        return (
            f"Web-app reality check for {url}: a public application/authentication "
            f"surface is present ({signal_text}). This is stronger than a landing "
            "page and does not depend on an App Store listing. It proves the public "
            "surface exists, but does not prove the authenticated workflow works, "
            "the backend is substantive, or the claimed user count is real."
        )
    if hint == "client_shell":
        return (
            f"Web-app reality check for {url}: a client application shell and bundle "
            f"are present ({signal_text}), but execution was not verified from raw "
            "HTML. This is evidence of a deployed front end, not a working product "
            "or substantive backend."
        )
    return (
        f"Web-app reality check for {url}: the fetch exposed a reachable marketing "
        f"surface only ({signal_text}). No public login or application surface was "
        "verified in the raw HTML. This is not proof that the product is fake: the "
        "app may live on another route/subdomain or behind an invite."
    )


def _runtime_probe(url: str) -> Optional[dict]:
    """Execute one public page in bundled headless Chromium.

    No form is submitted and no credential is entered. This checks that the
    deployed client renders, not that private workflows or backend claims work.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 900},
                user_agent=_USER_AGENT,
            )
            failed_requests = 0

            def on_failed(_request) -> None:
                nonlocal failed_requests
                failed_requests += 1

            page.on("requestfailed", on_failed)
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            page.wait_for_timeout(750)
            try:
                visible_text = page.locator("body").inner_text(timeout=3000)
            except Exception:
                visible_text = ""
            app_link_pattern = re.compile(
                r"/(?:app|login|signin|signup|sign-in|sign-up|dashboard|console|workspace|auth)(?:[/#?]|$)",
                re.IGNORECASE,
            )
            candidate_links: list[str] = []
            for href in page.locator("a[href]").evaluate_all(
                "els => els.slice(0, 200).map(el => el.getAttribute('href') || '')"
            ):
                if app_link_pattern.search(str(href or "")):
                    candidate_links.append(str(href))
            result = {
                "final_url": page.url,
                "status": int(response.status) if response is not None else 0,
                "title": page.title()[:200],
                "visible_text_chars": len(re.sub(r"\s+", " ", visible_text or "").strip()),
                "inputs": page.locator("input").count(),
                "password_inputs": page.locator('input[type="password"]').count(),
                "forms": page.locator("form").count(),
                "buttons": page.locator("button").count(),
                "app_links": 0,
                "script_count": page.locator("script[src]").count(),
                "failed_requests": failed_requests,
                "interaction_attempted": False,
                "interaction_succeeded": False,
                "interaction_route": "",
                "interaction_final_url": "",
                "interaction_status": 0,
                "interaction_visible_text_chars": 0,
                "interaction_inputs": 0,
                "interaction_password_inputs": 0,
                "interaction_buttons": 0,
            }
            origin = urlparse(page.url)
            safe_links: list[tuple[int, str]] = []
            priorities = {
                "app": 0,
                "dashboard": 1,
                "console": 2,
                "workspace": 3,
                "login": 4,
                "signin": 5,
                "signup": 6,
                "sign-in": 5,
                "sign-up": 6,
                "auth": 7,
            }
            for href in candidate_links:
                absolute = urljoin(page.url, href)
                parsed = urlparse(absolute)
                if parsed.scheme not in {"http", "https"} or not _same_site_host(
                    parsed.hostname or "", origin.hostname or ""
                ):
                    continue
                low = parsed.path.lower()
                if any(word in low for word in ("logout", "delete", "billing", "payment")):
                    continue
                rank = min(
                    (
                        value
                        for word, value in priorities.items()
                        if re.search(rf"/{word}(?:/|$)", low)
                    ),
                    default=99,
                )
                safe_links.append((rank, absolute))

            result["app_links"] = len(safe_links)
            if safe_links:
                route = sorted(safe_links, key=lambda item: (item[0], item[1]))[0][1]
                result["interaction_attempted"] = True
                result["interaction_route"] = route
                try:
                    target_response = page.goto(
                        route,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    page.wait_for_timeout(750)
                    try:
                        target_text = page.locator("body").inner_text(timeout=3000)
                    except Exception:
                        target_text = ""
                    target_status = (
                        int(target_response.status) if target_response is not None else 0
                    )
                    target_visible = len(
                        re.sub(r"\s+", " ", target_text or "").strip()
                    )
                    target_inputs = page.locator("input").count()
                    target_passwords = page.locator('input[type="password"]').count()
                    target_buttons = page.locator("button").count()
                    result.update(
                        {
                            "interaction_succeeded": (
                                target_status < 400
                                and target_visible >= 20
                                and (
                                    target_inputs > 0
                                    or target_buttons > 0
                                    or target_visible >= 100
                                )
                            ),
                            "interaction_final_url": page.url,
                            "interaction_status": target_status,
                            "interaction_visible_text_chars": target_visible,
                            "interaction_inputs": target_inputs,
                            "interaction_password_inputs": target_passwords,
                            "interaction_buttons": target_buttons,
                        }
                    )
                except Exception:
                    result["interaction_final_url"] = page.url
            return result
        finally:
            browser.close()


def _classify_runtime_probe(probe: dict) -> str:
    status = int((probe or {}).get("status") or 0)
    visible = int((probe or {}).get("visible_text_chars") or 0)
    failures = int((probe or {}).get("failed_requests") or 0)
    interactive = (
        int((probe or {}).get("password_inputs") or 0) > 0
        or int((probe or {}).get("app_links") or 0) > 0
        or (
            int((probe or {}).get("inputs") or 0) >= 2
            and int((probe or {}).get("forms") or 0) >= 1
            and int((probe or {}).get("buttons") or 0) >= 1
        )
    )
    if (probe or {}).get("interaction_succeeded"):
        return "interaction_verified"
    if status >= 400 or (visible < 10 and failures > 0):
        return "runtime_broken_or_blocked"
    if interactive:
        return "runtime_interactive"
    if visible >= 50 and int((probe or {}).get("script_count") or 0) > 0:
        return "runtime_rendered"
    return "runtime_marketing_only"


def _runtime_snippet(url: str, hint: str, probe: dict) -> str:
    facts = (
        f"HTTP {probe.get('status') or 'unknown'}, visible text: "
        f"{probe.get('visible_text_chars', 0)} chars, inputs: {probe.get('inputs', 0)}, "
        f"password inputs: {probe.get('password_inputs', 0)}, forms: "
        f"{probe.get('forms', 0)}, buttons: {probe.get('buttons', 0)}, "
        f"app/auth links: {probe.get('app_links', 0)}, failed requests: "
        f"{probe.get('failed_requests', 0)}"
    )
    if hint == "interaction_verified":
        conclusion = (
            "The browser followed a same-site app/auth route and verified that "
            "the destination rendered meaningful controls or content."
        )
    elif hint == "runtime_interactive":
        conclusion = (
            "The client rendered an interactive public application/auth surface. "
            "This is strong evidence that more than a static landing page is deployed."
        )
    elif hint == "runtime_rendered":
        conclusion = (
            "The JavaScript client rendered visible content, but no public interaction "
            "surface was verified."
        )
    elif hint == "runtime_broken_or_blocked":
        conclusion = (
            "The runtime failed or was blocked during this probe. This can indicate a "
            "broken deployment, but bot protection or a transient outage can look the same."
        )
    else:
        conclusion = (
            "The runtime rendered only a public marketing surface in this unauthenticated probe."
        )
    route_note = ""
    if probe.get("interaction_attempted"):
        route_note = (
            f" Tested route: {probe.get('interaction_route') or 'unknown'}; "
            f"landed on {probe.get('interaction_final_url') or 'unknown'} with "
            f"HTTP {probe.get('interaction_status') or 'unknown'}, "
            f"{probe.get('interaction_visible_text_chars', 0)} visible chars, "
            f"{probe.get('interaction_inputs', 0)} inputs, and "
            f"{probe.get('interaction_buttons', 0)} buttons."
        )
    return (
        f"Browser runtime check for {url}: executed in headless Chromium ({facts}). "
        f"{conclusion}{route_note} The check does not submit credentials, exercise private workflows, "
        "or prove backend quality, user counts, ownership, or revenue."
    )


def _discover_public_app_route(url: str) -> str:
    """Use the configured free search backend to find a same-site app route."""
    try:
        from detective.search import web_search

        host = urlparse(url).hostname or ""
        if not host:
            return ""
        rows = web_search(
            f"site:{host} (login OR sign-in OR app OR dashboard)",
            count=5,
        )
    except Exception:
        return ""
    route_pattern = re.compile(
        r"/(?:app|login|signin|signup|sign-in|sign-up|dashboard|console|workspace|auth)(?:[/#?]|$)",
        re.IGNORECASE,
    )
    for row in rows or []:
        candidate = (row.get("url") or "").strip()
        try:
            parsed = urlparse(candidate)
        except Exception:
            continue
        if (
            parsed.scheme in {"http", "https"}
            and _same_site_host(parsed.hostname or "", host)
            and route_pattern.search(parsed.path or "")
        ):
            return candidate
    return ""


def _build_snippet(url: str, platforms: list[str], llm_endpoints: list[str], hint: str) -> str:
    if hint == "no_code_detected":
        return (
            f"Tech-stack fingerprint for {url}: detected no-code/low-code builder "
            f"marker(s) for {', '.join(platforms)}. This is an escalate-flag toward "
            "TRIVIAL buildability, a real and useful signal, but it does not by "
            "itself prove there is no backend logic anywhere else."
        )
    if hint == "llm_wrapper_signals":
        return (
            f"Tech-stack fingerprint for {url}: no no-code builder marker found, but "
            f"the fetched HTML contains client-side reference(s) to {', '.join(llm_endpoints)}. "
            "A mostly-static front end calling an existing LLM API directly is a real "
            "thin-wrapper signal, though not proof on its own since server-side "
            "proxying could still exist."
        )
    if hint == "custom_stack":
        return (
            f"Tech-stack fingerprint for {url}: no no-code builder marker and no "
            "client-side LLM endpoint reference found in the fetched HTML. This is "
            "NOT proof of a real custom backend or genuine technical substance: the "
            "backend is invisible to a front-end fetch, and a CDN or reverse proxy "
            "can mask the true origin entirely. Absence of a marker here is silence, "
            "not vindication."
        )
    # inconclusive
    return (
        f"Tech-stack fingerprint for {url}: the fetched HTML is a near-empty "
        "client-rendered shell (minimal static content), so this heuristic could "
        "not read anything meaningful from the raw HTML alone. Not evidence either "
        "way."
    )


def verify_techstack(
    url: str,
    *,
    attempt_ledger: Optional[AttemptLedger] = None,
    claim_index: Optional[int] = None,
) -> list[dict]:
    """Single-page fingerprint check for one claimed company/product URL.

    Returns a single-record list carrying a buildability_hint (one of
    "no_code_detected", "llm_wrapper_signals", "custom_stack",
    "inconclusive"), or [] if the URL is blank or the fetch failed. Never
    raises.

    match_confidence is "medium" for a positive detection (a no-code builder
    marker or an LLM-endpoint reference actually matched), "low" for an
    absence-based read (custom_stack, inconclusive): this connector never
    reaches "high", since it is a front-end heuristic over one fetched page,
    not a verified identity or filing check.
    """
    url = (url or "").strip()
    if not url:
        return []

    try:
        fetched = _fetch(url)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("techstack: fetch failed for %r: %s", url, exc)
        return []
    if fetched is None:
        return []

    html, headers = fetched
    combined = _combined_text(html, headers)

    platforms = _detect_no_code_platforms(combined)
    llm_endpoints = _detect_llm_endpoints(combined)
    web_app_hint, web_app_signals = _web_app_surface(html, url)

    if platforms:
        hint = "no_code_detected"
        confidence = "medium"
    elif llm_endpoints:
        hint = "llm_wrapper_signals"
        confidence = "medium"
    elif _looks_like_empty_shell(html):
        hint = "inconclusive"
        confidence = "low"
    else:
        hint = "custom_stack"
        confidence = "low"
    if web_app_hint == "interactive_surface" and confidence == "low":
        confidence = "medium"

    runtime_hint = "not_run"
    runtime_note = ""
    runtime_enabled = os.environ.get("WEB_RUNTIME_ENABLED", "").strip().lower()
    if runtime_enabled in {"1", "true", "yes", "on"}:
        runtime_attempt = (
            attempt_ledger.attempt(
                "runtime_interaction",
                "playwright_chromium",
                claim_index=claim_index,
                target=url,
            )
            if attempt_ledger is not None
            else None
        )
        try:
            runtime_probe = _runtime_probe(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("techstack: browser runtime probe failed for %r: %s", url, exc)
            runtime_probe = None
            if runtime_attempt is not None:
                runtime_attempt.finish(
                    "error", error=f"{type(exc).__name__}: {exc}"
                )
        if runtime_probe:
            runtime_hint = _classify_runtime_probe(runtime_probe)
            if runtime_hint not in {"interaction_verified", "runtime_interactive"}:
                discovery_attempt = (
                    attempt_ledger.attempt(
                        "runtime_route_discovery",
                        "web_search",
                        claim_index=claim_index,
                        target=url,
                    )
                    if attempt_ledger is not None
                    else None
                )
                discovered_route = _discover_public_app_route(url)
                if discovery_attempt is not None:
                    discovery_attempt.finish(
                        "completed" if discovered_route else "completed_empty",
                        result_count=1 if discovered_route else 0,
                        final_url=discovered_route,
                    )
                if discovered_route:
                    try:
                        discovered_probe = _runtime_probe(discovered_route)
                    except Exception:
                        discovered_probe = None
                    discovered_hint = (
                        _classify_runtime_probe(discovered_probe)
                        if discovered_probe
                        else "unavailable"
                    )
                    if discovered_probe and discovered_hint in {
                        "runtime_interactive",
                        "interaction_verified",
                    }:
                        discovered_probe.update(
                            {
                                "interaction_attempted": True,
                                "interaction_succeeded": True,
                                "interaction_route": discovered_route,
                                "interaction_final_url": discovered_probe.get(
                                    "final_url", discovered_route
                                ),
                                "interaction_status": discovered_probe.get(
                                    "status", 0
                                ),
                                "interaction_visible_text_chars": discovered_probe.get(
                                    "visible_text_chars", 0
                                ),
                                "interaction_inputs": discovered_probe.get(
                                    "inputs", 0
                                ),
                                "interaction_password_inputs": discovered_probe.get(
                                    "password_inputs", 0
                                ),
                                "interaction_buttons": discovered_probe.get(
                                    "buttons", 0
                                ),
                            }
                        )
                        runtime_probe = discovered_probe
                        runtime_hint = "interaction_verified"
            runtime_note = " " + _runtime_snippet(url, runtime_hint, runtime_probe)
            if runtime_hint in {"runtime_interactive", "interaction_verified"}:
                confidence = "medium"
            if runtime_attempt is not None:
                runtime_attempt.finish(
                    "completed",
                    result_count=1,
                    final_url=runtime_probe.get("interaction_final_url")
                    or runtime_probe.get("final_url")
                    or "",
                    metadata={
                        "classification": runtime_hint,
                        "tested_route": runtime_probe.get("interaction_route") or "",
                        "interaction_attempted": bool(
                            runtime_probe.get("interaction_attempted")
                        ),
                        "interaction_succeeded": bool(
                            runtime_probe.get("interaction_succeeded")
                        ),
                    },
                )
        else:
            runtime_hint = "unavailable"
            if runtime_attempt is not None and not runtime_attempt.finished:
                runtime_attempt.finish("unavailable")

    return [
        {
            "source_url": url,
            "snippet": (
                _build_snippet(url, platforms, llm_endpoints, hint)
                + " "
                + _web_app_snippet(url, web_app_hint, web_app_signals)
                + runtime_note
            ),
            "source_name": _SOURCE_NAME,
            "weight": weight_for(_SOURCE_NAME),
            "match_confidence": confidence,
            "buildability_hint": hint,
            "web_app_hint": web_app_hint,
            "runtime_app_hint": runtime_hint,
        }
    ]
