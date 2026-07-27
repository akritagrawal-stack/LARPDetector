"""Weighted source registry for the LARP detector's independent data-source
connectors.

Weight formula: weight = (credibility x parsability x independence) / 125

Each factor is scored 1 to 5 (5 x 5 x 5 = 125, the ceiling), so weight lands
in (0, 1.0]:
  - credibility   : how much a hit from this source, standing alone, is
                    worth trusting.
  - parsability   : how cleanly the source's data turns into a structured
                    evidence record (a documented JSON/XML API scores high;
                    a fragile HTML scrape scores low).
  - independence  : how free the source is from the claim's own subject
                    self-reporting about themselves. A company's own claim
                    about itself scores low; a third party with no
                    incentive to lie for the subject scores high.

Batch 1 implemented 4 connectors: github, sec_edgar, wayback, domain_age.
Batch 2 (tech/research-substance cluster) added 4 more: uspto, arxiv,
openalex, packages. Batch 3 (final P0 batch) adds the last 3: app_store
(converting the app_store_play_store_reviews seed row to implemented,
Apple only this batch, Google Play is P1), accelerator_badges, and
hackernews (both new rows, not part of the original seed set).

Batch 4 (first P1 batch) adds 2 more, both new rows (not part of the
original seed set): techstack (the vibecode/no-code fingerprinter backing
the buildability read) and courtlistener (fraud/legal-record search: a real
federal court record is credible and fully independent of the subject's own
claims, but free-text case captions parse less cleanly than a structured
filing, and this source carries the highest same-name false-positive risk
of any connector here, which is a match_confidence policy matter, not a
registry-weight one; see courtlistener.py's own module docstring).

Every row below is now either implemented or a genuinely out-of-scope seed
(companies_house_or_state_sos, dns_certificate_transparency, crunchbase,
linkedin_company_page, glassdoor_or_similar): `implemented=False` rows carry
`connector=None` and calling them is not supported.

Public surface:
    SOURCES        -- list[SourceDef], the full P0 registry (seed + implemented)
    weight_for(name) -- float, the weight for a source name (0.5 default if
                        the name is not registered, so a caller can never
                        crash on a typo'd or future source name)
    get_source(name) -- Optional[SourceDef]

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Mid-weight fallback for any evidence record whose source is not in this
# registry (e.g. existing web-search evidence, which predates this weighting
# scheme and carries no source_name at all). Deliberately the midpoint of the
# 0 to 1.0 weight range, not the ceiling and not the floor: an un-weighted
# hit should neither dominate nor be dismissed by a reasoning provider doing
# source-weighted scoring.
DEFAULT_WEIGHT = 0.5


@dataclass(frozen=True)
class SourceDef:
    """One row of the weighted source registry.

    name        : short, stable identifier used as evidence["source_name"].
    credibility : 1 to 5.
    parsability : 1 to 5.
    independence: 1 to 5.
    implemented : whether a connector module in detective/sources actually
                  exists and is wired into verify.py yet.
    connector   : dotted "module.function" path, for reference only (never
                  imported dynamically from this string; it is documentation,
                  not a dispatch table).
    note        : one line on why the source scores the way it does.
    """

    name: str
    credibility: int
    parsability: int
    independence: int
    implemented: bool
    connector: Optional[str]
    note: str

    @property
    def weight(self) -> float:
        return round(
            (self.credibility * self.parsability * self.independence) / 125.0, 4
        )


# The full P0 source set. Order: implemented sources first (batch 1, then
# batch 2), then the seeded reference set for a future batch.
SOURCES: list[SourceDef] = [
    SourceDef(
        name="github",
        credibility=4,
        parsability=5,
        independence=3,
        implemented=True,
        connector="detective.sources.github.verify_github",
        note=(
            "Account and push timestamps are platform records, but profile fields, "
            "repository descriptions, and published code remain subject-controlled. "
            "Useful for artifact verification, not independent proof of a job title."
        ),
    ),
    SourceDef(
        name="sec_edgar_form_d",
        credibility=5,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.sec_edgar.verify_sec",
        note=(
            "Official federal filing under penalty of law, but it is filed "
            "BY the company about itself, so independence is docked one "
            "notch from a pure third party."
        ),
    ),
    SourceDef(
        name="wayback_machine",
        credibility=5,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.wayback.verify_wayback",
        note=(
            "The archive itself is a neutral third-party record of when a "
            "page existed, but the archived content is still the subject's "
            "own words frozen in time, so independence is docked one notch."
        ),
    ),
    SourceDef(
        name="domain_rdap_whois",
        credibility=4,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.domain_age.verify_domain_age",
        note=(
            "Creation date is reliable, but registrant identity is usually "
            "privacy-masked, so credibility is docked one notch versus a "
            "source that verifies identity as well as timeline."
        ),
    ),
    # --- Batch 2 (tech/research-substance cluster): implemented. ---
    SourceDef(
        name="uspto_patents_trademarks",
        credibility=5,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.uspto.verify_uspto",
        note=(
            "Federal patent record: granted-vs-pending status is un-fakeable once "
            "queried, but independence is docked one notch because the underlying "
            "application data is filed BY the applicant/assignee, same reasoning as "
            "sec_edgar_form_d."
        ),
    ),
    SourceDef(
        name="arxiv",
        credibility=4,
        parsability=5,
        independence=5,
        implemented=True,
        connector="detective.sources.arxiv.verify_arxiv",
        note=(
            "Submission timestamp is un-backdatable and the repository is neutral "
            "third-party infrastructure, but arXiv is NOT peer-reviewed, so "
            "credibility is docked one notch versus a source that also vets content."
        ),
    ),
    SourceDef(
        name="openalex",
        credibility=4,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.openalex.verify_openalex",
        note=(
            "Aggregates third-party citation/affiliation data (independent of the "
            "subject's own claims), but its own author-disambiguation algorithm can "
            "merge namesakes, so both credibility and independence are docked one "
            "notch versus a source with a cleaner 1:1 identity mapping."
        ),
    ),
    SourceDef(
        name="packages",
        credibility=5,
        parsability=5,
        independence=5,
        implemented=True,
        connector="detective.sources.packages.verify_packages",
        note=(
            "npm/PyPI registry data (version history, publish dates, maintainer "
            "identity) is registry-controlled and un-backdatable, not a self-reported "
            "claim, the same reasoning github.py's account-creation-date weight uses."
        ),
    ),
    # --- Batch 3 (final P0 batch): implemented. ---
    SourceDef(
        name="app_store_play_store_reviews",
        credibility=5,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.app_store.verify_app_store",
        note=(
            "Apple only this batch (Google Play is P1). Rating counts and review dates are "
            "real third-party (user-generated) footprint data via a clean JSON API, but the "
            "listing metadata this connector also reports (current version, release dates) is "
            "self-reported by the developer, so independence is docked one notch from a pure "
            "arms-length third party."
        ),
    ),
    SourceDef(
        name="product_site",
        credibility=3,
        parsability=5,
        independence=2,
        implemented=True,
        connector="detective.sources.product_site.probe_site",
        note=(
            "The product's OWN website. Trivially parsable and unambiguous once "
            "resolved, but it is the subject's self-published property: anyone can "
            "put up a landing page, so independence is the lowest in the table and "
            "credibility is mid. Deliberately weak on purpose. A live site "
            "substantiates that the PRODUCT exists, never the person's role in it, "
            "and this connector is excluded from the corroborating-source set for "
            "exactly that reason."
        ),
    ),
    SourceDef(
        name="accelerator_badges",
        credibility=5,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.accelerators.verify_accelerator",
        note=(
            "YC's own Algolia-backed directory and Techstars' own portfolio-highlight widget "
            "are each the accelerator's own record of who it backed, not the company's own "
            "claim about itself, but each directory's per-company fields (one-liner, "
            "description) are still self-submitted by the company within that listing, docking "
            "independence one notch, same reasoning as sec_edgar_form_d."
        ),
    ),
    SourceDef(
        name="hackernews",
        credibility=4,
        parsability=5,
        independence=4,
        implemented=True,
        connector="detective.sources.hackernews.verify_hackernews",
        note=(
            "Thread/comment text is genuinely third-party (strangers discussing the product), "
            "but a Show HN thread is typically posted BY the founder, and HN's contrarian/snark "
            "bias plus its complete lack of any identity verification for commenters docks "
            "credibility one notch versus a moderated or filed source."
        ),
    ),
    # --- Batch 4 (first P1 batch): implemented. ---
    SourceDef(
        name="techstack",
        credibility=3,
        parsability=4,
        independence=4,
        implemented=True,
        connector="detective.sources.techstack.verify_techstack",
        note=(
            "A no-code/LLM-wrapper marker match on the fetched page is real and "
            "observable, and an optional headless-browser pass can verify that the "
            "public client renders. This still has no private-workflow or backend "
            "visibility, no JS-bundle crawl, and no APK decompile, so "
            "credibility is docked two notches versus a filed record; independence is "
            "docked one notch because the fetched HTML is still the company's own "
            "public-facing page, not a third party's account of it."
        ),
    ),
    SourceDef(
        name="courtlistener",
        credibility=5,
        parsability=3,
        independence=5,
        implemented=True,
        connector="detective.sources.courtlistener.verify_courtlistener",
        note=(
            "Federal court dockets and opinions are a fully independent, credible "
            "record with no self-reporting involved, but full-text case-caption "
            "search returns free-text hits that do not always parse into a clean "
            "structured identity match, docking parsability versus a source with a "
            "documented per-field schema; see courtlistener.py's own module "
            "docstring for why this source also carries the highest same-name "
            "false-positive risk of any connector here (a match_confidence policy "
            "matter, kept separate from this weight)."
        ),
    ),
    # --- Batch 5 (entity-targeted sources): implemented. ---
    SourceDef(
        name="org_roster",
        credibility=3,
        parsability=3,
        independence=4,
        implemented=True,
        connector="detective.sources.org_roster.verify_org_roster",
        note=(
            "The org's own public team/people page listing the subject is a real "
            "third-party record (the org maintains it, not the individual), docking "
            "independence only one notch for the founder-lists-self-on-own-company "
            "case. Credibility and parsability are both docked hard because roster-page "
            "discovery is best-effort (a heuristic web search plus a single raw-HTML "
            "fetch, no JS execution, easily wrong org/aggregator), and a name-substring "
            "match over stripped HTML parses far less cleanly than a filed record; "
            "absence is never disproof (a match_confidence policy, see "
            "org_roster.py's own module docstring)."
        ),
    ),
    SourceDef(
        name="news_coverage",
        credibility=4,
        parsability=3,
        independence=5,
        implemented=True,
        connector="detective.sources.news.verify_news",
        note=(
            "Genuine editorial coverage from an independent outlet is fully "
            "independent of the subject's own self-reporting (max independence); "
            "credibility is docked one notch because a search snippet cannot prove "
            "the article is about this exact (possibly same-named) subject and the "
            "connector cannot fully guarantee an outlet hit is not a syndicated "
            "release; parsability is docked because a free-text news snippet has no "
            "structured per-field schema. Reprints of the subject's OWN press "
            "release are separated out and marked low match_confidence rather than "
            "docked from this weight, same policy-vs-weight split as courtlistener."
        ),
    ),
    # --- Seeded for a future batch: not implemented yet, connector is None. ---
    SourceDef(
        name="pitchbook",
        credibility=5,
        parsability=4,
        independence=4,
        implemented=False,
        connector=None,
        note=(
            "Institutional funding/role data; ALREADY wired separately via "
            "detective.pitchbook (its own budget/auth gating). Listed here "
            "only so its weight is comparable to the sources above."
        ),
    ),
    SourceDef(
        name="companies_house_or_state_sos",
        credibility=5,
        parsability=4,
        independence=4,
        implemented=False,
        connector=None,
        note=(
            "Corporate registration record (incorporation date, officers); "
            "parsability varies a lot by jurisdiction."
        ),
    ),
    SourceDef(
        name="dns_certificate_transparency",
        credibility=4,
        parsability=4,
        independence=4,
        implemented=False,
        connector=None,
        note="crt.sh cert-transparency logs corroborate a domain's real infra timeline.",
    ),
    SourceDef(
        name="crunchbase",
        credibility=4,
        parsability=4,
        independence=3,
        implemented=False,
        connector=None,
        note="Crowd/self-submitted profile data; useful but lower independence than a filing.",
    ),
    SourceDef(
        name="linkedin_company_page",
        credibility=3,
        parsability=3,
        independence=3,
        implemented=False,
        connector=None,
        note="Public headcount/follower signal; easy to inflate, no verification layer.",
    ),
    SourceDef(
        name="glassdoor_or_similar",
        credibility=3,
        parsability=3,
        independence=3,
        implemented=False,
        connector=None,
        note="Employee review signal; self-selected sample, moderate independence.",
    ),
]

_BY_NAME: dict[str, SourceDef] = {s.name: s for s in SOURCES}


def get_source(name: str) -> Optional[SourceDef]:
    return _BY_NAME.get(name)


def weight_for(name: str) -> float:
    """Weight for a registered source name, or DEFAULT_WEIGHT if unknown.

    Never raises: an unregistered name (a typo, or a future source that has
    not been added here yet) degrades to the mid-weight default rather than
    crashing a connector or dropping evidence.
    """
    source = _BY_NAME.get(name)
    if source is None:
        logger.warning("registry: unknown source name %r, using DEFAULT_WEIGHT", name)
        return DEFAULT_WEIGHT
    return source.weight
