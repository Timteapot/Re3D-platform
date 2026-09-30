from __future__ import annotations

import json
import os
import shutil
import smtplib
import ssl
from ipaddress import ip_network
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from backend.auth import AuthEmailSettings, AuthSettings
from backend.db.runtime import (
    DatabaseSettings,
    SchedulerSettings,
    create_database_engine,
)
from backend.jobs import FailedJobCleanupSettings
from backend.monitoring import ResourceMonitorSettings
from backend.re3d_adapter.real import verify_re3d_installation
from backend.re3d_adapter.settings import Re3DSettings, WorkerSettings
from backend.uploads import UploadSettings


ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "config" / "pipeline-baseline.json"
ProductionComponent = Literal["api", "worker", "all"]


def check_local_readiness() -> dict[str, Any]:
    environment = os.environ.get("APP_ENV", "").strip().lower()
    if environment != "development":
        raise ValueError("APP_ENV must be development for local readiness checks")

    AuthSettings.from_environment(environment=environment)
    email = AuthEmailSettings.from_environment(environment=environment)
    if email.host is None:
        raise ValueError("SMTP_HOST is required for local email acceptance")

    database = DatabaseSettings.from_environment()
    parsed_database = make_url(database.url)
    if parsed_database.get_backend_name() != "postgresql":
        raise ValueError("local development DATABASE_URL must use PostgreSQL")
    if parsed_database.database != "re3d_platform_dev":
        raise ValueError("local readiness only accepts database re3d_platform_dev")
    if parsed_database.username != "re3d_app":
        raise ValueError("local readiness only accepts database role re3d_app")
    if parsed_database.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("local development PostgreSQL must use a loopback host")

    engine = create_database_engine(database)
    try:
        try:
            with engine.connect() as connection:
                database_name, role_name = connection.execute(
                    text("SELECT current_database(), current_user")
                ).one()
                migrated_revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
        except SQLAlchemyError as exc:
            raise ValueError(
                "cannot connect to the configured development database or read its migration state"
            ) from exc
    finally:
        engine.dispose()

    expected_revision = _expected_migration_revision()
    if database_name != "re3d_platform_dev" or role_name != "re3d_app":
        raise ValueError("development database identity does not match the restricted role")
    if migrated_revision != expected_revision:
        raise ValueError(
            f"development database migration is {migrated_revision}; expected {expected_revision}"
        )

    worker = WorkerSettings.from_environment()
    resource_monitor = ResourceMonitorSettings.from_environment()
    if not worker.data_root.is_dir():
        raise ValueError("RE3D_DATA_ROOT must be an existing directory")
    if not os.access(worker.data_root, os.W_OK):
        raise ValueError("RE3D_DATA_ROOT is not writable")

    re3d = Re3DSettings.from_environment()
    if not re3d.driver_python.is_file():
        raise ValueError("configured Re3D driver Python does not exist")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    verify_re3d_installation(
        re3d.root,
        {
            "tag": baseline["tag"],
            "commit": baseline["commit"],
            "config_sha256": baseline["pipeline_config"]["sha256"],
        },
    )

    smtp_summary = _check_smtp(email)

    return {
        "status": "ready",
        "environment": environment,
        "database": {
            "name": database_name,
            "role": role_name,
            "migration": migrated_revision,
        },
        "re3d": {
            "root": str(re3d.root),
            "commit": baseline["commit"],
            "tag": baseline["tag"],
        },
        "data_root": str(worker.data_root),
        "resource_monitor": {
            "enabled": resource_monitor.enabled,
            "interval_seconds": resource_monitor.interval_seconds,
            "nvidia_smi_configured": resource_monitor.nvidia_smi_path is not None,
        },
        "smtp": smtp_summary,
    }


