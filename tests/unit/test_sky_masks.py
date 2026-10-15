"""Synthetic mask integration checks; no native reconstruction or private inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops

from backend.experiments import sky_masks as sky


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_bytes((json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode())
    return digest(path.read_bytes())


def pixels(path):
    with Image.open(path) as image:
        return image.mode, image.size, image.tobytes()


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def case(tmp_path, monkeypatch):
    source, masks = tmp_path / "source", tmp_path / "labels"
    source.mkdir(); masks.mkdir()
    for folder in ("colmap", "openmvs"):
        (masks / "masks" / folder).mkdir(parents=True)
    names = ("001.jpg", "002.png", "00057.jpg")
    expected, rows, selected = {}, [], []
    for index, name in enumerate(names):
        image = Image.new("RGB", (8, 6), (30 + index * 40, 100, 180))
        if name.endswith(".png"):
            image = image.convert("RGBA")
            alpha = Image.new("L", image.size, 255)
            alpha.putpixel((6, 5), 0)
            image.putalpha(alpha)
        image.save(source / name)
        mask = Image.new("L", image.size, 255)
        if name != "00057.jpg":
            for x in range(3):
                mask.putpixel((x, 0), 0)
        colmap = masks / "masks/colmap" / (name + ".png")
        openmvs = masks / "masks/openmvs" / (Path(name).stem + ".mask.png")
        mask.save(colmap); openmvs.write_bytes(colmap.read_bytes())
        stat = (source / name).stat()
        source_sha = digest((source / name).read_bytes())
        selected.append({"filename": name, "size_bytes": stat.st_size, "sha256": source_sha})
        rows.append({"filename": name, "width": 8, "height": 6, "keyframe_unchanged": False,
                     "source_sha256_before": source_sha, "source_sha256_after": source_sha,
                     "source_stat_before": {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                     "files": {"colmap_mask": {"path": colmap.relative_to(masks).as_posix(),
                                               "sha256": digest(colmap.read_bytes()), "size_bytes": colmap.stat().st_size},
                               "openmvs_mask": {"path": openmvs.relative_to(masks).as_posix(),
                                                "sha256": digest(openmvs.read_bytes()), "size_bytes": openmvs.stat().st_size}}})
        alpha = image.getchannel("A") if "A" in image.getbands() else Image.new("L", image.size, 255)
        expected[name] = ImageChops.multiply(alpha.point(lambda v: 255 if v >= 128 else 0), mask).tobytes()
    selection = {"selected_image_count": 3, "files": selected}
    selection_sha = write_json(source / "selection-manifest.json", selection)
    manifest = {"image_count": 3, "source_directory": str(source), "output_directory": str(masks),
                "selection_manifest_sha256": selection_sha, "files": rows}
    acceptance = {"status": "accepted_conservative_label_assets_only", "manifest_sha256": None,
                  "label_checks": {"all_white_exceptions": {"00057.jpg": "Synthetic no-safe-sky exception."}}}
    value = SimpleNamespace(root=tmp_path, source=source, masks=masks, names=names, expected=expected,
                            selection=selection, manifest=manifest, acceptance=acceptance)
    def publish():
        value.manifest["selection_manifest_sha256"] = write_json(source / "selection-manifest.json", value.selection)
        value.pin = write_json(masks / "manifest.json", value.manifest)
        value.acceptance["manifest_sha256"] = value.pin
        write_json(masks / "ACCEPTED.json", value.acceptance)
        write_json(masks / "DONE.json", {"manifest_sha256": value.pin})
    value.publish = publish
    publish()
    def forbidden(*args, **kwargs):
        pytest.fail("mask preparation must not start a subprocess or request UAC")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    return value


def inspect(case, **kwargs):
    return sky.inspect_masks(case.source, case.masks, expected_manifest_sha256=case.pin,
                             expected_count=3, **kwargs)


def rewrite_mask(case, index, mask, *, both=True):
    row = case.manifest["files"][index]
    variants = ("colmap_mask", "openmvs_mask") if both else ("colmap_mask",)
    for variant in variants:
        entry = row["files"][variant]
        path = case.masks / entry["path"]
        mask.save(path)
        entry.update(sha256=digest(path.read_bytes()), size_bytes=path.stat().st_size)
    case.publish()


def error_code(call):
    with pytest.raises(sky.SkyMaskError) as failure:
        call()
    code = getattr(failure.value, "code", str(failure.value))
    assert isinstance(code, str) and code and "\n" not in code
    return code


def test_inspect_binds_each_original_and_two_mask_variants_without_writes(case):
    before = snapshot(case.root)
    binding = inspect(case)
    assert Path(binding["source_root"]) == case.source
    assert Path(binding["mask_root"]) == case.masks
    assert binding["manifest_sha256"] == case.pin
    assert binding["selection_manifest_sha256"] == digest((case.source / "selection-manifest.json").read_bytes())
    assert [row["filename"] for row in binding["images"]] == list(case.names)
    for row, original in zip(binding["images"], case.manifest["files"]):
        assert (row["width"], row["height"]) == (8, 6)
        assert row["source_sha256"] == original["source_sha256_before"]
        assert row["source_stat"] == original["source_stat_before"]
        assert row["colmap_sha256"] == original["files"]["colmap_mask"]["sha256"]
        assert row["openmvs_sha256"] == original["files"]["openmvs_mask"]["sha256"]
        assert len(row["pixel_sha256"]) == 64
    assert snapshot(case.root) == before


@pytest.mark.parametrize("mutation", ["missing", "wrong_hash", "pair_mismatch", "nonbinary", "size", "mode",
                                      "allblack", "unexpected_allwhite", "source_changed", "extra_image"])
def test_bad_assets_are_rejected_before_any_preparation(case, mutation):
    first = case.masks / case.manifest["files"][0]["files"]["colmap_mask"]["path"]
    if mutation == "missing":
        first.unlink()
    elif mutation == "wrong_hash":
        first.write_bytes(first.read_bytes() + b"tampered")
    elif mutation == "pair_mismatch":
        mask = Image.new("L", (8, 6), 255); mask.putpixel((5, 0), 0)
        rewrite_mask(case, 0, mask, both=False)
    elif mutation == "nonbinary":
        mask = Image.new("L", (8, 6), 255); mask.putpixel((0, 0), 127)
        rewrite_mask(case, 0, mask)
    elif mutation == "size":
        rewrite_mask(case, 0, Image.new("L", (7, 6), 0))
    elif mutation == "mode":
        rewrite_mask(case, 0, Image.new("RGB", (8, 6), (0, 0, 0)))
    elif mutation == "allblack":
        rewrite_mask(case, 0, Image.new("L", (8, 6), 0))
    elif mutation == "unexpected_allwhite":
        rewrite_mask(case, 0, Image.new("L", (8, 6), 255))
    elif mutation == "source_changed":
        (case.source / case.names[0]).write_bytes(b"not the accepted input")
    else:
        Image.new("RGB", (8, 6)).save(case.source / "extra.jpg")
    error_code(lambda: inspect(case))


@pytest.mark.parametrize("field,value", [("path", "../outside.png"), ("path", "/outside.png"),
                                         ("path", "masks/colmap/../001.jpg.png")])
def test_mask_manifest_cannot_escape_or_alias_its_declared_leaf(case, field, value):
    case.manifest["files"][0]["files"]["colmap_mask"][field] = value
    case.publish()
    error_code(lambda: inspect(case))


def test_duplicate_stems_cannot_ambiguously_map_to_openmvs_masks(case):
    name = "001.png"
    Image.new("RGB", (8, 6)).save(case.source / name)
    case.selection["files"][1].update(filename=name, size_bytes=(case.source / name).stat().st_size,
                                     sha256=digest((case.source / name).read_bytes()))
    case.manifest["files"][1]["filename"] = name
    case.publish()
    error_code(lambda: inspect(case))


@pytest.mark.parametrize("mutation", ["manifest_pin", "acceptance_status", "acceptance_pin", "selection_pin",
                                      "unreviewed_allwhite", "false_exception", "duplicate_rows"])
def test_binding_requires_exact_metadata_and_reviewed_allwhite_exceptions(case, mutation):
    if mutation == "manifest_pin":
        case.pin = "0" * 64
    elif mutation == "acceptance_status":
        case.acceptance["status"] = "visual_review_pending"
        case.publish()
    elif mutation == "acceptance_pin":
        case.acceptance["manifest_sha256"] = "0" * 64
        write_json(case.masks / "ACCEPTED.json", case.acceptance)
    elif mutation == "selection_pin":
        case.manifest["selection_manifest_sha256"] = "0" * 64
        case.pin = write_json(case.masks / "manifest.json", case.manifest)
        case.acceptance["manifest_sha256"] = case.pin
        write_json(case.masks / "ACCEPTED.json", case.acceptance)
    elif mutation == "unreviewed_allwhite":
        case.acceptance["label_checks"]["all_white_exceptions"] = {}
        case.publish()
    elif mutation == "false_exception":
        case.acceptance["label_checks"]["all_white_exceptions"]["001.jpg"] = "Not an approved exception."
        case.publish()
    else:
        case.manifest["files"][1] = dict(case.manifest["files"][0])
        case.publish()
    error_code(lambda: inspect(case))


def test_guard_failure_is_not_ignored(case):
    class Stop(RuntimeError):
        pass
    calls = []
    def guard():
        calls.append(True)
        raise Stop("synthetic safety stop")
    with pytest.raises((Stop, sky.SkyMaskError)):
        inspect(case, guard=guard)
    assert calls


@pytest.mark.parametrize("leaf", ["source", "mask", "manifest"])
def test_reparse_entries_are_rejected_without_symlink_privileges(case, monkeypatch, leaf):
    target = {"source": case.source / case.names[0],
              "mask": case.masks / case.manifest["files"][0]["files"]["colmap_mask"]["path"],
              "manifest": case.masks / "manifest.json"}[leaf]
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=1024) if path == target else actual
    monkeypatch.setattr(Path, "lstat", lstat)
    assert "REPARSE" in error_code(lambda: inspect(case))


def fixture_pipeline(case, monkeypatch):
    """Frozen helper stand-ins assert the actual opened pixels, not copied labels.

    Their alpha-to-mask behavior matches the pinned helper contracts. Hashes are
    patched only for these synthetic files; no sibling algorithm path is read.
    """
    root = case.root / "pipeline"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    expected = {name: digest(data) for name, data in case.expected.items()}
    shared = """
