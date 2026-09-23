from __future__ import annotations

import json
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


@dataclass(frozen=True)
class Re3DSettings:
    root: Path
    driver_python: Path

    @classmethod
    def from_environment(
        cls,
        *,
        root: str | None = None,
        driver_python: str | None = None,
    ) -> "Re3DSettings":
        configured_root = root or os.environ.get("RE3D_ROOT")
        if not configured_root:
            raise AdapterError("RE3D_ROOT is required")
        resolved_root = Path(configured_root).expanduser().resolve()
        configured_python = driver_python or os.environ.get("RE3D_DRIVER_PYTHON")
        if not configured_python:
            paths_file = resolved_root / "configs" / "paths.local.json"
            try:
                paths = json.loads(paths_file.read_text(encoding="utf-8-sig"))
                configured_python = paths["mapanything_python"]
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                raise AdapterError("cannot resolve Re3D driver Python") from exc
        python_path = Path(configured_python).expanduser()
        if not python_path.is_absolute():
            python_path = resolved_root / python_path
        return cls(resolved_root, python_path.resolve())
