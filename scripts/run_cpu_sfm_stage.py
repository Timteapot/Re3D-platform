"""Contained CPU-only child adapter; the parent owns all safety supervision.

Without --execute-stage only a prepared-options report is produced. This child
does not initialize CUDA, inspect queues, start services, or run later stages.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

SFM_QUALITY_SHA = "9e164aeca125716771fc28bf04475caf18cbf39b76aa1ed10777070ae6a384a6"


def _require(value, code):
    if not value:
        raise ValueError(code)


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _ordinary(path, *, missing_leaf=False):
    _require(path.is_absolute() and ".." not in path.parts, "CPU_SFM_CHILD_PATH_INVALID")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            _require(missing_leaf and cursor == path, "CPU_SFM_CHILD_PATH_MISSING")
            return
        _require(not stat.S_ISLNK(info.st_mode) and not
                 (getattr(info, "st_file_attributes", 0) & 1024), "CPU_SFM_CHILD_PATH_REPARSE")


def _write_report(path, report):
    _ordinary(path, missing_leaf=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify_before_import(root, expected_manifest_sha256):
    """Only standard-library code runs before the sealed source hash gate."""
    manifest = root / "hybrid-tool-manifest.json"
    _ordinary(manifest)
    _require(re.fullmatch(r"[0-9a-f]{64}", str(expected_manifest_sha256))
             and manifest.stat().st_size <= 128 * 1024 and _sha(manifest) == expected_manifest_sha256,
             "CPU_SFM_CHILD_MANIFEST_CHANGED")
    document = json.loads(manifest.read_bytes())
    entries = document.get("files")
    required = {*(f"backend/experiments/{name}.py" for name in (
        "__init__", "checkpoint", "hybrid_checkpoint", "resources", "process", "preflight", "runner",
        "high_profile", "stages", "snapshot_store", "evidence", "supervision", "long_runner", "cpu_runner",
        "hybrid_handoff")), "scripts/run_hybrid_experiment.py", "scripts/run_cpu_sfm_stage.py",
        "scripts/run_profiled_colmap.py", "backend/monitoring/resources.py", "backend/re3d_adapter/errors.py"}
    _require(set(document) == {"schema_version", "toolpack_kind", "source_commit", "files", "autorun"}
             and document["schema_version"] == "1.0" and document["toolpack_kind"] == "re3d_cpu_sfm_handoff"
             and document["autorun"] is False
             and re.fullmatch(r"[0-9a-f]{40}", str(document["source_commit"]))
             and isinstance(entries, dict) and set(entries) == required, "CPU_SFM_CHILD_MANIFEST_INVALID")
    unique = set()
    for name, digest in entries.items():
        path = Path(name)
        _require(isinstance(name, str) and not path.is_absolute() and not path.drive and ".." not in path.parts
                 and "\\" not in name and name.casefold() not in unique
                 and re.fullmatch(r"[0-9a-f]{64}", str(digest)), "CPU_SFM_CHILD_MANIFEST_INVALID")
        unique.add(name.casefold())
        path = root / path
        _ordinary(path)
        _require(path.is_file() and _sha(path) == digest, "CPU_SFM_CHILD_HASH_CHANGED")


def bootstrap_verified_overlay(root, expected_manifest_sha256):
    """Share the sealed total CLI's namespace isolation, not platform initializers.

    The caller MUST already have passed verify_before_import. The stdlib CLI has
    itself been included in that exact manifest and has no backend imports.
    """
    script = root / "scripts/run_hybrid_experiment.py"
    spec = importlib.util.spec_from_file_location("re3d_verified_hybrid_cpu_bootstrap", script)
    module = importlib.util.module_from_spec(spec)
    # Execute the source bytes just hash-verified above, not an ignored pyc.
    exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
    module.verify_before_import(root, expected_manifest_sha256)
    module.bootstrap_namespace(root)


def load_frozen_source(path, module_name, expected_sha256):
    """Compile the pinned frozen module bytes; ignored bytecode is never read."""
    _ordinary(path)
    _require(module_name not in sys.modules, "CPU_SFM_CHILD_FROZEN_MODULE_ALREADY_IMPORTED")
    raw = path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == expected_sha256, "CPU_SFM_CHILD_FROZEN_IDENTITY_CHANGED")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # Required by stdlib dataclass annotations.
    try:
        exec(compile(raw, str(path), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--pipeline-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--tool-manifest-sha256", required=True)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--options-report", type=Path, required=True)
    parser.add_argument("--execute-stage", action="store_true")
    parser.add_argument("colmap_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    report = None
    try:
        overlay = Path(__file__).resolve().parents[1]
        # This sub-entry always reads only sealed code/config before imports.
        # Default preparation still never reads host/DB/GPU or starts compute.
        verify_before_import(overlay, args.tool_manifest_sha256)
        for path in (args.platform_root, args.pipeline_root, args.profile, args.experiment_root):
            _ordinary(path)
        _require(args.experiment_root.parent.name == "_experiments"
                 and args.options_report.parent == args.experiment_root / "reports"
                 and not any(part.casefold() == "jobs" for part in args.experiment_root.parts),
                 "CPU_SFM_CHILD_REPORT_PATH_INVALID")
        _ordinary(args.options_report, missing_leaf=True)
        _require(not args.options_report.exists(), "CPU_SFM_CHILD_REPORT_EXISTS")
        _require(re.fullmatch(r"[0-9a-f]{64}", args.profile_sha256)
                 and args.profile.stat().st_size <= 128 * 1024 and _sha(args.profile) == args.profile_sha256,
                 "CPU_SFM_CHILD_PROFILE_CHANGED")
        bootstrap_verified_overlay(overlay, args.tool_manifest_sha256)
        from backend.experiments.cpu_runner import validate_cpu_profile, build_cpu_colmap_flags
        from backend.experiments.high_profile import BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA
        profile = validate_cpu_profile(json.loads(args.profile.read_bytes()))
        _require(Path(profile["target_experiment"]["root"]) == args.experiment_root,
                 "CPU_SFM_CHILD_PROFILE_TARGET_MISMATCH")
        script = args.pipeline_root / "scripts/run_colmap_shared.py"
        config = args.pipeline_root / "configs/pipeline.json"
        _ordinary(script)
        _ordinary(config)
        _require(_sha(script) == COLMAP_SCRIPT_SHA and _sha(config) == BASE_CONFIG_SHA,
                 "CPU_SFM_CHILD_FROZEN_IDENTITY_CHANGED")
        flags = args.colmap_arguments
        if flags and flags[0] == "--":
            flags = flags[1:]
        expected = build_cpu_colmap_flags(args.experiment_root, json.loads(config.read_bytes()),
                                          profile["cpu"]["sfm_threads"])
        _require(flags == expected, "CPU_SFM_CHILD_QUALITY_ARGUMENTS_CHANGED")
        report = {"schema_version": "1.0", "status": "prepared_not_executed",
            "protection_owned_by_parent": True, "child_provides_resource_protection": False,
            "profile_sha256": args.profile_sha256, "tool_manifest_sha256": args.tool_manifest_sha256,
            "baseline_config_sha256": BASE_CONFIG_SHA, "colmap_script_sha256": COLMAP_SCRIPT_SHA,
            "requested_sfm_threads": profile["cpu"]["sfm_threads"], "requested_mapper_threads": 16,
            "actual_mapper_options": [], "sfm_stage_complete": False}
        if not args.execute_stage:
            _write_report(args.options_report, report)
            print(json.dumps({"status": "prepared_not_executed", "reconstruction_started": False}))
            return 0
        # Import only the frozen CPU SfM module, not run_pipeline or GPU branches.
        # The only non-native helper is also frozen and stdlib-only. Neither
        # module may inherit an ignored/mismatched __pycache__ implementation.
        load_frozen_source(args.pipeline_root / "scripts/sfm_quality.py", "sfm_quality", SFM_QUALITY_SHA)
        module = load_frozen_source(script, "re3d_frozen_cpu_sfm", COLMAP_SCRIPT_SHA)
        _require(module.pycolmap.__version__ == "3.13.0" and module.pycolmap.has_cuda is False
                 and sys.version.split()[0] == "3.12.14", "CPU_SFM_CHILD_RUNTIME_CHANGED")
        report["runtime_versions"] = {"python": sys.version.split()[0], "pycolmap": module.pycolmap.__version__,
                                      "pycolmap_has_cuda": False}
        report["status"] = "running"
        _write_report(args.options_report, report)
        from scripts.run_profiled_colmap import actual_option_recording
        original_argv = sys.argv
        try:
            sys.argv = [str(script), *flags]
            with actual_option_recording(module, profile["cpu"]["sfm_threads"], 16, report,
                                         lambda: _write_report(args.options_report, report)):
                module.main()
        finally:
            sys.argv = original_argv
        _require("actual_extraction" in report and "actual_matching" in report and report["actual_mapper_options"],
                 "CPU_SFM_CHILD_ACTUAL_OPTIONS_MISSING")
        report.update(status="succeeded", sfm_stage_complete=True)
        _write_report(args.options_report, report)
        print(json.dumps({"status": "succeeded", "actual_sfm_threads": profile["cpu"]["sfm_threads"],
                          "actual_mapper_threads": 16}))
        return 0
    except BaseException as exc:
        code = getattr(exc, "code", None)
        if code is None and isinstance(exc, ValueError) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", str(exc)):
            code = str(exc)
        code = code or ("CPU_SFM_CHILD_INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "CPU_SFM_CHILD_FAILED")
        if report is not None:
            try:
                report.update(status="failed", error_code=code, sfm_stage_complete=False)
                _write_report(args.options_report, report)
            except Exception:
                pass
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
