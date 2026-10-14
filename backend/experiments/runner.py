"""Explicit protected 4/8-thread windows; never a platform task resume API."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from backend.experiments.checkpoint import verify_checkpoint, restore_checkpoint, _database as validate_offline_database
from backend.experiments.preflight import (ExperimentGateError, IsolationGuard, read_queue_state,
    read_runtime_versions, read_windows_state, require_admin, validate_isolation, verify_git_identity)
from backend.experiments.process import run_contained_process
from backend.experiments.resources import ExperimentResourceMonitor, ResourceLimits
from backend.re3d_adapter.errors import AdapterError, PipelineTimedOut

PLATFORM_BASE = "6e8ad0cc3908b9abbbc29b8dcb54c56f4f69f777"
PIPELINE_BASE = "e517a161b4c0895f8be4258fb339272e4f99e1d6"
CONFIG_SHA = "9f8cbc83cb67b8313f624913686fbdc453e7572a18603d015bc198a9a92b8883"


def require(value, code):
    if not value:
        raise ExperimentGateError(code)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    require(path.is_file() and path.stat().st_size <= 4 * 1024**2, "EXPERIMENT_CONFIG_INVALID")
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), "EXPERIMENT_CONFIG_INVALID")
    return value


def write_exclusive(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def validate_plan(plan, policy):
    require(plan.get("execution_kind") == "standalone_checkpoint_experiment" and plan.get("autorun") is False, "EXPERIMENT_CONFIG_KIND_INVALID")
    require(plan["pipeline"]["commit"] == PIPELINE_BASE and plan["pipeline"]["config_sha256"] == CONFIG_SHA, "EXPERIMENT_FROZEN_IDENTITY_MISMATCH")
    require(plan["sfm"]["num_threads"] == policy["prepared_cpu_settings"]["sfm_feature_and_matching_threads"] == 8, "EXPERIMENT_THREAD_PLAN_INVALID")
    sfm = plan["sfm"]
    require(sfm["max_num_features"] == 6000 and sfm["max_image_size"] == 1600 and sfm["guided_matching"] is True and sfm["device"] == "cpu", "EXPERIMENT_QUALITY_SETTINGS_CHANGED")
    require(policy["preserved_gpu_and_quality_settings"]["task_gpu_concurrency"] == 1 and policy["preserved_gpu_and_quality_settings"]["mapanything"]["view_group_batch_size"] == 4, "EXPERIMENT_GPU_SETTINGS_CHANGED")
    require(plan["supervision"]["reservation_bytes"] == 64 * 1024**3 and plan["supervision"]["minimum_disk_free_bytes"] == 20 * 1024**3, "EXPERIMENT_DISK_SETTINGS_CHANGED")
    require(policy["promotion_test_limits"]["minimum_available_host_ram_bytes"] >= 32 * 1024**3 and policy["promotion_test_limits"]["minimum_free_gpu_vram_bytes"] >= 2 * 1024**3, "EXPERIMENT_MEMORY_HEADROOM_REDUCED")
    require(plan["supervision"]["disk_guard_interval_seconds"] == 1 and plan["supervision"]["resource_interval_seconds"] == 5 and plan["supervision"]["directory_sample_interval_seconds"] == 15, "EXPERIMENT_SAMPLING_SETTINGS_CHANGED")
    require(plan["sfm"]["matching"] == "exhaustive" and plan["sfm"]["camera_model"] == "PINHOLE" and plan["sfm"]["random_seed"] == 0 and plan["sfm"]["deferred_registration"] is True and plan["sfm"]["mapper_threads_override"] is None, "EXPERIMENT_QUALITY_SETTINGS_CHANGED")


def build_sfm_command(interpreter, pipeline_root, work, threads):
    require(threads in {4, 8}, "EXPERIMENT_THREAD_COUNT_NOT_APPROVED")
    return [str(interpreter), str(pipeline_root / "scripts/run_colmap_shared.py"),
        "--images", str(work / "input/images"), "--masks", str(work / "input/masks"),
        "--output", str(work / "shared/colmap"), "--num-threads", str(threads),
        "--max-num-features", "6000", "--camera-model", "PINHOLE", "--random-seed", "0",
        "--deferred-registration", "--defer-relative-observation-floor", "0.15",
        "--defer-min-triangulated-ratio", "0.05", "--defer-max-images", "5",
        "--retry-abs-pose-max-error", "6.0", "--retry-abs-pose-min-num-inliers", "80",
        "--retry-abs-pose-min-inlier-ratio", "0.30"]


def read_pair_counts(database, health_check=lambda: None):
    deadline = time.monotonic() + 1
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&cache=private", uri=True, timeout=0.2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        health_check()
        db.execute("BEGIN")
        row = db.execute("""SELECT (SELECT COUNT(*) FROM matches),
          (SELECT COUNT(*) FROM two_view_geometries),
          (SELECT COUNT(*) FROM matches JOIN two_view_geometries USING(pair_id)),
          (SELECT COUNT(*) FROM matches WHERE rows>0),
          (SELECT COUNT(*) FROM two_view_geometries WHERE rows>=15)""").fetchone()
        health_check()
        return dict(zip(("matches_rows", "geometry_rows", "complete_pair_records", "nonempty_match_records", "geometry_min15_records"), row))


def snapshot_stopped_clone(database, target, health_check, expected_names=None):
    require(not target.exists(), "EXPERIMENT_POST_SNAPSHOT_EXISTS")
    # The child Job is empty. Include committed WAL pages using online backup;
    # immutable mode is only appropriate for the finished destination snapshot.
    deadline = time.monotonic() + 120
    def bounded_health():
        health_check()
        require(time.monotonic() < deadline, "EXPERIMENT_POST_SNAPSHOT_TIMED_OUT")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&cache=private", uri=True, timeout=0.2)) as src:
        src.execute("PRAGMA query_only=ON")
        with target.open("xb"):
            pass
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=256, progress=lambda *_: bounded_health(), sleep=0.05)
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        def progress():
            bounded_health()
            return 0
        db.set_progress_handler(progress, 1000)
        require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "EXPERIMENT_POST_INTEGRITY_FAILED")
        require(not db.execute("PRAGMA foreign_key_check").fetchall(), "EXPERIMENT_POST_FOREIGN_KEY_FAILED")
        counts = [db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("images", "keypoints", "descriptors")]
        require(counts == [300, 300, 300], "EXPERIMENT_POST_FEATURE_COUNT_CHANGED")
    pairs = validate_offline_database(target, set(expected_names), bounded_health) if expected_names is not None else None
    return {"sha256": digest(target), "size_bytes": target.stat().st_size, "validated_complete_pair_records": pairs,
        "integrity_check": "ok", "foreign_key_check": "ok", "feature_image_counts": counts, "committed_wal_included": True}


def summarize_window(samples, duration_seconds):
    require(len(samples) >= 2 and duration_seconds > 0, "EXPERIMENT_PAIR_SAMPLES_INSUFFICIENT")
    first, last = samples[0], samples[-1]
    delta = last["complete_pair_records"] - first["complete_pair_records"]
    require(delta >= 0, "EXPERIMENT_PAIR_COUNT_REGRESSED")
    warm = next((sample for sample in samples if sample["elapsed_seconds"] >= 30), first)
    steady = last["elapsed_seconds"] - warm["elapsed_seconds"]
    return {"initial_complete_pairs": first["complete_pair_records"], "final_complete_pairs": last["complete_pair_records"],
        "added_complete_pairs": delta, "duration_seconds": duration_seconds, "overall_pairs_per_minute": delta / duration_seconds * 60,
        "steady_pairs_per_minute": (last["complete_pair_records"] - warm["complete_pair_records"]) / steady * 60 if steady > 0 else None,
        "warmup_sample_seconds": warm["elapsed_seconds"], "sample_count": len(samples)}


def create_private_root(root, *, kind="independent_cpu_benchmark"):
    require(kind in {"independent_cpu_benchmark", "independent_long_pipeline"}, "EXPERIMENT_ROOT_KIND_INVALID")
    require(not root.exists() and root.parent.name == "_experiments" and root.parent.is_dir(), "EXPERIMENT_TARGET_INVALID_OR_EXISTS")
    require(not any(part.lower() == "jobs" for part in root.parts), "EXPERIMENT_TARGET_INSIDE_JOBS")
    for parent in (root.parent, *root.parent.parents):
        require(not parent.is_symlink() and not (getattr(parent.stat(), "st_file_attributes", 0) & 1024), "EXPERIMENT_TARGET_REPARSE")
    root.mkdir()
    path = str(root).replace("'", "''")
    script = f"$ErrorActionPreference='Stop';$a=New-Object Security.AccessControl.DirectorySecurity;$a.SetSecurityDescriptorSddlForm('O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)');Set-Acl -LiteralPath '{path}' -AclObject $a;$v=Get-Acl -LiteralPath '{path}';$r=@($v.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]));if(-not $v.AreAccessRulesProtected-or $r.Count-ne 2){{exit 1}};foreach($x in $r){{if($x.IdentityReference.Value-notin @('S-1-5-18','S-1-5-32-544')-or $x.AccessControlType-ne 'Allow'-or $x.FileSystemRights-ne 'FullControl'-or $x.IsInherited){{exit 1}}}}"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()], capture_output=True, timeout=8, check=False)
    require(result.returncode == 0, "EXPERIMENT_PRIVATE_ACL_FAILED")
    (root / "reports").mkdir()
    write_exclusive(root / "operation-reservation.json", {"kind": kind, "created_at_utc": datetime.now(timezone.utc).isoformat()})


def run_window(*, plan, policy, checkpoint, root, pipeline_root, platform_root, interpreter, threads, seconds, allow_stale):
    create_private_root(root)
    limits = ResourceLimits(minimum_available_ram_bytes=policy["promotion_test_limits"]["minimum_available_host_ram_bytes"], minimum_free_vram_bytes=policy["promotion_test_limits"]["minimum_free_gpu_vram_bytes"])
    monitor = ExperimentResourceMonitor(experiment_root=root, sample_path=root / "reports/resource-samples.jsonl", limits=limits)
    isolation = IsolationGuard(lambda: {"host": read_windows_state(), "queue": read_queue_state(platform_root)}, plan["source_job_id"], allow_expired_source_job=allow_stale, stale_seconds=20, static_owned_pids={os.getpid()})
    started, samples = None, []
    result = {"schema_version": "1.0", "status": "incomplete", "threads": threads, "window_budget_seconds": seconds, "source_job_id": plan["source_job_id"], "source_job_modified": False, "platform_job_resume": False}
    def health():
        monitor.check_health()
        isolation.check_health()
    try:
        monitor.start()
        isolation.start()
        monitor.set_phase("restore")
        source = plan["source_checkpoint"]
        verified = verify_checkpoint(checkpoint, source["manifest_sha256"], source["database_sha256"], plan["source_job_id"], PIPELINE_BASE, CONFIG_SHA, health_check=health)
        restored = restore_checkpoint(verified, root, health_check=health, target_work_root=root / "work")
        command = build_sfm_command(interpreter, pipeline_root, restored.work_dir, threads)
        result["executed_argv"] = command  # private report, never console
        result["initial_database_sha256"] = digest(restored.database_path)
        samples.append({"elapsed_seconds": 0.0, **read_pair_counts(restored.database_path, health)})
        require(samples[0]["complete_pair_records"] == verified.committed_complete_pair_records, "EXPERIMENT_INITIAL_PAIR_COUNT_CHANGED")
        monitor.set_phase("benchmark")
        next_sample = 15.0
        def started_callback(pid):
            nonlocal started
            isolation.register_pid(pid)
            started = time.monotonic()
        def process_health():
            nonlocal next_sample
            health()
            if started is not None and time.monotonic() - started >= next_sample:
                elapsed = time.monotonic() - started
                samples.append({"elapsed_seconds": elapsed, **read_pair_counts(restored.database_path, health)})
                next_sample = elapsed + 15
        try:
            outcome = run_contained_process(command, cwd=pipeline_root, log_path=root / "reports/sfm-private.log", timeout_seconds=seconds, health_check=process_health, on_started=started_callback)
            require(outcome.return_code == 0, "EXPERIMENT_SFM_PROCESS_FAILED")
            status = "sfm_completed_before_window"
        except PipelineTimedOut:
            status = "benchmark_window_elapsed"
        require(started is not None, "EXPERIMENT_PROCESS_NOT_STARTED")
        elapsed = time.monotonic() - started
        isolation.clear_owned_pids()
        validate_isolation({"host": read_windows_state(), "queue": read_queue_state(platform_root)}, plan["source_job_id"], allow_expired_source_job=allow_stale, owned_pids={os.getpid()})
        health()
        samples.append({"elapsed_seconds": elapsed, **read_pair_counts(restored.database_path, health)})
        monitor.set_phase("verification")
        result["post_window_database_snapshot"] = snapshot_stopped_clone(restored.database_path, root / "reports/post-window-snapshot.db", health, verified.image_names)
        require(result["post_window_database_snapshot"]["validated_complete_pair_records"] == samples[-1]["complete_pair_records"], "EXPERIMENT_POST_PAIR_COUNT_CHANGED")
        result["throughput"] = summarize_window(samples, elapsed)
        result["pair_samples"] = samples
        result["source_checkpoint_database_unchanged"] = digest(checkpoint / "runtime/work/shared/colmap/database.db") == source["database_sha256"]
        require(result["source_checkpoint_database_unchanged"], "EXPERIMENT_SOURCE_CHECKPOINT_CHANGED")
        monitor.finish()
        isolation.check_health()
        result.update(status=status, isolation=isolation.last_summary)
    except BaseException as exc:
        result.update(status="failed", error_code=getattr(exc, "code", "EXPERIMENT_IO_OR_DATA_FAILED"))
        raise
    finally:
        monitor.close()
        isolation.close()
        result["resources"] = monitor.summary()
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_exclusive(root / "reports/window-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--platform-root", type=Path, default=Path(r"D:\Re3D-platform"))
    parser.add_argument("--pipeline-root", type=Path, default=Path(r"D:\Re3D"))
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--execute-benchmark", action="store_true")
    parser.add_argument("--allow-expired-source-job", action="store_true")
    parser.add_argument("--window-seconds", type=int, default=180)
    args = parser.parse_args()
    try:
        require_admin()
        plan, policy = read_json(args.config), read_json(args.policy)
        validate_plan(plan, policy)
        require(30 <= args.window_seconds <= 600, "EXPERIMENT_WINDOW_OUT_OF_RANGE")
        verify_git_identity(args.platform_root, PLATFORM_BASE)
        verify_git_identity(args.pipeline_root, PIPELINE_BASE)
        require(digest(args.pipeline_root / "configs/pipeline.json") == CONFIG_SHA, "EXPERIMENT_FROZEN_CONFIG_CHANGED")
        interpreter = Path(read_json(args.pipeline_root / "configs/paths.local.json")["mapanything_python"])
        if not interpreter.is_absolute():
            interpreter = args.pipeline_root / interpreter
        require(interpreter.resolve().is_relative_to(args.pipeline_root.resolve()), "EXPERIMENT_INTERPRETER_BOUNDARY")
        versions = read_runtime_versions(interpreter)
        require(versions["python_version"] == plan["pipeline"]["required_python_version"] and versions["pycolmap_version"] == plan["pipeline"]["required_pycolmap_version"], "EXPERIMENT_RUNTIME_VERSION_CHANGED")
        isolation = validate_isolation({"host": read_windows_state(), "queue": read_queue_state(args.platform_root)}, plan["source_job_id"], allow_expired_source_job=args.allow_expired_source_job, owned_pids={os.getpid()})
        if not args.execute_benchmark:
            print(json.dumps({"status": "read_only_preflight_passed", "runtime": versions, "isolation": isolation, "old_job_modified": False}), flush=True)
            return 0
        require(args.experiment_root is not None and args.checkpoint is not None and args.experiment_root.is_absolute() and args.experiment_root.parent.name == "_experiments", "EXPERIMENT_EXPLICIT_PATHS_REQUIRED")
        require(shutil.disk_usage(args.experiment_root.parent).free >= 84 * 1024**3, "EXPERIMENT_DISK_ADMISSION_FAILED")
        outputs = []
        for threads in (4, 8):
            root = args.experiment_root.with_name(args.experiment_root.name + f"-threads{threads}")
            print(json.dumps({"phase": "benchmark_window_starting", "threads": threads, "window_seconds": args.window_seconds}), flush=True)
            result = run_window(plan=plan, policy=policy, checkpoint=args.checkpoint, root=root, pipeline_root=args.pipeline_root, platform_root=args.platform_root, interpreter=interpreter, threads=threads, seconds=args.window_seconds, allow_stale=args.allow_expired_source_job)
            outputs.append(result)
            print(json.dumps({"phase": "benchmark_window_finished", "threads": threads, "status": result["status"], "throughput": result["throughput"]}), flush=True)
        baseline, candidate = [entry["throughput"]["overall_pairs_per_minute"] for entry in outputs]
        require(outputs[0]["initial_database_sha256"] == outputs[1]["initial_database_sha256"], "EXPERIMENT_COMPARISON_SOURCE_MISMATCH")
        report = {"schema_version": "1.0", "status": "initial_4_8_window_comparison_complete", "source_job_modified": False,
            "source_database_hash_same_for_both": outputs[0]["initial_database_sha256"] == outputs[1]["initial_database_sha256"],
            "baseline_threads": 4, "candidate_threads": 8, "baseline_pairs_per_minute": baseline, "candidate_pairs_per_minute": candidate,
            "observed_window_ratio": candidate / baseline if baseline > 0 else None, "window_budget_seconds": args.window_seconds, "windows": outputs,
            "limitations": ["SINGLE_PASS_FIXED_4_THEN_8_ORDER", "OS_CACHE_AND_PAIR_COMPLEXITY_MAY_DIFFER", "NOT_FULL_SFM_OR_THREE_BRANCH_COMPLETION", "PAIR_RECORDS_INCLUDE_EMPTY_RESULTS", "SAMPLED_RESOURCE_LIMITS_NOT_HARD_QUOTAS"]}
        write_exclusive(args.experiment_root.with_name(args.experiment_root.name + "-threads8") / "reports/comparison.json", report)
        print(json.dumps({key: value for key, value in report.items() if key != "windows"}), flush=True)
        return 0
    except KeyboardInterrupt:
        print('{"status":"stopped","error_code":"EXPERIMENT_OPERATOR_INTERRUPTED"}', flush=True)
        return 130
    except Exception as exc:
        print(json.dumps({"status": "not_completed", "error_code": getattr(exc, "code", "EXPERIMENT_PREFLIGHT_OR_RUN_FAILED"), "old_job_modified": False}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
