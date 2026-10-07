"""Change-impact scoring and skip decisions.

Safety contract
---------------
A test may be skipped only when every *available* evidence source agrees it is
outside the blast radius. Missing or delayed sources raise the skip threshold
so the selector fails closed (runs more tests) rather than skipping blindly.
"""

from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime

from impact_selector.access import can_execute_skips, policy_notes, visible_tests
from impact_selector.language import classify_change, locale_relevance
from impact_selector.models import (
    Actor,
    ChangeKind,
    CodeChange,
    Decision,
    SelectionRequest,
    SelectionResult,
    SourceHealth,
    SourceStatus,
    TestCase,
    TestRecommendation,
)
from impact_selector.store import DataStore

BASE_SKIP_THRESHOLD = 0.18
CRITICAL_KEEP_TAGS = {"smoke", "security", "shared-smoke"}


def expand_dependents(files: list[str], graph: dict[str, list[str]], depth: int = 4) -> set[str]:
    affected: set[str] = set(files)
    queue = deque(files)
    hops = {path: 0 for path in files}
    while queue:
        current = queue.popleft()
        if hops[current] >= depth:
            continue
        for dependent in graph.get(current, []):
            if dependent not in affected:
                affected.add(dependent)
                hops[dependent] = hops[current] + 1
                queue.append(dependent)
    return affected


def _path_tokens(path: str) -> set[str]:
    return {part.lower() for part in path.replace("\\", "/").split("/") if part}


def _pattern_overlap(test: TestCase, files: list[str]) -> list[str]:
    hits: list[str] = []
    name_tokens = set(test.name.lower().replace("-", "_").split("_"))
    tag_tokens = {tag.lower() for tag in test.tags}
    for path in files:
        tokens = _path_tokens(path)
        if name_tokens & tokens or tag_tokens & tokens:
            hits.append(path)
    return hits


def _failure_signal(test_id: str, files: set[str], store: DataStore) -> tuple[float, bool, int]:
    events = [event for event in store.failures if event.test_id == test_id]
    if not events:
        return 0.0, False, 0
    related = 0
    flakes = 0
    for event in events:
        if event.flaky:
            flakes += 1
        if event.failed and (not event.related_files or files.intersection(event.related_files)):
            related += 1
    related_rate = related / max(len(events), 1)
    flaky = flakes / max(len(events), 1) >= 0.25
    return min(related_rate, 1.0), flaky, len(events)


def skip_threshold_for(health: SourceHealth) -> float:
    """Incomplete evidence makes skipping harder."""
    threshold = BASE_SKIP_THRESHOLD
    if health.coverage == SourceStatus.MISSING:
        threshold += 0.12
    elif health.coverage == SourceStatus.DELAYED:
        threshold += 0.06
    if health.dependencies == SourceStatus.MISSING:
        threshold += 0.10
    elif health.dependencies == SourceStatus.DELAYED:
        threshold += 0.05
    if health.failure_history == SourceStatus.MISSING:
        threshold += 0.04
    elif health.failure_history == SourceStatus.DELAYED:
        threshold += 0.02
    if health.code_changes != SourceStatus.AVAILABLE:
        threshold = 1.0
    return round(min(threshold, 1.0), 3)


def _confidence(health: SourceHealth, evidence_count: int) -> float:
    base = health.completeness
    evidence_boost = min(evidence_count * 0.08, 0.25)
    return round(min(1.0, max(0.2, base * 0.85 + evidence_boost)), 3)


