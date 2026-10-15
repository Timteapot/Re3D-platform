"""Synthetic explicit sky execution wiring; no native child, UAC or server."""
from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.experiments import sky_executor as run
from backend.experiments import c_budget_chain as ledger
from backend.experiments.c_stages import C_STAGE_NAMES
from backend.experiments.process import ContainedProcessOutcome
from backend.experiments.resources import GIB
from backend.experiments.stages import PipelineStagePlan, StageSpec
from backend.experiments.supervision import SupervisionError, WholeRunBudget
from backend.experiments.evidence import EvidenceSink
from tests.unit.test_c_budget_chain import SOURCE, CPU, seed, read


class Monitor:
    def __init__(self, **kwargs):
        self.options, self._closed, self.started = kwargs, False, False
        self._threads = {}
    def start(self): self.started = True
    def check_health(self): assert self.started
    def set_phase(self, phase): self.phase = phase
    def finish(self): self.check_health(); self.close()
    def close(self): self._closed = True
    def summary(self): return {'trip': None, 'latest': {}, 'peaks': {}}


class Evidence:
    def __init__(self):
        self._closed, self.started, self.rows = False, False, []
        self._threads, self._terminal_threads = {}, []
    def start(self): self.started = True
    def check_health(self): assert self.started
    def append(self, kind, value): self.rows.append((kind, value))
    def finish_terminal(self, value): self.rows.append(('terminal', value))
    def write_forensic_summary(self, value): self.rows.append(('forensic', value))
    def close(self): self._closed = True


