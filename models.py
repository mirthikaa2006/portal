"""Canonical data schemas used by the selector, store, and API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ChangeKind(str, Enum):
    CODE = "code"
    CONTENT = "content"
    CONFIG = "config"
    SCHEMA = "schema"
    MIXED = "mixed"


class TestLayer(str, Enum):
    UNIT = "unit"
    INTEGRATION = "integration"
    E2E = "e2e"
    LOCALE = "locale"
    ACCESSIBILITY = "accessibility"
    SECURITY = "security"
    SMOKE = "smoke"


class Decision(str, Enum):
    MUST_RUN = "must_run"
    RECOMMENDED = "recommended"
    SKIP = "skip"


class Role(str, Enum):
    ORG_ADMIN = "org_admin"
    QA_OPERATOR = "qa_operator"
    PARTNER = "partner"
    AUDITOR = "auditor"


class SourceStatus(str, Enum):
    AVAILABLE = "available"
    DELAYED = "delayed"
    MISSING = "missing"


class ChangedFile(BaseModel):
    path: str
    kind: ChangeKind
    locale: Optional[str] = None
    lines_added: int = 0
    lines_removed: int = 0


class CodeChange(BaseModel):
    change_id: str
    org_id: str
    files: list[ChangedFile]
    author: str
    timestamp: datetime
    summary: str
    partner_visible: bool = False


class TestCase(BaseModel):
    test_id: str
    name: str
    org_id: str
    layer: TestLayer
    estimated_seconds: float
    tags: list[str] = Field(default_factory=list)
    locales: list[str] = Field(default_factory=list)
    critical: bool = False
    partner_visible: bool = True
    owner_team: str = "qa"


class CoverageEntry(BaseModel):
    test_id: str
    files: list[str]
    captured_at: datetime
    stale: bool = False


class DependencyEdge(BaseModel):
    source: str
    dependents: list[str]


class FailureEvent(BaseModel):
    test_id: str
    change_id: str
    failed: bool
    flaky: bool
    related_files: list[str] = Field(default_factory=list)
    timestamp: datetime


class SourceHealth(BaseModel):
    code_changes: SourceStatus = SourceStatus.AVAILABLE
    dependencies: SourceStatus = SourceStatus.AVAILABLE
    coverage: SourceStatus = SourceStatus.AVAILABLE
    failure_history: SourceStatus = SourceStatus.AVAILABLE
    notes: list[str] = Field(default_factory=list)

    @property
    def completeness(self) -> float:
        weights = {
            "code_changes": 0.35,
            "dependencies": 0.25,
            "coverage": 0.25,
            "failure_history": 0.15,
        }
        score = 0.0
        for name, weight in weights.items():
            status = getattr(self, name)
            if status == SourceStatus.AVAILABLE:
                score += weight
            elif status == SourceStatus.DELAYED:
                score += weight * 0.4
        return round(score, 3)


class Actor(BaseModel):
    actor_id: str
    org_id: str
    role: Role
    partner_org_id: Optional[str] = None


class SelectionRequest(BaseModel):
    change_id: Optional[str] = None
    org_id: str
    files: list[ChangedFile] = Field(default_factory=list)
    summary: str = ""
    simulate_missing: list[str] = Field(default_factory=list)


class TestRecommendation(BaseModel):
    test_id: str
    name: str
    decision: Decision
    impact_score: float
    confidence: float
    estimated_seconds: float
    sources_used: list[str]
    reasons: list[str]
    skip_rationale: Optional[str] = None
    layer: TestLayer
    locales: list[str] = Field(default_factory=list)


class SelectionResult(BaseModel):
    change_id: str
    org_id: str
    change_kind: ChangeKind
    locales_touched: list[str]
    source_health: SourceHealth
    skip_threshold: float
    recommendations: list[TestRecommendation]
    suite_seconds_full: float
    suite_seconds_selected: float
    tests_total: int
    tests_to_run: int
    tests_skipped: int
    safety_notes: list[str] = Field(default_factory=list)
    policy: dict[str, str] = Field(default_factory=dict)
