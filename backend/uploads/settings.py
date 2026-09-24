from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class UploadSettings:
    min_images: int = 3
    max_images: int = 150
    max_file_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_pixels: int = 50_000_000
    stale_after_hours: int = 24
    cleanup_batch_size: int = 100

    def __post_init__(self) -> None:
        if not 3 <= self.min_images <= self.max_images <= 150:
            raise ValueError("upload image limits must satisfy 3 <= min <= max <= 150")
        if not 1024 <= self.max_file_bytes <= self.max_total_bytes:
            raise ValueError("upload byte limits are inconsistent")
        if not 1_000_000 <= self.max_pixels <= 80_000_000:
            raise ValueError("UPLOAD_MAX_PIXELS must be between 1M and 80M")
        if not 1 <= self.stale_after_hours <= 24 * 30:
            raise ValueError("UPLOAD_STALE_AFTER_HOURS must be between 1 and 720")
        if not 1 <= self.cleanup_batch_size <= 1000:
            raise ValueError("UPLOAD_CLEANUP_BATCH_SIZE must be between 1 and 1000")

    @classmethod
    def from_environment(cls) -> "UploadSettings":
        return cls(
            max_file_bytes=_read_int(
                "UPLOAD_MAX_FILE_BYTES",
                25 * 1024 * 1024,
            ),
            max_total_bytes=_read_int(
                "UPLOAD_MAX_TOTAL_BYTES",
                1024 * 1024 * 1024,
            ),
            max_pixels=_read_int("UPLOAD_MAX_PIXELS", 50_000_000),
            stale_after_hours=_read_int("UPLOAD_STALE_AFTER_HOURS", 24),
            cleanup_batch_size=_read_int("UPLOAD_CLEANUP_BATCH_SIZE", 100),
        )


def _read_int(name: str, default: int) -> int:
    configured = os.environ.get(name)
    if configured is None:
        return default
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
