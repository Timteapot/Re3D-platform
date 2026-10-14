"""Offline high-CPU candidate preparation. No remote access or stage execution."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import re

from backend.experiments.preflight import ExperimentGateError

BASE_COMMIT = 'e517a161b4c0895f8be4258fb339272e4f99e1d6'
BASE_CONFIG_SHA = '9f8cbc83cb67b8313f624913686fbdc453e7572a18603d015bc198a9a92b8883'
PIPELINE_SCRIPT_SHA = '65ceba6c891b78ea49ed5b6f5079a1c6711bc00dfd45e6c729efbdc10c562378'
COLMAP_SCRIPT_SHA = 'a7918cce6dcd64fee5949e9f0e4d5091c72f97cefb70208be79dfbdc6c2ada41'
THREAD_PATHS = (('a_v4', 'openmvs_threads'), ('b_v2', 'openmvs_threads'),
                ('c', 'openmvs_threads'), ('texture', 'max_threads'))


def require(value, code):
    if not value:
        raise ExperimentGateError(code)


def integer(value):
    return type(value) is int


def file_sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def config_diff(left, right, prefix=''):
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            path = prefix + '/' + key
            if key not in left or key not in right:
                result.append({'path': path, 'before': left.get(key), 'after': right.get(key)})
            else:
                result.extend(config_diff(left[key], right[key], path))
        return result
    return [] if left == right else [{'path': prefix, 'before': left, 'after': right}]


def validate_profile(profile):
    require(profile.get('profile_kind') == 'offline_high_cpu_candidate' and profile.get('autorun') is False,
            'HIGH_PROFILE_EXECUTION_KIND_INVALID')
    require(profile.get('full_guarded_launcher_ready') is False and profile.get('server_applied') is False,
            'HIGH_PROFILE_UNVERIFIED_EXECUTION_CLAIM')
    identity = profile.get('pipeline', {})
    require(identity.get('commit') == BASE_COMMIT and identity.get('baseline_config_sha256') == BASE_CONFIG_SHA
            and identity.get('baseline_modified') is False and identity.get('quality_parameters_changed') is False,
            'HIGH_PROFILE_FROZEN_IDENTITY_CHANGED')
    target, cpu = profile['target_hardware'], profile['cpu']
    require(integer(target['logical_cpus']) and target['logical_cpus'] == 40,
            'HIGH_PROFILE_TARGET_CPU_CHANGED')
    require(integer(cpu['sfm_threads']) and 8 <= cpu['sfm_threads'] <= 12,
            'HIGH_PROFILE_SFM_THREAD_CEILING')
    # Three separate pools are not a global thread ceiling; this is a planning
    # budget, not a proof of instantaneous utilization or safety.
    require(3 * cpu['sfm_threads'] <= target['logical_cpus'], 'HIGH_PROFILE_MATCHER_POOL_BUDGET')
    require(integer(cpu['mapper_ba_threads']) and 1 <= cpu['mapper_ba_threads'] <= 32,
            'HIGH_PROFILE_MAPPER_THREAD_CEILING')
    require(integer(cpu['openmvs_threads']) and 1 <= cpu['openmvs_threads'] <= 32,
            'HIGH_PROFILE_OPENMVS_THREAD_CEILING')
    require(integer(cpu['texture_threads']) and 1 <= cpu['texture_threads'] <= 32,
            'HIGH_PROFILE_TEXTURE_THREAD_CEILING')
    require(cpu['global_blas_omp_override'] is None, 'HIGH_PROFILE_GLOBAL_THREAD_OVERRIDE')
    gpu = profile['gpu_preserved']
    expected_gpu = {'task_concurrency': 1, 'branches_serial': True, 'map_outer_batch': 4,
                    'map_dense_head_minibatch': 1, 'map_use_amp': True, 'map_requested_amp_dtype': 'bf16',
                    'mvs_source_views': 7, 'mvs_height': 384, 'mvs_width': 384, 'mvs_refinement_steps': 1}
    require(set(gpu) == set(expected_gpu) and all(type(gpu[key]) is type(value) and gpu[key] == value for key, value in expected_gpu.items()),
            'HIGH_PROFILE_UNVERIFIED_GPU_OR_QUALITY_CHANGE')
    guards = profile['protection']
    require(all(integer(guards[key]) for key in ('disk_floor_bytes', 'reservation_bytes', 'available_ram_floor_bytes',
                                                'free_vram_floor_bytes', 'host_guard_seconds', 'gpu_sample_seconds',
                                                'directory_sample_seconds', 'overall_timeout_seconds')),
            'HIGH_PROFILE_SUPERVISION_NUMERIC_TYPE')
    require(guards['disk_floor_bytes'] >= 20 * 1024**3 and guards['reservation_bytes'] == 64 * 1024**3
            and guards['available_ram_floor_bytes'] >= 32 * 1024**3
            and guards['free_vram_floor_bytes'] >= 2 * 1024**3,
            'HIGH_PROFILE_HEADROOM_REDUCED')
    require(guards['host_guard_seconds'] == 1 and guards['gpu_sample_seconds'] == 5
            and guards['directory_sample_seconds'] == 15 and guards['overall_timeout_seconds'] == 14400,
            'HIGH_PROFILE_SUPERVISION_SETTINGS_CHANGED')


def build_pipeline_overlay(baseline, profile):
    validate_profile(profile)
    require(baseline['a_v4']['batch_size'] == 4 and baseline['b_v2']['source_views'] == 7
            and baseline['b_v2']['height'] == 384 and baseline['b_v2']['width'] == 384
            and baseline['b_v2']['refinement_steps'] == 1, 'HIGH_PROFILE_BASELINE_GPU_CHANGED')
    result = deepcopy(baseline)
    for group, field in THREAD_PATHS:
        result[group][field] = profile['cpu']['texture_threads'] if group == 'texture' else profile['cpu']['openmvs_threads']
    diff = config_diff(baseline, result)
    require({row['path'] for row in diff} <= {'/' + group + '/' + field for group, field in THREAD_PATHS},
            'HIGH_PROFILE_UNEXPECTED_CONFIG_DIFF')
    return result, diff


def adapt_sfm_argv(command, profile):
    validate_profile(profile)
    result = [str(value) for value in command]
    require(len(result) > 2 and Path(result[1]).name == 'run_colmap_shared.py', 'HIGH_PROFILE_SFM_SCRIPT_MISMATCH')
    for flag, value in (('--num-threads', profile['cpu']['sfm_threads']), ('--max-num-features', 6000)):
        require(result.count(flag) <= 1, 'HIGH_PROFILE_DUPLICATE_SFM_FLAG')
        if flag in result:
            index = result.index(flag)
            require(index + 1 < len(result), 'HIGH_PROFILE_INCOMPLETE_SFM_FLAG')
            result[index + 1] = str(value)
        else:
            result.extend((flag, str(value)))
    return result


def apply_mapper_budget(options, threads):
    require(integer(threads) and 1 <= threads <= 32, 'HIGH_PROFILE_MAPPER_THREAD_CEILING')
    require(hasattr(options, 'num_threads') and hasattr(options, 'mapper')
            and hasattr(options.mapper, 'num_threads'), 'HIGH_PROFILE_MAPPER_CAPABILITY_MISSING')
    options.num_threads = threads  # COLMAP propagates this to Mapper and BA.
    options.mapper.num_threads = threads
    require(options.num_threads == threads and options.mapper.num_threads == threads,
            'HIGH_PROFILE_MAPPER_SETTING_NOT_APPLIED')
    if callable(getattr(options, 'check', None)):
        require(options.check(), 'HIGH_PROFILE_MAPPER_OPTIONS_INVALID')
    return options


@contextmanager
def mapper_budget_adapter(colmap_module, threads):
    """Adapter hook for a FUTURE supervised child, including deferred retry.

    Merely writing the profile does not call this hook or change the frozen CLI.
    It deliberately has no CLI that can launch an unsupervised reconstruction.
    """
    original = colmap_module.mapping_options
    def factory(*args, **kwargs):
        return apply_mapper_budget(original(*args, **kwargs), threads)
    colmap_module.mapping_options = factory
    try:
        yield
    finally:
        colmap_module.mapping_options = original


def validate_stage_plan(stages, profile):
    validate_profile(profile)
    require(stages and len({row['stage'] for row in stages}) == len(stages), 'HIGH_PROFILE_STAGE_PLAN_INVALID')
    sfm = [row for row in stages if row['stage'] == '01-colmap']
    require(len(sfm) == 1, 'HIGH_PROFILE_SFM_STAGE_MISSING')
    argv = sfm[0]['argv']
    require(argv[argv.index('--num-threads') + 1] == str(profile['cpu']['sfm_threads']), 'HIGH_PROFILE_SFM_FLAG_NOT_WIRED')
    capped = [row for row in stages if '--max-threads' in row['argv']]
    require(len(capped) == 9, 'HIGH_PROFILE_OPENMVS_STAGE_COUNT_CHANGED')
    for row in capped:
        argv = row['argv']
        require(argv.count('--max-threads') == 1, 'HIGH_PROFILE_DUPLICATE_OPENMVS_FLAG')
        expected = profile['cpu']['texture_threads'] if 'texture' in row['stage'] else profile['cpu']['openmvs_threads']
        require(argv[argv.index('--max-threads') + 1] == str(expected), 'HIGH_PROFILE_OPENMVS_FLAG_NOT_WIRED')
    a = next(row for row in stages if row['stage'] == '20-a-mapanything')['argv']
    require(a[a.index('--batch-size') + 1] == '4', 'HIGH_PROFILE_A_GROUPING_CHANGED')
    require(not any(row.get('stage_executed') for row in stages), 'HIGH_PROFILE_EXECUTION_DURING_PREPARATION')


def capture_stage_plan(pipeline_root, config_path, paths_path, profile, dimensions):
    """Call the frozen planner with step/prepare overridden to record only.

    No subprocess is launched, no old completion marker is trusted, and no
    work/log/output folder is created. Runtime paths are a LOCAL preview and
    must be regenerated from the real server paths after restart.
    """
    validate_profile(profile)
    script = pipeline_root / 'scripts/run_pipeline.py'
    require(file_sha(script) == PIPELINE_SCRIPT_SHA and file_sha(pipeline_root / 'scripts/run_colmap_shared.py') == COLMAP_SCRIPT_SHA,
            'HIGH_PROFILE_FROZEN_SCRIPT_CHANGED')
    require(len(dimensions) == 2 and all(integer(value) and value > 0 for value in dimensions),
            'HIGH_PROFILE_DIMENSIONS_INVALID')
    spec = importlib.util.spec_from_file_location('re3d_frozen_offline_planner', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stages = []
    class Recorder(module.Pipeline):
        def prepare(self):
            return tuple(dimensions)  # The later runner must restore/verify the real input manifest.
        def step(self, name, command, marker=None):
            require(re.fullmatch(r'[a-z0-9_-]{1,80}', name) is not None, 'HIGH_PROFILE_STAGE_NAME_INVALID')
            argv = adapt_sfm_argv(command, profile) if name == '01-colmap' else [str(value) for value in command]
            stages.append({'stage': name, 'argv': argv, 'expected_artifact': str(marker) if marker else None,
                           'stage_executed': False, 'existing_marker_reused': False,
                           'mapper_adapter_required_in_supervised_child': name == '01-colmap'})
    target = profile['target_experiment']
    root = Path(target['root'])
    require(root.is_absolute() and root.parent.name == '_experiments' and not root.exists()
            and not any(part.lower() == 'jobs' for part in root.parts), 'HIGH_PROFILE_TARGET_NOT_NEW_OR_IS_JOB')
    pipeline = Recorder(scene=target['scene'], images=None, branches={'a', 'b', 'c'}, config_path=config_path,
                        paths_path=paths_path, dry_run=True, work_dir=root / 'work', output_dir=root / 'output', log_dir=root / 'logs')
    pipeline.run()
    validate_stage_plan(stages, profile)
    require(not root.exists(), 'HIGH_PROFILE_OFFLINE_PLANNER_CREATED_RUNTIME_DATA')
    return stages
