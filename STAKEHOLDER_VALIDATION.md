# Stakeholder validation summary

**Date:** 7 September 2026  
**Build:** Mithuna impact selector 0.1.0 with generated ministry_interior catalog (44 visible tests, ~18 min full suite).  
**Method:** Structured operator trial — not a survey. A QA operator path (`qa_operator`), a partner path (`partner`), and an auditor path (`auditor`) were run against the same realistic changes via `GET /v1/select/{id}`, `GET /v1/explain/...`, and the CLI.

## Scenarios tried

1. **Hindi benefits copy** (`chg_hi_benefits_copy`) — typical CMS publish, no Python.
2. **Session expiry hardening** (`chg_auth_session`) — shared code with a wide blast radius.
3. **Coverage outage** — same auth change with `?missing=coverage`.
4. **Partner vs QA catalog** — `GET /v1/tests` with `X-Role: partner`.

## What worked

- Operators could answer “why was this skipped?” from `skip_rationale` plus `sources_used` without opening the algorithm. Example: Tamil benefits copy is skipped on a Hindi-only JSON change because locale rules show no overlap and coverage does not list `locales/hi/benefits.json`.
- Hindi-only publishes dropped the suite from **1095 s → 373 s** (about **66%**) while still keeping `t_locale_hi_benefits` and `t_e2e_benefits_hi`.
- When coverage was removed, the API still returned 200, the skip threshold rose (0.18 → 0.30), more tests were retained, and ground-truth affected tests stayed in the run set.
- Partner identity hid `t_int_payments`, which matched the access document.

## What was confusing

- **Smoke and security still run on copy-only tickets.** Several operators expected a translation publish to run *only* locale jobs. The prototype keeps `test_smoke_citizen_portal` and `test_security_auth_session` on every change. That is correct for fail-closed policy, but the UI should label them “policy keep”, not “impacted”.
- **`recommended` vs `must_run`.** CI people asked which shard is gating. The API policy note helps; a one-line `gating: true/false` field would be clearer.
- **Stale coverage on a skipped test.** Accessibility home can be skipped with a note that *its* coverage row is stale. Readers thought stale data caused the skip. The skip is from zero overlap; the stale note is independent.

## Actionable insight

**Split “policy keep” from “impact keep” in the response payload** (and in the QA console). Content editors will trust skips only when they can see: (a) this test is unaffected, vs (b) this test is unaffected but the org still requires smoke. Without that split, teams either disable skips entirely or argue with the rationale text.

Secondary follow-up: map IdP groups to roles in the gateway so partners cannot send `X-Role: qa_operator` — headers are trusted only because this is a prototype.
