from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.experiments.snapshot_store import GIB, SnapshotError, SnapshotStore


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_database(path: Path, *, count: int = 300) -> list[str]:
    names = [f"{index:06d}.jpg" for index in range(count)]
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE images(image_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE);
            CREATE TABLE keypoints(image_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
            CREATE TABLE descriptors(image_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
            CREATE TABLE matches(pair_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
            CREATE TABLE two_view_geometries(pair_id INTEGER PRIMARY KEY,rows INTEGER,cols INTEGER,data BLOB);
        """)
        for index, name in enumerate(names, start=1):
            db.execute("INSERT INTO images VALUES(?,?)", (index, name))
            db.execute("INSERT INTO keypoints VALUES(?,?,?,?)", (index, 1, 2, b"\0" * 8))
            db.execute("INSERT INTO descriptors VALUES(?,?,?,?)", (index, 1, 128, b"\0" * 128))
        for table in ("matches", "two_view_geometries"):
            db.execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (2147483647 + 2, 1, 2, b"\0" * 8))
            db.execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (2147483647 + 3, 0, 2, None))
    return names


class ExperimentSnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.database = self.base / "database.db"
        self.names = make_database(self.database)
        self.root = self.base / "snapshots"
        self.stores: list[SnapshotStore] = []
        self.addCleanup(self.stop_stores)

    def stop_stores(self) -> None:
        for store in self.stores:
            store.stop(timeout=1)

    def store(self, **changes) -> SnapshotStore:
        args = dict(root=self.root, database=self.database, expected_names=self.names,
                    health_check=lambda: None, free_bytes_reader=lambda: 100 * GIB,
                    interval_seconds=0.03, timeout_seconds=3)
        args.update(changes)
        result = SnapshotStore(**args)
        self.stores.append(result)
        return result

    def wait_for(self, condition, seconds=3) -> None:
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("bounded test condition did not complete")
            time.sleep(0.01)

    def test_real_300_feature_validation_includes_committed_wal_without_source_writes(self) -> None:
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        for table in ("matches", "two_view_geometries"):
            writer.execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (2147483647 + 4, 0, 2, None))
        writer.commit()
        wal = self.database.with_name(self.database.name + "-wal")
        before = digest(self.database), digest(wal)
        # immutable would only see the original two pairs, proving this fixture
        # has the extra committed pair in WAL rather than the main source file.
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM matches").fetchone()[0], 2)
        store = self.store()
        result = store.capture_final(tree_stopped=True, reason="unit_success")
        self.assertEqual(result["validated_complete_pair_records"], 3)
        self.assertEqual(before, (digest(self.database), digest(wal)))
        self.assertEqual(result["database_sha256"], digest(self.root / result["database_relative_path"]))
        self.assertEqual(store.latest, json.loads((self.root / "latest-valid.json").read_bytes()))
        self.assertTrue(result["committed_wal_included"])
        self.assertFalse(result["source_write_operations"])

    def test_periodic_worker_and_bounded_clean_stop(self) -> None:
        store = self.store()
        store.start()
        store.check_health()
        self.wait_for(lambda: store.summary()["successful_snapshots"] >= 1)
        self.assertTrue(store.stop(timeout=1))
        self.assertEqual(store.summary()["state"], "stopped")
        self.assertTrue(store.summary()["worker_stopped_confirmed"])
        with self.assertRaises(SnapshotError) as caught:
            store.check_health()
        self.assertEqual(caught.exception.code, "SNAPSHOT_SUPERVISOR_NOT_RUNNING")

    def test_latest_publication_and_only_two_owned_valid_snapshots_retained(self) -> None:
        store = self.store()
        first = store.capture_final(tree_stopped=True)
        second = store.capture_final(tree_stopped=True)
        unrelated = self.root / "snapshot-external-user-data"
        unrelated.mkdir()
        (unrelated / "keep.db").write_bytes(b"not owned")
        third = store.capture_final(tree_stopped=True)
        self.assertFalse((self.root / first["directory"]).exists())
        self.assertTrue((self.root / second["directory"]).exists())
        self.assertTrue((self.root / third["directory"]).exists())
        self.assertEqual((unrelated / "keep.db").read_bytes(), b"not owned")
        self.assertEqual(store.summary()["retention_removed_snapshots"], 1)
        self.assertEqual(store.summary()["retained_valid_snapshots"], 2)

    def test_changed_owned_database_is_never_deleted_by_retention(self) -> None:
        store = self.store()
        first = store.capture_final(tree_stopped=True)
        first_db = self.root / first["database_relative_path"]
        first_db.write_bytes(b"changed externally")
        store.capture_final(tree_stopped=True)
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_RETENTION_OWNERSHIP_CHANGED")
        self.assertEqual(first_db.read_bytes(), b"changed externally")
        self.assertEqual(store.summary()["retention_removed_snapshots"], 0)

    def test_external_file_inside_owned_snapshot_prevents_deletion(self) -> None:
        store = self.store()
        first = store.capture_final(tree_stopped=True)
        extra = self.root / first["directory"] / "external.txt"
        extra.write_text("must remain", encoding="utf-8")
        store.capture_final(tree_stopped=True)
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_RETENTION_INVENTORY_CHANGED")
        self.assertTrue(extra.exists())

    def test_existing_root_is_rejected_without_overwriting_any_file(self) -> None:
        self.root.mkdir()
        keep = self.root / "keep.txt"
        keep.write_bytes(b"preserve")
        with self.assertRaises(SnapshotError):
            self.store()
        self.assertEqual(keep.read_bytes(), b"preserve")

    def test_relative_or_source_overlap_paths_rejected(self) -> None:
        with self.assertRaises(SnapshotError):
            self.store(root=Path("relative"))
        with self.assertRaises(SnapshotError):
            self.store(root=self.base)

    def test_space_insufficient_does_not_start_database_copy_or_publish(self) -> None:
        store = self.store(free_bytes_reader=lambda: 20 * GIB + self.database.stat().st_size - 1)
        before = digest(self.database)
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_COPY_SPACE_INSUFFICIENT")
        self.assertEqual(before, digest(self.database))
        self.assertEqual(list(self.root.glob(".pending-*")), [])
        self.assertIsNone(store.latest)

    def test_validation_failure_retains_incomplete_target_and_no_valid_pointer(self) -> None:
        def bad_validator(*_):
            raise ValueError("private source path and token must not leak")
        store = self.store(validator=bad_validator)
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(str(caught.exception), "SNAPSHOT_FINAL_CAPTURE_FAILED")
        self.assertEqual(len(list(self.root.glob(".pending-*/INCOMPLETE.json"))), 1)
        self.assertFalse((self.root / "latest-valid.json").exists())

    def test_untrusted_exception_code_is_sanitized(self) -> None:
        class SecretException(Exception):
            code = "forbidden-field DO_NOT_PERSIST_MARKER"
        store = self.store(validator=lambda *_: (_ for _ in ()).throw(SecretException()))
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(str(caught.exception), "SNAPSHOT_FINAL_CAPTURE_FAILED")
        self.assertEqual(store.summary()["error_code"], "SNAPSHOT_FINAL_CAPTURE_FAILED")

    def test_metadata_pointer_hash_rechecked_before_replacement(self) -> None:
        store = self.store()
        first = store.capture_final(tree_stopped=True)
        pointer = self.root / "latest-valid.json"
        pointer.write_bytes(b"external pointer")
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_POINTER_OWNERSHIP_CHANGED")
        self.assertEqual(pointer.read_bytes(), b"external pointer")
        self.assertEqual(store.latest, first)

    def test_final_requires_explicit_confirmed_owned_tree_exit(self) -> None:
        store = self.store()
        for value in (False, None, 1, "yes"):
            with self.assertRaises(SnapshotError) as caught:
                store.capture_final(tree_stopped=value)
            self.assertEqual(caught.exception.code, "SNAPSHOT_FINAL_TREE_NOT_STOPPED")
        self.assertEqual(store.summary()["attempts"], 0)

    def test_final_replaces_sticky_runtime_guard_with_independent_forensic_health(self) -> None:
        calls = []
        def sticky_runtime():
            raise SnapshotError("UNIT_RUNTIME_RESOURCE_TRIP")
        store = self.store(health_check=sticky_runtime)
        store.start()
        self.wait_for(lambda: store.summary()["state"] == "failed")
        self.assertTrue(store.stop(timeout=1))
        record = store.capture_final(tree_stopped=True, health_check=lambda: calls.append(1), reason="resource_trip")
        self.assertTrue(calls)
        self.assertEqual(record["reason"], "resource_trip")
        self.assertEqual(store.summary()["error_code"], "UNIT_RUNTIME_RESOURCE_TRIP")

    def test_blocked_periodic_validator_cannot_block_main_guard_or_publish_after_cancel(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def validator(*_):
            entered.set()
            release.wait(2)
            return 2
        store = self.store(validator=validator, interval_seconds=0.01, timeout_seconds=0.08)
        store.start()
        self.assertTrue(entered.wait(1))
        time.sleep(0.1)
        started = time.monotonic()
        with self.assertRaises(SnapshotError) as caught:
            store.check_health()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(caught.exception.code, "SNAPSHOT_CAPTURE_TIMED_OUT")
        self.assertFalse(store.stop(timeout=0.02))
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_FINAL_WORKER_NOT_STOPPED")
        release.set()
        self.wait_for(lambda: store.summary()["worker_stopped_confirmed"])
        self.assertIsNone(store.latest)
        self.assertFalse((self.root / "latest-valid.json").exists())

    def test_blocked_final_validator_returns_at_deadline_and_never_late_publishes(self) -> None:
        release = threading.Event()
        def validator(*_):
            release.wait(2)
            return 2
        store = self.store(validator=validator, timeout_seconds=0.08)
        started = time.monotonic()
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(caught.exception.code, "SNAPSHOT_FINAL_CAPTURE_TIMED_OUT")
        self.assertFalse(store.summary()["worker_stopped_confirmed"])
        release.set()
        self.wait_for(lambda: store.summary()["worker_stopped_confirmed"])
        self.assertFalse((self.root / "latest-valid.json").exists())

    def test_blocked_space_reader_is_bounded_and_does_not_late_start_copy(self) -> None:
        release = threading.Event()
        def reader():
            release.wait(2)
            return 100 * GIB
        store = self.store(free_bytes_reader=reader, timeout_seconds=0.08)
        started = time.monotonic()
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(caught.exception.code, "SNAPSHOT_FINAL_CAPTURE_TIMED_OUT")
        release.set()
        self.wait_for(lambda: store.summary()["worker_stopped_confirmed"])
        self.assertEqual(list(self.root.glob(".pending-*")), [])
        self.assertIsNone(store.latest)

    def test_stop_must_precede_final_capture_and_store_cannot_restart(self) -> None:
        store = self.store(interval_seconds=60)
        store.start()
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_FINAL_WORKER_NOT_STOPPED")
        self.assertTrue(store.stop(timeout=1))
        store.capture_final(tree_stopped=True)
        with self.assertRaises(SnapshotError):
            store.start()

    def test_sqlite_source_busy_is_bounded_and_source_unmodified(self) -> None:
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        store = self.store(timeout_seconds=0.08)
        started = time.monotonic()
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(caught.exception.code, "SNAPSHOT_FINAL_CAPTURE_TIMED_OUT")
        writer.rollback()
        self.wait_for(lambda: store.summary()["worker_stopped_confirmed"])
        self.assertIsNone(store.latest)

    def test_duplicate_pair_mapping_is_rejected_by_complete_validator(self) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM two_view_geometries WHERE pair_id=?", (2147483647 + 3,))
        store = self.store()
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "CHECKPOINT_SQLITE_INCOMPLETE_PAIRS")
        self.assertIsNone(store.latest)

    def test_database_modified_during_validation_is_not_published(self) -> None:
        def changed(target, names, health):
            with target.open("ab") as stream:
                stream.write(b"changed after validation")
            return 2
        store = self.store(validator=changed)
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_DATABASE_HASH_CHANGED")
        self.assertFalse((self.root / "latest-valid.json").exists())

    def test_pair_count_regression_keeps_previous_valid_pointer(self) -> None:
        store = self.store()
        first = store.capture_final(tree_stopped=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            for table in ("matches", "two_view_geometries"):
                db.execute(f"DELETE FROM {table} WHERE pair_id=?", (2147483647 + 3,))
        with self.assertRaises(SnapshotError) as caught:
            store.capture_final(tree_stopped=True)
        self.assertEqual(caught.exception.code, "SNAPSHOT_PAIR_COUNT_REGRESSED")
        self.assertEqual(store.latest, first)

    def test_mock_reparse_source_rejected(self) -> None:
        original = Path.lstat
        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.database:
                class Fake:
                    st_mode = info.st_mode
                    st_file_attributes = 1024
                return Fake()
            return info
        with patch.object(Path, "lstat", reparse), self.assertRaises(SnapshotError) as caught:
            self.store()
        self.assertEqual(caught.exception.code, "SNAPSHOT_LINK_OR_REPARSE_REJECTED")

    def test_invalid_intervals_limits_and_stop_budget_fail_closed(self) -> None:
        for key, value in (("interval_seconds", 0), ("interval_seconds", float("nan")),
                           ("timeout_seconds", False), ("min_free_bytes", -1)):
            with self.assertRaises(SnapshotError):
                self.store(**{key: value})
        store = self.store()
        for value in (6, -1, float("inf"), True):
            with self.assertRaises(SnapshotError):
                store.stop(value)


if __name__ == "__main__":
    unittest.main()
