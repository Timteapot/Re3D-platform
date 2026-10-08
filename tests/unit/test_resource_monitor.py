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
    TaskStorageReader,
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
                    "RE3D_TASK_STORAGE_SAMPLE_INTERVAL_SECONDS": "90",
                    "RE3D_NVIDIA_SMI_PATH": str(executable),
                },
                clear=True,
            ):
                settings = ResourceMonitorSettings.from_environment()
            self.assertFalse(settings.enabled)
            self.assertEqual(settings.interval_seconds, 30)
            self.assertEqual(settings.task_storage_interval_seconds, 90)
            self.assertEqual(settings.nvidia_smi_path, executable.resolve())

        with self.assertRaisesRegex(ValueError, "between 5 and 300"):
            ResourceMonitorSettings(interval_seconds=1)
        with self.assertRaisesRegex(ValueError, "between 15 and 3600"):
            ResourceMonitorSettings(task_storage_interval_seconds=14)

    def test_task_storage_reader_breaks_down_tiers_and_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "task"
            files = {
                "input/images/camera.jpg": 10,
                "runtime/work/depth.bin": 20,
                "output/A-v4/mesh.glb": 30,
                "output/B-v2/mesh.glb": 40,
                "output/C/mesh.glb": 50,
                "reports/evaluation.json": 6,
                "reports/storage-usage.json": 9,
                "manifests/pipeline-result.json": 7,
                "unexpected.bin": 8,
            }
            for relative, size_bytes in files.items():
                path = task_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * size_bytes)

            snapshot, errors = TaskStorageReader(
                task_root,
                branches=("A-v4", "B-v2", "C"),
            ).read()

        self.assertEqual(errors, [])
        self.assertEqual(
            snapshot["total_bytes"],
            sum(files.values()) - files["reports/storage-usage.json"],
        )
        self.assertEqual(snapshot["input_bytes"], 10)
        self.assertEqual(snapshot["runtime_bytes"], 20)
        self.assertEqual(snapshot["output_bytes"], 120)
        self.assertEqual(snapshot["reports_bytes"], 6)
        self.assertEqual(snapshot["manifests_bytes"], 7)
        self.assertEqual(snapshot["other_bytes"], 8)
        self.assertEqual(
            snapshot["branches"],
            [
                {"name": "A-v4", "used_bytes": 30},
                {"name": "B-v2", "used_bytes": 40},
                {"name": "C", "used_bytes": 50},
            ],
        )

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
            task_root = root / "jobs" / job_id
            (task_root / "input/images").mkdir(parents=True)
            (task_root / "input/images/camera.jpg").write_bytes(b"image")
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
            summary_path = task_root / "reports" / "storage-usage.json"
            with ResourceMonitor(
                path,
                sampler=sampler,
                settings=ResourceMonitorSettings(interval_seconds=5),
                summary_path=summary_path,
            ):
                pass

            samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(samples), 2)
            for sample in samples:
                validate_contract("resource-sample", sample)
                self.assertEqual(sample["job_id"], job_id)
                self.assertEqual(sample["status"], "complete")
                self.assertNotIn(str(root), json.dumps(sample))
            self.assertTrue(all("task_storage" in sample for sample in samples))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            validate_contract("storage-usage", summary)
            self.assertEqual(summary["job_id"], job_id)
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["resource_sample_count"], len(samples))
            self.assertGreaterEqual(summary["storage_observation_count"], 3)
            self.assertGreaterEqual(
                summary["peaks"]["total_bytes"],
                summary["final"]["total_bytes"],
            )

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

    def test_task_storage_scan_is_throttled_and_forced_at_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sampler = SimpleNamespace(
                job_id=str(uuid.uuid4()),
                sample=Mock(return_value={"sample": True}),
            )
            monitor = ResourceMonitor(
                Path(temporary) / "resource-samples.jsonl",
                sampler=sampler,
                settings=ResourceMonitorSettings(
                    interval_seconds=5,
                    task_storage_interval_seconds=60,
                ),
            )

            monitor._write_sample()
            monitor._write_sample()
            monitor._write_sample(force_task_storage=True)

        self.assertEqual(
            [
                call.kwargs["include_task_storage"]
                for call in sampler.sample.call_args_list
            ],
            [True, False, True],
        )

    def test_task_storage_failure_is_partial_and_summary_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id = str(uuid.uuid4())
            (root / "jobs" / job_id).mkdir(parents=True)
            sampler = ResourceSampler(
                data_root=root,
                job_id=job_id,
                attempt=1,
                worker_id="resource-test-worker",
                resource_key="gpu:0",
                host_reader=FakeHostReader(),
                gpu_reader=FakeGpuReader(),
                task_storage_reader=SimpleNamespace(
                    read=Mock(side_effect=OSError("private path detail")),
                ),
            )

            sample = sampler.sample()
            summary = sampler.storage_summary(
                resource_sample_count=1,
                resource_sample_interval_seconds=15,
                task_storage_interval_seconds=60,
            )

        self.assertEqual(sample["status"], "partial")
        self.assertNotIn("task_storage", sample)
        self.assertEqual(sample["errors"], ["TASK_STORAGE_SAMPLE_FAILED"])
        self.assertEqual(summary["status"], "unavailable")
        self.assertIsNone(summary["final"])
        self.assertNotIn("private path detail", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
