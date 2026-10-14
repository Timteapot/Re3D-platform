"""Local immutable packaging/restore checks; no server or reconstruction."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/build_long_toolpack.py"
spec = importlib.util.spec_from_file_location("build_long_toolpack_under_test", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@unittest.skipUnless(shutil.which("git"), "Git is required for immutable blob fixtures")
class LongToolpackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="re3d-toolpack-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repository = self.base / "repo"
        self.repository.mkdir()
        self.output_parent = self.base / "packages"
        self.output_parent.mkdir()
        self.profile_directory = self.base / "profile"
        self.profile_directory.mkdir()
        self.git("init", "-q")
        for name in (*builder.CODE_FILES, *builder.DOCUMENT_FILES):
            path = self.repository.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("# immutable fixture " + name + "\n").encode("utf-8"))
        self.git("add", "--all")
        self.git("-c", "user.name=Local Toolpack Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "immutable fixture")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.overlay = {"a_v4": {"openmvs_threads": 32}, "b_v2": {"openmvs_threads": 32},
                        "c": {"openmvs_threads": 32}, "texture": {"max_threads": 32}}
        compact = (json.dumps(self.overlay, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        self.profile = {"profile_kind": "offline_high_cpu_candidate", "autorun": False, "server_applied": False,
            "full_guarded_launcher_ready": False, "prepared_overlay_sha256": hashlib.sha256(compact).hexdigest(),
            "pipeline": {"commit": builder.PIPELINE_BASE, "baseline_config_sha256": builder.BASE_CONFIG_SHA,
                         "baseline_modified": False, "quality_parameters_changed": False},
            "cpu": {"sfm_threads": 12, "mapper_ba_threads": 32, "openmvs_threads": 32,
                    "texture_threads": 32, "global_blas_omp_override": None},
            "target_hardware": {"logical_cpus": 40},
            "protection": {"disk_floor_bytes": 20 * 1024**3, "reservation_bytes": 64 * 1024**3,
                "available_ram_floor_bytes": 32 * 1024**3, "free_vram_floor_bytes": 2 * 1024**3,
                "host_guard_seconds": 1, "gpu_sample_seconds": 5, "directory_sample_seconds": 15,
                "overall_timeout_seconds": 14400},
            "gpu_preserved": {"task_concurrency": 1, "branches_serial": True, "map_outer_batch": 4,
                "map_dense_head_minibatch": 1, "map_use_amp": True, "map_requested_amp_dtype": "bf16",
                "mvs_source_views": 7, "mvs_height": 384, "mvs_width": 384, "mvs_refinement_steps": 1}}
        self.diff = {"baseline_config_sha256": builder.BASE_CONFIG_SHA, "diff": [
            {"path": "/a_v4/openmvs_threads", "before": 16, "after": 32},
            {"path": "/b_v2/openmvs_threads", "before": 24, "after": 32},
            {"path": "/c/openmvs_threads", "before": 24, "after": 32},
            {"path": "/texture/max_threads", "before": 24, "after": 32}],
            "quality_settings_changed": False, "gpu_settings_changed": False, "server_applied": False}
        self.save_configs()

    def git(self, *args):
        return subprocess.run(["git", "-c", "safe.directory=" + self.repository.as_posix(), "-C", str(self.repository), *args],
                              capture_output=True, text=True, check=True, timeout=15)

    def save_configs(self):
        for name, value in zip(builder.CONFIG_FILES, (self.profile, self.overlay, self.diff)):
            (self.profile_directory / name).write_bytes((json.dumps(value, indent=2) + "\n").encode("utf-8"))

    def build(self, name="first", **kwargs):
        return builder.build_toolpack(self.repository, self.commit, self.profile_directory,
            self.output_parent / name, allowed_output_parent=self.output_parent, **kwargs)

    def test_immutable_git_blobs_ignore_dirty_source_untracked_secrets_and_logs(self):
        selected = self.repository / builder.CODE_FILES[0]
        selected.write_text("# dirty, must not be packaged\n", encoding="utf-8")
        (self.repository / ".env.worker.restricted").write_text("SHOULD_NEVER_COPY=fixture\n", encoding="utf-8")
        (self.profile_directory / "raw.log").write_text("SHOULD_NEVER_COPY\n", encoding="utf-8")
        receipt = self.build()
        payload = self.output_parent / "first/payload"
        self.assertEqual((payload / builder.CODE_FILES[0]).read_bytes(),
                         ("# immutable fixture " + builder.CODE_FILES[0] + "\n").encode("utf-8"))
        self.assertFalse((payload / ".env.worker.restricted").exists())
        self.assertFalse((payload / "config/raw.log").exists())
        self.assertEqual(receipt["source_commit"], self.commit)
        self.assertEqual(receipt["target_platform_commit"], builder.PLATFORM_BASE)
        self.assertNotEqual(receipt["source_commit"], receipt["target_platform_commit"])
        self.assertFalse(receipt["server_contacted"])
        self.assertFalse(receipt["server_deployed"])

    def test_exact_14_code_files_and_full_manifest_and_zip_restoration(self):
        receipt = self.build()
        root = self.output_parent / "first"
        tool = json.loads((root / "payload/tool-manifest.json").read_bytes())
        bundle = json.loads((root / "payload/bundle-manifest.json").read_bytes())
        self.assertEqual(set(tool["files"]), set(builder.CODE_FILES))
        self.assertEqual(len(tool["files"]), 14)
        self.assertEqual(len(bundle["files"]), 24)
        self.assertEqual(receipt["restore"]["file_count"], 25)
        self.assertEqual(builder.verify_restored(root / "payload"), builder.verify_restored(root / "restore-verification"))
        self.assertEqual(receipt["archive_sha256"], builder.sha((root / receipt["archive"]).read_bytes()))
        self.assertNotEqual(receipt["formatted_overlay_sha256"], receipt["prepared_overlay_sha256"])

    def test_identical_inputs_produce_identical_archive_bytes(self):
        first, second = self.build("first"), self.build("second")
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        self.assertEqual(first["tool_manifest_sha256"], second["tool_manifest_sha256"])
        self.assertEqual(first["bundle_manifest_sha256"], second["bundle_manifest_sha256"])

    def test_preexisting_output_is_never_overwritten(self):
        root = self.output_parent / "first"
        root.mkdir()
        (root / "keep").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_OUTPUT_NOT_NEW_CONTROLLED_CHILD"):
            self.build()
        self.assertEqual((root / "keep").read_text(), "keep")

    def test_output_must_be_direct_child_of_controlled_parent(self):
        for target in (self.base / "elsewhere", self.output_parent, self.output_parent / "nested/child"):
            with self.subTest(target=target), self.assertRaises(builder.ToolpackError):
                builder.build_toolpack(self.repository, self.commit, self.profile_directory, target,
                                      allowed_output_parent=self.output_parent)

    def test_commit_requires_full_hex_identity_before_any_git_call(self):
        for value in ("HEAD", self.commit[:7], "--help", "a" * 41, "A" * 40):
            with self.subTest(value=value), patch.object(builder.subprocess, "run", side_effect=AssertionError("must reject first")), \
                    self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_FULL_SOURCE_COMMIT_REQUIRED"):
                builder.build_toolpack(self.repository, value, self.profile_directory, self.output_parent / "first",
                                      allowed_output_parent=self.output_parent)

    def test_missing_git_allowlist_blob_fails_before_creating_output(self):
        (self.repository / builder.DOCUMENT_FILES[0]).unlink()
        self.git("add", "--all")
        self.git("-c", "user.name=Local Toolpack Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "missing doc")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_GIT_BLOB_INVALID"):
            self.build()
        self.assertFalse((self.output_parent / "first").exists())

    def test_formatted_overlay_must_match_compact_lf_runtime_hash(self):
        self.overlay["unexpected"] = "changed"
        self.save_configs()
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_OVERLAY_COMPACT_HASH_MISMATCH"):
            self.build()
        self.assertFalse((self.output_parent / "first").exists())

    def test_profile_and_diff_cannot_claim_execution_or_change_quality(self):
        cases = [("profile", "autorun", True), ("profile", "server_applied", True),
                 ("profile", "full_guarded_launcher_ready", True), ("diff", "quality_settings_changed", True),
                 ("diff", "gpu_settings_changed", True)]
        for document, field, value in cases:
            selected = getattr(self, document)
            previous = selected[field]
            try:
                selected[field] = value
                self.save_configs()
                with self.subTest(document=document, field=field), self.assertRaises(builder.ToolpackError):
                    self.build()
                self.assertFalse((self.output_parent / "first").exists())
            finally:
                selected[field] = previous

    def test_gpu_boolean_types_cannot_be_replaced_by_equal_integers(self):
        self.profile["gpu_preserved"]["map_use_amp"] = 1
        self.save_configs()
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_GPU_PROFILE_CHANGED"):
            self.build()
        self.assertFalse((self.output_parent / "first").exists())

    def test_restore_detects_extra_file_and_tampered_code(self):
        self.build()
        restored = self.output_parent / "first/restore-verification"
        (restored / "unexpected").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_EXTRA_OR_MISSING_FILE"):
            builder.verify_restored(restored)
        (restored / "unexpected").unlink()
        (restored / builder.CODE_FILES[0]).write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_RESTORE_HASH_MISMATCH"):
            builder.verify_restored(restored)

    def malformed_archive(self, members):
        archive = self.base / "malformed.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive, "w") as package:
                for name, mode in members:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    package.writestr(info, b"fixture")
        return archive

    def test_archive_rejects_traversal_before_restore_root_creation(self):
        for name in ("../escape", "/absolute", "C:/outside", "a\\outside", "a//outside", "a/./outside"):
            archive = self.base / "malformed.zip"
            if archive.exists():
                archive.unlink()
            archive = self.malformed_archive([(name, stat.S_IFREG | 0o600)])
            with self.subTest(name=name), self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_ARCHIVE_PATH_INVALID"):
                builder.restore_archive(archive, self.base / "restored", expected_files={name})
            self.assertFalse((self.base / "restored").exists())

    def test_archive_rejects_duplicate_and_windows_case_colliding_members(self):
        for members in (("file", "file"), ("file", "FILE")):
            archive = self.base / "malformed.zip"
            if archive.exists():
                archive.unlink()
            archive = self.malformed_archive([(name, stat.S_IFREG | 0o600) for name in members])
            with self.subTest(members=members), self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_ARCHIVE_MEMBERS_INVALID"):
                builder.restore_archive(archive, self.base / "restored", expected_files=set(members))
            self.assertFalse((self.base / "restored").exists())

    def test_archive_rejects_symlink_member(self):
        archive = self.malformed_archive([("link", stat.S_IFLNK | 0o777)])
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_ARCHIVE_MEMBER_TYPE_INVALID"):
            builder.restore_archive(archive, self.base / "restored", expected_files={"link"})
        self.assertFalse((self.base / "restored").exists())

    def test_restore_refuses_existing_root(self):
        archive = self.malformed_archive([("file", stat.S_IFREG | 0o600)])
        restored = self.base / "restored"
        restored.mkdir()
        with self.assertRaisesRegex(builder.ToolpackError, "TOOLPACK_TARGET_EXISTS"):
            builder.restore_archive(archive, restored, expected_files={"file"})

    def test_prepare_restore_is_opt_in_at_library_layer_and_receipt_separate(self):
        with patch.object(builder, "verify_prepare_restoration", return_value={"status": "prepared_no_execution"}) as prepare:
            receipt = self.build(prepare_interpreter=Path(__file__).resolve())
        prepare.assert_called_once_with(self.output_parent / "first/restore-verification", self.repository, Path(__file__).resolve())
        self.assertEqual(receipt["offline_prepare"]["status"], "prepared_no_execution")
        self.assertNotIn("package-receipt.json", json.loads((self.output_parent / "first/payload/bundle-manifest.json").read_bytes())["files"])

    def test_real_restored_bootstrap_loads_only_unpacked_experiment_modules_without_runtime_actions(self):
        actual_repository = SCRIPT.parent.parent
        for name in builder.CODE_FILES:
            (self.repository / name).write_bytes((actual_repository / name).read_bytes())
        self.git("add", "--all")
        self.git("-c", "user.name=Local Toolpack Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "real bootstrap fixture")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.build()
        restored = self.output_parent / "first/restore-verification"
        outcome = builder.verify_prepare_restoration(restored, actual_repository, Path(sys.executable))
        self.assertEqual(outcome, {"status": "prepared_no_execution", "restored_overlay_modules_verified": 12,
                                  "runtime_probes_called": False, "reconstruction_started": False})
        self.assertFalse(any(restored.rglob("__pycache__")))

    def test_cli_failure_outputs_only_stable_code_not_private_exception(self):
        stdout = io.StringIO()
        with patch.object(builder, "build_toolpack", side_effect=RuntimeError("private local value")), redirect_stdout(stdout):
            result = builder.main(["--source-commit", self.commit, "--profile-directory", str(self.profile_directory),
                "--output-directory", str(self.output_parent / "first"), "--repository-root", str(self.repository)])
        self.assertEqual(result, 1)
        self.assertNotIn("private local value", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["error_code"], "TOOLPACK_LOCAL_BUILD_FAILED")


if __name__ == "__main__":
    unittest.main()
