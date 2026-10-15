"""Synthetic end-to-end local C wiring: no elevation, hardware or native work."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import backend.experiments.c_runner as runner
import scripts.run_local_c as cli
from backend.experiments.c_stages import C_STAGE_NAMES
from backend.experiments.process import ContainedProcessOutcome
from backend.experiments.resources import GIB
from backend.experiments.stages import PipelineStagePlan, StageSpec
from backend.experiments.supervision import SupervisionError
from tests.unit.test_c_profile import local_c_profile, terminal_receipt
from tests.unit.test_cpu_runner import local_profile


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class FakeMonitor:
    def __init__(self, **kwargs):
        self.options, self.closed, self.started = kwargs, False, False
    def start(self): self.started = True
    def check_health(self): assert self.started
    def set_phase(self, phase): self.phase = phase
    def finish(self): self.check_health(); self.close()
    def close(self): self.closed = True
    def summary(self): return {"trip": None, "latest": {}, "peaks": {}, "closed": self.closed}


class FakeEvidence:
    def __init__(self): self.rows, self.started, self.closed = [], False, False
    def start(self): self.started = True
    def check_health(self): assert self.started
    def append(self, kind, value):
        assert kind in {"event", "resources", "pairs"}
        self.rows.append((kind, value))
    def finish_terminal(self, value): self.rows.append(("terminal", value))
    def write_forensic_summary(self, value): self.rows.append(("forensic", value))
    def close(self): self.closed = True


@pytest.fixture
def wiring(tmp_path_factory, monkeypatch):
    # Default pytest node basenames plus the immutable 64-hex budget filenames
    # exceeded Windows' legacy 260-character boundary under a long D test root.
    # Reserve one short, exclusive root; never change production path handling.
    tmp_path = tmp_path_factory.mktemp("cw")
    profile = local_c_profile(tmp_path)
    cpu_profile = local_profile(tmp_path)
    cpu_profile_path = tmp_path / "accepted-cpu-profile.json"
    cpu_profile_sha = write_json(cpu_profile_path, cpu_profile)
    terminal = terminal_receipt()
    terminal.update(scene=cpu_profile["target_experiment"]["scene"], source_job_id=cpu_profile["source_job_id"],
                    result={"final_sfm_gate": {"database_images": 300, "registered_images": 300}})
    terminal["tool_identity"]["profile_sha256"] = cpu_profile_sha
    terminal["hashes"]["final_database_sha256"] = "d" * 64
    terminal_path = tmp_path / "accepted-terminal.json"
    terminal_sha = write_json(terminal_path, terminal)
    profile["source"]["terminal_receipt_sha256"] = terminal_sha
    profile["budget"]["source_terminal_receipt_sha256"] = terminal_sha
    profile_path = tmp_path / "c-profile.json"
    profile_sha = write_json(profile_path, profile)
    target = Path(profile["target_experiment"]["root"])
    target.parent.mkdir()
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    private_parent = tmp_path / "synthetic-private-evidence"
    private_parent.mkdir()
    claim_path = private_parent / ("c-budget-" + "a" * 64 + ".json")
    charge_path = claim_path.with_name(claim_path.stem + "-charge.json")
    assert max(len(str(claim_path)), len(str(charge_path))) < 260, "synthetic C budget path exceeds Windows test limit"
    runtime = {"python": str(tmp_path / "synthetic-python.exe")}
    stages = tuple(StageSpec(name, (str(tmp_path / "synthetic.exe"), name, "--max-threads", "8"),
                            target / ("marker-" + name), pipeline) for name in C_STAGE_NAMES)
    plan = PipelineStagePlan(stages, {"DATABASE_URL": "synthetic-not-a-credential", "OMP_NUM_THREADS": "24"}, (16, 16))
    source = SimpleNamespace(image_names=tuple(f"image-{index:03d}.jpg" for index in range(300)))
    handoff = SimpleNamespace(manifest={
        "files": [{"relative_path": "work/shared/colmap/database.db", "sha256": "d" * 64}],
        "sfm_gate": terminal["result"]["final_sfm_gate"],
        "execution": {"source_experiment_root": cpu_profile["target_experiment"]["root"]},
        "source": {"source_job_id": cpu_profile["source_job_id"]}})
    case = SimpleNamespace(profile=profile, target=target, plan=plan, runtime=runtime, source=source,
        handoff=handoff, private_parent=private_parent, evidence=FakeEvidence(), process_calls=[], owners=[],
        monitors=[], gates=[], probe_return_code=0, probe_stop=True, disk_free=28 * GIB,
        host_available=12 * GIB, terminal=terminal)
    case.args = SimpleNamespace(execute_c_only=True, pipeline_root=pipeline, profile=profile_path,
        terminal_receipt=terminal_path, source_cpu_profile=cpu_profile_path,
        source_cpu_profile_sha256=cpu_profile_sha, runtime_manifest=tmp_path / "runtime.json",
        runtime_manifest_sha256="e" * 64)
    case.caught_errors = []
    actual_error_code = runner.error_code
    def record_error_code(exc):
        case.caught_errors.append((type(exc).__name__, str(exc)))
        return actual_error_code(exc)
    monkeypatch.setattr(runner, "error_code", record_error_code)
    # Never touch production C evidence or execute OS privilege/native probes.
    monkeypatch.setattr(runner, "PRIVATE_PARENT", private_parent)
    case.admin = Mock()
    monkeypatch.setattr(runner, "require_admin", case.admin)
    monkeypatch.setattr(runner, "verify_git_identity", Mock())
    real_digest = runner.digest
    frozen = {"pipeline.json": runner.BASE_CONFIG_SHA, "run_pipeline.py": runner.PIPELINE_SCRIPT_SHA,
              "run_colmap_shared.py": runner.COLMAP_SCRIPT_SHA}
    monkeypatch.setattr(runner, "digest", lambda path, health=lambda: None:
                        frozen[Path(path).name] if Path(path).name in frozen else real_digest(path, health))
    case.host = Mock()
    def host_read(_):
        # Real HostResourceReader exposes total/used, not available_bytes.
        return {"cpu": {"logical_count": 32}, "memory": {"total_bytes": 34141495296,
                "used_bytes": 34141495296 - case.host_available}}, []
    case.host.read.side_effect = host_read
    monkeypatch.setattr(runner, "HostResourceReader", Mock(return_value=case.host))
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=20 * GIB if str(path).rstrip("\\/").casefold() == "c:" else case.disk_free))
    case.sleep = Mock(return_value=1)
    monkeypatch.setattr(runner.ctypes, "WinDLL", Mock(return_value=SimpleNamespace(SetThreadExecutionState=case.sleep)))
    monkeypatch.setattr(runner, "exclusive_long_test", lambda: nullcontext())
    monkeypatch.setattr(runner.EvidenceSink, "create_production", Mock(return_value=case.evidence))
    def reserve(root, **kwargs): (root / "reports").mkdir(parents=True)
    monkeypatch.setattr(runner, "create_private_root", reserve)
    def monitor(**kwargs):
        value = FakeMonitor(**kwargs); case.monitors.append(value); return value
    monkeypatch.setattr(runner, "ExperimentResourceMonitor", monitor)
    monkeypatch.setattr(runner, "verify_hybrid_source", Mock(return_value=source))
    monkeypatch.setattr(runner, "verify_sfm_handoff", Mock(return_value=handoff))
    case.importer = Mock(return_value=SimpleNamespace(input_manifest_path=target / "work/input/input-manifest.json"))
    monkeypatch.setattr(runner, "import_sfm_handoff", case.importer)
    monkeypatch.setattr(runner, "prepared_dimensions", Mock(return_value=(16, 16)))
    monkeypatch.setattr(runner, "build_c_stage_plan", Mock(return_value=plan))
    case.runtime_verify = Mock(return_value=runtime)
    monkeypatch.setattr(cli, "verify_runtime_manifest", case.runtime_verify)
    monkeypatch.setattr(cli, "verify_plan_runtime", Mock())
    case.stage_runtime = Mock()
    monkeypatch.setattr(cli, "verify_runtime_stage", case.stage_runtime)
    def contained(argv, **kwargs):
        kwargs["health_check"]()
        case.process_calls.append((argv, kwargs))
        is_probe = "-c" in argv
        if not is_probe or case.probe_stop:
            kwargs["on_tree_stopped"]()
        return ContainedProcessOutcome(case.probe_return_code if is_probe else 0, .001, 7,
                                       case.probe_stop if is_probe else True)
    monkeypatch.setattr(runner, "run_contained_process", contained)
    actual_supervisor = runner.CStageSupervisor
    def supervisor(**kwargs):
        def gate(stage, root, names, health_check):
            health_check(); case.gates.append(stage.name)
            assert names == source.image_names and root == target
            return {"all_passed": True, "geometry_accuracy_certified": False}
        value = actual_supervisor(**kwargs, gate=gate, sample_seconds=.01)
        case.owners.append(value)
        return value
    monkeypatch.setattr(runner, "CStageSupervisor", supervisor)
    # Poison unrelated access so a wiring regression cannot call real GPU/queue/OS work.
    monkeypatch.setattr("backend.monitoring.resources.NvidiaSmiReader", Mock(side_effect=AssertionError("GPU probe forbidden")))
    monkeypatch.setattr("backend.experiments.preflight.read_queue_state", Mock(side_effect=AssertionError("queue access forbidden")))
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=AssertionError("native process forbidden")))
    case.profile_sha = profile_sha
    return case


def test_complete_execution_wires_actual_supervisor_without_gpu_queue_or_server(wiring):
    c = wiring
    result = runner.execute_local_c(c.args, c.profile, tool_identity="f" * 64)
    assert result["status"] == "succeeded", (result, c.caught_errors)
    assert result["process_tree_stopped"] and result["observation_stopped"]
    assert result["terminal_evidence_confirmed"] and result["source_handoff_unchanged"]
    assert not result["server_accessed"] and not result["gpu_stage_started"] and not result["platform_job_resume"]
    assert len(c.process_calls) == 9 and c.gates == list(C_STAGE_NAMES)
    assert len(c.owners) == 1 and c.stage_runtime.call_count == 8
    assert c.evidence.closed and c.monitors[0].closed
    limits = c.monitors[0].options["limits"]
    assert limits.minimum_available_ram_bytes == 8 * GIB
    assert limits.minimum_disk_free_bytes == 20 * GIB and limits.gpu_required is False
    assert (limits.guard_interval_seconds, limits.resource_interval_seconds, limits.directory_interval_seconds) == (1, 5, 15)
    assert c.importer.call_args.kwargs["target_work_root"] == c.target / "work"
    assert c.owners[0].budget.seconds <= 9479.782
    for argv, kwargs in c.process_calls:
        assert kwargs["timeout_seconds"] <= 9479.782
        assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "-1"
        assert kwargs["env"]["OMP_NUM_THREADS"] == "8" and "DATABASE_URL" not in kwargs["env"]
    assert c.sleep.call_args_list[0].args == (0x80000001,)
    assert c.sleep.call_args_list[-1].args == (0x80000000,)
    claim = c.private_parent / ("c-budget-" + "a" * 64 + ".json")
    charge = claim.with_name(claim.stem + "-charge.json")
    assert json.loads(claim.read_bytes())["automatic_retry_allowed"] is False
    recorded = json.loads(charge.read_bytes())
    assert recorded["status"] == "succeeded" and recorded["prior_sfm_charge_seconds"] > 4920
    assert recorded["cumulative_charge_seconds"] >= recorded["prior_sfm_charge_seconds"]


def test_wiring_uses_short_exclusive_root_for_both_budget_receipts(wiring):
    c = wiring
    claim = c.private_parent / ("c-budget-" + "a" * 64 + ".json")
    charge = claim.with_name(claim.stem + "-charge.json")
    assert c.private_parent.parent.name.startswith("cw")
    assert max(len(str(claim)), len(str(charge))) < 260


@pytest.mark.parametrize("confirmed", (False, True))
def test_failed_dependencies_never_create_supervisor_and_report_stop_proof(wiring, confirmed):
    c = wiring; c.probe_return_code = 1; c.probe_stop = confirmed
    result = runner.execute_local_c(c.args, c.profile, tool_identity="f" * 64)
    assert result["status"] == "failed" and result["error_code"] == "LOCAL_C_PYTHON_DEPENDENCIES_UNAVAILABLE", (result, c.caught_errors)
    assert result["process_tree_stopped"] is confirmed
    assert result["reconstruction_started"] is False and not c.owners and not c.gates
    assert len(c.process_calls) == 1 and c.evidence.closed and c.monitors[0].closed
    assert result["terminal_evidence_confirmed"] and not c.sleep.called


def test_source_profile_sha_mismatch_fails_before_host_or_claim(wiring):
    c = wiring; c.args.source_cpu_profile_sha256 = "0" * 64
    with pytest.raises(cli.CLIError, match="LOCAL_C_DOCUMENT_HASH_CHANGED"):
        runner.execute_local_c(c.args, c.profile, tool_identity="f" * 64)
    c.host.read.assert_not_called()
    assert not c.process_calls and not tuple(c.private_parent.iterdir())


@pytest.mark.parametrize("available,disk,expected", ((8 * GIB, 28 * GIB, "LOCAL_C_RAM_ADMISSION_FAILED"),
    (12 * GIB, 28 * GIB - 1, "LOCAL_C_DISK_ADMISSION_FAILED")))
def test_host_admission_preserves_eight_gib_ram_and_twenty_plus_eight_disk(wiring, available, disk, expected):
    c = wiring; c.host_available = available; c.disk_free = disk
    with pytest.raises(SupervisionError, match=expected):
        runner.execute_local_c(c.args, c.profile, tool_identity="f" * 64)
    assert not c.process_calls and not c.owners and not tuple(c.private_parent.iterdir())


@pytest.mark.parametrize("key,value", (("available_ram_floor_bytes", 8 * GIB - 1),
                                      ("disk_floor_bytes", 20 * GIB - 1)))
def test_lowered_profile_safety_line_rejected_before_admin(wiring, key, value):
    c = wiring; c.profile["protection"][key] = value
    with pytest.raises(SupervisionError, match="C_ONLY_HEADROOM_REDUCED"):
        runner.execute_local_c(c.args, c.profile, tool_identity="f" * 64)
    c.admin.assert_not_called()
    assert not c.process_calls
