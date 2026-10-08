from __future__ import annotations

import csv
import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.re3d_adapter.contracts import validate_contract
from backend.re3d_adapter.io import atomic_write_json
from backend.re3d_adapter.paths import TaskLayout


LOGGER = logging.getLogger(__name__)
GPU_RESOURCE_PATTERN = re.compile(r"^gpu:(\d+)$")
MEBIBYTE = 1024 * 1024
DEFAULT_BRANCHES = ("A-v4", "B-v2", "C")
TASK_STORAGE_CATEGORIES = ("input", "runtime", "output", "reports", "manifests")
STORAGE_SUMMARY_PARTS = ("reports", "storage-usage.json")


@dataclass(frozen=True)
class ResourceMonitorSettings:
    enabled: bool = True
    interval_seconds: int = 15
    task_storage_interval_seconds: int = 60
    nvidia_smi_path: Path | None = None

    def __post_init__(self) -> None:
        if not 5 <= self.interval_seconds <= 300:
            raise ValueError(
                "RE3D_RESOURCE_SAMPLE_INTERVAL_SECONDS must be between 5 and 300"
            )
        if not 15 <= self.task_storage_interval_seconds <= 3600:
            raise ValueError(
                "RE3D_TASK_STORAGE_SAMPLE_INTERVAL_SECONDS must be between "
                "15 and 3600"
            )
        if self.nvidia_smi_path is not None and not self.nvidia_smi_path.is_file():
            raise ValueError("RE3D_NVIDIA_SMI_PATH must reference an existing file")

    @classmethod
    def from_environment(cls) -> "ResourceMonitorSettings":
        configured_path = os.environ.get("RE3D_NVIDIA_SMI_PATH", "").strip()
        return cls(
            enabled=_read_bool("RE3D_RESOURCE_MONITOR_ENABLED", True),
            interval_seconds=_read_int(
                "RE3D_RESOURCE_SAMPLE_INTERVAL_SECONDS",
                15,
            ),
            task_storage_interval_seconds=_read_int(
                "RE3D_TASK_STORAGE_SAMPLE_INTERVAL_SECONDS",
                60,
            ),
            nvidia_smi_path=(
                Path(configured_path).expanduser().resolve()
                if configured_path
                else None
            ),
        )


class HostResourceReader:
    """Read host CPU, memory and data-volume usage without third-party packages."""

    def __init__(self) -> None:
        self._previous_cpu: tuple[int, int] | None = None

    def read(self, data_root: Path) -> tuple[dict[str, Any], list[str]]:
        errors: list[str] = []
        cpu_percent: float | None = None
        memory_used: int | None = None
        memory_total: int | None = None
        disk_used: int | None = None
        disk_free: int | None = None
        disk_total: int | None = None

        try:
            idle, total = _system_cpu_times()
            if self._previous_cpu is not None:
                previous_idle, previous_total = self._previous_cpu
                total_delta = total - previous_total
                idle_delta = idle - previous_idle
                if total_delta > 0:
                    cpu_percent = _percentage(total_delta - idle_delta, total_delta)
            self._previous_cpu = (idle, total)
        except (IndexError, OSError, ValueError):
            errors.append("CPU_SAMPLE_FAILED")

        try:
            memory_used, memory_total = _system_memory_bytes()
        except (IndexError, OSError, ValueError):
            errors.append("MEMORY_SAMPLE_FAILED")

        try:
            usage = shutil.disk_usage(data_root)
            disk_used = usage.used
            disk_free = usage.free
            disk_total = usage.total
        except OSError:
            errors.append("DISK_SAMPLE_FAILED")

        return (
            {
                "cpu": {
                    "utilization_percent": cpu_percent,
                    "logical_count": os.cpu_count(),
                },
                "memory": {
                    "used_bytes": memory_used,
                    "total_bytes": memory_total,
                    "utilization_percent": _optional_percentage(
                        memory_used,
                        memory_total,
                    ),
                },
                "disk": {
                    "used_bytes": disk_used,
                    "free_bytes": disk_free,
                    "total_bytes": disk_total,
                    "utilization_percent": _optional_percentage(
                        disk_used,
                        disk_total,
                    ),
                },
            },
            errors,
        )


