from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import ip_network
from urllib.parse import urlsplit

from backend.environment import (
    DEVELOPMENT_ENVIRONMENTS,
    PRODUCTION_ENVIRONMENT,
    RESTRICTED_ENVIRONMENT,
    is_loopback_host,
    normalize_environment,
)


@dataclass(frozen=True)
class AuthSettings:
    jwt_secret: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    jwt_issuer: str = "re3d-platform"
    jwt_audience: str = "re3d-platform-api"
    refresh_cookie_name: str = "re3d_refresh"
    cookie_secure: bool = True
    login_window_minutes: int = 15
    login_account_max_failures: int = 5
    login_ip_max_failures: int = 30
    login_block_minutes: int = 15
    registration_window_minutes: int = 60
    registration_identity_max_attempts: int = 3
    registration_ip_max_attempts: int = 10
    registration_block_minutes: int = 60
    action_request_window_minutes: int = 60
    action_request_identity_max_attempts: int = 3
    action_request_ip_max_attempts: int = 10
    action_request_block_minutes: int = 60
    email_verification_ttl_hours: int = 24
    password_reset_ttl_minutes: int = 30
    trusted_proxy_cidrs: tuple[str, ...] = ()

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
        if not 1 <= self.login_window_minutes <= 1440:
            raise ValueError("AUTH_LOGIN_WINDOW_MINUTES must be between 1 and 1440")
        if not 1 <= self.login_account_max_failures <= 100:
            raise ValueError(
                "AUTH_LOGIN_ACCOUNT_MAX_FAILURES must be between 1 and 100"
            )
        if not self.login_account_max_failures <= self.login_ip_max_failures <= 1000:
            raise ValueError(
                "AUTH_LOGIN_IP_MAX_FAILURES must be between the account limit and 1000"
            )
        if not 1 <= self.login_block_minutes <= 1440:
            raise ValueError("AUTH_LOGIN_BLOCK_MINUTES must be between 1 and 1440")
        if not 1 <= self.registration_window_minutes <= 1440:
            raise ValueError(
                "AUTH_REGISTRATION_WINDOW_MINUTES must be between 1 and 1440"
            )
        if not 1 <= self.registration_identity_max_attempts <= 100:
            raise ValueError(
                "AUTH_REGISTRATION_IDENTITY_MAX_ATTEMPTS must be between 1 and 100"
            )
        if not (
            self.registration_identity_max_attempts
            <= self.registration_ip_max_attempts
            <= 1000
        ):
            raise ValueError(
                "AUTH_REGISTRATION_IP_MAX_ATTEMPTS must be between the identity "
                "limit and 1000"
            )
        if not 1 <= self.registration_block_minutes <= 1440:
            raise ValueError(
                "AUTH_REGISTRATION_BLOCK_MINUTES must be between 1 and 1440"
            )
        if not 1 <= self.action_request_window_minutes <= 1440:
            raise ValueError(
                "AUTH_ACTION_REQUEST_WINDOW_MINUTES must be between 1 and 1440"
            )
        if not 1 <= self.action_request_identity_max_attempts <= 100:
            raise ValueError(
                "AUTH_ACTION_REQUEST_IDENTITY_MAX_ATTEMPTS must be between 1 and 100"
            )
        if not (
            self.action_request_identity_max_attempts
            <= self.action_request_ip_max_attempts
            <= 1000
        ):
            raise ValueError(
                "AUTH_ACTION_REQUEST_IP_MAX_ATTEMPTS must be between the identity "
                "limit and 1000"
            )
        if not 1 <= self.action_request_block_minutes <= 1440:
            raise ValueError(
                "AUTH_ACTION_REQUEST_BLOCK_MINUTES must be between 1 and 1440"
            )
        if not 1 <= self.email_verification_ttl_hours <= 168:
            raise ValueError(
                "AUTH_EMAIL_VERIFICATION_TTL_HOURS must be between 1 and 168"
            )
        if not 5 <= self.password_reset_ttl_minutes <= 1440:
            raise ValueError(
                "AUTH_PASSWORD_RESET_TTL_MINUTES must be between 5 and 1440"
            )
        for cidr in self.trusted_proxy_cidrs:
            try:
                ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"AUTH_TRUSTED_PROXY_CIDRS contains an invalid network: {cidr}"
                ) from exc

    @classmethod
    def from_environment(cls, *, environment: str) -> "AuthSettings":
        environment = normalize_environment(environment)
        secret = os.environ.get("JWT_SECRET", "")
        restricted_scheme: str | None = None
        if environment == RESTRICTED_ENVIRONMENT:
            public_base_url = os.environ.get("APP_PUBLIC_BASE_URL", "").strip()
            restricted_scheme = _validate_restricted_public_base_url(
                public_base_url
            )
        secure_default = environment == PRODUCTION_ENVIRONMENT or (
            environment == RESTRICTED_ENVIRONMENT
            and restricted_scheme == "https"
        )
        cookie_secure = _environment_bool(
            "REFRESH_COOKIE_SECURE",
            secure_default,
        )
        if environment == PRODUCTION_ENVIRONMENT and not cookie_secure:
            raise ValueError("REFRESH_COOKIE_SECURE must be true outside development")
        if environment == RESTRICTED_ENVIRONMENT and cookie_secure != (
            restricted_scheme == "https"
        ):
            expected = "true" if restricted_scheme == "https" else "false"
            raise ValueError(
                "REFRESH_COOKIE_SECURE must be "
                f"{expected} for the restricted APP_PUBLIC_BASE_URL scheme"
            )
        trusted_proxy_cidrs = _environment_csv("AUTH_TRUSTED_PROXY_CIDRS")
        if environment not in DEVELOPMENT_ENVIRONMENTS and not trusted_proxy_cidrs:
            raise ValueError(
                "AUTH_TRUSTED_PROXY_CIDRS is required outside development"
            )
        if environment == RESTRICTED_ENVIRONMENT:
            _validate_restricted_proxy_networks(trusted_proxy_cidrs)
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
            login_window_minutes=_environment_integer(
                "AUTH_LOGIN_WINDOW_MINUTES",
                15,
            ),
            login_account_max_failures=_environment_integer(
                "AUTH_LOGIN_ACCOUNT_MAX_FAILURES",
                5,
            ),
            login_ip_max_failures=_environment_integer(
                "AUTH_LOGIN_IP_MAX_FAILURES",
                30,
            ),
            login_block_minutes=_environment_integer(
                "AUTH_LOGIN_BLOCK_MINUTES",
                15,
            ),
            registration_window_minutes=_environment_integer(
                "AUTH_REGISTRATION_WINDOW_MINUTES",
                60,
            ),
            registration_identity_max_attempts=_environment_integer(
                "AUTH_REGISTRATION_IDENTITY_MAX_ATTEMPTS",
                3,
            ),
            registration_ip_max_attempts=_environment_integer(
                "AUTH_REGISTRATION_IP_MAX_ATTEMPTS",
                10,
            ),
            registration_block_minutes=_environment_integer(
                "AUTH_REGISTRATION_BLOCK_MINUTES",
                60,
            ),
            action_request_window_minutes=_environment_integer(
                "AUTH_ACTION_REQUEST_WINDOW_MINUTES",
                60,
            ),
            action_request_identity_max_attempts=_environment_integer(
                "AUTH_ACTION_REQUEST_IDENTITY_MAX_ATTEMPTS",
                3,
            ),
            action_request_ip_max_attempts=_environment_integer(
                "AUTH_ACTION_REQUEST_IP_MAX_ATTEMPTS",
                10,
            ),
            action_request_block_minutes=_environment_integer(
                "AUTH_ACTION_REQUEST_BLOCK_MINUTES",
                60,
            ),
            email_verification_ttl_hours=_environment_integer(
                "AUTH_EMAIL_VERIFICATION_TTL_HOURS",
                24,
            ),
            password_reset_ttl_minutes=_environment_integer(
                "AUTH_PASSWORD_RESET_TTL_MINUTES",
                30,
            ),
            trusted_proxy_cidrs=trusted_proxy_cidrs,
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


def _environment_csv(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def _validate_restricted_public_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not is_loopback_host(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "restricted APP_PUBLIC_BASE_URL must be an HTTP(S) loopback origin"
        )
    return parsed.scheme


def _validate_restricted_proxy_networks(cidrs: tuple[str, ...]) -> None:
    for cidr in cidrs:
        network = ip_network(cidr, strict=False)
        if not (
            network.network_address.is_loopback
            and network.broadcast_address.is_loopback
        ):
            raise ValueError(
                "restricted AUTH_TRUSTED_PROXY_CIDRS may contain only loopback networks"
            )
