from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from backend.db.errors import JobNotFoundError, QueueConflictError
from backend.db.queue import JobQueue
from backend.db.runtime import (
    DatabaseSettings,
    create_database_engine,
    create_session_factory,
)
from backend.db.state_machine import JobStatus
from backend.jobs.development import DevelopmentJobService


DEVELOPMENT_ENVIRONMENTS = {"development", "test"}


@dataclass(frozen=True)
class AppServices:
    engine: Engine
    queue: JobQueue
    development_jobs: DevelopmentJobService


class SimulatedJobCreate(BaseModel):
    user_id: uuid.UUID
    image_count: int = Field(default=3, ge=3, le=150)
    idempotency_key: str = Field(min_length=8, max_length=128)


class JobOwnerRequest(BaseModel):
    user_id: uuid.UUID


class JobResponse(BaseModel):
    job_id: uuid.UUID
    user_id: uuid.UUID
    status: JobStatus
    execution_mode: str
    attempt: int
    progress: int
    cancel_requested: bool
    error_code: str | None
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    version: int
    reused: bool = False

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        reused: bool = False,
    ) -> "JobResponse":
        return cls(
            job_id=snapshot["id"],
            user_id=snapshot["user_id"],
            status=snapshot["status"],
            execution_mode=snapshot["execution_mode"],
            attempt=snapshot["attempt"],
            progress=snapshot["progress"],
            cancel_requested=snapshot["cancel_requested"],
            error_code=snapshot["error_code"],
            created_at=snapshot["created_at"],
            queued_at=snapshot["queued_at"],
            started_at=snapshot["started_at"],
            finished_at=snapshot["finished_at"],
            version=snapshot["version"],
            reused=reused,
        )


def build_services() -> AppServices:
    database = DatabaseSettings.from_environment()
    configured_data_root = os.environ.get("RE3D_DATA_ROOT")
    if not configured_data_root:
        raise ValueError("RE3D_DATA_ROOT is required")
    engine = create_database_engine(database)
    queue = JobQueue(create_session_factory(engine))
    development_jobs = DevelopmentJobService(
        queue,
        data_root=Path(configured_data_root),
    )
    return AppServices(engine, queue, development_jobs)


def create_app(
    *,
    services: AppServices | None = None,
    app_env: str | None = None,
) -> FastAPI:
    environment = (app_env or os.environ.get("APP_ENV") or "production").lower()
    app = FastAPI(title="Re3D Platform API", version="0.1.0")

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, Any]:
        return {
            "status": "ok",
            "environment": environment,
            "development_routes_enabled": environment in DEVELOPMENT_ENVIRONMENTS,
        }

    if environment not in DEVELOPMENT_ENVIRONMENTS:
        return app
    resolved_services = services or build_services()
    app.state.services = resolved_services

    @app.post(
        "/api/v1/development/simulated-jobs",
        response_model=JobResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["development"],
    )
    def create_simulated_job(
        payload: SimulatedJobCreate,
        response: Response,
    ) -> JobResponse:
        try:
            created = resolved_services.development_jobs.create_simulated_job(
                user_id=payload.user_id,
                image_count=payload.image_count,
                idempotency_token=payload.idempotency_key,
            )
        except QueueConflictError as exc:
            raise HTTPException(status_code=409, detail="job identity conflict") from exc
        if created.reused:
            response.status_code = status.HTTP_200_OK
        return JobResponse.from_snapshot(
            created.snapshot,
            reused=created.reused,
        )

    @app.get(
        "/api/v1/development/jobs/{job_id}",
        response_model=JobResponse,
        tags=["development"],
    )
    def get_job(job_id: uuid.UUID, user_id: uuid.UUID) -> JobResponse:
        try:
            snapshot = resolved_services.queue.get_job(job_id, user_id=user_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        return JobResponse.from_snapshot(snapshot)

    @app.post(
        "/api/v1/development/jobs/{job_id}/cancel",
        response_model=JobResponse,
        tags=["development"],
    )
    def cancel_job(job_id: uuid.UUID, payload: JobOwnerRequest) -> JobResponse:
        try:
            resolved_services.queue.request_cancel(job_id, user_id=payload.user_id)
            snapshot = resolved_services.queue.get_job(
                job_id,
                user_id=payload.user_id,
            )
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        return JobResponse.from_snapshot(snapshot)

    return app
