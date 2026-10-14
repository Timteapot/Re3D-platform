from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from backend.experiments.high_profile import (BASE_COMMIT, BASE_CONFIG_SHA, adapt_sfm_argv, apply_mapper_budget, build_pipeline_overlay, capture_stage_plan, file_sha,
    mapper_budget_adapter, validate_profile, validate_stage_plan)
from backend.experiments.preflight import ExperimentGateError


def profile():
    return {'profile_kind': 'offline_high_cpu_candidate', 'autorun': False,
            'full_guarded_launcher_ready': False, 'server_applied': False,
            'pipeline': {'commit': BASE_COMMIT, 'baseline_config_sha256': BASE_CONFIG_SHA, 'baseline_modified': False, 'quality_parameters_changed': False},
            'target_hardware': {'logical_cpus': 40},
            'cpu': {'sfm_threads': 12, 'mapper_ba_threads': 32, 'openmvs_threads': 32, 'texture_threads': 32, 'global_blas_omp_override': None},
            'gpu_preserved': {'task_concurrency': 1, 'branches_serial': True, 'map_outer_batch': 4, 'map_dense_head_minibatch': 1,
                              'map_use_amp': True, 'map_requested_amp_dtype': 'bf16', 'mvs_source_views': 7, 'mvs_height': 384, 'mvs_width': 384, 'mvs_refinement_steps': 1},
            'protection': {'disk_floor_bytes': 20 * 1024**3, 'reservation_bytes': 64 * 1024**3, 'available_ram_floor_bytes': 32 * 1024**3,
                           'free_vram_floor_bytes': 2 * 1024**3, 'host_guard_seconds': 1, 'gpu_sample_seconds': 5, 'directory_sample_seconds': 15, 'overall_timeout_seconds': 14400}}


