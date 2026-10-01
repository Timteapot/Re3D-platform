from __future__ import annotations

import argparse
import json
import sys
import uuid

from backend.admin import AdminRoleService
from backend.auth import AuthMaintenanceService, AuthMaintenanceSettings
from backend.db.runtime import (
    DatabaseSettings,
    create_database_engine,
    create_session_factory,
)
from backend.re3d_adapter import AdapterError
from .readiness import check_local_readiness, check_production_readiness


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
    production = subparsers.add_parser(
        "check-production-readiness",
        description="Validate production configuration without exposing secrets",
    )
    production.add_argument(
        "--component",
        choices=["api", "worker", "all"],
        default="all",
        help="Process role to validate (default: all)",
    )
    role_change = subparsers.add_parser(
        "set-user-role",
        description="Set one user role with explicit confirmation and database audit",
    )
    role_change.add_argument("--database-url")
    role_change.add_argument("--user-id", required=True, type=uuid.UUID)
    role_change.add_argument(
        "--role",
        required=True,
        choices=["user", "admin"],
    )
    role_change.add_argument(
        "--confirm-role-change",
        action="store_true",
        help="Required acknowledgement for a persisted role change",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check-local-readiness":
            response = check_local_readiness()
        elif args.command == "check-production-readiness":
            response = check_production_readiness(component=args.component)
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
        elif args.command == "set-user-role":
            database = DatabaseSettings.from_environment(args.database_url)
            engine = create_database_engine(database)
            try:
                result = AdminRoleService(
                    create_session_factory(engine)
                ).set_role(
                    user_id=args.user_id,
                    role=args.role,
                    confirmed=args.confirm_role_change,
                )
                response = {
                    "operation": "set-user-role",
                    "user_id": str(result.user_id),
                    "previous_role": result.previous_role,
                    "new_role": result.new_role,
                    "changed": result.changed,
                    "event_id": (
                        str(result.event_id)
                        if result.event_id is not None
                        else None
                    ),
                }
            finally:
                engine.dispose()
        else:
            raise ValueError(f"unsupported maintenance command: {args.command}")
    except (AdapterError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
