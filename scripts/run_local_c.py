"""Local C-only tools. No arguments prepares nothing and starts no process.

seal-tools writes a new code manifest, seal-runtime a new native dependency
manifest. inspect only checks fixed dependencies. preflight checks pinned source,
runtime and host snapshots without writes, children or execution permission.
execute needs pinned manifests,
a verified SfM bundle and the exact --execute-c-only flag; it never requests UAC.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

SHA = re.compile(r'[0-9a-f]{64}\Z')
TOOL_MANIFEST = 'c-only-tool-manifest.json'
CODE_FILES = frozenset({
    *(f'backend/experiments/{name}.py' for name in (
        '__init__', 'checkpoint', 'hybrid_checkpoint', 'resources', 'process', 'preflight',
        'runner', 'high_profile', 'stages', 'snapshot_store', 'evidence', 'supervision',
        'long_runner', 'cpu_runner', 'hybrid_handoff', 'c_profile', 'c_stages', 'c_integrity', 'c_runner')),
    'backend/monitoring/resources.py', 'backend/re3d_adapter/errors.py',
    'scripts/run_local_c.py', 'scripts/run_hybrid_experiment.py',
})
VC17 = 'vendor/openmvs-2.4.0-windows/vc17/x64/Release'
VC18 = 'vendor/openmvs-2.4.0-33d9484-windows/vc18/x64/Release'
EXECUTABLES = (VC17 + '/InterfaceCOLMAP.exe', VC17 + '/DensifyPointCloud.exe',
               VC17 + '/ReconstructMesh.exe', VC18 + '/TextureMesh.exe')
SCRIPTS = ('extract_colmap_cameras.py', 'prepare_openmvs_texture_input.py',
           'normalize_openmvs_output.py', 'validate_outputs.py')


class CLIError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def stable_public_error_code(value, *, fallback='LOCAL_C_ENTRY_FAILED'):
    """Only a bounded structured identifier may cross the public log boundary.

    Do not derive codes from exception text: it may contain paths, credentials,
    or a traceback. Strings are accepted only as already structured codes.
    """
    pattern = r'(?:C_OUTPUT|C_STAGE|C_ONLY|C_WRAPPER|EXPERIMENT|LOCAL_C|LOW|RESOURCE|EVIDENCE|LONG|HYBRID_CLI|HANDOFF|CHECKPOINT|CPU|SFM|HOST_RESOURCE|DIRECTORY_RESOURCE|GPU_RESOURCE)_[A-Z][A-Z0-9_]*'
    def valid(code):
        return type(code) is str and len(code) <= 96 and re.fullmatch(pattern, code) is not None
    safe_fallback = fallback if valid(fallback) else 'LOCAL_C_ENTRY_FAILED'
    try:
        code = value if type(value) is str else getattr(value, 'code', None)
    except BaseException:
        return safe_fallback
    return code if valid(code) else safe_fallback


def require(condition, code):
    if not condition: raise CLIError(code)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError('LOCAL_C_ARGUMENTS_INVALID')


def plain(path, *, missing=False):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts
            and not str(path).startswith(('\\\\', '//'))
            and not any(':' in part for part in path.parts[1:]), 'LOCAL_C_PATH_INVALID')
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try: info = cursor.lstat()
        except FileNotFoundError:
            require(missing and cursor == path, 'LOCAL_C_PATH_MISSING')
            return path
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 1024,
                'LOCAL_C_REPARSE_REJECTED')
    return path


def sha(path):
    with plain(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path, *, expected_sha256=None):
    path = plain(path)
    require(path.is_file() and path.stat().st_size <= 4 * 1024**2, 'LOCAL_C_DOCUMENT_INVALID')
    def unique(rows):
        value = {}
        for key, item in rows:
            require(key not in value, 'LOCAL_C_DUPLICATE_KEY')
            value[key] = item
        return value
    raw = path.read_bytes()
    require(len(raw) <= 4 * 1024**2, 'LOCAL_C_DOCUMENT_INVALID')
    if expected_sha256 is not None:
        require(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256)
                and hashlib.sha256(raw).hexdigest() == expected_sha256, 'LOCAL_C_DOCUMENT_HASH_CHANGED')
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    require(type(value) is dict, 'LOCAL_C_DOCUMENT_INVALID')
    return value


def write_new(path, value):
    plain(path, missing=True)
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    return sha(path)


def seal_tools(root):
    root = plain(root)
    return write_new(root / TOOL_MANIFEST, {'schema_version': '1.0',
        'kind': 'local_c_only_tools', 'autorun': False, 'source_state': 'uncommitted_candidate',
        'files': {name: sha(root / name) for name in sorted(CODE_FILES)}})


def load_backend(root, pin):
    root = plain(root)
    manifest = root / TOOL_MANIFEST
    require(isinstance(pin, str) and SHA.fullmatch(pin) and sha(manifest) == pin,
            'LOCAL_C_TOOL_MANIFEST_CHANGED')
    value = read(manifest, expected_sha256=pin)
    development_fields = {'schema_version', 'kind', 'autorun', 'source_state', 'files'}
    published = (set(value) == development_fields | {'source_commit'}
                 and value.get('source_state') == 'git_commit'
                 and isinstance(value.get('source_commit'), str)
                 and re.fullmatch(r'[0-9a-f]{40}', value['source_commit']) is not None)
    candidate = set(value) == development_fields and value.get('source_state') == 'uncommitted_candidate'
    require((candidate or published)
            and value['schema_version'] == '1.0' and value['kind'] == 'local_c_only_tools'
            and value['autorun'] is False
            and type(value['files']) is dict and set(value['files']) == CODE_FILES,
            'LOCAL_C_TOOL_MANIFEST_INVALID')
    for name, expected in value['files'].items():
        require(isinstance(expected, str) and SHA.fullmatch(expected) and sha(root / name) == expected,
                'LOCAL_C_TOOL_HASH_CHANGED')
    # Reuse the sealed-source loader. Its manifest is deliberately not the old
    # CPU manifest: bootstrap's verifier is supplied only this already checked
    # exact closure and rejects unsealed backend/script imports and cached pyc.
    path = root / 'scripts/run_hybrid_experiment.py'
    source = path.read_bytes()
    require(hashlib.sha256(source).hexdigest() == value['files']['scripts/run_hybrid_experiment.py'],
            'LOCAL_C_TOOL_HASH_CHANGED')
    from types import ModuleType
    module = ModuleType('local_c_pinned_bootstrap')
    module.__file__ = str(path)
    exec(compile(source, str(path), 'exec'), module.__dict__)
    module.CODE_FILES = CODE_FILES
    module._VERIFIED_TOOLS[str(root)] = (pin, dict(value['files']))
    original_sha = module.sha_file
    # bootstrap checks its named old manifest; bind only that check to the new
    # manifest, without constructing a fake CPU-toolpack file on disk.
    module.sha_file = lambda p: original_sha(manifest if Path(p) == root / 'hybrid-tool-manifest.json' else p)
    require(sha(manifest) == pin, 'LOCAL_C_TOOL_MANIFEST_CHANGED')
    module.bootstrap_namespace(root)
    import importlib
    return importlib.import_module('backend.experiments.c_runner')


def runtime_files(pipeline_root):
    root = plain(pipeline_root)
    names = set(EXECUTABLES) | {'scripts/' + name for name in SCRIPTS}
    for folder in (VC17, VC18):
        for file in plain(root / folder).iterdir():
            if file.suffix.casefold() == '.dll':
                plain(file)
                names.add(file.relative_to(root).as_posix())
    paths = read(root / 'configs/paths.local.json')
    require(type(paths.get('mapanything_python')) is str, 'LOCAL_C_PYTHON_REQUIRED')
    interpreter = Path(paths['mapanything_python'])
    interpreter = interpreter if interpreter.is_absolute() else root / interpreter
    plain(interpreter)
    return sorted(names), interpreter


def seal_runtime(root, output):
    names, interpreter = runtime_files(root)
    # Preserve complete DLL closures; do not copy or hash learned model weights.
    files = {name: sha(root / name) for name in names}
    # Prevent silently admitting a replacement CUDA build under gpu_required=False.
    for name in EXECUTABLES:
        if name.endswith(('DensifyPointCloud.exe', 'TextureMesh.exe')):
            raw = (root / name).read_bytes()
            require(b'cuda-device' not in raw and 'cuda-device'.encode('utf-16-le') not in raw,
                    'LOCAL_C_NONCPU_BINARY_REJECTED')
    return write_new(output, {'schema_version': '1.0', 'kind': 'local_c_cpu_runtime',
        'pipeline_root': str(root), 'python': str(interpreter), 'python_sha256': sha(interpreter),
        'cpu_build_static_check_only': True, 'files': files})


def verify_runtime_manifest(path, pin, root):
    require(isinstance(pin, str) and SHA.fullmatch(pin) and sha(path) == pin,
            'LOCAL_C_RUNTIME_MANIFEST_CHANGED')
    value = read(path, expected_sha256=pin)
    require(set(value) == {'schema_version', 'kind', 'pipeline_root', 'python', 'python_sha256',
                          'cpu_build_static_check_only', 'files'}
            and value['schema_version'] == '1.0' and value['kind'] == 'local_c_cpu_runtime'
            and Path(value['pipeline_root']) == root and value['cpu_build_static_check_only'] is True,
            'LOCAL_C_RUNTIME_MANIFEST_INVALID')
    names, interpreter = runtime_files(root)
    require(value['python'] == str(interpreter) and sha(interpreter) == value['python_sha256']
            and type(value['files']) is dict and set(value['files']) == set(names), 'LOCAL_C_RUNTIME_IDENTITY_CHANGED')
    observed_signatures = {}
    for name, expected in value['files'].items():
        before = signature(root / name)
        require(isinstance(expected, str) and SHA.fullmatch(expected) and sha(root / name) == expected,
                'LOCAL_C_RUNTIME_HASH_CHANGED')
        require(signature(root / name) == before, 'LOCAL_C_RUNTIME_CHANGED_DURING_ADMISSION')
        observed_signatures[name] = before
    for name in EXECUTABLES:
        if name.endswith(('DensifyPointCloud.exe', 'TextureMesh.exe')):
            raw = (root / name).read_bytes()
            require(b'cuda-device' not in raw and 'cuda-device'.encode('utf-16-le') not in raw,
                    'LOCAL_C_NONCPU_BINARY_REJECTED')
    require(all(signature(root / name) == saved for name, saved in observed_signatures.items()),
            'LOCAL_C_RUNTIME_CHANGED_DURING_ADMISSION')
    value['_stat_signatures'] = observed_signatures
    return value


def signature(path):
    info = plain(path).stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def verify_plan_runtime(plan, value):
    root, interpreter = Path(value['pipeline_root']), Path(value['python'])
    allowed = {root / name for name in EXECUTABLES}
    for stage in plan.stages:
        exe = Path(stage.argv[0])
        require(exe == interpreter or exe in allowed, 'LOCAL_C_PLAN_RUNTIME_CHANGED')
        if exe == interpreter:
            require(Path(stage.argv[1]) in {root / 'scripts' / name for name in SCRIPTS},
                    'LOCAL_C_PLAN_RUNTIME_CHANGED')


def verify_runtime_stage(stage, value):
    root, interpreter = Path(value['pipeline_root']), Path(value['python'])
    names, configured_python = runtime_files(root)
    require(set(names) == set(value['files']) and configured_python == interpreter,
            'LOCAL_C_RUNTIME_CHANGED_AFTER_ADMISSION')
    # DLL contents were hashed once at admission. Reject changed identity,
    # size/mtime or reparses later, without repeatedly rehashing the closure.
    for name, saved in value['_stat_signatures'].items():
        require(signature(root / name) == saved, 'LOCAL_C_RUNTIME_CHANGED_AFTER_ADMISSION')
    exe = Path(stage.argv[0])
    if exe == interpreter:
        require(sha(interpreter) == value['python_sha256'], 'LOCAL_C_RUNTIME_HASH_CHANGED')
        helper = Path(stage.argv[1]).relative_to(root).as_posix()
        require(sha(root / helper) == value['files'][helper], 'LOCAL_C_RUNTIME_HASH_CHANGED')
    else:
        name = exe.relative_to(root).as_posix()
        require(sha(exe) == value['files'][name], 'LOCAL_C_RUNTIME_HASH_CHANGED')


def main(argv=None, *, tool_root=None):
    try:
        parser = Parser(description=__doc__, allow_abbrev=False)
        parser.add_argument('--mode', choices=('prepare', 'inspect', 'seal-tools', 'seal-runtime', 'preflight', 'execute'), default='prepare')
        parser.add_argument('--profile', type=Path)
        parser.add_argument('--profile-sha256')
        parser.add_argument('--pipeline-root', type=Path, default=Path(r'D:\3Dreconstruction\Re3D'))
        parser.add_argument('--tool-manifest-sha256')
        parser.add_argument('--runtime-manifest', type=Path)
        parser.add_argument('--runtime-manifest-sha256')
        parser.add_argument('--source-cpu-profile', type=Path)
        parser.add_argument('--source-cpu-profile-sha256')
        parser.add_argument('--terminal-receipt', type=Path)
        parser.add_argument('--execute-c-only', action='store_true')
        args = parser.parse_args(argv)
        root = Path(tool_root) if tool_root else Path(__file__).absolute().parents[1]
        require(args.mode == 'execute' or not args.execute_c_only, 'LOCAL_C_EXECUTION_MODE_MISMATCH')
        execution_options = (args.profile_sha256, args.source_cpu_profile, args.source_cpu_profile_sha256,
                             args.terminal_receipt, args.runtime_manifest_sha256)
        require(args.mode in ('preflight', 'execute') or not any(value is not None for value in execution_options),
                'LOCAL_C_ARGUMENT_MODE_MISMATCH')
        require(args.mode in ('seal-runtime', 'preflight', 'execute') or args.runtime_manifest is None,
                'LOCAL_C_ARGUMENT_MODE_MISMATCH')
        require(args.mode in ('prepare', 'preflight', 'execute') or args.profile is None,
                'LOCAL_C_ARGUMENT_MODE_MISMATCH')
        require(args.mode in ('inspect', 'preflight', 'execute') or args.tool_manifest_sha256 is None,
                'LOCAL_C_ARGUMENT_MODE_MISMATCH')
        if args.mode == 'prepare':
            # No backend, host, input, UAC, runtime or process dependency here.
            if args.profile is not None: read(args.profile)
            print(json.dumps({'status': 'prepared_not_executed', 'reconstruction_started': False,
                              'profile_bound_and_admitted': False}))
            return 0
        if args.mode == 'seal-tools':
            print(json.dumps({'status': 'sealed_not_executed', 'tool_manifest_sha256': seal_tools(root)}))
            return 0
        if args.mode == 'seal-runtime':
            require(args.runtime_manifest is not None, 'LOCAL_C_RUNTIME_OUTPUT_REQUIRED')
            print(json.dumps({'status': 'sealed_not_executed', 'runtime_manifest_sha256':
                              seal_runtime(args.pipeline_root, args.runtime_manifest)}))
            return 0
        if args.mode == 'preflight':
            require(all(value is not None for value in (args.profile, args.profile_sha256,
                args.tool_manifest_sha256, args.terminal_receipt, args.source_cpu_profile,
                args.source_cpu_profile_sha256, args.runtime_manifest, args.runtime_manifest_sha256)),
                'LOCAL_C_PREFLIGHT_ARGUMENTS_REQUIRED')
        runner = load_backend(root, args.tool_manifest_sha256)
        if args.mode == 'inspect':
            from backend.experiments.c_integrity import inspect_requirements
            requirements = [{'name': name, 'path': str(args.pipeline_root / name), 'expected_kind': 'file'}
                            for name in (*EXECUTABLES, *('scripts/' + s for s in SCRIPTS))]
            paths = read(args.pipeline_root / 'configs/paths.local.json')
            interpreter = Path(paths['mapanything_python'])
            interpreter = interpreter if interpreter.is_absolute() else args.pipeline_root / interpreter
            requirements.append({'name': 'mapanything_python', 'path': str(interpreter), 'expected_kind': 'file'})
            observed = inspect_requirements(requirements)
            print(json.dumps(observed, ensure_ascii=True))
            return 0 if observed['status'] == 'observed_requirements_present' else 1
        if args.mode == 'preflight':
            result = runner.preflight_local_c(args, read(args.profile, expected_sha256=args.profile_sha256),
                                              tool_identity=args.tool_manifest_sha256)
            if result.get('error_code') is not None:
                result['error_code'] = stable_public_error_code(result['error_code'])
            print(json.dumps(result, ensure_ascii=True, allow_nan=False))
            return 0 if result.get('status') == 'read_only_preflight_passed' else 1
        require(args.execute_c_only and all(value is not None for value in (args.profile, args.terminal_receipt,
            args.source_cpu_profile, args.runtime_manifest, args.runtime_manifest_sha256,
            args.source_cpu_profile_sha256, args.profile_sha256)), 'LOCAL_C_EXPLICIT_EXECUTION_REQUIRED')
        require(SHA.fullmatch(args.profile_sha256) and sha(args.profile) == args.profile_sha256,
                'LOCAL_C_PROFILE_CHANGED')
        result = runner.execute_local_c(args, read(args.profile, expected_sha256=args.profile_sha256),
                                        tool_identity=args.tool_manifest_sha256)
        if result.get('error_code') is not None:
            result['error_code'] = stable_public_error_code(result['error_code'])
        print(json.dumps({key: result[key] for key in ('status', 'error_code', 'elapsed_seconds',
            'remaining_budget_seconds', 'process_tree_stopped', 'terminal_evidence_confirmed',
            'source_handoff_unchanged') if key in result}, ensure_ascii=True, allow_nan=False))
        return 0 if result.get('status') == 'succeeded' else 1
    except BaseException as exc:
        code = stable_public_error_code(exc)
        print(json.dumps({'status': 'blocked', 'error_code': code, 'reconstruction_started': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
