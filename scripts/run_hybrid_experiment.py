"""Explicit independent CPU SfM and cross-machine handoff operations.

No arguments / prepare only reads an optional profile with standard-library
validation. No backend, host, input, PostgreSQL or GPU code runs in that mode.
Every other operation pins the complete sealed code before importing it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import re
import stat
import sys
from types import ModuleType, SimpleNamespace

CODE_FILES = frozenset({
    *(f"backend/experiments/{name}.py" for name in (
        "__init__", "checkpoint", "hybrid_checkpoint", "resources", "process", "preflight", "runner",
        "high_profile", "stages", "snapshot_store", "evidence", "supervision", "long_runner", "cpu_runner", "hybrid_handoff")),
    "scripts/run_hybrid_experiment.py", "scripts/run_cpu_sfm_stage.py", "scripts/run_profiled_colmap.py",
    "backend/monitoring/resources.py", "backend/re3d_adapter/errors.py",
})
BASE_COMMIT = "e517a161b4c0895f8be4258fb339272e4f99e1d6"
BASE_CONFIG_SHA = "9f8cbc83cb67b8313f624913686fbdc453e7572a18603d015bc198a9a92b8883"
COLMAP_SHA = "a7918cce6dcd64fee5949e9f0e4d5091c72f97cefb70208be79dfbdc6c2ada41"
PIPELINE_SHA = "65ceba6c891b78ea49ed5b6f5079a1c6711bc00dfd45e6c729efbdc10c562378"
SHA = re.compile(r"[0-9a-f]{64}\Z")
GIB = 1024**3
_VERIFIED_TOOLS = {}


class HybridCLIError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Unknown/abbreviated arguments are rejected without echoing their data.
        raise HybridCLIError("HYBRID_CLI_ARGUMENTS_INVALID")


def require(condition, code):
    if not condition:
        raise HybridCLIError(code)


def plain_path(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "HYBRID_CLI_PATH_INVALID")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except OSError:
            raise HybridCLIError("HYBRID_CLI_PATH_MISSING") from None
        require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 1024),
                "HYBRID_CLI_REPARSE_REJECTED")
    return path


def read_document(path, *, limit=4 * 1024**2):
    path = plain_path(path)
    require(path.is_file() and path.stat().st_size <= limit, "HYBRID_CLI_DOCUMENT_INVALID")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "HYBRID_CLI_JSON_DUPLICATE_KEY")
            result[key] = value
        return result
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, HybridCLIError):
            raise
        raise HybridCLIError("HYBRID_CLI_JSON_INVALID") from None
    require(isinstance(value, dict), "HYBRID_CLI_JSON_OBJECT_REQUIRED")
    return value


def sha_file(path):
    with plain_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_profile(profile):
    """Mirror CPU profile shape without importing any backend/runtime modules."""
    require(isinstance(profile, dict) and profile.get("profile_kind") == "local_cpu_sfm_candidate"
            and profile.get("autorun") is False, "CPU_SFM_PROFILE_KIND_INVALID")
    pipeline = profile.get("pipeline", {})
    require(isinstance(pipeline, dict) and pipeline.get("commit") == BASE_COMMIT
            and pipeline.get("config_sha256") == BASE_CONFIG_SHA
            and pipeline.get("colmap_script_sha256") == COLMAP_SHA, "CPU_SFM_FROZEN_IDENTITY_CHANGED")
    require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(profile.get("source_job_id", ""))),
            "CPU_SFM_SOURCE_JOB_INVALID")
    source = profile.get("source_checkpoint", {})
    require(isinstance(source, dict) and set(source) == {
        "original_root", "original_manifest_sha256", "original_database_sha256", "snapshot_root",
        "snapshot_manifest_sha256", "snapshot_database_sha256", "expected_complete_pair_records"}
        and all(SHA.fullmatch(str(source[key])) for key in ("original_manifest_sha256", "original_database_sha256",
               "snapshot_manifest_sha256", "snapshot_database_sha256"))
        and type(source["expected_complete_pair_records"]) is int
        and 0 <= source["expected_complete_pair_records"] <= 44850, "CPU_SFM_SOURCE_CHECKPOINT_INVALID")
    for key in ("original_root", "snapshot_root"):
        require(isinstance(source[key], str) and Path(source[key]).is_absolute()
                and ".." not in Path(source[key]).parts, "CPU_SFM_SOURCE_PATH_INVALID")
    cpu = profile.get("cpu", {})
    require(isinstance(cpu, dict) and set(cpu) == {"sfm_threads", "mapper_ba_threads"}
            and type(cpu["sfm_threads"]) is int and cpu["sfm_threads"] in (8, 12)
            and type(cpu["mapper_ba_threads"]) is int and cpu["mapper_ba_threads"] == 16,
            "CPU_SFM_THREAD_BUDGET_INVALID")
    hardware = profile.get("target_hardware", {})
    require(isinstance(hardware, dict) and set(hardware) == {"logical_cpus", "host_ram_bytes"}
            and type(hardware["logical_cpus"]) is int and hardware["logical_cpus"] == 32
            and type(hardware["host_ram_bytes"]) is int and hardware["host_ram_bytes"] == 34141495296,
            "CPU_SFM_HARDWARE_PROFILE_INVALID")
    runtime = profile.get("runtime", {})
    require(isinstance(runtime, dict) and set(runtime) == {
        "mapanything_python", "python_version", "pycolmap_version", "pycolmap_has_cuda"}
        and isinstance(runtime["mapanything_python"], str) and Path(runtime["mapanything_python"]).is_absolute()
        and ".." not in Path(runtime["mapanything_python"]).parts
        and runtime["python_version"] == "3.12.14" and runtime["pycolmap_version"] == "3.13.0"
        and runtime["pycolmap_has_cuda"] is False, "CPU_SFM_RUNTIME_PROFILE_INVALID")
    expected = {"reservation_bytes": 8 * GIB, "disk_floor_bytes": 20 * GIB, "available_ram_floor_bytes": 8 * GIB,
                "host_guard_seconds": 1, "resource_sample_seconds": 5, "directory_sample_seconds": 15,
                "snapshot_interval_seconds": 600, "snapshot_retained": 2, "overall_timeout_seconds": 14400}
    guards = profile.get("protection", {})
    require(isinstance(guards, dict) and set(guards) == set(expected)
            and all(type(guards[key]) is int and guards[key] == value
                    for key, value in expected.items() if key != "overall_timeout_seconds")
            and type(guards["overall_timeout_seconds"]) is int
            and 0 < guards["overall_timeout_seconds"] <= expected["overall_timeout_seconds"],
            "CPU_SFM_PROTECTION_CHANGED")
    target = profile.get("target_experiment", {})
    require(isinstance(target, dict) and set(target) == {"scene", "root"}
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", str(target["scene"]))
            and isinstance(target["root"], str), "CPU_SFM_TARGET_INVALID")
    root = Path(target["root"])
    require(root.is_absolute() and ".." not in root.parts and root.parent.name == "_experiments"
            and root.name == target["scene"] and not any(part.casefold() == "jobs" for part in root.parts),
            "CPU_SFM_TARGET_INVALID")
    require(not any(Path(source[key]).is_relative_to(root) or root.is_relative_to(Path(source[key]))
                    for key in ("original_root", "snapshot_root")), "CPU_SFM_SOURCE_TARGET_OVERLAP")
    return profile


def verify_before_import(root, expected_sha256):
    """Exact sealed standard-library closure; no repository fallback imports."""
    root = plain_path(root)
    manifest_path = root / "hybrid-tool-manifest.json"
    require(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256)
            and sha_file(manifest_path) == expected_sha256, "HYBRID_CLI_TOOL_MANIFEST_HASH_MISMATCH")
    manifest = read_document(manifest_path, limit=128 * 1024)
    require(set(manifest) == {"schema_version", "toolpack_kind", "source_commit", "files", "autorun"}
            and manifest["schema_version"] == "1.0" and manifest["toolpack_kind"] == "re3d_cpu_sfm_handoff"
            and manifest["autorun"] is False and re.fullmatch(r"[0-9a-f]{40}", str(manifest["source_commit"]))
            and isinstance(manifest["files"], dict) and set(manifest["files"]) == CODE_FILES,
            "HYBRID_CLI_TOOL_MANIFEST_INVALID")
    for name, expected in manifest["files"].items():
        require(isinstance(expected, str) and SHA.fullmatch(expected), "HYBRID_CLI_TOOL_HASH_INVALID")
        require(sha_file(root / name) == expected, "HYBRID_CLI_TOOL_HASH_MISMATCH")
    require(sha_file(manifest_path) == expected_sha256, "HYBRID_CLI_TOOL_MANIFEST_CHANGED")
    _VERIFIED_TOOLS[str(root)] = (expected_sha256, dict(manifest["files"]))
    return manifest


class _PinnedSourceLoader(importlib.abc.Loader):
    def __init__(self, root, name, relative, digest):
        self.root, self.name, self.relative, self.digest = root, name, relative, digest

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = plain_path(self.root / self.relative)
        source = path.read_bytes()
        require(hashlib.sha256(source).hexdigest() == self.digest, "HYBRID_CLI_TOOL_HASH_CHANGED_BEFORE_IMPORT")
        module.__file__, module.__cached__ = str(path), None
        exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)


class _PinnedSourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, files):
        self.root, self.files = root, files
        self.names = {}
        for relative, digest in files.items():
            parts = Path(relative).with_suffix("").parts
            package = parts[-1] == "__init__"
            self.names[".".join(parts[:-1] if package else parts)] = (relative, digest, package)

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.names:
            relative, digest, package = self.names[fullname]
            return importlib.util.spec_from_loader(fullname,
                _PinnedSourceLoader(self.root, fullname, relative, digest), is_package=package)
        if fullname.startswith(("backend.", "scripts.")):
            raise HybridCLIError("HYBRID_CLI_UNSEALED_MODULE_IMPORT_REJECTED")
        return None


def bootstrap_namespace(root):
    """Use only the hash-verified overlay, skipping unrelated package initializers.

    Callers must run verify_before_import first. Existing foreign backend imports
    fail closed rather than being silently replaced in a long-lived interpreter.
    """
    root = Path(root)
    trusted = _VERIFIED_TOOLS.get(str(root))
    require(trusted is not None and sha_file(root / "hybrid-tool-manifest.json") == trusted[0],
            "HYBRID_CLI_BOOTSTRAP_NOT_HASH_VERIFIED")
    packages = {"backend": root / "backend", "backend.experiments": root / "backend/experiments",
                "backend.monitoring": root / "backend/monitoring", "backend.re3d_adapter": root / "backend/re3d_adapter",
                "scripts": root / "scripts"}
    for name, module in list(sys.modules.items()):
        if name == "backend" or name.startswith("backend.") or name == "scripts" or name.startswith("scripts."):
            origin = getattr(module, "__file__", None)
            if origin:
                relative = Path(origin).relative_to(root).as_posix() if Path(origin).is_relative_to(root) else None
                require(relative in CODE_FILES, "HYBRID_CLI_FOREIGN_MODULE_ALREADY_IMPORTED")
            elif name in packages:
                require(list(getattr(module, "__path__", ())) == [str(packages[name])],
                        "HYBRID_CLI_FOREIGN_MODULE_ALREADY_IMPORTED")
            else:
                raise HybridCLIError("HYBRID_CLI_FOREIGN_MODULE_ALREADY_IMPORTED")
    for name, path in packages.items():
        if name not in sys.modules:
            module = ModuleType(name)
            module.__path__ = [str(path)]
            module.__package__ = name
            module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
            sys.modules[name] = module
    finder = _PinnedSourceFinder(root, trusted[1])
    sys.meta_path.insert(0, finder)
    # The one package initializer in the exact code closure is loaded explicitly.
    package = sys.modules["backend.experiments"]
    if not getattr(package, "__file__", None):
        spec = finder.find_spec("backend.experiments")
        module = importlib.util.module_from_spec(spec)
        module.__path__ = [str(root / "backend/experiments")]
        sys.modules["backend.experiments"] = module
        spec.loader.exec_module(module)


def _load_modules(root):
    bootstrap_namespace(root)
    return SimpleNamespace(cpu_runner=importlib.import_module("backend.experiments.cpu_runner"),
                           checkpoint=importlib.import_module("backend.experiments.hybrid_checkpoint"),
                           handoff=importlib.import_module("backend.experiments.hybrid_handoff"))


def _source(profile, checkpoint):
    source = profile["source_checkpoint"]
    return checkpoint.verify_hybrid_source(**source, source_job_id=profile["source_job_id"],
        pipeline_commit=profile["pipeline"]["commit"], config_sha256=profile["pipeline"]["config_sha256"])


def main(argv=None, *, tool_root=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mode", choices=("prepare", "cpu", "verify-source", "restore-source", "export-sfm", "verify-handoff", "import-sfm"), default="prepare")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--tool-manifest-sha256")
    parser.add_argument("--pipeline-root", type=Path, default=Path(r"D:\3Dreconstruction\Re3D"))
    parser.add_argument("--platform-root", type=Path, default=Path(r"D:\3Dreconstruction\Re3D-platform"))
    parser.add_argument("--execute-cpu", action="store_true")
    parser.add_argument("--target-root", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--handoff-manifest-sha256")
    parser.add_argument("--quality-config-path", type=Path)
    parser.add_argument("--quality-config-sha256")
    parser.add_argument("--pipeline-script-sha256")
    parser.add_argument("--colmap-script-sha256")
    try:
        args = parser.parse_args(argv)
        require(args.mode == "cpu" or not args.execute_cpu, "HYBRID_CLI_EXECUTION_MODE_MISMATCH")
        if args.mode == "prepare":
            require(not any((args.target_root, args.bundle_root, args.experiment_root, args.handoff_manifest_sha256,
                             args.quality_config_path, args.quality_config_sha256, args.pipeline_script_sha256,
                             args.colmap_script_sha256)), "HYBRID_CLI_PREPARE_ACTION_ARGUMENT_REJECTED")
            profile = validate_profile(read_document(args.profile)) if args.profile else None
            result = {"status": "prepared_no_execution", "mode": "prepare", "profile_supplied": profile is not None,
                      "autorun": False, "input_source_accessed": False, "host_accessed": False,
                      "queue_accessed": False, "gpu_accessed": False, "filesystem_modified": False}
            if profile:
                result.update(sfm_threads=profile["cpu"]["sfm_threads"], mapper_threads=profile["cpu"]["mapper_ba_threads"],
                              expected_complete_pair_records=profile["source_checkpoint"]["expected_complete_pair_records"])
        else:
            require(args.profile is not None, "HYBRID_CLI_PROFILE_REQUIRED")
            require(args.tool_manifest_sha256 is not None, "HYBRID_CLI_TOOL_MANIFEST_PIN_REQUIRED")
            if args.mode == "cpu":
                require(args.execute_cpu, "CPU_SFM_EXPLICIT_EXECUTION_REQUIRED")
                require(not any((args.target_root, args.bundle_root, args.experiment_root, args.handoff_manifest_sha256,
                                 args.quality_config_path, args.quality_config_sha256, args.pipeline_script_sha256,
                                 args.colmap_script_sha256)), "HYBRID_CLI_CPU_ACTION_ARGUMENT_REJECTED")
            if args.mode in {"restore-source", "import-sfm"}:
                require(args.target_root is not None, "HYBRID_CLI_TARGET_REQUIRED")
            if args.mode in {"export-sfm", "verify-handoff", "import-sfm"}:
                require(args.bundle_root is not None, "HYBRID_CLI_BUNDLE_REQUIRED")
            if args.mode in {"verify-handoff", "import-sfm"}:
                require(isinstance(args.handoff_manifest_sha256, str) and SHA.fullmatch(args.handoff_manifest_sha256),
                        "HYBRID_CLI_HANDOFF_PIN_REQUIRED")
            incompatible = {
                "verify-source": ("target_root", "bundle_root", "experiment_root", "handoff_manifest_sha256",
                                  "quality_config_path", "quality_config_sha256", "pipeline_script_sha256", "colmap_script_sha256"),
                "restore-source": ("bundle_root", "experiment_root", "handoff_manifest_sha256",
                                   "quality_config_path", "quality_config_sha256", "pipeline_script_sha256", "colmap_script_sha256"),
                "export-sfm": ("target_root", "handoff_manifest_sha256"),
                "verify-handoff": ("target_root", "experiment_root", "quality_config_path"),
                "import-sfm": ("experiment_root", "quality_config_path"),
            }
            require(not any(getattr(args, key) is not None for key in incompatible.get(args.mode, ())),
                    "HYBRID_CLI_ACTION_ARGUMENT_MISMATCH")
            root = Path(tool_root) if tool_root is not None else Path(__file__).resolve().parents[1]
            verify_before_import(root, args.tool_manifest_sha256)
            profile = validate_profile(read_document(args.profile))
            modules = _load_modules(root)
            modules.cpu_runner.validate_cpu_profile(profile)
            if args.mode == "cpu":
                result = modules.cpu_runner.execute_cpu_sfm(args, profile, root)
            else:
                source = _source(profile, modules.checkpoint)
                if args.mode == "verify-source":
                    result = {"status": "source_verified", "complete_pair_records": source.complete_pair_records,
                              "sfm_complete": False, "source_modified": False}
                elif args.mode == "restore-source":
                    restored = modules.checkpoint.restore_hybrid_source(source, args.target_root)
                    result = {"status": "source_restored", "complete_pair_records": restored.committed_complete_pair_records,
                              "sfm_complete": False, "source_modified": False}
                else:
                    quality_sha = args.quality_config_sha256 or profile["pipeline"]["config_sha256"]
                    pipeline_sha = args.pipeline_script_sha256 or profile["pipeline"].get("pipeline_script_sha256", PIPELINE_SHA)
                    colmap_sha = args.colmap_script_sha256 or profile["pipeline"]["colmap_script_sha256"]
                    require(quality_sha == profile["pipeline"]["config_sha256"] and pipeline_sha == PIPELINE_SHA
                            and colmap_sha == profile["pipeline"]["colmap_script_sha256"], "HYBRID_CLI_FROZEN_IDENTITY_CHANGED")
                    if args.mode == "export-sfm":
                        require(sha_file(args.pipeline_root / "scripts/run_pipeline.py") == pipeline_sha
                                and sha_file(args.pipeline_root / "scripts/run_colmap_shared.py") == colmap_sha,
                                "HYBRID_CLI_FROZEN_SCRIPT_CHANGED")
                        saved = modules.handoff.export_sfm_handoff(args.experiment_root or Path(profile["target_experiment"]["root"]),
                            args.bundle_root, input_source=source, sfm_threads=profile["cpu"]["sfm_threads"],
                            mapper_threads=profile["cpu"]["mapper_ba_threads"], python_version=profile["runtime"]["python_version"],
                            quality_config_path=args.quality_config_path or args.pipeline_root / "configs/pipeline.json",
                            quality_config_sha256=quality_sha, pipeline_script_sha256=pipeline_sha, colmap_script_sha256=colmap_sha)
                        result = {"status": "handoff_exported", "handoff_manifest_sha256": saved.manifest_sha256,
                                  "complete_pair_records": saved.manifest["complete_pair_records"], "sfm_complete": True}
                    else:
                        saved = modules.handoff.verify_sfm_handoff(args.bundle_root, args.handoff_manifest_sha256,
                            input_source=source, expected_quality_config_sha256=quality_sha,
                            expected_pipeline_script_sha256=pipeline_sha, expected_colmap_script_sha256=colmap_sha)
                        if args.mode == "verify-handoff":
                            result = {"status": "handoff_verified", "handoff_manifest_sha256": saved.manifest_sha256,
                                      "complete_pair_records": saved.manifest["complete_pair_records"], "sfm_complete": True}
                        else:
                            imported = modules.handoff.import_sfm_handoff(saved, args.target_root, input_source=source)
                            result = {"status": "handoff_imported", "first_stage": imported.first_stage,
                                      "sfm_complete": True, "downstream_started": False, "source_modified": False}
                result.update(mode=args.mode, gpu_stage_started=False, queue_modified=False)
        # Keep stdout a whitelist: detailed CPU evidence stays private on disk.
        allowed = {"status", "mode", "error_code", "profile_supplied", "autorun", "input_source_accessed",
                   "host_accessed", "queue_accessed", "gpu_accessed", "filesystem_modified", "sfm_threads", "mapper_threads",
                   "expected_complete_pair_records", "complete_pair_records", "sfm_complete", "source_modified",
                   "handoff_manifest_sha256", "first_stage", "downstream_started", "gpu_stage_started", "queue_modified",
                   "process_tree_stopped", "final_snapshot_status"}
        print(json.dumps({key: value for key, value in result.items() if key in allowed}, allow_nan=False), flush=True)
        return 0 if result.get("status") in {"prepared_no_execution", "succeeded", "source_verified", "source_restored",
                    "handoff_exported", "handoff_verified", "handoff_imported"} else 1
    except Exception as exc:
        code = getattr(exc, "code", "HYBRID_CLI_ACTION_FAILED")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}", code):
            code = "HYBRID_CLI_ACTION_FAILED"
        print(json.dumps({"status": "not_completed", "error_code": code, "gpu_stage_started": False,
                          "queue_modified": False}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
