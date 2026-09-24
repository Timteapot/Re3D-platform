from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True)
class DatabaseSettings:
    url: str

    @classmethod
    def from_environment(cls, url: str | None = None) -> "DatabaseSettings":
        configured = url or os.environ.get("DATABASE_URL")
        if not configured:
            raise ValueError("DATABASE_URL is required")
        parsed = make_url(configured)
        if parsed.get_backend_name() not in {"postgresql", "sqlite"}:
            raise ValueError("DATABASE_URL must use PostgreSQL or SQLite")
        return cls(url=configured)


@dataclass(frozen=True)
class SchedulerSettings:
    resource_key: str
    lease_seconds: int
    heartbeat_seconds: int

    @classmethod
    def from_environment(
        cls,
        *,
        resource_key: str | None = None,
        lease_seconds: int | None = None,
        heartbeat_seconds: int | None = None,
    ) -> "SchedulerSettings":
        configured_resource = resource_key or os.environ.get(
            "RE3D_GPU_RESOURCE", "gpu:0"
        )
        configured_lease = (
            lease_seconds
            if lease_seconds is not None
            else _environment_integer("RE3D_LEASE_SECONDS", 60)
        )
        configured_heartbeat = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else _environment_integer("RE3D_HEARTBEAT_SECONDS", 20)
        )
        if not 5 <= configured_lease <= 3600:
            raise ValueError("RE3D_LEASE_SECONDS must be between 5 and 3600")
        if (
            configured_heartbeat <= 0
            or configured_heartbeat * 2 >= configured_lease
        ):
            raise ValueError(
                "RE3D_HEARTBEAT_SECONDS must be positive and less than half "
                "the lease TTL"
            )
        return cls(
            resource_key=configured_resource,
            lease_seconds=configured_lease,
            heartbeat_seconds=configured_heartbeat,
        )


def create_database_engine(settings: DatabaseSettings) -> Engine:
    options: dict[str, object] = {"pool_pre_ping": True}
    if make_url(settings.url).get_backend_name() == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
    return create_engine(settings.url, **options)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
