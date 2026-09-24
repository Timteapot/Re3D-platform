from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from backend.auth import UserIdentity
from backend.db.errors import JobNotFoundError, QueueConflictError
from backend.db.queue import JobQueue
from backend.db.state_machine import JobStatus, TERMINAL_STATUSES
from backend.jobs.details import JobDetailReader, JobDetailSnapshot
from backend.jobs.development import DevelopmentJobService


CurrentUserDependency = Callable[..., UserIdentity]


class SimulatedJobCreate(BaseModel):
    image_count: int = Field(default=3, ge=3, le=150)
    idempotency_key: str = Field(min_length=8, max_length=128)


class JobResponse(BaseModel):
    job_id: uuid.UUID
    user_id: uuid.UUID
    status: JobStatus
    execution_mode: str
    attempt: int
    progress: int
    cancel_requested: bool
    error_code: str | None
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    version: int
    reused: bool = False

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        reused: bool = False,
    ) -> "JobResponse":
        return cls(
            job_id=snapshot["id"],
            user_id=snapshot["user_id"],
            status=snapshot["status"],
            execution_mode=snapshot["execution_mode"],
            attempt=snapshot["attempt"],
            progress=snapshot["progress"],
            cancel_requested=snapshot["cancel_requested"],
            error_code=snapshot["error_code"],
            created_at=snapshot["created_at"],
            queued_at=snapshot["queued_at"],
            started_at=snapshot["started_at"],
            finished_at=snapshot["finished_at"],
            version=snapshot["version"],
            reused=reused,
        )


class JobInputSummary(BaseModel):
    image_count: int
    total_bytes: int
    branches: list[str]


class ArtifactSummary(BaseModel):
    kind: str
    size_bytes: int
    content_type: str


class ResultBranchSummary(BaseModel):
    name: str
    status: str
    duration_seconds: float
    artifacts: list[ArtifactSummary]
    metrics: dict[str, int | float | bool]


class ResultSummary(BaseModel):
    status: str
    execution_mode: str
    duration_seconds: float
    branches: list[ResultBranchSummary]


class EvaluationBranchSummary(BaseModel):
    name: str
    status: str
    score: float | None
    summary: str
    artifact_status: str | None


class EvaluationSummary(BaseModel):
    scope: str
    rules_version: str
    overall_status: str
    score: float | None
    summary: str
    branches: list[EvaluationBranchSummary]
    limitations: list[str]


class JobDetailResponse(BaseModel):
    job: JobResponse
    detail_state: Literal[
        "pending", "available", "not_available", "missing", "invalid"
    ]
    warning_code: str | None
    input: JobInputSummary | None
    result: ResultSummary | None
    evaluation: EvaluationSummary | None

    @classmethod
    def from_detail(cls, detail: JobDetailSnapshot) -> "JobDetailResponse":
        request = detail.request
        result = detail.result
        evaluation = detail.evaluation
        return cls(
            job=JobResponse.from_snapshot(detail.job),
            detail_state=detail.detail_state,
            warning_code=detail.warning_code,
            input=(
                JobInputSummary(
                    image_count=request["input"]["image_count"],
                    total_bytes=request["input"]["total_bytes"],
                    branches=request["branches"],
                )
                if request is not None
                else None
            ),
            result=_summarize_result(result) if result is not None else None,
            evaluation=(
                _summarize_evaluation(evaluation)
                if evaluation is not None
                else None
            ),
        )


def _summarize_result(result: dict[str, Any]) -> ResultSummary:
    return ResultSummary(
        status=result["status"],
        execution_mode=result["execution_mode"],
        duration_seconds=result["duration_seconds"],
        branches=[
            ResultBranchSummary(
                name=name,
                status=branch["status"],
                duration_seconds=branch["duration_seconds"],
                artifacts=[
                    ArtifactSummary(
                        kind=artifact["kind"],
                        size_bytes=artifact["size_bytes"],
                        content_type=artifact["content_type"],
                    )
                    for artifact in branch["artifacts"]
                ],
                metrics={
                    key: value
                    for key, value in branch["metrics"].items()
                    if isinstance(value, (bool, int, float))
                },
            )
            for name, branch in result["branches"].items()
        ],
    )


