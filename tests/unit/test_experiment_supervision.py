from __future__ import annotations

import json
from pathlib import Path
import tempfile
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.experiments.supervision import LongStageSupervisor, SupervisionError, WholeRunBudget, bounded_read_only
from backend.experiments.process import run_contained_process
from backend.experiments.evidence import EvidenceSink
from backend.experiments.snapshot_store import SnapshotStore, GIB
from backend.re3d_adapter.errors import PipelineTimedOut
from tests.unit.test_experiment_snapshot_store import make_database, digest


class Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Monitor:
    def __init__(self, calls): self.calls, self.trip = calls, None
    def check_health(self):
        if self.trip: raise SupervisionError(self.trip)
    def set_phase(self, name): self.calls.append(("monitor-phase", name)); self.check_health()
    def finish(self): self.calls.append(("monitor-finish",)); self.check_health()
    def summary(self):
        return {"latest": {"cpu": {"utilization_percent": 30, "logical_count": 40},
                          "memory": {"used_bytes": 10, "total_bytes": 100, "available_bytes": 90},
                          "disk": {"free_bytes": 100 * 1024**3, "total_bytes": 150 * 1024**3}},
                "peaks": {"ram_used_bytes": 10}, "errors": [self.trip] if self.trip else []}
    def close(self): self.calls.append(("monitor-close",))


class Evidence:
    def __init__(self, calls):
        self.calls, self.events, self.terminal, self.forensic = calls, [], None, None
        self.fail_terminal = False
    def check_health(self): pass
    def append(self, kind, value):
        self.calls.append(("event", kind, value.get("event_code")))
        self.events.append((kind, dict(value)))
    def finish_terminal(self, value):
        self.calls.append(("c-terminal",))
        if self.fail_terminal: raise SupervisionError("EVIDENCE_WRITE_FAILED")
        self.terminal = dict(value)
        return Path("terminal.json")
    def write_forensic_summary(self, value):
        self.calls.append(("c-forensic",))
        self.forensic = dict(value)
        return Path("forensic-summary.json")
    def close(self): self.calls.append(("evidence-close",))


class Isolation:
    def __init__(self, calls): self.calls, self.owned = calls, set()
    def check_health(self): pass
    def register_pid(self, pid): self.calls.append(("register-pid", pid)); self.owned.add(pid)
    def clear_owned_pids(self): self.calls.append(("clear-pids",)); self.owned.clear()
    def close(self): self.calls.append(("isolation-close",))


class Snapshots:
    def __init__(self, calls):
        self.calls, self.stop_confirmed, self.started = calls, True, False
        self.captures = []
        self.latest = {"directory": "snapshot-20261014T010000123456Z-" + "a" * 32,
                       "database_sha256": "b" * 64, "validated_complete_pair_records": 25997}
    def start(self): self.calls.append(("snapshot-start",)); self.started = True
    def check_health(self): pass
    def stop(self, timeout): self.calls.append(("snapshot-stop", timeout)); return self.stop_confirmed
    def capture_final(self, *, tree_stopped, health_check, reason):
        self.calls.append(("d-snapshot", reason, tree_stopped))
        health_check()
        self.captures.append(reason)
        self.latest = {**self.latest, "validated_complete_pair_records": 26000}
        return dict(self.latest)
    def summary(self): return {"latest": dict(self.latest), "captures": list(self.captures)}


