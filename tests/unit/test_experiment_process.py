from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.experiments import process as module
from backend.experiments.process import ContainedProcessError, run_contained_process
from backend.re3d_adapter.errors import AdapterError, PipelineTimedOut


_TREE_SCRIPT = """
import os, pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
role, mode = sys.argv[2:4]
if role != 'grand':
    child_role = 'child' if role == 'parent' else 'grand'
    subprocess.Popen([sys.executable, __file__, str(root), child_role, mode])
if role == 'parent':
    end = time.monotonic() + 5
    while not all((root / (r + '.pid')).exists() for r in ('child', 'grand')):
        if time.monotonic() > end: raise SystemExit(11)
        time.sleep(.01)
temporary = root / (role + '.pid.tmp')
temporary.write_text(str(os.getpid()))
os.replace(temporary, root / (role + '.pid'))
if role == 'parent' and mode == 'early': raise SystemExit(0)
if mode == 'hot':
    while True:
        os.write(1, b'x' * 32768)
        time.sleep(.001)
if mode == 'spawn' and role == 'parent':
    while not (root / 'spawn.trigger').exists(): time.sleep(.001)
    try:
        subprocess.Popen([sys.executable, '-c',
            'from pathlib import Path; Path(' + repr(str(root / 'newborn-ran')) + ').touch()']).wait()
    except OSError:
        (root / 'spawn-denied').touch()
time.sleep(120)
"""


def _open_process(pid):
    handle = module._kernel.OpenProcess
    handle.argtypes = [module.wintypes.DWORD, module.wintypes.BOOL, module.wintypes.DWORD]
    handle.restype = module.wintypes.HANDLE
    return handle(0x100000, False, pid)


def _alive(pid: int, retained_handle=None) -> bool:
    if os.name == "nt":
        if retained_handle:
            return module._wait(retained_handle, 0) == 258
        value = _open_process(pid)  # SYNCHRONIZE, only our recorded PIDs.
        if not value:
            return False
        try:
            return module._wait(value, 0) == 258
        finally:
            module._close_handle(value)
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class _HealthTrip(AdapterError):
    pass


class _ControlledLog:
    """Synthetic blocking I/O; the same writer thread must own close."""

    def __init__(self, stream, *, blocked_method="write", failure=False):
        self.stream = stream
        self.blocked_method = blocked_method
        self.failure = failure
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.writer_threads = []
        self.close_thread = None

    def _operation(self, method):
        self.writer_threads.append(threading.get_ident())
        if self.blocked_method == method:
            self.entered.set()
            if self.failure:
                raise OSError("private-token-must-not-be-emitted")
            self.release.wait(5)

    def write(self, value):
        self._operation("write")
        return self.stream.write(value)

    def flush(self):
        self._operation("flush")
        return self.stream.flush()

    def close(self):
        self.close_thread = threading.get_ident()
        self.stream.close()
        self.closed.set()


class ContainedProcessTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[2] / ".tmp-tests"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.retained_handles = {}
        self.addCleanup(self.close_retained)
        self.tree_script = self.base / "tree.py"
        self.tree_script.write_text(_TREE_SCRIPT, encoding="utf-8")

    def command(self, mode):
        return [sys.executable, str(self.tree_script), str(self.base), "parent", mode]

    def ready(self):
        complete = all((self.base / (role + ".pid")).exists()
                       for role in ("parent", "child", "grand"))
        if complete:
            for role in ("parent", "child", "grand"):
                self.retain(int((self.base / (role + ".pid")).read_text()))
        return complete

    def retain(self, pid):
        # Retain native identity across stop/close so rapid Windows PID reuse
        # cannot turn a successful teardown into a flaky "still alive" result.
        if os.name == "nt" and pid not in self.retained_handles:
            self.retained_handles[pid] = _open_process(pid)

    def close_retained(self):
        for handle in self.retained_handles.values():
            if handle:
                module._close_handle(handle)

    def run_process(self, *args, **kwargs):
        original_started = kwargs.pop("on_started", lambda _: None)
        original_health = kwargs.pop("health_check", lambda: None)

        def started(pid):
            self.retain(pid)
            original_started(pid)

        def health():
            self.ready()
            original_health()

        return run_contained_process(*args, **kwargs, on_started=started, health_check=health)

    def assert_tree_dead(self):
        self.assertTrue(self.ready(), "the real parent/child/grandchild must all have started")
        for role in ("parent", "child", "grand"):
            pid = int((self.base / (role + ".pid")).read_text())
            self.assertFalse(_alive(pid, self.retained_handles.get(pid)), role)

    def test_success_output_environment_and_pid(self):
        lines, pids, checks, stopped = [], [], [], []
        result = self.run_process(
            [sys.executable, "-c", "import os; print(os.environ['EXPERIMENT_VALUE']); print('错误')"],
            cwd=self.base, log_path=self.base / "private.log", timeout_seconds=10,
            env={**os.environ, "EXPERIMENT_VALUE": "fixture", "PYTHONIOENCODING": "utf-8"},
            health_check=lambda: checks.append(True), on_started=pids.append,
            on_line=lines.append, poll_seconds=.01,
            on_tree_stopped=lambda: stopped.append(True),
        )
        self.assertEqual(result.return_code, 0)
        self.assertEqual([result.pid], pids)
        self.assertGreater(result.duration_seconds, 0)
        self.assertIn("fixture", lines)
        self.assertIn("错误", lines)
        self.assertGreater(len(checks), 1)
        self.assertIn(b"fixture", (self.base / "private.log").read_bytes())
        self.assertFalse(_alive(result.pid, self.retained_handles.get(result.pid)))
        self.assertTrue(result.tree_stopped_confirmed)
        self.assertEqual(stopped, [True])

    def test_blocked_log_write_does_not_delay_resource_trip_or_native_teardown(self):
        controlled = _ControlledLog((self.base / "private.log").open("ab", buffering=0))
        proof = []

        def health():
            if controlled.entered.is_set() and self.ready():
                raise _HealthTrip("TEST_BLOCKED_LOG_RESOURCE_TRIP")

        def stopped():
            self.assert_tree_dead()
            proof.append(True)

        started = time.monotonic()
        try:
            with patch.object(module, "_open_binary_log", return_value=controlled):
                with self.assertRaisesRegex(_HealthTrip, "TEST_BLOCKED_LOG_RESOURCE_TRIP"):
                    self.run_process(self.command("hot"), cwd=self.base,
                        log_path=self.base / "private.log", timeout_seconds=10,
                        health_check=health, on_tree_stopped=stopped, poll_seconds=.01)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(proof, [True])
            self.assertFalse(controlled.closed.is_set(), "owner must not race close with the blocked write")
        finally:
            controlled.release.set()
            self.assertTrue(controlled.closed.wait(2))
        self.assertEqual(set(controlled.writer_threads), {controlled.close_thread})
        self.assertNotEqual(controlled.close_thread, threading.get_ident())

    def test_blocked_log_flush_does_not_delay_process_timeout_or_tree_proof(self):
        controlled = _ControlledLog((self.base / "private.log").open("ab", buffering=0), blocked_method="flush")
        proof = []
        started = time.monotonic()
        try:
            with patch.object(module, "_open_binary_log", return_value=controlled):
                with self.assertRaises(PipelineTimedOut):
                    self.run_process(self.command("hot"), cwd=self.base,
                        log_path=self.base / "private.log", timeout_seconds=1.5,
                        on_tree_stopped=lambda: proof.append(True), poll_seconds=.01)
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(controlled.entered.is_set())
            self.assert_tree_dead()
            self.assertEqual(proof, [True])
            self.assertFalse(controlled.closed.is_set())
        finally:
            controlled.release.set()
            self.assertTrue(controlled.closed.wait(2))
        self.assertEqual(set(controlled.writer_threads), {controlled.close_thread})

    def test_blocked_writer_has_own_staleness_deadline(self):
        controlled = _ControlledLog((self.base / "private.log").open("ab", buffering=0))
        proof = []
        started = time.monotonic()
        try:
            with patch.object(module, "_open_binary_log", return_value=controlled):
                with self.assertRaisesRegex(ContainedProcessError, "EXPERIMENT_PROCESS_LOG_IO_TIMED_OUT"):
                    self.run_process(self.command("hot"), cwd=self.base,
                        log_path=self.base / "private.log", timeout_seconds=10,
                        log_io_timeout_seconds=.6, on_tree_stopped=lambda: proof.append(True),
                        poll_seconds=.01)
            self.assertLess(time.monotonic() - started, 3)
            self.assert_tree_dead()
            self.assertEqual(proof, [True])
        finally:
            controlled.release.set()
            self.assertTrue(controlled.closed.wait(2))

    def test_blocked_log_open_proves_no_compute_process_was_created(self):
        release, entered, proof, started_pids = threading.Event(), threading.Event(), [], []
        controlled = _ControlledLog((self.base / "private.log").open("ab", buffering=0), blocked_method="none")
        def blocked(path):
            entered.set()
            release.wait(5)
            return controlled
        try:
            with patch.object(module, "_open_binary_log", side_effect=blocked):
                with self.assertRaises(PipelineTimedOut):
                    self.run_process(self.command("silent"), cwd=self.base,
                        log_path=self.base / "private.log", timeout_seconds=.15,
                        on_tree_stopped=lambda: proof.append(True), on_started=started_pids.append,
                        poll_seconds=.01)
            self.assertTrue(entered.is_set())
            self.assertEqual(started_pids, [])
            self.assertEqual(proof, [True])
            self.assertFalse((self.base / "parent.pid").exists())
        finally:
            release.set()
            self.assertTrue(controlled.closed.wait(2))

    def test_writer_error_is_sanitized_and_native_teardown_still_proven(self):
        controlled = _ControlledLog((self.base / "private.log").open("ab", buffering=0), failure=True)
        proof = []
        with patch.object(module, "_open_binary_log", return_value=controlled):
            with self.assertRaises(ContainedProcessError) as caught:
                self.run_process([sys.executable, "-c", "print('fixture', flush=True); import time; time.sleep(120)"],
                    cwd=self.base, log_path=self.base / "private.log", timeout_seconds=10,
                    on_tree_stopped=lambda: proof.append(True), poll_seconds=.01)
        self.assertEqual(str(caught.exception), "EXPERIMENT_PROCESS_IO_FAILED")
        self.assertEqual(proof, [True])
        self.assertTrue(controlled.closed.is_set())

    def test_dead_log_writer_before_creation_is_fail_closed_with_no_compute_proof(self):
        proof, pids = [], []
        def dead_writer(writer):
            writer.ready.set()
            # Simulate unexpected disappearance rather than the normal done path.
        with patch.object(module._AsyncLogWriter, "_run", new=dead_writer):
            with self.assertRaisesRegex(ContainedProcessError, "EXPERIMENT_PROCESS_LOG_WRITER_STOPPED"):
                self.run_process(self.command("silent"), cwd=self.base,
                    log_path=self.base / "private.log", timeout_seconds=10,
                    on_tree_stopped=lambda: proof.append(True), on_started=pids.append, poll_seconds=.01)
        self.assertEqual(pids, [])
        self.assertEqual(proof, [True])

    def test_parent_success_does_not_leave_child_or_grandchild(self):
        outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            result = self.run_process(
                self.command("early"), cwd=self.base, log_path=self.base / "private.log",
                timeout_seconds=10, poll_seconds=.01,
            )
            self.assertEqual(result.return_code, 0)
            self.assert_tree_dead()
            self.assertIsNone(outsider.poll(), "a different tree must not be terminated")
        finally:
            outsider.kill()
            outsider.wait(timeout=5)

    def test_silent_tree_timeout_is_fully_reaped(self):
        started = time.monotonic()
        with self.assertRaisesRegex(PipelineTimedOut, "EXPERIMENT_PROCESS_TIMED_OUT"):
            self.run_process(
                self.command("silent"), cwd=self.base, log_path=self.base / "private.log",
                timeout_seconds=1.5, poll_seconds=.01,
            )
        self.assertLess(time.monotonic() - started, 5)
        self.assert_tree_dead()

    def test_hot_stdout_cannot_starve_health_and_line_memory_is_bounded(self):
        lengths = []
        checks_after_ready = 0

        def health():
            nonlocal checks_after_ready
            if self.ready():
                checks_after_ready += 1
                if checks_after_ready > 15:
                    raise _HealthTrip("TEST_RESOURCE_FLOOR")

        started = time.monotonic()
        with self.assertRaisesRegex(_HealthTrip, "TEST_RESOURCE_FLOOR"):
            self.run_process(
                self.command("hot"), cwd=self.base, log_path=self.base / "private.log",
                timeout_seconds=10, health_check=health,
                on_line=lambda line: lengths.append(len(line)), poll_seconds=.01,
            )
        self.assertLess(time.monotonic() - started, 5)
        self.assert_tree_dead()
        self.assertTrue(lengths)
        self.assertLessEqual(max(lengths), module._LINE_CHARS)
        self.assertGreater((self.base / "private.log").stat().st_size, 0)

    def test_keyboard_interrupt_cleans_real_tree(self):
        pids = []
        def health():
            if self.ready():
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.run_process(
                self.command("silent"), cwd=self.base, log_path=self.base / "private.log",
                timeout_seconds=10, health_check=health, poll_seconds=.01, on_started=pids.append,
            )
        # The venv executable may be a redirector with its own child PID.
        self.assert_tree_dead()

    def test_hot_stdout_timeout_remains_bounded_with_slow_line_consumer(self):
        lengths = []

        def line(value):
            lengths.append(len(value))
            time.sleep(.005)  # Backpressure must not prevent timeout supervision.

        started = time.monotonic()
        with self.assertRaises(PipelineTimedOut):
            self.run_process(
                self.command("hot"), cwd=self.base, log_path=self.base / "private.log",
                timeout_seconds=1.5, on_line=line, poll_seconds=.01,
            )
        self.assertLess(time.monotonic() - started, 5)
        self.assert_tree_dead()
        self.assertTrue(lengths)
        self.assertLessEqual(max(lengths), module._LINE_CHARS)

    def test_zero_exit_does_not_skip_final_health_trip(self):
        seen = False

        def line(_):
            nonlocal seen
            seen = True

        def health():
            if seen:
                raise _HealthTrip("TEST_FINAL_HEALTH")

        with self.assertRaisesRegex(_HealthTrip, "TEST_FINAL_HEALTH"):
            self.run_process(
                [sys.executable, "-c", "print('complete')"], cwd=self.base,
                log_path=self.base / "private.log", timeout_seconds=10,
                health_check=health, on_line=line, poll_seconds=.01,
            )

    def test_start_error_and_callback_error_do_not_reveal_secret_arguments(self):
        with self.assertRaises(ContainedProcessError) as raised:
            self.run_process(
                [str(self.base / "nonexistent-secret-executable"), "forbidden-field DO_NOT_PERSIST_MARKER"],
                cwd=self.base, log_path=self.base / "private.log", timeout_seconds=10,
            )
        self.assertEqual(str(raised.exception), "EXPERIMENT_PROCESS_START_FAILED")
        self.assertIsNone(raised.exception.__cause__)

        started_pids = []

        def started(pid):
            started_pids.append(pid)
            raise RuntimeError("forbidden-field DO_NOT_PERSIST_MARKER")

        with self.assertRaisesRegex(ContainedProcessError, "EXPERIMENT_PROCESS_CALLBACK_FAILED"):
            self.run_process(
                [sys.executable, "-c", "import time; time.sleep(120)"], cwd=self.base,
                log_path=self.base / "private.log", timeout_seconds=10, on_started=started,
            )
        self.assertEqual(len(started_pids), 1)
        self.assertFalse(_alive(started_pids[0], self.retained_handles.get(started_pids[0])))

    def test_input_durations_are_finite_positive(self):
        for duration in (0, -1, float("inf"), float("nan")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.run_process([sys.executable], cwd=self.base,
                                      log_path=self.base / "private.log", timeout_seconds=duration)

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object contract")
    def test_assign_failure_never_executes_primary_thread(self):
        marker = self.base / "must-not-exist"
        proof = []
        command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
        with patch.object(module, "_assign_job", return_value=False):
            with self.assertRaisesRegex(ContainedProcessError, "EXPERIMENT_PROCESS_CONTAINMENT_FAILED"):
                self.run_process(command, cwd=self.base, log_path=self.base / "private.log",
                                      timeout_seconds=10, on_tree_stopped=lambda: proof.append(True))
        self.assertFalse(marker.exists())
        self.assertEqual(proof, [], "constructor teardown is not a completed owner-level native wait")

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object contract")
    def test_second_interrupt_during_native_stop_never_publishes_tree_proof(self):
        proof = []
        def health():
            if self.ready():
                raise KeyboardInterrupt()
        def interrupted_stop(process, grace):
            raise KeyboardInterrupt()
        with patch.object(module._WindowsProcess, "stop", new=interrupted_stop):
            with self.assertRaises(KeyboardInterrupt):
                self.run_process(self.command("silent"), cwd=self.base,
                    log_path=self.base / "private.log", timeout_seconds=10,
                    health_check=health, on_tree_stopped=lambda: proof.append(True), poll_seconds=.01)
        self.assertEqual(proof, [], "close fail-safe is not positive proof of member handles signaled")
        deadline = time.monotonic() + 5
        while any(_alive(pid, handle) for pid, handle in self.retained_handles.items()) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assert_tree_dead()

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object contract")
    def test_assign_then_started_then_resume_is_real_native_order(self):
        marker = self.base / "primary-has-run"
        order = []
        original_assign, original_resume = module._assign_job, module._resume

        def assign(*args):
            order.append("assign")
            self.assertFalse(marker.exists())
            return original_assign(*args)

        def started(_):
            order.append("started")
            self.assertFalse(marker.exists())

        def resume(*args):
            order.append("resume")
            self.assertFalse(marker.exists())
            return original_resume(*args)

        with patch.object(module, "_assign_job", side_effect=assign), \
                patch.object(module, "_resume", side_effect=resume):
            result = self.run_process(
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                cwd=self.base, log_path=self.base / "private.log", timeout_seconds=10,
                on_started=started, poll_seconds=.01,
            )
        self.assertEqual(result.return_code, 0)
        self.assertTrue(marker.exists())
        self.assertEqual(order, ["assign", "started", "resume"])

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object contract")
    def test_health_trip_after_zero_exit_still_reaps_descendants(self):
        exited = False
        original_poll = module._WindowsProcess.poll

        def poll(process):
            nonlocal exited
            result = original_poll(process)
            if result == 0:
                exited = True
            return result

        def health():
            if exited:
                raise _HealthTrip("TEST_AFTER_ZERO_EXIT")

        with patch.object(module._WindowsProcess, "poll", new=poll):
            with self.assertRaisesRegex(_HealthTrip, "TEST_AFTER_ZERO_EXIT"):
                self.run_process(
                    self.command("early"), cwd=self.base, log_path=self.base / "private.log",
                    timeout_seconds=10, health_check=health, poll_seconds=.01,
                )
        self.assert_tree_dead()

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object contract")
    def test_shutdown_fences_new_birth_before_identity_snapshot(self):
        original_members = module._WindowsProcess.member_handles

        def members(process):
            (self.base / "spawn.trigger").touch()
            deadline = time.monotonic() + 2
            while not (self.base / "spawn-denied").exists() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue((self.base / "spawn-denied").exists())
            self.assertFalse((self.base / "newborn-ran").exists())
            return original_members(process)

        def health():
            if self.ready():
                raise _HealthTrip("TEST_SHUTDOWN_FENCE")

        with patch.object(module._WindowsProcess, "member_handles", new=members):
            with self.assertRaisesRegex(_HealthTrip, "TEST_SHUTDOWN_FENCE"):
                self.run_process(
                    self.command("spawn"), cwd=self.base, log_path=self.base / "private.log",
                    timeout_seconds=10, health_check=health, poll_seconds=.01,
                )
        self.assert_tree_dead()

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object contract")
    def test_failed_member_query_still_explicitly_terminates_job(self):
        terminated = []
        proof = []
        original_query, original_terminate = module._query_job, module._terminate_job

        def query(*args):
            if args[1] == 3:
                module.ctypes.set_last_error(5)
                return False
            return original_query(*args)

        def terminate(*args):
            terminated.append(True)
            return original_terminate(*args)

        def health():
            if self.ready():
                raise _HealthTrip("TEST_TRIP_BEFORE_QUERY_FAILURE")

        with patch.object(module, "_query_job", side_effect=query), \
                patch.object(module, "_terminate_job", side_effect=terminate):
            with self.assertRaisesRegex(ContainedProcessError, "EXPERIMENT_PROCESS_TERMINATION_FAILED"):
                self.run_process(
                    self.command("silent"), cwd=self.base, log_path=self.base / "private.log",
                    timeout_seconds=10, health_check=health, poll_seconds=.01,
                    on_tree_stopped=lambda: proof.append(True),
                )
        self.assertTrue(terminated)
        self.assertEqual(proof, [], "failed native member proof must suppress final backup authority")
        # The API reports failure to prove full wait, never false success; the
        # explicit job termination/close fail-safe still tears down this tree.
        deadline = time.monotonic() + 5
        while any(_alive(pid, handle) for pid, handle in self.retained_handles.items()) \
                and time.monotonic() < deadline:
            time.sleep(.01)
        self.assert_tree_dead()

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object contract")
    def test_supervisor_process_death_kills_contained_tree(self):
        runner = self.base / "supervisor.py"
        runner.write_text(
            "from pathlib import Path\nfrom backend.experiments.process import run_contained_process\n"
            f"run_contained_process({self.command('silent')!r}, cwd=Path({str(self.base)!r}), "
            f"log_path=Path({str(self.base / 'private.log')!r}), timeout_seconds=120)\n",
            encoding="utf-8",
        )
        repository = Path(__file__).resolve().parents[2]
        supervisor = subprocess.Popen(
            # Kill the real interpreter, not a venv redirector whose own
            # lifetime policy could otherwise confound KILL_ON_JOB_CLOSE.
            [getattr(sys, "_base_executable", sys.executable), str(runner)], cwd=repository,
            env={**os.environ, "PYTHONPATH": str(repository)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while not self.ready() and supervisor.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(self.ready())
            supervisor.kill()  # Own test supervisor only, never a broad PID/name kill.
            supervisor.wait(timeout=5)
            deadline = time.monotonic() + 5
            while any(_alive(int((self.base / (role + ".pid")).read_text()))
                      for role in ("parent", "child", "grand")) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assert_tree_dead()
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
