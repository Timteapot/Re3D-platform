from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.experiments.c_profile import (
    BASE_COMMIT, BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA, PIPELINE_SCRIPT_SHA,
    GIB, build_c_pipeline_overlay, validate_c_profile, validate_terminal_binding,
)
from backend.experiments.supervision import SupervisionError


def local_c_profile(root):
    return {"schema_version": "1.0", "profile_kind": "local_c_only_candidate", "autorun": False,
        "execution": {"scope": "local-only", "branches": ["c"], "gpu_required": False,
                      "platform_job_resume": False, "openmvs_mode": "cpu-only-vendor-build"},
        "pipeline": {"commit": BASE_COMMIT, "config_sha256": BASE_CONFIG_SHA,
                     "pipeline_script_sha256": PIPELINE_SCRIPT_SHA, "colmap_script_sha256": COLMAP_SCRIPT_SHA},
        "source": {"root": str(root / "sealed-handoff"), "cpu_result_sha256": "a" * 64,
                   "terminal_receipt_sha256": "b" * 64, "handoff_manifest_sha256": "c" * 64},
        "target_hardware": {"logical_cpus": 32, "host_ram_bytes": 34141495296},
        "target_experiment": {"scene": "synthetic-c-only", "root": str(root / "_experiments/synthetic-c-only")},
        "budget": {"original_total_seconds": 14400, "consumed_sfm_seconds": 4920.218,
                   "remaining_seconds": 9479.782, "source_terminal_receipt_sha256": "b" * 64},
        "cpu": {"openmvs_threads": 8, "texture_threads": 8},
        "protection": {"reservation_bytes": 8 * GIB, "disk_floor_bytes": 20 * GIB,
                       "evidence_creation_free_bytes": 10 * GIB, "evidence_disk_floor_bytes": 2 * GIB,
                       "available_ram_floor_bytes": 8 * GIB, "host_guard_seconds": 1,
                       "resource_sample_seconds": 5, "directory_sample_seconds": 15}}


def terminal_receipt():
    return {"schema_version": "1.0", "status": "accepted_cpu_sfm_succeeded",
            "hashes": {"cpu_result_sha256": "a" * 64},
            "tool_identity": {"frozen_pipeline_commit": BASE_COMMIT},
            "proofs": {"process_tree_stopped": True, "observation_relay_stopped": True,
                       "snapshot_worker_stopped": True, "terminal_evidence_confirmed": True,
                       "source_checkpoint_unchanged": True, "final_snapshot_status": "valid",
                       "auxiliary_sampler_closed": True, "auxiliary_sampler_thread_closed": True},
            "timing": {"cumulative_budget_charge_seconds": 4920.218000000001,
                       "unused_total_budget_seconds": 9479.782, "total_budget_not_reset_or_extended": True}}


def baseline_config():
    return {"openmvs_dense_resolution": {"resolution_level": 1, "min_resolution": 640, "max_resolution": 1024},
            "c": {"number_views": 6, "ignore_mask_label": 0, "openmvs_threads": 24},
            "texture": {"global_seam_leveling": 0, "local_seam_leveling": 0, "sharpness_weight": 0.25,
                        "outlier_threshold": 0.06, "ignore_mask_label": 0, "empty_color": 16777215,
                        "max_texture_size": 8192, "max_threads": 24},
            "mesh": {"remove_spurious": 4, "close_holes": 30, "smooth": 2},
            "a_v4": {"batch_size": 4, "openmvs_threads": 16}, "b_v2": {"source_views": 7, "openmvs_threads": 24}}


