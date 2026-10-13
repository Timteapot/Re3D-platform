from __future__ import annotations

import io
import ctypes
import os
import queue as thread_queue
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import select

from backend.db.errors import LeaseLostError
from backend.db.models import TaskActionEvent, WorkerLease
from backend.db.state_machine import JobStatus
from backend.monitoring import ResourceMonitorSettings
from backend.re3d_adapter.errors import AdapterError, PipelineTimedOut
from backend.re3d_adapter.process import _drain_output
from backend.re3d_adapter.real import RealPipelineRunner
from backend.worker.disk_guard import (
    GIB,
    DiskGuard,
    DiskGuardCancelled,
    DiskGuardSettings,
)
from backend.worker.queued import QueuedRealWorker
from tests.unit.test_real_worker import create_queued_real_job
from tests.unit.test_worker_simulation import read_events


FLOOR = 20 * GIB
HEALTHY = FLOOR + GIB
ENABLED = DiskGuardSettings(enabled=True, minimum_free_bytes=FLOOR)


def wait_until(predicate, *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("controlled test condition was not reached")


def process_running(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An exited descendant can remain a zombie until the test host's init reaps it.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].strip().split()[0] != "Z"
        except FileNotFoundError:
            return False
    return True


class DiskGuardTests(unittest.TestCase):
    def test_settings_default_and_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(DiskGuardSettings.from_environment(), DiskGuardSettings())
        with patch.dict(
            os.environ,
            {
                "RE3D_LOW_DISK_GUARD_ENABLED": "true",
                "RE3D_MIN_FREE_DISK_BYTES": str(FLOOR),
                "RE3D_LOW_DISK_GUARD_INTERVAL_SECONDS": "2.5",
            },
            clear=True,
        ):
            settings = DiskGuardSettings.from_environment()
        self.assertEqual(settings.as_dict(), {
            "enabled": True,
            "minimum_free_bytes": FLOOR,
            "interval_seconds": 2.5,
        })
        for value in ("yes", "1", "", "maybe"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"RE3D_LOW_DISK_GUARD_ENABLED": value}, clear=True),
                self.assertRaises(ValueError),
            ):
                DiskGuardSettings.from_environment()

    def test_settings_reject_unsafe_values(self) -> None:
        for args in (
            {"enabled": 1},
            {"minimum_free_bytes": True},
            {"minimum_free_bytes": -1},
            {"enabled": True, "minimum_free_bytes": GIB - 1},
            {"interval_seconds": 0.99},
            {"interval_seconds": 5.01},
            {"interval_seconds": float("nan")},
            {"interval_seconds": float("inf")},
            {"interval_seconds": True},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                DiskGuardSettings(**args)

    def test_disabled_guard_never_reads_or_starts_thread(self) -> None:
        reader = Mock(side_effect=AssertionError("disabled guard performed I/O"))
        guard = DiskGuard(Path("unused"), settings=DiskGuardSettings(), free_bytes_reader=reader)
        with guard:
            guard.check_now()
            guard.raise_if_tripped()
            guard.finish()
        self.assertFalse(guard.thread_alive)
        reader.assert_not_called()

    def test_initial_threshold_is_inclusive_and_sticky(self) -> None:
        reader = Mock(side_effect=[FLOOR, HEALTHY])
        guard = DiskGuard(Path("unused"), settings=ENABLED, free_bytes_reader=reader)
        with self.assertRaises(DiskGuardCancelled) as caught:
            guard.start()
        self.assertEqual(caught.exception.trip.code, "LOW_DISK_AUTO_CANCELLED")
        self.assertEqual(caught.exception.trip.metrics(), {
            "minimum_free_bytes": FLOOR,
            "observed_free_bytes": FLOOR,
        })
        with self.assertRaises(DiskGuardCancelled):
            guard.check_now()
        self.assertEqual(reader.call_count, 1)
        self.assertFalse(guard.thread_alive)

    def test_probe_exception_and_invalid_result_fail_closed_without_private_text(self) -> None:
        for result in (OSError("private-volume-password"), -1, True, 1.5, None):
            reader = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            guard = DiskGuard(Path("private-volume-name"), settings=ENABLED, free_bytes_reader=reader)
            with self.subTest(result=result), self.assertRaises(DiskGuardCancelled) as caught:
                guard.start()
            self.assertEqual(caught.exception.trip.code, "DISK_GUARD_PROBE_FAILED")
            self.assertIsNone(caught.exception.trip.observed_free_bytes)
            self.assertNotIn("private", str(caught.exception))

    def test_background_sampling_trips_and_thread_exits(self) -> None:
        low = threading.Event()
        guard = DiskGuard(
            Path("unused"), settings=ENABLED,
            free_bytes_reader=lambda _root: FLOOR if low.is_set() else HEALTHY,
        )
        with guard:
            low.set()
            wait_until(lambda: guard.trip is not None, timeout=3)
            with self.assertRaises(DiskGuardCancelled):
                guard.raise_if_tripped()
        self.assertFalse(guard.thread_alive)

    def test_unexpected_sampler_exit_fail_closed(self) -> None:
        calls = 0

        def reader(_root: Path) -> int:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise SystemExit("private failure")
            return HEALTHY

        guard = DiskGuard(Path("unused"), settings=ENABLED, free_bytes_reader=reader)
        with guard:
            wait_until(lambda: guard.trip is not None, timeout=3)
        self.assertEqual(guard.trip.code, "DISK_GUARD_PROBE_FAILED")
        self.assertFalse(guard.thread_alive)

    def test_finish_rechecks_after_sampler_is_stopped(self) -> None:
        reader = Mock(side_effect=[HEALTHY, FLOOR])
        guard = DiskGuard(Path("unused"), settings=ENABLED, free_bytes_reader=reader)
        with guard:
            with self.assertRaises(DiskGuardCancelled):
                guard.finish()
        self.assertFalse(guard.thread_alive)

    def test_exit_failure_does_not_replace_lease_loss(self) -> None:
        guard = DiskGuard(Path("unused"), settings=ENABLED)
        with patch.object(guard, "stop", side_effect=DiskGuardCancelled(
            SimpleNamespace(safe_message="safe")
        )):
            self.assertFalse(guard.__exit__(LeaseLostError, LeaseLostError("lost"), None))
            with self.assertRaises(DiskGuardCancelled):
                guard.__exit__(None, None, None)

    def test_stdout_drain_services_health_without_discarding_lines(self) -> None:
        lines = thread_queue.Queue()
        for index in range(200):
            lines.put(f"line-{index}\n")
        health = Mock()
        log = io.StringIO()
        seen = []
        _drain_output(lines, log, seen.append, health)
        self.assertEqual(health.call_count, 4)
        self.assertEqual(len(seen), 200)
        self.assertEqual(log.getvalue().count("\n"), 200)
        health = Mock(side_effect=[None, DiskGuardCancelled(
            SimpleNamespace(safe_message="safe")
        )])
        for index in range(100):
            lines.put(f"line-{index}\n")
        with self.assertRaises(DiskGuardCancelled):
            _drain_output(lines, io.StringIO(), lambda _line: None, health)
        self.assertEqual(lines.qsize(), 36)


class LowDiskRealWorkerTests(unittest.TestCase):
    @staticmethod
    def worker(root, queue, fake_re3d, reader, **kwargs) -> QueuedRealWorker:
        return QueuedRealWorker(
            queue,
            data_root=root / "data",
            re3d_root=fake_re3d,
            driver_python=Path(sys.executable),
            worker_id="low-disk-test",
            resource_key="gpu:test-real",
            lease_seconds=5,
            heartbeat_seconds=0.1,
            process_poll_seconds=0.02,
            installation_verifier=lambda _root, _pipeline: None,
            resource_monitor_settings=ResourceMonitorSettings(enabled=False),
            disk_guard_settings=ENABLED,
            disk_free_bytes_reader=reader,
            **kwargs,
        )

    def assert_cancelled(self, queue, request, layout, code) -> None:
        snapshot = queue.get_job(uuid.UUID(request["job_id"]))
        self.assertEqual(snapshot["status"], JobStatus.CANCELLED)
        self.assertEqual(snapshot["error_code"], code)
        events = read_events(layout.events_path)
        self.assertEqual(events[-1]["type"], "job_cancelled")
        self.assertEqual(events[-1]["message_code"], code)
        self.assertFalse(any(item["type"] == "job_succeeded" for item in events))
        with queue.session_factory() as session:
            self.assertIsNone(session.get(WorkerLease, "gpu:test-real").job_id)
            self.assertEqual(len(session.execute(select(TaskActionEvent)).scalars().all()), 0)
        if code != "USER_CANCELLED":
            self.assertFalse(snapshot["cancel_requested"])
            self.assertEqual(set(events[-1]["metrics"]), {
                "minimum_free_bytes", "observed_free_bytes",
            })

    def test_initial_low_disk_and_probe_failure_cancel_without_user_audit(self) -> None:
        for reader, code in (
            (lambda _root: FLOOR, "LOW_DISK_AUTO_CANCELLED"),
            (Mock(side_effect=OSError("private password")), "DISK_GUARD_PROBE_FAILED"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layout, request, engine, queue, fake = create_queued_real_job(root)
                try:
                    outcome = self.worker(root, queue, fake, reader).run_once()
                    self.assertEqual(outcome.status, "cancelled")
                    self.assert_cancelled(queue, request, layout, code)
                    self.assertFalse(layout.result_path.exists())
                    self.assertFalse(layout.resolve("runtime/logs/re3d-run.log").exists())
                    self.assertNotIn("private password", layout.events_path.read_text(encoding="utf-8"))
                finally:
                    engine.dispose()

    def test_healthy_guard_allows_normal_success_with_monitor_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout, request, engine, queue, fake = create_queued_real_job(root)
            try:
                outcome = self.worker(root, queue, fake, lambda _root: HEALTHY).run_once()
                self.assertEqual(outcome.status, "succeeded")
                self.assertTrue(layout.evaluation_path.exists())
                self.assertFalse(layout.resolve("runtime/metrics/resource-samples.jsonl").exists())
                self.assertFalse(any(thread.name == "re3d-low-disk-guard" and thread.is_alive()
                                     for thread in threading.enumerate()))
            finally:
                engine.dispose()

    def test_runtime_low_disk_kills_real_parent_child_and_grandchild_without_monitor(self) -> None:
        self._assert_runtime_guard_tree_cancel(probe_failure=False)

    def test_runtime_probe_failure_kills_real_parent_child_and_grandchild_without_monitor(self) -> None:
        self._assert_runtime_guard_tree_cancel(probe_failure=True)

    def _assert_runtime_guard_tree_cancel(self, *, probe_failure: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout, request, engine, queue, fake = create_queued_real_job(root, slow=True)
            pid_paths = [root / f"controlled-{name}.pid" for name in ("parent", "child", "grandchild")]
            # Only these isolated test-owned Python processes are ever launched.
            grandchild = (
                "import os,time; from pathlib import Path; "
                f"Path({str(pid_paths[2])!r}).write_text(str(os.getpid())); time.sleep(90)"
            )
            child = (
                "import os,time,subprocess,sys; from pathlib import Path; "
                f"Path({str(pid_paths[1])!r}).write_text(str(os.getpid())); "
                f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(90)"
            )
            (fake / "scripts" / "run_pipeline.py").write_text(
                "import os,time,subprocess,sys\nfrom pathlib import Path\n"
                f"Path({str(pid_paths[0])!r}).write_text(str(os.getpid()))\n"
                f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                "print('[run] 00-prepare-input',flush=True)\ntime.sleep(90)\n",
                encoding="utf-8",
            )
            low = threading.Event()
            outcomes, failures = [], []

            def reader(_root: Path) -> int:
                if low.is_set():
                    if probe_failure:
                        raise OSError("private probe diagnostics")
                    return FLOOR
                return HEALTHY

            worker = self.worker(root, queue, fake, reader)

            def run() -> None:
                try:
                    outcomes.append(worker.run_once())
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=run)
            try:
                thread.start()
                wait_until(lambda: all(path.exists() and path.stat().st_size for path in pid_paths))
                pids = [int(path.read_text()) for path in pid_paths]
                self.assertTrue(all(process_running(pid) for pid in pids))
                low.set()
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(len(outcomes), 1)
                self.assert_cancelled(queue, request, layout,
                                      "DISK_GUARD_PROBE_FAILED" if probe_failure else "LOW_DISK_AUTO_CANCELLED")
                self.assertFalse(layout.result_path.exists())
                self.assertFalse(layout.resolve("runtime/metrics/resource-samples.jsonl").exists())
                for pid in pids:
                    wait_until(lambda pid=pid: not process_running(pid))
            finally:
                if thread.is_alive():
                    queue.request_cancel(uuid.UUID(request["job_id"]))
                    thread.join(timeout=8)
                engine.dispose()

    def test_zero_exit_race_guard_trips_before_result_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout, request, engine, queue, fake = create_queued_real_job(root)
            process_finished = threading.Event()

            def controlled_zero_exit(*_args, **_kwargs):
                process_finished.set()
                # Model a trip after run_managed_process's final health check.
                wait_until(lambda: guard.trip is not None, timeout=3)
                return SimpleNamespace(return_code=0, duration_seconds=0.1)

            guard = DiskGuard(root / "data", settings=ENABLED,
                              free_bytes_reader=lambda _root: FLOOR if process_finished.is_set() else HEALTHY)
            try:
                with (
                    patch("backend.worker.queued.DiskGuard", return_value=guard),
                    patch("backend.re3d_adapter.real.run_managed_process", side_effect=controlled_zero_exit),
                ):
                    self.worker(root, queue, fake, lambda _root: HEALTHY).run_once()
                self.assert_cancelled(queue, request, layout, "LOW_DISK_AUTO_CANCELLED")
                self.assertFalse(layout.result_path.exists())
            finally:
                engine.dispose()

    def test_low_disk_around_result_reuse_and_artifact_collection_cannot_succeed(self) -> None:
        for reuse in (False, True):
            with self.subTest(reuse=reuse), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layout, request, engine, queue, fake = create_queued_real_job(root)
                low = threading.Event()
                try:
                    if reuse:
                        RealPipelineRunner(layout, re3d_root=fake, driver_python=Path(sys.executable),
                                           installation_verifier=lambda _root, _pipeline: None).run(poll_seconds=0.01)
                    target = "_validate_existing_result" if reuse else "_collect_artifacts"
                    original = getattr(RealPipelineRunner, target)

                    def wrapper(runner, *args, **kwargs):
                        low.set()
                        wait_until(lambda: guard.trip is not None, timeout=3)
                        return original(runner, *args, **kwargs)

                    guard = DiskGuard(root / "data", settings=ENABLED,
                                      free_bytes_reader=lambda _root: FLOOR if low.is_set() else HEALTHY)
                    with (
                        patch("backend.worker.queued.DiskGuard", return_value=guard),
                        patch.object(RealPipelineRunner, target, wrapper),
                    ):
                        self.worker(root, queue, fake, lambda _root: HEALTHY).run_once()
                    self.assert_cancelled(queue, request, layout, "LOW_DISK_AUTO_CANCELLED")
                    self.assertFalse(layout.evaluation_path.exists())
                finally:
                    engine.dispose()

    def test_evaluation_or_monitor_exit_or_final_heartbeat_low_disk_cannot_succeed(self) -> None:
        for boundary in ("evaluation", "monitor_exit", "final_heartbeat"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layout, request, engine, queue, fake = create_queued_real_job(root)
                low = threading.Event()
                try:
                    worker = self.worker(root, queue, fake, lambda _root: FLOOR if low.is_set() else HEALTHY)
                    if boundary == "evaluation":
                        target_patch = patch("backend.worker.queued.RealEvaluator.run", side_effect=lambda: low.set())
                    elif boundary == "monitor_exit":
                        target_patch = patch("backend.worker.queued.ResourceMonitor.__exit__", side_effect=lambda *_args: low.set())
                    else:
                        original_heartbeat = queue.heartbeat

                        def heartbeat(*args, **kwargs):
                            value = original_heartbeat(*args, **kwargs)
                            if (queue.get_job(uuid.UUID(request["job_id"]))["status"] == JobStatus.EVALUATING
                                    and not any(t.name == "re3d-low-disk-guard" and t.is_alive()
                                                for t in threading.enumerate())):
                                low.set()
                            return value

                        target_patch = patch.object(queue, "heartbeat", side_effect=heartbeat)
                    with target_patch:
                        worker.run_once()
                    self.assert_cancelled(queue, request, layout, "LOW_DISK_AUTO_CANCELLED")
                finally:
                    engine.dispose()

    def test_sticky_guard_classifies_pipeline_exception_as_cancellation(self) -> None:
        for exception in (AdapterError("process failed"), PipelineTimedOut("timeout")):
            with self.subTest(exception=type(exception).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layout, request, engine, queue, fake = create_queued_real_job(root)
                low = threading.Event()
                guard = DiskGuard(root / "data", settings=ENABLED,
                                  free_bytes_reader=lambda _root: FLOOR if low.is_set() else HEALTHY)

                def runner(*_args, **_kwargs):
                    low.set()
                    wait_until(lambda: guard.trip is not None, timeout=3)
                    raise exception

                try:
                    with (
                        patch("backend.worker.queued.DiskGuard", return_value=guard),
                        patch("backend.worker.queued.RealPipelineRunner.run", side_effect=runner),
                    ):
                        self.worker(root, queue, fake, lambda _root: HEALTHY).run_once()
                    self.assert_cancelled(queue, request, layout, "LOW_DISK_AUTO_CANCELLED")
                finally:
                    engine.dispose()

    def test_user_cancellation_wins_over_low_disk_when_claim_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout, request, engine, queue, fake = create_queued_real_job(root)
            invoked = False

            def reader(_root: Path) -> int:
                nonlocal invoked
                if not invoked:
                    invoked = True
                    queue.request_cancel(uuid.UUID(request["job_id"]))
                return FLOOR

            try:
                self.worker(root, queue, fake, reader).run_once()
                self.assert_cancelled(queue, request, layout, "USER_CANCELLED")
            finally:
                engine.dispose()

    def test_lease_loss_wins_over_disk_trip_without_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout, request, engine, queue, fake = create_queued_real_job(root)
            original_heartbeat = queue.heartbeat
            first = True

            def heartbeat(*args, **kwargs):
                nonlocal first
                if first:
                    first = False
                    return original_heartbeat(*args, **kwargs)
                raise LeaseLostError("controlled lost lease")

            try:
                with patch.object(queue, "heartbeat", side_effect=heartbeat):
                    with self.assertRaises(LeaseLostError):
                        self.worker(root, queue, fake, lambda _root: FLOOR).run_once()
                self.assertEqual(queue.get_job(uuid.UUID(request["job_id"]))["status"], JobStatus.SFM)
                self.assertFalse(any(item["type"] in {"job_succeeded", "job_cancelled", "job_failed"}
                                     for item in read_events(layout.events_path)))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
