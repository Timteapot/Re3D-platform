"""Opt-in, single-stage CPU SfM supervision for an independent hybrid scene.

This is not a platform task resume, a queue consumer, or a GPU launcher. Default
preparation is pure configuration validation. Only the explicitly executing
operator uses host probes, the contained child, and private evidence factories.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time

from backend.experiments.evidence import EvidenceSink
from backend.experiments.high_profile import BASE_COMMIT, BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA
from backend.experiments.preflight import read_runtime_versions, require_admin, verify_git_identity
from backend.experiments.process import run_contained_process
from backend.experiments.resources import ExperimentResourceMonitor, ResourceLimits, GIB
from backend.experiments.runner import create_private_root, read_json, read_pair_counts, write_exclusive
from backend.experiments.snapshot_store import SnapshotStore
from backend.experiments.stages import StageSpec, gate_sfm
from backend.experiments.supervision import (
    ObservationRelay, SupervisionError, WholeRunBudget, bounded_read_only, error_code,
)
from backend.monitoring.resources import HostResourceReader
from backend.re3d_adapter.errors import PipelineTimedOut

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCENE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_ENV_OVERRIDES = ("RE3D_MAP_PYTHON", "RE3D_MVS_PYTHON", "RE3D_TEXTUREMESH_EXE",
                  "RE3D_WORK_DIR", "RE3D_OUTPUT_DIR", "RE3D_LOG_DIR")
CPU_STAGE = "01-colmap"
CPU_TOOL_FILES = frozenset({
    *(f"backend/experiments/{name}.py" for name in (
        "__init__", "checkpoint", "hybrid_checkpoint", "resources", "process", "preflight", "runner",
        "high_profile", "stages", "snapshot_store", "evidence", "supervision", "long_runner", "cpu_runner",
        "hybrid_handoff")),
    "scripts/run_hybrid_experiment.py", "scripts/run_cpu_sfm_stage.py", "scripts/run_profiled_colmap.py",
    "backend/monitoring/resources.py", "backend/re3d_adapter/errors.py",
})


def require(value, code):
    if not value:
        raise SupervisionError(code)


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ordinary(path, *, missing_leaf=False):
    """Absolute, plain paths only; never follow a reparse point."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "CPU_SFM_PATH_INVALID")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            require(missing_leaf and cursor == path, "CPU_SFM_PATH_MISSING")
            return path
        require(not stat.S_ISLNK(info.st_mode) and not
                (getattr(info, "st_file_attributes", 0) & 1024), "CPU_SFM_PATH_REPARSE")
    return path


