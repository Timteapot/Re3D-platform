"""Inspect or atomically initialize the fixed local CPU evidence parent.

Default inspection does not create directories, write receipts, change ACLs,
launch reconstruction, or invoke PowerShell. Only the single exact argument
--initialize permits creation, and only with an elevated Windows admin token.
Existing objects are never repaired, overwritten, deleted or traversed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import sys

ROOT = "C:\\"
PROGRAM_DATA = r"C:\ProgramData"
PRIVATE_PARENT = r"C:\ProgramData\Re3DRestricted"
PARENT_SDDL = "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
MINIMUM_FREE_BYTES = 10 * 1024**3
ADMIN_SID = "S-1-5-32-544"
SYSTEM_SID = "S-1-5-18"
DIRECTORY_ATTRIBUTE = 0x10
REPARSE_ATTRIBUTE = 0x400
FULL_CONTROL = 0x1F01FF
INHERIT_BOTH = 0x03


class InitializerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise InitializerError(code)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]


class NativeWindowsAPI:
    """Small, fail-closed native surface; no profile/module/third-party dependency."""
    def __init__(self):
        require(os.name == "nt", "CPU_EVIDENCE_WINDOWS_REQUIRED")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self._function(self.shell, "IsUserAnAdmin", wintypes.BOOL)
        self._function(self.kernel, "GetFileAttributesW", wintypes.DWORD, wintypes.LPCWSTR)
        self._function(self.kernel, "GetDiskFreeSpaceExW", wintypes.BOOL, wintypes.LPCWSTR,
                       ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
                       ctypes.POINTER(ctypes.c_ulonglong))
        self._function(self.kernel, "CreateDirectoryW", wintypes.BOOL, wintypes.LPCWSTR,
                       ctypes.POINTER(_SecurityAttributes))
        self._function(self.kernel, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
        self._function(self.advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL,
                       wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                       ctypes.POINTER(wintypes.DWORD))
        self._function(self.advapi, "GetNamedSecurityInfoW", wintypes.DWORD,
                       wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                       ctypes.POINTER(ctypes.c_void_p))
        self._function(self.advapi, "GetSecurityDescriptorControl", wintypes.BOOL, ctypes.c_void_p,
                       ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD))
        self._function(self.advapi, "GetAclInformation", wintypes.BOOL, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.c_int)
        self._function(self.advapi, "GetAce", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(ctypes.c_void_p))
        self._function(self.advapi, "IsValidSid", wintypes.BOOL, ctypes.c_void_p)
        self._function(self.advapi, "GetLengthSid", wintypes.DWORD, ctypes.c_void_p)
        self._function(self.advapi, "ConvertSidToStringSidW", wintypes.BOOL, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p))

    @staticmethod
    def _function(library, name, result, *arguments):
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments

    def is_admin(self):
        return bool(self.shell.IsUserAnAdmin())

    def attributes(self, path):
        value = self.kernel.GetFileAttributesW(path)
        if value == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            if error in (2, 3):
                return None
            if error == 5:
                raise InitializerError("CPU_EVIDENCE_PATH_ACCESS_DENIED")
            raise InitializerError("CPU_EVIDENCE_PATH_PROBE_FAILED")
        return int(value)

    def disk_free(self):
        available, total, free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        require(self.kernel.GetDiskFreeSpaceExW(ROOT, ctypes.byref(available), ctypes.byref(total),
                                               ctypes.byref(free)), "CPU_EVIDENCE_DISK_PROBE_FAILED")
        return int(available.value)

    def _sid(self, pointer):
        require(pointer and self.advapi.IsValidSid(pointer), "CPU_EVIDENCE_ACL_PROBE_FAILED")
        text = ctypes.c_void_p()
        require(self.advapi.ConvertSidToStringSidW(pointer, ctypes.byref(text)),
                "CPU_EVIDENCE_ACL_PROBE_FAILED")
        try:
            return ctypes.wstring_at(text.value)
        finally:
            self.kernel.LocalFree(text)

    def acl(self):
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        code = self.advapi.GetNamedSecurityInfoW(PRIVATE_PARENT, 1, 0x1 | 0x4,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            if descriptor:
                self.kernel.LocalFree(descriptor)
            raise InitializerError("CPU_EVIDENCE_ACL_ACCESS_DENIED" if code == 5 else "CPU_EVIDENCE_ACL_PROBE_FAILED")
        try:
            require(descriptor and owner and dacl, "CPU_EVIDENCE_ACL_PROBE_FAILED")
            control, revision = wintypes.WORD(), wintypes.DWORD()
            require(self.advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)),
                    "CPU_EVIDENCE_ACL_PROBE_FAILED")
            info = _AclSizeInformation()
            require(self.advapi.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2),
                    "CPU_EVIDENCE_ACL_PROBE_FAILED")
            # Exact policy only: avoid traversing any broad or untrusted ACL.
            require(info.AceCount == 2, "CPU_EVIDENCE_EXISTING_ACL_INVALID")
            entries = []
            for index in range(info.AceCount):
                pointer = ctypes.c_void_p()
                require(self.advapi.GetAce(dacl, index, ctypes.byref(pointer)) and pointer,
                        "CPU_EVIDENCE_ACL_PROBE_FAILED")
                header = _AceHeader.from_address(pointer.value)
                require(header.AceType == 0 and header.AceSize >= 16, "CPU_EVIDENCE_EXISTING_ACL_INVALID")
                mask = wintypes.DWORD.from_address(pointer.value + 4).value
                sid = ctypes.c_void_p(pointer.value + 8)
                require(self.advapi.IsValidSid(sid) and
                        self.advapi.GetLengthSid(sid) <= header.AceSize - 8, "CPU_EVIDENCE_ACL_PROBE_FAILED")
                entries.append({"type": int(header.AceType), "flags": int(header.AceFlags),
                                "mask": int(mask), "sid": self._sid(sid)})
            return {"owner_sid": self._sid(owner), "dacl_protected": bool(control.value & 0x1000), "aces": entries}
        finally:
            self.kernel.LocalFree(descriptor)

    def create(self):
        """Fixed target only; ACL and owner are present at creation, never patched."""
        descriptor = ctypes.c_void_p()
        require(self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            PARENT_SDDL, 1, ctypes.byref(descriptor), None), "CPU_EVIDENCE_SECURITY_DESCRIPTOR_FAILED")
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            if self.kernel.CreateDirectoryW(PRIVATE_PARENT, ctypes.byref(attributes)):
                return True
            if ctypes.get_last_error() == 183:
                return False  # Race: existing object must pass the same read-only checks.
            raise InitializerError("CPU_EVIDENCE_CREATE_FAILED")
        finally:
            self.kernel.LocalFree(descriptor)


def _plain_directory(api, path, *, missing_allowed=False):
    attributes = api.attributes(path)
    if attributes is None:
        require(missing_allowed, "CPU_EVIDENCE_PARENT_PATH_MISSING")
        return False
    require(attributes & DIRECTORY_ATTRIBUTE, "CPU_EVIDENCE_NOT_A_DIRECTORY")
    require(not attributes & REPARSE_ATTRIBUTE, "CPU_EVIDENCE_REPARSE_REJECTED")
    return True


def _verify_acl(value):
    require(isinstance(value, dict) and value.get("owner_sid") == ADMIN_SID
            and value.get("dacl_protected") is True and isinstance(value.get("aces"), list)
            and len(value["aces"]) == 2, "CPU_EVIDENCE_EXISTING_ACL_INVALID")
    identities = set()
    for ace in value["aces"]:
        require(isinstance(ace, dict) and type(ace.get("type")) is int and ace["type"] == 0
                and type(ace.get("flags")) is int and ace["flags"] == INHERIT_BOTH
                and type(ace.get("mask")) is int and ace["mask"] == FULL_CONTROL
                and ace.get("sid") in (ADMIN_SID, SYSTEM_SID) and ace["sid"] not in identities,
                "CPU_EVIDENCE_EXISTING_ACL_INVALID")
        identities.add(ace["sid"])
    require(identities == {ADMIN_SID, SYSTEM_SID}, "CPU_EVIDENCE_EXISTING_ACL_INVALID")


def perform(operation="inspect", *, _api=None):
    """Private injected API is a test seam, never a command-line path override."""
    result = {"schema_version": "1.0", "operation": operation, "status": "not_completed",
              "read_only": True, "administrator": False, "created": False,
              "creation_attempted": False, "creation_outcome_confirmed": True,
              "acl_verified": False, "parent_ready": False, "minimum_free_bytes": MINIMUM_FREE_BYTES,
              "reconstruction_started": False, "server_accessed": False, "policy_changed": False}
    try:
        require(operation in ("inspect", "initialize"), "CPU_EVIDENCE_INVALID_OPERATION")
        api = _api if _api is not None else NativeWindowsAPI()
        result["administrator"] = api.is_admin() is True
        if operation == "initialize":
            require(result["administrator"], "CPU_EVIDENCE_ADMIN_REQUIRED")
        _plain_directory(api, ROOT)
        _plain_directory(api, PROGRAM_DATA)
        available = api.disk_free()
        require(type(available) is int and available >= 0, "CPU_EVIDENCE_DISK_PROBE_FAILED")
        result["free_bytes"] = available
        require(available >= MINIMUM_FREE_BYTES, "CPU_EVIDENCE_DISK_ADMISSION_FAILED")
        exists = _plain_directory(api, PRIVATE_PARENT, missing_allowed=True)
        if not exists:
            if operation == "inspect":
                result["status"] = "initialization_required"
                return result
            # Recheck fixed ancestors immediately before the atomic creation.
            _plain_directory(api, ROOT)
            _plain_directory(api, PROGRAM_DATA)
            result.update(creation_attempted=True, creation_outcome_confirmed=False, read_only=False)
            result["created"] = api.create() is True
            result["creation_outcome_confirmed"] = True
            if not result["created"]:
                result["read_only"] = True
        _plain_directory(api, ROOT)
        _plain_directory(api, PROGRAM_DATA)
        _plain_directory(api, PRIVATE_PARENT)
        _verify_acl(api.acl())
        result.update(acl_verified=True, parent_ready=True,
                      status="initialized" if result["created"] else "already_initialized")
    except InitializerError as exc:
        result["error_code"] = exc.code
    except KeyboardInterrupt:
        result["error_code"] = "CPU_EVIDENCE_OPERATOR_INTERRUPTED"
    except Exception:
        result["error_code"] = "CPU_EVIDENCE_NATIVE_CALL_FAILED"
    return result


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        print("Inspect: initialize_cpu_evidence.py\nInitialize fixed C evidence parent: initialize_cpu_evidence.py --initialize\nNo other arguments, paths, environment overrides or reconstruction actions are accepted.")
        return 0
    if arguments not in ([], ["--initialize"]):
        result = {"status": "not_completed", "error_code": "CPU_EVIDENCE_INVALID_ARGUMENTS",
                  "read_only": True, "created": False, "acl_verified": False,
                  "reconstruction_started": False, "server_accessed": False, "policy_changed": False}
    else:
        result = perform("initialize" if arguments else "inspect")
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False), flush=True)
    return 1 if result.get("error_code") else 0


if __name__ == "__main__":
    raise SystemExit(main())