def check_production_readiness(
    *,
    component: ProductionComponent = "all",
) -> dict[str, Any]:
    """Validate one production process role without returning any secrets.

    ``api`` validates the public application, authentication, upload storage and
    email dependencies. ``worker`` validates the GPU worker, queue, shared
    storage and the frozen Re3D checkout. ``all`` is the initial single-host
    deployment contract and performs both sets of checks.
    """

    if component not in {"api", "worker", "all"}:
        raise ValueError("production component must be api, worker, or all")
    environment = os.environ.get("APP_ENV", "").strip().lower()
    if environment != "production":
        raise ValueError("APP_ENV must be production for production readiness checks")

    response: dict[str, Any] = {
        "status": "ready",
        "environment": environment,
        "component": component,
    }

    if component in {"api", "all"}:
        auth = AuthSettings.from_environment(environment=environment)
        _validate_production_proxy_networks(auth.trusted_proxy_cidrs)
        email = AuthEmailSettings.from_environment(environment=environment)
        _validate_production_email_identity(email)
        uploads = UploadSettings.from_environment()
        response["authentication"] = {
            "cookie_secure": auth.cookie_secure,
            "trusted_proxy_network_count": len(auth.trusted_proxy_cidrs),
        }
        response["public_base_url"] = email.public_base_url
        response["smtp"] = _check_smtp(email)
        response["uploads"] = {
            "max_file_bytes": uploads.max_file_bytes,
            "max_total_bytes": uploads.max_total_bytes,
            "max_pixels": uploads.max_pixels,
        }

    database = DatabaseSettings.from_environment()
    response["database"] = _check_production_database(database)
    response["storage"] = _check_production_storage()

    if component in {"worker", "all"}:
        scheduler = SchedulerSettings.from_environment()
        failed_job_cleanup = FailedJobCleanupSettings.from_environment()
        resource_monitor = ResourceMonitorSettings.from_environment()
        response["scheduler"] = {
            "resource_key": scheduler.resource_key,
            "lease_seconds": scheduler.lease_seconds,
            "heartbeat_seconds": scheduler.heartbeat_seconds,
        }
        response["failed_job_cleanup"] = {
            "grace_minutes": failed_job_cleanup.grace_minutes,
            "interval_seconds": failed_job_cleanup.interval_seconds,
            "batch_size": failed_job_cleanup.batch_size,
        }
        response["resource_monitor"] = {
            "enabled": resource_monitor.enabled,
            "interval_seconds": resource_monitor.interval_seconds,
            "nvidia_smi_configured": resource_monitor.nvidia_smi_path is not None,
        }
        response["re3d"] = _check_re3d_installation()

    return response


def _check_production_database(database: DatabaseSettings) -> dict[str, str]:
    parsed_database = make_url(database.url)
    if parsed_database.get_backend_name() != "postgresql":
        raise ValueError("production DATABASE_URL must use PostgreSQL")
    if parsed_database.password and "replace-with" in parsed_database.password:
        raise ValueError("production DATABASE_URL still contains an example password")

    engine = create_database_engine(database)
    try:
        try:
            with engine.connect() as connection:
                identity = connection.execute(
                    text(
                        """
                        SELECT current_database(), current_user,
                               rolsuper, rolcreatedb, rolcreaterole,
                               rolreplication, rolbypassrls,
                               has_database_privilege(
                                   current_user,
                                   current_database(),
                                   'CREATE'
                               ),
                               has_database_privilege(
                                   current_user,
                                   current_database(),
                                   'TEMPORARY'
                               ),
                               has_schema_privilege(
                                   current_user,
                                   'public',
                                   'CREATE'
                               )
                        FROM pg_roles
                        WHERE rolname = current_user
                        """
                    )
                ).one()
                migrated_revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
        except SQLAlchemyError as exc:
            raise ValueError(
                "cannot connect to the production database or read its migration state"
            ) from exc
    finally:
        engine.dispose()

    database_name, role_name, *role_capabilities = identity
    _validate_production_database_identity(
        database_name=database_name,
        role_name=role_name,
        role_capabilities=tuple(bool(value) for value in role_capabilities),
    )
    expected_revision = _expected_migration_revision()
    if migrated_revision != expected_revision:
        raise ValueError(
            f"production database migration is {migrated_revision}; "
            f"expected {expected_revision}"
        )
    return {
        "name": database_name,
        "role": role_name,
        "migration": migrated_revision,
    }


