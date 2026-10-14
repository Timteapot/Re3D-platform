"""Prepare or seal a clean-HEAD CPU SfM/handoff tool overlay without executing it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid
import zipfile


EXPERIMENT_MODULES = (
    "__init__", "checkpoint", "hybrid_checkpoint", "resources", "process", "preflight",
    "runner", "high_profile", "stages", "snapshot_store", "evidence", "supervision",
    "long_runner", "cpu_runner", "hybrid_handoff",
)
CODE_FILES = tuple(f"backend/experiments/{name}.py" for name in EXPERIMENT_MODULES) + (
    "scripts/run_hybrid_experiment.py", "scripts/run_cpu_sfm_stage.py",
    "scripts/run_profiled_colmap.py", "backend/monitoring/resources.py",
    "backend/re3d_adapter/errors.py",
)
OPTIONAL_FILES = ("docs/hybrid-cpu-sfm.md", "config/hybrid-cpu.example.json")
MANIFEST_NAME = "hybrid-tool-manifest.json"
README_NAME = "README-toolpack.md"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,180}\Z")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
MAX_FILE_BYTES = 2 * 1024**2
MAX_ARCHIVE_BYTES = 32 * 1024**2


class HybridToolpackError(ValueError):
    """Stable error code; never include subprocess or configuration contents."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise HybridToolpackError(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _checked_path(value: Path | str, *, missing_leaf: bool = False) -> Path:
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "HYBRID_TOOLPACK_ABSOLUTE_PATH_REQUIRED")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            _require(missing_leaf and cursor == path, "HYBRID_TOOLPACK_PATH_MISSING")
            continue
        _require(not stat.S_ISLNK(info.st_mode)
                 and not (getattr(info, "st_file_attributes", 0) & 1024),
                 "HYBRID_TOOLPACK_LINK_OR_REPARSE_REJECTED")
    return path


def _safe_relative(name: str) -> str:
    _require(isinstance(name, str) and bool(name) and ":" not in name and "\\" not in name,
             "HYBRID_TOOLPACK_RELATIVE_PATH_INVALID")
    _require(all(_COMPONENT.fullmatch(part) and part not in {".", ".."} and not part.endswith(".")
                 and part.split(".", 1)[0].casefold() not in _RESERVED for part in name.split("/")),
             "HYBRID_TOOLPACK_RELATIVE_PATH_INVALID")
    return name


