from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import (
    AdminRoleChangeEvent,
    AuthEvent,
    SuccessRetentionRun,
    TaskActionEvent,
)


RecordT = TypeVar("RecordT")
MAX_AUDIT_PAGE_SIZE = 100
MAX_AUDIT_OFFSET = 10_000


@dataclass(frozen=True)
class AuditPage(Generic[RecordT]):
    items: tuple[RecordT, ...]
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True)
class AuthAuditRecord:
    id: uuid.UUID
    action: str
    outcome: str
    user_id: uuid.UUID | None
    identifier_ref: str | None
    client_ip_ref: str | None
    user_agent_ref: str | None
    reason_code: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class TaskAuditRecord:
    id: uuid.UUID
    action: str
    outcome: str
    user_id: uuid.UUID | None
    job_id: uuid.UUID
    execution_mode: str
    reason_code: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class RetentionAuditRecord:
    id: uuid.UUID
    trigger: str
    mode: str
    status: str
    input_retention_days: int | None
    runtime_retention_days: int | None
    artifact_retention_days: int | None
    batch_size: int
    started_at: datetime
    finished_at: datetime | None
    report: dict[str, object] | None
    error_code: str | None


@dataclass(frozen=True)
class RoleChangeAuditRecord:
    id: uuid.UUID
    actor_kind: str
    actor_user_id: uuid.UUID | None
    target_user_id: uuid.UUID
    previous_role: str
    new_role: str
    reason_code: str
    occurred_at: datetime


class AdminAuditService:
    """Read bounded, already-sanitized operational audit projections."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def list_auth_events(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        outcome: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> AuditPage[AuthAuditRecord]:
        _validate_pagination(limit, offset)
        statement = select(AuthEvent)
        if action is not None:
            statement = statement.where(AuthEvent.action == action)
        if outcome is not None:
            statement = statement.where(AuthEvent.outcome == outcome)
        if user_id is not None:
            statement = statement.where(AuthEvent.user_id == user_id)
        rows = self._page(
            statement.order_by(AuthEvent.occurred_at.desc(), AuthEvent.id.desc()),
            limit=limit,
            offset=offset,
        )
        return AuditPage(
            items=tuple(
                AuthAuditRecord(
                    id=row.id,
                    action=row.action,
                    outcome=row.outcome,
                    user_id=row.user_id,
                    identifier_ref=_fingerprint_reference(
                        row.identifier_fingerprint
                    ),
                    client_ip_ref=_fingerprint_reference(
                        row.client_ip_fingerprint
                    ),
                    user_agent_ref=_fingerprint_reference(row.user_agent_sha256),
                    reason_code=row.reason_code,
                    occurred_at=_as_utc(row.occurred_at),
                )
                for row in rows[:limit]
            ),
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def list_task_events(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        outcome: str | None = None,
        user_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
    ) -> AuditPage[TaskAuditRecord]:
        _validate_pagination(limit, offset)
        statement = select(TaskActionEvent)
        if action is not None:
            statement = statement.where(TaskActionEvent.action == action)
        if outcome is not None:
            statement = statement.where(TaskActionEvent.outcome == outcome)
        if user_id is not None:
            statement = statement.where(TaskActionEvent.user_id == user_id)
        if job_id is not None:
            statement = statement.where(TaskActionEvent.job_id == job_id)
        rows = self._page(
            statement.order_by(
                TaskActionEvent.occurred_at.desc(),
                TaskActionEvent.id.desc(),
            ),
            limit=limit,
            offset=offset,
        )
        return AuditPage(
            items=tuple(
                TaskAuditRecord(
                    id=row.id,
                    action=row.action,
                    outcome=row.outcome,
                    user_id=row.user_id,
                    job_id=row.job_id,
                    execution_mode=row.execution_mode,
                    reason_code=row.reason_code,
                    occurred_at=_as_utc(row.occurred_at),
                )
                for row in rows[:limit]
            ),
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def list_retention_runs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        trigger: str | None = None,
        mode: str | None = None,
        status: str | None = None,
    ) -> AuditPage[RetentionAuditRecord]:
        _validate_pagination(limit, offset)
        statement = select(SuccessRetentionRun)
        if trigger is not None:
            statement = statement.where(SuccessRetentionRun.trigger == trigger)
        if mode is not None:
            statement = statement.where(SuccessRetentionRun.mode == mode)
        if status is not None:
            statement = statement.where(SuccessRetentionRun.status == status)
        rows = self._page(
            statement.order_by(
                SuccessRetentionRun.started_at.desc(),
                SuccessRetentionRun.id.desc(),
            ),
            limit=limit,
            offset=offset,
        )
        return AuditPage(
            items=tuple(
                RetentionAuditRecord(
                    id=row.id,
                    trigger=row.trigger,
                    mode=row.mode,
                    status=row.status,
                    input_retention_days=row.input_retention_days,
                    runtime_retention_days=row.runtime_retention_days,
                    artifact_retention_days=row.artifact_retention_days,
                    batch_size=row.batch_size,
                    started_at=_as_utc(row.started_at),
                    finished_at=(
                        _as_utc(row.finished_at)
                        if row.finished_at is not None
                        else None
                    ),
                    report=row.report,
                    error_code=row.error_code,
                )
                for row in rows[:limit]
            ),
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def list_role_changes(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        target_user_id: uuid.UUID | None = None,
    ) -> AuditPage[RoleChangeAuditRecord]:
        _validate_pagination(limit, offset)
        statement = select(AdminRoleChangeEvent)
        if target_user_id is not None:
            statement = statement.where(
                AdminRoleChangeEvent.target_user_id == target_user_id
            )
        rows = self._page(
            statement.order_by(
                AdminRoleChangeEvent.occurred_at.desc(),
                AdminRoleChangeEvent.id.desc(),
            ),
            limit=limit,
            offset=offset,
        )
        return AuditPage(
            items=tuple(
                RoleChangeAuditRecord(
                    id=row.id,
                    actor_kind=row.actor_kind,
                    actor_user_id=row.actor_user_id,
                    target_user_id=row.target_user_id,
                    previous_role=row.previous_role,
                    new_role=row.new_role,
                    reason_code=row.reason_code,
                    occurred_at=_as_utc(row.occurred_at),
                )
                for row in rows[:limit]
            ),
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def _page(
        self,
        statement: Select[tuple[RecordT]],
        *,
        limit: int,
        offset: int,
    ) -> list[RecordT]:
        with self.sessions() as session:
            return list(
                session.execute(
                    statement.offset(offset).limit(limit + 1)
                ).scalars()
            )


def _validate_pagination(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_AUDIT_PAGE_SIZE:
        raise ValueError("audit limit must be between 1 and 100")
    if not 0 <= offset <= MAX_AUDIT_OFFSET:
        raise ValueError("audit offset must be between 0 and 10000")


def _fingerprint_reference(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:12]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
