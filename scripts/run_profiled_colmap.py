"""Explicit supervised COLMAP child adapter; not a standalone safety launcher.

This child only wires frozen SfM/Mapper options and records actual calls. The
parent's contained process Job and resource monitor must supply the protection.
Without --execute-stage no reconstruction function is called.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


def _require(value, code):
    if not value:
        raise ValueError(code)


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _ordinary(path, *, missing_leaf=False):
    _require(path.is_absolute() and ".." not in path.parts, "PROFILED_COLMAP_ABSOLUTE_PATH_REQUIRED")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            _require(missing_leaf and cursor == path, "PROFILED_COLMAP_PATH_MISSING")
            return
        _require(not stat.S_ISLNK(info.st_mode)
                 and not (getattr(info, "st_file_attributes", 0) & 1024), "PROFILED_COLMAP_REPARSE")


def _write_report(path, report):
    _ordinary(path, missing_leaf=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def actual_option_recording(module, threads, mapper_threads, report, save):
    """Check actual objects at call sites; hooks are always restored."""
    from backend.experiments.high_profile import mapper_budget_adapter
    pycolmap = module.pycolmap
    extract, match = pycolmap.extract_features, pycolmap.match_exhaustive
    mapping_factory = module.mapping_options
    def checked_extract(*args, **kwargs):
        options = kwargs["extraction_options"]
        _require(options.num_threads == threads and options.max_image_size == 1600
                 and options.sift.max_num_features == 6000
                 and kwargs.get("device") == pycolmap.Device.cpu,
                 "PROFILED_COLMAP_EXTRACTION_NOT_APPLIED")
        report["actual_extraction"] = {"num_threads": options.num_threads,
            "max_image_size": options.max_image_size, "max_num_features": options.sift.max_num_features,
            "device": "cpu"}
        save()
        return extract(*args, **kwargs)
    def checked_match(*args, **kwargs):
        options = kwargs["matching_options"]
        _require(options.num_threads == threads and options.guided_matching is True
                 and kwargs.get("device") == pycolmap.Device.cpu,
                 "PROFILED_COLMAP_MATCHING_NOT_APPLIED")
        report["actual_matching"] = {"num_threads": options.num_threads,
            "guided_matching": options.guided_matching, "device": "cpu"}
        save()
        return match(*args, **kwargs)
    with mapper_budget_adapter(module, mapper_threads):
        budgeted_factory = module.mapping_options
        def recorded_factory(*args, **kwargs):
            options = budgeted_factory(*args, **kwargs)
            report["actual_mapper_options"].append({
                "call": len(report["actual_mapper_options"]) + 1,
                "phase": "initial" if not report["actual_mapper_options"] else "deferred_retry",
                "num_threads": options.num_threads, "mapper_num_threads": options.mapper.num_threads,
                "ba_inherits_top_level_num_threads": True})
            save()
            return options
        module.mapping_options = recorded_factory
        pycolmap.extract_features, pycolmap.match_exhaustive = checked_extract, checked_match
        try:
            yield
        finally:
            pycolmap.extract_features, pycolmap.match_exhaustive = extract, match
            module.mapping_options = budgeted_factory
    _require(module.mapping_options is mapping_factory, "PROFILED_COLMAP_ADAPTER_RESTORE_FAILED")


def _verify_tool_overlay(root):
    manifest = root / "tool-manifest.json"
    _ordinary(manifest)
    _require(manifest.stat().st_size <= 128*1024, "PROFILED_COLMAP_TOOL_MANIFEST_INVALID")
    entries = json.loads(manifest.read_bytes()).get("files", {})
    required = ("scripts/run_profiled_colmap.py", "backend/experiments/high_profile.py",
                "backend/experiments/preflight.py", "backend/experiments/__init__.py")
    _require(isinstance(entries, dict) and all(name in entries for name in required),
             "PROFILED_COLMAP_TOOL_MANIFEST_INVALID")
    for name in required:
        path = root / name
        _ordinary(path)
        _require(re.fullmatch(r"[0-9a-f]{64}", str(entries[name]))
                 and _sha(path) == entries[name], "PROFILED_COLMAP_TOOL_HASH_MISMATCH")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--pipeline-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--options-report", type=Path, required=True)
    parser.add_argument("--execute-stage", action="store_true")
    parser.add_argument("colmap_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    report = None
    try:
        _ordinary(args.profile)
        _ordinary(args.pipeline_root)
        _ordinary(args.platform_root)
        _ordinary(args.experiment_root)
        _require(args.experiment_root.parent.name == "_experiments"
                 and args.options_report.parent == args.experiment_root / "reports"
                 and not any(part.casefold() == "jobs" for part in args.experiment_root.parts),
                 "PROFILED_COLMAP_PRIVATE_REPORT_PATH_INVALID")
        _ordinary(args.options_report, missing_leaf=True)
        _require(not args.options_report.exists(), "PROFILED_COLMAP_REPORT_ALREADY_EXISTS")
        _require(re.fullmatch(r"[0-9a-f]{64}", args.profile_sha256)
                 and _sha(args.profile) == args.profile_sha256, "PROFILED_COLMAP_PROFILE_HASH_MISMATCH")
        overlay = Path(__file__).resolve().parents[1]
        if args.execute_stage:
            _verify_tool_overlay(overlay)
        sys.path.insert(0, str(args.platform_root))
        import backend
        tool_backend = str(overlay / "backend")
        if tool_backend in backend.__path__:
            backend.__path__.remove(tool_backend)
        backend.__path__.insert(0, tool_backend)
        from backend.experiments.high_profile import BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA, validate_profile
        profile = json.loads(args.profile.read_bytes())
        validate_profile(profile)
        script = args.pipeline_root / "scripts/run_colmap_shared.py"
        _ordinary(script)
        config = args.pipeline_root / "configs/pipeline.json"
        _ordinary(config)
        _require(_sha(script) == COLMAP_SCRIPT_SHA and _sha(config) == BASE_CONFIG_SHA,
                 "PROFILED_COLMAP_FROZEN_IDENTITY_CHANGED")
        flags = args.colmap_arguments
        if flags and flags[0] == "--":
            flags = flags[1:]
        expected_paths = {"--images": args.experiment_root / "work/input/images",
                          "--masks": args.experiment_root / "work/input/masks",
                          "--output": args.experiment_root / "work/shared/colmap"}
        expected_values = {"--num-threads": str(profile["cpu"]["sfm_threads"]),
                           "--max-num-features": "6000", "--camera-model": "PINHOLE", "--random-seed": "0"}
        baseline = json.loads(config.read_bytes())
        deferred = baseline["camera_frontend"]["deferred_registration"]
        deferred_flags = {"--defer-relative-observation-floor": "relative_observation_floor",
                          "--defer-min-triangulated-ratio": "minimum_triangulated_ratio",
                          "--defer-max-images": "max_images", "--retry-abs-pose-max-error": "retry_abs_pose_max_error",
                          "--retry-abs-pose-min-num-inliers": "retry_abs_pose_min_num_inliers",
                          "--retry-abs-pose-min-inlier-ratio": "retry_abs_pose_min_inlier_ratio"}
        _require(deferred["enabled"] is True and flags.count("--deferred-registration") == 1,
                 "PROFILED_COLMAP_QUALITY_ARGUMENTS_CHANGED")
        expected_values.update({flag:str(deferred[key]) for flag,key in deferred_flags.items()})
        _require({flag for flag in flags if flag.startswith("--")} ==
                 {*expected_paths,*expected_values,"--deferred-registration"},
                 "PROFILED_COLMAP_QUALITY_ARGUMENTS_CHANGED")
        for flag, value in {**expected_paths, **expected_values}.items():
            _require(flags.count(flag) == 1 and flags.index(flag) + 1 < len(flags),
                     "PROFILED_COLMAP_STAGE_ARGV_INVALID")
            actual = flags[flags.index(flag)+1]
            _require(Path(actual) == value if isinstance(value, Path) else actual == value,
                     "PROFILED_COLMAP_STAGE_ARGV_INVALID")
        report = {"schema_version": "1.0", "status": "prepared_not_executed",
                  "protection_owned_by_parent": True, "child_provides_resource_protection": False,
                  "profile_sha256": args.profile_sha256, "colmap_script_sha256": COLMAP_SCRIPT_SHA,
                  "baseline_config_sha256": BASE_CONFIG_SHA,
                  "requested_sfm_threads": profile["cpu"]["sfm_threads"],
                  "requested_mapper_threads": profile["cpu"]["mapper_ba_threads"],
                  "actual_mapper_options": [], "sfm_stage_complete": False}
        if not args.execute_stage:
            _write_report(args.options_report, report)
            print(json.dumps({"status": "prepared_not_executed", "reconstruction_started": False}))
            return 0
        sys.path.insert(0, str(args.pipeline_root / "scripts"))
        spec = importlib.util.spec_from_file_location("re3d_frozen_profiled_colmap", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _require(module.pycolmap.__version__ == "3.13.0", "PROFILED_COLMAP_VERSION_UNSUPPORTED")
        report["runtime_versions"] = {"python": sys.version.split()[0],
            "pycolmap": module.pycolmap.__version__, "pycolmap_has_cuda": bool(module.pycolmap.has_cuda)}
        _require(sys.version.split()[0] == "3.12.14" and module.pycolmap.has_cuda is False,
                 "PROFILED_COLMAP_RUNTIME_IDENTITY_CHANGED")
        report["status"] = "running"
        _write_report(args.options_report, report)
        original_argv = sys.argv
        try:
            sys.argv = [str(script), *flags]
            with actual_option_recording(module, profile["cpu"]["sfm_threads"],
                    profile["cpu"]["mapper_ba_threads"], report,
                    lambda: _write_report(args.options_report, report)):
                module.main()
        finally:
            sys.argv = original_argv
        _require("actual_extraction" in report and "actual_matching" in report
                 and report["actual_mapper_options"], "PROFILED_COLMAP_ACTUAL_OPTIONS_MISSING")
        report.update(status="succeeded", sfm_stage_complete=True)
        _write_report(args.options_report, report)
        print(json.dumps({"status": "succeeded", "actual_sfm_threads": report["requested_sfm_threads"],
                          "actual_mapper_threads": report["requested_mapper_threads"]}))
        return 0
    except BaseException as exc:
        code = getattr(exc, "code", None)
        if code is None and isinstance(exc, ValueError) and re.fullmatch(r"[A-Z0-9_]{3,100}", str(exc)):
            code = str(exc)
        code = code or ("PROFILED_COLMAP_INTERRUPTED" if isinstance(exc, KeyboardInterrupt)
                        else "PROFILED_COLMAP_STAGE_FAILED")
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
