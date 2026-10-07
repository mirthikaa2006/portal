"""Multi-organization visibility and role policies."""

from __future__ import annotations

from impact_selector.models import Actor, Role, TestCase


ROLE_CAPABILITIES = {
    Role.ORG_ADMIN: {
        "select",
        "execute_skips",
        "view_all_tests",
        "view_failure_history",
        "submit_feedback",
        "manage_partners",
    },
    Role.QA_OPERATOR: {
        "select",
        "execute_skips",
        "view_all_tests",
        "view_failure_history",
        "submit_feedback",
    },
    Role.PARTNER: {
        "select",
        "view_partner_tests",
        "submit_feedback",
    },
    Role.AUDITOR: {
        "select",
        "view_all_tests",
        "view_failure_history",
        "view_rationale",
    },
}


def capabilities_for(role: Role) -> set[str]:
    return set(ROLE_CAPABILITIES[role])


def can_execute_skips(actor: Actor) -> bool:
    return "execute_skips" in ROLE_CAPABILITIES[actor.role]


def visible_tests(actor: Actor, tests: list[TestCase]) -> list[TestCase]:
    """Filter catalog visibility by org boundary and partner scope."""
    if actor.role == Role.PARTNER:
        partner_org = actor.partner_org_id or actor.org_id
        return [
            t
            for t in tests
            if t.partner_visible and (t.org_id == actor.org_id or t.org_id == partner_org)
        ]
    return [t for t in tests if t.org_id == actor.org_id]


def policy_notes(actor: Actor) -> dict[str, str]:
    notes = {
        "role": actor.role.value,
        "org_id": actor.org_id,
        "can_execute_skips": "yes" if can_execute_skips(actor) else "no",
        "visibility": "org-wide" if actor.role != Role.PARTNER else "partner-scoped",
    }
    if actor.role == Role.AUDITOR:
        notes["ci_enforcement"] = (
            "Auditor recommendations are advisory. CI skip gates require org_admin or qa_operator."
        )
    if actor.role == Role.PARTNER:
        notes["visibility_detail"] = (
            "Partners see only partner-visible tests and cannot inspect other agencies' catalogs."
        )
    return notes
