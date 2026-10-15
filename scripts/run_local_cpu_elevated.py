"""Fixed, hash-pinned elevated launcher and read-only public CPU SfM status.

Load this file via an external hash-pinned -I -S bootstrap. The evidence
initializer must already be registered as re3d_cpu_evidence_initializer; this
module never imports a writable checkout initializer or starts on import.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from types import ModuleType
import uuid


CALLER_SID = "S-1-5-21-3895989996-989152356-177590390-1001"
SCENE = "lighthouse300-local-cpu12-v1"
TOOL_ROOT = Path(r"D:\3Dreconstruction\Re3D-data\releases\hybrid-cpu-tools-20261014-28770a6\restore-verification")
TOOL_SHA256 = "21e1b570a530270c4f36130d417985a41b219b917ec1cdb27fde97d5009e4ae6"
PROFILE_SHA256 = "e9bfa0b42d21566b6430c924fd8e42c1b9c7dce597965319ddb54dd91dcbb680"
PROFILE = TOOL_ROOT / "config/hybrid-cpu.example.json"
PIPELINE_ROOT = Path(r"D:\3Dreconstruction\Re3D")
PLATFORM_ROOT = Path(r"D:\3Dreconstruction\Re3D-platform")
EXPERIMENT_ROOT = Path(r"D:\3Dreconstruction\Re3D-data\_experiments\lighthouse300-local-cpu12-v1")
PRIVATE_PARENT = Path(r"C:\ProgramData\Re3DRestricted")
PRIVATE_JOURNAL = PRIVATE_PARENT / ("private-evidence-" + SCENE) / "journal.jsonl"
PUBLIC_ROOT = PRIVATE_PARENT / ("public-status-" + SCENE)
RESULT_PATH = EXPERIMENT_ROOT / "reports/cpu-result.json"
PUBLIC_STATUS = PUBLIC_ROOT / "status.json"
READY_EVENT_PREFIX = "Local\\Re3DCPUReady-"
PUBLIC_LIMIT = 32 * 1024
TAIL_LIMIT = 1024**2
TAIL_ROWS = 256
READY_TIMEOUT_SECONDS = 120
PUBLISH_INTERVAL_SECONDS = 20
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,95}\Z")
_PHASES = {"initialize", "ready_waiting_reader", "preflight", "restore", "sfm", "matching",
           "mapping", "checkpoint", "terminal", "completed", "failed"}
_STATUS = {"incomplete", "running", "succeeded", "failed", "cancelled", "timed_out", "blocked"}
_NUMBERS = {"elapsed_seconds", "duration_seconds", "complete_pair_records", "complete_pairs",
            "cpu_utilization_percent", "utilization_percent", "ram_used_bytes", "used_bytes",
            "available_bytes", "free_bytes", "total_bytes", "directory_bytes", "disk_min_free_bytes",
            "ram_min_available_bytes", "sample_count", "cpu_sample_count", "cpu_sampled_mean_percent",
            "database_images", "registered_images", "points3D", "observations",
            "mean_reprojection_error_px", "verified_pairs_min_15", "pair_attempt_count", "pair_retry_count",
            "pair_consecutive_failures", "pair_last_success_elapsed_seconds", "pair_observation_age_seconds",
            "pair_freshness_limit_seconds", "pair_retry_interval_seconds", "sqlite_errorcode", "sqlite_primary_code"}
_MAPS = {"resources", "peaks", "cpu", "memory", "disk", "directory", "latest", "sfm_gate",
         "observation", "last_failure"}
_PROOFS = {"process_tree_stopped", "snapshot_worker_stopped", "terminal_evidence_confirmed",
           "source_checkpoint_unchanged", "observation_relay_stopped", "geometry_accuracy_certified",
           "observation_closed", "deadline_triggered"}
_DIAGNOSTIC_EXCEPTION_TYPES = frozenset({"OperationalError", "DatabaseError", "IntegrityError", "ProgrammingError",
    "InterfaceError", "InternalError", "DataError", "NotSupportedError", "RuntimeError", "TypeError", "ValueError",
    "OverflowError", "OSError", "PermissionError", "FileNotFoundError", "TimeoutError", "SupervisionError", "Exception"})
_DIAGNOSTIC_CODES = frozenset({"LONG_PAIR_PROBE_BUSY", "LONG_PAIR_PROBE_LOCKED", "LONG_PAIR_PROBE_TIMED_OUT",
    "LONG_PAIR_PROBE_INTERRUPTED", "LONG_PAIR_PROBE_DATABASE_FAILED", "LONG_PAIR_PROBE_RESULT_INVALID",
    "LONG_OBSERVATION_PAIR_STALE", "LONG_OBSERVATION_STALE", "LONG_STAGE_OR_IO_FAILED", "LONG_BUDGET_EXHAUSTED"})
_NULLABLE_NUMBERS = {"pair_last_success_elapsed_seconds", "pair_observation_age_seconds", "sqlite_errorcode",
                     "sqlite_primary_code"}
_ERROR_CODES = frozenset({
    "LOCAL_CPU_WRAPPER_FAILED", "LOCAL_CPU_FIXED_PATH_INVALID", "LOCAL_CPU_REPARSE_REJECTED",
    "LOCAL_CPU_REGULAR_FILE_REQUIRED", "LOCAL_CPU_DOCUMENT_TOO_LARGE", "LOCAL_CPU_DUPLICATE_JSON_KEY",
    "LOCAL_CPU_JSON_OBJECT_REQUIRED", "LOCAL_CPU_FINAL_PROOF_INCOMPLETE", "LOCAL_CPU_FINAL_IDENTITY_MISMATCH",
    "LOCAL_CPU_FINAL_PROFILE_MISMATCH", "LOCAL_CPU_WINDOWS_REQUIRED", "LOCAL_CPU_SECURITY_DESCRIPTOR_FAILED",
    "LOCAL_CPU_PUBLIC_PATH_INVALID", "LOCAL_CPU_PUBLIC_ACL_PROBE_FAILED", "LOCAL_CPU_PUBLIC_ACL_INVALID",
    "LOCAL_CPU_PUBLIC_TARGET_EXISTS", "LOCAL_CPU_PUBLIC_CREATE_FAILED", "LOCAL_CPU_PUBLIC_RECORD_TOO_LARGE",
    "LOCAL_CPU_PUBLIC_STATUS_NOT_OWNED", "LOCAL_CPU_PUBLIC_FILE_CREATE_FAILED", "LOCAL_CPU_PUBLIC_FILE_WRITE_FAILED",
    "LOCAL_CPU_READY_EVENT_CREATE_FAILED", "LOCAL_CPU_READY_EVENT_ALREADY_EXISTS", "LOCAL_CPU_READY_WAIT_FAILED",
    "LOCAL_CPU_READY_EVENT_CLOSE_FAILED", "LOCAL_CPU_SLEEP_GUARD_FAILED", "LOCAL_CPU_TOOL_MANIFEST_CHANGED",
    "LOCAL_CPU_PROFILE_CHANGED", "LOCAL_CPU_ENTRY_HASH_MISMATCH", "LOCAL_CPU_CALLER_SID_MISMATCH",
    "LOCAL_CPU_PINNED_INITIALIZER_REQUIRED", "LOCAL_CPU_ADMIN_REQUIRED", "LOCAL_CPU_EVIDENCE_PARENT_NOT_READY",
    "LOCAL_CPU_READY_EVENT_NAME_INVALID", "LOCAL_CPU_PUBLIC_READER_UNCONFIRMED", "LOCAL_CPU_TOOL_RUN_FAILED",
    "LOCAL_CPU_PUBLIC_PUBLISHER_NOT_STOPPED", "LOCAL_CPU_CLEANUP_FAILED",
})
_COMMON_TOOL_CODES = frozenset({"LOW_RAM_AUTO_STOPPED", "LOW_DISK_AUTO_STOPPED", "LONG_BUDGET_EXHAUSTED",
    "CPU_SFM_STAGE_PROCESS_FAILED", "CPU_SFM_PRIVATE_REPORT_FAILED", "UNCLASSIFIED_ERROR"})


class ElevatedCPUError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise ElevatedCPUError(code)


def _stable_code(exc):
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code in _ERROR_CODES else "LOCAL_CPU_WRAPPER_FAILED"


def _plain_file(path):
    require(path.is_absolute() and ".." not in path.parts, "LOCAL_CPU_FIXED_PATH_INVALID")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        info = cursor.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 1024,
                "LOCAL_CPU_REPARSE_REJECTED")
    require(path.is_file(), "LOCAL_CPU_REGULAR_FILE_REQUIRED")
    return path


def _read_document(path, limit):
    _plain_file(path)
    require(path.stat().st_size <= limit, "LOCAL_CPU_DOCUMENT_TOO_LARGE")
    raw = path.read_bytes()
    require(len(raw) <= limit, "LOCAL_CPU_DOCUMENT_TOO_LARGE")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "LOCAL_CPU_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    document = json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    require(isinstance(document, dict), "LOCAL_CPU_JSON_OBJECT_REQUIRED")
    return document, raw


def _replace_status_file(temporary):
    """A short normal-reader sharing lock must not create an unbounded wait."""
    require(temporary.parent == PUBLIC_ROOT, "LOCAL_CPU_FIXED_PATH_INVALID")
    for attempt in range(5):
        try:
            os.replace(temporary, PUBLIC_STATUS)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (32, 33) or attempt == 4:
                raise
            time.sleep(0.05)


def sanitize(document, *, depth=0):
    """Publish numeric telemetry and fixed vocabulary only, never paths/logs/env."""
    if not isinstance(document, dict) or depth > 4:
        return {}
    result = {}
    for key, value in list(document.items())[:64]:
        if key in _NULLABLE_NUMBERS and value is None:
            result[key] = None
        elif key in _NUMBERS and type(value) in (int, float) and math.isfinite(value) and value >= 0:
            if key in {"complete_pair_records", "complete_pairs"} and (type(value) is not int or value > 44850):
                continue
            if key in {"cpu_utilization_percent", "utilization_percent", "cpu_sampled_mean_percent"} and value > 100:
                continue
            result[key] = value
        elif key in _MAPS and isinstance(value, dict):
            result[key] = sanitize(value, depth=depth + 1)
        elif key in _PROOFS and type(value) is bool:
            result[key] = value
        elif key == "phase" and isinstance(value, str) and value in _PHASES:
            result[key] = value
        elif key == "status" and isinstance(value, str) and value in _STATUS:
            result[key] = value
        elif key == "stage" and isinstance(value, str) and value in {"01-colmap", "restore", "preflight", "terminal"}:
            result[key] = value
        elif key in {"latest_snapshot_sha256", "database_sha256", "manifest_sha256"} \
                and isinstance(value, str) and _SHA.fullmatch(value):
            result[key] = value
        elif key == "final_snapshot_status" and isinstance(value, str) and value in {"valid", "failed", "skipped"}:
            result[key] = value
        elif key == "probe_kind" and isinstance(value, str) and value in {"pairs", "resources"}:
            result[key] = value
        elif key == "source_frame" and isinstance(value, str) and value in {
                "read_pair_counts", "ObservationRelay.pairs", "ObservationRelay.resources"}:
            result[key] = value
        elif key == "exception_type" and isinstance(value, str) and value in _DIAGNOSTIC_EXCEPTION_TYPES:
            result[key] = value
        elif key == "error_code" and isinstance(value, str) and value in _DIAGNOSTIC_CODES:
            result[key] = value
    return result


def read_public_observation():
    """Read at most 1 MiB and 256 journal rows from this fixed private run."""
    try:
        path = _plain_file(PRIVATE_JOURNAL)
    except FileNotFoundError:
        return {}
    size = path.stat().st_size
    with path.open("rb") as stream:
        start = max(0, size - TAIL_LIMIT)
        stream.seek(start)
        if start:
            stream.readline(TAIL_LIMIT)  # Ignore one potentially partial row.
        raw = stream.read(TAIL_LIMIT)
    summary = {}
    for row in raw.splitlines()[-TAIL_ROWS:]:
        if len(row) > 16 * 1024:
            continue
        try:
            document = json.loads(row)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(document, dict) or document.get("kind") not in {"event", "resources", "pairs"}:
            continue
        summary.update(sanitize(document.get("payload")))
    return summary


def verify_final_result():
    """Verify the persisted actual-tool outcome, not a zero CLI return code."""
    expected, expected_raw = _read_document(PROFILE, 128 * 1024)
    source = expected.get("source_checkpoint")
    require(isinstance(source, dict), "LOCAL_CPU_PROFILE_CHANGED")
    source_database_sha = source.get("snapshot_database_sha256")
    require(hashlib.sha256(expected_raw).hexdigest() == PROFILE_SHA256
            and isinstance(source_database_sha, str) and _SHA.fullmatch(source_database_sha),
            "LOCAL_CPU_PROFILE_CHANGED")
    result, raw = _read_document(RESULT_PATH, 4 * 1024**2)
    require(result.get("mode") == "cpu-sfm" and result.get("status") == "succeeded"
            and result.get("source_job_modified") is False and result.get("platform_job_resume") is False
            and result.get("gpu_stage_started") is False
            and all(result.get(key) is True for key in (
                "process_tree_stopped", "snapshot_worker_stopped", "terminal_evidence_confirmed",
                "source_checkpoint_unchanged", "observation_relay_stopped"))
            and result.get("final_snapshot_status") == "valid"
            and isinstance(result.get("sfm_gate"), dict) and bool(result["sfm_gate"])
            and not result.get("error_code") and not result.get("private_result_error"),
            "LOCAL_CPU_FINAL_PROOF_INCOMPLETE")
    # Existing CPUSfmSupervisor may store identity only in its private plan.
    # Always use that hash-pinned execution identity, without rerunning SfM gate.
    plan, _ = _read_document(EXPERIMENT_ROOT / "reports/executed-cpu-plan.json", 128 * 1024)
    require(plan.get("mode") == "cpu-sfm" and plan.get("tool_manifest_sha256") == TOOL_SHA256
            and plan.get("pipeline_commit") == "e517a161b4c0895f8be4258fb339272e4f99e1d6"
            and plan.get("source_database_sha256") == source_database_sha
            and plan.get("gpu_sampling_enabled") is False and plan.get("queue_accessed") is False,
            "LOCAL_CPU_FINAL_IDENTITY_MISMATCH")
    effective, effective_raw = _read_document(EXPERIMENT_ROOT / "reports/effective-cpu-profile.json", 128 * 1024)
    require(effective == expected and hashlib.sha256(effective_raw).hexdigest() == plan.get("profile_sha256")
            and effective.get("target_experiment", {}).get("scene") == SCENE,
            "LOCAL_CPU_FINAL_PROFILE_MISMATCH")
    selected = sanitize(result)
    selected.update(status="succeeded", phase="completed",
                    cpu_result_sha256=hashlib.sha256(raw).hexdigest())
    snapshot = result.get("final_snapshot", {})
    if isinstance(snapshot, dict):
        selected.update(sanitize({"database_sha256": snapshot.get("database_sha256"),
                                 "complete_pair_records": snapshot.get("validated_complete_pair_records")}))
    return selected


class NativeAPI:
    """Fixed Windows directory/file/event targets; atomic protected ACL creation."""
    def __init__(self, initializer):
        require(os.name == "nt", "LOCAL_CPU_WINDOWS_REQUIRED")
        self.initializer = initializer
        self.security = initializer.NativeWindowsAPI()
        self.kernel, self.advapi = self.security.kernel, self.security.advapi
        function = self.security._function
        function(self.kernel, "CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                 wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        function(self.kernel, "WriteFile", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p,
                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        function(self.kernel, "FlushFileBuffers", wintypes.BOOL, wintypes.HANDLE)
        function(self.kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
        function(self.kernel, "CreateEventW", wintypes.HANDLE, ctypes.c_void_p,
                 wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        function(self.kernel, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
        function(self.kernel, "SetThreadExecutionState", wintypes.DWORD, wintypes.DWORD)
        self.status_owned = False

    def is_admin(self):
        return self.security.is_admin()

    def _descriptor(self, sddl):
        descriptor = ctypes.c_void_p()
        require(self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None), "LOCAL_CPU_SECURITY_DESCRIPTOR_FAILED")
        return descriptor

    def _plain_directory(self, path):
        cursor = Path(path.anchor)
        for part in path.parts[1:]:
            cursor /= part
            attributes = self.security.attributes(str(cursor))
            require(attributes is not None and attributes & 0x10 and not attributes & 0x400,
                    "LOCAL_CPU_PUBLIC_PATH_INVALID")

    def _verify_acl(self, path, *, directory):
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        require(self.advapi.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, ctypes.byref(owner),
            None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) == 0,
            "LOCAL_CPU_PUBLIC_ACL_PROBE_FAILED")
        try:
            require(owner and dacl and descriptor, "LOCAL_CPU_PUBLIC_ACL_PROBE_FAILED")
            control, revision = wintypes.WORD(), wintypes.DWORD()
            require(self.advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
                    and control.value & 0x1000 and self.security._sid(owner) == "S-1-5-32-544",
                    "LOCAL_CPU_PUBLIC_ACL_INVALID")
            info = self.initializer._AclSizeInformation()
            require(self.advapi.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2)
                    and info.AceCount == 3, "LOCAL_CPU_PUBLIC_ACL_INVALID")
            actual = {}
            for index in range(3):
                pointer = ctypes.c_void_p()
                require(self.advapi.GetAce(dacl, index, ctypes.byref(pointer)) and pointer,
                        "LOCAL_CPU_PUBLIC_ACL_PROBE_FAILED")
                header = self.initializer._AceHeader.from_address(pointer.value)
                require(header.AceType == 0 and header.AceFlags == (3 if directory else 0)
                        and header.AceSize >= 16, "LOCAL_CPU_PUBLIC_ACL_INVALID")
                sid = ctypes.c_void_p(pointer.value + 8)
                require(self.advapi.IsValidSid(sid) and self.advapi.GetLengthSid(sid) <= header.AceSize - 8,
                        "LOCAL_CPU_PUBLIC_ACL_INVALID")
                name = self.security._sid(sid)
                require(name not in actual, "LOCAL_CPU_PUBLIC_ACL_INVALID")
                actual[name] = wintypes.DWORD.from_address(pointer.value + 4).value
            require(actual == {"S-1-5-18": 0x1F01FF, "S-1-5-32-544": 0x1F01FF,
                               CALLER_SID: 0x1200A9}, "LOCAL_CPU_PUBLIC_ACL_INVALID")
        finally:
            self.kernel.LocalFree(descriptor)

    def create_public(self):
        self._plain_directory(PRIVATE_PARENT)
        require(self.security.attributes(str(PUBLIC_ROOT)) is None, "LOCAL_CPU_PUBLIC_TARGET_EXISTS")
        descriptor = self._descriptor(
            f"O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;{CALLER_SID})")
        try:
            attributes = self.initializer._SecurityAttributes(ctypes.sizeof(self.initializer._SecurityAttributes),
                                                             descriptor, False)
            require(self.kernel.CreateDirectoryW(str(PUBLIC_ROOT), ctypes.byref(attributes)),
                    "LOCAL_CPU_PUBLIC_CREATE_FAILED")
        finally:
            self.kernel.LocalFree(descriptor)
        self._plain_directory(PUBLIC_ROOT)
        self._verify_acl(PUBLIC_ROOT, directory=True)

    def write_status(self, payload):
        raw = (json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        require(len(raw) <= PUBLIC_LIMIT, "LOCAL_CPU_PUBLIC_RECORD_TOO_LARGE")
        self._plain_directory(PUBLIC_ROOT)
        self._verify_acl(PUBLIC_ROOT, directory=True)
        if self.security.attributes(str(PUBLIC_STATUS)) is not None:
            require(self.status_owned, "LOCAL_CPU_PUBLIC_STATUS_NOT_OWNED")
            _plain_file(PUBLIC_STATUS)
            self._verify_acl(PUBLIC_STATUS, directory=False)
        temporary = PUBLIC_ROOT / ("status-" + uuid.uuid4().hex + ".tmp")
        descriptor = self._descriptor(f"O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x1200a9;;;{CALLER_SID})")
        handle = None
        try:
            attributes = self.initializer._SecurityAttributes(ctypes.sizeof(self.initializer._SecurityAttributes),
                                                             descriptor, False)
            handle = self.kernel.CreateFileW(str(temporary), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None)
            require(handle and handle != ctypes.c_void_p(-1).value, "LOCAL_CPU_PUBLIC_FILE_CREATE_FAILED")
            output = ctypes.create_string_buffer(raw)
            written = wintypes.DWORD()
            require(self.kernel.WriteFile(handle, output, len(raw), ctypes.byref(written), None)
                    and written.value == len(raw) and self.kernel.FlushFileBuffers(handle),
                    "LOCAL_CPU_PUBLIC_FILE_WRITE_FAILED")
        finally:
            if handle and handle != ctypes.c_void_p(-1).value:
                self.kernel.CloseHandle(handle)
            self.kernel.LocalFree(descriptor)
        self._verify_acl(temporary, directory=False)
        self._plain_directory(PUBLIC_ROOT)
        _replace_status_file(temporary)
        self.status_owned = True
        self._verify_acl(PUBLIC_STATUS, directory=False)

    def create_ready_event(self):
        name = READY_EVENT_PREFIX + uuid.uuid4().hex
        descriptor = self._descriptor(f"O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100002;;;{CALLER_SID})")
        try:
            attributes = self.initializer._SecurityAttributes(ctypes.sizeof(self.initializer._SecurityAttributes),
                                                             descriptor, False)
            ctypes.set_last_error(0)
            handle = self.kernel.CreateEventW(ctypes.byref(attributes), True, False, name)
            error = ctypes.get_last_error()
            require(handle, "LOCAL_CPU_READY_EVENT_CREATE_FAILED")
            if error == 183:
                self.kernel.CloseHandle(handle)
                raise ElevatedCPUError("LOCAL_CPU_READY_EVENT_ALREADY_EXISTS")
            return name, handle
        finally:
            self.kernel.LocalFree(descriptor)

    def await_reader(self, handle):
        outcome = self.kernel.WaitForSingleObject(handle, READY_TIMEOUT_SECONDS * 1000)
        require(outcome in (0, 258), "LOCAL_CPU_READY_WAIT_FAILED")
        return outcome == 0

    def close_event(self, handle):
        require(self.kernel.CloseHandle(handle), "LOCAL_CPU_READY_EVENT_CLOSE_FAILED")

    def keep_awake(self, enabled):
        require(self.kernel.SetThreadExecutionState(0x80000001 if enabled else 0x80000000),
                "LOCAL_CPU_SLEEP_GUARD_FAILED")


class PublicPublisher:
    def __init__(self, api):
        self.api, self.stop_event, self.lock = api, threading.Event(), threading.Lock()
        self.state = {"schema_version": "1.0", "scene": SCENE, "tool_manifest_sha256": TOOL_SHA256,
                      "profile_sha256": PROFILE_SHA256, "status": "incomplete", "phase": "initialize"}
        self.thread = None
        self.failure = None

    def publish(self, fields):
        with self.lock:
            self.state.update(fields)
            value = {**self.state, "recorded_at_utc": datetime.now(timezone.utc).isoformat()}
            self.api.write_status(value)

    def start(self):
        def loop():
            while not self.stop_event.wait(PUBLISH_INTERVAL_SECONDS):
                try:
                    self.publish(read_public_observation())
                except BaseException as exc:
                    self.failure = _stable_code(exc)
                    return
        self.thread = threading.Thread(target=loop, name="local-cpu-public-status", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(5)
        return (self.thread is None or not self.thread.is_alive()) and self.failure is None


class _BoundedStdout:
    """Suppress tool stdout; only parse a bounded final JSON line for diagnostics."""
    def __init__(self):
        self.tail = ""
    def write(self, value):
        self.tail = (self.tail + value)[-PUBLIC_LIMIT:]
        return len(value)
    def flush(self):
        pass


def load_sealed_cli():
    manifest, raw = _read_document(TOOL_ROOT / "hybrid-tool-manifest.json", 128 * 1024)
    require(hashlib.sha256(raw).hexdigest() == TOOL_SHA256, "LOCAL_CPU_TOOL_MANIFEST_CHANGED")
    profile, raw = _read_document(PROFILE, 128 * 1024)
    require(hashlib.sha256(raw).hexdigest() == PROFILE_SHA256
            and profile.get("target_experiment") == {"scene": SCENE, "root": EXPERIMENT_ROOT.as_posix()},
            "LOCAL_CPU_PROFILE_CHANGED")
    path = _plain_file(TOOL_ROOT / "scripts/run_hybrid_experiment.py")
    raw = path.read_bytes()
    require(len(raw) <= 1024**2 and hashlib.sha256(raw).hexdigest()
            == manifest.get("files", {}).get("scripts/run_hybrid_experiment.py"),
            "LOCAL_CPU_ENTRY_HASH_MISMATCH")
    module = ModuleType("re3d_sealed_local_cpu_cli")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    module.verify_before_import(TOOL_ROOT, TOOL_SHA256)
    # Build a closed vocabulary only from the already fixed code bytes. This
    # lets a failure retain LOW_RAM/TIMED_OUT causes without publishing arbitrary
    # exception text or a user-shaped uppercase "error code".
    codes = set(_COMMON_TOOL_CODES)
    expression = re.compile(rb'''["']((?:CPU_SFM|HYBRID|EXPERIMENT|HIGH_PROFILE|PROFILED_COLMAP|SNAPSHOT|LONG|RESOURCE|LOW|CHECKPOINT|HOST|GPU|DIRECTORY|EVIDENCE)_[A-Z0-9_]{2,80})["']''')
    for name, expected_hash in manifest.get("files", {}).items():
        code_path = _plain_file(TOOL_ROOT / name)
        require(code_path.stat().st_size <= 2 * 1024**2, "LOCAL_CPU_ENTRY_HASH_MISMATCH")
        code_bytes = code_path.read_bytes()
        require(hashlib.sha256(code_bytes).hexdigest() == expected_hash, "LOCAL_CPU_ENTRY_HASH_MISMATCH")
        codes.update(item.decode("ascii") for item in expression.findall(code_bytes))
    module._wrapper_error_codes = frozenset(codes)
    return module


def _failure_fields(captured, cli):
    """Prefer the fixed private result, otherwise a bounded tool JSON line."""
    result = None
    try:
        result, _ = _read_document(RESULT_PATH, 4 * 1024**2)
    except BaseException:
        for line in reversed(captured.tail.splitlines()[-8:]):
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict):
                result = value
                break
    if not isinstance(result, dict):
        return {"status": "failed", "wrapper_error_code": "LOCAL_CPU_TOOL_RUN_FAILED"}
    allowed = getattr(cli, "_wrapper_error_codes", _COMMON_TOOL_CODES)
    status = result.get("status")
    selected = sanitize(result)
    selected.update(status=status if isinstance(status, str) and status in {"failed", "timed_out", "cancelled"}
                    else "failed", phase="terminal", wrapper_error_code="LOCAL_CPU_TOOL_RUN_FAILED")
    code = result.get("error_code")
    if isinstance(code, str) and code in allowed:
        selected["error_code"] = code
    snapshot = result.get("final_snapshot")
    if (result.get("final_snapshot_status") == "valid"
            and all(result.get(key) is True for key in (
                "process_tree_stopped", "snapshot_worker_stopped", "terminal_evidence_confirmed",
                "source_checkpoint_unchanged", "observation_relay_stopped"))
            and isinstance(snapshot, dict)):
        count, digest = snapshot.get("validated_complete_pair_records"), snapshot.get("database_sha256")
        if (type(count) is int and 0 <= count <= 44850 and isinstance(digest, str) and _SHA.fullmatch(digest)
                and snapshot.get("sqlite_backup_completed") is True
                and snapshot.get("sqlite_integrity_and_mapping_validation") is True
                and snapshot.get("database_hash_checked_before_and_after_validation") is True):
            # The final captured transaction set can be newer than the last
            # 15-second public sample. This certifies a checkpoint, not success.
            selected.update(complete_pair_records=count, complete_pairs=count,
                            database_sha256=digest, latest_snapshot_sha256=digest)
    # A SfM stage return code/pair counts only appear after the contained child
    # was actually entered; an early preflight failure must not claim compute.
    if result.get("mode") == "cpu-sfm" and type(result.get("return_code")) is int:
        selected["reconstruction_started"] = True
    else:
        selected["reconstruction_started"] = None
    return selected


def main(*, caller_sid=CALLER_SID, _api=None, _initializer=None, _loader=None,
         _publisher_factory=None, _final_verifier=None):
    """Explicit single launch; private injections exist only for unit tests."""
    api = publisher = event = None
    publisher_stopped = None
    awake = False
    exit_code = 1
    status = {"status": "not_completed", "phase": "initialize", "scene": SCENE,
              "reconstruction_started": False, "execution_entry_invoked": False, "server_accessed": False}
    try:
        require(caller_sid == CALLER_SID, "LOCAL_CPU_CALLER_SID_MISMATCH")
        initializer = _initializer if _initializer is not None else sys.modules.get("re3d_cpu_evidence_initializer")
        require(initializer is not None and callable(getattr(initializer, "perform", None)),
                "LOCAL_CPU_PINNED_INITIALIZER_REQUIRED")
        api = _api if _api is not None else NativeAPI(initializer)
        require(api.is_admin() is True, "LOCAL_CPU_ADMIN_REQUIRED")
        initialized = initializer.perform(operation="initialize")
        require(initialized.get("status") in {"initialized", "already_initialized"}
                and initialized.get("administrator") is True and initialized.get("acl_verified") is True
                and initialized.get("parent_ready") is True and not initialized.get("error_code"),
                "LOCAL_CPU_EVIDENCE_PARENT_NOT_READY")
        api.create_public()
        publisher = (_publisher_factory or PublicPublisher)(api)
        # All code and fixed profile hashes pass before asking the normal reader
        # to acknowledge the safe public channel. Nothing executes at this point.
        cli = (_loader or load_sealed_cli)()
        api.keep_awake(True)
        awake = True
        event_name, event = api.create_ready_event()
        nonce = event_name.removeprefix(READY_EVENT_PREFIX)
        require(event_name.startswith(READY_EVENT_PREFIX) and re.fullmatch(r"[0-9a-f]{32}", nonce),
                "LOCAL_CPU_READY_EVENT_NAME_INVALID")
        publisher.publish({"phase": "ready_waiting_reader", "status": "incomplete",
                           "control_event_name": event_name, "control_nonce": nonce})
        require(api.await_reader(event) is True, "LOCAL_CPU_PUBLIC_READER_UNCONFIRMED")
        api.close_event(event)
        event = None
        publisher.publish({"phase": "preflight", "status": "running"})
        publisher.start()
        status["execution_entry_invoked"] = True
        status["reconstruction_started"] = None
        captured = _BoundedStdout()
        with redirect_stdout(captured):
            code = cli.main(["--mode", "cpu", "--execute-cpu", "--profile", str(PROFILE),
                "--tool-manifest-sha256", TOOL_SHA256, "--pipeline-root", str(PIPELINE_ROOT),
                "--platform-root", str(PLATFORM_ROOT)])
        if code != 0:
            fields = _failure_fields(captured, cli)
            status.update({key: fields[key] for key in ("status", "error_code", "wrapper_error_code",
                                                       "reconstruction_started") if key in fields})
            publisher_stopped = publisher.close()
            require(publisher_stopped, "LOCAL_CPU_PUBLIC_PUBLISHER_NOT_STOPPED")
            publisher.publish(fields)
            raise ElevatedCPUError("LOCAL_CPU_TOOL_RUN_FAILED")
        publisher_stopped = publisher.close()
        require(publisher_stopped, "LOCAL_CPU_PUBLIC_PUBLISHER_NOT_STOPPED")
        terminal = (_final_verifier or verify_final_result)()
        publisher.publish(terminal)
        status.update(status="succeeded", phase="completed", reconstruction_started=True)
        exit_code = 0
    except BaseException as exc:
        status.update(status=status["status"] if status["status"] in {"timed_out", "cancelled"}
                      else "failed" if status["execution_entry_invoked"] else "blocked", phase="terminal")
        status.setdefault("error_code", _stable_code(exc))
        if publisher is not None:
            try:
                if publisher_stopped is None:
                    publisher_stopped = publisher.close()
                if publisher_stopped:
                    publisher.publish({"status": status["status"], "phase": "terminal",
                                       "error_code": status["error_code"]})
                else:
                    # An unconfirmed writer may still own the publish lock or
                    # be blocked in kernel I/O. Never wait on it again or claim
                    # that a public final record was successfully committed.
                    status["public_failure_unconfirmed"] = True
            except BaseException:
                status["public_failure_unconfirmed"] = True
    finally:
        if event is not None and api is not None:
            try:
                api.close_event(event)
            except BaseException:
                status["event_close_failed"] = True
                exit_code = 1
        if awake and api is not None:
            try:
                api.keep_awake(False)
            except BaseException:
                status["sleep_guard_restore_failed"] = True
                exit_code = 1
        if exit_code and status["status"] == "succeeded":
            status.update(status="failed", phase="terminal", error_code="LOCAL_CPU_CLEANUP_FAILED")
            if publisher is not None and publisher_stopped:
                try:
                    publisher.publish({"status": "failed", "phase": "terminal",
                                       "error_code": "LOCAL_CPU_CLEANUP_FAILED"})
                except BaseException:
                    status["public_cleanup_failure_unconfirmed"] = True
        # Final stdout is a bounded, fixed-vocabulary wrapper summary, never the
        # tool's stdout, raw logs, paths, environment or initializer diagnostics.
        print(json.dumps(status, ensure_ascii=True, separators=(",", ":"), allow_nan=False), flush=True)
    return exit_code


if __name__ == "__main__":
    # Direct execution cannot silently import a writable initializer.
    raise SystemExit(main())
