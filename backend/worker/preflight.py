from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.monitoring.resources import NvidiaSmiReader


GIBIBYTE = 1024**3


@dataclass(frozen=True)
class WorkerExecutionProfile:
    """Auditable host requirements without changing the frozen pipeline."""

    name: str
    target_image_count: int | None
    minimum_job_reservation_bytes: int
    minimum_free_disk_bytes: int
    minimum_gpu_memory_bytes: int
    minimum_free_gpu_memory_bytes: int
    mapanything_batch_size: int = 4

    @classmethod
    def from_environment(cls) -> "WorkerExecutionProfile":
        name = os.environ.get("RE3D_EXECUTION_PROFILE", "baseline").strip().lower()
        profiles = {
            "baseline": cls(
                name="baseline",
                target_image_count=None,
                minimum_job_reservation_bytes=GIBIBYTE,
                minimum_free_disk_bytes=0,
                minimum_gpu_memory_bytes=0,
                minimum_free_gpu_memory_bytes=0,
            ),
            "gpu-large-100": cls(
                name="gpu-large-100",
                target_image_count=100,
                minimum_job_reservation_bytes=16 * GIBIBYTE,
                minimum_free_disk_bytes=50 * GIBIBYTE,
                minimum_gpu_memory_bytes=14 * GIBIBYTE,
                minimum_free_gpu_memory_bytes=12 * GIBIBYTE,
            ),
            "gpu-large-150": cls(
                name="gpu-large-150",
                target_image_count=150,
                minimum_job_reservation_bytes=32 * GIBIBYTE,
                minimum_free_disk_bytes=75 * GIBIBYTE,
                minimum_gpu_memory_bytes=14 * GIBIBYTE,
                minimum_free_gpu_memory_bytes=12 * GIBIBYTE,
            ),
        }
        try:
            profile = profiles[name]
        except KeyError as exc:
            raise ValueError(
                "RE3D_EXECUTION_PROFILE must be baseline, gpu-large-100, "
                "or gpu-large-150"
            ) from exc

        reservation = _environment_integer(
            "RE3D_JOB_STORAGE_RESERVATION_BYTES",
            default=2 * GIBIBYTE,
        )
        disk_floor = _environment_integer(
            "RE3D_MIN_FREE_DISK_BYTES",
            default=0,
        )
        if reservation < profile.minimum_job_reservation_bytes:
            raise ValueError(
                "RE3D_JOB_STORAGE_RESERVATION_BYTES is below the selected "
                "execution profile minimum"
            )
        if disk_floor < profile.minimum_free_disk_bytes:
            raise ValueError(
                "RE3D_MIN_FREE_DISK_BYTES is below the selected execution "
                "profile minimum"
            )
        return profile

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "name": self.name,
            "target_image_count": self.target_image_count,
            "minimum_job_reservation_bytes": self.minimum_job_reservation_bytes,
            "minimum_free_disk_bytes": self.minimum_free_disk_bytes,
            "minimum_gpu_memory_bytes": self.minimum_gpu_memory_bytes,
            "minimum_free_gpu_memory_bytes": self.minimum_free_gpu_memory_bytes,
            "mapanything_batch_size": self.mapanything_batch_size,
        }


