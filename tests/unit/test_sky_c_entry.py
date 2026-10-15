"""Sealed sky-C entry tests with synthetic code/assets; no native compute."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).absolute().parents[2]
ENTRY = ROOT / "scripts/run_sky_c_supervised.py"


def load_entry():
    spec = importlib.util.spec_from_file_location("sky_c_entry_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def sealed(tmp_path_factory):
    cli = load_entry()
    base = tmp_path_factory.mktemp("se")
    source, output = base / "source", base / "candidate"
    for name in cli.CODE_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic sealed fixture\n", encoding="utf-8")
    for name in ("scripts/run_hybrid_experiment.py", "scripts/run_local_c.py", "scripts/run_sky_c_supervised.py"):
        shutil.copyfile(ROOT / name, source / name)
    (source / "backend/experiments/sky_supervision.py").write_text(
        "from scripts.run_local_c import CODE_FILES\n"
        "assert len(CODE_FILES) == 23\n"
        "def preflight_sky_c(args, *, tool_identity):\n"
        "    return {'status':'read_only_sky_c_preflight_passed','budget_claimed':False,\n"
        "            'reconstruction_started':False, 'legacy_helper_closure':len(CODE_FILES)}\n",
        encoding="utf-8")
    (source / "backend/experiments/sky_executor.py").write_text(
        "def execute_sky_c(args, profile, *, tool_identity):\n"
        "    assert profile['synthetic_only'] is True\n"
        "    return {'status':'synthetic_execute_dispatched','reconstruction_started':False}\n",
        encoding="utf-8")
    # Data/secrets/models outside the allowlist must never enter the candidate.
    for name in (".env", "photo.jpg", "database.db", "model.glb", "private-config.json"):
        (source / name).write_bytes(b"not a real secret or asset; must not be copied")
    pin = cli.seal_tools(source, output, source_base_commit="a" * 40)
    for name in ("images", "original-images", "masks", "handoff", "old-cpu", "new-compute",
                 "sfm-handoff", "sfm-sparse"):
        (base / name).mkdir()
    write_json(base / "binding.json", {"source_root": str(base / "images"),
        "original_source_root": str(base / "original-images"),
        "mask_root": str(base / "masks"), "compute_root": str(base / "new-compute"),
        "sfm_handoff_root": str(base / "sfm-handoff"), "sfm_source_sparse": str(base / "sfm-sparse")})
    write_json(base / "profile.json", {"synthetic_only": True,
        "c_profile": {"source": {"root": str(base / "handoff")},
                      "target_experiment": {"root": str(base / "new-compute")}},
        "mask_binding": {"path": str(base / "binding.json"), "sha256": file_sha(base / "binding.json")}})
    write_json(base / "cpu.json", {"target_experiment": {"root": str(base / "old-cpu")}})
    return SimpleNamespace(cli=cli, base=base, source=source, root=output, pin=pin)


def invocation(fixture, mode="preflight", extra=()):
    args = [sys.executable, "-I", "-B", str(fixture.root / "scripts/run_sky_c_supervised.py"),
            "--mode", mode, "--tool-manifest-sha256", fixture.pin]
    if mode in {"preflight", "execute"}:
        for flag, value in (
            ("profile", fixture.base / "profile.json"), ("profile-sha256", file_sha(fixture.base / "profile.json")),
            ("source-cpu-profile", fixture.base / "cpu.json"),
            ("source-cpu-profile-sha256", file_sha(fixture.base / "cpu.json")),
            ("terminal-receipt", fixture.base / "terminal.json"),
            ("runtime-manifest", fixture.base / "runtime.json"), ("runtime-manifest-sha256", "c" * 64),
            ("pipeline-root", fixture.base / "pipeline"), ("output", fixture.base / "report.json")):
            args += ["--" + flag, str(value)]
    args += list(extra)
    completed = subprocess.run(args, cwd=fixture.base, capture_output=True, text=True, timeout=20)
    assert completed.stdout.strip(), completed.stderr
    return completed.returncode, json.loads(completed.stdout), completed.stderr


def repin(fixture):
    path = fixture.root / fixture.cli.TOOL_MANIFEST
    manifest = json.loads(path.read_bytes())
    manifest["files"] = {name: file_sha(fixture.root / name) for name in fixture.cli.CODE_FILES}
    write_json(path, manifest)
    fixture.pin = file_sha(path)


def publish_manifest(fixture, commit="f" * 40):
    path = fixture.root / fixture.cli.TOOL_MANIFEST
    manifest = json.loads(path.read_bytes())
    manifest.pop("source_base_commit")
    manifest.update(source_state="git_commit", source_commit=commit)
    write_json(path, manifest)
    fixture.pin = file_sha(path)
    return manifest


def test_default_is_no_io_import_probe_threads_or_children(monkeypatch, capsys):
    cli = load_entry()
    before = {name: value for name, value in sys.modules.items()
              if name == "backend" or name.startswith("backend.") or name.startswith("scripts.")}
    forbidden = Mock(side_effect=AssertionError("no side effects allowed"))
    for name in ("plain", "read", "sha", "load_backend", "verify_tools", "seal_tools", "PreparationGuard"):
        monkeypatch.setattr(cli, name, forbidden)
    for name in ("open", "stat", "lstat", "read_bytes", "write_bytes", "mkdir", "iterdir"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert cli.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "prepared_not_executed",
        "reconstruction_started": False, "execution_authorized": False,
        "budget_claimed": False, "budget_charged": False}
    forbidden.assert_not_called()
    assert before == {name: value for name, value in sys.modules.items()
                      if name == "backend" or name.startswith("backend.") or name.startswith("scripts.")}


@pytest.mark.parametrize("argv", [
    ["--execute"], ["--execute-sky-c"], ["--execute-sky-c-only=true"],
    ["--execute-sky-c-only"], ["--mode", "execute"],
    ["--mode", "preflight", "--execute-sky-c-only"],
    ["--mode", "prepare", "--profile", "ignored.json"],
    ["--mode", "seal-tools", "--runtime-manifest", "ignored.json"],
    ["--mode", "preflight", "--binding", "ignored.json"],
    ["--mode", "prepare-openmvs", "--source-cpu-profile", "ignored.json"],
])
def test_wrong_execution_flag_or_mode_arguments_fail_before_import(argv, monkeypatch, capsys):
    cli = load_entry()
    forbidden = Mock(side_effect=AssertionError("backend cannot load"))
    monkeypatch.setattr(cli, "load_backend", forbidden)
    assert cli.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    forbidden.assert_not_called()


def test_candidate_contains_exact_31_code_files_no_assets_or_other_manifests(sealed):
    f = sealed
    manifest = json.loads((f.root / f.cli.TOOL_MANIFEST).read_bytes())
    assert len(f.cli.LEGACY_CODE_FILES) == 23 and len(f.cli.CODE_FILES) == 31
    legacy = importlib.util.spec_from_file_location("unchanged_local_c_cli", ROOT / "scripts/run_local_c.py")
    local_c = importlib.util.module_from_spec(legacy)
    legacy.loader.exec_module(local_c)
    assert f.cli.LEGACY_CODE_FILES == local_c.CODE_FILES
    assert manifest["source_state"] == "uncommitted_candidate"
    assert manifest["source_base_commit"] == "a" * 40
    assert manifest["kind"] == "local_sky_c_supervised_tools" and manifest["autorun"] is False
    assert set(manifest["files"]) == f.cli.CODE_FILES
    actual = {path.relative_to(f.root).as_posix() for path in f.root.rglob("*") if path.is_file()}
    assert actual == f.cli.CODE_FILES | {f.cli.TOOL_MANIFEST}
    assert all(file_sha(f.source / name) == file_sha(f.root / name) == digest
               for name, digest in manifest["files"].items())
    assert not (f.root / "c-only-tool-manifest.json").exists()
    assert not (f.root / "hybrid-tool-manifest.json").exists()
    assert f.cli.verify_tools(f.root, f.pin) == manifest


def test_candidate_and_manifest_are_never_overwritten(sealed):
    f = sealed
    before = file_sha(f.root / f.cli.TOOL_MANIFEST)
    with pytest.raises(f.cli.CLIError, match="SKY_C_NEW_CANDIDATE_ROOT_REQUIRED"):
        f.cli.seal_tools(f.source, f.root, source_base_commit="a" * 40)
    assert file_sha(f.root / f.cli.TOOL_MANIFEST) == before


@pytest.mark.parametrize("commit", [None, "", "g" * 40, "a" * 39, "a" * 64])
def test_sealer_requires_full_base_commit_before_creating_output(sealed, commit):
    f = sealed
    path = f.base / "invalid-candidate"
    with pytest.raises(f.cli.CLIError, match="SKY_C_SOURCE_BASE_COMMIT_REQUIRED"):
        f.cli.seal_tools(f.source, path, source_base_commit=commit)
    assert not path.exists()


def test_preflight_loads_sealed_legacy_helper_without_claim_or_execute(sealed):
    code, report, error = invocation(sealed)
    assert code == 0, error
    receipt = sealed.base / "report.json"
    assert report["preflight_receipt_sha256"] == file_sha(receipt)
    assert json.loads(receipt.read_bytes()) == {"status": "read_only_sky_c_preflight_passed",
        "budget_claimed": False, "reconstruction_started": False, "legacy_helper_closure": 23}
    assert receipt.stat().st_size < 1024
    assert not list(sealed.base.glob("c-budget-*")) and not list((sealed.base / "new-compute").iterdir())


def test_execute_is_only_dispatched_with_exact_explicit_flag(sealed):
    code, report, error = invocation(sealed, "execute", ["--execute-sky-c-only"])
    assert code == 0, error
    assert report["status"] == "synthetic_execute_dispatched"
    assert report["reconstruction_started"] is False


def test_published_manifest_loads_same_exact_closure_before_read_only_preflight(sealed):
    f = sealed
    manifest = publish_manifest(f)
    assert set(manifest) == {"schema_version", "kind", "autorun", "source_state", "source_commit", "files"}
    assert manifest["source_commit"] == "f" * 40 and manifest["source_state"] == "git_commit"
    assert set(manifest["files"]) == f.cli.CODE_FILES and len(manifest["files"]) == 31
    assert f.cli.verify_tool_files(f.root, f.pin) == manifest
    code, report, error = invocation(f)
    assert code == 0, error
    assert report["status"] == "read_only_sky_c_preflight_passed"
    assert report["budget_claimed"] is False and report["reconstruction_started"] is False
    assert not list(f.base.glob("c-budget-*")) and not list((f.base / "new-compute").iterdir())


@pytest.mark.parametrize("commit", [None, "", True, 42, "g" * 40, "A" * 40,
                                    "a" * 39, "a" * 64, "a" * 40 + "\n"])
def test_published_manifest_requires_exact_lowercase_full_commit_before_import(sealed, commit):
    f = sealed
    publish_manifest(f, commit)
    (f.root / "backend/experiments/sky_supervision.py").write_text(
        "raise AssertionError('manifest must fail before importing backend')\n", encoding="utf-8")
    repin(f)
    code, report, error = invocation(f)
    assert code == 1, error
    assert report["error_code"] == "SKY_C_TOOL_MANIFEST_INVALID"


@pytest.mark.parametrize("mutation", ["missing", "extra", "autorun", "kind", "source", "mixed", "pin"])
def test_published_manifest_has_same_strict_hash_and_closure_gates(sealed, mutation):
    f = sealed
    manifest = publish_manifest(f)
    path = f.root / f.cli.TOOL_MANIFEST
    if mutation == "missing":
        del manifest["files"]["backend/experiments/c_budget_chain.py"]
    elif mutation == "extra":
        manifest["files"]["../outside.py"] = "a" * 64
    elif mutation == "autorun":
        manifest["autorun"] = True
    elif mutation == "kind":
        manifest["kind"] = "local_c_only_tools"
    elif mutation == "source":
        (f.root / "backend/experiments/sky_supervision.py").write_text("raise AssertionError('must not import')\n")
    elif mutation == "mixed":
        manifest["source_base_commit"] = "a" * 40
    else:
        f.pin = "0" * 64
    if mutation not in {"source", "pin"}:
        write_json(path, manifest)
        f.pin = file_sha(path)
    code, report, error = invocation(f)
    assert code == 1, error
    assert report["status"] == "blocked" and report["error_code"].startswith("SKY_C_")


def test_candidate_manifest_cannot_claim_commit_metadata_or_implicit_promotion(sealed):
    f = sealed
    path = f.root / f.cli.TOOL_MANIFEST
    manifest = json.loads(path.read_bytes())
    manifest["source_commit"] = "f" * 40
    write_json(path, manifest)
    f.pin = file_sha(path)
    code, report, error = invocation(f)
    assert code == 1, error
    assert report["error_code"] == "SKY_C_TOOL_MANIFEST_INVALID"


@pytest.mark.parametrize("mutation", ["pin", "missing", "extra", "autorun", "kind", "base", "state", "source"])
def test_manifest_and_every_source_are_verified_before_any_backend_import(sealed, mutation):
    f = sealed
    path = f.root / f.cli.TOOL_MANIFEST
    manifest = json.loads(path.read_bytes())
    if mutation == "pin":
        f.pin = "0" * 64
    elif mutation == "missing":
        del manifest["files"]["backend/experiments/c_budget_chain.py"]
    elif mutation == "extra":
        manifest["files"]["../unsealed.py"] = "a" * 64
    elif mutation == "autorun":
        manifest["autorun"] = True
    elif mutation == "kind":
        manifest["kind"] = "local_c_only_tools"
    elif mutation == "base":
        manifest["source_base_commit"] = "fake"
    elif mutation == "state":
        manifest["source_state"] = "git_commit"
    else:
        (f.root / "backend/experiments/sky_supervision.py").write_text(
            "raise AssertionError('unsafe code must never import')\n", encoding="utf-8")
    if mutation not in {"pin", "source"}:
        write_json(path, manifest)
        f.pin = file_sha(path)
    code, report, error = invocation(f)
    assert code == 1, error
    assert report["status"] == "blocked" and report["error_code"].startswith("SKY_C_")
    assert "unsafe code" not in json.dumps(report)


def test_unsealed_initializers_and_pyc_are_not_loaded(sealed):
    f = sealed
    for name in ("backend/__init__.py", "backend/monitoring/__init__.py",
                 "backend/re3d_adapter/__init__.py", "scripts/__init__.py"):
        (f.root / name).write_text("raise AssertionError('unsealed initializer')\n", encoding="utf-8")
    cache = f.root / "backend/experiments/__pycache__"
    cache.mkdir()
    (cache / "sky_supervision.cpython-312.pyc").write_bytes(b"untrusted cached bytecode")
    code, report, error = invocation(f)
    assert code == 0, error
    assert report["legacy_helper_closure"] == 23


@pytest.mark.parametrize("name", ["backend.experiments.unsealed", "scripts.unsealed"])
def test_unsealed_imports_cannot_fall_back_to_repository_or_installed_packages(sealed, name):
    f = sealed
    (f.root / (name.replace(".", "/") + ".py")).write_text("raise RuntimeError('must not load')\n")
    (f.root / "backend/experiments/sky_supervision.py").write_text(f"import {name}\n")
    repin(f)
    code, report, error = invocation(f)
    assert code == 1, error
    assert report["error_code"] == "HYBRID_CLI_UNSEALED_MODULE_IMPORT_REJECTED"


@pytest.mark.parametrize("value", [None, RuntimeError("password=not-real"), "SKY_BAD token=not-real",
    "SKY_BAD\nTraceback", "SKY_" + "X" * 96, "UNKNOWN_CODE", "LOCAL_C_bad"])
def test_errors_never_publish_raw_exception_data(value):
    cli = load_entry()
    assert cli.public_error_code(value) == "SKY_C_ENTRY_FAILED"


def test_raised_failure_is_only_public_stable_code(monkeypatch, capsys):
    cli = load_entry()
    def bad(*args, **kwargs):
        raise RuntimeError("password=not-a-secret-but-must-not-print")
    monkeypatch.setattr(cli, "seal_tools", bad)
    assert cli.main(["--mode", "seal-tools", "--output", "ignored", "--source-base-commit", "a" * 40]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "SKY_C_ENTRY_FAILED"
    assert "password=" not in json.dumps(report)


def test_adapter_calls_frozen_mask_preparer_only_after_sealed_loader(monkeypatch, capsys):
    cli = load_entry()
    calls = []
    binding = {"mask_scope": "post_sfm_c_dense_texture_only"}
    fake = ModuleType("backend.experiments.sky_masks")
    fake.read_json = lambda path, pin: binding
    def prepare(value, **kwargs):
        assert calls == ["verified_loader", "guard"]
        assert value is binding and kwargs["guard"] is guard
        calls.append("frozen_prepare")
        return {"status": "frozen_preparation_consumed_external_masks", "consumed_count": 300}
    fake.prepare_frozen_openmvs = prepare
    monkeypatch.setitem(sys.modules, "backend.experiments.sky_masks", fake)
    guard = lambda: calls.append("guard")
    monkeypatch.setattr(cli, "PreparationGuard", lambda disk: guard)
    monkeypatch.setattr(cli, "load_backend", lambda *args: calls.append("verified_loader"))
    argv = ["--mode", "prepare-openmvs", "--tool-manifest-sha256", "a" * 64,
        "--binding", "synthetic-binding", "--binding-sha256", "b" * 64,
        "--pipeline-root", "synthetic-pipeline", "--images", "synthetic-images",
        "--sparse", "synthetic-sparse", "--output", "synthetic-output"]
    assert cli.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consumed_count"] == 300 and not report["reconstruction_started"]
    assert calls == ["verified_loader", "guard", "frozen_prepare"]


def test_verify_tool_files_alias_never_bootstraps_or_imports_backend(sealed, monkeypatch):
    f = sealed
    forbidden = Mock(side_effect=AssertionError("revalidation must not bootstrap"))
    monkeypatch.setattr(f.cli, "load_backend", forbidden)
    manifest = f.cli.verify_tool_files(f.root, f.pin)
    assert len(manifest["files"]) == 31
    forbidden.assert_not_called()


def test_error_code_property_cannot_break_public_error_mapping():
    class HostileException(RuntimeError):
        @property
        def code(self):
            raise RuntimeError("not public")
    assert load_entry().public_error_code(HostileException()) == "SKY_C_ENTRY_FAILED"


def test_returned_failure_details_are_not_public(monkeypatch, capsys):
    cli = load_entry()
    runner = SimpleNamespace(preflight_sky_c=lambda *args, **kwargs: {
        "status": "failed", "error_code": "SKY_PREFLIGHT_FAILED token=not-real",
        "detail": "password=not-real", "reconstruction_started": False})
    monkeypatch.setattr(cli, "load_backend", lambda *args: runner)
    monkeypatch.setattr(cli, "preflight_receipt_target", lambda *args: Path("not-written.json"))
    argv = ["--mode", "preflight"]
    for flag in ("profile", "profile-sha256", "source-cpu-profile", "source-cpu-profile-sha256",
                 "terminal-receipt", "runtime-manifest", "runtime-manifest-sha256", "output",
                 "pipeline-root", "tool-manifest-sha256"):
        argv.extend(["--" + flag, "synthetic"])
    assert cli.main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error_code"] == "SKY_C_ENTRY_FAILED"
    assert "detail" not in result and "password=" not in json.dumps(result) and "token=" not in json.dumps(result)


def test_preflight_existing_output_is_rejected_before_backend_gates_and_never_overwritten(sealed):
    f = sealed
    path = f.base / "report.json"
    raw = b'{"previous_receipt":"preserved"}\n'
    path.write_bytes(raw)
    (f.root / "backend/experiments/sky_supervision.py").write_text(
        "def preflight_sky_c(*args, **kwargs):\n"
        "    raise AssertionError('expensive gates must not run')\n")
    repin(f)
    code, report, error = invocation(f)
    assert code == 1, error
    assert report["error_code"] == "SKY_C_NEW_RECEIPT_REQUIRED"
    assert path.read_bytes() == raw


@pytest.mark.parametrize("folder", ["images", "original-images", "masks", "handoff", "old-cpu",
                                    "new-compute", "candidate", "sfm-handoff", "sfm-sparse"])
def test_preflight_receipt_cannot_be_written_inside_any_source_or_compute_root(sealed, folder):
    f = sealed
    output = f.base / folder / "receipt.json"
    (f.root / "backend/experiments/sky_supervision.py").write_text(
        "def preflight_sky_c(*args, **kwargs):\n"
        "    raise AssertionError('expensive gates must not run before destination rejection')\n")
    repin(f)
    code, report, error = invocation(f, extra=["--output", str(output)])
    assert code == 1, error
    assert report["error_code"] == "SKY_C_RECEIPT_PROTECTED_OVERLAP"
    assert not output.exists()


def test_preflight_private_receipt_path_is_rejected_without_lstat_or_import(monkeypatch):
    cli = load_entry()
    forbidden = Mock(side_effect=AssertionError("private parent must not be inspected"))
    monkeypatch.setattr(cli, "plain", forbidden)
    with pytest.raises(cli.CLIError, match="SKY_C_RECEIPT_PROTECTED_OVERLAP"):
        cli.preflight_receipt_target(SimpleNamespace(
            output=Path(r"C:\ProgramData\Re3DRestricted\new-receipt.json")))
    forbidden.assert_not_called()


@pytest.mark.parametrize("value", [None, 17, "relative-source", r"D:\source\..\photos",
                                    r"\\remote\photos", r"D:\source:alternate"])
def test_original_binding_path_cannot_be_invalid_or_silently_ignored(sealed, value):
    f = sealed
    binding_path = f.base / "binding.json"
    binding = json.loads(binding_path.read_bytes())
    binding["original_source_root"] = value
    write_json(binding_path, binding)
    profile_path = f.base / "profile.json"
    profile = json.loads(profile_path.read_bytes())
    profile["mask_binding"]["sha256"] = file_sha(binding_path)
    write_json(profile_path, profile)
    args = SimpleNamespace(output=f.base / "report.json", profile=profile_path,
        profile_sha256=file_sha(profile_path), source_cpu_profile=f.base / "cpu.json",
        source_cpu_profile_sha256=file_sha(f.base / "cpu.json"), pipeline_root=f.base / "pipeline",
        tool_root=f.root, terminal_receipt=f.base / "terminal.json", runtime_manifest=f.base / "runtime.json")
    with pytest.raises(f.cli.CLIError, match="SKY_C_BINDING_PATH_INVALID"):
        f.cli.preflight_receipt_target(args)
    assert not args.output.exists()


def test_small_receipt_writer_uses_exclusive_creation_and_size_limit(sealed):
    f = sealed
    path = f.base / "small-receipt.json"
    pin = f.cli.write_preflight_receipt(path, {"status": "read_only_sky_c_preflight_passed"})
    before = path.read_bytes()
    assert pin == hashlib.sha256(before).hexdigest()
    with pytest.raises(FileExistsError):
        f.cli.write_preflight_receipt(path, {"status": "different"})
    assert path.read_bytes() == before
    oversized = f.base / "oversized.json"
    with pytest.raises(f.cli.CLIError, match="SKY_C_RECEIPT_TOO_LARGE"):
        f.cli.write_preflight_receipt(oversized, {"too_big": "x" * (1024**2)})
    assert not oversized.exists()
