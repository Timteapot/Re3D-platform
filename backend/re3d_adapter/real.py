from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import load_json_contract
from .errors import AdapterError, ContractValidationError, IntegrityError
from .events import utc_now
from .input_validation import validate_task_input
from .io import atomic_write_bytes, atomic_write_json, sha256_file
from .paths import TaskLayout


STEP_PATTERN = re.compile(
    r"^\[(run|skip)\]\s+([^:\r\n]+)(?::.*)?$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class RealDryRunOutcome:
    report_path: Path
    report: dict[str, Any]


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