def check_worker_runtime_preflight(
    *,
    data_root: Path,
    re3d_root: Path,
    driver_python: Path,
    resource_key: str,
    nvidia_smi_path: Path | None,
    gpu_reader: NvidiaSmiReader | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Fail closed before a real queue loop can claim a GPU task."""

    profile = WorkerExecutionProfile.from_environment()
    reservation_bytes = _environment_integer(
        "RE3D_JOB_STORAGE_RESERVATION_BYTES",
        default=2 * GIBIBYTE,
    )
    configured_floor = _environment_integer(
        "RE3D_MIN_FREE_DISK_BYTES",
        default=0,
    )
    try:
        free_bytes = int(disk_usage(data_root).free)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ValueError("worker data-volume free space cannot be inspected") from exc
    required_free_bytes = configured_floor + reservation_bytes
    if free_bytes < required_free_bytes:
        raise ValueError(
            "worker data volume cannot preserve the safety floor after one "
            "job reservation"
        )

    reader = gpu_reader or NvidiaSmiReader(
        resource_key=resource_key,
        executable=nvidia_smi_path,
    )
    gpu, gpu_error = reader.read()
    if gpu_error is not None or gpu.get("status") != "available":
        raise ValueError("configured NVIDIA GPU is not available")
    total_gpu_bytes = int(gpu["memory_total_bytes"])
    free_gpu_bytes = total_gpu_bytes - int(gpu["memory_used_bytes"])
    if total_gpu_bytes < profile.minimum_gpu_memory_bytes:
        raise ValueError("GPU memory is below the selected execution profile minimum")
    if free_gpu_bytes < profile.minimum_free_gpu_memory_bytes:
        raise ValueError(
            "available GPU memory is below the selected execution profile minimum"
        )

    pipeline_batch_size = _read_pipeline_batch_size(re3d_root)
    if pipeline_batch_size != profile.mapanything_batch_size:
        raise ValueError(
            "installed Re3D MapAnything batch size does not match the selected "
            "execution profile"
        )
    cuda = _probe_cuda(
        driver_python=driver_python,
        gpu_index=int(gpu["index"]),
        process_runner=process_runner,
    )
    if int(cuda["memory_total_bytes"]) < profile.minimum_gpu_memory_bytes:
        raise ValueError(
            "PyTorch CUDA device memory is below the selected execution profile minimum"
        )

    return {
        "status": "ready",
        "execution_profile": profile.as_dict(),
        "storage": {
            "free_bytes": free_bytes,
            "minimum_free_bytes": configured_floor,
            "job_reservation_bytes": reservation_bytes,
            "required_free_bytes": required_free_bytes,
        },
        "gpu": {
            "index": int(gpu["index"]),
            "name": str(gpu["name"]),
            "memory_total_bytes": total_gpu_bytes,
            "memory_free_bytes": free_gpu_bytes,
        },
        "cuda": cuda,
        "pipeline": {"mapanything_batch_size": pipeline_batch_size},
    }


def _probe_cuda(
    *,
    driver_python: Path,
    gpu_index: int,
    process_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, int | str]:
    if not driver_python.is_file():
        raise ValueError("configured Re3D driver Python does not exist")
    probe = (
        "import json,sys,torch; "
        "i=int(sys.argv[1]); "
        "ok=torch.cuda.is_available() and torch.cuda.device_count()>i; "
        "p=torch.cuda.get_device_properties(i) if ok else None; "
        "print(json.dumps({'available':bool(ok),'device_count':torch.cuda.device_count(),"
        "'name':p.name if p else '', 'memory_total_bytes':p.total_memory if p else 0}))"
    )
    try:
        completed = process_runner(
            [str(driver_python), "-c", probe, str(gpu_index)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("PyTorch CUDA probe could not be executed") from exc
    if completed.returncode != 0:
        raise ValueError("PyTorch CUDA probe failed")
    try:
        payload = json.loads(completed.stdout.strip())
        available = payload["available"] is True
        device_count = int(payload["device_count"])
        name = str(payload["name"]).strip()
        memory_total_bytes = int(payload["memory_total_bytes"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("PyTorch CUDA probe returned invalid output") from exc
    if not available or device_count <= gpu_index or not name or memory_total_bytes < 1:
        raise ValueError("PyTorch cannot use the configured CUDA device")
    return {
        "device_count": device_count,
        "device_index": gpu_index,
        "device_name": name[:128],
        "memory_total_bytes": memory_total_bytes,
    }


def _read_pipeline_batch_size(re3d_root: Path) -> int:
    path = re3d_root / "configs" / "pipeline.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        value = int(payload["a_v4"]["batch_size"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("installed Re3D MapAnything batch size cannot be read") from exc
    if value < 1:
        raise ValueError("installed Re3D MapAnything batch size is invalid")
    return value


def _environment_integer(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value
