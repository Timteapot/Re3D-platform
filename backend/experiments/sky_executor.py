"""Explicit, opt-in sky C continuation execution; never elevates or retries.

This closure is not invoked by the input-preparation CLI. A separately authorized
caller must supply its exact execution switch and pinned supervised profile.
The old C/web executors, frozen algorithm and historical budget files are not
modified. All children still run through the existing Windows Job Object owner.
"""
from __future__ import annotations

import ctypes
import ast
import hashlib
import json
from pathlib import Path
import re
import time

from backend.experiments.c_budget_chain import (
    claim_c_continuation_budget, write_c_continuation_charge,
)
from backend.experiments.c_runner import CStageSupervisor, LOCAL_C_CODES
from backend.experiments.c_stages import C_STAGE_NAMES, gate_c_stage
from backend.experiments.cpu_runner import ordinary
from backend.experiments.evidence import EvidenceSink, PRIVATE_PARENT
from backend.experiments.high_profile import BASE_COMMIT
from backend.experiments.hybrid_handoff import import_sfm_handoff
from backend.experiments.long_runner import bounded_private_report, exclusive_long_test
from backend.experiments.preflight import require_admin, verify_git_identity
from backend.experiments.process import run_contained_process
from backend.experiments.resources import ExperimentResourceMonitor, ResourceLimits, GIB
from backend.experiments.runner import create_private_root
from backend.experiments.stages import prepared_dimensions
from backend.experiments.supervision import WholeRunBudget, SupervisionError, error_code


SKY_EXECUTOR_CODES = frozenset({
    'SKY_EXECUTE_EXPLICIT_REQUIRED', 'SKY_EXECUTE_NEW_TARGET_REQUIRED',
    'SKY_EXECUTE_RESOURCE_CONFIGURATION_CHANGED', 'SKY_EXECUTE_THREAD_CONFIGURATION_CHANGED',
    'SKY_EXECUTE_PLAN_CHANGED', 'SKY_EXECUTE_MASK_INVENTORY_REQUIRED',
    'SKY_EXECUTE_DEPENDENCIES_UNAVAILABLE', 'SKY_EXECUTE_SLEEP_GUARD_FAILED',
    'SKY_EXECUTE_SLEEP_RELEASE_UNCONFIRMED', 'SKY_EXECUTE_RESOURCE_THREADS_UNCONFIRMED',
    'SKY_EXECUTE_EVIDENCE_THREADS_UNCONFIRMED', 'SKY_EXECUTE_BUDGET_CHARGE_UNCONFIRMED',
    'SKY_EXECUTE_FINAL_RECEIPT_UNCONFIRMED', 'SKY_EXECUTE_SOURCE_PROOF_UNCONFIRMED',
    'SKY_EXECUTE_UNEXPECTED_STAGE',
    'SKY_EXECUTE_EVIDENCE_CODE_SEAL_CHANGED',
})
_CODE_SOURCE_FILES = tuple('backend/experiments/' + name + '.py' for name in (
    'sky_masks', 'sky_sfm_binding', 'sky_mask_plan', 'sky_assets', 'sky_supervision',
    'sky_executor', 'c_budget_chain')) + ('scripts/run_sky_c_supervised.py',)
_EVIDENCE_CODE = re.compile(r'(?:SKY|LOCAL_C_SKY|LOCAL_C_BUDGET)_[A-Z][A-Z0-9_]*\Z')


def require(value, code):
    if not value:
        raise SupervisionError(code)


def _threads_stopped(owner):
    """Confirm shutdown of only this closure's owned background workers."""
    if owner is None:
        return True
    threads = list(getattr(owner, '_threads', {}).values())
    threads += list(getattr(owner, '_terminal_threads', []))
    return getattr(owner, '_closed', False) is True and all(not thread.is_alive() for thread in threads)


def _cpu_environment(plan):
    for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        plan.environment[key] = '8'
    plan.environment.update(CUDA_VISIBLE_DEVICES='-1', PYTHONDONTWRITEBYTECODE='1')
    for key in tuple(plan.environment):
        if key.startswith(('APP_', 'DATABASE_', 'JWT_', 'AUTH_', 'SMTP_', 'ACCESS_TOKEN_', 'REFRESH_', 'VITE_')):
            plan.environment.pop(key, None)


