# LARP Detector: Architecture Reference

A working reference for the detection engine under `detective/`. It documents,
accurately from the source: every data source connector, the full scan
pipeline, the two-score model, and the provider abstraction. Read this to
re-understand the system end to end.

House rule: no em dashes anywhere in this file.

---

## 1. What the engine does

The LARP Detector takes one public profile (a person, e.g. a LinkedIn-style
profile, or a company/app landing page) and produces a scored **Dossier**: a
set of decomposed claims, each assigned an evidence **tier**, plus a
deterministic **LARP score** and a free-form **verdict**.

Two design commitments run through the whole system:

1. **Evidence gathering never decides truth.** `verify.py` and the source
   connectors only gather and attach evidence. Only a reasoning provider (the
   LLM, or a human operator) sets a claim's `tier`. There is no mechanical
   "not found, therefore fake" rule anywhere, because that produces false
   accusations.
2. **The score is computed in code, never by the provider.** The provider
   assigns per-claim tiers (and, for companies, a buildability tier and metric
   scores); Python then folds those into the composite. The model cannot hand
   back a number it likes; it can only move the inputs the formula reads.

---

## 2. The two-score model

A Dossier carries `scan_type` = `"person"` or `"company_app"`, and each path
computes its own score. Both are 0 to 100, higher = more likely LARP.

### 2.1 Person: `founder_larp_score` (`llm.compute_founder_score`)

Per-claim **severity** weights how much a given claim matters:

- Base by type: `identity` 0.95, `employment` 0.85, `education` 0.85, anything
  else 0.6.
- For employment/education, base is scaled by a magnitude: both employer and
  title present = 1.0; only one = 0.65; neither = 0.3. (`identity` skips this.)

Contributions by tier:

- **DISPROVEN** claims combine by **noisy-OR** across their severities:
  `disproven_fraction = 1 - product(1 - severity_i)`, times 100. One clean
  fabrication is never diluted by adjacent true claims, and DISPROVEN is the
  ONLY path into the top LARP band (score >= 66).
- **UNVERIFIED + `expected_footprint == "high"` + has evidence records**: feeds
  a saturating **SUS** contribution `58 * (1 - 0.75 ** n)` where `n` is the
  count of such claims. One or two barely move the needle; a whole
  uncorroborable notable history lands solidly in the SUS band. (Gate: it only
  counts when `claim.evidence` is non-empty, i.e. a search actually ran. A
  high-footprint UNVERIFIED claim we never searched cannot read as SUS.)
- **UNVERIFIED low/unknown footprint** (or high-footprint but no evidence):
  small flat bump `15 * (unverified_severity_sum / total_severity_sum)`.
- **CONFIRMED**: contributes nothing; only enters the denominator.

The combined unverified contribution (flat bump + SUS) is hard-capped at 60,
strictly below the LARP floor of 66, so with **zero DISPROVEN claims the score
can never reach the top band**. Bands: CLEAR (low) -> SUS (mid, from
high-footprint unverifiables) -> LARP (>= 66, requires a DISPROVEN claim).
Returns `None` (unscored) when there are no claims or the reasoning step has
not run yet.

**Why this matters for calibration:** a mostly-legit person with one disproven
exaggeration is scored by that one claim's severity, not nuked to 99. A game
boast entered with a title but no employer has severity ~0.55, landing ~55
(moderate), while a wholesale fabricator with a disproven identity/employment
claim lands 85+.

### 2.2 Company: `company_larp_score` (`llm.compute_company_score`)

A weighted composite over the **active** rows of `metric_breakdown` (see
section 4). Weights: HIGH = 3, MED = 2, LOW = 1.

- Only `active` rows count; inactive rows are dropped and their weight excluded
  from the denominator (weight is redistributed to active rows, not counted as
  0 drag).
- The **buildability** row is hard-capped: its effective weight can never
  exceed 15% of total active weight (`cap = other_weight * (0.15 / 0.85)`), so
  it only nudges the composite, never drives it. If buildability is somehow the
  only active row, it carries the composite at raw weight.
- **None-blocking:** returns `None` (not 0) if the list is empty, nothing is
  active, or ANY active row's `score_0_10` is still `None`. An unscored metric
  can never silently read as "clean."
- Final: weighted average of the `score_0_10` values, times 10, clamped 0-100.
- **Unproven cap:** when claims are supplied and none is `DISPROVEN`, the
  composite is capped at 65. Metric suspicion can reach SUS, but never the
  accusation band by itself.
