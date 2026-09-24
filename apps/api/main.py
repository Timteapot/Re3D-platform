from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine

from apps.api.auth import create_auth_router
from apps.api.jobs import create_development_job_router
from apps.api.uploads import create_upload_router
from backend.auth import AuthService, AuthSettings
from backend.db.queue import JobQueue
from backend.db.runtime import (
    DatabaseSettings,
    create_database_engine,
    create_session_factory,
)
from backend.jobs.development import DevelopmentJobService
from backend.uploads import UploadService, UploadSettings


DEVELOPMENT_ENVIRONMENTS = {"development", "test"}


@dataclass(frozen=True)
class AppServices:
    engine: Engine
    queue: JobQueue
    development_jobs: DevelopmentJobService
    auth: AuthService
    uploads: UploadService


def build_services(*, environment: str) -> AppServices:
    database = DatabaseSettings.from_environment()
    configured_data_root = os.environ.get("RE3D_DATA_ROOT")
    if not configured_data_root:
        raise ValueError("RE3D_DATA_ROOT is required")
    engine = create_database_engine(database)
    sessions = create_session_factory(engine)
    queue = JobQueue(sessions)
    development_jobs = DevelopmentJobService(
        queue,
        data_root=Path(configured_data_root),
    )
    auth = AuthService(
        sessions,
        AuthSettings.from_environment(environment=environment),
    )
    uploads = UploadService(
        sessions,
        queue,
        data_root=Path(configured_data_root),
        settings=UploadSettings.from_environment(),
    )
    return AppServices(engine, queue, development_jobs, auth, uploads)


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

    resolved_services = services or build_services(environment=environment)
    app.state.services = resolved_services
    auth_router, current_user = create_auth_router(resolved_services.auth)
    app.include_router(auth_router)

    if environment not in DEVELOPMENT_ENVIRONMENTS:
        return app

    app.include_router(create_upload_router(resolved_services.uploads, current_user))

    app.include_router(
        create_development_job_router(
            resolved_services.queue,
            resolved_services.development_jobs,
            current_user,
            poll_seconds=0.05 if environment == "test" else 1.0,
        )
    )

    return app
