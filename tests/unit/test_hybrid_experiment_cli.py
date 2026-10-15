from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/run_hybrid_experiment.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("test_hybrid_total_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def cli_fixture(tmp_path):
    cli = load_cli()
    root = tmp_path / "sealed"
    for name in cli.CODE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic source; no native execution\n", encoding="utf-8")
    shutil.copyfile(SCRIPT, root / "scripts/run_hybrid_experiment.py")
    profile = {"profile_kind": "local_cpu_sfm_candidate", "autorun": False,
        "pipeline": {"commit": cli.BASE_COMMIT, "config_sha256": cli.BASE_CONFIG_SHA,
                     "colmap_script_sha256": cli.COLMAP_SHA},
        "source_job_id": "6a90b5e9-0c99-41aa-881b-c196ebb96750",
        "source_checkpoint": {"original_root": str(tmp_path / "absent-original"),
            "original_manifest_sha256": "a" * 64, "original_database_sha256": "b" * 64,
            "snapshot_root": str(tmp_path / "absent-snapshot"), "snapshot_manifest_sha256": "c" * 64,
            "snapshot_database_sha256": "d" * 64, "expected_complete_pair_records": 26842},
        "cpu": {"sfm_threads": 12, "mapper_ba_threads": 16},
        "target_hardware": {"logical_cpus": 32, "host_ram_bytes": 34141495296},
        "runtime": {"mapanything_python": str(tmp_path / "absent-python.exe"), "python_version": "3.12.14",
                    "pycolmap_version": "3.13.0", "pycolmap_has_cuda": False},
        "protection": {"reservation_bytes": 8 * cli.GIB, "disk_floor_bytes": 20 * cli.GIB,
            "available_ram_floor_bytes": 8 * cli.GIB, "host_guard_seconds": 1, "resource_sample_seconds": 5,
            "directory_sample_seconds": 15, "snapshot_interval_seconds": 600, "snapshot_retained": 2,
            "overall_timeout_seconds": 14400},
        "target_experiment": {"scene": "cpu-new", "root": str(tmp_path / "_experiments/cpu-new")}}
    profile_path = tmp_path / "profile.json"
    write(profile_path, profile)
    manifest = {"schema_version": "1.0", "toolpack_kind": "re3d_cpu_sfm_handoff",
                "source_commit": "a" * 40, "autorun": False,
                "files": {name: digest(root / name) for name in cli.CODE_FILES}}
    write(root / "hybrid-tool-manifest.json", manifest)
    return SimpleNamespace(cli=cli, root=root, profile=profile, profile_path=profile_path,
                           manifest=manifest, manifest_sha=digest(root / "hybrid-tool-manifest.json"), tmp=tmp_path)


def args(f, mode, *extra):
    return ["--mode", mode, "--profile", str(f.profile_path), "--tool-manifest-sha256", f.manifest_sha, *extra]


def output(capsys):
    return json.loads(capsys.readouterr().out)


def test_no_arguments_prepare_never_imports_backend_or_reads_paths(monkeypatch, capsys):
    cli = load_cli()
    monkeypatch.setattr(cli, "read_document", lambda *_a, **_k: pytest.fail("no path read"))
    monkeypatch.setattr(cli, "verify_before_import", lambda *_a: pytest.fail("no tool scan"))
    monkeypatch.setattr(cli, "_load_modules", lambda *_a: pytest.fail("no backend import"))
    assert cli.main([]) == 0
    report = output(capsys)
    assert report["status"] == "prepared_no_execution" and not report["profile_supplied"]
    assert not any(report[k] for k in ("input_source_accessed", "host_accessed", "queue_accessed", "gpu_accessed", "filesystem_modified"))


def test_prepare_validates_profile_but_never_accesses_missing_input_runtime_or_target(cli_fixture, monkeypatch, capsys):
    f = cli_fixture
    monkeypatch.setattr(f.cli, "_load_modules", lambda *_a: pytest.fail("no module import"))
    monkeypatch.setattr(f.cli, "_source", lambda *_a: pytest.fail("no source read"))
    assert f.cli.main(["--profile", str(f.profile_path)]) == 0
    report = output(capsys)
    assert report["sfm_threads"] == 12 and report["mapper_threads"] == 16
    assert not (f.tmp / "_experiments").exists()


@pytest.mark.parametrize("seconds", [14400, 14284, 1])
def test_recovery_budget_can_only_reduce_the_four_hour_cap(cli_fixture, seconds):
    f = cli_fixture
    f.profile["protection"]["overall_timeout_seconds"] = seconds
    assert f.cli.validate_profile(f.profile) is f.profile


@pytest.mark.parametrize("seconds", [0, -1, 14401, 14284.0, True, "14284"])
def test_invalid_or_extended_recovery_budget_rejected(cli_fixture, seconds):
    f = cli_fixture
    f.profile["protection"]["overall_timeout_seconds"] = seconds
    with pytest.raises(f.cli.HybridCLIError, match="CPU_SFM_PROTECTION_CHANGED"):
        f.cli.validate_profile(f.profile)


@pytest.mark.parametrize("extra", [["--execute"], ["--execute-c"], ["--execute-cpu=true"], ["--execute-long-test"], ["--mode", "full"]])
def test_abbreviated_or_wrong_execution_switch_never_reaches_import(cli_fixture, extra, monkeypatch, capsys):
    f = cli_fixture
    monkeypatch.setattr(f.cli, "_load_modules", lambda *_a: pytest.fail("no backend import"))
    assert f.cli.main([*args(f, "cpu"), *extra], tool_root=f.root) == 1
    assert output(capsys)["error_code"] == "HYBRID_CLI_ARGUMENTS_INVALID"


def test_cpu_requires_exact_explicit_switch_before_code_gate(cli_fixture, monkeypatch, capsys):
    f = cli_fixture
    monkeypatch.setattr(f.cli, "verify_before_import", lambda *_a: pytest.fail("no code gate yet"))
    assert f.cli.main(args(f, "cpu"), tool_root=f.root) == 1
    assert output(capsys)["error_code"] == "CPU_SFM_EXPLICIT_EXECUTION_REQUIRED"
    assert f.cli.main(args(f, "verify-source", "--execute-cpu"), tool_root=f.root) == 1
    assert output(capsys)["error_code"] == "HYBRID_CLI_EXECUTION_MODE_MISMATCH"


@pytest.mark.parametrize("mutation", ["missing", "extra", "hash", "bad_kind", "autorun", "source_commit", "manifest_pin"])
def test_all_active_actions_hash_exact_closure_before_any_backend_import(cli_fixture, mutation, monkeypatch, capsys):
    f = cli_fixture
    if mutation == "missing":
        del f.manifest["files"]["backend/experiments/long_runner.py"]
    elif mutation == "extra":
        f.manifest["files"]["../unsafe.py"] = "a" * 64
    elif mutation == "hash":
        (f.root / "backend/experiments/cpu_runner.py").write_text("changed")
    elif mutation == "bad_kind":
        f.manifest["toolpack_kind"] = "untrusted"
    elif mutation == "autorun":
        f.manifest["autorun"] = True
    elif mutation == "source_commit":
        f.manifest["source_commit"] = "a" * 7
    write(f.root / "hybrid-tool-manifest.json", f.manifest)
    f.manifest_sha = "f" * 64 if mutation == "manifest_pin" else digest(f.root / "hybrid-tool-manifest.json")
    monkeypatch.setattr(f.cli, "_load_modules", lambda *_a: pytest.fail("not before code gate"))
    assert f.cli.main(args(f, "verify-source"), tool_root=f.root) == 1
    assert output(capsys)["error_code"].startswith("HYBRID_CLI_TOOL_")


def test_cpu_dispatch_and_stdout_are_sanitized(cli_fixture, monkeypatch, capsys):
    f = cli_fixture
    calls = []
    def execute(received, profile, root):
        calls.append((received.execute_cpu, received.tool_manifest_sha256, profile, root))
        return {"status": "succeeded", "mode": "cpu-sfm", "gpu_stage_started": False,
                "process_tree_stopped": True, "private_env": "do not print"}
    modules = SimpleNamespace(cpu_runner=SimpleNamespace(validate_cpu_profile=lambda p: p, execute_cpu_sfm=execute))
    monkeypatch.setattr(f.cli, "_load_modules", lambda root: modules)
    assert f.cli.main(args(f, "cpu", "--execute-cpu"), tool_root=f.root) == 0
    assert len(calls) == 1 and calls[0][0] is True and calls[0][1] == f.manifest_sha
    assert "private_env" not in output(capsys)


@pytest.mark.parametrize("status", ["failed", "incomplete", "timed_out", "cancelled", "unknown_status"])
def test_cpu_terminal_failure_is_never_successful_exit(cli_fixture, monkeypatch, capsys, status):
    f = cli_fixture
    modules = SimpleNamespace(cpu_runner=SimpleNamespace(validate_cpu_profile=lambda p: p,
        execute_cpu_sfm=lambda *a: {"status": status, "mode": "cpu-sfm"}))
    monkeypatch.setattr(f.cli, "_load_modules", lambda root: modules)
    assert f.cli.main(args(f, "cpu", "--execute-cpu"), tool_root=f.root) == 1
    assert output(capsys)["status"] == status


def test_wrong_action_arguments_are_not_silently_ignored(cli_fixture, monkeypatch, capsys):
    f = cli_fixture
    monkeypatch.setattr(f.cli, "_load_modules", lambda root: pytest.fail("no import for wrong action args"))
    assert f.cli.main(args(f, "verify-source", "--target-root", str(f.tmp / "ignored")), tool_root=f.root) == 1
    assert output(capsys)["error_code"] == "HYBRID_CLI_ACTION_ARGUMENT_MISMATCH"


def test_source_restore_and_handoff_import_dispatch_only_expected_operations(cli_fixture, monkeypatch, capsys):
    f = cli_fixture
    calls = []
    source = SimpleNamespace(complete_pair_records=26842)
    saved = SimpleNamespace(manifest_sha256="f" * 64, manifest={"complete_pair_records": 44850})
    def verify_source(**kwargs):
        calls.append(("verify-source", kwargs)); return source
    def restore(value, target):
        calls.append(("restore-source", value, target)); return SimpleNamespace(committed_complete_pair_records=26842)
    def verify(bundle, pin, **kwargs):
        calls.append(("verify-handoff", bundle, pin, kwargs)); return saved
    def imported(value, target, **kwargs):
        calls.append(("import-sfm", value, target, kwargs)); return SimpleNamespace(first_stage="02-extract-cameras")
    modules = SimpleNamespace(cpu_runner=SimpleNamespace(validate_cpu_profile=lambda p: p),
        checkpoint=SimpleNamespace(verify_hybrid_source=verify_source, restore_hybrid_source=restore),
        handoff=SimpleNamespace(verify_sfm_handoff=verify, import_sfm_handoff=imported))
    monkeypatch.setattr(f.cli, "_load_modules", lambda root: modules)
    assert f.cli.main(args(f, "verify-source"), tool_root=f.root) == 0
    assert output(capsys)["complete_pair_records"] == 26842
    assert calls[-1][1]["snapshot_database_sha256"] == "d" * 64
    assert f.cli.main(args(f, "restore-source", "--target-root", str(f.tmp / "_experiments/new-copy")), tool_root=f.root) == 0
    assert output(capsys)["sfm_complete"] is False
    assert f.cli.main(args(f, "import-sfm", "--bundle-root", str(f.tmp / "bundle"),
        "--handoff-manifest-sha256", "f" * 64, "--target-root", str(f.tmp / "_experiments/new-import")), tool_root=f.root) == 0
    report = output(capsys)
    assert report["first_stage"] == "02-extract-cameras" and report["downstream_started"] is False
    assert not (f.tmp / "_experiments").exists()  # Synthetic dispatch does not execute native work.


@pytest.mark.parametrize("mode,code", [("restore-source", "TARGET_REQUIRED"), ("export-sfm", "BUNDLE_REQUIRED"),
    ("import-sfm", "TARGET_REQUIRED"), ("verify-handoff", "BUNDLE_REQUIRED")])
def test_mutating_actions_require_explicit_output_and_external_pins(cli_fixture, mode, code, monkeypatch, capsys):
    f = cli_fixture
    monkeypatch.setattr(f.cli, "_load_modules", lambda *_a: pytest.fail("missing arguments before imports"))
    assert f.cli.main(args(f, mode), tool_root=f.root) == 1
    assert output(capsys)["error_code"].endswith(code)
    assert f.cli.main(args(f, "verify-handoff", "--bundle-root", str(f.tmp / "bundle")), tool_root=f.root) == 1
    assert output(capsys)["error_code"] == "HYBRID_CLI_HANDOFF_PIN_REQUIRED"


def test_fresh_sealed_subprocess_skips_foreign_initializers_and_unfixed_bytecode(cli_fixture):
    f = cli_fixture
    (f.root / "backend/experiments/cpu_runner.py").write_text("def validate_cpu_profile(p): return p\n")
    (f.root / "backend/experiments/hybrid_checkpoint.py").write_text(
        "from types import SimpleNamespace\ndef verify_hybrid_source(**kwargs): return SimpleNamespace(complete_pair_records=26842)\n")
    for name in ("backend/__init__.py", "backend/re3d_adapter/__init__.py", "backend/monitoring/__init__.py", "scripts/__init__.py"):
        (f.root / name).write_text("raise AssertionError('unsealed initializer must not execute')\n")
    # An unrelated cached module cannot substitute the pinned .py source.
    cache = f.root / "backend/experiments/__pycache__"
    cache.mkdir()
    (cache / "cpu_runner.cpython-312.pyc").write_bytes(b"untrusted bytecode")
    f.manifest["files"] = {name: digest(f.root / name) for name in f.cli.CODE_FILES}
    write(f.root / "hybrid-tool-manifest.json", f.manifest)
    f.manifest_sha = digest(f.root / "hybrid-tool-manifest.json")
    completed = subprocess.run([sys.executable, str(f.root / "scripts/run_hybrid_experiment.py"),
        *args(f, "verify-source")], capture_output=True, text=True, timeout=30, cwd=f.tmp)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["status"] == "source_verified"


def test_backend_fallback_or_unverified_namespace_is_rejected(cli_fixture):
    f = cli_fixture
    with pytest.raises(f.cli.HybridCLIError, match="NOT_HASH_VERIFIED"):
        f.cli.bootstrap_namespace(f.root)
