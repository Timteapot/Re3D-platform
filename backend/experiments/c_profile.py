"""Pure local C-only configuration gates; no host probes or reconstruction.

``source.root`` is a sealed, verified SfM handoff bundle, not the old experiment
or a platform job directory. Execution must independently verify/import it with
``hybrid_handoff`` and bind the small terminal receipt before creating a scene.
Profile validation alone is deliberately not an admission or a proof of safety.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from backend.experiments.high_profile import (
    BASE_COMMIT, BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA, PIPELINE_SCRIPT_SHA, config_diff,
)
from backend.experiments.resources import GIB
from backend.experiments.supervision import SupervisionError

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCENE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_BUDGET_TOTAL = Decimal("14400")
_BUDGET_TOLERANCE = Decimal("0.001")


def require(value, code):
    if not value:
        raise SupervisionError(code)


def _object(value, keys, code):
    require(type(value) is dict and set(value) == set(keys), code)
    return value


def _number(value, code):
    require(type(value) in (int, float), code)  # bool is never a numeric budget.
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise SupervisionError(code) from None
    require(result.is_finite(), code)
    return result


def _path(value, code):
    require(isinstance(value, str) and bool(value) and not any(ord(char) < 32 for char in value), code)
    result = Path(value)
    require(result.is_absolute() and ".." not in result.parts
            and not result.drive.startswith("\\\\") and not value.startswith(("\\\\", "//"))
            and not any(":" in part for part in result.parts[1:])
            and not any(part.casefold() == "jobs" for part in result.parts), code)
    return result


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def validate_c_profile(profile):
    """Strict, side-effect-free settings for a new local single C route.

    The example intentionally contains unbound SHA placeholders. Filling them
    does not certify the source: the executing runner must call the existing
    handoff verifier and :func:`validate_terminal_binding` against actual bytes.
    """
    _object(profile, {"schema_version", "profile_kind", "autorun", "execution", "pipeline",
                      "source", "target_hardware", "target_experiment", "budget", "cpu", "protection"},
            "C_ONLY_PROFILE_FIELDS_INVALID")
    require(profile["schema_version"] == "1.0"
            and profile["profile_kind"] == "local_c_only_candidate"
            and profile["autorun"] is False, "C_ONLY_PROFILE_KIND_INVALID")
    expected_execution = {"scope": "local-only", "branches": ["c"], "gpu_required": False,
                          "platform_job_resume": False, "openmvs_mode": "cpu-only-vendor-build"}
    execution = _object(profile["execution"], expected_execution, "C_ONLY_EXECUTION_INVALID")
    require(all(type(execution[key]) is type(value) and execution[key] == value
                for key, value in expected_execution.items()), "C_ONLY_EXECUTION_INVALID")
    expected_pipeline = {"commit": BASE_COMMIT, "config_sha256": BASE_CONFIG_SHA,
                         "pipeline_script_sha256": PIPELINE_SCRIPT_SHA,
                         "colmap_script_sha256": COLMAP_SCRIPT_SHA}
    pipeline = _object(profile["pipeline"], expected_pipeline, "C_ONLY_FROZEN_IDENTITY_CHANGED")
    require(pipeline == expected_pipeline, "C_ONLY_FROZEN_IDENTITY_CHANGED")
    source = _object(profile["source"], {"root", "cpu_result_sha256", "terminal_receipt_sha256",
                                          "handoff_manifest_sha256"}, "C_ONLY_SOURCE_INVALID")
    require(all(_hash(source[key]) for key in ("cpu_result_sha256", "terminal_receipt_sha256",
                                               "handoff_manifest_sha256")), "C_ONLY_SOURCE_IDENTITY_UNBOUND")
    source_root = _path(source["root"], "C_ONLY_SOURCE_PATH_INVALID")
    hardware = _object(profile["target_hardware"], {"logical_cpus", "host_ram_bytes"},
                       "C_ONLY_HARDWARE_INVALID")
    require(type(hardware["logical_cpus"]) is int and hardware["logical_cpus"] == 32
            and type(hardware["host_ram_bytes"]) is int and hardware["host_ram_bytes"] == 34141495296,
            "C_ONLY_HARDWARE_INVALID")
    target = _object(profile["target_experiment"], {"scene", "root"}, "C_ONLY_TARGET_INVALID")
    require(isinstance(target["scene"], str) and _SCENE.fullmatch(target["scene"]), "C_ONLY_TARGET_INVALID")
    target_root = _path(target["root"], "C_ONLY_TARGET_INVALID")
    require(target_root.parent.name == "_experiments" and target_root.name == target["scene"],
            "C_ONLY_TARGET_INVALID")
    require(not target_root.is_relative_to(source_root) and not source_root.is_relative_to(target_root),
            "C_ONLY_SOURCE_TARGET_OVERLAP")
    cpu = _object(profile["cpu"], {"openmvs_threads", "texture_threads"}, "C_ONLY_THREAD_BUDGET_INVALID")
    require(all(type(value) is int and value == 8 for value in cpu.values()), "C_ONLY_THREAD_BUDGET_INVALID")
    guards = _object(profile["protection"], {"reservation_bytes", "disk_floor_bytes",
        "evidence_creation_free_bytes", "evidence_disk_floor_bytes", "available_ram_floor_bytes",
        "host_guard_seconds", "resource_sample_seconds", "directory_sample_seconds"},
        "C_ONLY_PROTECTION_INVALID")
    floors = {"reservation_bytes": 8 * GIB, "disk_floor_bytes": 20 * GIB,
              "evidence_creation_free_bytes": 10 * GIB, "evidence_disk_floor_bytes": 2 * GIB,
              "available_ram_floor_bytes": 8 * GIB}
    require(all(type(guards[key]) is int and guards[key] >= value for key, value in floors.items())
            and guards["evidence_creation_free_bytes"] >= guards["evidence_disk_floor_bytes"],
            "C_ONLY_HEADROOM_REDUCED")
    # The reused independent evidence sink has a fixed 2-GiB running floor.
    # Reject unsupported strengthening rather than silently applying a lower one.
    require(guards["evidence_disk_floor_bytes"] == 2 * GIB, "C_ONLY_EVIDENCE_FLOOR_UNSUPPORTED")
    require(all(type(guards[key]) is int and guards[key] == value for key, value in
                {"host_guard_seconds": 1, "resource_sample_seconds": 5, "directory_sample_seconds": 15}.items()),
            "C_ONLY_SAMPLING_CHANGED")
    budget = _object(profile["budget"], {"original_total_seconds", "consumed_sfm_seconds",
                                          "remaining_seconds", "source_terminal_receipt_sha256"},
                     "C_ONLY_BUDGET_INVALID")
    total = _number(budget["original_total_seconds"], "C_ONLY_BUDGET_INVALID")
    consumed = _number(budget["consumed_sfm_seconds"], "C_ONLY_BUDGET_INVALID")
    remaining = _number(budget["remaining_seconds"], "C_ONLY_BUDGET_INVALID")
    require(total == _BUDGET_TOTAL and 0 < consumed < total and 0 < remaining < total
            and abs(consumed + remaining - total) <= _BUDGET_TOLERANCE, "C_ONLY_BUDGET_RESET_OR_EXTENDED")
    require(budget["source_terminal_receipt_sha256"] == source["terminal_receipt_sha256"],
            "C_ONLY_BUDGET_RECEIPT_MISMATCH")
    return profile


def validate_terminal_binding(profile, terminal_receipt, actual_receipt_sha256):
    """Bind explicit remaining budget to the actual accepted small receipt.

    The caller supplies SHA256 calculated from the bytes it actually read, with
    normal/reparse-safe I/O. This function does not hash a different serialization
    or treat this acceptance receipt as a substitute for the full SfM handoff gate.
    """
    validate_c_profile(profile)
    require(_hash(actual_receipt_sha256)
            and actual_receipt_sha256 == profile["source"]["terminal_receipt_sha256"],
            "C_ONLY_TERMINAL_RECEIPT_CHANGED")
    require(type(terminal_receipt) is dict and terminal_receipt.get("schema_version") == "1.0"
            and terminal_receipt.get("status") == "accepted_cpu_sfm_succeeded",
            "C_ONLY_SOURCE_SFM_NOT_ACCEPTED")
    hashes = terminal_receipt.get("hashes", {})
    require(type(hashes) is dict and hashes.get("cpu_result_sha256") == profile["source"]["cpu_result_sha256"],
            "C_ONLY_SOURCE_CPU_RESULT_CHANGED")
    identity = terminal_receipt.get("tool_identity", {})
    require(type(identity) is dict and identity.get("frozen_pipeline_commit") == BASE_COMMIT,
            "C_ONLY_SOURCE_PIPELINE_CHANGED")
    proofs = terminal_receipt.get("proofs", {})
    required = ("process_tree_stopped", "observation_relay_stopped", "snapshot_worker_stopped",
                "terminal_evidence_confirmed", "source_checkpoint_unchanged", "auxiliary_sampler_closed",
                "auxiliary_sampler_thread_closed")
    require(type(proofs) is dict and all(proofs.get(key) is True for key in required)
            and proofs.get("final_snapshot_status") == "valid", "C_ONLY_SOURCE_PROOFS_INCOMPLETE")
    timing = terminal_receipt.get("timing", {})
    require(type(timing) is dict and timing.get("total_budget_not_reset_or_extended") is True,
            "C_ONLY_SOURCE_BUDGET_UNPROVEN")
    charged = _number(timing.get("cumulative_budget_charge_seconds"), "C_ONLY_SOURCE_BUDGET_INVALID")
    unused = _number(timing.get("unused_total_budget_seconds"), "C_ONLY_SOURCE_BUDGET_INVALID")
    budget = profile["budget"]
    require(charged > 0 and unused > 0 and abs(charged + unused - _BUDGET_TOTAL) <= _BUDGET_TOLERANCE
            and abs(charged - _number(budget["consumed_sfm_seconds"], "C_ONLY_BUDGET_INVALID")) <= _BUDGET_TOLERANCE
            and abs(unused - _number(budget["remaining_seconds"], "C_ONLY_BUDGET_INVALID")) <= _BUDGET_TOLERANCE,
            "C_ONLY_BUDGET_RECEIPT_MISMATCH")
    # Return the smaller real remaining allowance when round-off differs. Never
    # round up to a fresh four-hour budget or silently recover prior spent time.
    return {"original_total_seconds": 14400,
            "consumed_sfm_seconds": float(charged),
            "remaining_seconds": float(min(unused, _BUDGET_TOTAL - charged,
                                           _number(budget["remaining_seconds"], "C_ONLY_BUDGET_INVALID")))}


def build_c_pipeline_overlay(baseline, profile):
    """Only cap C and texture thread pools; preserve reconstruction quality."""
    validate_c_profile(profile)
    require(type(baseline) is dict, "C_ONLY_BASELINE_QUALITY_CHANGED")
    expected = {
        "openmvs_dense_resolution": {"resolution_level": 1, "min_resolution": 640, "max_resolution": 1024},
        "c": {"number_views": 6, "ignore_mask_label": 0, "openmvs_threads": 24},
        "texture": {"global_seam_leveling": 0, "local_seam_leveling": 0, "sharpness_weight": 0.25,
                    "outlier_threshold": 0.06, "ignore_mask_label": 0, "empty_color": 16777215,
                    "max_texture_size": 8192, "max_threads": 24},
        "mesh": {"remove_spurious": 4, "close_holes": 30, "smooth": 2},
    }
    for group, fields in expected.items():
        actual = baseline.get(group)
        require(type(actual) is dict and all(type(actual.get(key)) is type(value)
                and actual[key] == value for key, value in fields.items()), "C_ONLY_BASELINE_QUALITY_CHANGED")
    result = deepcopy(baseline)
    result["c"]["openmvs_threads"] = profile["cpu"]["openmvs_threads"]
    result["texture"]["max_threads"] = profile["cpu"]["texture_threads"]
    diff = config_diff(baseline, result)
    require({row["path"] for row in diff} == {"/c/openmvs_threads", "/texture/max_threads"},
            "C_ONLY_UNEXPECTED_OVERLAY_CHANGE")
    return result, diff
