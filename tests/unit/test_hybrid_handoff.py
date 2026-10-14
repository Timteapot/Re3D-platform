from __future__ import annotations

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import struct
from types import SimpleNamespace

import pytest

from backend.experiments import hybrid_handoff as handoff
from backend.experiments.hybrid_checkpoint import verify_hybrid_source
from backend.experiments.preflight import ExperimentGateError
from backend.experiments.stages import gate_sfm
from tests.unit import test_experiment_checkpoint as checkpoint_fixture
from tests.unit.test_experiment_checkpoint import document, digest


def model(root, names):
    folder = root / "work/shared/colmap"
    binary, text = folder / "mapper/0", folder / "refined_text"
    binary.mkdir(parents=True); text.mkdir(parents=True)
    (binary / "cameras.bin").write_bytes(struct.pack("<Q", 1) + struct.pack("<iiQQ4d", 1, 1, 1, 1, 1, 1, .5, .5))
    (text / "cameras.txt").write_text("1 PINHOLE 1 1 1 1 0.5 0.5\n")
    images, text_images = struct.pack("<Q", 3), "# genuine format, synthetic model\n"
    for i in range(3):
        images += struct.pack("<i7di", i + 1, 1, 0, 0, 0, i, 0, 0, 1) + names[i].encode() + b"\0"
        images += struct.pack("<Q", 1) + struct.pack("<ddq", 0, 0, 1)
        text_images += f"{i+1} 1 0 0 0 {i} 0 0 1 {names[i]}\n0 0 1\n"
    (binary / "images.bin").write_bytes(images)
    (text / "images.txt").write_text(text_images)
    point = struct.pack("<Q", 1) + struct.pack("<Q3d3BdQ", 1, 0, 0, 3, 128, 128, 128, .2, 3)
    (binary / "points3D.bin").write_bytes(point + struct.pack("<6i", 1, 0, 2, 0, 3, 0))
    (text / "points3D.txt").write_text("1 0 0 3 128 128 128 0.2 1 0 2 0 3 0\n")
    # Real versions may emit these additional files; none may be silently lost.
    for name in ("rigs", "frames"):
        (binary / (name + ".bin")).write_bytes(struct.pack("<Q", 0))
        (text / (name + ".txt")).write_text("# empty synthetic " + name + "\n")
    metrics = {"database_images": 300, "registered_images": 3, "cameras": 1, "points3D": 1,
               "observations": 3, "mean_reprojection_error_px": .2, "verified_pairs_min_15": 1,
               "binary_model": str(binary), "text_model": str(text)}
    document(folder / "reconstruction_metrics.json", metrics)
    return metrics