def _git(repository: Path, arguments: list[str], *, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(["git", "-c", "safe.directory=" + repository.as_posix(),
                                 "-C", str(repository), *arguments], capture_output=True, input=input_bytes,
                                timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise HybridToolpackError("HYBRID_TOOLPACK_GIT_OPERATION_FAILED") from None
    _require(result.returncode == 0, "HYBRID_TOOLPACK_GIT_OPERATION_FAILED")
    return result.stdout


def _head_identity(repository: Path, source_commit: str | None) -> str:
    _checked_path(repository)
    _require(repository.is_dir(), "HYBRID_TOOLPACK_REPOSITORY_REQUIRED")
    top = _git(repository, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()
    _require(Path(top).resolve() == repository.resolve(), "HYBRID_TOOLPACK_REPOSITORY_ROOT_REQUIRED")
    head = _git(repository, ["rev-parse", "--verify", "HEAD^{commit}"]).decode("ascii").strip()
    _require(bool(_COMMIT.fullmatch(head)), "HYBRID_TOOLPACK_FULL_SOURCE_COMMIT_REQUIRED")
    if source_commit is not None:
        _require(isinstance(source_commit, str) and bool(_COMMIT.fullmatch(source_commit)),
                 "HYBRID_TOOLPACK_FULL_SOURCE_COMMIT_REQUIRED")
        _require(source_commit == head, "HYBRID_TOOLPACK_SOURCE_MUST_BE_HEAD")
    # Untracked code is not part of HEAD. Reject rather than assigning it HEAD's
    # identity or accidentally including credentials/data from the filesystem.
    _require(not _git(repository, ["status", "--porcelain=v1", "--untracked-files=all"]),
             "HYBRID_TOOLPACK_REPOSITORY_DIRTY")
    return head


def _normalize_blob(data: bytes) -> bytes:
    _require(0 < len(data) <= MAX_FILE_BYTES and b"\0" not in data,
             "HYBRID_TOOLPACK_TEXT_BLOB_INVALID")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise HybridToolpackError("HYBRID_TOOLPACK_TEXT_BLOB_INVALID") from None
    # Worktree CRLF conversion is irrelevant: hashes and unpacked bytes share
    # the Git-blob, UTF-8/LF representation, not platform checkout bytes.
    text = text.replace("\r\n", "\n")
    _require("\r" not in text, "HYBRID_TOOLPACK_TEXT_BLOB_LINE_ENDINGS_INVALID")
    return text.encode("utf-8")


def _committed_files(repository: Path, head: str) -> dict[str, bytes]:
    """Read the fixed closure with bounded Git batch I/O, not 40 subprocesses."""
    requested = (*CODE_FILES, *OPTIONAL_FILES)
    records: dict[str, bytes] = {}
    for row in _git(repository, ["ls-tree", "-z", head, "--", *requested]).split(b"\0"):
        if not row:
            continue
        metadata, separator, raw_name = row.partition(b"\t")
        fields = metadata.split(b" ")
        name = raw_name.decode("utf-8")
        _safe_relative(name)
        _require(separator == b"\t" and len(fields) == 3 and fields[0] in (b"100644", b"100755")
                 and fields[1] == b"blob" and bool(_COMMIT.fullmatch(fields[2].decode("ascii")))
                 and name in requested and name not in records,
                 "HYBRID_TOOLPACK_REGULAR_GIT_BLOB_REQUIRED")
        records[name] = fields[2]
    _require(set(CODE_FILES) <= set(records), "HYBRID_TOOLPACK_REGULAR_GIT_BLOB_REQUIRED")
    names = sorted(records)
    commands = b"".join(records[name] + b"\n" for name in names)
    summaries = _git(repository, ["cat-file", "--batch-check"], input_bytes=commands).splitlines()
    _require(len(summaries) == len(names), "HYBRID_TOOLPACK_GIT_OPERATION_FAILED")
    sizes: list[int] = []
    for name, summary in zip(names, summaries):
        fields = summary.split(b" ")
        _require(len(fields) == 3 and fields[:2] == [records[name], b"blob"]
                 and fields[2].isdigit(), "HYBRID_TOOLPACK_GIT_OPERATION_FAILED")
        size = int(fields[2])
        _require(0 < size <= MAX_FILE_BYTES, "HYBRID_TOOLPACK_TEXT_BLOB_INVALID")
        sizes.append(size)
    _require(sum(sizes) <= MAX_ARCHIVE_BYTES, "HYBRID_TOOLPACK_ARCHIVE_SIZE_INVALID")
    output = _git(repository, ["cat-file", "--batch"], input_bytes=commands)
    cursor = 0
    payload: dict[str, bytes] = {}
    for name, size in zip(names, sizes):
        header = records[name] + b" blob " + str(size).encode("ascii") + b"\n"
        _require(output[cursor:cursor + len(header)] == header, "HYBRID_TOOLPACK_GIT_OPERATION_FAILED")
        cursor += len(header)
        data = output[cursor:cursor + size]
        cursor += size
        _require(len(data) == size and output[cursor:cursor + 1] == b"\n",
                 "HYBRID_TOOLPACK_GIT_OPERATION_FAILED")
        cursor += 1
        payload[name] = _normalize_blob(data)
    _require(cursor == len(output), "HYBRID_TOOLPACK_GIT_OPERATION_FAILED")
    return payload


def _payload(repository: Path, source_commit: str | None) -> tuple[str, dict[str, bytes]]:
    head = _head_identity(repository, source_commit)
    files = _committed_files(repository, head)
    manifest = {"schema_version": "1.0", "toolpack_kind": "re3d_cpu_sfm_handoff",
                "source_commit": head, "files": {name: _sha(files[name]) for name in CODE_FILES},
                "autorun": False}
    files[MANIFEST_NAME] = _json_bytes(manifest)
    files[README_NAME] = (
        "# Independent CPU SfM / GPU handoff tool overlay\n\n"
        f"Tool source: clean Git HEAD `{head}`. All code is immutable Git-blob UTF-8/LF.\n\n"
        "No autorun: extraction, preparation and verification never execute reconstruction.\n"
        "The exact 20-file code closure is fixed by hybrid-tool-manifest.json; the external\n"
        "package receipt hashes all archive members and the ZIP. Trust the receipt separately.\n"
        "Use scripts/run_hybrid_experiment.py for explicit preparation, CPU execution and\n"
        "SfM export/import. Any example profile is a non-executing template, not host acceptance.\n"
        "This overlay is not a full server image or dependency environment. It excludes .env,\n"
        "credentials, models, native executables, databases, checkpoints, images and result data.\n"
        "Install separately from the frozen pipeline and platform repositories. Old task and\n"
        "lease records are not resumed or rewritten. CPU/handoff acceptance is not complete\n"
        "300-image GPU reconstruction or geometric-quality acceptance.\n"
    ).encode("utf-8")
    _require(_head_identity(repository, head) == head, "HYBRID_TOOLPACK_SOURCE_CHANGED")
    return head, files


def prepare_toolpack(repository_root: Path | str, source_commit: str | None = None) -> dict:
    """Read fixed committed blobs and report readiness; create no output files."""
    head, files = _payload(Path(repository_root), source_commit)
    return {"schema_version": "1.0", "status": "prepared_no_execution", "source_commit": head,
            "code_file_count": len(CODE_FILES), "payload_file_count": len(files),
            "tool_manifest_sha256": _sha(files[MANIFEST_NAME]), "autorun": False,
            "server_contacted": False, "reconstruction_started": False}


def _write_new(path: Path, data: bytes) -> None:
    _checked_path(path.parent)
    _checked_path(path, missing_leaf=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _new_payload(root: Path, files: dict[str, bytes]) -> None:
    _checked_path(root, missing_leaf=True)
    root.mkdir()
    for name, data in sorted(files.items()):
        path = root.joinpath(*_safe_relative(name).split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_new(path, data)


def verify_payload(root: Path | str, expected_hashes: dict[str, str]) -> dict:
    """Verify exact restored membership and hashes without importing payload code."""
    root = _checked_path(root)
    _require(root.is_dir(), "HYBRID_TOOLPACK_PAYLOAD_DIRECTORY_REQUIRED")
    expected = set(expected_hashes)
    _require(set(CODE_FILES) | {MANIFEST_NAME, README_NAME} <= expected
             and expected <= set(CODE_FILES) | {MANIFEST_NAME, README_NAME, *OPTIONAL_FILES},
             "HYBRID_TOOLPACK_PAYLOAD_ALLOWLIST_INVALID")
    actual: set[str] = set()
    directories: set[str] = set()
    for directory, children, names in os.walk(root, followlinks=False):
        for name in children:
            path = _checked_path(Path(directory) / name)
            directories.add(_safe_relative(path.relative_to(root).as_posix()))
        for name in names:
            path = _checked_path(Path(directory) / name)
            _require(path.is_file(), "HYBRID_TOOLPACK_REGULAR_FILE_REQUIRED")
            actual.add(_safe_relative(path.relative_to(root).as_posix()))
    allowed_dirs = {ancestor.as_posix() for name in expected for ancestor in Path(name).parents
                    if ancestor.as_posix() != "."}
    _require(actual == expected and directories <= allowed_dirs,
             "HYBRID_TOOLPACK_EXTRA_OR_MISSING_FILE")
    _require(len(actual) == len({name.casefold() for name in actual}),
             "HYBRID_TOOLPACK_CASEFOLD_COLLISION")
    for name, digest in expected_hashes.items():
        _safe_relative(name)
        _require(isinstance(digest, str) and bool(_SHA.fullmatch(digest)),
                 "HYBRID_TOOLPACK_EXPECTED_HASH_INVALID")
        path = root.joinpath(*name.split("/"))
        _require(path.stat().st_size <= MAX_FILE_BYTES and _sha(path.read_bytes()) == digest,
                 "HYBRID_TOOLPACK_RESTORE_HASH_MISMATCH")
    manifest = json.loads((root / MANIFEST_NAME).read_bytes())
    _require(isinstance(manifest, dict) and set(manifest) == {
        "schema_version", "toolpack_kind", "source_commit", "files", "autorun"}
        and manifest["schema_version"] == "1.0" and manifest["toolpack_kind"] == "re3d_cpu_sfm_handoff"
        and manifest["autorun"] is False and isinstance(manifest["source_commit"], str)
        and bool(_COMMIT.fullmatch(manifest["source_commit"]))
        and manifest["files"] == {name: expected_hashes[name] for name in CODE_FILES},
        "HYBRID_TOOLPACK_MANIFEST_INVALID")
    return {"status": "all_restored_hashes_verified", "file_count": len(actual),
            "code_file_count": len(CODE_FILES), "tool_manifest_sha256": expected_hashes[MANIFEST_NAME]}


def restore_archive(archive: Path | str, target: Path | str,
                    *, expected_hashes: dict[str, str]) -> dict:
    """Validate the entire ZIP before exclusive writes; never use extractall."""
    archive, target = _checked_path(archive), _checked_path(target, missing_leaf=True)
    _require(not target.exists(), "HYBRID_TOOLPACK_TARGET_EXISTS")
    _require(archive.is_file() and archive.stat().st_size <= MAX_ARCHIVE_BYTES,
             "HYBRID_TOOLPACK_ARCHIVE_SIZE_INVALID")
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [_safe_relative(info.filename) for info in entries]
        _require(len(names) == len(set(names)) == len({name.casefold() for name in names})
                 and set(names) == set(expected_hashes), "HYBRID_TOOLPACK_ARCHIVE_MEMBERS_INVALID")
        _require(all(not info.is_dir() and not info.flag_bits & 1
                     and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
                     and info.file_size <= MAX_FILE_BYTES for info in entries)
                 and sum(info.file_size for info in entries) <= MAX_ARCHIVE_BYTES,
                 "HYBRID_TOOLPACK_ARCHIVE_MEMBER_TYPE_INVALID")
        files = {info.filename: package.read(info) for info in entries}
    _require(all(_sha(data) == expected_hashes[name] for name, data in files.items()),
             "HYBRID_TOOLPACK_ARCHIVE_HASH_MISMATCH")
    _new_payload(target, files)
    return verify_payload(target, expected_hashes)


def build_toolpack(repository_root: Path | str, source_commit: str | None,
                   output_dir: Path | str) -> dict:
    """Seal only clean HEAD into a new directory, then verify a fresh extraction."""
    repository, output = _checked_path(repository_root), _checked_path(output_dir, missing_leaf=True)
    _safe_relative(output.name)
    _require(not output.exists(), "HYBRID_TOOLPACK_TARGET_EXISTS")
    _require(not output.is_relative_to(repository) and not repository.is_relative_to(output),
             "HYBRID_TOOLPACK_SOURCE_OUTPUT_OVERLAP")
    head, files = _payload(repository, source_commit)
    # A unique staging directory is exclusively owned by this build. No earlier
    # payload, failed build, archive or verification output is ever overwritten.
    staging = output.parent / ("hybrid-staging-" + uuid.uuid4().hex)
    _new_payload(staging, files)
    expected_hashes = {name: _sha(data) for name, data in files.items()}
    verify_payload(staging, expected_hashes)
    _require(_head_identity(repository, head) == head, "HYBRID_TOOLPACK_SOURCE_CHANGED")
    _checked_path(output, missing_leaf=True)
    output.mkdir()
    archive = output / ("re3d-hybrid-toolpack-" + head[:12] + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, data)
    restored = output / "restore-verification"
    restoration = restore_archive(archive, restored, expected_hashes=expected_hashes)
    receipt = {"schema_version": "1.0", "status": "packaged_and_restored",
        "toolpack_kind": "re3d_cpu_sfm_handoff", "source_commit": head,
        "archive": archive.name, "archive_bytes": archive.stat().st_size,
        "archive_sha256": _sha(archive.read_bytes()),
        "tool_manifest_sha256": expected_hashes[MANIFEST_NAME],
        "payload_hashes": expected_hashes, "restore": restoration,
        "staging_directory": staging.name, "autorun": False, "server_contacted": False,
        "reconstruction_started": False, "server_deployed": False,
        "line_endings": "git_blob_utf8_lf", "contains_environment_or_data": False}
    _write_new(output / "package-receipt.json", _json_bytes(receipt))
    # Staging stays as an auditable payload. No recursive cleanup or deletion is
    # performed; its unique name is recorded in the external receipt.
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--source-commit", help="Optional full SHA; if supplied it must be clean HEAD.")
    parser.add_argument("--output-dir", type=Path, help="New absolute directory outside the repository.")
    parser.add_argument("--build-toolpack", action="store_true", help="Explicitly create a ZIP; otherwise prepare only.")
    args = parser.parse_args(argv)
    try:
        if args.build_toolpack:
            _require(args.output_dir is not None, "HYBRID_TOOLPACK_OUTPUT_REQUIRED")
            receipt = build_toolpack(args.repository_root, args.source_commit, args.output_dir)
            print(json.dumps({key: receipt[key] for key in ("status", "source_commit", "archive",
                "archive_sha256", "tool_manifest_sha256", "autorun", "server_contacted",
                "reconstruction_started")}, sort_keys=True))
        else:
            print(json.dumps(prepare_toolpack(args.repository_root, args.source_commit), sort_keys=True))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, HybridToolpackError) else "HYBRID_TOOLPACK_LOCAL_BUILD_FAILED"
        print(json.dumps({"status": "not_completed", "error_code": code, "autorun": False,
                          "server_contacted": False, "reconstruction_started": False}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
