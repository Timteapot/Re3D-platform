"""Package clean experiment-branch Git blobs; no real admission or reconstruction."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

if __package__:
    from . import build_local_c_toolpack as legacy
else:
    _path = Path(__file__).absolute().with_name('build_local_c_toolpack.py')
    _spec = importlib.util.spec_from_file_location('sky_c_legacy_build_helpers', _path)
    if _spec is None or _spec.loader is None:
        raise RuntimeError('SKY_C_TOOLPACK_BUILD_HELPER_UNAVAILABLE')
    legacy = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(legacy)

common = legacy.common
ToolpackError = common.HybridToolpackError
CODE_FILES = tuple(sorted((*legacy.CODE_FILES, *(f'backend/experiments/{name}.py' for name in (
    'sky_masks', 'sky_sfm_binding', 'sky_mask_plan', 'sky_assets', 'sky_supervision', 'sky_executor',
    'c_budget_chain')), 'scripts/run_sky_c_supervised.py')))
SUPPORT_FILES = ('scripts/run_sky_mask_experiment.py',)
SKY_PROFILE = 'config/local-sky-c-only.example.json'
DOCUMENT_FILES = (legacy.PROFILE_NAME, SKY_PROFILE, 'docs/sky-mask-experiment.md',
                  'docs/local-c-only.md', 'docs/hybrid-cpu-sfm.md', 'THIRD_PARTY_NOTICES.md')
MANIFEST_NAME = 'sky-c-tool-manifest.json'
BUNDLE_NAME = 'bundle-manifest.json'
README_NAME = 'README-toolpack.md'
PAYLOAD_NAMES = frozenset((*CODE_FILES, *SUPPORT_FILES, *DOCUMENT_FILES, MANIFEST_NAME, BUNDLE_NAME, README_NAME))
BRANCH = 'experiment/local-c-only'
PIPELINE_COMMIT = legacy.PIPELINE_COMMIT
LIMITATIONS = (
    'OFFLINE_TOOL_RESTORE_IS_NOT_NATIVE_SKY_C_RECONSTRUCTION_ACCEPTANCE',
    '31_SEALED_CODE_FILES_PLUS_ONE_UNSEALED_INPUT_PREPARATION_SUPPORT_SCRIPT',
    'NO_ENVIRONMENT_NATIVE_BINARY_MODEL_PHOTO_DATABASE_RESULT_BUDGET_LEDGER_OR_SECRET_INCLUDED',
    'UNBOUND_EXAMPLES_ARE_NOT_EXECUTION_AUTHORIZATION_OR_BUDGET_ALLOWANCE',
    'SERVER_UAC_REAL_RECONSTRUCTION_AND_BUDGET_MUTATION_NOT_PERFORMED',
)


def require(value, code):
    common._require(value, 'SKY_C_TOOLPACK_' + code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


json_bytes = common._json_bytes
read_document = legacy.read_document


def verify_sky_template(raw):
    value = read_document(raw)
    require(set(value) == {'schema_version', 'profile_kind', 'autorun', 'c_profile', 'mask_binding',
                           'prepared_admission', 'budget_lineage'}
            and value['schema_version'] == '1.0'
            and value['profile_kind'] == 'local_sky_c_supervised_candidate'
            and value['autorun'] is False, 'UNBOUND_TEMPLATE_REQUIRED')
    legacy.verify_template(json_bytes(value['c_profile']))
    for key in ('mask_binding', 'prepared_admission'):
        row = value[key]
        require(type(row) is dict and set(row) == {'path', 'sha256'} and row['sha256'] is None
                and type(row['path']) is str and row['path'].startswith('D:/REPLACE-WITH-VERIFIED-SKY-'),
                'UNBOUND_TEMPLATE_REQUIRED')
    require(value['budget_lineage'] == {key: None for key in ('root_profile_sha256', 'root_claim_sha256',
        'root_charge_sha256', 'expected_tip_sha256', 'maximum_remaining_seconds')}, 'UNBOUND_TEMPLATE_REQUIRED')
    require(value['c_profile']['budget'] == {'original_total_seconds': 14400, 'consumed_sfm_seconds': None,
        'remaining_seconds': None, 'source_terminal_receipt_sha256': None}, 'UNBOUND_TEMPLATE_REQUIRED')


def source_identity(repository, source_commit):
    head = common._head_identity(repository, source_commit)
    require(common._git(repository, ['symbolic-ref', '--quiet', '--short', 'HEAD']).decode().strip() == BRANCH,
            'EXPERIMENT_BRANCH_REQUIRED')
    return head


def committed_files(repository, head):
    requested = (*CODE_FILES, *SUPPORT_FILES, *DOCUMENT_FILES)
    records = {}
    for row in common._git(repository, ['ls-tree', '-z', head, '--', *requested]).split(b'\0'):
        if not row: continue
        metadata, separator, raw_name = row.partition(b'\t')
        fields, name = metadata.split(b' '), raw_name.decode('utf-8')
        common._safe_relative(name)
        require(separator == b'\t' and len(fields) == 3 and fields[0] in (b'100644', b'100755')
                and fields[1] == b'blob' and common._COMMIT.fullmatch(fields[2].decode())
                and name in requested and name not in records, 'REGULAR_GIT_BLOB_REQUIRED')
        records[name] = fields[2]
    require(set(records) == set(requested), 'REGULAR_GIT_BLOB_REQUIRED')
    names = sorted(records)
    commands = b''.join(records[name] + b'\n' for name in names)
    summaries = common._git(repository, ['cat-file', '--batch-check'], input_bytes=commands).splitlines()
    require(len(summaries) == len(names), 'GIT_OPERATION_FAILED')
    sizes = []
    for name, summary in zip(names, summaries):
        fields = summary.split(b' ')
        require(len(fields) == 3 and fields[:2] == [records[name], b'blob'] and fields[2].isdigit(),
                'GIT_OPERATION_FAILED')
        size = int(fields[2]); require(0 < size <= common.MAX_FILE_BYTES, 'TEXT_BLOB_INVALID'); sizes.append(size)
    require(sum(sizes) <= common.MAX_ARCHIVE_BYTES, 'ARCHIVE_SIZE_INVALID')
    raw = common._git(repository, ['cat-file', '--batch'], input_bytes=commands)
    cursor, files = 0, {}
    for name, size in zip(names, sizes):
        header = records[name] + b' blob ' + str(size).encode() + b'\n'
        require(raw[cursor:cursor + len(header)] == header, 'GIT_OPERATION_FAILED')
        cursor += len(header)
        blob = raw[cursor:cursor + size]; cursor += size
        require(len(blob) == size and raw[cursor:cursor + 1] == b'\n', 'GIT_OPERATION_FAILED'); cursor += 1
        files[name] = common._normalize_blob(blob)
    require(cursor == len(raw), 'GIT_OPERATION_FAILED')
    return files


def payload(repository, source_commit=None):
    head = source_identity(repository, source_commit)
    files = committed_files(repository, head)
    legacy.verify_template(files[legacy.PROFILE_NAME]); verify_sky_template(files[SKY_PROFILE])
    require(len(CODE_FILES) == 31, 'CODE_CLOSURE_CHANGED')
    files[MANIFEST_NAME] = json_bytes({'schema_version': '1.0', 'kind': 'local_sky_c_supervised_tools',
        'autorun': False, 'source_state': 'git_commit', 'source_commit': head,
        'files': {name: sha(files[name]) for name in CODE_FILES}})
    files[README_NAME] = (
        '# Temporary post-SfM sky-mask C toolpack\n\n'
        f'Source: clean `{BRANCH}` HEAD `{head}`; normalized Git-blob UTF-8/LF bytes.\n'
        'The 31 sealed runtime files are pinned by sky-c-tool-manifest.json. The separate\n'
        'run_sky_mask_experiment.py support script is included and hashed by the bundle,\n'
        'but is not an allowed module in the sealed backend closure. Both default entries\n'
        'are no-ops. No environment, native EXE/DLL, photograph, learned model, database,\n'
        'SfM/checkpoint/result, private profile, ledger, .env or credential is included.\n\n'
        'This release has not passed real native masked C reconstruction acceptance.\n'
        'Offline unpack/default-entry verification is not runtime, GPU, capacity, visual\n'
        'or geometry acceptance. Existing SfM was unmasked; sky applies only after SfM.\n'
        'Examples are intentionally unbound. Do not use their numbers as fresh allowance.\n'
        'Actual execution requires separate authorization, a new scene, pinned machine\n'
        'resources and the entire authoritative cumulative budget chain. Packaging never\n'
        'elevates, contacts a server or claims compute budget. main and frozen algorithms\n'
        'are not changed by this archive. Read docs/sky-mask-experiment.md and notices.\n'
        'Trust the external receipt separately: hashes detect drift, not authenticity.\n'
    ).encode()
    files[BUNDLE_NAME] = json_bytes({'schema_version': '1.0', 'source_commit': head,
        'package_kind': 'local_sky_c_tool_overlay_not_environment_image', 'experiment_branch': BRANCH,
        'pipeline_commit': PIPELINE_COMMIT, 'release_channel': 'temporary_experiment',
        'code_file_count': 31, 'support_file_count': 1, 'real_native_sky_c_accepted': False,
        'autorun': False, 'server_contacted': False, 'reconstruction_started': False,
        'limitations': list(LIMITATIONS),
        'files': {name: {'sha256': sha(data), 'bytes': len(data)} for name, data in sorted(files.items())}})
    require(source_identity(repository, head) == head, 'SOURCE_CHANGED')
    return head, files


def validate_files(files, expected_hashes):
    require(set(files) == set(expected_hashes) == PAYLOAD_NAMES, 'EXTRA_OR_MISSING_FILE')
    for name, raw in files.items():
        common._safe_relative(name)
        require(type(expected_hashes[name]) is str and common._SHA.fullmatch(expected_hashes[name])
                and 0 < len(raw) <= common.MAX_FILE_BYTES and sha(raw) == expected_hashes[name],
                'RESTORE_HASH_MISMATCH')
    bundle = read_document(files[BUNDLE_NAME])
    head = bundle.get('source_commit')
    require(isinstance(head, str) and common._COMMIT.fullmatch(head), 'BUNDLE_MANIFEST_INVALID')
    require(bundle == {'schema_version': '1.0', 'source_commit': head,
        'package_kind': 'local_sky_c_tool_overlay_not_environment_image', 'experiment_branch': BRANCH,
        'pipeline_commit': PIPELINE_COMMIT, 'release_channel': 'temporary_experiment',
        'code_file_count': 31, 'support_file_count': 1, 'real_native_sky_c_accepted': False,
        'autorun': False, 'server_contacted': False, 'reconstruction_started': False,
        'limitations': list(LIMITATIONS), 'files': {name: {'sha256': expected_hashes[name], 'bytes': len(files[name])}
                for name in files if name != BUNDLE_NAME}}, 'BUNDLE_MANIFEST_INVALID')
    require(read_document(files[MANIFEST_NAME]) == {'schema_version': '1.0',
        'kind': 'local_sky_c_supervised_tools', 'autorun': False, 'source_state': 'git_commit', 'source_commit': head,
        'files': {name: expected_hashes[name] for name in CODE_FILES}}, 'TOOL_MANIFEST_INVALID')
    legacy.verify_template(files[legacy.PROFILE_NAME]); verify_sky_template(files[SKY_PROFILE])
    return {'status': 'all_restored_hashes_verified', 'file_count': len(files), 'code_file_count': 31,
        'support_file_count': 1, 'source_commit': head, 'tool_manifest_sha256': expected_hashes[MANIFEST_NAME],
        'bundle_manifest_sha256': expected_hashes[BUNDLE_NAME]}


def verify_payload(root, expected_hashes):
    root = common._checked_path(root)
    files, directories = {}, set()
    require(root.is_dir() and set(expected_hashes) == PAYLOAD_NAMES, 'PAYLOAD_DIRECTORY_OR_ALLOWLIST_INVALID')
    for current, children, names in os.walk(root, followlinks=False):
        for name in children:
            path = common._checked_path(Path(current) / name)
            directories.add(common._safe_relative(path.relative_to(root).as_posix()))
        for name in names:
            path = common._checked_path(Path(current) / name)
            relative = common._safe_relative(path.relative_to(root).as_posix())
            require(path.is_file() and relative in expected_hashes
                    and path.stat().st_size <= common.MAX_FILE_BYTES, 'EXTRA_OR_MISSING_FILE')
            files[relative] = path.read_bytes()
    allowed = {p.as_posix() for name in expected_hashes for p in Path(name).parents if p.as_posix() != '.'}
    require(directories <= allowed, 'EXTRA_OR_MISSING_FILE')
    return validate_files(files, expected_hashes)


def restore_archive(archive, target, *, expected_hashes):
    archive, target = common._checked_path(archive), common._checked_path(target, missing_leaf=True)
    require(not target.exists(), 'TARGET_EXISTS')
    require(set(expected_hashes) == PAYLOAD_NAMES, 'PAYLOAD_ALLOWLIST_INVALID')
    require(archive.is_file() and archive.stat().st_size <= common.MAX_ARCHIVE_BYTES, 'ARCHIVE_SIZE_INVALID')
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist(); names = [common._safe_relative(info.filename) for info in entries]
        require(len(names) == len(set(names)) == len({name.casefold() for name in names})
                and set(names) == PAYLOAD_NAMES, 'ARCHIVE_MEMBERS_INVALID')
        require(all(not info.is_dir() and not info.flag_bits & 1
                    and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
                    and 0 < info.file_size <= common.MAX_FILE_BYTES for info in entries)
                and sum(info.file_size for info in entries) <= common.MAX_ARCHIVE_BYTES, 'ARCHIVE_MEMBER_TYPE_INVALID')
        files = {info.filename: package.read(info) for info in entries}
    validate_files(files, expected_hashes)
    common._new_payload(target, files)
    return verify_payload(target, expected_hashes)


def default_entry_probes(root):
    """Run only the two restored no-argument entrypoints in isolated Python."""
    results = []
    env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}
           and not key.startswith(('APP_', 'DATABASE_', 'JWT_', 'AUTH_', 'SMTP_', 'ACCESS_TOKEN_', 'REFRESH_', 'VITE_'))}
    for entry, status in (('scripts/run_sky_c_supervised.py', 'prepared_not_executed'),
                          ('scripts/run_sky_mask_experiment.py', 'not_executed')):
        try:
            process = subprocess.run([sys.executable, '-I', '-B', str(root / entry)], cwd=root,
                env=env, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ToolpackError('SKY_C_TOOLPACK_DEFAULT_ENTRY_FAILED') from None
        require(process.returncode == 0 and len(process.stdout) <= 8192 and len(process.stderr) <= 8192,
                'DEFAULT_ENTRY_FAILED')
        report = read_document(process.stdout)
        require(report.get('status') == status and report.get('reconstruction_started') is False
                and report.get('execution_authorized') is False
                and report.get('budget_claimed', False) is False and report.get('budget_charged', False) is False,
                'DEFAULT_ENTRY_NOT_NOOP')
        results.append({'entrypoint': entry, 'isolated_python': True, 'return_code': 0,
            'reported_status': status, 'reported_execution_authorized': False,
            'reported_reconstruction_started': False, 'reported_budget_claimed': False})
    return results


def prepare_toolpack(repository_root, source_commit=None):
    head, files = payload(Path(repository_root), source_commit)
    return {'status': 'prepared_no_execution', 'source_commit': head, 'experiment_branch': BRANCH,
        'code_file_count': 31, 'support_file_count': 1, 'payload_file_count': len(files),
        'tool_manifest_sha256': sha(files[MANIFEST_NAME]), 'bundle_manifest_sha256': sha(files[BUNDLE_NAME]),
        'autorun': False, 'server_contacted': False, 'reconstruction_started': False}


def build_toolpack(repository_root, source_commit, output_dir):
    repository, output = common._checked_path(repository_root), common._checked_path(output_dir, missing_leaf=True)
    common._safe_relative(output.name)
    require(not output.exists(), 'TARGET_EXISTS')
    require(not output.is_relative_to(repository) and not repository.is_relative_to(output), 'SOURCE_OUTPUT_OVERLAP')
    head, files = payload(repository, source_commit)
    hashes = {name: sha(raw) for name, raw in files.items()}
    validate_files(files, hashes)
    output.mkdir()
    common._new_payload(output / 'payload', files)
    archive = output / ('re3d-sky-c-toolpack-' + head[:12] + '.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as package:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)); info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16; info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, raw)
    restored_root = output / 'restore-verification'
    restored = restore_archive(archive, restored_root, expected_hashes=hashes)
    probes = default_entry_probes(restored_root)
    # No-op probes may not mutate even a single packaged byte or add pyc/files.
    require(verify_payload(restored_root, hashes) == restored, 'DEFAULT_ENTRY_CHANGED_PAYLOAD')
    receipt = {'schema_version': '1.0', 'status': 'packaged_and_restored', 'source_commit': head,
        'experiment_branch': BRANCH, 'release_channel': 'temporary_experiment', 'code_file_count': 31,
        'support_file_count': 1, 'payload_file_count': len(files), 'source_state': 'git_commit',
        'archive': archive.name, 'archive_bytes': archive.stat().st_size, 'archive_sha256': sha(archive.read_bytes()),
        'tool_manifest_sha256': hashes[MANIFEST_NAME], 'bundle_manifest_sha256': hashes[BUNDLE_NAME],
        'payload_hashes': hashes, 'payload_files': {name: {'sha256': hashes[name], 'bytes': len(raw)}
            for name, raw in sorted(files.items())}, 'restore': restored, 'default_entry_probes': probes,
        'autorun': False, 'server_contacted': False, 'reconstruction_started': False,
        'budget_claimed': False, 'budget_charged': False, 'uac_requested': False,
        'server_deployed': False, 'real_native_sky_c_accepted': False,
        'contains_environment_or_data': False, 'line_endings': 'git_blob_utf8_lf', 'limitations': list(LIMITATIONS)}
    common._write_new(output / 'package-receipt.json', json_bytes(receipt))
    return receipt


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolpackError('SKY_C_TOOLPACK_ARGUMENTS_INVALID')


def main(argv=None):
    try:
        parser = Parser(description=__doc__, allow_abbrev=False)
        parser.add_argument('--repository-root', type=Path, required=True)
        parser.add_argument('--source-commit')
        parser.add_argument('--output-dir', type=Path)
        parser.add_argument('--build-toolpack', action='store_true')
        args = parser.parse_args(argv)
        if args.build_toolpack:
            require(args.output_dir is not None, 'OUTPUT_REQUIRED')
            value = build_toolpack(args.repository_root, args.source_commit, args.output_dir)
            value = {key: value[key] for key in ('status', 'source_commit', 'archive', 'archive_sha256',
                'tool_manifest_sha256', 'bundle_manifest_sha256', 'reconstruction_started', 'budget_claimed')}
        else:
            require(args.output_dir is None, 'OUTPUT_REQUIRES_EXPLICIT_BUILD')
            value = prepare_toolpack(args.repository_root, args.source_commit)
        print(json.dumps(value, sort_keys=True)); return 0
    except Exception as error:
        print(json.dumps({'status': 'not_completed', 'error_code': str(error) if isinstance(error, ToolpackError)
            else 'SKY_C_TOOLPACK_BUILD_FAILED', 'reconstruction_started': False, 'budget_claimed': False}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
