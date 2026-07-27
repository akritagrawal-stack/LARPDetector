"""CLI entry point: python -m detective <linkedin_url> [options].

  python -m detective https://www.linkedin.com/in/someone/
  python -m detective https://www.linkedin.com/in/someone/ --live
  python -m detective <url> --provider api
  python -m detective --demo            # run on a bundled offline sample
  python -m detective --profile <path.json>          # offline person scan
  python -m detective --company <url> --live         # live company/app scan
  python -m detective --company-file <path.json>     # offline company/app scan

--company / --company-file run the second scan type: a company/app LARP scan
that produces a buildability meter (tier: TRIVIAL/MODERATE/HARD, plus a
one-line note) alongside the usual claim tiers. Buildability is a FACTOR the
reasoning step folds into larp_score, not a separate rebuild plan: a
trivially vibecodeable product sold at a premium scores higher on LARP.

--help must work with zero side effects (no provider, no network): argparse
handles it before anything else runs.

No em dashes in this file (house rule).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .models import Dossier, EvidenceTier
from .pipeline import safe_print


def _reconfigure_utf8_streams() -> None:
    """Best-effort: reconfigure stdout/stderr to UTF-8 so printing a name
    with non-ASCII characters (e.g. "Gregor Zunic") never crashes with a
    UnicodeEncodeError on a narrow console codepage (Windows cp1252). Guarded
    for streams that predate Python 3.7's reconfigure() (or that a test/host
    swapped out for something without it); never raises. See _print_dossier
    and pipeline.safe_print, the two places a profile's own text reaches the
    console.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="detective",
        description="LARP detector: turn a LinkedIn URL into an evidence Dossier.",
    )
    p.add_argument("url", nargs="?", help="LinkedIn profile URL to analyze.")
    p.add_argument(
        "--live",
        action="store_true",
        help="Allow a real LinkedIn fetch (gated; respects the scraper caps). "
        "Without this, live fetching is refused to protect the account.",
    )
    p.add_argument(
        "--provider",
        choices=["manual", "api"],
        default="manual",
        help="Reasoning brain: 'manual' (default, $0 queue file) or 'api' (Gemini key).",
    )
    p.add_argument(
        "--engine",
        choices=["dossier", "per_claim"],
        default="dossier",
        help="Detection engine: hardened aggregate dossier (default) or legacy per-claim.",
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help="Run the pipeline on a bundled offline sample profile (no network).",
    )
    p.add_argument(
        "--profile",
        default=None,
        metavar="PATH",
        help="Load a raw profile dict from a JSON file and run the pipeline on "
        "it (no fetch, real evidence search). Useful for validating reasoning "
        "against documented public cases without a live LinkedIn scrape.",
    )
    p.add_argument(
        "--company",
        default=None,
        metavar="URL",
        help="Product/company landing page URL to run a company_app scan on "
        "(live fetch, gated the same way as --live: refused without --live). "
        "Produces a company/app dossier with a buildability meter (tier plus "
        "note) instead of a person dossier.",
    )
    p.add_argument(
        "--company-file",
        default=None,
        metavar="PATH",
        help="Load a raw company profile dict from a JSON file and run a "
        "company_app scan on it (no fetch, real evidence search). Useful for "
        "offline testing of the teardown pipeline without a live scrape.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Path to write the dossier JSON (default: ./<name>_dossier.json).",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    return p


def _make_provider(name: str):
    from .llm import ManualProvider, ApiProvider

    return ApiProvider() if name == "api" else ManualProvider()


def _demo_profile() -> dict:
    """Build a raw profile from the bundled experience fixture (offline)."""
    fixture = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "experience_section.html"
    )
    from .extract_linkedin import parse_experience_html

    html = fixture.read_text(encoding="utf-8") if fixture.exists() else ""
    return {
        "profile_url": "https://www.linkedin.com/in/demo-sample/",
        "identity": {
            "name": "Demo Sample",
            "headline": "Senior Software Engineer at Google",
            "current_company": "Google",
            "location": "Mountain View, California",
        },
        "experience": parse_experience_html(html),
        "education": [],
    }


def _load_company_profile_from_file(path_str: str) -> dict:
    """Load a raw company profile dict from a JSON file for --company-file.

    Expects the shape parse_company_page produces:
        {
          "profile_url": str,
          "scan_type": "company_app",
          "identity": {"name", "headline", "current_company", "location"},
          "pricing": {"tiers": [{"name", "price", "period"}]},
          "metrics": [{"type": "user_count"|"revenue_metric"|"funding", "text", "value"}],
          "tech_claims": [{"type": "proprietary_tech", "text"}],
          "integrations": [str, ...]
        }
    Missing keys default to empty, same tolerance as _load_profile_from_file.
    """
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"error: --company-file file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"error: --company-file file is not valid JSON: {exc}")

    data["scan_type"] = "company_app"
    data.setdefault("identity", {})
    data.setdefault("pricing", {"tiers": []})
    data.setdefault("metrics", [])
    data.setdefault("tech_claims", [])
    data.setdefault("integrations", [])
    data.setdefault("profile_url", f"file://{path.resolve()}")
    return data


