"""Hash-pinned, offline SfM checkpoint verification and isolated restoration.

This module never opens a live job database, changes a task state, copies old
platform requests into an experiment, or creates a stage-completion marker.
An interrupted restore is deliberately retained as incomplete, not reused.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
from typing import Callable
import uuid


IMAGE_COUNT = 300
PAIR_ID_BASE = 2147483647
CHUNK_BYTES = 1024 * 1024
DOCUMENT_LIMIT = 4 * CHUNK_BYTES
DATABASE_RELATIVE = "runtime/work/shared/colmap/database.db"
PREPARED_MANIFEST = "runtime/work/input/input-manifest.json"
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
_FIXED_PAYLOADS = {
    "manifests/pipeline-request.json", "input/input-manifest.json", PREPARED_MANIFEST,
    DATABASE_RELATIVE, "identity/pipeline.json", "identity/caller-metadata.json",
    "manifests/pipeline-events.jsonl", "runtime/metrics/resource-samples.jsonl",
    "reports/storage-usage.json", "manifests/pipeline-result.json",
    "manifests/output-validation.json", "reports/evaluation.json",
    "runtime/logs/re3d-run.log", "runtime/logs/01-colmap.log",
}


class CheckpointError(ValueError):
    """Stable, non-secret error code; no source contents are included."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CheckpointFile:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class VerifiedCheckpoint:
    root: Path
    manifest_sha256: str
    database_sha256: str
    job_id: str
    pipeline_commit: str
    config_sha256: str
    pipeline_tag: str
    files: tuple[CheckpointFile, ...]
    image_names: tuple[str, ...]
    image_count: int
    prepared_images_count: int
    masks_count: int
    committed_complete_pair_records: int
    snapshot_finished_at: str
    sfm_complete: bool = False


@dataclass(frozen=True)
class RestoredCheckpoint:
    root: Path
    work_dir: Path
    images_dir: Path
    masks_dir: Path
    input_manifest_path: Path
    database_path: Path
    provenance_path: Path
    database_sha256: str
    image_count: int
    prepared_images_count: int
    masks_count: int
    committed_complete_pair_records: int
    sfm_complete: bool = False


