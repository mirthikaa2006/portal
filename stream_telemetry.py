#!/usr/bin/env python3
"""CI/CD Telemetry Streaming Script.

Collects test execution results and CI runner context from GitHub Actions
or GitLab CI and streams telemetry to the Mithuna impact-selector engine
via POST /v1/feedback with automatic retry and local spooling.

Usage:
    python scripts/stream_telemetry.py \
        --change-id chg_auth_session \
        --test-id t_unit_session \
        --passed true \
        --duration 1.25 \
        --files src/auth/session.py \
        --endpoint http://localhost:8000/v1/feedback

    # Flush spooled events:
    python scripts/stream_telemetry.py --flush --endpoint http://localhost:8000/v1/feedback
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is in sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from impact_selector.telemetry import (
    RunnerEnvironment,
    TelemetryPayload,
    flush_telemetry_spool,
    send_telemetry_with_retry,
)

DEFAULT_SPOOL = Path(".telemetry_spool.jsonl")


def detect_runner_environment() -> RunnerEnvironment:
    """Inspect environment variables to detect CI platform and job metadata."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return RunnerEnvironment(
            ci_provider="github-actions",
            runner_id=os.environ.get("RUNNER_NAME", "github-runner"),
            git_sha=os.environ.get("GITHUB_SHA", "unknown-sha"),
            git_branch=os.environ.get("GITHUB_REF_NAME"),
            run_id=os.environ.get("GITHUB_RUN_ID"),
            workflow_name=os.environ.get("GITHUB_WORKFLOW"),
            run_attempt=int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
        )
    if os.environ.get("GITLAB_CI") == "true":
        return RunnerEnvironment(
            ci_provider="gitlab-ci",
            runner_id=os.environ.get("CI_RUNNER_DESCRIPTION", "gitlab-runner"),
            git_sha=os.environ.get("CI_COMMIT_SHA", "unknown-sha"),
            git_branch=os.environ.get("CI_COMMIT_REF_NAME"),
            run_id=os.environ.get("CI_JOB_ID"),
            workflow_name=os.environ.get("CI_JOB_NAME"),
            run_attempt=1,
        )
    return RunnerEnvironment(
        ci_provider="local",
        runner_id=os.environ.get("HOSTNAME", os.environ.get("COMPUTERNAME", "local-dev")),
        git_sha=os.environ.get("GIT_COMMIT", "dev"),
        git_branch=os.environ.get("GIT_BRANCH", "main"),
        run_attempt=1,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream CI/CD telemetry to Mithuna selector")
    parser.add_argument("--endpoint", default="http://localhost:8000/v1/feedback", help="Feedback endpoint URL")
    parser.add_argument("--change-id", help="Change identifier under test")
    parser.add_argument("--test-id", help="Test identifier")
    parser.add_argument("--passed", type=lambda s: s.lower() in ("true", "1", "yes", "pass"), help="Did test pass?")
    parser.add_argument("--duration", type=float, default=0.0, help="Test execution duration in seconds")
    parser.add_argument("--files", nargs="*", default=[], help="Files touched or related to test")
    parser.add_argument("--actor", default="ci-bot", help="Actor ID for request header")
    parser.add_argument("--org", default="ministry_interior", help="Organization ID")
    parser.add_argument("--spool-file", default=str(DEFAULT_SPOOL), help="Offline spool file path")
    parser.add_argument("--flush", action="store_true", help="Flush offline spool file")

    args = parser.parse_args()

    if args.flush:
        print(f"Flushing spooled telemetry from {args.spool_file} to {args.endpoint}...")
        count = flush_telemetry_spool(args.spool_file, args.endpoint)
        print(f"Successfully flushed {count} spooled events.")
        sys.exit(0)

    if not args.change_id or not args.test_id:
        print("Error: --change-id and --test-id are required when not using --flush", file=sys.stderr)
        sys.exit(1)

    runner_env = detect_runner_environment()
    payload = TelemetryPayload(
        change_id=args.change_id,
        test_id=args.test_id,
        passed=args.passed if args.passed is not None else True,
        duration_seconds=args.duration,
        related_files=args.files,
        timestamp=datetime.now(timezone.utc),
        runner_env=runner_env,
        actor_id=args.actor,
        org_id=args.org,
    )

    print(f"Streaming telemetry for {args.test_id} (passed={payload.passed}) to {args.endpoint}...")
    res = send_telemetry_with_retry(
        payload=payload,
        endpoint_url=args.endpoint,
        max_retries=3,
        base_delay_seconds=0.5,
        spool_path=args.spool_file,
    )

    if res.success:
        print(f"Successfully streamed telemetry (HTTP {res.status_code}) in {res.attempts} attempt(s).")
    elif res.spooled:
        print(f"Server unreachable: Telemetry safely spooled to {args.spool_file} (Build preserved).")
    else:
        print(f"Warning: Telemetry transmission failed: {res.error_message}", file=sys.stderr)


if __name__ == "__main__":
    main()
