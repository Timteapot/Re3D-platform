from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack, nullcontext
import hashlib
import json
import marshal
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.experiments.cpu_runner import (
    BASE_COMMIT, BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA, CPUSfmSupervisor, CPU_TOOL_FILES,
    GIB, build_cpu_colmap_flags, execute_cpu_sfm, validate_cpu_profile,
)
from backend.experiments.supervision import SupervisionError, WholeRunBudget
from backend.re3d_adapter.errors import PipelineTimedOut
from scripts.run_cpu_sfm_stage import verify_before_import, load_frozen_source
from scripts.run_profiled_colmap import actual_option_recording


def local_profile(root):
    source = root / "source"
    return {"profile_kind": "local_cpu_sfm_candidate", "autorun": False,
            "pipeline": {"commit": BASE_COMMIT, "config_sha256": BASE_CONFIG_SHA,
                         "colmap_script_sha256": COLMAP_SCRIPT_SHA},
            "source_job_id": "6a90b5e9-0c99-41aa-881b-c196ebb96750",
            "source_checkpoint": {"original_root": str(source / "original"),
                "original_manifest_sha256": "a" * 64, "original_database_sha256": "b" * 64,
                "snapshot_root": str(source / "snapshot"), "snapshot_manifest_sha256": "c" * 64,
                "snapshot_database_sha256": "d" * 64, "expected_complete_pair_records": 26842},
            "cpu": {"sfm_threads": 12, "mapper_ba_threads": 16},
            "target_hardware": {"logical_cpus": 32, "host_ram_bytes": 34141495296},
            "runtime": {"mapanything_python": str(root / "algorithm-env/python.exe"),
                        "python_version": "3.12.14", "pycolmap_version": "3.13.0", "pycolmap_has_cuda": False},
            "target_experiment": {"scene": "synthetic-local-sfm", "root": str(root / "_experiments/synthetic-local-sfm")},
            "protection": {"reservation_bytes": 8 * GIB, "disk_floor_bytes": 20 * GIB,
                "available_ram_floor_bytes": 8 * GIB, "host_guard_seconds": 1,
                "resource_sample_seconds": 5, "directory_sample_seconds": 15,
                "snapshot_interval_seconds": 600, "snapshot_retained": 2,
                "overall_timeout_seconds": 14400}}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = local_profile(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_preparation_is_pure_and_never_reads_host_queue_gpu_or_checkpoint(self):
        with patch("backend.experiments.cpu_runner.HostResourceReader") as host, \
                patch("backend.experiments.cpu_runner.read_runtime_versions") as runtime, \
                patch("backend.experiments.cpu_runner.run_contained_process") as process:
            self.assertIs(validate_cpu_profile(self.profile), self.profile)
            host.assert_not_called()
            runtime.assert_not_called()
            process.assert_not_called()
        self.assertFalse(Path(self.profile["target_experiment"]["root"]).exists())

    def test_explicit_execute_flag_required_before_any_production_action(self):
        with patch("backend.experiments.cpu_runner.require_admin") as admin:
            with self.assertRaisesRegex(SupervisionError, "CPU_SFM_EXPLICIT_EXECUTION_REQUIRED"):
                execute_cpu_sfm(SimpleNamespace(execute_cpu=False), self.profile, self.root)
            admin.assert_not_called()

    def test_local_profile_not_server_gpu_profile_or_server_ram_reservation(self):
        for section, field, value in (("protection", "available_ram_floor_bytes", 32 * GIB),
                ("protection", "reservation_bytes", 64 * GIB),
                ("protection", "overall_timeout_seconds", 28800),
                ("cpu", "sfm_threads", 16), ("cpu", "mapper_ba_threads", 32)):
            profile = deepcopy(self.profile)
            profile[section][field] = value
            with self.subTest(field=field):
                with self.assertRaises(SupervisionError):
                    validate_cpu_profile(profile)
        self.profile["protection"]["free_vram_floor_bytes"] = 2 * GIB
        with self.assertRaisesRegex(SupervisionError, "CPU_SFM_PROTECTION_CHANGED"):
            validate_cpu_profile(self.profile)

    def test_eight_and_twelve_are_candidates_not_automatic_gpu_tuning(self):
        for threads in (8, 12):
            self.profile["cpu"]["sfm_threads"] = threads
            validate_cpu_profile(self.profile)
        self.profile["cpu"]["sfm_threads"] = True
        with self.assertRaises(SupervisionError):
            validate_cpu_profile(self.profile)

    def test_remaining_budget_may_only_be_reduced_as_a_positive_integer(self):
        for seconds in (14400, 14284, 1):
            self.profile['protection']['overall_timeout_seconds'] = seconds
            validate_cpu_profile(self.profile)
        for seconds in (0, -1, 14284.0, True, 14401):
            self.profile['protection']['overall_timeout_seconds'] = seconds
            with self.subTest(seconds=seconds):
                with self.assertRaisesRegex(SupervisionError, 'CPU_SFM_PROTECTION_CHANGED'):
                    validate_cpu_profile(self.profile)

    def test_source_target_overlap_and_jobs_path_rejected(self):
        self.profile["source_checkpoint"]["snapshot_root"] = self.profile["target_experiment"]["root"]
        with self.assertRaisesRegex(SupervisionError, "CPU_SFM_SOURCE_TARGET_OVERLAP"):
            validate_cpu_profile(self.profile)
        self.profile = local_profile(self.root)
        self.profile["target_experiment"]["root"] = str(self.root / "jobs/_experiments/synthetic-local-sfm")
        with self.assertRaisesRegex(SupervisionError, "CPU_SFM_TARGET_INVALID"):
            validate_cpu_profile(self.profile)

    def test_frozen_quality_and_deferred_flags_preserved(self):
        deferred = {"enabled": True, "relative_observation_floor": 0.15,
            "minimum_triangulated_ratio": 0.05, "max_images": 5, "retry_abs_pose_max_error": 6.0,
            "retry_abs_pose_min_num_inliers": 80, "retry_abs_pose_min_inlier_ratio": 0.3}
        flags = build_cpu_colmap_flags(self.root, {"camera_frontend": {"deferred_registration": deferred}}, 12)
        self.assertEqual(flags[flags.index("--num-threads") + 1], "12")
        self.assertEqual(flags[flags.index("--max-num-features") + 1], "6000")
        self.assertEqual(flags[flags.index("--camera-model") + 1], "PINHOLE")
        self.assertEqual(flags[flags.index("--random-seed") + 1], "0")
        self.assertEqual(flags.count("--deferred-registration"), 1)
        self.assertEqual(flags[flags.index("--retry-abs-pose-max-error") + 1], "6.0")
        self.assertFalse(any("gpu" in flag.lower() for flag in flags))


class Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value


class Monitor:
    def __init__(self, calls): self.calls, self.trip = calls, None
    def check_health(self):
        if self.trip: raise SupervisionError(self.trip)
    def start(self): self.calls.append(("monitor-start",))
    def set_phase(self, phase): self.calls.append(("phase", phase))
    def summary(self):
        return {"latest": {"gpu": None}, "peaks": {}, "trip": self.trip}
    def close(self): self.calls.append(("monitor-close",))


class Evidence:
    def __init__(self, calls):
        self.calls, self.fail_terminal, self.fail_forensic = calls, False, False
        self.terminal = self.forensic = None
    def check_health(self): pass
    def start(self): self.calls.append(("evidence-start",))
    def append(self, kind, payload): self.calls.append(("event", kind))
    def finish_terminal(self, value):
        self.calls.append(("small-terminal",))
        if self.fail_terminal: raise SupervisionError("EVIDENCE_WRITE_FAILED")
        self.terminal = dict(value)
    def write_forensic_summary(self, value):
        self.calls.append(("forensic-summary",))
        if self.fail_forensic: raise SupervisionError("EVIDENCE_WRITE_FAILED")
        self.forensic = dict(value)
    def close(self): self.calls.append(("evidence-close",))


class Snapshots:
    def __init__(self, calls):
        self.calls, self.stop_confirmed, self.fail_capture, self.count = calls, True, False, 26842
        self.latest = None
    def start(self): self.calls.append(("snapshot-start",))
    def check_health(self): pass
    def stop(self, timeout): self.calls.append(("snapshot-stop", timeout)); return self.stop_confirmed
    def capture_final(self, *, tree_stopped, health_check, reason):
        self.calls.append(("large-snapshot",))
        health_check()
        if self.fail_capture: raise SupervisionError("SNAPSHOT_FINAL_CAPTURE_FAILED")
        self.latest = {"directory": "snapshot-20261014T010000123456Z-" + "a" * 32,
                       "database_sha256": "b" * 64, "validated_complete_pair_records": self.count}
        return self.latest
    def summary(self): return {"retained_valid_snapshots": 1, "worker_stopped_confirmed": self.stop_confirmed}


class Relay:
    def __init__(self, monitor, evidence, database, budget):
        self.calls, self.pair_samples, self.cpu_samples = monitor.calls, [], [(0, 30), (5, 40)]
    def start(self): self.calls.append(("relay-start",))
    def set_stage(self, stage): self.calls.append(("relay-stage", stage))
    def check_health(self): pass
    def close(self): self.calls.append(("relay-close",)); return True


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calls, self.clock = [], Clock()
        self.monitor, self.evidence = Monitor(self.calls), Evidence(self.calls)
        self.snapshots = Snapshots(self.calls)
        self.budget = WholeRunBudget(14400, monotonic=self.clock)
        self.return_code, self.confirm_tree, self.process_error, self.source_error = 0, True, None, None
        self.gate_error = None
        self.timeouts = []

    def tearDown(self): self.temporary.cleanup()

    def process(self, command, **kwargs):
        self.calls.append(("cpu-process",))
        self.timeouts.append(kwargs["timeout_seconds"])
        kwargs["on_started"](9001)
        self.clock.value += 5
        try:
            if self.process_error: raise self.process_error
            kwargs["health_check"]()
            return SimpleNamespace(return_code=self.return_code)
        finally:
            if self.confirm_tree:
                self.calls.append(("tree-stopped",))
                kwargs["on_tree_stopped"]()

    def gate(self, health):
        self.calls.append(("sfm-gate",))
        health()
        if self.gate_error: raise self.gate_error
        return {"passed": True}

    def source_check(self, health):
        self.calls.append(("source-readonly-hash",))
        health()
        if self.source_error: raise self.source_error

    def run_stage(self, *, name="01-colmap", free=100 * GIB):
        supervisor = CPUSfmSupervisor(root=self.root, run_id="synthetic-run",
            source_job_id="6a90b5e9-0c99-41aa-881b-c196ebb96750", monitor=self.monitor,
            evidence=self.evidence, snapshots=self.snapshots, budget=self.budget,
            image_names=("synthetic.jpg",), database=self.root / "database.db", gate=self.gate,
            source_final_check=self.source_check, identity={"config_sha256": "a" * 64},
            process_runner=self.process, relay_factory=Relay, forensic_free_reader=lambda: free)
        stage = SimpleNamespace(name=name, argv=("synthetic-cpu-process",), cwd=self.root)
        with patch("backend.experiments.cpu_runner.read_pair_counts", return_value={"complete_pair_records": 26842}):
            return supervisor.run(stage, {"PYTHONPATH": "synthetic-only"})

    def test_one_cpu_stage_and_small_terminal_precedes_large_backup_and_source_hash(self):
        result = self.run_stage()
        self.assertEqual((result["mode"], result["status"]), ("cpu-sfm", "succeeded"))
        self.assertTrue(result["process_tree_stopped"])
        self.assertTrue(result["snapshot_worker_stopped"])
        self.assertTrue(result["terminal_evidence_confirmed"])
        self.assertTrue(result["source_checkpoint_unchanged"])
        self.assertFalse(result["gpu_stage_started"])
        self.assertFalse(result["gpu_sampling_enabled"])
        self.assertEqual(self.timeouts, [14400])
        self.assertEqual(result["resources"]["latest"]["gpu"], None)
        for earlier, later in (("tree-stopped", "small-terminal"), ("small-terminal", "large-snapshot"),
                              ("large-snapshot", "source-readonly-hash")):
            self.assertLess(self.calls.index((earlier,)), self.calls.index((later,)))

    def test_later_gpu_stage_rejected_before_process(self):
        with self.assertRaisesRegex(SupervisionError, "CPU_SFM_ONLY_STAGE_REQUIRED"):
            self.run_stage(name="20-a-mapanything")
        self.assertNotIn(("cpu-process",), self.calls)

    def test_missing_tree_proof_never_captures_or_verifies_source(self):
        self.confirm_tree = False
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "CPU_SFM_TREE_STOP_UNCONFIRMED")
        self.assertFalse(result["process_tree_stopped"])
        self.assertNotIn(("large-snapshot",), self.calls)
        self.assertNotIn(("source-readonly-hash",), self.calls)

    def test_terminal_write_failure_skips_large_forensic_actions(self):
        self.evidence.fail_terminal = True
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["terminal_evidence_confirmed"])
        self.assertNotIn(("large-snapshot",), self.calls)
        self.assertNotIn(("source-readonly-hash",), self.calls)

    def test_snapshot_worker_must_stop_before_final_capture(self):
        self.snapshots.stop_confirmed = False
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["snapshot_worker_stopped"])
        self.assertNotIn(("large-snapshot",), self.calls)

    def test_failure_is_not_faked_as_sfm_complete_but_immutable_source_is_checked(self):
        self.return_code = 9
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "CPU_SFM_STAGE_PROCESS_FAILED")
        self.assertNotIn("sfm_gate", result)
        self.assertTrue(result["source_checkpoint_unchanged"])
        self.assertEqual(result["final_snapshot_status"], "valid")

    def test_failed_sfm_gate_never_certifies_geometry(self):
        self.gate_error = SupervisionError("EXPERIMENT_SFM_METRICS_MODEL_MISMATCH")
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("sfm_gate", result)

    def test_timeout_keeps_budget_and_does_not_start_second_stage(self):
        self.process_error = PipelineTimedOut()
        result = self.run_stage()
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(self.timeouts, [14400])
        self.assertTrue(result["process_tree_stopped"])
        self.assertNotIn("sfm_gate", result)

    def test_source_verification_failure_revokes_success(self):
        self.source_error = SupervisionError("HYBRID_DATABASE_STATIC_IDENTITY_CHANGED")
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "CPU_SFM_SOURCE_VERIFICATION_FAILED")
        self.assertIsNone(result["source_checkpoint_unchanged"])

    def test_snapshot_failure_or_pair_count_mismatch_revokes_success(self):
        self.snapshots.count = 26843
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["final_snapshot_error"], "CPU_SFM_FINAL_PAIR_COUNT_CHANGED")
        self.assertEqual(result["final_snapshot_status"], "failed")

    def test_low_forensic_disk_saves_small_terminal_but_no_safe_snapshot(self):
        result = self.run_stage(free=20 * GIB)
        self.assertTrue(result["terminal_evidence_confirmed"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["final_snapshot_status"], "failed")

    def test_resource_trip_closes_tree_and_preserves_small_evidence(self):
        self.monitor.trip = "LOW_RAM_AUTO_STOPPED"
        result = self.run_stage()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "LOW_RAM_AUTO_STOPPED")
        self.assertTrue(result["terminal_evidence_confirmed"])

    def test_observation_numeric_diagnostics_survive_cpu_result_and_private_c_terminal(self):
        details = {'pair_retry_count':1, 'pair_observation_age_seconds':5,
                   'last_failure':{'probe_kind':'pairs', 'exception_type':'OperationalError',
                       'sqlite_errorcode':5, 'sqlite_primary_code':5, 'deadline_triggered':False,
                       'source_frame':'read_pair_counts', 'error_code':'LONG_PAIR_PROBE_BUSY'}}
        with patch.object(Relay, 'summary', return_value=details, create=True):
            result = self.run_stage()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['observation'], details)
        self.assertEqual(self.evidence.terminal['observation'], details)
        self.assertEqual(self.evidence.forensic['observation'], details)


