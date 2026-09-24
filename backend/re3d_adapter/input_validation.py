from __future__ import annotations

import json
import re
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
    manifest_records = _manifest_records(records)
    image_files = sorted(path for path in images_path.iterdir() if path.is_file())
    if any(path.is_symlink() for path in image_files):
        raise IntegrityError("input image directory contains a symbolic link")
    if len(image_files) != request["input"]["image_count"]:
        raise IntegrityError("stored image count does not match the request")
    if sorted(path.name for path in image_files) != sorted(manifest_records):
        raise IntegrityError("stored image names do not match the input manifest")
    for path in image_files:
        record = manifest_records[path.name]
        if path.stat().st_size != record["size_bytes"]:
            raise IntegrityError("stored image size does not match the input manifest")
        if sha256_file(path) != record["sha256"]:
            raise IntegrityError("stored image SHA-256 does not match the input manifest")
    total_bytes = sum(path.stat().st_size for path in image_files)
    if total_bytes != request["input"]["total_bytes"]:
        raise IntegrityError("stored image byte count does not match the request")


def _manifest_records(records: list[Any]) -> dict[str, dict[str, Any]]:
    validated: dict[str, dict[str, Any]] = {}
    for record in records:
        name = record.get("name") if isinstance(record, dict) else None
        size_bytes = record.get("size_bytes") if isinstance(record, dict) else None
        digest = record.get("sha256") if isinstance(record, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 1
            or not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            raise IntegrityError("input manifest contains an invalid image record")
        if name in validated:
            raise IntegrityError("input manifest contains duplicate image names")
        validated[name] = {
            "size_bytes": size_bytes,
            "sha256": digest,
        }
    if len(validated) != len(records):
        raise IntegrityError("input manifest contains duplicate image names")
    return validated