def _load_profile_from_file(path_str: str) -> dict:
    """Load a raw profile dict from a JSON file for --profile.

    Expects the same shape as the demo fixture builds:
        {
          "profile_url": str,
          "identity": {"name", "headline", "current_company", "location"},
          "experience": [{"title", "company", "start_date", "end_date", "location"}],
          "education":  [{"school", "degree", "start_date", "end_date"}]
        }
    Also accepts "raw_experience" as an alias for "experience" so validation
    case files that mirror the Dossier field name still load correctly.
    """
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"error: --profile file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"error: --profile file is not valid JSON: {exc}")

    if "experience" not in data and "raw_experience" in data:
        data["experience"] = data["raw_experience"]

    data.setdefault("identity", {})
    data.setdefault("experience", [])
    data.setdefault("education", [])
    data.setdefault("profile_url", f"file://{path.resolve()}")
    return data


def _print_dossier(d: Dossier) -> None:
    # Bug 2: a profile's own text (name, headline, claim assertions) can
    # carry non-ASCII characters (e.g. "Gregor Zunic"), which used to crash
    # this whole function with a UnicodeEncodeError on a narrow console
    # codepage (Windows cp1252). safe_print never raises on that; main() also
    # reconfigures stdout/stderr to UTF-8 at CLI entry (see
    # _reconfigure_utf8_streams), so both layers cover this.
    safe_print("\n" + "=" * 68)
    safe_print(f"DOSSIER  {d.profile_url}")
    safe_print(f"Scan type : {d.scan_type}")
    safe_print("=" * 68)
    ident = d.identity or {}
    if d.scan_type == "company_app":
        safe_print(f"Product   : {ident.get('name', '')}")
        safe_print(f"Tagline   : {ident.get('headline', '')}")
    else:
        safe_print(f"Name      : {ident.get('name', '')}")
        safe_print(f"Headline  : {ident.get('headline', '')}")
        safe_print(f"Company   : {ident.get('current_company', '')}")
        safe_print(f"Location  : {ident.get('location', '')}")
    if d.scan_type == "company_app":
        safe_print(
            f"COMPANY LARP score: "
            f"{d.company_larp_score if d.company_larp_score is not None else 'UNSCORED (pending operator)'}"
        )
    else:
        safe_print(
            f"FOUNDER LARP score: "
            f"{d.founder_larp_score if d.founder_larp_score is not None else 'UNSCORED (pending operator)'}"
        )
    safe_print(
        f"legacy larp_score : {d.larp_score if d.larp_score is not None else 'UNSCORED (pending operator)'}"
    )
    if d.buildability is not None:
        b = d.buildability
        safe_print(f"Buildability meter: {b.tier or 'UNFILLED (pending operator)'}")
        if b.note:
            safe_print(f"  note: {b.note}")
    if d.verdict:
        safe_print(f"Verdict   : {d.verdict}")
    if d.metric_breakdown:
        safe_print("-" * 68)
        safe_print("Metric breakdown:")
        for m in d.metric_breakdown:
            status = "active" if m.active else "inactive (redistributed)"
            score = m.score_0_10 if m.score_0_10 is not None else "unfilled"
            safe_print(f"  [{status:>24}] w={m.weight}  {m.name:<20} score={score}")
            if m.note:
                safe_print(f"      note: {m.note}")
    safe_print("-" * 68)
    for i, c in enumerate(d.claims, 1):
        tier = c.tier.value if isinstance(c.tier, EvidenceTier) else c.tier
        safe_print(f"[{i}] ({tier}) {c.assertion}")
        for ev in c.evidence[:3]:
            safe_print(f"      - {ev.get('source_url', '')}")
        if c.notes:
            safe_print(f"      note: {c.notes}")
    safe_print("=" * 68 + "\n")


def main(argv: list[str] | None = None) -> int:
    _reconfigure_utf8_streams()
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Load a local .env if python-dotenv is available (optional).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    if (
        not args.demo
        and not args.profile
        and not args.company
        and not args.company_file
        and not args.url
    ):
        print(
            "error: provide a LinkedIn URL, or use --demo, --profile <path.json>, "
            "--company <url>, or --company-file <path.json>.",
            file=sys.stderr,
        )
        return 2

    from . import pipeline

    provider = _make_provider(args.provider)

    if args.demo:
        raw = _demo_profile()
        dossier = pipeline.run(
            raw["profile_url"],
            provider=provider,
            raw_profile=raw,
            engine=args.engine,
            offline=True,
        )
    elif args.profile:
        raw = _load_profile_from_file(args.profile)
        dossier = pipeline.run(
            raw["profile_url"], provider=provider, raw_profile=raw, engine=args.engine
        )
    elif args.company_file:
        raw = _load_company_profile_from_file(args.company_file)
        dossier = pipeline.run(
            raw["profile_url"],
            provider=provider,
            raw_profile=raw,
            scan_type="company_app",
            engine=args.engine,
        )
    elif args.company:
        dossier = pipeline.run(
            args.company,
            provider=provider,
            live=args.live,
            scan_type="company_app",
            engine=args.engine,
        )
    else:
        dossier = pipeline.run(
            args.url, provider=provider, live=args.live, engine=args.engine
        )

    _print_dossier(dossier)

    # Write JSON to disk.
    if args.out:
        out_path = Path(args.out)
    else:
        default_name = "company" if dossier.scan_type == "company_app" else "profile"
        name = (dossier.identity or {}).get("name", default_name).strip() or default_name
        slug = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
        out_path = Path.cwd() / f"{slug}_dossier.json"
    out_path.write_text(
        json.dumps(dossier.to_dict(), indent=2), encoding="utf-8"
    )
    print(f"Wrote dossier JSON to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
