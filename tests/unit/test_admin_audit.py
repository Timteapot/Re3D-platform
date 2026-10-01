from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.maintenance.main import main as maintenance_main
from backend.admin import AdminAuditService, AdminRoleService
from backend.db.models import (
    AdminRoleChangeEvent,
    AuthEvent,
    Base,
    SuccessRetentionRun,
    TaskActionEvent,
    User,
)


class AdminAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_url = f"sqlite:///{(self.root / 'admin.sqlite3').as_posix()}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.user_id = uuid.uuid4()
        self.second_user_id = uuid.uuid4()
        with self.sessions.begin() as session:
            session.add_all(
                (
                    _user(self.user_id, "audit-user", "audit@example.com"),
                    _user(
                        self.second_user_id,
                        "second-user",
                        "second@example.com",
                    ),
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def test_audit_pages_are_bounded_filtered_and_sanitized(self) -> None:
        now = datetime.now(timezone.utc)
        raw_identifier = "a" * 64
        raw_ip = "b" * 64
        raw_user_agent = "c" * 64
        job_id = uuid.uuid4()
        with self.sessions.begin() as session:
            session.add_all(
                (
                    AuthEvent(
                        id=uuid.uuid4(),
                        action="login",
                        outcome="failure",
                        user_id=self.user_id,
                        identifier_fingerprint=raw_identifier,
                        client_ip_fingerprint=raw_ip,
                        user_agent_sha256=raw_user_agent,
                        reason_code="INVALID_CREDENTIALS",
                        occurred_at=now,
                    ),
                    AuthEvent(
                        id=uuid.uuid4(),
                        action="login",
                        outcome="success",
                        user_id=self.user_id,
                        occurred_at=now,
                    ),
                    TaskActionEvent(
                        id=uuid.uuid4(),
                        action="job_submitted",
                        outcome="success",
                        user_id=self.user_id,
                        job_id=job_id,
                        execution_mode="real",
                        occurred_at=now,
                    ),
                    SuccessRetentionRun(
                        id=uuid.uuid4(),
                        trigger="scheduled",
                        mode="dry_run",
                        status="succeeded",
                        input_retention_days=30,
                        runtime_retention_days=30,
                        artifact_retention_days=30,
                        batch_size=50,
                        started_at=now,
                        finished_at=now,
                        report={
                            "mode": "dry_run",
                            "tiers": {"input": {"candidates": 0}},
                        },
                    ),
                )
            )

        audit = AdminAuditService(self.sessions)
        first_page = audit.list_auth_events(limit=1)
        self.assertEqual(len(first_page.items), 1)
        self.assertTrue(first_page.has_more)
        second_page = audit.list_auth_events(limit=1, offset=1)
        self.assertEqual(len(second_page.items), 1)

        failures = audit.list_auth_events(action="login", outcome="failure")
        self.assertEqual(len(failures.items), 1)
        event = failures.items[0]
        self.assertEqual(event.identifier_ref, raw_identifier[:12])
        self.assertEqual(event.client_ip_ref, raw_ip[:12])
        self.assertEqual(event.user_agent_ref, raw_user_agent[:12])
        self.assertNotIn(raw_identifier, repr(event))
        self.assertNotIn(raw_ip, repr(event))
        self.assertNotIn(raw_user_agent, repr(event))

        tasks = audit.list_task_events(job_id=job_id)
        self.assertEqual(tasks.items[0].execution_mode, "real")
        retention = audit.list_retention_runs(status="succeeded")
        self.assertEqual(retention.items[0].report["mode"], "dry_run")
        with self.assertRaisesRegex(ValueError, "limit"):
            audit.list_auth_events(limit=101)
        with self.assertRaisesRegex(ValueError, "offset"):
            audit.list_auth_events(offset=10_001)

    def test_role_changes_require_confirmation_and_preserve_an_admin(self) -> None:
        roles = AdminRoleService(self.sessions)
        with self.assertRaisesRegex(ValueError, "confirmation"):
            roles.set_role(
                user_id=self.user_id,
                role="admin",
                confirmed=False,
            )

        promoted = roles.set_role(
            user_id=self.user_id,
            role="admin",
            confirmed=True,
        )
        self.assertTrue(promoted.changed)
        self.assertIsNotNone(promoted.event_id)
        with self.assertRaisesRegex(ValueError, "last active administrator"):
            roles.set_role(
                user_id=self.user_id,
                role="user",
                confirmed=True,
            )

        roles.set_role(
            user_id=self.second_user_id,
            role="admin",
            confirmed=True,
        )
        demoted = roles.set_role(
            user_id=self.user_id,
            role="user",
            confirmed=True,
        )
        self.assertTrue(demoted.changed)
        role_events = AdminAuditService(self.sessions).list_role_changes(
            target_user_id=self.user_id
        )
        self.assertEqual(len(role_events.items), 2)
        with self.sessions() as session:
            self.assertEqual(session.get(User, self.user_id).role, "user")
            self.assertEqual(
                session.query(AdminRoleChangeEvent).count(),
                3,
            )

    def test_role_change_cli_outputs_only_internal_identifiers(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = maintenance_main(
                [
                    "set-user-role",
                    "--database-url",
                    self.database_url,
                    "--user-id",
                    str(self.user_id),
                    "--role",
                    "admin",
                    "--confirm-role-change",
                ]
            )
        self.assertEqual(exit_code, 0)
        response = json.loads(stdout.getvalue())
        self.assertTrue(response["changed"])
        self.assertEqual(response["new_role"], "admin")
        serialized = json.dumps(response)
        self.assertNotIn("audit@example.com", serialized)
        self.assertEqual(stderr.getvalue(), "")


def _user(user_id: uuid.UUID, username: str, email: str) -> User:
    return User(
        id=user_id,
        username=username,
        email=email,
        password_hash="not-used-by-admin-audit-tests",
        role="user",
        is_active=True,
        email_verified=True,
    )


if __name__ == "__main__":
    unittest.main()
