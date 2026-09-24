from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.db.errors import LeaseLostError, SchedulerError
from backend.db.heartbeat import LeaseHeartbeatLoop
from backend.db.queue import JobClaim, JobQueue
from backend.db.state_machine import JobStatus, TERMINAL_STATUSES
from backend.evaluation import RealEvaluator, SimulationEvaluator
from backend.re3d_adapter.contracts import load_json_contract
from backend.re3d_adapter.errors import (
    AdapterError,
    PipelineCancelled,
    PipelineTimedOut,
)
from backend.re3d_adapter.events import EventJournal
from backend.re3d_adapter.paths import TaskLayout
from backend.re3d_adapter.real import RealPipelineRunner, verify_re3d_installation
from backend.re3d_adapter.simulation import SimulationRunner


@dataclass(frozen=True)
class QueuedSimulationOutcome:
    claimed: bool
    job_id: str | None
    status: str
    recovered: bool


QueuedJobOutcome = QueuedSimulationOutcome


class QueuedSimulationWorker:
    def __init__(
        self,
        queue: JobQueue,
        *,
        data_root: Path,
        worker_id: str,
        resource_key: str = "gpu:0",
        lease_seconds: int = 60,
        heartbeat_seconds: int = 20,
    ) -> None:
        self.queue = queue
        self.data_root = data_root
        self.worker_id = worker_id
        self.resource_key = resource_key
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def run_once(self) -> QueuedSimulationOutcome:
        claim = self.queue.claim_next(
            worker_id=self.worker_id,
            resource_key=self.resource_key,
            lease_seconds=self.lease_seconds,
            execution_mode="simulated",
        )
        if claim is None:
            return QueuedSimulationOutcome(False, None, "idle", False)
        job_id = str(claim.job_id)
        try:
            heartbeat = self.queue.heartbeat(
                claim,
                lease_seconds=self.lease_seconds,
            )
            if heartbeat.cancel_requested:
                self.queue.advance(
                    claim,
                    JobStatus.CANCELLED,
                    error_code="USER_CANCELLED",
                )
                return QueuedSimulationOutcome(
                    True,
                    job_id,
                    JobStatus.CANCELLED.value,
                    claim.recovered,
                )

            layout = TaskLayout.from_data_root(self.data_root, job_id)
            with LeaseHeartbeatLoop(
                self.queue,
                claim,
                interval_seconds=self.heartbeat_seconds,
                lease_seconds=self.lease_seconds,
            ) as heartbeat_loop:
                self.queue.advance(claim, JobStatus.SFM, progress=10)
                SimulationRunner(layout, worker_id=self.worker_id).run()
                heartbeat_loop.raise_if_failed()
                for status, progress in (
                    (JobStatus.DENSE_RECONSTRUCTION, 35),
                    (JobStatus.MESHING, 60),
                    (JobStatus.TEXTURING, 80),
                    (JobStatus.VALIDATING_OUTPUT, 90),
                ):
                    self.queue.advance(claim, status, progress=progress)
                self.queue.advance(claim, JobStatus.EVALUATING, progress=95)
                SimulationEvaluator(layout).run()
                heartbeat_loop.raise_if_failed()
            self.queue.advance(claim, JobStatus.SUCCEEDED, progress=100)
            return QueuedSimulationOutcome(
                True,
                job_id,
                JobStatus.SUCCEEDED.value,
                claim.recovered,
            )
        except LeaseLostError:
            raise
        except (AdapterError, SchedulerError, OSError, ValueError) as exc:
            self._mark_failed(claim)
            raise AdapterError(f"queued simulation failed for job {job_id}") from exc

    def _mark_failed(self, claim: JobClaim) -> None:
        try:
            current = self.queue.get_job(claim.job_id)["status"]
            if current in TERMINAL_STATUSES:
                return
            target = (
                JobStatus.FAILED_EVALUATION
                if current == JobStatus.EVALUATING
                else JobStatus.FAILED_PIPELINE
            )
            self.queue.advance(
                claim,
                target,
                error_code="SIMULATION_WORKER_FAILED",
            )
        except (LeaseLostError, SchedulerError, ValueError):
            return