@pytest.fixture
def fixture():
    case = checkpoint_fixture.ExperimentCheckpointTests()
    case.setUp()
    try:
        with closing(sqlite3.connect(case.database)) as db, db:
            db.execute("ALTER TABLE images ADD COLUMN camera_id INTEGER DEFAULT 1")
            db.execute("CREATE TABLE cameras(camera_id INTEGER PRIMARY KEY,model INTEGER,width INTEGER,height INTEGER,params BLOB)")
            db.execute("INSERT INTO cameras VALUES(1,1,1,1,?)", (struct.pack("<4d", 1, 1, .5, .5),))
            for table in ("matches", "two_view_geometries"):
                db.execute(f"UPDATE {table} SET rows=30,data=? WHERE pair_id=?", (b"\0" * 240, 2147483649))
        case.refresh_manifest()
        source = verify_hybrid_source(original_root=case.source, original_manifest_sha256=case.manifest_sha,
                    original_database_sha256=case.database_sha, source_job_id=case.manifest["job_id"],
                    pipeline_commit=case.pipeline["commit"], config_sha256=case.config_sha)
        root = case.experiments / "cpu-done"
        root.mkdir()
        for row in source.original.files:
            if row.relative_path.startswith("runtime/work/input/"):
                dest = root / row.relative_path.removeprefix("runtime/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(case.source / row.relative_path, dest)
        model(root, source.image_names)
        shutil.copyfile(case.database, root / "work/shared/colmap/database.db")
        options = {"schema_version": "1.0", "status": "succeeded", "sfm_stage_complete": True,
             "colmap_script_sha256": "c" * 64, "baseline_config_sha256": case.config_sha,
             "runtime_versions": {"python": "3.12.14", "pycolmap": "3.13.0", "pycolmap_has_cuda": False},
             "actual_extraction": {"num_threads": 12, "max_image_size": 1600, "max_num_features": 6000, "device": "cpu"},
             "actual_matching": {"num_threads": 12, "guided_matching": True, "device": "cpu"},
             "actual_mapper_options": [{"call": 1, "phase": "initial", "num_threads": 16, "mapper_num_threads": 16,
                                        "ba_inherits_top_level_num_threads": True}],
             "protection_owned_by_parent": True, "child_provides_resource_protection": False}
        document(root / "reports/sfm-actual-options.json", options)
        gate = gate_sfm(root, source.image_names, expected_mapper_threads=16)
        report = {"status": "succeeded", "mode": "cpu-sfm", "process_tree_stopped": True,
                  "snapshot_worker_stopped": True, "terminal_evidence_confirmed": True,
                  "source_checkpoint_unchanged": True, "sfm_gate": gate}
        document(root / "reports/cpu-result.json", report)
        arguments = {"input_source": source, "sfm_threads": 12, "mapper_threads": 16,
                     "python_version": "3.12.14", "quality_config_path": case.source / "identity/pipeline.json",
                     "quality_config_sha256": case.config_sha, "pipeline_script_sha256": "a" * 64,
                     "colmap_script_sha256": "c" * 64}
        yield SimpleNamespace(case=case, source=source, root=root, options=options, report=report,
                              gate=gate, arguments=arguments, bundle=case.base / "bundle")
    finally:
        case.doCleanups()


def verify_arguments(f):
    return {"input_source": f.source, "expected_quality_config_sha256": f.case.config_sha,
            "expected_pipeline_script_sha256": "a" * 64, "expected_colmap_script_sha256": "c" * 64}


def repin(bundle):
    sha = digest(bundle / handoff.MANIFEST)
    document(bundle / "DONE.json", {"schema_version": "1.0", "handoff_manifest_sha256": sha})
    return sha


def test_export_verify_import_preserves_source_and_rebinds_only_two_metrics_fields(fixture):
    f = fixture
    before = f.case.fingerprint()
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    assert len(saved.manifest["files"]) == 14
    assert saved.manifest["complete_pair_records"] == 2
    target = f.case.experiments / "server-import"
    result = handoff.import_sfm_handoff(saved, target, input_source=f.source)
    assert result.first_stage == "02-extract-cameras" and result.sfm_gate == f.gate
    assert f.case.fingerprint() == before
    assert not (target / "manifests/pipeline-request.json").exists()
    assert (target / "work/source-database-before-sfm.db").is_file()
    assert (target / "work/shared/colmap/mapper/0/frames.bin").is_file()
    old = json.loads((f.root / "work/shared/colmap/reconstruction_metrics.json").read_bytes())
    new = json.loads((target / "work/shared/colmap/reconstruction_metrics.json").read_bytes())
    assert {k: v for k, v in new.items() if k not in {"binary_model", "text_model"}} == {
        k: v for k, v in old.items() if k not in {"binary_model", "text_model"}}
    assert json.loads(result.provenance_path.read_bytes())["original_metrics"] == old
    assert new["binary_model"] == str(target / "work/shared/colmap/mapper/0")


@pytest.mark.parametrize("key", ["process_tree_stopped", "snapshot_worker_stopped", "terminal_evidence_confirmed",
                                 "source_checkpoint_unchanged"])
def test_export_rejects_unconfirmed_cpu_completion(fixture, key):
    f = fixture
    f.report[key] = False
    document(f.root / "reports/cpu-result.json", f.report)
    with pytest.raises(handoff.HandoffError, match="CPU_COMPLETION_UNCONFIRMED"):
        handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    assert not f.bundle.exists()


@pytest.mark.parametrize("mutation", ["image", "mask", "prepared", "extra_model", "metrics", "options_secret", "source_config"])
def test_export_rejects_changed_inputs_fabrication_or_unlisted_payload(fixture, mutation):
    f = fixture
    if mutation == "image":
        (f.root / "work/input/images" / f.source.image_names[0]).write_bytes(b"changed")
    elif mutation == "mask":
        (f.root / "work/input/masks" / (f.source.image_names[0] + ".png")).write_bytes(b"changed")
    elif mutation == "prepared":
        (f.root / "work/input/input-manifest.json").write_text("{}")
    elif mutation == "extra_model":
        (f.root / "work/shared/colmap/mapper/0/.env").write_text("not a model")
    elif mutation == "metrics":
        path = f.root / "work/shared/colmap/reconstruction_metrics.json"
        metrics = json.loads(path.read_bytes()); metrics["registered_images"] = 300; document(path, metrics)
    elif mutation == "options_secret":
        f.options["password"] = None; document(f.root / "reports/sfm-actual-options.json", f.options)
    else:
        f.arguments["quality_config_sha256"] = "f" * 64
    with pytest.raises((ValueError, ExperimentGateError)):
        handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    assert not f.bundle.exists()


@pytest.mark.parametrize("mutation", ["database", "extra", "empty_directory", "missing_model", "case_duplicate", "traversal", "manifest_field", "source"])
def test_verify_rejects_tampering_even_rehashed_manifest(fixture, mutation):
    f = fixture
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    manifest = json.loads((f.bundle / handoff.MANIFEST).read_bytes())
    if mutation == "database":
        path = f.bundle / "payload/work/shared/colmap/database.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE descriptors SET data=? WHERE image_id=1", (b"x" * 128,))
        row = next(r for r in manifest["files"] if r["relative_path"].endswith("database.db"))
        row["sha256"] = digest(path); row["size_bytes"] = path.stat().st_size
    elif mutation == "extra":
        (f.bundle / "payload/.env").write_text("rejected")
    elif mutation == "empty_directory":
        (f.bundle / "empty-secret-folder").mkdir()
    elif mutation == "missing_model":
        (f.bundle / "payload/work/shared/colmap/mapper/0/cameras.bin").unlink()
    elif mutation == "case_duplicate":
        manifest["files"][-1]["relative_path"] = manifest["files"][0]["relative_path"].upper()
    elif mutation == "traversal":
        manifest["files"][-1]["relative_path"] = "../outside.env"
    elif mutation == "manifest_field":
        manifest["private_env"] = "not permitted"
    else:
        manifest["source"]["selected_database_sha256"] = "f" * 64
    document(f.bundle / handoff.MANIFEST, manifest)
    sha = repin(f.bundle)
    with pytest.raises(ValueError):
        handoff.verify_sfm_handoff(f.bundle, sha, **verify_arguments(f))


