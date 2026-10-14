from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import builtins
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from backend.experiments import long_runner as module
from backend.experiments.high_profile import BASE_COMMIT, BASE_CONFIG_SHA
from backend.experiments.supervision import SupervisionError


REQUIRED_TOOLS = {
    f"backend/experiments/{name}.py" for name in (
        "__init__", "checkpoint", "resources", "process", "preflight", "runner", "high_profile",
        "stages", "snapshot_store", "evidence", "supervision", "long_runner",
    )
} | {"scripts/run_long_experiment.py", "scripts/run_profiled_colmap.py"}


def valid_profile() -> dict:
    return {
        "profile_kind": "offline_high_cpu_candidate", "autorun": False,
        "full_guarded_launcher_ready": False, "server_applied": False,
        "prepared_overlay_sha256": "b" * 64,
        "pipeline": {"commit": BASE_COMMIT, "baseline_config_sha256": BASE_CONFIG_SHA,
                     "baseline_modified": False, "quality_parameters_changed": False},
        "target_hardware": {"logical_cpus": 40},
        "cpu": {"sfm_threads": 12, "mapper_ba_threads": 32, "openmvs_threads": 32,
                "texture_threads": 32, "global_blas_omp_override": None},
        "gpu_preserved": {"task_concurrency": 1, "branches_serial": True, "map_outer_batch": 4,
                          "map_dense_head_minibatch": 1, "map_use_amp": True,
                          "map_requested_amp_dtype": "bf16", "mvs_source_views": 7,
                          "mvs_height": 384, "mvs_width": 384, "mvs_refinement_steps": 1},
        "protection": {"disk_floor_bytes": 20 * 1024**3, "reservation_bytes": 64 * 1024**3,
                       "available_ram_floor_bytes": 32 * 1024**3, "free_vram_floor_bytes": 2 * 1024**3,
                       "host_guard_seconds": 1, "gpu_sample_seconds": 5,
                       "directory_sample_seconds": 15, "overall_timeout_seconds": 14400},
        "source_checkpoint": {"database_sha256": "a" * 64, "manifest_sha256": "c" * 64,
                              "complete_pair_records": 25997},
    }


def valid_report(threads=12, *, added_pairs=720, duration=180) -> dict:
    return {
        "mode": "admission", "status": "admission_window_complete", "sfm_threads": threads,
        "pipeline_commit": BASE_COMMIT,
        "config_sha256": "b" * 64, "source_manifest_sha256": "c" * 64,
        "tool_manifest_sha256": "d" * 64,
        "source_database_sha256": "a" * 64, "source_checkpoint_unchanged": True,
        "process_tree_stopped": True, "terminal_evidence_confirmed": True,
        "final_snapshot_status": "valid", "resources": {"errors": [], "trip": None},
        "admission_duration_seconds": duration,
        "initial_pair_counts": {"complete_pair_records": 25997},
        "final_pair_counts": {"complete_pair_records": 25997 + added_pairs},
        "cpu_sampled_mean_percent": 35.8,
        "cpu_max_60s_mean_percent": 40.2,
        "cpu_sample_count": 36,
        "cpu_observed_span_seconds": 175,
    }


class LongRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[2] / ".tmp-tests"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.profile = valid_profile()
        self.baseline, self.candidate = valid_report(8, added_pairs=360), valid_report()

    def write_json(self, filename, document):
        path = self.base / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def compare(self, baseline=None, candidate=None):
        return module.compare_admission_reports(
            self.baseline if baseline is None else baseline,
            self.candidate if candidate is None else candidate,
            self.profile,
        )

    def assert_admission_rejected(self, baseline=None, candidate=None):
        with self.assertRaises(SupervisionError) as caught:
            self.compare(baseline, candidate)
        self.assertEqual(caught.exception.code, "LONG_CANDIDATE_ADMISSION_REQUIRED")

    def test_model_manifest_lf_and_crlf_preserve_raw_and_normalized_identity(self):
        lf = b'{\n  "model.pt": "0123456789abcdef"\n}\n'
        expected = hashlib.sha256(lf).hexdigest()
        path = self.base / "checksums.json"
        for raw in (lf, lf.replace(b"\n", b"\r\n")):
            path.write_bytes(raw)
            with self.subTest(raw_sha=hashlib.sha256(raw).hexdigest()), \
                    patch.object(module, "MODEL_MANIFEST_LF_SHA", expected):
                self.assertEqual(module.verify_model_checksum_manifest(path), {
                    "model_checksum_manifest_sha256": hashlib.sha256(raw).hexdigest(),
                    "model_checksum_manifest_lf_sha256": expected,
                    "model_checksum_manifest_hash_normalization": "crlf_to_lf_only",
                    "model_weights_full_rehash_performed": False,
                })

    def test_model_manifest_content_or_other_byte_changes_are_rejected(self):
        lf = b'{\n  "model.pt": "0123456789abcdef"\n}\n'
        expected = hashlib.sha256(lf).hexdigest()
        path = self.base / "checksums.json"
        changed = (
            lf.replace(b"abcdef", b"abcdee"),
            lf.replace(b"  ", b" "),
            b"\xef\xbb\xbf" + lf,
            lf.replace(b"\n", b"\r"),
            lf + b"\n",
            lf.rstrip(b"\n"),
        )
        for raw in changed:
            path.write_bytes(raw)
            with self.subTest(raw_sha=hashlib.sha256(raw).hexdigest()), \
                    patch.object(module, "MODEL_MANIFEST_LF_SHA", expected), \
                    self.assertRaises(SupervisionError) as caught:
                module.verify_model_checksum_manifest(path)
            self.assertEqual(caught.exception.code, "HIGH_PROFILE_FROZEN_IDENTITY_CHANGED")

    def test_model_manifest_expected_identity_is_frozen_lf_blob(self):
        self.assertEqual(module.MODEL_MANIFEST_LF_SHA,
                         "51f0aed037b1c3944c94c81153d0b1d1c8b0b52dca7ef8f5cf93638ce45a70ae")

    def invoke_cli_without_runtime(self, arguments):
        profile_path = self.write_json("profile.json", self.profile)
        stdout = io.StringIO()
        prohibited = (
            "execute_long_test", "read_windows_state", "read_queue_state", "require_admin",
            "read_runtime_versions", "runtime_gpu_check", "verify_git_identity", "verify_tool_manifest", "read_current_launcher_ownership",
            "HostResourceReader", "NvidiaSmiReader", "create_private_root", "verify_checkpoint",
            "restore_checkpoint", "ExperimentResourceMonitor", "SnapshotStore", "EvidenceSink",
            "exclusive_long_test",
        )
        with ExitStack() as stack:
            for name in prohibited:
                mock = stack.enter_context(patch.object(module, name, side_effect=AssertionError("forbidden runtime action")))
                stack.callback(mock.assert_not_called)
            stack.enter_context(patch.object(module.subprocess, "run", side_effect=AssertionError("forbidden process spawn")))
            stack.enter_context(patch.object(module.shutil, "disk_usage", side_effect=AssertionError("forbidden disk probe")))
            stack.enter_context(patch.object(sys, "argv", ["run_long_experiment.py", "--profile", str(profile_path), *arguments]))
            stack.enter_context(redirect_stdout(stdout))
            return_code = module.main(tool_root=self.base / "missing-tool-root")
        return return_code, json.loads(stdout.getvalue())

    def test_default_cli_only_prepares_without_admin_host_database_gpu_or_execution(self):
        return_code, result = self.invoke_cli_without_runtime([])
        self.assertEqual(return_code, 0)
        self.assertEqual(result["status"], "prepared_no_execution")
        self.assertFalse(result["server_contacted"])
        self.assertTrue(result["actual_hardware_preflight_required"])
        self.assertEqual(result["sfm_threads"], 12)
        self.assertEqual(result["mapper_ba_threads"], 32)

    def test_nonprepare_modes_without_explicit_execute_flag_remain_offline(self):
        for mode in ("admission", "full"):
            with self.subTest(mode=mode):
                return_code, result = self.invoke_cli_without_runtime(["--mode", mode])
                self.assertEqual(return_code, 0)
                self.assertEqual(result["status"], "prepared_no_execution")
                self.assertFalse(result["server_contacted"])

    def test_execute_flag_with_prepare_mode_is_rejected_before_any_runtime_action(self):
        return_code, result = self.invoke_cli_without_runtime(["--execute-long-test", "--run-name", "unit-run"])
        self.assertEqual(return_code, 1)
        self.assertEqual(result["status"], "not_completed")

    def test_compare_cli_reads_only_local_reports_without_hardware_or_server_probe(self):
        baseline = self.write_json("baseline.json", self.baseline)
        candidate = self.write_json("candidate.json", self.candidate)
        return_code, result = self.invoke_cli_without_runtime([
            "--mode", "compare-admission", "--baseline-admission", str(baseline),
            "--candidate-admission", str(candidate),
        ])
        self.assertEqual(return_code, 0)
        self.assertEqual(result["status"], "accepted_short_cpu_candidate_admission")
        self.assertEqual(result["observed_window_ratio"], 2)

    def test_compare_requires_both_reports_and_forbids_execute_flag(self):
        for args in (["--mode", "compare-admission"], ["--mode", "compare-admission", "--execute-long-test"]):
            with self.subTest(args=args):
                return_code, result = self.invoke_cli_without_runtime(args)
                self.assertEqual(return_code, 1)
                self.assertEqual(result["error_code"], "LONG_CANDIDATE_ADMISSION_REQUIRED")

    def test_valid_comparison_uses_8_and_12_threads_and_each_actual_duration(self):
        result = self.compare()
        self.assertEqual(result["baseline_threads"], 8)
        self.assertEqual(result["sfm_threads"], 12)
        self.assertEqual(result["baseline_pairs_per_minute"], 120)
        self.assertEqual(result["candidate_pairs_per_minute"], 240)
        self.assertEqual(result["observed_window_ratio"], 2)
        self.assertFalse(result["gpu_or_full_geometry_acceptance"])
        changed = deepcopy(self.candidate)
        changed["admission_duration_seconds"] = 195
        self.assertAlmostEqual(self.compare(candidate=changed)["observed_window_ratio"], 360 / 195)

    def test_cpu_mean_80_is_accepted_but_above_80_is_rejected(self):
        changed = deepcopy(self.candidate)
        changed["cpu_sampled_mean_percent"] = 80
        self.assertEqual(self.compare(candidate=changed)["status"], "accepted_short_cpu_candidate_admission")
        changed["cpu_sampled_mean_percent"] = 80.01
        self.assert_admission_rejected(candidate=changed)

    def test_each_required_admission_safety_condition_is_enforced_for_both_reports(self):
        changes = (
            ("mode", "full"), ("status", "succeeded"), ("source_database_sha256", "b" * 64),
            ("pipeline_commit", "b" * 40),
            ("config_sha256", "e" * 64), ("source_manifest_sha256", "e" * 64),
            ("tool_manifest_sha256", "e" * 64),
            ("source_checkpoint_unchanged", False), ("source_checkpoint_unchanged", 1),
            ("process_tree_stopped", False), ("process_tree_stopped", 1),
            ("terminal_evidence_confirmed", False), ("terminal_evidence_confirmed", 1),
            ("final_snapshot_status", "failed"), ("admission_duration_seconds", 179.99),
            ("admission_duration_seconds", 195.01),
        )
        for target in ("baseline", "candidate"):
            for key, value in changes:
                changed = deepcopy(getattr(self, target))
                changed[key] = value
                with self.subTest(target=target, key=key, value=value):
                    self.assert_admission_rejected(**{target: changed})
        changed = deepcopy(self.baseline)
        changed["sfm_threads"] = 4
        self.assert_admission_rejected(baseline=changed)
        changed = deepcopy(self.candidate)
        changed["sfm_threads"] = 8
        self.assert_admission_rejected(candidate=changed)

    def test_resource_errors_or_trip_prevent_admission_even_when_faster(self):
        for target in ("baseline", "candidate"):
            for key, value in (("errors", ["EXPERIMENT_RAM_HEADROOM_LOW"]), ("trip", "EXPERIMENT_DISK_HEADROOM_LOW")):
                changed = deepcopy(getattr(self, target))
                changed["resources"][key] = value
                with self.subTest(target=target, key=key):
                    self.assert_admission_rejected(**{target: changed})

    def test_no_growth_wrong_initial_or_no_speedup_prevents_admission(self):
        for final in (25997, 25996, 26357, 26356):
            changed = deepcopy(self.candidate)
            changed["final_pair_counts"]["complete_pair_records"] = final
            with self.subTest(final=final):
                self.assert_admission_rejected(candidate=changed)
        for target in ("baseline", "candidate"):
            changed = deepcopy(getattr(self, target))
            changed["initial_pair_counts"]["complete_pair_records"] += 1
            self.assert_admission_rejected(**{target: changed})

    def test_missing_required_numeric_fields_fail_with_stable_admission_error(self):
        for target in ("baseline", "candidate"):
            for key in ("admission_duration_seconds", "cpu_sampled_mean_percent", "cpu_max_60s_mean_percent",
                        "cpu_sample_count", "cpu_observed_span_seconds", "initial_pair_counts", "final_pair_counts"):
                changed = deepcopy(getattr(self, target))
                del changed[key]
                with self.subTest(target=target, key=key):
                    self.assert_admission_rejected(**{target: changed})
            for group in ("initial_pair_counts", "final_pair_counts"):
                changed = deepcopy(getattr(self, target))
                changed[group] = {}
                with self.subTest(target=target, group=group):
                    self.assert_admission_rejected(**{target: changed})

    def test_malformed_or_nonfinite_numeric_fields_fail_closed(self):
        for target in ("baseline", "candidate"):
            for value in (None, "180", True, False, {}, [], float("nan"), float("inf"), -float("inf")):
                changed = deepcopy(getattr(self, target))
                changed["admission_duration_seconds"] = value
                with self.subTest(target=target, field="duration", value=value):
                    self.assert_admission_rejected(**{target: changed})
            for value in (None, "35.8", True, False, {}, [], -1, float("nan"), float("inf"), -float("inf")):
                for cpu_field in ("cpu_sampled_mean_percent", "cpu_max_60s_mean_percent"):
                    changed = deepcopy(getattr(self, target))
                    changed[cpu_field] = value
                    with self.subTest(target=target, field=cpu_field, value=value):
                        self.assert_admission_rejected(**{target: changed})
            for value in (None, "36", True, {}, [], -1, 23, 36.1, float("nan"), float("inf")):
                changed = deepcopy(getattr(self, target))
                changed["cpu_sample_count"] = value
                with self.subTest(target=target, field="sample_count", value=value):
                    self.assert_admission_rejected(**{target: changed})
            for value in (None, "175", True, {}, [], -1, 119.99, float("nan"), float("inf")):
                changed = deepcopy(getattr(self, target))
                changed["cpu_observed_span_seconds"] = value
                with self.subTest(target=target, field="observed_span", value=value):
                    self.assert_admission_rejected(**{target: changed})
            for group in ("initial_pair_counts", "final_pair_counts"):
                for value in (None, "26717", True, {}, [], -1, 26717.5, float("nan"), float("inf")):
                    changed = deepcopy(getattr(self, target))
                    changed[group]["complete_pair_records"] = value
                    with self.subTest(target=target, group=group, value=value):
                        self.assert_admission_rejected(**{target: changed})

    def test_malformed_report_and_nested_objects_fail_with_stable_error(self):
        for target in ("baseline", "candidate"):
            for value in (None, [], "report", 12):
                # Explicit call is needed because compare() convenience treats
                # None as its default rather than a malformed provided report.
                args = [self.baseline, self.candidate]
                args[0 if target == "baseline" else 1] = value
                with self.subTest(target=target, value=value), self.assertRaises(SupervisionError) as caught:
                    module.compare_admission_reports(*args, self.profile)
                self.assertEqual(caught.exception.code, "LONG_CANDIDATE_ADMISSION_REQUIRED")
            for group in ("resources", "initial_pair_counts", "final_pair_counts"):
                for value in (None, [], "private-path-not-for-error", 42):
                    changed = deepcopy(getattr(self, target))
                    changed[group] = value
                    with self.subTest(target=target, group=group, value=value):
                        self.assert_admission_rejected(**{target: changed})

    def test_resource_evidence_cannot_be_missing_or_malformed(self):
        for target in ("baseline", "candidate"):
            for resources in ({}, {"errors": []}, {"trip": None}, {"errors": None, "trip": None},
                              {"errors": "", "trip": None}, {"errors": {}, "trip": None}):
                changed = deepcopy(getattr(self, target))
                changed["resources"] = resources
                with self.subTest(target=target, resources=resources):
                    self.assert_admission_rejected(**{target: changed})

    def tool_fixture(self):
        root = self.base / "tool-root"
        root.mkdir()
        entries = {}
        for name in REQUIRED_TOOLS:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("# synthetic allowlisted tool: " + name).encode())
            entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = root / "tool-manifest.json"
        manifest.write_text(json.dumps({"schema_version": "1.0", "files": entries}), encoding="utf-8")
        return root, entries

    def assert_tool_rejected(self, root):
        with self.assertRaises(SupervisionError) as caught:
            module.verify_tool_manifest(root)
        self.assertIn(caught.exception.code, {"LONG_TOOL_MANIFEST_REQUIRED", "LONG_TOOL_HASH_MISMATCH"})

    def test_exact_allowlisted_tool_manifest_returns_its_own_hash(self):
        root, _ = self.tool_fixture()
        self.assertEqual(module.verify_tool_manifest(root), hashlib.sha256((root / "tool-manifest.json").read_bytes()).hexdigest())

    def test_missing_or_oversized_tool_manifest_rejected(self):
        root = self.base / "missing"
        root.mkdir()
        self.assert_tool_rejected(root)
        (root / "tool-manifest.json").write_bytes(b" " * (128 * 1024 + 1))
        self.assert_tool_rejected(root)

    def test_missing_tool_or_mismatching_hash_rejected(self):
        root, entries = self.tool_fixture()
        name = next(iter(entries))
        (root / name).write_bytes(b"changed after manifest")
        self.assert_tool_rejected(root)
        (root / name).unlink()
        self.assert_tool_rejected(root)

    def test_nonobject_missing_or_unapproved_manifest_entries_rejected(self):
        root, entries = self.tool_fixture()
        for files in ([], {}, {**entries, "../external.py": "a" * 64}, {**entries, "extra-tool.py": "a" * 64}):
            (root / "tool-manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
            with self.subTest(files_type=type(files).__name__, count=len(files)):
                self.assert_tool_rejected(root)

    def test_manifest_itself_symlink_or_reparse_is_rejected_before_read(self):
        root, _ = self.tool_fixture()
        manifest = root / "tool-manifest.json"
        original_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == manifest or original_symlink(path)):
            self.assert_tool_rejected(root)
        original_stat = Path.stat
        def reparse(path, *args, **kwargs):
            actual = original_stat(path, *args, **kwargs)
            if path == manifest:
                class Fake:
                    st_file_attributes = 1024
                    def __getattr__(self, key):
                        return getattr(actual, key)
                return Fake()
            return actual
        with patch.object(Path, "stat", reparse):
            self.assert_tool_rejected(root)

    def test_required_module_reparse_ancestor_rejected(self):
        root, _ = self.tool_fixture()
        unsafe_parent = root / "backend"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == unsafe_parent or original(path)):
            self.assert_tool_rejected(root)

    def bootstrap(self, *, simulated_root=None):
        source = Path(__file__).resolve().parents[2] / "scripts/run_long_experiment.py"
        spec = importlib.util.spec_from_file_location("unit_long_bootstrap", source)
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        if simulated_root is not None:
            result.__file__ = str(simulated_root / "scripts/run_long_experiment.py")
        return result

    def test_stdlib_bootstrap_gate_accepts_exact_pinned_fixture_before_backend_import(self):
        root, _ = self.tool_fixture()
        bootstrap = self.bootstrap(simulated_root=root)
        with patch.object(builtins, "__import__", wraps=builtins.__import__) as imports:
            bootstrap.verify_before_import(root)
        self.assertFalse(any(str(call.args[0]).startswith("backend") for call in imports.call_args_list))

    def test_explicit_execute_rejects_modified_tool_before_any_backend_import(self):
        root, _ = self.tool_fixture()
        (root / "backend/experiments/process.py").write_bytes(b"modified after hash manifest")
        bootstrap = self.bootstrap(simulated_root=root)
        stdout = io.StringIO()
        imported = []
        original_import = builtins.__import__
        def no_backend(name, *args, **kwargs):
            if name.startswith("backend"):
                imported.append(name)
                raise AssertionError("hash gate must run before overlay imports")
            return original_import(name, *args, **kwargs)
        with patch.object(sys, "argv", ["run_long_experiment.py", "--execute-long-test"]), \
                patch.object(builtins, "__import__", side_effect=no_backend), redirect_stdout(stdout):
            return_code = bootstrap.main()
        self.assertEqual(return_code, 1)
        self.assertEqual(imported, [])
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "not_completed", "error_code": "LONG_TOOL_HASH_GATE_FAILED"})

    def test_explicit_execute_requires_manifest_before_any_backend_import(self):
        root = self.base / "without-manifest"
        (root / "scripts").mkdir(parents=True)
        bootstrap = self.bootstrap(simulated_root=root)
        stdout = io.StringIO()
        original_import = builtins.__import__
        def no_backend(name, *args, **kwargs):
            if name.startswith("backend"):
                self.fail("missing manifest must fail before backend import")
            return original_import(name, *args, **kwargs)
        with patch.object(sys, "argv", ["run_long_experiment.py", "--execute-long-test"]), \
                patch.object(builtins, "__import__", side_effect=no_backend), redirect_stdout(stdout):
            self.assertEqual(bootstrap.main(), 1)
        self.assertEqual(json.loads(stdout.getvalue())["error_code"], "LONG_TOOL_HASH_GATE_FAILED")

    def test_unsealed_overlay_default_prepare_remains_offline_and_available(self):
        import backend
        root = self.base / "unsealed-overlay"
        (root / "scripts").mkdir(parents=True)
        bootstrap = self.bootstrap(simulated_root=root)
        profile_path = self.write_json("bootstrap-profile.json", self.profile)
        stdout = io.StringIO()
        with ExitStack() as stack:
            for name in ("execute_long_test", "require_admin", "read_windows_state", "read_queue_state", "read_current_launcher_ownership",
                         "HostResourceReader", "NvidiaSmiReader", "runtime_gpu_check", "exclusive_long_test"):
                mock = stack.enter_context(patch.object(module, name, side_effect=AssertionError("default prepare must remain offline")))
                stack.callback(mock.assert_not_called)
            stack.enter_context(patch.object(bootstrap, "verify_before_import", side_effect=AssertionError("prepare requires no manifest")))
            stack.enter_context(patch.object(sys, "path", list(sys.path)))
            stack.enter_context(patch.object(backend, "__path__", list(backend.__path__)))
            stack.enter_context(patch.object(sys, "argv", ["run_long_experiment.py", "--profile", str(profile_path)]))
            stack.enter_context(redirect_stdout(stdout))
            self.assertEqual(bootstrap.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "prepared_no_execution")
        self.assertFalse((root / "tool-manifest.json").exists())

    def test_execute_flag_abbreviation_is_rejected_before_backend_import(self):
        bootstrap = self.bootstrap(simulated_root=self.base / "no-manifest")
        profile_path = self.write_json("abbreviation-profile.json", self.profile)
        original_import = builtins.__import__
        imported = []
        def no_backend(name, *args, **kwargs):
            if name.startswith("backend"):
                imported.append(name)
                raise AssertionError("abbreviated execute must be rejected before backend imports")
            return original_import(name, *args, **kwargs)
        for bad_flag in ("--execute-long-t", "--execute", "--execute-long-test=true"):
            with self.subTest(bad_flag=bad_flag), \
                    patch.object(sys, "argv", ["run_long_experiment.py", "--profile", str(profile_path), bad_flag]), \
                    patch.object(builtins, "__import__", side_effect=no_backend), \
                    patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    bootstrap.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(imported, [])

    def mutex_api(self, *, handle=1234, wait=0, wait_exception=None):
        api = SimpleNamespace(
            CreateMutexW=Mock(return_value=handle),
            WaitForSingleObject=Mock(return_value=wait, side_effect=wait_exception),
            ReleaseMutex=Mock(return_value=True), CloseHandle=Mock(return_value=True),
        )
        return api

    def test_exclusive_mutex_creation_failure_does_not_yield_or_close_invalid_handle(self):
        api = self.mutex_api(handle=None)
        with patch.object(module.os, "name", "nt"), patch.object(module.ctypes, "WinDLL", return_value=api), \
                self.assertRaisesRegex(SupervisionError, "LONG_EXCLUSIVE_LOCK_UNAVAILABLE"):
            with module.exclusive_long_test():
                self.fail("no valid mutex means no executing body")
        api.CloseHandle.assert_not_called()
        api.ReleaseMutex.assert_not_called()

    def test_exclusive_mutex_busy_or_bad_wait_never_yields_but_closes_handle(self):
        for wait, code in ((258, "LONG_EXCLUSIVE_LOCK_BUSY"), (0xFFFFFFFF, "LONG_EXCLUSIVE_LOCK_UNAVAILABLE")):
            api = self.mutex_api(wait=wait)
            with self.subTest(wait=wait), patch.object(module.os, "name", "nt"), \
                    patch.object(module.ctypes, "WinDLL", return_value=api), self.assertRaisesRegex(SupervisionError, code):
                with module.exclusive_long_test():
                    self.fail("busy or failed mutex wait must not execute body")
            api.WaitForSingleObject.assert_called_once_with(1234, 0)
            api.CloseHandle.assert_called_once_with(1234)
            api.ReleaseMutex.assert_not_called()

    def test_exclusive_mutex_normal_and_abandoned_acquisition_release_then_close(self):
        for wait in (0, 128):
            api = self.mutex_api(wait=wait)
            order = []
            api.ReleaseMutex.side_effect = lambda *_: order.append("release") or True
            api.CloseHandle.side_effect = lambda *_: order.append("close") or True
            with self.subTest(wait=wait), patch.object(module.os, "name", "nt"), \
                    patch.object(module.ctypes, "WinDLL", return_value=api):
                with module.exclusive_long_test():
                    order.append("body")
            self.assertEqual(order, ["body", "release", "close"])
            api.CreateMutexW.assert_called_once_with(None, False, r"Global\Re3DRestrictedIndependentReconstruction")

    def test_exclusive_mutex_released_on_body_exception_or_keyboard_interrupt(self):
        for error in (RuntimeError("synthetic body error"), KeyboardInterrupt()):
            api = self.mutex_api()
            with self.subTest(error=type(error).__name__), patch.object(module.os, "name", "nt"), \
                    patch.object(module.ctypes, "WinDLL", return_value=api), self.assertRaises(type(error)):
                with module.exclusive_long_test():
                    raise error
            api.ReleaseMutex.assert_called_once_with(1234)
            api.CloseHandle.assert_called_once_with(1234)

    def test_exclusive_mutex_wait_exception_closes_unacquired_handle(self):
        api = self.mutex_api(wait_exception=KeyboardInterrupt())
        with patch.object(module.os, "name", "nt"), patch.object(module.ctypes, "WinDLL", return_value=api), \
                self.assertRaises(KeyboardInterrupt):
            with module.exclusive_long_test():
                self.fail("interrupted wait must not execute body")
        api.CloseHandle.assert_called_once_with(1234)
        api.ReleaseMutex.assert_not_called()

    def test_exclusive_mutex_nonwindows_fails_before_native_api_access(self):
        with patch.object(module.os, "name", "posix"), patch.object(module.ctypes, "WinDLL") as library, \
                self.assertRaisesRegex(SupervisionError, "LONG_EXCLUSIVE_LOCK_UNAVAILABLE"):
            with module.exclusive_long_test():
                self.fail("unavailable containment host must not execute body")
        library.assert_not_called()


if __name__ == "__main__":
    unittest.main()