def select_tests(
    request: SelectionRequest,
    store: DataStore,
    actor: Actor,
) -> SelectionResult:
    working = store.with_simulated_outages(request.simulate_missing)
    health = working.health.model_copy(deep=True)

    change = _resolve_change(request, working, health)
    files = [item.path for item in change.files]
    kind, locales_touched, content_only = classify_change(change.files)

    affected = set(files)
    sources_global = ["code_changes"]
    if working.dependents:
        affected = expand_dependents(files, working.dependents)
        sources_global.append("dependencies")
    else:
        health.notes.append("Dependency map unavailable; using changed files plus coverage/patterns.")

    if not working.coverage:
        health.notes.append("Coverage unavailable; pattern matching and locale rules are the fallback.")
    if not working.failures:
        health.notes.append("Failure history unavailable; skip decisions ignore flake/correlation signals.")

    catalog = visible_tests(actor, working.tests_for_org(change.org_id))
    threshold = skip_threshold_for(health)

    recommendations: list[TestRecommendation] = []
    for test in catalog:
        rec = _score_test(
            test=test,
            changed_files=files,
            affected_files=affected,
            locales_touched=locales_touched,
            content_only=content_only,
            store=working,
            health=health,
            threshold=threshold,
            sources_global=sources_global,
        )
        recommendations.append(rec)

    recommendations.sort(key=lambda item: (-item.impact_score, item.name))

    if health.code_changes == SourceStatus.MISSING and not request.files:
        for rec in recommendations:
            rec.decision = Decision.MUST_RUN
            rec.skip_rationale = None
            rec.reasons = [
                "Code change payload is unavailable, so every visible test is retained (fail closed)."
            ]
            rec.impact_score = 1.0
            rec.confidence = _confidence(health, 0)

    full = sum(test.estimated_seconds for test in catalog)
    selected = [item for item in recommendations if item.decision != Decision.SKIP]
    selected_seconds = sum(item.estimated_seconds for item in selected)

    safety = [
        "Skip is allowed only when impact is below the raised threshold from available evidence.",
        "Smoke, security, and critical tests are never skipped.",
        "Auth/payment tests are kept when those modules are in the blast radius.",
        "Locale-only content changes may skip other-language UI tests, not shared services.",
    ]
    if not can_execute_skips(actor):
        safety.append(
            f"Role {actor.role.value} cannot apply skips in CI; recommendations are advisory."
        )

    return SelectionResult(
        change_id=change.change_id,
        org_id=change.org_id,
        change_kind=kind,
        locales_touched=locales_touched,
        source_health=health,
        skip_threshold=threshold,
        recommendations=recommendations,
        suite_seconds_full=round(full, 2),
        suite_seconds_selected=round(selected_seconds, 2),
        tests_total=len(catalog),
        tests_to_run=len(selected),
        tests_skipped=len(recommendations) - len(selected),
        safety_notes=safety,
        policy=policy_notes(actor),
    )


def _resolve_change(
    request: SelectionRequest,
    store: DataStore,
    health: SourceHealth,
) -> CodeChange:
    if request.files:
        return CodeChange(
            change_id=request.change_id or f"adhoc-{uuid.uuid4().hex[:8]}",
            org_id=request.org_id,
            files=request.files,
            author="api",
            timestamp=datetime.now(),
            summary=request.summary or "Ad-hoc change submitted to selector",
        )
    if request.change_id and request.change_id in store.changes:
        return store.changes[request.change_id]
    if request.change_id and request.change_id not in store.changes:
        health.code_changes = SourceStatus.MISSING
        health.notes.append(f"Change {request.change_id} was not found in the change store.")
    if not store.changes:
        health.code_changes = SourceStatus.MISSING
        return CodeChange(
            change_id=request.change_id or "unknown",
            org_id=request.org_id,
            files=[],
            author="unknown",
            timestamp=datetime.now(),
            summary="No change payload available",
        )
    # Last known change for the org — degraded but still useful
    org_changes = [item for item in store.changes.values() if item.org_id == request.org_id]
    if org_changes:
        health.code_changes = SourceStatus.DELAYED
        health.notes.append("Using last known org change because no payload was supplied.")
        return sorted(org_changes, key=lambda item: item.timestamp, reverse=True)[0]
    health.code_changes = SourceStatus.MISSING
    return CodeChange(
        change_id="unknown",
        org_id=request.org_id,
        files=[],
        author="unknown",
        timestamp=datetime.now(),
        summary="No change payload available",
    )