class HighProfileTests(unittest.TestCase):
    def test_fixed_cpu_high_candidate_not_all_core_or_automatic(self):
        validate_profile(profile())
        for field, bad in (('sfm_threads', 16), ('sfm_threads', 40), ('sfm_threads', True),
                           ('mapper_ba_threads', -1), ('openmvs_threads', 0), ('texture_threads', 40)):
            with self.subTest(field=field, bad=bad):
                value = profile(); value['cpu'][field] = bad
                with self.assertRaises(ExperimentGateError): validate_profile(value)

    def test_gpu_growth_and_false_acceptance_claim_rejected(self):
        for field, bad in (('map_outer_batch', 8), ('map_dense_head_minibatch', 2), ('task_concurrency', 2), ('mvs_height', 512)):
            value = profile(); value['gpu_preserved'][field] = bad
            with self.assertRaises(ExperimentGateError): validate_profile(value)
        for field in ('autorun', 'server_applied', 'full_guarded_launcher_ready'):
            value = profile(); value[field] = True
            with self.assertRaises(ExperimentGateError): validate_profile(value)

    def test_headroom_and_probe_settings_cannot_be_lowered(self):
        for field, bad in (('disk_floor_bytes', 1), ('available_ram_floor_bytes', 1), ('free_vram_floor_bytes', 1),
                           ('host_guard_seconds', 10), ('gpu_sample_seconds', 20)):
            value = profile(); value['protection'][field] = bad
            with self.assertRaises(ExperimentGateError): validate_profile(value)

    def test_false_identity_or_boolean_concurrency_is_rejected(self):
        value = profile(); value['pipeline']['commit'] = '0' * 40
        with self.assertRaises(ExperimentGateError): validate_profile(value)
        value = profile(); value['gpu_preserved']['task_concurrency'] = True
        with self.assertRaises(ExperimentGateError): validate_profile(value)

    def test_overlay_changes_exactly_four_thread_keys_preserving_quality(self):
        baseline = {'a_v4': {'batch_size': 4, 'openmvs_threads': 16, 'quality': {'threshold': .05}},
                    'b_v2': {'source_views': 7, 'height': 384, 'width': 384, 'refinement_steps': 1, 'openmvs_threads': 24},
                    'c': {'number_views': 6, 'openmvs_threads': 24}, 'texture': {'max_threads': 24, 'max_texture_size': 8192},
                    'camera_frontend': {'camera_model': 'PINHOLE'}, 'mesh': {'close_holes': 30}}
        before = deepcopy(baseline)
        overlay, diff = build_pipeline_overlay(baseline, profile())
        self.assertEqual(baseline, before)
        self.assertEqual({row['path'] for row in diff}, {'/a_v4/openmvs_threads', '/b_v2/openmvs_threads', '/c/openmvs_threads', '/texture/max_threads'})
        self.assertEqual(overlay['a_v4']['quality'], baseline['a_v4']['quality'])
        self.assertNotIn('num_threads', overlay['camera_frontend'])

    def test_native_sfm_argv_injection_is_explicit_not_an_ignored_json_key(self):
        command = ['python.exe', 'D:/Re3D/scripts/run_colmap_shared.py', '--images', 'images']
        value = adapt_sfm_argv(command, profile())
        self.assertEqual(value[-4:], ['--num-threads', '12', '--max-num-features', '6000'])
        self.assertEqual(command[-2:], ['--images', 'images'])
        self.assertNotIn('--mapper-num-threads', value)  # Not supported by frozen CLI.
        with self.assertRaises(ExperimentGateError): adapt_sfm_argv(command + ['--num-threads', '8', '--num-threads', '4'], profile())

    def test_mapper_adapter_applies_top_level_and_retry_without_mutating_factory(self):
        def factory(*, random_seed):
            return SimpleNamespace(num_threads=-1, mapper=SimpleNamespace(num_threads=-1), random_seed=random_seed, check=lambda: True)
        module = SimpleNamespace(mapping_options=factory)
        with mapper_budget_adapter(module, 32):
            initial = module.mapping_options(random_seed=0)
            retry = module.mapping_options(random_seed=0)
            self.assertEqual((initial.num_threads, initial.mapper.num_threads), (32, 32))
            self.assertEqual((retry.num_threads, retry.mapper.num_threads), (32, 32))
            self.assertEqual(initial.random_seed, 0)
        self.assertIs(module.mapping_options, factory)
        self.assertEqual(module.mapping_options(random_seed=0).num_threads, -1)

    def test_mapper_capability_errors_restore_factory_and_fail_closed(self):
        factory = lambda: SimpleNamespace(num_threads=-1)
        module = SimpleNamespace(mapping_options=factory)
        with self.assertRaises(ExperimentGateError):
            with mapper_budget_adapter(module, 32): module.mapping_options()
        self.assertIs(module.mapping_options, factory)
        with self.assertRaises(ExperimentGateError): apply_mapper_budget(SimpleNamespace(), 32)

    def test_stage_validation_detects_unwired_threads_or_missing_step(self):
        stages = [{'stage': '01-colmap', 'argv': ['python.exe', 'run_colmap_shared.py', '--num-threads', '12'], 'stage_executed': False}]
        stages += [{'stage': f'{i:02d}-dense', 'argv': ['tool.exe', '--max-threads', '32'], 'stage_executed': False} for i in range(9)]
        stages += [{'stage': '20-a-mapanything', 'argv': ['python.exe', 'map.py', '--batch-size', '4'], 'stage_executed': False}]
        validate_stage_plan(stages, profile())
        stages[1]['argv'][-1] = '24'
        with self.assertRaises(ExperimentGateError): validate_stage_plan(stages, profile())

    def test_local_native_planner_no_execution_or_runtime_data_creation(self):
        pipeline = Path(__file__).resolve().parents[3] / 'Re3D'
        if not (pipeline / 'configs/pipeline.json').is_file():
            self.skipTest('Separate local frozen pipeline checkout not available')
        self.assertEqual(file_sha(pipeline / 'configs/pipeline.json'), BASE_CONFIG_SHA)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / '_experiments/high-profile-test'
            value = profile()
            value['target_experiment'] = {'scene': 'high-profile-test', 'root': str(target)}
            overlay, _ = build_pipeline_overlay(json.loads((pipeline / 'configs/pipeline.json').read_bytes()), value)
            config = root / 'overlay.json'
            config.write_text(json.dumps(overlay))
            with patch('subprocess.Popen', side_effect=AssertionError('Offline planner started compute')):
                stages = capture_stage_plan(pipeline, config, pipeline / 'configs/paths.local.json', value, [2048, 1080])
            self.assertEqual(len(stages), 30)
            self.assertFalse(target.exists())
            self.assertTrue(all(not row['stage_executed'] for row in stages))


if __name__ == '__main__':
    unittest.main()
