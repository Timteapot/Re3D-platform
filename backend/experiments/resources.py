from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.monitoring.resources import HostResourceReader, NvidiaSmiReader
from backend.re3d_adapter.errors import AdapterError


GIB = 1024**3
PHASE_PATTERN = re.compile(r"[a-z0-9_-]{1,48}\Z")


@dataclass(frozen=True)
class ResourceLimits:
    minimum_disk_free_bytes: int = 20 * GIB
    minimum_available_ram_bytes: int = 32 * GIB
    minimum_free_vram_bytes: int = 2 * GIB
    guard_interval_seconds: float = 1
    resource_interval_seconds: float = 5
    directory_interval_seconds: float = 15
    gpu_required: bool = True
    gpu_index: int = 0

    def __post_init__(self) -> None:
        for name in (
            "minimum_disk_free_bytes",
            "minimum_available_ram_bytes",
            "minimum_free_vram_bytes",
        ):
            value = getattr(self, name)
            if not _integer(value) or value < GIB:
                raise ValueError(f"{name} must be an integer of at least 1 GiB")
        for name, low, high in (
            ("guard_interval_seconds", 1, 5),
            ("resource_interval_seconds", 5, 300),
            ("directory_interval_seconds", 15, 3600),
        ):
            value = getattr(self, name)
            if not _number(value) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        if not isinstance(self.gpu_required, bool):
            raise ValueError("gpu_required must be a boolean")
        if not _integer(self.gpu_index) or self.gpu_index < 0:
            raise ValueError("gpu_index must be a non-negative integer")


class ResourceTrip(AdapterError):
    """Sticky, deliberately sanitized failure for the process-tree supervisor."""

    def __init__(self, code: str, metrics: dict[str, int | float] | None = None):
        super().__init__(code)
        self.code = code
        self.metrics = {
            key: value
            for key, value in (metrics or {}).items()
            if _number(value)
        }


