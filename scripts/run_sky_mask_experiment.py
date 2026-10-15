"""Opt-in post-SfM sky-mask preparation/admission; never runs reconstruction.

No arguments are read-only/no-op. --mode prepare requires --prepare-inputs.
There is deliberately no execute/retry/budget-claim entry in this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time

REPOSITORY = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(REPOSITORY))

from backend.experiments.sky_masks import (
    SkyMaskError, require, ordinary, inspect_masks, prepare_frozen_dataset,
    prepare_frozen_openmvs, read_json, write_new, file_sha,
)


class PreparationGuard:
    """Between-image checks for finite input preparation, not a compute permit."""
    def __init__(self, disk: Path):
        self.disk = disk
        self.started = time.monotonic()
        self.minimum_available = None
        self.minimum_free = None
        self.checks = 0

    def __call__(self):
        from backend.monitoring.resources import _system_memory_bytes
        used, total = _system_memory_bytes()
        available, free = total - used, shutil.disk_usage(self.disk).free
        self.minimum_available = min(available, self.minimum_available or available)
        self.minimum_free = min(free, self.minimum_free or free)
        self.checks += 1
        require(available >= 8 * 1024**3, 'SKY_LOW_AVAILABLE_RAM_8GIB')
        require(free >= 20 * 1024**3, 'SKY_LOW_DISK_20GIB')
        require(time.monotonic() - self.started < 600, 'SKY_PREPARATION_DEADLINE')

    def summary(self):
        return {'available_ram_floor_bytes': 8 * 1024**3,
            'disk_free_floor_bytes': 20 * 1024**3, 'guard_checks': self.checks,
            'observed_min_available_ram_bytes': self.minimum_available,
            'observed_min_disk_free_bytes': self.minimum_free,
            'limitation': 'Between small image operations, not a continuous exact peak or future compute capacity guarantee.'}


def parser():
    value = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    value.add_argument('--mode', choices=('status', 'inspect', 'prepare', 'preflight', 'prepare-openmvs'), default='status')
    value.add_argument('--prepare-inputs', action='store_true')
    for flag in ('source', 'masks', 'pipeline-root', 'output', 'binding', 'images', 'sparse',
                 'handoff-root', 'sfm-input-manifest', 'experiment-root', 'runtime-manifest'):
        value.add_argument('--' + flag, type=Path)
    value.add_argument('--manifest-sha256')
    value.add_argument('--binding-sha256')
    value.add_argument('--handoff-sha256')
    value.add_argument('--runtime-manifest-sha256')
    return value


def build_plan(args, binding, binding_path, binding_sha):
    from backend.experiments.sky_mask_plan import build_sky_c_plan
    root = ordinary(args.experiment_root, missing=True)
    require(not root.exists(), 'SKY_NEW_COMPUTE_SCENE_REQUIRED')
    first = binding['images'][0]
    plan = build_sky_c_plan(binding_path=binding_path, binding_sha256=binding_sha,
        pipeline_root=args.pipeline_root, experiment_root=root,
        dimensions=(first['width'], first['height']), map_python=Path(sys.executable),
        adapter_script=Path(__file__).absolute(), config_path=args.pipeline_root / 'configs/pipeline.json',
        paths_path=args.pipeline_root / 'configs/paths.local.json')
    return {'scope': 'post_sfm_c_dense_texture_only', 'sfm_mask_applied': False,
        'execution_authorized': False, 'budget_claimed': False, 'budget_charged': False,
        'compute_root_created': False, 'scene': root.name,
        'stages': [stage.observation() for stage in plan.stages],
        'runtime_integration_note': 'Stage 03 uses the opt-in adapter. Existing default C/web executors are not modified or implicitly authorized by this plan.'}


def runtime_verifier():
    # The algorithm environment contains an unrelated installed ``scripts``
    # package. Load this repository's verifier by exact path, not namespace.
    path = REPOSITORY / 'scripts/run_local_c.py'
    spec = importlib.util.spec_from_file_location('re3d_sky_runtime_verifier', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_runtime_manifest


def verify_prepared(binding, root, guard):
    from PIL import Image
    expected = {row['filename']: row for row in binding['images']}
    for folder, suffix in (('dataset', 'masks'), ('openmvs-input', 'images')):
        receipt = read_json(root / folder / 'sky-mask-consumption.json')
        require(receipt['source_mask_manifest_sha256'] == binding['manifest_sha256']
                and receipt['consumed_count'] == len(expected), 'SKY_CONSUMPTION_RECEIPT_CHANGED')
        require({r['filename'] for r in receipt['files']} == set(expected), 'SKY_CONSUMPTION_SET_CHANGED')
        for row in receipt['files']:
            guard(); name = row['filename']; item = expected[name]
            require(row['external_pixel_sha256'] == item['pixel_sha256'], 'SKY_CONSUMED_WRONG_MASK')
            rel = ('masks/' + name + '.png') if suffix == 'masks' else ('images/' + Path(name).stem + '.mask.png')
            require(row['mask'] == rel, 'SKY_PREPARED_MASK_PATH_CHANGED')
            path = root / folder / rel
            require(file_sha(path) == row['mask_sha256'], 'SKY_PREPARED_MASK_HASH_CHANGED')
            with Image.open(path) as im:
                im.load()
                require(im.mode == 'L' and im.size == (item['width'], item['height'])
                        and hashlib.sha256(im.tobytes()).hexdigest() == row['effective_pixel_sha256'],
                        'SKY_PREPARED_PIXELS_CHANGED')
                # All actual Lighthouse inputs are RGB JPG: effective mask must
                # equal sky labels, not merely exist or have a nonzero count.
                require(row['effective_pixel_sha256'] == item['pixel_sha256'],
                        'SKY_EFFECTIVE_MASK_DIFFERS_FROM_ACCEPTED_LABELS')
    text_prefix = 'work/shared/colmap/refined_text/'
    for row in binding['sfm_text_files']:
        guard()
        require(row['relative_path'].startswith(text_prefix), 'SKY_SPARSE_PATH_CHANGED')
        name = row['relative_path'][len(text_prefix):]
        path = ordinary(root / 'openmvs-input/sparse' / name)
        require(path.stat().st_size == row['size_bytes'] and file_sha(path) == row['sha256'],
                'SKY_PREPARED_SPARSE_HASH_CHANGED')
    return {'actual_frozen_helpers_consumed_masks': True, 'dataset_mask_count': len(expected),
        'openmvs_mask_count': len(expected), 'pixel_equality_to_bound_sky_masks': True,
        'prepared_sparse_text_matches_verified_handoff': True,
        'native_densify_or_texture_filtering_executed': False}


def main(argv=None):
    args = parser().parse_args(argv)
    if args.mode == 'status':
        require(not args.prepare_inputs, 'SKY_PREPARE_FLAG_MODE_MISMATCH')
        return {'status': 'not_executed', 'scope': 'post_sfm_c_dense_texture_only',
                'reconstruction_started': False, 'execution_authorized': False}
    require(args.prepare_inputs == (args.mode == 'prepare'), 'SKY_PREPARE_FLAG_MODE_MISMATCH')
    if args.mode == 'inspect':
        require(args.source and args.masks and args.manifest_sha256, 'SKY_ASSET_ARGUMENTS_REQUIRED')
        binding = inspect_masks(args.source, args.masks, expected_manifest_sha256=args.manifest_sha256)
        return {'status': 'mask_assets_inspected', 'image_count': len(binding['images']),
                'execution_authorized': False, 'reconstruction_started': False}
    require(args.pipeline_root is not None, 'SKY_PIPELINE_ROOT_REQUIRED')
    if args.mode == 'prepare-openmvs':
        require(all((args.binding, args.binding_sha256, args.images, args.sparse, args.output)),
                'SKY_ADAPTER_ARGUMENTS_REQUIRED')
        binding = read_json(args.binding, args.binding_sha256)
        require(binding.get('mask_scope') == 'post_sfm_c_dense_texture_only', 'SKY_BINDING_SCOPE_INVALID')
        guard = PreparationGuard(args.output.parent); guard()
        result = prepare_frozen_openmvs(binding, pipeline_root=args.pipeline_root,
            images=args.images, sparse=args.sparse, output=args.output, guard=guard)
        return {'status': result['status'], 'consumed_count': result['consumed_count'],
                'reconstruction_started': False}
    if args.mode == 'prepare':
        from backend.experiments.sky_sfm_binding import bind_sfm_images
        require(all((args.source, args.masks, args.manifest_sha256, args.handoff_root,
                     args.handoff_sha256, args.sfm_input_manifest, args.output, args.experiment_root,
                     args.runtime_manifest, args.runtime_manifest_sha256)),
                'SKY_PREPARE_ARGUMENTS_REQUIRED')
        output = ordinary(args.output, missing=True)
        require(not output.exists(), 'SKY_NEW_ADMISSION_ROOT_REQUIRED')
        require(all(not output.is_relative_to(ordinary(p)) and not ordinary(p).is_relative_to(output)
                    for p in (args.source, args.masks, args.handoff_root, args.pipeline_root)),
                'SKY_ADMISSION_SOURCE_OVERLAP')
        guard = PreparationGuard(output.parent); guard()
        require(shutil.disk_usage(output.parent).free >= 28 * 1024**3, 'SKY_ADMISSION_DISK_28GIB')
        require(shutil.disk_usage(Path(output.anchor)).free >= 28 * 1024**3, 'SKY_ADMISSION_DISK_28GIB')
        initial = inspect_masks(args.source, args.masks,
            expected_manifest_sha256=args.manifest_sha256, guard=guard)
        binding = bind_sfm_images(initial, handoff_root=args.handoff_root,
            handoff_sha256=args.handoff_sha256, input_manifest=args.sfm_input_manifest, guard=guard)
        runtime = runtime_verifier()(args.runtime_manifest, args.runtime_manifest_sha256, args.pipeline_root)
        require(runtime['python'] == str(Path(sys.executable)), 'SKY_RUNTIME_INTERPRETER_CHANGED')
        binding.update(mask_scope='post_sfm_c_dense_texture_only', sfm_mask_applied=False,
            source_state='uncommitted_candidate_on_experiment_local_c_only',
            compute_root=str(args.experiment_root), execution_authorized=False)
        output.mkdir()
        binding_path = output / 'binding.json'; write_new(binding_path, binding)
        pin = file_sha(binding_path)
        plan = build_plan(args, binding, binding_path, pin)
        write_new(output / 'plan.json', plan)
        prepare_frozen_dataset(binding, pipeline_root=args.pipeline_root, output=output / 'dataset', guard=guard)
        prepare_frozen_openmvs(binding, pipeline_root=args.pipeline_root,
            images=output / 'dataset/images', sparse=Path(binding['sfm_source_sparse']),
            output=output / 'openmvs-input', guard=guard)
        proof = verify_prepared(binding, output, guard)
        report = {'status': 'mask_input_and_plan_preflight_passed', 'mask_scope': binding['mask_scope'],
            'binding_sha256': pin, 'source_mask_manifest_sha256': binding['manifest_sha256'],
            'image_count': len(binding['images']), 'sfm_registered_images': len(binding['images']),
            'sfm_mask_applied': False, 'sfm_name_mapping': 'one-to-one original JPEG SHA256, not input order',
            'runtime_manifest_sha256': args.runtime_manifest_sha256,
            'runtime_file_count': len(runtime['files']), 'native_programs_not_started': True,
            **proof, 'resources': guard.summary(), 'reconstruction_started': False,
            'execution_authorized': False, 'budget_claimed': False, 'budget_charged': False,
            'budget_reset': False, 'server_accessed': False, 'uac_requested': False,
            'compute_root_created': False, 'existing_results_modified': False,
            'deferred_execution_gates': ['explicit_supervised_execution_authorization',
                'cumulative_budget_accounting_including_previous_C_charge',
                'execution_time_resource_recheck_and_owned_process_containment',
                'native_Densify_and_Texture_filtering_acceptance']}
        write_new(output / 'preflight.json', report)
        return report
    require(all((args.binding, args.binding_sha256, args.output, args.experiment_root)),
            'SKY_PREFLIGHT_ARGUMENTS_REQUIRED')
    binding = read_json(args.binding, args.binding_sha256)
    guard = PreparationGuard(args.output); guard()
    proof = verify_prepared(binding, args.output, guard)
    build_plan(args, binding, args.binding, args.binding_sha256)
    return {'status': 'prepared_mask_inputs_read_only_recheck_passed', **proof,
            'execution_authorized': False, 'reconstruction_started': False,
            'budget_claimed': False, 'budget_charged': False}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2, allow_nan=False))
    except Exception as error:
        code = getattr(error, 'code', 'SKY_INPUT_ADMISSION_FAILED')
        if not isinstance(code, str) or not code.replace('_', '').isalnum():
            code = 'SKY_INPUT_ADMISSION_FAILED'
        print(json.dumps({'status': 'failed', 'error_code': code,
                          'reconstruction_started': False, 'execution_authorized': False}))
        raise SystemExit(1) from None
