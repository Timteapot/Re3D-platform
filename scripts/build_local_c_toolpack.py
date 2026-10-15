"""Seal clean-HEAD local C-only tools; never probe a host or start reconstruction."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import zipfile

if __package__:
    from . import build_hybrid_toolpack as common
else:
    # Isolated Python (-I) deliberately excludes scripts/ from sys.path. Load
    # this trusted sibling by absolute filename, not a mutable import path.
    _helper = Path(__file__).absolute().with_name("build_hybrid_toolpack.py")
    _spec = importlib.util.spec_from_file_location("local_c_build_common", _helper)
    if _spec is None or _spec.loader is None:
        raise RuntimeError("LOCAL_C_TOOLPACK_BUILD_HELPER_UNAVAILABLE")
    common = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(common)


CODE_FILES = tuple(f"backend/experiments/{name}.py" for name in (
    "__init__", "checkpoint", "hybrid_checkpoint", "resources", "process", "preflight",
    "runner", "high_profile", "stages", "snapshot_store", "evidence", "supervision",
    "long_runner", "cpu_runner", "hybrid_handoff", "c_profile", "c_stages", "c_integrity", "c_runner",
)) + ("backend/monitoring/resources.py", "backend/re3d_adapter/errors.py",
      "scripts/run_local_c.py", "scripts/run_hybrid_experiment.py")
PROFILE_NAME = "config/local-c-only.example.json"
DOCUMENT_FILES = (PROFILE_NAME, "docs/local-c-only.md", "docs/hybrid-cpu-sfm.md", "THIRD_PARTY_NOTICES.md")
MANIFEST_NAME = "c-only-tool-manifest.json"
BUNDLE_NAME = "bundle-manifest.json"
README_NAME = "README-toolpack.md"
PAYLOAD_NAMES = frozenset((*CODE_FILES, *DOCUMENT_FILES, MANIFEST_NAME, BUNDLE_NAME, README_NAME))
PIPELINE_COMMIT = "e517a161b4c0895f8be4258fb339272e4f99e1d6"
EXPERIMENT_BRANCH = "experiment/local-c-only"
LIMITATIONS = (
    "OFFLINE_RESTORATION_IS_NOT_REAL_C_BRANCH_ACCEPTANCE",
    "NO_ENVIRONMENT_NATIVE_BINARIES_MODELS_DATABASE_IMAGES_RESULTS_OR_SECRETS_INCLUDED",
    "PROFILE_IS_UNBOUND_AND_CANNOT_AUTHORIZE_EXECUTION",
    "PROTECTED_SFM_SOURCE_AND_RUNTIME_REQUIRE_SEPARATE_ADMISSION",
    "NO_SERVER_CONTACT_UAC_AUTORUN_OR_PLATFORM_TASK_RESUME",
)
ToolpackError = common.HybridToolpackError


def require(condition: bool, code: str) -> None:
    common._require(condition, "LOCAL_C_TOOLPACK_" + code)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: dict) -> bytes:
    return common._json_bytes(value)


def read_document(data: bytes) -> dict:
    def unique(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "DOCUMENT_INVALID")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise ToolpackError("LOCAL_C_TOOLPACK_DOCUMENT_INVALID") from None
    require(type(value) is dict, "DOCUMENT_INVALID")
    return value


def verify_template(data: bytes) -> None:
    """Never distribute a host-bound execution profile in the public toolpack."""
    value = read_document(data)
    require(set(value) == {"schema_version", "profile_kind", "autorun", "execution", "pipeline", "source",
        "target_hardware", "target_experiment", "budget", "cpu", "protection"}
        and value["schema_version"] == "1.0" and value["profile_kind"] == "local_c_only_candidate"
        and value["autorun"] is False, "UNBOUND_TEMPLATE_REQUIRED")
    require(value["execution"] == {"scope": "local-only", "branches": ["c"], "gpu_required": False,
        "platform_job_resume": False, "openmvs_mode": "cpu-only-vendor-build"}, "UNBOUND_TEMPLATE_REQUIRED")
    source = value["source"]
    require(type(source) is dict and set(source) == {
        "root", "cpu_result_sha256", "terminal_receipt_sha256", "handoff_manifest_sha256"}
        and type(source["root"]) is str and "REPLACE-WITH-VERIFIED-BUNDLE" in source["root"]
        and all(source[key] is None for key in source if key != "root"), "UNBOUND_TEMPLATE_REQUIRED")
    budget, pipeline = value["budget"], value["pipeline"]
    require(type(budget) is dict and budget.get("source_terminal_receipt_sha256") is None
        and type(pipeline) is dict and pipeline.get("commit") == PIPELINE_COMMIT,
        "UNBOUND_TEMPLATE_REQUIRED")


def committed_files(repository: Path, head: str) -> dict[str, bytes]:
    """Reuse bounded Git-blob helpers; never read payload from worktree paths."""
    requested = (*CODE_FILES, *DOCUMENT_FILES)
    records = {}
    for row in common._git(repository, ["ls-tree", "-z", head, "--", *requested]).split(b"\0"):
        if not row:
            continue
        metadata, separator, raw_name = row.partition(b"\t")
        fields, name = metadata.split(b" "), raw_name.decode("utf-8")
        common._safe_relative(name)
        require(separator == b"\t" and len(fields) == 3 and fields[0] in (b"100644", b"100755")
            and fields[1] == b"blob" and common._COMMIT.fullmatch(fields[2].decode("ascii"))
            and name in requested and name not in records, "REGULAR_GIT_BLOB_REQUIRED")
        records[name] = fields[2]
    require(set(records) == set(requested), "REGULAR_GIT_BLOB_REQUIRED")
    names = sorted(records)
    commands = b"".join(records[name] + b"\n" for name in names)
    summaries = common._git(repository, ["cat-file", "--batch-check"], input_bytes=commands).splitlines()
    require(len(summaries) == len(names), "GIT_OPERATION_FAILED")
    sizes = []
    for name, summary in zip(names, summaries):
        fields = summary.split(b" ")
        require(len(fields) == 3 and fields[:2] == [records[name], b"blob"] and fields[2].isdigit(),
                "GIT_OPERATION_FAILED")
        size = int(fields[2])
        require(0 < size <= common.MAX_FILE_BYTES, "TEXT_BLOB_INVALID")
        sizes.append(size)
    require(sum(sizes) <= common.MAX_ARCHIVE_BYTES, "ARCHIVE_SIZE_INVALID")
    output = common._git(repository, ["cat-file", "--batch"], input_bytes=commands)
    cursor, files = 0, {}
    for name, size in zip(names, sizes):
        header = records[name] + b" blob " + str(size).encode("ascii") + b"\n"
        require(output[cursor:cursor + len(header)] == header, "GIT_OPERATION_FAILED")
        cursor += len(header)
        data = output[cursor:cursor + size]
        cursor += size
        require(len(data) == size and output[cursor:cursor + 1] == b"\n", "GIT_OPERATION_FAILED")
        cursor += 1
        files[name] = common._normalize_blob(data)
    require(cursor == len(output), "GIT_OPERATION_FAILED")
    return files


def payload(repository: Path, source_commit: str | None) -> tuple[str, dict[str, bytes]]:
    head = common._head_identity(repository, source_commit)
    files = committed_files(repository, head)
    verify_template(files[PROFILE_NAME])
    files[MANIFEST_NAME] = json_bytes({"schema_version": "1.0", "kind": "local_c_only_tools",
        "autorun": False, "source_state": "git_commit", "source_commit": head,
        "files": {name: sha(files[name]) for name in CODE_FILES}})
    files[README_NAME] = (
        "# Temporary local C-only experiment tool overlay\n\n"
        f"Source: clean Git HEAD `{head}`; Git-blob UTF-8/LF bytes.\n\n"
        f"This temporary route is maintained separately on `{EXPERIMENT_BRANCH}`, not main.\n"
        "This packaged source version has not passed real C-branch reconstruction acceptance.\n"
        "Earlier candidate artifacts were reaccepted separately without rerunning reconstruction;\n"
        "that does not certify this package or rewrite the historical failed run. Source commit, not the\n"
        "moving branch name, identifies these packaged bytes.\n\n"
        "No autorun. Packaging and restoration never invoke UAC, contact a server, probe a host,\n"
        "inspect an SfM source or start reconstruction. scripts/run_local_c.py defaults to prepare.\n"
        "The 23-file code closure is fixed by c-only-tool-manifest.json. The bundle manifest\n"
        "hashes all other payload members; the external receipt hashes the bundle and archive.\n"
        "Trust the receipt separately: hashes detect drift, not independent authenticity.\n\n"
        "The profile is an unbound example, not execution permission or resource acceptance.\n"
        "Use a separate, explicitly authorized SfM handoff and private runtime manifest before\n"
        "real execution. Never overwrite the accepted SfM experiment or reuse old markers.\n"
        "This is not an environment/server image. No Python environment, native executable,\n"
        "DLL, learned model, checkpoint, database, photograph, result, .env or credential is included.\n"
        "Only C is planned; this does not resume a platform task or execute A/B.\n"
        "Read docs/local-c-only.md and THIRD_PARTY_NOTICES.md before use.\n"
    ).encode("utf-8")
    files[BUNDLE_NAME] = json_bytes({"schema_version": "1.0",
        "package_kind": "local_c_only_tool_overlay_not_environment_image", "source_commit": head,
        "release_channel": "temporary_experiment", "experiment_branch": EXPERIMENT_BRANCH,
        "real_c_branch_accepted": False,
        "pipeline_commit": PIPELINE_COMMIT, "autorun": False, "server_contacted": False,
        "reconstruction_started": False, "limitations": list(LIMITATIONS),
        "files": {name: {"sha256": sha(data), "bytes": len(data)} for name, data in sorted(files.items())}})
    require(common._head_identity(repository, head) == head, "SOURCE_CHANGED")
    return head, files


def validate_files(files: dict[str, bytes], expected_hashes: dict[str, str]) -> dict:
    require(set(files) == set(expected_hashes) == PAYLOAD_NAMES, "EXTRA_OR_MISSING_FILE")
    for name, data in files.items():
        common._safe_relative(name)
        digest = expected_hashes[name]
        require(type(digest) is str and common._SHA.fullmatch(digest), "EXPECTED_HASH_INVALID")
        require(0 < len(data) <= common.MAX_FILE_BYTES and sha(data) == digest, "RESTORE_HASH_MISMATCH")
    bundle = read_document(files[BUNDLE_NAME])
    require(set(bundle) == {"schema_version", "package_kind", "source_commit", "pipeline_commit", "autorun",
        "release_channel", "experiment_branch", "real_c_branch_accepted",
        "server_contacted", "reconstruction_started", "limitations", "files"}
        and bundle["schema_version"] == "1.0"
        and bundle["package_kind"] == "local_c_only_tool_overlay_not_environment_image"
        and type(bundle["source_commit"]) is str and common._COMMIT.fullmatch(bundle["source_commit"])
        and bundle["release_channel"] == "temporary_experiment"
        and bundle["experiment_branch"] == EXPERIMENT_BRANCH and bundle["real_c_branch_accepted"] is False
        and bundle["pipeline_commit"] == PIPELINE_COMMIT and bundle["autorun"] is False
        and bundle["server_contacted"] is False and bundle["reconstruction_started"] is False
        and bundle["limitations"] == list(LIMITATIONS)
        and bundle["files"] == {name: {"sha256": expected_hashes[name], "bytes": len(files[name])}
                                for name in files if name != BUNDLE_NAME}, "BUNDLE_MANIFEST_INVALID")
    tool = read_document(files[MANIFEST_NAME])
    require(tool == {"schema_version": "1.0", "kind": "local_c_only_tools", "autorun": False,
        "source_state": "git_commit", "source_commit": bundle["source_commit"],
        "files": {name: expected_hashes[name] for name in CODE_FILES}}, "TOOL_MANIFEST_INVALID")
    verify_template(files[PROFILE_NAME])
    return {"status": "all_restored_hashes_verified", "file_count": len(files),
        "code_file_count": len(CODE_FILES), "source_commit": bundle["source_commit"],
        "bundle_manifest_sha256": expected_hashes[BUNDLE_NAME],
        "tool_manifest_sha256": expected_hashes[MANIFEST_NAME]}


def verify_payload(root: Path | str, expected_hashes: dict[str, str]) -> dict:
    root = common._checked_path(root)
    require(root.is_dir() and set(expected_hashes) == PAYLOAD_NAMES, "PAYLOAD_DIRECTORY_OR_ALLOWLIST_INVALID")
    files, directories = {}, set()
    for current, children, names in os.walk(root, followlinks=False):
        for name in children:
            path = common._checked_path(Path(current) / name)
            directories.add(common._safe_relative(path.relative_to(root).as_posix()))
        for name in names:
            path = common._checked_path(Path(current) / name)
            relative = common._safe_relative(path.relative_to(root).as_posix())
            require(path.is_file() and relative in expected_hashes, "EXTRA_OR_MISSING_FILE")
            require(path.stat().st_size <= common.MAX_FILE_BYTES, "TEXT_BLOB_INVALID")
            files[relative] = path.read_bytes()
    allowed_dirs = {parent.as_posix() for name in expected_hashes for parent in Path(name).parents
                    if parent.as_posix() != "."}
    require(directories <= allowed_dirs, "EXTRA_OR_MISSING_FILE")
    return validate_files(files, expected_hashes)


def restore_archive(archive: Path | str, target: Path | str, *, expected_hashes: dict[str, str]) -> dict:
    archive = common._checked_path(archive)
    target = common._checked_path(target, missing_leaf=True)
    require(not target.exists(), "TARGET_EXISTS")
    require(set(expected_hashes) == PAYLOAD_NAMES, "PAYLOAD_ALLOWLIST_INVALID")
    require(archive.is_file() and archive.stat().st_size <= common.MAX_ARCHIVE_BYTES, "ARCHIVE_SIZE_INVALID")
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [common._safe_relative(info.filename) for info in entries]
        require(len(names) == len(set(names)) == len({name.casefold() for name in names})
            and set(names) == PAYLOAD_NAMES, "ARCHIVE_MEMBERS_INVALID")
        require(all(not info.is_dir() and not info.flag_bits & 1
            and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
            and 0 < info.file_size <= common.MAX_FILE_BYTES for info in entries)
            and sum(info.file_size for info in entries) <= common.MAX_ARCHIVE_BYTES,
            "ARCHIVE_MEMBER_TYPE_INVALID")
        files = {info.filename: package.read(info) for info in entries}
    validate_files(files, expected_hashes)
    common._new_payload(target, files)
    return verify_payload(target, expected_hashes)


def prepare_toolpack(repository_root: Path | str, source_commit: str | None = None) -> dict:
    head, files = payload(Path(repository_root), source_commit)
    return {"status": "prepared_no_execution", "source_commit": head, "code_file_count": len(CODE_FILES),
        "release_channel": "temporary_experiment", "experiment_branch": EXPERIMENT_BRANCH,
        "real_c_branch_accepted": False,
        "payload_file_count": len(files), "tool_manifest_sha256": sha(files[MANIFEST_NAME]),
        "bundle_manifest_sha256": sha(files[BUNDLE_NAME]), "autorun": False,
        "server_contacted": False, "reconstruction_started": False}


def build_toolpack(repository_root: Path | str, source_commit: str | None, output_dir: Path | str) -> dict:
    repository = common._checked_path(repository_root)
    output = common._checked_path(output_dir, missing_leaf=True)
    common._safe_relative(output.name)
    require(not output.exists(), "TARGET_EXISTS")
    require(not output.is_relative_to(repository) and not repository.is_relative_to(output), "SOURCE_OUTPUT_OVERLAP")
    head, files = payload(repository, source_commit)
    hashes = {name: sha(data) for name, data in files.items()}
    validate_files(files, hashes)
    # All validation precedes exclusive creation. Existing releases are never overwritten.
    output.mkdir()
    common._new_payload(output / "payload", files)
    archive = output / ("re3d-local-c-toolpack-" + head[:12] + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, data)
    restored = restore_archive(archive, output / "restore-verification", expected_hashes=hashes)
    receipt = {"schema_version": "1.0", "status": "packaged_and_restored", "source_commit": head,
        "release_channel": "temporary_experiment", "experiment_branch": EXPERIMENT_BRANCH,
        "real_c_branch_accepted": False,
        "archive": archive.name, "archive_bytes": archive.stat().st_size, "archive_sha256": sha(archive.read_bytes()),
        "tool_manifest_sha256": hashes[MANIFEST_NAME], "bundle_manifest_sha256": hashes[BUNDLE_NAME],
        "profile_sha256": hashes[PROFILE_NAME], "payload_hashes": hashes, "restore": restored,
        "autorun": False, "server_contacted": False, "reconstruction_started": False,
        "server_deployed": False, "contains_environment_or_data": False, "line_endings": "git_blob_utf8_lf",
        "limitations": list(LIMITATIONS)}
    common._write_new(output / "package-receipt.json", json_bytes(receipt))
    return receipt


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolpackError("LOCAL_C_TOOLPACK_ARGUMENTS_INVALID")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = Parser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--repository-root", type=Path, required=True)
        parser.add_argument("--source-commit")
        parser.add_argument("--output-dir", type=Path)
        parser.add_argument("--build-toolpack", action="store_true")
        args = parser.parse_args(argv)
        if args.build_toolpack:
            require(args.output_dir is not None, "OUTPUT_REQUIRED")
            value = build_toolpack(args.repository_root, args.source_commit, args.output_dir)
            value = {key: value[key] for key in ("status", "source_commit", "archive", "archive_sha256",
                "tool_manifest_sha256", "bundle_manifest_sha256", "autorun", "server_contacted", "reconstruction_started")}
        else:
            value = prepare_toolpack(args.repository_root, args.source_commit)
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ToolpackError) else "LOCAL_C_TOOLPACK_BUILD_FAILED"
        print(json.dumps({"status": "not_completed", "error_code": code, "autorun": False,
            "server_contacted": False, "reconstruction_started": False}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
