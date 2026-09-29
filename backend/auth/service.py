from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import jwt
from email_validator import EmailNotValidError, validate_email
from jwt.exceptions import InvalidTokenError as JWTInvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import AuthActionToken, RefreshSession, User

from .email import AuthEmailSender, DisabledAuthEmailSender
from .errors import (
    DuplicateIdentityError,
    EmailDeliveryError,
    InactiveUserError,
    InvalidActionTokenError,
    InvalidCredentialsError,
    InvalidTokenError,
    RateLimitExceededError,
)
from .context import AuthRequestContext
from .security import AuthSecurity, LoginThrottleKey
from .settings import AuthSettings


USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
PASSWORD_HASH = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = PASSWORD_HASH.hash("not-a-real-re3d-user-password")
JWT_ALGORITHM = "HS256"
JWT_CLOCK_SKEW_SECONDS = 5
LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class PendingAuthEmail:
    purpose: str
    token_id: uuid.UUID
    user_id: uuid.UUID
    recipient: str
    token: str = field(repr=False)


class AuthService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AuthSettings,
        email_sender: AuthEmailSender | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.security = AuthSecurity(settings)
        self.email_sender = email_sender or DisabledAuthEmailSender()

    def register(
        self,
        *,
        username: str,
        email: str,
        password: str,
        context: AuthRequestContext | None = None,
    ) -> UserIdentity:
        normalized_username = _normalize_username(username)
        normalized_email = _normalize_email(email)
        _validate_password(password)
        identifier_fingerprint = self.security.identifier_fingerprint(
            normalized_email
        )
        throttle_keys = self.security.registration_keys(
            normalized_username=normalized_username,
            normalized_email=normalized_email,
            client_ip_fingerprint=self.security.client_ip_fingerprint(context),
        )
        retry_after: int | None
        with self.session_factory.begin() as session:
            now = _database_now(session)
            retry_after = self.security.consume_registration_attempt(
                session,
                throttle_keys,
                now=now,
            )
            if retry_after is not None:
                self.security.add_event(
                    session,
                    action="register",
                    outcome="blocked",
                    context=context,
                    occurred_at=now,
                    identifier_fingerprint=identifier_fingerprint,
                    reason_code="rate_limited",
                )
        if retry_after is not None:
            raise RateLimitExceededError(retry_after)

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
                now = _database_now(session)
                session.add(user)
                self.security.add_event(
                    session,
                    action="register",
                    outcome="success",
                    context=context,
                    occurred_at=now,
                    user_id=user.id,
                    identifier_fingerprint=identifier_fingerprint,
                )
        except DatabaseIntegrityError as exc:
            self._record_event(
                action="register",
                outcome="failure",
                context=context,
                identifier=normalized_email,
                reason_code="duplicate_identity",
            )
            raise DuplicateIdentityError(
                "username or email is already registered"
            ) from exc
        return _user_identity(user)

    def login(
        self,
        *,
        identifier: str,
        password: str,
        context: AuthRequestContext | None = None,
    ) -> IssuedTokens:
        normalized_identifier = identifier.strip().lower()
        identifier_fingerprint = self.security.identifier_fingerprint(
            normalized_identifier
        )
        client_ip_fingerprint = self.security.client_ip_fingerprint(context)
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
            user_id = user.id if user is not None else None
        account_fingerprint = self.security.login_account_fingerprint(
            normalized_identifier=normalized_identifier,
            user_id=user_id,
        )
        throttle_keys = self.security.login_keys(
            account_fingerprint,
            client_ip_fingerprint,
        )
        retry_after: int | None
        with self.session_factory.begin() as session:
            now = _database_now(session)
            retry_after = self.security.login_retry_after(
                session,
                throttle_keys,
                now=now,
            )
            if retry_after is not None:
                self.security.add_event(
                    session,
                    action="login",
                    outcome="blocked",
                    context=context,
                    occurred_at=now,
                    user_id=user_id,
                    identifier_fingerprint=identifier_fingerprint,
                    reason_code="rate_limited",
                )
        if retry_after is not None:
            raise RateLimitExceededError(retry_after)

        password_valid = PASSWORD_HASH.verify(password, stored_hash)
        if user_id is None or not password_valid:
            self._record_login_failure(
                throttle_keys,
                context=context,
                identifier_fingerprint=identifier_fingerprint,
                user_id=user_id,
                reason_code="invalid_credentials",
            )
            raise InvalidCredentialsError("invalid username/email or password")

        issued: IssuedTokens | None = None
        inactive = False
        with self.session_factory.begin() as session:
            current_user = session.execute(
                select(User).where(User.id == user_id).with_for_update()
            ).scalar_one()
            now = _database_now(session)
            if not current_user.is_active:
                inactive = True
                self.security.record_login_failure(
                    session,
                    throttle_keys,
                    now=now,
                )
                self.security.add_event(
                    session,
                    action="login",
                    outcome="failure",
                    context=context,
                    occurred_at=now,
                    user_id=current_user.id,
                    identifier_fingerprint=identifier_fingerprint,
                    reason_code="inactive_user",
                )
            else:
                self.security.reset_account_throttle(
                    session,
                    throttle_keys,
                    now=now,
                )
                current_user.last_login_at = now
                current_user.updated_at = now
                issued = self._issue_tokens(session, current_user, now=now)
                self.security.add_event(
                    session,
                    action="login",
                    outcome="success",
                    context=context,
                    occurred_at=now,
                    user_id=current_user.id,
                    refresh_session_id=issued.refresh_session_id,
                    identifier_fingerprint=identifier_fingerprint,
                )
        if inactive:
            raise InactiveUserError("user account is inactive")
        assert issued is not None
        return issued

    def refresh(
        self,
        refresh_token: str | None,
        *,
        context: AuthRequestContext | None = None,
    ) -> IssuedTokens:
        if not refresh_token:
            raise InvalidTokenError("refresh token is invalid or expired")
        token_sha256 = _token_sha256(refresh_token)
        issued: IssuedTokens | None = None
        inactive = False
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
                    outcome = "reuse"
                    reason_code = "refresh_reuse_detected"
                else:
                    outcome = "failure"
                    reason_code = "refresh_revoked"
            elif current is not None and _as_utc(current.expires_at) > now:
                user = session.execute(
                    select(User).where(User.id == current.user_id).with_for_update()
                ).scalar_one_or_none()
                if user is None or not user.is_active:
                    inactive = True
                    outcome = "failure"
                    reason_code = "inactive_user"
                else:
                    issued = self._issue_tokens(
                        session,
                        user,
                        now=now,
                        refresh_expires_at=_as_utc(current.expires_at),
                        refresh_family_id=current.family_id,
                    )
                    # ``replaced_by_id`` is a scalar self-referencing foreign key,
                    # so SQLAlchemy cannot infer that the pending successor must be
                    # inserted before the current row is updated.  Flush the new
                    # session first while keeping both writes in this transaction.
                    session.flush()
                    current.revoked_at = now
                    current.last_used_at = now
                    current.replaced_by_id = issued.refresh_session_id
                    outcome = "success"
                    reason_code = None
            else:
                outcome = "failure"
                reason_code = "refresh_invalid_or_expired"
            if current is not None:
                self.security.add_event(
                    session,
                    action="refresh",
                    outcome=outcome,
                    context=context,
                    occurred_at=now,
                    user_id=current.user_id,
                    refresh_session_id=current.id,
                    reason_code=reason_code,
                )
        if inactive:
            raise InactiveUserError("user account is inactive")
        if issued is None:
            raise InvalidTokenError("refresh token is invalid or expired")
        return issued

    def logout(
        self,
        refresh_token: str | None,
        *,
        context: AuthRequestContext | None = None,
    ) -> None:
        token_sha256 = _token_sha256(refresh_token) if refresh_token else None
        with self.session_factory.begin() as session:
            current = (
                session.execute(
                    select(RefreshSession)
                    .where(RefreshSession.token_sha256 == token_sha256)
                    .with_for_update()
                ).scalar_one_or_none()
                if token_sha256 is not None
                else None
            )
            now = _database_now(session)
            session_was_active = current is not None and current.revoked_at is None
            if session_was_active:
                current.revoked_at = now
            if current is not None:
                self.security.add_event(
                    session,
                    action="logout",
                    outcome="success",
                    context=context,
                    occurred_at=now,
                    user_id=current.user_id,
                    refresh_session_id=current.id,
                    reason_code=None if session_was_active else "session_not_active",
                )

    def current_user(self, access_token: str) -> UserIdentity:
        user_id = self._decode_access_token(access_token)
        with self.session_factory() as session:
            user = session.get(User, user_id)
            if user is None:
                raise InvalidTokenError("access token subject does not exist")
            if not user.is_active:
                raise InactiveUserError("user account is inactive")
            return _user_identity(user)

    def request_email_verification(
        self,
        *,
        user_id: uuid.UUID,
        context: AuthRequestContext | None = None,
    ) -> PendingAuthEmail | None:
        pending: PendingAuthEmail | None = None
        retry_after: int | None = None
        inactive = False
        with self.session_factory.begin() as session:
            user = session.execute(
                select(User).where(User.id == user_id).with_for_update()
            ).scalar_one_or_none()
            if user is None or not user.is_active:
                inactive = True
            else:
                now = _database_now(session)
                keys = self.security.action_request_keys(
                    purpose="email_verification",
                    identity_subject=f"user:{user.id}",
                    client_ip_fingerprint=self.security.client_ip_fingerprint(
                        context
                    ),
                )
                retry_after = self.security.consume_action_request(
                    session,
                    keys,
                    now=now,
                )
                if retry_after is not None:
                    self.security.add_event(
                        session,
                        action="email_verification",
                        outcome="blocked",
                        context=context,
                        occurred_at=now,
                        user_id=user.id,
                        identifier_fingerprint=self.security.identifier_fingerprint(
                            user.email
                        ),
                        reason_code="rate_limited",
                    )
                elif user.email_verified:
                    self.security.add_event(
                        session,
                        action="email_verification",
                        outcome="success",
                        context=context,
                        occurred_at=now,
                        user_id=user.id,
                        identifier_fingerprint=self.security.identifier_fingerprint(
                            user.email
                        ),
                        reason_code="already_verified",
                    )
                else:
                    pending = self._issue_action_token(
                        session,
                        user,
                        purpose="email_verification",
                        now=now,
                        ttl=timedelta(
                            hours=self.settings.email_verification_ttl_hours
                        ),
                    )
        if inactive:
            raise InactiveUserError("user account is inactive")
        if retry_after is not None:
            raise RateLimitExceededError(retry_after)
        return pending

    def confirm_email_verification(
        self,
        token: str,
        *,
        context: AuthRequestContext | None = None,
    ) -> UserIdentity:
        token_sha256 = _token_sha256(token)
        verified_user: User | None = None
        invalid = False
        with self.session_factory.begin() as session:
            action_token = session.execute(
                select(AuthActionToken)
                .where(
                    AuthActionToken.token_sha256 == token_sha256,
                    AuthActionToken.purpose == "email_verification",
                )
                .with_for_update()
            ).scalar_one_or_none()
            now = _database_now(session)
            if not _action_token_is_active(action_token, now):
                invalid = True
                self._expire_known_action_token(
                    session,
                    action_token,
                    now=now,
                    context=context,
                )
            else:
                assert action_token is not None
                user = session.execute(
                    select(User)
                    .where(User.id == action_token.user_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if user is None or not user.is_active:
                    action_token.revoked_at = now
                    invalid = True
                else:
                    user.email_verified = True
                    user.updated_at = now
                    action_token.consumed_at = now
                    self._revoke_other_action_tokens(
                        session,
                        user_id=user.id,
                        purpose="email_verification",
                        keep_id=action_token.id,
                        now=now,
                    )
                    self.security.add_event(
                        session,
                        action="email_verification",
                        outcome="success",
                        context=context,
                        occurred_at=now,
                        user_id=user.id,
                        identifier_fingerprint=self.security.identifier_fingerprint(
                            user.email
                        ),
                        reason_code="confirmed",
                    )
                    verified_user = user
        if invalid or verified_user is None:
            raise InvalidActionTokenError(
                "email verification token is invalid or expired"
            )
        return _user_identity(verified_user)

    def request_password_reset(
        self,
        *,
        email: str,
        context: AuthRequestContext | None = None,
    ) -> PendingAuthEmail | None:
        normalized_email = _normalize_email(email)
        pending: PendingAuthEmail | None = None
        with self.session_factory.begin() as session:
            now = _database_now(session)
            keys = self.security.action_request_keys(
                purpose="password_reset",
                identity_subject=f"email:{normalized_email}",
                client_ip_fingerprint=self.security.client_ip_fingerprint(context),
            )
            retry_after = self.security.consume_action_request(
                session,
                keys,
                now=now,
            )
            if retry_after is not None:
                return None
            user = session.execute(
                select(User).where(User.email == normalized_email).with_for_update()
            ).scalar_one_or_none()
            if user is not None and user.is_active:
                pending = self._issue_action_token(
                    session,
                    user,
                    purpose="password_reset",
                    now=now,
                    ttl=timedelta(
                        minutes=self.settings.password_reset_ttl_minutes
                    ),
                )
        return pending

    def confirm_password_reset(
        self,
        *,
        token: str,
        new_password: str,
        context: AuthRequestContext | None = None,
    ) -> None:
        _validate_password(new_password)
        token_sha256 = _token_sha256(token)
        with self.session_factory() as session:
            candidate = session.execute(
                select(AuthActionToken).where(
                    AuthActionToken.token_sha256 == token_sha256,
                    AuthActionToken.purpose == "password_reset",
                )
            ).scalar_one_or_none()
            now = _database_now(session)
            if not _action_token_is_active(candidate, now):
                raise InvalidActionTokenError(
                    "password reset token is invalid or expired"
                )
        password_hash = PASSWORD_HASH.hash(new_password)

        invalid = False
        with self.session_factory.begin() as session:
            action_token = session.execute(
                select(AuthActionToken)
                .where(
                    AuthActionToken.token_sha256 == token_sha256,
                    AuthActionToken.purpose == "password_reset",
                )
                .with_for_update()
            ).scalar_one_or_none()
            now = _database_now(session)
            if not _action_token_is_active(action_token, now):
                invalid = True
                self._expire_known_action_token(
                    session,
                    action_token,
                    now=now,
                    context=context,
                )
            else:
                assert action_token is not None
                user = session.execute(
                    select(User)
                    .where(User.id == action_token.user_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if user is None or not user.is_active:
                    action_token.revoked_at = now
                    invalid = True
                else:
                    user.password_hash = password_hash
                    user.email_verified = True
                    user.updated_at = now
                    action_token.consumed_at = now
                    self._revoke_other_action_tokens(
                        session,
                        user_id=user.id,
                        purpose="password_reset",
                        keep_id=action_token.id,
                        now=now,
                    )
                    session.execute(
                        update(RefreshSession)
                        .where(
                            RefreshSession.user_id == user.id,
                            RefreshSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
                    self.security.add_event(
                        session,
                        action="password_reset",
                        outcome="success",
                        context=context,
                        occurred_at=now,
                        user_id=user.id,
                        identifier_fingerprint=self.security.identifier_fingerprint(
                            user.email
                        ),
                        reason_code="confirmed",
                    )
        if invalid:
            raise InvalidActionTokenError(
                "password reset token is invalid or expired"
            )

    def deliver_auth_email(
        self,
        pending: PendingAuthEmail,
        *,
        context: AuthRequestContext | None = None,
    ) -> None:
        try:
            if pending.purpose == "email_verification":
                self.email_sender.send_email_verification(
                    recipient=pending.recipient,
                    token=pending.token,
                )
            elif pending.purpose == "password_reset":
                self.email_sender.send_password_reset(
                    recipient=pending.recipient,
                    token=pending.token,
                )
            else:
                raise ValueError(f"unsupported authentication email: {pending.purpose}")
        except EmailDeliveryError:
            self._finish_auth_email_delivery(
                pending,
                delivered=False,
                context=context,
            )
            LOGGER.exception(
                "Authentication email delivery failed for purpose %s",
                pending.purpose,
            )
            return
        self._finish_auth_email_delivery(
            pending,
            delivered=True,
            context=context,
        )

    def _issue_action_token(
        self,
        session: Session,
        user: User,
        *,
        purpose: str,
        now: datetime,
        ttl: timedelta,
    ) -> PendingAuthEmail:
        session.execute(
            update(AuthActionToken)
            .where(
                AuthActionToken.user_id == user.id,
                AuthActionToken.purpose == purpose,
                AuthActionToken.consumed_at.is_(None),
                AuthActionToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        raw_token = secrets.token_urlsafe(48)
        token_id = uuid.uuid4()
        session.add(
            AuthActionToken(
                id=token_id,
                user_id=user.id,
                purpose=purpose,
                token_sha256=_token_sha256(raw_token),
                created_at=now,
                expires_at=now + ttl,
            )
        )
        return PendingAuthEmail(
            purpose=purpose,
            token_id=token_id,
            user_id=user.id,
            recipient=user.email,
            token=raw_token,
        )

    def _finish_auth_email_delivery(
        self,
        pending: PendingAuthEmail,
        *,
        delivered: bool,
        context: AuthRequestContext | None,
    ) -> None:
        with self.session_factory.begin() as session:
            action_token = session.execute(
                select(AuthActionToken)
                .where(AuthActionToken.id == pending.token_id)
                .with_for_update()
            ).scalar_one_or_none()
            if action_token is None:
                return
            now = _database_now(session)
            if not delivered and action_token.consumed_at is None:
                action_token.revoked_at = now
            self.security.add_event(
                session,
                action=pending.purpose,
                outcome="success" if delivered else "failure",
                context=context,
                occurred_at=now,
                user_id=pending.user_id,
                identifier_fingerprint=self.security.identifier_fingerprint(
                    pending.recipient
                ),
                reason_code="delivered" if delivered else "delivery_failed",
            )

    def _expire_known_action_token(
        self,
        session: Session,
        action_token: AuthActionToken | None,
        *,
        now: datetime,
        context: AuthRequestContext | None,
    ) -> None:
        if (
            action_token is None
            or action_token.consumed_at is not None
            or action_token.revoked_at is not None
        ):
            return
        if _as_utc(action_token.expires_at) > now:
            return
        action_token.revoked_at = now
        self.security.add_event(
            session,
            action=action_token.purpose,
            outcome="failure",
            context=context,
            occurred_at=now,
            user_id=action_token.user_id,
            reason_code="expired",
        )

    @staticmethod
    def _revoke_other_action_tokens(
        session: Session,
        *,
        user_id: uuid.UUID,
        purpose: str,
        keep_id: uuid.UUID,
        now: datetime,
    ) -> None:
        session.execute(
            update(AuthActionToken)
            .where(
                AuthActionToken.user_id == user_id,
                AuthActionToken.purpose == purpose,
                AuthActionToken.id != keep_id,
                AuthActionToken.consumed_at.is_(None),
                AuthActionToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    def _record_login_failure(
        self,
        throttle_keys: tuple[LoginThrottleKey, ...],
        *,
        context: AuthRequestContext | None,
        identifier_fingerprint: str,
        user_id: uuid.UUID | None,
        reason_code: str,
    ) -> None:
        with self.session_factory.begin() as session:
            now = _database_now(session)
            self.security.record_login_failure(
                session,
                throttle_keys,
                now=now,
            )
            self.security.add_event(
                session,
                action="login",
                outcome="failure",
                context=context,
                occurred_at=now,
                user_id=user_id,
                identifier_fingerprint=identifier_fingerprint,
                reason_code=reason_code,
            )

    def _record_event(
        self,
        *,
        action: str,
        outcome: str,
        context: AuthRequestContext | None,
        identifier: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            self.security.add_event(
                session,
                action=action,
                outcome=outcome,
                context=context,
                occurred_at=_database_now(session),
                identifier_fingerprint=(
                    self.security.identifier_fingerprint(identifier)
                    if identifier
                    else None
                ),
                reason_code=reason_code,
            )

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


def _action_token_is_active(
    action_token: AuthActionToken | None,
    now: datetime,
) -> bool:
    return (
        action_token is not None
        and action_token.consumed_at is None
        and action_token.revoked_at is None
        and _as_utc(action_token.expires_at) > now
    )


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
