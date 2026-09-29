"""Controlled authentication failures safe to map to API responses."""


class AuthError(RuntimeError):
    """Base class for expected authentication errors."""


class DuplicateIdentityError(AuthError):
    """A normalized username or email is already registered."""


class InvalidCredentialsError(AuthError):
    """A login identifier and password do not authenticate a user."""


class InvalidTokenError(AuthError):
    """An access or refresh token is invalid, expired, or revoked."""


class InactiveUserError(AuthError):
    """The authenticated user has been disabled."""


class RateLimitExceededError(AuthError):
    """A login throttle bucket is blocked until a later time."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many login attempts")
        self.retry_after_seconds = max(1, retry_after_seconds)