class Relay:
    def __init__(self, monitor, evidence, database, budget):
        self.calls, self.pair_samples, self.cpu_samples = monitor.calls, [], [(0.0, 20.0), (5.0, 40.0)]
    def start(self): self.calls.append(("relay-start",))
    def set_stage(self, name): self.calls.append(("relay-stage", name))
    def check_health(self): pass
    def close(self): self.calls.append(("relay-close",)); return True


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calls, self.clock = [], Clock()
        self.monitor, self.evidence = Monitor(self.calls), Evidence(self.calls)
        self.isolation, self.snapshots = Isolation(self.calls), Snapshots(self.calls)
        self.budget = WholeRunBudget(100, monotonic=self.clock)
        self.timeout_values, self.gated, self.source_checks = [], [], 0
        self.process_steps = []

    def tearDown(self): self.temporary.cleanup()

    def plan(self, *names):
        return SimpleNamespace(environment=None, stages=tuple(
            SimpleNamespace(name=name, argv=("synthetic-process", name), cwd=self.root, required_directories=())
            for name in names))

    def process(self, command, **kwargs):
        self.calls.append(("process", command[-1]))
        self.timeout_values.append(kwargs["timeout_seconds"])
        kwargs["on_started"](9000 + len(self.timeout_values))
        step = self.process_steps.pop(0) if self.process_steps else {}
        self.clock.advance(step.get("seconds", 1))
        try:
            if step.get("trip"):
                self.monitor.trip = step["trip"]
            if step.get("exception"):
                raise step["exception"]
            kwargs["health_check"]()
            return SimpleNamespace(return_code=step.get("return_code", 0))
        finally:
            if step.get("confirm_tree", True):
                self.calls.append(("tree-stop-confirmed",))
                kwargs["on_tree_stopped"]()

    def gate(self, stage, root, names, *, health_check):
        self.calls.append(("gate", stage.name))
        self.gated.append(stage.name)
        health_check()
        return {"valid": True}

    def source_check(self, health):
        self.calls.append(("source-hash",))
        health()
        self.source_checks += 1

    def supervisor(self, **kwargs):
        return LongStageSupervisor(root=self.root, run_id="synthetic-run", source_job_id="6a90b5e9-0c99-41aa-881b-c196ebb96750",
            monitor=self.monitor, evidence=self.evidence, isolation=self.isolation, snapshots=self.snapshots,
            budget=self.budget, image_names={"input.jpg"}, database=self.root / "database.db", gate=self.gate,
            process_runner=kwargs.pop("process_runner", self.process),
            forensic_free_reader=kwargs.pop("forensic_free_reader", lambda: 100 * 1024**3),
            source_final_check=self.source_check, relay_factory=kwargs.pop("relay_factory", Relay), **kwargs)

    def run_plan(self, names, **kwargs):
        with patch("backend.experiments.supervision.read_pair_counts", return_value={"complete_pair_records": 25997}):
            return self.supervisor().run(self.plan(*names), **kwargs)

    def test_whole_budget_does_not_reset_between_stages(self):
        self.process_steps = [{"seconds": 20}, {"seconds": 30}]
        result = self.run_plan(("20-a-mapanything", "30-b-mvsanywhere"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.timeout_values, [100, 80])
        self.assertEqual(self.budget.elapsed, 50)
        self.assertEqual([row["duration_seconds"] for row in result["stages"]], [20, 30])
        self.assertEqual(self.gated, ["20-a-mapanything", "30-b-mvsanywhere"])

    def test_whole_budget_exhaustion_stops_next_stage(self):
        self.process_steps = [{"seconds": 101}]
        result = self.run_plan(("20-a-mapanything", "30-b-mvsanywhere"))
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["error_code"], "LONG_BUDGET_EXHAUSTED")
        self.assertEqual(len(self.timeout_values), 1)
        self.assertEqual(self.gated, [])
        self.assertTrue(result["process_tree_stopped"])

    def test_failed_stage_keeps_row_duration_code_and_terminal_context(self):
        self.process_steps = [{"seconds": 4, "return_code": 9}]
        result = self.run_plan(("20-a-mapanything", "30-b-mvsanywhere"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LONG_STAGE_PROCESS_FAILED")
        self.assertEqual(len(result["stages"]), 1)
        row = result["stages"][0]
        self.assertEqual((row["stage"], row["status"], row["duration_seconds"]), ("20-a-mapanything", "failed", 4))
        self.assertIn("finished_at_utc", row)
        self.assertEqual(self.evidence.terminal["stage"], "20-a-mapanything")
        self.assertEqual(self.evidence.forensic["error_code"], "LONG_STAGE_PROCESS_FAILED")
        self.assertEqual(self.evidence.forensic["completed_stages_count"], 0)
        self.assertEqual(self.gated, [])

    def test_positive_tree_stop_callback_required_even_if_return_code_zero(self):
        self.process_steps = [{"confirm_tree": False}]
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LONG_TREE_STOP_UNCONFIRMED")
        self.assertFalse(result["process_tree_stopped"])
        self.assertFalse(self.evidence.terminal["tree_stopped"])
        self.assertEqual(self.snapshots.captures, [])
        self.assertEqual(self.source_checks, 0)
        self.assertTrue(self.isolation.owned)
        self.assertEqual(self.gated, [])

    def test_timeout_without_tree_callback_is_not_accepted_as_admission(self):
        self.process_steps = [{"exception": PipelineTimedOut(), "confirm_tree": False}]
        result = self.run_plan(("01-colmap",), admission_window_seconds=20)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LONG_TREE_STOP_UNCONFIRMED")
        self.assertEqual(self.snapshots.captures, [])

    def test_terminal_failure_blocks_large_final_snapshot_and_source_hash(self):
        self.evidence.fail_terminal = True
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LONG_TERMINAL_EVIDENCE_FAILED")
        self.assertFalse(result["terminal_evidence_confirmed"])
        self.assertEqual(self.snapshots.captures, [])
        self.assertEqual(self.source_checks, 0)
        self.assertEqual(result["final_snapshot_status"], "skipped")
        self.assertFalse(self.evidence.forensic["snapshot_attempted"])

    def test_small_terminal_precedes_terminal_snapshot_and_source_verification(self):
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["status"], "succeeded")
        names = [item[0] for item in self.calls]
        self.assertLess(names.index("c-terminal"), names.index("d-snapshot"))
        self.assertLess(names.index("d-snapshot"), names.index("source-hash"))
        self.assertLess(names.index("source-hash"), names.index("c-forensic"))
        self.assertTrue(result["source_checkpoint_unchanged"])

    def test_unconfirmed_snapshot_stop_keeps_previous_valid_checkpoint_and_skips_final_backup(self):
        self.snapshots.stop_confirmed = False
        old = dict(self.snapshots.latest)
        result = self.run_plan(("01-colmap", "20-a-mapanything"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LONG_SNAPSHOT_STOP_UNCONFIRMED")
        self.assertFalse(result["snapshot_worker_stopped"])
        self.assertEqual(result["final_snapshot_status"], "skipped")
        self.assertEqual(self.snapshots.latest, old)
        self.assertEqual(self.evidence.terminal["latest_snapshot_sha256"], old["database_sha256"])
        self.assertEqual(self.snapshots.captures, [])

    def test_sticky_runtime_trip_still_allows_independent_post_stop_forensics(self):
        self.process_steps = [{"trip": "LOW_RAM_AUTO_STOPPED"}]
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LOW_RAM_AUTO_STOPPED")
        self.assertEqual(self.monitor.trip, "LOW_RAM_AUTO_STOPPED")
        self.assertTrue(result["process_tree_stopped"])
        self.assertEqual(result["final_snapshot_status"], "valid")
        self.assertEqual(self.snapshots.captures, ["terminal"])
        self.assertEqual(self.source_checks, 1)
        self.assertEqual(self.evidence.forensic["error_code"], "LOW_RAM_AUTO_STOPPED")

    def test_independent_d_disk_floor_blocks_final_backup_but_keeps_small_c_terminal(self):
        with patch("backend.experiments.supervision.read_pair_counts", return_value={"complete_pair_records": 25997}):
            result = self.supervisor(forensic_free_reader=lambda: 20 * 1024**3).run(self.plan("20-a-mapanything"))
        self.assertIsNotNone(self.evidence.terminal)
        self.assertEqual(result["final_snapshot_status"], "failed")
        self.assertEqual(result["final_snapshot_error"], "LONG_FORENSIC_DISK_UNSAFE")
        self.assertEqual(self.snapshots.captures, [])
        self.assertEqual(self.source_checks, 0)
        self.assertEqual(result["status"], "failed")

    def test_admission_timeout_accepts_partial_sfm_without_calling_full_gate(self):
        self.process_steps = [{"seconds": 20, "exception": PipelineTimedOut()}]
        result = self.run_plan(("01-colmap", "20-a-mapanything"), admission_window_seconds=20)
        self.assertEqual(result["status"], "admission_window_complete")
        self.assertEqual(result["mode"], "admission")
        self.assertEqual(result["stage_count"], 1)
        self.assertEqual(result["stages"][0]["status"], "window_elapsed")
        self.assertEqual(self.timeout_values, [20])
        self.assertEqual(self.gated, [])
        self.assertEqual(self.evidence.forensic["completed_stages_count"], 0)
        self.assertEqual(self.snapshots.captures, ["terminal"])

    def test_sfm_completion_saves_stage_snapshot_only_after_full_gate(self):
        result = self.run_plan(("01-colmap", "20-a-mapanything"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.snapshots.captures, ["sfm-complete", "terminal"])
        gate_index = self.calls.index(("gate", "01-colmap"))
        snapshot_index = self.calls.index(("d-snapshot", "sfm-complete", True))
        self.assertLess(gate_index, snapshot_index)

    def test_arbitrary_exception_text_not_in_minimal_evidence(self):
        self.process_steps = [{"exception": OSError("forbidden-field DO_NOT_PERSIST_MARKER D:/private/env") }]
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["error_code"], "LONG_STAGE_OR_IO_FAILED")
        evidence = json.dumps([self.evidence.events, self.evidence.terminal, self.evidence.forensic])
        self.assertNotIn("DO_NOT_PERSIST_MARKER", evidence)
        self.assertNotIn("D:/private", evidence)

    def test_keyboard_interrupt_preserves_cancelled_outcome_after_positive_stop(self):
        self.process_steps = [{"exception": KeyboardInterrupt()}]
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error_code"], "EXPERIMENT_OPERATOR_INTERRUPTED")
        self.assertTrue(result["process_tree_stopped"])
        self.assertEqual(result["final_snapshot_status"], "valid")

    def test_relay_cleanup_exception_cannot_prevent_small_terminal_evidence(self):
        class BrokenRelay(Relay):
            def close(self):
                raise RuntimeError("thread was never started forbidden-field DO_NOT_PERSIST_MARKER")
        with patch("backend.experiments.supervision.read_pair_counts", return_value={"complete_pair_records": 25997}):
            result = self.supervisor(relay_factory=BrokenRelay).run(self.plan("20-a-mapanything"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["observation_relay_stopped"])
        self.assertIsNotNone(self.evidence.terminal)
        self.assertIsNotNone(self.evidence.forensic)
        self.assertNotIn("DO_NOT_PERSIST_MARKER", json.dumps([self.evidence.terminal, self.evidence.forensic]))

    def test_snapshot_cleanup_exception_keeps_previous_valid_and_small_terminal(self):
        old = dict(self.snapshots.latest)
        def stop(timeout):
            raise OSError("private secret snapshot worker")
        self.snapshots.stop = stop
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["snapshot_worker_stopped"])
        self.assertEqual(result["final_snapshot_status"], "skipped")
        self.assertEqual(self.snapshots.latest, old)
        self.assertIsNotNone(self.evidence.terminal)

    def test_final_c_failure_cannot_leave_nominal_success(self):
        def failed_final(value):
            raise SupervisionError("EVIDENCE_WRITE_FAILED")
        self.evidence.write_forensic_summary = failed_final
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LONG_FORENSIC_EVIDENCE_FAILED")
        self.assertIsNotNone(self.evidence.terminal)
        self.assertTrue(result["process_tree_stopped"])

    def test_native_cpu_helper_proves_positive_stop_without_reconstruction(self):
        (self.root / "reports").mkdir()
        stage = SimpleNamespace(name="20-a-mapanything", cwd=self.root, required_directories=(),
            argv=(sys.executable, "-c", "print(sum(range(10000)))"))
        plan = SimpleNamespace(environment=None, stages=(stage,))
        with patch("backend.experiments.supervision.read_pair_counts", return_value={"complete_pair_records": 25997}):
            result = self.supervisor(process_runner=run_contained_process).run(plan)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["process_tree_stopped"])
        self.assertFalse(self.isolation.owned)
        self.assertEqual(result["stages"][0]["return_code"], 0)
        self.assertEqual(self.gated, ["20-a-mapanything"])

    def test_real_small_evidence_snapshot_and_native_two_stage_integration(self):
        """Only a tiny synthetic SQLite fixture and CPU Python helpers are real.

        No platform database, model files, service, SSH or GPU is accessed. The
        test factory explicitly substitutes a temporary directory for C evidence.
        """
        (self.root / "reports").mkdir()
        database = self.root / "synthetic-database.db"
        names = make_database(database)
        source_sha = digest(database)
        stages = tuple(SimpleNamespace(name=name, cwd=self.root, required_directories=(),
                    argv=(sys.executable, "-c", "print(sum(range(10000)))"))
                    for name in ("01-colmap", "20-a-mapanything"))
        plan = SimpleNamespace(environment=None, stages=stages)
        sink = EvidenceSink.create_for_test(self.root / "c-evidence-substitute", free_reader=lambda _: 20 * GIB,
                    known_stages=[stage.name for stage in stages], intervals=(0.02, 2))
        store = SnapshotStore(self.root / "synthetic-checkpoints", database, names,
                    free_bytes_reader=lambda: 100 * GIB, interval_seconds=600, timeout_seconds=5)
        self.addCleanup(store.stop, timeout=1)
        self.addCleanup(sink.close)
        sink.start()
        budget = WholeRunBudget(30)
        def verify_source(health):
            health()
            self.assertEqual(source_sha, digest(database))
        supervisor = LongStageSupervisor(root=self.root, run_id="synthetic-integration", source_job_id="6a90b5e9-0c99-41aa-881b-c196ebb96750",
            monitor=self.monitor, evidence=sink, isolation=self.isolation, snapshots=store,
            budget=budget, image_names=set(names), database=database, gate=self.gate,
            process_runner=run_contained_process, forensic_free_reader=lambda: 100 * GIB,
            source_final_check=verify_source, relay_factory=Relay,
            identity={"pipeline_commit": "a" * 40, "config_sha256": "b" * 64, "profile_sha256": "c" * 64})
        # Unlike the pure mocks, real read_pair_counts intentionally reads only
        # this test-created 300-input SQLite database.
        result = supervisor.run(plan)
        for thread in [*sink._threads.values(), *sink._terminal_threads]:
            thread.join(timeout=2)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["process_tree_stopped"])
        self.assertTrue(result["source_checkpoint_unchanged"])
        self.assertEqual(result["initial_pair_counts"]["complete_pair_records"], 2)
        self.assertEqual(result["checkpoint_summary"]["successful_snapshots"], 2)
        self.assertEqual(self.gated, ["01-colmap", "20-a-mapanything"])
        pointer = json.loads((store.root / "latest-valid.json").read_bytes())
        self.assertEqual(pointer, store.latest)
        final_db = store.root / pointer["database_relative_path"]
        self.assertLess(final_db.stat().st_size, 1024**2)
        self.assertEqual(pointer["database_sha256"], digest(final_db))
        self.assertEqual(pointer["validated_complete_pair_records"], 2)
        self.assertEqual(source_sha, digest(database))
        initial = json.loads((sink.root / "terminal.json").read_bytes())
        final = json.loads((sink.root / "forensic-summary.json").read_bytes())
        self.assertEqual(initial["summary"]["status"], "stopped")
        self.assertEqual(final["summary"]["status"], "succeeded")
        self.assertEqual(final["summary"]["checkpoint_status"], "valid")
        self.assertEqual(final["summary"]["latest_snapshot_sha256"], pointer["database_sha256"])
        self.assertEqual(final["summary"]["latest_snapshot_id"], pointer["directory"])
        self.assertEqual(final["summary"]["completed_stages_count"], 2)
        self.assertTrue(final["summary"]["source_checkpoint_unchanged"])
        self.assertEqual(final["summary"]["pipeline_commit"], "a" * 40)
        journal = [json.loads(line) for line in (sink.root / "journal.jsonl").read_text().splitlines()]
        self.assertTrue(journal)
        self.assertEqual({item["payload"].get("stage") for item in journal} - {"restore"}, {"01-colmap", "20-a-mapanything"})
        self.assertNotIn(str(self.root), json.dumps([initial, final, journal]))
        self.assertFalse(self.isolation.owned)

    def test_bounded_read_only_timeout_never_waits_for_hung_read(self):
        release = threading.Event()
        worker_finished = threading.Event()
        self.addCleanup(release.set)
        health_calls = []
        def read():
            release.wait()
            worker_finished.set()
            return True
        began = time.monotonic()
        with self.assertRaises(SupervisionError) as caught:
            bounded_read_only(read, timeout_seconds=0.05, health_check=lambda: health_calls.append(True))
        self.assertEqual(caught.exception.code, "LONG_READONLY_VALIDATION_TIMED_OUT")
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertFalse(worker_finished.is_set())
        self.assertTrue(health_calls)
        release.set()
        self.assertTrue(worker_finished.wait(1))

    def test_timed_out_source_verification_does_not_claim_source_unchanged(self):
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)
        def hung_source(health):
            health()
            release.wait()
            finished.set()
        def shorten_read(function, *, timeout_seconds, health_check=lambda: None):
            return bounded_read_only(function, timeout_seconds=min(timeout_seconds, 0.05), health_check=health_check)
        supervisor = self.supervisor()
        supervisor.source_final_check = hung_source
        began = time.monotonic()
        with patch("backend.experiments.supervision.read_pair_counts", return_value={"complete_pair_records": 25997}), \
             patch("backend.experiments.supervision.bounded_read_only", side_effect=shorten_read):
            result = supervisor.run(self.plan("20-a-mapanything"))
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LONG_SOURCE_VERIFICATION_FAILED")
        self.assertEqual(result["source_verification_error"], "LONG_READONLY_VALIDATION_TIMED_OUT")
        self.assertIsNone(result["source_checkpoint_unchanged"])
        self.assertNotEqual(self.evidence.forensic.get("source_checkpoint_unchanged"), True)
        self.assertFalse(finished.is_set())
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertIsNone(result["source_checkpoint_unchanged"])

    def test_cpu_60_second_mean_uses_timestamped_24_sample_window(self):
        class CpuRelay(Relay):
            def __init__(self, *args):
                super().__init__(*args)
                self.cpu_samples = [(index * 5, 20.0 if index < 12 else 80.0) for index in range(24)]
        with patch("backend.experiments.supervision.read_pair_counts", return_value={"complete_pair_records": 25997}):
            result = self.supervisor(relay_factory=CpuRelay).run(self.plan("20-a-mapanything"))
        self.assertEqual(result["cpu_sample_count"], 24)
        self.assertEqual(result["cpu_observed_span_seconds"], 115)
        self.assertEqual(result["cpu_sampled_mean_percent"], 50)
        # The last inclusive 60-second window is one 20% + twelve 80% samples.
        self.assertAlmostEqual(result["cpu_max_60s_mean_percent"], (20 + 12 * 80) / 13)

    def test_cpu_short_observation_cannot_claim_60_second_mean(self):
        result = self.run_plan(("20-a-mapanything",))
        self.assertEqual(result["cpu_sample_count"], 2)
        self.assertEqual(result["cpu_observed_span_seconds"], 5)
        self.assertIsNone(result["cpu_max_60s_mean_percent"])

    def test_setup_failure_keeps_failed_stage_and_timing_before_process_creation(self):
        def fail_setup():
            self.clock.advance(3)
            raise SupervisionError('SNAPSHOT_SQLITE_BACKUP_FAILED')
        self.snapshots.start = fail_setup
        result = self.run_plan(('01-colmap',))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['stages']),1)
        self.assertEqual(result['stages'][0]['status'],'failed')
        self.assertEqual(result['stages'][0]['duration_seconds'],3)
        self.assertEqual(result['stages'][0]['stage_elapsed_seconds'],3)
        self.assertEqual(self.timeout_values,[])
        self.assertTrue(result['process_tree_stopped'])

    def test_notify_failure_before_process_keeps_failed_stage_and_evidence(self):
        def broken_notify(_): raise BrokenPipeError('untrusted diagnostic text')
        supervisor = self.supervisor();supervisor.notify = broken_notify
        with patch('backend.experiments.supervision.read_pair_counts',return_value={'complete_pair_records':25997}):
            result = supervisor.run(self.plan('20-a-mapanything'))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['stages'][0]['status'],'failed')
        self.assertIn('duration_seconds',result['stages'][0])
        self.assertTrue(result['terminal_evidence_confirmed'])
        self.assertEqual(self.timeout_values,[])


if __name__ == "__main__":
    unittest.main()
