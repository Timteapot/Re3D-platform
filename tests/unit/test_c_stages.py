from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

from backend.experiments import c_stages, stages
from backend.experiments.preflight import ExperimentGateError


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


FIXTURE_COMMANDS = [
    ("01-colmap", ["@PYTHON@", "@PIPELINE@/scripts/run_colmap_shared.py", "--images", "@ROOT@/work/input/images", "--masks", "@ROOT@/work/input/masks", "--output", "@ROOT@/work/shared/colmap", "--camera-model", "PINHOLE", "--random-seed", "0"], "work/shared/colmap/reconstruction_metrics.json"),
    ("02-extract-cameras", ["@PYTHON@", "@PIPELINE@/scripts/extract_colmap_cameras.py", "--model", "@ROOT@/work/shared/colmap/refined_text", "--output", "@ROOT@/work/shared/cameras_refined.npz"], "work/shared/cameras_refined.npz"),
    ("03-prepare-openmvs-input", ["@PYTHON@", "@PIPELINE@/scripts/prepare_openmvs_texture_input.py", "--images", "@ROOT@/work/input/images", "--sparse", "@ROOT@/work/shared/colmap/refined_text", "--output", "@ROOT@/work/shared/openmvs_input", "--alpha-threshold", "128"], "work/shared/openmvs_input/manifest.json"),
    ("10-c-interface", ["@PIPELINE@/vendor/openmvs-2.4.0-windows/vc17/x64/Release/InterfaceCOLMAP.exe", "-w", "@ROOT@/work/branches/c_openmvs", "-i", "@ROOT@/work/shared/openmvs_input", "-o", "scene.mvs", "--binary", "1"], "work/branches/c_openmvs/scene.mvs"),
    ("11-c-densify", ["@PIPELINE@/vendor/openmvs-2.4.0-windows/vc17/x64/Release/DensifyPointCloud.exe", "-w", "@ROOT@/work/branches/c_openmvs", "-i", "scene.mvs", "-o", "scene_dense.mvs", "--resolution-level", "1", "--min-resolution", "640", "--max-resolution", "1024", "--number-views", "6", "--mask-path", "@ROOT@/work/shared/openmvs_input/images", "--ignore-mask-label", "0", "--max-threads", "8"], "work/branches/c_openmvs/scene_dense.mvs"),
    ("12-c-reconstruct", ["@PIPELINE@/vendor/openmvs-2.4.0-windows/vc17/x64/Release/ReconstructMesh.exe", "-w", "@ROOT@/work/branches/c_openmvs", "-i", "scene_dense.mvs", "-o", "scene_mesh.mvs", "--remove-spurious", "4", "--close-holes", "30", "--smooth", "2", "--max-threads", "8"], "work/branches/c_openmvs/scene_mesh.ply"),
    ("13-c-texture", ["@PIPELINE@/vendor/openmvs-2.4.0-33d9484-windows/vc18/x64/Release/TextureMesh.exe", "-w", "@ROOT@/work/branches/c_openmvs", "-i", "scene.mvs", "--mesh-file", "scene_mesh.ply", "-o", "scene_textured_final.mvs", "--export-type", "obj", "--max-texture-size", "8192", "--global-seam-leveling", "0", "--local-seam-leveling", "0", "--sharpness-weight", "0.25", "--outlier-threshold", "0.06", "--ignore-mask-label", "0", "--empty-color", "16777215", "--max-threads", "8"], "work/branches/c_openmvs/scene_textured_final.obj"),
    ("13-c-normalize", ["@PYTHON@", "@PIPELINE@/scripts/normalize_openmvs_output.py", "--obj", "@ROOT@/work/branches/c_openmvs/scene_textured_final.obj", "--mtl", "@ROOT@/work/branches/c_openmvs/scene_textured_final.mtl", "--texture", "@ROOT@/work/branches/c_openmvs/scene_textured_final_material_00_map_Kd.jpg", "--output", "@ROOT@/output/C"], "output/C/mesh.glb"),
]

