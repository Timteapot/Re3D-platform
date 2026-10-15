"""Opt-in local C-only continuation of an externally verified SfM handoff.

No queue, server, CUDA, automatic retry, marker skipping, or in-flight OpenMVS
resume. Stage receipts retain hashes, not standalone copies of the working tree.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time

from backend.experiments.c_profile import validate_c_profile, validate_terminal_binding
from backend.experiments.c_stages import C_STAGE_NAMES, build_c_stage_plan, gate_c_stage
from backend.experiments.cpu_runner import ordinary
from backend.experiments.evidence import EvidenceSink, PRIVATE_PARENT
from backend.experiments.high_profile import BASE_COMMIT, BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA, PIPELINE_SCRIPT_SHA
from backend.experiments.hybrid_checkpoint import verify_hybrid_source
from backend.experiments.hybrid_handoff import import_sfm_handoff, verify_sfm_handoff
from backend.experiments.long_runner import bounded_private_report, exclusive_long_test
from backend.experiments.preflight import require_admin, verify_git_identity
from backend.experiments.process import run_contained_process
from backend.experiments.resources import ExperimentResourceMonitor, ResourceLimits, GIB
from backend.experiments.runner import create_private_root, read_json, write_exclusive
from backend.experiments.stages import prepared_dimensions
from backend.experiments.supervision import (WholeRunBudget, SupervisionError, bounded_read_only,
    error_code, make_stage_directory, stage_phase)
from backend.monitoring.resources import HostResourceReader


def require(condition, code):
    if not condition:
        raise SupervisionError(code)


def digest(path, health=lambda: None):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024**2):
            health(); value.update(block)
    health()
    return value.hexdigest()


def claim_c_budget(parent, *, cpu_result_sha256, profile_sha256, scene, remaining_seconds):
    """One C attempt per accepted CPU result, even if a different scene is used.

    A future manual retry needs explicit audited reconciliation, not deletion or
    reuse of this claim. This entry deliberately has no reset/retry switch.
    """
    import re
    require(re.fullmatch(r'[0-9a-f]{64}', cpu_result_sha256)
            and re.fullmatch(r'[0-9a-f]{64}', profile_sha256)
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', scene), 'LOCAL_C_BUDGET_CLAIM_INVALID')
    ordinary(Path(parent))
    path = Path(parent) / ('c-budget-' + cpu_result_sha256 + '.json')
    ordinary(path, missing_leaf=True)
    try:
        write_exclusive(path, {'schema_version': '1.0', 'mode': 'local-c-only',
            'cpu_result_sha256': cpu_result_sha256, 'profile_sha256': profile_sha256,
            'scene': scene, 'remaining_seconds_at_admission': remaining_seconds,
            'created_at_utc': datetime.now(timezone.utc).isoformat(), 'automatic_retry_allowed': False})
    except FileExistsError:
        raise SupervisionError('LOCAL_C_BUDGET_ALREADY_CLAIMED') from None
    return path


class CStageSupervisor:
    """Sequential contained children; first failed gate prevents all later stages."""
    def __init__(self, *, root, monitor, evidence, budget, image_names, identity,
                 gate=gate_c_stage, process_runner=run_contained_process,
                 source_final_check=lambda health: None, notify=lambda value: None,
                 persist=bounded_private_report, sample_seconds=5):
        self.root, self.monitor, self.evidence, self.budget = root, monitor, evidence, budget
        self.image_names, self.identity = image_names, identity
        self.gate, self.process_runner = gate, process_runner
        self.source_final_check, self.notify, self.persist = source_final_check, notify, persist
        self.sample_seconds = sample_seconds
        self.stage, self.tree_stopped, self.rows = None, True, []
        self.stop = threading.Event()
        self.publisher_error = None
        self.publisher = None
        self.last_observation = time.monotonic()

    def health(self):
        self.budget.check(); self.monitor.check_health(); self.evidence.check_health()
        require(self.publisher_error is None, 'LOCAL_C_OBSERVATION_FAILED')
        if self.publisher is not None:
            require(self.publisher.is_alive(), 'LOCAL_C_OBSERVATION_STOPPED')
            require(time.monotonic() - self.last_observation <= max(15, self.sample_seconds * 3),
                    'LOCAL_C_OBSERVATION_STALE')

    def observation(self, status='running'):
        summary = self.monitor.summary()
        return {'status': status, 'phase': stage_phase(self.stage) if self.stage else 'preflight',
                'stage': self.stage, 'elapsed_seconds': self.budget.elapsed,
                'remaining_budget_seconds': self.budget.remaining,
                'stage_count': len(C_STAGE_NAMES),
                'completed_stages_count': sum(row['status'] == 'passed' for row in self.rows),
                'resources': summary.get('latest', {}), 'peaks': summary.get('peaks', {}),
                **self.identity}

    def _observe(self):
        try:
            while not self.stop.wait(self.sample_seconds):
                value = self.observation()
                self.evidence.append('resources', value)
                self.notify(value)
                self.last_observation = time.monotonic()
        except BaseException:
            self.publisher_error = 'LOCAL_C_OBSERVATION_FAILED'

    def run(self, plan):
        result = {'schema_version': '1.0', 'mode': 'local-c-only', 'status': 'incomplete',
                  'stages': self.rows, 'source_job_modified': False, 'platform_job_resume': False,
                  'server_accessed': False, 'gpu_stage_started': False, **self.identity}
        try:
            require(tuple(stage.name for stage in plan.stages) == C_STAGE_NAMES,
                    'LOCAL_C_PLAN_REQUIRED')
            self.publisher = threading.Thread(target=self._observe, daemon=True, name='local-c-observation')
            self.publisher.start()
            self.health()
            for index, stage in enumerate(plan.stages):
                self.stage = stage.name
                self.monitor.set_phase(stage_phase(stage.name))
                self.health()
                # No marker reuse, including a leftover from a partial attempt.
                require(not stage.marker.exists(), 'LOCAL_C_EXISTING_STAGE_MARKER')
                for directory in stage.required_directories:
                    make_stage_directory(directory, self.root)
                row = {'stage': stage.name, 'status': 'running',
                       'started_at_utc': datetime.now(timezone.utc).isoformat()}
                self.rows.append(row)
                self.evidence.append('event', {'event_code': 'STAGE_START', **self.observation()})
                self.tree_stopped = False  # A failed spawn must not assert stop proof.
                def stopped():
                    self.tree_stopped = True
                outcome = self.process_runner(list(stage.argv), cwd=stage.cwd,
                    log_path=self.root / 'reports' / (stage.name + '-private.log'),
                    timeout_seconds=self.budget.remaining, env=plan.environment,
                    health_check=self.health, on_tree_stopped=stopped)
                row.update(return_code=outcome.return_code, duration_seconds=outcome.duration_seconds)
                require(self.tree_stopped and outcome.tree_stopped_confirmed, 'LOCAL_C_TREE_STOP_UNCONFIRMED')
                require(outcome.return_code == 0, 'LOCAL_C_PROCESS_FAILED')
                admitted = bounded_read_only(lambda: self.gate(stage, self.root, self.image_names,
                    health_check=self.health), timeout_seconds=self.budget.remaining, health_check=self.health)
                row.update(status='passed', gate=admitted, remaining_budget_seconds=self.budget.remaining,
                           finished_at_utc=datetime.now(timezone.utc).isoformat())
                # This is a gate receipt. No working file is copied or claimed to
                # be restartable in the middle of an OpenMVS stage.
                receipt = self.root / 'reports' / f'c-stage-{index+1:02d}.json'
                self.persist(receipt, {'schema_version': '1.0', **self.identity, **row,
                    'process_tree_stopped': True, 'automatic_resume_supported': False})
                self.evidence.append('event', {'event_code': 'STAGE_GATE_PASSED', **self.observation()})
            self.health()
            self.monitor.finish()
            result['status'] = 'succeeded'
        except BaseException as exc:
            if self.rows and self.rows[-1]['status'] == 'running':
                self.rows[-1].update(status='failed', error_code=error_code(exc))
            code = error_code(exc)
            result.update(status='timed_out' if code == 'LONG_BUDGET_EXHAUSTED' else
                          'cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed', error_code=code)
        finally:
            self.stop.set()
            if self.publisher is not None:
                self.publisher.join(timeout=2)
            result['observation_stopped'] = self.publisher is None or not self.publisher.is_alive()
            result['process_tree_stopped'] = self.tree_stopped
            try:
                self.monitor.close()
                result['resources'] = self.monitor.summary()
                require(not result['resources'].get('trip'), 'LOCAL_C_RESOURCE_SHUTDOWN_FAILED')
                require(result['observation_stopped'] and self.publisher_error is None,
                        'LOCAL_C_OBSERVATION_FAILED')
                require(self.tree_stopped, 'LOCAL_C_TREE_STOP_UNCONFIRMED')
            except BaseException as exc:
                result['shutdown_error'] = error_code(exc)
                if result['status'] == 'succeeded':
                    result.update(status='failed', error_code=error_code(exc))
            result.update(elapsed_seconds=self.budget.elapsed,
                          remaining_budget_seconds=self.budget.remaining,
                          finished_at_utc=datetime.now(timezone.utc).isoformat())
            # Small independent evidence before any terminal source hashing.
            try:
                minimal = {**self.observation('stopped'), 'tree_stopped': self.tree_stopped}
                if result.get('error_code'): minimal['error_code'] = result['error_code']
                self.evidence.finish_terminal(minimal)
                result['terminal_evidence_confirmed'] = True
            except BaseException as exc:
                result['terminal_evidence_confirmed'] = False
                result['terminal_evidence_error'] = error_code(exc)
                if result['status'] == 'succeeded':
                    result.update(status='failed', error_code='LOCAL_C_TERMINAL_EVIDENCE_FAILED')
            result['source_handoff_unchanged'] = None
            if self.tree_stopped and result['terminal_evidence_confirmed']:
                try:
                    # Verification has its own bounded forensic allowance, not
                    # additional compute time. No later child can be launched.
                    bounded_read_only(lambda: self.source_final_check(lambda: None), timeout_seconds=120)
                    result['source_handoff_unchanged'] = True
                except BaseException as exc:
                    result['source_verification_error'] = error_code(exc)
                    if result['status'] == 'succeeded':
                        result.update(status='failed', error_code='LOCAL_C_SOURCE_CHANGED')
            try:
                final = {**self.observation(result['status']), 'tree_stopped': self.tree_stopped}
                if result['source_handoff_unchanged'] is not None:
                    final['source_checkpoint_unchanged'] = result['source_handoff_unchanged']
                if result.get('error_code'): final['error_code'] = result['error_code']
                self.evidence.write_forensic_summary(final)
                self.persist(self.root / 'reports/c-result.json', result)
            except BaseException as exc:
                result['result_persistence_error'] = error_code(exc)
                if result['status'] == 'succeeded':
                    result.update(status='failed', error_code='LOCAL_C_RESULT_PERSISTENCE_FAILED')
            self.evidence.close()
        return result


def verify_handoff_receipt_binding(verified, receipt):
    """Tie a verified handoff's DB/model to the accepted CPU terminal receipt."""
    database = next((row for row in verified.manifest['files']
                     if row['relative_path'] == 'work/shared/colmap/database.db'), None)
    require(database and database['sha256'] == receipt['hashes']['final_database_sha256']
            and verified.manifest['sfm_gate']['database_images'] == 300
            and verified.manifest['sfm_gate']['registered_images'] == 300
            and verified.manifest['sfm_gate'] == receipt['result']['final_sfm_gate']
            and Path(verified.manifest['execution']['source_experiment_root']).name == receipt['scene']
            and verified.manifest['source']['source_job_id'] == receipt['source_job_id'],
            'LOCAL_C_HANDOFF_RECEIPT_MISMATCH')


