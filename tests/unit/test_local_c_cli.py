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


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run_local_c.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("test_local_c_entry", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def result(capsys):
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def sealed(tmp_path):
    cli = load_cli()
    root = tmp_path / "sealed"
    for name in cli.CODE_FILES:
        leaf = root / name
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("# synthetic pinned source; never native work\n", encoding="utf-8")
    shutil.copyfile(SCRIPT, root / "scripts/run_local_c.py")
    shutil.copyfile(ROOT / "scripts/run_hybrid_experiment.py", root / "scripts/run_hybrid_experiment.py")
    shutil.copyfile(ROOT / "backend/experiments/c_integrity.py", root / "backend/experiments/c_integrity.py")
    (root / "backend/experiments/c_runner.py").write_text(
        "def execute_local_c(*args, **kwargs):\n"
        "    raise AssertionError('synthetic fixture must not execute')\n", encoding="utf-8")
    pin = cli.seal_tools(root)
    return SimpleNamespace(cli=cli, root=root, pin=pin, tmp=tmp_path)


@pytest.fixture
def runtime(tmp_path):
    cli = load_cli()
    root = tmp_path / "pipeline"
    python = tmp_path / "algorithm-python.exe"
    python.write_bytes(b"synthetic interpreter, not executable\n")
    for name in (*cli.EXECUTABLES, *("scripts/" + name for name in cli.SCRIPTS),
                 cli.VC17 + "/MVS.dll", cli.VC17 + "/Common.dll",
                 cli.VC18 + "/MVS.dll", cli.VC18 + "/Common.dll"):
        leaf = root / name
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(b"synthetic native dependency, never executable\n")
    write(root / "configs/paths.local.json", {"mapanything_python": str(python)})
    manifest = tmp_path / "runtime-manifest.json"
    pin = cli.seal_runtime(root, manifest)
    return SimpleNamespace(cli=cli, root=root, python=python, manifest=manifest, pin=pin, tmp=tmp_path)


def invoke(f, mode, *extra):
    completed = subprocess.run(
        [sys.executable, "-B", str(f.root / "scripts/run_local_c.py"), "--mode", mode,
         "--tool-manifest-sha256", f.pin, *extra],
        cwd=f.tmp, capture_output=True, text=True, timeout=30,
    )
    assert completed.stdout.strip(), completed.stderr
    return completed.returncode, json.loads(completed.stdout), completed.stderr


def repin(f):
    path = f.root / f.cli.TOOL_MANIFEST
    value = json.loads(path.read_text(encoding="utf-8"))
    value["files"] = {name: digest(f.root / name) for name in f.cli.CODE_FILES}
    write(path, value)
    f.pin = digest(path)


def test_default_has_no_backend_import_probe_write_or_process(monkeypatch, capsys):
    cli = load_cli()
    before = {name: value for name, value in sys.modules.items()
              if name == "backend" or name.startswith("backend.")}
    def forbidden(*_args, **_kwargs):
        pytest.fail("default prepare must not inspect, import, write or launch")
    for name in ("plain", "read", "sha", "load_backend", "runtime_files", "seal_runtime", "seal_tools"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert cli.main([]) == 0
    assert result(capsys) == {"status": "prepared_not_executed", "reconstruction_started": False,
                              "profile_bound_and_admitted": False}
    assert before == {name: value for name, value in sys.modules.items()
                      if name == "backend" or name.startswith("backend.")}


@pytest.mark.parametrize("argv", [["--execute"], ["--execute-c"], ["--execute-c-only=true"],
    ["--mode", "cpu"], ["--execute-c-only"], ["--mode", "inspect", "--execute-c-only"]])
def test_wrong_or_mixed_execution_switches_block_before_backend(argv, monkeypatch, capsys):
    cli = load_cli()
    calls = []
    monkeypatch.setattr(cli, "load_backend", lambda *_: calls.append("unexpected_backend_import"))
    assert cli.main(argv) == 1
    assert result(capsys)["status"] == "blocked"
    assert not calls


@pytest.mark.parametrize("argv", [
    ["--mode", "prepare", "--runtime-manifest", "ignored-runtime.json"],
    ["--mode", "prepare", "--source-cpu-profile", "ignored-source.json"],
    ["--mode", "prepare", "--terminal-receipt", "ignored-terminal.json"],
    ["--mode", "seal-tools", "--runtime-manifest", "ignored-runtime.json"],
])
def test_action_specific_arguments_are_not_silently_ignored(argv, tmp_path, capsys):
    cli = load_cli()
    # A failed seal attempt must not accidentally look like a successful action
    # mismatch rejection: use a complete synthetic root for seal-tools below.
    if "seal-tools" in argv:
        for name in cli.CODE_FILES:
            leaf = tmp_path / name
            leaf.parent.mkdir(parents=True, exist_ok=True)
            leaf.write_text("# fixture\n", encoding="utf-8")
    assert cli.main(argv, tool_root=tmp_path) == 1
    assert result(capsys)["status"] == "blocked"
    assert not (tmp_path / cli.TOOL_MANIFEST).exists()


def test_inspect_checks_exact_native_and_script_leaves_without_launch(sealed, runtime):
    code, report, error = invoke(sealed, "inspect", "--pipeline-root", str(runtime.root))
    assert code == 0, error
    assert report["execution_ready"] is False
    expected = set(sealed.cli.EXECUTABLES) | {"scripts/" + name for name in sealed.cli.SCRIPTS} | {"mapanything_python"}
    assert {row["name"] for row in report["observations"]} == expected
    assert all(row["status"] == "ok" and row["hash_status"] == "not_requested"
               for row in report["observations"])
    assert not report["data_modified"] and not report["runtime_import_performed"]
    assert not list(sealed.tmp.glob("_experiments"))


def test_inspect_missing_leaf_is_reported_not_executed(sealed, runtime):
    missing = runtime.root / runtime.cli.EXECUTABLES[0]
    missing.unlink()  # Only a synthetic temporary fixture.
    code, report, error = invoke(sealed, "inspect", "--pipeline-root", str(runtime.root))
    assert code == 1, error
    assert report["status"] == "unverified_or_blocked" and not report["execution_ready"]
    assert next(row for row in report["observations"] if row["name"] == runtime.cli.EXECUTABLES[0])["status"] == "missing"


def test_seal_tools_binds_exact_closure_and_never_overwrites(sealed):
    f = sealed
    manifest = json.loads((f.root / f.cli.TOOL_MANIFEST).read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "kind", "autorun", "source_state", "files"}
    assert manifest["kind"] == "local_c_only_tools" and manifest["autorun"] is False
    assert manifest["source_state"] == "uncommitted_candidate"
    expected = {"backend/experiments/" + name + ".py" for name in (
        "__init__", "checkpoint", "hybrid_checkpoint", "resources", "process", "preflight",
        "runner", "high_profile", "stages", "snapshot_store", "evidence", "supervision",
        "long_runner", "cpu_runner", "hybrid_handoff", "c_profile", "c_stages", "c_integrity", "c_runner")}
    expected |= {"backend/monitoring/resources.py", "backend/re3d_adapter/errors.py",
                 "scripts/run_local_c.py", "scripts/run_hybrid_experiment.py"}
    assert set(manifest["files"]) == f.cli.CODE_FILES == expected
    assert all(digest(f.root / name) == pin for name, pin in manifest["files"].items())
    with pytest.raises(FileExistsError):
        f.cli.seal_tools(f.root)
    assert digest(f.root / f.cli.TOOL_MANIFEST) == f.pin
    assert not (f.root / "hybrid-tool-manifest.json").exists()


def test_committed_manifest_loads_without_execution(sealed, runtime):
    f = sealed
    manifest = json.loads((f.root / f.cli.TOOL_MANIFEST).read_bytes())
    manifest.update(source_state='git_commit', source_commit='a' * 40)
    write(f.root / f.cli.TOOL_MANIFEST, manifest)
    f.pin = digest(f.root / f.cli.TOOL_MANIFEST)
    code, report, _ = invoke(f, 'inspect', '--pipeline-root', str(runtime.root))
    assert code == 0 and report['execution_ready'] is False


@pytest.mark.parametrize('commit', [None, '', 'a' * 39, 'G' * 40, 'a' * 64])
def test_committed_manifest_requires_full_git_identity(sealed, runtime, commit):
    f = sealed
    manifest = json.loads((f.root / f.cli.TOOL_MANIFEST).read_bytes())
    manifest.update(source_state='git_commit', source_commit=commit)
    write(f.root / f.cli.TOOL_MANIFEST, manifest)
    f.pin = digest(f.root / f.cli.TOOL_MANIFEST)
    code, report, _ = invoke(f, 'inspect', '--pipeline-root', str(runtime.root))
    assert code == 1 and report['error_code'] == 'LOCAL_C_TOOL_MANIFEST_INVALID'


@pytest.mark.parametrize("mutation", ["manifest_pin", "missing", "extra", "autorun", "kind", "source_state", "source_hash"])
def test_loader_requires_exact_manifest_and_source_pins_before_import(sealed, mutation):
    f = sealed
    path = f.root / f.cli.TOOL_MANIFEST
    value = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "manifest_pin":
        f.pin = "0" * 64
    elif mutation == "missing":
        del value["files"]["backend/experiments/c_runner.py"]
    elif mutation == "extra":
        value["files"]["../outside.py"] = "a" * 64
    elif mutation == "autorun":
        value["autorun"] = True
    elif mutation == "kind":
        value["kind"] = "other"
    elif mutation == "source_state":
        value["source_state"] = "committed_without_evidence"
    elif mutation == "source_hash":
        (f.root / "backend/experiments/c_runner.py").write_text("raise AssertionError('must not import')\n")
    if mutation not in {"manifest_pin", "source_hash"}:
        write(path, value)
        f.pin = digest(path)
    code, report, _ = invoke(f, "inspect", "--pipeline-root", str(f.tmp / "absent"))
    assert code == 1 and report["error_code"].startswith("LOCAL_C_TOOL_")
    assert report["reconstruction_started"] is False


def test_pinned_loader_ignores_foreign_initializers_and_cached_bytecode(sealed, runtime):
    f = sealed
    for name in ("backend/__init__.py", "backend/re3d_adapter/__init__.py",
                 "backend/monitoring/__init__.py", "scripts/__init__.py"):
        (f.root / name).write_text("raise AssertionError('unsealed initializer executed')\n", encoding="utf-8")
    cache = f.root / "backend/experiments/__pycache__"
    cache.mkdir()
    (cache / "c_runner.cpython-312.pyc").write_bytes(b"untrusted pycache must never execute")
    code, report, error = invoke(f, "inspect", "--pipeline-root", str(runtime.root))
    assert code == 0, error
    assert report["execution_ready"] is False
    assert not (f.root / "hybrid-tool-manifest.json").exists()


@pytest.mark.parametrize("module", ["backend.experiments.unsealed", "scripts.unsealed"])
def test_unsealed_backend_and_script_modules_cannot_fallback(sealed, module):
    f = sealed
    leaf = f.root / (module.replace(".", "/") + ".py")
    leaf.write_text("raise AssertionError('must not execute unsealed code')\n", encoding="utf-8")
    (f.root / "backend/experiments/c_runner.py").write_text(f"import {module}\n", encoding="utf-8")
    repin(f)
    code, report, _ = invoke(f, "inspect", "--pipeline-root", str(f.tmp / "absent"))
    assert code == 1 and report["error_code"] == "HYBRID_CLI_UNSEALED_MODULE_IMPORT_REJECTED"


def test_runtime_manifest_binds_four_programs_four_scripts_and_all_dlls(runtime):
    f = runtime
    value = f.cli.verify_runtime_manifest(f.manifest, f.pin, f.root)
    assert value["cpu_build_static_check_only"] is True
    assert value["python"] == str(f.python)
    assert value["python_sha256"] == digest(f.python)
    assert len(f.cli.EXECUTABLES) == 4
    assert {Path(name).name for name in f.cli.EXECUTABLES} == {
        "InterfaceCOLMAP.exe", "DensifyPointCloud.exe", "ReconstructMesh.exe", "TextureMesh.exe"}
    assert set(f.cli.SCRIPTS) == {"extract_colmap_cameras.py", "prepare_openmvs_texture_input.py",
                                 "normalize_openmvs_output.py", "validate_outputs.py"}
    expected = set(f.cli.EXECUTABLES) | {"scripts/" + name for name in f.cli.SCRIPTS} | {
        folder + "/" + name for folder in (f.cli.VC17, f.cli.VC18) for name in ("MVS.dll", "Common.dll")}
    assert set(value["files"]) == expected


@pytest.mark.parametrize("mutation,code", [
    ("manifest_pin", "LOCAL_C_RUNTIME_MANIFEST_CHANGED"),
    ("omit_dll", "LOCAL_C_RUNTIME_IDENTITY_CHANGED"),
    ("remove_dll", "LOCAL_C_RUNTIME_IDENTITY_CHANGED"),
    ("extra_dll", "LOCAL_C_RUNTIME_IDENTITY_CHANGED"),
    ("change_exe", "LOCAL_C_RUNTIME_HASH_CHANGED"),
    ("change_python", "LOCAL_C_RUNTIME_IDENTITY_CHANGED"),
])
def test_runtime_missing_dll_or_replaced_binary_blocks(runtime, mutation, code):
    f = runtime
    if mutation == "manifest_pin":
        f.pin = "0" * 64
    elif mutation == "omit_dll":
        value = json.loads(f.manifest.read_text(encoding="utf-8"))
        del value["files"][f.cli.VC17 + "/MVS.dll"]
        write(f.manifest, value)
        f.pin = digest(f.manifest)
    elif mutation == "remove_dll":
        (f.root / f.cli.VC17 / "MVS.dll").unlink()
    elif mutation == "extra_dll":
        (f.root / f.cli.VC18 / "Unexpected.dll").write_bytes(b"new dependency")
    elif mutation == "change_exe":
        (f.root / f.cli.EXECUTABLES[0]).write_bytes(b"replacement exe")
    else:
        f.python.write_bytes(b"replacement interpreter")
    with pytest.raises(f.cli.CLIError, match=code):
        f.cli.verify_runtime_manifest(f.manifest, f.pin, f.root)


@pytest.mark.parametrize("name,encoding", [("DensifyPointCloud.exe", "ascii"), ("TextureMesh.exe", "utf-16-le")])
def test_runtime_static_cuda_marker_is_rejected_without_running_binary(runtime, name, encoding):
    f = runtime
    relative = next(item for item in f.cli.EXECUTABLES if item.endswith("/" + name))
    (f.root / relative).write_bytes("cuda-device".encode(encoding))
    with pytest.raises(f.cli.CLIError, match="LOCAL_C_NONCPU_BINARY_REJECTED"):
        f.cli.seal_runtime(f.root, f.tmp / "rejected-runtime.json")
    assert not (f.tmp / "rejected-runtime.json").exists()


def test_runtime_plan_accepts_only_four_programs_and_four_python_scripts(runtime):
    f = runtime
    stages = [SimpleNamespace(argv=(str(f.root / name), "--synthetic")) for name in f.cli.EXECUTABLES]
    stages += [SimpleNamespace(argv=(str(f.python), str(f.root / "scripts" / name))) for name in f.cli.SCRIPTS]
    admitted = f.cli.verify_runtime_manifest(f.manifest, f.pin, f.root)
    f.cli.verify_plan_runtime(SimpleNamespace(stages=stages), admitted)


@pytest.mark.parametrize("argv_kind", ["other_program", "other_python", "other_script", "module", "traversal_script"])
def test_runtime_plan_rejects_unbound_program_or_script(runtime, argv_kind):
    f = runtime
    argv = {
        "other_program": [str(f.root / "vendor/Other.exe")],
        "other_python": [str(f.tmp / "unbound-python.exe"), str(f.root / "scripts" / f.cli.SCRIPTS[0])],
        "other_script": [str(f.python), str(f.root / "scripts/unbound.py")],
        "module": [str(f.python), "-m", "unbound"],
        "traversal_script": [str(f.python), str(f.root / "scripts/../scripts" / f.cli.SCRIPTS[0])],
    }[argv_kind]
    admitted = f.cli.verify_runtime_manifest(f.manifest, f.pin, f.root)
    with pytest.raises(f.cli.CLIError, match="LOCAL_C_PLAN_RUNTIME_CHANGED"):
        f.cli.verify_plan_runtime(SimpleNamespace(stages=[SimpleNamespace(argv=argv)]), admitted)


def test_runtime_stage_rechecks_admitted_closure_without_running_any_program(runtime):
    f = runtime
    admitted = f.cli.verify_runtime_manifest(f.manifest, f.pin, f.root)
    native = SimpleNamespace(argv=(str(f.root / f.cli.EXECUTABLES[0]), "--synthetic"))
    script = SimpleNamespace(argv=(str(f.python), str(f.root / "scripts" / f.cli.SCRIPTS[0])))
    f.cli.verify_runtime_stage(native, admitted)
    f.cli.verify_runtime_stage(script, admitted)
    (f.root / f.cli.VC18 / "MVS.dll").write_bytes(b"changed after admission")
    with pytest.raises(f.cli.CLIError, match="LOCAL_C_RUNTIME_CHANGED_AFTER_ADMISSION"):
        f.cli.verify_runtime_stage(native, admitted)


@pytest.mark.parametrize('code', [
    'C_OUTPUT_FACE_COUNT_MISMATCH', 'C_OUTPUT_UV_NONFINITE', 'C_STAGE_GATE_FAILED',
    'C_OUTPUT_VERTEX_COUNT_INVALID',
    'C_ONLY_BUDGET_ALREADY_CLAIMED', 'EXPERIMENT_PROCESS_IO_FAILED',
    'LOCAL_C_ENTRY_FAILED', 'LOW_RAM_AUTO_STOPPED', 'RESOURCE_THREAD_SHUTDOWN_FAILED',
    'EVIDENCE_TERMINAL_TIMED_OUT', 'LONG_BUDGET_EXHAUSTED',
    'HYBRID_CLI_UNSEALED_MODULE_IMPORT_REJECTED', 'HANDOFF_FINAL_CPU_PROFILE_MISMATCH',
    'C_WRAPPER_PUBLIC_WRITE_TIMEOUT', 'HOST_RESOURCE_PROBE_FAILED',
])
def test_public_error_mapping_preserves_structured_namespace_codes(code):
    cli = load_cli()
    assert cli.stable_public_error_code(code) == code
    assert cli.stable_public_error_code(SimpleNamespace(code=code)) == code


@pytest.mark.parametrize('value', [
    None, False, 42, {}, RuntimeError('C_OUTPUT_FACE_COUNT_MISMATCH'),
    'C_OUTPUT_FAILED password=not-a-real-secret', 'C_OUTPUT_FAILED\nTraceback',
    'C_OUTPUT_FAILED D:\\private\\output.obj', 'C_OUTPUT_FAILED/token',
    'C_OUTPUT_' + 'A' * 96, 'C_OUTPUT_failed', 'C_OUTPUT_FAILED\n',
    'UNRECOGNIZED_CODE', 'LOCAL_C_',
])
def test_public_error_mapping_never_uses_raw_exception_or_secret_text(value):
    cli = load_cli()
    assert cli.stable_public_error_code(value) == 'LOCAL_C_ENTRY_FAILED'
    assert cli.stable_public_error_code(SimpleNamespace(code=value)) == 'LOCAL_C_ENTRY_FAILED'


def test_public_error_mapping_fallback_is_also_sanitized():
    cli = load_cli()
    assert cli.stable_public_error_code(None, fallback='C_WRAPPER_FAILED') == 'C_WRAPPER_FAILED'
    assert cli.stable_public_error_code(None, fallback='password=private') == 'LOCAL_C_ENTRY_FAILED'


@pytest.mark.parametrize('code,expected', [
    ('C_OUTPUT_FACE_COUNT_MISMATCH', 'C_OUTPUT_FACE_COUNT_MISMATCH'),
    ('C_STAGE_GATE_FAILED', 'C_STAGE_GATE_FAILED'),
    ('EXPERIMENT_PROCESS_IO_FAILED', 'EXPERIMENT_PROCESS_IO_FAILED'),
    ('C_OUTPUT_FAILED password=not-a-real-secret', 'LOCAL_C_ENTRY_FAILED'),
])
def test_cli_exception_uses_same_public_error_mapping(monkeypatch, capsys, code, expected):
    cli = load_cli()
    def failing_seal(root):
        raise SimpleStructuredFailure(code)
    monkeypatch.setattr(cli, 'seal_tools', failing_seal)
    assert cli.main(['--mode', 'seal-tools']) == 1
    observed = result(capsys)
    assert observed['error_code'] == expected
    assert 'password=' not in json.dumps(observed)


class SimpleStructuredFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('raw exception text must not be used')


@pytest.mark.parametrize('mode', ['preflight', 'execute'])
@pytest.mark.parametrize('code,expected', [
    ('C_OUTPUT_FACE_COUNT_MISMATCH', 'C_OUTPUT_FACE_COUNT_MISMATCH'),
    ('C_OUTPUT_FAILED token=not-a-real-secret', 'LOCAL_C_ENTRY_FAILED'),
])
def test_cli_returned_result_uses_same_error_mapping(monkeypatch, capsys, mode, code, expected):
    cli = load_cli()
    terminal = {'status': 'failed', 'error_code': code}
    runner = SimpleNamespace(preflight_local_c=lambda *a, **k: dict(terminal),
                             execute_local_c=lambda *a, **k: dict(terminal))
    monkeypatch.setattr(cli, 'load_backend', lambda *a: runner)
    monkeypatch.setattr(cli, 'read', lambda *a, **k: {})
    monkeypatch.setattr(cli, 'sha', lambda *a: 'a' * 64)
    argv = ['--mode', mode, '--profile', 'synthetic-profile', '--profile-sha256', 'a' * 64,
            '--tool-manifest-sha256', 'b' * 64, '--source-cpu-profile', 'synthetic-cpu-profile',
            '--source-cpu-profile-sha256', 'c' * 64, '--terminal-receipt', 'synthetic-receipt',
            '--runtime-manifest', 'synthetic-runtime', '--runtime-manifest-sha256', 'd' * 64]
    if mode == 'execute':
        argv += ['--execute-c-only']
    assert cli.main(argv) == 1
    observed = result(capsys)
    assert observed['error_code'] == expected
    assert 'token=' not in json.dumps(observed)
