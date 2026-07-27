"""Local FastAPI + websocket service wrapping the LARP-detector pipeline.

Runs on http://127.0.0.1:8756 (see service_run.py).
Built for a Cluely-style overlay: the overlay POSTs a scan, gets a job_id
back, then opens a websocket to stream live progress and the final verdict.

API CONTRACT:

  POST /scan
    body: {"url": str|null, "screenshot_b64": str|null,
           "scan_type": "person"|"company_app", "platform": str|null,
           "extract_from_screenshot": bool}
    (an extra optional "demo": true is also accepted as a second way to
    request the demo path; "url": "demo" does the same thing)
    -> {"job_id": str}
    Starts the pipeline in a background asyncio task.

    extract_from_screenshot (the overlay's "Go" button fallback, layer 2 of
    3; see overlay/electron/main.js's getActiveBrowserUrl for layer 1 and
    the overlay's manual URL input for layer 3): when true and no usable url
    is supplied, the screenshot is run through the provider's vision_extract
    (see llm.LLMProvider.vision_extract) instead of a plain scrape. If that
    yields a profile_url, the normal live scrape -> verify -> verdict flow
    runs on it, exactly like a supplied url. If it yields only a name (and
    maybe a company), that name is resolved to a LinkedIn URL via a web
    search (search.web_search, the same connector verify.py's evidence
    gathering already uses) before falling back to a normal live scrape.
    Ignored (a no-op) when url is also supplied, since the primary path
    (an exact URL, e.g. from the overlay's Windows UI-Automation read) never
    needs vision at all.

  WS /events/{job_id}
    Streams one JSON object per line, in this vocabulary:
      {"type":"status","text": "..."}
      {"type":"thought","text": "..."}                     one short running
        reasoning line per claim as evidence is gathered (see
        _thought_for_claim), the "showing its work" narration.
      {"type":"website","url":"...","title":"...","favicon":"...","domain":"..."}
        one card per distinct source domain the scan is checking, derived
        from that claim's evidence source_urls (see _website_events_for_claim).
        Deduped by domain, capped at _MAX_WEBSITE_CARDS_PER_SCAN (6) per scan.
      {"type":"image","url": "...", "caption":"...", "fallback": "...",
       "kind": "logo"|"photo", "hero": bool}
        a real image: the company logo (Clearbit, by domain), a captured
        LinkedIn profile photo, or a source-domain favicon thumbnail. Never a
        fabricated URL; see _emit_company_hero / _emit_person_hero_if_captured
        / _website_events_for_claim for exactly when each is derived.
        "fallback" is OPTIONAL and only ever set on a Clearbit-sourced logo
        image (the company hero and per-employer logos): a second,
        independently-reliable image URL (images.favicon_logo_url) the
        overlay swaps to if "url" fails to load client-side. Added because
        logo.clearbit.com's public endpoint is confirmed dead (NXDOMAIN) as
        of this writing, which otherwise silently starved the overlay of
        every company/employer image and left only the profile photo.
        "kind" tells the overlay how to RENDER the image, not just what it
        depicts: "logo" is a favicon/Clearbit mark, inherently low-res, and
        must be shown small and contained (never stretched to fill a big
        tile, which is what used to blur every company/employer image into
        an upscaled blob); "photo" is a real photograph or page screenshot
        (the proxied LinkedIn photo, an og:image) and may fill a tile via
        object-fit: cover. Omitted/anything else defaults to "photo" on the
        overlay side, matching mock-mode's fixture images.
        "hero" tells the overlay whether this image event is ALLOWED to
        become the big hero tile at all. Only the three genuine hero sources
        (_emit_company_hero, _emit_person_hero_if_captured, and the final
        proxied photo in _verdict_image_events) ever set it True; every
        thumbnail-only source (per-employer logos, source favicons,
        og:image thumbnails) sets it False so it can only ever land in the
        small thumbnail strip. Before this field existed, the overlay just
        treated the MOST RECENT image event as the hero, so a thumbnail-only
        favicon or og:image arriving after the real hero would silently hijack
        the big tile and then hand it back, which is what produced the hero
        flickering between a company/employer logo and the profile photo.
        Omitted defaults to True on the overlay side (mock-mode's fixture
        sequence relies on every image it sends becoming the hero in turn).
      {"type":"claim","assertion":"...","tier":"DISPROVEN|UNVERIFIED|CONFIRMED"}
      {"type":"scores","overall_larp_score": int|null,
                        "founder_larp_score": int|null,
                        "company_larp_score": int|null,
                        "company_assessments": [...],
                        "scan_depth": "full"|"shallow"}
      {"type":"verdict","text":"..."}
      {"type":"needs_url","text":"..."}
      {"type":"done"}
      {"type":"error","text":"..."}
    "done" is always the last message. An "error" is always followed by
    "done". The socket never raises to the caller; every failure is turned
    into an error+done pair.
    A "needs_url" is emitted (followed by "done", never a verdict) when a URL
    could NOT be confirmed for a screenshot scan: the vision read found no
    address-bar URL and no confident name resolution, the vision queue timed
    out, or a name-resolved profile's scraped name did not match the person on
    screen (the identity gate). It is distinct from "error" (a dead verdict
    card): needs_url returns the overlay to the paste field so the operator is
    one paste away from a full scan. This is the "no silent shallow scan"
    contract: a scored SUS verdict is only ever produced from a confirmed URL
    plus a full extraction, never from thin or wrong-person data.
    "scan_depth" on the scores event brands a degraded scan: "shallow" means an
    injected/dev profile or a live scrape that parsed zero experience, which the
    engine forbids from accruing absence-based suspicion (see dossier.scan_depth
    and compute_founder_score's scan_depth gate). The overlay renders a
    "SHALLOW SCAN" badge and de-emphasizes the number so it can never be
    screenshotted as a real finding.

BRAIN / SCORING (the reasoning step)
  Default brain is the existing ManualProvider queue flow (see llm.py): the
  pipeline writes a job file to queue/<job_id>.json (using this service's own
  job_id as the ManualProvider job_id, so the file to watch is always
  queue/<job_id>.json) and returns an UNSCORED dossier immediately
  (MANUAL_QUEUE_TIMEOUT_S=0 default). This service then watches that same
  file for status == "completed" (a human or fresh Codex reviewer fills in
  judgment fields and flips status), restores the mechanical scan evidence,
  applies reasoning safety, and only then computes
  founder_larp_score / company_larp_score / overall_larp_score (mirroring
  pipeline.run's own finalize step exactly, see _finalize_scores) and emits
  scores + verdict + done.

  Set LARP_SERVICE_PROVIDER=api (with GEMINI_API_KEY configured) to route a
  REAL (non-demo) scan through ApiProvider (Gemini) instead of the queue
  file; see _select_provider. If the Gemini call fails for any reason (no
  key, quota/exhausted, network, an unparseable or incomplete response),
  ApiProvider raises ApiProviderError (see llm.py); _run_job catches that
  specifically and falls back to the ManualProvider queue flow, re-running
  the pipeline so the job's queue file actually gets written. A real scan
  therefore never dead-ends on a Gemini failure: worst case it degrades to
  the same $0 human-in-the-loop path a key-less run always uses.

DEMO PATH (offline, no network, no login; see _build_demo_profile /
  _auto_complete_demo_job)
  POST /scan with {"url": "demo"} (or "demo": true) runs the pipeline on the
  bundled fixture profile instead of a live fetch, exactly like the CLI's
  --demo flag. The demo path ALWAYS uses ManualProvider regardless of any
  configured API key (kept hermetic on purpose, so the demo/test path never
  depends on environment state), and this service auto-completes the queue
  job itself with a small, clearly-labeled heuristic scorer (never used for
  a real scan) so the socket reaches scores/verdict/done with no human in
  the loop. This is what tests/test_service.py exercises.

Live scraping is gated exactly as it always was: a real (non-demo) url goes
through pipeline.run(..., live=True), which still refuses ANY network fetch
unless the operator explicitly asked for a live scan by supplying a real
url in the first place (extract_linkedin.fetch_profile / extract_company.
fetch_company raise cleanly on their own gate; that gate is untouched here).

RICH EVENTS (thought / website / image; the "showing its work" panel)
  These are presentation-layer, derived here in service.py (not pipeline.py,
  which stays pipeline-shape-only), from data pipeline.run already hands to
  the progress callback: pipeline.run's own "claim" event fires right after
  verify.gather_evidence populates claim.evidence, so that same evidence is
  what website cards and image thumbnails are built from, no new fetch.
    - "thought": templated per claim from claim.type / claim.employer / what
      is being checked (see _thought_for_claim). Always emitted, evidence or
      not, since it is a narration line, not a finding.
    - "website": one card per distinct evidence source_url domain, deduped
      and capped at _MAX_WEBSITE_CARDS_PER_SCAN (6) for the whole scan (see
      _website_events_for_claim). favicon is always
      https://www.google.com/s2/favicons?domain={domain}&sz=64, a public,
      no-key, real service.
    - "image": three honest sources, never fabricated.
        (a) HERO for a company_app scan: Clearbit logo
            (https://logo.clearbit.com/{domain}) of the scanned url's own
            domain, known up front (_emit_company_hero), with a
            "fallback" (images.favicon_logo_url) riding along since Clearbit's
            public logo host is confirmed dead; the overlay swaps to it
            client-side rather than degrading straight to a monogram. The
            same fallback rides along on every per-employer logo emitted by
            _employer_logo_events_for_claim (person scans).
        (b) HERO for a person scan: the LinkedIn profile photo URL, ONLY
            when extract_linkedin actually captured one (identity["image"];
            see _emit_person_hero_if_captured for the demo/up-front case and
            the "verdict" branch of _pipeline_event_to_messages for the live
            case, where identity is only known once pipeline.run returns
            the assembled Dossier). Falls back to a same-domain-as-employer
            match found IN evidence (_derive_own_domain_from_evidence) only
            when no photo was captured; skipped entirely otherwise. A person
            scan with no captured photo and no matching evidence domain gets
            no hero at all, on purpose: this code never invents a logo URL
            from a bare employer name.
        (c) source thumbnails: favicons of the first few distinct evidence
            domains, capped at _MAX_THUMBNAILS_PER_SCAN (3) for the scan.
  DEMO PATH note: the demo path's own claims gather zero real evidence
  offline (search.py returns [] with no SEARXNG_URL/BRAVE_API_KEY
  configured), so _emit_demo_rich_sequence separately injects a fixed,
  clearly-demo-only thought/website/image sequence using real, public,
  no-key favicon/logo URLs against plausible domains, so the "showing its
  work" panel has something to show even fully offline.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import images
from . import pipeline
from . import search
from .llm import (
    ApiProvider,
    ApiProviderError,
    CodexProvider,
    ManualProvider,
    QUEUE_DIR,
    finalize_dossier_scores,
)
from .models import Dossier, EvidenceTier

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8756

# How often the queue-watch loop polls the job file on disk, and how often
# (at most) it surfaces a "still waiting" status line while polling, so a
# real (non-demo) scan waiting on a slow human operator does not flood the
# socket with a status line every poll tick.
_QUEUE_POLL_INTERVAL_S = 0.25
_QUEUE_STATUS_EVERY_S = 5.0

# Safety net so a forgotten job never keeps a background task alive forever.
# Configurable; large by default since a real operator may take a while.
_QUEUE_TIMEOUT_S = float(os.environ.get("SERVICE_QUEUE_TIMEOUT_S", "1800"))

# Bounded wait for the OPTIONAL director / planning round trip on a live
# ManualProvider scan (see _PlanWaitingManualProvider). Deliberately much
# smaller than _QUEUE_TIMEOUT_S: the plan step is optional (the scan proceeds
# with no follow-ups if it is not filled), so a stalled or ignored plan job
# must not add a long delay before the REQUIRED scoring step. Read from the
# module attribute at construction time (not baked into a default arg) so a
# test can monkeypatch service._PLAN_QUEUE_TIMEOUT_S and have it take effect.
_PLAN_QUEUE_TIMEOUT_S = float(os.environ.get("SERVICE_PLAN_TIMEOUT_S", "180"))

# Bounded wait for the OPTIONAL product-site resolution round trip (stage 1.5).
# Same reasoning as the plan wait, and MORE urgent: resolution runs in FRONT of
# the whole evidence gather, so an unfilled resolve job delays everything the
# user can see. Optional by construction (an unresolved product just means the
# URL-keyed connectors do not fire), so it must never hold a scan long.
_RESOLVE_QUEUE_TIMEOUT_S = float(os.environ.get("SERVICE_RESOLVE_TIMEOUT_S", "180"))

# How long a finished job's _job_queues entry survives after its background
# task completes, so a websocket client that is slow to connect (or is
# reconnecting after an earlier drop, see events()'s finally block) can still
# drain the final scores/verdict/done events instead of hitting "unknown
# job_id". After this, the queue is dropped regardless, so a job whose
# websocket client never connects at all cannot leak forever (see scan()'s
# task done_callback).
_JOB_QUEUE_GRACE_PERIOD_S = 300.0

# How often the per-scan heartbeat emits a keepalive frame onto the socket.
# During a live scan the pipeline runs on a worker thread (run_in_executor) and
# streams nothing for long stretches (evidence gathering, then multi-minute
# operator waits), so an idle client drops the connection ~1 to 2 minutes in and
# the final verdict/score never lands. A periodic no-op keepalive keeps the
# socket warm through the whole scan. 15s is comfortably under a typical idle
# timeout without adding meaningful traffic.
_HEARTBEAT_INTERVAL_S = 15.0

# Which detection engine a real (or demo) scan runs, forwarded to
# pipeline.run's `engine` param. Default "dossier": the hardened
# aggregate-then-mismatch engine (detective.dossier.build_dossier). Its
# calibration fixtures are regression coverage, not proof of real-world
# accuracy. Set LARP_ENGINE=per_claim to roll back to the original
# serial per-claim path (detective.pipeline.run's own steps), so A/B and a
# one-env-var rollback stay possible. Read here (a service-level concern,
# mirroring LARP_SERVICE_PROVIDER) rather than inside pipeline.run, whose own
# default stays per_claim for compatibility with direct callers. The CLI
# explicitly defaults to dossier. Both
# engines fetch through the SAME gated fetchers and emit the SAME
# status/claim/verdict event vocabulary, so the cinematic image layer and the
# ManualProvider/ApiProvider paths below work identically under either.
_VALID_ENGINES = ("dossier", "per_claim")


def _select_engine() -> str:
    engine = os.environ.get("LARP_ENGINE", "dossier").strip().lower()
    return engine if engine in _VALID_ENGINES else "dossier"


_DEMO_COMPANY_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests" / "cases" / "wrapper_app.json"
)

_DEMO_AUTO_SCORE_NOTE = (
    "Auto-scored by the demo path's built-in heuristic scorer (not a real "
    "reasoning pass); this note only ever appears on a demo/test run."
)

app = FastAPI(title="LARP Detector Service")

# Local-only tool; permissive CORS so an Electron/web overlay on another
# origin can call http://127.0.0.1:8756 without extra configuration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One asyncio.Queue per in-flight job_id, drained by the matching websocket.
_job_queues: dict[str, "asyncio.Queue[dict]"] = {}

# Keeps a strong reference to each job's background task so it cannot be
# garbage-collected mid-flight (a bare, unstored asyncio.create_task(...) is
# only weakly referenced by the loop while pending; this dict is that
# reference). Popped once the task finishes, whether it errors or not.
_job_tasks: dict[str, "asyncio.Task"] = {}

# The browser companion publishes only the active LinkedIn profile URL. Keep
# it in memory, never on disk, and expire it so a tab from an old browser
# session cannot become a later scan target.
_active_browser_tab: dict[str, Any] = {}
_BROWSER_TAB_MAX_AGE_S = 300.0
_browser_companion_presence: dict[str, Any] = {}
_BROWSER_COMPANION_MAX_AGE_S = 120.0


class ScanRequest(BaseModel):
    url: Optional[str] = None
    screenshot_b64: Optional[str] = None
    scan_type: str = "person"
    platform: Optional[str] = None
    # Extra, optional: a second way to request the demo path besides
    # url == "demo". Not part of the strict 4-field contract but harmless to
    # accept; the UI agent can use whichever is more convenient.
    demo: bool = False
    # The overlay's "Go" button, layer 2: when true and no usable url is
    # given, screenshot_b64 is read via the provider's vision_extract
    # instead of being scraped directly. See the module docstring above.
    extract_from_screenshot: bool = False


class ScanResponse(BaseModel):
    job_id: str


class BrowserTabUpdate(BaseModel):
    url: str
    browser: Optional[str] = None


class BrowserCompanionHeartbeat(BaseModel):
    browser: Optional[str] = None


def _is_linkedin_profile_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (host == "linkedin.com" or host.endswith(".linkedin.com")) and parsed.path.startswith(
        "/in/"
    )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "project_root": str(Path(__file__).resolve().parent.parent),
    }


@app.post("/browser-tab")
async def update_browser_tab(req: BrowserTabUpdate) -> dict:
    if not _is_linkedin_profile_url(req.url):
        return {"accepted": False}
    _browser_companion_presence.clear()
    _browser_companion_presence.update(
        {"browser": (req.browser or "").strip(), "seen_at": time.time()}
    )
    _active_browser_tab.clear()
    _active_browser_tab.update(
        {"url": req.url.strip(), "browser": (req.browser or "").strip(), "seen_at": time.time()}
    )
    return {"accepted": True}


@app.get("/browser-tab")
async def browser_tab() -> dict:
    seen_at = float(_active_browser_tab.get("seen_at") or 0)
    age = max(0.0, time.time() - seen_at) if seen_at else None
    connected = bool(_active_browser_tab.get("url")) and age is not None and age <= _BROWSER_TAB_MAX_AGE_S
    return {
        "connected": connected,
        "url": _active_browser_tab.get("url") if connected else None,
        "browser": _active_browser_tab.get("browser") if connected else None,
        "age_seconds": round(age, 1) if age is not None else None,
    }


@app.post("/browser-companion")
async def update_browser_companion(req: BrowserCompanionHeartbeat) -> dict:
    _browser_companion_presence.clear()
    _browser_companion_presence.update(
        {"browser": (req.browser or "").strip(), "seen_at": time.time()}
    )
    return {"accepted": True}


@app.get("/browser-companion")
async def browser_companion() -> dict:
    seen_at = float(_browser_companion_presence.get("seen_at") or 0)
    age = max(0.0, time.time() - seen_at) if seen_at else None
    connected = age is not None and age <= _BROWSER_COMPANION_MAX_AGE_S
    return {
        "connected": connected,
        "browser": _browser_companion_presence.get("browser") if connected else None,
        "age_seconds": round(age, 1) if age is not None else None,
    }


@app.post("/scan", response_model=ScanResponse)
async def scan(req: ScanRequest) -> ScanResponse:
    scan_type = req.scan_type if req.scan_type in ("person", "company_app") else "person"
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job_queue: "asyncio.Queue[dict]" = asyncio.Queue()
    _job_queues[job_id] = job_queue

    task = asyncio.create_task(
        _run_job(
            job_id,
            req.url,
            req.screenshot_b64,
            scan_type,
            req.platform,
            req.demo,
            req.extract_from_screenshot,
            job_queue,
        )
    )
    _job_tasks[job_id] = task

    loop = asyncio.get_running_loop()

    def _on_job_task_done(_t: "asyncio.Task", jid: str = job_id) -> None:
        _job_tasks.pop(jid, None)
        # _job_queues[jid] is deliberately NOT popped here: the queue's
        # final events (scores/verdict/done) were only just enqueued, and a
        # client that is slow to open the websocket (or reconnecting after
        # an earlier disconnect, see events()'s finally block below) still
        # needs to be able to drain them. Instead, schedule a grace-period
        # drop so a job whose websocket client never connects at all cannot
        # leak its queue forever.
        loop.call_later(_JOB_QUEUE_GRACE_PERIOD_S, _job_queues.pop, jid, None)

    task.add_done_callback(_on_job_task_done)
    return ScanResponse(job_id=job_id)


@app.websocket("/events/{job_id}")
async def events(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    job_queue = _job_queues.get(job_id)
    if job_queue is None:
        await websocket.send_text(json.dumps({"type": "error", "text": f"unknown job_id: {job_id}"}))
        await websocket.send_text(json.dumps({"type": "done"}))
        await websocket.close()
        return

    try:
        while True:
            msg = await job_queue.get()
            await websocket.send_text(json.dumps(msg))
            if msg.get("type") == "done":
                # "done" was actually delivered to a client: this job_id has
                # nothing left to stream, safe to drop right away rather than
                # waiting out the grace period.
                _job_queues.pop(job_id, None)
                await websocket.close(code=1000)
                break
    except WebSocketDisconnect:
        # Deliberately do NOT pop _job_queues[job_id] here: the background
        # task may still be running (a reconnect should resume draining the
        # same queue), or may have already finished with its final events
        # still sitting unread in the queue (a reconnect should still get
        # them). Either way an early disconnect must not make a legitimate
        # reconnect see "unknown job_id". The scan() task done_callback's
        # grace-period sweep is the backstop if nobody ever reconnects.
        logger.info("websocket for job %s disconnected early", job_id)


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------


async def _run_job(
    job_id: str,
    url: Optional[str],
    screenshot_b64: Optional[str],
    scan_type: str,
    platform: Optional[str],
    demo_flag: bool,
    extract_from_screenshot: bool,
    job_queue: "asyncio.Queue[dict]",
) -> None:
    loop = asyncio.get_running_loop()

    def emit(msg: dict) -> None:
        # Only ever called from the event-loop thread (either directly, or
        # via call_soon_threadsafe below), so put_nowait is safe here.
        job_queue.put_nowait(msg)

    def emit_threadsafe(msg: dict) -> None:
        # Safe to call from pipeline.run's executor worker thread: hops back
        # onto the loop thread before touching the asyncio.Queue. Handed to
        # _PlanWaitingManualProvider so the plan-wait narration reaches the
        # socket even though it runs inside build_dossier on that worker thread.
        loop.call_soon_threadsafe(emit, msg)

    # Mutable, per-scan state shared by every _pipeline_event_to_messages call
    # for this job: which source domains have already become a "website" card
    # (dedup), how many website cards / image thumbnails have been emitted so
    # far (the ~6 and ~3 caps), whether a hero image has already been chosen,
    # and the scan_type to reason with (kept here, not read straight off the
    # request's scan_type param, since the demo path's raw_profile may carry
    # its own authoritative scan_type; see the is_demo branch below). Safe
    # without locking: progress() below only ever runs synchronously, one
    # call at a time, on pipeline.run's single worker thread.
    state: dict[str, Any] = {
        "scan_type": scan_type,
        "seen_domains": set(),
        "website_count": 0,
        "thumbnail_count": 0,
        "hero_emitted": False,
        # Rich-image bookkeeping (see the image-source helpers below):
        #   seen_employers / logo_count  cap + dedup the per-employer Clearbit
        #     logos streamed as each employment claim is processed.
        #   og_candidates  the top high-signal evidence URLs whose og:image is
        #     fetched (concurrently, bounded) at the verdict step.
        #   allow_network_images  hard offline guard: the demo/test path must
        #     never fire a real requests.get for a photo proxy or og:image.
        "seen_employers": set(),
        "logo_count": 0,
        "og_candidates": [],
        "allow_network_images": True,
    }

    def progress(event: str, payload: Any) -> None:
        # Runs on pipeline.run's worker thread (see run_in_executor below),
        # so hop back onto the loop thread before touching the asyncio.Queue.
        for msg in _pipeline_event_to_messages(event, payload, state):
            loop.call_soon_threadsafe(emit, msg)

    async def _heartbeat() -> None:
        # Keeps the overlay's websocket warm through the long silent phases of a
        # scan. pipeline.run executes on a worker thread (run_in_executor), so
        # the event loop is free and this task runs concurrently with the scan,
        # slipping a keepalive frame between real events every
        # _HEARTBEAT_INTERVAL_S seconds. Uses the direct `emit` (put_nowait on
        # the loop thread) since it already runs on the loop thread. "keepalive"
        # is a type the overlay's WS handler ignores via its switch default (see
        # overlay/src/App.jsx applyEvent), so it never touches the UI or the
        # feed. Cancelled in _run_job's finally, so it never outlives the job and
        # never lands a frame after the terminal "done".
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                emit({"type": "keepalive"})
            except asyncio.CancelledError:
                return
            except Exception:  # a heartbeat glitch must never crash the scan
                logger.exception("job %s: heartbeat emit failed", job_id)

    heartbeat_task = loop.create_task(_heartbeat())

    try:
        is_demo = bool(demo_flag) or (url or "").strip().lower() == "demo"
        # Identity-confirmation gate state (see the post-scrape gate below). Set
        # only on the screenshot path when a URL is found by NAME SEARCH (not an
        # exact omnibox/typed URL): such a URL must have its scraped name matched
        # against what the operator was viewing before it can score.
        url_was_resolved_by_search = False
        vision_name = ""
        # Hard offline guard: the demo/test path decomposes and "gathers
        # evidence" (offline, no real hits) and runs through the same
        # verdict/claim event handlers, so the network-backed image sources
        # (photo proxy, og:image) must be gated off for it.
        state["allow_network_images"] = not is_demo

        if is_demo:
            raw_profile = _build_demo_profile(scan_type)
            effective_scan_type = raw_profile.get("scan_type") or scan_type
            state["scan_type"] = effective_scan_type
            emit({"type": "status", "text": f"demo mode: using bundled fixture profile ({effective_scan_type})"})
            effective_url = raw_profile.get("profile_url", "demo")
            live = False

            if effective_scan_type == "company_app":
                _emit_company_hero(emit, effective_url, state)
            else:
                _emit_person_hero_if_captured(emit, raw_profile, state)
            _emit_demo_rich_sequence(emit, effective_scan_type, state)
        elif url:
            raw_profile = None
            effective_url = url
            live = True
            emit({"type": "status", "text": f"starting live scan: {url}"})
            if scan_type == "company_app":
                # The domain is known immediately (it is the url the caller
                # supplied), so the company hero does not need to wait on a
                # fetch; a person scan's hero (if any) is derived later, once
                # identity/evidence are actually known (see
                # _pipeline_event_to_messages's "claim"/"verdict" handling).
                _emit_company_hero(emit, effective_url, state)
        elif screenshot_b64 and extract_from_screenshot:
            # The overlay's "Go" button, layer 2: no exact URL was found (the
            # Windows UI-Automation active-tab read came back empty, or this
            # is not Windows), so read the screenshot with the provider's
            # vision_extract instead. Forced to a person scan: this path only
            # ever exists for "scan the LinkedIn profile I'm looking at".
            scan_type = "person"
            state["scan_type"] = "person"
            emit({"type": "status", "text": "reading the profile on your screen"})

            vision_provider = _select_provider(job_id, is_demo=False)
            try:
                # Offloaded onto the executor, same discipline as
                # pipeline.run below: vision_extract can block (a real
                # Gemini call, or ManualProvider's own file I/O), so it must
                # never run synchronously on the event-loop thread.
                vision = await loop.run_in_executor(None, vision_provider.vision_extract, screenshot_b64)
            except ApiProviderError as exc:
                emit(
                    {
                        "type": "status",
                        "text": f"screenshot reading failed ({exc}); falling back to the manual queue",
                    }
                )
                vision_provider = ManualProvider(job_id=job_id)
                try:
                    vision = await loop.run_in_executor(None, vision_provider.vision_extract, screenshot_b64)
                except Exception as exc2:  # never let a vision failure crash the socket
                    logger.exception("job %s: manual vision_extract failed", job_id)
                    emit({"type": "error", "text": str(exc2)})
                    emit({"type": "done"})
                    return
            except NotImplementedError:
                emit(
                    {
                        "type": "error",
                        "text": "the configured provider cannot read a screenshot; "
                        "paste the profile URL instead.",
                    }
                )
                emit({"type": "done"})
                return

            if isinstance(vision_provider, ManualProvider) and _vision_result_is_empty(vision):
                # ManualProvider's default (MANUAL_QUEUE_TIMEOUT_S=0): the
                # vision queue file was just written and vision came back
                # empty immediately, an operator has not looked yet. Watch
                # THAT SAME file (job_id is unchanged) for completion instead
                # of failing right away with no real recovery path; see
                # _watch_vision_queue_and_finish.
                emit(
                    {
                        "type": "status",
                        "text": f"queued for operator to read the screenshot; watching "
                        f"{vision_provider._vision_job_path().name} for completion",
                    }
                )
                watched = await _watch_vision_queue_and_finish(vision_provider, emit)
                if watched is not None:
                    vision = watched
                else:
                    # The operator never filled the vision job in time. This is
                    # a timeout, not a dead end: return the overlay to the paste
                    # affordance (needs_url) rather than a dead error card.
                    emit(
                        {
                            "type": "needs_url",
                            "text": "timed out reading the profile on your screen. "
                            "Paste the LinkedIn profile URL to run a full scan.",
                        }
                    )
                    emit({"type": "done"})
                    return

            resolved_url = (vision.get("profile_url") or "").strip()
            vision_name = (vision.get("name") or "").strip()
            if not resolved_url:
                company = (vision.get("company") or "").strip()
                if vision_name:
                    emit({"type": "thought", "text": f"Searching for {vision_name}'s LinkedIn profile"})
                    resolved_url = _resolve_linkedin_url(vision_name, company) or ""
                    # A URL found by name search is NOT an exact match: it must
                    # pass the post-scrape identity gate below before it can
                    # score, so a "scanned the wrong person" result is caught.
                    if resolved_url:
                        url_was_resolved_by_search = True

            if not resolved_url:
                # No exact URL and no confident name resolution: ask for the URL
                # (needs_url returns to the paste field), never a scored verdict
                # off thin data and never a dead error card.
                emit(
                    {
                        "type": "needs_url",
                        "text": "could not read a LinkedIn profile from your screen. "
                        "Paste the profile URL to run a full scan.",
                    }
                )
                emit({"type": "done"})
                return

            emit({"type": "status", "text": f"found profile from your screen: {resolved_url}"})
            raw_profile = None
            effective_url = resolved_url
            live = True
        elif screenshot_b64:
            emit(
                {
                    "type": "error",
                    "text": "screenshot-only scans need \"extract_from_screenshot\": true; "
                    "supply a url, that flag, or {\"demo\": true} for the offline demo path.",
                }
            )
            emit({"type": "done"})
            return
        else:
            emit({"type": "error", "text": "no url, screenshot_b64, or demo flag provided."})
            emit({"type": "done"})
            return

        provider = _select_provider(job_id, is_demo)
        # For a real (non-demo) ManualProvider scan, swap in the plan-waiting
        # wrapper so the director / planning round trip actually happens (the
        # base ManualProvider would write the plan job and return [] instantly,
        # so the operator never gets to fill it before scoring). ApiProvider is
        # left untouched (it does its own planning), and the demo path keeps the
        # plain ManualProvider so it never blocks on a plan job it will not fill.
        if not is_demo and isinstance(provider, ManualProvider):
            provider = _manual_provider_for_scan(job_id, is_demo, plan_emit=emit_threadsafe)
        provider._audit_job_id = job_id
        engine = _select_engine()

        run_kwargs = {
            "provider": provider,
            "live": live,
            "progress": progress,
            "raw_profile": raw_profile,
            "scan_type": scan_type,
            "engine": engine,
        }
        if is_demo:
            run_kwargs["offline"] = True
        run_fn = functools.partial(pipeline.run, effective_url, **run_kwargs)
        try:
            dossier: Dossier = await loop.run_in_executor(None, run_fn)
        except ApiProviderError as exc:
            # The Gemini path failed (no key, quota/exhausted, network, or an
            # unparseable/incomplete response): fall back to the $0
            # ManualProvider queue flow instead of surfacing an error. This
            # re-runs the WHOLE pipeline (fetch/decompose/gather-evidence
            # included), not just the scoring step, since ApiProvider only
            # ever fails inside assign_tiers_and_verdict and pipeline.run has
            # no seam to resume mid-way; on a LIVE (non-demo) scan this means
            # a second live fetch. Acceptable for an error path that should
            # be rare once a key is configured correctly, but real: a flaky
            # Gemini key can double a live LinkedIn/company fetch. Rich
            # progress events (thought/claim/status) also replay from
            # scratch; website-card dedup survives via the shared `state`
            # dict, but thought/status lines will repeat on the socket.
            logger.warning("job %s: ApiProvider failed (%s); falling back to ManualProvider queue", job_id, exc)
            emit(
                {
                    "type": "status",
                    "text": f"automated scoring failed ({exc}); falling back to the manual queue",
                }
            )
            # Same plan-waiting wrapper as the primary path: the fallback
            # re-runs the whole pipeline through ManualProvider, so it too must
            # round-trip the director plan job (is_demo is always False here,
            # since ApiProvider is never used on the demo path).
            provider = _manual_provider_for_scan(job_id, is_demo, plan_emit=emit_threadsafe)
            provider._audit_job_id = job_id
            run_fn = functools.partial(
                pipeline.run,
                effective_url,
                provider=provider,
                live=live,
                progress=progress,
                raw_profile=raw_profile,
                scan_type=scan_type,
                engine=engine,
            )
            dossier = await loop.run_in_executor(None, run_fn)

        # Identity-confirmation gate: when the URL was found by NAME SEARCH off a
        # screenshot (not an exact URL the operator read/typed), the scraped
        # profile might be a different person with the same-ish name. Compare the
        # scraped name to the vision name; on a real mismatch, do NOT surface the
        # score, ask for the exact URL instead. Scanning the wrong person and
        # scoring them is worse than not scanning at all. Skipped entirely for an
        # exact URL (url_was_resolved_by_search stays False there).
        if url_was_resolved_by_search:
            scraped_name = ((dossier.identity or {}).get("name") or "").strip()
            if not _names_plausibly_match(vision_name, scraped_name):
                emit(
                    {
                        "type": "needs_url",
                        "text": (
                            f"found a profile for {scraped_name or 'someone else'} but you "
                            f"were viewing {vision_name}; paste the exact profile URL to run "
                            "a full scan on the right person."
                        ),
                    }
                )
                emit({"type": "done"})
                return

        if getattr(dossier, "coverage_warning", ""):
            # Most claims were never looked up. Scoring proceeds as normal (the
            # number is still correct under the labeling rules), but the reader
            # is told what fraction of the profile went unchecked, so an outage
            # is never mistaken for a clean bill of health.
            logger.warning("job %s: %s", job_id, dossier.coverage_warning)
            emit({"type": "status", "text": dossier.coverage_warning})

        # Normal pipeline paths already finalize, but provider/test adapters and
        # old completed queue files may carry only the legacy component score.
        # The centralized finalizer is idempotent and fills overall_larp_score
        # before the completion predicate or websocket payload reads it.
        _finalize_scores(dossier)
        if _dossier_is_scored(dossier):
            # ApiProvider path, or a queue file that was already completed
            # (e.g. a re-run of a job_id an operator already finished).
            _emit_final(emit, dossier, job_id=job_id)
            return

        # ManualProvider's default (MANUAL_QUEUE_TIMEOUT_S=0): dossier came
        # back unscored, and the job file is sitting in queue/ pending.
        queue_path = QUEUE_DIR / f"{job_id}.json"
        if is_demo:
            _auto_complete_demo_job(queue_path)

        emit(
            {
                "type": "status",
                "text": f"queued for operator scoring; watching {queue_path.name} for completion",
            }
        )
        await _watch_queue_and_finish(queue_path, emit, job_id=job_id)

    except Exception as exc:  # never let a job crash the socket
        logger.exception("job %s failed", job_id)
        emit({"type": "error", "text": str(exc)})
        emit({"type": "done"})
    finally:
        # Stop the keepalive so it never outlives the job. This runs
        # synchronously right after the terminal "done" emit (no await in
        # between on any exit path), so a heartbeat parked on asyncio.sleep
        # cannot wake in that window: no keepalive is ever queued after "done".
        heartbeat_task.cancel()


def _pipeline_event_to_messages(event: str, payload: Any, state: dict) -> list[dict]:
    """Map pipeline.run's (event, payload) progress callback onto the WS
    event vocabulary, fanned out into possibly several messages per pipeline
    event: a running "thought" line, "website" cards for that claim's
    evidence sources, an occasional "image" thumbnail or hero, and the
    existing "status"/"claim" events.

    "claim" here fires right after evidence-gathering, so its tier is still
    the default UNVERIFIED; the real, post-scoring tier for every claim is
    re-emitted in _emit_final once the queue job completes.

    Never raises: any failure while deriving the rich events is swallowed
    (logged, not surfaced) so a malformed favicon/logo build can never break
    the error+done invariant the rest of this service guarantees.
    """
    try:
        if event == "status":
            return [{"type": "status", "text": str(payload)}]

        if event == "claim":
            out: list[dict] = []
            c = payload
            out.append({"type": "thought", "text": _thought_for_claim(c)})
            out.extend(_website_events_for_claim(c, state))
            # Source #2: a company logo per distinct employer, streamed as each
            # employment claim is processed (progressive), Clearbit by domain.
            out.extend(_employer_logo_events_for_claim(c, state))

            tier = c.tier.value if isinstance(c.tier, EvidenceTier) else str(c.tier)
            out.append({"type": "claim", "assertion": c.assertion, "tier": tier})
            return out

        if event == "verdict":
            # pipeline.run's own "verdict" event just means the raw Dossier is
            # assembled (evidence gathered, possibly still unscored); the
            # real final verdict/scores for the socket come from _emit_final
            # below. This is also the first point a LIVE person scan's
            # captured profile photo (identity["image"]) is actually known.
            #
            # Two network-backed image sources fire here, ONE concurrent
            # bounded batch (see _verdict_image_events): source #3 og:image
            # thumbnails for the top evidence URLs, emitted first, then
            # source #1 the SERVER-SIDE-PROXIED profile photo emitted LAST so
            # the identity photo is the resting hero (the overlay treats the
            # most recent image event as the hero).
            out = _verdict_image_events(payload, state)
            out.append({"type": "status", "text": "dossier assembled; entering scoring step"})
            return out

        return []
    except Exception:
        logger.exception("service: failed deriving rich events for pipeline event %r", event)
        return []


# ---------------------------------------------------------------------------
# Rich-event derivation: real images, favicons, and templated thoughts.
# Presentation-layer only (kept here, not in pipeline.py/verify.py), built
# from data the pipeline already gathers. No em dashes (house rule).
# ---------------------------------------------------------------------------

_MAX_WEBSITE_CARDS_PER_SCAN = 6
_MAX_THUMBNAILS_PER_SCAN = 3
# Source #2: at most this many distinct employer logos (Clearbit) per scan,
# so a long work history does not spam the cascade.
_MAX_EMPLOYER_LOGOS = 5
# Source #3: at most this many og:image fetches per scan (top evidence URLs
# only), each short-timeout and run concurrently so total added latency is one
# batch, not the sum. Kept small on purpose (latency + arbitrary pages).
_MAX_OG_IMAGES_PER_SCAN = 4
_OG_FETCH_TIMEOUT_S = 4.0
_PHOTO_PROXY_TIMEOUT_S = 5.0
# Hard ceiling on the whole concurrent verdict-image batch (photo + og:images),
# so the one blocking point this adds to the scan is strictly bounded.
_VERDICT_IMAGE_WAIT_S = 6.0

# Domains that are never the scanned company/person's OWN site, even if a
# name happens to substring-match (LinkedIn's own company page, press,
# reference, and social sites). Used only to gate the "derive a hero logo
# domain from evidence" fallback for person scans, so a stray LinkedIn or
# Crunchbase hit is never mistaken for the company's real domain.
_NON_COMPANY_DOMAINS = frozenset(
    {
        "linkedin.com", "crunchbase.com", "wikipedia.org", "techcrunch.com",
        "forbes.com", "bloomberg.com", "twitter.com", "x.com", "facebook.com",
        "instagram.com", "youtube.com", "medium.com", "github.com",
        "glassdoor.com", "indeed.com", "ycombinator.com", "reuters.com",
        "apnews.com", "nytimes.com", "wsj.com", "cnbc.com",
        "businessinsider.com", "theverge.com", "sec.gov", "reddit.com",
        "google.com",
    }
)

_EMPLOYER_SUFFIX_WORDS = ("inc", "llc", "corp", "co", "ai", "labs")


def _favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


def _clearbit_logo_url(domain: str) -> str:
    return f"https://logo.clearbit.com/{domain}"


def _strip_www(domain: str) -> str:
    return domain[4:] if domain.startswith("www.") else domain


def _domain_of(url: str) -> str:
    """Same extraction verify.py's evidence ranking uses, kept in step with
    it on purpose (one definition of "what domain is this url" for the whole
    service): everything after the scheme, up to the first "/".
    """
    return _strip_www((url or "").split("//")[-1].split("/")[0].lower())


def _title_from_snippet(snippet: str, domain: str) -> str:
    """The evidence snippet's leading text, or the domain if there is none.

    "Leading text": cut at the first sentence break or newline, then cap
    length on a word boundary, so a long snippet never turns into an
    unreadable card title.
    """
    text = (snippet or "").strip()
    if not text:
        return domain
    for sep in (". ", "\n"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    text = text.strip()
    if len(text) > 80:
        cut = text[:80].rsplit(" ", 1)[0].strip()
        text = cut or text[:80]
    return text or domain


def _website_events_for_claim(claim, state: dict) -> list[dict]:
    """Website cards (plus a few source-thumbnail images) for one claim's
    gathered evidence, deduped by domain and capped per SCAN (not per claim)
    via the shared `state` dict.
    """
    out: list[dict] = []
    seen: set = state.setdefault("seen_domains", set())
    count = state.get("website_count", 0)
    thumb_count = state.get("thumbnail_count", 0)

    for e in getattr(claim, "evidence", None) or []:
        if count >= _MAX_WEBSITE_CARDS_PER_SCAN:
            break
        url = e.get("source_url", "")
        domain = _domain_of(url)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        count += 1
        out.append(
            {
                "type": "website",
                "url": url,
                "title": _title_from_snippet(e.get("snippet", ""), domain),
                "favicon": _favicon_url(domain),
                "domain": domain,
            }
        )
        # Remember this high-signal source URL as an og:image candidate for the
        # bounded verdict-step batch (source #3). One per distinct domain, so a
        # single site never eats the whole og budget; capped when consumed.
        candidates = state.setdefault("og_candidates", [])
        if len(candidates) < _MAX_OG_IMAGES_PER_SCAN and url:
            candidates.append(url)
        if thumb_count < _MAX_THUMBNAILS_PER_SCAN:
            # A favicon, always small and low-res: kind="logo" so the overlay
            # renders it contained, never stretched; hero=False so it can
            # only ever land in the thumbnail strip, never hijack the hero.
            out.append(
                {
                    "type": "image",
                    "url": _favicon_url(domain),
                    "caption": domain,
                    "kind": "logo",
                    "hero": False,
                }
            )
            thumb_count += 1

    state["website_count"] = count
    state["thumbnail_count"] = thumb_count
    return out


def _employer_slug(employer: str) -> str:
    slug = "".join(ch for ch in employer.lower() if ch.isalnum())
    for suffix in _EMPLOYER_SUFFIX_WORDS:
        if slug.endswith(suffix) and len(slug) > len(suffix) + 2:
            slug = slug[: -len(suffix)]
    return slug


def _domain_root(domain: str) -> str:
    return _strip_www(domain).split(":")[0].split(".")[0]


def _looks_like_own_domain(employer: str, domain: str) -> bool:
    """Best-effort, conservative check that `domain` is plausibly the
    employer's OWN site (never a press/reference/social mention of it).

    Never a strong claim, just a gate against fabricating a hero logo: a
    known non-company domain (_NON_COMPANY_DOMAINS) always fails, and both
    the employer slug and the domain root must be long enough (>= 3 chars)
    and substring-match each other.
    """
    if not employer or not domain:
        return False
    if domain in _NON_COMPANY_DOMAINS:
        return False
    slug = _employer_slug(employer)
    root = _domain_root(domain)
    if len(slug) < 3 or len(root) < 3:
        return False
    return slug in root or root in slug


def _derive_own_domain_from_evidence(employer: str, evidence: list[dict]) -> Optional[str]:
    """First evidence source_url domain that plausibly IS the employer's own
    site (see _looks_like_own_domain), else None. Only ever called for a
    person scan's employment claims, never used to fabricate a domain that
    is not actually present in the gathered evidence.
    """
    for e in evidence or []:
        domain = _domain_of(e.get("source_url", ""))
        if domain and _looks_like_own_domain(employer, domain):
            return domain
    return None


def _employer_logo_events_for_claim(claim, state: dict) -> list[dict]:
    """Source #2: a Clearbit company logo for each DISTINCT employer on an
    employment claim, streamed progressively as claims are processed.

    The domain is derived best-effort: an employer's OWN site found IN the
    gathered evidence (_derive_own_domain_from_evidence) wins, else a slugified
    <company>.com guess (images.guess_company_domain). Clearbit 404s harmlessly
    for a wrong guess, which the overlay degrades to a monogram, so a guess is
    never a fabricated claim. Deduped by employer and capped at
    _MAX_EMPLOYER_LOGOS per scan. Company_app scans already get a Clearbit hero
    up front (_emit_company_hero), so this is person-scan only. Never raises.
    """
    if state.get("scan_type") == "company_app":
        return []
    if getattr(claim, "type", "") != "employment":
        return []
    employer = (getattr(claim, "employer", "") or "").strip()
    if not employer:
        return []

    seen: set = state.setdefault("seen_employers", set())
    key = employer.lower()
    if key in seen:
        return []
    if state.get("logo_count", 0) >= _MAX_EMPLOYER_LOGOS:
        return []

    own_domain = _derive_own_domain_from_evidence(employer, getattr(claim, "evidence", None) or [])
    domain = own_domain or images.guess_company_domain(employer)
    logo_url = images.clearbit_logo_url(domain) if domain else ""
    if not logo_url:
        return []

    seen.add(key)
    state["logo_count"] = state.get("logo_count", 0) + 1
    # fallback: a second, independently-reliable image for the SAME domain
    # (see images.favicon_logo_url and the module docstring), so a dead
    # logo.clearbit.com degrades to a real logo-ish image, not straight to a
    # bare monogram letter. kind="logo" so the overlay renders it small and
    # contained instead of stretching a tiny mark across a big tile (that
    # stretch is exactly what made an employer logo like this render as a
    # blurry blob). hero=False: a per-employer logo is a thumbnail, never
    # the big hero, so streaming one mid-scan can never bump the hero away
    # from whatever it currently is (the flicker this used to cause).
    return [
        {
            "type": "image",
            "url": logo_url,
            "caption": f"{employer} logo",
            "fallback": images.favicon_logo_url(domain),
            "kind": "logo",
            "hero": False,
        }
    ]


def _verdict_image_events(dossier, state: dict) -> list[dict]:
    """Sources #3 (og:image thumbnails) and #1 (proxied profile photo),
    fetched in ONE concurrent, time-bounded batch at the verdict step.

    Ordering matters: the overlay treats the MOST RECENT image event as the
    hero, so the og:image thumbnails are emitted first and the server-side
    proxied identity photo LAST, leaving the person's own photo as the resting
    hero. A company_app scan has no identity photo (its Clearbit hero was
    emitted up front), so it only gets og:image thumbnails here.

    Bounded: at most _MAX_OG_IMAGES_PER_SCAN og fetches plus one photo proxy,
    run concurrently and waited on for at most _VERDICT_IMAGE_WAIT_S total; any
    fetch still pending at the deadline is simply dropped. Never raises; the
    offline demo/test path (allow_network_images False) returns nothing here.
    """
    if not state.get("allow_network_images", True):
        return []

    identity = getattr(dossier, "identity", None) or {}
    person_name = (identity.get("name") or "").strip()
    photo_url = ""
    if state.get("scan_type") != "company_app":
        photo_url = (identity.get("image") or "").strip()

    candidates = list(state.get("og_candidates") or [])[:_MAX_OG_IMAGES_PER_SCAN]
    if not candidates and not photo_url:
        return []

    og_results: list[tuple] = []
    photo_data_uri: Optional[str] = None
    cookies = images.load_linkedin_cookies() if photo_url else None

    # One pool for the whole batch: og fetches + the photo proxy run
    # concurrently, so the added latency is a single bounded wait, not the sum
    # of every fetch. This function RETURNS within _VERDICT_IMAGE_WAIT_S: a
    # plain `with ThreadPoolExecutor(...)` block would call shutdown(wait=True)
    # on exit and re-block on any future the wait() below abandoned as pending
    # (defeating the deadline), so the pool is shut down non-blocking with
    # cancel_futures instead. Any fetch still in flight at the deadline keeps
    # running on its own worker thread until its own requests timeout fires,
    # then its result is simply discarded (never emitted late).
    workers = max(1, len(candidates) + (1 if photo_url else 0))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        og_futs = {pool.submit(images.fetch_og_image, u, _OG_FETCH_TIMEOUT_S): u for u in candidates}
        photo_fut = (
            pool.submit(images.proxy_image_as_data_uri, photo_url, cookies, _PHOTO_PROXY_TIMEOUT_S)
            if photo_url
            else None
        )
        done, _pending = concurrent.futures.wait(
            list(og_futs) + ([photo_fut] if photo_fut else []),
            timeout=_VERDICT_IMAGE_WAIT_S,
        )
        for fut in og_futs:
            if fut in done:
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res:
                    og_results.append(res)
        if photo_fut is not None and photo_fut in done:
            try:
                photo_data_uri = photo_fut.result()
            except Exception:
                photo_data_uri = None
    except Exception:
        logger.exception("service: verdict image batch failed")
    finally:
        # Non-blocking: do NOT wait on futures the deadline abandoned.
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # cancel_futures is Python 3.9+; fall back gracefully
            pool.shutdown(wait=False)

    out: list[dict] = []
    # og:image thumbnails first: a real page screenshot, kind="photo" (safe
    # to cover-fill AT THUMBNAIL SIZE), but hero=False always. An og:image is
    # a source thumbnail, never the scan's hero, so it can only ever land in
    # the strip; it must not be able to bump whatever the real hero is.
    for og_image, title in og_results:
        out.append({"type": "image", "url": og_image, "caption": title or "source", "kind": "photo", "hero": False})
    # The proxied identity photo LAST, so it is the resting hero. A miss (403,
    # timeout, non-image) simply emits nothing: the overlay's monogram covers
    # it, and this is never surfaced as an error. hero is guarded on
    # state["hero_emitted"] (defensive: nothing in this scan should have set
    # it before a person scan reaches this point, but a second True hero
    # emitted for the same scan is exactly the kind of thing that would
    # reintroduce a flicker, so this is a hard belt-and-suspenders check, not
    # just documentation).
    if photo_data_uri:
        caption = f"{person_name}" if person_name else "profile photo"
        out.append(
            {
                "type": "image",
                "url": photo_data_uri,
                "caption": caption,
                "kind": "photo",
                "hero": not state.get("hero_emitted", False),
            }
        )
        state["hero_emitted"] = True
    return out


def _thought_for_claim(claim) -> str:
    """One short, templated running-reasoning line for a claim, built from
    its type, employer, and what is being checked. Always produces a
    non-empty line, evidence or not, since this is narration, not a finding.
    """
    employer = (claim.employer or "").strip()
    ctype = (claim.type or "").strip().lower()

    if ctype == "identity":
        return f"Confirming this person's identity{f' at {employer}' if employer else ''}"
    if ctype == "employment":
        base = f"Cross-checking the role at {employer}" if employer else "Cross-checking this employment claim"
        return f"{base} against public record"
    if ctype == "education":
        return f"Verifying the education claim at {employer}" if employer else "Verifying this education claim"
    if ctype == "user_count":
        return (
            f"Checking if {employer}'s claimed user count matches its public footprint"
            if employer
            else "Checking if the claimed user count matches its public footprint"
        )
    if ctype == "revenue_metric":
        return (
            f"Cross-checking {employer}'s revenue claim against press and public filings"
            if employer
            else "Cross-checking this revenue claim against press and public filings"
        )
    if ctype == "proprietary_tech":
        return (
            f"Checking if {employer}'s product is real proprietary tech or a thin wrapper"
            if employer
            else "Checking if this product is real proprietary tech or a thin wrapper"
        )
    if ctype == "funding":
        return (
            f"Cross-checking {employer}'s raise against SEC and press"
            if employer
            else "Cross-checking this funding claim against SEC and press"
        )
    if ctype == "pricing":
        return (
            f"Checking {employer}'s pricing against user complaints"
            if employer
            else "Checking this pricing claim against user complaints"
        )
    if ctype == "headcount":
        return (
            f"Checking {employer}'s team size against public record"
            if employer
            else "Checking this headcount claim against public record"
        )
    if ctype == "company_overview":
        return (
            f"Company page loads, checking if {employer} actually ships a live product"
            if employer
            else "Company page loads, checking if the product actually ships"
        )

    assertion = (claim.assertion or "").strip()
    return f"Checking: {assertion}" if assertion else "Gathering evidence for this claim"


def _emit_company_hero(emit, url: str, state: dict) -> None:
    """HERO image for a company_app scan: the Clearbit logo of the scanned
    url's own domain, known up front (no evidence needed), with a real
    favicon-based fallback URL riding along (see images.favicon_logo_url and
    the module docstring: logo.clearbit.com's public endpoint is currently
    dead, so the overlay needs a second real source to swap to, not just a
    monogram, when "url" fails to load). Never raises.
    """
    try:
        domain = _domain_of(url)
        if not domain:
            return
        emit(
            {
                "type": "image",
                "url": _clearbit_logo_url(domain),
                "caption": f"{domain} logo",
                "fallback": images.favicon_logo_url(domain),
                # kind="logo": a company mark, inherently low-res even at the
                # fallback's largest favicon size, so the overlay must render
                # it small and contained, never stretched across the hero.
                # hero=True: this IS the company_app scan's one hero, emitted
                # up front and never replaced (every other image source for a
                # company_app scan sets hero=False), so it stays stable for
                # the whole scan instead of getting bumped by a later
                # og:image thumbnail.
                "kind": "logo",
                "hero": True,
            }
        )
        state["hero_emitted"] = True
    except Exception:
        logger.exception("service: failed emitting company hero for %r", url)


def _emit_person_hero_if_captured(emit, raw_profile: dict, state: dict) -> None:
    """HERO image for a person scan, ONLY when extract_linkedin actually
    captured a profile photo URL (identity["image"]). Never fabricated: a
    profile with no captured photo simply gets no hero from this call (the
    per-claim own-domain fallback, or the live "verdict" branch, may still
    supply one later). Never raises.
    """
    try:
        identity = (raw_profile or {}).get("identity") or {}
        image_url = (identity.get("image") or "").strip()
        if image_url:
            # kind="photo": a real captured photo, safe to cover-fill the
            # hero. hero=True: this is the person scan's up-front hero (the
            # demo/raw_profile case); every employer-logo/favicon event that
            # streams afterward sets hero=False, so it cannot bump this photo
            # back out of the hero slot.
            emit({"type": "image", "url": image_url, "caption": "profile photo", "kind": "photo", "hero": True})
            state["hero_emitted"] = True
    except Exception:
        logger.exception("service: failed emitting person hero from raw_profile")


# ---------------------------------------------------------------------------
# DEMO PATH rich sequence: offline, no network, no real evidence to draw on
# (search.py returns [] with no SEARXNG_URL/BRAVE_API_KEY configured), so the
# demo instead plays a fixed, clearly-demo-only thought/website/image
# sequence: real, public, no-key favicon/logo URLs against plausible domains,
# so the overlay's "showing its work" panel has something to render fully
# offline. Never presented as real findings outside the demo path.
# ---------------------------------------------------------------------------

_DEMO_SITES_PERSON = (
    ("https://techcrunch.com/tag/demo-sample", "TechCrunch coverage mentioning Demo Sample", "techcrunch.com"),
    ("https://github.com/demo-sample", "Demo Sample's public GitHub activity", "github.com"),
    ("https://www.sec.gov/cgi-bin/browse-edgar?company=demo", "SEC filings search for the claimed employer", "sec.gov"),
    ("https://www.crunchbase.com/organization/demo-sample-co", "Crunchbase profile for the claimed employer", "crunchbase.com"),
    ("https://en.wikipedia.org/wiki/Demo_Sample", "Wikipedia entry cross-referenced for the claim", "wikipedia.org"),
    ("https://www.linkedin.com/company/demo-sample-co", "LinkedIn company page for a headcount cross-check", "linkedin.com"),
)

_DEMO_SITES_COMPANY = (
    ("https://techcrunch.com/tag/resumegenie", "TechCrunch coverage of the funding claim", "techcrunch.com"),
    ("https://github.com/resumegenie-ai", "Public GitHub activity for the claimed product", "github.com"),
    ("https://www.sec.gov/cgi-bin/browse-edgar?company=resumegenie", "SEC filings search for the funding round", "sec.gov"),
    ("https://www.crunchbase.com/organization/resumegenie-ai", "Crunchbase funding record", "crunchbase.com"),
    ("https://apps.apple.com/us/app/resumegenie-ai", "App store footprint check", "apps.apple.com"),
    ("https://www.reddit.com/r/resumes/search?q=resumegenie", "Reddit mentions and complaints", "reddit.com"),
)

_DEMO_THOUGHTS_PERSON = (
    "Cross-checking Demo Sample's Senior Software Engineer role at Google against public record",
    "Searching for independent confirmation of Demo Sample's employment history",
    "Checking press and public filings for anything that contradicts the claimed timeline",
)

_DEMO_THOUGHTS_COMPANY = (
    "Cross-checking the $2 million pre-seed raise against SEC filings and press",
    "Company page loads, checking if the product actually ships or is just a landing page",
    "Checking if the proprietary AI claim is really just an OpenAI or Claude API call",
)


def _emit_demo_rich_sequence(emit, effective_scan_type: str, state: dict) -> None:
    """DEMO-ONLY: a fixed, realistic thought/website/image sequence so the
    overlay can be demoed offline with the full "showing its work" panel.
    Never raises: any failure here degrades to fewer demo events, never an
    error+done pair (a rich-event glitch must not look like a scan failure).
    """
    try:
        thoughts = _DEMO_THOUGHTS_COMPANY if effective_scan_type == "company_app" else _DEMO_THOUGHTS_PERSON
        sites = _DEMO_SITES_COMPANY if effective_scan_type == "company_app" else _DEMO_SITES_PERSON

        emit({"type": "thought", "text": thoughts[0]})

        seen: set = state.setdefault("seen_domains", set())
        emitted = 0
        for i, (url, snippet, domain) in enumerate(sites):
            if emitted >= _MAX_WEBSITE_CARDS_PER_SCAN:
                break
            emit(
                {
                    "type": "website",
                    "url": url,
                    "title": _title_from_snippet(snippet, domain),
                    "favicon": _favicon_url(domain),
                    "domain": domain,
                }
            )
            seen.add(domain)
            emitted += 1
            if emitted == 2:
                emit({"type": "thought", "text": thoughts[1]})
        state["website_count"] = state.get("website_count", 0) + emitted

        thumb_count = 0
        for _url, _snippet, domain in sites[:2]:
            # Same discipline as the live path: a favicon is kind="logo"
            # (contained, never stretched) and hero=False (thumbnail only).
            emit(
                {
                    "type": "image",
                    "url": _favicon_url(domain),
                    "caption": domain,
                    "kind": "logo",
                    "hero": False,
                }
            )
            thumb_count += 1
        state["thumbnail_count"] = state.get("thumbnail_count", 0) + thumb_count

        emit({"type": "thought", "text": thoughts[2]})
    except Exception:
        logger.exception("service: failed emitting demo rich sequence")


def _resolve_linkedin_url(name: str, company: Optional[str]) -> Optional[str]:
    """Best-effort: resolve a LinkedIn profile URL for a name (and, if known,
    current company) that vision_extract read off a screenshot but could not
    find an address-bar URL for. Uses the same web_search connector
    verify.py's evidence gathering already uses (search.web_search): no
    SEARXNG_URL/BRAVE_API_KEY configured returns [] harmlessly, same as every
    other caller of that function. Never raises: any failure here degrades
    to None, which the caller turns into an honest "could not identify a
    profile" error rather than a crash.
    """
    if not name:
        return None
    try:
        query = f'"{name}" {company or ""} site:linkedin.com/in'.strip()
        results = search.web_search(query, count=5)
        for r in results:
            candidate = (r.get("url") or "").strip()
            if "linkedin.com/in/" in candidate.lower():
                return candidate
    except Exception:
        logger.exception("service: failed resolving a linkedin url for %r", name)
    return None


def _name_tokens(name: str) -> set:
    """Lowercased, punctuation-stripped word tokens of a person name. Used by
    the identity-confirmation gate below. Never raises."""
    import re as _re

    if not name:
        return set()
    cleaned = _re.sub(r"[^a-z0-9\s]", " ", str(name).casefold())
    return {t for t in cleaned.split() if t}


def _names_plausibly_match(a: str, b: str) -> bool:
    """True when two person names plausibly refer to the same person: every
    token of the SHORTER name appears in the longer one (order-insensitive,
    case/punctuation-insensitive), which accepts an added middle name or a
    reordering while rejecting a clearly different person.

    Inconclusive-safe: if either side has no usable tokens we cannot assert a
    mismatch, so this returns True (never block a scan on missing data). This is
    the gate that stops a name-resolved URL from silently scanning the WRONG
    person: a real mismatch routes to needs_url instead of a scored verdict.
    """
    ta = _name_tokens(a)
    tb = _name_tokens(b)
    if not ta or not tb:
        return True
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return shorter <= longer


def _select_provider(job_id: str, is_demo: bool):
    """Pick the reasoning brain.

    ManualProvider (the queue-file flow) is ALWAYS the default, for the demo
    path and for a real scan, exactly like the CLI's own default: __main__.py
    only ever builds ApiProvider when the operator passes --provider api
    explicitly, it never auto-detects ApiProvider from ANTHROPIC_API_KEY /
    GEMINI_API_KEY just being present in the environment. This service
    mirrors that on purpose: a .env copied wholesale from another project
    (this repo's own .env carries keys used by unrelated tools) must never
    silently hijack every real scan into ApiProvider without an explicit
    opt-in, even now that ApiProvider actually calls Gemini (see llm.py).

    Set LARP_SERVICE_PROVIDER=codex to use a fresh, read-only Codex CLI
    reviewer authenticated through ChatGPT, with no API key. Set it to api to
    opt into Gemini explicitly. Both settings affect only real, non-demo
    scans. If either automated provider fails, _run_job catches
    ApiProviderError and falls back to the ManualProvider queue flow.

    ManualProvider is constructed with job_id=job_id so its queue file is
    always queue/<job_id>.json, i.e. exactly the path this service watches.
    """
    configured = os.environ.get("LARP_SERVICE_PROVIDER", "manual").strip().lower()
    if not is_demo and configured == "codex" and CodexProvider.available():
        return CodexProvider()
    wants_api = configured == "api"
    if not is_demo and wants_api:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if anthropic_key or gemini_key:
            return ApiProvider()
    return ManualProvider(job_id=job_id)


class _PlanWaitingManualProvider(ManualProvider):
    """A service-level ManualProvider that gives ONLY the director / planning
    round trip a bounded, blocking wait, so a LIVE scan actually round-trips
    the plan job the same way the vision and scoring jobs already do.

    Why this exists: build_dossier's stage 2.5 calls provider.plan_followups
    synchronously, mid-build, and then continues (still synchronously) through
    the mismatch detectors and the scoring step. The base ManualProvider,
    under its default MANUAL_QUEUE_TIMEOUT_S=0, WRITES the plan job and returns
    [] immediately, so on a live scan the director never runs: the operator
    never gets a chance to fill queue/<job_id>_plan.json before the detectors
    and scorer have already run over the un-enriched evidence.

    The fix is Option A's mechanism ("plan_followups blocks inside the executor
    thread polling the plan job while the service is free"), but scoped to ONLY
    this one method by overriding it here, instead of setting a global
    MANUAL_QUEUE_TIMEOUT_S > 0. The global env var is exactly what makes the
    naive Option A unsafe: it would ALSO make assign_tiers_and_verdict (and
    vision_extract) block inside build_dossier, colliding with this service's
    own async _watch_queue_and_finish scoring watch and stalling the demo path.
    Every OTHER provider method is inherited UNCHANGED, so scoring still returns
    the unscored dossier immediately and the existing scoring orchestration is
    untouched.

    Runs inside pipeline.run's run_in_executor worker thread, so its blocking
    poll (plain time.sleep, never asyncio.sleep) never stalls the event loop or
    the websocket. Bounded by plan_timeout_s, then proceeds with no follow-ups:
    the scan is NEVER hung by an unfilled plan job. Only ever consulted by the
    default "dossier" engine (build_dossier); the per_claim engine never calls
    plan_followups, so wrapping is a harmless no-op there.
    """

    def __init__(
        self,
        *args,
        plan_timeout_s: float,
        plan_emit=None,
        resolve_timeout_s: float = _RESOLVE_QUEUE_TIMEOUT_S,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._plan_timeout_s = plan_timeout_s
        self._resolve_timeout_s = resolve_timeout_s
        # Optional thread-safe status emitter (see _run_job): lets the plan
        # wait surface the same "watching X for completion" / "still waiting"
        # narration the vision and scoring watches already emit. Must be safe
        # to call from the executor thread (it hops back onto the loop).
        self._plan_emit = plan_emit

    def plan_followups(self, dossier_or_claims, identity=None):
        # Let the base provider WRITE the plan job (and idempotently read back
        # a job that is somehow already completed), using its own logic
        # unchanged. Under MANUAL_QUEUE_TIMEOUT_S=0 this returns [] right after
        # writing the pending file; if a completed file already exists it
        # returns those follow-ups WITHOUT rewriting it.
        followups = super().plan_followups(dossier_or_claims, identity)
        if followups:
            return followups

        path = self._plan_job_path()
        if not path.exists():
            # The base provider could not even write the job (an I/O problem it
            # already logged and degraded to [] for). Nothing to wait on.
            return []

        self._emit_plan_status(
            f"queued for operator to plan follow-ups; watching {path.name} for completion"
        )

        start = time.monotonic()
        last_status_at = start
        while True:
            done = ManualProvider._read_plan_if_completed(path)
            if done is not None:
                return done

            now = time.monotonic()
            if now - start > self._plan_timeout_s:
                # Bounded: proceed with NO follow-ups rather than hang the scan.
                self._emit_plan_status(
                    f"no operator plan within {self._plan_timeout_s:.0f}s; "
                    f"proceeding with no director follow-ups"
                )
                return []

            if now - last_status_at > _QUEUE_STATUS_EVERY_S:
                self._emit_plan_status(
                    f"still waiting for operator to plan follow-ups in {path.name}..."
                )
                last_status_at = now

            time.sleep(_QUEUE_POLL_INTERVAL_S)

    def resolve_product_site(self, requests_, identity=None, timeout_s=None):
        """Bounded, narrated wait for the product-site resolve job, mirroring
        plan_followups above.

        Passes timeout_s=0 down so the base provider only WRITES the job (and
        idempotently reads back one already completed) instead of inheriting the
        global MANUAL_QUEUE_TIMEOUT_S. That matters more here than for the plan
        job: stage 1.5 runs in FRONT of the evidence gather, so a long inherited
        wait would stall a live scan before it gathered anything. Bounded by
        _resolve_timeout_s, then proceeds unresolved: the scan is NEVER hung by
        an unfilled resolve job.
        """
        resolutions = super().resolve_product_site(requests_, identity, timeout_s=0.0)
        if resolutions:
            return resolutions

        path = self._resolve_job_path()
        if not path.exists():
            # Nothing was queued (no checkable product claim, or an I/O problem
            # the base provider already logged). Nothing to wait on.
            return []

        self._emit_plan_status(
            f"queued for operator to resolve the claimed product site(s); "
            f"watching {path.name} for completion"
        )

        start = time.monotonic()
        last_status_at = start
        while True:
            done = ManualProvider._read_resolve_if_completed(path)
            if done is not None:
                return done

            now = time.monotonic()
            if now - start > self._resolve_timeout_s:
                self._emit_plan_status(
                    f"no operator resolution within {self._resolve_timeout_s:.0f}s; "
                    f"proceeding with no resolved product site"
                )
                return []

            if now - last_status_at > _QUEUE_STATUS_EVERY_S:
                self._emit_plan_status(
                    f"still waiting for operator to resolve the product site in "
                    f"{path.name}..."
                )
                last_status_at = now

            time.sleep(_QUEUE_POLL_INTERVAL_S)

    def _emit_plan_status(self, text: str) -> None:
        if self._plan_emit is None:
            return
        try:
            self._plan_emit({"type": "status", "text": text})
        except Exception:
            logger.exception("service: failed emitting plan-wait status")


def _manual_provider_for_scan(
    job_id: str, is_demo: bool, plan_emit=None
) -> "ManualProvider":
    """Build the ManualProvider for a scan.

    A real (non-demo) scan gets the plan-waiting wrapper so the director /
    planning round trip actually happens (see _PlanWaitingManualProvider).
    The demo path keeps the plain ManualProvider: its plan_followups returns []
    immediately (no operator, nothing to wait for), so the demo never blocks on
    a plan job it will not fill, and its behavior is byte-for-byte unchanged.
    The plan timeout is read from the module attribute HERE (call time) so a
    test monkeypatching service._PLAN_QUEUE_TIMEOUT_S takes effect.
    """
    if is_demo:
        return ManualProvider(job_id=job_id)
    return _PlanWaitingManualProvider(
        job_id=job_id,
        plan_timeout_s=_PLAN_QUEUE_TIMEOUT_S,
        plan_emit=plan_emit,
        resolve_timeout_s=_RESOLVE_QUEUE_TIMEOUT_S,
    )


def _dossier_is_scored(dossier: Dossier) -> bool:
    if dossier.scan_type == "company_app":
        return (
            dossier.company_larp_score is not None
            and dossier.overall_larp_score is not None
        )
    company_complete = all(
        assessment.larp_score is not None
        for assessment in dossier.company_assessments
    )
    return (
        dossier.founder_larp_score is not None
        and company_complete
        and dossier.overall_larp_score is not None
    )


def _finalize_scores(dossier: Dossier) -> None:
    """Mirror pipeline.run's own post-provider finalize step (see
    pipeline.py's compute_company_score / compute_founder_score calls)
    exactly, for a dossier read back from a completed queue file after
    pipeline.run has already returned once (unscored).
    """
    finalize_dossier_scores(dossier)


def _persist_completed_dossier(dossier: Dossier, job_id: str) -> Optional[Path]:
    """Atomically persist the full scored dossier and its attempt ledger."""
    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id or "").strip("._")
    if not safe_job_id:
        return None
    path = QUEUE_DIR / f"{safe_job_id}_dossier.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(dossier.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path
    except Exception:
        logger.exception("could not persist completed dossier %s", path.name)
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _emit_final(emit, dossier: Dossier, *, job_id: str = "") -> None:
    """Emit the final claim tiers, scores, verdict, and done, in that order."""
    _persist_completed_dossier(dossier, job_id)
    for c in dossier.claims:
        tier = c.tier.value if isinstance(c.tier, EvidenceTier) else str(c.tier)
        emit({"type": "claim", "assertion": c.assertion, "tier": tier})
    emit(
        {
            "type": "scores",
            "founder_larp_score": dossier.founder_larp_score,
            "company_larp_score": dossier.company_larp_score,
            "overall_larp_score": dossier.overall_larp_score,
            "company_assessments": [
                {
                    "company_name": assessment.company_name,
                    "relationship": assessment.relationship,
                    "affects_overall": assessment.affects_overall,
                    "larp_score": assessment.larp_score,
                }
                for assessment in dossier.company_assessments
            ],
            # scan_depth rides the verdict payload so the overlay can brand a
            # shallow result (a degraded scan must never be screenshottable as a
            # real SUS finding). "full" for a normal live scan; "shallow" for an
            # injected/dev profile or a zero-experience scrape.
            "scan_depth": getattr(dossier, "scan_depth", "full") or "full",
        }
    )
    emit({"type": "verdict", "text": dossier.verdict or ""})
    emit({"type": "done"})


def _vision_result_is_empty(vision: dict) -> bool:
    """True if a vision_extract result has nothing usable in it: every one of
    the four fields is None/empty. Used to tell ManualProvider's immediate
    MANUAL_QUEUE_TIMEOUT_S=0 placeholder (nobody has looked at the screenshot
    yet) apart from a provider that actually looked and genuinely found
    nothing, so only the former triggers the vision-queue watch loop below.
    """
    return not any((vision or {}).get(k) for k in ("profile_url", "name", "headline", "company"))


async def _watch_vision_queue_and_finish(
    vision_provider: "ManualProvider", emit, timeout_s: float = _QUEUE_TIMEOUT_S
) -> Optional[dict]:
    """Poll queue/<job_id>_vision.json for status == "completed", the vision
    counterpart of _watch_queue_and_finish below.

    This is what actually lets the overlay's "Go" button screenshot fallback
    (layer 2) recover once an operator (human or fresh Codex reviewer) fills in the
    queued vision job: MANUAL_QUEUE_TIMEOUT_S=0 (the default) makes
    vision_extract return an empty result immediately after WRITING that
    file, so without this poll loop a completed vision job was never picked
    back up, and any retry only ever opened a fresh, never-to-be-filled file
    under a brand new job_id.

    Never blocks the event loop (asyncio.sleep between polls). Times out
    after timeout_s seconds (same default/env override as
    _watch_queue_and_finish) and returns None on timeout, never raises; the
    caller treats None the same as "nothing extracted".
    """
    path = vision_provider._vision_job_path()
    start = time.monotonic()
    last_status_at = start
    while True:
        result = ManualProvider._read_vision_if_completed(path)
        if result is not None:
            return result

        now = time.monotonic()
        if now - start > timeout_s:
            return None

        if now - last_status_at > _QUEUE_STATUS_EVERY_S:
            emit({"type": "status", "text": f"still waiting for operator to read {path.name}..."})
            last_status_at = now

        await asyncio.sleep(_QUEUE_POLL_INTERVAL_S)


async def _watch_queue_and_finish(
    queue_path: Path,
    emit,
    timeout_s: float = _QUEUE_TIMEOUT_S,
    *,
    job_id: str = "",
) -> None:
    """Poll queue_path for status == "completed", then finalize and emit.

    Never blocks the event loop (asyncio.sleep between polls). Times out
    after timeout_s seconds as a safety net (default 1800s, override via
    SERVICE_QUEUE_TIMEOUT_S) so a forgotten job cannot keep a background
    task alive forever.
    """
    reference_dossier = None
    try:
        pending_payload = json.loads(queue_path.read_text(encoding="utf-8"))
        reference_dossier = Dossier.from_dict(pending_payload.get("dossier", {}))
    except Exception:
        reference_dossier = None

    start = time.monotonic()
    last_status_at = start
    while True:
        dossier = ManualProvider._read_if_completed(
            queue_path, evidence_reference=reference_dossier
        )
        if dossier is not None:
            _finalize_scores(dossier)
            _emit_final(emit, dossier, job_id=job_id)
            return

        now = time.monotonic()
        if now - start > timeout_s:
            emit(
                {
                    "type": "error",
                    "text": f"timed out after {timeout_s:.0f}s waiting for the operator "
                    f"to complete {queue_path.name}",
                }
            )
            emit({"type": "done"})
            return

        if now - last_status_at > _QUEUE_STATUS_EVERY_S:
            emit({"type": "status", "text": f"still waiting for operator to score {queue_path.name}..."})
            last_status_at = now

        await asyncio.sleep(_QUEUE_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Demo path: bundled fixtures, no network, no login.
# ---------------------------------------------------------------------------


def _build_demo_profile(scan_type: str) -> dict:
    if scan_type == "company_app":
        return _demo_company_profile()
    return _demo_person_profile()


def _demo_person_profile() -> dict:
    # Reuses the same fixture-backed builder the CLI's --demo flag uses, so
    # the two paths never drift (see detective/__main__.py's _demo_profile).
    from .__main__ import _demo_profile as _cli_demo_profile

    return _cli_demo_profile()


def _demo_company_profile() -> dict:
    """Load the bundled company/app fixture (tests/cases/wrapper_app.json)
    the same way --company-file does: strip the human-grading-only
    "_expected" block (not part of the pipeline shape) and force
    scan_type to company_app.
    """
    data = json.loads(_DEMO_COMPANY_FIXTURE.read_text(encoding="utf-8"))
    data.pop("_expected", None)
    data["scan_type"] = "company_app"
    data.setdefault("profile_url", "https://resumegenie.example/")
    return data


def _auto_complete_demo_job(queue_path: Path) -> None:
    """DEMO-ONLY: fill in a pending ManualProvider queue job deterministically
    so the demo path (and tests/test_service.py) reaches scores, verdict, and
    done with no human or Codex operator in the loop. Never called for
    a real scan.

    Heuristic: a claim with any gathered evidence is marked CONFIRMED, one
    with none is left UNVERIFIED. Never marks a claim DISPROVEN: that tier
    needs evidence that actively contradicts the claim, which this simple
    heuristic has no way to judge, and a false DISPROVEN is exactly what a
    LARP detector must avoid.
    """
    if not queue_path.exists():
        return
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("demo auto-scorer: could not read %s", queue_path)
        return
    if data.get("status") == "completed":
        return

    dossier_dict = data.get("dossier", {}) or {}
    for claim in dossier_dict.get("claims", []) or []:
        claim["tier"] = (
            EvidenceTier.CONFIRMED.value if claim.get("evidence") else EvidenceTier.UNVERIFIED.value
        )
        if not claim.get("notes"):
            claim["notes"] = _DEMO_AUTO_SCORE_NOTE

    if dossier_dict.get("scan_type") == "company_app":
        dossier_dict["buildability"] = {"tier": "TRIVIAL", "note": _DEMO_AUTO_SCORE_NOTE}
        for m in dossier_dict.get("metric_breakdown", []) or []:
            if m.get("active") and m.get("name") != "buildability":
                m["score_0_10"] = 1
                m["note"] = _DEMO_AUTO_SCORE_NOTE
        dossier_dict["verdict"] = (
            "Demo scan (auto-scored, not a real reasoning pass): bundled fixture, "
            "no contradicting evidence found for any claim."
        )
    else:
        dossier_dict["larp_score"] = 5
        for assessment in dossier_dict.get("company_assessments", []) or []:
            assessment["buildability"] = {
                "tier": "MODERATE",
                "note": _DEMO_AUTO_SCORE_NOTE,
            }
            for metric in assessment.get("metric_breakdown", []) or []:
                if metric.get("active") and metric.get("name") != "buildability":
                    metric["score_0_10"] = 1
                    metric["note"] = _DEMO_AUTO_SCORE_NOTE
        dossier_dict["verdict"] = (
            "Demo scan (auto-scored, not a real reasoning pass): bundled fixture profile, "
            "no contradicting evidence found for any claim."
        )

    data["status"] = "completed"
    data["dossier"] = dossier_dict
    queue_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