def execute_local_c(args, profile, *, tool_identity):
    """Explicit execution only, with no implicit elevation or server access."""
    admission_started = time.monotonic()
    validate_c_profile(profile)
    require(getattr(args, 'execute_c_only', False) is True, 'LOCAL_C_EXPLICIT_EXECUTION_REQUIRED')
    require_admin()
    root = ordinary(Path(profile['target_experiment']['root']), missing_leaf=True)
    require(not root.exists(), 'LOCAL_C_NEW_TARGET_REQUIRED')
    pipeline = ordinary(Path(args.pipeline_root))
    verify_git_identity(pipeline, BASE_COMMIT)
    config = pipeline / 'configs/pipeline.json'
    require(digest(config) == BASE_CONFIG_SHA
            and digest(pipeline / 'scripts/run_pipeline.py') == PIPELINE_SCRIPT_SHA
            and digest(pipeline / 'scripts/run_colmap_shared.py') == COLMAP_SCRIPT_SHA,
            'LOCAL_C_FROZEN_IDENTITY_CHANGED')
    from scripts.run_local_c import read as read_pinned
    terminal = read_pinned(args.terminal_receipt, expected_sha256=profile['source']['terminal_receipt_sha256'])
    budget_values = validate_terminal_binding(profile, terminal, profile['source']['terminal_receipt_sha256'])
    budget = WholeRunBudget(budget_values['remaining_seconds'])
    budget.started = admission_started  # Admission does not get a fresh timeout.
    budget.check()
    source_profile = read_pinned(args.source_cpu_profile, expected_sha256=args.source_cpu_profile_sha256)
    from backend.experiments.cpu_runner import validate_cpu_profile
    validate_cpu_profile(source_profile)
    require(source_profile['target_experiment']['scene'] == terminal['scene']
            and args.source_cpu_profile_sha256 == terminal['tool_identity']['profile_sha256'],
            'LOCAL_C_SOURCE_PROFILE_CHANGED')
    guards = profile['protection']
    host, errors = HostResourceReader().read(root.parent)
    hardware = profile['target_hardware']
    require(not errors and host['cpu']['logical_count'] == hardware['logical_cpus']
            and abs(host['memory']['total_bytes'] - hardware['host_ram_bytes']) <= GIB,
            'LOCAL_C_HARDWARE_CHANGED')
    require(host['memory']['total_bytes'] - host['memory']['used_bytes'] > guards['available_ram_floor_bytes'],
            'LOCAL_C_RAM_ADMISSION_FAILED')
    require(shutil.disk_usage(root.parent).free >= guards['reservation_bytes'] + guards['disk_floor_bytes'],
            'LOCAL_C_DISK_ADMISSION_FAILED')
    require(shutil.disk_usage('C:\\').free >= guards['evidence_creation_free_bytes'],
            'LOCAL_C_EVIDENCE_ADMISSION_FAILED')
    # Runtime files and their DLL closure are externally pinned, not inferred from
    # presence alone. Imports/weights for learned A/B are deliberately unnecessary.
    from scripts.run_local_c import verify_runtime_manifest
    runtime = verify_runtime_manifest(Path(args.runtime_manifest), args.runtime_manifest_sha256, pipeline)
    identity = {'profile_sha256': digest(args.profile), 'tool_manifest_sha256': tool_identity,
                'source_manifest_sha256': profile['source']['handoff_manifest_sha256'],
                'pipeline_commit': BASE_COMMIT, 'config_sha256': BASE_CONFIG_SHA}
    def verify(health):
        source = source_profile['source_checkpoint']
        verified_input = verify_hybrid_source(**source, source_job_id=source_profile['source_job_id'],
            pipeline_commit=BASE_COMMIT, config_sha256=BASE_CONFIG_SHA, health_check=health)
        handoff = verify_sfm_handoff(profile['source']['root'], profile['source']['handoff_manifest_sha256'],
            input_source=verified_input, expected_quality_config_sha256=BASE_CONFIG_SHA,
            expected_pipeline_script_sha256=PIPELINE_SCRIPT_SHA,
            expected_colmap_script_sha256=COLMAP_SCRIPT_SHA, health_check=health)
        verify_handoff_receipt_binding(handoff, terminal)
        return verified_input, handoff
    with exclusive_long_test():
        evidence = EvidenceSink.create_production(profile['target_experiment']['scene'],
            known_stages=C_STAGE_NAMES, known_codes=LOCAL_C_CODES)
        monitor = supervisor = None
        probe_stopped, probe_started = [], False
        budget_claim = None
        final_result = None
        sleep_api = ctypes.WinDLL('kernel32', use_last_error=True)
        sleep_api.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        sleep_api.SetThreadExecutionState.restype = ctypes.c_uint32
        sleeping = False
        try:
            evidence.start()
            budget_claim = claim_c_budget(PRIVATE_PARENT,
                cpu_result_sha256=profile['source']['cpu_result_sha256'],
                profile_sha256=identity['profile_sha256'], scene=profile['target_experiment']['scene'],
                remaining_seconds=budget.remaining)
            create_private_root(root, kind='independent_long_pipeline')
            monitor = ExperimentResourceMonitor(experiment_root=root,
                sample_path=root / 'reports/resource-samples.jsonl', limits=ResourceLimits(
                    minimum_available_ram_bytes=guards['available_ram_floor_bytes'],
                    minimum_disk_free_bytes=guards['disk_floor_bytes'], gpu_required=False))
            monitor.start()
            def health():
                budget.check(); monitor.check_health(); evidence.check_health()
            monitor.set_phase('restore')
            # Bound reads; import writes remain synchronous and health-checked on
            # chunks. A daemon read timing out never starts a later child.
            source, handoff = bounded_read_only(lambda: verify(health),
                timeout_seconds=budget.remaining, health_check=health)
            imported = import_sfm_handoff(handoff, root, input_source=source,
                health_check=health, target_work_root=root / 'work')
            dimensions = prepared_dimensions(imported.input_manifest_path, root, source.image_names)
            plan = build_c_stage_plan(pipeline_root=pipeline, config_path=config,
                paths_path=pipeline / 'configs/paths.local.json', experiment_root=root, dimensions=dimensions)
            from scripts.run_local_c import verify_plan_runtime, verify_runtime_stage
            verify_plan_runtime(plan, runtime)
            # Bound OpenMP/BLAS helper pools as well as OpenMVS --max-threads.
            for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                plan.environment[key] = '8'
            plan.environment['CUDA_VISIBLE_DEVICES'] = '-1'
            plan.environment['PYTHONDONTWRITEBYTECODE'] = '1'
            for key in tuple(plan.environment):
                if key.startswith(('APP_', 'DATABASE_', 'JWT_', 'AUTH_', 'SMTP_', 'ACCESS_TOKEN_', 'REFRESH_', 'VITE_')):
                    plan.environment.pop(key, None)
            probe_started = True
            probe = run_contained_process([runtime['python'], '-I', '-B', '-c',
                'import json,numpy,scipy,PIL,trimesh; print(json.dumps({"numpy":numpy.__version__,"scipy":scipy.__version__,"pillow":PIL.__version__,"trimesh":trimesh.__version__}))'],
                cwd=pipeline, log_path=root / 'reports/c-python-imports-private.log',
                timeout_seconds=min(60, budget.remaining), env=plan.environment, health_check=health,
                on_tree_stopped=lambda: probe_stopped.append(True))
            require(probe.return_code == 0 and probe.tree_stopped_confirmed and probe_stopped,
                    'LOCAL_C_PYTHON_DEPENDENCIES_UNAVAILABLE')
            bounded_private_report(root / 'reports/effective-c-profile.json', profile)
            bounded_private_report(root / 'reports/executed-c-plan.json', {'schema_version': '1.0',
                **identity, 'budget_binding': budget_values, 'stages': [stage.observation() for stage in plan.stages],
                'mode': 'local-c-only', 'source_sfm_rerun': False})
            require(sleep_api.SetThreadExecutionState(0x80000001), 'LOCAL_C_SLEEP_GUARD_FAILED')
            sleeping = True
            def checked_process(argv, **kwargs):
                stage = next(item for item in plan.stages if list(item.argv) == argv)
                verify_runtime_stage(stage, runtime)
                return run_contained_process(argv, **kwargs)
            supervisor = CStageSupervisor(root=root, monitor=monitor, evidence=evidence, budget=budget,
                image_names=source.image_names, identity=identity, source_final_check=lambda h: verify(h),
                process_runner=checked_process,
                notify=lambda value: print(json.dumps(value, ensure_ascii=True), flush=True))
            final_result = supervisor.run(plan)
            return final_result
        except BaseException as exc:
            result = {'schema_version': '1.0', 'mode': 'local-c-only', 'status': 'failed',
                      'error_code': error_code(exc), 'reconstruction_started': supervisor is not None,
                      'server_accessed': False, 'source_job_modified': False,
                      'process_tree_stopped': supervisor.tree_stopped if supervisor else
                          (bool(probe_stopped) if probe_started else True),
                      'elapsed_seconds': budget.elapsed, 'remaining_budget_seconds': budget.remaining, **identity}
            try:
                evidence.finish_terminal({'status': 'failed', 'phase': 'terminal', 'error_code': error_code(exc), **identity})
                result['terminal_evidence_confirmed'] = True
            except BaseException:
                result['terminal_evidence_confirmed'] = False
            if root.is_dir():
                try: bounded_private_report(root / 'reports/c-preflight-failure.json', result)
                except BaseException: pass
            final_result = result
            return final_result
        finally:
            if supervisor is None:
                if monitor is not None: monitor.close()
                evidence.close()
            if sleeping:
                released = bool(sleep_api.SetThreadExecutionState(0x80000000))
                if final_result is not None:
                    final_result['sleep_request_released'] = released
                    if not released:
                        final_result.update(status='failed', error_code='LOCAL_C_SLEEP_RELEASE_UNCONFIRMED')
            if budget_claim is not None:
                # Keep an immutable charge next to the immutable one-shot claim.
                # A failed terminal write never permits another automatic run.
                charge = {'schema_version': '1.0', 'mode': 'local-c-only',
                    'status': final_result.get('status', 'failed') if final_result else 'failed',
                    'elapsed_seconds': budget.elapsed, 'remaining_budget_seconds': budget.remaining,
                    'prior_sfm_charge_seconds': budget_values['consumed_sfm_seconds'],
                    'cumulative_charge_seconds': budget_values['consumed_sfm_seconds'] + budget.elapsed,
                    'profile_sha256': identity['profile_sha256'], 'automatic_retry_allowed': False}
                try:
                    bounded_private_report(budget_claim.with_name(budget_claim.stem + '-charge.json'), charge)
                    if final_result is not None: final_result['budget_charge_confirmed'] = True
                except BaseException:
                    if final_result is not None:
                        final_result.update(status='failed', budget_charge_confirmed=False,
                                            error_code='LOCAL_C_BUDGET_CHARGE_UNCONFIRMED')
            # The stage result precedes release/accounting. Only this final
            # receipt can certify execution including cleanup and budget charge.
            if final_result is not None and (root / 'reports').is_dir():
                try:
                    bounded_private_report(root / 'reports/c-final-status.json', final_result)
                except BaseException:
                    final_result.update(status='failed', error_code='LOCAL_C_FINAL_RECEIPT_UNCONFIRMED')


