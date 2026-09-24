from __future__ import annotations

import threading
from types import TracebackType

from .queue import JobClaim, JobQueue, LeaseHeartbeat


class LeaseHeartbeatLoop:
    """Renew a claim in the background and surface lease loss to the worker."""

    def __init__(
        self,
        queue: JobQueue,
        claim: JobClaim,
        *,
        interval_seconds: float = 20,
        lease_seconds: int = 60,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if interval_seconds * 2 >= lease_seconds:
            raise ValueError("heartbeat interval must be less than half the lease TTL")
        self.queue = queue
        self.claim = claim
        self.interval_seconds = interval_seconds
        self.lease_seconds = lease_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._latest: LeaseHeartbeat | None = None
        self._cancel_requested = threading.Event()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    @property
    def latest(self) -> LeaseHeartbeat | None:
        return self._latest

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat loop has already been started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self.claim.job_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
            if self._thread.is_alive():
                raise RuntimeError("heartbeat thread did not stop")

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def __enter__(self) -> "LeaseHeartbeatLoop":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.stop()
        if exc_type is None:
            self.raise_if_failed()
        return False

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                heartbeat = self.queue.heartbeat(
                    self.claim,
                    lease_seconds=self.lease_seconds,
                )
            except BaseException as exc:  # propagated by raise_if_failed
                self._failure = exc
                self._stop_event.set()
                return
            self._latest = heartbeat
            if heartbeat.cancel_requested:
                self._cancel_requested.set()
