"""Immutable static input plus a newer, externally pinned SfM snapshot.

The original checkpoint remains the authority for images and feature identity.
The selected three-file snapshot only advances committed matching work; it does
not certify SfM completion or authorize any change to a platform task or lease.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable
import uuid

from backend.experiments import checkpoint as cp


_SNAPSHOT_NAME = re.compile(r"snapshot-[A-Za-z0-9-]{1,160}\Z")
_OWNER = re.compile(r"[0-9a-f]{32}\Z")


class HybridCheckpointError(cp.CheckpointError):
    """Stable, non-secret error code for a hybrid checkpoint boundary."""


@dataclass(frozen=True)
class VerifiedHybridSource:
    original: cp.VerifiedCheckpoint
    selected_database_path: Path
    selected_database_sha256: str
    snapshot_manifest_sha256: str | None
    complete_pair_records: int
    image_names: tuple[str, ...]
    snapshot_root: Path | None = None


def _noop() -> None:
    return None


def _stable_database_identity(
    original_path: Path, selected_path: Path, health: Callable[[], None],
) -> None:
    """Compare immutable image/camera/features and preserve all original pairs.

    Stream one row at a time instead of holding feature blobs in memory. Both
    handles are query-only, immutable, externally hashed database files.
    """
    failure: list[BaseException] = []

    def progress() -> int:
        try:
            health()
            return 0
        except BaseException as exc:
            failure.append(exc)
            return 1

    try:
        with closing(sqlite3.connect(original_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as original, \
                closing(sqlite3.connect(selected_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as selected:
            for db in (original, selected):
                db.execute("PRAGMA query_only=ON")
                db.set_progress_handler(progress, 1000)
            for table, key in (("images", "image_id"), ("cameras", "camera_id"),
                               ("keypoints", "image_id"), ("descriptors", "image_id")):
                health()
                columns = original.execute(f"PRAGMA table_info({table})").fetchall()
                other_columns = selected.execute(f"PRAGMA table_info({table})").fetchall()
                if not columns or columns != other_columns or key not in {row[1] for row in columns}:
                    raise HybridCheckpointError("HYBRID_DATABASE_IDENTITY_SCHEMA_INVALID")
                if table == "images" and "camera_id" not in {row[1] for row in columns}:
                    raise HybridCheckpointError("HYBRID_DATABASE_CAMERA_BINDING_MISSING")
                left = original.execute(f"SELECT * FROM {table} ORDER BY {key}")
                right = selected.execute(f"SELECT * FROM {table} ORDER BY {key}")
                while True:
                    health()
                    before, after = left.fetchone(), right.fetchone()
                    if before != after:
                        raise HybridCheckpointError("HYBRID_DATABASE_STATIC_IDENTITY_CHANGED")
                    if before is None:
                        break
            camera_ids = {row[0] for row in selected.execute("SELECT camera_id FROM cameras")}
            if not camera_ids or any(row[0] not in camera_ids for row in
                                     selected.execute("SELECT camera_id FROM images")):
                raise HybridCheckpointError("HYBRID_DATABASE_CAMERA_MAPPING_INVALID")
            for table in ("matches", "two_view_geometries"):
                before = {row[0] for row in original.execute(f"SELECT pair_id FROM {table}")}
                after = {row[0] for row in selected.execute(f"SELECT pair_id FROM {table}")}
                if not before <= after:
                    raise HybridCheckpointError("HYBRID_DATABASE_PAIR_RECORDS_REGRESSED")
    except sqlite3.Error:
        if failure:
            raise failure[0]
        raise HybridCheckpointError("HYBRID_DATABASE_IDENTITY_VALIDATION_FAILED") from None


def _verify_selected_snapshot(
    original: cp.VerifiedCheckpoint, snapshot_root: Path | str,
    manifest_sha256: str, database_sha256: str,
    expected_complete_pair_records: int | None,
    health: Callable[[], None],
) -> VerifiedHybridSource:
    health()
    manifest_sha = cp._identity(manifest_sha256, cp._SHA)
    database_sha = cp._identity(database_sha256, cp._SHA)
    root = cp._checked_absolute(snapshot_root)
    if not root.is_dir() or not _SNAPSHOT_NAME.fullmatch(root.name):
        raise HybridCheckpointError("HYBRID_SNAPSHOT_DIRECTORY_INVALID")
    if root.is_relative_to(original.root) or original.root.is_relative_to(root):
        raise HybridCheckpointError("HYBRID_SOURCE_ROOTS_OVERLAP")
    required = {"database.db", "snapshot-manifest.json", "DONE.json"}
    inventory = cp._inventory(root, health)
    if inventory != required or {item.name for item in root.iterdir()} != required:
        raise HybridCheckpointError("HYBRID_SNAPSHOT_INVENTORY_INVALID")
    manifest_path, database_path = cp._file(root, "snapshot-manifest.json"), cp._file(root, "database.db")
    if cp._hash(manifest_path, health) != manifest_sha:
        raise HybridCheckpointError("HYBRID_SNAPSHOT_MANIFEST_HASH_MISMATCH")
    manifest, done = cp._json(manifest_path), cp._json(cp._file(root, "DONE.json"))
    owner = manifest.get("owner_id")
    if (not isinstance(owner, str) or not _OWNER.fullmatch(owner)
            or done != {"schema_version": "1.0", "owner_id": owner,
                        "snapshot_manifest_sha256": manifest_sha, "database_sha256": database_sha}):
        raise HybridCheckpointError("HYBRID_SNAPSHOT_DONE_IDENTITY_INVALID")
    if (manifest.get("schema_version") != "1.0" or manifest.get("directory") != root.name
            or manifest.get("database_relative_path") != root.name + "/database.db"
            or manifest.get("database_sha256") != database_sha
            or manifest.get("database_size_bytes") != database_path.stat().st_size
            or type(manifest.get("database_size_bytes")) is not int
            or manifest.get("image_count") != original.image_count
            or type(manifest.get("image_count")) is not int
            or manifest.get("source_write_operations") is not False
            or manifest.get("sfm_completion_certified") is not False
            or any(manifest.get(key) is not True for key in (
                "committed_wal_included", "sqlite_backup_completed",
                "sqlite_integrity_and_mapping_validation",
                "database_hash_checked_before_and_after_validation"))):
        raise HybridCheckpointError("HYBRID_SNAPSHOT_MANIFEST_IDENTITY_INVALID")
    if cp._hash(database_path, health) != database_sha:
        raise HybridCheckpointError("HYBRID_SNAPSHOT_DATABASE_HASH_MISMATCH")
    pairs = cp._database(database_path, set(original.image_names), health)
    recorded = manifest.get("validated_complete_pair_records")
    if type(recorded) is not int or recorded != pairs or pairs < original.committed_complete_pair_records:
        raise HybridCheckpointError("HYBRID_SNAPSHOT_PAIR_COUNT_INVALID")
    if expected_complete_pair_records is not None and (
            type(expected_complete_pair_records) is not int or pairs != expected_complete_pair_records):
        raise HybridCheckpointError("HYBRID_EXPECTED_PAIR_COUNT_MISMATCH")
    _stable_database_identity(cp._file(original.root, cp.DATABASE_RELATIVE), database_path, health)
    # Reads never certify a file that changed during validation.
    if (cp._hash(database_path, health) != database_sha
            or cp._hash(manifest_path, health) != manifest_sha
            or cp._hash(cp._file(original.root, cp.DATABASE_RELATIVE), health) != original.database_sha256):
        raise HybridCheckpointError("HYBRID_SOURCE_CHANGED")
    return VerifiedHybridSource(original, database_path, database_sha, manifest_sha,
                                pairs, original.image_names, root)


def verify_hybrid_source(
    *, original_root: Path | str, original_manifest_sha256: str,
    original_database_sha256: str, source_job_id: str, pipeline_commit: str,
    config_sha256: str, snapshot_root: Path | str | None = None,
    snapshot_manifest_sha256: str | None = None, snapshot_database_sha256: str | None = None,
    expected_complete_pair_records: int | None = None,
    health_check: Callable[[], None] = _noop,
) -> VerifiedHybridSource:
    """Bind static checkpoint data to an optional later SnapshotStore export.

    All hash expectations are supplied externally, not inferred from a copied
    DONE or manifest. Omit all three snapshot arguments to select the original.
    """
    original = cp.verify_checkpoint(original_root, original_manifest_sha256, original_database_sha256,
                                    source_job_id, pipeline_commit, config_sha256,
                                    health_check=health_check)
    if snapshot_root is None:
        if snapshot_manifest_sha256 is not None or snapshot_database_sha256 is not None:
            raise HybridCheckpointError("HYBRID_SNAPSHOT_EXPECTATIONS_INCOMPLETE")
        pairs = original.committed_complete_pair_records
        if expected_complete_pair_records is not None and (
                type(expected_complete_pair_records) is not int or pairs != expected_complete_pair_records):
            raise HybridCheckpointError("HYBRID_EXPECTED_PAIR_COUNT_MISMATCH")
        return VerifiedHybridSource(original, cp._file(original.root, cp.DATABASE_RELATIVE),
                                    original.database_sha256, None, pairs, original.image_names)
    if snapshot_manifest_sha256 is None or snapshot_database_sha256 is None:
        raise HybridCheckpointError("HYBRID_SNAPSHOT_EXPECTATIONS_INCOMPLETE")
    return _verify_selected_snapshot(original, snapshot_root, snapshot_manifest_sha256,
                                     snapshot_database_sha256, expected_complete_pair_records, health_check)


def restore_hybrid_source(
    verified: VerifiedHybridSource, target_root: Path | str,
    health_check: Callable[[], None] = _noop, *, target_work_root: Path | str | None = None,
) -> cp.RestoredCheckpoint:
    """Create a fresh isolated clone; only its newly created database may change.

    The old manifest/provenance remains an unmodified original-restore receipt.
    A separate import provenance and rebinding completion marker describe the
    actual selected database. Failure retains a marked incomplete new root.
    """
    health_check()
    if not isinstance(verified, VerifiedHybridSource):
        raise HybridCheckpointError("HYBRID_VERIFIED_SOURCE_REQUIRED")
    original = verified.original
    # Reject a stale or manually constructed binding before creating a target.
    if verified.snapshot_root is not None:
        selected = _verify_selected_snapshot(original, verified.snapshot_root,
            verified.snapshot_manifest_sha256, verified.selected_database_sha256,
            verified.complete_pair_records, health_check)
        if selected.selected_database_path != verified.selected_database_path or selected.image_names != verified.image_names:
            raise HybridCheckpointError("HYBRID_VERIFIED_SOURCE_BINDING_CHANGED")
    else:
        if (verified.snapshot_manifest_sha256 is not None
                or verified.selected_database_path != cp._file(original.root, cp.DATABASE_RELATIVE)
                or verified.selected_database_sha256 != original.database_sha256
                or verified.complete_pair_records != original.committed_complete_pair_records
                or verified.image_names != original.image_names):
            raise HybridCheckpointError("HYBRID_VERIFIED_SOURCE_BINDING_CHANGED")
    restored = cp.restore_checkpoint(original, target_root, health_check, target_work_root=target_work_root)
    work = restored.work_dir
    incomplete = work / "HYBRID_IMPORT_INCOMPLETE.json"
    cp._write_json(incomplete, {"schema_version": "1.0", "status": "incomplete"}, health_check)
    # RESTORE_COMPLETE belongs to this newly created clone, not an old task.
    cp._checked_absolute(work / "RESTORE_COMPLETE.json").unlink()
    if verified.snapshot_root is not None:
        staged = restored.database_path.with_name(".hybrid-database-" + uuid.uuid4().hex + ".tmp")
        cp._checked_absolute(staged, exists=False)
        source = cp._checked_absolute(verified.selected_database_path)
        with source.open("rb") as input_stream, staged.open("xb") as output_stream:
            while True:
                health_check()
                block = input_stream.read(cp.CHUNK_BYTES)
                if not block:
                    break
                output_stream.write(block)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if (cp._hash(staged, health_check) != verified.selected_database_sha256
                or cp._database(staged, set(verified.image_names), health_check) != verified.complete_pair_records
                or cp._hash(source, health_check) != verified.selected_database_sha256
                or cp._hash(restored.database_path, health_check) != original.database_sha256):
            raise HybridCheckpointError("HYBRID_RESTORE_DATABASE_INVALID")
        health_check()
        cp._checked_absolute(restored.database_path)
        os.replace(staged, restored.database_path)
    if (cp._hash(restored.database_path, health_check) != verified.selected_database_sha256
            or cp._database(restored.database_path, set(verified.image_names), health_check) != verified.complete_pair_records):
        raise HybridCheckpointError("HYBRID_RESTORE_DATABASE_INVALID")
    provenance = work / "import-provenance.json"
    cp._write_json(provenance, {
        "schema_version": "1.0", "execution_kind": "standalone_hybrid_checkpoint_experiment",
        "source_job_id": original.job_id, "pipeline_commit": original.pipeline_commit,
        "pipeline_config_sha256": original.config_sha256,
        "original_checkpoint_manifest_sha256": original.manifest_sha256,
        "original_database_sha256": original.database_sha256,
        "selected_snapshot_manifest_sha256": verified.snapshot_manifest_sha256,
        "selected_database_sha256": verified.selected_database_sha256,
        "clone_database_sha256": verified.selected_database_sha256,
        "image_count": original.image_count, "prepared_images_count": original.prepared_images_count,
        "masks_count": original.masks_count, "complete_pair_records": verified.complete_pair_records,
        "imported_at_utc": datetime.now(timezone.utc).isoformat(),
        "original_checkpoint_modified": False, "selected_snapshot_modified": False,
        "old_platform_contracts_copied": False, "platform_resume_implemented": False,
        "sfm_complete": False,
    }, health_check)
    cp._write_json(work / "RESTORE_COMPLETE.json", {
        "schema_version": "1.0", "status": "verified", "execution_kind": "hybrid_checkpoint_import",
        "provenance_sha256": cp._hash(provenance, health_check),
    }, health_check)
    cp._checked_absolute(incomplete).unlink()
    return cp.RestoredCheckpoint(restored.root, work, restored.images_dir, restored.masks_dir,
        restored.input_manifest_path, restored.database_path, provenance,
        verified.selected_database_sha256, original.image_count, original.prepared_images_count,
        original.masks_count, verified.complete_pair_records)
