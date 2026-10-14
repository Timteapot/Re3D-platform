from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_hybrid_toolpack as builder


class HybridToolpackTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.output = self.base / "sealed"
        self.git("init")
        self.git("config", "core.autocrlf", "false")
        for name in builder.CODE_FILES:
            path = self.repository.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((f"# synthetic {name}\nraise RuntimeError('must never execute packaged code')\n").encode())
        (self.repository / "config").mkdir()
        (self.repository / "config/hybrid-cpu.example.json").write_bytes(b'{"autorun":false}\n')
        (self.repository / "docs").mkdir()
        (self.repository / "docs/hybrid-cpu-sfm.md").write_bytes(b"# Non-executing example\n")
        self.commit()

    def git(self, *args: str) -> bytes:
        result = subprocess.run(["git", "-c", "user.name=synthetic-test",
            "-c", "user.email=synthetic-test@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=" + str(self.base / "no-hooks"),
            "-C", str(self.repository), *args], capture_output=True, check=False, timeout=15)
        self.assertEqual(result.returncode, 0, "synthetic Git fixture operation failed")
        return result.stdout

    def commit(self) -> None:
        self.git("add", "--all")
        self.git("commit", "-m", "synthetic fixture")
        self.head = self.git("rev-parse", "HEAD").decode().strip()

    def test_clean_head_prepare_has_no_output_or_execution(self) -> None:
        before = {path.relative_to(self.base).as_posix() for path in self.base.rglob("*")}
        result = builder.prepare_toolpack(self.repository)
        self.assertEqual(result["status"], "prepared_no_execution")
        self.assertEqual(result["source_commit"], self.head)
        self.assertEqual(result["code_file_count"], 20)
        self.assertFalse(result["autorun"])
        self.assertFalse(result["reconstruction_started"])
        self.assertEqual({path.relative_to(self.base).as_posix() for path in self.base.rglob("*")}, before)

    def test_packaging_restoration_and_external_receipt_hashes(self) -> None:
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        archive = self.output / receipt["archive"]
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), receipt["archive_sha256"])
        self.assertEqual(receipt["restore"]["file_count"], 24)
        self.assertEqual(receipt["restore"]["code_file_count"], 20)
        self.assertFalse(receipt["autorun"])
        self.assertFalse(receipt["server_contacted"])
        self.assertFalse(receipt["reconstruction_started"])
        restored = self.output / "restore-verification"
        manifest = json.loads((restored / builder.MANIFEST_NAME).read_bytes())
        self.assertEqual(set(manifest), {"schema_version", "toolpack_kind", "source_commit", "files", "autorun"})
        self.assertEqual(set(manifest["files"]), set(builder.CODE_FILES))
        self.assertEqual(manifest["source_commit"], self.head)
        for name, digest in receipt["payload_hashes"].items():
            self.assertEqual(hashlib.sha256(restored.joinpath(*name.split("/")).read_bytes()).hexdigest(), digest)
        self.assertIn("No autorun", (restored / builder.README_NAME).read_text())
        self.assertNotIn(".env", receipt["payload_hashes"])
        self.assertTrue((self.output.parent / receipt["staging_directory"]).is_dir())

    def test_dirty_modified_and_untracked_files_rejected_without_output(self) -> None:
        source = self.repository / builder.CODE_FILES[0]
        before = source.read_bytes()
        source.write_bytes(before + b"# not committed\n")
        with self.assertRaisesRegex(builder.HybridToolpackError, "REPOSITORY_DIRTY"):
            builder.build_toolpack(self.repository, self.head, self.output)
        self.assertFalse(self.output.exists())
        source.write_bytes(before)
        untracked = self.repository / "uncommitted-feature.py"
        untracked.write_bytes(b"# also not HEAD\n")
        with self.assertRaisesRegex(builder.HybridToolpackError, "REPOSITORY_DIRTY"):
            builder.prepare_toolpack(self.repository)

    def test_non_head_short_and_invalid_source_references_rejected(self) -> None:
        old_head = self.head
        (self.repository / builder.CODE_FILES[0]).write_bytes(b"# changed committed fixture\n")
        self.commit()
        for source, code in ((old_head, "SOURCE_MUST_BE_HEAD"), (self.head[:7], "FULL_SOURCE"),
                             ("HEAD", "FULL_SOURCE"), ("0" * 40, "SOURCE_MUST_BE_HEAD")):
            with self.subTest(source=source), self.assertRaisesRegex(builder.HybridToolpackError, code):
                builder.prepare_toolpack(self.repository, source)
        self.assertEqual(builder.prepare_toolpack(self.repository, None)["source_commit"], self.head)

    def test_code_closure_is_exact_and_missing_committed_source_rejected(self) -> None:
        (self.repository / ".env").write_bytes(b"synthetic-not-packaged=private\n")
        (self.repository / "unexpected.py").write_bytes(b"# not in closure\n")
        self.commit()
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        self.assertEqual(set(receipt["payload_hashes"]), {*builder.CODE_FILES, *builder.OPTIONAL_FILES,
                         builder.MANIFEST_NAME, builder.README_NAME})
        self.assertNotIn(".env", receipt["payload_hashes"])
        (self.repository / builder.CODE_FILES[0]).unlink()
        self.commit()
        with self.assertRaisesRegex(builder.HybridToolpackError, "REGULAR_GIT_BLOB"):
            builder.prepare_toolpack(self.repository)

    def test_committed_crlf_and_lf_use_the_same_unpacked_identity(self) -> None:
        source = self.repository / builder.CODE_FILES[0]
        source.write_bytes(b"# committed CRLF\r\nvalue = 1\r\n")
        self.commit()
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        unpacked = (self.output / "restore-verification" / builder.CODE_FILES[0]).read_bytes()
        self.assertEqual(unpacked, b"# committed CRLF\nvalue = 1\n")
        expected = hashlib.sha256(unpacked).hexdigest()
        self.assertEqual(receipt["payload_hashes"][builder.CODE_FILES[0]], expected)
        manifest = json.loads((self.output / "restore-verification" / builder.MANIFEST_NAME).read_bytes())
        self.assertEqual(manifest["files"][builder.CODE_FILES[0]], expected)

    def test_optional_docs_absent_is_a_valid_code_only_toolpack(self) -> None:
        for name in builder.OPTIONAL_FILES:
            (self.repository / name).unlink()
        self.commit()
        receipt = builder.build_toolpack(self.repository, None, self.output)
        self.assertEqual(receipt["restore"]["file_count"], 22)

    def test_existing_output_relative_traversal_and_repository_overlap_rejected(self) -> None:
        self.output.mkdir()
        old = self.output / "keep.txt"
        old.write_bytes(b"preserve")
        with self.assertRaisesRegex(builder.HybridToolpackError, "TARGET_EXISTS"):
            builder.build_toolpack(self.repository, self.head, self.output)
        self.assertEqual(old.read_bytes(), b"preserve")
        for proposed, code in ((Path("relative"), "ABSOLUTE_PATH"),
                               (self.base / "unused/../escape", "ABSOLUTE_PATH"),
                               (self.repository / "inside", "SOURCE_OUTPUT_OVERLAP")):
            with self.subTest(proposed=proposed), self.assertRaisesRegex(builder.HybridToolpackError, code):
                builder.build_toolpack(self.repository, self.head, proposed)

    def test_reparse_output_rejected_without_build(self) -> None:
        original = Path.lstat

        class Reparse:
            st_mode = stat.S_IFDIR | 0o755
            st_file_attributes = 1024

        with patch.object(Path, "lstat", side_effect=lambda path: Reparse() if path == self.base else original(path),
                          autospec=True):
            with self.assertRaisesRegex(builder.HybridToolpackError, "REPARSE"):
                builder.build_toolpack(self.repository, self.head, self.output)

    def test_restored_tamper_and_extra_files_fail_closed(self) -> None:
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        restored = self.output / "restore-verification"
        path = restored / builder.CODE_FILES[0]
        original = path.read_bytes()
        path.write_bytes(original + b"# tampered\n")
        with self.assertRaisesRegex(builder.HybridToolpackError, "HASH_MISMATCH"):
            builder.verify_payload(restored, receipt["payload_hashes"])
        path.write_bytes(original)
        (restored / "database.db").write_bytes(b"not allowed")
        with self.assertRaisesRegex(builder.HybridToolpackError, "EXTRA_OR_MISSING"):
            builder.verify_payload(restored, receipt["payload_hashes"])

    def test_archive_traversal_and_code_tamper_rejected_before_restoration(self) -> None:
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        malicious = self.base / "malicious.zip"
        with zipfile.ZipFile(malicious, "w") as package:
            package.writestr("../escape.py", b"# invalid")
        target = self.base / "must-not-create"
        with self.assertRaisesRegex(builder.HybridToolpackError, "RELATIVE_PATH"):
            builder.restore_archive(malicious, target, expected_hashes=receipt["payload_hashes"])
        self.assertFalse(target.exists())
        archive = self.output / receipt["archive"]
        with zipfile.ZipFile(archive) as package:
            files = {name: package.read(name) for name in package.namelist()}
        files[builder.CODE_FILES[0]] += b"# tampered\n"
        with zipfile.ZipFile(self.base / "tampered.zip", "w") as package:
            for name, data in files.items():
                package.writestr(name, data)
        with self.assertRaisesRegex(builder.HybridToolpackError, "ARCHIVE_HASH"):
            builder.restore_archive(self.base / "tampered.zip", target, expected_hashes=receipt["payload_hashes"])
        self.assertFalse(target.exists())

    def test_cli_defaults_to_prepare_and_explicit_build_requires_output(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = builder.main(["--repository-root", str(self.repository), "--output-dir", str(self.output)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "prepared_no_execution")
        self.assertFalse(self.output.exists())
        with redirect_stdout(io.StringIO()):
            code = builder.main(["--repository-root", str(self.repository), "--build-toolpack"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