- **DISPROVEN path (claims-aware):** callers that pass the scored claims
  (pipeline, service, dossier all do) get a floor when any claim is
  DISPROVEN: a noisy-OR over the disproven claims' per-type severities
  (`proprietary_tech` 0.85, `company_overview` 0.9, `funding` 0.8, ...),
  mirroring the person model. This is how a contradicted claimed-autonomy /
  proprietary-AI assertion (the AI-washing / wizard-of-oz class, e.g. Amazon
  Just Walk Out) reaches the top band: the metric average alone cannot get
  there off one metric. The defamation guard is unchanged: DISPROVEN itself
  still requires actively contradicting evidence, and absence/GAP signals
  still cannot reach the top band. Frozen-fixture recomputes that omit
  `claims` get the pure metric composite.

---

## 3. The scan pipeline (`detective/pipeline.run`)

One function orchestrates a scan. Person and company scans share the shape and
diverge only where noted.

```
fetch (or accept injected raw_profile)
  -> decompose claims (mechanical, provider.decompose_claims)
  -> gather evidence per claim (verify.gather_evidence, concurrent connectors)
  -> [company only] scaffold Buildability + build_metric_breakdown
  -> assign tiers + verdict (provider.assign_tiers_and_verdict; the reasoning step)
  -> compute composite score in code (compute_founder_score / compute_company_score)
  -> emit verdict, return Dossier
```

Step by step:

1. **Fetch / inject.** With no `raw_profile`, a live fetch is used but only if
   `live=True` (both `extract_linkedin.fetch_profile` and
   `extract_company.fetch_company` refuse otherwise, so a stray call can never
   hit the network). Offline runs inject a pre-fetched `raw_profile` dict.
   `raw_profile["scan_type"]`, if present, is authoritative over the argument.

2. **Decompose (mechanical).** `provider.decompose_claims` branches on
   `scan_type` and calls `mechanical_decompose` (person) or
   `mechanical_decompose_company` (company). This is deterministic string work,
   not an LLM call. See section 4.

3. **Gather evidence (concurrent).** For each claim,
   `verify.gather_evidence(claim, identity, pb_budget, company_url)` builds
   targeted queries, runs web search, runs the gated PitchBook path, and runs
   the ~12 free source connectors **concurrently** (thread pool, max 8
   workers). It attaches evidence to `claim.evidence`; it sets no tier. One
   `PitchBookBudget` is shared across all claims to enforce a per-profile
   lookup cap; it is `None` (PitchBook untouched) unless `PITCHBOOK_ENABLED` is
   set. `company_url` is passed only for company scans (keys the
   wayback/domain_age/techstack site-history connectors).

4. **Company scaffold.** A company scan gets an empty `Buildability` and the
   8-row `metric_breakdown` skeleton (active flags decided from the claims).
   Person scans keep `buildability = None` and `metric_breakdown = []`.

5. **Assign tiers + verdict (the reasoning step).**
   `provider.assign_tiers_and_verdict(dossier)` is where a `tier` is set on
   every claim, `expected_footprint` is set, the verdict is written, and (for
   companies) the buildability tier and each active metric's `score_0_10` are
   filled. This is the only step that decides truth. See section 5.

6. **Compute composite (in code).** For a company scan,
   `sync_buildability_metric` derives the buildability row's `score_0_10` from
   the tier (TRIVIAL -> 3, MODERATE -> 1, HARD -> 0), then
   `compute_company_score` runs. For a person scan, `compute_founder_score`
   runs, but only once `larp_score` is set (the signal that the reasoning step
   actually completed), avoiding a premature all-default-tier number.

Progress is emitted through a `(event, payload)` callback (`status`, `claim`,
`verdict`) so the same shape maps onto a future websocket stream. `safe_print`
guards against `UnicodeEncodeError` on a narrow Windows console codepage.

---

## 4. Mechanical decomposition and the 8 company metrics

### 4.1 Person (`mechanical_decompose`)

Produces:

- One **`identity`** claim (if a name exists): "A real person named {name}
  exists and matches this profile."
