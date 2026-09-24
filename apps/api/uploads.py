from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from apps.api.auth import CurrentUserDependency
from backend.auth import UserIdentity
from backend.uploads import (
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


class UploadSubmitRequest(BaseModel):
    execution_mode: Literal["simulated", "real"] = "simulated"


def create_upload_router(
    service: UploadService,
    current_user: CurrentUserDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])

    @router.post(
        "",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_upload(
        payload: UploadCreateRequest,
        response: Response,
        user: UserIdentity = Depends(current_user),
    ) -> UploadResponse:
        try:
            snapshot, reused = service.create(
                user_id=user.id,
                idempotency_token=payload.idempotency_key,
            )
        except UploadValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if reused:
            response.status_code = status.HTTP_200_OK
        return UploadResponse(**snapshot, reused=reused)

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
        user: UserIdentity = Depends(current_user),
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
        user: UserIdentity = Depends(current_user),
    ) -> SubmittedJobResponse:
        try:
            job = service.submit(
                upload_id=upload_id,
                user_id=user.id,
                execution_mode=(
                    payload.execution_mode if payload is not None else "simulated"
                ),
            )
        except UploadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="upload not found") from exc
        except UploadConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UploadValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return SubmittedJobResponse(
            job_id=job["id"],
            status=job["status"],
            execution_mode=job["execution_mode"],
            progress=job["progress"],
            created_at=job["created_at"],
            queued_at=job["queued_at"],
        )

    return router
