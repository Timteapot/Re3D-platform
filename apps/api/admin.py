from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Generic, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict

from backend.admin import AdminAuditService
from backend.auth import UserIdentity


CurrentUserDependency = Callable[..., UserIdentity]
PageItemT = TypeVar("PageItemT")


class AuditPageResponse(BaseModel, Generic[PageItemT]):
    items: list[PageItemT]
    limit: int
    offset: int
    has_more: bool


class AuthAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    outcome: str
    user_id: uuid.UUID | None
    identifier_ref: str | None
    client_ip_ref: str | None
    user_agent_ref: str | None
    reason_code: str | None
    occurred_at: datetime


class TaskAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    outcome: str
    user_id: uuid.UUID | None
    job_id: uuid.UUID
    execution_mode: str
    reason_code: str | None
    occurred_at: datetime


class RetentionAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class RoleChangeAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_kind: str
    actor_user_id: uuid.UUID | None
    target_user_id: uuid.UUID
    previous_role: str
    new_role: str
    reason_code: str
    occurred_at: datetime


def create_admin_audit_router(
    service: AdminAuditService,
    current_user: CurrentUserDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/audit", tags=["administration"])

    def administrator(
        user: UserIdentity = Depends(current_user),
    ) -> UserIdentity:
        if user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="administrator role is required",
            )
        return user

    @router.get(
        "/auth-events",
        response_model=AuditPageResponse[AuthAuditResponse],
    )
    def auth_events(
        response: Response,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10_000),
        action: Literal[
            "register",
            "login",
            "refresh",
            "logout",
            "email_verification",
            "password_reset",
        ]
        | None = None,
        outcome: Literal["success", "failure", "blocked", "reuse"]
        | None = None,
        user_id: uuid.UUID | None = None,
        _administrator: UserIdentity = Depends(administrator),
    ) -> AuditPageResponse[AuthAuditResponse]:
        page = service.list_auth_events(
            limit=limit,
            offset=offset,
            action=action,
            outcome=outcome,
            user_id=user_id,
        )
        _set_no_store(response)
        return AuditPageResponse[AuthAuditResponse](
            items=[AuthAuditResponse.model_validate(item) for item in page.items],
            limit=page.limit,
            offset=page.offset,
            has_more=page.has_more,
        )

    @router.get(
        "/task-events",
        response_model=AuditPageResponse[TaskAuditResponse],
    )
    def task_events(
        response: Response,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10_000),
        action: Literal["job_submitted", "cancel_requested"] | None = None,
        outcome: Literal["success", "blocked", "reuse"] | None = None,
        user_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        _administrator: UserIdentity = Depends(administrator),
    ) -> AuditPageResponse[TaskAuditResponse]:
        page = service.list_task_events(
            limit=limit,
            offset=offset,
            action=action,
            outcome=outcome,
            user_id=user_id,
            job_id=job_id,
        )
        _set_no_store(response)
        return AuditPageResponse[TaskAuditResponse](
            items=[TaskAuditResponse.model_validate(item) for item in page.items],
            limit=page.limit,
            offset=page.offset,
            has_more=page.has_more,
        )

    @router.get(
        "/retention-runs",
        response_model=AuditPageResponse[RetentionAuditResponse],
    )
    def retention_runs(
        response: Response,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10_000),
        trigger: Literal["manual", "scheduled"] | None = None,
        mode: Literal["dry_run", "execute"] | None = None,
        run_status: Literal["running", "succeeded", "failed"]
        | None = Query(None, alias="status"),
        _administrator: UserIdentity = Depends(administrator),
    ) -> AuditPageResponse[RetentionAuditResponse]:
        page = service.list_retention_runs(
            limit=limit,
            offset=offset,
            trigger=trigger,
            mode=mode,
            status=run_status,
        )
        _set_no_store(response)
        return AuditPageResponse[RetentionAuditResponse](
            items=[
                RetentionAuditResponse.model_validate(item) for item in page.items
            ],
            limit=page.limit,
            offset=page.offset,
            has_more=page.has_more,
        )

    @router.get(
        "/role-changes",
        response_model=AuditPageResponse[RoleChangeAuditResponse],
    )
    def role_changes(
        response: Response,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10_000),
        target_user_id: uuid.UUID | None = None,
        _administrator: UserIdentity = Depends(administrator),
    ) -> AuditPageResponse[RoleChangeAuditResponse]:
        page = service.list_role_changes(
            limit=limit,
            offset=offset,
            target_user_id=target_user_id,
        )
        _set_no_store(response)
        return AuditPageResponse[RoleChangeAuditResponse](
            items=[
                RoleChangeAuditResponse.model_validate(item) for item in page.items
            ],
            limit=page.limit,
            offset=page.offset,
            has_more=page.has_more,
        )

    return router


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