class ExperimentDirectoryReader:
    """Count an independent experiment tree, never following links/reparse points.

    This is only periodic telemetry. Concurrent file creation/deletion can make a
    sample partial, and short-lived files can be missed. No file contents are read.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self) -> tuple[dict[str, int], list[str]]:
        _ensure_plain_directory(self.root)
        stack = [self.root]
        total = 0
        files = 0
        errors: set[str] = set()
        while stack:
            directory = stack.pop()
            # Recheck immediately before scandir, not just when enqueuing it.
            try:
                info = directory.lstat()
            except OSError:
                errors.add("DIRECTORY_SAMPLE_PARTIAL")
                continue
            if _is_reparse(info):
                errors.add("DIRECTORY_REPARSE_SKIPPED")
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if _is_reparse(info):
                                errors.add("DIRECTORY_REPARSE_SKIPPED")
                            elif stat.S_ISDIR(info.st_mode):
                                stack.append(Path(entry.path))
                            elif stat.S_ISREG(info.st_mode):
                                total += info.st_size
                                files += 1
                            else:
                                errors.add("DIRECTORY_SPECIAL_FILE_SKIPPED")
                        except OSError:
                            errors.add("DIRECTORY_SAMPLE_PARTIAL")
            except OSError:
                errors.add("DIRECTORY_SAMPLE_PARTIAL")
        return {"total_bytes": total, "file_count": files}, sorted(errors)


class ExperimentResourceMonitor:
    """Independent, fail-closed guards and whitelisted periodic observations.

    The host, GPU, log writer and directory scan each have their own daemon thread.
    A blocked probe cannot block another guard. The caller MUST poll check_health
    while supervising the process tree (recommended every 250 ms) and terminate
    its owned child tree on ResourceTrip. This class never kills processes itself.
    Thresholds and peaks are sampled boundaries, not OS quotas or exact peaks.
    """

    def __init__(
        self,
        *,
        experiment_root: Path,
        sample_path: Path,
        limits: ResourceLimits,
        nvidia_smi_path: Path | None = None,
        host_reader: Any = None,
        gpu_reader: Any = None,
        directory_reader: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
        _test_intervals: tuple[float, float, float] | None = None,
    ) -> None:
        root = Path(os.path.abspath(experiment_root))
        output = Path(os.path.abspath(sample_path))
        _ensure_plain_directory(root)
        try:
            output.relative_to(root)
        except ValueError:
            raise ValueError("resource samples must remain in experiment root") from None
        _ensure_plain_directory(output.parent)
        if output.exists() or output.is_symlink():
            raise ValueError("resource sample output must not exist")
        self.experiment_root = root
        self.sample_path = output
        self.limits = limits
        self.host_reader = host_reader or HostResourceReader()
        # A CPU-only run must not construct or probe a GPU dependency.
        self.gpu_reader = (gpu_reader or NvidiaSmiReader(
            resource_key=f"gpu:{limits.gpu_index}", executable=nvidia_smi_path
        )) if limits.gpu_required else None
        self.directory_reader = directory_reader or ExperimentDirectoryReader(root)
        self.monotonic = monotonic
        intervals = (
            limits.guard_interval_seconds,
            limits.resource_interval_seconds,
            limits.directory_interval_seconds,
        )
        if _test_intervals is not None:
            if (
                len(_test_intervals) != 3
                or not all(_number(value) and 0 < value <= 0.1 for value in _test_intervals)
                or host_reader is None
                or (limits.gpu_required and gpu_reader is None)
                or directory_reader is None
            ):
                raise ValueError("test intervals require three short intervals and injected readers")
            intervals = _test_intervals
        self._host_interval, self._resource_interval, self._directory_interval = intervals
        self._deadlines = {
            "host": max(3, self._host_interval * 3),
            "gpu": max(10, self._resource_interval * 2),
            "writer": max(10, self._resource_interval * 3),
            "directory": max(45, self._directory_interval * 3),
        }
        if _test_intervals is not None:
            self._deadlines = {name: 0.2 for name in self._deadlines}
        self._stop = threading.Event()
        self._wake_writer = threading.Event()
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._last: dict[str, float] = {}
        self._trip_value: ResourceTrip | None = None
        self._started = False
        self._closed = False
        self._started_at = 0.0
        self._phase = "preflight"
        self._host: dict[str, Any] | None = None
        self._gpu: dict[str, Any] | None = None
        self._directory: dict[str, int] | None = None
        self._descriptor: int | None = None
        self._checks = {"host": 0, "gpu": 0, "supervisor": 0}
        self._sample_count = 0
        self._directory_count = 0
        self._errors: set[str] = set()
        self._peaks: dict[str, int | float | None] = {
            "cpu_utilization_percent": None,
            "ram_used_bytes": None,
            "vram_used_bytes": None,
            "gpu_utilization_percent": None,
            "directory_bytes": None,
            "disk_min_free_bytes": None,
            "ram_min_available_bytes": None,
            "vram_min_free_bytes": None,
        }

    @property
    def trip(self) -> ResourceTrip | None:
        with self._lock:
            return self._trip_value

    def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("resource monitor cannot be started again")
        self._started = True
        self._started_at = self.monotonic()
        try:
            self._descriptor = os.open(
                self.sample_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except Exception:
            self._set_trip("RESOURCE_LOG_WRITE_FAILED")
            self._raise_trip()
        for name, target, interval in (
            ("host", self._probe_host, self._host_interval),
            ("gpu", self._probe_gpu, self._resource_interval),
            ("directory", self._probe_directory, self._directory_interval),
            ("writer", self._write_sample, self._resource_interval),
        ):
            if name == "gpu" and not self.limits.gpu_required:
                continue
            thread = threading.Thread(
                target=self._loop,
                args=(name, target, interval),
                name=f"re3d-experiment-{name}",
                daemon=True,
            )
            self._threads[name] = thread
            thread.start()
        # No synchronous GPU subprocess, recursive scan, or read can hang startup.
        while True:
            self.check_health()
            with self._lock:
                ready = (
                    self._host is not None
                    and (self._gpu is not None or not self.limits.gpu_required)
                    and self._sample_count > 0
                )
            if ready:
                return
            self._stop.wait(min(0.05, self._host_interval))

    def check_health(self) -> None:
        if not self._started:
            raise RuntimeError("resource monitor has not started")
        self._raise_trip()
        if self._closed:
            return
        now = self.monotonic()
        with self._lock:
            self._checks["supervisor"] += 1
            last = dict(self._last)
        for name, thread in self._threads.items():
            if not thread.is_alive():
                self._set_trip("RESOURCE_THREAD_STOPPED")
                break
            age = now - last.get(name, self._started_at)
            if age > self._deadlines[name]:
                self._set_trip(
                    "RESOURCE_PROBE_STALE",
                    {"age_seconds": round(age, 3), "maximum_age_seconds": self._deadlines[name]},
                )
                break
        self._raise_trip()

    def set_phase(self, name: str) -> None:
        if not isinstance(name, str) or PHASE_PATTERN.fullmatch(name) is None:
            raise ValueError("phase must be a short lowercase identifier")
        self.check_health()
        with self._lock:
            self._phase = name
        self._wake_writer.set()

    def finish(self) -> None:
        self.check_health()
        self.close()
        self._raise_trip()

    def close(self) -> None:
        """Bounded cleanup that does not mask an existing supervisor exception."""
        if self._closed:
            return
        self._stop.set()
        self._wake_writer.set()
        deadline = time.monotonic() + 4
        for thread in self._threads.values():
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads.values()):
            self._set_trip("RESOURCE_THREAD_SHUTDOWN_FAILED")
        # A blocked writer owns its descriptor; do not reuse/close it concurrently.
        writer = self._threads.get("writer")
        if self._descriptor is not None and (writer is None or not writer.is_alive()):
            try:
                os.close(self._descriptor)
            except OSError:
                self._set_trip("RESOURCE_LOG_WRITE_FAILED")
            self._descriptor = None
        self._closed = True

    def summary(self) -> dict[str, Any]:
        with self._lock:
            trip = self._trip_value
            return {
                "schema_version": "1.0",
                "limits": asdict(self.limits),
                "phase": self._phase,
                "sample_count": self._sample_count,
                "directory_observation_count": self._directory_count,
                "latest": {
                    **{key: dict(value) for key, value in (self._host or {}).items()},
                    "gpu": dict(self._gpu) if self._gpu else None,
                    "directory": dict(self._directory) if self._directory else None,
                },
                "guard_checks": dict(self._checks),
                "observation_ages_seconds": {
                    name: max(0.0, self.monotonic() - observed) for name, observed in self._last.items()
                },
                "peaks": dict(self._peaks),
                "errors": sorted(self._errors),
                "trip": {"code": trip.code, "metrics": dict(trip.metrics)} if trip else None,
                "limitations": [
                    "PERIODIC_OBSERVATIONS_ARE_NOT_EXACT_PEAKS",
                    "RESOURCE_THRESHOLDS_ARE_SAMPLED_NOT_OS_QUOTAS",
                    "DIRECTORY_SAMPLE_CAN_MISS_SHORT_LIVED_FILES",
                ],
            }

    def _loop(self, name: str, callback: Callable[[], None], interval: float) -> None:
        try:
            while not self._stop.is_set():
                callback()
                if self.trip is not None:
                    return
                if name == "writer":
                    self._wake_writer.wait(interval)
                    self._wake_writer.clear()
                elif self._stop.wait(interval):
                    return
        except BaseException:
            # No original exception text or traceback may enter experiment evidence.
            self._set_trip("RESOURCE_PROBE_FAILED")
        finally:
            if not self._stop.is_set() and self.trip is None:
                self._set_trip("RESOURCE_THREAD_STOPPED")

    def _probe_host(self) -> None:
        try:
            raw, errors = self.host_reader.read(self.experiment_root)
            disk = raw["disk"]
            memory = raw["memory"]
            free = _bytes(disk["free_bytes"])
            total_disk = _bytes(disk["total_bytes"])
            used_ram = _bytes(memory["used_bytes"])
            total_ram = _bytes(memory["total_bytes"])
            if free > total_disk or used_ram > total_ram or total_ram == 0:
                raise ValueError("invalid capacity")
            available = total_ram - used_ram
            cpu = raw["cpu"]
            cpu_percent = _percent(cpu.get("utilization_percent"))
            logical_count = cpu.get("logical_count")
            if logical_count is not None and (not _integer(logical_count) or logical_count < 1):
                raise ValueError("invalid CPU count")
            value = {
                "cpu": {"utilization_percent": cpu_percent, "logical_count": logical_count},
                "memory": {"used_bytes": used_ram, "total_bytes": total_ram, "available_bytes": available},
                "disk": {"free_bytes": free, "total_bytes": total_disk},
            }
        except Exception:
            self._set_trip("HOST_RESOURCE_PROBE_FAILED")
            return
        with self._lock:
            self._host = value
            self._last["host"] = self.monotonic()
            self._checks["host"] += 1
            if errors:
                self._errors.add("HOST_SAMPLE_PARTIAL")
            self._peak("cpu_utilization_percent", cpu_percent)
            self._peak("ram_used_bytes", used_ram)
            self._peak("disk_min_free_bytes", free, minimum=True)
            self._peak("ram_min_available_bytes", available, minimum=True)
        if free <= self.limits.minimum_disk_free_bytes:
            self._set_trip("LOW_DISK_AUTO_STOPPED", {"free_bytes": free, "minimum_free_bytes": self.limits.minimum_disk_free_bytes})
        elif available <= self.limits.minimum_available_ram_bytes:
            self._set_trip("LOW_RAM_AUTO_STOPPED", {"available_bytes": available, "minimum_available_bytes": self.limits.minimum_available_ram_bytes})

    def _probe_gpu(self) -> None:
        if not self.limits.gpu_required:
            return
        try:
            raw, error = self.gpu_reader.read()
            if error is not None or raw.get("status") != "available":
                raise ValueError("unavailable GPU")
            if raw.get("index") != self.limits.gpu_index:
                raise ValueError("unexpected GPU")
            used = _bytes(raw["memory_used_bytes"])
            total = _bytes(raw["memory_total_bytes"])
            if used > total or total == 0:
                raise ValueError("invalid GPU capacity")
            free = total - used
            value = {
                "index": self.limits.gpu_index,
                "utilization_percent": _percent(raw.get("utilization_percent")),
                "memory_used_bytes": used,
                "memory_total_bytes": total,
                "memory_free_bytes": free,
            }
        except Exception:
            if self.limits.gpu_required:
                self._set_trip("GPU_RESOURCE_PROBE_FAILED")
                return
            with self._lock:
                self._gpu = None
                self._last["gpu"] = self.monotonic()
                self._checks["gpu"] += 1
                self._errors.add("GPU_SAMPLE_UNAVAILABLE")
            return
        with self._lock:
            self._gpu = value
            self._last["gpu"] = self.monotonic()
            self._checks["gpu"] += 1
            self._peak("vram_used_bytes", used)
            self._peak("gpu_utilization_percent", value["utilization_percent"])
            self._peak("vram_min_free_bytes", free, minimum=True)
        if self.limits.gpu_required and free <= self.limits.minimum_free_vram_bytes:
            self._set_trip("LOW_VRAM_AUTO_STOPPED", {"free_bytes": free, "minimum_free_bytes": self.limits.minimum_free_vram_bytes})

    def _probe_directory(self) -> None:
        try:
            raw, errors = self.directory_reader.read()
            value = {"total_bytes": _bytes(raw["total_bytes"]), "file_count": _bytes(raw["file_count"])}
        except Exception:
            self._set_trip("DIRECTORY_RESOURCE_PROBE_FAILED")
            return
        with self._lock:
            self._directory = value
            self._last["directory"] = self.monotonic()
            self._directory_count += 1
            self._peak("directory_bytes", value["total_bytes"])
            if errors:
                self._errors.add("DIRECTORY_SAMPLE_PARTIAL")

    def _write_sample(self) -> None:
        with self._lock:
            # Do not label empty startup observations as valid resource samples.
            if self._host is None or (self.limits.gpu_required and self._gpu is None):
                self._last["writer"] = self.monotonic()
                return
            sample = {
                "schema_version": "1.0",
                "sequence": self._sample_count + 1,
                "sampled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "phase": self._phase,
                **self._host,
                "gpu": self._gpu,
                "directory": self._directory,
                "errors": sorted(self._errors),
            }
        try:
            line = (json.dumps(sample, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if self._descriptor is None or os.write(self._descriptor, line) != len(line):
                raise OSError("incomplete write")
            os.fsync(self._descriptor)
        except Exception:
            self._set_trip("RESOURCE_LOG_WRITE_FAILED")
            return
        with self._lock:
            self._sample_count += 1
            self._last["writer"] = self.monotonic()

    def _set_trip(self, code: str, metrics: dict[str, int | float] | None = None) -> None:
        with self._lock:
            self._errors.add(code)
            if self._trip_value is None:
                self._trip_value = ResourceTrip(code, metrics)

    def _raise_trip(self) -> None:
        trip = self.trip
        if trip is not None:
            raise trip

    def _peak(self, name: str, value: int | float | None, *, minimum: bool = False) -> None:
        if value is None:
            return
        old = self._peaks[name]
        self._peaks[name] = value if old is None else min(old, value) if minimum else max(old, value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: Any) -> bool:
    if _integer(value):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _bytes(value: Any) -> int:
    if not _integer(value) or value < 0:
        raise ValueError("invalid resource byte count")
    return value


def _percent(value: Any) -> int | float | None:
    if value is None:
        return None
    if not _number(value) or not 0 <= value <= 100:
        raise ValueError("invalid resource percentage")
    return value


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _ensure_plain_directory(path: Path) -> None:
    try:
        for component in (path, *path.parents):
            info = component.lstat()
            if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError
    except (OSError, ValueError):
        raise ValueError("experiment paths must contain only existing ordinary directories") from None
