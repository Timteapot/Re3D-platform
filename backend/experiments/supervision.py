"""Whole-budget stage supervision with independent periodic/terminal evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import shutil
import threading
import time
from pathlib import Path

from backend.experiments.process import run_contained_process
from backend.experiments.runner import read_pair_counts
from backend.re3d_adapter.errors import AdapterError, PipelineTimedOut


class SupervisionError(AdapterError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class WholeRunBudget:
    def __init__(self, seconds=14400, *, monotonic=time.monotonic):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise SupervisionError('LONG_BUDGET_INVALID')
        self.seconds, self.monotonic = seconds, monotonic
        self.started = monotonic()

    @property
    def elapsed(self):
        return max(0.0, self.monotonic() - self.started)

    @property
    def remaining(self):
        return max(0.0, self.seconds - self.elapsed)

    def check(self):
        if self.remaining <= 0:
            raise SupervisionError('LONG_BUDGET_EXHAUSTED')


def error_code(exc):
    if isinstance(exc, KeyboardInterrupt):
        return 'EXPERIMENT_OPERATOR_INTERRUPTED'
    if isinstance(exc, PipelineTimedOut):
        return 'LONG_BUDGET_EXHAUSTED'
    code = getattr(exc, 'code', None)
    return code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,95}', code) else 'LONG_STAGE_OR_IO_FAILED'


def stage_phase(name):
    if name == '01-colmap': return 'sfm'
    if name == '90-validate': return 'validation'
    if 'texture' in name: return 'texture'
    if name == '20-a-mapanything': return 'mapanything'
    if name == '30-b-mvsanywhere': return 'mvsanywhere'
    if any(word in name for word in ('densify', 'reconstruct', 'fuse')): return 'openmvs'
    return 'pipeline'


def bounded_read_only(function, *, timeout_seconds, health_check=lambda: None):
    """Read-only gates may hang in kernel IO; don't hang the supervision owner.

    Timed-out daemon reads are not claimed to be killed or completed, and no
    subsequent compute is launched. They must never write or publish evidence.
    """
    done, values = threading.Event(), []
    def invoke():
        try: values.append((True, function()))
        except BaseException as exc: values.append((False, exc))
        finally: done.set()
    threading.Thread(target=invoke, daemon=True, name='long-readonly-validation').start()
    deadline = time.monotonic() + max(0, timeout_seconds)
    while not done.wait(min(.2, max(0, deadline-time.monotonic()))):
        health_check()
        if time.monotonic() >= deadline:
            raise SupervisionError('LONG_READONLY_VALIDATION_TIMED_OUT')
    if time.monotonic() >= deadline:
        raise SupervisionError('LONG_READONLY_VALIDATION_TIMED_OUT')
    health_check()
    ok, value = values[0]
    if not ok: raise value
    return value


def make_stage_directory(path, root):
    if not path.is_absolute() or not path.is_relative_to(root) or '..' in path.parts:
        raise SupervisionError('LONG_STAGE_DIRECTORY_INVALID')
    for item in (*reversed(path.parents), path):
        if item.exists() and (item.is_symlink() or getattr(item.stat(), 'st_file_attributes', 0) & 1024):
            raise SupervisionError('LONG_STAGE_DIRECTORY_REPARSE')
    path.mkdir(parents=True, exist_ok=True)


class ObservationRelay:
    """Non-blocking-to-owner C witness and pair sampler, each with freshness.

    Resource probes remain in the monitor. Read-only SQLite counters have a SQL
    timeout/progress deadline, and never execute on the compute health callback.
    """
    def __init__(self, monitor, evidence, database, budget, *, resource_seconds=5, pair_seconds=15,
                 pair_reader=read_pair_counts):
        self.monitor, self.evidence, self.database, self.budget = monitor, evidence, database, budget
        self.intervals = {'resources': resource_seconds, 'pairs': pair_seconds}
        self.pair_reader = pair_reader
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.threads, self.last = {}, {}
        self.failure = None
        self.stage = 'restore'
        self.last_pairs = None
        self.pair_samples = []
        self.cpu_samples = []
        self.last_snapshot = None
        self.started = time.monotonic()

    def set_stage(self, name):
        with self.lock:
            self.stage = name

    def start(self):
        for kind in self.intervals:
            thread = threading.Thread(target=self._loop, args=(kind,), daemon=True, name='long-observation-' + kind)
            self.threads[kind] = thread
            thread.start()

    def _loop(self, kind):
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    stage = self.stage
                base = {'stage': stage, 'phase': stage_phase(stage), 'elapsed_seconds': self.budget.elapsed}
                if kind == 'resources':
                    summary = self.monitor.summary()
                    ages = summary.get('observation_ages_seconds', {})
                    self.evidence.append('resources', {**base, 'resources': summary.get('latest', {}),
                        'peaks': summary.get('peaks', {}), 'errors': summary.get('errors', []),
                        'host_age_seconds': ages.get('host'), 'gpu_age_seconds': ages.get('gpu'),
                        'directory_age_seconds': ages.get('directory')})
                    store = getattr(self, 'snapshot_store', None)
                    latest = store.latest if store else None  # Pure memory; no D IO in relay.
                    if latest and latest['directory'] != self.last_snapshot:
                        self.evidence.append('event', {**base, 'event_code':'SNAPSHOT_COMPLETED',
                            'latest_snapshot_id':latest['directory'],'latest_snapshot_sha256':latest['database_sha256'],
                            'complete_pairs':latest['validated_complete_pair_records'],'checkpoint_status':'valid'})
                        self.last_snapshot = latest['directory']
                    cpu = summary.get('latest', {}).get('cpu', {}).get('utilization_percent')
                    if stage == '01-colmap' and isinstance(cpu, (float, int)) and not isinstance(cpu, bool):
                        with self.lock:
                            if len(self.cpu_samples) < 4096:
                                self.cpu_samples.append((self.budget.elapsed, float(cpu)))
                elif stage == '01-colmap' and self.database.is_file():
                    value = self.pair_reader(self.database)
                    with self.lock:
                        self.last_pairs = value
                        # Bounded even for a 4h run. All rows also go to C JSONL.
                        if len(self.pair_samples) < 4096:
                            self.pair_samples.append({**base, **value})
                    self.evidence.append('pairs', {**base, **value})
                with self.lock:
                    self.last[kind] = time.monotonic()
                if self.stop_event.wait(self.intervals[kind]): return
        except BaseException as exc:
            with self.lock:
                self.failure = error_code(exc)

    def check_health(self):
        with self.lock:
            failure, last = self.failure, dict(self.last)
        if failure:
            raise SupervisionError(failure)
        for kind, thread in self.threads.items():
            if not thread.is_alive() or time.monotonic() - last.get(kind, self.started) > max(10, self.intervals[kind] * 3):
                raise SupervisionError('LONG_OBSERVATION_STALE')

    def close(self):
        self.stop_event.set()
        deadline = time.monotonic() + 2
        for thread in self.threads.values():
            thread.join(max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self.threads.values())


class LongStageSupervisor:
    def __init__(self, *, root, run_id, source_job_id, monitor, evidence, isolation, snapshots, budget,
                 image_names, database, gate, process_runner=run_contained_process,
                 forensic_free_reader=None, disk_floor_bytes=20 * 1024**3,
                 notify=lambda _: None, relay_factory=ObservationRelay,
                 source_final_check=lambda health: None, identity=None):
        self.root, self.run_id, self.source_job_id = root, run_id, source_job_id
        self.monitor, self.evidence, self.isolation, self.snapshots = monitor, evidence, isolation, snapshots
        self.budget, self.image_names, self.database, self.gate = budget, image_names, database, gate
        self.process_runner, self.notify = process_runner, notify
        self.forensic_free_reader = forensic_free_reader or (lambda: shutil.disk_usage(root).free)
        self.disk_floor_bytes = disk_floor_bytes
        self.source_final_check = source_final_check
        self.identity = dict(identity or {})
        self.forensic_deadline = None
        self.relay = relay_factory(monitor, evidence, database, budget)
        self.relay.snapshot_store = snapshots
        self.snapshot_running = False
        self.tree_stopped = True
        self.current_stage = 'restore'
        self.stages = []

    def health(self):
        self.budget.check()
        self.monitor.check_health()
        self.evidence.check_health()
        self.isolation.check_health()
        self.relay.check_health()
        if self.snapshot_running: self.snapshots.check_health()

    def _event(self, code, **fields):
        self.evidence.append('event', {'run_id': self.run_id, 'source_job_id': self.source_job_id,
            'stage': self.current_stage, 'phase': stage_phase(self.current_stage), 'event_code': code,
            'elapsed_seconds': self.budget.elapsed, 'remaining_budget_seconds': self.budget.remaining, **fields})

    def _forensic_health(self):
        # NOT runtime health: low-RAM/probe/deadline sticky trips must not
        # prevent independent small evidence or a safe stopped-tree snapshot.
        if not self.tree_stopped:
            raise SupervisionError('LONG_FORENSIC_TREE_NOT_STOPPED')
        if self.forensic_deadline is not None and time.monotonic() >= self.forensic_deadline:
            raise SupervisionError('LONG_FORENSIC_BUDGET_EXHAUSTED')
        free = self.forensic_free_reader()
        if type(free) is not int or free <= self.disk_floor_bytes:
            raise SupervisionError('LONG_FORENSIC_DISK_UNSAFE')

    def _minimal(self, result):
        latest = self.snapshots.latest
        value = {**self.identity, 'run_id': self.run_id, 'source_job_id': self.source_job_id, 'stage': self.current_stage,
            'phase': 'terminal', 'status': result['status'], 'error_code': result.get('error_code'),
            'source_job_modified': False, 'platform_job_resume': False,
            'elapsed_seconds': self.budget.elapsed, 'tree_stopped': self.tree_stopped,
            'stage_count': result['stage_count'], 'completed_stages_count': sum(row['status'] == 'passed' for row in self.stages),
            'checkpoint_status': 'valid' if latest else 'pending',
            'latest_snapshot_id': latest['directory'] if latest else None,
            'latest_snapshot_sha256': latest['database_sha256'] if latest else None,
            'complete_pairs': latest['validated_complete_pair_records'] if latest else None,
            'resources': self.monitor.summary().get('latest', {}),
            'peaks': self.monitor.summary().get('peaks', {}),
            'source_checkpoint_unchanged': result.get('source_checkpoint_unchanged'),
            'forensic_notes': [result[key] for key in ('terminal_evidence_error','snapshot_stop_error',
                'observation_stop_error','resource_stop_error','final_snapshot_error','source_verification_error') if result.get(key)]}
        return {key: item for key, item in value.items() if item is not None}

    def run(self, plan, *, admission_window_seconds=None):
        result = {'schema_version': '1.0', 'mode': 'admission' if admission_window_seconds else 'full',
                  'status': 'incomplete', 'source_job_modified': False, 'platform_job_resume': False,
                  'stage_count': 1 if admission_window_seconds else len(plan.stages), 'stages': self.stages}
        try:
            self.relay.start()
            self.health()
            result['initial_pair_counts'] = read_pair_counts(self.database)
            self._event('RUN_START', status='running')
            for stage in plan.stages[:1] if admission_window_seconds else plan.stages:
                self.health()
                self.current_stage = stage.name
                row = {'stage': stage.name, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
                       'started_at_elapsed_seconds': self.budget.elapsed,
                       'argv': list(stage.argv), 'status': 'running'}  # Only D private report.
                self.stages.append(row)
                self.monitor.set_phase(stage.name)
                self.relay.set_stage(stage.name)
                for path in stage.required_directories: make_stage_directory(path, self.root)
                if stage.name == '01-colmap':
                    self.snapshots.start()
                    self.snapshot_running = True
                self._event('STAGE_START', status='running')
                self.notify({'phase': 'stage_started', 'stage': stage.name})
                started = self.budget.monotonic()
                def on_started(pid):
                    self.tree_stopped = False
                    self.isolation.register_pid(pid)
                def on_tree_stopped():
                    self.tree_stopped = True
                window_elapsed = False
                self.tree_stopped = False  # Fail closed even if spawn/assignment fails before on_started.
                try:
                    outcome = self.process_runner(list(stage.argv), cwd=stage.cwd,
                        log_path=self.root / 'reports' / (stage.name + '-private.log'),
                        timeout_seconds=min(self.budget.remaining, admission_window_seconds) if admission_window_seconds else self.budget.remaining,
                        env=plan.environment, health_check=self.health, on_started=on_started,
                        on_tree_stopped=on_tree_stopped)
                    if not self.tree_stopped: raise SupervisionError('LONG_TREE_STOP_UNCONFIRMED')
                    if outcome.return_code != 0: raise SupervisionError('LONG_STAGE_PROCESS_FAILED')
                    row['return_code'] = outcome.return_code
                except PipelineTimedOut:
                    if not self.tree_stopped: raise SupervisionError('LONG_TREE_STOP_UNCONFIRMED')
                    if not admission_window_seconds or self.budget.remaining <= 0:
                        raise
                    window_elapsed = True
                except BaseException as exc:
                    row.update(status='failed', error_code=error_code(exc))
                    raise
                finally:
                    if self.tree_stopped: self.isolation.clear_owned_pids()
                    row['duration_seconds'] = max(0, self.budget.monotonic() - started)
                    row['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
                if stage.name == '01-colmap':
                    stopped = self.snapshots.stop(timeout=5)
                    self.snapshot_running = False
                    if not stopped: raise SupervisionError('LONG_SNAPSHOT_STOP_UNCONFIRMED')
                self.health()
                if admission_window_seconds:
                    result['admission_duration_seconds'] = row['duration_seconds']
                    result['final_pair_counts'] = read_pair_counts(self.database)
                    row['status'] = 'window_elapsed' if window_elapsed else 'process_completed'
                    row.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                        stage_elapsed_seconds=self.budget.elapsed-row['started_at_elapsed_seconds'],
                        remaining_budget_seconds=self.budget.remaining)
                    result['status'] = 'admission_window_complete'
                    break
                gate_result = bounded_read_only(lambda: self.gate(stage, self.root, self.image_names, health_check=self.health),
                    timeout_seconds=self.budget.remaining, health_check=self.health)
                row.update(gate=gate_result)
                if stage.name == '01-colmap':
                    row['sfm_snapshot'] = self.snapshots.capture_final(tree_stopped=True,
                        health_check=self.health, reason='sfm-complete')
                row.update(status='passed',finished_at_utc=datetime.now(timezone.utc).isoformat(),
                    stage_elapsed_seconds=self.budget.elapsed-row['started_at_elapsed_seconds'],
                    remaining_budget_seconds=self.budget.remaining)
                self._event('STAGE_GATE_PASSED', status='passed', duration_seconds=row['duration_seconds'])
                self.notify({'phase': 'stage_finished', 'stage': stage.name, 'status': 'passed'})
            if not admission_window_seconds:
                self.monitor.finish()
                self.evidence.check_health()
                self.isolation.check_health()
                result['status'] = 'succeeded'
        except BaseException as exc:
            if self.stages and self.stages[-1]['status'] == 'running':
                self.stages[-1].update(status='failed', error_code=error_code(exc))
            if self.stages and self.stages[-1]['stage'] == self.current_stage:
                row = self.stages[-1]
                elapsed = max(0, self.budget.elapsed-row['started_at_elapsed_seconds'])
                row.setdefault('duration_seconds',elapsed)
                row.update(stage_elapsed_seconds=elapsed,remaining_budget_seconds=self.budget.remaining,
                    finished_at_utc=datetime.now(timezone.utc).isoformat())
            result.update(status='timed_out' if isinstance(exc, PipelineTimedOut) or error_code(exc) == 'LONG_BUDGET_EXHAUSTED'
                          else 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed', error_code=error_code(exc))
        finally:
            # Stop compute is guaranteed by run_contained_process before here,
            # except for explicitly recorded unconfirmed containment/termination.
            result['process_tree_stopped'] = self.tree_stopped
            try:
                result['observation_relay_stopped'] = self.relay.close()
            except BaseException as exc:
                result['observation_relay_stopped'] = False
                result['observation_stop_error'] = error_code(exc)
            if not result['observation_relay_stopped'] and result['status'] in {'succeeded','admission_window_complete'}:
                result.update(status='failed', error_code='LONG_OBSERVATION_STOP_UNCONFIRMED')
            # Freeze the final resource summary before terminal acceptance. A
            # writer/probe that does not stop is not silently accepted as safe.
            try:
                self.monitor.close()
                if self.monitor.summary().get('trip') and result['status'] in {'succeeded','admission_window_complete'}:
                    result.update(status='failed', error_code='LONG_RESOURCE_SHUTDOWN_FAILED')
            except BaseException as exc:
                result['resource_stop_error'] = error_code(exc)
                if result['status'] in {'succeeded','admission_window_complete'}:
                    result.update(status='failed', error_code='LONG_RESOURCE_SHUTDOWN_FAILED')
            try:
                stopped = self.snapshots.stop(timeout=5)
            except BaseException as exc:
                stopped = False
                result['snapshot_stop_error'] = error_code(exc)
            if not stopped and result['status'] in {'succeeded','admission_window_complete'}:
                result.update(status='failed', error_code='LONG_SNAPSHOT_STOP_UNCONFIRMED')
            self.snapshot_running = False
            result['snapshot_worker_stopped'] = stopped
            # Small C evidence MUST precede any large hash/backup operation.
            result['terminal_evidence_error'] = None
            terminal_confirmed = False
            try:
                minimal = self._minimal(result)
                minimal['status'] = 'stopped'  # Provisional; forensic-summary carries final acceptance.
                self.evidence.finish_terminal(minimal)
                terminal_confirmed = True
            except BaseException as exc:
                result['terminal_evidence_error'] = error_code(exc)
                if result['status'] in {'succeeded', 'admission_window_complete'}:
                    result.update(status='failed', error_code='LONG_TERMINAL_EVIDENCE_FAILED')
            result['final_snapshot_status'] = 'skipped'
            self.forensic_deadline = time.monotonic() + 120
            result['terminal_evidence_confirmed'] = terminal_confirmed
            if self.tree_stopped and stopped and terminal_confirmed:
                try:
                    snapshot = self.snapshots.capture_final(tree_stopped=True, health_check=self._forensic_health,
                                                            reason='terminal')
                    result.update(final_snapshot_status='valid', final_snapshot=snapshot)
                except BaseException as exc:
                    result.update(final_snapshot_status='failed', final_snapshot_error=error_code(exc))
                    if result['status'] in {'succeeded', 'admission_window_complete'}:
                        result.update(status='failed', error_code='LONG_FINAL_SNAPSHOT_FAILED')
            if self.tree_stopped and terminal_confirmed:
                try:
                    bounded_read_only(lambda: self.source_final_check(self._forensic_health),
                        timeout_seconds=max(0, self.forensic_deadline-time.monotonic()))
                    result['source_checkpoint_unchanged'] = True
                except BaseException as exc:
                    result['source_checkpoint_unchanged'] = None
                    result['source_verification_error'] = error_code(exc)
                    if result['status'] in {'succeeded', 'admission_window_complete'}:
                        result.update(status='failed', error_code='LONG_SOURCE_VERIFICATION_FAILED')
            result['checkpoint_summary'] = self.snapshots.summary()
            result['resources'] = self.monitor.summary()
            result['pair_samples'] = list(self.relay.pair_samples)
            cpu = list(self.relay.cpu_samples)
            result['cpu_sample_count'] = len(cpu)
            result['cpu_sampled_mean_percent'] = sum(value for _, value in cpu) / len(cpu) if cpu else None
            result['cpu_observed_span_seconds'] = cpu[-1][0] - cpu[0][0] if cpu else 0
            windows = []
            for index, (elapsed, _) in enumerate(cpu):
                window = [(stamp, value) for stamp, value in cpu[:index+1] if elapsed-60 <= stamp <= elapsed]
                if len(window) >= 12 and window[-1][0] - window[0][0] >= 55:
                    windows.append(sum(value for _, value in window) / len(window))
            result['cpu_max_60s_mean_percent'] = max(windows) if windows else None
            result['raw_private_logs_may_be_incomplete'] = result['status'] != 'succeeded'
            result['evidence_journal_completion_not_guaranteed'] = True
            result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
            try:
                final = self._minimal(result)
                final['checkpoint_status'] = result['final_snapshot_status']
                final['snapshot_attempted'] = self.tree_stopped and stopped and terminal_confirmed
                self.evidence.write_forensic_summary(final)
            except BaseException as exc:
                result['forensic_evidence_error'] = error_code(exc)
                if result['status'] in {'succeeded', 'admission_window_complete'}:
                    result.update(status='failed', error_code='LONG_FORENSIC_EVIDENCE_FAILED')
            self.isolation.close()
            self.evidence.close()
        return result