def test_export_import_never_overwrite_existing_roots_or_old_jobs(fixture):
    f = fixture
    f.bundle.mkdir()
    (f.bundle / "preserve.txt").write_text("retain")
    with pytest.raises(handoff.HandoffError, match="NEW_SEPARATE"):
        handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    bundle = f.case.base / "new-bundle"
    saved = handoff.export_sfm_handoff(f.root, bundle, **f.arguments)
    with pytest.raises(handoff.HandoffError, match="NEW_EXPERIMENT"):
        handoff.import_sfm_handoff(saved, f.root, input_source=f.source)
    with pytest.raises(handoff.HandoffError, match="NEW_EXPERIMENT"):
        handoff.import_sfm_handoff(saved, f.case.base / "jobs/old", input_source=f.source)
    assert (f.bundle / "preserve.txt").read_text() == "retain"


def test_manifest_external_pin_and_reparse_are_required(fixture, monkeypatch):
    f = fixture
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    with pytest.raises(handoff.HandoffError, match="MANIFEST_HASH"):
        handoff.verify_sfm_handoff(f.bundle, "f" * 64, **verify_arguments(f))
    target = f.bundle / "payload/work/shared/colmap/database.db"
    original = Path.lstat
    def changed(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=1024)
        return value
    monkeypatch.setattr(Path, "lstat", changed)
    with pytest.raises(handoff.HandoffError, match="REPARSE"):
        handoff.verify_sfm_handoff(f.bundle, saved.manifest_sha256, **verify_arguments(f))


