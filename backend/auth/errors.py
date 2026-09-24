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
