from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
import sys
from types import SimpleNamespace

import pytest

from backend.experiments import high_profile, stages as stage_module
from backend.experiments.high_profile import build_pipeline_overlay
from backend.experiments.long_runner import STAGE_NAMES
from backend.experiments.preflight import ExperimentGateError
from backend.experiments.stages import (
    LABELS, StageSpec, build_stage_plan, gate_outputs, gate_sfm, gate_stage, prepared_dimensions,
)

WRAPPER_PATH = Path(__file__).resolve().parents[2] / "scripts/run_profiled_colmap.py"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


# Synthetic external Pipeline interface. The platform planner and all admission
# functions remain real; the separate frozen Re3D checkout is opt-in integration.
PLANNER_STAGE_NAMES = (
    "01-colmap", "02-extract-cameras", "03-prepare-openmvs-input", "10-c-interface",
    "11-c-densify", "12-c-reconstruct", "13-c-texture", "13-c-normalize",
    "20-a-mapanything", "21-a-scale-calibration", "22-a-crossview-v4", "23-a-export-dmap",
    "23a-a-validate-dmap", "23b-a-stage-openmvs", "24-a-fuse", "24a-a-verify-handoff",
    "25-a-reconstruct", "26-a-texture", "26-a-normalize", "30-b-mvsanywhere",
    "31-b-scale-calibration", "32-b-crossview-v2", "33-b-export-dmap", "33a-b-validate-dmap",
    "33b-b-stage-openmvs", "34-b-fuse", "34a-b-verify-handoff", "35-b-reconstruct",
    "36-b-texture", "36-b-normalize",
)
PLANNER_SOURCE = '''
import json
from pathlib import Path

class Pipeline:
    def __init__(self, *, scene, images, branches, config_path, paths_path,
                 dry_run, work_dir, output_dir, log_dir):
        assert dry_run and branches == {"a", "b", "c"}
        self.config = json.loads(Path(config_path).read_bytes())
        paths = json.loads(Path(paths_path).read_bytes())
        self.map_python = paths["map_python"]
        self.base_env = {}
        self.work = Path(work_dir)
        self.outputs = Path(output_dir)
        self.root = Path(paths_path).parents[1]

    def script(self, name):
        return self.root / "scripts" / name

    def run(self):
        self.prepare()
        capped = {"11-c-densify", "12-c-reconstruct", "13-c-texture",
                  "24-a-fuse", "25-a-reconstruct", "26-a-texture",
                  "34-b-fuse", "35-b-reconstruct", "36-b-texture"}
        for name in STAGES:
            command = [self.map_python, self.script("fixture_stage.py")]
            if name == "01-colmap":
                command = [self.map_python, self.script("run_colmap_shared.py"),
                           "--images", self.work / "input/images"]
            elif name == "20-a-mapanything":
                command += ["--batch-size", self.config["a_v4"]["batch_size"]]
            elif name in capped:
                group = "c" if "-c-" in name else "a_v4" if "-a-" in name else "b_v2"
                budget = self.config["texture"]["max_threads"] if "texture" in name else self.config[group]["openmvs_threads"]
                command += ["--max-threads", budget, "-w", self.work / name]
            self.step(name, command, self.work / (name + ".complete"))
        self.validate()

    def prepare(self):
        raise AssertionError("The platform recorder must override prepare")

    def validate(self):
        raise AssertionError("The platform recorder must add final validation")
'''


