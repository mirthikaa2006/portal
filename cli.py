"""CLI for local selection without standing up the API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from impact_selector.algorithm import select_tests
from impact_selector.models import Actor, Role, SelectionRequest
from impact_selector.store import DataStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Mithuna change-impact test selector")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--change-id", required=True)
    parser.add_argument("--org-id", default="ministry_interior")
    parser.add_argument("--role", default="qa_operator")
    parser.add_argument("--actor-id", default="cli-user")
    parser.add_argument("--missing", nargs="*", default=[])
    args = parser.parse_args()

    store = DataStore(Path(args.data_dir))
    actor = Actor(actor_id=args.actor_id, org_id=args.org_id, role=Role(args.role))
    result = select_tests(
        SelectionRequest(change_id=args.change_id, org_id=args.org_id, simulate_missing=args.missing),
        store,
        actor,
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
