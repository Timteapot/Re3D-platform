from __future__ import annotations

import os

from sqlalchemy.engine import make_url


def validated_test_database_url() -> str | None:
    """Return only an explicitly disposable PostgreSQL integration database."""

    configured = os.environ.get("RE3D_TEST_DATABASE_URL")
    if not configured:
        return None
    parsed = make_url(configured)
    if parsed.get_backend_name() != "postgresql":
        raise RuntimeError("RE3D_TEST_DATABASE_URL must use PostgreSQL")
    database = parsed.database or ""
    if database == "re3d_platform_dev" or not database.endswith("_test"):
        raise RuntimeError(
            "RE3D_TEST_DATABASE_URL must target a disposable database ending in _test"
        )
    return configured
