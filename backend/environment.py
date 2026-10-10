from __future__ import annotations

from ipaddress import ip_address


DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "test"})
RESTRICTED_ENVIRONMENT = "restricted"
PRODUCTION_ENVIRONMENT = "production"
SUPPORTED_ENVIRONMENTS = frozenset(
    {*DEVELOPMENT_ENVIRONMENTS, RESTRICTED_ENVIRONMENT, PRODUCTION_ENVIRONMENT}
)


def normalize_environment(value: str) -> str:
    environment = value.strip().lower()
    if environment not in SUPPORTED_ENVIRONMENTS:
        supported = ", ".join(sorted(SUPPORTED_ENVIRONMENTS))
        raise ValueError(f"APP_ENV must be one of: {supported}")
    return environment


def is_development_environment(value: str) -> bool:
    return normalize_environment(value) in DEVELOPMENT_ENVIRONMENTS


def requires_explicit_operational_settings(value: str) -> bool:
    return normalize_environment(value) in {
        RESTRICTED_ENVIRONMENT,
        PRODUCTION_ENVIRONMENT,
    }


def is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
