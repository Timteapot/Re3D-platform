from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.db.queue import JobQueue
from backend.db.state_machine import JobStatus
from backend.re3d_adapter.errors import AdapterError
from backend.re3d_adapter.io import sha256_file
from backend.re3d_adapter.paths import TaskLayout

from .details import JobDetailReader


ArtifactBranch = Literal["A-v4", "B-v2", "C"]
ArtifactKind = Literal["glb", "obj", "mtl", "texture"]


class ArtifactNotFoundError(RuntimeError):
    """The requested public artifact selector is absent from the result."""


class ArtifactUnavailableError(RuntimeError):
    """A result is not ready or its persisted artifact failed integrity checks."""


@dataclass(frozen=True)
class ArtifactFile:
    path: Path
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


class ArtifactReader:
    """Resolve only contract-declared artifacts after owner and integrity checks."""

    def __init__(self, queue: JobQueue, *, data_root: Path) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.details = JobDetailReader(queue, data_root=self.data_root)

    def read(
        self,
        job_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        branch: ArtifactBranch,
        kind: ArtifactKind,
    ) -> ArtifactFile:
        detail = self.details.read(job_id, user_id=user_id)
        if (
            JobStatus(detail.job["status"]) != JobStatus.SUCCEEDED
            or detail.detail_state != "available"
            or detail.result is None
            or detail.result["status"] != "succeeded"
        ):
            raise ArtifactUnavailableError("job artifacts are not available")

        branch_result = detail.result["branches"].get(branch)
        if branch_result is None or branch_result["status"] != "succeeded":
            raise ArtifactNotFoundError("artifact branch does not exist")
        matches = [
            artifact
            for artifact in branch_result["artifacts"]
            if artifact["kind"] == kind
        ]
        if not matches:
            raise ArtifactNotFoundError("artifact kind does not exist")
        if len(matches) != 1:
            raise ArtifactUnavailableError("artifact contract is ambiguous")

        artifact = matches[0]
        if (artifact["path"], artifact["content_type"]) not in _allowed_targets(
            branch, kind
        ):
            raise ArtifactUnavailableError("artifact target is not allowed")

        try:
            layout = TaskLayout.from_data_root(self.data_root, str(job_id))
            path = layout.resolve(artifact["path"])
            if not path.is_file():
                raise ArtifactUnavailableError("artifact file is missing")
            if path.stat().st_size != artifact["size_bytes"]:
                raise ArtifactUnavailableError("artifact size changed")
            if sha256_file(path) != artifact["sha256"]:
                raise ArtifactUnavailableError("artifact hash changed")
        except ArtifactUnavailableError:
            raise
        except (AdapterError, OSError) as exc:
            raise ArtifactUnavailableError("artifact cannot be verified") from exc

        return ArtifactFile(
            path=path,
            filename=f"re3d-{job_id}-{branch}-{path.name}",
            content_type=artifact["content_type"],
            size_bytes=artifact["size_bytes"],
            sha256=artifact["sha256"],
        )


def _allowed_targets(
    branch: ArtifactBranch,
    kind: ArtifactKind,
) -> frozenset[tuple[str, str]]:
    root = f"output/{branch}"
    if kind == "glb":
        return frozenset({(f"{root}/mesh.glb", "model/gltf-binary")})
    if kind == "obj":
        return frozenset({(f"{root}/mesh.obj", "model/obj")})
    if kind == "mtl":
        return frozenset({(f"{root}/mesh.mtl", "text/plain")})
    return frozenset(
        {
            (f"{root}/texture.jpg", "image/jpeg"),
            (f"{root}/texture.jpeg", "image/jpeg"),
            (f"{root}/texture.png", "image/png"),
        }
    )
