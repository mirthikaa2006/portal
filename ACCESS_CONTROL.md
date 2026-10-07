# Multi-organization access

The portal is not one tenant. Ministries, state overlays, and identity partners share CI but must not share catalogs.

## Tenancy

Every `TestCase` and `CodeChange` has `org_id`. Selection loads `tests_for_org(actor.org_id)` then applies role filters.

Demo tenants:

| org_id | Who |
| --- | --- |
| `ministry_interior` | National citizen portal QA |
| `partner_nid` | National ID integrator |
| `state_karnataka` | State scheme overlay |

## Roles

```
org_admin     full catalog, apply skips, manage partners
qa_operator   full catalog, apply skips, submit feedback
partner       partner_visible tests only, advisory skips
auditor       full catalog + rationale, cannot apply skips or submit feedback
```

Visibility is **not** the same as execution rights. An auditor can explain why `test_locale_ta_benefits` was skipped; they cannot flip a CI skip gate. A partner can request selection for the ministry org they are attached to but will not receive `t_int_payments` (`partner_visible: false`).

## What changes by role (same Hindi copy change)

| Field | qa_operator | partner | auditor |
| --- | --- | --- | --- |
| Sees Hindi locale tests | Yes | Yes | Yes |
| Sees internal payment integration | Yes | No | Yes |
| `policy.can_execute_skips` | yes | no | no |
| `POST /v1/feedback` | 200 | 200 | 403 |
| Cross-org select | 403 | 403 | 403 |

## Integration note

Map IdP groups to `Role` in the API gateway. Do not let the client pass an arbitrary org for partners; bind `X-Org-Id` from the token. This prototype trusts headers so the demo can switch roles without OIDC.
