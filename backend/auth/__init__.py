"""Authentication, access-token, and refresh-session services."""

from .errors import (
    AuthError,
    DuplicateIdentityError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
    RateLimitExceededError,
)
from .context import AuthRequestContext, resolve_client_ip
from .service import AuthService, IssuedTokens, UserIdentity
from .settings import AuthSettings

__all__ = [
    "AuthError",
    "AuthRequestContext",
    "AuthService",
    "AuthSettings",
    "DuplicateIdentityError",
    "InactiveUserError",
    "InvalidCredentialsError",
    "InvalidTokenError",
    "RateLimitExceededError",
    "IssuedTokens",
    "UserIdentity",
    "resolve_client_ip",
]
