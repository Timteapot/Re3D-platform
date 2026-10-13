from __future__ import annotations

import math
import os
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Callable

from backend.re3d_adapter.errors import PipelineCancelled


GIB = 1024**3


@dataclass(frozen=True)
class DiskGuardSettings:
    """Opt-in runtime protection, separate from nonfatal resource telemetry."""

    enabled: bool = False
    minimum_free_bytes: int = 0
    interval_seconds: float = 1

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("disk guard enabled must be a boolean")
        if (
            not isinstance(self.minimum_free_bytes, int)
            or isinstance(self.minimum_free_bytes, bool)
            or self.minimum_free_bytes < 0
        ):
            raise ValueError("disk guard minimum_free_bytes must be a non-negative integer")
        if self.enabled and self.minimum_free_bytes < GIB:
            raise ValueError("enabled disk guard requires at least 1 GiB of free space")
        if (
            not isinstance(self.interval_seconds, (int, float))
            or isinstance(self.interval_seconds, bool)
            or not math.isfinite(self.interval_seconds)
            or not 1 <= self.interval_seconds <= 5
        ):
            raise ValueError("disk guard interval_seconds must be between 1 and 5")

    @classmethod
    def from_environment(cls) -> "DiskGuardSettings":
        enabled = os.environ.get("RE3D_LOW_DISK_GUARD_ENABLED", "false").strip().lower()
        if enabled not in {"true", "false"}:
            raise ValueError("RE3D_LOW_DISK_GUARD_ENABLED must be true or false")
        return cls(
            enabled=enabled == "true",
            minimum_free_bytes=int(os.environ.get("RE3D_MIN_FREE_DISK_BYTES", "0")),
            interval_seconds=float(
                os.environ.get("RE3D_LOW_DISK_GUARD_INTERVAL_SECONDS", "1")
            ),
        )

    def as_dict(self) -> dict[str, bool | int | float]:
        return asdict(self)


@dataclass(frozen=True)
class DiskGuardTrip:
    code: str
    minimum_free_bytes: int
    observed_free_bytes: int | None

    @property
    def safe_message(self) -> str:
        if self.code == "LOW_DISK_AUTO_CANCELLED":
            return "数据盘空闲空间已达到安全线，任务已自动停止。"
        return "无法验证数据盘空闲空间，任务已自动停止以保护存储。"

    def metrics(self) -> dict[str, int | None]:
        return {
            "minimum_free_bytes": self.minimum_free_bytes,
            "observed_free_bytes": self.observed_free_bytes,
        }


class DiskGuardCancelled(PipelineCancelled):
    def __init__(self, trip: DiskGuardTrip) -> None:
        super().__init__(trip.safe_message)
        self.trip = trip


def read_disk_free_bytes(data_root: Path) -> int:
    """Read only volume free space; no directory walk, GPU probe or telemetry lock."""
    return shutil.disk_usage(data_root).free


class DiskGuard:
    """Sticky cancellation signal supervised by its own lightweight thread.

    A healthy final check is a sampled boundary, not a filesystem quota or an
    absolute guarantee against consuming the remaining space between samples.
    """

    def __init__(
        self,
        data_root: Path,
        *,
        settings: DiskGuardSettings,
        free_bytes_reader: Callable[[Path], int] = read_disk_free_bytes,
    ) -> None:
        self.data_root = data_root
        self.settings = settings
        self.free_bytes_reader = free_bytes_reader
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._trip: DiskGuardTrip | None = None
        self._started = False

    @property
    def trip(self) -> DiskGuardTrip | None:
        with self._lock:
            return self._trip

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._started:
            raise RuntimeError("disk guard has already been started")
        self._started = True
        if not self.settings.enabled:
            return
        # Fail before launching the pipeline, not after the first sample interval.
        self.check_now()
        self._thread = threading.Thread(
            target=self._run,
            name="re3d-low-disk-guard",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.settings.interval_seconds * 2))
            if self._thread.is_alive():
                self._set_trip("DISK_GUARD_PROBE_FAILED", None)
                self.raise_if_tripped()

    def finish(self) -> None:
        """Stop sampling, then synchronously seal the final healthy boundary."""
        self.stop()
        self.check_now()

    def raise_if_tripped(self) -> None:
        # No disk I/O here: this is called by the process supervisor every 250 ms.
        trip = self.trip
        if trip is not None:
            raise DiskGuardCancelled(trip)

    def check_now(self) -> None:
        if not self.settings.enabled:
            return
        self.raise_if_tripped()
        self._probe()
        self.raise_if_tripped()

    def __enter__(self) -> "DiskGuard":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            self.stop()
        except DiskGuardCancelled:
            if exc_type is None:
                raise
            # Preserve an existing lease-loss or user-interrupt exception. The
            # sticky trip remains available to the owned worker's error handler.
        return False

    def _set_trip(self, code: str, free_bytes: int | None) -> None:
        with self._lock:
            if self._trip is None:
                self._trip = DiskGuardTrip(
                    code,
                    self.settings.minimum_free_bytes,
                    free_bytes,
                )

    def _probe(self) -> None:
        with self._probe_lock:
            if self.trip is not None:
                return
            try:
                free_bytes = self.free_bytes_reader(self.data_root)
                if (
                    not isinstance(free_bytes, int)
                    or isinstance(free_bytes, bool)
                    or free_bytes < 0
                ):
                    raise ValueError("invalid volume free space sample")
            except Exception:
                # The exception/path can contain private information; retain no text.
                self._set_trip("DISK_GUARD_PROBE_FAILED", None)
                return
            if free_bytes <= self.settings.minimum_free_bytes:
                self._set_trip("LOW_DISK_AUTO_CANCELLED", free_bytes)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.settings.interval_seconds):
                self._probe()
                if self.trip is not None:
                    return
        except BaseException:
            self._set_trip("DISK_GUARD_PROBE_FAILED", None)
        finally:
            if not self._stop.is_set() and self.trip is None:
                self._set_trip("DISK_GUARD_PROBE_FAILED", None)
