# TDD Evidence: Traction claim capture -> inflation detection

**Source plan:** none; journeys derived during this TDD run from owner feedback
("13 ratings is super low for something that has 2,000 users... it can be
reasonably estimated that it is sus", and "2,000+ users is in his experience for
Organize Campus").

## User journey

> As someone scanning a founder, I want their own traction boast ("2,000+
> users") in an experience description to be captured and cross-checked against
> real footprint (13 App Store ratings), so the tool flags the inflation grounded
> in the person's own words, not an invented accusation.

## Task report

The chain is three links; each proven by a test.

1. **App Store fires on a founder person-scan.** `verify_app_store` now runs on
   founder/builder employment claims (not only company scans).
   - RED: `test_founder_employment_claim_fires_app_store` (before the gate
     change, a founder employment claim did not fire the connector).
   - GREEN: `python -m pytest tests/test_verify.py -q` -> pass.
   - Guarantees: a founder's own product gets its real store traction pulled.

2. **A traction number in a description becomes a `user_count` claim, and
   inflation fires.**
   - Test: `test_traction_in_description_becomes_user_count_and_fires_inflation`.
   - Evidence: `detect_inflation` returns `claimed 2,000 but discovered ~13
     (154x gap)`; the incidental number "team of 5" does NOT become a claim.
   - Guarantees: only genuine traction/revenue units become magnitude claims;
     the mismatch is grounded in the person's own stated number.

3. **The parser captures experience descriptions (the live plumbing).**
   - RED: `test_live_experience_captures_role_description` asserted the Talon
     intern entry carries its description ("Systems Test Hardware group...");
     the entry dict had no `description` key -> failed for the intended reason.
   - GREEN: attach leftover pending description lines to the prior entry ->
     pass; 47/47 parser tests unchanged (no regression).
   - Guarantees: descriptions survive parsing, so link 2 fires on a LIVE scan
     (profile_url path), not only in a unit test.

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|--------------------|------|------|--------|
| 1 | Founder employment fires App Store lookup | `tests/test_verify.py::test_founder_employment_claim_fires_app_store` | unit | PASS |
| 2 | Plain (non-founder) employment does NOT fire App Store | `tests/test_verify.py::test_plain_employment_claim_does_not_fire_app_store` | unit | PASS |
| 3 | "2,000+ users" in a description -> user_count claim -> 154x inflation vs 13 ratings | `tests/test_dossier.py::test_traction_in_description_becomes_user_count_and_fires_inflation` | unit | PASS |
| 4 | Incidental number ("team of 5") never becomes a magnitude claim | same as #3 | unit | PASS |
| 5 | Parser captures a role's free-text description | `tests/test_extract.py::test_live_experience_captures_role_description` | unit | PASS |

## Coverage and known gaps

- Full suite: `python -m pytest tests/ -q` -> **525 passed, 14 skipped**. No
  dedicated coverage tool is configured in this repo; the suite is the gate.
- KNOWN GAP: the LAST experience entry's description is not captured (there is
  no following role to trigger the attach). Acceptable for now; most traction
  boasts are not the final role. Follow-up: end-of-loop attach.
- KNOWN GAP: on a scan with `profile_url == null` (vision-only), descriptions
  are not fetched, so link 2 stays inert until the reasoning-brain planning
  pass extracts them. This is the intended next architecture step.

## Merge evidence

RED -> GREEN -> refactor preserved across commits `eedc7ae` (App Store + marker
+ calibration), `5fff102` (traction decompose + inflation test), `5e65df6`
(description capture RED/GREEN).
