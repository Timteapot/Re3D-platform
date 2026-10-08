from __future__ import annotations

import os
from dataclasses import dataclass, field


DEVELOPMENT_ENVIRONMENTS = {"development", "test"}


@dataclass(frozen=True)
class ApiRateLimitSettings:
    fingerprint_secret: str = field(repr=False)
    window_seconds: int = 60
    ip_max_requests: int = 300

    def __post_init__(self) -> None:
        if len(self.fingerprint_secret.encode("utf-8")) < 32:
            raise ValueError("API rate-limit fingerprint secret is too short")
        if not 1 <= self.window_seconds <= 3600:
            raise ValueError(
                "API_IP_RATE_LIMIT_WINDOW_SECONDS must be between 1 and 3600"
            )
        if not 1 <= self.ip_max_requests <= 100_000:
            raise ValueError(
                "API_IP_RATE_LIMIT_MAX_REQUESTS must be between 1 and 100000"
            )

    @classmethod
    def from_environment(
        cls,
        *,
        environment: str,
        fingerprint_secret: str,
    ) -> "ApiRateLimitSettings":
        production = environment not in DEVELOPMENT_ENVIRONMENTS
        return cls(
            fingerprint_secret=fingerprint_secret,
            window_seconds=_environment_integer(
                "API_IP_RATE_LIMIT_WINDOW_SECONDS",
                60,
                required=production,
            ),
            ip_max_requests=_environment_integer(
                "API_IP_RATE_LIMIT_MAX_REQUESTS",
                300,
                required=production,
            ),
        )


def _environment_integer(name: str, default: int, *, required: bool) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        if required:
            raise ValueError(f"{name} is required outside development")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
