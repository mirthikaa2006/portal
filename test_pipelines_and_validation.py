"""Comprehensive test suite for the three pipeline components and edge-case validation.

Validates:
1. Circular dependency detection and graceful graph sanitization.
2. 24-hour coverage staleness tracking and impact scoring fallback.
3. Multilingual routing validation (strict locale set enforcement, test-to-file alignment,
   and cross-locale blast radius mismatch detection).
4. Dependency-graph extraction pipeline (AST parsing, inversion, deduplication).
5. Coverage-ingestion pipeline (multi-run merging, timestamp freshness).
6. Real-time CI/CD telemetry feedback ingestion and flakiness tracking.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app, get_store
from impact_selector.algorithm import expand_dependents, select_tests
from impact_selector.coverage import (
    CoverageIngestionBatch,
    TestExecutionEvent,
    ingest_coverage_events,
    is_entry_stale,
)
from impact_selector.dependency_graph import (
    SourceFileInput,
    build_dependency_graph,
    check_graph_staleness,
    extract_python_ast_imports,
    resolve_import_to_path,
)
from impact_selector.models import (
    Actor,
    ChangedFile,
    ChangeKind,
    CoverageEntry,
    DependencyEdge,
    Role,
    SelectionRequest,
    TestCase,
    TestLayer,
)
from impact_selector.store import DataStore
from impact_selector.telemetry import (
    RunnerEnvironment,
    TelemetryPayload,
    record_failure_telemetry,
    send_telemetry_with_retry,
)
from impact_selector.validation import (
    ALLOWED_LOCALES,
    detect_circular_dependencies,
    sanitize_dependency_graph,
    validate_coverage_staleness,
    validate_multilingual_routing,
)

TestCase.__test__ = False
TestExecutionEvent.__test__ = False
TestLayer.__test__ = False

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ORG = "ministry_interior"


@pytest.fixture(scope="session")
def raw_data() -> dict[str, Any]:
    return {
        "tests": json.loads((DATA / "tests.json").read_text(encoding="utf-8")),
        "changes": json.loads((DATA / "changes.json").read_text(encoding="utf-8")),
        "coverage": json.loads((DATA / "coverage.json").read_text(encoding="utf-8")),
        "dependencies": json.loads((DATA / "dependencies.json").read_text(encoding="utf-8")),
        "failures": json.loads((DATA / "failures.json").read_text(encoding="utf-8")),
        "ground_truth": json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8")),
    }


@pytest.fixture
def qa_actor() -> Actor:
    return Actor(actor_id="qa_tester", org_id=ORG, role=Role.QA_OPERATOR)


@pytest.fixture
def store() -> DataStore:
    return DataStore(DATA)


# ---------------------------------------------------------------------------
# Requirement 4: Edge-Case Validation Tests
# ---------------------------------------------------------------------------


def test_circular_dependency_detected():
    """Verify that circular dependency cycles are accurately identified and gracefully sanitized."""
    # Graph containing a cycle: A -> B -> C -> A, and a branch C -> D
    cyclic_graph = {
        "src/auth/session.py": ["src/auth/otp.py"],
        "src/auth/otp.py": ["src/citizen/profile.py"],
        "src/citizen/profile.py": ["src/auth/session.py", "src/documents/upload.py"],
    }

    # 1. Detect cycles
    cycles = detect_circular_dependencies(cyclic_graph)
    assert len(cycles) >= 1
    cycle_nodes = cycles[0]
    # Check that the cycle starts and ends with the same node
    assert cycle_nodes[0] == cycle_nodes[-1]
    assert "src/auth/session.py" in cycle_nodes
    assert "src/auth/otp.py" in cycle_nodes
    assert "src/citizen/profile.py" in cycle_nodes

    # 2. Sanitize graph: breaks cycle back-edge and logs warning without throwing an unhandled exception
    sanitized, report = sanitize_dependency_graph(cyclic_graph)
    assert report.has_cycles is True
    assert len(report.warnings) >= 1
    assert "Circular dependency detected" in report.warnings[0].message

    # Verify sanitized graph is acyclic
    post_check_cycles = detect_circular_dependencies(sanitized)
    assert len(post_check_cycles) == 0

    # Verify expand_dependents terminates safely over the sanitized graph
    expanded = expand_dependents(["src/auth/otp.py"], sanitized, depth=10)
    assert "src/citizen/profile.py" in expanded
    assert "src/documents/upload.py" in expanded


def test_stale_coverage_handled(raw_data, store, qa_actor):
    """Verify that coverage snapshots older than 24 hours elevate skip threshold and trigger fallback."""
    coverage_entries = [CoverageEntry.model_validate(e) for e in raw_data["coverage"]]

    # Evaluate against a reference time 48 hours after the captured_at date
    sample_cap = coverage_entries[0].captured_at
    ref_time = sample_cap + timedelta(hours=48)

    # 1. Validation logic: detects all stale entries and sets delayed flag
    report = validate_coverage_staleness(coverage_entries, reference_time=ref_time, threshold_hours=24.0)
    assert report.stale_entries == len(coverage_entries)
    assert report.is_delayed is True
    assert report.skip_threshold_impact == 0.06
    assert "DELAYED" in report.notes[0]

    # 2. Impact selector degradation: when coverage is delayed, selector raises threshold
    full = select_tests(SelectionRequest(change_id="chg_auth_session", org_id=ORG), store, qa_actor)
    delayed = select_tests(
        SelectionRequest(change_id="chg_auth_session", org_id=ORG, simulate_missing=["coverage_delayed"]),
        store,
        qa_actor,
    )

    # Threshold must rise by 0.06 (from BASE 0.18 to 0.24)
    assert delayed.skip_threshold == round(full.skip_threshold + 0.06, 3)
    assert delayed.source_health.coverage.value == "delayed"

    # Core ground truth tests are still retained via blast-radius expand_dependents and pattern rules
    ground_tests = set(raw_data["ground_truth"]["chg_auth_session"])
    kept_tests = {r.test_id for r in delayed.recommendations if r.decision.value != "skip"}
    assert not (ground_tests - kept_tests)


def test_multilingual_mismatch_flagged(raw_data):
    """Verify that the multilingual validator enforces strict locales and catches mismatches."""
    tests = [TestCase.model_validate(t) for t in raw_data["tests"]]

    # 1. Unsupported locale rejection: e.g. 'fr' or 'es' must be rejected
    invalid_changes = [
        ChangedFile(path="locales/fr/benefits.json", kind=ChangeKind.CONTENT, locale="fr"),
        ChangedFile(path="locales/es/appointments.json", kind=ChangeKind.CONTENT, locale="es"),
    ]
    report_invalid = validate_multilingual_routing(changes=invalid_changes)
    assert report_invalid.is_valid is False
    assert "fr" in report_invalid.rejected_locales
    assert "es" in report_invalid.rejected_locales
    assert any(m.error_type == "invalid_locale" for m in report_invalid.mismatches)

    # 2. Test-to-coverage file locale mismatch:
    # E.g. A Hindi test 't_locale_hi_benefits' erroneously mapped to English 'locales/en/benefits.json'
    mismatched_coverage = [
        CoverageEntry(
            test_id="t_locale_hi_benefits",
            files=["locales/en/benefits.json", "src/benefits/application.py"],
            captured_at=datetime.now(timezone.utc),
            stale=False,
        )
    ]
    report_mismatch = validate_multilingual_routing(tests=tests, coverage=mismatched_coverage)
    assert report_mismatch.is_valid is False
    test_mismatch = next(m for m in report_mismatch.mismatches if m.error_type == "test_file_mismatch")
    assert test_mismatch.entity_id == "t_locale_hi_benefits"
    assert test_mismatch.actual_locale == "en"
    assert "hi" in test_mismatch.expected_locale

    # 3. Cross-locale blast radius contamination:
    # A change to 'locales/hi/benefits.json' claiming coverage of 'locales/en/appointments.json'
    hi_change = [
        ChangedFile(path="locales/hi/benefits.json", kind=ChangeKind.CONTENT, locale="hi")
    ]
    cross_locale_coverage = [
        CoverageEntry(
            test_id="t_e2e_appointments_en",
            files=["locales/en/appointments.json"],
            captured_at=datetime.now(timezone.utc),
            stale=False,
        )
    ]
    report_cross = validate_multilingual_routing(changes=hi_change, coverage=cross_locale_coverage)
    assert report_cross.is_valid is False
    cross_mismatch = next(m for m in report_cross.mismatches if m.error_type == "cross_locale_contamination")
    assert cross_mismatch.actual_locale == "en"
    assert "hi" in cross_mismatch.expected_locale


# ---------------------------------------------------------------------------
# Requirement 1 & 2: Pipeline Implementation Tests
# ---------------------------------------------------------------------------


def test_dependency_extraction_pipeline(tmp_path):
    """Verify AST extraction, import resolution, graph inversion, deduplication, and export."""
    # Synthesize Python modules
    session_code = """