def validate_cpu_profile(profile):
    """A distinct local profile: no server RAM floor or GPU settings accepted."""
    require(isinstance(profile, dict) and profile.get("profile_kind") == "local_cpu_sfm_candidate"
            and profile.get("autorun") is False, "CPU_SFM_PROFILE_KIND_INVALID")
    pipeline = profile.get("pipeline", {})
    require(pipeline.get("commit") == BASE_COMMIT
            and pipeline.get("config_sha256") == BASE_CONFIG_SHA
            and pipeline.get("colmap_script_sha256") == COLMAP_SCRIPT_SHA,
            "CPU_SFM_FROZEN_IDENTITY_CHANGED")
    require(_UUID.fullmatch(str(profile.get("source_job_id", ""))) is not None,
            "CPU_SFM_SOURCE_JOB_INVALID")
    source = profile.get("source_checkpoint", {})
    required_source = {"original_root", "original_manifest_sha256", "original_database_sha256",
                       "snapshot_root", "snapshot_manifest_sha256", "snapshot_database_sha256",
                       "expected_complete_pair_records"}
    require(set(source) == required_source and all(_HASH.fullmatch(str(source[key]))
            for key in ("original_manifest_sha256", "original_database_sha256",
                        "snapshot_manifest_sha256", "snapshot_database_sha256"))
            and type(source["expected_complete_pair_records"]) is int
            and 0 <= source["expected_complete_pair_records"] <= 44850,
            "CPU_SFM_SOURCE_CHECKPOINT_INVALID")
    for key in ("original_root", "snapshot_root"):
        path = Path(source[key])
        require(path.is_absolute() and ".." not in path.parts, "CPU_SFM_SOURCE_PATH_INVALID")
    cpu = profile.get("cpu", {})
    require(set(cpu) == {"sfm_threads", "mapper_ba_threads"}
            and type(cpu["sfm_threads"]) is int and cpu["sfm_threads"] in (8, 12)
            and type(cpu["mapper_ba_threads"]) is int and cpu["mapper_ba_threads"] == 16,
            "CPU_SFM_THREAD_BUDGET_INVALID")
    hardware = profile.get("target_hardware", {})
    require(set(hardware) == {"logical_cpus", "host_ram_bytes"}
            and hardware["logical_cpus"] == 32 and type(hardware["logical_cpus"]) is int
            and hardware["host_ram_bytes"] == 34141495296 and type(hardware["host_ram_bytes"]) is int,
            "CPU_SFM_HARDWARE_PROFILE_INVALID")
    runtime = profile.get("runtime", {})
    require(set(runtime) == {"mapanything_python", "python_version", "pycolmap_version", "pycolmap_has_cuda"}
            and isinstance(runtime["mapanything_python"], str)
            and Path(runtime["mapanything_python"]).is_absolute()
            and ".." not in Path(runtime["mapanything_python"]).parts
            and runtime["python_version"] == "3.12.14" and runtime["pycolmap_version"] == "3.13.0"
            and runtime["pycolmap_has_cuda"] is False, "CPU_SFM_RUNTIME_PROFILE_INVALID")
    guards = profile.get("protection", {})
    expected = {"reservation_bytes": 8 * GIB, "disk_floor_bytes": 20 * GIB,
                "available_ram_floor_bytes": 8 * GIB, "host_guard_seconds": 1,
                "resource_sample_seconds": 5, "directory_sample_seconds": 15,
                "snapshot_interval_seconds": 600, "snapshot_retained": 2,
                "overall_timeout_seconds": 14400}
    require(set(guards) == set(expected)
            and all(type(guards[key]) is int and guards[key] == value for key, value in expected.items()
                    if key != "overall_timeout_seconds")
            and type(guards["overall_timeout_seconds"]) is int
            and 0 < guards["overall_timeout_seconds"] <= 14400,
            "CPU_SFM_PROTECTION_CHANGED")
    target = profile.get("target_experiment", {})
    root = Path(target.get("root", ""))
    require(set(target) == {"scene", "root"} and _SCENE.fullmatch(str(target.get("scene", "")))
            and root.is_absolute() and ".." not in root.parts and root.parent.name == "_experiments"
            and root.name == target["scene"] and not any(part.casefold() == "jobs" for part in root.parts),
            "CPU_SFM_TARGET_INVALID")
    require(not any(Path(source[key]).is_relative_to(root) or root.is_relative_to(Path(source[key]))
                    for key in ("original_root", "snapshot_root")), "CPU_SFM_SOURCE_TARGET_OVERLAP")
    return profile


def verify_cpu_tool_manifest(tool_root, expected_sha256):
    """Pin the sealed overlay before using it, rather than trusting a checkout."""
    manifest_path = ordinary(Path(tool_root) / "hybrid-tool-manifest.json")
    require(_HASH.fullmatch(str(expected_sha256)) and manifest_path.stat().st_size <= 128 * 1024
            and file_sha(manifest_path) == expected_sha256, "CPU_SFM_TOOL_MANIFEST_CHANGED")
    document = read_json(manifest_path)
    entries = document.get("files")
    require(set(document) == {"schema_version", "toolpack_kind", "source_commit", "files", "autorun"}
            and document["schema_version"] == "1.0" and document["toolpack_kind"] == "re3d_cpu_sfm_handoff"
            and document["autorun"] is False
            and re.fullmatch(r"[0-9a-f]{40}", str(document["source_commit"]))
            and isinstance(entries, dict) and set(entries) == CPU_TOOL_FILES, "CPU_SFM_TOOL_MANIFEST_INVALID")
    names = set()
    for name, digest in entries.items():
        path = Path(name)
        require(isinstance(name, str) and not path.is_absolute() and not path.drive and ".." not in path.parts
                and "\\" not in name and name.casefold() not in names and _HASH.fullmatch(str(digest)),
                "CPU_SFM_TOOL_MANIFEST_INVALID")
        names.add(name.casefold())
        candidate = ordinary(Path(tool_root) / path)
        require(candidate.is_file() and file_sha(candidate) == digest, "CPU_SFM_TOOL_HASH_MISMATCH")
    return expected_sha256


