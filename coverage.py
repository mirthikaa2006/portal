"""Coverage-Ingestion Pipeline.

Ingests test execution telemetry from test runners, maps test IDs to affected files,
consolidates multi-run file coverage with deduplication, tracks snapshot freshness
against a 24-hour staleness threshold, and outputs coverage.json format.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union

from pydantic import BaseModel, Field

from impact_selector.models import CoverageEntry

logger = logging.getLogger("impact_selector.coverage")

# Production threshold: coverage older than 24 hours is marked as stale
STALENESS_THRESHOLD_HOURS: float = 24.0
STALENESS_THRESHOLD_SECONDS: float = STALENESS_THRESHOLD_HOURS * 3600.0


class TestExecutionEvent(BaseModel):
    """Telemetry event produced by a test runner upon test execution completion."""

    test_id: str
    files_touched: list[str] = Field(default_factory=list)
    passed: bool = True
    duration_seconds: float = Field(default=0.0, ge=0.0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    runner_id: Optional[str] = None
    environment: dict[str, str] = Field(default_factory=dict)


class CoverageIngestionBatch(BaseModel):
    """Batch payload of test execution events to ingest."""

    events: list[TestExecutionEvent]
    reference_time: Optional[datetime] = None
    merge_strategy: str = Field(default="union", description="'union' or 'replace'")


class IngestionSummary(BaseModel):
    """Metrics produced after coverage ingestion."""

    total_tests_cataloged: int
    tests_updated: int
    tests_new: int
    stale_count: int
    stale_ratio: float
    coverage_health: str  # "available" or "delayed"


def normalize_path(path: str) -> str:
    """Normalize file path to POSIX relative form."""
    cleaned = path.replace("\\", "/").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def to_utc_datetime(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_entry_stale(
    captured_at: datetime,
    reference_time: Optional[datetime] = None,
    threshold_hours: float = STALENESS_THRESHOLD_HOURS,
) -> bool:
    """Check whether a coverage snapshot exceeds the staleness window."""
    ref = to_utc_datetime(reference_time or datetime.now(timezone.utc))
    cap = to_utc_datetime(captured_at)
    age_seconds = (ref - cap).total_seconds()
    return age_seconds > (threshold_hours * 3600.0)


def ingest_coverage_events(
    events: list[TestExecutionEvent],
    existing_coverage: Optional[Union[list[CoverageEntry], dict[str, CoverageEntry]]] = None,
    reference_time: Optional[datetime] = None,
    merge_strategy: str = "union",
) -> tuple[list[CoverageEntry], IngestionSummary]:
    """Ingest test execution events, merge coverage, and refresh staleness.

    Args:
        events: List of new test execution records.
        existing_coverage: Prior coverage catalog (list or dict).
        reference_time: Evaluation timestamp (defaults to now).
        merge_strategy: 'union' (accumulate touched files across runs) or 'replace'
            (overwrite touched files with latest run).

    Returns:
        tuple of (updated_coverage_entries, ingestion_summary)
    """
    ref_time = to_utc_datetime(reference_time or datetime.now(timezone.utc))

    # Convert existing into a working dictionary
    catalog: dict[str, CoverageEntry] = {}
    if existing_coverage:
        if isinstance(existing_coverage, dict):
            catalog = {k: v.model_copy(deep=True) for k, v in existing_coverage.items()}
        else:
            catalog = {e.test_id: e.model_copy(deep=True) for e in existing_coverage}

    tests_updated = 0
    tests_new = 0

    for event in events:
        test_id = event.test_id
        event_time = to_utc_datetime(event.timestamp)
        norm_files = sorted({normalize_path(f) for f in event.files_touched if f.strip()})

        if test_id in catalog:
            existing = catalog[test_id]
            tests_updated += 1

            if merge_strategy == "replace":
                merged_files = norm_files
            else:
                # Union mode: consolidate coverage files across executions
                merged_files = sorted(set(existing.files) | set(norm_files))

            # Latest timestamp takes precedence
            latest_time = max(to_utc_datetime(existing.captured_at), event_time)
            stale = is_entry_stale(latest_time, ref_time)

            catalog[test_id] = CoverageEntry(
                test_id=test_id,
                files=merged_files,
                captured_at=latest_time,
                stale=stale,
            )
        else:
            tests_new += 1
            stale = is_entry_stale(event_time, ref_time)
            catalog[test_id] = CoverageEntry(
                test_id=test_id,
                files=norm_files,
                captured_at=event_time,
                stale=stale,
            )

    # Recalculate staleness for all untouched existing entries against the reference time
    for test_id, entry in catalog.items():
        entry.stale = is_entry_stale(entry.captured_at, ref_time)

    result_list = sorted(catalog.values(), key=lambda e: e.test_id)
    total_count = len(result_list)
    stale_count = sum(1 for e in result_list if e.stale)
    stale_ratio = stale_count / max(total_count, 1)

    # Production rule: >= 25% stale coverage shifts source status to DELAYED
    coverage_health = "delayed" if stale_ratio >= 0.25 else "available"

    summary = IngestionSummary(
        total_tests_cataloged=total_count,
        tests_updated=tests_updated,
        tests_new=tests_new,
        stale_count=stale_count,
        stale_ratio=round(stale_ratio, 4),
        coverage_health=coverage_health,
    )

    return result_list, summary


def load_coverage_json(file_path: Path | str) -> list[CoverageEntry]:
    """Load and validate coverage.json file."""
    path = Path(file_path)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [CoverageEntry.model_validate(item) for item in raw]


def write_coverage_json(
    entries: list[CoverageEntry],
    output_path: Path | str,
) -> None:
    """Save coverage catalog to disk in standard coverage.json format."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [e.model_dump(mode="json") for e in sorted(entries, key=lambda x: x.test_id)]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(f"Wrote {len(entries)} coverage records to {output_path}")
