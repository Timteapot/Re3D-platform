from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine

from apps.api.admin import create_admin_audit_router
from apps.api.auth import create_auth_router
from apps.api.jobs import (
    LEGACY_DEVELOPMENT_JOB_PREFIX,
    create_development_job_router,
    create_job_router,
)
from apps.api.rate_limit import ApiIpRateLimitMiddleware
from apps.api.uploads import create_upload_router
from backend.auth import (
    AuthEmailSettings,
    AuthService,
    AuthSettings,
    build_auth_email_sender,
)
from backend.admin import AdminAuditService
from backend.db.queue import JobQueue
from backend.db.runtime import (
    DatabaseSettings,
    create_database_engine,
    create_session_factory,
)
from backend.environment import (
    RESTRICTED_ENVIRONMENT,
    is_development_environment,
    is_loopback_host,
    normalize_environment,
)
from backend.jobs import TaskSubmissionSettings
from backend.jobs.development import DevelopmentJobService
from backend.rate_limit import ApiRateLimitService, ApiRateLimitSettings
from backend.transfers import TransferLimitService, TransferLimitSettings
from backend.uploads import StorageQuotaSettings, UploadService, UploadSettings


@dataclass(frozen=True)
class AppServices:
    engine: Engine
    queue: JobQueue
    development_jobs: DevelopmentJobService
    auth: AuthService
    uploads: UploadService
    transfer_limits: TransferLimitService | None = None


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
        build_auth_email_sender(
            AuthEmailSettings.from_environment(environment=environment)
        ),
    )
    uploads = UploadService(
        sessions,
        queue,
        data_root=Path(configured_data_root),
        settings=UploadSettings.from_environment(),
        submission_settings=TaskSubmissionSettings.from_environment(
            environment=environment
        ),
        quota_settings=StorageQuotaSettings.from_environment(
            environment=environment
        ),
    )
    transfer_limits = TransferLimitService(
        sessions,
        TransferLimitSettings.from_environment(environment=environment),
    )
    return AppServices(
        engine,
        queue,
        development_jobs,
        auth,
        uploads,
        transfer_limits,
    )


def create_app(
    *,
    services: AppServices | None = None,
    app_env: str | None = None,
) -> FastAPI:
    environment = normalize_environment(
        app_env or os.environ.get("APP_ENV") or "production"
    )
    if environment == RESTRICTED_ENVIRONMENT and not is_loopback_host(
        os.environ.get("API_HOST")
    ):
        raise ValueError("restricted API_HOST must be a loopback host")
    app = FastAPI(title="Re3D Platform API", version="0.1.0")

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, Any]:
        return {
            "status": "ok",
            "environment": environment,
            "development_routes_enabled": is_development_environment(environment),
        }

    resolved_services = services or build_services(environment=environment)
    app.state.services = resolved_services
    rate_limit_settings = ApiRateLimitSettings.from_environment(
        environment=environment,
        fingerprint_secret=resolved_services.auth.settings.jwt_secret,
    )
    rate_limiter = ApiRateLimitService(
        resolved_services.auth.session_factory,
        rate_limit_settings,
    )
    app.state.api_rate_limiter = rate_limiter
    app.add_middleware(
        ApiIpRateLimitMiddleware,
        limiter=rate_limiter,
        trusted_proxy_cidrs=resolved_services.auth.settings.trusted_proxy_cidrs,
    )
    transfer_limits = resolved_services.transfer_limits or TransferLimitService(
        resolved_services.auth.session_factory,
        TransferLimitSettings.from_environment(environment=environment),
    )
    app.state.transfer_limits = transfer_limits
    auth_router, current_user, verified_user = create_auth_router(
        resolved_services.auth
    )
    app.include_router(auth_router)
    app.include_router(
        create_admin_audit_router(
            AdminAuditService(resolved_services.auth.session_factory),
            current_user,
        )
    )

    poll_seconds = 0.05 if environment == "test" else 1.0
    app.include_router(
        create_job_router(
            resolved_services.queue,
            data_root=resolved_services.development_jobs.data_root,
            current_user=current_user,
            transfer_limits=transfer_limits,
            poll_seconds=poll_seconds,
        )
    )

    is_development = is_development_environment(environment)
    app.include_router(
        create_upload_router(
            resolved_services.uploads,
            current_user,
            verified_user,
            transfer_limits=transfer_limits,
            default_execution_mode="simulated" if is_development else "real",
            allowed_execution_modes=(
                frozenset({"simulated", "real"})
                if is_development
                else frozenset({"real"})
            ),
        )
    )

    if not is_development:
        return app

    app.include_router(
        create_development_job_router(
            resolved_services.development_jobs,
            verified_user,
        )
    )
    app.include_router(
        create_job_router(
            resolved_services.queue,
            data_root=resolved_services.development_jobs.data_root,
            current_user=current_user,
            transfer_limits=transfer_limits,
            poll_seconds=poll_seconds,
            prefix=LEGACY_DEVELOPMENT_JOB_PREFIX,
            tags=["development"],
            include_in_schema=False,
        )
    )

    return app
