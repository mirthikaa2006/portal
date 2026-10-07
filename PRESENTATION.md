# Mithuna — change-impact test selection

**Audience:** portal QA leads, CI owners, partner integrators  
**Claim:** Skip low-risk tests on a multilingual government portal without missing affected tests, even when a data source is late.

## Problem

Citizen portal releases mix Python services and five-locale CMS copy. The default is a ~18 minute org suite (`1095 s`, 44 tests). File-name matching is faster but **misses** real blast radius (session.py → benefits integration). Coverage jobs lag content publishes. Partners must not see internal payment tests.

## Approach

1. Classify change: code vs content vs mixed; detect locales.
2. Expand files through a dependency graph.
3. Score tests from coverage, patterns (fallback), locale rules, failure history.
4. Skip only below a threshold that **rises** when sources are missing.
5. Explain every skip: sources, reasons, confidence.
6. RBAC: org_admin / qa_operator can apply skips; partner and auditor cannot.

Content-only Hindi does not run Tamil UI tests. Mixed eligibility+Hindi **does** keep all benefits E2E because the code file is in their coverage.

## Why it works

- **Fail closed** is the product: incomplete data → fewer skips, never a 500.
- **Locale is a first-class signal**, not a filename hack.
- **Ground-truth experiment** (12 realistic changes, seed 42) is the acceptance bar: recall must stay 1.0.

## Results vs baseline

| Method | Avg suite time | Time saved | Recall on affected tests |
| --- | --- | --- | --- |
| Run all | 1095 s | 0% | 1.00 |
| File-pattern match | 349 s | 68% | **0.79** (unsafe) |
| **Impact selector** | **624 s** | **43%** | **1.00** |

Pattern matching missed `t_int_benefits_flow` on auth, payments, search, and schema-adjacent upload tests. The selector did not skip any ground-truth affected test.

**Headline change (Hindi copy):** 1095 s → 373 s, both affected locale tests kept.

**Degradation (auth change, coverage missing):** still recall 1.0; suite 959 s vs 926 s with full data (runs more, as designed).

## Ask

Pilot on content pipelines first (highest savings, clearest rationales). Keep smoke/security as policy keeps. Feed CI outcomes to `/v1/feedback` before widening skips to payment/auth services.
