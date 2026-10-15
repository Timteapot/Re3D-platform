from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import ctypes
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import initialize_cpu_evidence as init


def expected_acl():
    return {"owner_sid": init.ADMIN_SID, "dacl_protected": True, "aces": [
        {"type": 0, "flags": init.INHERIT_BOTH, "mask": init.FULL_CONTROL, "sid": init.SYSTEM_SID},
        {"type": 0, "flags": init.INHERIT_BOTH, "mask": init.FULL_CONTROL, "sid": init.ADMIN_SID},
    ]}


class FakeWindowsAPI:
    """Windows paths map only into a disposable fixture, never the real C disk."""
    def __init__(self, fixture):
        self.paths = {init.ROOT: fixture / "C", init.PROGRAM_DATA: fixture / "C/ProgramData",
                      init.PRIVATE_PARENT: fixture / "C/ProgramData/Re3DRestricted"}
        self.paths[init.PROGRAM_DATA].mkdir(parents=True)
        self.admin, self.free, self.calls = True, 20 * 1024**3, []
        self.security = expected_acl()
        self.attribute_overrides = {}
        self.acl_error = self.create_error = None
        self.race = False

    def is_admin(self): self.calls.append(("admin",)); return self.admin
    def disk_free(self): self.calls.append(("disk",)); return self.free
    def attributes(self, path):
        self.calls.append(("attributes", path))
        if path in self.attribute_overrides:
            value = self.attribute_overrides[path]
            if isinstance(value, Exception): raise value
            return value
        physical = self.paths[path]
        return (init.DIRECTORY_ATTRIBUTE if physical.is_dir() else 0) if physical.exists() else None
    def acl(self):
        self.calls.append(("acl",))
        if self.acl_error: raise self.acl_error
        return deepcopy(self.security)
    def create(self):
        self.calls.append(("atomic-create", init.PRIVATE_PARENT, init.PARENT_SDDL))
        if self.create_error: raise self.create_error
        self.paths[init.PRIVATE_PARENT].mkdir()
        return not self.race


class InitializerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.api = FakeWindowsAPI(Path(self.temporary.name))

    def tearDown(self): self.temporary.cleanup()

    def inspect(self): return init.perform("inspect", _api=self.api)
    def initialize(self): return init.perform("initialize", _api=self.api)
    def seed_target(self): self.api.paths[init.PRIVATE_PARENT].mkdir()
    def assert_no_create(self):
        self.assertFalse(any(call[0] == "atomic-create" for call in self.api.calls))

    def test_default_inspection_missing_is_read_only_and_never_creates(self):
        result = self.inspect()
        self.assertEqual(result["status"], "initialization_required")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["created"])
        self.assertFalse(result["parent_ready"])
        self.assert_no_create()
        self.assertFalse(self.api.paths[init.PRIVATE_PARENT].exists())

    def test_existing_private_parent_inspected_without_mutation(self):
        self.seed_target()
        sentinel = self.api.paths[init.PRIVATE_PARENT] / "do-not-traverse.bin"
        sentinel.write_bytes(b"fixture marker")
        result = self.inspect()
        self.assertEqual(result["status"], "already_initialized")
        self.assertTrue(result["acl_verified"])
        self.assertTrue(result["read_only"])
        self.assert_no_create()
        self.assertEqual(sentinel.read_bytes(), b"fixture marker")

    def test_exact_initialize_creates_only_fixed_parent_with_private_acl(self):
        result = self.initialize()
        self.assertEqual(result["status"], "initialized")
        self.assertTrue(result["administrator"])
        self.assertTrue(result["created"])
        self.assertTrue(result["acl_verified"])
        self.assertTrue(result["parent_ready"])
        self.assertFalse(result["read_only"])
        self.assertIn(("atomic-create", init.PRIVATE_PARENT, init.PARENT_SDDL), self.api.calls)
        self.assertEqual(list(self.api.paths[init.PRIVATE_PARENT].iterdir()), [])
        self.assertFalse(result["reconstruction_started"])
        self.assertFalse(result["server_accessed"])
        self.assertFalse(result["policy_changed"])

    def test_existing_valid_initialize_is_validation_only(self):
        self.seed_target()
        result = self.initialize()
        self.assertEqual(result["status"], "already_initialized")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["created"])
        self.assert_no_create()

    def test_non_admin_initialization_rejected_even_for_valid_existing_parent(self):
        self.api.admin = False
        self.seed_target()
        result = self.initialize()
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_ADMIN_REQUIRED")
        self.assert_no_create()
        self.assertEqual(self.api.calls, [("admin",)])

    def test_non_admin_read_only_inspection_distinguishes_access_denied_from_missing(self):
        self.api.admin = False
        self.seed_target()
        self.api.acl_error = init.InitializerError("CPU_EVIDENCE_ACL_ACCESS_DENIED")
        result = self.inspect()
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_ACL_ACCESS_DENIED")
        self.assertNotEqual(result["status"], "initialization_required")
        self.assert_no_create()

    def test_c_free_ten_gib_is_required_before_creation(self):
        self.api.free = init.MINIMUM_FREE_BYTES - 1
        result = self.initialize()
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_DISK_ADMISSION_FAILED")
        self.assert_no_create()
        self.api.free = init.MINIMUM_FREE_BYTES
        self.assertTrue(self.initialize()["created"])

    def test_malformed_disk_capacity_does_not_initialize(self):
        for value in (None, True, -1, 1.5):
            self.api.free = value
            with self.subTest(value=value):
                self.assertEqual(self.initialize()["error_code"], "CPU_EVIDENCE_DISK_PROBE_FAILED")
        self.assert_no_create()

    def test_reparse_point_at_any_component_rejected_without_recursion(self):
        for path in (init.ROOT, init.PROGRAM_DATA, init.PRIVATE_PARENT):
            self.api.attribute_overrides = {path: init.DIRECTORY_ATTRIBUTE | init.REPARSE_ATTRIBUTE}
            with self.subTest(path=path):
                result = self.initialize()
                self.assertEqual(result["error_code"], "CPU_EVIDENCE_REPARSE_REJECTED")
                self.assert_no_create()

    def test_file_with_target_name_is_never_replaced(self):
        self.api.paths[init.PRIVATE_PARENT].write_bytes(b"original file")
        result = self.initialize()
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_NOT_A_DIRECTORY")
        self.assert_no_create()
        self.assertEqual(self.api.paths[init.PRIVATE_PARENT].read_bytes(), b"original file")

    def test_invalid_existing_acl_owner_ace_flags_mask_and_extra_principals_never_repaired(self):
        self.seed_target()
        baseline = expected_acl()
        mutations = []
        for key, value in (("owner_sid", "untrusted-owner"), ("dacl_protected", False)):
            candidate = deepcopy(baseline); candidate[key] = value; mutations.append(candidate)
        for key, value in (("type", 1), ("flags", init.INHERIT_BOTH | 16), ("mask", 0),
                           ("sid", "untrusted-principal")):
            candidate = deepcopy(baseline); candidate["aces"][0][key] = value; mutations.append(candidate)
        candidate = deepcopy(baseline); candidate["aces"].append(deepcopy(candidate["aces"][0])); mutations.append(candidate)
        candidate = deepcopy(baseline); candidate["aces"][1] = deepcopy(candidate["aces"][0]); mutations.append(candidate)
        for security in mutations:
            self.api.security = deepcopy(security)
            with self.subTest(security=security):
                result = self.initialize()
                self.assertEqual(result["error_code"], "CPU_EVIDENCE_EXISTING_ACL_INVALID")
                self.assertFalse(result["acl_verified"])
                self.assertEqual(self.api.security, security)
                self.assert_no_create()

    def test_access_or_native_create_failure_retains_unknown_outcome_and_never_deletes(self):
        self.api.create_error = init.InitializerError("CPU_EVIDENCE_CREATE_FAILED")
        result = self.initialize()
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_CREATE_FAILED")
        self.assertTrue(result["creation_attempted"])
        self.assertFalse(result["creation_outcome_confirmed"])
        self.assertFalse(self.api.paths[init.PRIVATE_PARENT].exists())

    def test_raced_existing_object_must_pass_same_acl_without_repair(self):
        self.api.race = True
        self.api.security["owner_sid"] = "untrusted-owner"
        result = self.initialize()
        self.assertFalse(result["created"])
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_EXISTING_ACL_INVALID")
        self.assertTrue(self.api.paths[init.PRIVATE_PARENT].is_dir())

    def test_acl_verification_failure_after_creation_preserves_directory_and_reports_created(self):
        self.api.security["dacl_protected"] = False
        result = self.initialize()
        self.assertTrue(result["created"])
        self.assertFalse(result["acl_verified"])
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_EXISTING_ACL_INVALID")
        self.assertTrue(self.api.paths[init.PRIVATE_PARENT].is_dir())

    def test_native_exception_text_and_foreign_acl_identity_never_echoed(self):
        self.api.attribute_overrides[init.PROGRAM_DATA] = RuntimeError("private secret exception")
        result = self.initialize()
        self.assertEqual(result["error_code"], "CPU_EVIDENCE_NATIVE_CALL_FAILED")
        self.assertNotIn("secret", json.dumps(result))


