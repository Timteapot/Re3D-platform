from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import load_json_contract, validate_contract
from .errors import ContractValidationError, IntegrityError, SimulatedCrash
from .events import EventJournal, utc_now
from .input_validation import validate_task_input
from .io import atomic_write_bytes, atomic_write_json, sha256_file
from .paths import TaskLayout


BRANCH_STAGE = {
    "A-v4": "a_texturing",
    "B-v2": "b_texturing",
    "C": "c_texturing",
}


@dataclass(frozen=True)
class SimulationOutcome:
    result_path: Path
    result: dict[str, Any]
    reused: bool


def minimal_glb() -> bytes:
    document = {
        "asset": {"generator": "Re3D Platform simulator", "version": "2.0"},
        "scene": 0,
        "scenes": [{}],
    }
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
    total_length = 12 + 8 + len(json_chunk)
    return (
        struct.pack("<4sII", b"glTF", 2, total_length)
        + struct.pack("<I4s", len(json_chunk), b"JSON")
        + json_chunk
    )


class SimulationRunner:
    def __init__(self, layout: TaskLayout, *, worker_id: str) -> None:
        self.layout = layout
        self.worker_id = worker_id

    def run(self, *, crash_after_branch: str | None = None) -> SimulationOutcome:
        self.layout.ensure_worker_directories()
        request = load_json_contract(self.layout.request_path, "pipeline-request")
        self._validate_request_identity(request)
        validate_task_input(self.layout, request)

        journal = EventJournal(
            self.layout.events_path,
            job_id=request["job_id"],
            attempt=request["attempt"],
            worker_id=self.worker_id,
        )
        if self.layout.result_path.exists():
            result = load_json_contract(self.layout.result_path, "pipeline-result")
            self._validate_existing_result(request, result)
            if not journal.has_terminal_event:
                journal.append(
                    "job_succeeded",
                    progress={"percent": 100},
                    message_code="JOB_SUCCEEDED",
                    safe_message="模拟任务结果已恢复。",
                )
            return SimulationOutcome(self.layout.result_path, result, reused=True)
        if journal.has_terminal_event:
            raise IntegrityError("terminal event exists without a pipeline result")

        if journal.events:
            journal.append(
                "warning",
                message_code="WORKER_RESUMED",
                safe_message="Worker 已从持久化事件恢复模拟任务。",
            )
        else:
            journal.append(
                "job_started",
                message_code="JOB_STARTED",
                safe_message="模拟重建任务已开始。",
            )

        self._run_shared_stages(journal, request["input"]["image_count"])
        for branch in request["branches"]:
            self._run_branch(journal, branch)
            if crash_after_branch == branch:
                raise SimulatedCrash(f"simulated interruption after {branch}")

        self._write_output_validation(request["branches"])
        result = self._build_result(request, journal)
        validate_contract("pipeline-result", result)
        atomic_write_json(self.layout.result_path, result)
        journal.append(
            "job_succeeded",
            progress={"percent": 100},
            message_code="JOB_SUCCEEDED",
            safe_message="三条模拟分支均已完成并通过契约校验。",
        )
        return SimulationOutcome(self.layout.result_path, result, reused=False)

    def _validate_request_identity(self, request: dict[str, Any]) -> None:
        if request["job_id"] != self.layout.job_id:
            raise IntegrityError("request job_id does not match its task directory")
        if request["execution_mode"] != "simulated":
            raise ContractValidationError(
                "simulation runner only accepts execution_mode=simulated"
            )

    def _run_shared_stages(self, journal: EventJournal, image_count: int) -> None:
        stages = (
            ("preparing", "正在准备模拟输入。", {}),
            (
                "sfm",
                "模拟相机位姿求解完成。",
                {"registered_images": image_count, "mean_reprojection_error_px": 0.75},
            ),
        )
        for stage, message, metrics in stages:
            if journal.has_completed_stage(stage):
                continue
            journal.append(
                "stage_started",
                stage=stage,
                branch="shared",
                message_code="STAGE_STARTED",
                safe_message=message,
            )
            journal.append(
                "stage_progress",
                stage=stage,
                branch="shared",
                progress={"percent": 100},
                metrics=metrics,
                message_code="STAGE_PROGRESS",
                safe_message=message,
            )
            journal.append(
                "stage_completed",
                stage=stage,
                branch="shared",
                message_code="STAGE_COMPLETED",
                safe_message=message,
            )

    def _run_branch(self, journal: EventJournal, branch: str) -> None:
        stage = BRANCH_STAGE[branch]
        artifact_relative = f"output/{branch}/mesh.glb"
        artifact_path = self.layout.resolve(artifact_relative)
        existing_event = journal.artifact_event(branch)
        if existing_event is not None:
            artifact = existing_event["artifact"]
            if artifact["path"] != artifact_relative or not artifact_path.is_file():
                raise IntegrityError(f"persisted {branch} artifact is missing")
            if sha256_file(artifact_path) != artifact["sha256"]:
                raise IntegrityError(f"persisted {branch} artifact hash does not match")
            return
        if artifact_path.exists():
            raise IntegrityError(f"untracked {branch} artifact already exists")

        journal.append(
            "stage_started",
            stage=stage,
            branch=branch,
            message_code="STAGE_STARTED",
            safe_message=f"正在生成 {branch} 模拟产物。",
        )
        journal.append(
            "stage_progress",
            stage=stage,
            branch=branch,
            progress={"percent": 100, "completed_units": 1, "total_units": 1},
            metrics={"simulation": True},
            message_code="STAGE_PROGRESS",
            safe_message=f"{branch} 模拟产物已生成。",
        )
        atomic_write_bytes(artifact_path, minimal_glb())
        artifact_sha = sha256_file(artifact_path)
        manifest_path = artifact_path.parent / "artifact-manifest.json"
        atomic_write_json(
            manifest_path,
            {
                "simulation": True,
                "branch": branch,
                "artifacts": [
                    {
                        "path": artifact_relative,
                        "sha256": artifact_sha,
                        "size_bytes": artifact_path.stat().st_size,
                    }
                ],
            },
        )
        journal.append(
            "artifact_created",
            stage=stage,
            branch=branch,
            artifact={
                "kind": "glb",
                "path": artifact_relative,
                "sha256": artifact_sha,
                "size_bytes": artifact_path.stat().st_size,
                "content_type": "model/gltf-binary",
            },
            message_code="ARTIFACT_CREATED",
            safe_message=f"{branch} 模拟 GLB 已归档。",
        )
        journal.append(
            "stage_completed",
            stage=stage,
            branch=branch,
            message_code="STAGE_COMPLETED",
            safe_message=f"{branch} 模拟阶段完成。",
        )

    def _write_output_validation(self, branches: list[str]) -> None:
        atomic_write_json(
            self.layout.output_validation_path,
            {
                "simulation": True,
                "all_passed": True,
                "branches": {
                    branch: {
                        "status": "pass",
                        "glb": f"output/{branch}/mesh.glb",
                    }
                    for branch in branches
                },
            },
        )

    def _build_result(
        self,
        request: dict[str, Any],
        journal: EventJournal,
    ) -> dict[str, Any]:
        started_at = journal.events[0]["occurred_at"]
        finished_at = utc_now()
        duration = max(
            0.0,
            (
                datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                - datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            ).total_seconds(),
        )
        branches: dict[str, Any] = {}
        for branch in request["branches"]:
            artifact_relative = f"output/{branch}/mesh.glb"
            artifact_path = self.layout.resolve(artifact_relative)
            branches[branch] = {
                "status": "succeeded",
                "duration_seconds": 0,
                "artifacts": [
                    {
                        "kind": "glb",
                        "path": artifact_relative,
                        "sha256": sha256_file(artifact_path),
                        "size_bytes": artifact_path.stat().st_size,
                        "content_type": "model/gltf-binary",
                    }
                ],
                "metrics": {"simulation": True, "vertices": 0, "faces": 0},
            }
        return {
            "contract_version": "1.0",
            "execution_mode": "simulated",
            "job_id": request["job_id"],
            "request_id": request["request_id"],
            "attempt": request["attempt"],
            "status": "succeeded",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "pipeline": {
                "tag": request["pipeline"]["tag"],
                "commit": request["pipeline"]["commit"],
                "config_sha256": request["pipeline"]["config_sha256"],
            },
            "input_summary": {
                "image_count": request["input"]["image_count"],
                "manifest_sha256": request["input"]["manifest_sha256"],
                "registered_images": request["input"]["image_count"],
                "mean_reprojection_error_px": 0.75,
            },
            "branches": branches,
            "diagnostics": {
                "input_manifest": request["input"]["manifest_path"],
                "events": "manifests/pipeline-events.jsonl",
                "output_validation": "manifests/output-validation.json",
            },
        }

    def _validate_existing_result(
        self, request: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if (
            result["job_id"] != request["job_id"]
            or result["request_id"] != request["request_id"]
            or result["attempt"] != request["attempt"]
            or result["execution_mode"] != "simulated"
        ):
            raise IntegrityError("existing result identity does not match the request")
        for branch, branch_result in result["branches"].items():
            for artifact in branch_result["artifacts"]:
                artifact_path = self.layout.resolve(artifact["path"])
                if not artifact_path.is_file():
                    raise IntegrityError(f"existing {branch} result artifact is missing")
                if artifact_path.stat().st_size != artifact["size_bytes"]:
                    raise IntegrityError(
                        f"existing {branch} result artifact size does not match"
                    )
                if sha256_file(artifact_path) != artifact["sha256"]:
                    raise IntegrityError(
                        f"existing {branch} result artifact hash does not match"
                    )