class QueuedRealWorker:
    """Claim and supervise one real Re3D job under a database lease."""

    def __init__(
        self,
        queue: JobQueue,
        *,
        data_root: Path,
        re3d_root: Path,
        driver_python: Path,
        worker_id: str,
        resource_key: str = "gpu:0",
        lease_seconds: int = 60,
        heartbeat_seconds: int = 20,
        process_poll_seconds: float = 0.25,
        installation_verifier: Callable[
            [Path, dict[str, Any]], None
        ] = verify_re3d_installation,
    ) -> None:
        self.queue = queue
        self.data_root = data_root
        self.re3d_root = re3d_root
        self.driver_python = driver_python
        self.worker_id = worker_id
        self.resource_key = resource_key
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.process_poll_seconds = process_poll_seconds
        self.installation_verifier = installation_verifier

    def run_once(self) -> QueuedJobOutcome:
        claim = self.queue.claim_next(
            worker_id=self.worker_id,
            resource_key=self.resource_key,
            lease_seconds=self.lease_seconds,
            execution_mode="real",
        )
        if claim is None:
            return QueuedJobOutcome(False, None, "idle", False)
        job_id = str(claim.job_id)
        layout: TaskLayout | None = None
        journal: EventJournal | None = None
        try:
            heartbeat = self.queue.heartbeat(
                claim,
                lease_seconds=self.lease_seconds,
            )
            if heartbeat.cancel_requested:
                self.queue.advance(
                    claim,
                    JobStatus.CANCELLED,
                    error_code="USER_CANCELLED",
                )
                return QueuedJobOutcome(
                    True,
                    job_id,
                    JobStatus.CANCELLED.value,
                    claim.recovered,
                )

            layout = TaskLayout.from_data_root(self.data_root, job_id)
            request = load_json_contract(layout.request_path, "pipeline-request")
            database_job = self.queue.get_job(claim.job_id)
            if (
                request["job_id"] != job_id
                or request["requested_by"]["user_id"]
                != str(database_job["user_id"])
                or request["attempt"] != claim.attempt
                or request["execution_mode"] != claim.execution_mode
            ):
                raise AdapterError(
                    "queued real request does not match its database claim"
                )
            journal = EventJournal(
                layout.events_path,
                job_id=job_id,
                attempt=claim.attempt,
                worker_id=self.worker_id,
            )
            if not journal.has_terminal_event:
                if journal.events:
                    journal.append(
                        "warning",
                        message_code="WORKER_RESUMED",
                        safe_message="Worker 已在新租约下恢复真实 Re3D 任务。",
                    )
                else:
                    journal.append(
                        "job_started",
                        message_code="JOB_STARTED",
                        safe_message="真实 Re3D 任务已开始。",
                    )

            reporter = _RealProgressReporter(self.queue, claim, journal)
            _advance_to(self.queue, claim, JobStatus.SFM, progress=5)
            with LeaseHeartbeatLoop(
                self.queue,
                claim,
                interval_seconds=self.heartbeat_seconds,
                lease_seconds=self.lease_seconds,
            ) as heartbeat_loop:
                outcome = RealPipelineRunner(
                    layout,
                    re3d_root=self.re3d_root,
                    driver_python=self.driver_python,
                    installation_verifier=self.installation_verifier,
                ).run(
                    cancel_requested=lambda: heartbeat_loop.cancel_requested,
                    health_check=heartbeat_loop.raise_if_failed,
                    on_step=reporter.on_step,
                    poll_seconds=self.process_poll_seconds,
                    timeout_seconds=_remaining_timeout_seconds(
                        database_job["started_at"],
                        request["limits"]["timeout_seconds"],
                    ),
                )
                reporter.finish()
                heartbeat_loop.raise_if_failed()
                _advance_to(
                    self.queue,
                    claim,
                    JobStatus.VALIDATING_OUTPUT,
                    progress=94,
                )
                _advance_to(
                    self.queue,
                    claim,
                    JobStatus.EVALUATING,
                    progress=97,
                )
                RealEvaluator(layout).run()
                heartbeat_loop.raise_if_failed()
            if journal is not None and not journal.has_terminal_event:
                journal.append(
                    "job_succeeded",
                    progress={"percent": 100},
                    message_code="JOB_SUCCEEDED",
                    safe_message=(
                        "真实 Re3D 三分支和结构健康评估已完成。"
                        if not outcome.reused
                        else "已恢复并验证真实 Re3D 结果。"
                    ),
                )
            self.queue.advance(claim, JobStatus.SUCCEEDED, progress=100)
            return QueuedJobOutcome(
                True,
                job_id,
                JobStatus.SUCCEEDED.value,
                claim.recovered,
            )
        except PipelineCancelled:
            self._append_cancelled(journal)
            self.queue.advance(
                claim,
                JobStatus.CANCELLED,
                error_code="USER_CANCELLED",
            )
            return QueuedJobOutcome(
                True,
                job_id,
                JobStatus.CANCELLED.value,
                claim.recovered,
            )
        except LeaseLostError:
            raise
        except PipelineTimedOut as exc:
            cancelled = self._honor_pending_cancellation(claim, journal)
            if cancelled is not None:
                return cancelled
            self._append_failure(
                journal,
                code="REAL_PIPELINE_TIMEOUT",
                category="timeout",
                safe_message="真实重建超过任务时间限制。",
            )
            self._mark_failed(claim, error_code="REAL_PIPELINE_TIMEOUT")
            raise AdapterError(f"queued real pipeline timed out for job {job_id}") from exc
        except (AdapterError, SchedulerError, OSError, ValueError) as exc:
            cancelled = self._honor_pending_cancellation(claim, journal)
            if cancelled is not None:
                return cancelled
            current = self.queue.get_job(claim.job_id)["status"]
            error_code = (
                "REAL_EVALUATION_FAILED"
                if current == JobStatus.EVALUATING
                else "REAL_PIPELINE_FAILED"
            )
            self._append_failure(
                journal,
                code=error_code,
                category="pipeline",
                safe_message="真实重建或产物校验未完成。",
            )
            self._mark_failed(claim, error_code=error_code)
            raise AdapterError(f"queued real pipeline failed for job {job_id}") from exc

    def _honor_pending_cancellation(
        self,
        claim: JobClaim,
        journal: EventJournal | None,
    ) -> QueuedJobOutcome | None:
        """Resolve a cancellation/failure race before writing a failure terminal event."""
        heartbeat = self.queue.heartbeat(
            claim,
            lease_seconds=self.lease_seconds,
        )
        if not heartbeat.cancel_requested:
            return None
        self._append_cancelled(journal)
        self.queue.advance(
            claim,
            JobStatus.CANCELLED,
            error_code="USER_CANCELLED",
        )
        return QueuedJobOutcome(
            True,
            str(claim.job_id),
            JobStatus.CANCELLED.value,
            claim.recovered,
        )

    @staticmethod
    def _append_cancelled(journal: EventJournal | None) -> None:
        if journal is None or journal.has_terminal_event:
            return
        journal.append(
            "job_cancelled",
            message_code="USER_CANCELLED",
            safe_message="任务已按用户请求停止。",
        )

    @staticmethod
    def _append_failure(
        journal: EventJournal | None,
        *,
        code: str,
        category: str,
        safe_message: str,
    ) -> None:
        if journal is None or journal.has_terminal_event:
            return
        journal.append(
            "job_failed",
            message_code=code,
            safe_message=safe_message,
            error={
                "code": code,
                "category": category,
                "retryable": category in {"resource", "timeout"},
                "safe_message": safe_message,
                "diagnostic_ref": "runtime/logs/re3d-run.log",
            },
        )

    def _mark_failed(self, claim: JobClaim, *, error_code: str) -> None:
        try:
            current = self.queue.get_job(claim.job_id)["status"]
            if current in TERMINAL_STATUSES:
                return
            target = (
                JobStatus.FAILED_EVALUATION
                if current == JobStatus.EVALUATING
                else JobStatus.FAILED_PIPELINE
            )
            self.queue.advance(claim, target, error_code=error_code)
        except (LeaseLostError, SchedulerError, ValueError):
            return


