from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.db.errors import LeaseLostError, SchedulerError
from backend.db.heartbeat import LeaseHeartbeatLoop
from backend.db.queue import JobClaim, JobQueue
from backend.db.state_machine import JobStatus, TERMINAL_STATUSES
from backend.evaluation import SimulationEvaluator
from backend.re3d_adapter.errors import AdapterError
from backend.re3d_adapter.paths import TaskLayout
from backend.re3d_adapter.simulation import SimulationRunner


@dataclass(frozen=True)
class QueuedSimulationOutcome:
    claimed: bool
    job_id: str | None
    status: str
    recovered: bool


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
