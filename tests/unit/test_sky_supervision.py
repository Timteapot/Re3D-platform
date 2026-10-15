"""Synthetic independent sky admission: no claim, import or native process."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.experiments import c_runner
from backend.experiments import sky_supervision as sky
from backend.experiments.c_stages import C_STAGE_NAMES
from backend.experiments.resources import GIB
from backend.experiments.stages import PipelineStagePlan, StageSpec
from backend.experiments.supervision import SupervisionError
from scripts import run_local_c as cli
from scripts import run_sky_c_supervised as entry
from tests.unit.test_c_profile import local_c_profile


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def case(tmp_path, monkeypatch):
    base = local_c_profile(tmp_path)
    target = Path(base["target_experiment"]["root"]); target.parent.mkdir()
    pipeline, tools, admission = (tmp_path / n for n in ("frozen", "tools", "admission"))
    for folder in (pipeline, tools, admission, Path(base["source"]["root"]),
                   tmp_path / "source-experiment", tmp_path / "source-original", tmp_path / "source-snapshot"):
        folder.mkdir()
    binding_path = admission / "binding.json"; binding_path.write_bytes(b"{}\n")
    receipt_path = admission / "preflight.json"
    binding_sha = hashlib.sha256(binding_path.read_bytes()).hexdigest()
    receipt = {"status": "mask_input_and_plan_preflight_passed", "execution_authorized": False,
        "binding_sha256": binding_sha, "source_mask_manifest_sha256": "9" * 64,
        "actual_frozen_helpers_consumed_masks": True}
    receipt_sha = write_json(receipt_path, receipt)
    profile = {"schema_version": "1.0", "profile_kind": "local_sky_c_supervised_candidate", "autorun": False,
        "c_profile": base, "mask_binding": {"path": str(binding_path), "sha256": binding_sha},
        "prepared_admission": {"path": str(receipt_path), "sha256": receipt_sha},
        "budget_lineage": {"root_profile_sha256": "1" * 64, "root_claim_sha256": "2" * 64,
            "root_charge_sha256": "3" * 64, "expected_tip_sha256": "4" * 64,
            "maximum_remaining_seconds": 7234.922}}
    profile_path = tmp_path / "sky-profile.json"; profile_sha = write_json(profile_path, profile)
    args = SimpleNamespace(profile=profile_path, profile_sha256=profile_sha, pipeline_root=pipeline,
        tool_root=tools, runtime_manifest=tmp_path / "runtime.json", runtime_manifest_sha256="7" * 64,
        source_cpu_profile=tmp_path / "cpu-profile.json", source_cpu_profile_sha256="8" * 64,
        terminal_receipt=tmp_path / "terminal.json", execute_sky_c_only=False)
    source_profile = {"target_experiment": {"root": str(tmp_path / "source-experiment")},
        "source_checkpoint": {"original_root": str(tmp_path / "source-original"),
                              "snapshot_root": str(tmp_path / "source-snapshot")}}
    source_budget = {"original_total_seconds": 14400, "consumed_sfm_seconds": 4920.218,
                     "remaining_seconds": 9479.782}
    chain = {"remaining_seconds": 7234.922, "previous_scenes": ["prior-c"],
             "cumulative_charge_seconds": 7165.078, "ledger_tip_sha256": "4" * 64}
    binding = {"manifest_sha256": "9" * 64, "images": [{"width": 8, "height": 6} for _ in range(3)]}
    python = tmp_path / "algorithm-python.exe"
    runtime = {"pipeline_root": str(pipeline), "python": str(python), "python_sha256": "a" * 64,
        "files": {"synthetic-native.exe": "b" * 64}, "_stat_signatures": {}}
    stages = []
    for name in C_STAGE_NAMES:
        helper = {"02-extract-cameras": "extract_colmap_cameras.py", "13-c-normalize": "normalize_openmvs_output.py",
                  "90-validate": "validate_outputs.py"}.get(name)
        if name == "03-prepare-openmvs-input":
            argv = tuple(str(x) for x in (python, tools / "scripts/run_sky_c_supervised.py", "--mode", "prepare-openmvs",
                "--binding", binding_path, "--binding-sha256", binding_sha, "--pipeline-root", pipeline,
                "--images", target / "work/input/images", "--sparse", target / "work/shared/colmap/refined_text",
                "--output", target / "work/shared/openmvs_input"))
        elif helper:
            argv = (str(python), str(pipeline / "scripts" / helper))
        else:
            index = {"10-c-interface": 0, "11-c-densify": 1, "12-c-reconstruct": 2, "13-c-texture": 3}[name]
            argv = (str(pipeline / cli.EXECUTABLES[index]),)
        stages.append(StageSpec(name, argv, target / ("marker-" + name), pipeline, ()))
    baseline = PipelineStagePlan(tuple(stages), {"APP_SECRET": "not-forwarded", "DATABASE_URL": "not-forwarded",
        "SMTP_PASSWORD": "not-forwarded", "HF_HUB_OFFLINE": "1"}, (8, 6))
    value = SimpleNamespace(root=tmp_path, base=base, target=target, pipeline=pipeline, tools=tools,
        admission=admission, binding=binding, binding_path=binding_path, binding_sha=binding_sha,
        profile=profile, profile_path=profile_path, receipt=receipt, receipt_path=receipt_path, args=args,
        source_budget=source_budget, source_profile=source_profile, chain=chain, runtime=runtime, baseline=baseline)
    value.tool_check = Mock(); monkeypatch.setattr(entry, "verify_tool_files", value.tool_check, raising=False)
    value.frozen = Mock(return_value=pipeline / "configs/pipeline.json")
    monkeypatch.setattr(sky, "_verify_c_frozen_files", value.frozen)
    value.sources = Mock(return_value=({}, source_budget, source_profile))
    monkeypatch.setattr(sky, "_read_c_source_bindings", value.sources)
    value.ledger = Mock(side_effect=lambda *a, **k: deepcopy(value.chain))
    monkeypatch.setattr(sky, "read_c_budget_chain", value.ledger)
    value.host = Mock(return_value={"available_ram_bytes": 12 * GIB, "data_disk_free_bytes": 28 * GIB,
                                  "evidence_disk_free_bytes": 10 * GIB})
    monkeypatch.setattr(sky, "_check_c_host_resources", value.host)
    value.runtime_check = Mock(return_value=runtime); monkeypatch.setattr(cli, "verify_runtime_manifest", value.runtime_check)
    value.assets = Mock(return_value=binding); monkeypatch.setattr(sky, "verify_sky_binding", value.assets)
    value.inventory = Mock(return_value={"files": [], "kind": "synthetic_inventory"})
    monkeypatch.setattr(sky, "read_sky_prepared_inventory", value.inventory)
    value.source = SimpleNamespace(image_names=("one.jpg", "two.jpg", "three.jpg"))
    value.handoff = SimpleNamespace(manifest={})
    value.verify_source = Mock(return_value=(value.source, value.handoff))
    monkeypatch.setattr(sky, "_verify_c_source_handoff", value.verify_source)
    value.build = Mock(return_value=baseline); monkeypatch.setattr(sky, "build_sky_c_plan", value.build)
    def forbidden(*a, **k): pytest.fail("read-only sky admission started a forbidden side effect")
    for name in ("claim_c_budget", "create_private_root", "import_sfm_handoff", "run_contained_process",
                 "require_admin", "exclusive_long_test", "ExperimentResourceMonitor", "CStageSupervisor",
                 "bounded_private_report", "write_exclusive"):
        monkeypatch.setattr(c_runner, name, forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    return value


def persist(case):
    case.args.profile_sha256 = write_json(case.profile_path, case.profile)


def admit(case, **kwargs):
    return sky.admit_sky_c(case.args, case.profile, tool_identity="f" * 64, **kwargs)


def test_admission_preserves_old_sfm_budget_and_reconciles_prior_c_without_writes(case):
    before = {p.relative_to(case.root): p.read_bytes() for p in case.root.rglob("*") if p.is_file()}
    old_budget = deepcopy(case.base["budget"])
    context = admit(case)
    assert context["c_profile"]["budget"] == old_budget
    assert context["source_budget"]["remaining_seconds"] == 9479.782
    assert context["reconciliation"]["remaining_seconds"] == 7234.922
    assert context["verified_source"] is case.source and context["verified_handoff"] is case.handoff
    assert not case.target.exists()
    assert before == {p.relative_to(case.root): p.read_bytes() for p in case.root.rglob("*") if p.is_file()}
    assert case.ledger.call_count == 2 and case.host.call_count == 2
    assert case.assets.call_args.kwargs.get("verify_source_images") is True


def test_preflight_is_a_snapshot_not_compute_authorization(case):
    result = sky.preflight_sky_c(case.args, tool_identity="f" * 64)
    assert result["status"] == "read_only_sky_c_preflight_passed"
    assert result["remaining_budget_seconds"] == 7234.922
    assert result["protection"]["available_ram_floor_bytes"] == 8 * GIB
    assert result["sfm_mask_applied"] is False and result["native_mask_execution_verified"] is False
    for key in ("execution_authorized", "reconstruction_started", "target_created", "handoff_imported",
                "budget_claimed", "budget_charged", "budget_reset_or_extended", "server_accessed",
                "gpu_accessed", "source_modified", "child_process_started", "uac_requested_by_tool"):
        assert result[key] is False


@pytest.mark.parametrize("mutate", [lambda p: p.update(extra=True), lambda p: p.update(autorun=True),
    lambda p: p["mask_binding"].update(extra=True), lambda p: p["prepared_admission"].update(path="relative.json"),
    lambda p: p["budget_lineage"].update(maximum_remaining_seconds=True),
    lambda p: p["budget_lineage"].update(maximum_remaining_seconds=float("nan")),
    lambda p: p["budget_lineage"].update(maximum_remaining_seconds=14400),
    lambda p: p["budget_lineage"].update(extra=True)])
def test_sky_profile_is_closed_and_cannot_extend_old_budget(case, mutate):
    mutate(case.profile)
    with pytest.raises(SupervisionError): sky.validate_sky_profile(case.profile)
    case.assets.assert_not_called()


def test_memory_floor_cannot_be_reduced_by_sky_profile(case):
    case.base["protection"]["available_ram_floor_bytes"] = 8 * GIB - 1
    with pytest.raises(SupervisionError, match="HEADROOM_REDUCED"):
        admit(case)
    case.host.assert_not_called()


def test_preflight_cannot_implicitly_execute(case):
    case.args.execute_sky_c_only = True
    with pytest.raises(SupervisionError, match="MODE_MISMATCH"):
        sky.preflight_sky_c(case.args, tool_identity="f" * 64)
    case.sources.assert_not_called()


def test_profile_pin_drift_blocks_all_source_or_ledger_work(case):
    case.args.profile_sha256 = "0" * 64
    with pytest.raises(cli.CLIError, match="HASH_CHANGED"):
        admit(case)
    case.ledger.assert_not_called(); case.sources.assert_not_called()


@pytest.mark.parametrize("mutation,error", [("larger", "WOULD_INCREASE"), ("exhausted", "EXHAUSTED"),
                                           ("same-scene", "SCENE_ALREADY_CHARGED")])
def test_reconciliation_never_reclaims_a_previous_c_attempt(case, mutation, error):
    if mutation == "larger": case.chain["remaining_seconds"] = 8000
    elif mutation == "exhausted": case.chain["remaining_seconds"] = 0
    else: case.chain["previous_scenes"] = [case.base["target_experiment"]["scene"]]
    with pytest.raises(SupervisionError, match=error): admit(case)
    case.host.assert_not_called(); case.assets.assert_not_called()


def test_private_ledger_access_denied_is_not_admission_success_or_new_claim(case):
    case.ledger.side_effect = PermissionError("private synthetic path")
    with pytest.raises(PermissionError): admit(case)
    case.host.assert_not_called(); assert not case.target.exists()


def test_ledger_is_rechecked_after_all_source_reads(case):
    changed = {**case.chain, "remaining_seconds": 7234.0}
    case.ledger.side_effect = [deepcopy(case.chain), changed]
    with pytest.raises(SupervisionError, match="CHAIN_CHANGED"):
        admit(case)
    assert case.ledger.call_count == 2 and not case.target.exists()


@pytest.mark.parametrize("mutation", ["legacy-status", "authorized", "binding", "manifest", "consumption"])
def test_prepared_receipt_must_be_actual_mask_input_admission(case, mutation):
    if mutation == "legacy-status": case.receipt["status"] = "prepared_mask_inputs_read_only_recheck_passed"
    elif mutation == "authorized": case.receipt["execution_authorized"] = True
    elif mutation == "binding": case.receipt["binding_sha256"] = "0" * 64
    elif mutation == "manifest": case.receipt["source_mask_manifest_sha256"] = "0" * 64
    else: case.receipt["actual_frozen_helpers_consumed_masks"] = False
    case.profile["prepared_admission"]["sha256"] = write_json(case.receipt_path, case.receipt); persist(case)
    with pytest.raises(SupervisionError, match="PREPARED_RECEIPT_REJECTED"):
        admit(case)
    case.inventory.assert_not_called(); assert not case.target.exists()


def test_existing_target_does_not_reuse_old_stage_markers(case):
    case.target.mkdir()
    with pytest.raises(SupervisionError, match="NEW_TARGET_REQUIRED"):
        admit(case)
    case.sources.assert_not_called()


def test_target_created_during_admission_is_rejected_without_removing_it(case):
    def create_target(*a, **k):
        case.target.mkdir()
        return case.source, case.handoff
    case.verify_source.side_effect = create_target
    with pytest.raises(SupervisionError, match="NEW_TARGET_REQUIRED"):
        admit(case)
    assert case.target.exists()


def test_source_dimension_disagreement_blocks_before_plan(case):
    case.binding["images"][1]["width"] = 9
    with pytest.raises(SupervisionError, match="DIMENSIONS_CHANGED"):
        admit(case)
    case.build.assert_not_called()


def test_caller_guard_is_additive_and_deadline_is_finite(case, monkeypatch):
    guard = Mock(side_effect=SupervisionError("SKY_TEST_GUARD_FAILED"))
    with pytest.raises(SupervisionError, match="GUARD_FAILED"):
        admit(case, health_check=guard)
    case.assets.assert_not_called()
    monkeypatch.setattr(sky.time, "monotonic", Mock(side_effect=[0.0, 121.0]))
    with pytest.raises(SupervisionError, match="READ_DEADLINE"):
        admit(case)


def test_only_one_adapter_gets_isolation_flags_and_all_other_stages_unchanged(case):
    context = admit(case); plan = context["plan"]
    assert tuple(s.name for s in plan.stages) == C_STAGE_NAMES
    assert plan.stages[1].argv[1:3] == ("-I", "-B")
    assert plan.stages[1].argv[-2:] == ("--tool-manifest-sha256", "f" * 64)
    assert all(before is after for i, (before, after) in enumerate(zip(case.baseline.stages, plan.stages)) if i != 1)
    assert case.baseline.environment["APP_SECRET"] == "not-forwarded"
    assert not any(k.startswith(("APP_", "DATABASE_", "SMTP_")) for k in plan.environment)
    assert plan.environment["CUDA_VISIBLE_DEVICES"] == "-1"
    assert all(plan.environment[k] == "8" for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"))


@pytest.mark.parametrize("mutation", ["script", "binding", "toolpin", "no-isolation", "wrong-stage", "reorder", "extra"])
def test_adapter_exception_cannot_be_reused_for_arbitrary_runtime_or_argv(case, mutation):
    context = admit(case); plan = context["plan"]; stages = list(plan.stages)
    argv = list(stages[1].argv)
    if mutation == "script": argv[3] = str(case.tools / "evil.py")
    elif mutation == "binding": argv[argv.index("--binding") + 1] = str(case.original if hasattr(case, "original") else case.root / "foreign.json")
    elif mutation == "toolpin": argv[-1] = "0" * 64
    elif mutation == "no-isolation": argv.remove("-I")
    elif mutation == "extra": argv.extend(("--extra", "argument"))
    elif mutation == "wrong-stage": stages[0] = replace(stages[0], argv=tuple(argv))
    else: stages[0], stages[1] = stages[1], stages[0]
    if mutation not in {"wrong-stage", "reorder"}: stages[1] = replace(stages[1], argv=tuple(argv))
    with pytest.raises((SupervisionError, cli.CLIError)):
        sky.verify_sky_plan_runtime(replace(plan, stages=tuple(stages)), context)


def test_original_seven_stage_python_and_native_whitelists_remain_closed(case):
    context = admit(case); plan = context["plan"]; stages = list(plan.stages)
    stages[0] = replace(stages[0], argv=(str(case.runtime["python"]), str(case.pipeline / "scripts/not-frozen.py")))
    with pytest.raises(cli.CLIError, match="PLAN_RUNTIME_CHANGED"):
        sky.verify_sky_plan_runtime(replace(plan, stages=tuple(stages)), context)
    stages = list(plan.stages); stages[2] = replace(stages[2], argv=(str(case.pipeline / "foreign.exe"),))
    with pytest.raises(cli.CLIError, match="PLAN_RUNTIME_CHANGED"):
        sky.verify_sky_plan_runtime(replace(plan, stages=tuple(stages)), context)


def test_runtime_stage_rechecks_sealed_tools_then_original_runtime(case, monkeypatch):
    context = admit(case); checks = Mock(); monkeypatch.setattr(cli, "verify_runtime_stage", checks)
    case.tool_check.reset_mock()
    sky.verify_sky_runtime_stage(context["plan"].stages[2], context)
    case.tool_check.assert_called_once_with(case.tools, "f" * 64)
    checks.assert_called_once_with(context["plan"].stages[2], case.runtime)


def test_adapter_stage_rechecks_interpreter_and_frozen_helper_bytes(case, monkeypatch):
    context = admit(case); stage = context["plan"].stages[1]
    monkeypatch.setattr(cli, "runtime_files", lambda _: (list(case.runtime["files"]), Path(case.runtime["python"])))
    helper_checks = []
    def digest(path):
        helper_checks.append(Path(path))
        if Path(path) == Path(case.runtime["python"]): return case.runtime["python_sha256"]
        return sky.FROZEN_SHA[Path(path).relative_to(case.pipeline).as_posix()]
    monkeypatch.setattr(cli, "sha", digest)
    sky.verify_sky_runtime_stage(stage, context)
    assert len(helper_checks) == 3
    monkeypatch.setattr(cli, "sha", lambda _: "0" * 64)
    with pytest.raises(SupervisionError, match="RUNTIME_HASH_CHANGED"):
        sky.verify_sky_runtime_stage(stage, context)


def test_adapter_stage_rejects_changed_native_dll_set_before_hashing_or_launch(case, monkeypatch):
    context = admit(case); stage = context["plan"].stages[1]
    monkeypatch.setattr(cli, "runtime_files", lambda _: (
        [*case.runtime["files"], "vendor/added.dll"], Path(case.runtime["python"])))
    hash_check = Mock(side_effect=AssertionError("changed runtime set must fail before file hashes"))
    monkeypatch.setattr(cli, "sha", hash_check)
    with pytest.raises(SupervisionError, match="RUNTIME_CHANGED_AFTER_ADMISSION"):
        sky.verify_sky_runtime_stage(stage, context)
    hash_check.assert_not_called()


def test_adapter_stage_rejects_changed_configured_interpreter_before_hashing_or_launch(case, monkeypatch):
    context = admit(case); stage = context["plan"].stages[1]
    monkeypatch.setattr(cli, "runtime_files", lambda _: (
        list(case.runtime["files"]), case.root / "replacement-python.exe"))
    hash_check = Mock(side_effect=AssertionError("changed interpreter must fail before file hashes"))
    monkeypatch.setattr(cli, "sha", hash_check)
    with pytest.raises(SupervisionError, match="RUNTIME_CHANGED_AFTER_ADMISSION"):
        sky.verify_sky_runtime_stage(stage, context)
    hash_check.assert_not_called()


def test_final_source_proof_rechecks_binding_without_rehashing_source_rgb(case):
    context = admit(case); case.assets.reset_mock(); case.verify_source.reset_mock()
    result = sky.verify_sky_sources(context, lambda: None)
    assert result == (case.source, case.handoff)
    assert case.assets.call_args.kwargs["verify_source_images"] is False
    assert case.assets.call_args.kwargs["new_target"] is True
    case.target.mkdir()
    sky.verify_sky_sources(context, lambda: None)
    assert case.assets.call_args.kwargs["new_target"] is False
