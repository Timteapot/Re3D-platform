"""Offline release tests in synthetic Git repositories, never private state."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

from scripts import build_sky_c_toolpack as build


def template():
    base = {'schema_version': '1.0', 'profile_kind': 'local_c_only_candidate', 'autorun': False,
        'execution': {'scope': 'local-only', 'branches': ['c'], 'gpu_required': False,
            'platform_job_resume': False, 'openmvs_mode': 'cpu-only-vendor-build'},
        'pipeline': {'commit': build.PIPELINE_COMMIT},
        'source': {'root': 'D:/REPLACE-WITH-VERIFIED-BUNDLE', 'cpu_result_sha256': None,
            'terminal_receipt_sha256': None, 'handoff_manifest_sha256': None},
        'target_hardware': {}, 'target_experiment': {}, 'cpu': {}, 'protection': {},
        'budget': {'original_total_seconds': 14400, 'consumed_sfm_seconds': None, 'remaining_seconds': None,
            'source_terminal_receipt_sha256': None}}
    return {'schema_version': '1.0', 'profile_kind': 'local_sky_c_supervised_candidate', 'autorun': False,
        'c_profile': base, 'mask_binding': {'path': 'D:/REPLACE-WITH-VERIFIED-SKY-BINDING.json', 'sha256': None},
        'prepared_admission': {'path': 'D:/REPLACE-WITH-VERIFIED-SKY-ADMISSION.json', 'sha256': None},
        'budget_lineage': {key: None for key in ('root_profile_sha256', 'root_claim_sha256', 'root_charge_sha256',
            'expected_tip_sha256', 'maximum_remaining_seconds')}}


@pytest.fixture
def case(tmp_path_factory):
    root = tmp_path_factory.mktemp('sp')
    repository = root / 'repo'; repository.mkdir()
    def git(*args):
        result = subprocess.run(['git', '-c', 'safe.directory=' + str(repository),
            '-c', 'user.name=synthetic', '-c', 'user.email=synthetic@example.invalid',
            '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + str(root / 'no-hooks'),
            '-C', str(repository), *args], capture_output=True, timeout=15, check=False)
        assert result.returncode == 0, 'synthetic fixture Git failed'
        return result.stdout
    git('init'); git('config', 'core.autocrlf', 'false'); git('checkout', '-b', build.BRANCH)
    for name in (*build.CODE_FILES, *build.SUPPORT_FILES, *build.DOCUMENT_FILES):
        path = repository / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("# synthetic " + name + "\nraise AssertionError('must not import runtime code')\n").encode())
    # The restored no-argument probes are intentionally tiny synthetic entry
    # contracts. Real CLI control-flow coverage belongs to test_sky_c_entry.
    for name, status in (('scripts/run_sky_c_supervised.py', 'prepared_not_executed'),
                          ('scripts/run_sky_mask_experiment.py', 'not_executed')):
        (repository / name).write_text("import json,sys\nassert len(sys.argv)==1\nprint(json.dumps(" + repr({
            'status': status, 'reconstruction_started': False, 'execution_authorized': False,
            'budget_claimed': False, 'budget_charged': False}) + "))\n", encoding='utf-8')
    sky_template = template()
    (repository / build.SKY_PROFILE).write_bytes(build.json_bytes(sky_template))
    (repository / build.legacy.PROFILE_NAME).write_bytes(build.json_bytes(sky_template['c_profile']))
    def commit():
        git('add', '--all'); git('commit', '-m', 'synthetic fixture')
        return git('rev-parse', 'HEAD').decode().strip()
    head = commit()
    return type('Case', (), {'root': root, 'repository': repository, 'output': root / 'release',
        'git': staticmethod(git), 'commit': staticmethod(commit), 'head': head, 'template': sky_template})()


def test_read_only_prepare_has_31_sealed_plus_one_support_closure(case):
    before = {str(p.relative_to(case.root)) for p in case.root.rglob('*')}
    result = build.prepare_toolpack(case.repository, case.head)
    assert result['status'] == 'prepared_no_execution' and result['code_file_count'] == 31
    assert result['support_file_count'] == 1 and result['payload_file_count'] == 41
    assert result['source_commit'] == case.head and result['experiment_branch'] == build.BRANCH
    assert not result['reconstruction_started'] and not result['autorun']
    assert {str(p.relative_to(case.root)) for p in case.root.rglob('*')} == before


def test_build_restores_every_exact_hash_and_noop_entry_without_compute(case):
    receipt = build.build_toolpack(case.repository, case.head, case.output)
    restored = case.output / 'restore-verification'
    assert receipt['source_commit'] == case.head and receipt['source_state'] == 'git_commit'
    assert receipt['code_file_count'] == 31 and receipt['support_file_count'] == 1
    assert len(receipt['default_entry_probes']) == 2
    assert all(row['reported_execution_authorized'] is False for row in receipt['default_entry_probes'])
    for name, digest in receipt['payload_hashes'].items():
        raw = (restored / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        assert receipt['payload_files'][name] == {'sha256': digest, 'bytes': len(raw)}
    tool = json.loads((restored / build.MANIFEST_NAME).read_bytes())
    assert set(tool) == {'schema_version', 'kind', 'autorun', 'source_state', 'source_commit', 'files'}
    assert tool['source_state'] == 'git_commit' and tool['source_commit'] == case.head
    assert set(tool['files']) == set(build.CODE_FILES)
    assert 'scripts/run_sky_mask_experiment.py' not in tool['files']
    assert 'scripts/run_sky_mask_experiment.py' in receipt['payload_hashes']
    assert build.verify_payload(restored, receipt['payload_hashes']) == receipt['restore']
    assert receipt['archive_sha256'] == hashlib.sha256((case.output / receipt['archive']).read_bytes()).hexdigest()
    for key in ('reconstruction_started', 'server_contacted', 'server_deployed', 'budget_claimed', 'budget_charged',
                'uac_requested', 'real_native_sky_c_accepted', 'contains_environment_or_data'):
        assert receipt[key] is False


def test_builder_closure_matches_entry_without_enlarging_legacy_closure():
    from scripts import run_sky_c_supervised as entry
    assert set(build.CODE_FILES) == set(entry.CODE_FILES) and len(build.CODE_FILES) == 31
    assert len(build.legacy.CODE_FILES) == 23


@pytest.mark.parametrize('mutation', ['dirty', 'untracked', 'wrong_branch', 'detached', 'old_head', 'short_sha'])
def test_only_clean_exact_experiment_head_can_be_packaged(case, mutation):
    head = case.head
    if mutation == 'dirty':
        with (case.repository / build.CODE_FILES[0]).open('ab') as stream: stream.write(b'# dirty\n')
    elif mutation == 'untracked':
        (case.repository / '.env').write_bytes(b'not-a-real-secret\n')
    elif mutation == 'wrong_branch':
        case.git('checkout', '-b', 'main')
    elif mutation == 'detached':
        case.git('checkout', '--detach', case.head)
    elif mutation == 'old_head':
        (case.repository / 'extra.md').write_text('new committed file\n'); case.commit()
    else:
        head = case.head[:7]
    with pytest.raises(build.ToolpackError):
        build.build_toolpack(case.repository, head, case.output)
    assert not case.output.exists()


@pytest.mark.parametrize('field', ['mask_binding', 'prepared_admission', 'budget_lineage', 'c_source', 'autorun'])
def test_host_bound_templates_are_rejected_before_output(case, field):
    value = deepcopy(case.template)
    if field in {'mask_binding', 'prepared_admission'}:
        value[field]['sha256'] = 'a' * 64
    elif field == 'budget_lineage':
        value[field]['maximum_remaining_seconds'] = 7234
    elif field == 'c_source':
        value['c_profile']['source']['cpu_result_sha256'] = 'a' * 64
    else:
        value['autorun'] = True
    (case.repository / build.SKY_PROFILE).write_bytes(build.json_bytes(value)); head = case.commit()
    with pytest.raises(build.ToolpackError):
        build.build_toolpack(case.repository, head, case.output)
    assert not case.output.exists()


def test_committed_extra_secrets_data_and_runtime_files_are_not_payload(case):
    for name in ('.env', 'database.db', 'input.jpg', 'mesh.glb', 'runtime-manifest.json',
                 'bound-sky-profile.json', 'c-budget-old-charge.json'):
        (case.repository / name).write_bytes(b'not a real input or secret\n')
    head = case.commit()
    receipt = build.build_toolpack(case.repository, head, case.output)
    assert set(receipt['payload_hashes']) == build.PAYLOAD_NAMES


def test_committed_crlf_is_normalized_before_manifest_and_restore_hashing(case):
    name = build.CODE_FILES[0]
    (case.repository / name).write_bytes(b'# CRLF\r\nvalue=1\r\n'); head = case.commit()
    receipt = build.build_toolpack(case.repository, head, case.output)
    assert (case.output / 'restore-verification' / name).read_bytes() == b'# CRLF\nvalue=1\n'
    assert receipt['payload_hashes'][name] == hashlib.sha256(b'# CRLF\nvalue=1\n').hexdigest()


def test_existing_output_and_repository_overlap_are_never_written(case):
    case.output.mkdir(); (case.output / 'keep').write_bytes(b'keep')
    with pytest.raises(build.ToolpackError): build.build_toolpack(case.repository, case.head, case.output)
    assert (case.output / 'keep').read_bytes() == b'keep'
    with pytest.raises(build.ToolpackError): build.build_toolpack(case.repository, case.head, case.repository / 'release')
    assert not (case.repository / 'release').exists()


def test_missing_required_support_or_document_fails_closed(case):
    (case.repository / build.SUPPORT_FILES[0]).unlink(); head = case.commit()
    with pytest.raises(build.ToolpackError, match='REGULAR_GIT_BLOB'):
        build.prepare_toolpack(case.repository, head)


def test_mutated_restored_member_or_extra_directory_is_rejected(case):
    receipt = build.build_toolpack(case.repository, case.head, case.output)
    root = case.output / 'restore-verification'
    (root / 'extra').mkdir()
    with pytest.raises(build.ToolpackError, match='EXTRA_OR_MISSING'):
        build.verify_payload(root, receipt['payload_hashes'])
    (root / 'extra').rmdir()
    (root / build.CODE_FILES[0]).write_bytes(b'changed\n')
    with pytest.raises(build.ToolpackError, match='RESTORE_HASH'):
        build.verify_payload(root, receipt['payload_hashes'])


@pytest.mark.parametrize('entry,symlink', [('../escape.py', False), ('C:/escape.py', False),
                                         ('scripts/foreign.py', False), ('scripts/run_sky_c_supervised.py', True)])
def test_unsafe_or_nonregular_archive_is_rejected_before_extract(case, entry, symlink):
    _, files = build.payload(case.repository, case.head)
    expected = {name: build.sha(raw) for name, raw in files.items()}
    if entry not in files: files[entry] = b'# malicious archive member\n'
    archive = case.root / 'bad.zip'
    with zipfile.ZipFile(archive, 'x') as package:
        for name, raw in files.items():
            info = zipfile.ZipInfo(name)
            if symlink and name == entry:
                info.create_system = 3; info.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(info, raw)
    target = case.root / 'rejected-restore'
    with pytest.raises(build.ToolpackError): build.restore_archive(archive, target, expected_hashes=expected)
    assert not target.exists()


def test_default_entry_must_not_claim_execution_or_write_packaged_files(case):
    path = case.repository / 'scripts/run_sky_c_supervised.py'
    path.write_text("import json\nprint(json.dumps({'status':'prepared_not_executed','reconstruction_started':True,'execution_authorized':True}))\n")
    head = case.commit()
    with pytest.raises(build.ToolpackError, match='DEFAULT_ENTRY_NOT_NOOP'):
        build.build_toolpack(case.repository, head, case.output)
    assert not (case.output / 'package-receipt.json').exists()


def test_cli_prepare_is_read_only_and_requires_explicit_build_for_output(case, capsys):
    assert build.main(['--repository-root', str(case.repository), '--source-commit', case.head]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'prepared_no_execution'
    assert not case.output.exists()
    assert build.main(['--repository-root', str(case.repository), '--output-dir', str(case.output)]) == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == 'SKY_C_TOOLPACK_OUTPUT_REQUIRES_EXPLICIT_BUILD'
    assert not case.output.exists()
