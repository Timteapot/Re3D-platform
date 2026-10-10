from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from backend.environment import requires_explicit_operational_settings

TransferDirection = Literal["upload", "download"]


@dataclass(frozen=True)
class TransferLimitSettings:
    window_seconds: int = 3600
    upload_max_concurrent: int = 2
    upload_max_bytes: int = 2 * 1024**3
    download_max_concurrent: int = 3
    download_max_bytes: int = 4 * 1024**3
    lease_seconds: int = 4 * 60 * 60

    def __post_init__(self) -> None:
        if not 60 <= self.window_seconds <= 24 * 60 * 60:
            raise ValueError(
                "TRANSFER_WINDOW_SECONDS must be between 60 and 86400"
            )
        for name, value in (
            ("TRANSFER_UPLOAD_MAX_CONCURRENT", self.upload_max_concurrent),
            ("TRANSFER_DOWNLOAD_MAX_CONCURRENT", self.download_max_concurrent),
        ):
            if not 1 <= value <= 32:
                raise ValueError(f"{name} must be between 1 and 32")
        for name, value in (
            ("TRANSFER_UPLOAD_MAX_BYTES", self.upload_max_bytes),
            ("TRANSFER_DOWNLOAD_MAX_BYTES", self.download_max_bytes),
        ):
            if not 1 <= value <= 10 * 1024**4:
                raise ValueError(f"{name} must be between 1 byte and 10 TiB")
        if not 60 <= self.lease_seconds <= 24 * 60 * 60:
            raise ValueError(
                "TRANSFER_LEASE_SECONDS must be between 60 and 86400"
            )

    @classmethod
    def from_environment(
        cls,
        *,
        environment: str,
    ) -> "TransferLimitSettings":
        explicit = requires_explicit_operational_settings(environment)
        return cls(
            window_seconds=_environment_integer(
                "TRANSFER_WINDOW_SECONDS",
                3600,
                required=explicit,
            ),
            upload_max_concurrent=_environment_integer(
                "TRANSFER_UPLOAD_MAX_CONCURRENT",
                2,
                required=explicit,
            ),
            upload_max_bytes=_environment_integer(
                "TRANSFER_UPLOAD_MAX_BYTES",
                2 * 1024**3,
                required=explicit,
            ),
            download_max_concurrent=_environment_integer(
                "TRANSFER_DOWNLOAD_MAX_CONCURRENT",
                3,
                required=explicit,
            ),
            download_max_bytes=_environment_integer(
                "TRANSFER_DOWNLOAD_MAX_BYTES",
                4 * 1024**3,
                required=explicit,
            ),
            lease_seconds=_environment_integer(
                "TRANSFER_LEASE_SECONDS",
                4 * 60 * 60,
                required=explicit,
            ),
        )

    def max_concurrent(self, direction: TransferDirection) -> int:
        return (
            self.upload_max_concurrent
            if direction == "upload"
            else self.download_max_concurrent
        )

    def max_bytes(self, direction: TransferDirection) -> int:
        return (
            self.upload_max_bytes
            if direction == "upload"
            else self.download_max_bytes
        )


def _environment_integer(name: str, default: int, *, required: bool) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        if required:
            raise ValueError(f"{name} is required outside development")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
