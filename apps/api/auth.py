from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, SecretStr

from backend.auth import (
    AuthService,
    AuthRequestContext,
    DuplicateIdentityError,
    InactiveUserError,
    InvalidActionTokenError,
    InvalidCredentialsError,
    InvalidTokenError,
    IssuedTokens,
    RateLimitExceededError,
    UserIdentity,
    resolve_client_ip,
)


CurrentUserDependency = Callable[..., UserIdentity]
VerifiedUserDependency = Callable[..., UserIdentity]


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: EmailStr
    password: SecretStr = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128)


class ActionTokenRequest(BaseModel):
    token: SecretStr = Field(min_length=32, max_length=512)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirmRequest(ActionTokenRequest):
    new_password: SecretStr = Field(min_length=12, max_length=128)


class AcceptedResponse(BaseModel):
    detail: str


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    role: str
    is_active: bool
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def from_identity(cls, user: UserIdentity) -> "UserResponse":
        return cls(
            id=str(user.id),
            username=user.username,
            email=user.email,
            role=user.role,
            is_active=user.is_active,
            email_verified=user.email_verified,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserResponse


def create_auth_router(
    service: AuthService,
) -> tuple[APIRouter, CurrentUserDependency, VerifiedUserDependency]:
    router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
    bearer = HTTPBearer(auto_error=False)

    def current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> UserIdentity:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _authentication_error()
        try:
            return service.current_user(credentials.credentials)
        except InvalidTokenError as exc:
            raise _authentication_error() from exc
        except InactiveUserError as exc:
            raise HTTPException(status_code=403, detail="user account is inactive") from exc

    def verified_user(
        user: UserIdentity = Depends(current_user),
    ) -> UserIdentity:
        if not user.email_verified:
            raise HTTPException(
                status_code=403,
                detail="email verification is required",
            )
        return user

    @router.post(
        "/register",
        response_model=UserResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def register(payload: RegisterRequest, request: Request) -> UserResponse:
        try:
            user = service.register(
                username=payload.username,
                email=str(payload.email),
                password=payload.password.get_secret_value(),
                context=_request_context(request, service),
            )
        except RateLimitExceededError as exc:
            raise HTTPException(
                status_code=429,
                detail="too many registration attempts; try again later",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except DuplicateIdentityError as exc:
            raise HTTPException(
                status_code=409,
                detail="username or email is already registered",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return UserResponse.from_identity(user)

    @router.post("/login", response_model=TokenResponse)
    def login(
        payload: LoginRequest,
        request: Request,
        response: Response,
    ) -> TokenResponse:
        try:
            issued = service.login(
                identifier=payload.identifier,
                password=payload.password.get_secret_value(),
                context=_request_context(request, service),
            )
        except RateLimitExceededError as exc:
            raise HTTPException(
                status_code=429,
                detail="too many login attempts; try again later",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except InvalidCredentialsError as exc:
            raise HTTPException(
                status_code=401,
                detail="invalid username/email or password",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except InactiveUserError as exc:
            raise HTTPException(status_code=403, detail="user account is inactive") from exc
        _set_refresh_cookie(response, service, issued)
        _set_no_store(response)
        return _token_response(issued)

    @router.post(
        "/email-verification/request",
        response_model=AcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def request_email_verification(
        request: Request,
        background_tasks: BackgroundTasks,
        user: UserIdentity = Depends(current_user),
    ) -> AcceptedResponse:
        context = _request_context(request, service)
        try:
            pending = service.request_email_verification(
                user_id=user.id,
                context=context,
            )
        except RateLimitExceededError as exc:
            raise HTTPException(
                status_code=429,
                detail="too many email verification requests; try again later",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except InactiveUserError as exc:
            raise HTTPException(status_code=403, detail="user account is inactive") from exc
        if pending is not None:
            background_tasks.add_task(
                service.deliver_auth_email,
                pending,
                context=context,
            )
        return AcceptedResponse(
            detail="email verification request accepted",
        )

    @router.post(
        "/email-verification/confirm",
        response_model=UserResponse,
    )
    def confirm_email_verification(
        payload: ActionTokenRequest,
        request: Request,
    ) -> UserResponse:
        try:
            user = service.confirm_email_verification(
                payload.token.get_secret_value(),
                context=_request_context(request, service),
            )
        except InvalidActionTokenError as exc:
            raise HTTPException(
                status_code=400,
                detail="email verification token is invalid or expired",
            ) from exc
        return UserResponse.from_identity(user)

    @router.post(
        "/password-reset/request",
        response_model=AcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def request_password_reset(
        payload: PasswordResetRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> AcceptedResponse:
        context = _request_context(request, service)
        pending = service.request_password_reset(
            email=str(payload.email),
            context=context,
        )
        if pending is not None:
            background_tasks.add_task(
                service.deliver_auth_email,
                pending,
                context=context,
            )
        return AcceptedResponse(
            detail="if the account exists, password reset instructions will be sent",
        )

    @router.post(
        "/password-reset/confirm",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def confirm_password_reset(
        payload: PasswordResetConfirmRequest,
        request: Request,
        response: Response,
    ) -> None:
        try:
            service.confirm_password_reset(
                token=payload.token.get_secret_value(),
                new_password=payload.new_password.get_secret_value(),
                context=_request_context(request, service),
            )
        except InvalidActionTokenError as exc:
            raise HTTPException(
                status_code=400,
                detail="password reset token is invalid or expired",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _delete_refresh_cookie(response, service)

    @router.post("/refresh", response_model=TokenResponse)
    def refresh(
        request: Request,
        response: Response,
        refresh_token: str | None = Cookie(
            default=None,
            alias=service.settings.refresh_cookie_name,
        ),
    ) -> TokenResponse:
        try:
            issued = service.refresh(
                refresh_token,
                context=_request_context(request, service),
            )
        except InvalidTokenError as exc:
            raise _authentication_error("refresh token is invalid or expired") from exc
        except InactiveUserError as exc:
            raise HTTPException(status_code=403, detail="user account is inactive") from exc
        _set_refresh_cookie(response, service, issued)
        _set_no_store(response)
        return _token_response(issued)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        request: Request,
        response: Response,
        refresh_token: str | None = Cookie(
            default=None,
            alias=service.settings.refresh_cookie_name,
        ),
    ) -> None:
        service.logout(
            refresh_token,
            context=_request_context(request, service),
        )
        _delete_refresh_cookie(response, service)
        _set_no_store(response)

    @router.get("/me", response_model=UserResponse)
    def me(user: UserIdentity = Depends(current_user)) -> UserResponse:
        return UserResponse.from_identity(user)

    return router, current_user, verified_user


def _token_response(issued: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.access_expires_in,
        user=UserResponse.from_identity(issued.user),
    )


def _set_refresh_cookie(
    response: Response,
    service: AuthService,
    issued: IssuedTokens,
) -> None:
    remaining = issued.refresh_expires_at - datetime.now(timezone.utc)
    response.set_cookie(
        key=service.settings.refresh_cookie_name,
        value=issued.refresh_token,
        max_age=max(0, int(remaining.total_seconds())),
        expires=issued.refresh_expires_at,
        path="/api/v1/auth",
        secure=service.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _delete_refresh_cookie(response: Response, service: AuthService) -> None:
    response.delete_cookie(
        key=service.settings.refresh_cookie_name,
        path="/api/v1/auth",
        secure=service.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _authentication_error(detail: str = "could not validate credentials") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _request_context(request: Request, service: AuthService) -> AuthRequestContext:
    peer = request.client.host if request.client is not None else None
    user_agent = request.headers.get("user-agent")
    return AuthRequestContext(
        client_ip=resolve_client_ip(
            peer=peer,
            forwarded_for=request.headers.get("x-forwarded-for"),
            trusted_proxy_cidrs=service.settings.trusted_proxy_cidrs,
        ),
        user_agent=user_agent[:512] if user_agent else None,
    )
