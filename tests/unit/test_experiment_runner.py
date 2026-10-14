from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.experiments.preflight import ExperimentGateError, IsolationGuard, SERVICES, validate_isolation
from backend.experiments.runner import CONFIG_SHA, PIPELINE_BASE, build_sfm_command, read_pair_counts, snapshot_stopped_clone, summarize_window, validate_plan
from backend.experiments.resources import ResourceTrip

JOB = '6a90b5e9-0c99-41aa-881b-c196ebb96750'


def isolated_state(stale=True):
    return {'host': {'services': [{'name': name, 'status': 'Stopped'} for name in SERVICES], 'processes': []},
            'queue': {'read_only': True, 'active_uploads': 0,
                      'nonterminal_jobs': [{'id': JOB, 'status': 'sfm'}] if stale else [],
                      'leases': [{'resource_key': 'gpu:0', 'job_id': JOB if stale else None,
                                  'expires_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat() if stale else None}]}}


class IsolationTests(unittest.TestCase):
    def test_old_records_only_allowed_with_explicit_stale_authorization(self):
        state = isolated_state()
        before = deepcopy(state)
        with self.assertRaisesRegex(ExperimentGateError, 'REQUIRES_AUTHORIZATION'):
            validate_isolation(state, JOB, allow_expired_source_job=False)
        value = validate_isolation(state, JOB, allow_expired_source_job=True)
        self.assertTrue(value['source_stale_record_preserved'])
        self.assertTrue(value['source_expired_lease_preserved'])
        self.assertFalse(value['queue_modified'])
        self.assertEqual(before, state)

    def test_stopped_services_foreign_process_or_active_lease_block(self):
        for alter in ('service', 'process', 'lease', 'upload', 'job'):
            with self.subTest(alter=alter):
                state = isolated_state()
                if alter == 'service': state['host']['services'][0]['status'] = 'Running'
                if alter == 'process': state['host']['processes'] = [{'pid': 123, 'parent_pid': 0, 'reconstruction': True}]
                if alter == 'lease': state['queue']['leases'][0]['expires_at'] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                if alter == 'upload': state['queue']['active_uploads'] = 1
                if alter == 'job': state['queue']['nonterminal_jobs'][0]['id'] = 'foreign'
                with self.assertRaises(ExperimentGateError):
                    validate_isolation(state, JOB, allow_expired_source_job=True)

    def test_only_owned_tree_is_permitted(self):
        state = isolated_state()
        state['host']['processes'] = [{'pid': 10, 'parent_pid': 0, 'reconstruction': False},
                                     {'pid': 11, 'parent_pid': 10, 'reconstruction': True},
                                     {'pid': 12, 'parent_pid': 11, 'reconstruction': True}]
        validate_isolation(state, JOB, allow_expired_source_job=True, owned_pids={10})
        with self.assertRaises(ExperimentGateError):
            validate_isolation(state, JOB, allow_expired_source_job=True, owned_pids={9})

    def test_unknown_failure_sanitized_and_sticky(self):
        def bad(): raise RuntimeError('secret-value-must-not-appear')
        guard = IsolationGuard(bad, JOB, allow_expired_source_job=True, stale_seconds=.1)
        self.addCleanup(guard.close)
        with self.assertRaises(ExperimentGateError) as caught: guard.start()
        self.assertEqual(str(caught.exception), 'EXPERIMENT_ISOLATION_PROBE_FAILED')
        with self.assertRaises(ExperimentGateError): guard.check_health()

    def test_blocked_probe_is_bounded_and_fail_closed(self):
        unblock = threading.Event()
        guard = IsolationGuard(lambda: (unblock.wait(1), isolated_state())[1], JOB, allow_expired_source_job=True, stale_seconds=.05)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(ExperimentGateError, 'PROBE_STALE'): guard.start()
            self.assertLess(time.monotonic() - started, .5)
        finally:
            unblock.set(); guard.close()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'database.db'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('CREATE TABLE matches(pair_id INTEGER PRIMARY KEY,rows INTEGER); CREATE TABLE two_view_geometries(pair_id INTEGER PRIMARY KEY,rows INTEGER);')
            db.execute('INSERT INTO matches VALUES(1,0)'); db.execute('INSERT INTO two_view_geometries VALUES(1,0)')
            for table in ('images', 'keypoints', 'descriptors'):
                db.execute(f'CREATE TABLE {table}(image_id INTEGER PRIMARY KEY)')
                db.executemany(f'INSERT INTO {table} VALUES(?)', [(i,) for i in range(300)])

    def test_consistent_pair_reader_includes_empty_records_and_live_wal(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('INSERT INTO matches VALUES(2,20)'); writer.execute('INSERT INTO two_view_geometries VALUES(2,16)'); writer.commit()
            counts = read_pair_counts(self.db)
            self.assertEqual(counts['complete_pair_records'], 2)
            self.assertEqual(counts['nonempty_match_records'], 1)
            self.assertEqual(counts['geometry_min15_records'], 1)

    def test_stopped_snapshot_includes_wal_without_mutating_source(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('INSERT INTO matches VALUES(2,20)'); writer.execute('INSERT INTO two_view_geometries VALUES(2,16)'); writer.commit()
            before = self.db.read_bytes()
            target = self.root / 'post.db'
            result = snapshot_stopped_clone(self.db, target, lambda: None)
            self.assertEqual(result['integrity_check'], 'ok')
            self.assertEqual(read_pair_counts(target)['complete_pair_records'], 2)
            self.assertEqual(before, self.db.read_bytes())
            with self.assertRaises(ExperimentGateError): snapshot_stopped_clone(self.db, target, lambda: None)

    def test_guard_failure_aborts_backup_and_is_not_masked(self):
        def bad(): raise ResourceTrip('RAM_LOW')
        with self.assertRaisesRegex(ResourceTrip, 'RAM_LOW'):
            snapshot_stopped_clone(self.db, self.root / 'interrupted.db', bad)

    def test_throughput_is_committed_pair_rate_not_successful_match_rate(self):
        value = summarize_window([{'elapsed_seconds': 0, 'complete_pair_records': 25997},
                                  {'elapsed_seconds': 30, 'complete_pair_records': 26027},
                                  {'elapsed_seconds': 180, 'complete_pair_records': 26177}], 180)
        self.assertEqual(value['overall_pairs_per_minute'], 60)
        self.assertEqual(value['steady_pairs_per_minute'], 60)
        with self.assertRaises(ExperimentGateError): summarize_window([{'complete_pair_records': 2}, {'complete_pair_records': 1}], 10)

    def test_only_approved_thread_counts_and_fixed_quality_argv(self):
        command = build_sfm_command(Path('python.exe'), self.root, self.root / 'work', 8)
        self.assertEqual(command[command.index('--num-threads') + 1], '8')
        self.assertIn('--deferred-registration', command)
        self.assertNotIn('--use-gpu', command)
        with self.assertRaises(ExperimentGateError): build_sfm_command(Path('python.exe'), self.root, self.root / 'work', 12)

    def test_plan_rejects_reduced_headroom_or_sampling_changes(self):
        plan = {'execution_kind': 'standalone_checkpoint_experiment', 'autorun': False,
                'pipeline': {'commit': PIPELINE_BASE, 'config_sha256': CONFIG_SHA},
                'sfm': {'num_threads': 8, 'max_num_features': 6000, 'max_image_size': 1600, 'guided_matching': True,
                        'device': 'cpu', 'matching': 'exhaustive', 'camera_model': 'PINHOLE', 'random_seed': 0,
                        'deferred_registration': True, 'mapper_threads_override': None},
                'supervision': {'reservation_bytes': 64 * 1024**3, 'minimum_disk_free_bytes': 20 * 1024**3,
                                'disk_guard_interval_seconds': 1, 'resource_interval_seconds': 5, 'directory_sample_interval_seconds': 15}}
        policy = {'prepared_cpu_settings': {'sfm_feature_and_matching_threads': 8},
                  'preserved_gpu_and_quality_settings': {'task_gpu_concurrency': 1, 'mapanything': {'view_group_batch_size': 4}},
                  'promotion_test_limits': {'minimum_available_host_ram_bytes': 32 * 1024**3, 'minimum_free_gpu_vram_bytes': 2 * 1024**3}}
        validate_plan(plan, policy)
        policy['promotion_test_limits']['minimum_available_host_ram_bytes'] = 1024**3
        with self.assertRaises(ExperimentGateError): validate_plan(plan, policy)
        policy['promotion_test_limits']['minimum_available_host_ram_bytes'] = 32 * 1024**3
        plan['supervision']['disk_guard_interval_seconds'] = 5
        with self.assertRaises(ExperimentGateError): validate_plan(plan, policy)


class RunnerIntegrationTests(unittest.TestCase):
    def test_timeout_window_wal_snapshot_and_original_checkpoint_preserved(self):
        from tests.unit.test_experiment_checkpoint import ExperimentCheckpointTests, COMMIT
        from backend.experiments import runner
        from backend.re3d_adapter.errors import PipelineTimedOut
        fixture = ExperimentCheckpointTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before = fixture.fingerprint()
        plan = {'source_job_id': JOB, 'source_checkpoint': {'manifest_sha256': fixture.manifest_sha, 'database_sha256': fixture.database_sha}}
        policy = {'promotion_test_limits': {'minimum_available_host_ram_bytes': 32 * 1024**3, 'minimum_free_gpu_vram_bytes': 2 * 1024**3}}
        def private_root(root):
            root.mkdir(); (root / 'reports').mkdir()
            (root / 'operation-reservation.json').write_text('{}')
        class Monitor:
            def __init__(self, **kwargs): self.path = kwargs['sample_path']
            def start(self): self.path.touch()
            def check_health(self): pass
            def set_phase(self, name): pass
            def finish(self): pass
            def close(self): pass
            def summary(self): return {'errors': [], 'trip': None}
        class Isolation:
            def __init__(self, *args, **kwargs): self.last_summary = {'queue_modified': False}
            def start(self): pass
            def register_pid(self, pid): pass
            def clear_owned_pids(self): pass
            def check_health(self): pass
            def close(self): pass
        def short_process(command, **kwargs):
            kwargs['on_started'](123)
            kwargs['health_check']()
            time.sleep(.03)  # Simulated work exceeds Windows' coarse clock tick.
            database = fixture.target / 'work/shared/colmap/database.db'
            with closing(sqlite3.connect(database)) as db, db:
                for table in ('matches', 'two_view_geometries'):
                    db.execute(f'INSERT INTO {table} VALUES(?,?,?,?)', (2147483647 + 4, 1, 2, b'\0' * 8))
            raise PipelineTimedOut()
        with patch.object(runner, 'create_private_root', private_root), patch.object(runner, 'ExperimentResourceMonitor', Monitor), \
             patch.object(runner, 'IsolationGuard', Isolation), patch.object(runner, 'PIPELINE_BASE', COMMIT), \
             patch.object(runner, 'CONFIG_SHA', fixture.config_sha), patch.object(runner, 'run_contained_process', short_process), \
             patch.object(runner, 'read_windows_state', lambda: isolated_state()['host']), \
             patch.object(runner, 'read_queue_state', lambda _: isolated_state()['queue']):
            result = runner.run_window(plan=plan, policy=policy, checkpoint=fixture.source, root=fixture.target,
                                       pipeline_root=fixture.base, platform_root=fixture.base, interpreter=Path('python.exe'),
                                       threads=8, seconds=180, allow_stale=True)
        self.assertEqual(result['status'], 'benchmark_window_elapsed')
        self.assertEqual(result['throughput']['added_complete_pairs'], 1)
        self.assertEqual(result['post_window_database_snapshot']['validated_complete_pair_records'], 3)
        self.assertFalse(result['source_job_modified'])
        self.assertEqual(before, fixture.fingerprint())


if __name__ == '__main__':
    unittest.main()
