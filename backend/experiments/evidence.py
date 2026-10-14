"""Small, bounded C-volume evidence for opt-in experiments, never raw logs.

This sink cannot terminate a process: the owner must poll ``check_health`` while
supervising its contained process tree. Probe and writer threads are independent,
so a blocked C-volume operation cannot block that poll. Terminal writes have a
separate, bounded worker and do not consult a sticky running-health failure.
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from backend.re3d_adapter.errors import AdapterError

GIB = 1024**3
MAX_EVIDENCE_BYTES = 512 * 1024**2
TERMINAL_RESERVE_BYTES = 64 * 1024
ALL_TERMINAL_RESERVE_BYTES = 2 * TERMINAL_RESERVE_BYTES
MAX_RECORD_BYTES = 16 * 1024
PRIVATE_PARENT = Path(r"C:\ProgramData\Re3DRestricted")
_SCENE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,95}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_SNAPSHOT_ID = re.compile(r"snapshot-[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\Z")
_PHASES = frozenset({"preflight", "restore", "sfm", "matching", "mapping", "pipeline", "mapanything", "mvsanywhere", "openmvs", "texture", "validation", "verification", "checkpoint", "terminal", "completed", "failed", "shared_sfm", "mapanything_depth", "mvsanywhere_depth", "branch_a", "branch_b", "branch_c", "assessment", "result_validation"})
_STATUS = frozenset({"incomplete", "running", "succeeded", "failed", "cancelled", "timed_out", "passed", "accepted", "completed", "stopped", "admission_window_complete"})
DEFAULT_CODES = frozenset({
    "UNCLASSIFIED_ERROR", "UNCLASSIFIED_EVENT", "LONG_TEST_STARTED", "PHASE_CHANGED",
    "PROCESS_STARTED", "PROCESS_EXITED", "CHECKPOINT_STARTED", "CHECKPOINT_COMPLETED",
    "CHECKPOINT_FAILED", "CHECKPOINT_PRUNED", "VALIDATION_STARTED", "VALIDATION_COMPLETED",
    "TERMINAL_CAPTURE_STARTED", "TERMINAL_CAPTURE_COMPLETED", "LONG_TEST_COMPLETED",
    "RUN_START", "STAGE_START", "STAGE_FINISHED", "STAGE_GATE_PASSED", "RUN_FINISHED", "RESOURCE_TRIP",
    "OPERATOR_INTERRUPTED", "DEADLINE_EXCEEDED", "SNAPSHOT_STARTED", "SNAPSHOT_COMPLETED", "SNAPSHOT_FAILED",
    "SNAPSHOT_PRUNED", "SNAPSHOT_DEFERRED", "SNAPSHOT_SKIPPED", "TERMINAL",
    "EXPERIMENT_OPERATOR_INTERRUPTED", "EXPERIMENT_PROCESS_TIMED_OUT", "EXPERIMENT_PROCESS_TERMINATION_FAILED",
    "EXPERIMENT_PROCESS_CONTAINMENT_FAILED", "EXPERIMENT_PROCESS_START_FAILED", "EXPERIMENT_PROCESS_IO_FAILED",
    "EXPERIMENT_PROCESS_CALLBACK_FAILED", "EXPERIMENT_IO_OR_DATA_FAILED", "EXPERIMENT_SFM_PROCESS_FAILED",
    "LOW_DISK_AUTO_STOPPED", "LOW_RAM_AUTO_STOPPED", "LOW_VRAM_AUTO_STOPPED",
    "HOST_RESOURCE_PROBE_FAILED", "GPU_RESOURCE_PROBE_FAILED", "DIRECTORY_RESOURCE_PROBE_FAILED",
    "RESOURCE_PROBE_FAILED", "RESOURCE_PROBE_STALE", "RESOURCE_THREAD_STOPPED", "RESOURCE_THREAD_SHUTDOWN_FAILED",
    "RESOURCE_LOG_WRITE_FAILED", "GPU_SAMPLE_UNAVAILABLE", "HOST_SAMPLE_PARTIAL", "DIRECTORY_SAMPLE_PARTIAL",
    "EVIDENCE_PRIVATE_ROOT_INVALID", "EVIDENCE_PRIVATE_ACL_FAILED", "EVIDENCE_TARGET_WINDOWS_REQUIRED",
    "EVIDENCE_ADMIN_REQUIRED", "EVIDENCE_ADMISSION_FAILED", "EVIDENCE_FREE_PROBE_FAILED",
    "EVIDENCE_LOW_DISK", "EVIDENCE_CAPACITY_EXCEEDED", "EVIDENCE_WRITE_FAILED", "EVIDENCE_QUEUE_FULL",
    "EVIDENCE_PROBE_STALE", "EVIDENCE_THREAD_STOPPED", "EVIDENCE_NOT_STARTED", "EVIDENCE_RECORD_INVALID",
    "EVIDENCE_TERMINAL_FAILED", "EVIDENCE_TERMINAL_TIMED_OUT", "EVIDENCE_TERMINAL_EXISTS",
})
_NUMERIC = frozenset({
    "elapsed_seconds", "duration_seconds", "budget_seconds", "budget_remaining_seconds", "remaining_budget_seconds", "pid", "return_code",
    "matches_rows", "geometry_rows", "complete_pair_records", "nonempty_match_records", "geometry_min15_records",
    "initial_complete_pairs", "final_complete_pairs", "added_complete_pairs", "overall_pairs_per_minute",
    "steady_pairs_per_minute", "sample_count", "logical_count", "utilization_percent", "used_bytes", "total_bytes",
    "available_bytes", "free_bytes", "memory_used_bytes", "memory_total_bytes", "memory_free_bytes", "index",
    "file_count", "cpu_utilization_percent", "ram_used_bytes", "vram_used_bytes", "gpu_utilization_percent",
    "directory_bytes", "disk_min_free_bytes", "ram_min_available_bytes", "vram_min_free_bytes", "host", "gpu",
    "supervisor", "directory_observation_count", "snapshot_count", "snapshot_sequence", "size_bytes", "sequence",
    "threads", "minimum_free_bytes", "minimum_available_bytes", "age_seconds", "maximum_age_seconds",
    "checkpoint_count", "checkpoint_sequence", "retained_checkpoint_count", "journal_record_count", "journal_bytes",
    "complete_pairs", "stage_count", "completed_stages_count", "observation_age_seconds", "writer", "host_age_seconds",
    "gpu_age_seconds", "directory_age_seconds", "minimum_free_vram_bytes", "minimum_available_ram_bytes",
})
_BOOLEAN = frozenset({"process_tree_exited", "source_checkpoint_unchanged", "source_job_modified", "platform_job_resume",
    "queue_modified", "services_stopped", "committed_wal_included", "snapshot_valid", "sfm_complete", "passed",
    "tree_stopped", "cleanup_confirmed", "snapshot_attempted"})
_MAPS = frozenset({"cpu", "memory", "disk", "gpu", "directory", "latest", "peaks", "guard_checks", "resources", "throughput", "metrics", "trip", "snapshot", "checkpoint", "observation_ages_seconds", "evidence"})
_HASHES = frozenset({"sha256", "database_sha256", "source_database_sha256", "source_manifest_sha256", "manifest_sha256", "tool_manifest_sha256",
    "config_sha256", "profile_sha256", "latest_snapshot_sha256"})


class EvidenceTrip(AdapterError):
    """Stable identifier only; never attach a caught exception or secret path."""
    def __init__(self, code: str):
        self.code = code if code in DEFAULT_CODES else "UNCLASSIFIED_ERROR"
        super().__init__(self.code)


def _ordinary_directory(path: Path) -> None:
    try:
        for component in (path, *path.parents):
            info = component.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError
    except (OSError, ValueError):
        raise EvidenceTrip("EVIDENCE_PRIVATE_ROOT_INVALID") from None


def _free_bytes(root: Path) -> int:
    return shutil.disk_usage(root).free


def _valid_free(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvidenceTrip("EVIDENCE_FREE_PROBE_FAILED")
    return value


def _bounded_call(function: Callable[[], Any], seconds: float, failure: str) -> Any:
    result: list[Any] = []
    done = threading.Event()
    def invoke() -> None:
        try:
            result.append((True, function()))
        except BaseException:
            result.append((False, None))
        finally:
            done.set()
    threading.Thread(target=invoke, daemon=True, name="re3d-evidence-bounded-operation").start()
    if not done.wait(seconds) or not result or not result[0][0]:
        raise EvidenceTrip(failure)
    return result[0][1]


def _private_acl(root: Path) -> None:
    """Touch only the just-created run directory, and verify the exact ACL."""
    path = str(root).replace("'", "''")
    script = f"""$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$a=New-Object Security.AccessControl.DirectorySecurity
