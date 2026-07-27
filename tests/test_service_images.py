"""Offline tests for the rich-image wiring in detective/service.py.

Exercises the two new service helpers directly (no websocket, no network): the
progressive per-employer Clearbit logo events and the bounded verdict-step
image batch (og:image thumbnails first, proxied profile photo last). All image
network functions are mocked; nothing here hits the real network.

No em dashes anywhere (house rule).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from detective import service


def _claim(ctype="employment", employer="", evidence=None, assertion="a"):
    return SimpleNamespace(
        type=ctype,
        employer=employer,
        evidence=evidence or [],
        assertion=assertion,
        tier="UNVERIFIED",
    )


def _fresh_state(scan_type="person"):
    return {
        "scan_type": scan_type,
        "seen_domains": set(),
        "website_count": 0,
        "thumbnail_count": 0,
        "hero_emitted": False,
        "seen_employers": set(),
        "logo_count": 0,
        "og_candidates": [],
        "allow_network_images": True,
    }


# ---------------------------------------------------------------------------
# Source #2: per-employer Clearbit logos, progressive + deduped + capped
# ---------------------------------------------------------------------------


def test_employer_logo_guessed_domain():
    state = _fresh_state()
    out = service._employer_logo_events_for_claim(_claim(employer="Stripe"), state)
    assert out == [
        {
            "type": "image",
            "url": "https://logo.clearbit.com/stripe.com",
            "caption": "Stripe logo",
            "fallback": "https://www.google.com/s2/favicons?domain=stripe.com&sz=256",
            "kind": "logo",
            "hero": False,
        }
    ]
    assert state["logo_count"] == 1


def test_employer_logo_fallback_is_a_different_working_host():
    # The whole point of the fallback: it must NOT be the same dead
    # logo.clearbit.com host as "url", so the overlay has a real second
    # source to swap to when the primary fails to load client-side.
    state = _fresh_state()
    out = service._employer_logo_events_for_claim(_claim(employer="Acme"), state)
    assert "logo.clearbit.com" not in out[0]["fallback"]
    assert out[0]["fallback"]


def test_employer_logo_prefers_own_domain_from_evidence():
    ev = [{"source_url": "https://stripe.com/careers", "snippet": "x"}]
    state = _fresh_state()
    out = service._employer_logo_events_for_claim(_claim(employer="Stripe", evidence=ev), state)
    assert out[0]["url"] == "https://logo.clearbit.com/stripe.com"


def test_employer_logo_dedups_same_employer():
    state = _fresh_state()
    first = service._employer_logo_events_for_claim(_claim(employer="Google"), state)
    second = service._employer_logo_events_for_claim(_claim(employer="google"), state)
    assert first and second == []
    assert state["logo_count"] == 1


def test_employer_logo_capped_per_scan():
    state = _fresh_state()
    emitted = 0
    for i in range(10):
        emitted += len(service._employer_logo_events_for_claim(_claim(employer=f"Company{i}"), state))
    assert emitted == service._MAX_EMPLOYER_LOGOS


def test_employer_logo_skipped_for_company_scan():
    state = _fresh_state(scan_type="company_app")
    assert service._employer_logo_events_for_claim(_claim(employer="Stripe"), state) == []


# ---------------------------------------------------------------------------
# Company hero: same Clearbit + fallback pairing, up front for a company_app
# scan (the root-cause repro: logo.clearbit.com is confirmed dead, so a real
# fallback riding along is what actually gets a company image on screen).
# ---------------------------------------------------------------------------


def test_company_hero_carries_a_fallback():
    state = _fresh_state(scan_type="company_app")
    emitted = []
    service._emit_company_hero(lambda m: emitted.append(m), "https://stripe.com/careers", state)
    assert len(emitted) == 1
    msg = emitted[0]
    assert msg["url"] == "https://logo.clearbit.com/stripe.com"
    assert msg["fallback"] == "https://www.google.com/s2/favicons?domain=stripe.com&sz=256"
    assert state["hero_emitted"] is True
    # The blur/flicker fix: a Clearbit/favicon company mark is "logo" (the
    # overlay must render it small+contained, never stretched), and it IS
    # this scan's one true hero (hero=True), stable for the whole scan.
    assert msg["kind"] == "logo"
    assert msg["hero"] is True


# ---------------------------------------------------------------------------
# End-to-end-ish: a profile with several employers streams MULTIPLE distinct
# image events (not just the one eventual profile photo), which is the whole
# point of this fix. Drives _pipeline_event_to_messages directly, exactly the
# way _run_job's `progress` callback does for every pipeline "claim" event.
# ---------------------------------------------------------------------------


def test_claim_stream_emits_multiple_image_events_for_employers():
    state = _fresh_state()
    all_events = []
    for employer in ("Stripe", "Google", "Notion"):
        claim = _claim(employer=employer, assertion=f"Worked at {employer}")
        all_events.extend(service._pipeline_event_to_messages("claim", claim, state))

    image_events = [e for e in all_events if e["type"] == "image"]
    # One Clearbit-plus-fallback logo per distinct employer: three real,
    # DISTINCT image events, not a single lonely profile photo.
    assert len(image_events) == 3
    urls = {e["url"] for e in image_events}
    assert len(urls) == 3
    assert all(e.get("fallback") for e in image_events)
    # Every per-employer logo is kind="logo" (render small+contained, never
    # stretched into a blurry hero) and hero=False (thumbnail only, so
    # streaming one mid-scan can never bump whatever the real hero is).
    assert all(e["kind"] == "logo" for e in image_events)
    assert all(e["hero"] is False for e in image_events)


def test_employer_logo_skipped_for_non_employment_claim():
    state = _fresh_state()
    assert service._employer_logo_events_for_claim(_claim(ctype="identity", employer="Stripe"), state) == []


# ---------------------------------------------------------------------------
# Sources #1 + #3: bounded verdict image batch, photo emitted LAST (hero)
# ---------------------------------------------------------------------------


def test_verdict_images_offline_guard_returns_nothing():
    state = _fresh_state()
    state["allow_network_images"] = False
    state["og_candidates"] = ["https://example.com/a"]
    dossier = SimpleNamespace(identity={"name": "Jane", "image": "https://media.licdn.com/x.jpg"})
    with mock.patch.object(service.images, "proxy_image_as_data_uri", side_effect=AssertionError("no net")):
        assert service._verdict_image_events(dossier, state) == []


def test_verdict_images_photo_emitted_last_as_hero():
    state = _fresh_state()
    state["og_candidates"] = ["https://a.com/1", "https://b.com/2"]
    dossier = SimpleNamespace(identity={"name": "Jane Doe", "image": "https://media.licdn.com/p.jpg"})

    def fake_og(url, timeout=4.0):
        return (f"{url}/og.png", f"title-for-{url}")

    with mock.patch.object(service.images, "fetch_og_image", side_effect=fake_og), \
        mock.patch.object(service.images, "proxy_image_as_data_uri", return_value="data:image/jpeg;base64,AAAA"), \
        mock.patch.object(service.images, "load_linkedin_cookies", return_value=None):
        out = service._verdict_image_events(dossier, state)

    assert all(e["type"] == "image" for e in out)
    # Two og thumbnails, then the proxied photo LAST.
    assert len(out) == 3
    assert out[-1]["url"].startswith("data:image/jpeg;base64,")
    assert out[-1]["caption"] == "Jane Doe"
    assert all(e["url"].endswith("/og.png") for e in out[:2])
    assert state["hero_emitted"] is True
    # The og:image thumbnails are real photos (kind="photo") but hero=False:
    # a source thumbnail must never be able to hijack the hero slot. The
    # proxied identity photo is the one and only hero=True event.
    assert all(e["kind"] == "photo" for e in out)
    assert [e["hero"] for e in out] == [False, False, True]


def test_verdict_images_photo_miss_is_graceful():
    # A 403/timeout on the photo proxy -> None -> no photo event, no error.
    state = _fresh_state()
    state["og_candidates"] = ["https://a.com/1"]
    dossier = SimpleNamespace(identity={"name": "Jane", "image": "https://media.licdn.com/p.jpg"})
    with mock.patch.object(service.images, "fetch_og_image", return_value=("https://a.com/1/og.png", "T")), \
        mock.patch.object(service.images, "proxy_image_as_data_uri", return_value=None), \
        mock.patch.object(service.images, "load_linkedin_cookies", return_value=None):
        out = service._verdict_image_events(dossier, state)
    assert len(out) == 1
    assert out[0]["url"] == "https://a.com/1/og.png"
    assert state["hero_emitted"] is False
    assert out[0]["kind"] == "photo"
    assert out[0]["hero"] is False


def test_verdict_images_company_scan_has_no_photo():
    # company_app: no identity photo proxied here (its Clearbit hero is up
    # front); only og:image thumbnails.
    state = _fresh_state(scan_type="company_app")
    state["og_candidates"] = ["https://a.com/1"]
    dossier = SimpleNamespace(identity={"name": "ResumeGenie"})
    with mock.patch.object(service.images, "fetch_og_image", return_value=("https://a.com/1/og.png", "T")), \
        mock.patch.object(service.images, "proxy_image_as_data_uri", side_effect=AssertionError("no photo")):
        out = service._verdict_image_events(dossier, state)
    assert out == [
        {"type": "image", "url": "https://a.com/1/og.png", "caption": "T", "kind": "photo", "hero": False}
    ]
