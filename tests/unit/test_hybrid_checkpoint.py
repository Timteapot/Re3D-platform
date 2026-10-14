from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.experiments import checkpoint as cp
from backend.experiments.hybrid_checkpoint import (
    HybridCheckpointError, restore_hybrid_source, verify_hybrid_source,
)
from backend.experiments.snapshot_store import SnapshotStore
from tests.unit.test_experiment_checkpoint import document, mask


JOB = "6a90b5e9-0c99-41aa-881b-c196ebb96750"
COMMIT = "e" * 40


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HybridCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep the genuine verifier and SHA/inventory/SQLite implementation.
        # Three synthetic images exercise every pair without 900 fixture files.
        count = patch.object(cp, "IMAGE_COUNT", 3)
        count.start()
        self.addCleanup(count.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.original = self.base / "original"
        self.original.mkdir()
        self.experiments = self.base / "_experiments"
        self.experiments.mkdir()
        self.target = self.experiments / "hybrid-cpu"
        self.names = [f"{index:06d}-abc.jpg" for index in range(3)]
        rows, prepared = [], []
        for index, name in enumerate(self.names):
            data = f"synthetic image {index}".encode()
            for prefix in ("input/images", "runtime/work/input/images"):
                path = self.original / prefix / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            path = self.original / "runtime/work/input/masks" / (name + ".png")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(mask())
            rows.append({"name": name, "size_bytes": len(data), "sha256": digest(self.original / "input/images" / name)})
            prepared.append({"name": name, "sha256": rows[-1]["sha256"], "width": 1, "height": 1})
        document(self.original / "input/input-manifest.json", {"images": rows})
        document(self.original / cp.PREPARED_MANIFEST, {"count": 3, "uniform_size": [1, 1], "images": prepared})
        document(self.original / "identity/pipeline.json", {"sfm": {"matcher": "exhaustive"}})
        self.config_sha = digest(self.original / "identity/pipeline.json")
        pipeline = {"tag": "re3d-pipeline-v1.1.3", "commit": COMMIT, "config_sha256": self.config_sha}
        document(self.original / "manifests/pipeline-request.json", {
            "contract_version": "1.0", "execution_mode": "real", "job_id": JOB,
            "pipeline": {"name": "Re3D", **pipeline}, "storage": {"work_path": "runtime/work"},
            "input": {"image_count": 3, "total_bytes": sum(row["size_bytes"] for row in rows),
                      "images_path": "input/images", "manifest_path": "input/input-manifest.json",
                      "manifest_sha256": digest(self.original / "input/input-manifest.json")},
        })
        self.database = self.original / cp.DATABASE_RELATIVE
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE cameras(camera_id INTEGER PRIMARY KEY,model INTEGER,width INTEGER,height INTEGER,params BLOB,prior_focal_length INTEGER);
                CREATE TABLE images(image_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,camera_id INTEGER REFERENCES cameras(camera_id));
                CREATE TABLE keypoints(image_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
                CREATE TABLE descriptors(image_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
                CREATE TABLE matches(pair_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
                CREATE TABLE two_view_geometries(pair_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
            """)
            db.execute("INSERT INTO cameras VALUES(1,1,1,1,?,0)", (b"camera parameters",))
            for index, name in enumerate(self.names, start=1):
                db.execute("INSERT INTO images VALUES(?,?,1)", (index, name))
                db.execute("INSERT INTO keypoints VALUES(?,?,?,?)", (index, 1, 2, b"\0" * 8))
                db.execute("INSERT INTO descriptors VALUES(?,?,?,?)", (index, 1, 128, b"\0" * 128))
            for table in ("matches", "two_view_geometries"):
                for pair in (cp.PAIR_ID_BASE + 2, cp.PAIR_ID_BASE + 3):
                    db.execute(f"INSERT INTO {table} VALUES(?,0,2,NULL)", (pair,))
        files = [{"relative_path": path.relative_to(self.original).as_posix(),
                  "size_bytes": path.stat().st_size, "sha256": digest(path)}
                 for path in self.original.rglob("*") if path.is_file()]
        document(self.original / "checkpoint-manifest.json", {
            "schema_version": "1.0", "job_id": JOB, "pipeline": pipeline,
            "image_count": 3, "source_task_modified": False, "static_sources_rehashed_unchanged": True,
            "files": files, "copied_file_count": len(files),
            "sqlite_backup": {"last_status": sqlite3.SQLITE_DONE, "last_remaining_pages": 0,
                              "snapshot_finished_at": "2026-10-13T12:42:28Z"},
        })
        self.manifest_sha, self.database_sha = digest(self.original / "checkpoint-manifest.json"), digest(self.database)
        document(self.original / "DONE", {"schema_version": "1.0", "job_id": JOB,
                                           "checkpoint_manifest_sha256": self.manifest_sha})
        self.live = self.base / "live.db"
        shutil.copyfile(self.database, self.live)
        with closing(sqlite3.connect(self.live)) as db, db:
            for table in ("matches", "two_view_geometries"):
                db.execute(f"INSERT INTO {table} VALUES(?,0,2,NULL)", (2 * cp.PAIR_ID_BASE + 3,))
        store = SnapshotStore(self.base / "snapshots", self.live, self.names, min_free_bytes=0,
                              free_bytes_reader=lambda: 1024**3)
        self.record = store.capture_final(tree_stopped=True)
        self.snapshot = store.root / self.record["directory"]
        self.snapshot_sha, self.snapshot_db_sha = self.record["manifest_sha256"], self.record["database_sha256"]

    def arguments(self) -> dict:
        return {"original_root": self.original, "original_manifest_sha256": self.manifest_sha,
                "original_database_sha256": self.database_sha, "source_job_id": JOB,
                "pipeline_commit": COMMIT, "config_sha256": self.config_sha,
                "snapshot_root": self.snapshot, "snapshot_manifest_sha256": self.snapshot_sha,
                "snapshot_database_sha256": self.snapshot_db_sha, "expected_complete_pair_records": 3}

    def verify(self, **changes):
        args = self.arguments()
        args.update(changes)
        return verify_hybrid_source(**args)

    def repin_snapshot(self) -> None:
        path = self.snapshot / "snapshot-manifest.json"
        manifest = json.loads(path.read_bytes())
        database = self.snapshot / "database.db"
        self.snapshot_db_sha = digest(database)
        manifest.update(database_sha256=self.snapshot_db_sha, database_size_bytes=database.stat().st_size)
        document(path, manifest)
        self.snapshot_sha = digest(path)
        document(self.snapshot / "DONE.json", {"schema_version": "1.0", "owner_id": manifest["owner_id"],
                 "snapshot_manifest_sha256": self.snapshot_sha, "database_sha256": self.snapshot_db_sha})

    def fingerprint(self) -> dict:
        return {path.as_posix(): (digest(path), path.stat().st_mtime_ns)
                for root in (self.original, self.snapshot) for path in root.rglob("*") if path.is_file()}

    def test_verified_snapshot_and_isolated_restore_preserve_both_sources(self) -> None:
        before = self.fingerprint()
        source = self.verify()
        self.assertEqual(source.original.committed_complete_pair_records, 2)
        self.assertEqual(source.complete_pair_records, 3)
        restored = restore_hybrid_source(source, self.target)
        self.assertEqual(digest(restored.database_path), self.snapshot_db_sha)
        self.assertEqual(restored.committed_complete_pair_records, 3)
        self.assertFalse(restored.sfm_complete)
        receipt = json.loads(restored.provenance_path.read_bytes())
        self.assertEqual(receipt["selected_database_sha256"], self.snapshot_db_sha)
        self.assertEqual(receipt["original_database_sha256"], self.database_sha)
        self.assertFalse(receipt["platform_resume_implemented"])
        self.assertFalse((restored.work_dir / "HYBRID_IMPORT_INCOMPLETE.json").exists())
        self.assertFalse((restored.work_dir / "shared/colmap/mapper").exists())
        self.assertFalse((restored.root / "manifests/pipeline-request.json").exists())
        with closing(sqlite3.connect(restored.database_path)) as db, db:
            db.execute("DELETE FROM matches")
        self.assertEqual(self.fingerprint(), before)

    def test_original_only_and_partial_snapshot_arguments(self) -> None:
        source = self.verify(snapshot_root=None, snapshot_manifest_sha256=None,
                             snapshot_database_sha256=None, expected_complete_pair_records=2)
        self.assertEqual(source.selected_database_sha256, self.database_sha)
        restored = restore_hybrid_source(source, self.target)
        self.assertEqual(digest(restored.database_path), self.database_sha)
        for changes in ({"snapshot_manifest_sha256": None}, {"snapshot_database_sha256": None},
                        {"snapshot_root": None}):
            with self.subTest(changes=changes), self.assertRaisesRegex(HybridCheckpointError, "INCOMPLETE"):
                self.verify(**changes)

    def test_external_hash_and_done_identity_rejected(self) -> None:
        for changes in ({"snapshot_manifest_sha256": "0" * 64}, {"snapshot_database_sha256": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(HybridCheckpointError):
                self.verify(**changes)
        document(self.snapshot / "DONE.json", {"schema_version": "1.0"})
        with self.assertRaisesRegex(HybridCheckpointError, "DONE_IDENTITY"):
            self.verify()

    def test_snapshot_inventory_rejects_empty_directory_wal_shm_and_incomplete(self) -> None:
        for name in ("database.db-wal", "database.db-shm", "INCOMPLETE.json", "extra.txt"):
            path = self.snapshot / name
            path.write_bytes(b"")
            with self.subTest(name=name), self.assertRaisesRegex(HybridCheckpointError, "INVENTORY"):
                self.verify()
            path.unlink()
        extra = self.snapshot / "empty-directory"
        extra.mkdir()
        with self.assertRaisesRegex(HybridCheckpointError, "INVENTORY"):
            self.verify()

    def test_selected_snapshot_database_tamper_rejected(self) -> None:
        with closing(sqlite3.connect(self.snapshot / "database.db")) as db, db:
            db.execute("DELETE FROM matches")
        with self.assertRaisesRegex(HybridCheckpointError, "DATABASE_HASH"):
            self.verify()

    def test_camera_and_feature_changes_rejected_even_when_externally_repinned(self) -> None:
        with closing(sqlite3.connect(self.snapshot / "database.db")) as db, db:
            db.execute("UPDATE cameras SET params=?", (b"different camera",))
        self.repin_snapshot()
        with self.assertRaisesRegex(HybridCheckpointError, "STATIC_IDENTITY"):
            self.verify()
        with closing(sqlite3.connect(self.snapshot / "database.db")) as db, db:
            db.execute("UPDATE cameras SET params=?", (b"camera parameters",))
            db.execute("UPDATE descriptors SET data=? WHERE image_id=1", (b"x" * 128,))
        self.repin_snapshot()
        with self.assertRaisesRegex(HybridCheckpointError, "STATIC_IDENTITY"):
            self.verify()

    def test_image_id_name_binding_changed_even_when_names_identical(self) -> None:
        with closing(sqlite3.connect(self.snapshot / "database.db")) as db, db:
            db.execute("UPDATE images SET name='temporary.jpg' WHERE image_id=1")
            db.execute("UPDATE images SET name=? WHERE image_id=2", (self.names[0],))
            db.execute("UPDATE images SET name=? WHERE image_id=1", (self.names[1],))
        self.repin_snapshot()
        with self.assertRaisesRegex(HybridCheckpointError, "STATIC_IDENTITY"):
            self.verify()

    def test_pair_count_and_completed_backup_flags_rejected(self) -> None:
        with self.assertRaisesRegex(HybridCheckpointError, "EXPECTED_PAIR_COUNT"):
            self.verify(expected_complete_pair_records=2)
        manifest = json.loads((self.snapshot / "snapshot-manifest.json").read_bytes())
        manifest["sqlite_backup_completed"] = False
        document(self.snapshot / "snapshot-manifest.json", manifest)
        self.repin_snapshot()
        with self.assertRaisesRegex(HybridCheckpointError, "MANIFEST_IDENTITY"):
            self.verify()

    def test_regressed_pair_count_rejected(self) -> None:
        with closing(sqlite3.connect(self.snapshot / "database.db")) as db, db:
            for table in ("matches", "two_view_geometries"):
                db.execute(f"DELETE FROM {table} WHERE pair_id<>?", (cp.PAIR_ID_BASE + 2,))
        manifest = json.loads((self.snapshot / "snapshot-manifest.json").read_bytes())
        manifest["validated_complete_pair_records"] = 1
        document(self.snapshot / "snapshot-manifest.json", manifest)
        self.repin_snapshot()
        with self.assertRaisesRegex(HybridCheckpointError, "PAIR_COUNT_INVALID"):
            self.verify(expected_complete_pair_records=1)

    def test_original_pair_ids_cannot_be_replaced_at_equal_count(self) -> None:
        with closing(sqlite3.connect(self.snapshot / "database.db")) as db, db:
            for table in ("matches", "two_view_geometries"):
                db.execute(f"DELETE FROM {table} WHERE pair_id=?", (cp.PAIR_ID_BASE + 3,))
        manifest = json.loads((self.snapshot / "snapshot-manifest.json").read_bytes())
        manifest["validated_complete_pair_records"] = 2
        document(self.snapshot / "snapshot-manifest.json", manifest)
        self.repin_snapshot()
        with self.assertRaisesRegex(HybridCheckpointError, "PAIR_RECORDS_REGRESSED"):
            self.verify(expected_complete_pair_records=2)

    def test_path_identity_traversal_and_duplicate_json_rejected(self) -> None:
        manifest_path = self.snapshot / "snapshot-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["database_relative_path"] = "../database.db"
        document(manifest_path, manifest)
        self.repin_snapshot()
        with self.assertRaisesRegex(HybridCheckpointError, "MANIFEST_IDENTITY"):
            self.verify()
        manifest_path.write_bytes(b'{"schema_version":"1.0","schema_version":"1.0"}')
        self.snapshot_sha = digest(manifest_path)
        with self.assertRaisesRegex(cp.CheckpointError, "JSON_DUPLICATE"):
            self.verify()

    def test_reparse_and_stale_source_rejected_before_target_creation(self) -> None:
        source = self.verify()
        original_is_link = cp._is_link
        with patch.object(cp, "_is_link", side_effect=lambda path: path == self.snapshot or original_is_link(path)):
            with self.assertRaisesRegex(cp.CheckpointError, "REPARSE"):
                self.verify()
        (self.snapshot / "database.db").write_bytes(b"changed")
        with self.assertRaises(HybridCheckpointError):
            restore_hybrid_source(source, self.target)
        self.assertFalse(self.target.exists())

    def test_existing_target_rejected_and_monitor_reservation_supported(self) -> None:
        source = self.verify()
        self.target.mkdir()
        (self.target / "old.txt").write_text("preserve")
        with self.assertRaisesRegex(cp.CheckpointError, "ALREADY_EXISTS"):
            restore_hybrid_source(source, self.target)
        self.assertEqual((self.target / "old.txt").read_text(), "preserve")
        target = self.experiments / "reserved"
        (target / "reports").mkdir(parents=True)
        (target / "reports/resource-samples.jsonl").write_text("{}\n")
        document(target / "operation-reservation.json", {"status": "reserved"})
        restored = restore_hybrid_source(source, target, target_work_root=target / "work")
        self.assertEqual(restored.committed_complete_pair_records, 3)

    def test_health_trip_keeps_new_root_incomplete_and_sources_unchanged(self) -> None:
        source = self.verify()
        before = self.fingerprint()

        def health() -> None:
            colmap = self.target / "work/shared/colmap"
            if colmap.exists() and any(path.name.startswith(".hybrid-database-") for path in colmap.iterdir()):
                raise RuntimeError("synthetic health trip")

        with self.assertRaisesRegex(RuntimeError, "health trip"):
            restore_hybrid_source(source, self.target, health)
        self.assertTrue((self.target / "work/HYBRID_IMPORT_INCOMPLETE.json").exists())
        self.assertFalse((self.target / "work/RESTORE_COMPLETE.json").exists())
        self.assertEqual(self.fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