- One **`employment`** claim per experience entry (employer = company, plus
  title/start/end). Special case `_reframe_utterance_title`: if a title begins
  with an utterance verb (claimed/said/promised/announced/stated/touted/
  boasted/alleged/insisted/asserted) AND has >= 6 words, it is rewritten to the
  embedded fact so the engine judges the truth of the fact, not the act of
  claiming. (The >= 6-word guard protects proper nouns like "Promised Land
  Realty.")
- One **`education`** claim per education entry (employer = school, title =
  degree): "Studied {degree} at {school}."

### 4.2 Company (`mechanical_decompose_company`)

Reads the normalized company profile (from `extract_company.parse_company_page`
or an injected fixture) and produces:

- One **`company_overview`** claim (anchors product_realness, zombie_liveness,
  and the site-history/techstack connectors).
- One **`pricing`** claim per priced tier (`pricing.tiers`).
- Metric claims from `metrics[]`, only for these four types:
  `user_count` ("claims {value} users"), `revenue_metric`, `funding`,
  `headcount`. For `user_count` the raw `unit` is stored on `claim.title` so the
  metric layer can tell consumer scale from a B2B seat count without re-parsing.
- One **`proprietary_tech`** claim per `tech_claims[]` entry.

### 4.3 The 8 company-LARP metrics (`build_metric_breakdown`)

| Metric | Weight | Active when |
|---|---|---|
| `raise_inflation` | HIGH (3) | a `funding` claim exists |
| `reach_vs_footprint` | HIGH (3) | a consumer-scale `user_count` claim exists |
| `product_realness` | HIGH (3) | always |
| `headcount_inflation` | MED (2) | a `headcount` claim exists |
| `proprietary_ai_gap` | MED (2) | a `proprietary_tech` claim exists |
| `zombie_liveness` | MED (2) | always |
| `key_role_coverage` | MED (2) | a `proprietary_tech` claim exists |
| `buildability` | LOW (1) | always (score derived from Buildability.tier) |

"Consumer-scale" means a `user_count` claim whose unit is NOT one of
companies/teams/company/team (a B2B seat count marks `reach_vs_footprint`
inactive). Unknown/missing unit defaults to consumer-scale (active), the safer
failure mode. `proprietary_ai_gap` and `key_role_coverage` are the two
wizard-of-oz-relevant metrics: both fire on the presence of a loud
`proprietary_tech` claim.

---

## 5. Data sources

Every connector lives in `detective/sources/`, returns `list[dict]` shaped
`{source_url, snippet, source_name, weight, match_confidence}` (techstack adds
`buildability_hint`), is defensive (returns `[]` on any failure, never raises),
and self-gates on claim type. `verify.py` calls them; it never sets a tier.

### 5.1 The weight model (`sources/registry.py`)

```
weight = (credibility x parsability x independence) / 125
```

Each factor is 1 to 5 (ceiling 125), so weight lands in (0, 1.0]. The factors:

- **credibility**: how much a standalone hit is worth trusting.
- **parsability**: how cleanly the source becomes a structured record (a
  documented JSON/XML API is high; a fragile HTML scrape is low).
- **independence**: how free the source is from the subject's own
  self-reporting (a disinterested third party is high; a company describing
  itself is low).

`DEFAULT_WEIGHT = 0.5` (the deliberate midpoint) is used by `weight_for(name)`
for any unregistered source, e.g. plain web-search evidence that carries no
`source_name`. An unweighted hit is thus neither silently trusted more nor
dismissed versus a weighted one (`models.evidence_weight` mirrors this).

**Full weight table (implemented connectors):**

| source | cred | pars | indep | weight | verifies |
|---|---|---|---|---|---|
| `github` | 4 | 5 | 3 | **0.48** | person: founder/technical footprint |
| `packages` | 5 | 5 | 5 | **1.0** | company: SDK/library claims (npm/PyPI) |
| `sec_edgar_form_d` | 5 | 5 | 4 | **0.8** | funding (company) / funding-flavored employment |
| `wayback_machine` | 5 | 5 | 4 | **0.8** | company site history (liveness) |
| `uspto_patents_trademarks` | 5 | 5 | 4 | **0.8** | proprietary_tech (company) / patent-flavored person claims |
| `arxiv` | 4 | 5 | 5 | **0.8** | person research credentials |
| `app_store_play_store_reviews` | 5 | 5 | 4 | **0.8** | company user_count/revenue/footprint |
| `accelerator_badges` | 5 | 5 | 4 | **0.8** | company overview / YC/Techstars claims |
| `domain_rdap_whois` | 4 | 5 | 4 | **0.64** | company domain age |
| `openalex` | 4 | 5 | 4 | **0.64** | person research credentials |
| `hackernews` | 4 | 5 | 4 | **0.64** | company overview / person identity, plus critical-comment scan |
| `courtlistener` | 5 | 3 | 5 | **0.6** | person identity / company adverse legal record |
| `techstack` | 3 | 4 | 4 | **0.384** | company: no-code / LLM-wrapper front-end fingerprint |

Seed-only (not implemented): `pitchbook` (0.64, wired separately via
`detective.pitchbook`), `companies_house_or_state_sos` (0.64),
`dns_certificate_transparency` (0.512), `crunchbase` (0.384),
`linkedin_company_page` (0.216), `glassdoor_or_similar` (0.216).

### 5.2 Per-connector detail

For each: what it verifies, how it queries, how it gates match confidence, and
what pushes a claim toward CONFIRMED vs DISPROVEN vs UNVERIFIED. The connector
only supplies evidence and a match confidence; the DISPROVEN/CONFIRMED call is
the reasoning provider's, made against the whole evidence set and the operator
instructions in section 6.

- **github** (0.48). Person founder/technical claims. Hits `api.github.com`
  (`/search/users`, `/users/{login}`, `/repos`, `/contributors`, `/git/trees`,
  `/commits`);
  GitHub CLI Keychain auth, `GITHUB_TOKEN`, or `GH_TOKEN` raises the rate limit;
  credentials are never logged or persisted by the connector. `match_confidence`
  is `low` by default, raised to `high` only when the claimed company appears
  in the account's own bio/company field or a personal-site hint matches the
  account `blog`. Emits account creation date, repo count, push spans, stars,
  languages, topics, and contributor counts. Those are useful footprint
  signals, but profiles, descriptions, and repository contents remain
  subject-controlled. For a high-confidence identity only, it performs a
  bounded inspection of one unauthenticated or two authenticated repositories:
  recursive public tree shape, source/test counts, CI, dependency manifests,
  generated/vendor exclusions, repository layers, project-hygiene files, and
  a recent commit sample linked to the account. It also reads at most five small
  public build/deployment configs to verify frameworks and declared test, build,
  lint, and typecheck commands. This is artifact maturity and authorship
  evidence, not proof of runtime quality, originality, security, private work,
  or a claimed job title.

- **packages** (1.0). Company SDK/library/open-source claims. Hits npm
  (`registry.npmjs.org` + downloads) and PyPI (`pypi.org` + pypistats).
  `match_confidence` always `high` (bound to the exact package name). Flags a
  stub/squat when `version_count <= 1` or the README/summary is near-empty.
  CONFIRMED: a real, published, downloaded package. DISPROVEN-leaning: the
  claimed package does not exist or is an empty squat.

- **sec_edgar_form_d** (0.8). Funding claims. Full-text search of
  `efts.sec.gov` for Form D, then one `primary_doc.xml` fetch; requires a
  User-Agent with an "@" email (`SEC_EDGAR_CONTACT`). Parses offering amount,
  amount sold, first-sale date, related persons. `match_confidence` `high` when
  the filing's entity name matches the query (normalized prefix, either
  direction), else `medium`; never `low` (full-text search already required a
  name mention). Absence of a filing is explicitly weak evidence (returns []),
  never grounds for DISPROVEN on its own. CONFIRMED: a matching Form D at the
  claimed magnitude.