class _RealProgressReporter:
    def __init__(
        self,
        queue: JobQueue,
        claim: JobClaim,
        journal: EventJournal,
    ) -> None:
        self.queue = queue
        self.claim = claim
        self.journal = journal
        self.active: tuple[str, str] | None = None
        self.completed_steps = 0

    def on_step(self, step: dict[str, str]) -> None:
        self._complete_active()
        stage = step["platform_stage"]
        branch = step["branch"]
        action = step["action"]
        if action == "run":
            self.journal.append(
                "stage_started",
                stage=stage,
                branch=branch,
                message_code="STAGE_STARTED",
                safe_message=f"Re3D 正在执行 {step['re3d_step']}。",
            )
            self.active = (stage, branch)
        else:
            self.journal.append(
                "stage_completed",
                stage=stage,
                branch=branch,
                message_code="STAGE_REUSED",
                safe_message=f"Re3D 已复用 {step['re3d_step']} 的检查点。",
            )
        self.completed_steps += 1
        self._project_database_progress(step["re3d_step"])

    def finish(self) -> None:
        self._complete_active()

    def _complete_active(self) -> None:
        if self.active is None:
            return
        stage, branch = self.active
        self.journal.append(
            "stage_completed",
            stage=stage,
            branch=branch,
            message_code="STAGE_COMPLETED",
            safe_message=f"Re3D 阶段 {stage} 已完成。",
        )
        self.active = None

    def _project_database_progress(self, re3d_step: str) -> None:
        number = re3d_step.split("-", 1)[0]
        if number in {"01", "02", "03"}:
            _advance_to(self.queue, self.claim, JobStatus.SFM, progress=5)
        elif number == "90":
            _advance_to(
                self.queue,
                self.claim,
                JobStatus.VALIDATING_OUTPUT,
                progress=92,
            )
            return
        elif number != "00":
            _advance_to(
                self.queue,
                self.claim,
                JobStatus.DENSE_RECONSTRUCTION,
                progress=15,
            )
        current = self.queue.get_job(self.claim.job_id)["progress"]
        projected = min(89, 5 + self.completed_steps * 3)
        if projected > current:
            self.queue.update_progress(self.claim, projected)