FIXTURE_SOURCE = '''
import json
from pathlib import Path
def read_json(path):
    return json.loads(Path(path).read_bytes())
class Pipeline:
    def __init__(self, *, scene, images, branches, config_path, paths_path, dry_run, work_dir, output_dir, log_dir):
        assert branches == {'c'} and dry_run and images is None
        self.config = read_json(config_path)
        paths = read_json(paths_path)
        self.map_python = paths['mapanything_python']
        self.mvs_python = paths['mvsanywhere_python']  # A fake placeholder, never used.
        self.root = Path(__file__).parents[1]
        self.work = Path(work_dir)
        self.outputs = Path(output_dir)
        self.base_env = {'PYTHONPATH': 'must-not-be-required'}
        assert self.config['c']['openmvs_threads'] == self.config['texture']['max_threads'] == 8
    def prepare(self):
        raise AssertionError('prepare must be replaced, never create markers')
    def run(self):
        assert self.prepare() == (2048, 1080)
        def replace(value):
            if value.startswith('@PYTHON@'):
                return self.map_python
            if value.startswith('@PIPELINE@/'):
                return str(self.root / value[len('@PIPELINE@/'):])
            if value.startswith('@ROOT@/'):
                return str(self.work.parent / value[len('@ROOT@/'):])
            return value
        for name, argv, marker in COMMANDS:
            self.step(name, [replace(value) for value in argv], self.work.parent / marker)
        self.validate()
    def validate(self):
        raise AssertionError('C-only validation must be provided by Recorder')
'''


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    pipeline = tmp_path / "synthetic-pipeline"
    (pipeline / "scripts").mkdir(parents=True)
    source = pipeline / "scripts/run_pipeline.py"
    source.write_text("COMMANDS = " + repr(FIXTURE_COMMANDS) + "\n" + FIXTURE_SOURCE, encoding="utf-8")
    colmap = pipeline / "scripts/run_colmap_shared.py"
    colmap.write_text("raise AssertionError('never execute SfM')\n", encoding="utf-8")
    baseline = {"input": {"alpha_threshold": 128},
                "camera_frontend": {"camera_model": "PINHOLE", "random_seed": 0, "deferred_registration": {"enabled": False}},
                "openmvs_dense_resolution": {"resolution_level": 1, "min_resolution": 640, "max_resolution": 1024},
                "c": {"number_views": 6, "ignore_mask_label": 0, "openmvs_threads": 24},
                "mesh": {"remove_spurious": 4, "close_holes": 30, "smooth": 2},
                "texture": {"max_threads": 24, "max_texture_size": 8192, "global_seam_leveling": 0,
                            "local_seam_leveling": 0, "sharpness_weight": .25, "outlier_threshold": .06,
                            "ignore_mask_label": 0, "empty_color": 16777215}}
    config, paths = pipeline / "configs/pipeline.json", pipeline / "configs/paths.json"
    write_json(config, baseline)
    # No B interpreter, A/B weights, executable files, or real input dataset.
    write_json(paths, {"mapanything_python": sys.executable})
    monkeypatch.setattr(c_stages, "PIPELINE_SCRIPT_SHA", hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(c_stages, "COLMAP_SCRIPT_SHA", hashlib.sha256(colmap.read_bytes()).hexdigest())
    monkeypatch.setattr(c_stages, "BASE_CONFIG_SHA", hashlib.sha256(config.read_bytes()).hexdigest())
    for key in c_stages._OVERRIDES:
        monkeypatch.delenv(key, raising=False)
    return dict(pipeline_root=pipeline, config_path=config, paths_path=paths,
                experiment_root=tmp_path / "_experiments/c-only", dimensions=(2048, 1080))


def test_plan_eight_stages_without_sfm_or_runtime_creation(fixture, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no process permitted"))
    before = fixture["config_path"].read_bytes()
    plan = c_stages.build_c_stage_plan(**fixture)
    assert tuple(stage.name for stage in plan.stages) == c_stages.C_STAGE_NAMES
    assert len(plan.stages) == 8
    assert not fixture["experiment_root"].exists()
    assert fixture["config_path"].read_bytes() == before
    assert "PYTHONPATH" not in plan.environment
    assert all("mvsanywhere" not in " ".join(stage.argv) and "mapanything" not in " ".join(stage.argv) for stage in plan.stages)
    assert plan.stages[-1].argv[-3:] == ("C", "--report", str(fixture["experiment_root"] / "output/validation.json"))
    assert all(stage.argv[stage.argv.index("--max-threads") + 1] == "8" for stage in plan.stages if "--max-threads" in stage.argv)
    assert len([stage for stage in plan.stages if stage.required_directories]) == 4


def test_existing_markers_are_not_reused_or_created(fixture):
    marker = fixture["experiment_root"] / "work/shared/colmap/reconstruction_metrics.json"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"existing private marker untouched")
    plan = c_stages.build_c_stage_plan(**fixture)
    assert len(plan.stages) == 8
    assert marker.read_bytes() == b"existing private marker untouched"
    assert not (fixture["experiment_root"] / "output").exists()
    assert all(not stage.observation()["existing_marker_reused"] for stage in plan.stages)


def test_overlay_only_changes_two_thread_fields():
    baseline = {"c": {"openmvs_threads": 24, "number_views": 6}, "texture": {"max_threads": 24, "max_texture_size": 8192}, "a": {"weights": "not-read"}}
    original = deepcopy(baseline)
    overlay, diff = c_stages.build_c_pipeline_overlay(baseline)
    assert baseline == original
    assert {row["path"] for row in diff} == {"/c/openmvs_threads", "/texture/max_threads"}
    assert overlay["c"]["number_views"] == 6 and overlay["texture"]["max_texture_size"] == 8192
    with pytest.raises(ExperimentGateError, match="THREAD_BUDGET"):
        c_stages.build_c_pipeline_overlay(baseline, openmvs_threads=12)


@pytest.mark.parametrize("mutation,code", [("script", "FROZEN_SCRIPT"), ("config", "FROZEN_CONFIG"), ("texture", "TEXTURE_BINARY"), ("python", "MAP_PYTHON")])
def test_plan_rejects_changed_identity(fixture, mutation, code):
    if mutation == "script":
        path = fixture["pipeline_root"] / "scripts/run_pipeline.py"
        path.write_text(path.read_text() + "\nraise AssertionError('must not load')\n")
    elif mutation == "config":
        value = json.loads(fixture["config_path"].read_bytes())
        value["c"]["number_views"] = 7
        write_json(fixture["config_path"], value)
    elif mutation == "texture":
        write_json(fixture["paths_path"], {"mapanything_python": sys.executable, "texturemesh_executable": "unapproved.exe"})
    else:
        fixture["map_python"] = fixture["pipeline_root"] / "wrong-python.exe"
    with pytest.raises(ExperimentGateError, match=code):
        c_stages.build_c_stage_plan(**fixture)


def test_frozen_command_and_marker_contract_is_checked(fixture, monkeypatch):
    path = fixture["pipeline_root"] / "scripts/run_pipeline.py"
    text = path.read_text().replace("'--number-views', '6'", "'--number-views', '7'")
    path.write_text(text)
    monkeypatch.setattr(c_stages, "PIPELINE_SCRIPT_SHA", hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ExperimentGateError, match="FROZEN_ARGV"):
        c_stages.build_c_stage_plan(**fixture)


def test_environment_override_refused_before_loading(fixture, monkeypatch):
    monkeypatch.setenv("RE3D_MVS_PYTHON", "must-not-apply-even-to-C")
    with pytest.raises(ExperimentGateError, match="ENVIRONMENT_OVERRIDE"):
        c_stages.build_c_stage_plan(**fixture)


def output_fixture(root):
    folder = root / "output/C"
    folder.mkdir(parents=True)
    (folder / "mesh.obj").write_text("mtllib mesh.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    (folder / "mesh.mtl").write_text("newmtl material\nd 1.000000\nmap_Kd texture.jpg\n")
    (folder / "texture.jpg").write_bytes(b"\xff\xd8\xff\xe0fixture\xff\xd9")
    gltf = {"asset": {"version": "2.0"}, "accessors": [{"count": 3}], "meshes": [{"primitives": [{"indices": 0}]}]}
    payload = json.dumps(gltf).encode()
    payload += b" " * ((-len(payload)) % 4)
    binary = b"\0" * 12
    size = 12 + 8 + len(payload) + 8 + len(binary)
    (folder / "mesh.glb").write_bytes(struct.pack("<4sII", b"glTF", 2, size) + struct.pack("<I4s", len(payload), b"JSON")
                                      + payload + struct.pack("<I4s", len(binary), b"BIN\0") + binary)
    write_json(root / "output/validation.json", {"all_passed": True, "branches": {"C": {"status": "pass", "errors": [], "obj_vertices": 3, "obj_faces": 1, "glb_faces": 1}}})


def test_c_only_outputs_four_hashed_artifacts_without_global_mutation(tmp_path):
    output_fixture(tmp_path)
    labels = stages.LABELS
    result = c_stages.gate_c_outputs(tmp_path)
    assert result["all_passed"] and result["artifact_count"] == 4
    assert not result["geometry_accuracy_certified"]
    assert all(len(row["sha256"]) == 64 for row in result["artifacts"])
    assert stages.LABELS == labels == ("A-v4", "B-v2", "C")


@pytest.mark.parametrize("mutation,code", [("extra_branch", "VALIDATION"), ("material", "MATERIAL_REFERENCE"), ("texture", "TEXTURE_REFERENCE"), ("opacity", "TEXTURE_REFERENCE"), ("glb", "GLB_HEADER"), ("jpeg", "JPEG"), ("faces", "FACE_COUNT")])
def test_bad_c_outputs_rejected(tmp_path, mutation, code):
    output_fixture(tmp_path)
    folder = tmp_path / "output/C"
    if mutation == "extra_branch":
        path = tmp_path / "output/validation.json"
        value = json.loads(path.read_bytes()); value["branches"]["A-v4"] = {}
        write_json(path, value)
    elif mutation == "material":
        (folder / "mesh.obj").write_text((folder / "mesh.obj").read_text() + "mtllib ../../outside.mtl\n")
    elif mutation == "texture":
        (folder / "mesh.mtl").write_text((folder / "mesh.mtl").read_text() + "map_Kd ../outside.jpg\n")
    elif mutation == "opacity":
        (folder / "mesh.mtl").write_text((folder / "mesh.mtl").read_text() + "d 0.1\n")
    elif mutation == "glb":
        path = folder / "mesh.glb"; path.write_bytes(b"nope" + path.read_bytes()[4:])
    elif mutation == "jpeg":
        (folder / "texture.jpg").write_bytes(b"x")
    else:
        path = tmp_path / "output/validation.json"
        value = json.loads(path.read_bytes()); value["branches"]["C"]["glb_faces"] = 2
        write_json(path, value)
    with pytest.raises(ExperimentGateError, match=code):
        c_stages.gate_c_outputs(tmp_path)


def test_c_stage_gate_cannot_admit_a_or_fake_marker(tmp_path):
    stage = stages.StageSpec("20-a-mapanything", (), tmp_path / "fake", tmp_path)
    with pytest.raises(ExperimentGateError, match="SCOPE"):
        c_stages.gate_c_stage(stage, tmp_path, ())
    output_fixture(tmp_path)
    stage = stages.StageSpec("90-validate", (), tmp_path / "output/validation.json", tmp_path)
    assert c_stages.gate_c_stage(stage, tmp_path, ())["artifact_count"] == 4


def test_prepared_input_gate_rejects_malformed_rows(tmp_path):
    marker = tmp_path / "work/shared/openmvs_input/manifest.json"
    write_json(marker, {"count": 300, "images": [None]})
    names = tuple(f"{index:06}.jpg" for index in range(300))
    stage = stages.StageSpec("03-prepare-openmvs-input", (), marker, tmp_path)
    with pytest.raises(ExperimentGateError, match="INPUT_IDENTITY"):
        c_stages.gate_c_stage(stage, tmp_path, names)


def test_c_gate_rejects_reparse_without_real_symlink_privileges(tmp_path, monkeypatch):
    marker = tmp_path / "work/branches/c_openmvs/scene.mvs"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"mvs")
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=1024) if path == marker else result
    monkeypatch.setattr(Path, "lstat", lstat)
    stage = stages.StageSpec("10-c-interface", (), marker, tmp_path)
    with pytest.raises(ExperimentGateError, match="REPARSE"):
        c_stages.gate_c_stage(stage, tmp_path, ())
