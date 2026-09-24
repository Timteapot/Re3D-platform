from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import unicodedata
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image, UnidentifiedImageError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import JobUpload, JobUploadImage, ReconstructionJob
from backend.db.queue import JobQueue
from backend.re3d_adapter.contracts import validate_contract
from backend.re3d_adapter.events import utc_now
from backend.re3d_adapter.io import atomic_write_json, sha256_file
from backend.re3d_adapter.paths import TaskLayout

from .errors import (
    UploadConflictError,
    UploadNotFoundError,
    UploadTooLargeError,
    UploadValidationError,
)
from .settings import UploadSettings


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE_PATH = ROOT / "config" / "pipeline-baseline.json"
CHUNK_BYTES = 1024 * 1024
FORMAT_DETAILS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
}
LOGGER = logging.getLogger(__name__)


class UploadService:
    """Owns an authenticated upload until immutable task submission."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        queue: JobQueue,
        *,
        data_root: Path,
        settings: UploadSettings | None = None,
        baseline_path: Path = DEFAULT_BASELINE_PATH,
    ) -> None:
        self.sessions = session_factory
        self.queue = queue
        self.data_root = data_root.expanduser().resolve()
        self.settings = settings or UploadSettings()
        self.baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    def create(
        self,
        *,
        user_id: uuid.UUID,
        idempotency_token: str,
    ) -> tuple[dict[str, Any], bool]:
        token = idempotency_token.strip()
        if not 8 <= len(token) <= 128:
            raise UploadValidationError(
                "idempotency_key must contain 8 to 128 non-blank characters"
            )
        idempotency_key = hashlib.sha256(
            f"upload:{user_id}:{token}".encode("utf-8")
        ).hexdigest()
        with self.sessions() as session:
            existing = session.execute(
                select(JobUpload).where(
                    JobUpload.user_id == user_id,
                    JobUpload.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _upload_snapshot(session, existing), True

        upload_id = uuid.uuid4()
        layout = TaskLayout.from_data_root(
            self.data_root,
            str(upload_id),
            create=True,
        )
        layout.resolve("input/images").mkdir(parents=True)
        layout.resolve("input/staging").mkdir(parents=True)
        try:
            with self.sessions.begin() as session:
                now = _database_now(session)
                session.add(
                    JobUpload(
                        id=upload_id,
                        user_id=user_id,
                        status="uploading",
                        idempotency_key=idempotency_key,
                        image_count=0,
                        total_bytes=0,
                        updated_at=now,
                    )
                )
        except DatabaseIntegrityError:
            _remove_unsubmitted_layout(layout)
            with self.sessions() as session:
                existing = session.execute(
                    select(JobUpload).where(
                        JobUpload.user_id == user_id,
                        JobUpload.idempotency_key == idempotency_key,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise UploadConflictError("upload identity conflict")
                return _upload_snapshot(session, existing), True
        return self.get(upload_id=upload_id, user_id=user_id), False

    def get(
        self,
        *,
        upload_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        with self.sessions() as session:
            upload = session.execute(
                select(JobUpload).where(
                    JobUpload.id == upload_id,
                    JobUpload.user_id == user_id,
                )
            ).scalar_one_or_none()
            if upload is None:
                raise UploadNotFoundError("upload does not exist")
            return _upload_snapshot(session, upload)

    def add_image(
        self,
        *,
        upload_id: uuid.UUID,
        user_id: uuid.UUID,
        source: BinaryIO,
        original_name: str | None,
    ) -> dict[str, Any]:
        current = self.get(upload_id=upload_id, user_id=user_id)
        if current["status"] != "uploading":
            raise UploadConflictError(
                "images cannot be added after upload submission or cancellation"
            )
        layout = TaskLayout.from_data_root(self.data_root, str(upload_id))
        staging_path = layout.resolve(f"input/staging/{uuid.uuid4().hex}.part")
        destination: Path | None = None
        try:
            size_bytes, digest = _copy_limited(
                source,
                staging_path,
                max_bytes=self.settings.max_file_bytes,
            )
            image_format, width, height = _inspect_image(
                staging_path,
                max_pixels=self.settings.max_pixels,
            )
            extension, content_type = FORMAT_DETAILS[image_format]
            safe_original_name = _safe_display_name(original_name, extension)

            try:
                with self.sessions.begin() as session:
                    upload = _locked_upload(session, upload_id, user_id)
                    if upload.status != "uploading":
                        raise UploadConflictError(
                            "images cannot be added after upload submission"
                        )
                    if upload.image_count >= self.settings.max_images:
                        raise UploadValidationError(
                            f"an upload accepts at most {self.settings.max_images} images"
                        )
                    if upload.total_bytes + size_bytes > self.settings.max_total_bytes:
                        raise UploadTooLargeError("upload total byte limit exceeded")
                    duplicate = session.execute(
                        select(JobUploadImage.id).where(
                            JobUploadImage.upload_id == upload_id,
                            JobUploadImage.sha256 == digest,
                        )
                    ).scalar_one_or_none()
                    if duplicate is not None:
                        raise UploadConflictError(
                            "the same image content is already in this upload"
                        )
                    last_sequence = session.execute(
                        select(func.max(JobUploadImage.sequence)).where(
                            JobUploadImage.upload_id == upload_id
                        )
                    ).scalar_one()
                    sequence = 0 if last_sequence is None else last_sequence + 1
                    stored_name = (
                        f"{sequence:06d}-{uuid.uuid4().hex[:12]}{extension}"
                    )
                    destination = layout.resolve(f"input/images/{stored_name}")
                    os.replace(staging_path, destination)
                    session.add(
                        JobUploadImage(
                            id=uuid.uuid4(),
                            upload_id=upload_id,
                            sequence=sequence,
                            stored_name=stored_name,
                            original_name=safe_original_name,
                            content_type=content_type,
                            size_bytes=size_bytes,
                            sha256=digest,
                            width=width,
                            height=height,
                        )
                    )
                    upload.image_count += 1
                    upload.total_bytes += size_bytes
                    upload.updated_at = _database_now(session)
                    session.flush()
            except DatabaseIntegrityError as exc:
                if destination is not None:
                    destination.unlink(missing_ok=True)
                raise UploadConflictError("uploaded image identity conflict") from exc
            except Exception:
                if destination is not None:
                    destination.unlink(missing_ok=True)
                raise
        finally:
            staging_path.unlink(missing_ok=True)
        return self.get(upload_id=upload_id, user_id=user_id)

    def delete_image(
        self,
        *,
        upload_id: uuid.UUID,
        image_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        source_path: Path | None = None
        tombstone_path: Path | None = None
        try:
            with self.sessions.begin() as session:
                upload = _locked_upload(session, upload_id, user_id)
                if upload.status != "uploading":
                    raise UploadConflictError(
                        "images can only be deleted from an active upload"
                    )
                image = session.execute(
                    select(JobUploadImage).where(
                        JobUploadImage.id == image_id,
                        JobUploadImage.upload_id == upload.id,
                    )
                ).scalar_one_or_none()
                if image is None:
                    raise UploadNotFoundError("uploaded image does not exist")

                layout = TaskLayout.from_data_root(
                    self.data_root,
                    str(upload.id),
                )
                source_path = layout.resolve(f"input/images/{image.stored_name}")
                if source_path.is_symlink() or not source_path.is_file():
                    raise UploadConflictError(
                        "uploaded image file is missing or unsafe"
                    )
                staging = layout.resolve("input/staging")
                staging.mkdir(exist_ok=True)
                tombstone_path = staging / f"delete-{uuid.uuid4().hex}.part"
                os.replace(source_path, tombstone_path)

                upload.image_count -= 1
                upload.total_bytes -= image.size_bytes
                upload.updated_at = _database_now(session)
                session.delete(image)
                session.flush()
        except Exception:
            if (
                source_path is not None
                and tombstone_path is not None
                and tombstone_path.is_file()
                and not source_path.exists()
            ):
                os.replace(tombstone_path, source_path)
            raise

        if tombstone_path is not None:
            try:
                tombstone_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning(
                    "Could not remove deleted upload image tombstone %s",
                    tombstone_path,
                )
        return self.get(upload_id=upload_id, user_id=user_id)

    def cancel(
        self,
        *,
        upload_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            upload = _locked_upload(session, upload_id, user_id)
            if upload.status == "submitted":
                raise UploadConflictError(
                    "submitted uploads must be cancelled through the job API"
                )
            if upload.status == "uploading":
                _mark_upload_cancelled(session, upload, reason="user")

        storage_removed = self._remove_cancelled_storage(upload_id)
        snapshot = self.get(upload_id=upload_id, user_id=user_id)
        snapshot["storage_removed"] = storage_removed
        return snapshot

    def cleanup_stale(
        self,
        *,
        now: datetime | None = None,
        stale_after_hours: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        configured_hours = (
            self.settings.stale_after_hours
            if stale_after_hours is None
            else stale_after_hours
        )
        configured_limit = (
            self.settings.cleanup_batch_size if limit is None else limit
        )
        if not 1 <= configured_hours <= 24 * 30:
            raise ValueError("stale_after_hours must be between 1 and 720")
        if not 1 <= configured_limit <= 1000:
            raise ValueError("cleanup limit must be between 1 and 1000")
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(hours=configured_hours)

        with self.sessions() as session:
            candidate_ids = list(
                session.execute(
                    select(JobUpload.id)
                    .where(_cleanup_candidate_filter(cutoff))
                    .order_by(JobUpload.updated_at, JobUpload.id)
                    .limit(configured_limit)
                ).scalars()
            )

        scanned = 0
        expired = 0
        storage_cleaned = 0
        failures: list[str] = []
        for upload_id in candidate_ids:
            with self.sessions.begin() as session:
                upload = session.execute(
                    select(JobUpload)
                    .where(
                        JobUpload.id == upload_id,
                        _cleanup_candidate_filter(cutoff),
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if upload is None:
                    continue
                scanned += 1
                if upload.status == "uploading":
                    _mark_upload_cancelled(session, upload, reason="expired")
                    expired += 1

            if self._remove_cancelled_storage(upload_id):
                storage_cleaned += 1
            else:
                failures.append(str(upload_id))

        return {
            "scanned": scanned,
            "expired": expired,
            "storage_cleaned": storage_cleaned,
            "storage_cleanup_failures": failures,
            "cutoff": cutoff.isoformat(),
        }

    def _remove_cancelled_storage(self, upload_id: uuid.UUID) -> bool:
        try:
            _remove_upload_directory(self.data_root, upload_id)
        except OSError:
            LOGGER.exception(
                "Could not remove cancelled upload directory for %s",
                upload_id,
            )
            return False

        with self.sessions.begin() as session:
            upload = session.execute(
                select(JobUpload)
                .where(JobUpload.id == upload_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                upload is not None
                and upload.status == "cancelled"
                and upload.storage_cleaned_at is None
            ):
                upload.storage_cleaned_at = _database_now(session)
                session.flush()
        return True

    def submit(
        self,
        *,
        upload_id: uuid.UUID,
        user_id: uuid.UUID,
        execution_mode: str = "simulated",
    ) -> dict[str, Any]:
        if execution_mode not in {"simulated", "real"}:
            raise UploadValidationError(
                "execution_mode must be simulated or real"
            )
        try:
            with self.sessions.begin() as session:
                upload = _locked_upload(session, upload_id, user_id)
                if upload.status == "submitted":
                    existing = session.get(ReconstructionJob, upload.id)
                    if existing is None:
                        raise UploadConflictError(
                            "submitted upload is missing its queued job"
                        )
                    if existing.execution_mode != execution_mode:
                        raise UploadConflictError(
                            "upload was already submitted with a different execution mode"
                        )
                    return _job_snapshot(existing)
                if upload.status != "uploading":
                    raise UploadConflictError("upload cannot be submitted")
                images = list(
                    session.execute(
                        select(JobUploadImage)
                        .where(JobUploadImage.upload_id == upload.id)
                        .order_by(JobUploadImage.sequence)
                    ).scalars()
                )
                if len(images) < self.settings.min_images:
                    raise UploadValidationError(
                        f"at least {self.settings.min_images} valid images are required"
                    )
                if len(images) != upload.image_count:
                    raise UploadConflictError("upload image count is inconsistent")

                layout = TaskLayout.from_data_root(self.data_root, str(upload.id))
                manifest = _build_manifest(layout, images, upload.total_bytes)
                manifest_path = layout.resolve("input/input-manifest.json")
                atomic_write_json(manifest_path, manifest)
                manifest_sha256 = sha256_file(manifest_path)
                queue_key = hashlib.sha256(
                    f"upload-submit:{upload.id}".encode("ascii")
                ).hexdigest()
                request = self._build_request(
                    upload=upload,
                    manifest_sha256=manifest_sha256,
                    queue_key=queue_key,
                    execution_mode=execution_mode,
                )
                validate_contract("pipeline-request", request)
                atomic_write_json(layout.request_path, request)
                self.queue.enqueue_in_session(
                    session,
                    job_id=upload.id,
                    user_id=user_id,
                    execution_mode=execution_mode,
                    pipeline_tag=request["pipeline"]["tag"],
                    pipeline_commit=request["pipeline"]["commit"],
                    config_sha256=request["pipeline"]["config_sha256"],
                    input_manifest_sha256=manifest_sha256,
                    idempotency_key=queue_key,
                )
                now = _database_now(session)
                upload.status = "submitted"
                upload.submitted_at = now
                upload.updated_at = now
                session.flush()
        except DatabaseIntegrityError as exc:
            raise UploadConflictError("upload submission identity conflict") from exc
        return self.queue.get_job(upload_id, user_id=user_id)

    def _build_request(
        self,
        *,
        upload: JobUpload,
        manifest_sha256: str,
        queue_key: str,
        execution_mode: str,
    ) -> dict[str, Any]:
        return {
            "contract_version": "1.0",
            "execution_mode": execution_mode,
            "request_id": str(uuid.uuid4()),
            "job_id": str(upload.id),
            "attempt": 1,
            "created_at": utc_now(),
            "requested_by": {"user_id": str(upload.user_id)},
            "pipeline": {
                "name": "Re3D",
                "tag": self.baseline["tag"],
                "commit": self.baseline["commit"],
                "config_sha256": self.baseline["pipeline_config"]["sha256"],
            },
            "input": {
                "images_path": "input/images",
                "manifest_path": "input/input-manifest.json",
                "manifest_sha256": manifest_sha256,
                "image_count": upload.image_count,
                "total_bytes": upload.total_bytes,
            },
            "branches": self.baseline["default_branches"],
            "storage": {
                "work_path": "runtime/work",
                "logs_path": "runtime/logs",
                "output_path": "output",
                "reports_path": "reports",
                "manifests_path": "manifests",
            },
            "limits": {
                "timeout_seconds": 3600,
                "max_disk_bytes": max(
                    1024 * 1024 * 1024,
                    self.settings.max_total_bytes,
                ),
                "gpu_concurrency": 1,
            },
            "idempotency_key": queue_key,
        }


def _copy_limited(
    source: BinaryIO,
    destination: Path,
    *,
    max_bytes: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with destination.open("xb") as output:
        while True:
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UploadTooLargeError("image exceeds the per-file byte limit")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if total == 0:
        raise UploadValidationError("empty files are not accepted")
    return total, digest.hexdigest()


def _inspect_image(path: Path, *, max_pixels: int) -> tuple[str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                frames = getattr(image, "n_frames", 1)
                if image_format not in FORMAT_DETAILS:
                    raise UploadValidationError("only JPEG and PNG images are accepted")
                if frames != 1:
                    raise UploadValidationError(
                        "animated or multi-frame images are not accepted"
                    )
                if width * height > max_pixels:
                    raise UploadValidationError(
                        "image pixel count exceeds the configured limit"
                    )
                image.verify()
            with Image.open(path) as decoded:
                decoded.load()
    except UploadValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise UploadValidationError("file is not a decodable JPEG or PNG image") from exc
    return image_format, width, height


def _safe_display_name(value: str | None, extension: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    basename = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in basename if ord(character) >= 32)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = f"image{extension}"
    return cleaned[:255]


def _build_manifest(
    layout: TaskLayout,
    images: list[JobUploadImage],
    expected_total_bytes: int,
) -> dict[str, Any]:
    images_path = layout.resolve("input/images")
    expected_names = [image.stored_name for image in images]
    actual_entries = list(images_path.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in actual_entries):
        raise UploadValidationError("input directory contains an unexpected entry")
    if sorted(path.name for path in actual_entries) != sorted(expected_names):
        raise UploadValidationError("stored images do not match upload metadata")

    records: list[dict[str, Any]] = []
    total_bytes = 0
    for image in images:
        path = layout.resolve(f"input/images/{image.stored_name}")
        size = path.stat().st_size
        if size != image.size_bytes or sha256_file(path) != image.sha256:
            raise UploadValidationError("an uploaded image changed before submission")
        total_bytes += size
        records.append(
            {
                "name": image.stored_name,
                "original_name": image.original_name,
                "sha256": image.sha256,
                "size_bytes": image.size_bytes,
                "content_type": image.content_type,
                "width": image.width,
                "height": image.height,
            }
        )
    if total_bytes != expected_total_bytes:
        raise UploadValidationError("upload byte count is inconsistent")
    return {
        "schema_version": "1.0",
        "source": "user_upload",
        "simulation": False,
        "images": records,
    }


def _locked_upload(
    session: Session,
    upload_id: uuid.UUID,
    user_id: uuid.UUID,
) -> JobUpload:
    upload = session.execute(
        select(JobUpload)
        .where(JobUpload.id == upload_id, JobUpload.user_id == user_id)
        .with_for_update()
    ).scalar_one_or_none()
    if upload is None:
        raise UploadNotFoundError("upload does not exist")
    return upload


def _mark_upload_cancelled(
    session: Session,
    upload: JobUpload,
    *,
    reason: str,
) -> None:
    if reason not in {"user", "expired"}:
        raise ValueError("unsupported upload cancellation reason")
    images = list(
        session.execute(
            select(JobUploadImage).where(JobUploadImage.upload_id == upload.id)
        ).scalars()
    )
    for image in images:
        session.delete(image)
    now = _database_now(session)
    upload.status = "cancelled"
    upload.image_count = 0
    upload.total_bytes = 0
    upload.cancelled_at = now
    upload.cancellation_reason = reason
    upload.storage_cleaned_at = None
    upload.updated_at = now
    session.flush()


def _cleanup_candidate_filter(cutoff: datetime):
    return or_(
        and_(
            JobUpload.status == "uploading",
            JobUpload.updated_at < cutoff,
        ),
        and_(
            JobUpload.status == "cancelled",
            JobUpload.storage_cleaned_at.is_(None),
        ),
    )


def _upload_snapshot(session: Session, upload: JobUpload) -> dict[str, Any]:
    images = list(
        session.execute(
            select(JobUploadImage)
            .where(JobUploadImage.upload_id == upload.id)
            .order_by(JobUploadImage.sequence)
        ).scalars()
    )
    return {
        "upload_id": upload.id,
        "status": upload.status,
        "image_count": upload.image_count,
        "total_bytes": upload.total_bytes,
        "created_at": upload.created_at,
        "updated_at": upload.updated_at,
        "submitted_at": upload.submitted_at,
        "cancelled_at": upload.cancelled_at,
        "cancellation_reason": upload.cancellation_reason,
        "storage_cleaned_at": upload.storage_cleaned_at,
        "images": [
            {
                "id": image.id,
                "original_name": image.original_name,
                "content_type": image.content_type,
                "size_bytes": image.size_bytes,
                "sha256": image.sha256,
                "width": image.width,
                "height": image.height,
            }
            for image in images
        ],
    }


def _job_snapshot(job: ReconstructionJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "user_id": job.user_id,
        "status": job.status,
        "execution_mode": job.execution_mode,
        "attempt": job.attempt,
        "progress": job.progress,
        "cancel_requested": job.cancel_requested,
        "error_code": job.error_code,
        "created_at": job.created_at,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "version": job.version,
    }


def _database_now(session: Session) -> datetime:
    return session.execute(select(func.current_timestamp())).scalar_one()


def _remove_unsubmitted_layout(layout: TaskLayout) -> None:
    if layout.root.is_dir() and layout.root.parent == layout.jobs_root:
        shutil.rmtree(layout.root)


def _remove_upload_directory(data_root: Path, upload_id: uuid.UUID) -> None:
    jobs_root = (data_root.expanduser().resolve() / "jobs").resolve()
    candidate = jobs_root / str(upload_id)
    if not candidate.exists() and not candidate.is_symlink():
        return
    resolved = candidate.resolve()
    if (
        candidate.is_symlink()
        or not candidate.is_dir()
        or resolved.parent != jobs_root
    ):
        raise OSError("upload cleanup target is not a safe task directory")
    shutil.rmtree(candidate)
