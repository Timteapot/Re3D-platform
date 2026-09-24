from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import load_json_contract, validate_contract
from .errors import (
    AdapterError,
    ContractValidationError,
    IntegrityError,
    PipelineTimedOut,
)
from .events import utc_now
from .input_validation import validate_task_input
from .io import atomic_write_bytes, atomic_write_json, sha256_file
from .paths import TaskLayout
from .process import run_managed_process


STEP_PATTERN = re.compile(
    r"^\[(run|skip)\]\s+([^:\r\n]+)(?::.*)?$",
    re.MULTILINE,
)

SAFE_RE3D_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HF_HUB_OFFLINE",
        "HOME",
        "LOCALAPPDATA",
        "MKL_NUM_THREADS",
        "NUMBER_OF_PROCESSORS",
        "OMP_NUM_THREADS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "RE3D_MAP_PYTHON",
        "RE3D_MVS_PYTHON",
        "RE3D_TEXTUREMESH_EXE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TRANSFORMERS_OFFLINE",
        "USERPROFILE",
        "WINDIR",
    }
)
SAFE_RE3D_ENVIRONMENT_PREFIXES = ("CUDA_", "NVIDIA_")
BLOCKED_RE3D_ENVIRONMENT_NAMES = frozenset(
    {
        "DATABASE_URL",
        "JWT_SECRET",
        "REFRESH_TOKEN",
        "SMTP_PASSWORD",
        "RE3D_TEST_DATABASE_URL",
    }
)


@dataclass(frozen=True)
class RealDryRunOutcome:
    report_path: Path
    report: dict[str, Any]


@dataclass(frozen=True)
class RealPipelineOutcome:
    result_path: Path
    result: dict[str, Any]
    reused: bool


def map_re3d_step(name: str) -> tuple[str, str]:
    number = name.split("-", 1)[0]
    if number == "00":
        return "preparing", "shared"
    if number in {"01", "02", "03"}:
        return "sfm", "shared"
    if number in {"10", "11"}:
        return "c_geometry", "C"
    if number == "12":
        return "c_meshing", "C"
    if number == "13":
        return "c_texturing", "C"
    if number == "20":
        return "a_inference", "A-v4"
    if number == "21":
        return "a_calibration", "A-v4"
    if number == "22":
        return "a_cross_view", "A-v4"
    if number in {"23", "23a", "23b", "24", "24a"}:
        return "a_fusion", "A-v4"
    if number == "25":
        return "a_meshing", "A-v4"
    if number == "26":
        return "a_texturing", "A-v4"
    if number == "30":
        return "b_inference", "B-v2"
    if number == "31":
        return "b_calibration", "B-v2"
    if number == "32":
        return "b_cross_view", "B-v2"
    if number in {"33", "33a", "33b", "34", "34a"}:
        return "b_fusion", "B-v2"
    if number == "35":
        return "b_meshing", "B-v2"
    if number == "36":
        return "b_texturing", "B-v2"
    if number == "90":
        return "output_validation", "shared"
    raise IntegrityError(f"unmapped Re3D step: {name}")