@pytest.fixture
def wiring(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp('sx')
    ledger_root, pipeline = base / 'ledger', base / 'pipeline'
    ledger_root.mkdir(); pipeline.mkdir()
    original = seed(ledger_root)
    reconciliation = read(ledger_root)
    history = {path.name: path.read_bytes() for path in ledger_root.iterdir()}
    target = base / '_experiments/sky-scene'
    target.parent.mkdir()
    profile = {'target_experiment': {'scene': 'sky-scene', 'root': str(target)},
               'cpu': {'openmvs_threads': 8, 'texture_threads': 8},
               'protection': {'available_ram_floor_bytes': 8 * GIB, 'disk_floor_bytes': 20 * GIB}}
    names = ('renamed-3.jpg', 'renamed-1.jpg', 'renamed-2.jpg')
    source, handoff = SimpleNamespace(image_names=names), SimpleNamespace(identity='synthetic-handoff')
    stages = tuple(StageSpec(name, (str(base / 'fake.exe'), name), target / ('marker-' + name), pipeline)
                   for name in C_STAGE_NAMES)
    plan = PipelineStagePlan(stages, {'DATABASE_URL': 'not-a-real-secret', 'OMP_NUM_THREADS': '99'}, (8, 6))
    context = {'root': target, 'pipeline': pipeline, 'c_profile': profile,
               'identity': {'profile_sha256': 'd' * 64, 'tool_manifest_sha256': 'e' * 64},
               'reconciliation': reconciliation, 'source_budget': SOURCE,
               'runtime': {'python': str(base / 'fake-python.exe')},
               'tool_root': base / 'sealed-tools',
               'verified_source': source, 'verified_handoff': handoff, 'binding': {'synthetic': True}}
    value = SimpleNamespace(base=base, target=target, pipeline=pipeline, profile=profile,
        ledger=ledger_root, original=original, history=history, context=context, plan=plan,
        process_calls=[], gates=[], inventory_checks=[], monitors=[], evidence=Evidence(),
        imports=[], probe_code=0, probe_stop=True, failed_stage=None, inventory_failure=False,
        source_failure=False, owners=[])
    value.args = SimpleNamespace(execute_sky_c_only=True)
    value.admin = Mock()
    monkeypatch.setattr(run, 'require_admin', value.admin)
    monkeypatch.setattr(run, 'verify_git_identity', Mock())
    monkeypatch.setattr(run, 'PRIVATE_PARENT', ledger_root)
    monkeypatch.setattr(run, 'evidence_codes_for_sealed_tool',
                        Mock(return_value=run.LOCAL_C_CODES | run.SKY_EXECUTOR_CODES))
    monkeypatch.setattr(run, 'exclusive_long_test', lambda: nullcontext())
    monkeypatch.setattr(run.EvidenceSink, 'create_production', Mock(return_value=value.evidence))
    def create(root, **kwargs): (root / 'reports').mkdir(parents=True)
    monkeypatch.setattr(run, 'create_private_root', create)
    def monitor(**kwargs):
        result = Monitor(**kwargs); value.monitors.append(result); return result
    monkeypatch.setattr(run, 'ExperimentResourceMonitor', monitor)
    value.sleep = Mock(return_value=1)
    monkeypatch.setattr(run.ctypes, 'WinDLL', Mock(return_value=SimpleNamespace(SetThreadExecutionState=value.sleep)),
                        raising=False)
    def importer(handoff, root, **kwargs):
        value.imports.append((handoff, root, kwargs))
        return SimpleNamespace(input_manifest_path=root / 'work/input/input-manifest.json')
    monkeypatch.setattr(run, 'import_sfm_handoff', importer)
    monkeypatch.setattr(run, 'prepared_dimensions', lambda *args: (8, 6))
    def gate(stage, root, image_names, health_check):
        health_check(); value.gates.append(stage.name)
        assert image_names == names
        return {'all_passed': True}
    monkeypatch.setattr(run, 'gate_c_stage', gate)
    def contained(argv, **kwargs):
        kwargs['health_check']()
        value.process_calls.append((argv, kwargs))
        probe = '-c' in argv
        if not probe or value.probe_stop:
            kwargs['on_tree_stopped']()
        code = value.probe_code if probe else int(argv[1] == value.failed_stage)
        return ContainedProcessOutcome(code, .001, 7, value.probe_stop if probe else True)
    monkeypatch.setattr(run, 'run_contained_process', contained)
    actual_supervisor = run.CStageSupervisor
    def supervisor(**kwargs):
        owner = actual_supervisor(**kwargs, sample_seconds=.01)
        value.owners.append(owner)
        return owner
    monkeypatch.setattr(run, 'CStageSupervisor', supervisor)
    # Fake only the separate admission/asset API boundary. The actual owner,
    # budget continuation ledger and finalizer below remain under test.
    support = ModuleType('backend.experiments.sky_supervision')
    support.admit_sky_c = Mock(return_value=context)
    support.build_supervised_sky_plan = Mock(return_value=plan)
    support.verify_sky_runtime_stage = Mock()
    def sources(ctx, health):
        health()
        if value.source_failure: raise SupervisionError('SKY_TEST_SOURCE_CHANGED')
        return source, handoff
    support.verify_sky_sources = Mock(side_effect=sources)
    assets = ModuleType('backend.experiments.sky_assets')
    assets.gate_sky_openmvs = Mock(return_value={'files': ['synthetic-bound-mask']})
    def inventory(inventory, *, health, hash_content):
        health(); value.inventory_checks.append(hash_content)
        assert hash_content is True
        if value.inventory_failure: raise SupervisionError('SKY_TEST_MASK_CHANGED')
    assets.verify_inventory = Mock(side_effect=inventory)
    monkeypatch.setitem(__import__('sys').modules, support.__name__, support)
    monkeypatch.setitem(__import__('sys').modules, assets.__name__, assets)
    value.support, value.assets = support, assets
    monkeypatch.setattr('subprocess.Popen', Mock(side_effect=AssertionError('native process forbidden')))
    monkeypatch.setattr('backend.monitoring.resources.NvidiaSmiReader',
                        Mock(side_effect=AssertionError('GPU probe forbidden')))
    monkeypatch.setattr('backend.experiments.preflight.read_queue_state',
                        Mock(side_effect=AssertionError('queue/server access forbidden')))
    return value


def execute(wiring):
    return run.execute_sky_c(wiring.args, {'synthetic_sky_profile': True}, tool_identity='e' * 64)


def charge(case):
    files = list(case.ledger.glob('*-continuation-000001-charge.json'))
    assert len(files) == 1
    return json.loads(files[0].read_bytes())


def assert_old_receipts_unchanged(case):
    for name, raw in case.history.items():
        assert (case.ledger / name).read_bytes() == raw


def test_full_sky_execution_uses_actual_owner_and_append_only_charge(wiring):
    c = wiring; result = execute(c)
    assert result['status'] == 'succeeded', result
    assert len(c.process_calls) == 9 and c.gates == list(C_STAGE_NAMES)
    assert len(c.owners) == 1 and c.support.verify_sky_runtime_stage.call_count == 16
    c.assets.gate_sky_openmvs.assert_called_once()
    assert c.inventory_checks == [True, True, True]  # densify, texture, final proof
    assert c.support.verify_sky_sources.call_count == 1
    assert c.imports[0][2]['target_work_root'] == c.target / 'work'
    for key in ('process_tree_stopped', 'observation_stopped', 'resource_threads_stopped',
                'evidence_threads_stopped', 'source_handoff_unchanged', 'sky_source_unchanged',
                'terminal_evidence_confirmed', 'sleep_request_released', 'budget_charge_confirmed'):
        assert result[key] is True, key
    assert not result['server_accessed'] and not result['gpu_stage_started']
    assert result['sfm_mask_applied'] is False and result['budget_reset_or_extended'] is False
    limits = c.monitors[0].options['limits']
    assert limits.minimum_available_ram_bytes == 8 * GIB and limits.minimum_disk_free_bytes == 20 * GIB
    assert limits.gpu_required is False
    for argv, kwargs in c.process_calls:
        assert kwargs['timeout_seconds'] <= c.context['reconciliation']['remaining_seconds']
        assert kwargs['env']['CUDA_VISIBLE_DEVICES'] == '-1'
        assert kwargs['env']['OMP_NUM_THREADS'] == '8' and 'DATABASE_URL' not in kwargs['env']
    assert c.sleep.call_args_list[0].args == (0x80000001,)
    assert c.sleep.call_args_list[-1].args == (0x80000000,)
    assert charge(c)['status'] == 'succeeded'
    assert read(c.ledger)['remaining_seconds'] < c.context['reconciliation']['remaining_seconds']
    assert_old_receipts_unchanged(c)
    saved = json.loads((c.target / 'reports/sky-c-final-status.json').read_bytes())
    assert saved['budget_charge_confirmed'] and saved['sleep_request_released']


def test_no_explicit_switch_does_not_elevate_claim_or_compute(wiring):
    c = wiring; c.args.execute_sky_c_only = False
    with pytest.raises(SupervisionError, match='EXPLICIT_REQUIRED'):
        execute(c)
    c.admin.assert_not_called(); c.support.admit_sky_c.assert_not_called()
    assert not c.process_calls and not c.target.exists()
    assert {path.name for path in c.ledger.iterdir()} == set(c.history)


def test_cli_call_reads_its_exact_pinned_profile_when_not_supplied(wiring, monkeypatch):
    c = wiring
    c.args.profile, c.args.profile_sha256 = c.base / 'profile.json', 'f' * 64
    reader = Mock(return_value={'synthetic_sky_profile': True})
    monkeypatch.setattr('scripts.run_local_c.read', reader)
    result = run.execute_sky_c(c.args, tool_identity='e' * 64)
    assert result['status'] == 'succeeded'
    reader.assert_called_once_with(c.args.profile, expected_sha256=c.args.profile_sha256)


@pytest.mark.parametrize('key,value', [('available_ram_floor_bytes', 8 * GIB - 1),
                                      ('disk_floor_bytes', 20 * GIB - 1)])
def test_safety_lines_cannot_be_lowered_even_by_an_admission_regression(wiring, key, value):
    c = wiring; c.profile['protection'][key] = value
    with pytest.raises(SupervisionError, match='RESOURCE_CONFIGURATION_CHANGED'):
        execute(c)
    assert not c.process_calls and not c.target.exists()
    assert {path.name for path in c.ledger.iterdir()} == set(c.history)


def test_eight_thread_limit_cannot_be_silently_changed(wiring):
    c = wiring; c.profile['cpu']['openmvs_threads'] = 12
    with pytest.raises(SupervisionError, match='THREAD_CONFIGURATION_CHANGED'):
        execute(c)
    assert not c.process_calls


@pytest.mark.parametrize('stop', [False, True])
def test_dependency_failure_preserves_stop_proof_and_closes_claim(wiring, stop):
    c = wiring; c.probe_code = 1; c.probe_stop = stop
    result = execute(c)
    assert result['status'] == 'failed' and result['error_code'] == 'SKY_EXECUTE_DEPENDENCIES_UNAVAILABLE'
    assert result['process_tree_stopped'] is stop and result['reconstruction_started'] is False
    assert not c.owners and len(c.process_calls) == 1 and not c.sleep.called
    assert result['resource_threads_stopped'] and result['evidence_threads_stopped']
    assert result['budget_charge_confirmed'] and charge(c)['status'] == 'failed'
    assert_old_receipts_unchanged(c)


def test_mask_change_stops_before_densify_and_never_runs_texture(wiring):
    c = wiring; c.inventory_failure = True
    result = execute(c)
    assert result['status'] == 'failed'
    assert [argv[1] for argv, _ in c.process_calls if '-c' not in argv] == list(C_STAGE_NAMES[:3])
    assert result['process_tree_stopped'] is False  # no fabricated stop proof for a failed spawn gate
    assert result['budget_charge_confirmed'] and charge(c)['status'] == 'failed'


def test_failed_stage_does_not_start_later_stage_and_is_charged(wiring):
    c = wiring; c.failed_stage = '12-c-reconstruct'
    result = execute(c)
    assert result['status'] == 'failed' and result['process_tree_stopped']
    assert all('13-c-texture' not in argv for argv, _ in c.process_calls)
    assert result['budget_charge_confirmed'] and charge(c)['status'] == 'failed'
    assert_old_receipts_unchanged(c)


def test_failed_source_final_proof_cannot_report_success(wiring):
    c = wiring; c.source_failure = True
    result = execute(c)
    assert result['status'] == 'failed' and result['sky_source_unchanged'] is False
    assert charge(c)['status'] == 'failed' and result['budget_charge_confirmed']


def test_import_failure_after_claim_still_has_terminal_evidence_and_charge(wiring, monkeypatch):
    def fail(*args, **kwargs): raise SupervisionError('SKY_TEST_IMPORT_FAILED')
    monkeypatch.setattr(run, 'import_sfm_handoff', fail)
    result = execute(wiring)
    assert result['status'] == 'failed' and result['reconstruction_started'] is False
    assert not wiring.process_calls
    assert result['process_tree_stopped'] and result['terminal_evidence_confirmed']
    assert result['budget_charge_confirmed'] and charge(wiring)['status'] == 'failed'
    assert_old_receipts_unchanged(wiring)


def test_target_creation_failure_after_claim_still_closes_the_ledger(wiring, monkeypatch):
    def fail(*args, **kwargs): raise OSError('synthetic target creation failure')
    monkeypatch.setattr(run, 'create_private_root', fail)
    result = execute(wiring)
    assert result['status'] == 'failed' and not wiring.target.exists()
    assert not wiring.process_calls and result['terminal_evidence_confirmed']
    assert result['budget_claimed'] and result['budget_charged'] and charge(wiring)['status'] == 'failed'
    assert_old_receipts_unchanged(wiring)


def test_windows_sleep_initialization_failure_still_closes_resources_and_charge(wiring, monkeypatch):
    def fail(*args, **kwargs): raise OSError('synthetic sleep API failure')
    monkeypatch.setattr(run.ctypes, 'WinDLL', fail)
    result = execute(wiring)
    assert result['status'] == 'failed' and not wiring.owners
    assert result['process_tree_stopped'] and result['resource_threads_stopped']
    assert result['evidence_threads_stopped'] and result['sleep_request_released']
    assert result['budget_charge_confirmed'] and charge(wiring)['status'] == 'failed'


def test_an_unclosed_continuation_blocks_a_second_attempt(wiring):
    c = wiring
    ledger.claim_c_continuation_budget(c.ledger, c.context['reconciliation'], source_budget=SOURCE,
                                     profile_sha256='f' * 64, scene='other-scene')
    result = execute(c)
    assert result['status'] == 'failed' and not c.process_calls and not c.target.exists()
    assert len(list(c.ledger.glob('*-continuation-*.json'))) == 1
    assert not list(c.ledger.glob('*-continuation-*-charge.json'))
    assert_old_receipts_unchanged(c)


def test_charge_write_failure_is_not_terminal_success(wiring, monkeypatch):
    def fail(*args, **kwargs): raise OSError('synthetic exclusive charge failure')
    monkeypatch.setattr(run, 'write_c_continuation_charge', fail)
    result = execute(wiring)
    assert result['status'] == 'failed' and result['budget_charge_confirmed'] is False
    assert result['error_code'] == 'SKY_EXECUTE_BUDGET_CHARGE_UNCONFIRMED'
    assert len(list(wiring.ledger.glob('*-continuation-*.json'))) == 1
    assert_old_receipts_unchanged(wiring)


def test_sleep_release_failure_is_recorded_before_charge_and_final_receipt(wiring):
    wiring.sleep.side_effect = [1, 0]
    result = execute(wiring)
    assert result['status'] == 'failed' and result['sleep_request_released'] is False
    assert charge(wiring)['status'] == 'failed'
    saved = json.loads((wiring.target / 'reports/sky-c-final-status.json').read_bytes())
    assert saved['status'] == 'failed' and saved['budget_charge_confirmed']


def test_admission_time_is_not_given_back_when_budget_owner_is_created(wiring, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(run.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(run, 'WholeRunBudget', lambda seconds: WholeRunBudget(seconds, monotonic=lambda: clock[0]))
    def admission(*args, **kwargs):
        clock[0] += 30
        return wiring.context
    wiring.support.admit_sky_c.side_effect = admission
    result = execute(wiring)
    assert result['elapsed_seconds'] >= 30
    first = json.loads(next(wiring.ledger.glob('*-continuation-000001.json')).read_bytes())
    assert first['remaining_seconds_at_admission'] <= wiring.context['reconciliation']['remaining_seconds'] - 30
    recorded = charge(wiring)
    assert 29.999 <= recorded['cumulative_charge_seconds'] - wiring.context['reconciliation']['cumulative_charge_seconds'] <= 30.001
    assert_old_receipts_unchanged(wiring)


def test_live_chain_claim_io_is_charged_as_well_as_admission(wiring, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(run.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(run, 'WholeRunBudget', lambda seconds: WholeRunBudget(seconds, monotonic=lambda: clock[0]))
    def admission(*args, **kwargs):
        clock[0] += 30
        return wiring.context
    wiring.support.admit_sky_c.side_effect = admission
    actual_claim = run.claim_c_continuation_budget
    def claim(*args, **kwargs):
        clock[0] += 5
        return actual_claim(*args, **kwargs)
    monkeypatch.setattr(run, 'claim_c_continuation_budget', claim)
    result = execute(wiring)
    assert result['elapsed_seconds'] >= 35
    recorded = charge(wiring)
    assert 34.999 <= recorded['cumulative_charge_seconds'] - wiring.context['reconciliation']['cumulative_charge_seconds'] <= 35.001


def code_seal(tmp_path):
    root = tmp_path / 'code-seal'; root.mkdir()
    files = {}
    for index, name in enumerate(run._CODE_SOURCE_FILES):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        raw = ("ASSET = 'SKY_ASSET_HASH_CHANGED'\n"
               "BUDGET = 'LOCAL_C_BUDGET_CHAIN_CHANGED'\n"
               "PROFILE = 'LOCAL_C_SKY_BINDING_INVALID'\n"
               "PRIVATE = 'password=not-a-real-secret'\n"
               "DYNAMIC = 'SKY_' + 'NOT_A_LITERAL_CODE'\n").encode()
        path.write_bytes(raw)
        files[name] = __import__('hashlib').sha256(raw).hexdigest()
    raw = (json.dumps({'kind': 'local_sky_c_supervised_tools', 'files': files}) + '\n').encode()
    (root / 'sky-c-tool-manifest.json').write_bytes(raw)
    return root, __import__('hashlib').sha256(raw).hexdigest()


def test_sealed_literal_asset_and_budget_codes_survive_c_evidence_sanitization(tmp_path):
    root, pin = code_seal(tmp_path)
    codes = run.evidence_codes_for_sealed_tool(root, pin)
    assert {'SKY_ASSET_HASH_CHANGED', 'LOCAL_C_BUDGET_CHAIN_CHANGED', 'LOCAL_C_SKY_BINDING_INVALID'} <= codes
    assert 'SKY_NOT_A_LITERAL_CODE' not in codes and 'password=not-a-real-secret' not in codes
    sink = EvidenceSink.create_for_test(tmp_path / 'evidence', free_reader=lambda _: 20 * GIB,
                                       known_codes=codes, known_stages=C_STAGE_NAMES)
    try:
        for code in ('SKY_ASSET_HASH_CHANGED', 'LOCAL_C_BUDGET_CHAIN_CHANGED', 'LOCAL_C_SKY_BINDING_INVALID'):
            assert sink.sanitize({'error_code': code}) == {'error_code': code}
        assert sink.sanitize({'error_code': 'SKY_CALLER_INVENTED'}) == {'error_code': 'UNCLASSIFIED_ERROR'}
    finally:
        sink.close()


def test_code_literals_from_changed_or_unsealed_source_are_not_allowed(tmp_path):
    root, pin = code_seal(tmp_path)
    path = root / run._CODE_SOURCE_FILES[0]
    path.write_text("ERROR = 'SKY_UNSEALED_CALLER_CODE'\n")
    with pytest.raises(SupervisionError, match='EVIDENCE_CODE_SEAL_CHANGED'):
        run.evidence_codes_for_sealed_tool(root, pin)
