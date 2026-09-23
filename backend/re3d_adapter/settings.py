from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from .errors import AdapterError


@dataclass(frozen=True)
class WorkerSettings:
    data_root: Path
    worker_id: str

    @classmethod
    def from_environment(
        cls,
        *,
        data_root: str | None = None,
        worker_id: str | None = None,
    ) -> "WorkerSettings":
        configured_root = data_root or os.environ.get("RE3D_DATA_ROOT")
        if not configured_root:
            raise AdapterError("RE3D_DATA_ROOT is required")
        configured_worker = (
            worker_id
            or os.environ.get("RE3D_WORKER_ID")
            or f"{platform.node() or 'windows'}-worker"
        )
        return cls(Path(configured_root).expanduser().resolve(), configured_worker)
