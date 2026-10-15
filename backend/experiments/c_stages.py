"""Pure local C-only planning after a separately verified SfM import.

No marker is fabricated, reused, or used to decide whether a stage is planned.
The caller owns source verification, exclusive work directories, containment,
timeouts and resource guards. This module never starts a process.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
from types import ModuleType

from backend.experiments.high_profile import (
    BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA, PIPELINE_SCRIPT_SHA, config_diff, require,
)
from backend.experiments.stages import (
    PipelineStagePlan, StageSpec, _glb, _hash, _json, checked_path, gate_stage,
)

C_STAGE_NAMES = (
    "02-extract-cameras", "03-prepare-openmvs-input", "10-c-interface",
    "11-c-densify", "12-c-reconstruct", "13-c-texture", "13-c-normalize", "90-validate",
)
_OVERRIDES = ("RE3D_MAP_PYTHON", "RE3D_MVS_PYTHON", "RE3D_TEXTUREMESH_EXE",
              "RE3D_WORK_DIR", "RE3D_OUTPUT_DIR", "RE3D_LOG_DIR")
_MARKERS = (
    "work/shared/cameras_refined.npz", "work/shared/openmvs_input/manifest.json",
    "work/branches/c_openmvs/scene.mvs", "work/branches/c_openmvs/scene_dense.mvs",
    "work/branches/c_openmvs/scene_mesh.ply", "work/branches/c_openmvs/scene_textured_final.obj",
    "output/C/mesh.glb", "output/validation.json",
)


def build_c_pipeline_overlay(baseline: dict, *, openmvs_threads=8, texture_threads=8):
    """Only the two local parallelism budgets differ from the frozen baseline."""
    require(type(openmvs_threads) is int and openmvs_threads == 8
            and type(texture_threads) is int and texture_threads == 8,
            "C_STAGE_THREAD_BUDGET_INVALID")
    overlay = deepcopy(baseline)
    overlay["c"]["openmvs_threads"] = openmvs_threads
    overlay["texture"]["max_threads"] = texture_threads
    differences = config_diff(baseline, overlay)
    require({row["path"] for row in differences} <= {"/c/openmvs_threads", "/texture/max_threads"},
            "C_STAGE_QUALITY_CONFIGURATION_CHANGED")
    return overlay, differences


def _expected_commands(pipeline_root, root, config, map_python, texturemesh):
    scripts, work, output = pipeline_root / "scripts", root / "work", root / "output"
    colmap, cameras = work / "shared/colmap", work / "shared/cameras_refined.npz"
    source, folder = work / "shared/openmvs_input", work / "branches/c_openmvs"
    vendor = pipeline_root / "vendor/openmvs-2.4.0-windows/vc17/x64/Release"
    dense, c, mesh, texture = (config[key] for key in ("openmvs_dense_resolution", "c", "mesh", "texture"))
    commands = [
        [map_python, scripts / "extract_colmap_cameras.py", "--model", colmap / "refined_text", "--output", cameras],
        [map_python, scripts / "prepare_openmvs_texture_input.py", "--images", work / "input/images",
         "--sparse", colmap / "refined_text", "--output", source, "--alpha-threshold", config["input"]["alpha_threshold"]],
        [vendor / "InterfaceCOLMAP.exe", "-w", folder, "-i", source, "-o", "scene.mvs", "--binary", 1],
        [vendor / "DensifyPointCloud.exe", "-w", folder, "-i", "scene.mvs", "-o", "scene_dense.mvs",
         "--resolution-level", dense["resolution_level"], "--min-resolution", dense["min_resolution"],
         "--max-resolution", dense["max_resolution"], "--number-views", c["number_views"],
         "--mask-path", source / "images", "--ignore-mask-label", c["ignore_mask_label"], "--max-threads", c["openmvs_threads"]],
        [vendor / "ReconstructMesh.exe", "-w", folder, "-i", "scene_dense.mvs", "-o", "scene_mesh.mvs",
         "--remove-spurious", mesh["remove_spurious"], "--close-holes", mesh["close_holes"],
         "--smooth", mesh["smooth"], "--max-threads", c["openmvs_threads"]],
        [texturemesh, "-w", folder, "-i", "scene.mvs", "--mesh-file", "scene_mesh.ply", "-o", "scene_textured_final.mvs",
         "--export-type", "obj", "--max-texture-size", texture["max_texture_size"],
         "--global-seam-leveling", texture["global_seam_leveling"], "--local-seam-leveling", texture["local_seam_leveling"],
         "--sharpness-weight", texture["sharpness_weight"], "--outlier-threshold", texture["outlier_threshold"],
         "--ignore-mask-label", texture["ignore_mask_label"], "--empty-color", texture["empty_color"], "--max-threads", texture["max_threads"]],
        [map_python, scripts / "normalize_openmvs_output.py", "--obj", folder / "scene_textured_final.obj",
         "--mtl", folder / "scene_textured_final.mtl", "--texture", folder / "scene_textured_final_material_00_map_Kd.jpg",
         "--output", output / "C"],
        [map_python, scripts / "validate_outputs.py", "--outputs", output, "--branches", "C", "--report", output / "validation.json"],
    ]
    return {name: tuple(str(value) for value in command) for name, command in zip(C_STAGE_NAMES, commands)}


def _skipped_sfm_command(pipeline_root, root, config, map_python):
    camera = config["camera_frontend"]
    deferred = camera["deferred_registration"]
    command = [map_python, pipeline_root / "scripts/run_colmap_shared.py", "--images", root / "work/input/images",
               "--masks", root / "work/input/masks", "--output", root / "work/shared/colmap",
               "--camera-model", camera["camera_model"], "--random-seed", camera["random_seed"]]
    if deferred["enabled"]:
        command.append("--deferred-registration")
        for flag, key in (("--defer-relative-observation-floor", "relative_observation_floor"),
                          ("--defer-min-triangulated-ratio", "minimum_triangulated_ratio"),
                          ("--defer-max-images", "max_images"), ("--retry-abs-pose-max-error", "retry_abs_pose_max_error"),
                          ("--retry-abs-pose-min-num-inliers", "retry_abs_pose_min_num_inliers"),
                          ("--retry-abs-pose-min-inlier-ratio", "retry_abs_pose_min_inlier_ratio")):
            command.extend((flag, deferred[key]))
    return tuple(str(value) for value in command)


def build_c_stage_plan(*, pipeline_root: Path, config_path: Path, paths_path: Path,
                       experiment_root: Path, dimensions: tuple[int, int], map_python: Path | None = None,
                       openmvs_threads=8, texture_threads=8) -> PipelineStagePlan:
    """Record the pinned Pipeline's C branch without any input or marker writes.

    The path file needs only mapanything_python. A never-executed B interpreter
    placeholder is injected into this private planning module, not into files.
    The baseline config remains hash-pinned; thread changes exist only in memory.
    An existing target can contain a verified SfM import; this does not validate it.
    """
    require(experiment_root.is_absolute() and experiment_root.parent.name == "_experiments"
            and ".." not in experiment_root.parts and re.fullmatch(r"[A-Za-z0-9._-]+", experiment_root.name)
            and not any(part.casefold() == "jobs" for part in experiment_root.parts), "C_STAGE_TARGET_INVALID")
    require(pipeline_root.is_absolute() and ".." not in pipeline_root.parts,
            "C_STAGE_PIPELINE_PATH_INVALID")
    require(all(not os.environ.get(key) for key in _OVERRIDES), "C_STAGE_ENVIRONMENT_OVERRIDE")
    require(len(dimensions) == 2 and all(type(value) is int and value > 0 for value in dimensions),
            "C_STAGE_DIMENSIONS_INVALID")
    script = checked_path(pipeline_root / "scripts/run_pipeline.py", pipeline_root)
    colmap_script = checked_path(pipeline_root / "scripts/run_colmap_shared.py", pipeline_root)
    source = script.read_bytes()
    require(hashlib.sha256(source).hexdigest() == PIPELINE_SCRIPT_SHA
            and _hash(colmap_script, lambda: None) == COLMAP_SCRIPT_SHA, "C_STAGE_FROZEN_SCRIPT_CHANGED")
    baseline = _json(config_path, config_path.parent)
    require(_hash(config_path, lambda: None) == BASE_CONFIG_SHA, "C_STAGE_FROZEN_CONFIG_CHANGED")
    overlay, _ = build_c_pipeline_overlay(baseline, openmvs_threads=openmvs_threads, texture_threads=texture_threads)
    paths = _json(paths_path, paths_path.parent)
    require(isinstance(paths.get("mapanything_python"), str) and paths["mapanything_python"], "C_STAGE_MAP_PYTHON_REQUIRED")
    configured_python = Path(paths["mapanything_python"])
    configured_python = configured_python if configured_python.is_absolute() else pipeline_root / configured_python
    require(".." not in configured_python.parts and (map_python is None or Path(map_python) == configured_python),
            "C_STAGE_MAP_PYTHON_CHANGED")
    fixed_texture = pipeline_root / "vendor/openmvs-2.4.0-33d9484-windows/vc18/x64/Release/TextureMesh.exe"
    configured_texture = Path(paths.get("texturemesh_executable", str(fixed_texture)))
    configured_texture = configured_texture if configured_texture.is_absolute() else pipeline_root / configured_texture
    require(configured_texture == fixed_texture, "C_STAGE_TEXTURE_BINARY_CHANGED")
    local_paths = {**paths, "mvsanywhere_python": str(configured_python)}
    module = ModuleType("re3d_frozen_local_c_recorder")
    module.__file__ = str(script)
    # Execute exactly the previously hashed bytes, ignoring external __pycache__.
    exec(compile(source, str(script), "exec"), module.__dict__)
    def in_memory_json(path):
        if Path(path) == config_path:
            return deepcopy(overlay)
        require(Path(path) == paths_path, "C_STAGE_UNEXPECTED_PLANNER_READ")
        return deepcopy(local_paths)
    module.read_json = in_memory_json
    expected = _expected_commands(pipeline_root, experiment_root, overlay, configured_python, fixed_texture)
    skipped = _skipped_sfm_command(pipeline_root, experiment_root, baseline, configured_python)
    stages = []
    skipped_count = 0
    class Recorder(module.Pipeline):
        def prepare(self):
            return dimensions
        def step(self, name, command, marker=None):
            nonlocal skipped_count
            argv = tuple(str(value) for value in command)
            if name == "01-colmap":
                require(skipped_count == 0 and not stages and argv == skipped
                        and marker == experiment_root / "work/shared/colmap/reconstruction_metrics.json",
                        "C_STAGE_SFM_SKIP_CONTRACT_CHANGED")
                skipped_count += 1
                return
            index = len(stages)
            require(index < len(C_STAGE_NAMES) and name == C_STAGE_NAMES[index]
                    and argv == expected[name], "C_STAGE_FROZEN_ARGV_CHANGED")
            expected_marker = experiment_root / _MARKERS[index]
            require(marker is not None and Path(marker) == expected_marker, "C_STAGE_MARKER_CHANGED")
            directories = (Path(argv[argv.index("-w") + 1]),) if "-w" in argv else ()
            stages.append(StageSpec(name, argv, expected_marker, pipeline_root, directories))
        def validate(self):
            self.step("90-validate", expected["90-validate"], experiment_root / "output/validation.json")
    planner = Recorder(scene=experiment_root.name, images=None, branches={"c"}, config_path=config_path,
        paths_path=paths_path, dry_run=True, work_dir=experiment_root / "work",
        output_dir=experiment_root / "output", log_dir=experiment_root / "logs")
    planner.run()
    require(skipped_count == 1 and tuple(stage.name for stage in stages) == C_STAGE_NAMES,
            "C_STAGE_PLAN_INCOMPLETE")
    require(all(Path(stage.argv[0]).is_absolute() for stage in stages), "C_STAGE_EXECUTABLE_NOT_ABSOLUTE")
    environment = dict(planner.base_env)
    # CPU C helpers require no learned-model repositories/weights in PYTHONPATH.
    environment.pop("PYTHONPATH", None)
    environment["HF_HUB_OFFLINE"] = environment["TRANSFORMERS_OFFLINE"] = "1"
    return PipelineStagePlan(tuple(stages), environment, tuple(dimensions))


def gate_c_outputs(experiment_root: Path, health_check=lambda: None) -> dict:
    """Four hashed standard C outputs, not a ground-truth accuracy certificate."""
    validation = _json(experiment_root / "output/validation.json", experiment_root)
    require(validation.get("all_passed") is True and isinstance(validation.get("branches"), dict)
            and set(validation["branches"]) == {"C"}, "C_OUTPUT_VALIDATION_REJECTED")
    files = {name: checked_path(experiment_root / "output/C" / name, experiment_root)
             for name in ("mesh.obj", "mesh.mtl", "texture.jpg", "mesh.glb")}
    glb = _glb(files["mesh.glb"])
    vertices = faces = references = 0
    with files["mesh.obj"].open(encoding="utf-8", errors="strict") as stream:
        for index, raw in enumerate(stream):
            if index % 4096 == 0:
                health_check()
            vertices += raw.startswith("v ")
            faces += raw.startswith("f ")
            if raw.strip().startswith("mtllib "):
                require(raw.strip() == "mtllib mesh.mtl", "C_OUTPUT_MATERIAL_REFERENCE_INVALID")
                references += 1
    require(references == 1 and files["mesh.mtl"].stat().st_size <= 1024**2, "C_OUTPUT_MATERIAL_REFERENCE_INVALID")
    material = [line.strip() for line in files["mesh.mtl"].read_text(encoding="utf-8").splitlines()]
    texture_references = [line for line in material if line.startswith("map_") or line.startswith("bump ")]
    opacity = [line for line in material if line.startswith("d ")]
    require(texture_references == ["map_Kd texture.jpg"] and opacity == ["d 1.000000"], "C_OUTPUT_TEXTURE_REFERENCE_INVALID")
    require(files["texture.jpg"].stat().st_size >= 5, "C_OUTPUT_JPEG_INVALID")
    with files["texture.jpg"].open("rb") as stream:
        start = stream.read(3)
        stream.seek(-2, 2)
        end = stream.read(2)
    require(start == b"\xff\xd8\xff" and end == b"\xff\xd9", "C_OUTPUT_JPEG_INVALID")
    report = validation["branches"]["C"]
    require(isinstance(report, dict) and report.get("status") == "pass" and report.get("errors") == []
            and type(report.get("obj_faces")) is int and faces > 0
            and report.get("obj_faces") == report.get("glb_faces") == faces == glb["indexed_triangle_faces"]
            and report.get("obj_vertices") == vertices > 0, "C_OUTPUT_FACE_COUNT_MISMATCH")
    artifacts = []
    for name, path in files.items():
        health_check()
        artifacts.append({"relative_path": path.relative_to(experiment_root).as_posix(),
                          "size_bytes": path.stat().st_size, "sha256": _hash(path, health_check),
                          **({"glb_header": glb} if name == "mesh.glb" else {})})
    return {"all_passed": True, "artifact_count": 4, "artifacts": artifacts, "geometry_accuracy_certified": False}


def gate_c_stage(stage: StageSpec, experiment_root: Path, image_names: tuple[str, ...], health_check=lambda: None):
    require(stage.name in C_STAGE_NAMES and stage.marker == experiment_root / _MARKERS[C_STAGE_NAMES.index(stage.name)],
            "C_STAGE_GATE_SCOPE_INVALID")
    if stage.name == "90-validate":
        return gate_c_outputs(experiment_root, health_check)
    result = gate_stage(stage, experiment_root, image_names, health_check)
    if stage.name == "03-prepare-openmvs-input":
        document = _json(stage.marker, experiment_root)
        require(len(image_names) == 300 and len(set(image_names)) == 300
                and all(isinstance(name, str) and Path(name).name == name for name in image_names)
                and document.get("count") == 300
                and isinstance(document.get("images"), list)
                and all(isinstance(row, dict) for row in document["images"])
                and tuple(row.get("name") for row in document["images"]) == image_names,
                "C_STAGE_INPUT_IDENTITY_MISMATCH")
        folder = experiment_root / "work/shared/openmvs_input/images"
        for row, name in zip(document["images"], image_names):
            health_check()
            image, mask = folder / name, folder / (Path(name).stem + ".mask.png")
            require(Path(row.get("image", "")) == image and Path(row.get("mask", "")) == mask,
                    "C_STAGE_INPUT_REFERENCE_INVALID")
            checked_path(image, experiment_root)
            checked_path(mask, experiment_root)
    return result
