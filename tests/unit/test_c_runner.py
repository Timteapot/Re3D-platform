from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
import json
import threading
import time

import pytest

from backend.experiments.c_runner import CStageSupervisor, verify_handoff_receipt_binding, claim_c_budget
from backend.experiments.c_stages import C_STAGE_NAMES
from backend.experiments.evidence import EvidenceSink
from backend.experiments.process import ContainedProcessOutcome
from backend.experiments.resources import ResourceTrip
from backend.experiments.stages import PipelineStagePlan, StageSpec
from backend.experiments.supervision import WholeRunBudget, SupervisionError


class Monitor:
    def __init__(self):
        self.closed = False
        self.trip = None
    def check_health(self):
        if self.trip: raise self.trip
    def set_phase(self, value): self.phase = value
    def finish(self): self.check_health()
    def close(self): self.closed = True
    def summary(self):
        return {'trip': self.trip.code if self.trip else None, 'latest': {}, 'peaks': {}, 'closed': self.closed}


class Evidence:
    def __init__(self): self.rows = []; self.closed = False
    def check_health(self): pass
    def append(self, kind, value):
        assert kind in ('event', 'resources', 'pairs')
        self.rows.append((kind, value))
    def finish_terminal(self, value): self.rows.append(('terminal', value))
    def write_forensic_summary(self, value): self.rows.append(('forensic', value))
    def close(self): self.closed = True


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'experiment'
    (root / 'reports').mkdir(parents=True)
    stages = tuple(StageSpec(name, ('synthetic.exe', name), root / ('marker-' + name), root)
                   for name in C_STAGE_NAMES)
    plan = PipelineStagePlan(stages, {}, (1, 1))
    monitor, evidence = Monitor(), Evidence()
    calls = []
    def process(argv, **kwargs):
        calls.append(argv[1]); kwargs['health_check']()
        stages[len(calls)-1].marker.write_bytes(b'nonempty synthetic output')
        kwargs['on_tree_stopped']()
        return ContainedProcessOutcome(0, .01, 1, True)
    owner = CStageSupervisor(root=root, monitor=monitor, evidence=evidence, budget=WholeRunBudget(30),
        image_names=('image.jpg',), identity={'config_sha256': 'a'*64},
        gate=lambda stage, *args, **kwargs: {'gate_passed': True}, process_runner=process,
        sample_seconds=.01)
    return SimpleNamespace(root=root, stages=stages, plan=plan, monitor=monitor, evidence=evidence,
                           owner=owner, calls=calls, process=process)


def test_success_requires_eight_gates_stop_and_small_evidence_before_source_check(case):
    c = case
    def source(health):
        assert any(kind == 'terminal' for kind, _ in c.evidence.rows)
        assert c.monitor.closed
    c.owner.source_final_check = source
    result = c.owner.run(c.plan)
    assert result['status'] == 'succeeded'
    assert c.calls == list(C_STAGE_NAMES)
    assert result['source_handoff_unchanged'] is True and result['terminal_evidence_confirmed'] is True
    assert result['process_tree_stopped'] and result['observation_stopped']
    assert c.evidence.closed and c.monitor.closed
    assert len(list((c.root/'reports').glob('c-stage-*.json'))) == 8
    assert json.loads((c.root/'reports/c-result.json').read_bytes())['status'] == 'succeeded'
    assert not result['server_accessed'] and not result['gpu_stage_started']


@pytest.mark.parametrize('bad', ('return_code', 'stop_callback', 'outcome_stop', 'gate'))
def test_failed_process_or_gate_never_starts_next_stage(case, bad):
    c = case
    def process(argv, **kwargs):
        c.calls.append(argv[1])
        if bad != 'stop_callback': kwargs['on_tree_stopped']()
        return ContainedProcessOutcome(1 if bad == 'return_code' else 0, .01, 1, bad != 'outcome_stop')
    c.owner.process_runner = process
    if bad == 'gate': c.owner.gate = Mock(side_effect=SupervisionError('C_OUTPUT_VALIDATION_REJECTED'))
    result = c.owner.run(c.plan)
    assert result['status'] == 'failed' and len(c.calls) == 1
    assert not list((c.root/'reports').glob('c-stage-*.json'))


@pytest.mark.parametrize('code', ('LOW_RAM_AUTO_STOPPED', 'LOW_DISK_AUTO_STOPPED'))
def test_sticky_resource_trip_stops_compute_and_keeps_evidence(case, code):
    c = case
    def process(argv, **kwargs):
        c.calls.append(argv[1]); c.monitor.trip = ResourceTrip(code)
        try: kwargs['health_check']()
        finally: kwargs['on_tree_stopped']()
    c.owner.process_runner = process
    result = c.owner.run(c.plan)
    assert result['status'] == 'failed' and result['error_code'] == code
    assert result['process_tree_stopped'] and result['terminal_evidence_confirmed']
    assert len(c.calls) == 1 and c.monitor.closed