class NativeCreationWiringTests(unittest.TestCase):
    def test_native_attribute_access_denial_is_not_mistaken_for_missing(self):
        native = init.NativeWindowsAPI.__new__(init.NativeWindowsAPI)
        native.kernel = SimpleNamespace(GetFileAttributesW=lambda path: 0xFFFFFFFF)
        with patch.object(init.ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaisesRegex(init.InitializerError, "CPU_EVIDENCE_PATH_ACCESS_DENIED"):
                native.attributes(init.PRIVATE_PARENT)
        with patch.object(init.ctypes, "get_last_error", return_value=2, create=True):
            self.assertIsNone(native.attributes(init.PRIVATE_PARENT))

    def test_native_directory_creation_receives_descriptor_atomically_and_frees_it(self):
        calls = []
        def convert(sddl, revision, output, length):
            calls.append(("convert", sddl, revision))
            output._obj.value = 1234
            return True
        def create(path, security):
            value = security._obj
            calls.append(("create", path, value.nLength, value.lpSecurityDescriptor, value.bInheritHandle))
            return True
        native = init.NativeWindowsAPI.__new__(init.NativeWindowsAPI)
        native.advapi = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=convert)
        native.kernel = SimpleNamespace(CreateDirectoryW=create, LocalFree=lambda pointer: calls.append(("free", pointer.value)))
        self.assertTrue(native.create())
        self.assertEqual(calls[0], ("convert", init.PARENT_SDDL, 1))
        self.assertEqual(calls[1], ("create", init.PRIVATE_PARENT, ctypes.sizeof(init._SecurityAttributes), 1234, False))
        self.assertEqual(calls[2], ("free", 1234))


class CLITests(unittest.TestCase):
    def invoke(self, arguments, value):
        output = io.StringIO()
        with patch.object(init, "perform", return_value=value) as perform, redirect_stdout(output):
            exit_code = init.main(arguments)
        return exit_code, json.loads(output.getvalue()), perform

    def test_cli_default_inspects_and_exact_single_initialize_is_the_only_write_switch(self):
        for arguments, operation in (([], "inspect"), (["--initialize"], "initialize")):
            exit_code, result, perform = self.invoke(arguments, {"status": "already_initialized", "created": False})
            self.assertEqual(exit_code, 0)
            perform.assert_called_once_with(operation)

    def test_unknown_duplicate_abbreviated_and_target_arguments_rejected_without_host_probe(self):
        for arguments in (["--initial"], ["--initialize", "--initialize"], ["--initialize=true"],
                          ["--initialize", "--target", "D:\\foreign"], ["private-token"]):
            with self.subTest(arguments=arguments):
                exit_code, result, perform = self.invoke(arguments, {})
                self.assertEqual(exit_code, 1)
                self.assertEqual(result["error_code"], "CPU_EVIDENCE_INVALID_ARGUMENTS")
                perform.assert_not_called()
                self.assertNotIn("private-token", json.dumps(result))

    def test_error_results_have_nonzero_exit(self):
        exit_code, result, _ = self.invoke(["--initialize"], {
            "status": "not_completed", "error_code": "CPU_EVIDENCE_ADMIN_REQUIRED", "created": False})
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
