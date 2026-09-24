from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from email_validator import EmailNotValidError, validate_email
from jwt.exceptions import InvalidTokenError as JWTInvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import RefreshSession, User

from .errors import (
    DuplicateIdentityError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from .settings import AuthSettings


USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
PASSWORD_HASH = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = PASSWORD_HASH.hash("not-a-real-re3d-user-password")
JWT_ALGORITHM = "HS256"
JWT_CLOCK_SKEW_SECONDS = 5


@dataclass(frozen=True)
class UserIdentity:
    id: uuid.UUID
    username: str
    email: str
    role: str
    is_active: bool
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    refresh_session_id: uuid.UUID
    access_expires_in: int
    refresh_expires_at: datetime
    user: UserIdentity


class AuthService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AuthSettings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def register(
        self,
        *,
        username: str,
        email: str,
        password: str,
    ) -> UserIdentity:
        normalized_username = _normalize_username(username)
        normalized_email = _normalize_email(email)
        _validate_password(password)
        password_hash = PASSWORD_HASH.hash(password)
        user = User(
            id=uuid.uuid4(),
            username=normalized_username,
            email=normalized_email,
            password_hash=password_hash,
            role="user",
            is_active=True,
            email_verified=False,
        )
        try:
            with self.session_factory.begin() as session:
                session.add(user)
        except DatabaseIntegrityError as exc:
            raise DuplicateIdentityError(
                "username or email is already registered"
            ) from exc
        return _user_identity(user)

    def login(self, *, identifier: str, password: str) -> IssuedTokens:
        normalized_identifier = identifier.strip().lower()
        with self.session_factory() as session:
            user = session.execute(
                select(User).where(
                    or_(
                        User.username == normalized_identifier,
                        User.email == normalized_identifier,
                    )
                )
            ).scalar_one_or_none()
            stored_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
            password_valid = PASSWORD_HASH.verify(password, stored_hash)
            if user is None or not password_valid:
                raise InvalidCredentialsError("invalid username/email or password")
            user_id = user.id

        with self.session_factory.begin() as session:
            current_user = session.execute(
                select(User).where(User.id == user_id).with_for_update()
            ).scalar_one()
            if not current_user.is_active:
                raise InactiveUserError("user account is inactive")
            now = _database_now(session)
            current_user.last_login_at = now
            current_user.updated_at = now
            return self._issue_tokens(session, current_user, now=now)

    def refresh(self, refresh_token: str) -> IssuedTokens:
        token_sha256 = _token_sha256(refresh_token)
        issued: IssuedTokens | None = None
        with self.session_factory.begin() as session:
            current = session.execute(
                select(RefreshSession)
                .where(RefreshSession.token_sha256 == token_sha256)
                .with_for_update()
            ).scalar_one_or_none()
            now = _database_now(session)
            if current is not None and current.revoked_at is not None:
                if current.replaced_by_id is not None:
                    session.execute(
                        update(RefreshSession)
                        .where(
                            RefreshSession.family_id == current.family_id,
                            RefreshSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
            elif current is not None and _as_utc(current.expires_at) > now:
                user = session.execute(
                    select(User).where(User.id == current.user_id).with_for_update()
                ).scalar_one_or_none()
                if user is None or not user.is_active:
                    raise InactiveUserError("user account is inactive")

                issued = self._issue_tokens(
                    session,
                    user,
                    now=now,
                    refresh_expires_at=_as_utc(current.expires_at),
                    refresh_family_id=current.family_id,
                )
                current.revoked_at = now
                current.last_used_at = now
                current.replaced_by_id = issued.refresh_session_id
        if issued is None:
            raise InvalidTokenError("refresh token is invalid or expired")
        return issued

    def logout(self, refresh_token: str | None) -> None:
        if not refresh_token:
            return
        token_sha256 = _token_sha256(refresh_token)
        with self.session_factory.begin() as session:
            current = session.execute(
                select(RefreshSession)
                .where(RefreshSession.token_sha256 == token_sha256)
                .with_for_update()
            ).scalar_one_or_none()
            if current is not None and current.revoked_at is None:
                current.revoked_at = _database_now(session)

    def current_user(self, access_token: str) -> UserIdentity:
        user_id = self._decode_access_token(access_token)
        with self.session_factory() as session:
            user = session.get(User, user_id)
            if user is None:
                raise InvalidTokenError("access token subject does not exist")
            if not user.is_active:
                raise InactiveUserError("user account is inactive")
            return _user_identity(user)

    def _issue_tokens(
        self,
        session: Session,
        user: User,
        *,
        now: datetime,
        refresh_expires_at: datetime | None = None,
        refresh_family_id: uuid.UUID | None = None,
    ) -> IssuedTokens:
        refresh_token = secrets.token_urlsafe(48)
        refresh_session_id = uuid.uuid4()
        expires_at = refresh_expires_at or (
            now + timedelta(days=self.settings.refresh_token_ttl_days)
        )
        session.add(
            RefreshSession(
                id=refresh_session_id,
                user_id=user.id,
                family_id=refresh_family_id or uuid.uuid4(),
                token_sha256=_token_sha256(refresh_token),
                expires_at=expires_at,
            )
        )
        access_expires_in = self.settings.access_token_ttl_minutes * 60
        access_token = jwt.encode(
            {
                "sub": f"user:{user.id}",
                "jti": str(uuid.uuid4()),
                "token_use": "access",
                "iss": self.settings.jwt_issuer,
                "aud": self.settings.jwt_audience,
                "iat": now,
                "exp": now + timedelta(seconds=access_expires_in),
            },
            self.settings.jwt_secret,
            algorithm=JWT_ALGORITHM,
        )
        return IssuedTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_session_id=refresh_session_id,
            access_expires_in=access_expires_in,
            refresh_expires_at=expires_at,
            user=_user_identity(user),
        )

    def _decode_access_token(self, token: str) -> uuid.UUID:
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=[JWT_ALGORITHM],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                leeway=JWT_CLOCK_SKEW_SECONDS,
                options={
                    "require": ["sub", "jti", "token_use", "iss", "aud", "iat", "exp"]
                },
            )
            if payload["token_use"] != "access":
                raise InvalidTokenError("token is not an access token")
            subject = payload["sub"]
            if not isinstance(subject, str) or not subject.startswith("user:"):
                raise InvalidTokenError("access token subject is invalid")
            return uuid.UUID(subject.removeprefix("user:"))
        except (JWTInvalidTokenError, KeyError, TypeError, ValueError) as exc:
            raise InvalidTokenError("access token is invalid or expired") from exc


def _normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "username must contain 3 to 32 lowercase letters, numbers, dots, "
            "underscores, or hyphens"
        )
    return normalized


def _normalize_email(email: str) -> str:
    try:
        return validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError("email address is invalid") from exc


def _validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 128 or len(password.strip()) < 12:
        raise ValueError("password must contain 12 to 128 non-blank characters")


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _database_now(session: Session) -> datetime:
    value = session.execute(select(func.current_timestamp())).scalar_one()
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _user_identity(user: User) -> UserIdentity:
    return UserIdentity(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        email_verified=user.email_verified,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )
