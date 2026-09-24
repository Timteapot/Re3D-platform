from __future__ import annotations

import argparse
import json
import sys

from backend.db.errors import SchedulerError
from backend.db.queue import JobQueue
from backend.db.runtime import (
    DatabaseSettings,
    SchedulerSettings,
    create_database_engine,
    create_session_factory,
)
from backend.re3d_adapter import (
    AdapterError,
    RealDryRunRunner,
    SimulatedCrash,
    SimulationRunner,
    TaskLayout,
)
from backend.re3d_adapter.settings import Re3DSettings, WorkerSettings
from backend.worker import QueuedSimulationWorker


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
    real_dry_run = subparsers.add_parser(
        "real-dry-run",
        description="Validate the installed Re3D baseline and preview all commands",
    )
    real_dry_run.add_argument("--job-id", required=True)
    real_dry_run.add_argument("--data-root")
    real_dry_run.add_argument("--worker-id")
    real_dry_run.add_argument("--re3d-root")
    real_dry_run.add_argument("--driver-python")
    queued = subparsers.add_parser(
        "run-queued-once",
        description="Claim and execute at most one simulated PostgreSQL job",
    )
    queued.add_argument("--data-root")
    queued.add_argument("--worker-id")
    queued.add_argument("--database-url")
    queued.add_argument("--resource-key")
    queued.add_argument("--lease-seconds", type=int)
    queued.add_argument("--heartbeat-seconds", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = WorkerSettings.from_environment(
            data_root=args.data_root,
            worker_id=args.worker_id,
        )
        if args.command == "simulate":
            layout = TaskLayout.from_data_root(settings.data_root, args.job_id)
            outcome = SimulationRunner(layout, worker_id=settings.worker_id).run(
                crash_after_branch=args.crash_after_branch
            )
            response = {
                "job_id": outcome.result["job_id"],
                "status": outcome.result["status"],
                "execution_mode": outcome.result["execution_mode"],
                "reused": outcome.reused,
                "result": str(outcome.result_path),
            }
        elif args.command == "real-dry-run":
            layout = TaskLayout.from_data_root(settings.data_root, args.job_id)
            re3d = Re3DSettings.from_environment(
                root=args.re3d_root,
                driver_python=args.driver_python,
            )
            outcome = RealDryRunRunner(
                layout,
                re3d_root=re3d.root,
                driver_python=re3d.driver_python,
            ).run()
            response = {
                "job_id": outcome.report["job_id"],
                "status": outcome.report["status"],
                "execution_mode": "real",
                "operation": outcome.report["operation"],
                "step_count": outcome.report["step_count"],
                "report": str(outcome.report_path),
            }
        else:
            database = DatabaseSettings.from_environment(args.database_url)
            scheduler = SchedulerSettings.from_environment(
                resource_key=args.resource_key,
                lease_seconds=args.lease_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
            )
            engine = create_database_engine(database)
            try:
                queue = JobQueue(create_session_factory(engine))
                outcome = QueuedSimulationWorker(
                    queue,
                    data_root=settings.data_root,
                    worker_id=settings.worker_id,
                    resource_key=scheduler.resource_key,
                    lease_seconds=scheduler.lease_seconds,
                    heartbeat_seconds=scheduler.heartbeat_seconds,
                ).run_once()
            finally:
                engine.dispose()
            response = {
                "claimed": outcome.claimed,
                "job_id": outcome.job_id,
                "status": outcome.status,
                "recovered": outcome.recovered,
            }
    except SimulatedCrash as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except AdapterError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (SchedulerError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            response,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
