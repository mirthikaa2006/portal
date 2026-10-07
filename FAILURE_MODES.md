# Failure-mode analysis

The selector is built to stay useful when a warehouse lags. Three production-shaped cases plus how the prototype behaves (reproduced with `simulate_missing` on the API).

## 1. Missing dependency map

**Symptom.** Graph job failed; `dependencies.json` absent or `simulate_missing=["dependencies"]`.

**Risk.** Transitive impact (session.py → application.py) is invisible. A naive system might skip benefits integration tests after an auth change.

**Handling.**

- Source health: `dependencies = missing`; skip threshold **+0.10**.
- Scoring still uses **coverage** of changed files and **failure correlation**.
- Notes tell the operator the graph is down.
- Unit tests assert `chg_payments_gateway` still keeps ground-truth affected tests without the graph.

**Why this is safe.** Coverage is a direct observation of what the test executed. Missing edges remove *bonus* transitive score; they do not create skips for tests that already cover the changed file. Tests that *only* covered dependents may drop to `recommended` instead of `skip` because the threshold rose.

## 2. Delayed / stale coverage metrics

**Symptom.** Last successful coverage upload is old (`stale: true` on some rows, or `coverage_delayed`). Content teams publish several times a day; coverage often trails.

**Risk.** Over-trusting an outdated map could skip a new Playwright spec that now touches Hindi copy.

**Handling.**

- Store marks coverage `delayed` when any row is stale.
- Skip threshold **+0.12** (missing) or **+0.06** (delayed).
- Reasons include: “Coverage metrics are delayed/stale; treating overlap conservatively.”
- Pattern matching and locale rules still apply, so Hindi copy keeps Hindi locale tests even if coverage rows are stale.

**Operator action.** If completeness is below ~0.7, treat skips as a preview and keep E2E `recommended` in the gating shard until coverage refreshes.

## 3. Change that affects multiple languages

**Symptom.** `chg_ta_and_te_content` edits `locales/ta/appointments.json` and `locales/te/appointments.json` together. A Hindi-only rule would under-select.

**Handling.**

- Locale detection unions **all** touched locales (`ta`, `te`).
- Tamil **and** Telugu appointment E2E stay `must_run` / `recommended`.
- Bengali appointment E2E is skip-eligible (content-only, no locale overlap).
- Mixed change `chg_mixed_benefits_hi` (eligibility **code** + Hindi JSON) is classified `mixed`, not content-only, so other-language **benefits E2E stay** (they cover `eligibility.py`).

**Why this is safe.** Content-only skip of other locales is gated on `content_only`. Any code file in the payload disables that shortcut.

## 4. Flaky test history (additional)

**Symptom.** `t_e2e_benefits_ta` and `t_int_appointments` flake in `failures.json`.

**Risk.** Either (a) skip a flaky-but-affected test and miss a real regression, or (b) never skip anything because flake ≠ impact.

**Handling.** Flake adds score **only if** the test is already in the blast radius. Unrelated flake does not force a keep, and is called out in reasons. Affected + flaky is kept.

## 5. Missing change payload (fail closed)

**Symptom.** CI called `/v1/select` without files and with an unknown `change_id`.

**Handling.** `code_changes = missing`, skip threshold 1.0, every visible test `must_run`. The API still returns 200 with an explanation rather than 500.

## Quick matrix

| Outage | Still returns 200? | Skips allowed? | Recall on demo ground truth |
| --- | --- | --- | --- |
| Dependencies | Yes | Yes, stricter | 1.0 on `chg_payments_gateway` |
| Coverage | Yes | Yes, stricter | 1.0 on `chg_auth_session` |
| Failure history | Yes | Slightly stricter | Unaffected |
| Change files | Yes | No | N/A (full suite) |
| Multi-locale content | Yes | Other locales only | 1.0 on `chg_ta_and_te_content` |
