"""Sealed independent post-SfM C sky-mask experiment.

Default preparation is a no-op. Candidate sealing copies only the fixed Python
closure into a new directory. Preflight never claims compute budget. Execution
is separate and requires the exact --execute-sky-c-only switch; no mode elevates,
connects to a server, starts a queue, or implicitly retries an old experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from types import ModuleType

SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
TOOL_MANIFEST = "sky-c-tool-manifest.json"
# Do not enlarge the historical C-only closure or change its existing manifest.
LEGACY_CODE_FILES = frozenset({
    *(f"backend/experiments/{name}.py" for name in (
        "__init__", "checkpoint", "hybrid_checkpoint", "resources", "process", "preflight",
        "runner", "high_profile", "stages", "snapshot_store", "evidence", "supervision",
        "long_runner", "cpu_runner", "hybrid_handoff", "c_profile", "c_stages", "c_integrity", "c_runner")),
    "backend/monitoring/resources.py", "backend/re3d_adapter/errors.py",
    "scripts/run_local_c.py", "scripts/run_hybrid_experiment.py",
})
CODE_FILES = LEGACY_CODE_FILES | frozenset({
    *(f"backend/experiments/{name}.py" for name in (
        "sky_masks", "sky_sfm_binding", "sky_mask_plan", "sky_assets", "sky_supervision",
        "sky_executor", "c_budget_chain")),
    "scripts/run_sky_c_supervised.py",
})


class CLIError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise CLIError(code)


def public_error_code(error):
    """Preserve bounded structured codes; never exception messages or argv."""
    try:
        code = error if type(error) is str else getattr(error, "code", None)
    except BaseException:
        return "SKY_C_ENTRY_FAILED"
    prefixes = r"(?:SKY|LOCAL_C|C_ONLY|C_OUTPUT|C_STAGE|C_WRAPPER|HYBRID_CLI|HANDOFF|CHECKPOINT|CPU|SFM|LOW|RESOURCE|EVIDENCE|LONG|EXPERIMENT|HOST_RESOURCE|DIRECTORY_RESOURCE|GPU_RESOURCE)"
    return code if type(code) is str and len(code) <= 96 and re.fullmatch(
        prefixes + r"_[A-Z][A-Z0-9_]*", code) else "SKY_C_ENTRY_FAILED"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError("SKY_C_ARGUMENTS_INVALID")


def plain(path, *, missing=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts
            and not str(path).startswith(("\\\\", "//"))
            and not any(":" in part for part in path.parts[1:]), "SKY_C_PATH_INVALID")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            require(missing and cursor == path, "SKY_C_PATH_MISSING")
            return path
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 1024,
                "SKY_C_REPARSE_REJECTED")
    return path


def signature(path):
    info = plain(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def sha(path):
    with plain(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path, *, expected_sha256=None, limit=128 * 1024):
    path = plain(path)
    before = signature(path)
    require(path.is_file() and before[2] <= limit, "SKY_C_DOCUMENT_INVALID")
    raw = path.read_bytes()
    require(len(raw) <= limit and signature(path) == before, "SKY_C_DOCUMENT_CHANGED")
    if expected_sha256 is not None:
        require(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256)
                and hashlib.sha256(raw).hexdigest() == expected_sha256, "SKY_C_DOCUMENT_HASH_CHANGED")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "SKY_C_DOCUMENT_INVALID")
            value[key] = item
        return value
    try:
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise CLIError("SKY_C_DOCUMENT_INVALID") from None
    require(type(result) is dict, "SKY_C_DOCUMENT_INVALID")
    return result


def _write_bytes_new(path, raw):
    plain(path, missing=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def seal_tools(root, output, *, source_base_commit):
    """New allowlist-only candidate; no Git changes, secrets, models or data."""
    root, output = plain(root), plain(output, missing=True)
    require(isinstance(source_base_commit, str) and COMMIT.fullmatch(source_base_commit),
            "SKY_C_SOURCE_BASE_COMMIT_REQUIRED")
    require(not output.exists() and not output.is_relative_to(root) and not root.is_relative_to(output),
            "SKY_C_NEW_CANDIDATE_ROOT_REQUIRED")
    sources = {}
    for name in sorted(CODE_FILES):
        path = plain(root / name)
        before = signature(path)
        require(path.is_file() and before[2] <= 1024 * 1024, "SKY_C_TOOL_SOURCE_INVALID")
        raw = path.read_bytes()
        require(signature(path) == before and len(raw) == before[2], "SKY_C_TOOL_SOURCE_CHANGED")
        sources[name] = (raw, hashlib.sha256(raw).hexdigest(), before)
    output.mkdir()
    for name, (raw, _, _) in sources.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_new(path, raw)
    require(all(signature(root / name) == previous for name, (_, _, previous) in sources.items()),
            "SKY_C_TOOL_SOURCE_CHANGED")
    value = {"schema_version": "1.0", "kind": "local_sky_c_supervised_tools", "autorun": False,
        "source_state": "uncommitted_candidate", "source_base_commit": source_base_commit,
        "files": {name: digest for name, (_, digest, _) in sources.items()}}
    raw = (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    _write_bytes_new(output / TOOL_MANIFEST, raw)
    # Verify the copied bytes once; no original inputs/runtime or native program.
    require(all(sha(output / name) == digest for name, digest in value["files"].items()),
            "SKY_C_CANDIDATE_COPY_CHANGED")
    return hashlib.sha256(raw).hexdigest()


def verify_tools(root, pin):
    root = plain(root)
    require(isinstance(pin, str) and SHA.fullmatch(pin), "SKY_C_TOOL_MANIFEST_CHANGED")
    manifest = root / TOOL_MANIFEST
    value = read(manifest, expected_sha256=pin)
    common_fields = {"schema_version", "kind", "autorun", "source_state", "files"}
    candidate = (set(value) == common_fields | {"source_base_commit"}
                 and value.get("source_state") == "uncommitted_candidate"
                 and isinstance(value.get("source_base_commit"), str)
                 and COMMIT.fullmatch(value["source_base_commit"]) is not None)
    # A committed package is bound by the external manifest pin and all source
    # byte pins, not by a local Git checkout or a claim that a candidate is clean.
    # Packaging must obtain these exact bytes from this commit's Git objects.
    published = (set(value) == common_fields | {"source_commit"}
                 and value.get("source_state") == "git_commit"
                 and isinstance(value.get("source_commit"), str)
                 and COMMIT.fullmatch(value["source_commit"]) is not None)
    require((candidate or published) and value.get("schema_version") == "1.0"
            and value.get("kind") == "local_sky_c_supervised_tools"
            and value.get("autorun") is False
            and type(value.get("files")) is dict and set(value["files"]) == CODE_FILES,
            "SKY_C_TOOL_MANIFEST_INVALID")
    signatures = {}
    for name, expected in value["files"].items():
        signatures[name] = signature(root / name)
        require(isinstance(expected, str) and SHA.fullmatch(expected) and sha(root / name) == expected,
                "SKY_C_TOOL_HASH_CHANGED")
    require(all(signature(root / name) == saved for name, saved in signatures.items())
            and sha(manifest) == pin, "SKY_C_TOOL_CHANGED_DURING_IMPORT")
    return value


# Runtime/preflight revalidation intentionally does not reinstall namespaces or
# start another bootstrap. This remains a standard-library-only byte verifier.
verify_tool_files = verify_tools


def preflight_receipt_target(args):
    """Reject overwrites/protected destinations before expensive read-only gates."""
    output = Path(args.output)
    private = Path(r"C:\ProgramData\Re3DRestricted")
    require(not output.is_relative_to(private) and not private.is_relative_to(output),
            "SKY_C_RECEIPT_PROTECTED_OVERLAP")
    output = plain(output, missing=True)
    require(output.suffix.casefold() == ".json" and not output.exists(), "SKY_C_NEW_RECEIPT_REQUIRED")
    profile = read(args.profile, expected_sha256=args.profile_sha256)
    source_cpu = read(args.source_cpu_profile, expected_sha256=args.source_cpu_profile_sha256)
    base = profile.get("c_profile", {})
    require(type(base) is dict, "SKY_C_PROFILE_INVALID")
    protected = [Path(r"C:\ProgramData\Re3DRestricted"), Path(args.pipeline_root), Path(args.tool_root),
                 args.profile, args.source_cpu_profile, args.terminal_receipt, args.runtime_manifest]
    for value in (base, source_cpu):
        for key in ("source", "target_experiment"):
            row = value.get(key, {})
            if isinstance(row, dict) and isinstance(row.get("root"), str):
                protected.append(Path(row["root"]))
    checkpoint = source_cpu.get("source_checkpoint", {})
    if isinstance(checkpoint, dict):
        protected.extend(Path(checkpoint[key]) for key in ("original_root", "snapshot_root")
                         if isinstance(checkpoint.get(key), str))
    binding_pin = profile.get("mask_binding", {})
    if isinstance(binding_pin, dict) and binding_pin.get("path") and binding_pin.get("sha256"):
        binding = read(binding_pin["path"], expected_sha256=binding_pin["sha256"], limit=4 * 1024**2)
        protected.append(Path(binding_pin["path"]))
        for key in ("source_root", "original_source_root", "mask_root", "compute_root",
                    "sfm_handoff_root", "sfm_source_sparse"):
            if key not in binding:
                continue
            value = binding[key]
            require(isinstance(value, str) and bool(value), "SKY_C_BINDING_PATH_INVALID")
            path = Path(value)
            require(path.is_absolute() and ".." not in path.parts
                    and not value.startswith(("\\\\", "//"))
                    and not any(":" in part for part in path.parts[1:]), "SKY_C_BINDING_PATH_INVALID")
            protected.append(path)
    # Check lexically; never lstat/list a privileged parent merely to reject it.
    require(all(path.is_absolute() and ".." not in path.parts
                and not output.is_relative_to(path) and not path.is_relative_to(output)
                for path in protected), "SKY_C_RECEIPT_PROTECTED_OVERLAP")
    return output


def write_preflight_receipt(path, result):
    raw = (json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    require(len(raw) <= 1024**2, "SKY_C_RECEIPT_TOO_LARGE")
    _write_bytes_new(path, raw)
    return hashlib.sha256(raw).hexdigest()


def load_backend(root, pin):
    """Verify every source byte with stdlib *before* any backend/scripts import."""
    root = plain(root)
    value = verify_tools(root, pin)
    path = root / "scripts/run_hybrid_experiment.py"
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == value["files"]["scripts/run_hybrid_experiment.py"],
            "SKY_C_TOOL_HASH_CHANGED")
    bootstrap = ModuleType("sky_c_pinned_bootstrap")
    bootstrap.__file__ = str(path)
    exec(compile(raw, str(path), "exec", dont_inherit=True), bootstrap.__dict__)
    bootstrap.CODE_FILES = CODE_FILES
    bootstrap._VERIFIED_TOOLS[str(root)] = (pin, dict(value["files"]))
    original_sha = bootstrap.sha_file
    bootstrap.sha_file = lambda target: original_sha(
        root / TOOL_MANIFEST if Path(target) == root / "hybrid-tool-manifest.json" else target)
    require(sha(root / TOOL_MANIFEST) == pin, "SKY_C_TOOL_MANIFEST_CHANGED")
    bootstrap.bootstrap_namespace(root)
    return importlib.import_module("backend.experiments.sky_supervision")


class PreparationGuard:
    """Small between-image child checks; parent supplies continuous supervision."""
    def __init__(self, disk):
        self.disk, self.started = disk, time.monotonic()

    def __call__(self):
        from backend.monitoring.resources import _system_memory_bytes
        used, total = _system_memory_bytes()
        require(total - used >= 8 * 1024**3, "SKY_LOW_AVAILABLE_RAM_8GIB")
        require(shutil.disk_usage(self.disk).free >= 20 * 1024**3, "SKY_LOW_DISK_20GIB")
        require(time.monotonic() - self.started < 600, "SKY_PREPARATION_DEADLINE")


def _parser():
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mode", choices=("prepare", "seal-tools", "preflight", "prepare-openmvs", "execute"),
                        default="prepare")
    for flag in ("profile", "source-cpu-profile", "terminal-receipt", "runtime-manifest", "pipeline-root",
                 "output", "binding", "images", "sparse"):
        parser.add_argument("--" + flag, type=Path)
    for flag in ("profile-sha256", "source-cpu-profile-sha256", "runtime-manifest-sha256",
                 "tool-manifest-sha256", "binding-sha256", "source-base-commit"):
        parser.add_argument("--" + flag)
    parser.add_argument("--execute-sky-c-only", action="store_true")
    return parser


def main(argv=None, *, tool_root=None):
    try:
        args = _parser().parse_args(argv)
        root = Path(tool_root) if tool_root is not None else Path(__file__).absolute().parents[1]
        controls = {"profile", "profile_sha256", "source_cpu_profile", "source_cpu_profile_sha256",
                    "terminal_receipt", "runtime_manifest", "runtime_manifest_sha256"}
        allowed = {"prepare": set(), "seal-tools": {"output", "source_base_commit"},
            "preflight": controls | {"output", "pipeline_root", "tool_manifest_sha256"},
            "prepare-openmvs": {"binding", "binding_sha256", "images", "sparse", "output",
                                "pipeline_root", "tool_manifest_sha256"},
            "execute": controls | {"output", "pipeline_root", "tool_manifest_sha256"}}
        require(args.execute_sky_c_only == (args.mode == "execute"), "SKY_C_EXECUTION_MODE_MISMATCH")
        require(all(getattr(args, key) is None or key in allowed[args.mode]
                    for key in vars(args) if key not in {"mode", "execute_sky_c_only"}),
                "SKY_C_ARGUMENT_MODE_MISMATCH")
        if args.mode == "prepare":
            result = {"status": "prepared_not_executed", "reconstruction_started": False,
                      "execution_authorized": False, "budget_claimed": False, "budget_charged": False}
        elif args.mode == "seal-tools":
            require(args.output is not None and args.source_base_commit is not None,
                    "SKY_C_SEAL_ARGUMENTS_REQUIRED")
            pin = seal_tools(root, args.output, source_base_commit=args.source_base_commit)
            result = {"status": "sealed_not_executed", "tool_manifest_sha256": pin,
                      "source_state": "uncommitted_candidate", "source_base_commit": args.source_base_commit,
                      "code_file_count": len(CODE_FILES), "reconstruction_started": False}
        else:
            required = allowed[args.mode] - ({"output"} if args.mode == "execute" else set())
            require(all(getattr(args, key) is not None for key in required),
                    "SKY_C_PREFLIGHT_ARGUMENTS_REQUIRED" if args.mode != "prepare-openmvs"
                    else "SKY_C_ADAPTER_ARGUMENTS_REQUIRED")
            runner = load_backend(root, args.tool_manifest_sha256)
            args.tool_root = root
            if args.mode == "preflight":
                receipt = preflight_receipt_target(args)
                result = runner.preflight_sky_c(args, tool_identity=args.tool_manifest_sha256)
                if result.get("status") == "read_only_sky_c_preflight_passed":
                    receipt_sha = write_preflight_receipt(receipt, result)
                    result = {**result, "preflight_receipt_sha256": receipt_sha}
            elif args.mode == "execute":
                from backend.experiments.sky_executor import execute_sky_c
                result = execute_sky_c(args, read(args.profile, expected_sha256=args.profile_sha256),
                                       tool_identity=args.tool_manifest_sha256)
            else:
                from backend.experiments.sky_masks import prepare_frozen_openmvs, read_json
                binding = read_json(args.binding, args.binding_sha256)
                require(binding.get("mask_scope") == "post_sfm_c_dense_texture_only",
                        "SKY_BINDING_SCOPE_INVALID")
                guard = PreparationGuard(args.output.parent)
                guard()
                prepared = prepare_frozen_openmvs(binding, pipeline_root=args.pipeline_root,
                    images=args.images, sparse=args.sparse, output=args.output, guard=guard)
                result = {"status": prepared["status"], "consumed_count": prepared["consumed_count"],
                          "reconstruction_started": False}
        require(type(result) is dict, "SKY_C_ENTRY_RESULT_INVALID")
        failed = result.get("status") in {"blocked", "failed", "incomplete", "timed_out", "cancelled"}
        if failed or result.get("error_code") is not None:
            # Diagnostic details remain in the private/minimal on-disk receipt;
            # do not trust a returned exception detail as public JSON content.
            result = {"status": result.get("status", "blocked"),
                "error_code": public_error_code(result.get("error_code")),
                "reconstruction_started": result.get("reconstruction_started") is True,
                "execution_authorized": False}
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 1 if failed or result.get("error_code") is not None else 0
    except BaseException as error:
        print(json.dumps({"status": "blocked", "error_code": public_error_code(error),
                          "reconstruction_started": False, "execution_authorized": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
