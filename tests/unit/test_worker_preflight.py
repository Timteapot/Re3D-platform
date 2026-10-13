from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.worker.preflight import (
    GIBIBYTE,
    WorkerExecutionProfile,
    check_worker_runtime_preflight,
)


class FakeGpuReader:
    def __init__(self, *, total: int = 15 * GIBIBYTE, used: int = GIBIBYTE):
        self.total = total
        self.used = used

    def read(self):
        return (
            {
                "status": "available",
                "index": 0,
                "name": "Tesla T4",
                "memory_used_bytes": self.used,
                "memory_total_bytes": self.total,
            },
            None,
        )


def successful_cuda_probe(*_args, **_kwargs):
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {
                "available": True,
                "device_count": 1,
                "name": "Tesla T4",
                "memory_total_bytes": 15 * GIBIBYTE,
            }
        ),
        stderr="",
    )


class WorkerPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "configs").mkdir()
        (self.root / "configs" / "pipeline.json").write_text(
            json.dumps({"a_v4": {"batch_size": 4}}),
            encoding="utf-8",
        )
        self.driver_python = self.root / "python.exe"
        self.driver_python.touch()
        self.environment = {
            "RE3D_EXECUTION_PROFILE": "gpu-large-100",
            "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(16 * GIBIBYTE),
            "RE3D_MIN_FREE_DISK_BYTES": str(50 * GIBIBYTE),
        }
        self.environment_150 = {
            "RE3D_EXECUTION_PROFILE": "gpu-large-150",
            "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(32 * GIBIBYTE),
            "RE3D_MIN_FREE_DISK_BYTES": str(75 * GIBIBYTE),
        }
        self.environment_300 = {
            "RE3D_EXECUTION_PROFILE": "gpu-large-300",
            "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(64 * GIBIBYTE),
            "RE3D_MIN_FREE_DISK_BYTES": str(75 * GIBIBYTE),
        }
        self.environment_experiment_300 = {
            "APP_ENV": "restricted",
            "RE3D_EXECUTION_PROFILE": "gpu-experiment-300-20",
            "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(64 * GIBIBYTE),
            "RE3D_MIN_FREE_DISK_BYTES": str(20 * GIBIBYTE),
            "RE3D_LOW_DISK_GUARD_ENABLED": "true",
            "RE3D_LOW_DISK_GUARD_INTERVAL_SECONDS": "1",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_large_profile_enforces_provisional_capacity_contract(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = WorkerExecutionProfile.from_environment()
        self.assertEqual(profile.name, "gpu-large-100")
        self.assertEqual(profile.target_image_count, 100)
        self.assertEqual(profile.minimum_job_reservation_bytes, 16 * GIBIBYTE)
        self.assertEqual(profile.minimum_gpu_memory_bytes, 14 * GIBIBYTE)
        self.assertEqual(profile.mapanything_batch_size, 4)

    def test_large_profile_rejects_understated_reservation(self) -> None:
        environment = {
            **self.environment,
            "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(15 * GIBIBYTE),
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "RESERVATION"):
                WorkerExecutionProfile.from_environment()

    def test_large_150_profile_enforces_provisional_capacity_contract(self) -> None:
        with patch.dict(os.environ, self.environment_150, clear=True):
            profile = WorkerExecutionProfile.from_environment()
        self.assertEqual(profile.name, "gpu-large-150")
        self.assertEqual(profile.target_image_count, 150)
        self.assertEqual(profile.minimum_job_reservation_bytes, 32 * GIBIBYTE)
        self.assertEqual(profile.minimum_free_disk_bytes, 75 * GIBIBYTE)
        self.assertEqual(profile.minimum_gpu_memory_bytes, 14 * GIBIBYTE)
        self.assertEqual(profile.minimum_free_gpu_memory_bytes, 12 * GIBIBYTE)
        self.assertEqual(profile.mapanything_batch_size, 4)

    def test_large_150_profile_rejects_understated_capacity(self) -> None:
        invalid_environments = (
            (
                {
                    **self.environment_150,
                    "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(32 * GIBIBYTE - 1),
                },
                "RESERVATION",
            ),
            (
                {
                    **self.environment_150,
                    "RE3D_MIN_FREE_DISK_BYTES": str(75 * GIBIBYTE - 1),
                },
                "MIN_FREE_DISK_BYTES",
            ),
        )
        for environment, message in invalid_environments:
            with self.subTest(message=message):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, message):
                        WorkerExecutionProfile.from_environment()

    def test_large_300_profile_enforces_provisional_capacity_contract(self) -> None:
        with patch.dict(os.environ, self.environment_300, clear=True):
            profile = WorkerExecutionProfile.from_environment()
        self.assertEqual(profile.name, "gpu-large-300")
        self.assertEqual(profile.target_image_count, 300)
        self.assertEqual(profile.minimum_job_reservation_bytes, 64 * GIBIBYTE)
        self.assertEqual(profile.minimum_free_disk_bytes, 75 * GIBIBYTE)
        self.assertEqual(profile.minimum_gpu_memory_bytes, 14 * GIBIBYTE)
        self.assertEqual(profile.minimum_free_gpu_memory_bytes, 12 * GIBIBYTE)
        self.assertEqual(profile.mapanything_batch_size, 4)

    def test_restricted_experiment_keeps_task_and_gpu_minima(self) -> None:
        with patch.dict(os.environ, self.environment_experiment_300, clear=True):
            profile = WorkerExecutionProfile.from_environment()
        self.assertEqual(profile.name, "gpu-experiment-300-20")
        self.assertEqual(profile.target_image_count, 300)
        self.assertEqual(profile.minimum_job_reservation_bytes, 64 * GIBIBYTE)
        self.assertEqual(profile.minimum_free_disk_bytes, 20 * GIBIBYTE)
        self.assertEqual(profile.minimum_gpu_memory_bytes, 14 * GIBIBYTE)
        self.assertEqual(profile.minimum_free_gpu_memory_bytes, 12 * GIBIBYTE)
        self.assertEqual(profile.mapanything_batch_size, 4)

    def test_restricted_experiment_cannot_be_used_outside_restricted(self) -> None:
        for app_env in (None, "development", "test", "production"):
            with self.subTest(app_env=app_env):
                environment = dict(self.environment_experiment_300)
                if app_env is None:
                    environment.pop("APP_ENV")
                else:
                    environment["APP_ENV"] = app_env
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "APP_ENV=restricted"):
                        WorkerExecutionProfile.from_environment()

    def test_restricted_experiment_requires_enabled_fast_guard(self) -> None:
        for enabled in (None, "false"):
            with self.subTest(enabled=enabled):
                environment = dict(self.environment_experiment_300)
                if enabled is None:
                    environment.pop("RE3D_LOW_DISK_GUARD_ENABLED")
                else:
                    environment["RE3D_LOW_DISK_GUARD_ENABLED"] = enabled
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "GUARD_ENABLED"):
                        WorkerExecutionProfile.from_environment()
        for interval in ("0", "6", "nan", "inf"):
            with self.subTest(interval=interval):
                environment = {
                    **self.environment_experiment_300,
                    "RE3D_LOW_DISK_GUARD_INTERVAL_SECONDS": interval,
                }
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValueError):
                        WorkerExecutionProfile.from_environment()

    def test_restricted_experiment_rejects_understated_task_or_floor(self) -> None:
        for key, value in (
            ("RE3D_JOB_STORAGE_RESERVATION_BYTES", 64 * GIBIBYTE - 1),
            ("RE3D_MIN_FREE_DISK_BYTES", 20 * GIBIBYTE - 1),
        ):
            with self.subTest(key=key):
                environment = {**self.environment_experiment_300, key: str(value)}
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, key):
                        WorkerExecutionProfile.from_environment()

    def test_standard_300_profile_still_rejects_the_20_gib_floor(self) -> None:
        environment = {
            **self.environment_experiment_300,
            "RE3D_EXECUTION_PROFILE": "gpu-large-300",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "MIN_FREE_DISK_BYTES"):
                WorkerExecutionProfile.from_environment()

    def test_restricted_experiment_runtime_requires_exact_84_gib(self) -> None:
        with patch.dict(os.environ, self.environment_experiment_300, clear=True):
            report = check_worker_runtime_preflight(
                data_root=self.root,
                re3d_root=self.root,
                driver_python=self.driver_python,
                resource_key="gpu:0",
                nvidia_smi_path=None,
                gpu_reader=FakeGpuReader(),
                disk_usage=lambda _path: SimpleNamespace(free=84 * GIBIBYTE),
                process_runner=successful_cuda_probe,
            )
            with self.assertRaisesRegex(ValueError, "safety floor"):
                check_worker_runtime_preflight(
                    data_root=self.root,
                    re3d_root=self.root,
                    driver_python=self.driver_python,
                    resource_key="gpu:0",
                    nvidia_smi_path=None,
                    gpu_reader=FakeGpuReader(),
                    disk_usage=lambda _path: SimpleNamespace(free=84 * GIBIBYTE - 1),
                    process_runner=successful_cuda_probe,
                )
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["execution_profile"]["name"], "gpu-experiment-300-20")
        self.assertEqual(report["storage"]["required_free_bytes"], 84 * GIBIBYTE)

    def test_restricted_experiment_runtime_still_checks_gpu_and_batch(self) -> None:
        with patch.dict(os.environ, self.environment_experiment_300, clear=True):
            for gpu in (
                FakeGpuReader(total=14 * GIBIBYTE - 1, used=0),
                FakeGpuReader(total=15 * GIBIBYTE, used=3 * GIBIBYTE + 1),
            ):
                with self.subTest(gpu_total=gpu.total, gpu_used=gpu.used):
                    with self.assertRaisesRegex(ValueError, "GPU memory"):
                        check_worker_runtime_preflight(
                            data_root=self.root,
                            re3d_root=self.root,
                            driver_python=self.driver_python,
                            resource_key="gpu:0",
                            nvidia_smi_path=None,
                            gpu_reader=gpu,
                            disk_usage=lambda _path: SimpleNamespace(free=84 * GIBIBYTE),
                            process_runner=successful_cuda_probe,
                        )
            (self.root / "configs" / "pipeline.json").write_text(
                json.dumps({"a_v4": {"batch_size": 3}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "batch size"):
                check_worker_runtime_preflight(
                    data_root=self.root,
                    re3d_root=self.root,
                    driver_python=self.driver_python,
                    resource_key="gpu:0",
                    nvidia_smi_path=None,
                    gpu_reader=FakeGpuReader(),
                    disk_usage=lambda _path: SimpleNamespace(free=84 * GIBIBYTE),
                    process_runner=successful_cuda_probe,
                )

    def test_runtime_preflight_checks_disk_gpu_cuda_and_fixed_batch(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            report = check_worker_runtime_preflight(
                data_root=self.root,
                re3d_root=self.root,
                driver_python=self.driver_python,
                resource_key="gpu:0",
                nvidia_smi_path=None,
                gpu_reader=FakeGpuReader(),
                disk_usage=lambda _path: SimpleNamespace(free=70 * GIBIBYTE),
                process_runner=successful_cuda_probe,
            )
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["execution_profile"]["name"], "gpu-large-100")
        self.assertEqual(report["storage"]["required_free_bytes"], 66 * GIBIBYTE)
        self.assertEqual(report["pipeline"]["mapanything_batch_size"], 4)
        self.assertEqual(report["cuda"]["device_index"], 0)

    def test_runtime_preflight_preserves_floor_after_reservation(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ValueError, "safety floor"):
                check_worker_runtime_preflight(
                    data_root=self.root,
                    re3d_root=self.root,
                    driver_python=self.driver_python,
                    resource_key="gpu:0",
                    nvidia_smi_path=None,
                    gpu_reader=FakeGpuReader(),
                    disk_usage=lambda _path: SimpleNamespace(
                        free=66 * GIBIBYTE - 1
                    ),
                    process_runner=successful_cuda_probe,
                )

    def test_runtime_preflight_accepts_large_150_capacity(self) -> None:
        with patch.dict(os.environ, self.environment_150, clear=True):
            report = check_worker_runtime_preflight(
                data_root=self.root,
                re3d_root=self.root,
                driver_python=self.driver_python,
                resource_key="gpu:0",
                nvidia_smi_path=None,
                gpu_reader=FakeGpuReader(),
                disk_usage=lambda _path: SimpleNamespace(free=110 * GIBIBYTE),
                process_runner=successful_cuda_probe,
            )
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["execution_profile"]["name"], "gpu-large-150")
        self.assertEqual(report["execution_profile"]["target_image_count"], 150)
        self.assertEqual(report["storage"]["required_free_bytes"], 107 * GIBIBYTE)
        self.assertEqual(report["pipeline"]["mapanything_batch_size"], 4)

    def test_runtime_preflight_accepts_large_300_capacity(self) -> None:
        with patch.dict(os.environ, self.environment_300, clear=True):
            report = check_worker_runtime_preflight(
                data_root=self.root,
                re3d_root=self.root,
                driver_python=self.driver_python,
                resource_key="gpu:0",
                nvidia_smi_path=None,
                gpu_reader=FakeGpuReader(),
                disk_usage=lambda _path: SimpleNamespace(free=140 * GIBIBYTE),
                process_runner=successful_cuda_probe,
            )
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["execution_profile"]["name"], "gpu-large-300")
        self.assertEqual(report["execution_profile"]["target_image_count"], 300)
        self.assertEqual(report["storage"]["required_free_bytes"], 139 * GIBIBYTE)
        self.assertEqual(report["pipeline"]["mapanything_batch_size"], 4)

    def test_runtime_preflight_rejects_small_gpu_for_large_profile(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ValueError, "GPU memory"):
                check_worker_runtime_preflight(
                    data_root=self.root,
                    re3d_root=self.root,
                    driver_python=self.driver_python,
                    resource_key="gpu:0",
                    nvidia_smi_path=None,
                    gpu_reader=FakeGpuReader(total=8 * GIBIBYTE, used=0),
                    disk_usage=lambda _path: SimpleNamespace(free=70 * GIBIBYTE),
                    process_runner=successful_cuda_probe,
                )

    def test_operational_templates_keep_api_and_worker_capacity_aligned(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        production_api = _read_environment(
            project_root / ".env.production.example"
        )
        production_worker = _read_environment(
            project_root / ".env.worker.production.example"
        )
        restricted_api = _read_environment(
            project_root / ".env.restricted.example"
        )
        restricted_worker = _read_environment(
            project_root / ".env.worker.restricted.example"
        )

        self.assertEqual(
            production_api["RE3D_JOB_STORAGE_RESERVATION_BYTES"],
            production_worker["RE3D_JOB_STORAGE_RESERVATION_BYTES"],
        )
        self.assertEqual(
            production_worker["RE3D_EXECUTION_PROFILE"],
            "gpu-large-300",
        )
        self.assertEqual(
            production_api["RE3D_USER_STORAGE_QUOTA_BYTES"],
            str(128 * GIBIBYTE),
        )
        self.assertEqual(
            production_worker["RE3D_MIN_FREE_DISK_BYTES"],
            str(75 * GIBIBYTE),
        )
        self.assertEqual(
            restricted_api["RE3D_JOB_STORAGE_RESERVATION_BYTES"],
            restricted_worker["RE3D_JOB_STORAGE_RESERVATION_BYTES"],
        )
        self.assertEqual(
            restricted_worker["RE3D_EXECUTION_PROFILE"],
            "baseline",
        )


def _read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


if __name__ == "__main__":
    unittest.main()
