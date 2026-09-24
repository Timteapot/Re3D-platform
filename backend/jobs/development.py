from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.db.errors import QueueConflictError
from backend.db.queue import JobQueue
from backend.re3d_adapter.contracts import validate_contract
from backend.re3d_adapter.events import utc_now
from backend.re3d_adapter.io import atomic_write_bytes, atomic_write_json, sha256_file
from backend.re3d_adapter.paths import TaskLayout


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE_PATH = ROOT / "config" / "pipeline-baseline.json"


@dataclass(frozen=True)
class CreatedDevelopmentJob:
    snapshot: dict[str, Any]
    reused: bool


class DevelopmentJobService:
    """Create synthetic simulation tasks for the local API/Worker integration loop."""

    def __init__(
        self,
        queue: JobQueue,
        *,
        data_root: Path,
        baseline_path: Path = DEFAULT_BASELINE_PATH,
    ) -> None:
        self.queue = queue
        self.data_root = data_root.expanduser().resolve()
        self.baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    def create_simulated_job(
        self,
        *,
        user_id: uuid.UUID,
        image_count: int,
        idempotency_token: str,
    ) -> CreatedDevelopmentJob:
        if not 3 <= image_count <= 150:
            raise ValueError("image_count must be between 3 and 150")
        if not 8 <= len(idempotency_token) <= 128:
            raise ValueError("idempotency_token must contain 8 to 128 characters")
        idempotency_key = hashlib.sha256(
            f"{user_id}:{idempotency_token}".encode("utf-8")
        ).hexdigest()
        existing = self.queue.get_job_by_idempotency(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return CreatedDevelopmentJob(existing, reused=True)

        job_id = uuid.uuid4()
        request_id = uuid.uuid4()
        layout = TaskLayout.from_data_root(self.data_root, str(job_id), create=True)
        try:
            request = self._write_task_files(
                layout,
                user_id=user_id,
                request_id=request_id,
                image_count=image_count,
                idempotency_key=idempotency_key,
            )
            self.queue.enqueue(
                job_id=job_id,
                user_id=user_id,
                execution_mode="simulated",
                pipeline_tag=request["pipeline"]["tag"],
                pipeline_commit=request["pipeline"]["commit"],
                config_sha256=request["pipeline"]["config_sha256"],
                input_manifest_sha256=request["input"]["manifest_sha256"],
                idempotency_key=idempotency_key,
            )
        except QueueConflictError:
            self._remove_unqueued_task(layout)
            existing = self.queue.get_job_by_idempotency(
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                raise
            return CreatedDevelopmentJob(existing, reused=True)
        except Exception:
            self._remove_unqueued_task(layout)
            raise
        return CreatedDevelopmentJob(
            self.queue.get_job(job_id, user_id=user_id),
            reused=False,
        )

    def _write_task_files(
        self,
        layout: TaskLayout,
        *,
        user_id: uuid.UUID,
        request_id: uuid.UUID,
        image_count: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        images_path = layout.resolve("input/images")
        images_path.mkdir(parents=True)
        image_records: list[dict[str, Any]] = []
        total_bytes = 0
        for index in range(image_count):
            image_path = images_path / f"{index:06d}.jpg"
            payload = f"re3d-development-simulation-{index}".encode("ascii")
            atomic_write_bytes(image_path, payload)
            size = len(payload)
            total_bytes += size
            image_records.append(
                {
                    "name": image_path.name,
                    "sha256": sha256_file(image_path),
                    "size_bytes": size,
                    "content_type": "image/jpeg",
                    "simulation": True,
                }
            )
        manifest_path = layout.resolve("input/input-manifest.json")
        atomic_write_json(
            manifest_path,
            {
                "schema_version": "1.0",
                "simulation": True,
                "images": image_records,
            },
        )
        request = {
            "contract_version": "1.0",
            "execution_mode": "simulated",
            "request_id": str(request_id),
            "job_id": layout.job_id,
            "attempt": 1,
            "created_at": utc_now(),
            "requested_by": {"user_id": str(user_id)},
            "pipeline": {
                "name": "Re3D",
                "tag": self.baseline["tag"],
                "commit": self.baseline["commit"],
                "config_sha256": self.baseline["pipeline_config"]["sha256"],
            },
            "input": {
                "images_path": "input/images",
                "manifest_path": "input/input-manifest.json",
                "manifest_sha256": sha256_file(manifest_path),
                "image_count": image_count,
                "total_bytes": total_bytes,
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
                "max_disk_bytes": 1073741824,
                "gpu_concurrency": 1,
            },
            "idempotency_key": idempotency_key,
        }
        validate_contract("pipeline-request", request)
        atomic_write_json(layout.request_path, request)
        return request

    @staticmethod
    def _remove_unqueued_task(layout: TaskLayout) -> None:
        if layout.root.is_dir() and layout.root.parent == layout.jobs_root:
            shutil.rmtree(layout.root)
