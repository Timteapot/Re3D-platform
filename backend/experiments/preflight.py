"""Read-only host/queue isolation gates for the opt-in checkpoint experiment."""
from __future__ import annotations

import base64
import ctypes
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable

from backend.re3d_adapter.errors import AdapterError

SERVICES = ("Re3DRestrictedApi", "Re3DRestrictedCaddy", "Re3DRestrictedMailpit", "Re3DRestrictedWorker")
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


class ExperimentGateError(AdapterError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(value: bool, code: str) -> None:
    if not value:
        raise ExperimentGateError(code)


def read_windows_state() -> dict:
    _require(os.name == "nt", "EXPERIMENT_TARGET_WINDOWS_REQUIRED")
    script = r"""$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$services=@(Get-Service Re3DRestrictedApi,Re3DRestrictedCaddy,Re3DRestrictedMailpit,Re3DRestrictedWorker | ForEach-Object {@{name=$_.Name;status=$_.Status.ToString()}})
$processes=@(Get-CimInstance Win32_Process | ForEach-Object {@{pid=$_.ProcessId;parent_pid=$_.ParentProcessId;creation_time=if($_.CreationDate){$_.CreationDate.ToUniversalTime().ToString('o')}else{$null};reconstruction=($_.Name-in @('DensifyPointCloud.exe','ReconstructMesh.exe','TextureMesh.exe','InterfaceCOLMAP.exe')-or ($_.Name-in @('python.exe','pythonw.exe')-and $_.CommandLine-match '(run_pipeline\.py|run_colmap_shared\.py|run_profiled_colmap\.py|run_long_experiment\.py|run_checkpoint_experiment\.py|run_mapanything_batched\.py|run_mvsanywhere\.py)'))}})
@{services=$services;processes=$processes}|ConvertTo-Json -Compress -Depth 4"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded],
            capture_output=True, encoding="utf-8", errors="replace", timeout=8, check=False,
        )
        _require(result.returncode == 0, "EXPERIMENT_HOST_GATE_PROBE_FAILED")
        value = json.loads(result.stdout)
        _require(isinstance(value, dict), "EXPERIMENT_HOST_GATE_PROBE_FAILED")
        return value
    except ExperimentGateError:
        raise
    except Exception:
        raise ExperimentGateError("EXPERIMENT_HOST_GATE_PROBE_FAILED") from None


def _windows_argv(command_line: str) -> list[str]:
    """Parse only inside the private ownership probe; never serialize argv."""
    _require(isinstance(command_line, str) and bool(command_line), "EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED")
    api = ctypes.WinDLL("shell32", use_last_error=True)
    api.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    api.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    value = api.CommandLineToArgvW(command_line, ctypes.byref(count))
    _require(bool(value), "EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED")
    try:
        return [value[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))


def _verified_launcher_instances(rows: list[dict], platform_root: Path, *, current_pid: int,
                                 parent_pid: int, executable: str, base_executable: str,
                                 argv_reader: Callable[[str], list[str]] = _windows_argv) -> dict[int, str]:
    """A direct, exact Windows venv redirector is the only extra owned root."""
    code = "EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED"
    _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), code)
    by_pid = {row.get("pid"): row for row in rows}
    _require(len(by_pid) == len(rows) and current_pid in by_pid, code)
    current = by_pid[current_pid]
    creation = current.get("creation_time")
    _require(isinstance(creation, str) and bool(creation) and current.get("parent_pid") == parent_pid, code)
    instances = {current_pid: creation}
    expected = str(platform_root / ".venv" / "Scripts" / "python.exe")
    def canonical(path):
        _require(isinstance(path, str) and ntpath.isabs(path), code)
        return ntpath.normcase(ntpath.normpath(path))
    if canonical(executable) != canonical(expected):
        return instances  # Not the platform venv entry: no ancestor exemption.
    actual = canonical(current.get("executable"))
    if actual == canonical(expected):
        return instances  # No redirector; the current process is the entry.
    _require(actual == canonical(base_executable) and canonical(base_executable) != canonical(expected), code)
    _require(parent_pid in by_pid and parent_pid != current_pid, code)
    parent = by_pid[parent_pid]
    parent_creation = parent.get("creation_time")
    _require(canonical(parent.get("executable")) == canonical(expected)
             and isinstance(parent_creation, str) and bool(parent_creation), code)
    try:
        parent_started, current_started = datetime.fromisoformat(parent_creation), datetime.fromisoformat(creation)
        _require(parent_started.tzinfo is not None and current_started.tzinfo is not None
                 and parent_started <= current_started, code)
        own_argv = argv_reader(current.get("command_line"))
        parent_argv = argv_reader(parent.get("command_line"))
        _require(len(own_argv) > 1 and own_argv[1:] == parent_argv[1:], code)
    except ExperimentGateError:
        raise
    except Exception:
        raise ExperimentGateError(code) from None
    instances[parent_pid] = parent_creation
    return instances


def read_current_launcher_ownership(platform_root: Path) -> dict[int, str]:
    """Read self/direct parent identity once; keep raw CIM command lines private."""
    _require(os.name == "nt", "EXPERIMENT_TARGET_WINDOWS_REQUIRED")
    current_pid, parent_pid = os.getpid(), os.getppid()
    script = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        f"$rows=@(Get-CimInstance Win32_Process -Filter 'ProcessId={current_pid} OR ProcessId={parent_pid}' "
        "|ForEach-Object {@{pid=$_.ProcessId;parent_pid=$_.ParentProcessId;executable=$_.ExecutablePath;"
        "command_line=$_.CommandLine;creation_time=if($_.CreationDate){$_.CreationDate.ToUniversalTime().ToString('o')}else{$null}}});"
        "ConvertTo-Json -InputObject $rows -Compress -Depth 3"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded],
            capture_output=True, encoding="utf-8", errors="replace", timeout=8, check=False,
        )
        _require(result.returncode == 0, "EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED")
        return _verified_launcher_instances(json.loads(result.stdout), platform_root,
            current_pid=current_pid, parent_pid=parent_pid, executable=sys.executable,
            base_executable=getattr(sys, "_base_executable", sys.executable))
    except ExperimentGateError:
        raise
    except Exception:
        raise ExperimentGateError("EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED") from None


def read_queue_state(platform_root: Path) -> dict:
    # Credentials remain in the existing protected server file and never enter
    # public output, provenance, command lines, copied payloads or exception text.
    from dotenv import dotenv_values
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.orm import Session
    from backend.db.models import JobUpload, ReconstructionJob, WorkerLease
    engine = None
    try:
        config = dotenv_values(platform_root / ".env.worker.restricted")
        url = config.get("DATABASE_URL")
        _require(isinstance(url, str) and url.startswith("postgresql"), "EXPERIMENT_QUEUE_CONFIG_UNAVAILABLE")
        engine = create_engine(url, echo=False, hide_parameters=True, connect_args={"connect_timeout": 3})
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            jobs = session.execute(select(ReconstructionJob.id, ReconstructionJob.status).where(ReconstructionJob.status.not_in(TERMINAL))).all()
            uploads = session.execute(select(JobUpload.id).where(JobUpload.status == "uploading")).all()
            leases = session.execute(select(WorkerLease.resource_key, WorkerLease.job_id, WorkerLease.expires_at)).all()
            return {
                "read_only": True,
                "nonterminal_jobs": [{"id": str(row.id), "status": row.status} for row in jobs],
                "active_uploads": len(uploads),
                "leases": [{"resource_key": row.resource_key, "job_id": str(row.job_id) if row.job_id else None,
                            "expires_at": row.expires_at.isoformat() if row.expires_at else None} for row in leases],
            }
    except ExperimentGateError:
        raise
    except Exception:
        raise ExperimentGateError("EXPERIMENT_QUEUE_PROBE_FAILED") from None
    finally:
        if engine is not None:
            engine.dispose()


def validate_isolation(state: dict, source_job_id: str, *, allow_expired_source_job: bool,
                       owned_pids: set[int] | None = None, now: datetime | None = None,
                       owned_process_instances: dict[int, str] | None = None,
                       launcher_pids: set[int] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    host, queue = state["host"], state["queue"]
    services = {row["name"]: row["status"] for row in host["services"]}
    _require(set(services) == set(SERVICES) and all(v == "Stopped" for v in services.values()),
             "EXPERIMENT_PLATFORM_SERVICES_NOT_STOPPED")
    rows = host["processes"]
    owned = set(owned_pids or ())
    launchers = set(launcher_pids or ())
    _require(launchers <= set(owned_process_instances or {}) and launchers <= owned,
             "EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED")
    by_pid = {row["pid"]: row for row in rows}
    _require(not owned_process_instances or len(by_pid) == len(rows), "EXPERIMENT_LAUNCHER_INSTANCE_CHANGED")
    for pid, creation in (owned_process_instances or {}).items():
        _require(pid in owned and pid in by_pid and by_pid[pid].get("creation_time") == creation,
                 "EXPERIMENT_LAUNCHER_INSTANCE_CHANGED")
    while True:
        # The verified redirector is an owned leaf, not a blanket grant to
        # siblings of the current supervisor or additional launcher children.
        added = {row["pid"] for row in rows if row["parent_pid"] in owned - launchers} - owned
        if not added:
            break
        owned.update(added)
    _require(not any(row["reconstruction"] and row["pid"] not in owned for row in rows),
             "EXPERIMENT_OTHER_RECONSTRUCTION_ACTIVE")
    _require(queue.get("read_only") is True and queue["active_uploads"] == 0,
             "EXPERIMENT_ACTIVE_UPLOAD_OR_QUEUE_INVALID")
    jobs = queue["nonterminal_jobs"]
    _require(len(jobs) <= 1 and all(row["id"] == source_job_id and row["status"] == "sfm" for row in jobs),
             "EXPERIMENT_OTHER_NONTERMINAL_JOB")
    _require(not jobs or allow_expired_source_job, "EXPERIMENT_STALE_SOURCE_REQUIRES_AUTHORIZATION")
    stale_lease = False
    for lease in queue["leases"]:
        if lease["job_id"] is None:
            _require(lease["expires_at"] is None, "EXPERIMENT_QUEUE_LEASE_INVALID")
            continue
        _require(allow_expired_source_job and lease["job_id"] == source_job_id and bool(jobs),
                 "EXPERIMENT_FOREIGN_GPU_LEASE")
        try:
            expires = datetime.fromisoformat(lease["expires_at"])
            _require(expires.tzinfo is not None and expires <= now, "EXPERIMENT_GPU_LEASE_NOT_EXPIRED")
        except (TypeError, ValueError):
            raise ExperimentGateError("EXPERIMENT_QUEUE_LEASE_INVALID") from None
        stale_lease = True
    return {"services_stopped": True, "foreign_reconstruction_processes": 0,
            "active_uploads": 0, "source_stale_record_preserved": bool(jobs),
            "source_expired_lease_preserved": stale_lease, "queue_modified": False}


class IsolationGuard:
    """Fail closed if stopped-service/queue isolation changes or becomes stale."""
    def __init__(self, reader: Callable[[], dict], source_job_id: str, *, allow_expired_source_job: bool,
                 interval_seconds: float = 3, stale_seconds: float = 15, static_owned_pids: set[int] | None = None,
                 static_owned_process_instances: dict[int, str] | None = None,
                 static_launcher_pids: set[int] | None = None):
        self.reader = reader
        self.source_job_id = source_job_id
        self.allow_expired = allow_expired_source_job
        self.interval = interval_seconds
        self.stale_seconds = stale_seconds
        self.owned_pids: set[int] = set()
        self.static_owned_pids = frozenset(static_owned_pids or ())
        self.static_owned_process_instances = dict(static_owned_process_instances or {})
        self.static_launcher_pids = frozenset(static_launcher_pids or ())
        _require(set(self.static_owned_process_instances) <= self.static_owned_pids,
                 "EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED")
        _require(self.static_launcher_pids <= set(self.static_owned_process_instances),
                 "EXPERIMENT_LAUNCHER_OWNERSHIP_UNVERIFIED")
        self.ownership_generation = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.last_checked = 0.0
        self.last_summary: dict = {}

    def register_pid(self, pid: int) -> None:
        with self.lock:
            self.owned_pids.add(pid)
            self.ownership_generation += 1

    def clear_owned_pids(self) -> None:
        # Only after the contained process supervisor proves its private Job
        # empty. Do not trust an exited PID during post-run verification.
        with self.lock:
            self.owned_pids.clear()
            self.ownership_generation += 1

    def start(self) -> None:
        _require(self.thread is None, "EXPERIMENT_ISOLATION_ALREADY_STARTED")
        self.thread = threading.Thread(target=self._run, name="experiment-isolation", daemon=True)
        self.thread.start()
        if not self.ready_event.wait(self.stale_seconds):
            self.error = "EXPERIMENT_ISOLATION_PROBE_STALE"
        self.check_health()

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    generation = self.ownership_generation
                state = self.reader()
                with self.lock:
                    if generation != self.ownership_generation:
                        continue  # Discard a snapshot spanning start/stop, never clear a trip.
                    owned = set(self.owned_pids) | set(self.static_owned_pids)
                summary = validate_isolation(state, self.source_job_id, allow_expired_source_job=self.allow_expired,
                    owned_pids=owned, owned_process_instances=self.static_owned_process_instances,
                    launcher_pids=set(self.static_launcher_pids))
                self.last_summary = summary
                self.last_checked = time.monotonic()
                self.ready_event.set()
                if self.stop_event.wait(self.interval):
                    return
        except ExperimentGateError as exc:
            self.error = exc.code
        except BaseException:
            self.error = "EXPERIMENT_ISOLATION_PROBE_FAILED"
        finally:
            self.ready_event.set()

    def check_health(self) -> None:
        if self.error:
            raise ExperimentGateError(self.error)
        if self.thread is None or not self.thread.is_alive() or time.monotonic() - self.last_checked > self.stale_seconds:
            raise ExperimentGateError("EXPERIMENT_ISOLATION_PROBE_STALE")

    def close(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=0.2)


def verify_git_identity(root: Path, expected_commit: str) -> None:
    try:
        base = ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root)]
        head = subprocess.run(base + ["rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False)
        dirty = subprocess.run(base + ["status", "--porcelain"], capture_output=True, text=True, timeout=10, check=False)
        _require(head.returncode == 0 and dirty.returncode == 0 and head.stdout.strip() == expected_commit and not dirty.stdout.strip(),
                 "EXPERIMENT_SOURCE_IDENTITY_OR_DIRTY_TREE")
    except ExperimentGateError:
        raise
    except Exception:
        raise ExperimentGateError("EXPERIMENT_SOURCE_IDENTITY_PROBE_FAILED") from None


def require_admin() -> None:
    _require(os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin()), "EXPERIMENT_ADMIN_REQUIRED")


def read_runtime_versions(interpreter: Path) -> dict:
    try:
        code = "import json,sys,pycolmap;print(json.dumps(dict(python_version=sys.version.split()[0],pycolmap_version=pycolmap.__version__,pycolmap_has_cuda=bool(pycolmap.has_cuda))))"
        result = subprocess.run([str(interpreter), "-c", code], capture_output=True, text=True, timeout=15, check=False)
        _require(result.returncode == 0, "EXPERIMENT_RUNTIME_VERSION_FAILED")
        return json.loads(result.stdout)
    except ExperimentGateError:
        raise
    except Exception:
        raise ExperimentGateError("EXPERIMENT_RUNTIME_VERSION_FAILED") from None