WORKER_FLOW = (
    JobStatus.PREPARING,
    JobStatus.SFM,
    JobStatus.DENSE_RECONSTRUCTION,
    JobStatus.MESHING,
    JobStatus.TEXTURING,
    JobStatus.VALIDATING_OUTPUT,
    JobStatus.EVALUATING,
)


def _advance_to(
    queue: JobQueue,
    claim: JobClaim,
    target: JobStatus,
    *,
    progress: int,
) -> None:
    snapshot = queue.get_job(claim.job_id)
    current = JobStatus(snapshot["status"])
    if current in TERMINAL_STATUSES:
        return
    current_index = WORKER_FLOW.index(current)
    target_index = WORKER_FLOW.index(target)
    if target_index < current_index:
        if progress > snapshot["progress"]:
            queue.update_progress(claim, progress)
        return
    for status in WORKER_FLOW[current_index + 1 : target_index + 1]:
        snapshot = queue.get_job(claim.job_id)
        next_progress = max(snapshot["progress"], progress)
        queue.advance(claim, status, progress=next_progress)
    snapshot = queue.get_job(claim.job_id)
    if snapshot["status"] == target and progress > snapshot["progress"]:
        queue.update_progress(claim, progress)


def _remaining_timeout_seconds(
    started_at: datetime | None,
    configured_timeout: int,
) -> int:
    if started_at is None:
        return configured_timeout
    normalized = started_at
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    elapsed = max(
        0.0,
        (
            datetime.now(timezone.utc) - normalized.astimezone(timezone.utc)
        ).total_seconds(),
    )
    return max(0, math.ceil(configured_timeout - elapsed))