- **wayback_machine** (0.8). Company site history, keyed off `company_url`.
  `web.archive.org/cdx`; `match_confidence` always `high` (bound to the exact
  URL). Emits first-capture date, capture count, sampled timestamps. Signal:
  a long capture history supports liveness; a URL that first appears yesterday
  next to "we're scaling" copy is a LARP tell (fed to `zombie_liveness` and
  buildability).

- **uspto_patents_trademarks** (0.8). Company `proprietary_tech` (as assignee),
  or patent-flavored person claims (as inventor). USPTO ODP API (needs
  `USPTO_API_KEY`, often unset) with a Google Patents XHR fallback. Buckets
  granted vs pending separately and re-checks each hit's own assignee/inventor
  field. `match_confidence` `high` only when a returned record's own
  assignee/inventor matches the query, else `low`. This is the artifact check
  behind `proprietary_ai_gap`: real patents backing a proprietary-AI claim push
  it toward CONFIRMED / a low gap; a loud claim with only pending or no filings
  is a proprietary-AI-gap tell.

- **arxiv** (0.8). Person research credentials. `export.arxiv.org/api/query`
  by author. Never `high`; `low` when > 20 results (namesake collision risk),
  else `medium`. Snippet stresses arXiv is preprints, not peer review.

- **app_store_play_store_reviews** (0.8; Apple only this batch). Company
  user_count/revenue/footprint. iTunes Search + Customer Reviews RSS.
  `match_confidence` `high` on a direct app_id lookup or a single clean
  name match; `low` on an ambiguous or fuzzy match. The KEY tell is rating
  COUNT and review RECENCY, not star average: a "100k users" claim next to 12
  reviews feeds `reach_vs_footprint` high.

