"""Hash-pinned, new-directory-only CPU SfM handoffs; no queue or execution APIs.

The package binds a genuine CPU completion report to the original static input
and a selected database snapshot. A receiver rechecks database/model consistency
and may rebind only the two metrics model paths, retaining the original metrics.
This is format/identity admission, never a geometry accuracy certificate.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable

from backend.experiments.checkpoint import _database, _hash, _json, _relative
from backend.experiments.hybrid_checkpoint import (
    VerifiedHybridSource, _stable_database_identity, _verify_selected_snapshot, restore_hybrid_source,
)
from backend.experiments.stages import gate_sfm

CHUNK = 1024 * 1024
MANIFEST = "handoff-manifest.json"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIXED = {"work/shared/colmap/database.db", "work/shared/colmap/reconstruction_metrics.json",
          "reports/sfm-actual-options.json", "identity/quality-config.json"}
_MODEL_NAMES = {"cameras", "images", "points3D", "rigs", "frames"}
_SNAPSHOT_NAME = re.compile(r"snapshot-\d{8}T\d{12}Z-[0-9a-f]{32}\Z")
_SNAPSHOT_PREFIX = 'identity/final-snapshot/'
_SNAPSHOT_FILES = frozenset(_SNAPSHOT_PREFIX + name for name in (
    'cpu-result.json', 'effective-cpu-profile.json', 'snapshot-manifest.json', 'DONE.json'))
_SNAPSHOT_KEYS = {'schema_version', 'owner_id', 'directory', 'database_relative_path',
    'database_sha256', 'database_size_bytes', 'validated_complete_pair_records', 'image_count',
    'created_at_utc', 'reason', 'committed_wal_included', 'source_write_operations',
    'sqlite_backup_completed', 'sqlite_integrity_and_mapping_validation',
    'database_hash_checked_before_and_after_validation', 'sfm_completion_certified'}
_OPTIONS_KEYS = {
    "schema_version", "status", "profile_sha256", "colmap_script_sha256",
    "pipeline_script_sha256", "baseline_config_sha256", "quality_config_sha256",
    "requested_sfm_threads", "requested_mapper_threads", "actual_extraction",
    "actual_matching", "actual_mapper_options", "sfm_stage_complete", "runtime_versions",
    "protection_owned_by_parent", "child_provides_resource_protection", "tool_manifest_sha256",
}
_METRIC_KEYS = {"database_images", "verified_pairs_min_15", "candidate_models", "selected_candidate_id",
                "registered_images", "cameras", "points3D", "observations", "mean_track_length",
                "mean_observations_per_image", "mean_reprojection_error_px", "binary_model", "text_model",
                "deferred_registration"}


class HandoffError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise HandoffError(code)


def _noop():
    return None


def _pin(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value), "HANDOFF_SHA_INVALID")
    return value


def _path(path, *, exists=True):
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts, "HANDOFF_PATH_INVALID")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            _require(not exists, "HANDOFF_PATH_MISSING")
            continue
        _require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 1024),
                 "HANDOFF_REPARSE_REJECTED")
    return path


def _inventory(root, health):
    root = _path(root)
    _require(root.is_dir(), "HANDOFF_DIRECTORY_REQUIRED")
    files = set()
    pending, directories = [root], set()
    while pending:
        folder = pending.pop()
        for path in folder.iterdir():
            health()
            _path(path)
            relative = path.relative_to(root).as_posix()
            _relative(relative)
            if path.is_dir():
                directories.add(relative)
                pending.append(path)
            else:
                _require(path.is_file(), "HANDOFF_REGULAR_FILE_REQUIRED")
                files.add(relative)
    _require(len({item.casefold() for item in files}) == len(files), "HANDOFF_CASE_COLLISION")
    required_directories = {parent.as_posix() for item in files for parent in Path(item).parents
                            if parent != Path(".")}
    _require(directories == required_directories, "HANDOFF_EXTRA_DIRECTORY_REJECTED")
    return files


def _allowed(relative, *, final_snapshot=False):
    if relative in _FIXED:
        return True
    if final_snapshot and relative in _SNAPSHOT_FILES:
        return True
    for prefix, suffix in (("work/shared/colmap/mapper/0/", ".bin"),
                           ("work/shared/colmap/refined_text/", ".txt")):
        if relative.startswith(prefix):
            name = relative.removeprefix(prefix)
            return name.endswith(suffix) and name.removesuffix(suffix) in _MODEL_NAMES
    return False


def _write(path, value):
    _path(path, exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _path(path.parent)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    _require(len(payload) <= 4 * CHUNK, "HANDOFF_DOCUMENT_TOO_LARGE")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(payload).hexdigest()


def _copy(source, destination, size, sha, health):
    _path(source)
    _path(destination, exists=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _path(destination.parent)
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        while block := reader.read(CHUNK):
            health()
            writer.write(block)
            digest.update(block)
        writer.flush()
        os.fsync(writer.fileno())
    _require(destination.stat().st_size == size and digest.hexdigest() == sha,
             "HANDOFF_COPY_CHANGED")


def source_identity(source: VerifiedHybridSource) -> dict:
    original = source.original
    prepared = next(row for row in original.files
                    if row.relative_path == "runtime/work/input/input-manifest.json")
    return {"source_job_id": original.job_id, "pipeline_commit": original.pipeline_commit,
            "pipeline_config_sha256": original.config_sha256,
            "original_checkpoint_manifest_sha256": original.manifest_sha256,
            "original_database_sha256": original.database_sha256,
            "selected_database_sha256": source.selected_database_sha256,
            "snapshot_manifest_sha256": source.snapshot_manifest_sha256,
            "selected_complete_pair_records": source.complete_pair_records,
            "prepared_input_manifest_sha256": prepared.sha256, "image_count": 300}


def _validate_database(database, source, health):
    _path(source.selected_database_path)
    _require(_hash(source.selected_database_path, health) == source.selected_database_sha256,
             "HANDOFF_SOURCE_DATABASE_CHANGED")
    for sidecar in ("-wal", "-shm"):
        _require(not database.with_name(database.name + sidecar).exists(), "HANDOFF_DATABASE_SIDECAR")
    count = _database(database, set(source.image_names), health)
    _require(source.complete_pair_records <= count <= 44850, "HANDOFF_PAIR_COUNT_REGRESSED")
    # This also compares cameras and preserves every preexisting pair ID. It
    # streams rows, so a 300-image descriptor database is not buffered in RAM.
    _stable_database_identity(source.selected_database_path, database, health)
    _require(_hash(source.selected_database_path, health) == source.selected_database_sha256,
             "HANDOFF_SOURCE_DATABASE_CHANGED")
    return count


def _gate(root, source, execution, health, *, database_path=None):
    original_root = Path(execution["source_experiment_root"])
    _require(original_root.is_absolute() and ".." not in original_root.parts
             and original_root.parent.name == "_experiments"
             and not any(part.casefold() == "jobs" for part in original_root.parts),
             "HANDOFF_SOURCE_EXPERIMENT_INVALID")
    metrics = _json(root / "work/shared/colmap/reconstruction_metrics.json")
    _require(set(metrics) <= _METRIC_KEYS, "HANDOFF_METRIC_FIELD_REJECTED")
    extra = {'database_path': database_path} if database_path is not None else {}
    return gate_sfm(root, source.image_names, health,
                    expected_sfm_threads=execution["sfm_threads"],
                    expected_mapper_threads=execution["mapper_threads"],
                    expected_python_version=execution["python_version"],
                    expected_binary_model_path=original_root / "work/shared/colmap/mapper/0",
                    expected_text_model_path=original_root / "work/shared/colmap/refined_text", **extra)


def _options(options, execution, quality_sha, colmap_sha):
    _require(set(options) <= _OPTIONS_KEYS, "HANDOFF_OPTIONS_FIELD_REJECTED")
    _require(options.get("colmap_script_sha256") == colmap_sha
             and options.get("baseline_config_sha256") == quality_sha,
             "HANDOFF_OPTIONS_SOURCE_MISMATCH")
    runtime = options.get("runtime_versions", {})
    _require(isinstance(runtime, dict) and set(runtime) == {"python", "pycolmap", "pycolmap_has_cuda"},
             "HANDOFF_RUNTIME_FIELDS_INVALID")
    rows = options.get("actual_mapper_options", [])
    _require(isinstance(rows, list) and all(isinstance(row, dict) and set(row) == {
        "call", "phase", "num_threads", "mapper_num_threads", "ba_inherits_top_level_num_threads"}
        and type(row["call"]) is int and row["call"] == index + 1
        and row["ba_inherits_top_level_num_threads"] is True
        for index, row in enumerate(rows)), "HANDOFF_MAPPER_FIELDS_INVALID")
    for key, value in (("requested_sfm_threads", execution["sfm_threads"]),
                       ("requested_mapper_threads", execution["mapper_threads"]),
                       ("protection_owned_by_parent", True), ("child_provides_resource_protection", False)):
        _require(key not in options or (type(options[key]) is type(value) and options[key] == value),
                 "HANDOFF_REQUESTED_OPTIONS_MISMATCH")
    for key in ("profile_sha256", "tool_manifest_sha256", "pipeline_script_sha256", "quality_config_sha256"):
        if key in options:
            _pin(options[key])


def _snapshot_evidence_paths(root):
    return {name: root / name for name in _SNAPSHOT_FILES}


def _verify_snapshot_evidence(provenance, paths, database, source, execution, options,
                              quality_sha, pipeline_sha, colmap_sha, sfm_gate, health):
    """Verify copied evidence using only fixed payload paths, never report paths."""
    keys = {'kind', 'snapshot_directory', 'cpu_result_sha256', 'cpu_profile_sha256',
        'snapshot_manifest_sha256', 'snapshot_done_sha256', 'database_sha256',
        'database_size_bytes', 'validated_complete_pair_records'}
    _require(type(provenance) is dict and set(provenance) == keys
             and provenance['kind'] == 'validated_final_snapshot'
             and isinstance(provenance['snapshot_directory'], str)
             and _SNAPSHOT_NAME.fullmatch(provenance['snapshot_directory']),
             'HANDOFF_FINAL_SNAPSHOT_PROVENANCE_INVALID')
    pins = {'cpu-result.json': 'cpu_result_sha256', 'effective-cpu-profile.json': 'cpu_profile_sha256',
            'snapshot-manifest.json': 'snapshot_manifest_sha256', 'DONE.json': 'snapshot_done_sha256'}
    documents = {}
    for name, key in pins.items():
        path = _path(paths[_SNAPSHOT_PREFIX + name])
        _require(path.stat().st_size <= 4 * CHUNK and _hash(path, health) == _pin(provenance[key]),
                 'HANDOFF_FINAL_SNAPSHOT_EVIDENCE_CHANGED')
        documents[name] = _json(path)
    report, profile = documents['cpu-result.json'], documents['effective-cpu-profile.json']
    snapshot, done = documents['snapshot-manifest.json'], documents['DONE.json']
    _require(set(snapshot) == _SNAPSHOT_KEYS and snapshot.get('schema_version') == '1.0'
             and snapshot.get('directory') == provenance['snapshot_directory']
             and snapshot.get('database_relative_path') == provenance['snapshot_directory'] + '/database.db'
             and snapshot.get('database_sha256') == _pin(provenance['database_sha256'])
             and type(snapshot.get('database_size_bytes')) is int
             and snapshot['database_size_bytes'] == provenance['database_size_bytes'] == database.stat().st_size
             and type(snapshot.get('validated_complete_pair_records')) is int
             and snapshot['validated_complete_pair_records'] == provenance['validated_complete_pair_records']
             and type(snapshot.get('image_count')) is int and snapshot['image_count'] == 300
             and snapshot.get('reason') == 'terminal'
             and snapshot.get('source_write_operations') is False
             and snapshot.get('sfm_completion_certified') is False
             and all(snapshot.get(key) is True for key in ('committed_wal_included', 'sqlite_backup_completed',
                 'sqlite_integrity_and_mapping_validation', 'database_hash_checked_before_and_after_validation'))
             and isinstance(snapshot.get('owner_id'), str) and re.fullmatch(r'[0-9a-f]{32}', snapshot['owner_id']),
             'HANDOFF_FINAL_SNAPSHOT_IDENTITY_INVALID')
    _require(done == {'schema_version': '1.0', 'owner_id': snapshot['owner_id'],
        'snapshot_manifest_sha256': provenance['snapshot_manifest_sha256'],
        'database_sha256': provenance['database_sha256']}, 'HANDOFF_FINAL_SNAPSHOT_DONE_INVALID')
    _require(report.get('schema_version') == '1.0' and report.get('mode') == 'cpu-sfm'
             and report.get('status') == 'succeeded' and report.get('final_snapshot_status') == 'valid'
             and report.get('final_snapshot') == {**snapshot, 'manifest_sha256': provenance['snapshot_manifest_sha256']}
             and all(report.get(key) is True for key in ('process_tree_stopped', 'snapshot_worker_stopped',
                 'observation_relay_stopped', 'terminal_evidence_confirmed', 'source_checkpoint_unchanged'))
             and report.get('sfm_gate') == sfm_gate
             and report.get('profile_sha256') == provenance['cpu_profile_sha256']
             and report.get('pipeline_commit') == source.original.pipeline_commit
             and report.get('config_sha256') == quality_sha
             and report.get('source_manifest_sha256') == source.original.manifest_sha256
             and report.get('source_database_sha256') == source.selected_database_sha256
             and report.get('source_job_id') == source.original.job_id
             and type(report.get('sfm_threads')) is int and report['sfm_threads'] == execution['sfm_threads']
             and type(report.get('mapper_ba_threads')) is int and report['mapper_ba_threads'] == execution['mapper_threads']
             and report.get('final_pair_counts', {}).get('complete_pair_records') == snapshot['validated_complete_pair_records'],
             'HANDOFF_FINAL_CPU_RESULT_UNCONFIRMED')
    pipeline, cpu, runtime = profile.get('pipeline', {}), profile.get('cpu', {}), profile.get('runtime', {})
    checkpoint, target = profile.get('source_checkpoint', {}), profile.get('target_experiment', {})
    _require(all(type(value) is dict for value in (pipeline, cpu, runtime, checkpoint, target)),
             'HANDOFF_FINAL_CPU_PROFILE_MISMATCH')
    profile_root = target.get('root')
    _require(isinstance(profile_root, str) and Path(profile_root).is_absolute()
             and '..' not in Path(profile_root).parts, 'HANDOFF_FINAL_CPU_PROFILE_MISMATCH')
    _require(profile.get('profile_kind') == 'local_cpu_sfm_candidate' and profile.get('autorun') is False
             and profile.get('source_job_id') == source.original.job_id
             and pipeline.get('commit') == source.original.pipeline_commit
             and pipeline.get('config_sha256') == quality_sha and pipeline.get('colmap_script_sha256') == colmap_sha
             and cpu == {'sfm_threads': execution['sfm_threads'], 'mapper_ba_threads': execution['mapper_threads']}
             and runtime.get('python_version') == execution['python_version']
             and runtime.get('pycolmap_version') == '3.13.0' and runtime.get('pycolmap_has_cuda') is False
             # Root paths are metadata here, never opened at the receiver.
             # Windows forward/backslash serialization does not change identity.
             and Path(profile_root) == Path(execution['source_experiment_root'])
             and target.get('scene') == Path(execution['source_experiment_root']).name
             and checkpoint.get('original_manifest_sha256') == source.original.manifest_sha256
             and checkpoint.get('original_database_sha256') == source.original.database_sha256
             and checkpoint.get('snapshot_manifest_sha256') == source.snapshot_manifest_sha256
             and checkpoint.get('snapshot_database_sha256') == source.selected_database_sha256
             and options.get('profile_sha256') == provenance['cpu_profile_sha256']
             # The CPU-only producer executes run_colmap_shared, not the full
             # pipeline. Older genuine options therefore omit this optional
             # field; a supplied value must still match the transfer identity.
             and ('pipeline_script_sha256' not in options or options['pipeline_script_sha256'] == pipeline_sha),
             'HANDOFF_FINAL_CPU_PROFILE_MISMATCH')
    _require(_hash(database, health) == provenance['database_sha256'], 'HANDOFF_FINAL_DATABASE_CHANGED')


def _final_snapshot_source(root, proposed, cpu_result_path, cpu_pin, input_source, execution,
                           options, quality_sha, pipeline_sha, colmap_sha, health):
    """Admit only the exact terminal snapshot pinned by the completed CPU report."""
    snapshot_root = _path(proposed)
    _require(snapshot_root.parent == root / 'checkpoints' and _SNAPSHOT_NAME.fullmatch(snapshot_root.name),
             'HANDOFF_FINAL_SNAPSHOT_ROOT_INVALID')
    report_path = root / 'reports/cpu-result.json'
    _require(cpu_result_path is None or Path(cpu_result_path) == report_path,
             'HANDOFF_FINAL_CPU_RESULT_PATH_INVALID')
    _require(_hash(_path(report_path), health) == _pin(cpu_pin), 'HANDOFF_FINAL_CPU_RESULT_HASH_MISMATCH')
    report = _json(report_path)
    record = report.get('final_snapshot')
    _require(type(record) is dict and record.get('directory') == snapshot_root.name
             and report.get('final_snapshot_status') == 'valid', 'HANDOFF_FINAL_SNAPSHOT_UNCONFIRMED')
    _verify_selected_snapshot(input_source.original, snapshot_root, _pin(record.get('manifest_sha256')),
        _pin(record.get('database_sha256')), record.get('validated_complete_pair_records'), health)
    paths = {_SNAPSHOT_PREFIX + 'cpu-result.json': report_path,
             _SNAPSHOT_PREFIX + 'effective-cpu-profile.json': root / 'reports/effective-cpu-profile.json',
             _SNAPSHOT_PREFIX + 'snapshot-manifest.json': snapshot_root / 'snapshot-manifest.json',
             _SNAPSHOT_PREFIX + 'DONE.json': snapshot_root / 'DONE.json'}
    database = snapshot_root / 'database.db'
    provenance = {'kind': 'validated_final_snapshot', 'snapshot_directory': snapshot_root.name,
        'cpu_result_sha256': cpu_pin, 'cpu_profile_sha256': _pin(report.get('profile_sha256')),
        'snapshot_manifest_sha256': record['manifest_sha256'],
        'snapshot_done_sha256': _hash(paths[_SNAPSHOT_PREFIX + 'DONE.json'], health),
        'database_sha256': record['database_sha256'], 'database_size_bytes': record.get('database_size_bytes'),
        'validated_complete_pair_records': record.get('validated_complete_pair_records')}
    gate = _gate(root, input_source, execution, health, database_path=database)
    _verify_snapshot_evidence(provenance, paths, database, input_source, execution, options,
                              quality_sha, pipeline_sha, colmap_sha, gate, health)
    return database, provenance, paths, gate


@dataclass(frozen=True)
class VerifiedSfmHandoff:
    root: Path
    manifest_sha256: str
    manifest: dict
    input_source: VerifiedHybridSource


@dataclass(frozen=True)
class ImportedSfmHandoff:
    root: Path
    work_dir: Path
    database_path: Path
    input_manifest_path: Path
    provenance_path: Path
    sfm_gate: dict
    first_stage: str = "02-extract-cameras"


def export_sfm_handoff(experiment_root, bundle_root, *, input_source: VerifiedHybridSource,
                       sfm_threads: int, mapper_threads: int, python_version: str,
                       quality_config_path, quality_config_sha256: str,
                       pipeline_script_sha256: str, colmap_script_sha256: str,
                       cpu_result_path=None, final_snapshot_root=None, expected_cpu_result_sha256=None,
                       health_check: Callable[[], None] = _noop) -> VerifiedSfmHandoff:
    root, bundle = _path(experiment_root), _path(bundle_root, exists=False)
    _require(not bundle.exists() and not bundle.is_relative_to(root) and not root.is_relative_to(bundle),
             "HANDOFF_NEW_SEPARATE_BUNDLE_REQUIRED")
    _require(sfm_threads in (8, 12) and type(sfm_threads) is int
             and mapper_threads in (8, 16, 24) and type(mapper_threads) is int,
             "HANDOFF_CPU_THREADS_INVALID")
    for pin in (quality_config_sha256, pipeline_script_sha256, colmap_script_sha256):
        _pin(pin)
    config = _path(quality_config_path)
    _require(_hash(config, health_check) == quality_config_sha256
             and quality_config_sha256 == input_source.original.config_sha256,
             "HANDOFF_QUALITY_CONFIG_CHANGED")
    _json(config)
    options_path = root / "reports/sfm-actual-options.json"
    options = _json(options_path)
    execution = {"source_experiment_root": str(root), "sfm_threads": sfm_threads,
                 "mapper_threads": mapper_threads, "python_version": python_version,
                 "pycolmap_version": "3.13.0"}
    _options(options, execution, quality_config_sha256, colmap_script_sha256)
    _require((final_snapshot_root is None) == (expected_cpu_result_sha256 is None),
             'HANDOFF_FINAL_SNAPSHOT_ARGUMENTS_REQUIRED')
    final_snapshot = final_snapshot_root is not None
    provenance, evidence_paths = None, {}
    if final_snapshot:
        database, provenance, evidence_paths, sfm_gate = _final_snapshot_source(root, final_snapshot_root,
            cpu_result_path, expected_cpu_result_sha256, input_source, execution, options,
            quality_config_sha256, pipeline_script_sha256, colmap_script_sha256, health_check)
    else:
        database = root / 'work/shared/colmap/database.db'
        sfm_gate = _gate(root, input_source, execution, health_check)
        report = _json(_path(cpu_result_path or root / "reports/cpu-result.json"))
        _require(report.get("status") == "succeeded" and report.get("mode") == "cpu-sfm"
                 and all(report.get(key) is True for key in ("process_tree_stopped", "snapshot_worker_stopped",
                     "terminal_evidence_confirmed", "source_checkpoint_unchanged"))
                 and report.get("sfm_gate") == sfm_gate, "HANDOFF_CPU_COMPLETION_UNCONFIRMED")
    prepared_info = next(row for row in input_source.original.files
                         if row.relative_path == "runtime/work/input/input-manifest.json")
    _require(_hash(root / "work/input/input-manifest.json", health_check) == prepared_info.sha256,
             "HANDOFF_PREPARED_INPUT_CHANGED")
    for row in input_source.original.files:
        if row.relative_path.startswith(("runtime/work/input/images/", "runtime/work/input/masks/")):
            path = _path(root / row.relative_path.removeprefix("runtime/"))
            _require(path.stat().st_size == row.size_bytes and _hash(path, health_check) == row.sha256,
                     "HANDOFF_STATIC_INPUT_CHANGED")
    files = _inventory(root / "work/shared/colmap/mapper/0", health_check)
    payload_names = {"work/shared/colmap/mapper/0/" + item for item in files}
    files = _inventory(root / "work/shared/colmap/refined_text", health_check)
    payload_names |= {"work/shared/colmap/refined_text/" + item for item in files}
    payload_names |= _FIXED - {"identity/quality-config.json"}
    payload_names |= set(evidence_paths)
    _require(all(_allowed(item, final_snapshot=final_snapshot) for item in payload_names), "HANDOFF_PAYLOAD_NOT_ALLOWED")
    pair_count = _validate_database(database, input_source, health_check)
    records = []
    for relative in sorted(payload_names):
        path = _path(database if relative == 'work/shared/colmap/database.db'
                     else evidence_paths.get(relative, root / relative))
        records.append({"relative_path": relative, "size_bytes": path.stat().st_size,
                        "sha256": _hash(path, health_check)})
    records.append({"relative_path": "identity/quality-config.json", "size_bytes": config.stat().st_size,
                    "sha256": quality_config_sha256})
    bundle.parent.mkdir(parents=True, exist_ok=True)
    _path(bundle.parent)
    bundle.mkdir()
    _write(bundle / "INCOMPLETE.json", {"status": "incomplete", "schema_version": "1.0"})
    for row in records:
        relative = row['relative_path']
        source = config if relative == 'identity/quality-config.json' else (
            database if relative == 'work/shared/colmap/database.db'
            else evidence_paths.get(relative, root / relative))
        _copy(source, bundle / "payload" / row["relative_path"], row["size_bytes"], row["sha256"], health_check)
    manifest = {"schema_version": "1.0", "kind": "re3d_cpu_sfm_handoff", "source": source_identity(input_source),
                "execution": execution, "quality_config_sha256": quality_config_sha256,
                "scripts": {"run_pipeline_sha256": pipeline_script_sha256,
                            "run_colmap_shared_sha256": colmap_script_sha256},
                "complete_pair_records": pair_count, "sfm_gate": sfm_gate, "files": records,
                "completion": {key: True for key in ("process_tree_stopped", "snapshot_worker_stopped",
                               "terminal_evidence_confirmed", "source_checkpoint_unchanged")},
                "old_platform_contracts_copied": False, "geometry_accuracy_certified": False}
    if final_snapshot:
        manifest['database_source'] = provenance
        # Copying never authorizes drift in the pinned source after admission.
        _require(_hash(database, health_check) == provenance['database_sha256'], 'HANDOFF_FINAL_DATABASE_CHANGED')
        for relative, path in evidence_paths.items():
            record = next(row for row in records if row['relative_path'] == relative)
            _require(_hash(path, health_check) == record['sha256'], 'HANDOFF_FINAL_SNAPSHOT_EVIDENCE_CHANGED')
    manifest_sha = _write(bundle / MANIFEST, manifest)
    _write(bundle / "DONE.json", {"schema_version": "1.0", "handoff_manifest_sha256": manifest_sha})
    _path(bundle / "INCOMPLETE.json").unlink()
    return verify_sfm_handoff(bundle, manifest_sha, input_source=input_source,
                             expected_quality_config_sha256=quality_config_sha256,
                             expected_pipeline_script_sha256=pipeline_script_sha256,
                             expected_colmap_script_sha256=colmap_script_sha256, health_check=health_check)


def verify_sfm_handoff(bundle_root, expected_manifest_sha256, *, input_source: VerifiedHybridSource,
                       expected_quality_config_sha256: str, expected_pipeline_script_sha256: str,
                       expected_colmap_script_sha256: str,
                       health_check: Callable[[], None] = _noop) -> VerifiedSfmHandoff:
    root = _path(bundle_root)
    expected_sha = _pin(expected_manifest_sha256)
    for pin in (expected_quality_config_sha256, expected_pipeline_script_sha256, expected_colmap_script_sha256):
        _pin(pin)
    _require(_hash(root / MANIFEST, health_check) == expected_sha, "HANDOFF_MANIFEST_HASH_MISMATCH")
    manifest, done = _json(root / MANIFEST), _json(root / "DONE.json")
    legacy_fields = {"schema_version", "kind", "source", "execution", "quality_config_sha256",
                 "scripts", "complete_pair_records", "sfm_gate", "files", "completion",
                 "old_platform_contracts_copied", "geometry_accuracy_certified"}
    _require(set(manifest) in (legacy_fields, legacy_fields | {'database_source'}), 'HANDOFF_MANIFEST_FIELDS_INVALID')
    final_snapshot = 'database_source' in manifest
    _require(done == {"schema_version": "1.0", "handoff_manifest_sha256": expected_sha}, "HANDOFF_DONE_INVALID")
    _require(manifest.get("schema_version") == "1.0" and manifest.get("kind") == "re3d_cpu_sfm_handoff"
             and manifest.get("source") == source_identity(input_source)
             and manifest.get("quality_config_sha256") == expected_quality_config_sha256
             and expected_quality_config_sha256 == input_source.original.config_sha256
             and manifest.get("scripts") == {"run_pipeline_sha256": expected_pipeline_script_sha256,
                                            "run_colmap_shared_sha256": expected_colmap_script_sha256}
             and manifest.get("old_platform_contracts_copied") is False
             and manifest.get("geometry_accuracy_certified") is False,
             "HANDOFF_IDENTITY_MISMATCH")
    _require(manifest.get("completion") == {key: True for key in ("process_tree_stopped", "snapshot_worker_stopped",
                 "terminal_evidence_confirmed", "source_checkpoint_unchanged")}, "HANDOFF_COMPLETION_INVALID")
    execution = manifest.get("execution", {})
    _require(isinstance(execution, dict) and set(execution) == {"source_experiment_root", "sfm_threads",
                 "mapper_threads", "python_version", "pycolmap_version"}
             and type(execution.get("sfm_threads")) is int and execution["sfm_threads"] in (8, 12)
             and type(execution.get("mapper_threads")) is int and execution["mapper_threads"] in (8, 16, 24)
             and isinstance(execution.get("python_version"), str)
             and re.fullmatch(r"3\.12\.\d+", execution["python_version"])
             and execution.get("pycolmap_version") == "3.13.0", "HANDOFF_RUNTIME_INVALID")
    entries, expected, folded = manifest.get("files"), set(), set()
    minimum, maximum = (14, 18) if final_snapshot else (10, 14)
    _require(isinstance(entries, list) and minimum <= len(entries) <= maximum, "HANDOFF_FILE_COUNT_INVALID")
    for row in entries:
        _require(isinstance(row, dict) and set(row) == {"relative_path", "size_bytes", "sha256"},
                 "HANDOFF_FILE_RECORD_INVALID")
        relative = _relative(row["relative_path"])
        _require(_allowed(relative, final_snapshot=final_snapshot) and relative.casefold() not in folded,
                 "HANDOFF_PAYLOAD_NOT_ALLOWED")
        folded.add(relative.casefold()); expected.add(relative)
        _require(type(row["size_bytes"]) is int and row["size_bytes"] > 0, "HANDOFF_FILE_SIZE_INVALID")
        path = _path(root / "payload" / relative)
        _require(path.stat().st_size == row["size_bytes"] and _hash(path, health_check) == _pin(row["sha256"]),
                 "HANDOFF_FILE_HASH_MISMATCH")
    required = _FIXED | {prefix + name + suffix for prefix, suffix in
                         (("work/shared/colmap/mapper/0/", ".bin"), ("work/shared/colmap/refined_text/", ".txt"))
                         for name in ("cameras", "images", "points3D")}
    if final_snapshot:
        required |= _SNAPSHOT_FILES
    _require(required <= expected, "HANDOFF_REQUIRED_MODEL_MISSING")
    inventory = _inventory(root, health_check)
    _require(inventory == {MANIFEST, "DONE.json"} | {"payload/" + item for item in expected},
             "HANDOFF_EXTRA_FILE_REJECTED")
    options = _json(root / "payload/reports/sfm-actual-options.json")
    _options(options, execution, expected_quality_config_sha256, expected_colmap_script_sha256)
    _require(_hash(root / "payload/identity/quality-config.json", health_check) == expected_quality_config_sha256,
             "HANDOFF_QUALITY_CONFIG_CHANGED")
    database = root / 'payload/work/shared/colmap/database.db'
    gate_options = {'database_path': database} if final_snapshot else {}
    _require(_gate(root / "payload", input_source, execution, health_check, **gate_options) == manifest.get("sfm_gate"),
             "HANDOFF_GATE_MISMATCH")
    if final_snapshot:
        _verify_snapshot_evidence(manifest['database_source'], _snapshot_evidence_paths(root / 'payload'),
            database, input_source, execution, options, expected_quality_config_sha256,
            expected_pipeline_script_sha256, expected_colmap_script_sha256, manifest['sfm_gate'], health_check)
    pair_count = _validate_database(database, input_source, health_check)
    _require(pair_count == manifest.get("complete_pair_records"), "HANDOFF_PAIR_COUNT_MISMATCH")
    if final_snapshot:
        _require(pair_count == manifest['database_source']['validated_complete_pair_records'],
                 'HANDOFF_FINAL_SNAPSHOT_PAIR_COUNT_MISMATCH')
    # A model/database consistency read must not certify an export that changed
    # after its first hash pass. This is the transfer boundary, not a polling loop.
    for row in entries:
        path = _path(root / "payload" / row["relative_path"])
        _require(path.stat().st_size == row["size_bytes"] and _hash(path, health_check) == row["sha256"],
                 "HANDOFF_PAYLOAD_CHANGED_DURING_VALIDATION")
    _require(_hash(root / MANIFEST, health_check) == expected_sha, "HANDOFF_MANIFEST_CHANGED")
    return VerifiedSfmHandoff(root, expected_sha, manifest, input_source)


def import_sfm_handoff(verified: VerifiedSfmHandoff, target_root, *, input_source: VerifiedHybridSource,
                       health_check: Callable[[], None] = _noop,
                       target_work_root=None) -> ImportedSfmHandoff:
    fresh = verify_sfm_handoff(verified.root, verified.manifest_sha256, input_source=input_source,
        expected_quality_config_sha256=verified.manifest["quality_config_sha256"],
        expected_pipeline_script_sha256=verified.manifest["scripts"]["run_pipeline_sha256"],
        expected_colmap_script_sha256=verified.manifest["scripts"]["run_colmap_shared_sha256"], health_check=health_check)
    target = _path(target_root, exists=False)
    _require((not target.exists() or target_work_root is not None) and target.parent.name == "_experiments"
             and not any(part.casefold() == "jobs" for part in target.parts)
             and not target.is_relative_to(fresh.root) and not fresh.root.is_relative_to(target),
             "HANDOFF_NEW_EXPERIMENT_REQUIRED")
    # The checkpoint restorer only accepts the reserved-root file layout
    # (operation-reservation + resource samples), never old work/results/markers.
    # This layout check is not authentication of the reservation's caller.
    restored = restore_hybrid_source(input_source, target, health_check=health_check,
                                    target_work_root=target_work_root)
    _write(target / "HANDOFF_IMPORT_INCOMPLETE.json", {"schema_version": "1.0", "status": "incomplete"})
    # Preserve the newly cloned pre-SfM source; never replace any original or old job DB.
    original_clone = target / "work/shared/colmap/database.db"
    preserved = target / "work/source-database-before-sfm.db"
    _path(original_clone); _path(preserved, exists=False)
    original_clone.rename(preserved)
    for row in fresh.manifest["files"]:
        relative = row["relative_path"]
        destination = target / relative
        if relative == "work/shared/colmap/reconstruction_metrics.json":
            continue
        _copy(fresh.root / "payload" / relative, destination, row["size_bytes"], row["sha256"], health_check)
    original_metrics = _json(fresh.root / "payload/work/shared/colmap/reconstruction_metrics.json")
    rebound = dict(original_metrics)
    rebound["binary_model"] = str(target / "work/shared/colmap/mapper/0")
    rebound["text_model"] = str(target / "work/shared/colmap/refined_text")
    _write(target / "work/shared/colmap/reconstruction_metrics.json", rebound)
    execution = fresh.manifest["execution"]
    database_options = {'database_path': target / 'work/shared/colmap/database.db'} if 'database_source' in fresh.manifest else {}
    result = gate_sfm(target, input_source.image_names, health_check,
                      expected_sfm_threads=execution["sfm_threads"], expected_mapper_threads=execution["mapper_threads"],
                      expected_python_version=execution["python_version"], **database_options)
    _require(result == fresh.manifest["sfm_gate"], "HANDOFF_IMPORTED_GATE_MISMATCH")
    provenance = target / "reports/handoff-import.json"
    _write(provenance, {"schema_version": "1.0", "kind": "verified_cpu_sfm_import",
                       "handoff_manifest_sha256": fresh.manifest_sha256, "source": source_identity(input_source),
                       "original_metrics": original_metrics, "rebound_fields": ["binary_model", "text_model"],
                       "sfm_gate": result, "first_stage": "02-extract-cameras",
                       "source_task_modified": False, "platform_job_resume": False})
    _write(target / "HANDOFF_IMPORT_COMPLETE.json", {"schema_version": "1.0", "status": "verified",
            "provenance_sha256": _hash(provenance, health_check), "sfm_gate_passed": True})
    _path(target / "HANDOFF_IMPORT_INCOMPLETE.json").unlink()
    return ImportedSfmHandoff(target, restored.work_dir, target / "work/shared/colmap/database.db",
                             restored.input_manifest_path, provenance, result)