@pytest.fixture
def pipeline_fixture(tmp_path, monkeypatch):
    pipeline = tmp_path / "synthetic-pipeline"
    (pipeline / "scripts").mkdir(parents=True)
    planner = pipeline / "scripts/run_pipeline.py"
    planner.write_text("STAGES = " + repr(PLANNER_STAGE_NAMES) + "\n" + PLANNER_SOURCE, encoding="utf-8")
    colmap = pipeline / "scripts/run_colmap_shared.py"
    colmap.write_text("raise AssertionError('Synthetic COLMAP must never execute')\n", encoding="utf-8")
    baseline = {
        "a_v4": {"batch_size": 4, "openmvs_threads": 16},
        "b_v2": {"source_views": 7, "height": 384, "width": 384, "refinement_steps": 1, "openmvs_threads": 24},
        "c": {"openmvs_threads": 24}, "texture": {"max_threads": 24},
        "camera_frontend": {"deferred_registration": {
            "enabled": True, "relative_observation_floor": 0.2, "minimum_triangulated_ratio": 0.1,
            "max_images": 10, "retry_abs_pose_max_error": 12.0,
            "retry_abs_pose_min_num_inliers": 15, "retry_abs_pose_min_inlier_ratio": 0.2,
        }},
    }
    config = pipeline / "configs/pipeline.json"
    write_json(config, baseline)
    paths = pipeline / "configs/paths.fixture.json"
    write_json(paths, {"map_python": sys.executable})
    # Bind the trust metadata to fixture bytes, keeping the identity checks live.
    baseline_sha = hashlib.sha256(config.read_bytes()).hexdigest()
    monkeypatch.setattr(high_profile, "BASE_CONFIG_SHA", baseline_sha)
    monkeypatch.setattr(high_profile, "COLMAP_SCRIPT_SHA", hashlib.sha256(colmap.read_bytes()).hexdigest())
    monkeypatch.setattr(stage_module, "PIPELINE_SCRIPT_SHA", hashlib.sha256(planner.read_bytes()).hexdigest())
    monkeypatch.setattr(stage_module, "COLMAP_SCRIPT_SHA", high_profile.COLMAP_SCRIPT_SHA)
    profile = {
        "profile_kind": "offline_high_cpu_candidate", "autorun": False,
        "full_guarded_launcher_ready": False, "server_applied": False,
        "pipeline": {"commit": high_profile.BASE_COMMIT, "baseline_config_sha256": baseline_sha,
                     "baseline_modified": False, "quality_parameters_changed": False},
        "target_hardware": {"logical_cpus": 40},
        "cpu": {"sfm_threads": 12, "mapper_ba_threads": 32, "openmvs_threads": 32,
                "texture_threads": 32, "global_blas_omp_override": None},
        "gpu_preserved": {"task_concurrency": 1, "branches_serial": True, "map_outer_batch": 4,
                          "map_dense_head_minibatch": 1, "map_use_amp": True, "map_requested_amp_dtype": "bf16",
                          "mvs_source_views": 7, "mvs_height": 384, "mvs_width": 384, "mvs_refinement_steps": 1},
        "protection": {"disk_floor_bytes": 20 * 1024**3, "reservation_bytes": 64 * 1024**3,
                       "available_ram_floor_bytes": 32 * 1024**3, "free_vram_floor_bytes": 2 * 1024**3,
                       "host_guard_seconds": 1, "gpu_sample_seconds": 5, "directory_sample_seconds": 15,
                       "overall_timeout_seconds": 14400},
        "target_experiment": {"scene": "synthetic-test", "root": str(tmp_path / "_experiments/synthetic-test")},
    }
    profile_path = tmp_path / "profile.json"
    write_json(profile_path, profile)
    return SimpleNamespace(root=pipeline, profile=profile, profile_path=profile_path, baseline=baseline, paths=paths)


def test_frozen_plan_has_real_final_validation_and_no_creation(tmp_path, monkeypatch, pipeline_fixture):
    fixture = pipeline_fixture
    profile = deepcopy(fixture.profile)
    root = tmp_path / "_experiments/test-complete"
    profile["target_experiment"]["root"] = str(root)
    overlay, _ = build_pipeline_overlay(fixture.baseline, profile)
    config = tmp_path / "overlay.json"
    write_json(config, overlay)
    for key in ("RE3D_MAP_PYTHON", "RE3D_MVS_PYTHON", "RE3D_TEXTUREMESH_EXE",
                "RE3D_WORK_DIR", "RE3D_OUTPUT_DIR", "RE3D_LOG_DIR"):
        monkeypatch.delenv(key, raising=False)
    plan = build_stage_plan(pipeline_root=fixture.root, config_path=config,
        paths_path=fixture.paths, profile_path=fixture.profile_path,
        profile=profile, dimensions=(2048,1080), experiment_root=root, wrapper_path=WRAPPER_PATH)
    assert len(plan.stages) == 31
    assert tuple(stage.name for stage in plan.stages) == STAGE_NAMES
    assert STAGE_NAMES[10] == "22-a-crossview-v4"
    assert STAGE_NAMES[21] == "32-b-crossview-v2"
    assert plan.stages[0].name == "01-colmap"
    assert "run_profiled_colmap.py" in plan.stages[0].argv[1]
    argv = plan.stages[0].argv
    assert argv[argv.index("--num-threads")+1] == "12"
    assert argv[argv.index("--max-num-features")+1] == "6000"
    assert "--execute-stage" in argv
    assert plan.stages[-1].name == "90-validate"
    assert len([s for s in plan.stages if "--max-threads" in s.argv]) == 9
    assert all(s.argv[s.argv.index("--max-threads")+1] == "32" for s in plan.stages if "--max-threads" in s.argv)
    assert all(s.marker.is_relative_to(root) for s in plan.stages)
    assert any(s.required_directories for s in plan.stages)
    assert not root.exists()


