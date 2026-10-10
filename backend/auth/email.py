from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol
from urllib.parse import quote, urlsplit

from backend.environment import (
    DEVELOPMENT_ENVIRONMENTS,
    PRODUCTION_ENVIRONMENT,
    RESTRICTED_ENVIRONMENT,
    is_loopback_host,
    normalize_environment,
)

from .errors import EmailDeliveryError


@dataclass(frozen=True)
class AuthEmailSettings:
    host: str | None
    port: int = 1025
    sender: str = "no-reply@example.invalid"
    username: str | None = None
    password: str | None = None
    starttls: bool = False
    timeout_seconds: int = 10
    public_base_url: str = "http://localhost:5173"

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("SMTP_TIMEOUT_SECONDS must be between 1 and 60")
        if (self.username is None) != (self.password is None):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together")
        _, parsed_sender = parseaddr(self.sender)
        if not parsed_sender or "@" not in parsed_sender:
            raise ValueError("SMTP_FROM must be a valid mailbox")
        parsed_url = urlsplit(self.public_base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("APP_PUBLIC_BASE_URL must be an HTTP(S) origin or base path")

    @classmethod
    def from_environment(cls, *, environment: str) -> "AuthEmailSettings":
        environment = normalize_environment(environment)
        host = os.environ.get("SMTP_HOST", "").strip() or None
        username = os.environ.get("SMTP_USERNAME", "").strip() or None
        password = os.environ.get("SMTP_PASSWORD", "") or None
        settings = cls(
            host=host,
            port=_environment_integer("SMTP_PORT", 1025),
            sender=os.environ.get("SMTP_FROM", "no-reply@example.invalid"),
            username=username,
            password=password,
            starttls=_environment_bool("SMTP_STARTTLS", False),
            timeout_seconds=_environment_integer("SMTP_TIMEOUT_SECONDS", 10),
            public_base_url=os.environ.get(
                "APP_PUBLIC_BASE_URL",
                "http://localhost:5173",
            ).rstrip("/"),
        )
        if environment not in DEVELOPMENT_ENVIRONMENTS:
            if settings.host is None:
                raise ValueError("SMTP_HOST is required outside development")
        if environment == PRODUCTION_ENVIRONMENT:
            if urlsplit(settings.public_base_url).scheme != "https":
                raise ValueError("APP_PUBLIC_BASE_URL must use HTTPS outside development")
            if not settings.starttls:
                raise ValueError("SMTP_STARTTLS must be true outside development")
            if parseaddr(settings.sender)[1].lower().endswith(".invalid"):
                raise ValueError("SMTP_FROM must not use an .invalid domain outside development")
        elif environment == RESTRICTED_ENVIRONMENT:
            parsed_url = urlsplit(settings.public_base_url)
            if (
                not is_loopback_host(parsed_url.hostname)
                or parsed_url.path not in {"", "/"}
            ):
                raise ValueError(
                    "restricted APP_PUBLIC_BASE_URL must be a loopback origin"
                )
            if not is_loopback_host(settings.host):
                raise ValueError("restricted SMTP_HOST must be a loopback host")
            if parseaddr(settings.sender)[1].lower().endswith(".invalid"):
                raise ValueError(
                    "restricted SMTP_FROM must not use an .invalid domain"
                )
        return settings


class AuthEmailSender(Protocol):
    def send_email_verification(self, *, recipient: str, token: str) -> None: ...

    def send_password_reset(self, *, recipient: str, token: str) -> None: ...


class DisabledAuthEmailSender:
    def send_email_verification(self, *, recipient: str, token: str) -> None:
        raise EmailDeliveryError("email delivery is not configured")

    def send_password_reset(self, *, recipient: str, token: str) -> None:
        raise EmailDeliveryError("email delivery is not configured")


class SmtpAuthEmailSender:
    def __init__(self, settings: AuthEmailSettings) -> None:
        if settings.host is None:
            raise ValueError("SMTP_HOST is required for SMTP delivery")
        self.settings = settings

    def send_email_verification(self, *, recipient: str, token: str) -> None:
        link = _fragment_link(
            self.settings.public_base_url,
            "/auth/verify-email",
            token,
        )
        self._send(
            recipient=recipient,
            subject="验证你的 Re3D Platform 邮箱",
            body=(
                "请在浏览器中打开以下链接完成邮箱验证：\n\n"
                f"{link}\n\n"
                "如果不是你发起的请求，可以忽略本邮件。"
            ),
        )

    def send_password_reset(self, *, recipient: str, token: str) -> None:
        link = _fragment_link(
            self.settings.public_base_url,
            "/auth/reset-password",
            token,
        )
        self._send(
            recipient=recipient,
            subject="重置你的 Re3D Platform 密码",
            body=(
                "请在浏览器中打开以下链接设置新密码：\n\n"
                f"{link}\n\n"
                "如果不是你发起的请求，可以忽略本邮件。"
            ),
        )

    def _send(self, *, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout_seconds,
            ) as smtp:
                if self.settings.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.settings.username is not None:
                    assert self.settings.password is not None
                    smtp.login(self.settings.username, self.settings.password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryError("authentication email delivery failed") from exc


def build_auth_email_sender(settings: AuthEmailSettings) -> AuthEmailSender:
    if settings.host is None:
        return DisabledAuthEmailSender()
    return SmtpAuthEmailSender(settings)


def _fragment_link(base_url: str, path: str, token: str) -> str:
    return f"{base_url.rstrip('/')}{path}#token={quote(token, safe='')}"


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")
