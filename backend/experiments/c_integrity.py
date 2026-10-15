"""Read-only, fixed-requirement presence checks before a local C-branch gate.

This is not an execution admission or complete source verification. It does not
enumerate directories, resolve links, traverse parent inventories, import a
runtime, read credentials, copy data or launch a reconstruction. Large hashes
remain the responsibility of the explicitly executing source/runtime gate.
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Iterable


DEFAULT_HASH_LIMIT = 2 * 1024**2
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = {"file", "directory", "any"}
_BLOCKING_ANCESTORS = {"denied", "reparse", "unavailable", "invalid_path", "wrong_kind"}


def _error_status(exc: OSError) -> str:
    if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM} \
            or getattr(exc, "winerror", None) == 5:
        return "denied"
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT \
            or getattr(exc, "winerror", None) in {2, 3}:
        return "missing"
    return "unavailable"


def _is_reparse(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _identity(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def inspect_path(
    path: Path | str, *, expected_kind: str = "file", expected_sha256: str | None = None,
    max_hash_bytes: int = DEFAULT_HASH_LIMIT,
) -> dict:
    """Inspect only the explicit leaf; access denial never means missing.

    A file over ``max_hash_bytes`` is observed but not opened. ``status=ok`` is
    merely the recorded leaf/type result unless ``hash_status=verified``. No
    ancestor inspection is silently performed: a full executing gate must also
    verify trusted anchors/ancestors before consuming this input.
    """
    candidate = Path(path)
    result = {"path": str(candidate), "expected_kind": expected_kind, "status": "invalid_path",
              "hash_status": "not_requested", "data_modified": False,
              "ancestor_chain_verified": False, "recursive_inventory_performed": False}
    if (not candidate.is_absolute() or ".." in candidate.parts or expected_kind not in _KINDS
            or type(max_hash_bytes) is not int or not 0 <= max_hash_bytes <= DEFAULT_HASH_LIMIT
            or expected_sha256 is not None and (not isinstance(expected_sha256, str)
                                                or not _SHA.fullmatch(expected_sha256))):
        return result
    try:
        before = candidate.lstat()
    except OSError as exc:
        result["status"] = _error_status(exc)
        return result
    if _is_reparse(before):
        result.update(status="reparse", hash_status="not_opened")
        return result
    kind = "file" if stat.S_ISREG(before.st_mode) else "directory" if stat.S_ISDIR(before.st_mode) else "special"
    result["observed_kind"] = kind
    if kind == "special" or expected_kind != "any" and kind != expected_kind:
        result["status"] = "wrong_kind"
        return result
    result["status"] = "ok"
    if kind == "file":
        result["size_bytes"] = before.st_size
    if expected_sha256 is None:
        return result
    if kind != "file":
        result.update(status="wrong_kind", hash_status="not_opened")
        return result
    if before.st_size > max_hash_bytes:
        result["hash_status"] = "deferred_large_file"
        return result
    digest = hashlib.sha256()
    try:
        # Reject a swapped leaf and avoid opening a POSIX symlink. On Windows
        # the immediate lstat/open/fstat checks fail closed if a replacement is
        # visible; ancestor authenticity still belongs to the execution gate.
        current = candidate.lstat()
        if _is_reparse(current):
            result.update(status="reparse", hash_status="not_opened")
            return result
        if _identity(current) != _identity(before):
            result.update(status="changed_during_read", hash_status="not_verified")
            return result
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                result.update(status="changed_during_read", hash_status="not_verified")
                return result
            remaining = max_hash_bytes + 1
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    break
                digest.update(block)
                remaining -= len(block)
            if remaining == 0:
                result.update(status="changed_during_read", hash_status="not_verified")
                return result
            after = os.fstat(stream.fileno())
        final = candidate.lstat()
        if _is_reparse(final) or _identity(after) != _identity(before) or _identity(final) != _identity(before):
            result.update(status="changed_during_read", hash_status="not_verified")
            return result
    except OSError as exc:
        result.update(status=_error_status(exc), hash_status="not_verified")
        return result
    if digest.hexdigest() != expected_sha256:
        result.update(status="hash_mismatch", hash_status="mismatch")
    else:
        result["hash_status"] = "verified"
    return result


def inspect_requirements(requirements: Iterable[dict]) -> dict:
    """Inspect an explicit small path list, never a recursive glob.

    Rows accept ``name``, ``path`` and the keyword arguments of inspect_path.
    Explicit directory rows should precede their children. A denied/reparse
    directory blocks descendant probes, preserving an honest unverified state.
    No source files, logs or manifests are inferred from directory contents.
    """
    observations = []
    blockers: list[tuple[Path, str]] = []
    names = set()
    for requirement in requirements:
        if (not isinstance(requirement, dict) or set(requirement) - {
                "name", "path", "expected_kind", "expected_sha256", "max_hash_bytes"}
                or not isinstance(requirement.get("name"), str) or not requirement["name"]
                or requirement["name"] in names or "path" not in requirement):
            raise ValueError("C_INTEGRITY_REQUIREMENT_INVALID")
        name, candidate = requirement["name"], Path(requirement["path"])
        names.add(name)
        ancestor = next((status for parent, status in blockers
                         if candidate != parent and candidate.is_relative_to(parent)), None)
        if ancestor is not None:
            observation = {"name": name, "path": str(candidate), "status": ancestor,
                "hash_status": "not_opened", "ancestor_probe_blocked": True, "data_modified": False,
                "ancestor_chain_verified": False, "recursive_inventory_performed": False}
        else:
            observation = inspect_path(candidate, **{key: requirement[key] for key in (
                "expected_kind", "expected_sha256", "max_hash_bytes") if key in requirement})
            observation["name"] = name
        observations.append(observation)
        if requirement.get("expected_kind") == "directory" and observation["status"] in _BLOCKING_ANCESTORS:
            blockers.append((candidate, observation["status"]))
    return {"schema_version": "1.0", "status": "observed_requirements_present" if observations
            and all(row["status"] == "ok" for row in observations) else "unverified_or_blocked",
            "observations": observations, "execution_ready": False,
            "large_hashes_deferred": sum(row["hash_status"] == "deferred_large_file" for row in observations),
            "data_modified": False, "recursive_inventory_performed": False,
            "runtime_import_performed": False}
