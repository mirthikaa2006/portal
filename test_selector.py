"""Unit tests for selection safety, locale rules, access, and degradation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from impact_selector.access import visible_tests
from impact_selector.algorithm import select_tests
from impact_selector.language import classify_change, detect_locale
from impact_selector.models import Actor, ChangedFile, ChangeKind, Role, SelectionRequest
from impact_selector.store import DataStore

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ORG = "ministry_interior"


@pytest.fixture(scope="session")
def store() -> DataStore:
    if not (DATA / "tests.json").exists():
        pytest.skip("Run python scripts/generate_data.py first")
    return DataStore(DATA)


@pytest.fixture
def qa() -> Actor:
    return Actor(actor_id="qa", org_id=ORG, role=Role.QA_OPERATOR)


@pytest.fixture
def ground() -> dict:
    return json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))


def _keep(result) -> set[str]:
    return {r.test_id for r in result.recommendations if r.decision.value != "skip"}


def test_never_skips_ground_truth_affected(store, qa, ground):
    for change_id, affected in ground.items():
        result = select_tests(SelectionRequest(change_id=change_id, org_id=ORG), store, qa)
        kept = _keep(result)
        missed = set(affected) - kept
        assert not missed, f"{change_id} skipped affected tests: {missed}"


def test_hindi_content_skips_other_locale_ui(store, qa):
    result = select_tests(SelectionRequest(change_id="chg_hi_benefits_copy", org_id=ORG), store, qa)
    kept = _keep(result)
    assert "t_locale_hi_benefits" in kept
    assert "t_e2e_benefits_hi" in kept
    assert "t_locale_ta_benefits" not in kept
    assert "t_e2e_appointments_bn" not in kept
    skipped = next(r for r in result.recommendations if r.test_id == "t_locale_ta_benefits")
    assert skipped.skip_rationale
    assert "locale" in " ".join(skipped.reasons).lower() or "language" in " ".join(skipped.reasons).lower()


def test_protected_tests_never_skipped(store, qa):
    result = select_tests(SelectionRequest(change_id="chg_bn_common", org_id=ORG), store, qa)
    kept = _keep(result)
    assert "t_smoke_portal" in kept
    assert "t_sec_auth" in kept
    assert "t_unit_gateway" not in kept  # payment guard is module-scoped, not global


def test_missing_coverage_still_returns_and_fails_closed(store, qa, ground):
    change_id = "chg_auth_session"
    full = select_tests(SelectionRequest(change_id=change_id, org_id=ORG), store, qa)
    degraded = select_tests(
        SelectionRequest(change_id=change_id, org_id=ORG, simulate_missing=["coverage"]),
        store,
        qa,
    )
    assert degraded.tests_to_run >= 1
    assert degraded.skip_threshold > full.skip_threshold
    missed = set(ground[change_id]) - _keep(degraded)
    assert not missed


def test_missing_dependencies_does_not_crash(store, qa, ground):
    change_id = "chg_payments_gateway"
    result = select_tests(
        SelectionRequest(change_id=change_id, org_id=ORG, simulate_missing=["dependencies"]),
        store,
        qa,
    )
    assert result.source_health.dependencies.value == "missing"
    assert not (set(ground[change_id]) - _keep(result))


def test_missing_change_payload_runs_all(store, qa):
    result = select_tests(
        SelectionRequest(change_id="does-not-exist", org_id=ORG, simulate_missing=["code_changes"]),
        store,
        qa,
    )
    assert result.tests_skipped == 0
    assert result.tests_to_run == result.tests_total


def test_partner_cannot_see_internal_payments(store):
    partner = Actor(
        actor_id="nid",
        org_id=ORG,
        role=Role.PARTNER,
        partner_org_id="partner_nid",
    )
    visible = {t.test_id for t in visible_tests(partner, store.tests_for_org(ORG))}
    assert "t_int_payments" not in visible
    assert "t_partner_nid_hook" in visible


def test_auditor_cannot_execute_skips():
    from impact_selector.access import can_execute_skips

    auditor = Actor(actor_id="audit", org_id=ORG, role=Role.AUDITOR)
    assert can_execute_skips(auditor) is False


def test_locale_detection():
    assert detect_locale("locales/hi/benefits.json") == "hi"
    files = [ChangedFile(path="locales/ta/appointments.json", kind=ChangeKind.CONTENT, locale="ta")]
    kind, locales, content_only = classify_change(files)
    assert kind == ChangeKind.CONTENT
    assert locales == ["ta"]
    assert content_only is True
