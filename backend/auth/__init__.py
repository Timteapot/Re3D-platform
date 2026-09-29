"""Authentication, access-token, and refresh-session services."""

from .errors import (
    AuthError,
    DuplicateIdentityError,
    EmailDeliveryError,
    InactiveUserError,
    InvalidActionTokenError,
    InvalidCredentialsError,
    InvalidTokenError,
    RateLimitExceededError,
)
from .email import (
    AuthEmailSender,
    AuthEmailSettings,
    DisabledAuthEmailSender,
    SmtpAuthEmailSender,
    build_auth_email_sender,
)
from .context import AuthRequestContext, resolve_client_ip
from .maintenance import AuthMaintenanceService, AuthMaintenanceSettings
from .service import AuthService, IssuedTokens, UserIdentity
from .settings import AuthSettings

__all__ = [
    "AuthError",
    "AuthEmailSender",
    "AuthEmailSettings",
    "AuthRequestContext",
    "AuthMaintenanceService",
    "AuthMaintenanceSettings",
    "AuthService",
    "AuthSettings",
    "DuplicateIdentityError",
    "DisabledAuthEmailSender",
    "EmailDeliveryError",
    "InactiveUserError",
    "InvalidActionTokenError",
    "InvalidCredentialsError",
    "InvalidTokenError",
    "RateLimitExceededError",
    "SmtpAuthEmailSender",
    "IssuedTokens",
    "UserIdentity",
    "resolve_client_ip",
    "build_auth_email_sender",
]