- **accelerator_badges** (0.8). Company overview / "YC-backed" style claims.
  YC's Algolia index + Techstars portfolio HTML. `high` for a single clean
  match; `low` when ambiguous. "Not listed" is explicitly weak, never proof of
  a lie. CONFIRMED: a real batch/portfolio listing.

- **domain_rdap_whois** (0.64). Company domain age, keyed off the domain of
  `company_url`. RDAP first, raw WHOIS (port 43) fallback. Always `high` (bound
  to the exact domain). Proves WHEN not WHO: a fresh registration is the strong
  LARP signal; an aged domain is weak corroboration.

- **openalex** (0.64). Person research credentials (same gate as arxiv), with
  the person's company/headline passed as an institution hint. `api.openalex.org`.
  `high` when the hinted institution matches a recorded affiliation; `medium`
  when no institution but an ORCID and <= 2 candidates; else `low`.

- **hackernews** (0.64). Company overview or person identity.
  `hn.algolia.com` threads, comments, and account age. Runs a critical-comment
  scan for wrapper/vibecoded/scam/grift/fraud/snake-oil/overhyped/exaggerated
  language. Ceiling is `medium` (unmoderated, contrarian culture); account-age
  records default `low`, raised to `medium` only when the bio mentions the
  company. Never `high` (a username is not a verified identity). This is a soft
  corroborator of the wizard-of-oz signal (community calling something a
  wrapper), never proof by itself.

- **courtlistener** (0.6). Person identity or company adverse legal record.
  `courtlistener.com/api/rest/v4/search` over dockets and opinions.
  **Highest same-name false-positive risk in the registry**, so every hit is
  `low` by default, raised to `medium` only when `is_company=True` and the case
  caption carries a legal-entity suffix (inc/llc/corp/...). Never `high` on a
  bare name match, and the operator instructions forbid DISPROVEN off a
  courtlistener hit alone.

- **techstack** (0.384, lowest). Company `company_overview` only, keyed off
  `company_url`. A single HTTP fetch of the URL (HTML + headers, no JS-bundle
  crawl). Fingerprints no-code builders (Bubble/Webflow/Wix/Softr/Framer/
  Carrd/Glide/Adalo/Retool/WordPress-page-builder) and client-side LLM
  endpoints (api.openai.com, api.anthropic.com, generativelanguage.googleapis
  .com, cohere/mistral/stability/together/groq/openrouter). Emits an extra
  `buildability_hint`: `no_code_detected` or `llm_wrapper_signals` (both
  `match_confidence` `medium`, strong escalate-flags toward TRIVIAL
  buildability) vs `custom_stack` / `inconclusive` (`low`, NOT vindication:
  the backend is invisible to a front-end fetch, so absence of a marker must
  not push toward MODERATE/HARD). This is the strongest MECHANICAL wizard-of-oz
  fingerprint. The same record also distinguishes a marketing-only page, a
  deployed client shell, and a public interactive/auth surface. With
  `WEB_RUNTIME_ENABLED=1`, bundled headless Chromium executes the public page
  once and reports rendered controls, text, app/auth links, and request
  failures without submitting a form or entering credentials. This can verify
  that more than a landing page is deployed, but not private workflow behavior
  or backend substance. It requires a live URL fetch, so it is quiet on offline
  fixtures with placeholder URLs; there the wrapper read falls to Brave
  adversarial hits plus the provider's buildability reasoning.

### 5.3 What `verify.gather_evidence` does with all this

