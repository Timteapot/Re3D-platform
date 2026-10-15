"""Independent, hash-bound post-SfM sky C admission and supervisor wiring.

Admission is a synchronous read-only snapshot, not an execution permit. The
accepted SfM timing stays intact; a separate closed C ledger deducts every prior
attempt. Nothing here creates a claim, imports a model or launches a process.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import time
from types import SimpleNamespace

from backend.experiments.c_budget_chain import read_c_budget_chain
from backend.experiments.c_profile import validate_c_profile
from backend.experiments.c_runner import (
    _check_c_host_resources, _read_c_source_bindings, _verify_c_frozen_files,
    _verify_c_source_handoff,
)
from backend.experiments.c_stages import C_STAGE_NAMES
from backend.experiments.evidence import PRIVATE_PARENT
from backend.experiments.sky_assets import (
    read_sky_prepared_inventory, verify_sky_binding,
)
from backend.experiments.sky_mask_plan import build_sky_c_plan
from backend.experiments.sky_masks import FROZEN_SHA
from backend.experiments.stages import PipelineStagePlan
from backend.experiments.supervision import SupervisionError

_SHA = re.compile(r"[0-9a-f]{64}\Z")


def require(value, code):
    if not value:
        raise SupervisionError(code)


def validate_sky_profile(value):
    require(type(value) is dict and set(value) == {
        "schema_version", "profile_kind", "autorun", "c_profile", "mask_binding",
        "prepared_admission", "budget_lineage"}, "LOCAL_C_SKY_PROFILE_INVALID")
    require(value["schema_version"] == "1.0"
            and value["profile_kind"] == "local_sky_c_supervised_candidate"
            and value["autorun"] is False, "LOCAL_C_SKY_PROFILE_INVALID")
    base = validate_c_profile(value["c_profile"])
    for key in ("mask_binding", "prepared_admission"):
        row = value[key]
        require(type(row) is dict and set(row) == {"path", "sha256"}
                and isinstance(row["path"], str) and Path(row["path"]).is_absolute()
                and ".." not in Path(row["path"]).parts
                and isinstance(row["sha256"], str) and _SHA.fullmatch(row["sha256"]),
                "LOCAL_C_SKY_BINDING_INVALID")
    ledger = value["budget_lineage"]
    require(type(ledger) is dict and set(ledger) == {
        "root_profile_sha256", "root_claim_sha256", "root_charge_sha256",
        "expected_tip_sha256", "maximum_remaining_seconds"}, "LOCAL_C_SKY_LEDGER_INVALID")
    require(all(isinstance(ledger[key], str) and _SHA.fullmatch(ledger[key]) for key in ledger
                if key != "maximum_remaining_seconds"), "LOCAL_C_SKY_LEDGER_INVALID")
    remaining = ledger["maximum_remaining_seconds"]
    require(type(remaining) in (int, float) and 0 < remaining <= base["budget"]["remaining_seconds"],
            "LOCAL_C_SKY_BUDGET_INVALID")
    return value


def _reconcile(context):
    base, ledger = context["c_profile"], context["sky_profile"]["budget_lineage"]
    result = read_c_budget_chain(PRIVATE_PARENT, source_budget=context["source_budget"],
        cpu_result_sha256=base["source"]["cpu_result_sha256"],
        source_terminal_receipt_sha256=base["source"]["terminal_receipt_sha256"],
        **{key: ledger[key] for key in ("root_profile_sha256", "root_claim_sha256",
                                      "root_charge_sha256", "expected_tip_sha256")})
    require(result["remaining_seconds"] > 0, "LONG_BUDGET_EXHAUSTED")
    require(result["remaining_seconds"] <= ledger["maximum_remaining_seconds"],
            "LOCAL_C_SKY_BUDGET_WOULD_INCREASE")
    require(base["target_experiment"]["scene"] not in result["previous_scenes"],
            "LOCAL_C_SKY_SCENE_ALREADY_CHARGED")
    return result


def verify_sky_sources(context, health):
    """Finalization source proof; never mutate or import the accepted source."""
    base = context["c_profile"]
    result = _verify_c_source_handoff(base, context["source_profile"], context["terminal"], health)
    verify_sky_binding(context["binding_path"], context["binding_sha256"],
        compute_root=context["root"], source_cpu_sha=base["source"]["cpu_result_sha256"],
        handoff_sha=base["source"]["handoff_manifest_sha256"], health=health,
        new_target=not context["root"].exists(), verify_source_images=False)
    return result


def build_supervised_sky_plan(context, dimensions):
    """Only stage 03 changes; its isolated child must verify the same seal."""
    plan = build_sky_c_plan(binding_path=context["binding_path"],
        binding_sha256=context["binding_sha256"], pipeline_root=context["pipeline"],
        experiment_root=context["root"], dimensions=dimensions,
        map_python=Path(context["runtime"]["python"]), adapter_script=context["adapter_script"],
        config_path=context["config"], paths_path=context["pipeline"] / "configs/paths.local.json")
    stages = tuple(replace(stage, argv=(stage.argv[0], "-I", "-B", *stage.argv[1:],
        "--tool-manifest-sha256", context["tool_identity"]))
        if stage.name == "03-prepare-openmvs-input" else stage for stage in plan.stages)
    environment = dict(plan.environment)
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        environment[key] = "8"
    environment["CUDA_VISIBLE_DEVICES"] = "-1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in tuple(environment):
        if key.startswith(("APP_", "DATABASE_", "JWT_", "AUTH_", "SMTP_", "ACCESS_TOKEN_", "REFRESH_", "VITE_")):
            environment.pop(key, None)
    result = PipelineStagePlan(stages, environment, plan.dimensions)
    verify_sky_plan_runtime(result, context)
    return result


def verify_sky_plan_runtime(plan, context):
    from scripts.run_local_c import verify_plan_runtime
    require(tuple(stage.name for stage in plan.stages) == C_STAGE_NAMES,
            "LOCAL_C_SKY_PLAN_CHANGED")
    adapter = plan.stages[1]
    expected = tuple(str(v) for v in (context["runtime"]["python"], "-I", "-B",
        context["adapter_script"], "--mode", "prepare-openmvs", "--binding", context["binding_path"],
        "--binding-sha256", context["binding_sha256"], "--pipeline-root", context["pipeline"],
        "--images", context["root"] / "work/input/images", "--sparse",
        context["root"] / "work/shared/colmap/refined_text", "--output",
        context["root"] / "work/shared/openmvs_input", "--tool-manifest-sha256", context["tool_identity"]))
    require(adapter.argv == expected, "LOCAL_C_SKY_ADAPTER_ARGV_CHANGED")
    # The original runtime whitelist remains closed, with exactly one exception
    # for the separately sealed adapter, not arbitrary Python commands.
    verify_plan_runtime(SimpleNamespace(stages=tuple(s for s in plan.stages if s is not adapter)),
                        context["runtime"])


def verify_sky_runtime_stage(stage, context):
    from scripts.run_local_c import sha, signature, verify_runtime_stage, runtime_files
    from scripts.run_sky_c_supervised import verify_tool_files
    verify_tool_files(context["tool_root"], context["tool_identity"])
    if stage.name != "03-prepare-openmvs-input":
        verify_runtime_stage(stage, context["runtime"])
        return
    require(stage.argv == context["plan"].stages[1].argv, "LOCAL_C_SKY_ADAPTER_ARGV_CHANGED")
    runtime, pipeline = context["runtime"], context["pipeline"]
    names, interpreter = runtime_files(pipeline)
    require(set(names) == set(runtime["files"]) and interpreter == Path(runtime["python"]),
            "LOCAL_C_RUNTIME_CHANGED_AFTER_ADMISSION")
    require(all(signature(pipeline / name) == saved
                for name, saved in runtime["_stat_signatures"].items()),
            "LOCAL_C_RUNTIME_CHANGED_AFTER_ADMISSION")
    require(sha(Path(runtime["python"])) == runtime["python_sha256"], "LOCAL_C_RUNTIME_HASH_CHANGED")
    require(all(sha(pipeline / name) == expected for name, expected in FROZEN_SHA.items()),
            "LOCAL_C_SKY_FROZEN_HELPER_CHANGED")


def admit_sky_c(args, sky_profile, *, tool_identity, health_check=lambda: None):
    """No claims, target writes, model copies, monitors, sleeps or subprocesses."""
    from scripts.run_local_c import read, plain, verify_runtime_manifest
    started = time.monotonic()
    validate_sky_profile(sky_profile)
    require(isinstance(tool_identity, str) and _SHA.fullmatch(tool_identity), "LOCAL_C_SKY_TOOL_PIN_REQUIRED")
    require(read(args.profile, expected_sha256=args.profile_sha256) == sky_profile,
            "LOCAL_C_SKY_PROFILE_CHANGED")
    base = sky_profile["c_profile"]
    root = plain(base["target_experiment"]["root"], missing=True)
    require(not root.exists(), "LOCAL_C_NEW_TARGET_REQUIRED")
    pipeline, tool_root = plain(args.pipeline_root), plain(args.tool_root)
    from scripts.run_sky_c_supervised import verify_tool_files
    verify_tool_files(tool_root, tool_identity)
    config = _verify_c_frozen_files(pipeline)
    terminal, source_budget, source_profile = _read_c_source_bindings(args, base)
    protected = [source_profile["target_experiment"]["root"],
        source_profile["source_checkpoint"]["original_root"], source_profile["source_checkpoint"]["snapshot_root"],
        str(pipeline), str(tool_root), base["source"]["root"]]
    require(all(not root.is_relative_to(plain(value)) and not plain(value).is_relative_to(root)
                for value in protected), "LOCAL_C_SOURCE_TARGET_OVERLAP")
    context = dict(args=args, sky_profile=sky_profile, c_profile=base, root=root, pipeline=pipeline,
        config=config, terminal=terminal, source_profile=source_profile, source_budget=source_budget,
        tool_root=tool_root, tool_identity=tool_identity,
        adapter_script=tool_root / "scripts/run_sky_c_supervised.py",
        binding_path=plain(sky_profile["mask_binding"]["path"]),
        binding_sha256=sky_profile["mask_binding"]["sha256"])
    reconciliation = _reconcile(context)
    deadline = started + min(120, reconciliation["remaining_seconds"])
    def health():
        require(time.monotonic() < deadline, "LOCAL_C_PREFLIGHT_READ_DEADLINE")
        health_check()
    health()
    resources = _check_c_host_resources(root, base)
    runtime = verify_runtime_manifest(Path(args.runtime_manifest), args.runtime_manifest_sha256, pipeline)
    health()
    binding = verify_sky_binding(context["binding_path"], context["binding_sha256"],
        compute_root=root, source_cpu_sha=base["source"]["cpu_result_sha256"],
        handoff_sha=base["source"]["handoff_manifest_sha256"], health=health,
        verify_source_images=True)
    prepared = sky_profile["prepared_admission"]
    prepared_receipt = read(prepared["path"], expected_sha256=prepared["sha256"])
    require(prepared_receipt.get("status") == "mask_input_and_plan_preflight_passed"
            and prepared_receipt.get("execution_authorized") is False
            and prepared_receipt.get("binding_sha256") == context["binding_sha256"]
            and prepared_receipt.get("source_mask_manifest_sha256") == binding["manifest_sha256"]
            and prepared_receipt.get("actual_frozen_helpers_consumed_masks") is True,
            "LOCAL_C_SKY_PREPARED_RECEIPT_REJECTED")
    inventory = read_sky_prepared_inventory(binding, prepared_root=Path(prepared["path"]).parent, health=health)
    context.update(runtime=runtime, binding=binding, reconciliation=reconciliation,
        prepared_inventory=inventory, host_resource_snapshot=resources)
    # Binding labels were checked above; avoid decoding them a second time in
    # this same read-only admission. Final execution proof rechecks separately.
    source, handoff = _verify_c_source_handoff(base, source_profile, terminal, health)
    context.update(verified_source=source, verified_handoff=handoff)
    rows = binding["images"]
    dimensions = (rows[0]["width"], rows[0]["height"])
    require(all((row["width"], row["height"]) == dimensions for row in rows),
            "LOCAL_C_SKY_DIMENSIONS_CHANGED")
    context["plan"] = build_supervised_sky_plan(context, dimensions)
    require(_reconcile(context) == reconciliation, "LOCAL_C_BUDGET_CHAIN_CHANGED")
    require(not root.exists(), "LOCAL_C_NEW_TARGET_REQUIRED")
    health()
    context["host_resource_snapshot"] = _check_c_host_resources(root, base)
    context["identity"] = {"profile_sha256": args.profile_sha256,
        "tool_manifest_sha256": tool_identity, "sky_binding_sha256": context["binding_sha256"],
        "mask_manifest_sha256": binding["manifest_sha256"],
        "source_manifest_sha256": base["source"]["handoff_manifest_sha256"],
        "pipeline_commit": base["pipeline"]["commit"], "config_sha256": base["pipeline"]["config_sha256"]}
    context["admission_elapsed_seconds"] = max(0, time.monotonic() - started)
    return context


def preflight_sky_c(args, *, tool_identity):
    from scripts.run_local_c import read
    require(getattr(args, "execute_sky_c_only", False) is False,
            "LOCAL_C_EXECUTION_MODE_MISMATCH")
    profile = read(args.profile, expected_sha256=args.profile_sha256)
    context = admit_sky_c(args, profile, tool_identity=tool_identity)
    chain, base = context["reconciliation"], context["c_profile"]
    return {"schema_version": "1.0", "status": "read_only_sky_c_preflight_passed",
        "mode": "preflight", "temporary_experiment": True, "scope": "local-only", "branches": ["c"],
        "execution_authorized": False, "reconstruction_started": False,
        "target_created": False, "handoff_imported": False,
        "budget_claimed": False, "budget_charged": False, "budget_reset_or_extended": False,
        "server_accessed": False, "gpu_accessed": False, "source_modified": False,
        "child_process_started": False, "uac_requested_by_tool": False,
        "supervisor": "CStageSupervisor", "process_containment": "Windows Job Object",
        "source_verified": True, "sky_assets_verified": True, "sfm_mask_applied": False,
        "mask_scope": "post_sfm_c_dense_texture_only", "image_count": len(context["binding"]["images"]),
        "source_sfm_budget": context["source_budget"], "reconciled_budget": chain,
        "remaining_budget_seconds": chain["remaining_seconds"],
        "protection": base["protection"], "cpu": base["cpu"],
        "host_resource_snapshot": context["host_resource_snapshot"],
        "runtime_manifest_verified": True, "runtime_file_count": len(context["runtime"]["files"]),
        "identity": context["identity"], "scene": base["target_experiment"]["scene"],
        "target_root": str(context["root"]),
        "stages": [stage.observation() for stage in context["plan"].stages],
        "elapsed_seconds": context["admission_elapsed_seconds"],
        "native_mask_execution_verified": False,
        "deferred_checks": ["explicit_execution_authorization", "live_budget_chain_recheck_and_exclusive_claim",
            "execution_time_resources_and_source_recheck", "native_mask_consumption_and_geometry_acceptance"]}
