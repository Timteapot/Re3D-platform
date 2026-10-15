from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts import build_local_c_toolpack as builder


class LocalCToolpackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.output = self.base / "release"
        self.git("init")
        self.git("config", "core.autocrlf", "false")
        for name in (*builder.CODE_FILES, *builder.DOCUMENT_FILES):
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("# synthetic " + name + "\nraise RuntimeError('must never execute')\n").encode())
        template = {"schema_version": "1.0", "profile_kind": "local_c_only_candidate", "autorun": False,
            "execution": {"scope": "local-only", "branches": ["c"], "gpu_required": False,
                "platform_job_resume": False, "openmvs_mode": "cpu-only-vendor-build"},
            "pipeline": {"commit": builder.PIPELINE_COMMIT},
            "source": {"root": "D:/REPLACE-WITH-VERIFIED-BUNDLE", "cpu_result_sha256": None,
                "terminal_receipt_sha256": None, "handoff_manifest_sha256": None},
            "budget": {"source_terminal_receipt_sha256": None}, "target_hardware": {},
            "target_experiment": {}, "cpu": {}, "protection": {}}
        self.write_template(template)
        self.commit()

    def write_template(self, value):
        (self.repository / builder.PROFILE_NAME).write_bytes(builder.json_bytes(value))

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "user.name=synthetic-test",
            "-c", "user.email=synthetic-test@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=" + str(self.base / "no-hooks"), "-C", str(self.repository), *arguments],
            capture_output=True, check=False, timeout=15)
        self.assertEqual(result.returncode, 0, "synthetic Git fixture failed")
        return result.stdout

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-m", "synthetic fixture")
        self.head = self.git("rev-parse", "HEAD").decode().strip()

    def test_prepare_is_read_only_with_exact_23_file_closure(self):
        before = {path.relative_to(self.base) for path in self.base.rglob("*")}
        result = builder.prepare_toolpack(self.repository)
        self.assertEqual(result["source_commit"], self.head)
        self.assertEqual(result["code_file_count"], 23)
        self.assertEqual(result["payload_file_count"], 30)
        self.assertFalse(result["autorun"])
        self.assertFalse(result["reconstruction_started"])
        self.assertEqual(before, {path.relative_to(self.base) for path in self.base.rglob("*")})

    def test_build_restores_exact_bytes_and_published_manifest_identity(self):
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        root = self.output / "restore-verification"
        self.assertEqual(receipt["restore"]["file_count"], 30)
        self.assertEqual(receipt["restore"]["code_file_count"], 23)
        self.assertEqual(receipt["source_commit"], self.head)
        for name, digest in receipt["payload_hashes"].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)
        tool = json.loads((root / builder.MANIFEST_NAME).read_bytes())
        self.assertEqual(tool["source_state"], "git_commit")
        self.assertEqual(tool["source_commit"], self.head)
        self.assertEqual(set(tool["files"]), set(builder.CODE_FILES))
        bundle = json.loads((root / builder.BUNDLE_NAME).read_bytes())
        self.assertEqual(set(bundle["files"]), builder.PAYLOAD_NAMES - {builder.BUNDLE_NAME})
        self.assertEqual(receipt["archive_sha256"], hashlib.sha256((self.output / receipt["archive"]).read_bytes()).hexdigest())
        self.assertEqual(builder.verify_payload(root, receipt["payload_hashes"]), receipt["restore"])
        self.assertFalse(receipt["server_contacted"])
        self.assertFalse(receipt["reconstruction_started"])
        self.assertEqual(receipt["release_channel"], "temporary_experiment")
        self.assertEqual(receipt["experiment_branch"], "experiment/local-c-only")
        self.assertFalse(receipt["real_c_branch_accepted"])
        self.assertIn("has not passed real C-branch", (root / builder.README_NAME).read_text())

    def test_dirty_or_untracked_repository_does_not_create_output(self):
        path = self.repository / builder.CODE_FILES[0]
        original = path.read_bytes()
        path.write_bytes(original + b"# dirty\n")
        with self.assertRaisesRegex(builder.ToolpackError, "REPOSITORY_DIRTY"):
            builder.build_toolpack(self.repository, self.head, self.output)
        path.write_bytes(original)
        (self.repository / "secret.env").write_bytes(b"not-packaged\n")
        with self.assertRaisesRegex(builder.ToolpackError, "REPOSITORY_DIRTY"):
            builder.prepare_toolpack(self.repository)
        self.assertFalse(self.output.exists())

    def test_source_must_be_full_clean_head(self):
        old_head = self.head
        (self.repository / builder.CODE_FILES[0]).write_bytes(b"# another fixture\n")
        self.commit()
        for value, error in ((old_head, "SOURCE_MUST_BE_HEAD"), (self.head[:7], "FULL_SOURCE"), ("HEAD", "FULL_SOURCE")):
            with self.subTest(value=value), self.assertRaisesRegex(builder.ToolpackError, error):
                builder.prepare_toolpack(self.repository, value)

    def test_missing_required_document_or_code_fails_closed(self):
        for name in (builder.DOCUMENT_FILES[-1], builder.CODE_FILES[0]):
            (self.repository / name).unlink()
            self.commit()
            with self.assertRaisesRegex(builder.ToolpackError, "REGULAR_GIT_BLOB"):
                builder.prepare_toolpack(self.repository)

    def test_committed_nonallowlisted_data_is_not_packaged(self):
        for name in (".env", "database.db", "photo.jpg", "runtime-manifest.json", "bound-profile.json"):
            (self.repository / name).write_bytes(b"synthetic-not-packaged\n")
        self.commit()
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        self.assertEqual(set(receipt["payload_hashes"]), builder.PAYLOAD_NAMES)

    def test_crlf_is_normalized_from_committed_blob(self):
        path = self.repository / builder.CODE_FILES[0]
        path.write_bytes(b"# synthetic CRLF\r\nvalue = 3\r\n")
        self.commit()
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        restored = self.output / "restore-verification" / builder.CODE_FILES[0]
        self.assertEqual(restored.read_bytes(), b"# synthetic CRLF\nvalue = 3\n")
        self.assertEqual(hashlib.sha256(restored.read_bytes()).hexdigest(), receipt["payload_hashes"][builder.CODE_FILES[0]])

    def test_bound_profile_or_execution_claim_is_rejected_before_output(self):
        original = json.loads((self.repository / builder.PROFILE_NAME).read_bytes())
        for mutation in (lambda v: v["source"].update(cpu_result_sha256="a" * 64),
                         lambda v: v.update(autorun=True),
                         lambda v: v["budget"].update(source_terminal_receipt_sha256="b" * 64),
                         lambda v: v.update(secret="not-allowed")):
            value = json.loads(json.dumps(original))
            mutation(value)
            self.write_template(value)
            self.commit()
            with self.assertRaisesRegex(builder.ToolpackError, "UNBOUND_TEMPLATE_REQUIRED"):
                builder.build_toolpack(self.repository, self.head, self.output)
            self.assertFalse(self.output.exists())

    def test_existing_or_overlapping_output_is_preserved(self):
        self.output.mkdir()
        (self.output / "keep").write_bytes(b"unchanged")
        with self.assertRaisesRegex(builder.ToolpackError, "TARGET_EXISTS"):
            builder.build_toolpack(self.repository, self.head, self.output)
        self.assertEqual((self.output / "keep").read_bytes(), b"unchanged")
        with self.assertRaisesRegex(builder.ToolpackError, "SOURCE_OUTPUT_OVERLAP"):
            builder.build_toolpack(self.repository, self.head, self.repository / "release")
        with self.assertRaisesRegex(builder.ToolpackError, "ABSOLUTE_PATH"):
            builder.build_toolpack(self.repository, self.head, "relative")

    def test_extra_directory_file_or_tamper_rejected(self):
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        root = self.output / "restore-verification"
        (root / "extra").mkdir()
        with self.assertRaisesRegex(builder.ToolpackError, "EXTRA_OR_MISSING"):
            builder.verify_payload(root, receipt["payload_hashes"])
        (root / "extra").rmdir()
        code = root / builder.CODE_FILES[0]
        code.write_bytes(code.read_bytes() + b"# tamper\n")
        with self.assertRaisesRegex(builder.ToolpackError, "RESTORE_HASH"):
            builder.verify_payload(root, receipt["payload_hashes"])

    def make_zip(self, files, name="malicious.zip", *, symlink=None):
        archive = self.base / name
        with zipfile.ZipFile(archive, "x") as package:
            for path, data in files.items():
                info = zipfile.ZipInfo(path)
                if path == symlink:
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(info, data)
        return archive

    def test_unsafe_archive_paths_rejected_before_any_extraction(self):
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        for index, name in enumerate(("../escape.py", "C:/escape.py", "NUL.py", "scripts\\escape.py", "/escape.py")):
            archive = self.make_zip({name: b"bad"}, f"malicious-{index}.zip")
            target = self.base / f"must-not-create-{index}"
            # ZipInfo may normalize a Windows separator before our reader sees
            # it. Exact membership must still reject that normalized name.
            with self.assertRaisesRegex(builder.ToolpackError, "RELATIVE_PATH|ARCHIVE_MEMBERS"):
                builder.restore_archive(archive, target, expected_hashes=receipt["payload_hashes"])
            self.assertFalse(target.exists())

    def test_code_closure_matches_execution_entrypoint(self):
        from scripts import run_local_c
        self.assertEqual(set(builder.CODE_FILES), set(run_local_c.CODE_FILES))

    def test_archive_case_collision_and_extra_member_fail_before_extraction(self):
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        root = self.output / "restore-verification"
        files = {name: (root / name).read_bytes() for name in builder.PAYLOAD_NAMES}
        for index, extra in enumerate((builder.CODE_FILES[0].upper(), "database.db")):
            proposed = {**files, extra: b"must not extract"}
            archive = self.make_zip(proposed, f"collision-{index}.zip")
            target = self.base / f"must-not-create-{index}"
            with self.assertRaisesRegex(builder.ToolpackError, "ARCHIVE_MEMBERS"):
                builder.restore_archive(archive, target, expected_hashes=receipt["payload_hashes"])
            self.assertFalse(target.exists())

    def test_duplicate_json_template_keys_are_rejected_before_output(self):
        path = self.repository / builder.PROFILE_NAME
        data = path.read_bytes().replace(b'{"autorun":false,', b'{"autorun":false,"autorun":false,', 1)
        path.write_bytes(data)
        self.commit()
        with self.assertRaisesRegex(builder.ToolpackError, "DOCUMENT_INVALID"):
            builder.build_toolpack(self.repository, self.head, self.output)
        self.assertFalse(self.output.exists())

    def test_archive_symlink_and_tamper_rejected_before_any_extraction(self):
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        root = self.output / "restore-verification"
        files = {name: (root / name).read_bytes() for name in builder.PAYLOAD_NAMES}
        target = self.base / "must-not-create"
        archive = self.make_zip(files, symlink=builder.CODE_FILES[0])
        with self.assertRaisesRegex(builder.ToolpackError, "MEMBER_TYPE"):
            builder.restore_archive(archive, target, expected_hashes=receipt["payload_hashes"])
        files[builder.CODE_FILES[0]] += b"# tampered\n"
        archive = self.make_zip(files, "tampered.zip")
        with self.assertRaisesRegex(builder.ToolpackError, "RESTORE_HASH"):
            builder.restore_archive(archive, target, expected_hashes=receipt["payload_hashes"])
        self.assertFalse(target.exists())

    def test_manifest_mismatch_rejected_before_any_extraction_even_with_new_external_hash(self):
        receipt = builder.build_toolpack(self.repository, self.head, self.output)
        root = self.output / "restore-verification"
        files = {name: (root / name).read_bytes() for name in builder.PAYLOAD_NAMES}
        tool = json.loads(files[builder.MANIFEST_NAME])
        tool["source_commit"] = "0" * 40
        files[builder.MANIFEST_NAME] = builder.json_bytes(tool)
        bundle = json.loads(files[builder.BUNDLE_NAME])
        bundle["files"][builder.MANIFEST_NAME] = {"sha256": builder.sha(files[builder.MANIFEST_NAME]),
                                                "bytes": len(files[builder.MANIFEST_NAME])}
        files[builder.BUNDLE_NAME] = builder.json_bytes(bundle)
        archive = self.make_zip(files)
        target = self.base / "must-not-create"
        hashes = {name: builder.sha(data) for name, data in files.items()}
        with self.assertRaisesRegex(builder.ToolpackError, "TOOL_MANIFEST_INVALID"):
            builder.restore_archive(archive, target, expected_hashes=hashes)
        self.assertFalse(target.exists())

    def test_cli_default_prepare_and_stable_error_never_execute_payload(self):
        text = io.StringIO()
        with redirect_stdout(text):
            result = builder.main(["--repository-root", str(self.repository), "--output-dir", str(self.output)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(text.getvalue())["status"], "prepared_no_execution")
        self.assertFalse(self.output.exists())
        for arguments in (["--repository-root", str(self.repository), "--build-toolpack"], ["--unknown-secret", "do-not-print"]):
            text = io.StringIO()
            with redirect_stdout(text):
                self.assertEqual(builder.main(arguments), 1)
            value = json.loads(text.getvalue())
            self.assertFalse(value["reconstruction_started"])
            self.assertNotIn("do-not-print", text.getvalue())

    def test_isolated_python_cli_prepare_loads_only_known_sibling_builder(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(builder.__file__).absolute()),
            "--repository-root", str(self.repository), "--source-commit", self.head,
            "--output-dir", str(self.output)], capture_output=True, text=True,
            timeout=30, check=False, cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "prepared_no_execution")
        self.assertEqual(value["source_commit"], self.head)
        self.assertEqual(value["release_channel"], "temporary_experiment")
        self.assertFalse(value["reconstruction_started"])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