def test_legacy_sfm_gate_stays_12_32_and_cpu_expectations_are_explicit(fixture):
    f = fixture
    with pytest.raises(ExperimentGateError, match="ACTUAL_MAPPER_MISMATCH"):
        gate_sfm(f.root, f.source.image_names)
    assert gate_sfm(f.root, f.source.image_names, expected_sfm_threads=12, expected_mapper_threads=16) == f.gate
    f.options["actual_extraction"]["num_threads"] = 8
    f.options["actual_matching"]["num_threads"] = 8
    document(f.root / "reports/sfm-actual-options.json", f.options)
    assert gate_sfm(f.root, f.source.image_names, expected_sfm_threads=8, expected_mapper_threads=16) == f.gate


def test_verification_does_not_certify_payload_changed_after_hash_pass(fixture, monkeypatch):
    f = fixture
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    original_gate = handoff._gate
    def changed(root, source, execution, health):
        result = original_gate(root, source, execution, health)
        path = root / "reports/sfm-actual-options.json"
        path.write_bytes(path.read_bytes() + b"\n")
        return result
    monkeypatch.setattr(handoff, "_gate", changed)
    with pytest.raises(handoff.HandoffError, match="PAYLOAD_CHANGED_DURING_VALIDATION"):
        handoff.verify_sfm_handoff(f.bundle, saved.manifest_sha256, **verify_arguments(f))


def test_real_actual_option_recording_mapper_schema_is_accepted_by_handoff():
    wrapper_path = Path(__file__).resolve().parents[2] / "scripts/run_profiled_colmap.py"
    spec = importlib.util.spec_from_file_location("handoff_real_profiled_colmap_hook", wrapper_path)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    native = SimpleNamespace(Device=SimpleNamespace(cpu="cpu"), extract_features=lambda **kwargs: None,
                             match_exhaustive=lambda **kwargs: None)
    def factory(**kwargs):
        return SimpleNamespace(num_threads=4, mapper=SimpleNamespace(num_threads=4), check=lambda: True)
    frozen = SimpleNamespace(pycolmap=native, mapping_options=factory)
    options = {"runtime_versions": {"python": "3.12.14", "pycolmap": "3.13.0", "pycolmap_has_cuda": False},
               "actual_mapper_options": [], "colmap_script_sha256": "c" * 64, "baseline_config_sha256": "b" * 64}
    with wrapper.actual_option_recording(frozen, 12, 16, options, lambda: None):
        frozen.mapping_options(random_seed=0)
        frozen.mapping_options(random_seed=0)
    assert [row["phase"] for row in options["actual_mapper_options"]] == ["initial", "deferred_retry"]
    assert all(row["ba_inherits_top_level_num_threads"] is True for row in options["actual_mapper_options"])
    handoff._options(options, {"sfm_threads": 12, "mapper_threads": 16}, "b" * 64, "c" * 64)


def test_handoff_rejects_false_ba_thread_inheritance_from_actual_hook_schema():
    options = {"runtime_versions": {"python": "3.12.14", "pycolmap": "3.13.0", "pycolmap_has_cuda": False},
               "colmap_script_sha256": "c" * 64, "baseline_config_sha256": "b" * 64,
               "actual_mapper_options": [{"call": 1, "phase": "initial", "num_threads": 16,
                  "mapper_num_threads": 16, "ba_inherits_top_level_num_threads": False}]}
    with pytest.raises(handoff.HandoffError, match="MAPPER_FIELDS_INVALID"):
        handoff._options(options, {"sfm_threads": 12, "mapper_threads": 16}, "b" * 64, "c" * 64)
