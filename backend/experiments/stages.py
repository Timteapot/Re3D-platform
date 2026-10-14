"""Pure frozen-pipeline planning and post-process artifact admission gates.

No process is started here.  Gates establish reconstruction/format consistency,
not a ground-truth geometry accuracy certificate.  The parent supervisor owns
timeouts, resource protection, checkpoints and process containment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import struct
from typing import Callable

from backend.experiments.high_profile import (
    COLMAP_SCRIPT_SHA, PIPELINE_SCRIPT_SHA, adapt_sfm_argv, file_sha,
    require, validate_profile, validate_stage_plan,
)
from backend.experiments.preflight import ExperimentGateError

LABELS = ("A-v4", "B-v2", "C")
_ENV_OVERRIDES = ("RE3D_MAP_PYTHON", "RE3D_MVS_PYTHON", "RE3D_TEXTUREMESH_EXE",
                  "RE3D_WORK_DIR", "RE3D_OUTPUT_DIR", "RE3D_LOG_DIR")


@dataclass(frozen=True)
class StageSpec:
    name: str
    argv: tuple[str, ...]
    marker: Path
    cwd: Path
    required_directories: tuple[Path, ...] = ()

    def observation(self) -> dict:
        return {"stage": self.name, "argv": list(self.argv),
                "expected_artifact": str(self.marker), "stage_executed": False,
                "existing_marker_reused": False}


@dataclass(frozen=True)
class PipelineStagePlan:
    stages: tuple[StageSpec, ...]
    environment: dict[str, str] = field(repr=False)
    dimensions: tuple[int, int]


def checked_path(path: Path, root: Path, *, regular_file: bool = True) -> Path:
    require(path.is_absolute() and root.is_absolute() and ".." not in path.parts,
            "EXPERIMENT_STAGE_PATH_INVALID")
    require(path.is_relative_to(root), "EXPERIMENT_STAGE_PATH_OUTSIDE_ROOT")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except OSError:
            raise ExperimentGateError("EXPERIMENT_STAGE_ARTIFACT_MISSING") from None
        require(not stat.S_ISLNK(info.st_mode) and not (
            getattr(info, "st_file_attributes", 0) & 1024), "EXPERIMENT_STAGE_REPARSE")
    if regular_file:
        require(path.is_file() and path.stat().st_size > 0,
                "EXPERIMENT_STAGE_NONEMPTY_FILE_REQUIRED")
    return path


def _json(path: Path, root: Path, limit: int = 16 * 1024**2, *, object_required=True):
    checked_path(path, root)
    require(path.stat().st_size <= limit, "EXPERIMENT_STAGE_REPORT_TOO_LARGE")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "EXPERIMENT_STAGE_JSON_DUPLICATE_KEY")
            result[key] = value
        return result
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise ExperimentGateError("EXPERIMENT_STAGE_JSON_INVALID") from None
    require(not object_required or isinstance(value, dict), "EXPERIMENT_STAGE_JSON_OBJECT_REQUIRED")
    return value


def prepared_dimensions(manifest_path: Path, root: Path,
                        expected_names: tuple[str, ...]) -> tuple[int, int]:
    document = _json(manifest_path, root)
    dimensions = document.get("uniform_size")
    rows = document.get("images", [])
    require(isinstance(dimensions, list) and len(dimensions) == 2
            and all(type(value) is int and value > 0 for value in dimensions),
            "EXPERIMENT_STAGE_DIMENSIONS_INVALID")
    require(document.get("count") == len(expected_names) == 300
            and isinstance(rows, list) and len(rows) == 300
            and tuple(row.get("name") for row in rows) == expected_names,
            "EXPERIMENT_STAGE_PREPARED_IDENTITY_MISMATCH")
    require(all(row.get("width") == dimensions[0] and row.get("height") == dimensions[1]
                for row in rows), "EXPERIMENT_STAGE_PREPARED_SIZE_MISMATCH")
    return tuple(dimensions)


def build_stage_plan(*, pipeline_root: Path, config_path: Path, paths_path: Path,
                     profile_path: Path, profile: dict, dimensions: tuple[int, int],
                     experiment_root: Path, wrapper_path: Path,
                     platform_root: Path | None = None) -> PipelineStagePlan:
    """Collect all 31 real stages without creating data or launching children."""
    validate_profile(profile)
    require(experiment_root.is_absolute() and experiment_root.parent.name == "_experiments"
            and ".." not in experiment_root.parts
            and not any(part.casefold() == "jobs" for part in experiment_root.parts),
            "EXPERIMENT_STAGE_TARGET_INVALID")
    require(all(not os.environ.get(key) for key in _ENV_OVERRIDES),
            "EXPERIMENT_STAGE_ENVIRONMENT_OVERRIDE")
    require(len(dimensions) == 2 and all(type(value) is int and value > 0 for value in dimensions),
            "EXPERIMENT_STAGE_DIMENSIONS_INVALID")
    script = pipeline_root / "scripts/run_pipeline.py"
    require(file_sha(script) == PIPELINE_SCRIPT_SHA
            and file_sha(pipeline_root / "scripts/run_colmap_shared.py") == COLMAP_SCRIPT_SHA,
            "EXPERIMENT_STAGE_FROZEN_SCRIPT_CHANGED")
    spec = importlib.util.spec_from_file_location("re3d_frozen_supervised_planner", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result: list[StageSpec] = []
    raw_stages: list[dict] = []
    class Recorder(module.Pipeline):
        def prepare(self):
            return dimensions
        def step(self, name, command, marker=None):
            require(marker is not None, "EXPERIMENT_STAGE_MARKER_REQUIRED")
            command = adapt_sfm_argv(command, profile) if name == "01-colmap" else [str(v) for v in command]
            raw_stages.append({"stage": name, "argv": command, "stage_executed": False})
            dirs = ()
            if "-w" in command:
                dirs = (Path(command[command.index("-w") + 1]),)
            if name == "01-colmap":
                command = [command[0], str(wrapper_path), "--platform-root",
                           str(platform_root or wrapper_path.parents[1]), "--pipeline-root", str(pipeline_root),
                           "--profile", str(profile_path), "--profile-sha256", file_sha(profile_path),
                           "--experiment-root", str(experiment_root), "--options-report",
                           str(experiment_root / "reports/sfm-actual-options.json"),
                           "--execute-stage", "--", *command[2:]]
            result.append(StageSpec(name, tuple(command), Path(marker), pipeline_root, dirs))
        def validate(self):
            self.step("90-validate", [self.map_python, self.script("validate_outputs.py"),
                "--outputs", self.outputs, "--branches", *LABELS, "--report",
                self.outputs / "validation.json"], self.outputs / "validation.json")
    planner = Recorder(scene=profile["target_experiment"]["scene"], images=None,
        branches={"a", "b", "c"}, config_path=config_path, paths_path=paths_path,
        dry_run=True, work_dir=experiment_root / "work", output_dir=experiment_root / "output",
        log_dir=experiment_root / "logs")
    planner.run()
    validate_stage_plan(raw_stages, profile)
    require(len(result) == 31 and result[0].name == "01-colmap"
            and result[-1].name == "90-validate", "EXPERIMENT_STAGE_PLAN_COUNT_CHANGED")
    for stage in result:
        require(stage.marker.is_relative_to(experiment_root), "EXPERIMENT_STAGE_MARKER_OUTSIDE_ROOT")
        require(Path(stage.argv[0]).is_absolute(), "EXPERIMENT_STAGE_EXECUTABLE_NOT_ABSOLUTE")
        for directory in stage.required_directories:
            require(directory.is_relative_to(experiment_root), "EXPERIMENT_STAGE_DIRECTORY_OUTSIDE_ROOT")
    return PipelineStagePlan(tuple(result), planner.base_env, dimensions)


def _hash(path: Path, health: Callable[[], None]) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            health()
            value.update(block)
    health()
    return value.hexdigest()


def _read(stream, length):
    block = stream.read(length)
    require(len(block) == length, "EXPERIMENT_SFM_BINARY_TRUNCATED")
    return block


def _binary_images(path: Path, health) -> dict[int, tuple[str, int]]:
    result = {}
    with path.open("rb") as stream:
        count, = struct.unpack("<Q", _read(stream, 8))
        require(0 < count <= 300, "EXPERIMENT_SFM_IMAGE_COUNT_INVALID")
        for _ in range(count):
            health()
            row = struct.unpack("<i7di", _read(stream, 64))
            require(row[0] > 0 and row[-1] > 0 and all(math.isfinite(v) for v in row[1:8])
                    and sum(v*v for v in row[1:5]) > 0, "EXPERIMENT_SFM_POSE_INVALID")
            name = bytearray()
            while True:
                character = _read(stream, 1)
                if character == b"\0":
                    break
                name += character
                require(len(name) <= 240, "EXPERIMENT_SFM_IMAGE_NAME_INVALID")
            try:
                decoded = name.decode("utf-8")
            except UnicodeError:
                raise ExperimentGateError("EXPERIMENT_SFM_IMAGE_NAME_INVALID") from None
            require(row[0] not in result and decoded not in {v[0] for v in result.values()},
                    "EXPERIMENT_SFM_IMAGE_DUPLICATE")
            result[row[0]] = (decoded, row[-1])
            points, = struct.unpack("<Q", _read(stream, 8))
            require(points <= 100000, "EXPERIMENT_SFM_FEATURE_COUNT_INVALID")
            require(stream.tell() + 24*points <= path.stat().st_size,
                    "EXPERIMENT_SFM_BINARY_TRUNCATED")
            stream.seek(24*points, 1)
        require(stream.read(1) == b"", "EXPERIMENT_SFM_BINARY_TRAILING_DATA")
    return result


def _binary_points(path: Path, health) -> tuple[int, int, float, str]:
    observations = 0
    error_sum = 0.0
    fingerprint = 0
    with path.open("rb") as stream:
        count, = struct.unpack("<Q", _read(stream, 8))
        require(0 < count <= 30_000_000, "EXPERIMENT_SFM_POINT_COUNT_INVALID")
        for index in range(count):
            if index % 1024 == 0:
                health()
            block = _read(stream, 51)
            row = struct.unpack("<Q3d3BdQ", block)
            require(row[0] < 2**64-1 and all(math.isfinite(v) for v in row[1:4])
                    and math.isfinite(row[-2]) and row[-2] >= 0
                    and 2 <= row[-1] <= 300, "EXPERIMENT_SFM_POINT_INVALID")
            observations += row[-1]
            error_sum += row[-2]
            require(stream.tell() + 8*row[-1] <= path.stat().st_size,
                    "EXPERIMENT_SFM_BINARY_TRUNCATED")
            tracks = _read(stream, 8*row[-1])
            fingerprint = (fingerprint + int.from_bytes(hashlib.sha256(block + tracks).digest(), "big")) % 2**256
        require(stream.read(1) == b"", "EXPERIMENT_SFM_BINARY_TRAILING_DATA")
    return count, observations, error_sum/count, f"{fingerprint:064x}"


def _text_points(path: Path, health) -> tuple[int, int, float, str]:
    count = observations = fingerprint = 0
    error_sum = 0.0
    with path.open(encoding="utf-8") as stream:
        while raw := stream.readline(128*1024):
            health()
            require(raw.endswith("\n") or stream.tell() == path.stat().st_size,
                    "EXPERIMENT_SFM_TEXT_POINT_LINE_TOO_LONG")
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            fields = raw.split()
            require(len(fields) >= 12 and (len(fields)-8)%2 == 0, "EXPERIMENT_SFM_TEXT_POINT_INVALID")
            try:
                point_id = int(fields[0]); xyz = tuple(float(v) for v in fields[1:4])
                rgb = tuple(int(v) for v in fields[4:7]); error = float(fields[7])
                tracks = tuple(int(v) for v in fields[8:]); track_count = len(tracks)//2
                require(all(math.isfinite(v) for v in (*xyz,error)) and error >= 0
                        and 2 <= track_count <= 300, "EXPERIMENT_SFM_TEXT_POINT_INVALID")
                block = struct.pack("<Q3d3BdQ",point_id,*xyz,*rgb,error,track_count)
                track_block = struct.pack("<"+"i"*len(tracks),*tracks)
            except (ValueError, struct.error):
                raise ExperimentGateError("EXPERIMENT_SFM_TEXT_POINT_INVALID") from None
            fingerprint = (fingerprint + int.from_bytes(hashlib.sha256(block+track_block).digest(),"big")) % 2**256
            observations += track_count; error_sum += error; count += 1
    require(count > 0, "EXPERIMENT_SFM_TEXT_POINT_INVALID")
    return count, observations, error_sum/count, f"{fingerprint:064x}"


def _camera_models(binary: Path, text: Path, health) -> dict:
    cameras = {}
    with binary.open("rb") as stream:
        count, = struct.unpack("<Q", _read(stream, 8))
        require(0 < count <= 300, "EXPERIMENT_SFM_CAMERA_COUNT_INVALID")
        for _ in range(count):
            health()
            row = struct.unpack("<iiQQ4d", _read(stream, 56))
            require(row[0] > 0 and row[0] not in cameras and row[1] == 1
                    and row[2] > 0 and row[3] > 0 and all(math.isfinite(v) for v in row[4:])
                    and row[4] > 0 and row[5] > 0, "EXPERIMENT_SFM_CAMERA_INVALID")
            cameras[row[0]] = row[2:]
        require(stream.read(1) == b"", "EXPERIMENT_SFM_BINARY_TRAILING_DATA")
    text_cameras = {}
    with text.open(encoding="utf-8") as stream:
        for raw in stream:
            health()
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            fields = raw.split()
            require(len(fields) == 8 and fields[1] == "PINHOLE", "EXPERIMENT_SFM_TEXT_CAMERA_INVALID")
            try:
                camera_id = int(fields[0])
                value = (int(fields[2]), int(fields[3]), *(float(v) for v in fields[4:]))
            except ValueError:
                raise ExperimentGateError("EXPERIMENT_SFM_TEXT_CAMERA_INVALID") from None
            require(camera_id not in text_cameras, "EXPERIMENT_SFM_TEXT_CAMERA_INVALID")
            text_cameras[camera_id] = value
    require(text_cameras == cameras, "EXPERIMENT_SFM_TEXT_BINARY_CAMERA_MISMATCH")
    return cameras


def gate_sfm(experiment_root: Path, image_names: tuple[str, ...], health_check=lambda: None) -> dict:
    folder = experiment_root / "work/shared/colmap"
    metrics = _json(folder / "reconstruction_metrics.json", experiment_root)
    options = _json(experiment_root / "reports/sfm-actual-options.json", experiment_root)
    require(options.get("status") == "succeeded" and options.get("sfm_stage_complete") is True
            and options.get("runtime_versions", {}).get("pycolmap") == "3.13.0"
            and options.get("runtime_versions", {}).get("python") == "3.12.14"
            and options.get("runtime_versions", {}).get("pycolmap_has_cuda") is False,
            "EXPERIMENT_SFM_ACTUAL_EXECUTION_EVIDENCE_MISSING")
    extraction, matching = options.get("actual_extraction", {}), options.get("actual_matching", {})
    require(extraction == {"num_threads": 12, "max_image_size": 1600, "max_num_features": 6000, "device": "cpu"}
            and matching == {"num_threads": 12, "guided_matching": True, "device": "cpu"},
            "EXPERIMENT_SFM_ACTUAL_THREAD_MISMATCH")
    mapper = options.get("actual_mapper_options")
    require(isinstance(mapper, list) and 1 <= len(mapper) <= 2
            and all(row.get("num_threads") == 32 and row.get("mapper_num_threads") == 32
                    and row.get("phase") == ("initial" if index == 0 else "deferred_retry")
                    for index, row in enumerate(mapper)), "EXPERIMENT_SFM_ACTUAL_MAPPER_MISMATCH")
    require(len(image_names) == len(set(image_names)) == 300, "EXPERIMENT_SFM_EXPECTED_IMAGES_INVALID")
    database = checked_path(folder / "database.db", experiment_root)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: (health_check(), 0)[1], 10000)
        require(connection.execute("PRAGMA quick_check").fetchone() == ("ok",), "EXPERIMENT_SFM_DATABASE_INVALID")
        rows = connection.execute("SELECT image_id,name,camera_id FROM images ORDER BY image_id").fetchall()
        require(len(rows) == 300 and {row[1] for row in rows} == set(image_names),
                "EXPERIMENT_SFM_DATABASE_IDENTITY_MISMATCH")
        mapping = {row[0]: (row[1], row[2]) for row in rows}
        for table in ("keypoints", "descriptors"):
            require(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 300,
                    "EXPERIMENT_SFM_FEATURES_MISSING")
        verified = connection.execute("SELECT COUNT(*) FROM two_view_geometries WHERE rows >= 15").fetchone()[0]
    binary = folder / "mapper/0"
    text = folder / "refined_text"
    for model in (binary, text):
        for name in ("cameras", "images", "points3D"):
            checked_path(model / (name + (".bin" if model == binary else ".txt")), experiment_root)
    binary_rows = _binary_images(binary / "images.bin", health_check)
    cameras = _camera_models(binary / "cameras.bin", text / "cameras.txt", health_check)
    require(len(binary_rows) >= 3 and all(mapping.get(key) == value and value[1] in cameras
            for key, value in binary_rows.items()),
            "EXPERIMENT_SFM_MODEL_DATABASE_IDENTITY_MISMATCH")
    binary_points = _binary_points(binary / "points3D.bin", health_check)
    text_points = _text_points(text / "points3D.txt", health_check)
    require(binary_points[:2] == text_points[:2] and binary_points[3] == text_points[3]
            and math.isclose(binary_points[2], text_points[2], rel_tol=1e-9, abs_tol=1e-12),
            "EXPERIMENT_SFM_TEXT_BINARY_POINT_MISMATCH")
    points, observations, reprojection, _ = binary_points
    text_rows = {}
    with (text / "images.txt").open(encoding="utf-8") as stream:
        for raw in stream:
            health_check()
            if raw.lstrip().startswith("#") or not raw.strip():
                continue
            fields = raw.strip().split()
            require(len(fields) >= 10, "EXPERIMENT_SFM_TEXT_MODEL_INVALID")
            try:
                image_id, camera = int(fields[0]), int(fields[8])
                require(all(math.isfinite(float(v)) for v in fields[1:8]), "EXPERIMENT_SFM_POSE_INVALID")
            except ValueError:
                raise ExperimentGateError("EXPERIMENT_SFM_TEXT_MODEL_INVALID") from None
            require(image_id not in text_rows, "EXPERIMENT_SFM_IMAGE_DUPLICATE")
            text_rows[image_id] = (" ".join(fields[9:]), camera)
            require(stream.readline() != "", "EXPERIMENT_SFM_TEXT_MODEL_TRUNCATED")
    require(text_rows == binary_rows, "EXPERIMENT_SFM_TEXT_BINARY_MISMATCH")
    require(metrics.get("database_images") == 300
            and metrics.get("registered_images") == len(binary_rows)
            and metrics.get("cameras") == len(cameras)
            and metrics.get("points3D") == points and metrics.get("observations") == observations
            and metrics.get("verified_pairs_min_15") == verified and verified > 0,
            "EXPERIMENT_SFM_METRICS_MODEL_MISMATCH")
    require(type(metrics.get("mean_reprojection_error_px")) in (int, float)
            and math.isfinite(metrics["mean_reprojection_error_px"])
            and math.isclose(metrics["mean_reprojection_error_px"], reprojection, rel_tol=1e-6, abs_tol=1e-9),
            "EXPERIMENT_SFM_REPROJECTION_MISMATCH")
    require(isinstance(metrics.get("binary_model"),str) and isinstance(metrics.get("text_model"),str)
            and Path(metrics.get("binary_model", "")) == binary
            and Path(metrics.get("text_model", "")) == text,
            "EXPERIMENT_SFM_MODEL_PATH_MISMATCH")
    return {"database_images": 300, "registered_images": len(binary_rows), "points3D": points,
            "observations": observations, "mean_reprojection_error_px": reprojection,
            "verified_pairs_min_15": verified, "geometry_accuracy_certified": False}


def _glb(path: Path) -> dict:
    with path.open("rb") as stream:
        magic, version, size = struct.unpack("<4sII", _read(stream, 12))
        require(magic == b"glTF" and version == 2 and size == path.stat().st_size,
                "EXPERIMENT_OUTPUT_GLB_HEADER_INVALID")
        length, kind = struct.unpack("<I4s", _read(stream, 8))
        require(kind == b"JSON" and 0 < length <= 16*1024**2 and length % 4 == 0,
                "EXPERIMENT_OUTPUT_GLB_JSON_INVALID")
        try:
            value = json.loads(_read(stream, length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            accessors = value["accessors"]
            faces = 0
            for mesh in value["meshes"]:
                for primitive in mesh["primitives"]:
                    require(primitive.get("mode", 4) == 4, "EXPERIMENT_OUTPUT_GLB_NONTRIANGLE")
                    count = accessors[primitive["indices"]]["count"]
                    require(type(count) is int and count > 0 and count % 3 == 0,
                            "EXPERIMENT_OUTPUT_GLB_INDICES_INVALID")
                    faces += count // 3
            require(value.get("asset", {}).get("version") == "2.0" and faces > 0,
                    "EXPERIMENT_OUTPUT_GLB_MODEL_INVALID")
        except (ValueError, KeyError, TypeError, IndexError):
            raise ExperimentGateError("EXPERIMENT_OUTPUT_GLB_MODEL_INVALID") from None
        binary_length, binary_kind = struct.unpack("<I4s", _read(stream, 8))
        require(binary_kind == b"BIN\x00" and binary_length > 0 and binary_length % 4 == 0
                and stream.tell() + binary_length == size, "EXPERIMENT_OUTPUT_GLB_BINARY_INVALID")
    return {"version": version, "declared_length": size, "indexed_triangle_faces": faces}


def gate_outputs(experiment_root: Path, health_check=lambda: None) -> dict:
    output = experiment_root / "output"
    validation = _json(output / "validation.json", experiment_root)
    require(validation.get("all_passed") is True and isinstance(validation.get("branches"), dict)
            and set(validation["branches"]) == set(LABELS), "EXPERIMENT_OUTPUT_VALIDATION_REJECTED")
    artifacts = []
    for label in LABELS:
        health_check()
        folder = output / label
        files = {name: checked_path(folder / name, experiment_root)
                 for name in ("mesh.obj", "mesh.mtl", "texture.jpg", "mesh.glb")}
        report = validation["branches"][label]
        glb = _glb(files["mesh.glb"])
        obj_vertices = obj_faces = 0
        mtl_reference = False
        with files["mesh.obj"].open(encoding="utf-8", errors="strict") as stream:
            for index, raw in enumerate(stream):
                if index % 4096 == 0:
                    health_check()
                obj_vertices += raw.startswith("v ")
                obj_faces += raw.startswith("f ")
                mtl_reference |= raw.strip() == "mtllib mesh.mtl"
        require(files["mesh.mtl"].stat().st_size <= 1024**2, "EXPERIMENT_OUTPUT_MTL_TOO_LARGE")
        mtl = files["mesh.mtl"].read_text(encoding="utf-8")
        require(mtl_reference and "map_Kd texture.jpg" in mtl and "d 1.000000" in mtl,
                "EXPERIMENT_OUTPUT_TEXTURE_LINK_INVALID")
        with files["texture.jpg"].open("rb") as stream:
            start = stream.read(3)
            stream.seek(-2, 2)
            end = stream.read(2)
        require(start == b"\xff\xd8\xff" and end == b"\xff\xd9", "EXPERIMENT_OUTPUT_JPEG_HEADER_INVALID")
        require(report.get("status") == "pass" and report.get("errors") == []
                and type(report.get("obj_faces")) is int and obj_faces > 0
                and report.get("obj_faces") == report.get("glb_faces") == obj_faces == glb["indexed_triangle_faces"]
                and report.get("obj_vertices") == obj_vertices > 0,
                "EXPERIMENT_OUTPUT_FACE_COUNT_MISMATCH")
        for name, path in files.items():
            artifacts.append({"relative_path": path.relative_to(experiment_root).as_posix(),
                              "size_bytes": path.stat().st_size, "sha256": _hash(path, health_check),
                              **({"glb_header": glb} if name == "mesh.glb" else {})})
    return {"all_passed": True, "artifact_count": len(artifacts), "artifacts": artifacts,
            "geometry_accuracy_certified": False}


def gate_stage(stage: StageSpec, experiment_root: Path, image_names: tuple[str, ...],
               health_check=lambda: None) -> dict:
    health_check()
    marker = checked_path(stage.marker, experiment_root)
    if stage.name == "01-colmap":
        return gate_sfm(experiment_root, image_names, health_check)
    if stage.name == "90-validate":
        return gate_outputs(experiment_root, health_check)
    if stage.name == "11-c-densify":
        dmaps = sorted(marker.parent.glob("depth*.dmap"))
        require(0 < len(dmaps) <= 300, "EXPERIMENT_STAGE_TEMPLATE_DMAPS_MISSING")
        for path in dmaps:
            health_check()
            checked_path(path,experiment_root)
    if marker.suffix == ".json":
        prediction = marker.name == "prediction_manifest.json"
        value = _json(marker, experiment_root, object_required=not prediction)
        if prediction:
            require(isinstance(value,list) and 3 <= len(value) <= 300
                    and all(isinstance(row,dict) for row in value), "EXPERIMENT_STAGE_PREDICTIONS_INVALID")
            names = [row.get("name") for row in value]
            require(len(set(names)) == len(names) and set(names) <= set(image_names),
                    "EXPERIMENT_STAGE_PREDICTION_IDENTITY_MISMATCH")
            require(all(type(row.get("valid_depth_ratio")) in (int,float)
                        and math.isfinite(row["valid_depth_ratio"]) and 0 <= row["valid_depth_ratio"] <= 1
                        for row in value) and any(row["valid_depth_ratio"] > 0 for row in value),
                    "EXPERIMENT_STAGE_PREDICTION_DEPTH_INVALID")
            for name in names:
                health_check()
                for suffix in ("_depth.npy","_mask.npy"):
                    checked_path(marker.parent / "depth" / (Path(name).stem+suffix),experiment_root)
            return {"stage":stage.name,"predicted_images":len(value),"gate_passed":True,
                    "geometry_accuracy_certified":False}
        if "validate-dmap" in stage.name or "verify-handoff" in stage.name:
            require(value.get("all_passed") is True and value.get("errors") == [],
                    "EXPERIMENT_STAGE_HANDOFF_REJECTED")
            if "validate-dmap" in stage.name:
                require(type(value.get("dmap_count")) is int and value["dmap_count"] > 0
                        and type(value.get("valid_depth_samples")) is int and value["valid_depth_samples"] > 0,
                        "EXPERIMENT_STAGE_HANDOFF_EMPTY")
            else:
                require(type(value.get("file_count")) is int and value["file_count"] > 0,
                        "EXPERIMENT_STAGE_HANDOFF_EMPTY")
        if "stage-openmvs" in stage.name:
            require(type(value.get("file_count")) is int and value["file_count"] > 0
                    and isinstance(value.get("records"), list)
                    and len(value["records"]) == value["file_count"],
                    "EXPERIMENT_STAGE_STAGING_MANIFEST_INVALID")
    return {"stage": stage.name, "marker_relative_path": marker.relative_to(experiment_root).as_posix(),
            "marker_size_bytes": marker.stat().st_size, "gate_passed": True}
