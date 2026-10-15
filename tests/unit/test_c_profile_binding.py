from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.experiments.c_profile import (
    BASE_COMMIT, BASE_CONFIG_SHA, COLMAP_SCRIPT_SHA, PIPELINE_SCRIPT_SHA,
    GIB, bind_c_profile, validate_c_profile, validate_terminal_binding,
    verify_handoff_receipt_binding,
)
from backend.experiments.supervision import SupervisionError
from tests.unit.test_c_profile import local_c_profile, terminal_receipt
from tests.unit.test_cpu_runner import local_profile


class CProfileBindingTests(unittest.TestCase):
    def setUp(self):
        # Lexical, nonexistent paths only: no private data, native binary or host.
        self.root = Path(__file__).absolute().parents[2] / '.tmp-tests/pure-c-binding'
        self.template = local_c_profile(self.root)
        for key in ('cpu_result_sha256', 'terminal_receipt_sha256', 'handoff_manifest_sha256'):
            self.template['source'][key] = None
        self.template['budget']['source_terminal_receipt_sha256'] = None
        self.cpu = local_profile(self.root)
        self.cpu['cpu']['sfm_threads'] = 8
        self.receipt = terminal_receipt()
        self.receipt.update(scene=self.cpu['target_experiment']['scene'],
            source_job_id=self.cpu['source_job_id'],
            result={'final_sfm_gate': {'database_images': 300, 'registered_images': 300,
                                      'points3D': 123456}})
        self.receipt['tool_identity']['profile_sha256'] = 'e' * 64
        self.receipt['hashes']['final_database_sha256'] = 'd' * 64
        cp = self.cpu['source_checkpoint']
        self.manifest = {
            'schema_version': '1.0', 'kind': 're3d_cpu_sfm_handoff',
            'source': {'source_job_id': self.cpu['source_job_id'],
                'pipeline_commit': BASE_COMMIT, 'pipeline_config_sha256': BASE_CONFIG_SHA,
                'original_checkpoint_manifest_sha256': cp['original_manifest_sha256'],
                'original_database_sha256': cp['original_database_sha256'],
                'selected_database_sha256': cp['snapshot_database_sha256'],
                'snapshot_manifest_sha256': cp['snapshot_manifest_sha256'],
                'selected_complete_pair_records': cp['expected_complete_pair_records']},
            'execution': {'source_experiment_root': self.cpu['target_experiment']['root'],
                'sfm_threads': 8, 'mapper_threads': 16, 'python_version': '3.12.14',
                'pycolmap_version': '3.13.0'},
            'quality_config_sha256': BASE_CONFIG_SHA,
            'scripts': {'run_pipeline_sha256': PIPELINE_SCRIPT_SHA,
                        'run_colmap_shared_sha256': COLMAP_SCRIPT_SHA},
            'files': [{'relative_path': 'work/shared/colmap/database.db',
                       'sha256': 'd' * 64, 'size_bytes': 4096}],
            'sfm_gate': deepcopy(self.receipt['result']['final_sfm_gate']),
            'complete_pair_records': 44850,
            'completion': {key: True for key in ('process_tree_stopped', 'snapshot_worker_stopped',
                'terminal_evidence_confirmed', 'source_checkpoint_unchanged')},
            'old_platform_contracts_copied': False, 'geometry_accuracy_certified': False,
        }
        self.handoff = SimpleNamespace(root=self.root / 'actual-handoff',
            manifest_sha256='f' * 64, manifest=self.manifest)

    def bind(self, template=None, **kwargs):
        arguments = {'terminal_receipt': self.receipt, 'terminal_receipt_sha256': 'b' * 64,
            'verified_handoff': self.handoff, 'source_cpu_profile': self.cpu,
            'source_cpu_profile_sha256': 'e' * 64}
        arguments.update(kwargs)
        return bind_c_profile(self.template if template is None else template, **arguments)

    def provenance(self):
        self.manifest['database_source'] = {
            'kind': 'validated_final_snapshot', 'snapshot_directory': 'snapshot-synthetic',
            'cpu_result_sha256': 'a' * 64, 'cpu_profile_sha256': 'e' * 64,
            'snapshot_manifest_sha256': '1' * 64, 'snapshot_done_sha256': '2' * 64,
            'database_sha256': 'd' * 64, 'database_size_bytes': 4096,
            'validated_complete_pair_records': 44850}

    def test_binding_is_pure_and_does_not_mutate_any_inputs(self):
        before = deepcopy((self.template, self.cpu, self.receipt, self.manifest))
        with patch.object(Path, 'open', side_effect=AssertionError('read forbidden')), \
                patch.object(Path, 'exists', side_effect=AssertionError('probe forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('launch forbidden')):
            bound = self.bind()
            validate_c_profile(bound)
        self.assertEqual(before, (self.template, self.cpu, self.receipt, self.manifest))
        self.assertEqual(bound['source'], {'root': str(self.handoff.root),
            'cpu_result_sha256': 'a' * 64, 'terminal_receipt_sha256': 'b' * 64,
            'handoff_manifest_sha256': 'f' * 64})
        for key in ('execution', 'pipeline', 'target_hardware', 'target_experiment', 'cpu', 'protection'):
            self.assertEqual(bound[key], self.template[key])
        self.assertIs(bound['autorun'], False)
        self.assertEqual(bound['cpu'], {'openmvs_threads': 8, 'texture_threads': 8})
        self.assertEqual(bound['protection']['available_ram_floor_bytes'], 8 * GIB)

    def test_receipt_not_historical_template_numbers_owns_remaining_budget(self):
        self.receipt['timing'].update(cumulative_budget_charge_seconds=6000,
                                      unused_total_budget_seconds=8400)
        bound = self.bind()
        self.assertEqual(bound['budget']['consumed_sfm_seconds'], 6000)
        self.assertEqual(bound['budget']['remaining_seconds'], 8400)

    def test_roundoff_uses_smaller_receipt_or_total_minus_charge(self):
        self.receipt['timing'].update(cumulative_budget_charge_seconds=6000.0004,
                                      unused_total_budget_seconds=8400)
        bound = self.bind()
        self.assertLessEqual(bound['budget']['remaining_seconds'], 8399.9996)
        validate_terminal_binding(bound, self.receipt, 'b' * 64)
        self.receipt['timing'].update(cumulative_budget_charge_seconds=6000,
                                      unused_total_budget_seconds=8399.9996)
        bound = self.bind()
        self.assertLessEqual(bound['budget']['remaining_seconds'], 8399.9996)

    def test_already_bound_identical_source_is_idempotent(self):
        bound = self.bind()
        self.assertEqual(self.bind(template=bound), bound)

    def test_already_bound_identity_or_budget_cannot_be_reclaimed(self):
        bound = self.bind()
        for key, value in [('cpu_result_sha256', '1' * 64), ('handoff_manifest_sha256', '2' * 64),
                           ('terminal_receipt_sha256', None), ('root', str(self.root / 'different'))]:
            candidate = deepcopy(bound); candidate['source'][key] = value
            with self.subTest(key=key), self.assertRaises(SupervisionError):
                self.bind(template=candidate)
        bound['budget'].update(consumed_sfm_seconds=1, remaining_seconds=14399)
        with self.assertRaises(SupervisionError):
            self.bind(template=bound)

    def test_all_terminal_stop_and_evidence_proofs_remain_mandatory(self):
        for key in self.receipt['proofs']:
            receipt = deepcopy(self.receipt); receipt['proofs'][key] = False
            with self.subTest(key=key), self.assertRaises(SupervisionError):
                self.bind(terminal_receipt=receipt)
        receipt = deepcopy(self.receipt); receipt['timing']['total_budget_not_reset_or_extended'] = False
        with self.assertRaises(SupervisionError):
            self.bind(terminal_receipt=receipt)

    def test_nonaccepted_cpu_source_or_invalid_hashes_fail(self):
        for mutation in (lambda r: r.update(status='running'), lambda r: r.update(schema_version='2.0'),
                lambda r: r['hashes'].update(cpu_result_sha256=None),
                lambda r: r['tool_identity'].update(frozen_pipeline_commit='0' * 40)):
            receipt = deepcopy(self.receipt); mutation(receipt)
            with self.subTest(mutation=mutation), self.assertRaises(SupervisionError):
                self.bind(terminal_receipt=receipt)
        for key in ('terminal_receipt_sha256', 'source_cpu_profile_sha256'):
            with self.subTest(key=key), self.assertRaises(SupervisionError):
                self.bind(**{key: 'not-a-hash'})

    def test_source_profile_receipt_scene_job_and_hash_are_exact(self):
        for key, value in [('scene', 'wrong-scene'), ('source_job_id', '0' * 36)]:
            receipt = deepcopy(self.receipt); receipt[key] = value
            with self.subTest(key=key), self.assertRaises(SupervisionError):
                self.bind(terminal_receipt=receipt)
        with self.assertRaises(SupervisionError):
            self.bind(source_cpu_profile_sha256='7' * 64)

    def test_cpu_profile_must_remain_actual_cpu_only_frozen_and_guarded(self):
        for mutation in (lambda p: p.update(autorun=True),
                lambda p: p['runtime'].update(pycolmap_has_cuda=True),
                lambda p: p['pipeline'].update(commit='0' * 40),
                lambda p: p['protection'].update(available_ram_floor_bytes=7 * GIB)):
            cpu = deepcopy(self.cpu); mutation(cpu)
            with self.subTest(mutation=mutation), self.assertRaises(SupervisionError):
                self.bind(source_cpu_profile=cpu)

    def test_handoff_execution_threads_runtime_and_exact_root_match_profile(self):
        for key, value in [('sfm_threads', 12), ('mapper_threads', 8), ('python_version', '3.12.13'),
                ('pycolmap_version', '3.12.0'),
                ('source_experiment_root', str(self.root / 'different/_experiments/synthetic-local-sfm'))]:
            original = self.manifest['execution'][key]
            self.manifest['execution'][key] = value
            with self.subTest(key=key), self.assertRaises(SupervisionError): self.bind()
            self.manifest['execution'][key] = original

    def test_handoff_source_checkpoint_identity_is_not_rebound_to_another_source(self):
        for key in ('original_checkpoint_manifest_sha256', 'original_database_sha256',
                    'selected_database_sha256', 'snapshot_manifest_sha256', 'selected_complete_pair_records'):
            original = self.manifest['source'][key]
            self.manifest['source'][key] = 1 if type(original) is int else '0' * 64
            with self.subTest(key=key), self.assertRaises(SupervisionError): self.bind()
            self.manifest['source'][key] = original

    def test_handoff_database_gate_and_duplicate_database_record_fail_closed(self):
        original = deepcopy(self.manifest)
        for mutation in (lambda m: m['files'][0].update(sha256='9' * 64),
                lambda m: m['files'].append(deepcopy(m['files'][0])),
                lambda m: m['sfm_gate'].update(registered_images=299),
                lambda m: m['sfm_gate'].update(database_images=True)):
            self.manifest.clear(); self.manifest.update(deepcopy(original)); mutation(self.manifest)
            with self.subTest(mutation=mutation), self.assertRaises(SupervisionError): self.bind()

    def test_snapshot_provenance_binds_cpu_result_profile_and_database(self):
        self.provenance()
        self.assertEqual(self.bind()['source']['cpu_result_sha256'], 'a' * 64)
        original = deepcopy(self.manifest['database_source'])
        for key, value in [('cpu_result_sha256', '8' * 64), ('cpu_profile_sha256', '8' * 64),
                ('database_sha256', '8' * 64), ('database_size_bytes', 1),
                ('validated_complete_pair_records', 40000), ('kind', 'unverified')]:
            self.manifest['database_source'] = deepcopy(original)
            self.manifest['database_source'][key] = value
            with self.subTest(key=key), self.assertRaises(SupervisionError): self.bind()

    def test_handoff_identity_completion_and_quality_cannot_change(self):
        original = deepcopy(self.manifest)
        for mutation in (lambda m: m.update(quality_config_sha256='0' * 64),
                lambda m: m['scripts'].update(run_pipeline_sha256='0' * 64),
                lambda m: m['source'].update(pipeline_commit='0' * 40),
                lambda m: m.update(old_platform_contracts_copied=True),
                lambda m: m.update(geometry_accuracy_certified=True),
                lambda m: m['completion'].update(process_tree_stopped=1)):
            self.manifest.clear(); self.manifest.update(deepcopy(original)); mutation(self.manifest)
            with self.subTest(mutation=mutation), self.assertRaises(SupervisionError): self.bind()

    def test_template_cannot_reduce_guards_raise_threads_or_use_source_target(self):
        for mutation in (lambda p: p['cpu'].update(openmvs_threads=12),
                lambda p: p['protection'].update(available_ram_floor_bytes=7 * GIB),
                lambda p: p['execution'].update(branches=['a', 'b', 'c']),
                lambda p: p['target_experiment'].update(**self.cpu['target_experiment']),
                lambda p: p['budget'].update(extra='unrecognized')):
            template = deepcopy(self.template); mutation(template)
            with self.subTest(mutation=mutation), self.assertRaises(SupervisionError):
                self.bind(template=template)

    def test_malformed_receipt_and_handoff_are_bounded_validation_failures(self):
        for receipt in ({}, {'hashes': None}, {'hashes': {}, 'tool_identity': None}):
            with self.subTest(receipt=receipt), self.assertRaises(SupervisionError):
                self.bind(terminal_receipt=receipt)
        for handoff in (None, SimpleNamespace(root=self.handoff.root, manifest={}, manifest_sha256=None)):
            with self.subTest(handoff=handoff), self.assertRaises(SupervisionError):
                self.bind(verified_handoff=handoff)


if __name__ == '__main__':
    unittest.main()
