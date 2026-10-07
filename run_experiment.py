"""Reproducible comparison: run-all vs file-pattern vs impact selector."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from impact_selector.algorithm import select_tests
from impact_selector.baselines import file_pattern_baseline, run_all
from impact_selector.models import Actor, Role, SelectionRequest
from impact_selector.store import DataStore

DATA = ROOT / "data"
ORG = "ministry_interior"


def evaluate_keep(keep: set[str], affected: set[str], tests_by_id: dict, full_seconds: float) -> dict:
    selected_seconds = sum(tests_by_id[tid].estimated_seconds for tid in keep if tid in tests_by_id)
    missed = sorted(affected - keep)
    extra = sorted(keep - affected)
    return {
        "tests_run": len(keep),
        "suite_seconds": round(selected_seconds, 2),
        "time_saved_seconds": round(full_seconds - selected_seconds, 2),
        "time_saved_pct": round(100 * (1 - selected_seconds / full_seconds), 2) if full_seconds else 0,
        "affected": len(affected),
        "affected_caught": len(affected & keep),
        "missed_affected": missed,
        "false_positives": len(extra),
        "recall": round(len(affected & keep) / len(affected), 3) if affected else 1.0,
    }


def run_for_change(store: DataStore, change_id: str, ground: dict, actor: Actor, missing: list[str] | None = None):
    change = store.changes[change_id]
    tests = store.tests_for_org(ORG)
    tests_by_id = {t.test_id: t for t in tests}
    full = run_all(tests)
    full_seconds = sum(t.estimated_seconds for t in tests)
    affected = set(ground[change_id])
    pattern_keep = file_pattern_baseline(tests, change.files)

    result = select_tests(
        SelectionRequest(change_id=change_id, org_id=ORG, simulate_missing=missing or []),
        store,
        actor,
    )
    selector_keep = {r.test_id for r in result.recommendations if r.decision.value != "skip"}
    return {
        "change_id": change_id,
        "summary": change.summary,
        "files": [f.path for f in change.files],
        "kind": result.change_kind.value,
        "locales": result.locales_touched,
        "missing": missing or [],
        "skip_threshold": result.skip_threshold,
        "source_completeness": result.source_health.completeness,
        "run_all": evaluate_keep(full, affected, tests_by_id, full_seconds),
        "pattern": evaluate_keep(pattern_keep, affected, tests_by_id, full_seconds),
        "selector": evaluate_keep(selector_keep, affected, tests_by_id, full_seconds),
        "skipped_examples": [
            {
                "test_id": r.test_id,
                "name": r.name,
                "rationale": r.skip_rationale,
                "reasons": r.reasons,
                "sources": r.sources_used,
            }
            for r in result.recommendations
            if r.decision.value == "skip"
        ][:5],
    }


def main() -> dict:
    store = DataStore(DATA)
    ground = json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))
    actor = Actor(actor_id="experiment", org_id=ORG, role=Role.QA_OPERATOR)

    rows = [run_for_change(store, cid, ground, actor) for cid in store.changes if store.changes[cid].org_id == ORG]

    def avg(key_path):
        values = []
        for row in rows:
            cur = row
            for key in key_path:
                cur = cur[key]
            values.append(cur)
        return round(sum(values) / len(values), 3)

    error_rows = [row for row in rows if row["selector"]["missed_affected"]]
    degradation = []
    sample_id = "chg_auth_session"
    for missing in ([], ["coverage"], ["dependencies"], ["failure_history"], ["coverage", "dependencies"]):
        degradation.append(run_for_change(store, sample_id, ground, actor, missing=missing))

    report = {
        "targets": {
            "baseline": "Run entire org suite (run-all)",
            "pattern_baseline": "Keep tests whose names/tags match changed path tokens, plus smoke/security",
            "suite_time_goal_pct_reduction": 35,
            "coverage_goal": "Recall 1.0 on ground-truth affected tests (never skip a test that would catch the change)",
        },
        "averages": {
            "run_all_seconds": avg(["run_all", "suite_seconds"]),
            "pattern_seconds": avg(["pattern", "suite_seconds"]),
            "selector_seconds": avg(["selector", "suite_seconds"]),
            "run_all_recall": avg(["run_all", "recall"]),
            "pattern_recall": avg(["pattern", "recall"]),
            "selector_recall": avg(["selector", "recall"]),
            "selector_time_saved_pct": avg(["selector", "time_saved_pct"]),
            "pattern_time_saved_pct": avg(["pattern", "time_saved_pct"]),
        },
        "changes": rows,
        "error_analysis": {
            "selector_miss_count": len(error_rows),
            "misses": error_rows,
            "pattern_misses": [
                {"change_id": r["change_id"], "missed": r["pattern"]["missed_affected"]}
                for r in rows
                if r["pattern"]["missed_affected"]
            ],
        },
        "degradation": degradation,
    }
    out = ROOT / "experiments" / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["averages"], indent=2))
    print("selector misses:", report["error_analysis"]["selector_miss_count"])
    print("wrote", out)
    return report


if __name__ == "__main__":
    main()
