from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    HTTPException,
    Response,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, SecretStr

from backend.auth import (
    AuthService,
    DuplicateIdentityError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
    IssuedTokens,
    UserIdentity,
)


CurrentUserDependency = Callable[..., UserIdentity]


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: EmailStr
    password: SecretStr = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128)


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
) -> tuple[APIRouter, CurrentUserDependency]:
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

    @router.post(
        "/register",
        response_model=UserResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def register(payload: RegisterRequest) -> UserResponse:
        try:
            user = service.register(
                username=payload.username,
                email=str(payload.email),
                password=payload.password.get_secret_value(),
            )
        except DuplicateIdentityError as exc:
            raise HTTPException(
                status_code=409,
                detail="username or email is already registered",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return UserResponse.from_identity(user)

    @router.post("/login", response_model=TokenResponse)
    def login(payload: LoginRequest, response: Response) -> TokenResponse:
        try:
            issued = service.login(
                identifier=payload.identifier,
                password=payload.password.get_secret_value(),
            )
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

    @router.post("/refresh", response_model=TokenResponse)
    def refresh(
        response: Response,
        refresh_token: str | None = Cookie(
            default=None,
            alias=service.settings.refresh_cookie_name,
        ),
    ) -> TokenResponse:
        if refresh_token is None:
            raise _authentication_error("refresh token is missing")
        try:
            issued = service.refresh(refresh_token)
        except InvalidTokenError as exc:
            raise _authentication_error("refresh token is invalid or expired") from exc
        except InactiveUserError as exc:
            raise HTTPException(status_code=403, detail="user account is inactive") from exc
        _set_refresh_cookie(response, service, issued)
        _set_no_store(response)
        return _token_response(issued)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        response: Response,
        refresh_token: str | None = Cookie(
            default=None,
            alias=service.settings.refresh_cookie_name,
        ),
    ) -> None:
        service.logout(refresh_token)
        response.delete_cookie(
            key=service.settings.refresh_cookie_name,
            path="/api/v1/auth",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        _set_no_store(response)

    @router.get("/me", response_model=UserResponse)
    def me(user: UserIdentity = Depends(current_user)) -> UserResponse:
        return UserResponse.from_identity(user)

    return router, current_user


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


def _authentication_error(detail: str = "could not validate credentials") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