1. Builds per-claim queries by type (corroboration + adversarial + footprint
   roles). For `proprietary_tech` the adversarial query is
   `"{product}" built on (OpenAI OR Claude OR GPT OR Gemini) OR wrapper OR
   no-code`, aimed squarely at surfacing whether "proprietary AI" is just an
   API call. For metrics it adds a footprint query (app-store reviews / twitter
   / reddit) and a thin-wrapper adversarial query.
2. Runs `web_search` serially (shared rate-limited backend). The backend
   (`search.py`) tries **SearXNG, Brave, then optional DDGS**. Backend failures
   set process-global cooldowns so repeated calls fail over instead of
   hammering a dead service. Each result becomes `{source_url, snippet}`, max
   4 per query.
3. Runs the gated PitchBook path (employment/funding claims only, disabled by
   default).
4. Runs the 12 free connectors concurrently, reassembled in fixed order so the
   evidence set is byte-identical regardless of completion order (deterministic
   scoring).
5. Dedups by URL (keeping the best-ranked record per URL) and caps at 8
   records per claim, reserving 4 slots for weighted high/medium connector hits
   and 2 for adversarial/footprint web records so generic corroboration cannot
   crowd them out.

`verify.py` reads `weight` and `match_confidence` only for ranking; it assigns
neither (the connectors do) and never assigns a tier (the provider does).

---

## 6. The reasoning step and the provider abstraction

The reasoning step assigns tiers, footprints, the verdict, and (company) the
buildability tier and metric scores. It is pluggable behind `LLMProvider`.

### 6.1 ManualProvider (default, $0)

Human or fresh-Codex-in-the-loop. `decompose_claims` is the mechanical pass
above. `assign_tiers_and_verdict` is idempotent: it reads back a completed job
if present, otherwise writes `queue/<job_id>.json` with the matching operator
instructions plus the dossier, status "pending", and returns the UNSCORED
dossier immediately (default `MANUAL_QUEUE_TIMEOUT_S=0`; a positive value polls
the file for `status == "completed"`). A separate vision-extract queue handles
screenshot OCR. This is the live $0 path: the operator (a human or fresh Codex reviewer
reading the queue file) fills in the tiers by hand per the instructions.

### 6.2 ApiProvider (Gemini)

`__init__` selects the provider: **Gemini wins whenever `GEMINI_API_KEY` is
set**, even if `ANTHROPIC_API_KEY` is also present; the `anthropic` branch
exists only when there is no Gemini key and always raises "not wired." Model
name comes from `GEMINI_MODEL` (default `gemini-2.5-flash`); set it to
`gemini-flash-lite-latest` to use the separate-quota lite model.

`assign_tiers_and_verdict` builds the prompt (`_build_prompt` = operator
instructions + a claims/evidence block carrying each record's optional
`source_name`/`weight`/`match_confidence` + the active metric names + a JSON
format addendum), calls Gemini (the `google-genai` SDK if present, else a bare
REST POST to `generativelanguage.googleapis.com` with the key in the
`x-goog-api-key` header so it cannot leak into an exception URL; `temperature
0.1`, JSON response), parses the JSON, and applies it via `_apply_result`.

`_apply_result` clamps defensively: it requires exactly one entry per claim
with an integer `index`, parses each `tier` (invalid raises), clamps
`expected_footprint`, and for a company requires a valid buildability tier and
numeric `score_0_10` per active non-buildability row (clamped 0-10; a bool is
rejected). Any failure (quota, network, unparseable response, missing field)
raises **`ApiProviderError`**, whose message is scrubbed of the raw key.
`service.py` catches it and falls back to the ManualProvider queue flow, so an
exhausted Gemini quota degrades to the $0 path rather than crashing.

### 6.3 The operator instructions (the discipline the model must follow)

The prompt text (`_OPERATOR_INSTRUCTIONS` for a person,
`_COMPANY_OPERATOR_INSTRUCTIONS` for a company) gives the reviewer its
discipline. It is defense in depth, not the safety boundary. Python restores
the reference claims/evidence and downgrades unsupported `DISPROVEN` labels
before scoring. Key rules, verbatim in spirit:

- **DISPROVEN needs contradicting evidence, not an absence of confirmation.**
  A missing record is UNVERIFIED, never DISPROVEN.