def test_planner_rejects_environment_override_before_import(tmp_path, monkeypatch, pipeline_fixture):
    monkeypatch.setenv("RE3D_MAP_PYTHON", "not-approved")
    with pytest.raises(ExperimentGateError, match="ENVIRONMENT_OVERRIDE"):
        build_stage_plan(pipeline_root=pipeline_fixture.root, config_path=tmp_path / "missing",
            paths_path=tmp_path / "missing", profile_path=pipeline_fixture.profile_path,
            profile=pipeline_fixture.profile, dimensions=(1,1),
            experiment_root=tmp_path / "_experiments/new", wrapper_path=WRAPPER_PATH)


def test_prepared_dimensions_uses_verified_names(tmp_path):
    names = tuple(f"{i:06}.jpg" for i in range(300))
    path = tmp_path / "input-manifest.json"
    value = {"count":300,"uniform_size":[2048,1080],"images":[
        {"name":n,"width":2048,"height":1080} for n in names]}
    write_json(path,value)
    assert prepared_dimensions(path,tmp_path,names) == (2048,1080)
    value["images"][0]["name"] = "changed.jpg"
    write_json(path,value)
    with pytest.raises(ExperimentGateError, match="IDENTITY_MISMATCH"):
        prepared_dimensions(path,tmp_path,names)


def sfm_fixture(root):
    folder = root / "work/shared/colmap"
    binary, text = folder / "mapper/0", folder / "refined_text"
    binary.mkdir(parents=True)
    text.mkdir(parents=True)
    names = tuple(f"{i:06}.jpg" for i in range(300))
    with sqlite3.connect(folder / "database.db") as connection:
        connection.executescript("CREATE TABLE images(image_id INTEGER,name TEXT,camera_id INTEGER);"
            "CREATE TABLE keypoints(image_id INTEGER);CREATE TABLE descriptors(image_id INTEGER);"
            "CREATE TABLE two_view_geometries(rows INTEGER);")
        connection.executemany("INSERT INTO images VALUES(?,?,1)",[(i+1,n) for i,n in enumerate(names)])
        connection.executemany("INSERT INTO keypoints VALUES(?)",[(i+1,) for i in range(300)])
        connection.executemany("INSERT INTO descriptors VALUES(?)",[(i+1,) for i in range(300)])
        connection.execute("INSERT INTO two_view_geometries VALUES(30)")
    cameras = struct.pack("<Q",1) + struct.pack("<iiQQ4d",1,1,2048,1080,1000,1000,1024,540)
    (binary / "cameras.bin").write_bytes(cameras)
    (text / "cameras.txt").write_text("1 PINHOLE 2048 1080 1000 1000 1024 540\n")
    images = struct.pack("<Q",3)
    text_images = "# IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME\n"
    for i in range(3):
        images += struct.pack("<i7di",i+1,1,0,0,0,i,0,0,1) + names[i].encode() + b"\0"
        images += struct.pack("<Q",1)+struct.pack("<ddq",0,0,1)
        text_images += f"{i+1} 1 0 0 0 {i} 0 0 1 {names[i]}\n0 0 1\n"
    (binary / "images.bin").write_bytes(images)
    (text / "images.txt").write_text(text_images)
    point = struct.pack("<Q",1)+struct.pack("<Q3d3BdQ",1,0,0,3,128,128,128,.2,3)
    point += struct.pack("<6i",1,0,2,0,3,0)
    (binary / "points3D.bin").write_bytes(point)
    (text / "points3D.txt").write_text("1 0 0 3 128 128 128 0.2 1 0 2 0 3 0\n")
    metrics={"database_images":300,"registered_images":3,"cameras":1,"points3D":1,
        "observations":3,"mean_reprojection_error_px":.2,"verified_pairs_min_15":1,
        "binary_model":str(binary),"text_model":str(text)}
    write_json(folder / "reconstruction_metrics.json",metrics)
    options={"status":"succeeded","sfm_stage_complete":True,
        "runtime_versions":{"python":"3.12.14","pycolmap":"3.13.0","pycolmap_has_cuda":False},
        "actual_extraction":{"num_threads":12,"max_image_size":1600,"max_num_features":6000,"device":"cpu"},
        "actual_matching":{"num_threads":12,"guided_matching":True,"device":"cpu"},
        "actual_mapper_options":[{"phase":"initial","num_threads":32,"mapper_num_threads":32}]}
    write_json(root / "reports/sfm-actual-options.json",options)
    return names, metrics, options