def parse_dry_run_steps(stdout: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for sequence, match in enumerate(STEP_PATTERN.finditer(stdout), start=1):
        action, name = match.groups()
        stage, branch = map_re3d_step(name)
        steps.append(
            {
                "sequence": sequence,
                "action": action,
                "re3d_step": name,
                "platform_stage": stage,
                "branch": branch,
            }
        )
    if not steps:
        raise IntegrityError("Re3D dry-run did not emit any recognized steps")
    observed_branches = {step["branch"] for step in steps}
    if not {"A-v4", "B-v2", "C"}.issubset(observed_branches):
        raise IntegrityError("Re3D dry-run did not include all three branches")
    return steps


def parse_re3d_step_line(line: str) -> dict[str, str] | None:
    match = STEP_PATTERN.fullmatch(line.strip())
    if match is None:
        return None
    action, name = match.groups()
    stage, branch = map_re3d_step(name)
    return {
        "action": action,
        "re3d_step": name,
        "platform_stage": stage,
        "branch": branch,
    }


def build_re3d_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    configured = source if source is not None else os.environ
    environment = {
        name: value
        for name, value in configured.items()
        if name.upper() not in BLOCKED_RE3D_ENVIRONMENT_NAMES
        and (
            name.upper() in SAFE_RE3D_ENVIRONMENT_NAMES
            or name.upper().startswith(SAFE_RE3D_ENVIRONMENT_PREFIXES)
        )
    }
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def verify_re3d_installation(re3d_root: Path, pipeline: dict[str, Any]) -> None:
    required = (
        re3d_root / "scripts" / "run_pipeline.py",
        re3d_root / "configs" / "pipeline.json",
        re3d_root / "configs" / "paths.local.json",
    )
    if not all(path.is_file() for path in required):
        raise IntegrityError("Re3D installation is missing required runtime files")
    if sha256_file(required[1]) != pipeline["config_sha256"]:
        raise IntegrityError("installed Re3D pipeline config hash does not match request")
    head = _git_value(re3d_root, "rev-parse", "HEAD")
    tagged_commit = _git_value(re3d_root, "rev-list", "-n", "1", pipeline["tag"])
    if head != pipeline["commit"] or tagged_commit != pipeline["commit"]:
        raise IntegrityError("installed Re3D Git identity does not match request")
    dirty = _git_value(re3d_root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise IntegrityError("installed Re3D tracked files contain uncommitted changes")


def _git_value(re3d_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(re3d_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise IntegrityError("cannot verify installed Re3D Git identity")
    return completed.stdout.strip()


class RealDryRunRunner:
    def __init__(
        self,
        layout: TaskLayout,
        *,
        re3d_root: Path,
        driver_python: Path,
        installation_verifier: Callable[[Path, dict[str, Any]], None] = verify_re3d_installation,
    ) -> None:
        self.layout = layout
        self.re3d_root = re3d_root.expanduser().resolve()
        self.driver_python = driver_python.expanduser().resolve()
        self.installation_verifier = installation_verifier

    @property
    def report_path(self) -> Path:
        return self.layout.resolve("manifests/real-dry-run-report.json")

    @property
    def log_path(self) -> Path:
        return self.layout.resolve("runtime/logs/re3d-dry-run.log")

    def build_command(self, request: dict[str, Any]) -> list[str]:
        return [
            str(self.driver_python),
            str(self.re3d_root / "scripts" / "run_pipeline.py"),
            "--scene",
            request["job_id"],
            "--images",
            str(self.layout.resolve(request["input"]["images_path"])),
            "--branches",
            "all",
            "--config",
            str(self.re3d_root / "configs" / "pipeline.json"),
            "--paths",
            str(self.re3d_root / "configs" / "paths.local.json"),
            "--work-dir",
            str(self.layout.resolve(request["storage"]["work_path"])),
            "--output-dir",
            str(self.layout.resolve(request["storage"]["output_path"])),
            "--log-dir",
            str(self.layout.resolve(request["storage"]["logs_path"])),
            "--dry-run",
        ]

    def run(self, *, timeout_seconds: int = 120) -> RealDryRunOutcome:
        self.layout.ensure_worker_directories()
        request = load_json_contract(self.layout.request_path, "pipeline-request")
        if request["job_id"] != self.layout.job_id:
            raise IntegrityError("request job_id does not match its task directory")
        if request["execution_mode"] != "real":
            raise ContractValidationError(
                "real dry-run only accepts execution_mode=real"
            )
        validate_task_input(self.layout, request)
        if not self.driver_python.is_file():
            raise IntegrityError("Re3D driver Python does not exist")
        self.installation_verifier(self.re3d_root, request["pipeline"])
        command = self.build_command(request)
        try:
            completed = subprocess.run(
                command,
                cwd=self.re3d_root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env=build_re3d_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterError("Re3D dry-run timed out") from exc
        combined_output = completed.stdout
        if completed.stderr:
            combined_output += "\n" + completed.stderr
        atomic_write_bytes(self.log_path, combined_output.encode("utf-8"))
        if completed.returncode != 0:
            raise AdapterError(
                f"Re3D dry-run failed with exit code {completed.returncode}"
            )
        steps = parse_dry_run_steps(completed.stdout)
        report = {
            "schema_version": "1.0",
            "operation": "real_pipeline_dry_run",
            "status": "pass",
            "checked_at": utc_now(),
            "job_id": request["job_id"],
            "request_id": request["request_id"],
            "pipeline": request["pipeline"],
            "command_contract": {
                "script": "scripts/run_pipeline.py",
                "branches": request["branches"],
                "work_path": request["storage"]["work_path"],
                "output_path": request["storage"]["output_path"],
                "logs_path": request["storage"]["logs_path"],
                "stdout_log": "runtime/logs/re3d-dry-run.log",
            },
            "step_count": len(steps),
            "steps": steps,
        }
        atomic_write_json(self.report_path, report)
        return RealDryRunOutcome(self.report_path, report)


class RealPipelineRunner:
    """Execute the fixed Re3D baseline and write a contract-valid result."""

    def __init__(
        self,
        layout: TaskLayout,
        *,
        re3d_root: Path,
        driver_python: Path,
        installation_verifier: Callable[
            [Path, dict[str, Any]], None
        ] = verify_re3d_installation,
    ) -> None:
        self.layout = layout
        self.re3d_root = re3d_root.expanduser().resolve()
        self.driver_python = driver_python.expanduser().resolve()
        self.installation_verifier = installation_verifier

    @property
    def log_path(self) -> Path:
        return self.layout.resolve("runtime/logs/re3d-run.log")

    def build_command(self, request: dict[str, Any]) -> list[str]:
        return [
            str(self.driver_python),
            str(self.re3d_root / "scripts" / "run_pipeline.py"),
            "--scene",
            request["job_id"],
            "--images",
            str(self.layout.resolve(request["input"]["images_path"])),
            "--branches",
            "all",
            "--config",
            str(self.re3d_root / "configs" / "pipeline.json"),
            "--paths",
            str(self.re3d_root / "configs" / "paths.local.json"),
            "--work-dir",
            str(self.layout.resolve(request["storage"]["work_path"])),
            "--output-dir",
            str(self.layout.resolve(request["storage"]["output_path"])),
            "--log-dir",
            str(self.layout.resolve(request["storage"]["logs_path"])),
        ]

    def run(
        self,
        *,
        cancel_requested: Callable[[], bool] = lambda: False,
        health_check: Callable[[], None] = lambda: None,
        on_step: Callable[[dict[str, str]], None] = lambda _step: None,
        poll_seconds: float = 0.25,
        timeout_seconds: int | None = None,
    ) -> RealPipelineOutcome:
        self.layout.ensure_worker_directories()
        request = load_json_contract(self.layout.request_path, "pipeline-request")
        self._validate_request(request)
        validate_task_input(self.layout, request)

        if self.layout.result_path.exists():
            result = load_json_contract(self.layout.result_path, "pipeline-result")
            self._validate_existing_result(request, result)
            return RealPipelineOutcome(self.layout.result_path, result, reused=True)

        effective_timeout = request["limits"]["timeout_seconds"]
        if timeout_seconds is not None:
            effective_timeout = min(effective_timeout, timeout_seconds)
        if effective_timeout < 1:
            raise PipelineTimedOut("Re3D task timeout was already exhausted")
        if not self.driver_python.is_file():
            raise IntegrityError("Re3D driver Python does not exist")
        self.installation_verifier(self.re3d_root, request["pipeline"])

        started_at = utc_now()
        process = run_managed_process(
            self.build_command(request),
            cwd=self.re3d_root,
            log_path=self.log_path,
            timeout_seconds=effective_timeout,
            cancel_requested=cancel_requested,
            health_check=health_check,
            on_line=lambda line: self._handle_line(line, on_step),
            poll_seconds=poll_seconds,
            env=build_re3d_environment(),
        )
        if process.return_code != 0:
            raise AdapterError(
                f"Re3D pipeline failed with exit code {process.return_code}"
            )

        result = self._build_result(
            request,
            started_at=started_at,
            finished_at=utc_now(),
            duration_seconds=process.duration_seconds,
        )
        validate_contract("pipeline-result", result)
        atomic_write_json(self.layout.result_path, result)
        return RealPipelineOutcome(self.layout.result_path, result, reused=False)

    @staticmethod
    def _handle_line(
        line: str,
        on_step: Callable[[dict[str, str]], None],
    ) -> None:
        step = parse_re3d_step_line(line)
        if step is not None:
            on_step(step)

    def _validate_request(self, request: dict[str, Any]) -> None:
        if request["job_id"] != self.layout.job_id:
            raise IntegrityError("request job_id does not match its task directory")
        if request["execution_mode"] != "real":
            raise ContractValidationError(
                "real pipeline only accepts execution_mode=real"
            )
        if request["branches"] != ["A-v4", "B-v2", "C"]:
            raise IntegrityError("real pipeline requires the fixed three branches")

    def _validate_existing_result(
        self,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if (
            result["job_id"] != request["job_id"]
            or result["request_id"] != request["request_id"]
            or result["attempt"] != request["attempt"]
            or result["execution_mode"] != "real"
            or result["status"] != "succeeded"
            or result["pipeline"]
            != {
                "tag": request["pipeline"]["tag"],
                "commit": request["pipeline"]["commit"],
                "config_sha256": request["pipeline"]["config_sha256"],
            }
            or result["input_summary"]["image_count"]
            != request["input"]["image_count"]
            or result["input_summary"]["manifest_sha256"]
            != request["input"]["manifest_sha256"]
        ):
            raise IntegrityError("existing real result identity does not match request")
        registered_images = _required_non_negative_int(
            result["input_summary"],
            "registered_images",
        )
        if registered_images > request["input"]["image_count"]:
            raise IntegrityError("registered image count exceeds input image count")
        _required_non_negative_number(
            result["input_summary"],
            "mean_reprojection_error_px",
        )
        _required_non_negative_number(result, "duration_seconds")
        for branch, branch_result in result["branches"].items():
            _required_positive_int(branch_result["metrics"], "vertices")
            _required_positive_int(branch_result["metrics"], "faces")
            if branch in {"A-v4", "B-v2"}:
                _required_ratio(
                    branch_result["metrics"],
                    "retained_valid_fraction",
                )
            self._validate_existing_artifact_contract(
                branch,
                branch_result["artifacts"],
            )
            for artifact in branch_result["artifacts"]:
                path = self.layout.resolve(artifact["path"])
                if path.is_symlink() or not path.is_file():
                    raise IntegrityError(f"persisted {branch} artifact is missing")
                if path.stat().st_size != artifact["size_bytes"]:
                    raise IntegrityError(f"persisted {branch} artifact size changed")
                if sha256_file(path) != artifact["sha256"]:
                    raise IntegrityError(f"persisted {branch} artifact hash changed")

    @staticmethod
    def _validate_existing_artifact_contract(
        branch: str,
        artifacts: list[dict[str, Any]],
    ) -> None:
        expected = {
            ("glb", f"output/{branch}/mesh.glb", "model/gltf-binary"),
            ("obj", f"output/{branch}/mesh.obj", "model/obj"),
            ("mtl", f"output/{branch}/mesh.mtl", "text/plain"),
        }
        observed = {
            (artifact["kind"], artifact["path"], artifact["content_type"])
            for artifact in artifacts
        }
        textures = [
            item
            for item in observed
            if item
            in {
                ("texture", f"output/{branch}/texture.jpg", "image/jpeg"),
                ("texture", f"output/{branch}/texture.jpeg", "image/jpeg"),
                ("texture", f"output/{branch}/texture.png", "image/png"),
            }
        ]
        if len(artifacts) != 4 or not expected.issubset(observed) or len(textures) != 1:
            raise IntegrityError(f"persisted {branch} artifact contract changed")
        if any(artifact["size_bytes"] < 1 for artifact in artifacts):
            raise IntegrityError(f"persisted {branch} artifact is empty")

    def _build_result(
        self,
        request: dict[str, Any],
        *,
        started_at: str,
        finished_at: str,
        duration_seconds: float,
    ) -> dict[str, Any]:
        validation_source = self.layout.resolve("output/validation.json")
        validation = _read_json_object(validation_source, "Re3D output validation")
        _validate_output_report(validation)
        atomic_write_json(self.layout.output_validation_path, validation)

        sfm = _read_json_object(
            self.layout.resolve(
                "runtime/work/shared/colmap/reconstruction_metrics.json"
            ),
            "SfM metrics",
        )
        registered_images = _required_non_negative_int(sfm, "registered_images")
        if registered_images > request["input"]["image_count"]:
            raise IntegrityError("registered image count exceeds input image count")
        reprojection_error = _required_non_negative_number(
            sfm,
            "mean_reprojection_error_px",
        )

        branch_results: dict[str, Any] = {}
        for branch in request["branches"]:
            branch_validation = validation["branches"][branch]
            metrics: dict[str, int | float | bool] = {
                "vertices": _required_positive_int(
                    branch_validation,
                    "glb_vertices",
                ),
                "faces": _required_positive_int(branch_validation, "glb_faces"),
            }
            consistency_path = {
                "A-v4": "runtime/work/branches/a_mapanything/depth-v4/consistency_manifest.json",
                "B-v2": "runtime/work/branches/b_mvsanywhere/depth-v2/consistency_manifest.json",
            }.get(branch)
            if consistency_path is not None:
                consistency = _read_json_object(
                    self.layout.resolve(consistency_path),
                    f"{branch} consistency metrics",
                )
                metrics["retained_valid_fraction"] = _required_ratio(
                    consistency,
                    "retained_valid_fraction",
                )
            branch_results[branch] = {
                "status": "succeeded",
                "duration_seconds": 0,
                "artifacts": self._collect_artifacts(branch),
                "metrics": metrics,
            }

        return {
            "contract_version": "1.0",
            "execution_mode": "real",
            "job_id": request["job_id"],
            "request_id": request["request_id"],
            "attempt": request["attempt"],
            "status": "succeeded",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": max(0.0, duration_seconds),
            "pipeline": {
                "tag": request["pipeline"]["tag"],
                "commit": request["pipeline"]["commit"],
                "config_sha256": request["pipeline"]["config_sha256"],
            },
            "input_summary": {
                "image_count": request["input"]["image_count"],
                "manifest_sha256": request["input"]["manifest_sha256"],
                "registered_images": registered_images,
                "mean_reprojection_error_px": reprojection_error,
            },
            "branches": branch_results,
            "diagnostics": {
                "input_manifest": request["input"]["manifest_path"],
                "events": "manifests/pipeline-events.jsonl",
                "output_validation": "manifests/output-validation.json",
            },
        }

    def _collect_artifacts(self, branch: str) -> list[dict[str, Any]]:
        root = self.layout.resolve(f"output/{branch}")
        texture_candidates = [
            path
            for path in (root / "texture.jpg", root / "texture.jpeg", root / "texture.png")
            if path.is_file()
        ]
        if len(texture_candidates) != 1:
            raise IntegrityError(f"{branch} must contain exactly one normalized texture")
        specs = (
            (root / "mesh.glb", "glb", "model/gltf-binary"),
            (root / "mesh.obj", "obj", "model/obj"),
            (root / "mesh.mtl", "mtl", "text/plain"),
            (
                texture_candidates[0],
                "texture",
                "image/png"
                if texture_candidates[0].suffix.lower() == ".png"
                else "image/jpeg",
            ),
        )
        artifacts: list[dict[str, Any]] = []
        for path, kind, content_type in specs:
            if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                raise IntegrityError(f"{branch} normalized {kind} artifact is invalid")
            relative = path.relative_to(self.layout.root).as_posix()
            artifacts.append(
                {
                    "kind": kind,
                    "path": relative,
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                    "content_type": content_type,
                }
            )
        return artifacts


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be a JSON object")
    return value


def _validate_output_report(report: dict[str, Any]) -> None:
    if report.get("all_passed") is not True:
        raise IntegrityError("Re3D output validation did not pass")
    branches = report.get("branches")
    if not isinstance(branches, dict) or set(branches) != {"A-v4", "B-v2", "C"}:
        raise IntegrityError("Re3D output validation branches are incomplete")
    for branch, value in branches.items():
        if not isinstance(value, dict) or value.get("status") != "pass":
            raise IntegrityError(f"Re3D output validation failed for {branch}")


def _required_non_negative_int(value: dict[str, Any], key: str) -> int:
    actual = value.get(key)
    if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
        raise IntegrityError(f"metric {key} must be a non-negative integer")
    return actual


def _required_positive_int(value: dict[str, Any], key: str) -> int:
    actual = _required_non_negative_int(value, key)
    if actual < 1:
        raise IntegrityError(f"metric {key} must be positive")
    return actual


def _required_non_negative_number(value: dict[str, Any], key: str) -> float:
    actual = value.get(key)
    if (
        not isinstance(actual, (int, float))
        or isinstance(actual, bool)
        or not math.isfinite(float(actual))
        or actual < 0
    ):
        raise IntegrityError(f"metric {key} must be a non-negative number")
    return float(actual)


def _required_ratio(value: dict[str, Any], key: str) -> float:
    actual = _required_non_negative_number(value, key)
    if actual > 1:
        raise IntegrityError(f"metric {key} must be between zero and one")
    return actual