- **Corroboration discipline:** a source reporting that a claim was MADE is not
  evidence the claim is TRUE. Adverse findings ("admitted lying", "convicted
  of", "no record of", "did not attend") support DISPROVEN.
- **Embedded-utterance discipline:** judge the truth of the embedded fact, not
  the act of claiming it.
- **Source weighting:** combine by `weight x match_confidence`; never DISPROVEN
  off a single low-confidence hit or a bare absence; courtlistener never alone.
- **Expected footprint:** separately from tier, mark whether a truthful version
  of the claim would leave a public trace (`high`/`low`); when unsure choose
  `low`, so a legitimately low-footprint person is never punished for being
  hard to verify. Only high-footprint UNVERIFIED claims lift the SUS band.
- **Verdict tone, three tiers, defamation-safe:** TIER 1 (maximum roast,
  profanity allowed) ONLY when at least one claim is DISPROVEN, and the insult
  must be anchored to the named disproven claim in a "says X, but the record
  says Y" shape. TIER 2 (SUS/mocking) when the worst thing is
  unverifiable-should-have-been-verifiable: fry the ABSENCE of proof, never
  assert the person lied. TIER 3 (back off) when nothing is disproven and
  nothing notable is unverifiable. The words liar/fraud/lied/fake/fabricated
  and any profanity aimed at the person may appear ONLY when there is a
  DISPROVEN claim, even inside a hedge. This is the false-accusation guard.

For a company, PART 2 fills the **buildability** tier (TRIVIAL / MODERATE /
HARD) and PART 3 scores each active metric. The wizard-of-oz / thin-wrapper
judgment lives here:

- **Buildability** reads the techstack `buildability_hint`: `no_code_detected`
  is a strong escalate-flag toward TRIVIAL; `llm_wrapper_signals` also leans
  TRIVIAL; `custom_stack`/`inconclusive` is silence, NOT proof of substance
  (judge those on other artifacts). Honesty discipline: only TRIVIAL when the
  thin-wrapper evidence actually supports it; a genuinely hard product lands
  MODERATE/HARD, never TRIVIAL by default because the price feels high.
- **`proprietary_ai_gap`** is artifact-first: does the evidence show real
  proprietary-AI artifacts (patents, papers, a public repo, a technical
  writeup) behind the loud proprietary-AI claim? No artifacts plus thin-wrapper
  evidence scores high; real artifacts score low.
- **`key_role_coverage`** asks whether a technical co-founder/engineer is
  findable for an AI/hard-tech company, consuming only CONFIRMED-tier roles.

The company scoring philosophy is stated up front: only OUTRIGHT fabrication
scores high; normal startup rounding and optimism score near zero; when unsure,
score low and say so. Every guard exists to stop a false accusation.

---

## 7. Evaluation and regression

- `evals/run_eval.py` runs the Gemini brain against fixed ground-truth cases
  (`tests/cases/*.json` and `evals/cases/*.json`, each with an `_expected`
  sidecar). Phase 1 (mechanical decompose + real evidence gather) is cached to
  `evals/cache/` so every prompt version is graded against the SAME evidence;
  Phase 2 (Gemini tier assignment) runs fresh. PitchBook is force-disabled for
  determinism. Results append to `evals/RESULTS.md`.
- The eval runner includes documented extremes and prominent controls. These
  are diagnostic calibration cases, not a blinded estimate of production
  accuracy and not adequate coverage of ambiguous middle cases.
- Offline regression tests (`tests/test_blind_cases.py`) assert, against the
  cached scored dossiers, that the frauds land high, the calibration controls
  are not over-penalized, and the wizard-of-oz metrics activate, all with zero
  Gemini quota so the suite stays deterministic.

---

## 8. File map

| File | Responsibility |
|---|---|
| `detective/pipeline.py` | Orchestration (`run`) |
| `detective/models.py` | Dataclasses: `Claim`, `EvidenceTier`, `Buildability`, `MetricEntry`, `Dossier` |
| `detective/llm.py` | Providers, mechanical decompose, metric breakdown, both score functions, prompts |
| `detective/verify.py` | Evidence gathering, query building, connector orchestration |
| `detective/search.py` | `web_search` (SearXNG, Brave, optional DDGS) |
| `detective/extract_linkedin.py` | Person profile fetch/parse (gated) |
| `detective/extract_company.py` | Company page fetch/parse (gated) |
| `detective/pitchbook.py` | Gated PitchBook path + per-profile budget |
| `detective/sources/*.py` | The 13 implemented source connectors + `registry.py` |
| `detective/service.py` | Service entry with ApiProvider -> ManualProvider fallback |
```