def _summarize_evaluation(evaluation: dict[str, Any]) -> EvaluationSummary:
    return EvaluationSummary(
        scope=evaluation["scope"],
        rules_version=evaluation["rules_version"],
        overall_status=evaluation["overall"]["status"],
        score=evaluation["overall"]["score"],
        summary=evaluation["overall"]["summary"],
        branches=[
            EvaluationBranchSummary(
                name=name,
                status=branch["status"],
                score=branch["score"],
                summary=branch["summary"],
                artifact_status=(
                    branch.get("dimensions", {})
                    .get("artifacts", {})
                    .get("status")
                ),
            )
            for name, branch in evaluation["branches"].items()
        ],
        limitations=evaluation["limitations"],
    )


def encode_job_event(snapshot: dict[str, Any]) -> str:
    job = JobResponse.from_snapshot(snapshot)
    data = json.dumps(job.model_dump(mode="json"), ensure_ascii=False)
    return f"id: {job.version}\nevent: job\ndata: {data}\n\n"


def create_development_job_router(
    queue: JobQueue,
    development_jobs: DevelopmentJobService,
    current_user: CurrentUserDependency,
    *,
    poll_seconds: float = 1.0,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/development", tags=["development"])
    details = JobDetailReader(queue, data_root=development_jobs.data_root)

    @router.post(
        "/simulated-jobs",
        response_model=JobResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_simulated_job(
        payload: SimulatedJobCreate,
        response: Response,
        user: UserIdentity = Depends(current_user),
    ) -> JobResponse:
        try:
            created = development_jobs.create_simulated_job(
                user_id=user.id,
                image_count=payload.image_count,
                idempotency_token=payload.idempotency_key,
            )
        except QueueConflictError as exc:
            raise HTTPException(status_code=409, detail="job identity conflict") from exc
        if created.reused:
            response.status_code = status.HTTP_200_OK
        return JobResponse.from_snapshot(created.snapshot, reused=created.reused)

    @router.get("/jobs", response_model=list[JobResponse])
    def list_jobs(
        limit: int = 50,
        user: UserIdentity = Depends(current_user),
    ) -> list[JobResponse]:
        if not 1 <= limit <= 100:
            raise HTTPException(
                status_code=422,
                detail="limit must be between 1 and 100",
            )
        return [
            JobResponse.from_snapshot(snapshot)
            for snapshot in queue.list_jobs(user_id=user.id, limit=limit)
        ]

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(
        job_id: uuid.UUID,
        user: UserIdentity = Depends(current_user),
    ) -> JobResponse:
        try:
            return JobResponse.from_snapshot(
                queue.get_job(job_id, user_id=user.id)
            )
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @router.get("/jobs/{job_id}/detail", response_model=JobDetailResponse)
    def get_job_detail(
        job_id: uuid.UUID,
        user: UserIdentity = Depends(current_user),
    ) -> JobDetailResponse:
        try:
            return JobDetailResponse.from_detail(
                details.read(job_id, user_id=user.id)
            )
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
    def cancel_job(
        job_id: uuid.UUID,
        user: UserIdentity = Depends(current_user),
    ) -> JobResponse:
        try:
            queue.request_cancel(job_id, user_id=user.id)
            snapshot = queue.get_job(job_id, user_id=user.id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        return JobResponse.from_snapshot(snapshot)

    @router.get("/jobs/{job_id}/events")
    async def stream_job_events(
        job_id: uuid.UUID,
        request: Request,
        user: UserIdentity = Depends(current_user),
    ) -> StreamingResponse:
        try:
            initial = await run_in_threadpool(
                lambda: queue.get_job(job_id, user_id=user.id)
            )
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

        async def events():
            snapshot = initial
            last_version = -1
            heartbeat_elapsed = 0.0
            while True:
                if await request.is_disconnected():
                    return
                if snapshot["version"] != last_version:
                    yield encode_job_event(snapshot)
                    last_version = snapshot["version"]
                    heartbeat_elapsed = 0.0
                if JobStatus(snapshot["status"]) in TERMINAL_STATUSES:
                    return
                await asyncio.sleep(poll_seconds)
                heartbeat_elapsed += poll_seconds
                if heartbeat_elapsed >= 15:
                    yield ": keep-alive\n\n"
                    heartbeat_elapsed = 0.0
                try:
                    snapshot = await run_in_threadpool(
                        lambda: queue.get_job(job_id, user_id=user.id)
                    )
                except JobNotFoundError:
                    return

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return router
