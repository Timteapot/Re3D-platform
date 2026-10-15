from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.experiments import c_integrity as integrity


class CIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "config.json"
        self.file.write_bytes(b'{"fixture":true}\n')
        self.sha = hashlib.sha256(self.file.read_bytes()).hexdigest()

    def test_leaf_presence_and_small_hash_are_read_only(self):
        before = (self.file.read_bytes(), self.file.stat().st_mtime_ns, set(self.root.iterdir()))
        value = integrity.inspect_path(self.file, expected_sha256=self.sha)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["hash_status"], "verified")
        self.assertFalse(value["ancestor_chain_verified"])
        self.assertFalse(value["data_modified"])
        self.assertEqual((self.file.read_bytes(), self.file.stat().st_mtime_ns, set(self.root.iterdir())), before)

    def test_missing_and_denied_are_different(self):
        self.assertEqual(integrity.inspect_path(self.root / "missing")["status"], "missing")
        with patch.object(Path, "lstat", side_effect=PermissionError(errno.EACCES, "private message")):
            value = integrity.inspect_path(self.file)
        self.assertEqual(value["status"], "denied")
        self.assertNotIn("private message", str(value))
        denied = OSError("not a FileNotFoundError")
        denied.winerror = 5
        with patch.object(Path, "lstat", side_effect=denied):
            self.assertEqual(integrity.inspect_path(self.file)["status"], "denied")

    def test_other_io_error_is_unavailable_not_missing(self):
        with patch.object(Path, "lstat", side_effect=OSError(errno.EIO, "opaque native error")):
            value = integrity.inspect_path(self.file)
        self.assertEqual(value["status"], "unavailable")
        self.assertNotIn("opaque", str(value))

    def test_directory_and_wrong_kind_do_not_hash(self):
        self.assertEqual(integrity.inspect_path(self.root, expected_kind="directory")["status"], "ok")
        self.assertEqual(integrity.inspect_path(self.root)["status"], "wrong_kind")
        self.assertEqual(integrity.inspect_path(self.file, expected_kind="directory")["status"], "wrong_kind")
        with patch.object(os, "open", side_effect=AssertionError("must not open directory")):
            value = integrity.inspect_path(self.root, expected_kind="directory", expected_sha256=self.sha)
        self.assertEqual(value["status"], "wrong_kind")

    def test_large_file_hash_is_deferred_without_open(self):
        with patch.object(os, "open", side_effect=AssertionError("must not hash large source")):
            value = integrity.inspect_path(self.file, expected_sha256=self.sha, max_hash_bytes=4)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["hash_status"], "deferred_large_file")
        self.assertEqual(value["size_bytes"], self.file.stat().st_size)

    def test_hash_exact_size_limit_and_mismatch(self):
        value = integrity.inspect_path(self.file, expected_sha256=self.sha,
                                       max_hash_bytes=self.file.stat().st_size)
        self.assertEqual(value["hash_status"], "verified")
        self.assertEqual(integrity.inspect_path(self.file, expected_sha256="0" * 64)["status"], "hash_mismatch")

    def test_leaf_reparse_rejected_before_hash_open(self):
        with patch.object(integrity, "_is_reparse", return_value=True), \
                patch.object(os, "open", side_effect=AssertionError("must not follow reparse")):
            value = integrity.inspect_path(self.file, expected_sha256=self.sha)
        self.assertEqual(value["status"], "reparse")
        self.assertEqual(value["hash_status"], "not_opened")

    def test_read_denied_remains_unverified_without_content_or_error_text(self):
        with patch.object(os, "open", side_effect=PermissionError(errno.EACCES, "secret error message")):
            value = integrity.inspect_path(self.file, expected_sha256=self.sha)
        self.assertEqual(value["status"], "denied")
        self.assertEqual(value["hash_status"], "not_verified")
        self.assertNotIn("secret", str(value))

    def test_invalid_paths_expectations_and_hash_budget_never_probe(self):
        invalid = [(Path("relative"), {}), (self.root / ".." / "elsewhere", {}),
                   (self.file, {"expected_kind": "socket"}), (self.file, {"expected_sha256": "not-a-pin"}),
                   (self.file, {"max_hash_bytes": True}),
                   (self.file, {"max_hash_bytes": integrity.DEFAULT_HASH_LIMIT + 1})]
        with patch.object(Path, "lstat", side_effect=AssertionError("must not probe invalid input")):
            for path, options in invalid:
                with self.subTest(path=path, options=options):
                    self.assertEqual(integrity.inspect_path(path, **options)["status"], "invalid_path")

    def test_no_parent_listing_resolve_or_recursive_inventory(self):
        with patch.object(Path, "iterdir", side_effect=AssertionError("no directory enumeration")), \
                patch.object(Path, "rglob", side_effect=AssertionError("no recursive search")), \
                patch.object(Path, "resolve", side_effect=AssertionError("no ancestor resolution")):
            report = integrity.inspect_requirements([
                {"name": "root", "path": self.root, "expected_kind": "directory"},
                {"name": "config", "path": self.file, "expected_sha256": self.sha},
            ])
        self.assertEqual(report["status"], "observed_requirements_present")
        self.assertFalse(report["execution_ready"])
        self.assertFalse(report["recursive_inventory_performed"])
        self.assertFalse(report["runtime_import_performed"])

    def test_denied_explicit_directory_blocks_child_probes_without_false_missing(self):
        original = Path.lstat
        seen = []
        def probe(path):
            seen.append(path)
            if path == self.root:
                raise PermissionError(errno.EACCES, "private parent")
            return original(path)
        with patch.object(Path, "lstat", autospec=True, side_effect=probe):
            report = integrity.inspect_requirements([
                {"name": "private_root", "path": self.root, "expected_kind": "directory"},
                {"name": "private_child", "path": self.root / "not_visible.json", "expected_sha256": self.sha},
            ])
        self.assertEqual(report["status"], "unverified_or_blocked")
        self.assertEqual([row["status"] for row in report["observations"]], ["denied", "denied"])
        self.assertTrue(report["observations"][1]["ancestor_probe_blocked"])
        self.assertEqual(seen, [self.root])

    def test_reparse_explicit_directory_blocks_descendants(self):
        with patch.object(integrity, "_is_reparse", return_value=True):
            report = integrity.inspect_requirements([
                {"name": "root", "path": self.root, "expected_kind": "directory"},
                {"name": "child", "path": self.file, "expected_sha256": self.sha},
            ])
        self.assertEqual([row["status"] for row in report["observations"]], ["reparse", "reparse"])
        self.assertTrue(report["observations"][1]["ancestor_probe_blocked"])

    def test_bad_or_duplicate_requirements_rejected(self):
        for rows in ([{"path": self.file}], [{"name": "x", "path": self.file, "unknown": True}],
                     [{"name": "x", "path": self.file}, {"name": "x", "path": self.file}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "C_INTEGRITY_REQUIREMENT_INVALID"):
                integrity.inspect_requirements(rows)

    def test_large_deferred_count_is_explicit_not_execution_acceptance(self):
        report = integrity.inspect_requirements([{"name": "large_source", "path": self.file,
            "expected_sha256": self.sha, "max_hash_bytes": 4}])
        self.assertEqual(report["large_hashes_deferred"], 1)
        self.assertFalse(report["execution_ready"])


if __name__ == "__main__":
    unittest.main()