def _validate_production_database_identity(
    *,
    database_name: str,
    role_name: str,
    role_capabilities: tuple[bool, ...],
) -> None:
    reserved_databases = {
        "postgres",
        "template0",
        "template1",
        "re3d_platform_dev",
    }
    if database_name in reserved_databases or database_name.endswith("_test"):
        raise ValueError("production must use a dedicated non-development database")
    if role_name in {"postgres", "re3d_migrator"} or any(role_capabilities):
        raise ValueError(
            "production database role must be the restricted runtime identity "
            "without administrative, temporary, or schema-creation privileges"
        )


def _validate_production_proxy_networks(cidrs: tuple[str, ...]) -> None:
    if any(ip_network(cidr, strict=False).prefixlen == 0 for cidr in cidrs):
        raise ValueError(
            "AUTH_TRUSTED_PROXY_CIDRS must not trust an all-address network"
        )


def _validate_production_email_identity(email: AuthEmailSettings) -> None:
    public_host = (urlsplit(email.public_base_url).hostname or "").lower()
    smtp_host = (email.host or "").lower()
    sender_domain = email.sender.rpartition("@")[2].lower()
    configured_hosts = (public_host, smtp_host, sender_domain)
    if any(_is_example_host(host) for host in configured_hosts):
        raise ValueError("production URL and email settings must replace example domains")
    if any(
        value is not None and "replace-with" in value
        for value in (email.username, email.password)
    ):
        raise ValueError("production SMTP settings still contain example credentials")


def _is_example_host(host: str) -> bool:
    return (
        host == "example.com"
        or host.endswith(".example.com")
        or host == "example"
        or host.endswith(".example")
    )


def _check_production_storage() -> dict[str, int | str]:
    worker = WorkerSettings.from_environment()
    if not worker.data_root.is_dir():
        raise ValueError("RE3D_DATA_ROOT must be an existing directory")
    if not os.access(worker.data_root, os.W_OK):
        raise ValueError("RE3D_DATA_ROOT is not writable")
    if worker.data_root == ROOT or ROOT in worker.data_root.parents:
        raise ValueError("production RE3D_DATA_ROOT must be outside the source checkout")

    minimum_free_bytes = _required_environment_integer(
        "RE3D_MIN_FREE_DISK_BYTES",
        minimum=1024**3,
    )
    free_bytes = shutil.disk_usage(worker.data_root).free
    if free_bytes < minimum_free_bytes:
        raise ValueError(
            "RE3D_DATA_ROOT does not satisfy RE3D_MIN_FREE_DISK_BYTES"
        )
    return {
        "data_root": str(worker.data_root),
        "free_bytes": free_bytes,
        "minimum_free_bytes": minimum_free_bytes,
    }


def _check_re3d_installation() -> dict[str, str]:
    re3d = Re3DSettings.from_environment()
    if not re3d.driver_python.is_file():
        raise ValueError("configured Re3D driver Python does not exist")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    verify_re3d_installation(
        re3d.root,
        {
            "tag": baseline["tag"],
            "commit": baseline["commit"],
            "config_sha256": baseline["pipeline_config"]["sha256"],
        },
    )
    return {
        "root": str(re3d.root),
        "commit": baseline["commit"],
        "tag": baseline["tag"],
    }


def _check_smtp(email: AuthEmailSettings) -> dict[str, int | str | bool]:
    if email.host is None:
        raise ValueError("SMTP_HOST is required for email readiness checks")
    try:
        with smtplib.SMTP(
            email.host,
            email.port,
            timeout=email.timeout_seconds,
        ) as smtp:
            if email.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if email.username is not None:
                assert email.password is not None
                smtp.login(email.username, email.password)
            code, _ = smtp.noop()
            if code != 250:
                raise ValueError("configured SMTP service did not accept NOOP")
    except (OSError, smtplib.SMTPException) as exc:
        raise ValueError("cannot connect to the configured SMTP service") from exc
    return {
        "host": email.host,
        "port": email.port,
        "starttls": email.starttls,
    }


def _required_environment_integer(name: str, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raise ValueError(f"{name} is required")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _expected_migration_revision() -> str:
    configuration = Config(str(ROOT / "alembic.ini"))
    configuration.set_main_option(
        "script_location",
        str(ROOT / "backend" / "db" / "migrations"),
    )
    revision = ScriptDirectory.from_config(configuration).get_current_head()
    if revision is None:
        raise ValueError("Alembic migration head could not be resolved")
    return revision