class ChildBootstrapTests(unittest.TestCase):
    def test_frozen_module_pinned_source_loader_preserves_dataclass_and_skips_pyc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic_helper.py"
            raw = b"from dataclasses import dataclass\n@dataclass\nclass Value:\n    number: int = 7\n"
            path.write_bytes(raw)
            cached = Path(py_compile.compile(str(path), doraise=True))
            cached.write_bytes(cached.read_bytes()[:16] + marshal.dumps(compile(
                'raise RuntimeError("ignored bytecode must not run")', str(path), "exec")))
            name = "re3d_unit_synthetic_pinned_helper"
            try:
                module = load_frozen_source(path, name, hashlib.sha256(raw).hexdigest())
                self.assertEqual(module.Value().number, 7)
                with self.assertRaisesRegex(ValueError, "CPU_SFM_CHILD_FROZEN_MODULE_ALREADY_IMPORTED"):
                    load_frozen_source(path, name, hashlib.sha256(raw).hexdigest())
            finally:
                sys.modules.pop(name, None)
            with self.assertRaisesRegex(ValueError, "CPU_SFM_CHILD_FROZEN_IDENTITY_CHANGED"):
                load_frozen_source(path, name, "a" * 64)
            self.assertNotIn(name, sys.modules)

    def test_changed_source_rejected_before_overlay_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = CPU_TOOL_FILES
            entries = {}
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"# synthetic only\n")
                entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = root / "hybrid-tool-manifest.json"
            manifest.write_text(json.dumps({"schema_version": "1.0", "toolpack_kind": "re3d_cpu_sfm_handoff",
                "source_commit": "f" * 40, "files": entries, "autorun": False}), encoding="utf-8")
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            verify_before_import(root, digest)
            (root / "scripts/run_cpu_sfm_stage.py").write_bytes(b"# changed\n")
            with self.assertRaisesRegex(ValueError, "CPU_SFM_CHILD_HASH_CHANGED"):
                verify_before_import(root, digest)

    def test_actual_cpu_calls_record_twelve_and_mapper_sixteen_and_restore_hooks(self):
        calls = []
        extract = lambda *a, **kw: calls.append(("extract", kw["device"]))
        match = lambda *a, **kw: calls.append(("match", kw["device"]))
        mapping = lambda: SimpleNamespace(num_threads=-1, mapper=SimpleNamespace(num_threads=-1))
        pycolmap = SimpleNamespace(Device=SimpleNamespace(cpu="cpu"), extract_features=extract,
                                 match_exhaustive=match)
        module = SimpleNamespace(pycolmap=pycolmap, mapping_options=mapping)
        report = {"actual_mapper_options": []}
        with actual_option_recording(module, 12, 16, report, lambda: None):
            module.pycolmap.extract_features(extraction_options=SimpleNamespace(num_threads=12,
                max_image_size=1600, sift=SimpleNamespace(max_num_features=6000)), device="cpu")
            module.pycolmap.match_exhaustive(matching_options=SimpleNamespace(num_threads=12,
                guided_matching=True), device="cpu")
            for _ in range(2):
                options = module.mapping_options()
                self.assertEqual((options.num_threads, options.mapper.num_threads), (16, 16))
        self.assertIs(module.mapping_options, mapping)
        self.assertIs(pycolmap.extract_features, extract)
        self.assertIs(pycolmap.match_exhaustive, match)
        self.assertEqual(report["actual_matching"]["num_threads"], 12)
        self.assertEqual([row["phase"] for row in report["actual_mapper_options"]], ["initial", "deferred_retry"])
        self.assertEqual(calls, [("extract", "cpu"), ("match", "cpu")])

    def test_child_import_closure_uses_only_sealed_sources_and_ignores_poisoned_pyc(self):
        """Fresh-process import only; no host/DB/native reconstruction allowed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = Path(__file__).resolve().parents[2]
            entries = {}
            for name in CPU_TOOL_FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((repository / name).read_bytes())
                entries[name] = hashlib.sha256(target.read_bytes()).hexdigest()
            manifest = root / "hybrid-tool-manifest.json"
            manifest.write_text(json.dumps({"schema_version": "1.0", "toolpack_kind": "re3d_cpu_sfm_handoff",
                "source_commit": "f" * 40, "files": entries, "autorun": False}), encoding="utf-8")
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            cpu_path = root / "backend/experiments/cpu_runner.py"
            cached = Path(py_compile.compile(str(cpu_path), doraise=True))
            cached.write_bytes(cached.read_bytes()[:16] + marshal.dumps(compile(
                'raise RuntimeError("unsealed cache must not run")', str(cpu_path), "exec")))
            code = '''import importlib.util,importlib,json,pathlib,sqlite3,subprocess,sys
root=pathlib.Path(sys.argv[1]);digest=sys.argv[2]
def forbidden(*a,**k): raise AssertionError('unexpected host, queue, GPU, or subprocess call')
sqlite3.connect=forbidden
subprocess.run=forbidden
spec=importlib.util.spec_from_file_location('sealed_cpu_child',root/'scripts/run_cpu_sfm_stage.py')
child=importlib.util.module_from_spec(spec);spec.loader.exec_module(child)
child.verify_before_import(root,digest)
child.bootstrap_verified_overlay(root,digest)
cpu=importlib.import_module('backend.experiments.cpu_runner')
loaded={name:pathlib.Path(module.__file__).relative_to(root).as_posix()
        for name,module in sys.modules.items()
        if name.startswith(('backend.','scripts.')) and getattr(module,'__file__',None)}
assert all(path in cpu.CPU_TOOL_FILES for path in loaded.values())
assert 'jsonschema' not in sys.modules
assert 'backend.monitoring.calibration' not in sys.modules
assert 'backend.re3d_adapter.real' not in sys.modules
print(json.dumps({'safe_source_closure':True,'native_compute_started':False,'loaded_count':len(loaded)}))
'''
            completed = subprocess.run([sys.executable, "-I", "-c", code, str(root), digest],
                capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertTrue(result["safe_source_closure"])
            self.assertFalse(result["native_compute_started"])


class ExecutionWiringTests(unittest.TestCase):
    def test_fake_execution_wires_latest_source_only_cpu_and_exact_pinned_external_runtime(self):
        """Production wiring, but all OS/restore/runtime/compute dependencies fake."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = local_profile(root)
            target = Path(profile["target_experiment"]["root"])
            target.parent.mkdir()
            pipeline = root / "pipeline"
            for relative in ("configs/pipeline.json", "scripts/run_colmap_shared.py", "configs/paths.local.json"):
                path = pipeline / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            interpreter = Path(profile["runtime"]["mapanything_python"])
            interpreter.parent.mkdir()
            interpreter.write_bytes(b"synthetic executable, never invoked")
            calls, source_reads, captures = [], [], []
            monitor, evidence, snapshots = Monitor(calls), Evidence(calls), Snapshots(calls)
            deferred = {"enabled": True, "relative_observation_floor": 0.15,
                "minimum_triangulated_ratio": 0.05, "max_images": 5, "retry_abs_pose_max_error": 6.0,
                "retry_abs_pose_min_num_inliers": 80, "retry_abs_pose_min_inlier_ratio": 0.3}
            def read_config(path):
                return {"mapanything_python": str(interpreter)} if path.name == "paths.local.json" else {
                    "camera_frontend": {"deferred_registration": deferred}}
            def source_verify(**kwargs):
                source_reads.append(kwargs)
                kwargs["health_check"]()
                return SimpleNamespace(image_names=("synthetic.jpg",))
            def private_root(path, **kwargs):
                path.mkdir(); (path / "reports").mkdir()
            def restore(verified, path, **kwargs):
                self.assertEqual(set(p.name for p in (path / "reports").iterdir()), set())
                database = path / "work/shared/colmap/database.db"
                database.parent.mkdir(parents=True)
                database.write_bytes(b"synthetic database, queried only by injected reader")
                return SimpleNamespace(database_path=database)
            def monitor_factory(**kwargs):
                self.assertFalse(kwargs["limits"].gpu_required)
                self.assertEqual(kwargs["limits"].minimum_available_ram_bytes, 8 * GIB)
                self.assertNotIn("gpu_reader", kwargs)
                captures.append(kwargs)
                return monitor
            def process(command, **kwargs):
                calls.append(("one-contained-child", tuple(command)))
                self.assertEqual(Path(command[0]), interpreter)
                self.assertEqual(Path(command[1]).name, "run_cpu_sfm_stage.py")
                self.assertNotIn("run_pipeline.py", " ".join(command))
                kwargs["on_started"](999)
                kwargs["health_check"]()
                kwargs["on_tree_stopped"]()
                return SimpleNamespace(return_code=0)
            real_supervisor = CPUSfmSupervisor
            def supervisor_factory(**kwargs):
                return real_supervisor(**kwargs, relay_factory=Relay, process_runner=process,
                                       forensic_free_reader=lambda: 100 * GIB)
            with ExitStack() as stack:
                def mock(name, **kwargs): return stack.enter_context(patch(name, **kwargs))
                mock("backend.experiments.cpu_runner.require_admin")
                mock("backend.experiments.cpu_runner.verify_git_identity")
                mock("backend.experiments.cpu_runner.verify_cpu_tool_manifest", return_value="e" * 64)
                mock("backend.experiments.cpu_runner.declared_cpu_codes", return_value=set())
                mock("backend.experiments.cpu_runner.file_sha", side_effect=lambda path: COLMAP_SCRIPT_SHA
                     if Path(path).name == "run_colmap_shared.py" else BASE_CONFIG_SHA)
                mock("backend.experiments.cpu_runner.read_json", side_effect=read_config)
                mock("backend.experiments.cpu_runner.shutil.disk_usage", return_value=SimpleNamespace(free=100 * GIB))
                reader = mock("backend.experiments.cpu_runner.HostResourceReader")
                reader.return_value.read.return_value = ({"cpu": {"logical_count": 32},
                    "memory": {"total_bytes": 34141495296, "used_bytes": 8 * GIB}}, [])
                runtime = mock("backend.experiments.cpu_runner.read_runtime_versions", return_value={
                    "python_version": "3.12.14", "pycolmap_version": "3.13.0", "pycolmap_has_cuda": False})
                mock("backend.experiments.cpu_runner.EvidenceSink.create_production", return_value=evidence)
                mock("backend.experiments.cpu_runner.create_private_root", side_effect=private_root)
                mock("backend.experiments.cpu_runner.ExperimentResourceMonitor", side_effect=monitor_factory)
                mock("backend.experiments.cpu_runner.SnapshotStore", return_value=snapshots)
                mock("backend.experiments.cpu_runner.CPUSfmSupervisor", side_effect=supervisor_factory)
                mock("backend.experiments.cpu_runner.read_pair_counts", return_value={"complete_pair_records": 26842})
                gate = mock("backend.experiments.cpu_runner.gate_sfm", return_value={"passed": True})
                mock("backend.experiments.long_runner.exclusive_long_test", return_value=nullcontext())
                mock("backend.experiments.hybrid_checkpoint.verify_hybrid_source", side_effect=source_verify)
                restore_mock = mock("backend.experiments.hybrid_checkpoint.restore_hybrid_source", side_effect=restore)
                args = SimpleNamespace(execute_cpu=True, pipeline_root=pipeline, platform_root=root,
                                       tool_manifest_sha256="e" * 64)
                result = execute_cpu_sfm(args, profile, root)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(len(captures), 1)
                self.assertEqual(len(source_reads), 2)  # initial plus final immutable-source proof
                self.assertEqual(source_reads[0]["snapshot_database_sha256"], "d" * 64)
                self.assertEqual(restore_mock.call_count, 1)
                runtime.assert_called_once_with(interpreter)
                self.assertEqual(gate.call_args.kwargs["expected_sfm_threads"], 12)
                self.assertEqual(gate.call_args.kwargs["expected_mapper_threads"], 16)
                self.assertFalse(result["queue_modified"])
                self.assertFalse(result["services_modified"])
                saved = json.loads((target / "reports/cpu-result.json").read_bytes())
                self.assertTrue(saved["process_tree_stopped"])
                self.assertTrue(saved["source_checkpoint_unchanged"])


if __name__ == "__main__":
    unittest.main()
