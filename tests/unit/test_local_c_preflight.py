"""Read-only local C admission wiring, using only synthetic small files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import backend.experiments.c_runner as runner
import scripts.run_local_c as cli
from backend.experiments.resources import GIB
from backend.experiments.supervision import SupervisionError
from tests.unit.test_c_profile import local_c_profile, terminal_receipt
from tests.unit.test_cpu_runner import local_profile


def write_json(path, value):
    path.write_bytes((json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode())
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def case(tmp_path, monkeypatch):
    profile = local_c_profile(tmp_path)
    cpu = local_profile(tmp_path)
    cpu_path = tmp_path / 'source-cpu-profile.json'
    cpu_sha = write_json(cpu_path, cpu)
    receipt = terminal_receipt()
    receipt.update(scene=cpu['target_experiment']['scene'], source_job_id=cpu['source_job_id'],
                   result={'final_sfm_gate': {'database_images': 300, 'registered_images': 300}})
    receipt['tool_identity']['profile_sha256'] = cpu_sha
    receipt['hashes']['final_database_sha256'] = 'd' * 64
    terminal_path = tmp_path / 'terminal.json'
    terminal_sha = write_json(terminal_path, receipt)
    profile['source']['terminal_receipt_sha256'] = terminal_sha
    profile['budget']['source_terminal_receipt_sha256'] = terminal_sha
    profile_path = tmp_path / 'c-profile.json'
    profile_sha = write_json(profile_path, profile)
    target = Path(profile['target_experiment']['root'])
    target.parent.mkdir()
    pipeline = tmp_path / 'pipeline'
    pipeline.mkdir()
    source = SimpleNamespace(image_names=tuple(f'image-{i:03d}.jpg' for i in range(300)))
    handoff = SimpleNamespace(manifest={
        'files': [{'relative_path': 'work/shared/colmap/database.db', 'sha256': 'd' * 64}],
        'sfm_gate': receipt['result']['final_sfm_gate'],
        'execution': {'source_experiment_root': cpu['target_experiment']['root']},
        'source': {'source_job_id': cpu['source_job_id']}})
    value = SimpleNamespace(root=tmp_path, profile=profile, profile_path=profile_path, target=target,
        cpu=cpu, receipt=receipt, terminal_path=terminal_path, source=source, handoff=handoff,
        host_available=12 * GIB, data_free=28 * GIB, evidence_free=10 * GIB,
        pipeline=pipeline, forbidden=[])
    value.args = SimpleNamespace(mode='preflight', execute_c_only=False, pipeline_root=pipeline,
        profile=profile_path, profile_sha256=profile_sha, terminal_receipt=terminal_path,
        source_cpu_profile=cpu_path, source_cpu_profile_sha256=cpu_sha,
        runtime_manifest=tmp_path / 'runtime.json', runtime_manifest_sha256='e' * 64,
        tool_manifest_sha256='f' * 64)
    frozen = {'pipeline.json': runner.BASE_CONFIG_SHA, 'run_pipeline.py': runner.PIPELINE_SCRIPT_SHA,
              'run_colmap_shared.py': runner.COLMAP_SCRIPT_SHA}
    actual_digest = runner.digest
    monkeypatch.setattr(runner, 'digest', lambda path, health=lambda: None:
                        frozen[Path(path).name] if Path(path).name in frozen else actual_digest(path, health))
    value.host = Mock()
    value.host.read.side_effect = lambda _: ({'cpu': {'logical_count': 32}, 'memory': {
        'total_bytes': 34141495296, 'used_bytes': 34141495296 - value.host_available}}, [])
    monkeypatch.setattr(runner, 'HostResourceReader', Mock(return_value=value.host))
    monkeypatch.setattr(runner.shutil, 'disk_usage', lambda path: SimpleNamespace(
        free=value.evidence_free if str(path).rstrip('\\/').casefold() == 'c:' else value.data_free))
    value.runtime = Mock(return_value={'files': {'synthetic-native.exe': '0' * 64}})
    monkeypatch.setattr(cli, 'verify_runtime_manifest', value.runtime)
    value.verify_input = Mock(return_value=source)
    value.verify_handoff = Mock(return_value=handoff)
    monkeypatch.setattr(runner, 'verify_hybrid_source', value.verify_input)
    monkeypatch.setattr(runner, 'verify_sfm_handoff', value.verify_handoff)
    def forbidden(name):
        def fail(*args, **kwargs):
            value.forbidden.append(name)
            pytest.fail('preflight must not call ' + name)
        return fail
    for name in ('require_admin', 'verify_git_identity', 'claim_c_budget', 'create_private_root',
                 'import_sfm_handoff', 'run_contained_process', 'exclusive_long_test',
                 'ExperimentResourceMonitor', 'CStageSupervisor', 'bounded_private_report',
                 'write_exclusive'):
        monkeypatch.setattr(runner, name, forbidden(name))
    monkeypatch.setattr(runner.EvidenceSink, 'create_production', forbidden('evidence'))
    monkeypatch.setattr(runner.ctypes, 'WinDLL', forbidden('WinDLL'), raising=False)
    monkeypatch.setattr(runner.threading, 'Thread', forbidden('background_thread'))
    monkeypatch.setattr('subprocess.Popen', forbidden('child_process'))
    return value


def run(case):
    return runner.preflight_local_c(case.args, case.profile, tool_identity='f' * 64)


def persist_profile(case):
    case.args.profile_sha256 = write_json(case.profile_path, case.profile)


def test_read_only_preflight_uses_actual_binding_and_source_gates_without_side_effects(case):
    before = {path.relative_to(case.root): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    result = run(case)
    assert result['status'] == 'read_only_preflight_passed'
    assert result['execution_authorized'] is False
    for field in ('reconstruction_started', 'source_modified', 'target_created', 'handoff_imported',
                  'budget_claimed', 'budget_charged', 'server_accessed', 'gpu_accessed',
                  'uac_requested', 'child_process_started'):
        assert result[field] is False
    assert result['source_verified'] and result['runtime_manifest_verified']
    assert result['remaining_budget_seconds'] <= 9479.782
    assert result['host_resource_snapshot']['available_ram_bytes'] == 12 * GIB
    assert 'actual_python_dependency_imports' in result['deferred_checks']
    assert 'git_clean_worktree_identity' in result['deferred_checks']
    assert not case.target.exists() and not case.forbidden
    assert before == {path.relative_to(case.root): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    case.verify_input.assert_called_once()
    case.verify_handoff.assert_called_once()
    assert case.verify_handoff.call_args.kwargs['input_source'] is case.source
    assert case.verify_handoff.call_args.args == (case.profile['source']['root'], 'c' * 64)


@pytest.mark.parametrize('where,value,code', [
    ('host_available', 8 * GIB, 'LOCAL_C_RAM_ADMISSION_FAILED'),
    ('data_free', 28 * GIB - 1, 'LOCAL_C_DISK_ADMISSION_FAILED'),
    ('evidence_free', 10 * GIB - 1, 'LOCAL_C_EVIDENCE_ADMISSION_FAILED'),
])
def test_resource_lines_block_without_source_reads_or_side_effects(case, where, value, code):
    setattr(case, where, value)
    with pytest.raises(SupervisionError, match=code):
        run(case)
    case.verify_input.assert_not_called()
    assert not case.target.exists() and not case.forbidden


def test_receipt_profile_budget_cannot_reclaim_time_or_change_source(case):
    case.profile['budget'].update(consumed_sfm_seconds=1, remaining_seconds=14399)
    persist_profile(case)
    with pytest.raises(SupervisionError, match='C_ONLY_BUDGET_RECEIPT_MISMATCH'):
        run(case)
    case.host.read.assert_not_called()
    assert not case.forbidden


def test_changed_cpu_profile_pin_blocks_before_host(case):
    case.args.source_cpu_profile_sha256 = '0' * 64
    with pytest.raises(cli.CLIError, match='LOCAL_C_DOCUMENT_HASH_CHANGED'):
        run(case)
    case.host.read.assert_not_called()
    assert not case.forbidden


def test_profile_file_and_supplied_profile_must_match(case):
    case.args.profile_sha256 = '0' * 64
    with pytest.raises(cli.CLIError, match='LOCAL_C_DOCUMENT_HASH_CHANGED'):
        run(case)
    case.host.read.assert_not_called()


def test_handoff_receipt_identity_mismatch_is_not_preflight_success(case):
    case.handoff.manifest['files'][0]['sha256'] = '0' * 64
    with pytest.raises(SupervisionError, match='LOCAL_C_HANDOFF_RECEIPT_MISMATCH'):
        run(case)
    assert not case.target.exists() and not case.forbidden


def test_source_permission_denied_is_explicit_and_does_not_request_uac(case):
    case.verify_input.side_effect = PermissionError('private synthetic source')
    with pytest.raises(SupervisionError, match='LOCAL_C_PREFLIGHT_ACCESS_DENIED'):
        run(case)
    assert not case.target.exists() and not case.forbidden


def test_existing_target_is_never_reused(case):
    case.target.mkdir()
    with pytest.raises(SupervisionError, match='LOCAL_C_NEW_TARGET_REQUIRED'):
        run(case)
    case.host.read.assert_not_called()
    assert not case.forbidden


def test_original_cpu_experiment_target_overlap_is_rejected(case):
    root = Path(case.cpu['target_experiment']['root'])
    target = root / '_experiments' / case.profile['target_experiment']['scene']
    target.parent.mkdir(parents=True)
    case.profile['target_experiment']['root'] = str(target)
    persist_profile(case)
    with pytest.raises(SupervisionError, match='LOCAL_C_SOURCE_TARGET_OVERLAP'):
        run(case)
    case.host.read.assert_not_called()
    assert not target.exists() and not case.forbidden


def test_preflight_does_not_accept_execute_flag(case):
    case.args.execute_c_only = True
    with pytest.raises(SupervisionError, match='LOCAL_C_EXECUTION_MODE_MISMATCH'):
        run(case)
    case.host.read.assert_not_called()


@pytest.mark.parametrize('field', ['available_ram_floor_bytes', 'disk_floor_bytes'])
def test_preflight_never_lowers_resource_safety_lines(case, field):
    case.profile['protection'][field] -= 1
    persist_profile(case)
    with pytest.raises(SupervisionError, match='C_ONLY_HEADROOM_REDUCED'):
        run(case)
    case.host.read.assert_not_called()
    assert not case.target.exists() and not case.forbidden


def test_preflight_read_deadline_does_not_claim_or_start_work(case, monkeypatch):
    monkeypatch.setattr(runner.time, 'monotonic', Mock(side_effect=[1.0, 122.0]))
    with pytest.raises(SupervisionError, match='LOCAL_C_PREFLIGHT_READ_DEADLINE'):
        run(case)
    case.verify_input.assert_not_called()
    assert not case.target.exists() and not case.forbidden


def test_preflight_rejects_network_pipeline_before_accessing_it(case):
    case.args.pipeline_root = Path('\\\\remote-host\\share\\pipeline')
    with pytest.raises(cli.CLIError, match='LOCAL_C_PATH_INVALID'):
        run(case)
    case.host.read.assert_not_called()
    case.verify_input.assert_not_called()


def test_frozen_file_mismatch_blocks_before_source_and_host(case, monkeypatch):
    monkeypatch.setattr(runner, 'digest', lambda *args, **kwargs: '0' * 64)
    with pytest.raises(SupervisionError, match='LOCAL_C_FROZEN_IDENTITY_CHANGED'):
        run(case)
    case.host.read.assert_not_called()
    case.verify_input.assert_not_called()


def test_source_validation_errors_are_never_reported_as_success(case):
    case.verify_input.side_effect = SupervisionError('CHECKPOINT_MANIFEST_HASH_MISMATCH')
    with pytest.raises(SupervisionError, match='CHECKPOINT_MANIFEST_HASH_MISMATCH'):
        run(case)
    case.verify_handoff.assert_not_called()
    assert not case.forbidden


def test_runtime_hash_validation_is_required_before_source_gate(case):
    case.runtime.side_effect = cli.CLIError('LOCAL_C_RUNTIME_HASH_CHANGED')
    with pytest.raises(cli.CLIError, match='LOCAL_C_RUNTIME_HASH_CHANGED'):
        run(case)
    case.verify_input.assert_not_called()
    assert not case.forbidden


def test_caller_health_guard_is_additive_and_reaches_source_reads(case):
    checks = []
    guard = lambda: checks.append('checked')
    def verify_source(**kwargs):
        kwargs['health_check']()
        return case.source
    case.verify_input.side_effect = verify_source
    assert runner.preflight_local_c(case.args, case.profile, tool_identity='f' * 64,
        health_check=guard)['status'] == 'read_only_preflight_passed'
    assert len(checks) >= 4
    assert not case.forbidden


def test_caller_guard_failure_cannot_be_ignored_or_start_source_work(case):
    def fail_guard():
        raise SupervisionError('STATIC_C_RAM_FLOOR')
    with pytest.raises(SupervisionError, match='STATIC_C_RAM_FLOOR'):
        runner.preflight_local_c(case.args, case.profile, tool_identity='f' * 64, health_check=fail_guard)
    case.verify_input.assert_not_called()
    assert not case.target.exists() and not case.forbidden


def arguments(case):
    return ['--mode', 'preflight', '--profile', str(case.args.profile),
        '--profile-sha256', case.args.profile_sha256, '--pipeline-root', str(case.pipeline),
        '--tool-manifest-sha256', 'f' * 64, '--terminal-receipt', str(case.args.terminal_receipt),
        '--source-cpu-profile', str(case.args.source_cpu_profile),
        '--source-cpu-profile-sha256', case.args.source_cpu_profile_sha256,
        '--runtime-manifest', str(case.args.runtime_manifest),
        '--runtime-manifest-sha256', case.args.runtime_manifest_sha256]


def test_cli_dispatches_real_read_only_preflight_without_execute(case, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'load_backend', lambda *_: runner)
    assert cli.main(arguments(case)) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['status'] == 'read_only_preflight_passed' and not value['execution_authorized']
    assert not value['child_process_started'] and not case.forbidden


@pytest.mark.parametrize('argv', [
    ['--mode', 'preflight'], ['--mode', 'preflight', '--execute-c-only'],
    ['--mode', 'inspect', '--profile-sha256', 'a' * 64],
    ['--mode', 'prepare', '--runtime-manifest-sha256', 'a' * 64],
    ['--mode', 'preflight', '--execute'], ['--mode', 'preflight', '--execute-c'],
])
def test_cli_mixed_or_missing_action_arguments_fail_before_backend(argv, monkeypatch, capsys):
    loader = Mock(side_effect=AssertionError('backend should not load'))
    monkeypatch.setattr(cli, 'load_backend', loader)
    assert cli.main(argv) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['status'] == 'blocked' and not value['reconstruction_started']
    loader.assert_not_called()


def test_cli_read_denied_remains_stable_and_has_no_private_text(case, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'load_backend', lambda *_: runner)
    case.verify_input.side_effect = PermissionError('synthetic private pathname must not be printed')
    assert cli.main(arguments(case)) == 1
    raw = capsys.readouterr().out
    assert json.loads(raw)['error_code'] == 'LOCAL_C_PREFLIGHT_ACCESS_DENIED'
    assert 'private pathname' not in raw and not case.forbidden
