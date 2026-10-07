"""FastAPI layer — thin wrapper around the selector."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from impact_selector.access import visible_tests
from impact_selector.algorithm import select_tests
from impact_selector.models import Actor, ChangedFile, Role, SelectionRequest
from impact_selector.store import DataStore
from impact_selector.telemetry import (
    RunnerEnvironment,
    TelemetryPayload,
    record_failure_telemetry,
)

DATA_DIR = Path(os.environ.get("MITHUNA_DATA_DIR", "data"))

app = FastAPI(
    title="Mithuna Change-Impact Test Selector",
    description=(
        "Recommends which portal tests can be skipped for a change, "
        "with transparent rationale and graceful degradation when data sources lag."
    ),
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SelectBody(BaseModel):
    change_id: Optional[str] = None
    org_id: str = "ministry_interior"
    files: list[ChangedFile] = Field(default_factory=list)
    summary: str = ""
    simulate_missing: list[str] = Field(default_factory=list)


class FeedbackBody(BaseModel):
    change_id: str
    test_id: str
    actual_result: Optional[str] = None
    passed: Optional[bool] = None
    duration_seconds: float = 0.0
    related_files: list[str] = Field(default_factory=list)
    timestamp: Optional[datetime] = None
    runner_env: Optional[RunnerEnvironment] = None
    notes: str = ""


_FEEDBACK: list[dict] = []


@lru_cache(maxsize=1)
def get_store() -> DataStore:
    return DataStore(DATA_DIR)


def _actor(
    x_actor_id: str,
    x_org_id: str,
    x_role: str,
    x_partner_org_id: Optional[str],
) -> Actor:
    try:
        role = Role(x_role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown role: {x_role}") from exc
    return Actor(
        actor_id=x_actor_id,
        org_id=x_org_id,
        role=role,
        partner_org_id=x_partner_org_id,
    )


@app.get("/")
def demo_page():
    page = Path(__file__).resolve().parents[1] / "html" / "index.html"
    return FileResponse(page)


@app.get("/health")
def health():
    store = get_store()
    return {"status": "ok", "source_health": store.health.model_dump()}


@app.get("/v1/tests")
def list_tests(
    x_actor_id: str = Header(default="demo-user"),
    x_org_id: str = Header(default="ministry_interior"),
    x_role: str = Header(default="qa_operator"),
    x_partner_org_id: Optional[str] = Header(default=None),
):
    actor = _actor(x_actor_id, x_org_id, x_role, x_partner_org_id)
    store = get_store()
    tests = visible_tests(actor, store.tests_for_org(actor.org_id))
    return {"count": len(tests), "tests": [t.model_dump() for t in tests]}


@app.post("/v1/select")
def select(
    body: SelectBody,
    x_actor_id: str = Header(default="demo-user"),
    x_org_id: str = Header(default="ministry_interior"),
    x_role: str = Header(default="qa_operator"),
    x_partner_org_id: Optional[str] = Header(default=None),
):
    actor = _actor(x_actor_id, x_org_id, x_role, x_partner_org_id)
    org_id = body.org_id or actor.org_id
    if actor.role.value != "org_admin" and org_id != actor.org_id:
        raise HTTPException(status_code=403, detail="Cannot select tests for another organization.")
    store = get_store()
    request = SelectionRequest(
        change_id=body.change_id,
        org_id=org_id,
        files=body.files,
        summary=body.summary,
        simulate_missing=body.simulate_missing,
    )
    return select_tests(request, store, actor).model_dump(mode="json")


@app.get("/v1/select/{change_id}")
def select_known(
    change_id: str,
    missing: list[str] = Query(default=[]),
    x_actor_id: str = Header(default="demo-user"),
    x_org_id: str = Header(default="ministry_interior"),
    x_role: str = Header(default="qa_operator"),
    x_partner_org_id: Optional[str] = Header(default=None),
):
    actor = _actor(x_actor_id, x_org_id, x_role, x_partner_org_id)
    store = get_store()
    request = SelectionRequest(
        change_id=change_id,
        org_id=actor.org_id,
        simulate_missing=missing,
    )
    return select_tests(request, store, actor).model_dump(mode="json")


@app.get("/v1/explain/{change_id}/{test_id}")
def explain(
    change_id: str,
    test_id: str,
    x_actor_id: str = Header(default="demo-user"),
    x_org_id: str = Header(default="ministry_interior"),
    x_role: str = Header(default="qa_operator"),
    x_partner_org_id: Optional[str] = Header(default=None),
):
    actor = _actor(x_actor_id, x_org_id, x_role, x_partner_org_id)
    store = get_store()
    result = select_tests(
        SelectionRequest(change_id=change_id, org_id=actor.org_id),
        store,
        actor,
    )
    match = next((item for item in result.recommendations if item.test_id == test_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Test not visible or not in catalog.")
    return {
        "change_id": change_id,
        "source_health": result.source_health.model_dump(),
        "recommendation": match.model_dump(mode="json"),
        "how_to_read": (
            "impact_score is blast-radius evidence; skip_threshold rises when sources "
            "are missing. A skip is safe only if score is below threshold and reasons "
            "show no coverage, dependency, locale, or failure link."
        ),
    }


@app.post("/v1/feedback")
def feedback(
    body: FeedbackBody,
    x_actor_id: str = Header(default="demo-user"),
    x_org_id: str = Header(default="ministry_interior"),
    x_role: str = Header(default="qa_operator"),
    x_partner_org_id: Optional[str] = Header(default=None),
):
    actor = _actor(x_actor_id, x_org_id, x_role, x_partner_org_id)
    if actor.role == Role.AUDITOR:
        raise HTTPException(status_code=403, detail="Auditors cannot submit execution feedback.")

    passed = body.passed
    if passed is None:
        if body.actual_result:
            passed = body.actual_result.lower() in ("pass", "passed", "true", "ok", "success")
        else:
            passed = True

    payload = TelemetryPayload(
        change_id=body.change_id,
        test_id=body.test_id,
        passed=passed,
        duration_seconds=body.duration_seconds,
        related_files=body.related_files,
        timestamp=body.timestamp or datetime.now(timezone.utc),
        runner_env=body.runner_env,
        actor_id=actor.actor_id,
        org_id=actor.org_id,
    )

    failures_path = DATA_DIR / "failures.json"
    event, is_flaky = record_failure_telemetry(payload, failures_path)

    store = get_store()
    store.failures.append(event)

    record = body.model_dump(mode="json") | {
        "actor_id": actor.actor_id,
        "org_id": actor.org_id,
        "flaky": is_flaky,
        "failed": not passed,
    }
    _FEEDBACK.append(record)
    return {
        "accepted": True,
        "stored": len(_FEEDBACK),
        "flaky": is_flaky,
        "record": record,
    }


@app.get("/v1/changes")
def list_changes(
    x_org_id: str = Header(default="ministry_interior"),
):
    store = get_store()
    changes = [c.model_dump(mode="json") for c in store.changes.values() if c.org_id == x_org_id]
    return {"count": len(changes), "changes": changes}
