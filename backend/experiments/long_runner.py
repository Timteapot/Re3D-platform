"""Explicit independent long-test entry. Default is offline preparation only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

from backend.experiments.checkpoint import verify_checkpoint, restore_checkpoint
from backend.experiments.evidence import EvidenceSink
from backend.experiments.high_profile import BASE_COMMIT, BASE_CONFIG_SHA, build_pipeline_overlay, file_sha, validate_profile
from backend.experiments.preflight import (IsolationGuard, read_queue_state, read_runtime_versions,
    read_windows_state, require_admin, validate_isolation, verify_git_identity)
from backend.experiments.resources import ExperimentResourceMonitor, ResourceLimits
from backend.experiments.runner import PLATFORM_BASE, create_private_root, read_json, write_exclusive
from backend.experiments.snapshot_store import SnapshotStore
from backend.experiments.stages import build_stage_plan, gate_stage, prepared_dimensions
from backend.experiments.supervision import LongStageSupervisor, SupervisionError, WholeRunBudget, error_code
from backend.monitoring.resources import HostResourceReader, NvidiaSmiReader

STAGE_NAMES = (
    '01-colmap','02-extract-cameras','03-prepare-openmvs-input','10-c-interface','11-c-densify','12-c-reconstruct',
    '13-c-texture','13-c-normalize','20-a-mapanything','21-a-scale-calibration','22-a-crossview-v4',
    '23-a-export-dmap','23a-a-validate-dmap','23b-a-stage-openmvs','24-a-fuse','24a-a-verify-handoff',
    '25-a-reconstruct','26-a-texture','26-a-normalize','30-b-mvsanywhere','31-b-scale-calibration','32-b-crossview-v2',
    '33-b-export-dmap','33a-b-validate-dmap','33b-b-stage-openmvs','34-b-fuse','34a-b-verify-handoff',
    '35-b-reconstruct','36-b-texture','36-b-normalize','90-validate')
MODEL_MANIFEST_SHA = '3a046f35752ce22300fbdaec3386ddb9f9b0c0decf7e1f2d1d10994f488ec78e'
LONG_CODES = frozenset({
    'LONG_BUDGET_INVALID','LONG_BUDGET_EXHAUSTED','LONG_OBSERVATION_STALE','LONG_STAGE_OR_IO_FAILED',
    'LONG_STAGE_DIRECTORY_INVALID','LONG_STAGE_DIRECTORY_REPARSE','LONG_FORENSIC_TREE_NOT_STOPPED',
    'LONG_FORENSIC_DISK_UNSAFE','LONG_FORENSIC_BUDGET_EXHAUSTED','LONG_STAGE_PROCESS_FAILED',
    'LONG_TREE_STOP_UNCONFIRMED','LONG_SNAPSHOT_STOP_UNCONFIRMED','LONG_TERMINAL_EVIDENCE_FAILED',
    'LONG_FINAL_SNAPSHOT_FAILED','LONG_FORENSIC_EVIDENCE_FAILED','LONG_SOURCE_VERIFICATION_FAILED',
    'LONG_HARDWARE_CHANGED','LONG_GPU_RUNTIME_UNAVAILABLE','LONG_TOOL_MANIFEST_REQUIRED',
    'LONG_EXCLUSIVE_LOCK_UNAVAILABLE','LONG_EXCLUSIVE_LOCK_BUSY',
    'LONG_OBSERVATION_STOP_UNCONFIRMED','LONG_RESOURCE_SHUTDOWN_FAILED','LONG_READONLY_VALIDATION_TIMED_OUT','EXPERIMENT_PROCESS_LOG_IO_TIMED_OUT',
    'LONG_TOOL_HASH_MISMATCH','LONG_CANDIDATE_ADMISSION_REQUIRED','LONG_REPORT_PERSISTENCE_FAILED',
    'SNAPSHOT_CAPTURE_TIMED_OUT','SNAPSHOT_STOP_UNCONFIRMED','SNAPSHOT_DISK_HEADROOM_LOW',
    'SNAPSHOT_COPY_SPACE_INSUFFICIENT','SNAPSHOT_FINAL_TREE_NOT_STOPPED','SNAPSHOT_FINAL_WORKER_NOT_STOPPED',
    'SNAPSHOT_FINAL_CAPTURE_FAILED','SNAPSHOT_FINAL_CAPTURE_TIMED_OUT','SNAPSHOT_SQLITE_BACKUP_FAILED'})


def bounded_private_report(path, value, timeout=5):
    """No compute remains; still avoid indefinitely blocking the owner on D IO."""
    finished = threading.Event()
    outcome = []
    def writer():
        try:
            write_exclusive(path, value)
            outcome.append(True)
        except BaseException:
            outcome.append(False)
        finally:
            finished.set()
    threading.Thread(target=writer, daemon=True, name='long-private-report').start()
    if not finished.wait(timeout) or not outcome or not outcome[0]:
        raise SupervisionError('LONG_REPORT_PERSISTENCE_FAILED')


def verify_tool_manifest(root):
    path = root / 'tool-manifest.json'
    if not path.is_file() or path.is_symlink() or getattr(path.stat(), 'st_file_attributes', 0) & 1024 or path.stat().st_size > 128 * 1024:
        raise SupervisionError('LONG_TOOL_MANIFEST_REQUIRED')
    document = read_json(path)
    entries = document.get('files', {})
    required = {f'backend/experiments/{name}.py' for name in ('__init__','checkpoint','resources','process','preflight',
        'runner','high_profile','stages','snapshot_store','evidence','supervision','long_runner')}
    required |= {'scripts/run_long_experiment.py','scripts/run_profiled_colmap.py'}
    if not isinstance(entries, dict) or set(entries) != required:
        raise SupervisionError('LONG_TOOL_MANIFEST_REQUIRED')
    for name in required:
        file = root / name
        if not file.is_file() or file.is_symlink() or file_sha(file) != entries[name]:
            raise SupervisionError('LONG_TOOL_HASH_MISMATCH')
        for part in (file, *file.parents):
            if part.is_symlink() or getattr(part.stat(), 'st_file_attributes', 0) & 1024:
                raise SupervisionError('LONG_TOOL_HASH_MISMATCH')
    return file_sha(path)


def declared_failure_codes(root):
    """Only literals in already hash-verified tool source extend the C allowlist.

    An arbitrary exception message/code cannot introduce user text into it.
    Do this before computation; health callbacks do not perform source-file IO.
    """
    names = ('checkpoint','resources','process','preflight','runner','high_profile','stages','snapshot_store','supervision','long_runner')
    codes = set(LONG_CODES)
    for name in names:
        source = (root / 'backend' / 'experiments' / (name + '.py')).read_text(encoding='utf-8')
        codes.update(re.findall(r'''["']((?:EXPERIMENT|HIGH_PROFILE|SNAPSHOT|LONG|RESOURCE|LOW|CHECKPOINT)_[A-Z0-9_]{2,80})["']''', source))
    source = (root / 'scripts/run_profiled_colmap.py').read_text(encoding='utf-8')
    codes.update(re.findall(r'''["'](PROFILED_COLMAP_[A-Z0-9_]{2,70})["']''', source))
    return frozenset(codes)


@contextmanager
def exclusive_long_test():
    """Fail closed rather than launch two independent trees during admission."""
    if os.name != 'nt': raise SupervisionError('LONG_EXCLUSIVE_LOCK_UNAVAILABLE')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    api.CreateMutexW.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.ReleaseMutex.argtypes = [ctypes.c_void_p]
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = api.CreateMutexW(None, False, r'Global\Re3DRestrictedIndependentReconstruction')
    if not handle: raise SupervisionError('LONG_EXCLUSIVE_LOCK_UNAVAILABLE')
    acquired = False
    try:
        result = api.WaitForSingleObject(handle, 0)
        if result == 258: raise SupervisionError('LONG_EXCLUSIVE_LOCK_BUSY')
        if result not in (0, 128): raise SupervisionError('LONG_EXCLUSIVE_LOCK_UNAVAILABLE')
        acquired = True
        yield
    finally:
        if acquired: api.ReleaseMutex(handle)
        api.CloseHandle(handle)


def compare_admission_reports(baseline, candidate, profile):
    """Admission is a short CPU/database test, not full geometry/GPU acceptance."""
    validate_profile(profile)
    source_sha = profile['source_checkpoint']['database_sha256']
    expected_config = profile.get('prepared_overlay_sha256')
    if not isinstance(expected_config, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_config):
        raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
    for report, threads in ((baseline, 8), (candidate, profile['cpu']['sfm_threads'])):
        if not isinstance(report, dict) or not isinstance(report.get('resources'), dict):
            raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
        duration = report.get('admission_duration_seconds')
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 180 <= duration <= 195:
            raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
        required = (report.get('mode') == 'admission' and report.get('status') == 'admission_window_complete'
            and report.get('sfm_threads') == threads and report.get('source_database_sha256') == source_sha
            and report.get('source_checkpoint_unchanged') is True and report.get('process_tree_stopped') is True
            and report.get('terminal_evidence_confirmed') is True and report.get('final_snapshot_status') == 'valid'
            and report['resources'].get('errors') == [] and 'trip' in report['resources'] and report['resources']['trip'] is None
            and report.get('pipeline_commit') == BASE_COMMIT and report.get('config_sha256') == expected_config
            and report.get('source_manifest_sha256') == profile['source_checkpoint']['manifest_sha256']
            and isinstance(report.get('tool_manifest_sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', report['tool_manifest_sha256']))
        if not required: raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
        cpu, window_cpu = report.get('cpu_sampled_mean_percent'), report.get('cpu_max_60s_mean_percent')
        samples, span = report.get('cpu_sample_count'), report.get('cpu_observed_span_seconds')
        if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 80 for value in (cpu, window_cpu)) or type(samples) is not int or samples < 24 or type(span) not in (int, float) or not math.isfinite(span) or not 120 <= span <= duration:
            raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
    rates = []
    if baseline['tool_manifest_sha256'] != candidate['tool_manifest_sha256']:
        raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
    for report in (baseline, candidate):
        initial = report.get('initial_pair_counts', {}).get('complete_pair_records') if isinstance(report.get('initial_pair_counts'), dict) else None
        final = report.get('final_pair_counts', {}).get('complete_pair_records') if isinstance(report.get('final_pair_counts'), dict) else None
        if type(initial) is not int or type(final) is not int or initial != profile['source_checkpoint']['complete_pair_records'] or not initial < final <= 44850:
            raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
        rates.append((final - initial) / report['admission_duration_seconds'] * 60)
    if rates[1] <= rates[0]:
        raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
    return {'schema_version':'1.0','status':'accepted_short_cpu_candidate_admission',
        'source_database_sha256':source_sha,'pipeline_commit':BASE_COMMIT,
        'config_sha256':expected_config,'tool_manifest_sha256':candidate['tool_manifest_sha256'],
        'sfm_threads':profile['cpu']['sfm_threads'],'baseline_threads':8,
        'baseline_pairs_per_minute':rates[0],'candidate_pairs_per_minute':rates[1],
        'observed_window_ratio':rates[1]/rates[0], 'gpu_or_full_geometry_acceptance':False}


def runtime_gpu_check(interpreter, pipeline_root):
    code = "import json,torch;ok=torch.cuda.is_available();p=torch.cuda.get_device_properties(0) if ok else None;print(json.dumps(dict(cuda_available=ok,torch_version=torch.__version__,cuda_version=torch.version.cuda,gpu_name=p.name if p else None,gpu_total_bytes=p.total_memory if p else None,compute_capability=[p.major,p.minor] if p else None)))"
    result = subprocess.run([str(interpreter), '-c', code], capture_output=True, text=True, timeout=30, cwd=pipeline_root)
    if result.returncode: raise SupervisionError('LONG_GPU_RUNTIME_UNAVAILABLE')
    value = json.loads(result.stdout)
    if not value['cuda_available'] or 'T4' not in value['gpu_name'] or value['compute_capability'] != [7,5]:
        raise SupervisionError('LONG_GPU_RUNTIME_UNAVAILABLE')
    return value


def execute_long_test(args, profile, tool_root):
    require_admin()
    tool_sha = verify_tool_manifest(tool_root)
    validate_profile(profile)
    verify_git_identity(args.platform_root, PLATFORM_BASE)
    verify_git_identity(args.pipeline_root, BASE_COMMIT)
    if file_sha(args.pipeline_root / 'configs/pipeline.json') != BASE_CONFIG_SHA:
        raise SupervisionError('HIGH_PROFILE_FROZEN_IDENTITY_CHANGED')
    if file_sha(args.pipeline_root / 'models/checksums.json') != MODEL_MANIFEST_SHA:
        raise SupervisionError('HIGH_PROFILE_FROZEN_IDENTITY_CHANGED')
    source_id = profile['source_job_id']
    initial_queue = read_queue_state(args.platform_root)
    validate_isolation({'host':read_windows_state(),'queue':initial_queue}, source_id,
        allow_expired_source_job=args.allow_expired_source_job, owned_pids={os.getpid()})
    target = Path(profile['target_experiment']['root'])
    if target.exists() or not target.parent.is_dir() or target.parent.name != '_experiments':
        raise SupervisionError('HIGH_PROFILE_TARGET_NOT_NEW_OR_IS_JOB')
    protections = profile['protection']
    admission_free = protections['reservation_bytes'] + protections['disk_floor_bytes']
    if shutil.disk_usage(target.parent).free < admission_free:
        raise SupervisionError('EXPERIMENT_DISK_ADMISSION_FAILED')
    host, errors = HostResourceReader().read(target.parent)
    gpu, gpu_error = NvidiaSmiReader(resource_key='gpu:0').read()
    hardware = profile['target_hardware']
    if errors or gpu_error or host['cpu']['logical_count'] != hardware['logical_cpus'] or abs(host['memory']['total_bytes'] - hardware['host_ram_bytes']) > 1024**3 or gpu['memory_total_bytes'] != hardware['gpu_total_bytes']:
        raise SupervisionError('LONG_HARDWARE_CHANGED')
    paths = read_json(args.pipeline_root / 'configs/paths.local.json')
    interpreters = {}
    for label in ('mapanything_python','mvsanywhere_python'):
        path = Path(paths[label])
        path = path if path.is_absolute() else args.pipeline_root / path
        if not path.resolve().is_relative_to(args.pipeline_root.resolve()):
            raise SupervisionError('EXPERIMENT_INTERPRETER_BOUNDARY')
        interpreters[label] = path
    versions = read_runtime_versions(interpreters['mapanything_python'])
    if versions != {'python_version':'3.12.14','pycolmap_version':'3.13.0','pycolmap_has_cuda':False}:
        raise SupervisionError('EXPERIMENT_RUNTIME_VERSION_CHANGED')
    runtime_gpu = {key:runtime_gpu_check(path,args.pipeline_root) for key,path in interpreters.items()}
    if args.mode == 'full':
        if args.baseline_admission is None or args.candidate_admission is None:
            raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
        admission = compare_admission_reports(read_json(args.baseline_admission),read_json(args.candidate_admission),profile)
        if admission['tool_manifest_sha256'] != tool_sha:
            raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
    else:
        admission = None
    evidence = EvidenceSink.create_production(profile['target_experiment']['scene'], known_codes=declared_failure_codes(tool_root), known_stages=STAGE_NAMES)
    budget = WholeRunBudget(profile['protection']['overall_timeout_seconds'])
    monitor = isolation = snapshots = None
    supervisor = None
    result = None
    try:
        evidence.start()
        create_private_root(target,kind='independent_long_pipeline')
        limits = ResourceLimits(minimum_disk_free_bytes=protections['disk_floor_bytes'],
            minimum_available_ram_bytes=protections['available_ram_floor_bytes'],minimum_free_vram_bytes=protections['free_vram_floor_bytes'],
            guard_interval_seconds=protections['host_guard_seconds'],resource_interval_seconds=protections['gpu_sample_seconds'],
            directory_interval_seconds=protections['directory_sample_seconds'])
        monitor = ExperimentResourceMonitor(experiment_root=target,sample_path=target/'reports/resource-samples.jsonl',limits=limits)
        isolation = IsolationGuard(lambda:{'host':read_windows_state(),'queue':read_queue_state(args.platform_root)},source_id,
            allow_expired_source_job=args.allow_expired_source_job,static_owned_pids={os.getpid()},stale_seconds=20)
        monitor.start(); isolation.start()
        def health():
            budget.check(); monitor.check_health(); evidence.check_health(); isolation.check_health()
        monitor.set_phase('restore')
        source = profile['source_checkpoint']
        checkpoint = Path(source['server_path'])
        verify = lambda health: verify_checkpoint(checkpoint,source['manifest_sha256'],source['database_sha256'],source_id,BASE_COMMIT,BASE_CONFIG_SHA,health_check=health)
        verified = verify(health)
        restored = restore_checkpoint(verified,target,health_check=health,target_work_root=target/'work')
        overlay,_ = build_pipeline_overlay(read_json(args.pipeline_root/'configs/pipeline.json'),profile)
        profile_path,config_path = target/'reports/effective-profile.json',target/'reports/pipeline-high.json'
        write_exclusive(profile_path,profile); write_exclusive(config_path,overlay)
        if file_sha(config_path) != profile.get('prepared_overlay_sha256'):
            raise SupervisionError('HIGH_PROFILE_OVERLAY_HASH_CHANGED')
        dimensions = prepared_dimensions(restored.input_manifest_path,restored.root,verified.image_names)
        plan = build_stage_plan(pipeline_root=args.pipeline_root,config_path=config_path,paths_path=args.pipeline_root/'configs/paths.local.json',
            profile_path=profile_path,profile=profile,dimensions=dimensions,experiment_root=target,
            wrapper_path=tool_root/'scripts/run_profiled_colmap.py',platform_root=args.platform_root)
        if tuple(row.name for row in plan.stages) != STAGE_NAMES: raise SupervisionError('EXPERIMENT_STAGE_PLAN_COUNT_CHANGED')
        # Runtime environment is only passed to contained children; NEVER serialized.
        plan.environment['PYTHONPATH'] = os.pathsep.join((str(tool_root),str(args.platform_root),plan.environment.get('PYTHONPATH','')))
        plan.environment['HF_HUB_OFFLINE']='1'; plan.environment['TRANSFORMERS_OFFLINE']='1'
        bounded_private_report(target/'reports/executed-plan.json', {'schema_version':'1.0','tool_manifest_sha256':tool_sha,
            'profile_sha256':file_sha(profile_path),'config_sha256':file_sha(config_path),'pipeline_commit':BASE_COMMIT,
            'model_checksum_manifest_sha256':MODEL_MANIFEST_SHA,'model_weights_full_rehash_performed':False,
            'runtime':versions,'runtime_gpu':runtime_gpu,'stages':[row.observation() for row in plan.stages],'mode':args.mode})
        snapshots = SnapshotStore(target/'checkpoints',restored.database_path,verified.image_names,health_check=health,
            free_bytes_reader=lambda:shutil.disk_usage(target).free,interval_seconds=600,min_free_bytes=protections['disk_floor_bytes'],timeout_seconds=120)
        def source_check(forensic):
            verify(forensic)
            if read_queue_state(args.platform_root) != initial_queue:
                raise SupervisionError('LONG_SOURCE_VERIFICATION_FAILED')
        supervisor = LongStageSupervisor(root=target,run_id=profile['target_experiment']['scene'],source_job_id=source_id,
            monitor=monitor,evidence=evidence,isolation=isolation,snapshots=snapshots,budget=budget,image_names=verified.image_names,
            database=restored.database_path,gate=gate_stage,source_final_check=source_check,
            disk_floor_bytes=protections['disk_floor_bytes'],
            identity={'pipeline_commit':BASE_COMMIT,'source_database_sha256':source['database_sha256'],'source_manifest_sha256':source['manifest_sha256'],
                'profile_sha256':file_sha(profile_path),'config_sha256':file_sha(config_path),'tool_manifest_sha256':tool_sha},
            notify=lambda item:print(json.dumps(item),flush=True))
        result = supervisor.run(plan,admission_window_seconds=180 if args.mode=='admission' else None)
        result.update(sfm_threads=profile['cpu']['sfm_threads'],source_database_sha256=source['database_sha256'],
            source_manifest_sha256=source['manifest_sha256'],tool_manifest_sha256=tool_sha,
            pipeline_commit=BASE_COMMIT,profile_sha256=file_sha(profile_path),config_sha256=file_sha(config_path),admission=admission)
        try:
            bounded_private_report(target/'reports/long-result.json',result)
        except SupervisionError as exc:
            # C terminal and forensic summaries already preserve the result.
            result['private_d_report_error']=exc.code
        return result
    except BaseException as exc:
        result={'schema_version':'1.0','status':'failed','error_code':error_code(exc),'source_job_modified':False,'platform_job_resume':False}
        if supervisor is None:
            try: evidence.finish_terminal(result)
            except BaseException: result['terminal_evidence_error']='LONG_TERMINAL_EVIDENCE_FAILED'
        return result
    finally:
        if supervisor is None:
            if snapshots: snapshots.stop(timeout=5)
            if monitor: monitor.close()
            if isolation: isolation.close()
            evidence.close()


def main(tool_root=None):
    parser=argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--mode',choices=('prepare','admission','full','compare-admission'),default='prepare')
    parser.add_argument('--pipeline-root',type=Path,default=Path(r'D:\Re3D'))
    parser.add_argument('--platform-root',type=Path,default=Path(r'D:\Re3D-platform'))
    parser.add_argument('--execute-long-test',action='store_true')
    parser.add_argument('--allow-expired-source-job',action='store_true')
    parser.add_argument('--run-name')
    parser.add_argument('--admission-threads',type=int,choices=(8,12))
    parser.add_argument('--baseline-admission',type=Path)
    parser.add_argument('--candidate-admission',type=Path)
    args=parser.parse_args()
    try:
        profile=read_json(args.profile); validate_profile(profile)
        if args.mode=='compare-admission':
            if args.execute_long_test or args.baseline_admission is None or args.candidate_admission is None:
                raise SupervisionError('LONG_CANDIDATE_ADMISSION_REQUIRED')
            result=compare_admission_reports(read_json(args.baseline_admission),read_json(args.candidate_admission),profile)
        elif not args.execute_long_test:
            result={'status':'prepared_no_execution','long_supervisor_implemented':True,'mode':args.mode,
                'autorun':False,'server_contacted':False,'actual_hardware_preflight_required':True,
                'candidate_admission_required_for_full':True,'sfm_threads':profile['cpu']['sfm_threads'],'mapper_ba_threads':profile['cpu']['mapper_ba_threads']}
        else:
            if args.mode not in {'admission','full'} or not args.run_name or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',args.run_name):
                raise SupervisionError('HIGH_PROFILE_TARGET_NOT_NEW_OR_IS_JOB')
            profile=deepcopy(profile)
            profile['target_experiment']={'scene':args.run_name,'root':str(Path(r'D:\Re3D-data\_experiments')/args.run_name)}
            if args.mode=='admission':
                if args.admission_threads is None: raise SupervisionError('HIGH_PROFILE_SFM_THREAD_CEILING')
                profile['cpu']['sfm_threads']=args.admission_threads
            elif args.admission_threads is not None:
                raise SupervisionError('HIGH_PROFILE_SFM_THREAD_CEILING')
            with exclusive_long_test():
                result=execute_long_test(args,profile,tool_root or Path(__file__).resolve().parents[2])
        print(json.dumps({key:value for key,value in result.items() if key in {'status','error_code','mode','server_contacted','long_supervisor_implemented','candidate_admission_required_for_full','sfm_threads','mapper_ba_threads','actual_hardware_preflight_required','observed_window_ratio','baseline_pairs_per_minute','candidate_pairs_per_minute','gpu_or_full_geometry_acceptance','process_tree_stopped','final_snapshot_status'}},allow_nan=False),flush=True)
        return 0 if result['status'] in {'prepared_no_execution','succeeded','admission_window_complete','accepted_short_cpu_candidate_admission'} else 1
    except BaseException as exc:
        print(json.dumps({'status':'not_completed','error_code':error_code(exc)}),flush=True)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