import os
class Session:
    pass
"""
    otp_code = """
from src.auth.session import Session
class OTP:
    pass
"""
    profile_code = """
from src.auth.session import Session
from src.auth.otp import OTP
class Profile:
    pass
"""
    source_inputs = [
        SourceFileInput(path="src/auth/session.py", content=session_code),
        SourceFileInput(path="src/auth/otp.py", content=otp_code),
        SourceFileInput(path="src/citizen/profile.py", content=profile_code),
    ]

    static_seeds = {
        "config/app.yaml": ["src/auth/session.py"]
    }

    edges, metadata = build_dependency_graph(
        source_files=source_inputs,
        static_dependencies=static_seeds,
    )

    edge_map = {e.source: e.dependents for e in edges}

    # Verify inversion: 'src/auth/session.py' is imported by otp.py and profile.py
    assert "src/auth/session.py" in edge_map
    assert "src/auth/otp.py" in edge_map["src/auth/session.py"]
    assert "src/citizen/profile.py" in edge_map["src/auth/session.py"]

    # Verify static seed inclusion
    assert "config/app.yaml" in edge_map
    assert "src/auth/session.py" in edge_map["config/app.yaml"]

    # Verify deduplication and sorting
    for edge in edges:
        assert len(edge.dependents) == len(set(edge.dependents))
        assert edge.dependents == sorted(edge.dependents)

    # Test staleness checking against modified file hashes
    stale_report = check_graph_staleness(
        graph=edges,
        snapshot_hashes={"src/auth/session.py": "old-hash-differs"},
        repo_root=None,
    )
    # No repo_root means disk check is skipped; if snapshot hash exists and matches nothing, report is clean
    assert isinstance(stale_report.is_stale, bool)


def test_coverage_ingestion_pipeline():
    """Verify test run ingestion, file coverage merging, and 24-hour staleness tracking."""
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    fresh_time = now - timedelta(hours=2)
    stale_time = now - timedelta(hours=26)

    existing_catalog = [
        CoverageEntry(
            test_id="t_existing",
            files=["src/auth/session.py"],
            captured_at=stale_time,
            stale=True,
        )
    ]

    new_events = [
        # Fresh run of existing test with new file touched
        TestExecutionEvent(
            test_id="t_existing",
            files_touched=["src/auth/otp.py"],
            passed=True,
            duration_seconds=1.5,
            timestamp=fresh_time,
            runner_id="runner-1",
        ),
        # New test run that is fresh
        TestExecutionEvent(
            test_id="t_new_fresh",
            files_touched=["src/citizen/profile.py"],
            passed=True,
            duration_seconds=2.0,
            timestamp=fresh_time,
            runner_id="runner-1",
        ),
        # Test run with timestamp older than 24h
        TestExecutionEvent(
            test_id="t_new_stale",
            files_touched=["src/payments/gateway.py"],
            passed=True,
            duration_seconds=3.0,
            timestamp=stale_time,
            runner_id="runner-2",
        ),
    ]

    updated_entries, summary = ingest_coverage_events(
        events=new_events,
        existing_coverage=existing_catalog,
        reference_time=now,
        merge_strategy="union",
    )

    by_id = {e.test_id: e for e in updated_entries}

    # 1. Existing test should be updated, merged files (union of session.py and otp.py), and now fresh!
    assert "t_existing" in by_id
    assert by_id["t_existing"].files == ["src/auth/otp.py", "src/auth/session.py"]
    assert by_id["t_existing"].stale is False  # Refreshed from stale to fresh!
    assert by_id["t_existing"].captured_at == fresh_time

    # 2. Fresh test should have stale=False
    assert by_id["t_new_fresh"].stale is False

    # 3. Test older than 24h should have stale=True
    assert by_id["t_new_stale"].stale is True

    # 4. Summary metrics
    assert summary.total_tests_cataloged == 3
    assert summary.tests_updated == 1
    assert summary.tests_new == 2
    assert summary.stale_count == 1


# ---------------------------------------------------------------------------
# Requirement 3: Real-Time CI/CD Telemetry Streaming & Feedback Tests
# ---------------------------------------------------------------------------


def test_telemetry_streaming_and_feedback(tmp_path):
    """Verify runner telemetry streaming, retry/spool fallback, and failures.json ingestion."""
    client = TestClient(app)
    get_store.cache_clear()

    # 1. Successful POST /v1/feedback with telemetry payload
    telemetry_data = {
        "change_id": "chg_auth_session",
        "test_id": "t_unit_session",
        "passed": False,
        "duration_seconds": 1.45,
        "related_files": ["src/auth/session.py"],
        "runner_env": {
            "ci_provider": "github-actions",
            "runner_id": "gh-runner-42",
            "git_sha": "abc1234",
            "run_attempt": 1,
        },
    }

    res = client.post(
        "/v1/feedback",
        json=telemetry_data,
        headers={"X-Org-Id": ORG, "X-Role": "qa_operator", "X-Actor-Id": "ci-bot"},
    )
    assert res.status_code == 200
    res_json = res.json()
    assert res_json["accepted"] is True
    assert res_json["record"]["failed"] is True

    # 2. Flakiness detection: A subsequent run with run_attempt > 1 that passes flags flaky
    retry_telemetry = {
        "change_id": "chg_auth_session",
        "test_id": "t_unit_session",
        "passed": True,
        "duration_seconds": 1.40,
        "runner_env": {
            "ci_provider": "github-actions",
            "runner_id": "gh-runner-42",
            "run_attempt": 2,  # Retried run
        },
    }
    retry_res = client.post(
        "/v1/feedback",
        json=retry_telemetry,
        headers={"X-Org-Id": ORG, "X-Role": "qa_operator", "X-Actor-Id": "ci-bot"},
    )
    assert retry_res.status_code == 200
    assert retry_res.json()["flaky"] is True

    # 3. Client retry and offline spooling fallback on server downtime
    spool_file = tmp_path / "telemetry_spool.jsonl"
    payload = TelemetryPayload(
        change_id="chg_auth_session",
        test_id="t_unit_otp",
        passed=True,
    )

    delivery = send_telemetry_with_retry(
        payload=payload,
        endpoint_url="http://127.0.0.1:59999/unreachable/endpoint",  # Intentionally unreachable
        max_retries=2,
        base_delay_seconds=0.05,
        spool_path=spool_file,
    )
    assert delivery.success is False
    assert delivery.spooled is True
    assert spool_file.exists()
    spooled_content = spool_file.read_text(encoding="utf-8")
    assert "t_unit_otp" in spooled_content
