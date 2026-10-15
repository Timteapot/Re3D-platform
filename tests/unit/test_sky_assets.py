"""Synthetic, read-only sky supervision gates; no private/native dependencies."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.experiments import sky_assets as assets
from backend.experiments import sky_masks, sky_sfm_binding


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return sha(path)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def case(tmp_path, monkeypatch):
    original, masks, inputs, handoff, admission = (tmp_path / n for n in
        ("original", "labels", "inputs", "handoff", "admission"))
    for root in (original, masks, inputs, handoff, admission):
        root.mkdir()
    images = inputs / "images"; images.mkdir()
    for folder in ("colmap", "openmvs"):
        (masks / "masks" / folder).mkdir(parents=True)
    target = tmp_path / "_experiments" / "test-sky"; target.parent.mkdir()
    selected, declared, legacy = [], [], []
    for index, name in enumerate(("001.jpg", "002.jpg", "00057.jpg")):
        Image.new("RGB", (8, 6), (40 + index * 50, 100, 180)).save(original / name)
        info = (original / name).stat()
        selected.append({"filename": name, "size_bytes": info.st_size, "sha256": sha(original / name)})
        mask = Image.new("L", (8, 6), 255)
        if name != "00057.jpg": mask.putpixel((0, 0), 0)
        one, two = masks / "masks/colmap" / (name + ".png"), masks / "masks/openmvs" / (Path(name).stem + ".mask.png")
        mask.save(one); two.write_bytes(one.read_bytes())
        declared.append({"filename": name, "width": 8, "height": 6,
            "source_sha256_before": sha(original / name), "source_sha256_after": sha(original / name),
            "files": {key: {"path": path.relative_to(masks).as_posix(), "size_bytes": path.stat().st_size,
                              "sha256": sha(path)} for key, path in (("colmap_mask", one), ("openmvs_mask", two))}})
        renamed = f"renamed-{index}.jpg"
        (images / renamed).write_bytes((original / name).read_bytes())
        legacy.append({"name": renamed, "sha256": sha(original / name), "width": 8, "height": 6})
    selection_sha = write_json(original / "selection-manifest.json", {"files": selected})
    mask_sha = write_json(masks / "manifest.json", {"selection_manifest_sha256": selection_sha, "files": declared})
    write_json(masks / "ACCEPTED.json", {"status": "accepted_conservative_label_assets_only",
        "manifest_sha256": mask_sha, "label_checks": {"all_white_exceptions": {"00057.jpg": "No safe sky."}}})
    input_path = inputs / "input-manifest.json"
    input_sha = write_json(input_path, {"count": 3, "images": legacy})
    sparse = handoff / "payload/work/shared/colmap/refined_text"; sparse.mkdir(parents=True)
    text = []
    for name in sorted(assets._TEXT_NAMES):
        path = sparse / name; path.write_bytes(("# tiny " + name + "\n").encode())
        text.append({"relative_path": assets._TEXT_PREFIX + name, "size_bytes": path.stat().st_size, "sha256": sha(path)})
    cpu_sha = "c" * 64
    handoff_doc = {"kind": "re3d_cpu_sfm_handoff", "source": {"image_count": 3,
        "pipeline_commit": assets.BASE_COMMIT, "prepared_input_manifest_sha256": input_sha},
        "sfm_gate": {"registered_images": 3}, "database_source": {
            "kind": "validated_final_snapshot", "cpu_result_sha256": cpu_sha},
        "files": text + [{"relative_path": "work/shared/colmap/database.db", "size_bytes": 99, "sha256": "d" * 64}],
        "completion": {key: True for key in sky_sfm_binding._COMPLETION}}
    handoff_sha = write_json(handoff / "handoff-manifest.json", handoff_doc)
    write_json(handoff / "DONE.json", {"handoff_manifest_sha256": handoff_sha})
    inspected = sky_masks.inspect_masks(original, masks, expected_manifest_sha256=mask_sha, expected_count=3)
    binding = sky_sfm_binding.bind_sfm_images(inspected, handoff_root=handoff,
        handoff_sha256=handoff_sha, input_manifest=input_path)
    binding.update(mask_scope="post_sfm_c_dense_texture_only", sfm_mask_applied=False,
                   compute_root=str(target))
    path = admission / "binding.json"
    value = SimpleNamespace(root=tmp_path, original=original, masks=masks, source=images, handoff=handoff,
        handoff_doc=handoff_doc, handoff_sha=handoff_sha, cpu_sha=cpu_sha, binding=binding,
        binding_path=path, target=target, admission=admission, sparse=sparse)
    def publish():
        value.pin = write_json(path, binding)
    value.publish = publish; publish()
    def forbidden(*args, **kwargs):
        pytest.fail("Asset gates must not start a process, UAC or reconstruction")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    return value


def verify(case, **kwargs):
    options = {"compute_root": case.target, "source_cpu_sha": case.cpu_sha,
               "handoff_sha": case.handoff_sha, "expected_count": 3, **kwargs}
    return assets.verify_sky_binding(case.binding_path, case.pin, **options)


def prepared(case, *, folder=None, dataset=False):
    folder = folder or case.admission / ("dataset" if dataset else "openmvs-input")
    (folder / "images").mkdir(parents=True)
    mask_root = folder / ("masks" if dataset else "images")
    mask_root.mkdir(exist_ok=True)
    receipt_rows, native_rows = [], []
    for row in case.binding["images"]:
        name = row["filename"]
        image = folder / "images" / name
        if dataset:
            image.write_bytes((case.source / name).read_bytes())
        else:
            Image.new("RGB", (8, 6), (100, 100, 120)).save(image)
        rel = "masks/" + name + ".png" if dataset else "images/" + Path(name).stem + ".mask.png"
        mask = folder / rel
        mask.write_bytes((case.masks / "masks/colmap" / (row["mask_filename"] + ".png")).read_bytes())
        receipt_rows.append({"filename": name, "mask": rel, "mask_sha256": sha(mask),
            "external_pixel_sha256": row["pixel_sha256"], "effective_pixel_sha256": row["pixel_sha256"]})
        native_rows.append({"name": name, "image": str(image), "mask": str(mask)})
    helper = "prepare_dataset.py" if dataset else "prepare_openmvs_texture_input.py"
    write_json(folder / "sky-mask-consumption.json", {"status": "frozen_preparation_consumed_external_masks",
        "frozen_helper_sha256": sky_masks.FROZEN_SHA["scripts/" + helper], "source_written": False,
        "reconstruction_started": False, "source_mask_manifest_sha256": case.binding["manifest_sha256"],
        "consumed_count": 3, "files": receipt_rows})
    write_json(folder / ("input-manifest.json" if dataset else "manifest.json"),
               {"count": 3, "images": native_rows, "output": str(folder), "alpha_threshold": 128})
    if not dataset:
        (folder / "sparse").mkdir()
        for name in assets._TEXT_NAMES:
            (folder / "sparse" / name).write_bytes((case.sparse / name).read_bytes())
    return folder


def test_binding_is_read_only_and_reuses_only_final_static_sfm_metadata(case):
    before = snapshot(case.root)
    checks = []
    observed = verify(case, health=lambda: checks.append(True), verify_source_images=True)
    assert observed == case.binding and len(checks) > 10
    assert before == snapshot(case.root) and not case.target.exists()
    assert not (case.handoff / "payload/work/shared/colmap/database.db").exists()


def test_binding_default_requires_all_300_and_pinned_bytes(case):
    with pytest.raises(sky_masks.SkyMaskError, match="IMAGE_COUNT"):
        assets.verify_sky_binding(case.binding_path, case.pin, compute_root=case.target,
                                 source_cpu_sha=case.cpu_sha, handoff_sha=case.handoff_sha)
    case.binding_path.write_bytes(case.binding_path.read_bytes() + b" ")
    with pytest.raises(sky_masks.SkyMaskError, match="HASH_CHANGED"):
        verify(case)


@pytest.mark.parametrize("field,value", [("mask_scope", "all-sfm"), ("sfm_mask_applied", True),
    ("sfm_masked", True), ("execution_authorized", True), ("compute_root", "different-root")])
def test_scope_and_new_target_are_strict(case, field, value):
    case.binding[field] = value; case.publish()
    with pytest.raises(sky_masks.SkyMaskError, match="SCOPE"):
        verify(case)


def test_old_target_can_only_be_inspected_when_explicitly_runtime(case):
    case.target.mkdir()
    with pytest.raises(sky_masks.SkyMaskError, match="NEW_COMPUTE"):
        verify(case)
    assert verify(case, new_target=False) == case.binding


@pytest.mark.parametrize("which", ["mask_root", "source_root", "original_source_root", "sfm_source_sparse", "binding_parent"])
def test_target_cannot_overlap_assets_in_either_direction(case, which):
    target = case.binding_path.parent / "new" if which == "binding_parent" else Path(case.binding[which]) / "new"
    case.binding["compute_root"] = str(target); case.publish()
    with pytest.raises(sky_masks.SkyMaskError, match="OVERLAP"):
        verify(case, compute_root=target)


@pytest.mark.parametrize("mutation", ["mask", "pixels", "alias", "duplicate", "missing", "extra", "white"])
def test_mask_sets_hashes_pixels_and_alias_mapping_fail_closed(case, mutation):
    row = case.binding["images"][0]
    path = case.masks / "masks/colmap" / (row["mask_filename"] + ".png")
    if mutation == "mask": path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "pixels": row["pixel_sha256"] = "f" * 64
    elif mutation == "alias": row["mask_filename"] = "../001.jpg"
    elif mutation == "duplicate": case.binding["images"][1]["filename"] = row["filename"]
    elif mutation == "missing": path.unlink()
    elif mutation == "extra": (path.parent / "foreign.png").write_bytes(b"extra")
    else: case.binding["mask_white_exceptions"] = []
    case.publish()
    with pytest.raises(sky_masks.SkyMaskError): verify(case)


def test_source_metadata_default_and_optional_full_hash_are_distinct(case):
    row = case.binding["images"][0]; path = case.source / row["filename"]
    old = path.stat(); data = bytearray(path.read_bytes()); data[-5] ^= 1; path.write_bytes(data)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert verify(case) == case.binding
    with pytest.raises(sky_masks.SkyMaskError, match="HASH_CHANGED"):
        verify(case, verify_source_images=True)


def test_source_changed_metadata_blocks_without_hashing_other_inputs(case):
    (case.source / case.binding["images"][0]["filename"]).write_bytes(b"different")
    with pytest.raises(sky_masks.SkyMaskError, match="METADATA_CHANGED"):
        verify(case)


def test_only_correct_cpu_and_final_static_handoff_can_bind(case):
    with pytest.raises(sky_masks.SkyMaskError, match="FINAL_SFM_PROVENANCE"):
        verify(case, source_cpu_sha="0" * 64)
    with pytest.raises(sky_masks.SkyMaskError, match="HANDOFF_CHANGED"):
        verify(case, handoff_sha="0" * 64)
    case.binding["sfm_text_files"][0]["sha256"] = "0" * 64; case.publish()
    with pytest.raises(sky_masks.SkyMaskError, match="SPARSE_DECLARATION"):
        verify(case)


def test_actual_native_prepared_assets_are_recorded_then_rechecked(case):
    folder = prepared(case)
    before = snapshot(case.root)
    inventory = assets.gate_sky_openmvs(case.binding, folder=folder)
    assert inventory["mask_count"] == inventory["image_count"] == 3
    assert len(inventory["files"]) == 13
    assert assets.verify_inventory(inventory)["content_hashed"] is True
    assert assets.verify_inventory(inventory, hash_content=False)["file_count"] == 13
    assert snapshot(case.root) == before


def test_both_prepared_routes_are_gated_without_repreparing(case):
    prepared(case, dataset=True); prepared(case)
    before = snapshot(case.root)
    inventory = assets.read_sky_prepared_inventory(case.binding, prepared_root=case.admission)
    assert len(inventory["files"]) == 21
    assert inventory["dataset_mask_count"] == inventory["openmvs_mask_count"] == 3
    assert assets.verify_inventory(inventory)["file_count"] == 21
    assert snapshot(case.root) == before


@pytest.mark.parametrize("mutation", ["receipt_count", "receipt_names", "external_pixels", "effective_pixels",
    "mask_content", "white_mask", "rgb_mode", "rgb_dimensions", "sparse", "manifest_path", "extra_mask"])
def test_prepared_gate_requires_real_consumption_and_data_not_just_paths(case, mutation):
    folder = prepared(case)
    receipt_path = folder / "sky-mask-consumption.json"
    receipt = json.loads(receipt_path.read_text())
    name = case.binding["images"][0]["filename"]
    mask = folder / "images" / (Path(name).stem + ".mask.png")
    if mutation == "receipt_count": receipt["consumed_count"] = 1
    elif mutation == "receipt_names": receipt["files"][1]["filename"] = name
    elif mutation == "external_pixels": receipt["files"][0]["external_pixel_sha256"] = "0" * 64
    elif mutation == "effective_pixels": receipt["files"][0]["effective_pixel_sha256"] = "0" * 64
    elif mutation == "mask_content": mask.write_bytes(b"tampered")
    elif mutation == "white_mask":
        Image.new("L", (8, 6), 255).save(mask)
        receipt["files"][0]["mask_sha256"] = sha(mask)
    elif mutation == "rgb_mode": Image.new("L", (8, 6)).save(folder / "images" / name)
    elif mutation == "rgb_dimensions": Image.new("RGB", (4, 6)).save(folder / "images" / name)
    elif mutation == "sparse": (folder / "sparse/images.txt").write_bytes(b"changed")
    elif mutation == "manifest_path":
        path = folder / "manifest.json"; value = json.loads(path.read_text())
        value["images"][0]["mask"] = str(case.original / name); write_json(path, value)
    else: (folder / "images/foreign.mask.png").write_bytes(b"extra")
    write_json(receipt_path, receipt)
    with pytest.raises(sky_masks.SkyMaskError):
        assets.gate_sky_openmvs(case.binding, folder=folder)


@pytest.mark.parametrize("which", ["mask", "derived_rgb", "sparse", "receipt", "native_manifest"])
def test_inventory_detects_changes_before_later_native_stage_even_preserved_mtime(case, which):
    folder = prepared(case); inventory = assets.gate_sky_openmvs(case.binding, folder=folder)
    name = case.binding["images"][0]["filename"]
    path = {"mask": folder / "images" / (Path(name).stem + ".mask.png"),
        "derived_rgb": folder / "images" / name, "sparse": folder / "sparse/images.txt",
        "receipt": folder / "sky-mask-consumption.json", "native_manifest": folder / "manifest.json"}[which]
    old = path.stat(); raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(sky_masks.SkyMaskError, match="HASH_CHANGED"):
        assets.verify_inventory(inventory)


def test_inventory_cannot_be_used_to_hash_database_or_escape_its_prepared_root(case):
    folder = prepared(case); inventory = assets.gate_sky_openmvs(case.binding, folder=folder)
    arbitrary = folder / "database.db"; arbitrary.write_bytes(b"must not be consumed")
    inventory["files"] = [{"path": str(arbitrary)}]
    with pytest.raises(sky_masks.SkyMaskError, match="PATH_INVALID"):
        assets.verify_inventory(inventory)


def test_permission_failure_has_stable_code_and_no_elevation(case, monkeypatch):
    real = assets.ordinary
    def denied(path, **kwargs):
        if Path(path) == case.binding_path: raise PermissionError("private path should not escape")
        return real(path, **kwargs)
    monkeypatch.setattr(assets, "ordinary", denied)
    with pytest.raises(sky_masks.SkyMaskError) as error: verify(case)
    assert error.value.code == "SKY_ASSET_ACCESS_DENIED" and "private path" not in str(error.value)


def test_caller_guard_is_never_ignored(case):
    def fail(): raise sky_masks.SkyMaskError("SKY_TEST_GUARD_FAILED")
    with pytest.raises(sky_masks.SkyMaskError, match="GUARD_FAILED"):
        verify(case, health=fail)


@pytest.mark.parametrize("where", ["binding", "mask", "source", "text"])
def test_reparse_is_rejected_without_following_links_or_elevation(case, monkeypatch, where):
    row = case.binding["images"][0]
    leaf = {"binding": case.binding_path,
        "mask": case.masks / "masks/colmap" / (row["mask_filename"] + ".png"),
        "source": case.source / row["filename"], "text": case.sparse / "images.txt"}[where]
    original = Path.lstat
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == leaf:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=1024)
        return info
    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(sky_masks.SkyMaskError, match="REPARSE_REJECTED"):
        verify(case)


def test_prepared_dataset_cannot_change_original_rgb_bytes(case):
    folder = prepared(case, dataset=True); prepared(case)
    name = case.binding["images"][0]["filename"]
    Image.new("RGB", (8, 6), (222, 111, 0)).save(folder / "images" / name)
    with pytest.raises(sky_masks.SkyMaskError, match="HASH_CHANGED"):
        assets.read_sky_prepared_inventory(case.binding, prepared_root=case.admission)


def test_inventory_rejects_new_metadata_even_when_content_hash_is_same(case):
    folder = prepared(case); inventory = assets.gate_sky_openmvs(case.binding, folder=folder)
    path = folder / "sparse/images.txt"; info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    with pytest.raises(sky_masks.SkyMaskError, match="METADATA_CHANGED"):
        assets.verify_inventory(inventory, hash_content=False)
