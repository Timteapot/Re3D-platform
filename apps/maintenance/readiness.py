from __future__ import annotations

import json
import os
import smtplib
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from backend.auth import AuthEmailSettings, AuthSettings
from backend.db.runtime import DatabaseSettings, create_database_engine
from backend.re3d_adapter.real import verify_re3d_installation
from backend.re3d_adapter.settings import Re3DSettings, WorkerSettings


ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "config" / "pipeline-baseline.json"


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

    try:
        with smtplib.SMTP(
            email.host,
            email.port,
            timeout=email.timeout_seconds,
        ) as smtp:
            if email.starttls:
                smtp.starttls()
            if email.username is not None:
                assert email.password is not None
                smtp.login(email.username, email.password)
            code, _ = smtp.noop()
            if code != 250:
                raise ValueError("configured SMTP service did not accept NOOP")
    except (OSError, smtplib.SMTPException) as exc:
        raise ValueError("cannot connect to the configured SMTP service") from exc

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
        "smtp": {
            "host": email.host,
            "port": email.port,
            "starttls": email.starttls,
        },
    }


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
