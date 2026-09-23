from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import PathBoundaryError


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(parent)]) == str(parent)
    except ValueError:
        return False


def _canonical_job_id(job_id: str) -> str:
    try:
        parsed = uuid.UUID(job_id)
    except ValueError as exc:
        raise PathBoundaryError("job_id must be a UUID") from exc
    canonical = str(parsed)
    if job_id.lower() != canonical:
        raise PathBoundaryError("job_id must use canonical UUID form")
    return canonical


@dataclass(frozen=True)
class TaskLayout:
    data_root: Path
    jobs_root: Path
    root: Path
    job_id: str

    @classmethod
    def from_data_root(
        cls,
        data_root: Path | str,
        job_id: str,
        *,
        create: bool = False,
    ) -> "TaskLayout":
        canonical_id = _canonical_job_id(job_id)
        resolved_data_root = Path(data_root).expanduser().resolve()
        jobs_root = (resolved_data_root / "jobs").resolve()
        root = (jobs_root / canonical_id).resolve()
        if root.parent != jobs_root or not _is_within(root, jobs_root):
            raise PathBoundaryError("task directory is outside the jobs root")
        if create:
            root.mkdir(parents=True, exist_ok=True)
        elif not root.is_dir():
            raise PathBoundaryError("task directory does not exist")
        return cls(resolved_data_root, jobs_root, root, canonical_id)

    @property
    def request_path(self) -> Path:
        return self.resolve("manifests/pipeline-request.json")

    @property
    def events_path(self) -> Path:
        return self.resolve("manifests/pipeline-events.jsonl")

    @property
    def result_path(self) -> Path:
        return self.resolve("manifests/pipeline-result.json")

    @property
    def output_validation_path(self) -> Path:
        return self.resolve("manifests/output-validation.json")

    def ensure_worker_directories(self) -> None:
        for relative in ("runtime/work", "runtime/logs", "output", "reports", "manifests"):
            self.resolve(relative).mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        pure = PurePosixPath(relative_path)
        if (
            not relative_path
            or pure.is_absolute()
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in pure.parts)
            or len(pure.parts) == 0
        ):
            raise PathBoundaryError("task path must be a normalized POSIX relative path")
        candidate = self.root.joinpath(*pure.parts).resolve()
        if not _is_within(candidate, self.root):
            raise PathBoundaryError("task path escapes the task directory")
        return candidate
