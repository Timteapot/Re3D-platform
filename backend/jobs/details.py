from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from backend.db.queue import JobQueue
from backend.db.state_machine import JobStatus, TERMINAL_STATUSES
from backend.re3d_adapter.contracts import load_json_contract
from backend.re3d_adapter.errors import AdapterError, IntegrityError
from backend.re3d_adapter.io import sha256_file
from backend.re3d_adapter.paths import TaskLayout


DetailState = Literal["pending", "available", "not_available", "missing", "invalid"]


@dataclass(frozen=True)
class JobDetailSnapshot:
    job: dict[str, Any]
    detail_state: DetailState
    warning_code: str | None
    request: dict[str, Any] | None
    result: dict[str, Any] | None
    evaluation: dict[str, Any] | None


class JobDetailReader:
    """Read contract-validated task summaries after database ownership checks."""

    def __init__(self, queue: JobQueue, *, data_root: Path) -> None:
        self.queue = queue
        self.data_root = data_root.expanduser().resolve()

    def read(
        self,
        job_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> JobDetailSnapshot:
        job = self.queue.get_job(job_id, user_id=user_id)
        status = JobStatus(job["status"])
        try:
            layout = TaskLayout.from_data_root(self.data_root, str(job_id))
        except AdapterError:
            return JobDetailSnapshot(
                job=job,
                detail_state="missing",
                warning_code="TASK_DIRECTORY_MISSING",
                request=None,
                result=None,
                evaluation=None,
            )

        try:
            request = load_json_contract(layout.request_path, "pipeline-request")
            self._validate_request_identity(request, job, user_id)
            result = self._load_optional(layout.result_path, "pipeline-result")
            if result is not None:
                self._validate_result_identity(result, job)
            evaluation = self._load_optional(layout.evaluation_path, "evaluation")
            if evaluation is not None:
                self._validate_evaluation_identity(evaluation, layout, job)
        except AdapterError:
            return JobDetailSnapshot(
                job=job,
                detail_state="invalid",
                warning_code="TASK_CONTRACT_INVALID",
                request=None,
                result=None,
                evaluation=None,
            )

        if result is not None and evaluation is not None:
            detail_state: DetailState = "available"
            warning_code = None
        elif status == JobStatus.SUCCEEDED:
            detail_state = "missing"
            warning_code = "TERMINAL_REPORT_MISSING"
        elif status in TERMINAL_STATUSES:
            detail_state = "not_available"
            warning_code = None
        else:
            detail_state = "pending"
            warning_code = None

        return JobDetailSnapshot(
            job=job,
            detail_state=detail_state,
            warning_code=warning_code,
            request=request,
            result=result,
            evaluation=evaluation,
        )

    @staticmethod
    def _load_optional(path: Path, contract_name: str) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        return load_json_contract(path, contract_name)

    @staticmethod
    def _validate_request_identity(
        request: dict[str, Any],
        job: dict[str, Any],
        user_id: uuid.UUID,
    ) -> None:
        if request["job_id"] != str(job["id"]):
            raise IntegrityError("request job_id does not match database job")
        if request["requested_by"]["user_id"] != str(user_id):
            raise IntegrityError("request owner does not match database owner")
        if request["execution_mode"] != job["execution_mode"]:
            raise IntegrityError("request execution mode does not match database job")

    @staticmethod
    def _validate_result_identity(
        result: dict[str, Any],
        job: dict[str, Any],
    ) -> None:
        if result["job_id"] != str(job["id"]):
            raise IntegrityError("result job_id does not match database job")
        if result["execution_mode"] != job["execution_mode"]:
            raise IntegrityError("result execution mode does not match database job")

    @staticmethod
    def _validate_evaluation_identity(
        evaluation: dict[str, Any],
        layout: TaskLayout,
        job: dict[str, Any],
    ) -> None:
        if evaluation["job_id"] != str(job["id"]):
            raise IntegrityError("evaluation job_id does not match database job")
        if not layout.result_path.is_file():
            raise IntegrityError("evaluation exists without a pipeline result")
        if evaluation["source_result_sha256"] != sha256_file(layout.result_path):
            raise IntegrityError("evaluation source hash does not match pipeline result")
