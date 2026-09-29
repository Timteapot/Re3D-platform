from __future__ import annotations

import argparse
import json
import sys

from backend.auth import AuthMaintenanceService, AuthMaintenanceSettings
from backend.db.runtime import (
    DatabaseSettings,
    create_database_engine,
    create_session_factory,
)
from .readiness import check_local_readiness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Re3D Platform maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    cleanup = subparsers.add_parser(
        "cleanup-auth-security",
        description="Delete expired authentication audit and throttle records",
    )
    cleanup.add_argument("--database-url")
    cleanup.add_argument("--event-retention-days", type=int)
    cleanup.add_argument("--action-token-retention-days", type=int)
    cleanup.add_argument("--throttle-retention-days", type=int)
    cleanup.add_argument("--limit", type=int)
    subparsers.add_parser(
        "check-local-readiness",
        description="Validate local development configuration without exposing secrets",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check-local-readiness":
            response = check_local_readiness()
        elif args.command == "cleanup-auth-security":
            database = DatabaseSettings.from_environment(args.database_url)
            engine = create_database_engine(database)
            try:
                maintenance = AuthMaintenanceService(
                    create_session_factory(engine),
                    AuthMaintenanceSettings.from_environment(),
                )
                response = {
                    "operation": "cleanup-auth-security",
                    **maintenance.cleanup(
                        event_retention_days=args.event_retention_days,
                        action_token_retention_days=args.action_token_retention_days,
                        throttle_retention_days=args.throttle_retention_days,
                        limit=args.limit,
                    ),
                }
            finally:
                engine.dispose()
        else:
            raise ValueError(f"unsupported maintenance command: {args.command}")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
