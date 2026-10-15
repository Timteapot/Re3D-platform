"""Read-only asset gates for the opt-in post-SfM sky-mask C experiment.

These gates do not import a handoff, read its database/binary models, create a
target, claim a budget, start a process, or request elevation. Native execution
and continuous resource protection remain the caller's responsibility.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re

from backend.experiments.high_profile import BASE_COMMIT
from backend.experiments.sky_masks import FROZEN_SHA, SkyMaskError, ordinary, read_json, require

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
_TEXT_PREFIX = "work/shared/colmap/refined_text/"
_TEXT_NAMES = frozenset({"cameras.txt", "frames.txt", "images.txt", "points3D.txt", "rigs.txt"})


def _pin(value):
    require(isinstance(value, str) and _SHA.fullmatch(value), "SKY_ASSET_SHA_INVALID")
    return value


def _name(value):
    require(isinstance(value, str) and _NAME.fullmatch(value) and value not in {".", ".."}
            and Path(value).name == value and Path(value).suffix.lower() in _EXTENSIONS,
            "SKY_ASSET_NAME_INVALID")
    return value


def _signature(path):
    path = ordinary(path)
    info = path.stat()
    require(path.is_file(), "SKY_ASSET_FILE_REQUIRED")
    return {"device": info.st_dev, "inode": info.st_ino, "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns}


def _file(path, health, *, expected_sha=None, expected_size=None):
    path = ordinary(path)
    before = _signature(path)
    if expected_size is not None:
        require(type(expected_size) is int and expected_size >= 0
                and before["size_bytes"] == expected_size, "SKY_ASSET_SIZE_CHANGED")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            health(); digest.update(block)
    health()
    value = digest.hexdigest()
    require(expected_sha is None or value == _pin(expected_sha), "SKY_ASSET_HASH_CHANGED")
    require(_signature(path) == before, "SKY_ASSET_CHANGED_DURING_READ")
    return {"path": str(path), "size_bytes": before["size_bytes"], "sha256": value, "stat": before}


def _mask(path, sha, size, health):
    from PIL import Image
    path = ordinary(path)
    require(_signature(path)["size_bytes"] <= 8 * 1024**2, "SKY_MASK_TOO_LARGE")
    record = _file(path, health, expected_sha=sha)
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == record["sha256"], "SKY_ASSET_CHANGED_DURING_READ")
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        require(image.mode == "L" and image.size == size, "SKY_MASK_MODE_OR_SIZE")
        histogram = image.histogram()
        require({i for i, count in enumerate(histogram) if count} <= {0, 255}
                and histogram[255] > 0, "SKY_MASK_NOT_BINARY_OR_EMPTY")
        pixel_sha = hashlib.sha256(image.tobytes()).hexdigest()
        white = not histogram[0]
    require(_signature(path) == record["stat"], "SKY_ASSET_CHANGED_DURING_READ")
    return record, pixel_sha, white


def _images(folder):
    return {p.name for p in ordinary(folder).iterdir() if p.suffix.lower() in _EXTENSIONS}


def _rows(binding, expected_count=None):
    rows = binding.get("images")
    require(type(rows) is list and len(rows) >= 3
            and (expected_count is None or type(expected_count) is int and len(rows) == expected_count),
            "SKY_ASSET_IMAGE_COUNT_INVALID")
    names, stems, masks, hashes = set(), set(), set(), set()
    for row in rows:
        require(type(row) is dict, "SKY_ASSET_BINDING_INVALID")
        name, mask = _name(row.get("filename")), _name(row.get("mask_filename"))
        require(row.get("original_filename") == mask, "SKY_ASSET_MASK_MAPPING_INVALID")
        sha = _pin(row.get("source_sha256"))
        require(name.casefold() not in names and Path(name).stem.casefold() not in stems
                and mask.casefold() not in masks and sha not in hashes, "SKY_ASSET_MAPPING_AMBIGUOUS")
        require(all(type(row.get(key)) is int and row[key] > 0 for key in ("width", "height")),
                "SKY_ASSET_DIMENSIONS_INVALID")
        for key in ("pixel_sha256", "colmap_sha256", "openmvs_sha256"):
            _pin(row.get(key))
        names.add(name.casefold()); stems.add(Path(name).stem.casefold())
        masks.add(mask.casefold()); hashes.add(sha)
    return rows


def _guarded(call):
    try:
        return call()
    except SkyMaskError:
        raise
    except PermissionError:
        raise SkyMaskError("SKY_ASSET_ACCESS_DENIED") from None
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        raise SkyMaskError("SKY_ASSET_INVALID_OR_MISSING") from None


def verify_sky_binding(binding_path, pin, *, compute_root: Path, source_cpu_sha: str,
                       handoff_sha: str, health=lambda: None, new_target=True,
                       expected_count=300, verify_source_images=False) -> dict:
    """Verify accepted labels and identities, optionally hashing source RGB once.

    Default source checks are the pinned filenames and size/mtime signatures;
    stage 03 independently verifies actual source bytes as it consumes them.
    The bound handoff's small final-static provenance is checked, not its DB.
    """
    def verify():
        health()
        path = ordinary(binding_path)
        binding = read_json(path, _pin(pin))
        target = ordinary(compute_root, missing=new_target)
        require(type(new_target) is bool and (not new_target or not target.exists()),
                "SKY_NEW_COMPUTE_SCENE_REQUIRED")
        require(binding.get("mask_scope") == "post_sfm_c_dense_texture_only"
                and binding.get("sfm_mask_applied") is False and binding.get("sfm_masked") is False
                and binding.get("execution_authorized") is False
                and Path(binding.get("compute_root", "")) == target, "SKY_ASSET_BINDING_SCOPE_INVALID")
        require(binding.get("sfm_manifest_sha256") == _pin(handoff_sha), "SKY_ASSET_HANDOFF_CHANGED")
        rows = _rows(binding, expected_count)
        roots = [ordinary(Path(binding[key])) for key in
                 ("mask_root", "source_root", "original_source_root", "sfm_source_sparse", "sfm_handoff_root")]
        require(all(not target.is_relative_to(root) and not root.is_relative_to(target)
                    for root in (*roots, path.parent)), "SKY_ASSET_TARGET_OVERLAP")
        mask_root, source, original, sparse, handoff_root = roots
        manifest = read_json(mask_root / "manifest.json", _pin(binding.get("manifest_sha256")))
        accepted = read_json(mask_root / "ACCEPTED.json")
        require(accepted.get("status") == "accepted_conservative_label_assets_only"
                and accepted.get("manifest_sha256") == binding["manifest_sha256"], "SKY_ASSETS_NOT_ACCEPTED")
        originals = {row["mask_filename"]: row for row in rows}
        manifest_rows = manifest.get("files")
        require(type(manifest_rows) is list and len(manifest_rows) == len(rows)
                and all(type(row) is dict for row in manifest_rows)
                and len({row.get("filename") for row in manifest_rows}) == len(rows)
                and {row.get("filename") for row in manifest_rows} == set(originals),
                "SKY_ASSET_MANIFEST_MAPPING_INVALID")
        require(manifest.get("selection_manifest_sha256") == binding.get("selection_manifest_sha256"),
                "SKY_ASSET_SELECTION_CHANGED")
        selection = read_json(original / "selection-manifest.json", _pin(binding["selection_manifest_sha256"]))
        selected = selection.get("files")
        require(type(selected) is list and len(selected) == len(rows)
                and all(type(row) is dict for row in selected)
                and len({row.get("filename") for row in selected}) == len(rows)
                and {row.get("filename") for row in selected} == set(originals),
                "SKY_ASSET_SELECTION_CHANGED")
        selected = {row["filename"]: row for row in selected}
        whites = accepted.get("label_checks", {}).get("all_white_exceptions", {})
        require(type(whites) is dict and set(whites) <= {"00057.jpg", "00302.jpg"}
                and type(binding.get("mask_white_exceptions")) is list
                and set(binding["mask_white_exceptions"]) == set(whites),
                "SKY_ASSET_WHITE_EXCEPTION_INVALID")
        require(set(binding.get("all_white_exceptions", [])) ==
                {row["filename"] for row in rows if row["mask_filename"] in whites},
                "SKY_ASSET_WHITE_EXCEPTION_INVALID")
        require(_images(source) == {row["filename"] for row in rows}
                and _images(original) == set(originals), "SKY_ASSET_SOURCE_SET_CHANGED")
        for folder, wanted in (("colmap", {name + ".png" for name in originals}),
                               ("openmvs", {Path(name).stem + ".mask.png" for name in originals})):
            require({p.name for p in ordinary(mask_root / "masks" / folder).iterdir()} == wanted,
                    "SKY_MASK_SET_CHANGED")
        assets, actual_whites = [], set()
        for declared in manifest_rows:
            health()
            name = declared["filename"]; row = originals[name]
            size = (row["width"], row["height"])
            require(size == (declared.get("width"), declared.get("height"))
                    and declared.get("source_sha256_before") == declared.get("source_sha256_after")
                    == selected[name].get("sha256") == row["source_sha256"], "SKY_ASSET_SOURCE_BINDING_CHANGED")
            observed_pixels = []
            for kind, relative, bound_key in (
                ("colmap_mask", "masks/colmap/" + name + ".png", "colmap_sha256"),
                ("openmvs_mask", "masks/openmvs/" + Path(name).stem + ".mask.png", "openmvs_sha256")):
                item = declared["files"][kind]
                require(item.get("path") == relative and item.get("sha256") == row[bound_key],
                        "SKY_ASSET_MASK_DECLARATION_CHANGED")
                record, pixel_sha, white = _mask(mask_root / relative, row[bound_key], size, health)
                require(record["size_bytes"] == item.get("size_bytes")
                        and pixel_sha == row["pixel_sha256"], "SKY_ASSET_MASK_PIXELS_CHANGED")
                observed_pixels.append(pixel_sha); assets.append(record)
                if white: actual_whites.add(name)
            require(len(set(observed_pixels)) == 1, "SKY_MASK_PAIR_CHANGED")
            current = _signature(source / row["filename"])
            stat = row.get("source_stat", {})
            require(type(stat) is dict and current["size_bytes"] == stat.get("size_bytes")
                    == selected[name].get("size_bytes") and current["mtime_ns"] == stat.get("mtime_ns"),
                    "SKY_ASSET_SOURCE_METADATA_CHANGED")
            if verify_source_images:
                _file(source / row["filename"], health, expected_sha=row["source_sha256"],
                      expected_size=current["size_bytes"])
        require(actual_whites == set(whites), "SKY_ASSET_WHITE_EXCEPTION_INVALID")
        handoff = read_json(handoff_root / "handoff-manifest.json", handoff_sha)
        provenance = handoff.get("database_source", {})
        require(handoff.get("kind") == "re3d_cpu_sfm_handoff"
                and handoff.get("source", {}).get("pipeline_commit") == BASE_COMMIT
                and handoff.get("source", {}).get("image_count") == len(rows)
                and handoff.get("sfm_gate", {}).get("registered_images") == len(rows)
                and provenance.get("kind") == "validated_final_snapshot"
                and provenance.get("cpu_result_sha256") == _pin(source_cpu_sha),
                "SKY_ASSET_FINAL_SFM_PROVENANCE_CHANGED")
        done = read_json(handoff_root / "DONE.json")
        require(done.get("handoff_manifest_sha256") == handoff_sha
                and not ordinary(handoff_root / "INCOMPLETE.json", missing=True).exists(),
                "SKY_ASSET_HANDOFF_INCOMPLETE")
        require(sparse == handoff_root / "payload/work/shared/colmap/refined_text",
                "SKY_ASSET_SPARSE_ROOT_CHANGED")
        declared_text = [row for row in handoff.get("files", []) if type(row) is dict
                         and isinstance(row.get("relative_path"), str)
                         and row["relative_path"].startswith(_TEXT_PREFIX)]
        require(len(declared_text) == 5 and
                {row["relative_path"]: row for row in declared_text} ==
                {row["relative_path"]: row for row in binding.get("sfm_text_files", [])},
                "SKY_ASSET_SPARSE_DECLARATION_INVALID")
        _text_inventory(binding, sparse, health)
        require(_file(path, health, expected_sha=pin)["sha256"] == pin, "SKY_ASSET_BINDING_CHANGED")
        return binding
    return _guarded(verify)


def _text_inventory(binding, folder, health):
    rows = binding.get("sfm_text_files")
    require(type(rows) is list and len(rows) == 5 and all(type(row) is dict for row in rows),
            "SKY_ASSET_SPARSE_DECLARATION_INVALID")
    require({row.get("relative_path") for row in rows} == {_TEXT_PREFIX + n for n in _TEXT_NAMES}
            and {p.name for p in ordinary(folder).iterdir()} == _TEXT_NAMES,
            "SKY_ASSET_SPARSE_SET_CHANGED")
    return [_file(folder / row["relative_path"][len(_TEXT_PREFIX):], health,
                  expected_sha=_pin(row.get("sha256")), expected_size=row.get("size_bytes")) for row in rows]


def _prepared(binding, folder, health, *, dataset=False):
    from PIL import Image
    folder = ordinary(folder)
    rows = _rows(binding)
    expected = {row["filename"]: row for row in rows}
    receipt = read_json(folder / "sky-mask-consumption.json")
    records = receipt.get("files")
    helper = "prepare_dataset.py" if dataset else "prepare_openmvs_texture_input.py"
    require(receipt.get("status") == "frozen_preparation_consumed_external_masks"
            and receipt.get("frozen_helper_sha256") == FROZEN_SHA["scripts/" + helper]
            and receipt.get("source_written") is False and receipt.get("reconstruction_started") is False
            and receipt.get("source_mask_manifest_sha256") == binding.get("manifest_sha256")
            and type(receipt.get("consumed_count")) is int and receipt["consumed_count"] == len(rows)
            and type(records) is list and len(records) == len(rows)
            and all(type(row) is dict for row in records)
            and len({row.get("filename") for row in records}) == len(rows)
            and {row.get("filename") for row in records} == set(expected), "SKY_ASSET_CONSUMPTION_INVALID")
    image_folder, mask_folder = folder / "images", folder / ("masks" if dataset else "images")
    expected_masks = {(name + ".png") if dataset else (Path(name).stem + ".mask.png") for name in expected}
    require({p.name for p in ordinary(image_folder).iterdir() if p.suffix.lower() in _EXTENSIONS
             and not p.name.endswith(".mask.png")} == set(expected), "SKY_ASSET_PREPARED_IMAGE_SET_CHANGED")
    require({p.name for p in ordinary(mask_folder).iterdir() if dataset or p.name.endswith(".mask.png")}
            == expected_masks, "SKY_ASSET_PREPARED_MASK_SET_CHANGED")
    native_name = "input-manifest.json" if dataset else "manifest.json"
    native = read_json(folder / native_name)
    native_rows = native.get("images")
    require(type(native.get("count")) is int and native["count"] == len(rows)
            and type(native_rows) is list and len(native_rows) == len(rows)
            and all(type(row) is dict for row in native_rows)
            and len({row.get("name") for row in native_rows}) == len(rows)
            and {row.get("name") for row in native_rows} == set(expected),
            "SKY_ASSET_PREPARED_MANIFEST_CHANGED")
    if not dataset:
        require(Path(native.get("output", "")) == folder and native.get("alpha_threshold") == 128
                and all(Path(row.get("image", "")) == image_folder / row["name"]
                        and Path(row.get("mask", "")) == mask_folder / (Path(row["name"]).stem + ".mask.png")
                        for row in native_rows), "SKY_ASSET_PREPARED_REFERENCE_CHANGED")
    inventory = [_file(folder / "sky-mask-consumption.json", health),
                 _file(folder / native_name, health)]
    for record in records:
        health(); name = record["filename"]; row = expected[name]
        relative = ("masks/" + name + ".png") if dataset else ("images/" + Path(name).stem + ".mask.png")
        require(record.get("mask") == relative and record.get("external_pixel_sha256")
                == record.get("effective_pixel_sha256") == row["pixel_sha256"],
                "SKY_ASSET_EFFECTIVE_MASK_CHANGED")
        mask, pixel_sha, _ = _mask(folder / relative, _pin(record.get("mask_sha256")),
                                   (row["width"], row["height"]), health)
        require(pixel_sha == row["pixel_sha256"], "SKY_ASSET_EFFECTIVE_MASK_CHANGED")
        inventory.append(mask)
        image_path = image_folder / name
        image = _file(image_path, health, expected_sha=row["source_sha256"] if dataset else None)
        with Image.open(image_path) as rgb:
            require(rgb.mode == "RGB" and rgb.size == (row["width"], row["height"]),
                    "SKY_ASSET_PREPARED_RGB_INVALID")
        require(_signature(image_path) == image["stat"], "SKY_ASSET_CHANGED_DURING_READ")
        inventory.append(image)
    if not dataset:
        inventory.extend(_text_inventory(binding, folder / "sparse", health))
    return {"schema_version": "1.0", "kind": "sky_prepared_asset_inventory", "folder": str(folder),
            "image_count": len(rows), "mask_count": len(rows), "files": inventory,
            "source_mask_manifest_sha256": binding["manifest_sha256"],
            "pixel_equality_to_bound_sky_masks": True, "source_written": False,
            "reconstruction_started": False, "budget_claimed": False, "budget_charged": False}


def gate_sky_openmvs(binding, *, folder: Path, health=lambda: None) -> dict:
    """Gate the actual stage-03 output, retaining prepared RGB and mask hashes."""
    return _guarded(lambda: _prepared(binding, folder, health))


def read_sky_prepared_inventory(binding, *, prepared_root: Path, health=lambda: None) -> dict:
    """Read the prior dataset and OpenMVS preparation without creating a target."""
    def verify():
        root = ordinary(prepared_root)
        dataset = _prepared(binding, root / "dataset", health, dataset=True)
        openmvs = _prepared(binding, root / "openmvs-input", health)
        return {**openmvs, "kind": "sky_preparation_asset_inventory", "folder": str(root),
                "files": dataset["files"] + openmvs["files"], "dataset_image_count": dataset["image_count"],
                "dataset_mask_count": dataset["mask_count"], "openmvs_mask_count": openmvs["mask_count"]}
    return _guarded(verify)


def verify_inventory(inventory, *, health=lambda: None, hash_content=True) -> dict:
    """Recheck only the already-bound prepared assets, never database or models."""
    def verify():
        require(type(inventory) is dict and inventory.get("kind") in
                {"sky_prepared_asset_inventory", "sky_preparation_asset_inventory"}
                and type(inventory.get("files")) is list and bool(inventory["files"])
                and type(hash_content) is bool,
                "SKY_ASSET_INVENTORY_INVALID")
        root = ordinary(Path(inventory["folder"]))
        seen = set()
        for row in inventory["files"]:
            health()
            require(type(row) is dict and type(row.get("path")) is str, "SKY_ASSET_INVENTORY_INVALID")
            path = ordinary(Path(row["path"]))
            require(path.is_relative_to(root) and path not in seen, "SKY_ASSET_INVENTORY_PATH_INVALID")
            allowed = (path.parent == root and path.name in {"sky-mask-consumption.json", "manifest.json"} or
                path.name in {"sky-mask-consumption.json", "manifest.json", "input-manifest.json"}
                and path.parent.name in {"dataset", "openmvs-input"} or
                path.parent.name in {"images", "masks"} and path.suffix.lower() in _EXTENSIONS or
                path.parent.name == "sparse" and path.name in _TEXT_NAMES)
            require(allowed, "SKY_ASSET_INVENTORY_PATH_INVALID")
            require(_signature(path) == row.get("stat") and row.get("size_bytes") == row["stat"]["size_bytes"],
                    "SKY_ASSET_INVENTORY_METADATA_CHANGED")
            _pin(row.get("sha256")); seen.add(path)
            if hash_content:
                _file(path, health, expected_sha=row["sha256"], expected_size=row["size_bytes"])
        return {"status": "sky_prepared_assets_unchanged", "file_count": len(seen),
                "content_hashed": hash_content, "reconstruction_started": False,
                "budget_claimed": False, "budget_charged": False}
    return _guarded(verify)