def _noop() -> None:
    return None


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _checked_absolute(value: Path | str, *, exists: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CheckpointError("CHECKPOINT_ABSOLUTE_PATH_REQUIRED")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            if _is_link(cursor):
                raise CheckpointError("CHECKPOINT_LINK_OR_REPARSE_REJECTED")
        except FileNotFoundError:
            if exists:
                raise CheckpointError("CHECKPOINT_PATH_MISSING") from None
    return path.resolve(strict=exists)


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise CheckpointError("CHECKPOINT_RELATIVE_PATH_REJECTED")
    parts = value.split("/")
    if any(not _SAFE_COMPONENT.fullmatch(part) or part in {".", ".."}
           or part.endswith(".") or part.split(".", 1)[0].casefold() in _RESERVED for part in parts):
        raise CheckpointError("CHECKPOINT_RELATIVE_PATH_REJECTED")
    return value


def _file(root: Path, relative: str) -> Path:
    path = _checked_absolute(root.joinpath(*_relative(relative).split("/")))
    if not path.is_relative_to(root) or not path.is_file():
        raise CheckpointError("CHECKPOINT_REGULAR_FILE_REQUIRED")
    return path


def _hash(path: Path, health: Callable[[], None]) -> str:
    result = hashlib.sha256()
    # Recheck ancestry before every open. Files copied later are rehashed again.
    _checked_absolute(path)
    with path.open("rb") as stream:
        while True:
            health()
            block = stream.read(CHUNK_BYTES)
            if not block:
                break
            result.update(block)
    return result.hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    document: dict = {}
    for key, value in pairs:
        if key in document:
            raise CheckpointError("CHECKPOINT_JSON_DUPLICATE_KEY")
        document[key] = value
    return document


def _json(path: Path) -> dict:
    if path.stat().st_size > DOCUMENT_LIMIT:
        raise CheckpointError("CHECKPOINT_DOCUMENT_TOO_LARGE")
    try:
        raw = path.read_bytes()
        if len(raw) > DOCUMENT_LIMIT:
            raise CheckpointError("CHECKPOINT_DOCUMENT_TOO_LARGE")
        document = json.loads(raw, object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, CheckpointError):
            raise
        raise CheckpointError("CHECKPOINT_JSON_INVALID") from None
    if not isinstance(document, dict):
        raise CheckpointError("CHECKPOINT_JSON_OBJECT_REQUIRED")
    return document


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CheckpointError("CHECKPOINT_INTEGER_INVALID")
    return value


def _identity(value: object, expression: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not expression.fullmatch(value):
        raise CheckpointError("CHECKPOINT_EXPECTED_IDENTITY_INVALID")
    return value


def _inventory(root: Path, health: Callable[[], None]) -> set[str]:
    """Inspect all entries without following links; reject Windows aliases on all OSes."""
    files: set[str] = set()
    keys: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        _checked_absolute(directory)
        for path in directory.iterdir():
            health()
            relative = _relative(path.relative_to(root).as_posix())
            if relative.casefold() in keys:
                raise CheckpointError("CHECKPOINT_CASEFOLD_COLLISION")
            keys.add(relative.casefold())
            if _is_link(path):
                raise CheckpointError("CHECKPOINT_LINK_OR_REPARSE_REJECTED")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                files.add(relative)
            else:
                raise CheckpointError("CHECKPOINT_SPECIAL_ENTRY_REJECTED")
    return files


def _mask(path: Path, dimensions: tuple[int, int]) -> None:
    with path.open("rb") as stream:
        header = stream.read(33)
    if (len(header) != 33 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or struct.unpack(">II", header[16:24]) != dimensions
            or header[24:29] != bytes((8, 0, 0, 0, 0))):
        raise CheckpointError("CHECKPOINT_MASK_HEADER_INVALID")


def _sidecars(root: Path) -> None:
    for suffix, sizes in (("-wal", {0}), ("-shm", {0, 32768})):
        path = root / (DATABASE_RELATIVE + suffix)
        if path.exists() or path.is_symlink():
            if _file(root, DATABASE_RELATIVE + suffix).stat().st_size not in sizes:
                raise CheckpointError("CHECKPOINT_DATABASE_SIDECAR_REJECTED")


def _database(path: Path, names: set[str], health: Callable[[], None]) -> int:
    """Validate a completed hash-pinned .db alone, never its sidecars or live state."""
    failure: list[BaseException] = []

    def progress() -> int:
        try:
            health()
            return 0
        except BaseException as exc:
            failure.append(exc)
            return 1

    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(progress, 1000)
            for check in ("quick_check", "integrity_check"):
                health()
                if db.execute("PRAGMA " + check).fetchall() != [("ok",)]:
                    raise CheckpointError("CHECKPOINT_SQLITE_INTEGRITY_FAILED")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CheckpointError("CHECKPOINT_SQLITE_FOREIGN_KEY_FAILED")
            rows = db.execute("SELECT image_id,name FROM images").fetchall()
            ids = {row[0] for row in rows}
            if (len(rows) != IMAGE_COUNT or len(ids) != IMAGE_COUNT
                    or {row[1] for row in rows} != names
                    or any(type(item) is not int or not 0 < item < PAIR_ID_BASE for item in ids)):
                raise CheckpointError("CHECKPOINT_SQLITE_IMAGE_MAPPING_INVALID")
            feature_counts: dict[str, dict[int, int]] = {}
            for table, cols, value_bytes in (("keypoints", {2, 4, 6}, 4), ("descriptors", {128}, 1)):
                records = db.execute(f"SELECT image_id,rows,cols,length(data) FROM {table}").fetchall()
                if len(records) != IMAGE_COUNT or {item[0] for item in records} != ids:
                    raise CheckpointError("CHECKPOINT_SQLITE_FEATURE_MAPPING_INVALID")
                for image_id, count, width, size in records:
                    health()
                    if (type(count) is not int or count <= 0 or width not in cols
                            or size != count * width * value_bytes):
                        raise CheckpointError("CHECKPOINT_SQLITE_FEATURE_SHAPE_INVALID")
                feature_counts[table] = {row[0]: row[1] for row in records}
            if feature_counts["keypoints"] != feature_counts["descriptors"]:
                raise CheckpointError("CHECKPOINT_SQLITE_FEATURE_COUNTS_INVALID")
            pairs: dict[str, set[int]] = {}
            for table in ("matches", "two_view_geometries"):
                records = db.execute(f"SELECT pair_id,rows,cols,length(data) FROM {table}").fetchall()
                if len(records) > IMAGE_COUNT * (IMAGE_COUNT - 1) // 2:
                    raise CheckpointError("CHECKPOINT_SQLITE_PAIR_COUNT_INVALID")
                pair_ids: set[int] = set()
                for pair, count, width, size in records:
                    health()
                    if type(pair) is not int or type(count) is not int or count < 0 or width != 2:
                        raise CheckpointError("CHECKPOINT_SQLITE_PAIR_SHAPE_INVALID")
                    if (size or 0) != count * width * 4:
                        raise CheckpointError("CHECKPOINT_SQLITE_PAIR_SHAPE_INVALID")
                    first, second = divmod(pair, PAIR_ID_BASE)
                    if first not in ids or second not in ids or first >= second or pair in pair_ids:
                        raise CheckpointError("CHECKPOINT_SQLITE_PAIR_MAPPING_INVALID")
                    pair_ids.add(pair)
                pairs[table] = pair_ids
            if pairs["matches"] != pairs["two_view_geometries"]:
                raise CheckpointError("CHECKPOINT_SQLITE_INCOMPLETE_PAIRS")
            return len(pairs["matches"])
    except sqlite3.Error:
        if failure:
            raise failure[0]
        raise CheckpointError("CHECKPOINT_SQLITE_VALIDATION_FAILED") from None


def verify_checkpoint(
    checkpoint_root: Path | str,
    expected_manifest_sha256: str,
    expected_database_sha256: str,
    expected_job_id: str,
    expected_pipeline_commit: str,
    expected_config_sha256: str,
    *,
    health_check: Callable[[], None] = _noop,
) -> VerifiedCheckpoint:
    """Verify the existing 300-image export; all expectations must be supplied externally."""
    health_check()
    manifest_sha = _identity(expected_manifest_sha256, _SHA)
    database_sha = _identity(expected_database_sha256, _SHA)
    commit = _identity(expected_pipeline_commit, _COMMIT)
    config_sha = _identity(expected_config_sha256, _SHA)
    try:
        job_id = str(uuid.UUID(expected_job_id))
    except (TypeError, ValueError, AttributeError):
        raise CheckpointError("CHECKPOINT_JOB_ID_INVALID") from None
    if job_id != expected_job_id:
        raise CheckpointError("CHECKPOINT_JOB_ID_INVALID")
    root = _checked_absolute(checkpoint_root)
    if not root.is_dir():
        raise CheckpointError("CHECKPOINT_DIRECTORY_REQUIRED")
    manifest_path = _file(root, "checkpoint-manifest.json")
    if _hash(manifest_path, health_check) != manifest_sha:
        raise CheckpointError("CHECKPOINT_MANIFEST_HASH_MISMATCH")
    done = _json(_file(root, "DONE"))
    manifest = _json(manifest_path)
    if (done.get("schema_version") != "1.0" or done.get("job_id") != job_id
            or done.get("checkpoint_manifest_sha256") != manifest_sha):
        raise CheckpointError("CHECKPOINT_DONE_IDENTITY_INVALID")
    if (manifest.get("schema_version") != "1.0" or manifest.get("job_id") != job_id
            or manifest.get("image_count") != IMAGE_COUNT
            or manifest.get("source_task_modified") is not False
            or manifest.get("static_sources_rehashed_unchanged") is not True):
        raise CheckpointError("CHECKPOINT_MANIFEST_IDENTITY_INVALID")
    pipeline = manifest.get("pipeline")
    if (not isinstance(pipeline, dict) or pipeline.get("commit") != commit
            or pipeline.get("config_sha256") != config_sha
            or not isinstance(pipeline.get("tag"), str)):
        raise CheckpointError("CHECKPOINT_PIPELINE_IDENTITY_INVALID")
    _relative(pipeline["tag"])
    backup = manifest.get("sqlite_backup", {})
    if (not isinstance(backup, dict) or backup.get("last_status") != sqlite3.SQLITE_DONE
            or backup.get("last_remaining_pages") != 0
            or not isinstance(backup.get("snapshot_finished_at"), str)):
        raise CheckpointError("CHECKPOINT_SNAPSHOT_INCOMPLETE")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != manifest.get("copied_file_count"):
        raise CheckpointError("CHECKPOINT_FILE_COUNT_INVALID")
    files: dict[str, CheckpointFile] = {}
    normalized: set[str] = set()
    for row in entries:
        health_check()
        if not isinstance(row, dict):
            raise CheckpointError("CHECKPOINT_FILE_RECORD_INVALID")
        relative = _relative(row.get("relative_path"))
        if relative.casefold() in normalized:
            raise CheckpointError("CHECKPOINT_DUPLICATE_FILE")
        normalized.add(relative.casefold())
        if (relative not in _FIXED_PAYLOADS and not relative.startswith((
                "input/images/", "runtime/work/input/images/", "runtime/work/input/masks/"))):
            raise CheckpointError("CHECKPOINT_PAYLOAD_NOT_ALLOWED")
        info = CheckpointFile(relative, _integer(row.get("size_bytes")), _identity(row.get("sha256"), _SHA))
        path = _file(root, relative)
        if path.stat().st_size != info.size_bytes or _hash(path, health_check) != info.sha256:
            raise CheckpointError("CHECKPOINT_FILE_HASH_MISMATCH")
        files[relative] = info
    actual = _inventory(root, health_check)
    if any((root / "runtime/work/shared/colmap" / marker).exists()
           for marker in ("reconstruction_metrics.json", "refined_text", "mapper")):
        raise CheckpointError("CHECKPOINT_UNEXPECTED_SFM_MARKER")
    expected = set(files) | {"DONE", "checkpoint-manifest.json"}
    for relative in actual - expected:
        if relative not in {DATABASE_RELATIVE + "-wal", DATABASE_RELATIVE + "-shm"}:
            raise CheckpointError("CHECKPOINT_EXTRA_FILE_REJECTED")
    _sidecars(root)
    if not expected <= actual:
        raise CheckpointError("CHECKPOINT_FILE_MISSING")
    required = {"manifests/pipeline-request.json", "input/input-manifest.json", PREPARED_MANIFEST,
                DATABASE_RELATIVE, "identity/pipeline.json"}
    if not required <= set(files) or files[DATABASE_RELATIVE].sha256 != database_sha:
        raise CheckpointError("CHECKPOINT_REQUIRED_IDENTITY_MISSING")
    request = _json(_file(root, "manifests/pipeline-request.json"))
    request_input = request.get("input", {})
    request_storage = request.get("storage", {})
    if (request.get("contract_version") != "1.0" or request.get("job_id") != job_id
            or request.get("execution_mode") != "real"
            or request.get("pipeline") != {"name": "Re3D", **pipeline}
            or not isinstance(request_input, dict) or request_input.get("image_count") != IMAGE_COUNT
            or request_input.get("images_path") != "input/images"
            or request_input.get("manifest_path") != "input/input-manifest.json"
            or request_input.get("manifest_sha256") != files["input/input-manifest.json"].sha256
            or not isinstance(request_storage, dict) or request_storage.get("work_path") != "runtime/work"):
        raise CheckpointError("CHECKPOINT_REQUEST_IDENTITY_INVALID")
    if files["identity/pipeline.json"].sha256 != config_sha:
        raise CheckpointError("CHECKPOINT_CONFIG_HASH_MISMATCH")
    _json(_file(root, "identity/pipeline.json"))
    external = manifest.get("external_metadata", {})
    if isinstance(external, dict) and external.get("available") is True:
        metadata_info = files.get("identity/caller-metadata.json")
        if metadata_info is None or metadata_info.sha256 != external.get("metadata_sha256"):
            raise CheckpointError("CHECKPOINT_METADATA_IDENTITY_INVALID")
        metadata = _json(_file(root, "identity/caller-metadata.json"))
        if (metadata != external.get("values") or metadata.get("pipeline_commit") != commit
                or metadata.get("pipeline_config_sha256") != config_sha
                or metadata.get("pipeline_tag") != pipeline["tag"]):
            raise CheckpointError("CHECKPOINT_METADATA_IDENTITY_INVALID")
    original = _json(_file(root, "input/input-manifest.json"))
    prepared = _json(_file(root, PREPARED_MANIFEST))
    original_rows, prepared_rows = original.get("images"), prepared.get("images")
    if (not isinstance(original_rows, list) or len(original_rows) != IMAGE_COUNT
            or not isinstance(prepared_rows, list) or len(prepared_rows) != IMAGE_COUNT
            or prepared.get("count") != IMAGE_COUNT):
        raise CheckpointError("CHECKPOINT_INPUT_COUNT_INVALID")
    dimensions = prepared.get("uniform_size")
    if not isinstance(dimensions, list) or len(dimensions) != 2:
        raise CheckpointError("CHECKPOINT_INPUT_DIMENSIONS_INVALID")
    dimensions_tuple = tuple(_integer(value, minimum=1) for value in dimensions)
    names: dict[str, dict] = {}
    folded: set[str] = set()
    for row in original_rows:
        if not isinstance(row, dict):
            raise CheckpointError("CHECKPOINT_INPUT_RECORD_INVALID")
        name = _relative(row.get("name"))
        if "/" in name or Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg"} or name.casefold() in folded:
            raise CheckpointError("CHECKPOINT_INPUT_NAME_INVALID")
        folded.add(name.casefold())
        size, sha = _integer(row.get("size_bytes"), minimum=1), _identity(row.get("sha256"), _SHA)
        for prefix in ("input/images/", "runtime/work/input/images/"):
            entry = files.get(prefix + name)
            if entry is None or entry.size_bytes != size or entry.sha256 != sha:
                raise CheckpointError("CHECKPOINT_INPUT_MAPPING_INVALID")
        mask_relative = "runtime/work/input/masks/" + name + ".png"
        if mask_relative not in files:
            raise CheckpointError("CHECKPOINT_MASK_MISSING")
        _mask(_file(root, mask_relative), dimensions_tuple)
        names[name] = row
    prepared_names: set[str] = set()
    for row in prepared_rows:
        if (not isinstance(row, dict) or not isinstance(row.get("name"), str) or row.get("name") not in names
                or row["name"] in prepared_names or row.get("sha256") != names[row["name"]]["sha256"]
                or (row.get("width"), row.get("height")) != dimensions_tuple):
            raise CheckpointError("CHECKPOINT_PREPARED_MAPPING_INVALID")
        prepared_names.add(row["name"])
    if request_input.get("total_bytes") != sum(row["size_bytes"] for row in names.values()):
        raise CheckpointError("CHECKPOINT_INPUT_TOTAL_INVALID")
    for prefix, suffix in (("input/images/", ""), ("runtime/work/input/images/", ""), ("runtime/work/input/masks/", ".png")):
        if {item for item in files if item.startswith(prefix)} != {prefix + name + suffix for name in names}:
            raise CheckpointError("CHECKPOINT_INPUT_DIRECTORY_INVALID")
    pairs = _database(_file(root, DATABASE_RELATIVE), set(names), health_check)
    # Pin the source again after SQLite and inventory reads; verification must not
    # silently certify a changing snapshot. Sidecars are not used or copied.
    if _hash(_file(root, DATABASE_RELATIVE), health_check) != database_sha or _hash(manifest_path, health_check) != manifest_sha:
        raise CheckpointError("CHECKPOINT_SOURCE_CHANGED")
    _sidecars(root)
    return VerifiedCheckpoint(root, manifest_sha, database_sha, job_id, commit, config_sha,
                              pipeline["tag"], tuple(files.values()), tuple(sorted(names)),
                              IMAGE_COUNT, IMAGE_COUNT, IMAGE_COUNT, pairs, backup["snapshot_finished_at"])


def _write_json(path: Path, document: dict, health: Callable[[], None]) -> None:
    health()
    _checked_absolute(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def restore_checkpoint(
    verified: VerifiedCheckpoint,
    target_experiment_root: Path | str,
    health_check: Callable[[], None] = _noop,
    *,
    target_work_root: Path | str | None = None,
) -> RestoredCheckpoint:
    """Clone into a new `_experiments/<id>/work`, checking supervisor health per chunk.

    Default: the experiment root must not exist. If ``target_work_root`` is
    supplied, its exact ``root/work`` may be reserved inside a precreated root
    containing only operation-reservation.json and reports/resource-samples.jsonl.
    No prior work, artifacts, or incomplete restore is ever overwritten.
    """
    health_check()
    root = _checked_absolute(target_experiment_root, exists=False)
    if root.parent.name.casefold() != "_experiments" or any(part.casefold() == "jobs" for part in root.parts):
        raise CheckpointError("CHECKPOINT_EXPERIMENT_TARGET_REQUIRED")
    _relative(root.name)
    source = _checked_absolute(verified.root)
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise CheckpointError("CHECKPOINT_SOURCE_TARGET_OVERLAP")
    parent = _checked_absolute(root.parent)
    if not parent.is_dir():
        raise CheckpointError("CHECKPOINT_TARGET_PARENT_REQUIRED")
    work = root / "work"
    if target_work_root is not None:
        proposed = _checked_absolute(target_work_root, exists=False)
        if proposed != work or not root.is_dir():
            raise CheckpointError("CHECKPOINT_MONITOR_ROOT_INVALID")
        inventory = _inventory(root, health_check)
        if not inventory <= {"operation-reservation.json", "reports/resource-samples.jsonl"}:
            raise CheckpointError("CHECKPOINT_MONITOR_ROOT_NOT_EMPTY")
        if {item.name for item in root.iterdir()} - {"operation-reservation.json", "reports"}:
            raise CheckpointError("CHECKPOINT_MONITOR_ROOT_NOT_EMPTY")
        reports = root / "reports"
        if reports.exists() and (not reports.is_dir() or
                {item.name for item in reports.iterdir()} - {"resource-samples.jsonl"}):
            raise CheckpointError("CHECKPOINT_MONITOR_ROOT_NOT_EMPTY")
        if work.exists() or work.is_symlink():
            raise CheckpointError("CHECKPOINT_TARGET_ALREADY_EXISTS")
    elif root.exists() or root.is_symlink():
        raise CheckpointError("CHECKPOINT_TARGET_ALREADY_EXISTS")
    # A VerifiedCheckpoint is not an authorization token and may have gone stale.
    fresh = verify_checkpoint(source, verified.manifest_sha256, verified.database_sha256,
                              verified.job_id, verified.pipeline_commit, verified.config_sha256,
                              health_check=health_check)
    health_check()
    if target_work_root is None:
        root.mkdir()  # exclusive: race or existing incomplete root fails closed
    _checked_absolute(root)
    work.mkdir()
    _checked_absolute(work)
    _write_json(work / "RESTORE_INCOMPLETE.json", {"schema_version": "1.0", "status": "incomplete"}, health_check)
    copied: list[dict] = []
    for info in fresh.files:
        relative = info.relative_path
        if relative == DATABASE_RELATIVE:
            destination_relative = "shared/colmap/database.db"
        elif relative == PREPARED_MANIFEST or relative.startswith(("runtime/work/input/images/", "runtime/work/input/masks/")):
            destination_relative = relative.removeprefix("runtime/work/")
        else:
            continue
        target = work.joinpath(*destination_relative.split("/"))
        health_check()
        _checked_absolute(target, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        _checked_absolute(target.parent)
        source_path = _file(source, relative)
        digest = hashlib.sha256()
        with source_path.open("rb") as input_stream, target.open("xb") as output_stream:
            while True:
                health_check()
                block = input_stream.read(CHUNK_BYTES)
                if not block:
                    break
                output_stream.write(block)
                digest.update(block)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if target.stat().st_size != info.size_bytes or digest.hexdigest() != info.sha256 or _hash(target, health_check) != info.sha256:
            raise CheckpointError("CHECKPOINT_RESTORE_HASH_MISMATCH")
        # Fresh xb copies are writable; never copy source ACLs/read-only attributes.
        copied.append({"relative_path": destination_relative, "size_bytes": info.size_bytes, "sha256": info.sha256})
    if len(copied) != IMAGE_COUNT * 2 + 2:
        raise CheckpointError("CHECKPOINT_RESTORE_COUNT_INVALID")
    clone_database = work / "shared" / "colmap" / "database.db"
    pairs = _database(clone_database, set(fresh.image_names), health_check)
    if pairs != fresh.committed_complete_pair_records or _hash(clone_database, health_check) != fresh.database_sha256:
        raise CheckpointError("CHECKPOINT_RESTORE_DATABASE_INVALID")
    # Never fabricate SfM's reconstruction_metrics.json/refined_text/mapper gate.
    if any((work / "shared" / "colmap" / name).exists() for name in ("reconstruction_metrics.json", "refined_text", "mapper")):
        raise CheckpointError("CHECKPOINT_UNEXPECTED_SFM_MARKER")
    provenance_path = work / "provenance.json"
    _write_json(provenance_path, {
        "schema_version": "1.0", "execution_kind": "standalone_checkpoint_experiment",
        "source_job_id": fresh.job_id, "source_checkpoint_manifest_sha256": fresh.manifest_sha256,
        "source_database_sha256": fresh.database_sha256, "clone_database_sha256": fresh.database_sha256,
        "pipeline_commit": fresh.pipeline_commit, "pipeline_config_sha256": fresh.config_sha256,
        "pipeline_tag": fresh.pipeline_tag, "image_count": IMAGE_COUNT,
        "prepared_images_count": IMAGE_COUNT, "masks_count": IMAGE_COUNT,
        "committed_complete_pair_records": pairs, "snapshot_finished_at": fresh.snapshot_finished_at,
        "restored_at_utc": datetime.now(timezone.utc).isoformat(), "copied_files": copied,
        "source_checkpoint_modified": False, "old_platform_contracts_copied": False,
        "sfm_complete": False, "platform_resume_implemented": False,
    }, health_check)
    _write_json(work / "RESTORE_COMPLETE.json", {"schema_version": "1.0", "status": "verified",
               "provenance_sha256": _hash(provenance_path, health_check)}, health_check)
    # Remove only our newly created provisional marker after completion. On any
    # earlier failure it remains, and neither old data nor the source is deleted.
    _checked_absolute(work / "RESTORE_INCOMPLETE.json").unlink()
    return RestoredCheckpoint(root, work, work / "input/images", work / "input/masks",
                              work / "input/input-manifest.json", clone_database, provenance_path,
                              fresh.database_sha256, IMAGE_COUNT, IMAGE_COUNT, IMAGE_COUNT, pairs)
