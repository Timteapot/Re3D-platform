from __future__ import annotations

import argparse
import json
import sys

from backend.re3d_adapter import AdapterError, SimulatedCrash, SimulationRunner, TaskLayout
from backend.re3d_adapter.settings import WorkerSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Re3D Platform development worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    simulate = subparsers.add_parser(
        "simulate", description="Run the contract-valid three-branch simulator"
    )
    simulate.add_argument("--job-id", required=True)
    simulate.add_argument("--data-root")
    simulate.add_argument("--worker-id")
    simulate.add_argument(
        "--crash-after-branch",
        choices=["A-v4", "B-v2", "C"],
        help="Development-only recovery test hook",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = WorkerSettings.from_environment(
            data_root=args.data_root,
            worker_id=args.worker_id,
        )
        layout = TaskLayout.from_data_root(settings.data_root, args.job_id)
        outcome = SimulationRunner(layout, worker_id=settings.worker_id).run(
            crash_after_branch=args.crash_after_branch
        )
    except SimulatedCrash as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except AdapterError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "job_id": outcome.result["job_id"],
                "status": outcome.result["status"],
                "execution_mode": outcome.result["execution_mode"],
                "reused": outcome.reused,
                "result": str(outcome.result_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
