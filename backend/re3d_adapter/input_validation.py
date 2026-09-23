from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import IntegrityError
from .io import sha256_file
from .paths import TaskLayout


def validate_task_input(layout: TaskLayout, request: dict[str, Any]) -> None:
    manifest_path = layout.resolve(request["input"]["manifest_path"])
    images_path = layout.resolve(request["input"]["images_path"])
    if not manifest_path.is_file() or not images_path.is_dir():
        raise IntegrityError("input manifest or image directory is missing")
    if sha256_file(manifest_path) != request["input"]["manifest_sha256"]:
        raise IntegrityError("input manifest SHA-256 does not match the request")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("input manifest is not readable JSON") from exc
    records = manifest.get("images") if isinstance(manifest, dict) else None
    if not isinstance(records, list) or len(records) != request["input"]["image_count"]:
        raise IntegrityError("input manifest image count does not match the request")
    manifest_names = _manifest_names(records)
    image_files = sorted(path for path in images_path.iterdir() if path.is_file())
    if any(path.is_symlink() for path in image_files):
        raise IntegrityError("input image directory contains a symbolic link")
    if len(image_files) != request["input"]["image_count"]:
        raise IntegrityError("stored image count does not match the request")
    if sorted(path.name for path in image_files) != sorted(manifest_names):
        raise IntegrityError("stored image names do not match the input manifest")
    total_bytes = sum(path.stat().st_size for path in image_files)
    if total_bytes != request["input"]["total_bytes"]:
        raise IntegrityError("stored image byte count does not match the request")


def _manifest_names(records: list[Any]) -> list[str]:
    names: list[str] = []
    for record in records:
        name = record.get("name") if isinstance(record, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or "/" in name
            or "\\" in name
        ):
            raise IntegrityError("input manifest contains an unsafe image name")
        names.append(name)
    if len(set(names)) != len(names):
        raise IntegrityError("input manifest contains duplicate image names")
    return names