def test_existing_marker_is_not_reused(case):
    c = case; c.stages[0].marker.touch()
    result = c.owner.run(c.plan)
    assert result['status'] == 'failed' and result['error_code'] == 'LOCAL_C_EXISTING_STAGE_MARKER'
    assert not c.calls


def test_malformed_plan_does_not_launch(case):
    c = case
    result = c.owner.run(PipelineStagePlan(c.stages[1:], {}, (1,1)))
    assert result['error_code'] == 'LOCAL_C_PLAN_REQUIRED' and not c.calls


def test_budget_is_not_reset_per_stage(case):
    c = case; clock = [0.]
    c.owner.budget = WholeRunBudget(1, monotonic=lambda: clock[0])
    def process(argv, **kwargs):
        clock[0] += 2
        try: return c.process(argv, **kwargs)
        finally: kwargs['on_tree_stopped']()
    c.owner.process_runner = process
    result = c.owner.run(c.plan)
    assert result['status'] == 'timed_out' and result['remaining_budget_seconds'] == 0
    assert len(c.calls) == 1


def test_source_change_rejects_otherwise_successful_outputs(case):
    c = case
    c.owner.source_final_check = Mock(side_effect=SupervisionError('HANDOFF_MANIFEST_CHANGED'))
    result = c.owner.run(c.plan)
    assert result['status'] == 'failed' and result['error_code'] == 'LOCAL_C_SOURCE_CHANGED'
    assert result['source_handoff_unchanged'] is None


def test_terminal_failure_prevents_large_forensics(case):
    c = case
    c.evidence.finish_terminal = Mock(side_effect=OSError())
    c.owner.source_final_check = Mock()
    result = c.owner.run(c.plan)
    assert result['status'] == 'failed' and not result['terminal_evidence_confirmed']
    c.owner.source_final_check.assert_not_called()


def test_journal_failure_prevents_next_stage(case):
    c = case; c.owner.persist = Mock(side_effect=OSError())
    result = c.owner.run(c.plan)
    assert result['status'] == 'failed' and len(c.calls) == 1


def test_blocked_observer_is_not_allowed_to_stall_owner(case):
    c = case; release = threading.Event()
    def notify(value): release.wait(3)
    c.owner.notify = notify
    def process(argv, **kwargs):
        c.calls.append(argv[1])
        time.sleep(.03)
        c.owner.last_observation = time.monotonic() - 16
        try: kwargs['health_check']()
        finally: kwargs['on_tree_stopped']()
    c.owner.process_runner = process
    try:
        result = c.owner.run(c.plan)
    finally: release.set()
    assert result['status'] == 'failed' and result['error_code'] == 'LOCAL_C_OBSERVATION_STALE'
    assert len(c.calls) == 1


def test_real_evidence_sink_interface_is_compatible(case, tmp_path):
    c = case
    sink = EvidenceSink.create_for_test(tmp_path/'private-c', free_reader=lambda _: 50*1024**3,
        known_stages=C_STAGE_NAMES)
    sink.start(); c.owner.evidence = sink
    result = c.owner.run(c.plan)
    assert result['status'] == 'succeeded'
    assert (sink.root/'terminal.json').is_file()
    terminal = json.loads((sink.root/'terminal.json').read_bytes())
    assert 'UNCLASSIFIED_ERROR' not in str(terminal)


def test_handoff_binding_requires_same_completed_database_and_model():
    sfm = {'database_images': 300, 'registered_images': 300}
    verified = SimpleNamespace(manifest={'files': [{'relative_path':'work/shared/colmap/database.db', 'sha256':'a'*64}],
        'sfm_gate':sfm, 'execution': {'source_experiment_root':'D:/_experiments/cpu-v3'},
        'source':{'source_job_id':'job'}})
    receipt = {'hashes':{'final_database_sha256':'a'*64}, 'result':{'final_sfm_gate':sfm},
               'scene':'cpu-v3', 'source_job_id':'job'}
    verify_handoff_receipt_binding(verified, receipt)
    receipt['hashes']['final_database_sha256'] = 'b'*64
    with pytest.raises(SupervisionError, match='HANDOFF_RECEIPT_MISMATCH'):
        verify_handoff_receipt_binding(verified, receipt)


def test_same_cpu_result_cannot_get_fresh_budget_by_changing_scene(tmp_path):
    path = claim_c_budget(tmp_path, cpu_result_sha256='a'*64, profile_sha256='b'*64,
                         scene='first-c', remaining_seconds=9479.782)
    before = path.read_bytes()
    with pytest.raises(SupervisionError, match='BUDGET_ALREADY_CLAIMED'):
        claim_c_budget(tmp_path, cpu_result_sha256='a'*64, profile_sha256='c'*64,
                       scene='second-c', remaining_seconds=9479.782)
    assert path.read_bytes() == before