import argparse, hashlib, json, shutil
from pathlib import Path
from PIL import Image
EXPECTED = __EXPECTED__
p = argparse.ArgumentParser()
__ARGUMENTS__
a = p.parse_args()
source = a.__SOURCE__
output = a.output
(output / 'images').mkdir(parents=True)
__MASK_DIRECTORY__
rows = []
observed = {}
for path in sorted(source.iterdir()):
    if path.suffix.lower() not in {'.png', '.jpg', '.jpeg'}: continue
    with Image.open(path) as image:
        rgba = image.convert('RGBA')
        alpha = rgba.getchannel('A').point(lambda v: 255 if v >= a.alpha_threshold else 0)
        sha = hashlib.sha256(alpha.tobytes()).hexdigest()
        assert sha == EXPECTED[path.name], 'frozen helper did not read combined external mask alpha'
        observed[path.name] = sha
        __SAVE_IMAGE__
        __SAVE_MASK__
        rows.append({'name': path.name, 'width': image.width, 'height': image.height,
                     'foreground_fraction': alpha.histogram()[255] / (image.width * image.height),
                     'image': str(output / 'images' / path.name), 'mask': str(mask)})
(output / 'helper-observed.json').write_text(json.dumps(observed))
__MANIFEST__
"""
    dataset = (shared.replace("__EXPECTED__", repr(expected))
               .replace("__ARGUMENTS__", "p.add_argument('--source',type=Path); p.add_argument('--output',type=Path); p.add_argument('--alpha-threshold',type=int,default=128)")
               .replace("__SOURCE__", "source")
               .replace("__MASK_DIRECTORY__", "(output / 'masks').mkdir()")
               .replace("__SAVE_IMAGE__", "shutil.copy2(path, output / 'images' / path.name)")
               .replace("__SAVE_MASK__", "mask = output / 'masks' / (path.name + '.png'); alpha.save(mask)")
               .replace("__MANIFEST__", "(output / 'input-manifest.json').write_text(json.dumps({'count':len(rows),'uniform_size':[8,6],'images':rows}))"))
    openmvs = (shared.replace("__EXPECTED__", repr(expected))
               .replace("__ARGUMENTS__", "p.add_argument('--images',type=Path); p.add_argument('--sparse',type=Path); p.add_argument('--output',type=Path); p.add_argument('--alpha-threshold',type=int,default=128)")
               .replace("__SOURCE__", "images")
               .replace("__MASK_DIRECTORY__", "shutil.copytree(a.sparse, output / 'sparse')")
               .replace("__SAVE_IMAGE__", "rgba.convert('RGB').save(output / 'images' / path.name)")
               .replace("__SAVE_MASK__", "mask = output / 'images' / (path.stem + '.mask.png'); alpha.save(mask)")
               .replace("__MANIFEST__", "(output / 'manifest.json').write_text(json.dumps({'count':len(rows),'images':rows}))"))
    pins = {}
    for filename, text in (("prepare_dataset.py", dataset), ("prepare_openmvs_texture_input.py", openmvs)):
        # Match the frozen helper's import-safe shape. The injected Image facade
        # is assigned after import, before main opens any source image.
        prefix, body = text.split("p = argparse.ArgumentParser()", 1)
        text = prefix + "def main():\n" + "\n".join(
            "    " + line for line in ("p = argparse.ArgumentParser()" + body).splitlines()) + "\n"
        path = scripts / filename
        path.write_text(text, encoding="utf-8")
        pins["scripts/" + filename] = digest(path.read_bytes())
    monkeypatch.setattr(sky, "FROZEN_SHA", pins)
    return root


def test_frozen_dataset_helper_reads_sky_combined_with_source_alpha(case, monkeypatch):
    binding = inspect(case)
    pipeline = fixture_pipeline(case, monkeypatch)
    before_source, before_masks = snapshot(case.source), snapshot(case.masks)
    output = case.root / "prepared"
    sky.prepare_frozen_dataset(binding, pipeline_root=pipeline, output=output)
    for name in case.names:
        assert pixels(output / "masks" / (name + ".png")) == ("L", (8, 6), case.expected[name])
    assert json.loads((output / "helper-observed.json").read_text()) == {
        name: digest(data) for name, data in case.expected.items()}
    assert snapshot(case.source) == before_source and snapshot(case.masks) == before_masks


def test_frozen_openmvs_helper_reads_mask_alpha_not_allwhite_jpeg_alpha(case, monkeypatch):
    binding = inspect(case)
    pipeline = fixture_pipeline(case, monkeypatch)
    sparse = case.root / "synthetic-sparse"
    sparse.mkdir()
    (sparse / "cameras.txt").write_text("# synthetic, no model computation\n")
    output = case.root / "openmvs"
    sky.prepare_frozen_openmvs(binding, pipeline_root=pipeline, images=case.source,
                              sparse=sparse, output=output)
    for name in case.names:
        assert pixels(output / "images" / (Path(name).stem + ".mask.png")) == ("L", (8, 6), case.expected[name])
    assert json.loads((output / "helper-observed.json").read_text()) == {
        name: digest(data) for name, data in case.expected.items()}
    assert (output / "sparse/cameras.txt").read_bytes() == (sparse / "cameras.txt").read_bytes()


@pytest.mark.parametrize("phase", ["dataset", "openmvs"])
def test_changed_frozen_helper_cannot_start_or_create_prepared_outputs(case, monkeypatch, phase):
    binding = inspect(case)
    pipeline = fixture_pipeline(case, monkeypatch)
    helper = "prepare_dataset.py" if phase == "dataset" else "prepare_openmvs_texture_input.py"
    with (pipeline / "scripts" / helper).open("a") as stream:
        stream.write("\nraise AssertionError('changed helper must never execute')\n")
    output = case.root / "must-not-exist"
    if phase == "dataset":
        call = lambda: sky.prepare_frozen_dataset(binding, pipeline_root=pipeline, output=output)
    else:
        sparse = case.root / "sparse"; sparse.mkdir()
        call = lambda: sky.prepare_frozen_openmvs(binding, pipeline_root=pipeline, images=case.source,
                                                 sparse=sparse, output=output)
    error_code(call)
    assert not output.exists()


def test_existing_output_is_never_overwritten(case, monkeypatch):
    binding = inspect(case)
    pipeline = fixture_pipeline(case, monkeypatch)
    output = case.root / "existing"
    output.mkdir()
    sentinel = output / "existing-result.txt"
    sentinel.write_text("preserve existing output")
    before = snapshot(output)
    error_code(lambda: sky.prepare_frozen_dataset(binding, pipeline_root=pipeline, output=output))
    assert snapshot(output) == before


def test_changed_mask_after_binding_is_rejected(case, monkeypatch):
    binding = inspect(case)
    pipeline = fixture_pipeline(case, monkeypatch)
    path = case.masks / case.manifest["files"][0]["files"]["colmap_mask"]["path"]
    path.write_bytes(path.read_bytes() + b"changed after binding")
    error_code(lambda: sky.prepare_frozen_dataset(binding, pipeline_root=pipeline, output=case.root / "output"))


def test_changed_source_after_binding_is_rejected(case, monkeypatch):
    binding = inspect(case)
    pipeline = fixture_pipeline(case, monkeypatch)
    path = case.source / case.names[0]
    path.write_bytes(path.read_bytes() + b"changed after binding")
    error_code(lambda: sky.prepare_frozen_dataset(binding, pipeline_root=pipeline, output=case.root / "output"))
