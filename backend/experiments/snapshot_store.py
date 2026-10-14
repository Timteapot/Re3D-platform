"""Bounded, asynchronous, verified SfM snapshots for independent experiments.

Only the read-only SQLite online-backup API touches a live source. A snapshot
does not become recoverable until its complete database and manifest have been
validated and hashed, and an atomic latest-valid pointer has been published.
The private parent directory and its ACL must be established by the caller.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
from typing import Callable
import uuid

from backend.experiments.checkpoint import _database as validate_database
from backend.re3d_adapter.errors import AdapterError


GIB = 1024**3
CHUNK = 1024**2
_REASON = re.compile(r"[a-z0-9_-]{1,64}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,80}\Z")


class SnapshotError(AdapterError):
    """Stable error code; neither source paths nor exception text are emitted."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _noop() -> None:
    return None


def _error_code(exc: BaseException, fallback: str) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else fallback


def _checked_path(value: Path | str, *, exists: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SnapshotError("SNAPSHOT_ABSOLUTE_PATH_REQUIRED")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if exists:
                raise SnapshotError("SNAPSHOT_PATH_MISSING") from None
            continue
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 1024):
            raise SnapshotError("SNAPSHOT_LINK_OR_REPARSE_REJECTED")
    return path


def _write_exclusive(path: Path, value: dict) -> str:
    _checked_path(path.parent)
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(encoded).hexdigest()


