"""Renamed SfM inputs are mapped by bytes, using only tiny synthetic fixtures."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.experiments import sky_sfm_binding as sfm
from backend.experiments.sky_masks import SkyMaskError


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_bytes((json.dumps(value, sort_keys=True) + "\n").encode())
    return sha(path.read_bytes())


def inventory(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def case(tmp_path, monkeypatch):
    original = tmp_path / "original"; original.mkdir()
    runtime = tmp_path / "runtime"; (runtime / "images").mkdir(parents=True)
    handoff = tmp_path / "handoff"; handoff.mkdir()
    sparse = handoff / "payload/work/shared/colmap/refined_text"; sparse.mkdir(parents=True)
    rows, legacy = [], []
    for number, filename in enumerate(("001.jpg", "002.jpg", "00057.jpg")):
        raw = ("synthetic-jpeg-" + str(number)).encode()
        (original / filename).write_bytes(raw)
        runtime_name = f"00000{number}-renamed.jpg"
        path = runtime / "images" / runtime_name
        path.write_bytes(raw)
        rows.append({"filename": filename, "source_sha256": sha(raw), "width": 8, "height": 6,
                     "source_stat": {"size_bytes": len(raw), "mtime_ns": 1},
                     "pixel_sha256": str(number) * 64, "colmap_sha256": "a" * 64, "openmvs_sha256": "b" * 64})
        legacy.append({"name": runtime_name, "sha256": sha(raw), "width": 8, "height": 6})
    # Positional mapping would be wrong: the prepared input order is deliberately
    # not the original-label order, while every byte identity remains unique.
    legacy = [legacy[2], legacy[0], legacy[1]]
    prepared = {"count": 3, "images": legacy}
    input_manifest = runtime / "input-manifest.json"
    input_sha = write_json(input_manifest, prepared)
    files = []
    for name in ("cameras.txt", "images.txt", "points3D.txt", "frames.txt", "rigs.txt"):
        path = sparse / name
        path.write_bytes(("# synthetic " + name + "\n").encode())
        files.append({"relative_path": "work/shared/colmap/refined_text/" + name,
                      "size_bytes": path.stat().st_size, "sha256": sha(path.read_bytes())})
    # These references are deliberately absent on disk. The small binding must
    # never read or stat a database or binary model as a hidden prerequisite.
    files += [{"relative_path": "work/shared/colmap/database.db", "size_bytes": 999, "sha256": "c" * 64},
              {"relative_path": "work/shared/colmap/mapper/0/images.bin", "size_bytes": 999, "sha256": "d" * 64}]
    manifest = {"schema_version": "1.0", "kind": "re3d_cpu_sfm_handoff",
                "source": {"pipeline_commit": sfm.BASE_COMMIT, "image_count": 3,
                           "prepared_input_manifest_sha256": input_sha},
                "sfm_gate": {"registered_images": 3}, "files": files,
                "completion": {key: True for key in sfm._COMPLETION}}
    binding = {"source_root": str(original), "mask_root": str(tmp_path / "labels"),
               "images": rows, "all_white_exceptions": ["00057.jpg"],
               "manifest_sha256": "e" * 64, "selection_manifest_sha256": "f" * 64,
               "execution_authorized": False}
    value = SimpleNamespace(root=tmp_path, runtime=runtime, handoff=handoff, sparse=sparse,
                            input_manifest=input_manifest, prepared=prepared, manifest=manifest, binding=binding)
    def publish():
        value.manifest["source"]["prepared_input_manifest_sha256"] = write_json(input_manifest, value.prepared)
        value.pin = write_json(handoff / "handoff-manifest.json", value.manifest)
        write_json(handoff / "DONE.json", {"schema_version": "1.0", "handoff_manifest_sha256": value.pin})
    value.publish = publish
    publish()
    def forbidden(*args, **kwargs):
        pytest.fail("SfM binding may not execute any child or reconstruction")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    return value


def run(case, **kwargs):
    return sfm.bind_sfm_images(case.binding, handoff_root=case.handoff, handoff_sha256=case.pin,
                               input_manifest=case.input_manifest, **kwargs)


def rejected(case):
    with pytest.raises(SkyMaskError) as error:
        run(case)
    assert error.value.code.startswith("SKY_")


def test_renamed_reordered_inputs_bind_by_hash_and_preserve_original_mask_names(case):
    before = inventory(case.root)
    old = deepcopy(case.binding)
    bound = run(case)
    assert Path(bound["source_root"]) == case.runtime / "images"
    assert bound["original_source_root"] == case.binding["source_root"]
    assert [row["filename"] for row in bound["images"]] == [row["name"] for row in case.prepared["images"]]
    assert [row["mask_filename"] for row in bound["images"]] == ["00057.jpg", "001.jpg", "002.jpg"]
    assert all(row["original_filename"] == row["mask_filename"] for row in bound["images"])
    for row in bound["images"]:
        stat = (case.runtime / "images" / row["filename"]).stat()
        assert row["source_stat"] == {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    assert bound["all_white_exceptions"] == [case.prepared["images"][0]["name"]]
    assert bound["mask_white_exceptions"] == ["00057.jpg"]
    assert Path(bound["sfm_source_sparse"]) == case.sparse
    assert bound["sfm_manifest_sha256"] == case.pin
    assert bound["sfm_input_manifest_sha256"] == sha(case.input_manifest.read_bytes())
    assert len(bound["sfm_text_files"]) == 5
    assert bound["sfm_masked"] is False and bound["sfm_database_read"] is False
    assert bound["sfm_binary_models_read"] is False
    assert all(bound[key] is False for key in ("execution_authorized", "budget_claimed", "budget_charged",
                                               "reconstruction_started", "server_accessed", "uac_requested"))
    assert case.binding == old and inventory(case.root) == before


@pytest.mark.parametrize("mutation", ["original_duplicate_hash", "input_duplicate_hash", "missing", "extra",
                                      "name_traversal", "name_duplicate", "stem_duplicate", "dimensions",
                                      "runtime_hash", "unexpected_runtime_image", "input_sha"])
def test_input_ambiguity_identity_and_names_are_rejected(case, mutation):
    if mutation == "original_duplicate_hash":
        case.binding["images"][1]["source_sha256"] = case.binding["images"][0]["source_sha256"]
    elif mutation == "input_duplicate_hash":
        case.prepared["images"][1]["sha256"] = case.prepared["images"][0]["sha256"]
    elif mutation == "missing":
        case.prepared["images"].pop()
    elif mutation == "extra":
        case.prepared["images"].append(dict(case.prepared["images"][0]))
    elif mutation == "name_traversal":
        case.prepared["images"][0]["name"] = "../outside.jpg"
    elif mutation == "name_duplicate":
        case.prepared["images"][1]["name"] = case.prepared["images"][0]["name"]
    elif mutation == "stem_duplicate":
        case.prepared["images"][1]["name"] = Path(case.prepared["images"][0]["name"]).stem + ".png"
    elif mutation == "dimensions":
        case.prepared["images"][0]["width"] = 99
    elif mutation == "runtime_hash":
        (case.runtime / "images" / case.prepared["images"][0]["name"]).write_bytes(b"changed input")
    elif mutation == "unexpected_runtime_image":
        (case.runtime / "images/extra.jpg").write_bytes(b"extra")
    else:
        case.input_manifest.write_bytes(case.input_manifest.read_bytes() + b" ")
        rejected(case)
        return
    case.publish()
    rejected(case)


@pytest.mark.parametrize("mutation", ["kind", "commit", "count", "registered", "completion", "done", "incomplete"])
def test_handoff_identity_and_completion_are_not_inferred_from_a_model_file(case, mutation):
    if mutation == "kind":
        case.manifest["kind"] = "unknown"
    elif mutation == "commit":
        case.manifest["source"]["pipeline_commit"] = "0" * 40
    elif mutation == "count":
        case.manifest["source"]["image_count"] = 2
    elif mutation == "registered":
        case.manifest["sfm_gate"]["registered_images"] = 2
    elif mutation == "completion":
        case.manifest["completion"]["process_tree_stopped"] = False
    elif mutation == "incomplete":
        write_json(case.handoff / "INCOMPLETE.json", {"status": "incomplete"})
    else:
        write_json(case.handoff / "DONE.json", {"handoff_manifest_sha256": "0" * 64})
        rejected(case)
        return
    case.publish()
    rejected(case)


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra", "bad_manifest_path", "duplicate_record"])
def test_only_exact_five_text_payloads_are_verified(case, mutation):
    if mutation == "missing":
        (case.sparse / "frames.txt").unlink()
    elif mutation == "changed":
        (case.sparse / "images.txt").write_bytes(b"changed text")
    elif mutation == "extra":
        (case.sparse / "foreign.txt").write_bytes(b"not allowed")
    elif mutation == "bad_manifest_path":
        case.manifest["files"][0]["relative_path"] = "work/shared/colmap/refined_text/../cameras.txt"
    else:
        case.manifest["files"].append(dict(case.manifest["files"][0]))
    case.publish()
    rejected(case)


@pytest.mark.parametrize("leaf", ["image", "text", "input_manifest", "handoff_manifest"])
def test_reparse_leaf_is_rejected_without_creating_links_or_uac(case, monkeypatch, leaf):
    target = {"image": case.runtime / "images" / case.prepared["images"][0]["name"],
              "text": case.sparse / "images.txt", "input_manifest": case.input_manifest,
              "handoff_manifest": case.handoff / "handoff-manifest.json"}[leaf]
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=1024) if path == target else actual
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(SkyMaskError, match="REPARSE"):
        run(case)


def test_additive_guard_can_stop_binding_without_writing_or_reading_database(case):
    calls, before = [], inventory(case.root)
    def guard():
        calls.append(True)
        raise SkyMaskError("SKY_SYNTHETIC_GUARD_STOP")
    with pytest.raises(SkyMaskError, match="GUARD_STOP"):
        run(case, guard=guard)
    assert calls and inventory(case.root) == before


def test_database_and_binary_paths_are_not_opened_or_statted(case, monkeypatch):
    original_stat, original_open = Path.stat, Path.open
    def stat(path, *args, **kwargs):
        assert path.suffix not in {".db", ".bin"}, "forbidden model stat"
        return original_stat(path, *args, **kwargs)
    def open_file(path, *args, **kwargs):
        assert path.suffix not in {".db", ".bin"}, "forbidden model read"
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "open", open_file)
    assert run(case)["sfm_database_read"] is False