$a.SetSecurityDescriptorSddlForm('O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')
Set-Acl -LiteralPath '{path}' -AclObject $a
$v=Get-Acl -LiteralPath '{path}'
$r=@($v.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
if(-not $v.AreAccessRulesProtected-or $v.GetOwner([Security.Principal.SecurityIdentifier]).Value-ne 'S-1-5-32-544'-or $r.Count-ne 2){{exit 1}}
foreach($x in $r){{if($x.IdentityReference.Value-notin @('S-1-5-18','S-1-5-32-544')-or $x.AccessControlType-ne 'Allow'-or $x.FileSystemRights-ne 'FullControl'-or $x.IsInherited-or $x.InheritanceFlags-ne 'ContainerInherit, ObjectInherit'-or $x.PropagationFlags-ne 'None'){{exit 1}}}}
"""
    try:
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")], capture_output=True, timeout=8, check=False)
        if result.returncode:
            raise ValueError
    except Exception:
        raise EvidenceTrip("EVIDENCE_PRIVATE_ACL_FAILED") from None


def create_private_directory(scene_name: str) -> Path:
    """Production factory: new C-volume run root only, never a supplied target."""
    if os.name != "nt":
        raise EvidenceTrip("EVIDENCE_TARGET_WINDOWS_REQUIRED")
    from backend.experiments.preflight import require_admin
    try:
        require_admin()
    except AdapterError:
        raise EvidenceTrip("EVIDENCE_ADMIN_REQUIRED") from None
    if not isinstance(scene_name, str) or _SCENE.fullmatch(scene_name) is None:
        raise EvidenceTrip("EVIDENCE_PRIVATE_ROOT_INVALID")
    _ordinary_directory(PRIVATE_PARENT)
    available = _bounded_call(lambda: _valid_free(_free_bytes(PRIVATE_PARENT)), 5, "EVIDENCE_FREE_PROBE_FAILED")
    if available < 10 * GIB:
        raise EvidenceTrip("EVIDENCE_ADMISSION_FAILED")
    root = PRIVATE_PARENT / ("private-evidence-" + scene_name)
    try:
        root.mkdir()  # Exclusive: no exist_ok, no overwrite, no broad ACL changes.
    except OSError:
        raise EvidenceTrip("EVIDENCE_PRIVATE_ROOT_INVALID") from None
    _ordinary_directory(root)
    _private_acl(root)
    return root


class EvidenceSink:
    """An exclusive, bounded small-evidence journal plus independent terminal file.

    Use create_production in deployment. create_for_test is an explicit injected
    test seam, not a CLI option or an alternate production data path. The writer
    queue has 128 records and every committed journal record is fsynced. The last
    accepted event is retained in memory and in terminal evidence even if journal
    persistence trips. User text, argv, filesystem paths, env and logs are dropped.
    """
    def __init__(self, root: Path, *, _factory_token: object, free_reader: Callable[[Path], int],
                 capacity_bytes: int = MAX_EVIDENCE_BYTES, known_codes: Iterable[str] = (),
                 known_stages: Iterable[str] = (),
                 _test_intervals: tuple[float, float] | None = None,
                 _write_bytes: Callable[[int, bytes], int] = os.write) -> None:
        if _factory_token is not _FACTORY_TOKEN:
            raise EvidenceTrip("EVIDENCE_PRIVATE_ROOT_INVALID")
        _ordinary_directory(root)
        if any(root.iterdir()):
            raise EvidenceTrip("EVIDENCE_PRIVATE_ROOT_INVALID")
        if not isinstance(capacity_bytes, int) or isinstance(capacity_bytes, bool) or not 256 * 1024 <= capacity_bytes <= MAX_EVIDENCE_BYTES:
            raise EvidenceTrip("EVIDENCE_RECORD_INVALID")
        self.root = root
        self.capacity_bytes = capacity_bytes
        self.free_reader = free_reader
        self._codes = DEFAULT_CODES | frozenset(code for code in known_codes if isinstance(code, str) and _CODE.fullmatch(code))
        self._stages = _PHASES | frozenset(stage for stage in known_stages if isinstance(stage, str) and _SCENE.fullmatch(stage))
        self._interval, self._stale = 1.0, 5.0
        if _test_intervals is not None:
            if (len(_test_intervals) != 2 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in _test_intervals)
                    or not 0 < _test_intervals[0] <= 0.25 or not 0 < _test_intervals[1] <= 2):
                raise EvidenceTrip("EVIDENCE_RECORD_INVALID")
            self._interval, self._stale = _test_intervals
        self._write_bytes = _write_bytes
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._terminal_threads: list[threading.Thread] = []
        self._last_probe = 0.0
        self._last_writer = 0.0
        self._journal_ready = False
        self._started_at = 0.0
        self._started = False
        self._closed = False
        self._trip: EvidenceTrip | None = None
        self._last_event: dict[str, Any] | None = None
        self._record_count = 0
        self._bytes_reserved = 0
        self._bytes_written = 0
        self._free_minimum: int | None = None
        self._terminal_started = False
        self._forensic_started = False

    @classmethod
    def create_production(cls, scene_name: str, *, known_codes: Iterable[str] = (), known_stages: Iterable[str] = ()) -> EvidenceSink:
        return cls(create_private_directory(scene_name), _factory_token=_FACTORY_TOKEN, free_reader=_free_bytes,
                   known_codes=known_codes, known_stages=known_stages)

    @classmethod
    def create_for_test(cls, root: Path, *, free_reader: Callable[[Path], int], capacity_bytes: int = MAX_EVIDENCE_BYTES,
                        known_codes: Iterable[str] = (), known_stages: Iterable[str] = (), intervals: tuple[float, float] = (0.02, 0.8),
                        write_bytes: Callable[[int, bytes], int] = os.write) -> EvidenceSink:
        """Explicit fixture-only factory; requires an injected resource reader."""
        _ordinary_directory(root.parent)
        try:
            root.mkdir(mode=0o700)
        except OSError:
            raise EvidenceTrip("EVIDENCE_PRIVATE_ROOT_INVALID") from None
        available = _bounded_call(lambda: _valid_free(free_reader(root)), 1, "EVIDENCE_FREE_PROBE_FAILED")
        if available < 10 * GIB:
            raise EvidenceTrip("EVIDENCE_ADMISSION_FAILED")
        return cls(root, _factory_token=_FACTORY_TOKEN, free_reader=free_reader, capacity_bytes=capacity_bytes,
                   known_codes=known_codes, known_stages=known_stages, _test_intervals=intervals, _write_bytes=write_bytes)

    def sanitize(self, payload: Any, *, depth: int = 0, _budget: list[int] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict) or depth > 4:
            return {}
        if _budget is None:
            _budget = [128]
        output: dict[str, Any] = {}
        # The limit is global across all branches, not 128 at every recursion.
        # Thus even a mistakenly nested generated payload has bounded work/size.
        for key, value in payload.items():
            if _budget[0] <= 0:
                break
            _budget[0] -= 1
            if key in _MAPS and isinstance(value, dict):
                output[key] = self.sanitize(value, depth=depth + 1, _budget=_budget)
            elif key in _NUMERIC and (value is None or isinstance(value, (int, float)) and not isinstance(value, bool)):
                if value is None or abs(value) <= 10**18 and (isinstance(value, int) or math.isfinite(value)):
                    output[key] = value
            elif key in _BOOLEAN and isinstance(value, bool):
                output[key] = value
            elif key == "phase" and isinstance(value, str) and value in _PHASES:
                output[key] = value
            elif key == "stage" and isinstance(value, str) and value in self._stages:
                output[key] = value
            elif key == "status" and isinstance(value, str) and value in _STATUS:
                output[key] = value
            elif key == "checkpoint_status" and isinstance(value, str) and value in {"not_attempted", "pending", "valid", "failed", "skipped", "deferred", "completed"}:
                output[key] = value
            elif key in {"code", "error_code", "event_code"}:
                output[key] = value if isinstance(value, str) and value in self._codes else ("UNCLASSIFIED_EVENT" if key in {"code", "event_code"} else "UNCLASSIFIED_ERROR")
            elif key in _HASHES and isinstance(value, str) and _HASH.fullmatch(value):
                output[key] = value
            elif key == "pipeline_commit" and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
                output[key] = value
            elif key in {"run_id", "scene"} and isinstance(value, str) and _SCENE.fullmatch(value):
                output[key] = value
            elif key == "latest_snapshot_id" and isinstance(value, str) and _SNAPSHOT_ID.fullmatch(value):
                output[key] = value
            elif key == "source_job_id" and isinstance(value, str) and _UUID.fullmatch(value):
                output[key] = value
            elif key in {"errors", "forensic_notes"} and isinstance(value, (tuple, list)):
                selected = value[:min(32, _budget[0])]
                _budget[0] -= len(selected)
                output[key] = [code if isinstance(code, str) and code in self._codes else "UNCLASSIFIED_ERROR" for code in selected]
        return output

    def start(self) -> None:
        if self._started or self._closed:
            raise EvidenceTrip("EVIDENCE_NOT_STARTED")
        self._started = True
        self._started_at = time.monotonic()
        self._last_probe = self._last_writer = self._started_at
        for name, callback in (("probe", self._probe_loop), ("writer", self._writer_loop)):
            thread = threading.Thread(target=callback, daemon=True, name="re3d-small-evidence-" + name)
            self._threads[name] = thread
            thread.start()
        # Bounded startup readiness before any owned compute is launched.
        deadline = self._started_at + self._stale
        while True:
            self.check_health()
            with self._lock:
                ready = self._free_minimum is not None and self._journal_ready
            if ready:
                return
            if time.monotonic() >= deadline:
                self._set_trip("EVIDENCE_PROBE_STALE")
            self._stop.wait(0.005)

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        self.check_health()
        if self._closed:
            raise EvidenceTrip("EVIDENCE_NOT_STARTED")
        if kind not in {"event", "resources", "pairs"} or not isinstance(payload, dict):
            self._set_trip("EVIDENCE_RECORD_INVALID")
            self.check_health()
        sanitized = self.sanitize(payload)
        with self._lock:
            record = {"schema_version": "1.0", "kind": kind, "record_sequence": self._record_count + 1,
                      "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "payload": sanitized}
            raw = (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if kind == "event":
                self._last_event = record
            if len(raw) > MAX_RECORD_BYTES:
                self._set_trip_locked("EVIDENCE_RECORD_INVALID")
            elif self._bytes_reserved + len(raw) > self.capacity_bytes - ALL_TERMINAL_RESERVE_BYTES:
                self._set_trip_locked("EVIDENCE_CAPACITY_EXCEEDED")
            else:
                try:
                    self._queue.put_nowait(raw)
                    self._bytes_reserved += len(raw)
                    self._record_count += 1
                except queue.Full:
                    self._set_trip_locked("EVIDENCE_QUEUE_FULL")
        self.check_health()

    def check_health(self) -> None:
        # No filesystem/probe/write operation here, only finite state checks.
        if not self._started:
            raise EvidenceTrip("EVIDENCE_NOT_STARTED")
        with self._lock:
            if self._trip is not None:
                raise self._trip
            if self._closed:
                return
            now = time.monotonic()
            if now - self._last_probe > self._stale or now - self._last_writer > max(self._stale, self._interval * 3):
                self._set_trip_locked("EVIDENCE_PROBE_STALE")
            elif any(not thread.is_alive() for thread in self._threads.values()):
                self._set_trip_locked("EVIDENCE_THREAD_STOPPED")
            if self._trip is not None:
                raise self._trip

    def _set_trip_locked(self, code: str) -> None:
        if self._trip is None:
            self._trip = EvidenceTrip(code)

    def _set_trip(self, code: str) -> None:
        with self._lock:
            self._set_trip_locked(code)

    def _probe_loop(self) -> None:
        try:
            while not self._stop.is_set():
                _ordinary_directory(self.root)
                free = _valid_free(self.free_reader(self.root))
                with self._lock:
                    self._last_probe = time.monotonic()
                    self._free_minimum = free if self._free_minimum is None else min(self._free_minimum, free)
                if free <= 2 * GIB:
                    self._set_trip("EVIDENCE_LOW_DISK")
                    return
                self._stop.wait(self._interval)
        except BaseException:
            self._set_trip("EVIDENCE_FREE_PROBE_FAILED")

    def _writer_loop(self) -> None:
        descriptor = None
        try:
            descriptor = os.open(self.root / "journal.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fsync(descriptor)
            with self._lock:
                self._journal_ready = True
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    raw = self._queue.get(timeout=self._interval)
                except queue.Empty:
                    with self._lock:
                        self._last_writer = time.monotonic()
                    continue
                _ordinary_directory(self.root)
                if self._write_bytes(descriptor, raw) != len(raw):
                    raise OSError
                os.fsync(descriptor)
                with self._lock:
                    self._bytes_written += len(raw)
                    self._last_writer = time.monotonic()
                self._queue.task_done()
        except BaseException:
            self._set_trip("EVIDENCE_WRITE_FAILED")
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    self._set_trip("EVIDENCE_WRITE_FAILED")

    def finish_terminal(self, summary: dict[str, Any], *, timeout_seconds: float = 5) -> Path:
        """Bounded terminal attempt even after sticky running failure.

        Call only after the owner's tree-stop attempt; record its actual outcome.
        A timed-out OS write may finish later inside this private root. No retry or
        overwrite is attempted. C below its own floor fails; D floor is irrelevant.
        """
        return self._terminal_write(summary, "terminal.json", forensic=False, timeout_seconds=timeout_seconds)

    def write_forensic_summary(self, summary: dict[str, Any], *, timeout_seconds: float = 5) -> Path:
        """Second exclusive C file after bounded final D snapshot/verification.

        The first terminal file remains unchanged. This method may follow a
        failed/timed-out first attempt and never enables further computation.
        """
        return self._terminal_write(summary, "forensic-summary.json", forensic=True, timeout_seconds=timeout_seconds)

    def _terminal_write(self, summary: dict[str, Any], name: str, *, forensic: bool, timeout_seconds: float) -> Path:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 10:
            raise EvidenceTrip("EVIDENCE_TERMINAL_FAILED")
        with self._lock:
            if (self._forensic_started if forensic else self._terminal_started):
                raise EvidenceTrip("EVIDENCE_TERMINAL_EXISTS")
            if forensic:
                self._forensic_started = True
            else:
                self._terminal_started = True
            terminal = {"schema_version": "1.0", "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "summary": self.sanitize(summary), "last_event": self._last_event,
                        "sink": {"journal_record_count": self._record_count, "journal_bytes": self._bytes_written,
                                 "journal_reserved_bytes": self._bytes_reserved, "free_minimum_bytes": self._free_minimum,
                                 "running_error_code": self._trip.code if self._trip else None}}
        raw = (json.dumps(terminal, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > TERMINAL_RESERVE_BYTES:
            raise EvidenceTrip("EVIDENCE_TERMINAL_FAILED")
        destination = self.root / name
        done = threading.Event()
        failures: list[str] = []
        def persist() -> None:
            descriptor = None
            try:
                _ordinary_directory(self.root)
                if _valid_free(self.free_reader(self.root)) <= 2 * GIB:
                    raise EvidenceTrip("EVIDENCE_LOW_DISK")
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
                if os.write(descriptor, raw) != len(raw):
                    raise OSError
                os.fsync(descriptor)
            except BaseException as error:
                failures.append(error.code if isinstance(error, EvidenceTrip) else "EVIDENCE_TERMINAL_FAILED")
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        failures.append("EVIDENCE_TERMINAL_FAILED")
                done.set()
        worker = threading.Thread(target=persist, daemon=True, name="re3d-small-evidence-terminal")
        self._terminal_threads.append(worker)
        worker.start()
        if not done.wait(timeout_seconds):
            raise EvidenceTrip("EVIDENCE_TERMINAL_TIMED_OUT")
        if failures:
            raise EvidenceTrip(failures[0])
        return destination

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        deadline = time.monotonic() + 0.25
        for thread in [*self._threads.values(), *self._terminal_threads]:
            thread.join(max(0, deadline - time.monotonic()))

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"capacity_bytes": self.capacity_bytes, "journal_record_count": self._record_count,
                    "journal_bytes": self._bytes_written, "journal_reserved_bytes": self._bytes_reserved,
                    "free_minimum_bytes": self._free_minimum, "running_error_code": self._trip.code if self._trip else None,
                    "terminal_attempted": self._terminal_started, "forensic_attempted": self._forensic_started,
                    "writer_still_alive": bool(self._threads.get("writer") and self._threads["writer"].is_alive())}


_FACTORY_TOKEN = object()
