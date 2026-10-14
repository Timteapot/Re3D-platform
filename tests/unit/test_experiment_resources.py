from __future__ import annotations

import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.experiments.resources import (
    ExperimentDirectoryReader,
    ExperimentResourceMonitor,
    GIB,
    ResourceLimits,
    ResourceTrip,
    _is_reparse,
)
from backend.re3d_adapter.errors import AdapterError


class HostReader:
    def __init__(self):
        self.free = 100 * GIB
        self.available = 100 * GIB
        self.cpu = 40.0
        self.calls = 0
        self.error = None

    def read(self, _root):
        self.calls += 1
        if self.error:
            raise self.error
        return {
            "cpu": {"utilization_percent": self.cpu, "logical_count": 40},
            "memory": {"used_bytes": 160 * GIB - self.available, "total_bytes": 160 * GIB},
            "disk": {"free_bytes": self.free, "total_bytes": 150 * GIB},
            "private_environment": "password-that-must-not-escape",
        }, []


class GpuReader:
    def __init__(self):
        self.free = 10 * GIB
        self.calls = 0
        self.error = None

    def read(self):
        self.calls += 1
        if self.error:
            raise self.error
        return {
            "status": "available", "index": 0, "name": "private-user-machine",
            "memory_used_bytes": 15 * GIB - self.free, "memory_total_bytes": 15 * GIB,
            "utilization_percent": 60,
        }, None


class DirectoryReader:
    def __init__(self):
        self.calls = 0

    def read(self):
        self.calls += 1
        return {"total_bytes": 1000, "file_count": 10}, []


class ExperimentResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.host = HostReader()
        self.gpu = GpuReader()
        self.directory = DirectoryReader()
        self.monitors = []

    def tearDown(self):
        for monitor in self.monitors:
            monitor.close()
        self.temporary.cleanup()

    def monitor(self, **kwargs):
        monitor = ExperimentResourceMonitor(
            experiment_root=self.root, sample_path=self.root / "resources.jsonl",
            limits=kwargs.pop("limits", ResourceLimits()),
            host_reader=kwargs.pop("host_reader", self.host),
            gpu_reader=kwargs.pop("gpu_reader", self.gpu),
            directory_reader=kwargs.pop("directory_reader", self.directory),
            _test_intervals=(0.05, 0.05, 0.05), **kwargs,
        )
        self.monitors.append(monitor)
        return monitor

    def wait_trip(self, monitor, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                monitor.check_health()
            except ResourceTrip as error:
                self.assertEqual(error.code, expected)
                self.assertIsInstance(error, AdapterError)
                return error
            time.sleep(0.01)
        self.fail("guard did not trip")

    def test_safe_sampling_whitelists_peaks_latest_phase_and_counts(self):
        monitor = self.monitor()
        monitor.start()
        monitor.set_phase("sfm")
        time.sleep(0.08)
        monitor.finish()
        summary = monitor.summary()
        self.assertGreaterEqual(summary["sample_count"], 2)
        self.assertGreater(summary["guard_checks"]["host"], 0)
        self.assertGreater(summary["guard_checks"]["gpu"], 0)
        self.assertGreater(summary["directory_observation_count"], 0)
        self.assertEqual(summary["peaks"]["ram_used_bytes"], 60 * GIB)
        self.assertEqual(summary["peaks"]["disk_min_free_bytes"], 100 * GIB)
        self.assertEqual(summary["latest"]["gpu"]["memory_free_bytes"], 10 * GIB)
        self.assertEqual(summary["peaks"]["directory_bytes"], 1000)
        samples = [json.loads(line) for line in monitor.sample_path.read_text().splitlines()]
        self.assertEqual(len(samples), summary["sample_count"])
        self.assertIn("sfm", [sample["phase"] for sample in samples])
        evidence = json.dumps([summary, samples])
        self.assertNotIn("password-that-must-not-escape", evidence)
        self.assertNotIn("private-user-machine", evidence)
        self.assertNotIn(str(self.root), evidence)

    def test_low_disk_inclusive_and_sticky_after_recovery(self):
        monitor = self.monitor()
        monitor.start()
        self.host.free = 20 * GIB
        first = self.wait_trip(monitor, "LOW_DISK_AUTO_STOPPED")
        self.host.free = 100 * GIB
        with self.assertRaises(ResourceTrip) as caught:
            monitor.check_health()
        self.assertIs(caught.exception, first)
        self.assertEqual(first.metrics["free_bytes"], 20 * GIB)

    def test_low_ram_inclusive(self):
        monitor = self.monitor()
        monitor.start()
        self.host.available = 32 * GIB
        self.wait_trip(monitor, "LOW_RAM_AUTO_STOPPED")

    def test_low_vram_inclusive(self):
        monitor = self.monitor()
        monitor.start()
        self.gpu.free = 2 * GIB
        self.wait_trip(monitor, "LOW_VRAM_AUTO_STOPPED")

    def test_unavailable_gpu_blocks_startup_without_exception_details(self):
        self.gpu.error = RuntimeError("secret path and password")
        monitor = self.monitor()
        with self.assertRaises(ResourceTrip) as caught:
            monitor.start()
        self.assertEqual(caught.exception.code, "GPU_RESOURCE_PROBE_FAILED")
        self.assertNotIn("secret", json.dumps(monitor.summary()))
        self.assertNotIn("secret", str(caught.exception))

    def test_missing_host_probe_fails_closed(self):
        self.host.error = OSError("private host path")
        monitor = self.monitor()
        with self.assertRaises(ResourceTrip) as caught:
            monitor.start()
        self.assertEqual(caught.exception.code, "HOST_RESOURCE_PROBE_FAILED")

    def test_malformed_numeric_values_fail_closed(self):
        self.host.free = True
        monitor = self.monitor()
        with self.assertRaises(ResourceTrip) as caught:
            monitor.start()
        self.assertEqual(caught.exception.code, "HOST_RESOURCE_PROBE_FAILED")

    def test_initial_none_cpu_is_a_valid_warming_up_sample(self):
        self.host.cpu = None
        monitor = self.monitor()
        monitor.start()
        monitor.finish()
        self.assertIsNone(monitor.summary()["peaks"]["cpu_utilization_percent"])

    def test_blocked_gpu_does_not_block_host_guard_and_stale_fails_closed(self):
        release = threading.Event()
        gpu = SimpleNamespace(read=lambda: (release.wait(2), self.gpu.read())[1])
        monitor = self.monitor(gpu_reader=gpu)
        try:
            with self.assertRaises(ResourceTrip) as caught:
                monitor.start()
            self.assertEqual(caught.exception.code, "RESOURCE_PROBE_STALE")
            self.assertGreaterEqual(self.host.calls, 3)
        finally:
            release.set()

    def test_blocked_directory_does_not_block_host_guard(self):
        release = threading.Event()
        directory = SimpleNamespace(read=lambda: (release.wait(2), self.directory.read())[1])
        monitor = self.monitor(directory_reader=directory)
        try:
            monitor.start()
            initial = self.host.calls
            self.wait_trip(monitor, "RESOURCE_PROBE_STALE")
            self.assertGreater(self.host.calls, initial)
        finally:
            release.set()

    def test_stale_injected_clock_trips_without_blocking_io(self):
        value = [1.0]
        monitor = self.monitor(monotonic=lambda: value[0])
        monitor.start()
        value[0] += 0.3
        self.wait_trip(monitor, "RESOURCE_PROBE_STALE")

    def test_log_write_failure_is_fatal_and_sanitized(self):
        monitor = self.monitor()
        with patch("backend.experiments.resources.os.write", side_effect=OSError("secret output path")):
            with self.assertRaises(ResourceTrip) as caught:
                monitor.start()
        self.assertEqual(caught.exception.code, "RESOURCE_LOG_WRITE_FAILED")
        self.assertNotIn("secret output path", json.dumps(monitor.summary()))

    def test_blocked_log_flush_cannot_block_lightweight_host_guard(self):
        release = threading.Event()
        monitor = self.monitor()
        def blocked_flush(_descriptor):
            release.wait(2)
        try:
            with patch("backend.experiments.resources.os.fsync", side_effect=blocked_flush):
                with self.assertRaises(ResourceTrip) as caught:
                    monitor.start()
                self.assertEqual(caught.exception.code, "RESOURCE_PROBE_STALE")
                self.assertGreaterEqual(self.host.calls, 3)
        finally:
            release.set()

    def test_dead_guard_thread_is_detected_by_supervisor(self):
        monitor = self.monitor()
        monitor.start()
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        monitor._threads["host"] = dead
        self.wait_trip(monitor, "RESOURCE_THREAD_STOPPED")

    def test_gpu_index_mismatch_is_fatal(self):
        original = self.gpu.read
        def wrong_gpu():
            value, error = original()
            value["index"] = 1
            return value, error
        monitor = self.monitor(gpu_reader=SimpleNamespace(read=wrong_gpu))
        with self.assertRaises(ResourceTrip) as caught:
            monitor.start()
        self.assertEqual(caught.exception.code, "GPU_RESOURCE_PROBE_FAILED")

    def test_directory_partial_errors_are_only_safe_codes(self):
        monitor = self.monitor(directory_reader=SimpleNamespace(read=lambda: ({"total_bytes": 1, "file_count": 1}, ["secret-name-and-password"])))
        monitor.start()
        monitor.finish()
        summary = monitor.summary()
        self.assertIn("DIRECTORY_SAMPLE_PARTIAL", summary["errors"])
        self.assertNotIn("secret-name-and-password", json.dumps(summary))

    def test_sample_output_cannot_be_reused_or_escape_root(self):
        (self.root / "existing.jsonl").write_text("prior evidence")
        with self.assertRaises(ValueError):
            ExperimentResourceMonitor(experiment_root=self.root, sample_path=self.root / "existing.jsonl", limits=ResourceLimits())
        with self.assertRaises(ValueError):
            ExperimentResourceMonitor(experiment_root=self.root, sample_path=self.root.parent / "escape.jsonl", limits=ResourceLimits())

    def test_limits_validate_real_intervals_and_test_hook_requires_injected_readers(self):
        for kwargs in ({"guard_interval_seconds": 0.05}, {"resource_interval_seconds": 1}, {"directory_interval_seconds": 1}, {"gpu_index": True}, {"minimum_available_ram_bytes": False}):
            with self.assertRaises(ValueError):
                ResourceLimits(**kwargs)
        with self.assertRaises(ValueError):
            ExperimentResourceMonitor(experiment_root=self.root, sample_path=self.root / "test.jsonl", limits=ResourceLimits(), _test_intervals=(0.05, 0.05, 0.05))

    def test_directory_does_not_read_file_contents_and_skips_reparse(self):
        (self.root / "normal.bin").write_bytes(b"abcdef")
        snapshot, errors = ExperimentDirectoryReader(self.root).read()
        self.assertEqual(snapshot, {"total_bytes": 6, "file_count": 1})
        self.assertEqual(errors, [])
        self.assertTrue(_is_reparse(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)))
        self.assertTrue(_is_reparse(SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)))
        self.assertFalse(_is_reparse(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)))

    def test_directory_skips_a_real_symlink_when_host_allows_creation(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "large.bin").write_bytes(b"x" * 99)
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "normal.bin").write_bytes(b"abcdef")
        try:
            (tree / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit creating symlinks")
        snapshot, errors = ExperimentDirectoryReader(tree).read()
        self.assertEqual(snapshot, {"total_bytes": 6, "file_count": 1})
        self.assertEqual(errors, ["DIRECTORY_REPARSE_SKIPPED"])

    def test_phase_labels_reject_paths_and_free_text(self):
        monitor = self.monitor()
        monitor.start()
        for phase in ("D:\\secret\\path", "secret with spaces", "", "X", "x" * 49):
            with self.assertRaises(ValueError):
                monitor.set_phase(phase)

    def test_cpu_only_never_constructs_or_probes_gpu(self):
        self.gpu.error = RuntimeError("not installed")
        with patch("backend.experiments.resources.NvidiaSmiReader", side_effect=AssertionError("GPU forbidden")):
            monitor = self.monitor(limits=ResourceLimits(gpu_required=False), gpu_reader=None)
            monitor.start()
            monitor._probe_gpu()
            monitor.finish()
        self.assertNotIn("gpu", monitor._threads)
        self.assertEqual(self.gpu.calls, 0)
        self.assertIsNone(monitor.gpu_reader)
        self.assertEqual(monitor.summary()["guard_checks"]["gpu"], 0)
        self.assertEqual(monitor.summary()["errors"], [])
        self.assertIsNone(monitor.summary()["trip"])

    def test_finish_reports_a_sticky_guard_trip(self):
        monitor = self.monitor()
        monitor.start()
        self.host.free = 20 * GIB
        self.wait_trip(monitor, "LOW_DISK_AUTO_STOPPED")
        with self.assertRaises(ResourceTrip):
            monitor.finish()
        monitor.close()


if __name__ == "__main__":
    unittest.main()
