from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.monitoring.resources import (
    NvidiaSmiReader,
    ResourceMonitor,
    ResourceMonitorSettings,
    ResourceSampler,
)
from backend.re3d_adapter.contracts import validate_contract


class FakeHostReader:
    def read(self, _data_root: Path):
        return (
            {
                "cpu": {"utilization_percent": 25.0, "logical_count": 8},
                "memory": {
                    "used_bytes": 4 * 1024**3,
                    "total_bytes": 16 * 1024**3,
                    "utilization_percent": 25.0,
                },
                "disk": {
                    "used_bytes": 100,
                    "free_bytes": 900,
                    "total_bytes": 1000,
                    "utilization_percent": 10.0,
                },
            },
            [],
        )


class FakeGpuReader:
    def read(self):
        return (
            {
                "status": "available",
                "index": 0,
                "name": "Test GPU",
                "utilization_percent": 75.0,
                "memory_used_bytes": 2 * 1024**3,
                "memory_total_bytes": 8 * 1024**3,
                "temperature_c": 60.0,
                "error_code": None,
            },
            None,
        )


class ResourceMonitorTests(unittest.TestCase):
    def test_settings_read_environment_and_validate_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "nvidia-smi.exe"
            executable.touch()
            with patch.dict(
                os.environ,
                {
                    "RE3D_RESOURCE_MONITOR_ENABLED": "false",
                    "RE3D_RESOURCE_SAMPLE_INTERVAL_SECONDS": "30",
                    "RE3D_NVIDIA_SMI_PATH": str(executable),
                },
                clear=True,
            ):
                settings = ResourceMonitorSettings.from_environment()
            self.assertFalse(settings.enabled)
            self.assertEqual(settings.interval_seconds, 30)
            self.assertEqual(settings.nvidia_smi_path, executable.resolve())

        with self.assertRaisesRegex(ValueError, "between 5 and 300"):
            ResourceMonitorSettings(interval_seconds=1)

    def test_nvidia_smi_reader_normalizes_metrics_to_bytes(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="0, Test GPU, 81, 2048, 8192, 66\n",
                stderr="",
            )
        )
        gpu, error = NvidiaSmiReader(
            resource_key="gpu:0",
            executable=Path("nvidia-smi"),
            runner=runner,
        ).read()

        self.assertIsNone(error)
        self.assertEqual(gpu["status"], "available")
        self.assertEqual(gpu["utilization_percent"], 81.0)
        self.assertEqual(gpu["memory_used_bytes"], 2048 * 1024**2)
        self.assertEqual(gpu["memory_total_bytes"], 8192 * 1024**2)
        self.assertEqual(gpu["temperature_c"], 66.0)

    def test_unmapped_gpu_resource_is_reported_without_running_a_command(self) -> None:
        runner = Mock()
        gpu, error = NvidiaSmiReader(
            resource_key="gpu:test-real",
            executable=Path("nvidia-smi"),
            runner=runner,
        ).read()

        self.assertEqual(error, "GPU_INDEX_UNMAPPED")
        self.assertEqual(gpu["status"], "unavailable")
        runner.assert_not_called()

    def test_sampler_and_monitor_write_private_contract_valid_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id = str(uuid.uuid4())
            sampler = ResourceSampler(
                data_root=root,
                job_id=job_id,
                attempt=1,
                worker_id="resource-test-worker",
                resource_key="gpu:0",
                host_reader=FakeHostReader(),
                gpu_reader=FakeGpuReader(),
            )
            path = root / "jobs" / job_id / "runtime" / "metrics" / "resource-samples.jsonl"
            with ResourceMonitor(
                path,
                sampler=sampler,
                settings=ResourceMonitorSettings(interval_seconds=5),
            ):
                pass

            samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(samples), 2)
            for sample in samples:
                validate_contract("resource-sample", sample)
                self.assertEqual(sample["job_id"], job_id)
                self.assertEqual(sample["status"], "complete")
                self.assertNotIn(str(root), json.dumps(sample))

    def test_sampling_failure_does_not_fail_the_task_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sampler = SimpleNamespace(
                job_id=str(uuid.uuid4()),
                sample=Mock(side_effect=RuntimeError("private diagnostic")),
            )
            path = root / "resource-samples.jsonl"
            with self.assertLogs("backend.monitoring.resources", level="ERROR"):
                with ResourceMonitor(
                    path,
                    sampler=sampler,
                    settings=ResourceMonitorSettings(interval_seconds=5),
                ):
                    pass
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
