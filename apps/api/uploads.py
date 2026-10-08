from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from apps.api.auth import CurrentUserDependency, VerifiedUserDependency
from backend.auth import UserIdentity
from backend.jobs import TaskSubmissionLimitError
from backend.uploads import (
    StorageQuotaExceededError,
    UploadCapacityError,
    UploadConflictError,
    UploadNotFoundError,
    UploadService,
    UploadTooLargeError,
    UploadValidationError,
)


class UploadCreateRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)


class UploadedImageResponse(BaseModel):
    id: uuid.UUID
    original_name: str
    content_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int


class UploadResponse(BaseModel):
    upload_id: uuid.UUID
    status: str
    image_count: int
    total_bytes: int
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    storage_cleaned_at: datetime | None
    images: list[UploadedImageResponse]
    reused: bool = False


class UploadCancellationResponse(UploadResponse):
    storage_removed: bool


class SubmittedJobResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    execution_mode: str
    progress: int
    created_at: datetime
    queued_at: datetime


class StorageQuotaResponse(BaseModel):
    quota_bytes: int
    used_bytes: int
    remaining_bytes: int
    over_quota_bytes: int
    upload_bytes: int
    reserved_job_bytes: int
    job_reservation_bytes: int


ExecutionMode = Literal["simulated", "real"]


class UploadSubmitRequest(BaseModel):
    execution_mode: ExecutionMode | None = None


def create_upload_router(
    service: UploadService,
    current_user: CurrentUserDependency,
    verified_user: VerifiedUserDependency,
    *,
    default_execution_mode: ExecutionMode = "simulated",
    allowed_execution_modes: frozenset[ExecutionMode] = frozenset(
        {"simulated", "real"}
    ),
) -> APIRouter:
    if default_execution_mode not in allowed_execution_modes:
        raise ValueError("default execution mode must be allowed")
    router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])

    @router.post(
        "",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_upload(
        payload: UploadCreateRequest,
        response: Response,
        user: UserIdentity = Depends(verified_user),
    ) -> UploadResponse:
        try:
            snapshot, reused = service.create(
                user_id=user.id,
                idempotency_token=payload.idempotency_key,
            )
        except UploadValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UploadCapacityError as exc:
            raise _storage_capacity_http_exception(exc) from exc
        if reused:
            response.status_code = status.HTTP_200_OK
        return UploadResponse(**snapshot, reused=reused)

    @router.get("/quota", response_model=StorageQuotaResponse)
    def get_storage_quota(
        response: Response,
        user: UserIdentity = Depends(current_user),
    ) -> StorageQuotaResponse:
        response.headers["Cache-Control"] = "no-store"
        return StorageQuotaResponse(**service.storage_quota(user_id=user.id))

    @router.get("/{upload_id}", response_model=UploadResponse)
    def get_upload(
        upload_id: uuid.UUID,
        user: UserIdentity = Depends(current_user),
    ) -> UploadResponse:
        try:
            return UploadResponse(
                **service.get(upload_id=upload_id, user_id=user.id)
            )
        except UploadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="upload not found") from exc

    @router.post(
        "/{upload_id}/images",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def add_image(
        upload_id: uuid.UUID,
        file: UploadFile = File(...),
        user: UserIdentity = Depends(verified_user),
    ) -> UploadResponse:
        try:
            snapshot = service.add_image(
                upload_id=upload_id,
                user_id=user.id,
                source=file.file,
                original_name=file.filename,
            )
            return UploadResponse(**snapshot)
        except UploadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="upload not found") from exc
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except UploadConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UploadValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UploadCapacityError as exc:
            raise _storage_capacity_http_exception(exc) from exc
        except StorageQuotaExceededError as exc:
            raise _storage_quota_http_exception(exc) from exc
        finally:
            file.file.close()

    @router.delete(
        "/{upload_id}/images/{image_id}",
        response_model=UploadResponse,
    )
    def delete_image(
        upload_id: uuid.UUID,
        image_id: uuid.UUID,
        user: UserIdentity = Depends(current_user),
    ) -> UploadResponse:
        try:
            snapshot = service.delete_image(
                upload_id=upload_id,
                image_id=image_id,
                user_id=user.id,
            )
            return UploadResponse(**snapshot)
        except UploadNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="upload or image not found",
            ) from exc
        except UploadConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/{upload_id}/cancel",
        response_model=UploadCancellationResponse,
    )
    def cancel_upload(
        upload_id: uuid.UUID,
        user: UserIdentity = Depends(current_user),
    ) -> UploadCancellationResponse:
        try:
            snapshot = service.cancel(upload_id=upload_id, user_id=user.id)
            return UploadCancellationResponse(**snapshot)
        except UploadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="upload not found") from exc
        except UploadConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/{upload_id}/submit",
        response_model=SubmittedJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_upload(
        upload_id: uuid.UUID,
        payload: UploadSubmitRequest | None = None,
        user: UserIdentity = Depends(verified_user),
    ) -> SubmittedJobResponse:
        execution_mode = (
            payload.execution_mode
            if payload is not None and payload.execution_mode is not None
            else default_execution_mode
        )
        if execution_mode not in allowed_execution_modes:
            raise HTTPException(
                status_code=422,
                detail="execution mode is unavailable in this environment",
            )
        try:
            job = service.submit(
                upload_id=upload_id,
                user_id=user.id,
                execution_mode=execution_mode,
            )
        except UploadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="upload not found") from exc
        except UploadConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UploadValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TaskSubmissionLimitError as exc:
            headers = {"X-Re3D-Error-Code": exc.reason_code}
            if exc.retry_after_seconds is not None:
                headers["Retry-After"] = str(exc.retry_after_seconds)
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers=headers,
            ) from exc
        except UploadCapacityError as exc:
            raise _storage_capacity_http_exception(exc) from exc
        except StorageQuotaExceededError as exc:
            raise _storage_quota_http_exception(exc) from exc
        return SubmittedJobResponse(
            job_id=job["id"],
            status=job["status"],
            execution_mode=job["execution_mode"],
            progress=job["progress"],
            created_at=job["created_at"],
            queued_at=job["queued_at"],
        )

    return router


def _storage_capacity_http_exception(exc: UploadCapacityError) -> HTTPException:
    return HTTPException(
        status_code=507,
        detail=str(exc),
        headers={
            "X-Re3D-Error-Code": "STORAGE_CAPACITY_FLOOR_REACHED",
            "Retry-After": "300",
            "Cache-Control": "no-store",
        },
    )


def _storage_quota_http_exception(
    exc: StorageQuotaExceededError,
) -> HTTPException:
    return HTTPException(
        status_code=507,
        detail=str(exc),
        headers={
            "X-Re3D-Error-Code": exc.reason_code,
            "Cache-Control": "no-store",
        },
    )