class CProfileTests(unittest.TestCase):
    def test_unsupported_evidence_floor_is_not_silently_lowered(self):
        profile = local_c_profile(Path(__file__).resolve().parents[2] / '.tmp-tests/c-profile-virtual-root')
        profile['protection']['evidence_disk_floor_bytes'] = 4 * GIB
        with self.assertRaisesRegex(SupervisionError, 'EVIDENCE_FLOOR_UNSUPPORTED'):
            validate_c_profile(profile)

    def setUp(self):
        # These are pure configuration tests; do not create a temp directory at
        # all. The fictitious absolute root is never read or written.
        self.root = Path(__file__).resolve().parents[2] / ".tmp-tests/c-profile-virtual-root"
        self.profile = local_c_profile(self.root)

    def test_validation_does_not_touch_disk_probe_hardware_or_launch(self):
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected launch")), \
                patch.object(Path, "exists", side_effect=AssertionError("unexpected filesystem probe")), \
                patch.object(Path, "open", side_effect=AssertionError("unexpected file read")):
            self.assertIs(validate_c_profile(self.profile), self.profile)
        self.assertFalse(Path(self.profile["target_experiment"]["root"]).exists())

    def test_no_automatic_server_platform_gpu_or_argv_configuration(self):
        mutations = [lambda p: p.update(autorun=True), lambda p: p.update(command=["powershell.exe"]),
            lambda p: p["execution"].update(scope="server"), lambda p: p["execution"].update(branches=["a", "c"]),
            lambda p: p["execution"].update(gpu_required=True), lambda p: p["execution"].update(platform_job_resume=True),
            lambda p: p["execution"].update(openmvs_mode="cuda"), lambda p: p["cpu"].update(openmvs_threads=24),
            lambda p: p["cpu"].update(texture_threads=True), lambda p: p["source"].update(cpu_result_sha256=None)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                value = deepcopy(self.profile); mutation(value)
                with self.assertRaises(SupervisionError): validate_c_profile(value)

    def test_frozen_identity_and_hardware_are_pinned(self):
        for section, key, value in [("pipeline", "commit", "0" * 40), ("pipeline", "config_sha256", "0" * 64),
                ("pipeline", "pipeline_script_sha256", "0" * 64), ("target_hardware", "logical_cpus", 40),
                ("target_hardware", "logical_cpus", True), ("target_hardware", "host_ram_bytes", 16 * GIB)]:
            candidate = deepcopy(self.profile); candidate[section][key] = value
            with self.subTest(key=key):
                with self.assertRaises(SupervisionError): validate_c_profile(candidate)

    def test_headroom_floors_cannot_be_reduced_or_sampling_weakened(self):
        for key, value in self.profile["protection"].items():
            candidate = deepcopy(self.profile)
            candidate["protection"][key] = value - 1 if key.endswith("bytes") else value + 1
            with self.subTest(key=key):
                with self.assertRaises(SupervisionError): validate_c_profile(candidate)
        value = deepcopy(self.profile); value["protection"]["available_ram_floor_bytes"] = 10 * GIB
        validate_c_profile(value)  # Stricter resource admission is allowed.

    def test_budget_is_explicit_finite_positive_conserved_and_not_reset(self):
        for key, value in [("original_total_seconds", 28800), ("remaining_seconds", 14400),
                ("consumed_sfm_seconds", 0), ("consumed_sfm_seconds", True), ("remaining_seconds", float("nan")),
                ("remaining_seconds", float("inf")), ("source_terminal_receipt_sha256", "d" * 64)]:
            candidate = deepcopy(self.profile); candidate["budget"][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(SupervisionError): validate_c_profile(candidate)
        candidate = deepcopy(self.profile); del candidate["budget"]["consumed_sfm_seconds"]
        with self.assertRaises(SupervisionError): validate_c_profile(candidate)

    def test_new_scene_never_overlaps_handoff_or_platform_job(self):
        for source, target in [(self.root / "_experiments", self.root / "_experiments/synthetic-c-only"),
                (self.root / "_experiments/synthetic-c-only/payload", self.root / "_experiments/synthetic-c-only"),
                (self.root / "sealed-handoff", self.root / "jobs/_experiments/synthetic-c-only")]:
            candidate = deepcopy(self.profile); candidate["source"]["root"] = str(source)
            candidate["target_experiment"]["root"] = str(target)
            with self.assertRaises(SupervisionError): validate_c_profile(candidate)
        candidate = deepcopy(self.profile); candidate["target_experiment"]["scene"] = "other-scene"
        with self.assertRaises(SupervisionError): validate_c_profile(candidate)
        for source in ("\\\\remote\\share\\handoff", "//remote/share/handoff", str(self.root / "source:stream"),
                       str(self.root / "bad\x00path")):
            candidate = deepcopy(self.profile); candidate["source"]["root"] = source
            with self.subTest(source=source):
                with self.assertRaises(SupervisionError): validate_c_profile(candidate)

    def test_terminal_budget_binds_actual_hash_result_proofs_and_float_roundoff(self):
        value = validate_terminal_binding(self.profile, terminal_receipt(), "b" * 64)
        self.assertAlmostEqual(value["remaining_seconds"], 9479.782)
        self.assertLessEqual(value["remaining_seconds"], 9479.782)
        for key in terminal_receipt()["proofs"]:
            receipt = terminal_receipt(); receipt["proofs"][key] = False
            with self.subTest(proof=key):
                with self.assertRaises(SupervisionError): validate_terminal_binding(self.profile, receipt, "b" * 64)
        for receipt, actual in [(terminal_receipt(), "c" * 64), ({}, "b" * 64)]:
            with self.assertRaises(SupervisionError): validate_terminal_binding(self.profile, receipt, actual)
        receipt = terminal_receipt(); receipt["hashes"]["cpu_result_sha256"] = "f" * 64
        with self.assertRaises(SupervisionError): validate_terminal_binding(self.profile, receipt, "b" * 64)
        receipt = terminal_receipt(); receipt["timing"]["cumulative_budget_charge_seconds"] = 100
        with self.assertRaises(SupervisionError): validate_terminal_binding(self.profile, receipt, "b" * 64)

    def test_profile_cannot_reclaim_spent_time_even_when_sum_is_conserved(self):
        self.profile["budget"].update(consumed_sfm_seconds=1, remaining_seconds=14399)
        validate_c_profile(self.profile)  # Syntax is not admission.
        with self.assertRaisesRegex(SupervisionError, "C_ONLY_BUDGET_RECEIPT_MISMATCH"):
            validate_terminal_binding(self.profile, terminal_receipt(), "b" * 64)

    def test_overlay_only_changes_two_threads_no_quality_or_ab_change(self):
        baseline = baseline_config(); before = deepcopy(baseline)
        overlay, diff = build_c_pipeline_overlay(baseline, self.profile)
        self.assertEqual(baseline, before)
        self.assertEqual({row["path"] for row in diff}, {"/c/openmvs_threads", "/texture/max_threads"})
        self.assertEqual(overlay["a_v4"], before["a_v4"])
        self.assertEqual(overlay["b_v2"], before["b_v2"])
        self.assertEqual(overlay["texture"]["max_texture_size"], 8192)
        self.assertEqual(overlay["c"]["number_views"], 6)
        self.assertEqual(overlay["texture"]["max_threads"], 8)
        for group, key, value in [("texture", "max_texture_size", 4096), ("texture", "global_seam_leveling", 1),
                ("openmvs_dense_resolution", "max_resolution", 640), ("c", "number_views", 4),
                ("mesh", "smooth", 1)]:
            candidate = baseline_config(); candidate[group][key] = value
            with self.subTest(key=key):
                with self.assertRaises(SupervisionError): build_c_pipeline_overlay(candidate, self.profile)

    def test_example_is_non_executable_until_source_hashes_are_bound(self):
        example_path = Path(__file__).resolve().parents[2] / "config/local-c-only.example.json"
        example = json.loads(example_path.read_bytes())
        self.assertIs(example["autorun"], False)
        with self.assertRaisesRegex(SupervisionError, "C_ONLY_SOURCE_IDENTITY_UNBOUND"):
            validate_c_profile(example)


if __name__ == "__main__":
    unittest.main()