def declared_cpu_codes(tool_root):
    """Only literals in the already hash-verified sealed source extend evidence."""
    entries = read_json(Path(tool_root) / "hybrid-tool-manifest.json")["files"]
    codes = set(CPU_ERROR_CODES)
    pattern = r'''["']((?:CPU_SFM|HYBRID|EXPERIMENT|HIGH_PROFILE|PROFILED_COLMAP|SNAPSHOT|LONG|RESOURCE|LOW|CHECKPOINT)_[A-Z0-9_]{2,80})["']'''
    for name in entries:
        if name.endswith(".py"):
            codes.update(re.findall(pattern, (Path(tool_root) / name).read_text(encoding="utf-8")))
    return frozenset(codes)


def build_cpu_colmap_flags(root, baseline, sfm_threads):
    """The frozen quality flags, with only the explicit CPU thread change."""
    require(sfm_threads in (8, 12) and type(sfm_threads) is int, "CPU_SFM_THREAD_BUDGET_INVALID")
    deferred = baseline["camera_frontend"]["deferred_registration"]
    require(deferred["enabled"] is True, "CPU_SFM_QUALITY_ARGUMENTS_CHANGED")
    flags = ["--images", str(root / "work/input/images"), "--masks", str(root / "work/input/masks"),
             "--output", str(root / "work/shared/colmap"), "--num-threads", str(sfm_threads),
             "--max-num-features", "6000", "--camera-model", "PINHOLE", "--random-seed", "0",
             "--deferred-registration"]
    for flag, key in (("--defer-relative-observation-floor", "relative_observation_floor"),
                      ("--defer-min-triangulated-ratio", "minimum_triangulated_ratio"),
                      ("--defer-max-images", "max_images"),
                      ("--retry-abs-pose-max-error", "retry_abs_pose_max_error"),
                      ("--retry-abs-pose-min-num-inliers", "retry_abs_pose_min_num_inliers"),
                      ("--retry-abs-pose-min-inlier-ratio", "retry_abs_pose_min_inlier_ratio")):
        flags.extend((flag, str(deferred[key])))
    return flags


def bounded_private_report(path, value):
    outcome, done = [], threading.Event()
    def writer():
        try:
            write_exclusive(path, value)
            outcome.append(True)
        except BaseException:
            outcome.append(False)
        finally:
            done.set()
    threading.Thread(target=writer, daemon=True, name="cpu-sfm-private-result").start()
    require(done.wait(5) and outcome == [True], "CPU_SFM_PRIVATE_REPORT_FAILED")


