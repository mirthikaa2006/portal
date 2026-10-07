"""Edge-Case Validation Methodology.

Provides production-ready validation for:
1. Circular dependency detection and graceful graph sanitization.
2. Stale build artifact handling (24-hour threshold, threshold elevation, pattern fallback).
3. Multilingual routing validation (strict locale set enforcement, test-to-file locale
   coherence, and cross-locale blast radius mismatch detection).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from impact_selector.coverage import is_entry_stale
from impact_selector.language import SUPPORTED_LOCALES, detect_locale
from impact_selector.models import (
    ChangedFile,
    ChangeKind,
    CoverageEntry,
    DependencyEdge,
    SourceHealth,
    SourceStatus,
    TestCase,
)

logger = logging.getLogger("impact_selector.validation")

ALLOWED_LOCALES: set[str] = set(SUPPORTED_LOCALES)  # {"en", "hi", "ta", "te", "bn"}


class CircularDependencyWarning(BaseModel):
    """Details of a detected cycle in the dependency graph."""

    cycle_path: list[str]
    broken_edge: tuple[str, str]
    message: str


class DependencyCycleReport(BaseModel):
    """Summary of circular dependency analysis."""

    has_cycles: bool
    cycles_detected: list[list[str]] = Field(default_factory=list)
    warnings: list[CircularDependencyWarning] = Field(default_factory=list)


class StaleArtifactReport(BaseModel):
    """Summary of coverage artifact staleness analysis."""

    total_entries: int
    stale_entries: int
    stale_ratio: float
    is_delayed: bool
    skip_threshold_impact: float
    fallback_strategy: str
    stale_test_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MultilingualMismatch(BaseModel):
    """Record of a multilingual routing violation or mismatch."""

    error_type: str  # "invalid_locale" | "test_file_mismatch" | "cross_locale_contamination"
    entity_id: str
    message: str
    expected_locale: Optional[str] = None
    actual_locale: Optional[str] = None
    conflicting_path: Optional[str] = None


class MultilingualValidationReport(BaseModel):
    """Validation report across changes, tests, and coverage mappings."""

    is_valid: bool
    rejected_locales: list[str] = Field(default_factory=list)
    mismatches: list[MultilingualMismatch] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Circular Dependency Detection and Sanitization
# ---------------------------------------------------------------------------


def detect_circular_dependencies(graph: dict[str, list[str]]) -> list[list[str]]:
    """Detect all cycles in a directed graph using 3-color DFS.

    Colors:
    0 = White (unvisited)
    1 = Gray (visiting, currently in recursion stack)
    2 = Black (visited, completely explored)
    """
    color: dict[str, int] = {node: 0 for node in graph}
    # Ensure all targets are in color dict
    for targets in graph.values():
        for target in targets:
            color.setdefault(target, 0)

    cycles: list[list[str]] = []
    current_path: list[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        current_path.append(node)

        for neighbor in graph.get(node, []):
            neighbor_color = color.get(neighbor, 0)
            if neighbor_color == 1:
                # Cycle found! Extract subpath from neighbor to current
                if neighbor in current_path:
                    idx = current_path.index(neighbor)
                    cycle = current_path[idx:] + [neighbor]
                    cycles.append(cycle)
            elif neighbor_color == 0:
                dfs(neighbor)

        current_path.pop()
        color[node] = 2

    for node in sorted(graph.keys()):
        if color[node] == 0:
            dfs(node)

    return cycles


def sanitize_dependency_graph(
    graph: dict[str, list[str]],
) -> tuple[dict[str, list[str]], DependencyCycleReport]:
    """Gracefully sanitize dependency graph by detecting cycles and breaking back-edges.

    Prevents infinite recursion in the test selector while logging actionable warnings.
    """
    sanitized: dict[str, list[str]] = {k: list(v) for k, v in graph.items()}
    detected_cycles = detect_circular_dependencies(sanitized)

    warnings: list[CircularDependencyWarning] = []

    if not detected_cycles:
        return sanitized, DependencyCycleReport(has_cycles=False)

    for cycle in detected_cycles:
        # Break the back-edge that closes the cycle (last node -> first node of loop)
        u, v = cycle[-2], cycle[-1]
        if u in sanitized and v in sanitized[u]:
            sanitized[u].remove(v)
            warning = CircularDependencyWarning(
                cycle_path=cycle,
                broken_edge=(u, v),
                message=(
                    f"Circular dependency detected: {' -> '.join(cycle)}. "
                    f"Gracefully pruned back-edge ({u} -> {v}) to prevent infinite expansion."
                ),
            )
            warnings.append(warning)
            logger.warning(warning.message)

    return sanitized, DependencyCycleReport(
        has_cycles=True,
        cycles_detected=detected_cycles,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 2. Stale Build Artifact / Coverage Handling
# ---------------------------------------------------------------------------


def validate_coverage_staleness(
    coverage_entries: list[CoverageEntry],
    reference_time: Optional[datetime] = None,
    threshold_hours: float = 24.0,
) -> StaleArtifactReport:
    """Evaluate coverage freshness against the 24-hour production threshold.

    Calculates impact on the test selector:
    - If >= 25% entries are stale, coverage health becomes DELAYED.
    - Skip threshold increases by +0.06 in algorithm scoring.
    - Selector falls back to name/tag overlap (+0.45) and locale overlap (+0.55).
    """
    total = len(coverage_entries)
    if total == 0:
        return StaleArtifactReport(
            total_entries=0,
            stale_entries=0,
            stale_ratio=0.0,
            is_delayed=True,
            skip_threshold_impact=0.12,  # Missing coverage impact
            fallback_strategy="Pattern overlap matching and locale rules (coverage missing)",
            notes=["No coverage entries available; selector fails closed."],
        )

    stale_ids: list[str] = []
    for entry in coverage_entries:
        if is_entry_stale(entry.captured_at, reference_time, threshold_hours) or entry.stale:
            stale_ids.append(entry.test_id)

    stale_count = len(stale_ids)
    stale_ratio = round(stale_count / total, 4)
    is_delayed = stale_ratio >= 0.25

    skip_impact = 0.06 if is_delayed else 0.0
    notes: list[str] = []

    if is_delayed:
        notes.append(
            f"{stale_count}/{total} ({stale_ratio * 100:.1f}%) coverage entries exceed {threshold_hours}h "
            "staleness threshold. Coverage marked as DELAYED."
        )
        notes.append(f"Skip threshold elevated by +{skip_impact} to fail closed against stale data.")
        fallback = "Elevated skip threshold; reliance on name/tag pattern overlap (+0.45) and locale rules"
    else:
        fallback = "Direct coverage analysis with standard skip threshold"
        if stale_count > 0:
            notes.append(f"{stale_count} coverage entries are stale but below 25% delay threshold.")

    return StaleArtifactReport(
        total_entries=total,
        stale_entries=stale_count,
        stale_ratio=stale_ratio,
        is_delayed=is_delayed,
        skip_threshold_impact=skip_impact,
        fallback_strategy=fallback,
        stale_test_ids=stale_ids,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 3. Multilingual Routing Validation
# ---------------------------------------------------------------------------


def validate_multilingual_routing(
    changes: Optional[list[ChangedFile]] = None,
    tests: Optional[list[TestCase]] = None,
    coverage: Optional[list[CoverageEntry]] = None,
) -> MultilingualValidationReport:
    """Validate locale constraints and catch routing mismatches.

    Rules enforced:
    1. Rejects any locale not in ALLOWED_LOCALES {'en', 'hi', 'ta', 'te', 'bn'}.
    2. Flags tests tagged with locale X touching locale content files for locale Y (X != Y).
    3. Flags changes targeting locale X whose coverage routes to locale content files for locale Y.
    """
    rejected_locales: set[str] = set()
    mismatches: list[MultilingualMismatch] = []

    # 1. Validate changes
    if changes:
        for f in changes:
            loc = f.locale or detect_locale(f.path)
            if loc and loc not in ALLOWED_LOCALES:
                rejected_locales.add(loc)
                mismatches.append(
                    MultilingualMismatch(
                        error_type="invalid_locale",
                        entity_id=f.path,
                        message=f"Change file '{f.path}' specifies unsupported locale '{loc}'. Must be one of {sorted(ALLOWED_LOCALES)}.",
                        actual_locale=loc,
                        conflicting_path=f.path,
                    )
                )

    # 2. Validate tests
    tests_by_id: dict[str, TestCase] = {}
    if tests:
        for t in tests:
            tests_by_id[t.test_id] = t
            for loc in t.locales:
                if loc not in ALLOWED_LOCALES:
                    rejected_locales.add(loc)
                    mismatches.append(
                        MultilingualMismatch(
                            error_type="invalid_locale",
                            entity_id=t.test_id,
                            message=f"Test '{t.test_id}' specifies unsupported locale '{loc}'. Must be one of {sorted(ALLOWED_LOCALES)}.",
                            actual_locale=loc,
                        )
                    )

    # 3. Validate test-to-file coverage coherence
    if coverage and tests_by_id:
        for entry in coverage:
            test = tests_by_id.get(entry.test_id)
            if not test or not test.locales:
                continue

            test_locales = set(test.locales)
            # Find content files touched by this test
            for file_path in entry.files:
                file_loc = detect_locale(file_path)
                if file_loc:
                    if file_loc not in ALLOWED_LOCALES:
                        rejected_locales.add(file_loc)
                        mismatches.append(
                            MultilingualMismatch(
                                error_type="invalid_locale",
                                entity_id=entry.test_id,
                                message=f"Coverage for '{entry.test_id}' references unsupported locale file '{file_path}' (locale '{file_loc}').",
                                actual_locale=file_loc,
                                conflicting_path=file_path,
                            )
                        )
                    elif file_loc not in test_locales:
                        mismatches.append(
                            MultilingualMismatch(
                                error_type="test_file_mismatch",
                                entity_id=entry.test_id,
                                message=(
                                    f"Test '{entry.test_id}' is tagged for locale(s) {sorted(test_locales)} "
                                    f"but its coverage references contradictory locale file '{file_path}' (locale '{file_loc}')."
                                ),
                                expected_locale=",".join(sorted(test_locales)),
                                actual_locale=file_loc,
                                conflicting_path=file_path,
                            )
                        )

    # 4. Validate change-to-coverage cross-locale blast radius
    if changes and coverage:
        # If change is content-only in specific locales
        change_locales = {
            f.locale or detect_locale(f.path)
            for f in changes
            if (f.locale or detect_locale(f.path)) and f.kind == ChangeKind.CONTENT
        }
        change_locales.discard(None)

        if change_locales:
            for entry in coverage:
                for file_path in entry.files:
                    file_loc = detect_locale(file_path)
                    # Check if coverage maps to a distinct content locale file
                    if file_loc and file_loc in ALLOWED_LOCALES and file_loc not in change_locales:
                        # If the file touched is content (e.g. locales/en/appointments.json)
                        if "locales/" in file_path.replace("\\", "/"):
                            # This is cross-locale contamination!
                            mismatches.append(
                                MultilingualMismatch(
                                    error_type="cross_locale_contamination",
                                    entity_id=entry.test_id,
                                    message=(
                                        f"Change in locale(s) {sorted(change_locales)} affected coverage "
                                        f"for test '{entry.test_id}' referencing unrelated locale content '{file_path}' (locale '{file_loc}')."
                                    ),
                                    expected_locale=",".join(sorted(change_locales)),
                                    actual_locale=file_loc,
                                    conflicting_path=file_path,
                                    )
                                )

    is_valid = len(rejected_locales) == 0 and len(mismatches) == 0

    return MultilingualValidationReport(
        is_valid=is_valid,
        rejected_locales=sorted(rejected_locales),
        mismatches=mismatches,
    )