def _score_test(
    test: TestCase,
    changed_files: list[str],
    affected_files: set[str],
    locales_touched: list[str],
    content_only: bool,
    store: DataStore,
    health: SourceHealth,
    threshold: float,
    sources_global: list[str],
) -> TestRecommendation:
    score = 0.0
    reasons: list[str] = []
    sources = list(sources_global)
    evidence = 0

    coverage = store.coverage.get(test.test_id)
    covered: set[str] = set(coverage.files) if coverage else set()
    direct = covered.intersection(changed_files)
    transitive = covered.intersection(affected_files - set(changed_files))

    if coverage:
        sources.append("coverage")
        if coverage.stale:
            reasons.append("This test's coverage row is stale; overlap still counted, confidence reduced.")
        if direct:
            score += 1.0
            evidence += 2
            reasons.append(f"Covers changed files: {', '.join(sorted(direct)[:4])}.")
        elif transitive:
            score += 0.62
            evidence += 1
            reasons.append(f"Covers dependent modules: {', '.join(sorted(transitive)[:4])}.")
    else:
        pattern_hits = _pattern_overlap(test, changed_files + sorted(affected_files))
        if pattern_hits:
            score += 0.45
            evidence += 1
            sources.append("path_patterns")
            reasons.append(f"Name/tag pattern overlap with {pattern_hits[0]}.")

    locale_flag = locale_relevance(test, locales_touched, content_only)
    if locales_touched:
        sources.append("locale_rules")
        if locale_flag == "keep":
            score += 0.55 if content_only else 0.2
            evidence += 1
            reasons.append(
                f"Locale overlap with changed content ({', '.join(locales_touched)})."
            )
        elif locale_flag == "skip_locale":
            reasons.append(
                "Language-specific test for a locale not touched by this content-only change."
            )
        elif locale_flag == "shared":
            reasons.append("Shared (non-locale) test; content changes do not auto-skip it.")

    related_rate, flaky, sample = _failure_signal(test.test_id, affected_files, store)
    if store.failures:
        sources.append("failure_history")
        if related_rate > 0:
            score += min(0.5, related_rate)
            evidence += 1
            reasons.append(
                f"Historical failures correlated with similar files (rate={related_rate:.2f}, n={sample})."
            )
        if flaky and score >= threshold:
            score += 0.12
            reasons.append("Flaky history on an impacted test — keep to avoid a silent miss.")
        elif flaky and score < threshold:
            reasons.append("Flaky history exists, but no impact evidence for this change.")

    guarded = False
    if test.critical or set(test.tags) & CRITICAL_KEEP_TAGS or test.layer.value in {"smoke", "security"}:
        guarded = True
    else:
        blob = " ".join(affected_files).lower()
        if "auth" in test.tags and any(token in blob for token in ("/auth/", "auth")):
            guarded = True
        if "payment" in test.tags and any(token in blob for token in ("/payments/", "payment")):
            guarded = True
    if guarded:
        score = max(score, 0.95)
        evidence += 1
        reasons.append("Protected suite member (critical/smoke/security or in-module auth/payment).")

    if not changed_files:
        score = 1.0
        reasons.append("No file list resolved; defaulting to must-run.")

    confidence = _confidence(health, evidence)
    decision = Decision.SKIP
    skip_rationale = None
    if score >= 0.55 or test.critical:
        decision = Decision.MUST_RUN
    elif score >= threshold:
        decision = Decision.RECOMMENDED
    else:
        decision = Decision.SKIP
        skip_rationale = (
            f"Impact score {score:.2f} is below skip threshold {threshold:.2f}. "
            "No coverage/dependency/locale/failure evidence ties this test to the change."
        )
        if not reasons:
            reasons.append("No blast-radius evidence from available data sources.")

    if (
        decision == Decision.SKIP
        and locale_flag != "skip_locale"
        and test.layer.value in {"integration", "e2e"}
        and (
            health.dependencies != SourceStatus.AVAILABLE
            or health.coverage != SourceStatus.AVAILABLE
        )
    ):
        decision = Decision.RECOMMENDED
        skip_rationale = None
        reasons.append(
            "Fail closed: integration/e2e tests are retained when coverage or the dependency map is incomplete."
        )

    # Fail closed when we have almost no evidence and data is incomplete
    if decision == Decision.SKIP and confidence < 0.35:
        decision = Decision.RECOMMENDED
        skip_rationale = None
        reasons.append("Confidence too low to skip; retaining test under graceful degradation.")

    return TestRecommendation(
        test_id=test.test_id,
        name=test.name,
        decision=decision,
        impact_score=round(score, 3),
        confidence=confidence,
        estimated_seconds=test.estimated_seconds,
        sources_used=sorted(set(sources)),
        reasons=reasons,
        skip_rationale=skip_rationale,
        layer=test.layer,
        locales=test.locales,
    )
