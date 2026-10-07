"""Load portal datasets and report per-source availability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from impact_selector.models import (
    CodeChange,
    CoverageEntry,
    DependencyEdge,
    FailureEvent,
    SourceHealth,
    SourceStatus,
    TestCase,
)

DATA_FILES = {
    "code_changes": "changes.json",
    "dependencies": "dependencies.json",
    "coverage": "coverage.json",
    "failure_history": "failures.json",
    "tests": "tests.json",
}


class DataStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.tests: list[TestCase] = []
        self.changes: dict[str, CodeChange] = {}
        self.coverage: dict[str, CoverageEntry] = {}
        self.dependents: dict[str, list[str]] = {}
        self.failures: list[FailureEvent] = []
        self.health = SourceHealth()
        self._load()

    def _read_json(self, filename: str):
        path = self.data_dir / filename
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _load(self) -> None:
        tests_raw = self._read_json(DATA_FILES["tests"])
        if tests_raw is None:
            self.health.notes.append("Test catalog missing; selector cannot run.")
        else:
            self.tests = [TestCase.model_validate(item) for item in tests_raw]

        changes_raw = self._read_json(DATA_FILES["code_changes"])
        if changes_raw is None:
            self.health.code_changes = SourceStatus.MISSING
        else:
            loaded = [CodeChange.model_validate(item) for item in changes_raw]
            self.changes = {item.change_id: item for item in loaded}

        deps_raw = self._read_json(DATA_FILES["dependencies"])
        if deps_raw is None:
            self.health.dependencies = SourceStatus.MISSING
        else:
            edges = [DependencyEdge.model_validate(item) for item in deps_raw]
            self.dependents = {edge.source: list(edge.dependents) for edge in edges}

        cov_raw = self._read_json(DATA_FILES["coverage"])
        if cov_raw is None:
            self.health.coverage = SourceStatus.MISSING
        else:
            entries = [CoverageEntry.model_validate(item) for item in cov_raw]
            self.coverage = {entry.test_id: entry for entry in entries}
            if stale_n := sum(1 for entry in entries if entry.stale):
                ratio = stale_n / max(len(entries), 1)
                self.health.notes.append(f"{stale_n} coverage row(s) are stale.")
                if ratio >= 0.25:
                    self.health.coverage = SourceStatus.DELAYED
                    self.health.notes.append("Coverage snapshot is delayed; skip threshold raised.")

        fail_raw = self._read_json(DATA_FILES["failure_history"])
        if fail_raw is None:
            self.health.failure_history = SourceStatus.MISSING
        else:
            self.failures = [FailureEvent.model_validate(item) for item in fail_raw]

    def with_simulated_outages(self, missing: Iterable[str]) -> "DataStore":
        clone = DataStore.__new__(DataStore)
        clone.data_dir = self.data_dir
        clone.tests = list(self.tests)
        clone.changes = dict(self.changes)
        clone.coverage = dict(self.coverage)
        clone.dependents = dict(self.dependents)
        clone.failures = list(self.failures)
        clone.health = self.health.model_copy(deep=True)
        for name in missing:
            if name == "code_changes":
                clone.changes = {}
                clone.health.code_changes = SourceStatus.MISSING
            elif name == "dependencies":
                clone.dependents = {}
                clone.health.dependencies = SourceStatus.MISSING
            elif name == "coverage":
                clone.coverage = {}
                clone.health.coverage = SourceStatus.MISSING
            elif name == "failure_history":
                clone.failures = []
                clone.health.failure_history = SourceStatus.MISSING
            elif name == "coverage_delayed":
                clone.health.coverage = SourceStatus.DELAYED
                clone.health.notes.append("Simulated delayed coverage metrics.")
        return clone

    def tests_for_org(self, org_id: str) -> list[TestCase]:
        return [test for test in self.tests if test.org_id == org_id]
