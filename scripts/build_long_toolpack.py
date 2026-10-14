"""Build a local, immutable Git-blob tool overlay; never run reconstruction.

The overlay depends on the separately installed, clean platform baseline. It is
not a server image, environment backup, database backup or automatic launcher.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile

PLATFORM_BASE = "6e8ad0cc3908b9abbbc29b8dcb54c56f4f69f777"
PIPELINE_BASE = "e517a161b4c0895f8be4258fb339272e4f99e1d6"
BASE_CONFIG_SHA = "9f8cbc83cb67b8313f624913686fbdc453e7572a18603d015bc198a9a92b8883"
OUTPUT_PARENT = Path(r"D:\3Dreconstruction\Re3D-data\releases")
DEPLOYMENT_PARENT = Path(r"D:\3Dreconstruction\Re3D-data\_deployment")
EXPERIMENT_NAMES = (
    "__init__", "checkpoint", "resources", "process", "preflight", "runner",
    "high_profile", "stages", "snapshot_store", "evidence", "supervision", "long_runner",
)
CODE_FILES = tuple(f"backend/experiments/{name}.py" for name in EXPERIMENT_NAMES) + (
    "scripts/run_long_experiment.py", "scripts/run_profiled_colmap.py",
)
DOCUMENT_FILES = ("backend/experiments/README.md", "README.md", "docs/module-integration.md", "docs/progress.md",
                  "THIRD_PARTY_NOTICES.md")
CONFIG_FILES = ("performance-profile.json", "pipeline.high-cpu.json", "configuration-diff.json")
LIMITATIONS = (
    "LOCAL_TOOLPACK_RESTORATION_ONLY_NOT_SERVER_ACCEPTANCE",
    "HIGH_CPU_VALUES_REMAIN_UNVALIDATED_CANDIDATES",
    "NO_PLATFORM_ENVIRONMENT_MODELS_DATABASE_CHECKPOINT_INPUTS_OR_SECRETS_INCLUDED",
    "CLEAN_RUNTIME_PLATFORM_BASELINE_IS_NOT_THE_TOOL_SOURCE_COMMIT",
    "FULL_RUN_REQUIRES_TARGET_PREFLIGHT_AND_IDENTICAL_SOURCE_8_12_ADMISSION",
    "SAMPLING_CANNOT_GUARANTEE_INSTANTANEOUS_OOM_OR_POWER_LOSS_PROTECTION",
)


class ToolpackError(RuntimeError):
    """Stable local error code; do not expose subprocess/environment output."""


def require(condition, code):
    if not condition:
        raise ToolpackError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def ordinary_path(path, *, missing_leaf=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "TOOLPACK_ABSOLUTE_PATH_REQUIRED")
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            require(missing_leaf and part == path, "TOOLPACK_PATH_MISSING")
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 1024,
                "TOOLPACK_REPARSE_REJECTED")


def safe_name(name):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name,
            "TOOLPACK_ARCHIVE_PATH_INVALID")
    parts = name.split("/")
    require(not name.startswith("/") and all(part not in {"", ".", ".."} for part in parts),
            "TOOLPACK_ARCHIVE_PATH_INVALID")
    require(PurePosixPath(name).as_posix() == name, "TOOLPACK_ARCHIVE_PATH_INVALID")
    return name


def git_blob(repository_root, commit, name):
    safe_name(name)
    base = ["git", "-c", "safe.directory=" + repository_root.as_posix(), "-C", str(repository_root)]
    try:
        result = subprocess.run(base + ["show", commit + ":" + name], capture_output=True, timeout=15, check=False)
    except Exception:
        raise ToolpackError("TOOLPACK_GIT_READ_FAILED") from None
    require(result.returncode == 0 and 0 < len(result.stdout) <= 2 * 1024**2, "TOOLPACK_GIT_BLOB_INVALID")
    return result.stdout


def read_configurations(profile_directory):
    ordinary_path(profile_directory)
    payload, documents = {}, {}
    for name in CONFIG_FILES:
        path = profile_directory / name
        ordinary_path(path)
        require(path.is_file() and path.stat().st_size <= 1024**2, "TOOLPACK_CONFIG_INVALID")
        data = path.read_bytes()
        try:
            document = json.loads(data)
        except Exception:
            raise ToolpackError("TOOLPACK_CONFIG_INVALID") from None
        require(isinstance(document, dict), "TOOLPACK_CONFIG_INVALID")
        payload["config/" + name], documents[name] = data, document
    profile, overlay, diff = (documents[name] for name in CONFIG_FILES)
    require(profile.get("profile_kind") == "offline_high_cpu_candidate"
            and profile.get("autorun") is False and profile.get("server_applied") is False
            and profile.get("full_guarded_launcher_ready") is False,
            "TOOLPACK_CONFIG_EXECUTION_CLAIM_INVALID")
    pipeline = profile.get("pipeline", {})
    require(pipeline.get("commit") == PIPELINE_BASE and pipeline.get("baseline_config_sha256") == BASE_CONFIG_SHA
            and pipeline.get("baseline_modified") is False and pipeline.get("quality_parameters_changed") is False,
            "TOOLPACK_PIPELINE_IDENTITY_CHANGED")
    cpu = profile.get("cpu", {})
    require(all(type(cpu.get(key)) is int and cpu[key] == value for key, value in
                {"sfm_threads": 12, "mapper_ba_threads": 32, "openmvs_threads": 32, "texture_threads": 32}.items())
            and cpu.get("global_blas_omp_override") is None, "TOOLPACK_CPU_PROFILE_CHANGED")
    expected_gpu = {"task_concurrency": 1, "branches_serial": True,
            "map_outer_batch": 4, "map_dense_head_minibatch": 1, "map_use_amp": True,
            "map_requested_amp_dtype": "bf16", "mvs_source_views": 7, "mvs_height": 384,
            "mvs_width": 384, "mvs_refinement_steps": 1}
    gpu = profile.get("gpu_preserved")
    require(isinstance(gpu, dict) and set(gpu) == set(expected_gpu)
            and all(type(gpu[key]) is type(value) and gpu[key] == value for key, value in expected_gpu.items()),
            "TOOLPACK_GPU_PROFILE_CHANGED")
    expected_changes = [{"path": f"/{group}/{field}", "before": before, "after": 32}
                        for group, field, before in (("a_v4", "openmvs_threads", 16),
                            ("b_v2", "openmvs_threads", 24), ("c", "openmvs_threads", 24),
                            ("texture", "max_threads", 24))]
    require(diff.get("baseline_config_sha256") == BASE_CONFIG_SHA and diff.get("diff") == expected_changes
            and diff.get("quality_settings_changed") is False and diff.get("gpu_settings_changed") is False
            and diff.get("server_applied") is False, "TOOLPACK_CONFIGURATION_DIFF_CHANGED")
    for row in expected_changes:
        _, group, field = row["path"].split("/")
        require(isinstance(overlay.get(group), dict) and type(overlay[group].get(field)) is int
                and overlay[group][field] == 32, "TOOLPACK_OVERLAY_THREADS_CHANGED")
    # The runtime writer uses compact ASCII JSON and an explicit LF, not the
    # formatted preview bytes. Both byte identities are recorded separately.
    compact = (json.dumps(overlay, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    require(sha(compact) == profile.get("prepared_overlay_sha256"), "TOOLPACK_OVERLAY_COMPACT_HASH_MISMATCH")
    return payload, profile


def write_new(path, data):
    ordinary_path(path.parent)
    ordinary_path(path, missing_leaf=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def create_payload(root, files):
    ordinary_path(root, missing_leaf=True)
    require(not root.exists(), "TOOLPACK_TARGET_EXISTS")
    root.mkdir()
    for name, data in sorted(files.items()):
        safe_name(name)
        path = root.joinpath(*name.split("/"))
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
        write_new(path, data)


def verify_restored(root):
    ordinary_path(root)
    manifest_path = root / "bundle-manifest.json"
    ordinary_path(manifest_path)
    require(manifest_path.is_file() and manifest_path.stat().st_size <= 128 * 1024, "TOOLPACK_BUNDLE_MANIFEST_INVALID")
    manifest = json.loads(manifest_path.read_bytes())
    entries = manifest.get("files")
    require(isinstance(entries, dict) and set(entries) == {*CODE_FILES, *DOCUMENT_FILES,
            *("config/" + name for name in CONFIG_FILES), "tool-manifest.json", "README-toolpack.md"},
            "TOOLPACK_BUNDLE_MANIFEST_INVALID")
    actual = set()
    for current, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            ordinary_path(Path(current) / name)
        for name in names:
            path = Path(current) / name
            ordinary_path(path)
            actual.add(path.relative_to(root).as_posix())
    require(actual == {*entries, "bundle-manifest.json"}, "TOOLPACK_EXTRA_OR_MISSING_FILE")
    for name, identity in entries.items():
        safe_name(name)
        require(isinstance(identity, dict) and set(identity) == {"sha256", "bytes"}
                and re.fullmatch(r"[0-9a-f]{64}", str(identity.get("sha256")))
                and type(identity.get("bytes")) is int and identity["bytes"] >= 0,
                "TOOLPACK_BUNDLE_MANIFEST_INVALID")
        data = root.joinpath(*name.split("/")).read_bytes()
        require(len(data) == identity["bytes"] and sha(data) == identity["sha256"], "TOOLPACK_RESTORE_HASH_MISMATCH")
    tool = json.loads((root / "tool-manifest.json").read_bytes())
    require(isinstance(tool.get("files"), dict) and set(tool["files"]) == set(CODE_FILES), "TOOLPACK_TOOL_MANIFEST_INVALID")
    require(all(tool["files"][name] == entries[name]["sha256"] for name in CODE_FILES), "TOOLPACK_TOOL_MANIFEST_INVALID")
    require(tool.get("source_commit") == manifest.get("source_commit")
            and tool.get("expected_platform_commit") == manifest.get("target_platform_commit") == PLATFORM_BASE
            and tool.get("pipeline_commit") == manifest.get("pipeline_commit") == PIPELINE_BASE
            and tool.get("profile_sha256") == entries["config/performance-profile.json"]["sha256"],
            "TOOLPACK_MANIFEST_IDENTITY_MISMATCH")
    _, profile = read_configurations(root / "config")
    require(tool.get("prepared_overlay_sha256") == profile.get("prepared_overlay_sha256"), "TOOLPACK_MANIFEST_IDENTITY_MISMATCH")
    return {"status": "all_restored_hashes_verified", "file_count": len(actual),
            "tool_code_file_count": len(CODE_FILES), "bundle_manifest_sha256": sha(manifest_path.read_bytes())}


def archive_and_restore(payload, archive, restored):
    verified = verify_restored(payload)
    ordinary_path(archive, missing_leaf=True)
    require(not archive.exists(), "TOOLPACK_TARGET_EXISTS")
    files = sorted(path.relative_to(payload).as_posix() for path in payload.rglob("*") if path.is_file())
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED) as package:
        for name in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            package.writestr(info, payload.joinpath(*name.split("/")).read_bytes())
    restore_archive(archive, restored, expected_files=files)
    require(verify_restored(restored) == verified, "TOOLPACK_RESTORE_HASH_MISMATCH")
    return verified


def restore_archive(archive, restored, *, expected_files):
    """Validate the entire ZIP directory before writing, never extractall()."""
    ordinary_path(archive)
    ordinary_path(restored, missing_leaf=True)
    require(not restored.exists(), "TOOLPACK_TARGET_EXISTS")
    expected = set(expected_files)
    require(all(safe_name(name) for name in expected), "TOOLPACK_ARCHIVE_PATH_INVALID")
    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        names = [safe_name(info.filename) for info in infos]
        require(len(names) == len(set(names)) == len({name.casefold() for name in names})
                and set(names) == expected, "TOOLPACK_ARCHIVE_MEMBERS_INVALID")
        require(all(not info.is_dir() and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
                    and not info.flag_bits & 1 and info.file_size <= 2 * 1024**2 for info in infos),
                "TOOLPACK_ARCHIVE_MEMBER_TYPE_INVALID")
        # This fixed allowlist package cannot expand without a bounded limit.
        require(sum(info.file_size for info in infos) <= 32 * 1024**2, "TOOLPACK_ARCHIVE_SIZE_INVALID")
        data = {info.filename: package.read(info) for info in infos}
    create_payload(restored, data)


def verify_prepare_restoration(restored, platform_root, interpreter):
    """Fresh interpreter; overlay module origin proof plus forbidden probes."""
    verify_restored(restored)
    ordinary_path(platform_root)
    ordinary_path(interpreter)
    code = r'''
import builtins,importlib.util,json,pathlib,socket,subprocess,sys
root,platform=map(pathlib.Path,sys.argv[1:3])
sys.path.insert(0,str(platform))
def forbidden(*a,**k): raise AssertionError('TOOLPACK_OFFLINE_RUNTIME_ACTION_FORBIDDEN')
subprocess.run=forbidden
# Keep socket a type: stdlib ssl defines SSLSocket(socket.socket) on import.
# Block actual network operations without breaking that harmless declaration.
socket.create_connection=forbidden
socket.socket.connect=forbidden
socket.socket.connect_ex=forbidden
socket.socket.sendto=forbidden
names=('__init__','checkpoint','resources','process','preflight','runner','high_profile','stages','snapshot_store','evidence','supervision','long_runner')
original_import=builtins.__import__
origin_verified=[]
def guarded_import(name,*a,**k):
    result=original_import(name,*a,**k)
    if name=='backend.experiments.long_runner':
        runner=sys.modules[name]
        for operation in ('execute_long_test','require_admin','read_windows_state','read_queue_state','HostResourceReader','NvidiaSmiReader','runtime_gpu_check','exclusive_long_test'):
            setattr(runner,operation,forbidden)
        for item in names:
            key='backend.experiments' if item=='__init__' else 'backend.experiments.'+item
            module=sys.modules.get(key)
            if module is None or not pathlib.Path(module.__file__).resolve().is_relative_to(root.resolve()):
                raise AssertionError('TOOLPACK_OVERLAY_IMPORT_ORIGIN_INVALID')
        origin_verified.append(True)
    return result
builtins.__import__=guarded_import
spec=importlib.util.spec_from_file_location('restored_tool_bootstrap',root/'scripts/run_long_experiment.py')
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)
sys.argv=[str(root/'scripts/run_long_experiment.py'),'--profile',str(root/'config/performance-profile.json'),'--platform-root',str(platform)]
if bootstrap.main()!=0: raise AssertionError('TOOLPACK_PREPARE_RESTORE_FAILED')
if not origin_verified: raise AssertionError('TOOLPACK_OVERLAY_IMPORT_ORIGIN_INVALID')
print(json.dumps({'restored_overlay_modules_verified':len(names),'runtime_probes_called':False,'reconstruction_started':False}))
'''
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run([str(interpreter), "-B", "-c", code, str(restored), str(platform_root)],
                                capture_output=True, text=True, timeout=30, check=False, env=environment,
                                cwd=restored)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        raise ToolpackError("TOOLPACK_PREPARE_RESTORE_FAILED") from None
    require(result.returncode == 0 and len(lines) == 2 and lines[0].get("status") == "prepared_no_execution"
            and lines[0].get("server_contacted") is False
            and lines[1] == {"restored_overlay_modules_verified": len(EXPERIMENT_NAMES),
                             "runtime_probes_called": False, "reconstruction_started": False},
            "TOOLPACK_PREPARE_RESTORE_FAILED")
    # -B is deliberate: restoration must not add pycache or any runtime files.
    verify_restored(restored)
    return {"status": "prepared_no_execution", **lines[1]}


def build_toolpack(repository_root, source_commit, profile_directory, output_directory, *,
                   allowed_output_parent=None, prepare_interpreter=None):
    if allowed_output_parent is None:
        candidate_parent = Path(output_directory).parent
        require(candidate_parent in (OUTPUT_PARENT, DEPLOYMENT_PARENT), "TOOLPACK_OUTPUT_NOT_NEW_CONTROLLED_CHILD")
        allowed_output_parent = candidate_parent
    repository_root, profile_directory, output_directory, allowed_output_parent = map(Path,
        (repository_root, profile_directory, output_directory, allowed_output_parent))
    require(isinstance(source_commit, str) and re.fullmatch(r"[0-9a-f]{40}", source_commit), "TOOLPACK_FULL_SOURCE_COMMIT_REQUIRED")
    ordinary_path(repository_root)
    ordinary_path(allowed_output_parent)
    ordinary_path(output_directory, missing_leaf=True)
    require(output_directory.parent.resolve() == allowed_output_parent.resolve()
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", output_directory.name)
            and not output_directory.exists(), "TOOLPACK_OUTPUT_NOT_NEW_CONTROLLED_CHILD")
    base = ["git", "-c", "safe.directory=" + repository_root.as_posix(), "-C", str(repository_root)]
    result = subprocess.run(base + ["rev-parse", "--verify", source_commit + "^{commit}"], capture_output=True, timeout=15, check=False)
    require(result.returncode == 0 and result.stdout.decode("ascii").strip() == source_commit, "TOOLPACK_SOURCE_COMMIT_INVALID")
    files = {name: git_blob(repository_root, source_commit, name) for name in (*CODE_FILES, *DOCUMENT_FILES)}
    configurations, profile = read_configurations(profile_directory)
    files.update(configurations)
    tool_manifest = {"schema_version": "1.0", "execution_kind": "independent_long_pipeline_tool_overlay",
        "autorun": False, "server_contacted": False, "source_commit": source_commit,
        "expected_platform_commit": PLATFORM_BASE, "pipeline_commit": PIPELINE_BASE,
        "profile_sha256": sha(files["config/performance-profile.json"]),
        "prepared_overlay_sha256": profile["prepared_overlay_sha256"],
        "files": {name: sha(files[name]) for name in CODE_FILES}}
    files["tool-manifest.json"] = json_bytes(tool_manifest)
    files["README-toolpack.md"] = (
        "# Independent long-test tool overlay\n\n"
        f"Tool source commit: `{source_commit}`. Runtime platform baseline: `{PLATFORM_BASE}`.\n"
        f"Frozen pipeline: `{PIPELINE_BASE}`. These are distinct identities.\n\n"
        "No automatic execution. Install the overlay outside the clean runtime platform repository.\n"
        "Default prepare performs no host, PostgreSQL or GPU probes and starts no reconstruction.\n"
        "A local restoration check explicitly selects unpacked experiment modules before importing them; "
        "otherwise an already installed experiments package could shadow the overlay.\n\n"
        "This archive contains only 14 tool code files, explanatory documentation and 3 non-secret "
        "candidate configurations. It does not include the platform dependency baseline, Python "
        "environments, drivers, native executables, models, inputs, source checkpoints, databases or secrets.\n"
        "The formatted overlay SHA differs from the compact ASCII JSON + LF runtime overlay SHA.\n"
        "Full execution remains gated by target preflight and same-source, same-tool, same-configuration "
        "8/12-thread admission. A toolpack restoration is not a server or geometry acceptance.\n\n"
        "The bundle manifest hashes every payload file except itself; the external package receipt "
        "hashes the ZIP and the bundle manifest. Hashes detect drift, not authenticity without a trusted receipt.\n"
    ).encode("utf-8")
    bundle_manifest = {"schema_version": "1.0", "package_kind": "independent_long_tool_overlay_not_environment_image",
        "source_commit": source_commit, "target_platform_commit": PLATFORM_BASE, "pipeline_commit": PIPELINE_BASE,
        "autorun": False, "server_contacted": False, "server_deployed": False,
        "limitations": list(LIMITATIONS),
        "files": {name: {"sha256": sha(data), "bytes": len(data)} for name, data in sorted(files.items())}}
    files["bundle-manifest.json"] = json_bytes(bundle_manifest)
    # All source/configuration validation precedes creation of the exclusive root.
    output_directory.mkdir()
    payload = output_directory / "payload"
    create_payload(payload, files)
    archive = output_directory / ("re3d-long-toolpack-" + source_commit[:12] + ".zip")
    restored = output_directory / "restore-verification"
    restoration = archive_and_restore(payload, archive, restored)
    prepare = verify_prepare_restoration(restored, repository_root, Path(prepare_interpreter)) if prepare_interpreter else {
        "status": "not_requested", "reconstruction_started": False}
    receipt = {"schema_version": "1.0", "status": "packaged_and_restored",
        "source_commit": source_commit, "target_platform_commit": PLATFORM_BASE, "pipeline_commit": PIPELINE_BASE,
        "archive": archive.name, "archive_bytes": archive.stat().st_size, "archive_sha256": sha(archive.read_bytes()),
        "bundle_manifest_sha256": sha(files["bundle-manifest.json"]),
        "profile_sha256": tool_manifest["profile_sha256"], "tool_manifest_sha256": sha(files["tool-manifest.json"]),
        "prepared_overlay_sha256": profile["prepared_overlay_sha256"],
        "formatted_overlay_sha256": sha(files["config/pipeline.high-cpu.json"]),
        "restore": restoration, "offline_prepare": prepare,
        "autorun": False, "server_contacted": False, "server_deployed": False, "limitations": list(LIMITATIONS)}
    write_new(output_directory / "package-receipt.json", json_bytes(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--profile-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = build_toolpack(args.repository_root, args.source_commit, args.profile_directory,
                                args.output_directory, prepare_interpreter=Path(sys.executable))
        print(json.dumps({key: receipt[key] for key in ("status", "source_commit", "archive", "archive_sha256",
            "tool_manifest_sha256", "server_contacted", "server_deployed")}, sort_keys=True))
        return 0
    except BaseException as exc:
        code = str(exc) if isinstance(exc, ToolpackError) else "TOOLPACK_LOCAL_BUILD_FAILED"
        print(json.dumps({"status": "not_completed", "error_code": code, "server_contacted": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
