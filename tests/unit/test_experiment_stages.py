from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
from types import SimpleNamespace

import pytest

from backend.experiments.high_profile import build_pipeline_overlay
from backend.experiments.long_runner import STAGE_NAMES
from backend.experiments.preflight import ExperimentGateError
from backend.experiments.stages import (
    LABELS, StageSpec, build_stage_plan, gate_outputs, gate_sfm, gate_stage, prepared_dimensions,
)

WORKSPACE = Path(__file__).resolve().parents[3]
PIPELINE = WORKSPACE / "Re3D"
PROFILE_PATH = WORKSPACE / "Re3D-data/experiments/lighthouse300-high-cpu-v1/performance-profile.json"
WRAPPER_PATH = Path(__file__).resolve().parents[2] / "scripts/run_profiled_colmap.py"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_frozen_plan_has_real_final_validation_and_no_creation(tmp_path, monkeypatch):
    profile = json.loads(PROFILE_PATH.read_bytes())
    root = tmp_path / "_experiments/test-complete"
    profile["target_experiment"]["root"] = str(root)
    baseline = json.loads((PIPELINE / "configs/pipeline.json").read_bytes())
    overlay, _ = build_pipeline_overlay(baseline, profile)
    config = tmp_path / "overlay.json"
    write_json(config, overlay)
    for key in ("RE3D_MAP_PYTHON", "RE3D_MVS_PYTHON", "RE3D_TEXTUREMESH_EXE",
                "RE3D_WORK_DIR", "RE3D_OUTPUT_DIR", "RE3D_LOG_DIR"):
        monkeypatch.delenv(key, raising=False)
    plan = build_stage_plan(pipeline_root=PIPELINE, config_path=config,
        paths_path=PIPELINE / "configs/paths.local.json", profile_path=PROFILE_PATH,
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


def test_planner_rejects_environment_override_before_import(tmp_path, monkeypatch):
    monkeypatch.setenv("RE3D_MAP_PYTHON", "not-approved")
    with pytest.raises(ExperimentGateError, match="ENVIRONMENT_OVERRIDE"):
        build_stage_plan(pipeline_root=PIPELINE, config_path=tmp_path / "missing",
            paths_path=tmp_path / "missing", profile_path=PROFILE_PATH,
            profile=json.loads(PROFILE_PATH.read_bytes()), dimensions=(1,1),
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


def test_child_without_execute_only_prepares_and_never_imports_native(tmp_path,monkeypatch,capsys):
    wrapper=load_wrapper();root=tmp_path / "_experiments/child-preview";reports=root / "reports"
    reports.mkdir(parents=True)
    profile=json.loads(PROFILE_PATH.read_bytes());profile_path=tmp_path / "profile.json"
    write_json(profile_path,profile)
    baseline=json.loads((PIPELINE / "configs/pipeline.json").read_bytes())
    deferred=baseline["camera_frontend"]["deferred_registration"]
    args=["--platform-root",str(WRAPPER_PATH.parents[1]),"--pipeline-root",str(PIPELINE),
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


def test_child_invalid_hash_is_sanitized_and_does_not_start(tmp_path,capsys):
    wrapper=load_wrapper();root=tmp_path / "_experiments/child-no-start";(root / "reports").mkdir(parents=True)
    rc=wrapper.main(["--platform-root",str(WRAPPER_PATH.parents[1]),"--pipeline-root",str(PIPELINE),
        "--profile",str(PROFILE_PATH),"--profile-sha256","0"*64,"--experiment-root",str(root),
        "--options-report",str(root / "reports/sfm-actual-options.json")])
    value=json.loads(capsys.readouterr().out)
    assert rc==1 and value["error_code"]=="PROFILED_COLMAP_PROFILE_HASH_MISMATCH"
    assert not (root / "reports/sfm-actual-options.json").exists()