def test_sfm_requires_database_binary_text_and_actual_options(tmp_path):
    names,_,_=sfm_fixture(tmp_path)
    calls=[]
    report=gate_sfm(tmp_path,names,lambda:calls.append(1))
    assert report["registered_images"] == 3 and not report["geometry_accuracy_certified"]
    assert len(calls)>6


@pytest.mark.parametrize("mutation,code",[
    ("metrics","METRICS_MODEL_MISMATCH"),("pose","TEXT_BINARY_MISMATCH"),
    ("binary","BINARY_TRUNCATED"),("mapper","ACTUAL_MAPPER_MISMATCH"),
    ("actual_missing","ACTUAL_EXECUTION_EVIDENCE_MISSING"),("database","DATABASE_IDENTITY_MISMATCH"),
    ("text_point","TEXT_BINARY_POINT_MISMATCH"),
])
def test_sfm_rejects_fabricated_or_partial_completion(tmp_path,mutation,code):
    names,metrics,options=sfm_fixture(tmp_path)
    folder=tmp_path / "work/shared/colmap"
    if mutation=="metrics":
        metrics["points3D"]=2;write_json(folder / "reconstruction_metrics.json",metrics)
    elif mutation=="pose":
        path=folder / "refined_text/images.txt";path.write_text(path.read_text().replace(names[0],"unknown.jpg"))
    elif mutation=="binary":
        path=folder / "mapper/0/points3D.bin";path.write_bytes(path.read_bytes()[:-2])
    elif mutation=="mapper":
        options["actual_mapper_options"][0]["mapper_num_threads"]=4
        write_json(tmp_path / "reports/sfm-actual-options.json",options)
    elif mutation=="actual_missing":
        options["status"]="running";write_json(tmp_path / "reports/sfm-actual-options.json",options)
    elif mutation=="text_point":
        path=folder / "refined_text/points3D.txt";path.write_text(path.read_text().replace("0 0 3","0 0 4"))
    else:
        with sqlite3.connect(folder / "database.db") as c:c.execute("UPDATE images SET name='unknown.jpg' WHERE image_id=1")
    with pytest.raises(ExperimentGateError,match=code):gate_sfm(tmp_path,names)