class CPUSfmSupervisor:
    """One CPU stage, one bounded compute budget, no platform or GPU calls."""
    def __init__(self, *, root, run_id, source_job_id, monitor, evidence, snapshots, budget,
                 image_names, database, gate, source_final_check, identity,
                 process_runner=run_contained_process, relay_factory=ObservationRelay,
                 forensic_free_reader=None, disk_floor_bytes=20 * GIB, notify=lambda _: None):
        self.root, self.run_id, self.source_job_id = root, run_id, source_job_id
        self.monitor, self.evidence, self.snapshots, self.budget = monitor, evidence, snapshots, budget
        self.image_names, self.database = image_names, database
        self.gate, self.source_final_check, self.identity = gate, source_final_check, dict(identity)
        self.process_runner, self.notify = process_runner, notify
        self.forensic_free_reader = forensic_free_reader or (lambda: shutil.disk_usage(root).free)
        self.disk_floor_bytes = disk_floor_bytes
        self.relay = relay_factory(monitor, evidence, database, budget)
        self.relay.snapshot_store = snapshots
        self.snapshot_running, self.tree_stopped = False, True
        self.forensic_deadline = None

    def health(self):
        self.budget.check()
        self.monitor.check_health()
        self.evidence.check_health()
        self.relay.check_health()
        if self.snapshot_running:
            self.snapshots.check_health()

    def forensic_health(self):
        require(self.tree_stopped, "CPU_SFM_TREE_STOP_UNCONFIRMED")
        require(self.forensic_deadline is not None and time.monotonic() < self.forensic_deadline,
                "CPU_SFM_FORENSIC_BUDGET_EXHAUSTED")
        free = self.forensic_free_reader()
        require(type(free) is int and free > self.disk_floor_bytes, "CPU_SFM_FORENSIC_DISK_UNSAFE")

    def minimal(self, result):
        latest = self.snapshots.latest
        return {**self.identity, "run_id": self.run_id, "source_job_id": self.source_job_id,
                "stage": CPU_STAGE, "phase": "terminal", "status": result["status"],
                "error_code": result.get("error_code"), "source_job_modified": False,
                "platform_job_resume": False, "tree_stopped": self.tree_stopped,
                "elapsed_seconds": self.budget.elapsed, "stage_count": 1,
                "completed_stages_count": int(bool(result.get("sfm_gate"))),
                "checkpoint_status": result.get("final_snapshot_status", "pending"),
                "latest_snapshot_id": latest["directory"] if latest else None,
                "latest_snapshot_sha256": latest["database_sha256"] if latest else None,
                "complete_pairs": latest["validated_complete_pair_records"] if latest else None,
                "resources": self.monitor.summary().get("latest", {}),
                "peaks": self.monitor.summary().get("peaks", {}),
                "observation": self.relay.summary() if callable(getattr(self.relay, "summary", None)) else {},
                "source_checkpoint_unchanged": result.get("source_checkpoint_unchanged")}

    def run(self, stage, environment):
        require(stage.name == CPU_STAGE, "CPU_SFM_ONLY_STAGE_REQUIRED")
        result = {"schema_version": "1.0", "mode": "cpu-sfm", "status": "incomplete",
                  "stage_count": 1, "source_job_modified": False, "platform_job_resume": False,
                  "gpu_sampling_enabled": False, "gpu_stage_started": False}
        started = self.budget.monotonic()
        try:
            self.relay.start()
            self.health()
            result["initial_pair_counts"] = read_pair_counts(self.database)
            self.monitor.set_phase(CPU_STAGE)
            self.relay.set_stage(CPU_STAGE)
            self.snapshots.start()
            self.snapshot_running = True
            self.evidence.append("event", {"event_code": "STAGE_START", "stage": CPU_STAGE, "phase": "sfm",
                "run_id": self.run_id, "source_job_id": self.source_job_id, "status": "running"})
            self.notify({"phase": "stage_started", "stage": CPU_STAGE})
            def on_started(_pid):
                self.tree_stopped = False
            def on_tree_stopped():
                self.tree_stopped = True
            self.tree_stopped = False
            outcome = self.process_runner(list(stage.argv), cwd=stage.cwd,
                log_path=self.root / "reports/01-colmap-private.log", env=environment,
                timeout_seconds=self.budget.remaining, health_check=self.health,
                on_started=on_started, on_tree_stopped=on_tree_stopped)
            require(self.tree_stopped, "CPU_SFM_TREE_STOP_UNCONFIRMED")
            result["return_code"] = outcome.return_code
            require(outcome.return_code == 0, "CPU_SFM_STAGE_PROCESS_FAILED")
            require(self.snapshots.stop(timeout=5), "CPU_SFM_SNAPSHOT_STOP_UNCONFIRMED")
            self.snapshot_running = False
            self.health()
            result["sfm_gate"] = bounded_read_only(lambda: self.gate(self.health),
                timeout_seconds=self.budget.remaining, health_check=self.health)
            result["final_pair_counts"] = read_pair_counts(self.database)
            result["status"] = "succeeded"
            self.notify({"phase": "stage_finished", "stage": CPU_STAGE, "status": "passed"})
        except BaseException as exc:
            result.update(status="timed_out" if isinstance(exc, PipelineTimedOut)
                          or error_code(exc) == "LONG_BUDGET_EXHAUSTED"
                          else "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                          error_code=error_code(exc))
        finally:
            result["duration_seconds"] = max(0, self.budget.monotonic() - started)
            result["process_tree_stopped"] = self.tree_stopped
            try:
                result["observation_relay_stopped"] = self.relay.close()
                require(result["observation_relay_stopped"], "CPU_SFM_OBSERVATION_STOP_UNCONFIRMED")
            except BaseException as exc:
                result["observation_stop_error"] = error_code(exc)
                self.fail_success(result, "CPU_SFM_OBSERVATION_STOP_UNCONFIRMED")
            try:
                self.monitor.close()
                require(not self.monitor.summary().get("trip"), "CPU_SFM_RESOURCE_SHUTDOWN_FAILED")
            except BaseException as exc:
                result["resource_stop_error"] = error_code(exc)
                self.fail_success(result, "CPU_SFM_RESOURCE_SHUTDOWN_FAILED")
            try:
                stopped = self.snapshots.stop(timeout=5)
            except BaseException as exc:
                stopped = False
                result["snapshot_stop_error"] = error_code(exc)
            self.snapshot_running = False
            result["snapshot_worker_stopped"] = stopped
            if not stopped:
                self.fail_success(result, "CPU_SFM_SNAPSHOT_STOP_UNCONFIRMED")
            # Confirmed small C terminal precedes final DB backup and source hashes.
            result["terminal_evidence_confirmed"] = False
            try:
                self.evidence.finish_terminal({**self.minimal(result), "status": "stopped"})
                result["terminal_evidence_confirmed"] = True
            except BaseException as exc:
                result["terminal_evidence_error"] = error_code(exc)
                self.fail_success(result, "CPU_SFM_TERMINAL_EVIDENCE_FAILED")
            self.forensic_deadline = time.monotonic() + 120
            result["final_snapshot_status"] = "skipped"
            if self.tree_stopped and stopped and result["terminal_evidence_confirmed"]:
                try:
                    snapshot = self.snapshots.capture_final(tree_stopped=True,
                        health_check=self.forensic_health, reason="terminal")
                    result.update(final_snapshot_status="valid", final_snapshot=snapshot)
                    if result.get("final_pair_counts"):
                        require(snapshot["validated_complete_pair_records"] ==
                                result["final_pair_counts"]["complete_pair_records"],
                                "CPU_SFM_FINAL_PAIR_COUNT_CHANGED")
                except BaseException as exc:
                    result.update(final_snapshot_status="failed", final_snapshot_error=error_code(exc))
                    self.fail_success(result, "CPU_SFM_FINAL_SNAPSHOT_FAILED")
            if self.tree_stopped and result["terminal_evidence_confirmed"]:
                try:
                    bounded_read_only(lambda: self.source_final_check(self.forensic_health),
                        timeout_seconds=max(0, self.forensic_deadline - time.monotonic()),
                        health_check=self.forensic_health)
                    result["source_checkpoint_unchanged"] = True
                except BaseException as exc:
                    result["source_checkpoint_unchanged"] = None
                    result["source_verification_error"] = error_code(exc)
                    self.fail_success(result, "CPU_SFM_SOURCE_VERIFICATION_FAILED")
            result["resources"] = self.monitor.summary()
            result["checkpoint_summary"] = self.snapshots.summary()
            result["pair_samples"] = list(self.relay.pair_samples)
            result["observation"] = self.relay.summary() if callable(getattr(self.relay, "summary", None)) else {}
            cpu = list(self.relay.cpu_samples)
            result["cpu_sample_count"] = len(cpu)
            result["cpu_sampled_mean_percent"] = sum(value for _, value in cpu) / len(cpu) if cpu else None
            result["cpu_observed_span_seconds"] = cpu[-1][0] - cpu[0][0] if cpu else 0
            result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            try:
                self.evidence.write_forensic_summary(self.minimal(result))
            except BaseException as exc:
                result["forensic_evidence_error"] = error_code(exc)
                self.fail_success(result, "CPU_SFM_FORENSIC_EVIDENCE_FAILED")
            self.evidence.close()
        return result

    @staticmethod
    def fail_success(result, code):
        if result["status"] == "succeeded":
            result.update(status="failed", error_code=code)


def execute_cpu_sfm(args, profile, tool_root):
    """Executing entry; the total CLI owns the explicit --execute-cpu switch."""
    from backend.experiments.hybrid_checkpoint import verify_hybrid_source, restore_hybrid_source
    from backend.experiments.long_runner import exclusive_long_test
    validate_cpu_profile(profile)
    require(getattr(args, "execute_cpu", False) is True, "CPU_SFM_EXPLICIT_EXECUTION_REQUIRED")
    require_admin()
    tool_sha = verify_cpu_tool_manifest(tool_root, args.tool_manifest_sha256)
    pipeline_root, platform_root = Path(args.pipeline_root), Path(args.platform_root)
    verify_git_identity(pipeline_root, BASE_COMMIT)
    config = ordinary(pipeline_root / "configs/pipeline.json")
    require(file_sha(config) == BASE_CONFIG_SHA and
            file_sha(ordinary(pipeline_root / "scripts/run_colmap_shared.py")) == COLMAP_SCRIPT_SHA,
            "CPU_SFM_FROZEN_IDENTITY_CHANGED")
    require(all(not os.environ.get(key) for key in _ENV_OVERRIDES), "CPU_SFM_ENVIRONMENT_OVERRIDE")
    target = ordinary(Path(profile["target_experiment"]["root"]), missing_leaf=True)
    require(not target.exists() and target.parent.is_dir(), "CPU_SFM_TARGET_EXISTS")
    protections = profile["protection"]
    require(shutil.disk_usage(target.parent).free >= protections["reservation_bytes"] + protections["disk_floor_bytes"],
            "CPU_SFM_DISK_ADMISSION_FAILED")
    host, errors = HostResourceReader().read(target.parent)
    hardware = profile["target_hardware"]
    require(not errors and host["cpu"]["logical_count"] == hardware["logical_cpus"]
            and abs(host["memory"]["total_bytes"] - hardware["host_ram_bytes"]) <= GIB,
            "CPU_SFM_HARDWARE_CHANGED")
    require(host["memory"]["total_bytes"] - host["memory"]["used_bytes"] >
            protections["available_ram_floor_bytes"], "CPU_SFM_RAM_ADMISSION_FAILED")
    paths = read_json(ordinary(pipeline_root / "configs/paths.local.json"))
    interpreter = Path(paths["mapanything_python"])
    interpreter = interpreter if interpreter.is_absolute() else pipeline_root / interpreter
    ordinary(interpreter)
    # The existing local algorithm env is outside the pipeline checkout. Its
    # exact configured path is sealed in this profile, not a broad ancestor.
    require(interpreter == Path(profile["runtime"]["mapanything_python"]), "CPU_SFM_INTERPRETER_BOUNDARY")
    versions = read_runtime_versions(interpreter)
    require(versions == {key: profile["runtime"][key] for key in
                        ("python_version", "pycolmap_version", "pycolmap_has_cuda")},
            "CPU_SFM_RUNTIME_VERSION_CHANGED")
    scene = profile["target_experiment"]["scene"]
    source = profile["source_checkpoint"]
    def verify(health):
        return verify_hybrid_source(original_root=Path(source["original_root"]),
            original_manifest_sha256=source["original_manifest_sha256"],
            original_database_sha256=source["original_database_sha256"],
            source_job_id=profile["source_job_id"], pipeline_commit=BASE_COMMIT,
            config_sha256=BASE_CONFIG_SHA, snapshot_root=Path(source["snapshot_root"]),
            snapshot_manifest_sha256=source["snapshot_manifest_sha256"],
            snapshot_database_sha256=source["snapshot_database_sha256"],
            expected_complete_pair_records=source["expected_complete_pair_records"], health_check=health)
    with exclusive_long_test():
        evidence = EvidenceSink.create_production(scene, known_codes=declared_cpu_codes(tool_root), known_stages=(CPU_STAGE,))
        budget = WholeRunBudget(protections["overall_timeout_seconds"])
        monitor = snapshots = supervisor = None
        result = None
        try:
            evidence.start()
            create_private_root(target, kind="independent_cpu_benchmark")
            monitor = ExperimentResourceMonitor(experiment_root=target,
                sample_path=target / "reports/resource-samples.jsonl",
                limits=ResourceLimits(minimum_disk_free_bytes=protections["disk_floor_bytes"],
                    minimum_available_ram_bytes=protections["available_ram_floor_bytes"],
                    gpu_required=False, guard_interval_seconds=protections["host_guard_seconds"],
                    resource_interval_seconds=protections["resource_sample_seconds"],
                    directory_interval_seconds=protections["directory_sample_seconds"]))
            monitor.start()
            def health():
                budget.check(); monitor.check_health(); evidence.check_health()
            monitor.set_phase("restore")
            verified = verify(health)
            restored = restore_hybrid_source(verified, target, health_check=health, target_work_root=target / "work")
            profile_path = target / "reports/effective-cpu-profile.json"
            write_exclusive(profile_path, profile)
            flags = build_cpu_colmap_flags(target, read_json(config), profile["cpu"]["sfm_threads"])
            command = (str(interpreter), str(tool_root / "scripts/run_cpu_sfm_stage.py"),
                "--platform-root", str(platform_root), "--pipeline-root", str(pipeline_root),
                "--profile", str(profile_path), "--profile-sha256", file_sha(profile_path),
                "--tool-manifest-sha256", tool_sha, "--experiment-root", str(target),
                "--options-report", str(target / "reports/sfm-actual-options.json"), "--execute-stage", "--", *flags)
            stage = StageSpec(CPU_STAGE, command, target / "work/shared/colmap/refined_text/cameras.txt", pipeline_root)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join((str(tool_root), str(platform_root), environment.get("PYTHONPATH", "")))
            environment["HF_HUB_OFFLINE"] = "1"
            environment["TRANSFORMERS_OFFLINE"] = "1"
            identity = {"pipeline_commit": BASE_COMMIT, "source_manifest_sha256": source["original_manifest_sha256"],
                "source_database_sha256": source["snapshot_database_sha256"],
                "config_sha256": BASE_CONFIG_SHA, "profile_sha256": file_sha(profile_path), "tool_manifest_sha256": tool_sha}
            bounded_private_report(target / "reports/executed-cpu-plan.json", {"schema_version": "1.0",
                **identity, "runtime": versions, "stage": stage.observation(), "mode": "cpu-sfm",
                "gpu_sampling_enabled": False, "queue_accessed": False})
            snapshots = SnapshotStore(target / "checkpoints", restored.database_path, verified.image_names,
                health_check=health, free_bytes_reader=lambda: shutil.disk_usage(target).free,
                interval_seconds=protections["snapshot_interval_seconds"],
                min_free_bytes=protections["disk_floor_bytes"], timeout_seconds=120)
            supervisor = CPUSfmSupervisor(root=target, run_id=scene, source_job_id=profile["source_job_id"],
                monitor=monitor, evidence=evidence, snapshots=snapshots, budget=budget,
                image_names=verified.image_names, database=restored.database_path,
                gate=lambda health: gate_sfm(target, verified.image_names, health_check=health,
                    expected_sfm_threads=profile["cpu"]["sfm_threads"], expected_mapper_threads=16,
                    expected_python_version=profile["runtime"]["python_version"]), source_final_check=verify, identity=identity,
                disk_floor_bytes=protections["disk_floor_bytes"], notify=lambda item: print(json.dumps(item), flush=True))
            result = supervisor.run(stage, environment)
            result.update(**identity, sfm_threads=profile["cpu"]["sfm_threads"], mapper_ba_threads=16,
                          source_job_id=profile["source_job_id"], queue_modified=False, services_modified=False)
            try:
                bounded_private_report(target / "reports/cpu-result.json", result)
            except SupervisionError as exc:
                result["private_result_error"] = exc.code
                CPUSfmSupervisor.fail_success(result, "CPU_SFM_PRIVATE_REPORT_FAILED")
            return result
        except BaseException as exc:
            result = {"schema_version": "1.0", "mode": "cpu-sfm", "status": "failed",
                      "error_code": error_code(exc), "source_job_modified": False,
                      "platform_job_resume": False, "gpu_stage_started": False}
            if supervisor is None:
                try:
                    evidence.finish_terminal(result)
                except BaseException:
                    result["terminal_evidence_error"] = "CPU_SFM_TERMINAL_EVIDENCE_FAILED"
            return result
        finally:
            if supervisor is None:
                if snapshots:
                    snapshots.stop(timeout=5)
                if monitor:
                    monitor.close()
                evidence.close()


CPU_ERROR_CODES = frozenset({
    "CPU_SFM_TREE_STOP_UNCONFIRMED", "CPU_SFM_FORENSIC_BUDGET_EXHAUSTED",
    "CPU_SFM_FORENSIC_DISK_UNSAFE", "CPU_SFM_ONLY_STAGE_REQUIRED", "CPU_SFM_STAGE_PROCESS_FAILED",
    "CPU_SFM_SNAPSHOT_STOP_UNCONFIRMED", "CPU_SFM_OBSERVATION_STOP_UNCONFIRMED",
    "CPU_SFM_RESOURCE_SHUTDOWN_FAILED", "CPU_SFM_TERMINAL_EVIDENCE_FAILED",
    "CPU_SFM_FINAL_SNAPSHOT_FAILED", "CPU_SFM_FINAL_PAIR_COUNT_CHANGED",
    "CPU_SFM_SOURCE_VERIFICATION_FAILED", "CPU_SFM_FORENSIC_EVIDENCE_FAILED", "CPU_SFM_PRIVATE_REPORT_FAILED",
})
