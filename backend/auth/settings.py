from __future__ import annotations

import os
import re
from dataclasses import dataclass


DEVELOPMENT_ENVIRONMENTS = {"development", "test"}


@dataclass(frozen=True)
class AuthSettings:
    jwt_secret: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    jwt_issuer: str = "re3d-platform"
    jwt_audience: str = "re3d-platform-api"
    refresh_cookie_name: str = "re3d_refresh"
    cookie_secure: bool = True

    def __post_init__(self) -> None:
        if len(self.jwt_secret.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")
        if self.jwt_secret.startswith("replace-with"):
            raise ValueError("JWT_SECRET must not use the example placeholder")
        if not 1 <= self.access_token_ttl_minutes <= 60:
            raise ValueError("ACCESS_TOKEN_TTL_MINUTES must be between 1 and 60")
        if not 1 <= self.refresh_token_ttl_days <= 90:
            raise ValueError("REFRESH_TOKEN_TTL_DAYS must be between 1 and 90")
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", self.refresh_cookie_name):
            raise ValueError("REFRESH_COOKIE_NAME has an invalid format")
        if not self.jwt_issuer or not self.jwt_audience:
            raise ValueError("JWT issuer and audience must not be empty")

    @classmethod
    def from_environment(cls, *, environment: str) -> "AuthSettings":
        secret = os.environ.get("JWT_SECRET", "")
        secure_default = environment not in DEVELOPMENT_ENVIRONMENTS
        cookie_secure = _environment_bool(
            "REFRESH_COOKIE_SECURE",
            secure_default,
        )
        if environment not in DEVELOPMENT_ENVIRONMENTS and not cookie_secure:
            raise ValueError("REFRESH_COOKIE_SECURE must be true outside development")
        return cls(
            jwt_secret=secret,
            access_token_ttl_minutes=_environment_integer(
                "ACCESS_TOKEN_TTL_MINUTES",
                15,
            ),
            refresh_token_ttl_days=_environment_integer(
                "REFRESH_TOKEN_TTL_DAYS",
                7,
            ),
            jwt_issuer=os.environ.get("JWT_ISSUER", "re3d-platform"),
            jwt_audience=os.environ.get("JWT_AUDIENCE", "re3d-platform-api"),
            refresh_cookie_name=os.environ.get(
                "REFRESH_COOKIE_NAME",
                "re3d_refresh",
            ),
            cookie_secure=cookie_secure,
        )


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")