def output_fixture(root):
    reports={}
    for label in LABELS:
        folder=root / "output" / label;folder.mkdir(parents=True)
        (folder / "mesh.obj").write_text("mtllib mesh.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        (folder / "mesh.mtl").write_text("newmtl material\nd 1.000000\nmap_Kd texture.jpg\n")
        (folder / "texture.jpg").write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9")
        gltf={"asset":{"version":"2.0"},"accessors":[{"count":3}],"meshes":[{"primitives":[{"indices":0}]}]}
        payload=json.dumps(gltf).encode();payload+=b" "*((-len(payload))%4)
        binary=b"\0"*12
        size=12+8+len(payload)+8+len(binary)
        (folder / "mesh.glb").write_bytes(struct.pack("<4sII",b"glTF",2,size)+struct.pack("<I4s",len(payload),b"JSON")+
            payload+struct.pack("<I4s",len(binary),b"BIN\0")+binary)
        reports[label]={"status":"pass","errors":[],"obj_vertices":3,"obj_faces":1,"glb_vertices":3,"glb_faces":1}
    write_json(root / "output/validation.json",{"all_passed":True,"branches":reports})


def test_outputs_twelve_hashed_files_and_structural_scope(tmp_path):
    output_fixture(tmp_path)
    result=gate_outputs(tmp_path)
    assert result["artifact_count"] == 12 and not result["geometry_accuracy_certified"]
    assert all(len(a["sha256"])==64 for a in result["artifacts"])


@pytest.mark.parametrize("mutation,code",[("header","GLB_HEADER_INVALID"),("faces","FACE_COUNT_MISMATCH"),
    ("validation","VALIDATION_REJECTED"),("jpeg","JPEG_HEADER_INVALID")])
def test_output_gate_rejects_bad_outputs(tmp_path,mutation,code):
    output_fixture(tmp_path)
    if mutation=="header":
        path=tmp_path / "output/C/mesh.glb";path.write_bytes(b"wrong"+path.read_bytes()[5:])
    elif mutation=="jpeg":(tmp_path / "output/C/texture.jpg").write_bytes(b"not-a-jpeg")
    else:
        path=tmp_path / "output/validation.json";value=json.loads(path.read_bytes())
        if mutation=="faces":value["branches"]["C"]["glb_faces"]=3
        else:value["all_passed"]=False
        write_json(path,value)
    with pytest.raises(ExperimentGateError,match=code):gate_outputs(tmp_path)


def test_simple_marker_and_handoff_gate(tmp_path):
    marker=tmp_path / "work/result.mvs";marker.parent.mkdir();marker.write_bytes(b"mvs")
    stage=StageSpec("10-c-interface",("exe",),marker,tmp_path)
    assert gate_stage(stage,tmp_path,())["gate_passed"]
    marker.write_bytes(b"")
    with pytest.raises(ExperimentGateError,match="NONEMPTY"):gate_stage(stage,tmp_path,())
    marker=tmp_path / "work/validation.json";write_json(marker,{"all_passed":False,"errors":[]})
    stage=StageSpec("23a-a-validate-dmap",("exe",),marker,tmp_path)
    with pytest.raises(ExperimentGateError,match="HANDOFF_REJECTED"):gate_stage(stage,tmp_path,())


def test_real_prediction_manifest_is_a_list_and_checks_payloads(tmp_path):
    names=tuple(f"{i:06}.jpg" for i in range(300))
    marker=tmp_path / "work/raw/prediction_manifest.json"
    write_json(marker,[{"name":name,"valid_depth_ratio":.5} for name in names[:3]])
    folder=marker.parent / "depth";folder.mkdir()
    for name in names[:3]:
        for suffix in ("_depth.npy","_mask.npy"):(folder / (Path(name).stem+suffix)).write_bytes(b"npy")
    stage=StageSpec("20-a-mapanything",("exe",),marker,tmp_path)
    assert gate_stage(stage,tmp_path,names)["predicted_images"]==3
    write_json(marker,[{"name":"foreign.jpg","valid_depth_ratio":.5}]*3)
    with pytest.raises(ExperimentGateError,match="IDENTITY_MISMATCH"):gate_stage(stage,tmp_path,names)


def test_c_dense_gate_demands_templates(tmp_path):
    marker=tmp_path / "work/scene_dense.mvs";marker.parent.mkdir();marker.write_bytes(b"mvs")
    stage=StageSpec("11-c-densify",("exe",),marker,tmp_path)
    with pytest.raises(ExperimentGateError,match="TEMPLATE_DMAPS_MISSING"):gate_stage(stage,tmp_path,())
    (marker.parent / "depth0000.dmap").write_bytes(b"dmap")
    assert gate_stage(stage,tmp_path,())["gate_passed"]


def test_gate_rejects_reparse_flag_without_needing_symlink_privilege(tmp_path,monkeypatch):
    marker=tmp_path / "artifact.mvs";marker.write_bytes(b"mvs")
    original=Path.lstat
    def lstat(path,*args,**kwargs):
        value=original(path,*args,**kwargs)
        if path==marker:
            return SimpleNamespace(st_mode=value.st_mode,st_file_attributes=1024)
        return value
    monkeypatch.setattr(Path,"lstat",lstat)
    with pytest.raises(ExperimentGateError,match="REPARSE"):
        gate_stage(StageSpec("10-c-interface",("exe",),marker,tmp_path),tmp_path,())


def load_wrapper():
    spec=importlib.util.spec_from_file_location("test_profiled_colmap_wrapper",WRAPPER_PATH)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_actual_child_options_hook_covers_initial_retry_and_restores():
    wrapper=load_wrapper();calls=[]
    def make_options(**kwargs):
        return SimpleNamespace(num_threads=4,mapper=SimpleNamespace(num_threads=4),check=lambda:True)
    extraction=SimpleNamespace(num_threads=12,max_image_size=1600,sift=SimpleNamespace(max_num_features=6000))
    matching=SimpleNamespace(num_threads=12,guided_matching=True)
    pycolmap=SimpleNamespace(Device=SimpleNamespace(cpu="cpu"),extract_features=lambda **kw:calls.append("extract"),
                            match_exhaustive=lambda **kw:calls.append("match"))
    module=SimpleNamespace(pycolmap=pycolmap,mapping_options=make_options)
    extract,match=pycolmap.extract_features,pycolmap.match_exhaustive
    report={"actual_mapper_options":[]};saved=[]
    with wrapper.actual_option_recording(module,12,32,report,lambda:saved.append(deepcopy(report))):
        pycolmap.extract_features(extraction_options=extraction,device="cpu")
        pycolmap.match_exhaustive(matching_options=matching,device="cpu")
        assert module.mapping_options(random_seed=0).num_threads==32
        assert module.mapping_options(random_seed=0).mapper.num_threads==32
    assert module.mapping_options is make_options
    assert pycolmap.extract_features is extract and pycolmap.match_exhaustive is match
    assert [row["phase"] for row in report["actual_mapper_options"]]==["initial","deferred_retry"]
    assert len(saved)==4 and calls==["extract","match"]


def test_options_hooks_fail_closed_and_restore():
    wrapper=load_wrapper()
    extraction=SimpleNamespace(num_threads=4,max_image_size=1600,sift=SimpleNamespace(max_num_features=6000))
    extract=lambda **kw:pytest.fail("compute must not run")
    colmap=SimpleNamespace(Device=SimpleNamespace(cpu="cpu"),extract_features=extract,match_exhaustive=lambda **kw:None)
    factory=lambda **kw:SimpleNamespace(num_threads=4,mapper=SimpleNamespace(num_threads=4),check=lambda:True)
    module=SimpleNamespace(pycolmap=colmap,mapping_options=factory)
    with pytest.raises(ValueError,match="EXTRACTION_NOT_APPLIED"):
        with wrapper.actual_option_recording(module,12,32,{"actual_mapper_options":[]},lambda:None):
            colmap.extract_features(extraction_options=extraction,device="cpu")
    assert colmap.extract_features is extract and module.mapping_options is factory


def test_child_without_execute_only_prepares_and_never_imports_native(tmp_path,monkeypatch,capsys,pipeline_fixture):
    wrapper=load_wrapper();root=tmp_path / "_experiments/child-preview";reports=root / "reports"
    reports.mkdir(parents=True)
    profile=deepcopy(pipeline_fixture.profile);profile_path=tmp_path / "child-profile.json"
    write_json(profile_path,profile)
    baseline=pipeline_fixture.baseline
    deferred=baseline["camera_frontend"]["deferred_registration"]
    args=["--platform-root",str(WRAPPER_PATH.parents[1]),"--pipeline-root",str(pipeline_fixture.root),
        "--profile",str(profile_path),"--profile-sha256",hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        "--experiment-root",str(root),"--options-report",str(reports / "sfm-actual-options.json"),"--",
        "--images",str(root / "work/input/images"),"--masks",str(root / "work/input/masks"),
        "--output",str(root / "work/shared/colmap"),"--num-threads","12","--max-num-features","6000",
        "--camera-model","PINHOLE","--random-seed","0","--deferred-registration"]
    names={"--defer-relative-observation-floor":"relative_observation_floor",
           "--defer-min-triangulated-ratio":"minimum_triangulated_ratio","--defer-max-images":"max_images",
           "--retry-abs-pose-max-error":"retry_abs_pose_max_error",
           "--retry-abs-pose-min-num-inliers":"retry_abs_pose_min_num_inliers",
           "--retry-abs-pose-min-inlier-ratio":"retry_abs_pose_min_inlier_ratio"}
    for flag,key in names.items():args.extend([flag,str(deferred[key])])
    monkeypatch.setattr(wrapper.importlib.util,"spec_from_file_location",lambda *a,**kw:pytest.fail("native import forbidden"))
    assert wrapper.main(args)==0
    saved=json.loads((reports / "sfm-actual-options.json").read_bytes())
    assert saved["status"]=="prepared_not_executed" and not saved["sfm_stage_complete"]
    assert "reconstruction_started\": false" in capsys.readouterr().out


def test_child_invalid_hash_is_sanitized_and_does_not_start(tmp_path,capsys,pipeline_fixture):
    wrapper=load_wrapper();root=tmp_path / "_experiments/child-no-start";(root / "reports").mkdir(parents=True)
    rc=wrapper.main(["--platform-root",str(WRAPPER_PATH.parents[1]),"--pipeline-root",str(pipeline_fixture.root),
        "--profile",str(pipeline_fixture.profile_path),"--profile-sha256","0"*64,"--experiment-root",str(root),
        "--options-report",str(root / "reports/sfm-actual-options.json")])
    value=json.loads(capsys.readouterr().out)
    assert rc==1 and value["error_code"]=="PROFILED_COLMAP_PROFILE_HASH_MISMATCH"
    assert not (root / "reports/sfm-actual-options.json").exists()