def _hash(path: Path, health: Callable[[], None]) -> str:
    _checked_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            health()
            block = stream.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class SnapshotStore:
    """One source, one exclusive target root, and at most one capture worker.

    ``check_health`` never probes SQLite or calls a supplied reader. A slow or
    blocked capture is detected from its monotonic deadline; stop/capture_final
    return within their budgets even when a daemon worker is blocked in I/O.
    Such a worker must not be reused, and cannot knowingly publish after cancel.
    Python cannot preempt a kernel call: stop=False means quiescence unconfirmed.
    """

    def __init__(
        self,
        root: Path | str,
        database: Path | str,
        expected_names: set[str] | tuple[str, ...] | list[str],
        health_check: Callable[[], None] = _noop,
        free_bytes_reader: Callable[[], int] | None = None,
        *,
        interval_seconds: float = 600,
        min_free_bytes: int = 20 * GIB,
        timeout_seconds: float = 120,
        validator: Callable[[Path, set[str], Callable[[], None]], int] = validate_database,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        for value in (interval_seconds, timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise SnapshotError("SNAPSHOT_INTERVAL_INVALID")
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            raise SnapshotError("SNAPSHOT_SPACE_LIMIT_INVALID")
        names = set(expected_names)
        if not names or any(not isinstance(name, str) or not name for name in names):
            raise SnapshotError("SNAPSHOT_IMAGE_NAMES_INVALID")
        proposed = _checked_path(root, exists=False)
        source = _checked_path(database)
        if not source.is_file() or proposed.exists() or not proposed.parent.is_dir():
            raise SnapshotError("SNAPSHOT_TARGET_OR_SOURCE_INVALID")
        if source.is_relative_to(proposed) or proposed.is_relative_to(source):
            raise SnapshotError("SNAPSHOT_SOURCE_TARGET_OVERLAP")
        if any(part.casefold() == "jobs" for part in proposed.parts):
            raise SnapshotError("SNAPSHOT_TARGET_INSIDE_JOBS")
        self.root, self.database = proposed, source
        self.expected_names = names
        self.health_check = health_check
        self.free_bytes_reader = free_bytes_reader or (lambda: shutil.disk_usage(self.root).free)
        self.interval_seconds = float(interval_seconds)
        self.min_free_bytes = min_free_bytes
        self.timeout_seconds = float(timeout_seconds)
        self.validator = validator
        self.monotonic = monotonic
        self.owner = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._active_at: float | None = None
        self._last_success_at: float | None = None
        self._error: str | None = None
        self._state = "prepared"
        self._attempts = self._successful = self._removed = 0
        self._latest: dict | None = None
        self._owned: list[dict] = []
        self._pointer_sha: str | None = None
        # Exclusively create after all input validation. Never reuse an earlier
        # process' target or deletion authority, even if its marker looks valid.
        proposed.mkdir()
        _write_exclusive(proposed / "store-owner.json", {
            "schema_version": "1.0", "owner_id": self.owner,
            "source_write_operations": False, "retained_valid_snapshots": 2,
        })

    @property
    def latest(self) -> dict | None:
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def _set_error(self, code: str) -> None:
        with self._lock:
            if self._error is None:
                self._error = code

    def _capture_health(self, deadline: float, health: Callable[[], None]) -> None:
        if self._cancel.is_set():
            raise SnapshotError("SNAPSHOT_CAPTURE_STOPPED")
        if self.monotonic() >= deadline:
            raise SnapshotError("SNAPSHOT_CAPTURE_TIMED_OUT")
        health()
        if self._cancel.is_set():
            raise SnapshotError("SNAPSHOT_CAPTURE_STOPPED")
        if self.monotonic() >= deadline:
            raise SnapshotError("SNAPSHOT_CAPTURE_TIMED_OUT")
        free = self.free_bytes_reader()
        if self._cancel.is_set():
            raise SnapshotError("SNAPSHOT_CAPTURE_STOPPED")
        if self.monotonic() >= deadline:
            raise SnapshotError("SNAPSHOT_CAPTURE_TIMED_OUT")
        if type(free) is not int or free < 0:
            raise SnapshotError("SNAPSHOT_SPACE_PROBE_INVALID")
        if free < self.min_free_bytes:
            raise SnapshotError("SNAPSHOT_DISK_HEADROOM_LOW")

    def _capture(self, *, reason: str, health: Callable[[], None]) -> dict:
        started = self.monotonic()
        deadline = started + self.timeout_seconds
        with self._lock:
            self._active_at = started
            self._attempts += 1
        bounded = lambda: self._capture_health(deadline, health)
        bounded()
        _checked_path(self.root)
        _checked_path(self.database)
        wal = self.database.with_name(self.database.name + "-wal")
        required = self.database.stat().st_size
        if wal.exists():
            _checked_path(wal)
            required += wal.stat().st_size
        # Do not begin a large copy when current free space cannot also preserve
        # the safety line. Further pages are checked at every backup callback.
        free = self.free_bytes_reader()
        bounded()
        if type(free) is not int or free < self.min_free_bytes + required:
            raise SnapshotError("SNAPSHOT_COPY_SPACE_INSUFFICIENT")
        token = uuid.uuid4().hex
        pending = self.root / (".pending-" + token)
        pending.mkdir()
        _write_exclusive(pending / "INCOMPLETE.json", {
            "schema_version": "1.0", "owner_id": self.owner,
            "status": "not_verified", "reason": reason,
        })
        target = pending / "database.db"
        with target.open("xb"):
            pass
        failure: list[BaseException] = []

        def sql_progress() -> int:
            try:
                bounded()
                return 0
            except BaseException as exc:
                failure.append(exc)
                return 1

        try:
            # No immutable flag on a live source: committed WAL pages must be
            # visible. query_only prohibits SQL writes by this connection.
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro&cache=private",
                                         uri=True, timeout=0.2)) as source:
                source.execute("PRAGMA query_only=ON")
                source.set_progress_handler(sql_progress, 1000)
                with closing(sqlite3.connect(target, timeout=0.2)) as destination:
                    source.backup(destination, pages=256,
                                  progress=lambda *_: bounded(), sleep=0.05)
                    destination.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            if failure:
                raise failure[0]
            raise SnapshotError("SNAPSHOT_SQLITE_BACKUP_FAILED") from None
        bounded()
        database_sha = _hash(target, bounded)
        pairs = self.validator(target, self.expected_names, bounded)
        if type(pairs) is not int or pairs < 0:
            raise SnapshotError("SNAPSHOT_VALIDATION_RESULT_INVALID")
        if _hash(target, bounded) != database_sha:
            raise SnapshotError("SNAPSHOT_DATABASE_HASH_CHANGED")
        previous = self.latest
        if previous is not None and pairs < previous["validated_complete_pair_records"]:
            raise SnapshotError("SNAPSHOT_PAIR_COUNT_REGRESSED")
        name = "snapshot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + token
        record = {
            "schema_version": "1.0", "owner_id": self.owner,
            "directory": name, "database_relative_path": name + "/database.db",
            "database_sha256": database_sha, "database_size_bytes": target.stat().st_size,
            "validated_complete_pair_records": pairs, "image_count": len(self.expected_names),
            "created_at_utc": datetime.now(timezone.utc).isoformat(), "reason": reason,
            "committed_wal_included": True, "source_write_operations": False,
            "sqlite_backup_completed": True,
            "sqlite_integrity_and_mapping_validation": True,
            "database_hash_checked_before_and_after_validation": True,
            "sfm_completion_certified": False,
        }
        manifest_sha = _write_exclusive(pending / "snapshot-manifest.json", record)
        _write_exclusive(pending / "DONE.json", {
            "schema_version": "1.0", "owner_id": self.owner,
            "snapshot_manifest_sha256": manifest_sha, "database_sha256": database_sha,
        })
        bounded()
        # Re-hash after manifest creation, before publication. Failed targets
        # retain INCOMPLETE and never become latest-valid.
        if _hash(target, bounded) != database_sha:
            raise SnapshotError("SNAPSHOT_DATABASE_HASH_CHANGED")
        _checked_path(pending / "INCOMPLETE.json").unlink()
        bounded()
        completed = self.root / name
        _checked_path(completed, exists=False)
        if completed.exists():
            raise SnapshotError("SNAPSHOT_PUBLISH_TARGET_EXISTS")
        pending.rename(completed)
        record["manifest_sha256"] = manifest_sha
        self._validate_owned(record, bounded)
        pointer = self.root / "latest-valid.json"
        _checked_path(pointer, exists=False)
        if pointer.exists():
            if self._pointer_sha is None or _hash(pointer, bounded) != self._pointer_sha:
                raise SnapshotError("SNAPSHOT_POINTER_OWNERSHIP_CHANGED")
        elif self._pointer_sha is not None:
            raise SnapshotError("SNAPSHOT_POINTER_MISSING")
        staged_pointer = self.root / (".pointer-" + token + ".json")
        new_pointer_sha = _write_exclusive(staged_pointer, record)
        bounded()
        os.replace(staged_pointer, pointer)
        with self._lock:
            self._pointer_sha = new_pointer_sha
            self._latest = dict(record)
            self._owned.append(dict(record))
            self._last_success_at = self.monotonic()
            self._successful += 1
        self._retain_two(bounded)
        return record

    def _validate_owned(self, record: dict, health: Callable[[], None]) -> None:
        path = self.root / record["directory"]
        _checked_path(path)
        if {item.name for item in path.iterdir()} != {"database.db", "snapshot-manifest.json", "DONE.json"}:
            raise SnapshotError("SNAPSHOT_RETENTION_INVENTORY_CHANGED")
        for item in path.iterdir():
            _checked_path(item)
            if not item.is_file():
                raise SnapshotError("SNAPSHOT_RETENTION_INVENTORY_CHANGED")
        manifest_path, done_path = path / "snapshot-manifest.json", path / "DONE.json"
        if manifest_path.stat().st_size > CHUNK or done_path.stat().st_size > CHUNK:
            raise SnapshotError("SNAPSHOT_RETENTION_METADATA_INVALID")
        manifest, done = json.loads(manifest_path.read_bytes()), json.loads(done_path.read_bytes())
        if (manifest.get("owner_id") != self.owner or done.get("owner_id") != self.owner
                or manifest.get("directory") != record["directory"]
                or done.get("snapshot_manifest_sha256") != record["manifest_sha256"]
                or done.get("database_sha256") != record["database_sha256"]
                or _hash(manifest_path, health) != record["manifest_sha256"]
                or _hash(path / "database.db", health) != record["database_sha256"]):
            raise SnapshotError("SNAPSHOT_RETENTION_OWNERSHIP_CHANGED")

    def _retain_two(self, health: Callable[[], None]) -> None:
        while len(self._owned) > 2:
            record = self._owned[0]
            self._validate_owned(record, health)
            path = self.root / record["directory"]
            # Delete only three exact files whose ownership/hashes were just
            # rechecked. No recursive delete, glob, link traversal or source path.
            for filename in ("database.db", "snapshot-manifest.json", "DONE.json"):
                health()
                _checked_path(path / filename).unlink()
            path.rmdir()
            with self._lock:
                self._owned.pop(0)
                self._removed += 1

    def _loop(self) -> None:
        try:
            while not self._cancel.wait(self.interval_seconds):
                try:
                    self._capture(reason="periodic", health=self.health_check)
                finally:
                    with self._lock:
                        self._active_at = None
        except BaseException as exc:
            if not (self._cancel.is_set() and isinstance(exc, SnapshotError)
                    and exc.code == "SNAPSHOT_CAPTURE_STOPPED"):
                self._set_error(_error_code(exc, "SNAPSHOT_CAPTURE_FAILED"))
        finally:
            with self._lock:
                self._state = "stopped" if self._cancel.is_set() else "failed"

    def start(self) -> None:
        with self._lock:
            if self._state != "prepared":
                raise SnapshotError("SNAPSHOT_STORE_ALREADY_STARTED_OR_STOPPED")
            self._state = "running"
            self._started_at = self.monotonic()
            self._thread = threading.Thread(target=self._loop, name="experiment-sfm-snapshot", daemon=True)
            self._thread.start()

    def check_health(self) -> None:
        # Main process protection must not wait for SQLite, hashing, validators,
        # free-space readers or file I/O executing on a capture thread.
        with self._lock:
            error, state, thread = self._error, self._state, self._thread
            active, last, started = self._active_at, self._last_success_at, self._started_at
        now = self.monotonic()
        if error is not None:
            raise SnapshotError(error)
        if state != "running" or thread is None or not thread.is_alive():
            raise SnapshotError("SNAPSHOT_SUPERVISOR_NOT_RUNNING")
        if active is not None and now - active >= self.timeout_seconds:
            self._set_error("SNAPSHOT_CAPTURE_TIMED_OUT")
            self._cancel.set()
            raise SnapshotError("SNAPSHOT_CAPTURE_TIMED_OUT")
        if started is not None and now - (last if last is not None else started) > self.interval_seconds + self.timeout_seconds:
            self._set_error("SNAPSHOT_FRESHNESS_EXCEEDED")
            self._cancel.set()
            raise SnapshotError("SNAPSHOT_FRESHNESS_EXCEEDED")

    def stop(self, timeout: float = 5) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 5:
            raise SnapshotError("SNAPSHOT_STOP_BUDGET_INVALID")
        self._cancel.set()
        with self._lock:
            thread = self._thread
            if self._state == "prepared":
                self._state = "stopped"
        if thread is not None:
            thread.join(timeout)
        stopped = thread is None or not thread.is_alive()
        if not stopped:
            self._set_error("SNAPSHOT_STOP_UNCONFIRMED")
        return stopped

    def capture_final(self, *, tree_stopped: bool, health_check: Callable[[], None] = _noop,
                      reason: str = "terminal") -> dict:
        if tree_stopped is not True:
            raise SnapshotError("SNAPSHOT_FINAL_TREE_NOT_STOPPED")
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            raise SnapshotError("SNAPSHOT_REASON_INVALID")
        with self._lock:
            if self._state not in {"prepared", "stopped", "failed"} or (self._thread is not None and self._thread.is_alive()):
                raise SnapshotError("SNAPSHOT_FINAL_WORKER_NOT_STOPPED")
            self._state = "final_capture"
        # A fresh forensic health policy deliberately replaces a sticky runtime
        # trip. The caller has already proved all owned compute processes ended.
        self._cancel.clear()
        outcome: dict = {}
        finished = threading.Event()

        def capture() -> None:
            try:
                outcome["result"] = self._capture(reason=reason, health=health_check)
            except BaseException as exc:
                outcome["error"] = _error_code(exc, "SNAPSHOT_FINAL_CAPTURE_FAILED")
            finally:
                with self._lock:
                    self._active_at = None
                    self._state = "stopped"
                finished.set()

        self._thread = threading.Thread(target=capture, name="experiment-final-snapshot", daemon=True)
        self._thread.start()
        if not finished.wait(self.timeout_seconds):
            self._cancel.set()
            self._set_error("SNAPSHOT_FINAL_CAPTURE_TIMED_OUT")
            raise SnapshotError("SNAPSHOT_FINAL_CAPTURE_TIMED_OUT")
        self._cancel.set()
        if "error" in outcome:
            self._set_error(outcome["error"])
            raise SnapshotError(outcome["error"])
        return dict(outcome["result"])

    def summary(self) -> dict:
        with self._lock:
            return {
                "state": self._state, "error_code": self._error,
                "attempts": self._attempts, "successful_snapshots": self._successful,
                "retention_removed_snapshots": self._removed,
                "retained_valid_snapshots": len(self._owned), "latest_valid": dict(self._latest) if self._latest else None,
                "active_capture": self._active_at is not None,
                "worker_stopped_confirmed": self._thread is None or not self._thread.is_alive(),
                "interval_seconds": self.interval_seconds, "timeout_seconds": self.timeout_seconds,
                "minimum_disk_free_bytes": self.min_free_bytes, "source_write_operations": False,
                "incomplete_snapshots_are_recoverable": False,
            }
