"""Read-only SHA mapping from accepted sky labels to an existing SfM handoff.

Only original/renamed image bytes and the five text-model files are verified.
No database, binary model, computation, import, budget or old marker is touched.
Reusing the cameras never claims that their original SfM used these sky labels.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from backend.experiments.high_profile import BASE_COMMIT
from backend.experiments.sky_masks import SkyMaskError, ordinary, read_json, file_sha


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_TEXT_PREFIX = "work/shared/colmap/refined_text/"
_TEXT_NAMES = frozenset({"cameras.txt", "frames.txt", "images.txt", "points3D.txt", "rigs.txt"})
_COMPLETION = frozenset({"process_tree_stopped", "snapshot_worker_stopped",
                         "terminal_evidence_confirmed", "source_checkpoint_unchanged"})
_SUPPORTED = frozenset({".png", ".jpg", ".jpeg"})


def require(value, code):
    if not value:
        raise SkyMaskError(code)


def _pin(value):
    require(isinstance(value, str) and _HASH.fullmatch(value), "SKY_SFM_SHA_INVALID")
    return value


def _name(value):
    require(isinstance(value, str) and _NAME.fullmatch(value)
            and value not in {".", ".."} and Path(value).name == value
            and Path(value).suffix.lower() in _SUPPORTED, "SKY_SFM_IMAGE_NAME_INVALID")
    return value


def _stat(path):
    info = ordinary(path).stat()
    require(ordinary(path).is_file(), "SKY_SFM_FILE_REQUIRED")
    return {"size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def _verify_text_models(root, manifest, guard):
    entries = manifest.get("files")
    require(type(entries) is list, "SKY_SFM_FILE_RECORDS_INVALID")
    wanted, folded = {}, set()
    for entry in entries:
        require(type(entry) is dict and isinstance(entry.get("relative_path"), str),
                "SKY_SFM_FILE_RECORDS_INVALID")
        relative = entry["relative_path"]
        require(relative.casefold() not in folded, "SKY_SFM_DUPLICATE_PAYLOAD_RECORD")
        folded.add(relative.casefold())
        # Deliberately do not open or stat database, binary models or unrelated
        # historical evidence. Only this exact text-model prefix is consumable.
        if not relative.startswith(_TEXT_PREFIX):
            continue
        leaf = relative[len(_TEXT_PREFIX):]
        require(leaf in _TEXT_NAMES and relative == _TEXT_PREFIX + leaf,
                "SKY_SFM_TEXT_PAYLOAD_NOT_ALLOWED")
        require(type(entry.get("size_bytes")) is int and entry["size_bytes"] > 0,
                "SKY_SFM_TEXT_SIZE_INVALID")
        _pin(entry.get("sha256"))
        wanted[leaf] = entry
    require(set(wanted) == _TEXT_NAMES, "SKY_SFM_TEXT_MODEL_MISSING")
    folder = ordinary(root / "payload" / "work/shared/colmap/refined_text")
    require({path.name for path in folder.iterdir()} == _TEXT_NAMES, "SKY_SFM_TEXT_EXTRA_FILE")
    records = []
    for name in sorted(_TEXT_NAMES):
        guard()
        row = wanted[name]
        path = ordinary(folder / name)
        before = _stat(path)
        require(before["size_bytes"] == row["size_bytes"] and file_sha(path) == row["sha256"],
                "SKY_SFM_TEXT_HASH_CHANGED")
        require(_stat(path) == before, "SKY_SFM_TEXT_CHANGED_DURING_READ")
        records.append({"relative_path": row["relative_path"], "size_bytes": row["size_bytes"],
                        "sha256": row["sha256"]})
    return folder, records


def _bind(binding, *, handoff_root, handoff_sha256, input_manifest, guard):
    guard()
    require(type(binding) is dict and type(binding.get("images")) is list,
            "SKY_SFM_BINDING_INVALID")
    original_rows = binding["images"]
    count = len(original_rows)
    require(count >= 3, "SKY_SFM_IMAGE_COUNT_INVALID")
    root, input_path = ordinary(handoff_root), ordinary(input_manifest)
    manifest = read_json(root / "handoff-manifest.json", _pin(handoff_sha256))
    source = manifest.get("source")
    gate = manifest.get("sfm_gate")
    completion = manifest.get("completion")
    require(manifest.get("kind") == "re3d_cpu_sfm_handoff" and type(source) is dict
            and type(source.get("image_count")) is int and source["image_count"] == count
            and source.get("pipeline_commit") == BASE_COMMIT
            and type(gate) is dict and type(gate.get("registered_images")) is int
            and gate["registered_images"] == count, "SKY_SFM_HANDOFF_IDENTITY_INVALID")
    require(type(completion) is dict and set(completion) == _COMPLETION
            and all(value is True for value in completion.values()), "SKY_SFM_HANDOFF_NOT_COMPLETED")
    done = read_json(root / "DONE.json")
    require(done.get("handoff_manifest_sha256") == handoff_sha256,
            "SKY_SFM_HANDOFF_DONE_MISMATCH")
    require(not ordinary(root / "INCOMPLETE.json", missing=True).exists(),
            "SKY_SFM_HANDOFF_INCOMPLETE")
    input_sha = _pin(source.get("prepared_input_manifest_sha256"))
    prepared = read_json(input_path, input_sha)
    legacy_rows = prepared.get("images")
    require(type(legacy_rows) is list and len(legacy_rows) == count
            and ("count" not in prepared or type(prepared["count"]) is int and prepared["count"] == count),
            "SKY_SFM_INPUT_COUNT_INVALID")
    by_sha, original_names = {}, set()
    for row in original_rows:
        require(type(row) is dict, "SKY_SFM_BINDING_INVALID")
        name, sha = _name(row.get("filename")), _pin(row.get("source_sha256"))
        require(sha not in by_sha and name.casefold() not in original_names,
                "SKY_SFM_SOURCE_HASH_AMBIGUOUS")
        by_sha[sha] = row
        original_names.add(name.casefold())
    seen_names, seen_stems, seen_hashes, new_rows = set(), set(), set(), []
    image_root = ordinary(input_path.parent / "images")
    for legacy in legacy_rows:
        guard()
        require(type(legacy) is dict, "SKY_SFM_INPUT_ROW_INVALID")
        name, sha = _name(legacy.get("name")), _pin(legacy.get("sha256"))
        require(name.casefold() not in seen_names and Path(name).stem.casefold() not in seen_stems,
                "SKY_SFM_RUNTIME_NAME_AMBIGUOUS")
        require(sha not in seen_hashes and sha in by_sha, "SKY_SFM_IMAGE_SHA_NOT_BIJECTIVE")
        old = by_sha[sha]
        require(type(legacy.get("width")) is int and type(legacy.get("height")) is int
                and legacy["width"] == old["width"] and legacy["height"] == old["height"]
                and legacy["width"] > 0 and legacy["height"] > 0, "SKY_SFM_IMAGE_DIMENSIONS_CHANGED")
        path = ordinary(image_root / name)
        before = _stat(path)
        require(file_sha(path) == sha, "SKY_SFM_RUNTIME_IMAGE_HASH_CHANGED")
        require(_stat(path) == before, "SKY_SFM_RUNTIME_IMAGE_CHANGED_DURING_READ")
        row = deepcopy(old)
        row.update(filename=name, original_filename=old["filename"], mask_filename=old["filename"],
                   source_stat=before)
        new_rows.append(row)
        seen_names.add(name.casefold()); seen_stems.add(Path(name).stem.casefold()); seen_hashes.add(sha)
    require(seen_hashes == set(by_sha), "SKY_SFM_IMAGE_SHA_NOT_BIJECTIVE")
    require({path.name.casefold() for path in image_root.iterdir() if path.suffix.lower() in _SUPPORTED}
            == seen_names, "SKY_SFM_RUNTIME_IMAGE_SET_CHANGED")
    sparse, text_records = _verify_text_models(root, manifest, guard)
    guard()
    require(file_sha(root / "handoff-manifest.json") == handoff_sha256
            and file_sha(input_path) == input_sha, "SKY_SFM_SMALL_BINDING_CHANGED")
    old_whites = binding.get("all_white_exceptions", [])
    require(type(old_whites) is list and set(old_whites) <= {row["filename"] for row in original_rows},
            "SKY_SFM_WHITE_BINDING_INVALID")
    bound = deepcopy(binding)
    bound.update(original_source_root=binding["source_root"], source_root=str(image_root), images=new_rows,
                 mask_white_exceptions=old_whites,
                 all_white_exceptions=[row["filename"] for row in new_rows if row["mask_filename"] in old_whites],
                 sfm_source_sparse=str(sparse), sfm_handoff_root=str(root),
                 sfm_manifest_sha256=handoff_sha256, sfm_input_manifest_sha256=input_sha,
                 sfm_input_manifest=str(input_path), sfm_text_files=text_records,
                 sfm_masked=False, sfm_database_read=False, sfm_binary_models_read=False,
                 sfm_binding_method="exact source SHA-256 bijection, never positional filename mapping",
                 execution_authorized=False, budget_claimed=False, budget_charged=False,
                 reconstruction_started=False, server_accessed=False, uac_requested=False)
    return bound


def bind_sfm_images(binding, *, handoff_root: Path, handoff_sha256: str,
                    input_manifest: Path, guard=lambda: None) -> dict:
    """Bind renamed inputs by SHA and verify only the text handoff for preparation.

    This does not re-validate the database/model geometry or authorize execution.
    Missing/denied/malformed files fail with stable codes, never an elevation.
    """
    try:
        return _bind(binding, handoff_root=handoff_root, handoff_sha256=handoff_sha256,
                     input_manifest=input_manifest, guard=guard)
    except SkyMaskError:
        raise
    except PermissionError:
        raise SkyMaskError("SKY_SFM_ACCESS_DENIED") from None
    except (OSError, KeyError, TypeError, ValueError):
        raise SkyMaskError("SKY_SFM_BINDING_MALFORMED_OR_MISSING") from None
