from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from backend.experiments.checkpoint import (
    CheckpointError, DATABASE_RELATIVE, PREPARED_MANIFEST, restore_checkpoint, verify_checkpoint,
)


JOB_ID = "6a90b5e9-0c99-41aa-881b-c196ebb96750"
COMMIT = "e" * 40


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def mask() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">II5B", 1, 1, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff")) + chunk(b"IEND", b"")


class ExperimentCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "checkpoint"
        self.source.mkdir()
        self.experiments = self.base / "_experiments"
        self.experiments.mkdir()
        self.target = self.experiments / "resume8"
        self.names = [f"{index:06d}-abc.jpg" for index in range(300)]
        rows, prepared = [], []
        for index, name in enumerate(self.names):
            image = f"synthetic image {index}".encode()
            for prefix in ("input/images", "runtime/work/input/images"):
                path = self.source / prefix / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(image)
            mask_path = self.source / "runtime/work/input/masks" / (name + ".png")
            mask_path.parent.mkdir(parents=True, exist_ok=True)
            mask_path.write_bytes(mask())
            rows.append({"name": name, "size_bytes": len(image), "sha256": hashlib.sha256(image).hexdigest()})
            prepared.append({"name": name, "sha256": rows[-1]["sha256"], "width": 1, "height": 1})
        document(self.source / "input/input-manifest.json", {"images": rows})
        document(self.source / PREPARED_MANIFEST, {"count": 300, "uniform_size": [1, 1], "images": prepared})
        document(self.source / "identity/pipeline.json", {"sfm": {"matcher": "exhaustive"}})
        self.config_sha = digest(self.source / "identity/pipeline.json")
        self.pipeline = {"tag": "re3d-pipeline-v1.1.3", "commit": COMMIT, "config_sha256": self.config_sha}
        document(self.source / "manifests/pipeline-request.json", {
            "contract_version": "1.0", "execution_mode": "real", "job_id": JOB_ID,
            "pipeline": {"name": "Re3D", **self.pipeline}, "storage": {"work_path": "runtime/work"},
            "input": {"image_count": 300, "total_bytes": sum(row["size_bytes"] for row in rows),
                      "images_path": "input/images", "manifest_path": "input/input-manifest.json",
                      "manifest_sha256": digest(self.source / "input/input-manifest.json")},
        })
        self.database = self.source / DATABASE_RELATIVE
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE images(image_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE);
                CREATE TABLE keypoints(image_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
                CREATE TABLE descriptors(image_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
                CREATE TABLE matches(pair_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
                CREATE TABLE two_view_geometries(pair_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
            """)
            for index, name in enumerate(self.names, start=1):
                db.execute("INSERT INTO images VALUES(?,?)", (index, name))
                db.execute("INSERT INTO keypoints VALUES(?,?,?,?)", (index, 1, 2, b"\0" * 8))
                db.execute("INSERT INTO descriptors VALUES(?,?,?,?)", (index, 1, 128, b"\0" * 128))
            for table in ("matches", "two_view_geometries"):
                db.execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (2147483647 + 2, 1, 2, b"\0" * 8))
                db.execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (2147483647 + 3, 0, 2, None))
        self.refresh_manifest()

    def refresh_manifest(self) -> None:
        files = []
        for path in self.source.rglob("*"):
            if path.is_file() and path.name not in {"DONE", "checkpoint-manifest.json", "database.db-wal", "database.db-shm"}:
                files.append({"relative_path": path.relative_to(self.source).as_posix(),
                              "size_bytes": path.stat().st_size, "sha256": digest(path)})
        self.manifest = {
            "schema_version": "1.0", "job_id": JOB_ID, "pipeline": self.pipeline,
            "image_count": 300, "source_task_modified": False, "static_sources_rehashed_unchanged": True,
            "files": files, "copied_file_count": len(files),
            "sqlite_backup": {"last_status": 101, "last_remaining_pages": 0,
                              "snapshot_finished_at": "2026-10-13T12:42:28Z"},
        }
        self.pin_manifest()

    def pin_manifest(self) -> None:
        document(self.source / "checkpoint-manifest.json", self.manifest)
        self.manifest_sha = digest(self.source / "checkpoint-manifest.json")
        self.database_sha = digest(self.database)
        document(self.source / "DONE", {"schema_version": "1.0", "job_id": JOB_ID,
                                        "checkpoint_manifest_sha256": self.manifest_sha})

    def verify(self, **changes):
        arguments = {"checkpoint_root": self.source, "expected_manifest_sha256": self.manifest_sha,
                     "expected_database_sha256": self.database_sha, "expected_job_id": JOB_ID,
                     "expected_pipeline_commit": COMMIT, "expected_config_sha256": self.config_sha}
        arguments.update(changes)
        return verify_checkpoint(**arguments)

    def fingerprint(self) -> dict:
        return {path.relative_to(self.source).as_posix(): (digest(path), path.stat().st_mtime_ns)
                for path in self.source.rglob("*") if path.is_file()}

    def test_verify_and_restore_isolated_clone_without_source_changes(self) -> None:
        before = self.fingerprint()
        checks = []
        verified = self.verify(health_check=lambda: checks.append(True))
        self.assertEqual(verified.committed_complete_pair_records, 2)
        restored = restore_checkpoint(verified, self.target, lambda: checks.append(True))
        self.assertEqual(restored.image_count, 300)
        self.assertEqual(len(list(restored.images_dir.iterdir())), 300)
        self.assertEqual(len(list(restored.masks_dir.iterdir())), 300)
        self.assertEqual(digest(restored.database_path), self.database_sha)
        self.assertFalse(restored.sfm_complete)
        self.assertTrue(restored.provenance_path.is_file())
        self.assertTrue((restored.work_dir / "RESTORE_COMPLETE.json").is_file())
        self.assertFalse((restored.root / "manifests/pipeline-request.json").exists())
        self.assertFalse((restored.work_dir / "shared/colmap/reconstruction_metrics.json").exists())
        with closing(sqlite3.connect(restored.database_path)) as db, db:
            db.execute("UPDATE matches SET rows=0,data=NULL")
        self.assertEqual(self.fingerprint(), before)
        self.assertGreater(len(checks), 2000)

    def test_expected_identity_and_done_tamper_rejected(self) -> None:
        for change in ({"expected_manifest_sha256": "0" * 64}, {"expected_database_sha256": "0" * 64},
                       {"expected_pipeline_commit": "f" * 40}, {"expected_config_sha256": "f" * 64},
                       {"expected_job_id": "00000000-0000-0000-0000-000000000000"}):
            with self.subTest(change=change), self.assertRaises(CheckpointError):
                self.verify(**change)
        document(self.source / "DONE", {"schema_version": "1.0", "job_id": JOB_ID,
                                        "checkpoint_manifest_sha256": "0" * 64})
        with self.assertRaisesRegex(CheckpointError, "DONE_IDENTITY"):
            self.verify()

    def test_file_data_tamper_rejected(self) -> None:
        (self.source / "input/images" / self.names[0]).write_bytes(b"changed")
        with self.assertRaisesRegex(CheckpointError, "FILE_HASH"):
            self.verify()

    def test_duplicate_casefold_manifest_and_path_traversal_rejected(self) -> None:
        original = list(self.manifest["files"])
        extra = dict(original[0])
        extra["relative_path"] = extra["relative_path"].upper()
        self.manifest["files"].append(extra)
        self.manifest["copied_file_count"] += 1
        self.pin_manifest()
        with self.assertRaisesRegex(CheckpointError, "DUPLICATE_FILE"):
            self.verify()
        self.manifest["files"] = original
        self.manifest["files"][0]["relative_path"] = "../outside.jpg"
        self.manifest["copied_file_count"] = len(original)
        self.pin_manifest()
        with self.assertRaisesRegex(CheckpointError, "RELATIVE_PATH"):
            self.verify()

    def test_prepared_manifest_mapping_tamper_rejected(self) -> None:
        path = self.source / PREPARED_MANIFEST
        prepared = json.loads(path.read_text())
        prepared["images"][1]["name"] = prepared["images"][0]["name"]
        document(path, prepared)
        self.refresh_manifest()
        with self.assertRaisesRegex(CheckpointError, "PREPARED_MAPPING"):
            self.verify()

    def test_mask_header_tamper_rejected_even_when_hash_pinned(self) -> None:
        path = self.source / "runtime/work/input/masks" / (self.names[0] + ".png")
        path.write_bytes(b"not a valid grayscale PNG header")
        self.refresh_manifest()
        with self.assertRaisesRegex(CheckpointError, "MASK_HEADER"):
            self.verify()

    def test_nonempty_wal_rejected_empty_wal_and_ephemeral_shm_not_copied(self) -> None:
        wal = Path(str(self.database) + "-wal")
        wal.write_bytes(b"pending transactions")
        with self.assertRaisesRegex(CheckpointError, "SIDECAR"):
            self.verify()
        wal.write_bytes(b"")
        Path(str(self.database) + "-shm").write_bytes(b"\0" * 32768)
        before = self.fingerprint()
        restored = restore_checkpoint(self.verify(), self.target)
        self.assertFalse(Path(str(restored.database_path) + "-wal").exists())
        self.assertFalse(Path(str(restored.database_path) + "-shm").exists())
        self.assertEqual(self.fingerprint(), before)

    def test_sqlite_feature_shape_and_pair_mapping_tamper_rejected(self) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE descriptors SET data=? WHERE image_id=1", (b"bad",))
        self.refresh_manifest()
        with self.assertRaisesRegex(CheckpointError, "FEATURE_SHAPE"):
            self.verify()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE descriptors SET data=? WHERE image_id=1", (b"\0" * 128,))
            db.execute("UPDATE matches SET pair_id=? WHERE pair_id=?", (301 * 2147483647 + 302, 2147483647 + 2))
        self.refresh_manifest()
        with self.assertRaisesRegex(CheckpointError, "PAIR_MAPPING"):
            self.verify()

    def test_sqlite_incomplete_pair_and_image_mapping_rejected(self) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM two_view_geometries WHERE pair_id=?", (2147483647 + 2,))
        self.refresh_manifest()
        with self.assertRaisesRegex(CheckpointError, "INCOMPLETE_PAIRS"):
            self.verify()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE images SET name='unknown.jpg' WHERE image_id=1")
        self.refresh_manifest()
        with self.assertRaisesRegex(CheckpointError, "IMAGE_MAPPING"):
            self.verify()

    def test_existing_target_and_jobs_directory_rejected(self) -> None:
        verified = self.verify()
        self.target.mkdir()
        (self.target / "old.txt").write_text("preserve")
        with self.assertRaisesRegex(CheckpointError, "ALREADY_EXISTS"):
            restore_checkpoint(verified, self.target)
        bad = self.base / "jobs/_experiments"
        bad.mkdir(parents=True)
        with self.assertRaisesRegex(CheckpointError, "EXPERIMENT_TARGET"):
            restore_checkpoint(verified, bad / "resume8")
        self.assertEqual((self.target / "old.txt").read_text(), "preserve")

    def test_source_target_overlap_rejected_before_creation(self) -> None:
        verified = self.verify()
        overlap_parent = self.source / "_experiments"
        overlap_parent.mkdir()
        with self.assertRaisesRegex(CheckpointError, "OVERLAP"):
            restore_checkpoint(verified, overlap_parent / "resume8")
        self.assertFalse((overlap_parent / "resume8").exists())

    def test_reparse_rejected_deterministically(self) -> None:
        from backend.experiments import checkpoint
        original = checkpoint._is_link
        with patch.object(checkpoint, "_is_link", side_effect=lambda path: path == self.source or original(path)):
            with self.assertRaisesRegex(CheckpointError, "REPARSE"):
                self.verify()

    def test_extra_file_and_fake_sfm_gate_rejected(self) -> None:
        unknown = self.source / "extra.txt"
        unknown.write_text("untracked")
        with self.assertRaisesRegex(CheckpointError, "EXTRA_FILE"):
            self.verify()
        unknown.unlink()
        (self.database.parent / "mapper").mkdir()
        with self.assertRaisesRegex(CheckpointError, "SFM_MARKER"):
            self.verify()

    def test_stale_verification_is_rechecked_before_target_created(self) -> None:
        verified = self.verify()
        (self.source / "runtime/work/input/images" / self.names[0]).write_bytes(b"changed")
        with self.assertRaises(CheckpointError):
            restore_checkpoint(verified, self.target)
        self.assertFalse(self.target.exists())

    def test_health_failure_retains_incomplete_clone_and_original(self) -> None:
        verified = self.verify()
        before = self.fingerprint()

        def health() -> None:
            if (self.target / "work/input/input-manifest.json").exists():
                raise RuntimeError("test health trip")

        with self.assertRaisesRegex(RuntimeError, "health trip"):
            restore_checkpoint(verified, self.target, health)
        self.assertTrue((self.target / "work/RESTORE_INCOMPLETE.json").exists())
        self.assertFalse((self.target / "work/RESTORE_COMPLETE.json").exists())
        self.assertEqual(self.fingerprint(), before)
        with self.assertRaisesRegex(CheckpointError, "ALREADY_EXISTS"):
            restore_checkpoint(verified, self.target)

    def test_precreated_monitor_root_is_strict_and_work_exclusive(self) -> None:
        verified = self.verify()
        (self.target / "reports").mkdir(parents=True)
        sample = self.target / "reports/resource-samples.jsonl"
        sample.write_text('{"sample":1}\n')
        document(self.target / "operation-reservation.json", {"status": "reserved"})
        restored = restore_checkpoint(verified, self.target, target_work_root=self.target / "work")
        self.assertEqual(restored.work_dir, self.target / "work")
        self.assertEqual(sample.read_text(), '{"sample":1}\n')
        with self.assertRaises(CheckpointError):
            restore_checkpoint(verified, self.target, target_work_root=self.target / "work")

    def test_precreated_monitor_root_unknown_files_rejected(self) -> None:
        self.target.mkdir()
        (self.target / "other.txt").write_text("preserve")
        with self.assertRaisesRegex(CheckpointError, "MONITOR_ROOT_NOT_EMPTY"):
            restore_checkpoint(self.verify(), self.target, target_work_root=self.target / "work")
        self.assertFalse((self.target / "work").exists())


if __name__ == "__main__":
    unittest.main()