LOCAL_C_CODES = frozenset({
    'LOCAL_C_PLAN_REQUIRED', 'LOCAL_C_EXISTING_STAGE_MARKER', 'LOCAL_C_TREE_STOP_UNCONFIRMED',
    'LOCAL_C_PROCESS_FAILED', 'LOCAL_C_OBSERVATION_FAILED', 'LOCAL_C_OBSERVATION_STOPPED', 'LOCAL_C_OBSERVATION_STALE',
    'LOCAL_C_RESOURCE_SHUTDOWN_FAILED', 'LOCAL_C_TERMINAL_EVIDENCE_FAILED', 'LOCAL_C_SOURCE_CHANGED',
    'LOCAL_C_RESULT_PERSISTENCE_FAILED', 'LOCAL_C_HANDOFF_RECEIPT_MISMATCH',
    'LOCAL_C_SLEEP_GUARD_FAILED', 'LONG_BUDGET_EXHAUSTED', 'LONG_READONLY_VALIDATION_TIMED_OUT',
    'LOCAL_C_BUDGET_ALREADY_CLAIMED', 'LOCAL_C_BUDGET_CHARGE_UNCONFIRMED',
    'LOCAL_C_PYTHON_DEPENDENCIES_UNAVAILABLE', 'LOCAL_C_RUNTIME_CHANGED_AFTER_ADMISSION',
    'LOCAL_C_RUNTIME_HASH_CHANGED',
    'LOCAL_C_SLEEP_RELEASE_UNCONFIRMED', 'LOCAL_C_FINAL_RECEIPT_UNCONFIRMED',
})
