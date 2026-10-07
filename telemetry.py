"""Real-Time CI/CD Telemetry Streaming.

Provides schemas, client-side streaming with exponential backoff and offline spooling,
and server-side telemetry receiver for streaming runner feedback into failures.json
to update flakiness history and failure correlation.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from impact_selector.models import Actor, FailureEvent, Role

logger = logging.getLogger("impact_selector.telemetry")


class RunnerEnvironment(BaseModel):
    """Metadata regarding the CI/CD runner executing the test suite."""

    ci_provider: str = Field(default="github-actions", description="'github-actions' or 'gitlab-ci'")
    runner_id: str = Field(default="unknown-runner")
    git_sha: str = Field(default="")
    git_branch: Optional[str] = None
    run_id: Optional[str] = None
    workflow_name: Optional[str] = None
    run_attempt: int = Field(default=1, ge=1)


class TelemetryPayload(BaseModel):
    """Telemetry record streamed from a CI/CD test runner back to the selector."""

    change_id: str
    test_id: str
    passed: bool
    duration_seconds: float = Field(default=0.0, ge=0.0)
    related_files: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None
    runner_env: Optional[RunnerEnvironment] = None
    actor_id: str = "ci-bot"
    org_id: str = "ministry_interior"


class TelemetryBatch(BaseModel):
    """Batch of telemetry payloads streamed at the end of a CI test run."""

    change_id: str
    events: list[TelemetryPayload]
    runner_env: Optional[RunnerEnvironment] = None
    actor_id: str = "ci-bot"
    org_id: str = "ministry_interior"


class TelemetryDeliveryResult(BaseModel):
    """Client-side delivery confirmation or spool status."""

    success: bool
    status_code: Optional[int] = None
    spooled: bool = False
    attempts: int = 1
    error_message: Optional[str] = None


def normalize_iso_datetime(dt: datetime) -> str:
    """Format datetime as UTC ISO-8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def send_telemetry_with_retry(
    payload: TelemetryPayload | dict[str, Any],
    endpoint_url: str,
    headers: Optional[dict[str, str]] = None,
    max_retries: int = 3,
    base_delay_seconds: float = 0.5,
    spool_path: Optional[Path | str] = None,
) -> TelemetryDeliveryResult:
    """Stream telemetry with exponential backoff and local spool fallback.

    Ensures that CI/CD test telemetry is never lost on network blips, while
    guaranteeing that telemetry transmission failures never fail a developer build.
    """
    if isinstance(payload, TelemetryPayload):
        body_dict = payload.model_dump(mode="json")
    else:
        body_dict = payload

    req_headers = {
        "Content-Type": "application/json",
        "X-Actor-Id": body_dict.get("actor_id", "ci-bot"),
        "X-Org-Id": body_dict.get("org_id", "ministry_interior"),
        "X-Role": "qa_operator",
    }
    if headers:
        req_headers.update(headers)

    last_error: Optional[str] = None
    last_status: Optional[int] = None

    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(endpoint_url, json=body_dict, headers=req_headers)
                last_status = resp.status_code
                if resp.is_success:
                    return TelemetryDeliveryResult(
                        success=True,
                        status_code=resp.status_code,
                        spooled=False,
                        attempts=attempt,
                    )
                # Client error (4xx) usually means validation error; do not uselessly retry
                if 400 <= resp.status_code < 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text}"
                    logger.warning(f"Telemetry rejected by server: {last_error}")
                    break

                last_error = f"Server error HTTP {resp.status_code}: {resp.text}"
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = f"Network connection failed: {exc}"

        # Exponential backoff with jitter
        if attempt < max_retries:
            delay = (base_delay_seconds * (2 ** (attempt - 1))) + random.uniform(0.05, 0.25)
            time.sleep(delay)

    # If all attempts failed, spool locally if spool_path is configured
    spooled = False
    if spool_path:
        spool_file = Path(spool_path)
        try:
            spool_file.parent.mkdir(parents=True, exist_ok=True)
            with spool_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(body_dict) + "\n")
            spooled = True
            logger.info(f"Telemetry spooled to {spool_path} after failed delivery.")
        except OSError as exc:
            logger.error(f"Failed to spool telemetry to disk: {exc}")

    return TelemetryDeliveryResult(
        success=False,
        status_code=last_status,
        spooled=spooled,
        attempts=max_retries,
        error_message=last_error,
    )


def flush_telemetry_spool(
    spool_path: Path | str,
    endpoint_url: str,
    headers: Optional[dict[str, str]] = None,
) -> int:
    """Flush spooled telemetry events back to the server once online."""
    path = Path(spool_path)
    if not path.is_file():
        return 0

    lines = path.read_text(encoding="utf-8").splitlines()
    remaining_lines: list[str] = []
    flushed_count = 0

    for line in lines:
        if not line.strip():
            continue
        try:
            event_dict = json.loads(line)
            res = send_telemetry_with_retry(
                payload=event_dict,
                endpoint_url=endpoint_url,
                headers=headers,
                max_retries=2,
                base_delay_seconds=0.2,
                spool_path=None,
            )
            if res.success:
                flushed_count += 1
            else:
                remaining_lines.append(line)
        except Exception:
            remaining_lines.append(line)

    if remaining_lines:
        path.write_text("\n".join(remaining_lines) + "\n", encoding="utf-8")
    else:
        try:
            path.unlink()
        except OSError:
            pass

    return flushed_count


def record_failure_telemetry(
    payload: TelemetryPayload,
    failures_file_path: Path | str,
) -> tuple[FailureEvent, bool]:
    """Ingest test telemetry into failures.json and update flakiness signals.

    Flakiness logic:
    - If runner environment indicates retry (run_attempt > 1) and test passed, test is flaky.
    - If prior runs on this (test_id, change_id) had differing outcomes (failed + passed), test is flaky.
    """
    path = Path(failures_file_path)
    existing_records: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing_records = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Could not read existing failures.json: {exc}")

    is_failed = not payload.passed

    # Detect flakiness
    is_flaky = False
    if payload.runner_env and payload.runner_env.run_attempt > 1 and payload.passed:
        is_flaky = True

    # Check historical runs for this test on the change
    matching_runs = [
        r for r in existing_records
        if r.get("test_id") == payload.test_id and r.get("change_id") == payload.change_id
    ]
    if matching_runs:
        prior_failed = any(r.get("failed") for r in matching_runs)
        if prior_failed and payload.passed:
            is_flaky = True
        elif (not prior_failed) and is_failed:
            is_flaky = True

    # Check overall test flakiness in history
    test_runs = [r for r in existing_records if r.get("test_id") == payload.test_id]
    if test_runs:
        historical_flakes = sum(1 for r in test_runs if r.get("flaky"))
        if historical_flakes / max(len(test_runs), 1) >= 0.25:
            is_flaky = True

    # Create canonical FailureEvent
    event = FailureEvent(
        test_id=payload.test_id,
        change_id=payload.change_id,
        failed=is_failed,
        flaky=is_flaky,
        related_files=payload.related_files,
        timestamp=payload.timestamp,
    )

    existing_records.append(event.model_dump(mode="json"))

    # Write atomically
    temp_path = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_text(json.dumps(existing_records, indent=2), encoding="utf-8")
    temp_path.replace(path)

    return event, is_flaky