class NvidiaSmiReader:
    def __init__(
        self,
        *,
        resource_key: str,
        executable: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        match = GPU_RESOURCE_PATTERN.fullmatch(resource_key)
        self.gpu_index = int(match.group(1)) if match is not None else None
        self.executable = executable or _discover_nvidia_smi()
        self.runner = runner

    def read(self) -> tuple[dict[str, Any], str | None]:
        if self.gpu_index is None:
            return _unavailable_gpu("GPU_INDEX_UNMAPPED"), "GPU_INDEX_UNMAPPED"
        if self.executable is None:
            return _unavailable_gpu("GPU_TOOL_NOT_FOUND"), "GPU_TOOL_NOT_FOUND"
        try:
            completed = self.runner(
                [
                    str(self.executable),
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                    f"--id={self.gpu_index}",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            return (
                _unavailable_gpu("GPU_QUERY_TIMEOUT", status="error"),
                "GPU_QUERY_TIMEOUT",
            )
        except OSError:
            return (
                _unavailable_gpu("GPU_QUERY_FAILED", status="error"),
                "GPU_QUERY_FAILED",
            )
        if completed.returncode != 0:
            return (
                _unavailable_gpu("GPU_QUERY_FAILED", status="error"),
                "GPU_QUERY_FAILED",
            )
        try:
            rows = list(csv.reader(completed.stdout.splitlines(), skipinitialspace=True))
            if len(rows) != 1 or len(rows[0]) != 6:
                raise ValueError("unexpected nvidia-smi row")
            index, name, utilization, memory_used, memory_total, temperature = (
                item.strip() for item in rows[0]
            )
            if int(index) != self.gpu_index:
                raise ValueError("nvidia-smi returned an unexpected GPU")
            if not name:
                raise ValueError("nvidia-smi returned an empty GPU name")
            memory_used_bytes = _mebibytes_to_bytes(memory_used)
            memory_total_bytes = _mebibytes_to_bytes(memory_total)
            if memory_used_bytes > memory_total_bytes:
                raise ValueError("GPU memory usage exceeds capacity")
            gpu = {
                "status": "available",
                "index": self.gpu_index,
                "name": name[:128],
                "utilization_percent": _bounded_number(utilization, 0, 100),
                "memory_used_bytes": memory_used_bytes,
                "memory_total_bytes": memory_total_bytes,
                "temperature_c": _bounded_number(temperature, -100, 200),
                "error_code": None,
            }
        except (TypeError, ValueError):
            return (
                _unavailable_gpu("GPU_QUERY_INVALID", status="error"),
                "GPU_QUERY_INVALID",
            )
        return gpu, None


class TaskStorageReader:
    """Measure one canonical task directory without following links."""

    def __init__(self, task_root: Path, *, branches: tuple[str, ...]) -> None:
        self.task_root = task_root
        self.branches = branches

    def read(self) -> tuple[dict[str, Any], list[str]]:
        category_bytes = {name: 0 for name in TASK_STORAGE_CATEGORIES}
        branch_bytes = {name: 0 for name in self.branches}
        other_bytes = 0
        errors: set[str] = set()
        stack: list[tuple[Path, tuple[str, ...]]] = [(self.task_root, ())]

        while stack:
            directory, relative_parts = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError:
                if directory == self.task_root:
                    raise
                errors.add("TASK_STORAGE_SAMPLE_FAILED")
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink() or getattr(
                        path,
                        "is_junction",
                        lambda: False,
                    )():
                        errors.add("TASK_STORAGE_LINK_SKIPPED")
                        continue
                    parts = (*relative_parts, entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((path, parts))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        errors.add("TASK_STORAGE_SPECIAL_FILE_SKIPPED")
                        continue
                    if parts == STORAGE_SUMMARY_PARTS:
                        continue
                    size_bytes = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    errors.add("TASK_STORAGE_SAMPLE_FAILED")
                    continue

                top_level = parts[0]
                if top_level in category_bytes:
                    category_bytes[top_level] += size_bytes
                else:
                    other_bytes += size_bytes
                if (
                    top_level == "output"
                    and len(parts) >= 2
                    and parts[1] in branch_bytes
                ):
                    branch_bytes[parts[1]] += size_bytes

        total_bytes = sum(category_bytes.values()) + other_bytes
        return (
            {
                "total_bytes": total_bytes,
                **{
                    f"{category}_bytes": category_bytes[category]
                    for category in TASK_STORAGE_CATEGORIES
                },
                "other_bytes": other_bytes,
                "branches": [
                    {"name": branch, "used_bytes": branch_bytes[branch]}
                    for branch in self.branches
                ],
            },
            sorted(errors),
        )


class ResourceSampler:
    def __init__(
        self,
        *,
        data_root: Path,
        job_id: str,
        attempt: int,
        worker_id: str,
        resource_key: str,
        nvidia_smi_path: Path | None = None,
        host_reader: HostResourceReader | None = None,
        gpu_reader: NvidiaSmiReader | None = None,
        task_storage_reader: TaskStorageReader | None = None,
        branches: tuple[str, ...] = DEFAULT_BRANCHES,
    ) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.job_id = str(uuid.UUID(job_id))
        self.attempt = attempt
        self.worker_id = worker_id
        if len(set(branches)) != len(branches) or not branches:
            raise ValueError("resource sample branches must be unique and non-empty")
        self.branches = branches
        self.host_reader = host_reader or HostResourceReader()
        self.gpu_reader = gpu_reader or NvidiaSmiReader(
            resource_key=resource_key,
            executable=nvidia_smi_path,
        )
        layout = TaskLayout.from_data_root(self.data_root, self.job_id)
        self.task_storage_reader = task_storage_reader or TaskStorageReader(
            layout.root,
            branches=branches,
        )
        self._peak_total_bytes = 0
        self._peak_runtime_bytes = 0
        self._peak_output_bytes = 0
        self._peak_branch_bytes = {branch: 0 for branch in branches}
        self._storage_observation_count = 0
        self._storage_errors: set[str] = set()

    def sample(self, *, include_task_storage: bool = True) -> dict[str, Any]:
        host, errors = self.host_reader.read(self.data_root)
        gpu, gpu_error = self.gpu_reader.read()
        if gpu_error is not None:
            errors.append(gpu_error)
        task_storage: dict[str, Any] | None = None
        if include_task_storage:
            task_storage, storage_errors = self._read_task_storage()
            errors.extend(storage_errors)
        unique_errors = sorted(set(errors))
        cpu_percent = host["cpu"]["utilization_percent"]
        status = (
            "partial"
            if unique_errors
            else "warming_up"
            if cpu_percent is None
            else "complete"
        )
        sample = {
            "contract_version": "1.0",
            "sample_id": str(uuid.uuid4()),
            "job_id": self.job_id,
            "attempt": self.attempt,
            "sampled_at": _utc_now(),
            "worker_id": self.worker_id,
            "status": status,
            **host,
            "gpu": gpu,
            "errors": unique_errors,
        }
        if include_task_storage and task_storage is not None:
            sample["task_storage"] = task_storage
        validate_contract("resource-sample", sample)
        return sample

    def storage_summary(
        self,
        *,
        resource_sample_count: int,
        resource_sample_interval_seconds: int,
        task_storage_interval_seconds: int,
    ) -> dict[str, Any]:
        final_storage, final_errors = self._read_task_storage()
        self._storage_errors.update(final_errors)
        status = (
            "unavailable"
            if final_storage is None
            else "partial"
            if self._storage_errors
            else "complete"
        )
        summary = {
            "contract_version": "1.0",
            "job_id": self.job_id,
            "attempt": self.attempt,
            "measured_at": _utc_now(),
            "status": status,
            "resource_sample_interval_seconds": resource_sample_interval_seconds,
            "task_storage_interval_seconds": task_storage_interval_seconds,
            "resource_sample_count": resource_sample_count,
            "storage_observation_count": self._storage_observation_count,
            "final": final_storage,
            "peaks": {
                "total_bytes": self._peak_total_bytes,
                "runtime_bytes": self._peak_runtime_bytes,
                "output_bytes": self._peak_output_bytes,
                "branches": [
                    {
                        "name": branch,
                        "used_bytes": self._peak_branch_bytes[branch],
                    }
                    for branch in self.branches
                ],
            },
            "errors": sorted(self._storage_errors),
            "limitations": [
                "PERIODIC_OBSERVATION_MAY_MISS_SHORT_LIVED_FILES",
                "SUMMARY_FILE_BYTES_ARE_EXCLUDED",
            ],
        }
        validate_contract("storage-usage", summary)
        return summary

    def _read_task_storage(self) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            storage, errors = self.task_storage_reader.read()
        except Exception:
            self._storage_errors.add("TASK_STORAGE_SAMPLE_FAILED")
            return None, ["TASK_STORAGE_SAMPLE_FAILED"]
        self._storage_observation_count += 1
        self._storage_errors.update(errors)
        self._peak_total_bytes = max(
            self._peak_total_bytes,
            storage["total_bytes"],
        )
        self._peak_runtime_bytes = max(
            self._peak_runtime_bytes,
            storage["runtime_bytes"],
        )
        self._peak_output_bytes = max(
            self._peak_output_bytes,
            storage["output_bytes"],
        )
        for branch in storage["branches"]:
            name = branch["name"]
            self._peak_branch_bytes[name] = max(
                self._peak_branch_bytes[name],
                branch["used_bytes"],
            )
        return storage, errors


class ResourceMonitor:
    """Append private resource samples without changing task success semantics."""

    def __init__(
        self,
        path: Path,
        *,
        sampler: ResourceSampler,
        settings: ResourceMonitorSettings,
        summary_path: Path | None = None,
    ) -> None:
        self.path = path
        self.sampler = sampler
        self.settings = settings
        self.summary_path = summary_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._resource_sample_count = 0
        self._last_task_storage_sample_at: float | None = None

    def __enter__(self) -> "ResourceMonitor":
        if not self.settings.enabled:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name=f"re3d-resource-monitor-{self.sampler.job_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            LOGGER.error("Resource monitor did not stop for job %s", self.sampler.job_id)
            return
        self._write_sample(force_task_storage=True)
        self._write_storage_summary()

    def _run(self) -> None:
        while True:
            self._write_sample()
            if self._stop.wait(self.settings.interval_seconds):
                return

    def _write_sample(self, *, force_task_storage: bool = False) -> None:
        try:
            now = time.monotonic()
            include_task_storage = (
                force_task_storage
                or self._last_task_storage_sample_at is None
                or now - self._last_task_storage_sample_at
                >= self.settings.task_storage_interval_seconds
            )
            sample = self.sampler.sample(
                include_task_storage=include_task_storage,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = (
                json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                if os.write(descriptor, line) != len(line):
                    raise OSError("resource sample append was incomplete")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._resource_sample_count += 1
            if include_task_storage:
                self._last_task_storage_sample_at = now
        except Exception:
            LOGGER.exception("Resource sampling failed for job %s", self.sampler.job_id)

    def _write_storage_summary(self) -> None:
        if self.summary_path is None:
            return
        try:
            summary = self.sampler.storage_summary(
                resource_sample_count=self._resource_sample_count,
                resource_sample_interval_seconds=self.settings.interval_seconds,
                task_storage_interval_seconds=(
                    self.settings.task_storage_interval_seconds
                ),
            )
            atomic_write_json(self.summary_path, summary)
        except Exception:
            LOGGER.exception(
                "Task storage summary failed for job %s",
                self.sampler.job_id,
            )


def _read_bool(name: str, default: bool) -> bool:
    configured = os.environ.get(name)
    if configured is None:
        return default
    normalized = configured.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _read_int(name: str, default: int) -> int:
    configured = os.environ.get(name)
    if configured is None:
        return default
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _discover_nvidia_smi() -> Path | None:
    discovered = shutil.which("nvidia-smi")
    if discovered:
        return Path(discovered).resolve()
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = Path(system_root) / "System32" / "nvidia-smi.exe"
        if candidate.is_file():
            return candidate.resolve()
    return None


def _system_cpu_times() -> tuple[int, int]:
    if os.name == "nt":
        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

            def integer(self) -> int:
                return (self.high << 32) | self.low

        idle = FileTime()
        kernel = FileTime()
        user = FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError("GetSystemTimes failed")
        return idle.integer(), kernel.integer() + user.integer()

    stat_path = Path("/proc/stat")
    if stat_path.is_file():
        first_line = stat_path.read_text(encoding="ascii").splitlines()[0]
        fields = first_line.split()
        if not fields or fields[0] != "cpu" or len(fields) < 5:
            raise ValueError("invalid /proc/stat")
        values = [int(value) for value in fields[1:9]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values)
    raise OSError("system CPU counters are unavailable")


def _system_memory_bytes() -> tuple[int, int]:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
            ctypes.byref(status)
        ):
            raise OSError("GlobalMemoryStatusEx failed")
        return (
            int(status.total_physical - status.available_physical),
            int(status.total_physical),
        )

    memory_path = Path("/proc/meminfo")
    if memory_path.is_file():
        values: dict[str, int] = {}
        for line in memory_path.read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
        if {"MemTotal", "MemAvailable"} <= values.keys():
            return values["MemTotal"] - values["MemAvailable"], values["MemTotal"]
        raise ValueError("invalid /proc/meminfo")
    raise OSError("system memory counters are unavailable")


def _unavailable_gpu(
    error_code: str,
    *,
    status: str = "unavailable",
) -> dict[str, Any]:
    return {
        "status": status,
        "index": None,
        "name": None,
        "utilization_percent": None,
        "memory_used_bytes": None,
        "memory_total_bytes": None,
        "temperature_c": None,
        "error_code": error_code,
    }


def _mebibytes_to_bytes(raw: str) -> int:
    value = _bounded_number(raw, 0, float("inf"))
    return int(value * MEBIBYTE)


def _bounded_number(raw: str, minimum: float, maximum: float) -> float:
    if raw in {"", "N/A", "[N/A]"}:
        raise ValueError("metric is unavailable")
    value = float(raw)
    if not minimum <= value <= maximum:
        raise ValueError("metric is outside its valid range")
    return value


def _optional_percentage(used: int | None, total: int | None) -> float | None:
    if used is None or total is None or total <= 0:
        return None
    return _percentage(used, total)


def _percentage(numerator: int, denominator: int) -> float:
    return round(max(0.0, min(100.0, numerator * 100 / denominator)), 3)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