def evidence_codes_for_sealed_tool(tool_root, tool_identity):
    """Static code literals only, from the already admitted immutable closure.

    No caller-provided error strings or imports/evaluation of code are allowed.
    Rechecking these small source bytes against the seal keeps evidence useful
    for asset/lineage failures without broadening the historical sink whitelist.
    """
    from backend.experiments.sky_masks import read_json
    root = ordinary(Path(tool_root))
    manifest = read_json(root / 'sky-c-tool-manifest.json', tool_identity)
    files = manifest.get('files')
    require(manifest.get('kind') == 'local_sky_c_supervised_tools' and type(files) is dict
            and all(name in files for name in _CODE_SOURCE_FILES),
            'SKY_EXECUTE_EVIDENCE_CODE_SEAL_CHANGED')
    codes = set(LOCAL_C_CODES | SKY_EXECUTOR_CODES)
    for name in _CODE_SOURCE_FILES:
        path = ordinary(root / name)
        before = path.stat()
        require(path.is_file() and before.st_size <= 1024**2, 'SKY_EXECUTE_EVIDENCE_CODE_SEAL_CHANGED')
        raw = path.read_bytes()
        after = path.stat()
        require(hashlib.sha256(raw).hexdigest() == files[name]
                and (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                'SKY_EXECUTE_EVIDENCE_CODE_SEAL_CHANGED')
        try:
            tree = ast.parse(raw, filename=name)
        except (SyntaxError, ValueError):
            raise SupervisionError('SKY_EXECUTE_EVIDENCE_CODE_SEAL_CHANGED') from None
        codes.update(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
            and isinstance(node.value, str) and len(node.value) <= 96 and _EVIDENCE_CODE.fullmatch(node.value))
    return frozenset(codes)


def execute_sky_c(args, sky_profile=None, *, tool_identity):
    """Run one explicitly authorized continuation, with append-only accounting.

    Admission and source/import checks consume the same live remaining budget.
    No automatic UAC, server/network, retry, marker skip or budget reset exists.
    A terminal stage success is not final until shutdown, sleep release and the
    new continuation charge have been durably confirmed.
    """
    admission_started = time.monotonic()
    require(getattr(args, 'execute_sky_c_only', False) is True, 'SKY_EXECUTE_EXPLICIT_REQUIRED')
    if sky_profile is None:
        from scripts.run_local_c import read
        sky_profile = read(args.profile, expected_sha256=args.profile_sha256)
    require_admin()
    # Kept lazy so merely importing the executor cannot initialize its caller's
    # prepared-input or profile tooling, much less start a computation.
    from backend.experiments.sky_supervision import (
        admit_sky_c, verify_sky_sources, build_supervised_sky_plan, verify_sky_runtime_stage,
    )
    from backend.experiments.sky_assets import gate_sky_openmvs, verify_inventory

    context = admit_sky_c(args, sky_profile, tool_identity=tool_identity)
    profile = context['c_profile']
    guards = profile['protection']
    require(guards['available_ram_floor_bytes'] >= 8 * GIB and guards['disk_floor_bytes'] >= 20 * GIB,
            'SKY_EXECUTE_RESOURCE_CONFIGURATION_CHANGED')
    require(profile['cpu'] == {'openmvs_threads': 8, 'texture_threads': 8},
            'SKY_EXECUTE_THREAD_CONFIGURATION_CHANGED')
    root = ordinary(Path(context['root']), missing_leaf=True)
    require(not root.exists(), 'SKY_EXECUTE_NEW_TARGET_REQUIRED')
    pipeline = ordinary(Path(context['pipeline']))
    verify_git_identity(pipeline, BASE_COMMIT)
    reconciliation = context['reconciliation']
    budget = WholeRunBudget(reconciliation['remaining_seconds'])
    budget.started = admission_started
    budget.check()
    identity = dict(context['identity'])
    identity.update(experiment_kind='local_sky_mask_c_only', sfm_mask_applied=False,
                    pipeline_commit=BASE_COMMIT)
    runtime = context['runtime']
    source, handoff = context['verified_source'], context['verified_handoff']
    known_codes = evidence_codes_for_sealed_tool(context['tool_root'], tool_identity)
    with exclusive_long_test():
        evidence = EvidenceSink.create_production(profile['target_experiment']['scene'],
            known_stages=C_STAGE_NAMES, known_codes=known_codes)
        monitor = supervisor = None
        claim_path = claim_document = None
        claim_elapsed = None
        final_result = None
        sleep_api = None
        sleeping = False
        probe_started, probe_stopped = False, []
        sky_unchanged = False
        inventory = {}
        try:
            evidence.start()
            budget.check()
            # Capture before the live ledger re-read and exclusive write so
            # their IO time is also charged, not silently given back.
            claim_elapsed = budget.elapsed
            claim_path, claim_document = claim_c_continuation_budget(PRIVATE_PARENT,
                reconciliation, source_budget=context['source_budget'],
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
            health()
            imported = import_sfm_handoff(handoff, root, input_source=source,
                health_check=health, target_work_root=root / 'work')
            dimensions = prepared_dimensions(imported.input_manifest_path, root, source.image_names)
            plan = build_supervised_sky_plan(context, dimensions)
            context['plan'] = plan
            require(tuple(stage.name for stage in plan.stages) == C_STAGE_NAMES, 'SKY_EXECUTE_PLAN_CHANGED')
            for stage in plan.stages:
                verify_sky_runtime_stage(stage, context)
            _cpu_environment(plan)
            probe_started = True
            probe = run_contained_process([runtime['python'], '-I', '-B', '-c',
                'import json,numpy,scipy,PIL,trimesh; print(json.dumps({"numpy":numpy.__version__,"scipy":scipy.__version__,"pillow":PIL.__version__,"trimesh":trimesh.__version__}))'],
                cwd=pipeline, log_path=root / 'reports/sky-python-imports-private.log',
                timeout_seconds=min(60, budget.remaining), env=plan.environment, health_check=health,
                on_tree_stopped=lambda: probe_stopped.append(True))
            require(probe.return_code == 0 and probe.tree_stopped_confirmed and probe_stopped,
                    'SKY_EXECUTE_DEPENDENCIES_UNAVAILABLE')
            bounded_private_report(root / 'reports/effective-sky-c-profile.json', sky_profile)
            bounded_private_report(root / 'reports/executed-sky-c-plan.json', {
                'schema_version': '1.0', **identity, 'budget_binding': reconciliation,
                'stages': [stage.observation() for stage in plan.stages],
                'mode': 'local-sky-c-only', 'source_sfm_rerun': False, 'automatic_retry_allowed': False})
            sleep_api = ctypes.WinDLL('kernel32', use_last_error=True)
            sleep_api.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
            sleep_api.SetThreadExecutionState.restype = ctypes.c_uint32
            require(sleep_api.SetThreadExecutionState(0x80000001), 'SKY_EXECUTE_SLEEP_GUARD_FAILED')
            sleeping = True
            def stage_gate(stage, target, names, health_check=lambda: None):
                result = gate_c_stage(stage, target, names, health_check=health_check)
                if stage.name == '03-prepare-openmvs-input':
                    inventory.update(gate_sky_openmvs(context['binding'],
                        folder=target / 'work/shared/openmvs_input', health=health_check))
                    result['sky_mask_gate_passed'] = True
                return result
            def checked_process(argv, **kwargs):
                matches = [stage for stage in plan.stages if list(stage.argv) == argv]
                require(len(matches) == 1, 'SKY_EXECUTE_UNEXPECTED_STAGE')
                stage = matches[0]
                verify_sky_runtime_stage(stage, context)
                if stage.name in ('11-c-densify', '13-c-texture'):
                    require(bool(inventory), 'SKY_EXECUTE_MASK_INVENTORY_REQUIRED')
                    verify_inventory(inventory, health=kwargs['health_check'], hash_content=True)
                return run_contained_process(argv, **kwargs)
            def source_final(health_check):
                nonlocal sky_unchanged
                verify_sky_sources(context, health_check)
                verify_git_identity(pipeline, BASE_COMMIT)
                if inventory:
                    verify_inventory(inventory, health=health_check, hash_content=True)
                sky_unchanged = True
            supervisor = CStageSupervisor(root=root, monitor=monitor, evidence=evidence, budget=budget,
                image_names=source.image_names, identity=identity, gate=stage_gate,
                source_final_check=source_final, process_runner=checked_process,
                notify=lambda value: print(json.dumps(value, ensure_ascii=True), flush=True))
            final_result = supervisor.run(plan)
            final_result.update(mode='local-sky-c-only', reconstruction_started=True,
                                sky_source_unchanged=sky_unchanged, sfm_mask_applied=False)
            if final_result.get('status') == 'succeeded':
                require(sky_unchanged and bool(inventory) and final_result.get('source_handoff_unchanged') is True,
                        'SKY_EXECUTE_SOURCE_PROOF_UNCONFIRMED')
            return final_result
        except BaseException as exc:
            code = error_code(exc)
            final_result = {'schema_version': '1.0', 'mode': 'local-sky-c-only',
                'status': 'timed_out' if code == 'LONG_BUDGET_EXHAUSTED' else
                          'cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed',
                'error_code': code, 'reconstruction_started': supervisor is not None,
                'server_accessed': False, 'gpu_stage_started': False, 'source_job_modified': False,
                'platform_job_resume': False, 'sky_source_unchanged': sky_unchanged,
                'process_tree_stopped': supervisor.tree_stopped if supervisor else
                    bool(probe_stopped) if probe_started else True,
                'elapsed_seconds': budget.elapsed, 'remaining_budget_seconds': budget.remaining, **identity}
            try:
                evidence.finish_terminal({'status': final_result['status'], 'phase': 'terminal',
                                          'error_code': code, **identity})
                final_result['terminal_evidence_confirmed'] = True
            except BaseException:
                final_result['terminal_evidence_confirmed'] = False
            return final_result
        finally:
            # CStageSupervisor normally closes these; idempotent closure also
            # covers every claim/import/dependency/constructor failure path.
            if monitor is not None:
                try: monitor.close()
                except BaseException: pass
            try: evidence.close()
            except BaseException: pass
            if final_result is not None:
                final_result.update(budget_claimed=claim_path is not None, budget_charged=False,
                    budget_reset_or_extended=False, uac_requested=False, source_sfm_rerun=False)
                if claim_path is not None:
                    final_result.update(continuation_claim_sha256=claim_document.sha256,
                                        continuation_claim_path=str(claim_path))
                final_result['resource_threads_stopped'] = _threads_stopped(monitor)
                final_result['evidence_threads_stopped'] = _threads_stopped(evidence)
                if not final_result['resource_threads_stopped']:
                    final_result.update(status='failed', error_code='SKY_EXECUTE_RESOURCE_THREADS_UNCONFIRMED')
                if not final_result['evidence_threads_stopped']:
                    final_result.update(status='failed', error_code='SKY_EXECUTE_EVIDENCE_THREADS_UNCONFIRMED')
                final_result['sleep_request_released'] = not sleeping
            if sleeping:
                try: released = bool(sleep_api.SetThreadExecutionState(0x80000000))
                except BaseException: released = False
                if final_result is not None:
                    final_result['sleep_request_released'] = released
                    if not released:
                        final_result.update(status='failed', error_code='SKY_EXECUTE_SLEEP_RELEASE_UNCONFIRMED')
            if claim_path is not None:
                try:
                    charge_path, document = write_c_continuation_charge(claim_path, claim_document,
                        elapsed_seconds=max(0, budget.elapsed - claim_elapsed),
                        remaining_seconds=budget.remaining,
                        status=final_result.get('status', 'failed') if final_result else 'failed')
                    if final_result is not None:
                        final_result.update(budget_charge_confirmed=True,
                            budget_charged=True,
                            continuation_charge_sha256=document.sha256,
                            continuation_charge_path=str(charge_path), budget_reset_or_extended=False)
                except BaseException:
                    if final_result is not None:
                        final_result.update(status='failed', budget_charge_confirmed=False,
                                            budget_charged=False,
                                            error_code='SKY_EXECUTE_BUDGET_CHARGE_UNCONFIRMED')
            if final_result is not None:
                final_result.update(elapsed_seconds=budget.elapsed, remaining_budget_seconds=budget.remaining)
                if (root / 'reports').is_dir():
                    try:
                        bounded_private_report(root / 'reports/sky-c-final-status.json', final_result)
                    except BaseException:
                        final_result.update(status='failed', error_code='SKY_EXECUTE_FINAL_RECEIPT_UNCONFIRMED')
