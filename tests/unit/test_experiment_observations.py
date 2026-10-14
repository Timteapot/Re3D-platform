from pathlib import Path
from copy import deepcopy
import json
import os
import threading
from types import SimpleNamespace
import unittest

from backend.experiments.long_runner import declared_failure_codes
from backend.experiments.preflight import (ExperimentGateError, IsolationGuard, SERVICES, validate_isolation,
    _verified_launcher_instances, _windows_argv)
from backend.experiments.supervision import ObservationRelay, WholeRunBudget


class ObservationIntegrationTests(unittest.TestCase):
    def launcher_fixture(self):
        root = Path(r'D:\Re3D-platform')
        entry = str(root / '.venv' / 'Scripts' / 'python.exe')
        base = r'C:\Program Files\Re3DPython312\python.exe'
        arguments = [r'D:\tools\scripts\run_long_experiment.py', '--mode', 'admission', '--admission-threads', '8']
        rows = [
            {'pid':100,'parent_pid':99,'executable':base,'creation_time':'2026-10-14T01:00:01+00:00',
             'command_line':json.dumps([base,*arguments]),'reconstruction':True},
            {'pid':99,'parent_pid':98,'executable':entry,'creation_time':'2026-10-14T01:00:00+00:00',
             'command_line':json.dumps([entry,*arguments]),'reconstruction':True},
        ]
        return root, entry, base, rows

    def launcher_instances(self, rows=None, *, executable=None):
        root, entry, base, original = self.launcher_fixture()
        return _verified_launcher_instances(original if rows is None else rows, root,
            current_pid=100,parent_pid=99,executable=entry if executable is None else executable,
            base_executable=base,argv_reader=json.loads)

    def test_direct_venv_redirector_requires_exact_identity_and_equal_nonzero_argv(self):
        self.assertEqual(self.launcher_instances(),
                         {100:'2026-10-14T01:00:01+00:00',99:'2026-10-14T01:00:00+00:00'})

    def test_wrong_parent_exe_args_lineage_or_creation_is_rejected(self):
        _, _, _, rows = self.launcher_fixture()
        for index, key, value in (
            (1,'executable',r'D:\another\.venv\Scripts\python.exe'),
            (1,'command_line',json.dumps(['python.exe','foreign.py'])),
            (0,'executable',r'C:\other\python.exe'),
            (0,'parent_pid',98),
            (1,'creation_time','2026-10-14T01:00:02+00:00'),
            (1,'creation_time',None),
        ):
            changed = deepcopy(rows)
            changed[index][key] = value
            with self.subTest(index=index,key=key), self.assertRaisesRegex(ExperimentGateError,'LAUNCHER_OWNERSHIP_UNVERIFIED'):
                self.launcher_instances(changed)
        with self.assertRaisesRegex(ExperimentGateError,'LAUNCHER_OWNERSHIP_UNVERIFIED'):
            self.launcher_instances(rows[:1])

    def test_nonvenv_execution_owns_only_self_not_an_ancestor(self):
        self.assertEqual(self.launcher_instances(executable=r'C:\standalone\python.exe'),
                         {100:'2026-10-14T01:00:01+00:00'})

    def test_launcher_instance_reuse_or_unrelated_sibling_is_not_owned(self):
        _, _, _, rows = self.launcher_fixture()
        state = self.state()
        state['host']['processes'] = rows
        ownership = self.launcher_instances()
        options = dict(owned_pids=set(ownership),owned_process_instances=ownership,launcher_pids={99})
        validate_isolation(state,'source',allow_expired_source_job=False,**options)
        changed = deepcopy(state)
        changed['host']['processes'][1]['creation_time'] = '2026-10-14T01:01:00+00:00'
        with self.assertRaisesRegex(ExperimentGateError,'LAUNCHER_INSTANCE_CHANGED'):
            validate_isolation(changed,'source',allow_expired_source_job=False,**options)
        state['host']['processes'].append({'pid':111,'parent_pid':99,'reconstruction':True})
        with self.assertRaisesRegex(ExperimentGateError,'OTHER_RECONSTRUCTION'):
            validate_isolation(state,'source',allow_expired_source_job=False,**options)

    def test_runtime_guard_pins_launcher_instance_after_dynamic_children_clear(self):
        _, _, _, rows = self.launcher_fixture()
        state = self.state()
        state['host']['processes'] = rows
        ownership = self.launcher_instances()
        guard = IsolationGuard(lambda:state,'source',allow_expired_source_job=False,interval_seconds=.01,
            stale_seconds=1,static_owned_pids=set(ownership),static_owned_process_instances=ownership,static_launcher_pids={99})
        guard.register_pid(101);guard.clear_owned_pids()
        guard.start()
        try:
            state['host']['processes'][1]['creation_time'] = '2026-10-14T01:01:00+00:00'
            guard.thread.join(timeout=.5)
            with self.assertRaisesRegex(ExperimentGateError,'LAUNCHER_INSTANCE_CHANGED'):
                guard.check_health()
        finally:
            guard.close()

    @unittest.skipUnless(os.name == 'nt', 'native Windows argv parser')
    def test_native_windows_argv_disregards_only_executable_quoting(self):
        self.assertEqual(_windows_argv('"C:\\Program Files\\base\\python.exe" "D:\\tools\\run_long_experiment.py" --mode admission')[1:],
                         _windows_argv('D:\\venv\\python.exe D:\\tools\\run_long_experiment.py --mode admission')[1:])

    def state(self, foreign=False):
        processes = [{'pid':100,'parent_pid':0,'reconstruction':True},
                     {'pid':101,'parent_pid':100,'reconstruction':True}]
        if foreign: processes.append({'pid':999,'parent_pid':0,'reconstruction':True})
        return {'host':{'services':[{'name':name,'status':'Stopped'} for name in SERVICES],
                        'processes':processes},
                'queue':{'read_only':True,'nonterminal_jobs':[],'active_uploads':0,'leases':[]}}

    def test_static_supervisor_is_preserved_when_dynamic_child_ownership_clears(self):
        guard = IsolationGuard(lambda:self.state(), 'source', allow_expired_source_job=False,
                               static_owned_pids={100})
        guard.register_pid(101)
        guard.clear_owned_pids()
        self.assertEqual(guard.static_owned_pids, {100})
        self.assertEqual(guard.owned_pids, set())
        result = validate_isolation(self.state(), 'source', allow_expired_source_job=False,
                                    owned_pids=set(guard.static_owned_pids))
        self.assertEqual(result['foreign_reconstruction_processes'],0)

    def test_static_ownership_does_not_whitelist_unrelated_reconstruction(self):
        with self.assertRaisesRegex(ExperimentGateError, 'OTHER_RECONSTRUCTION'):
            validate_isolation(self.state(True),'source',allow_expired_source_job=False,owned_pids={100})

    def test_periodic_snapshot_pointer_is_mirrored_to_small_witness_without_disk_reads(self):
        records, ready = [], threading.Event()
        def append(kind, value):
            records.append((kind,value))
            if value.get('event_code')=='SNAPSHOT_COMPLETED': ready.set()
        monitor = SimpleNamespace(summary=lambda:{'latest':{'cpu':{'utilization_percent':30}},
            'peaks':{},'errors':[],'observation_ages_seconds':{'host':1,'gpu':2,'directory':3}})
        evidence = SimpleNamespace(append=append)
        database = SimpleNamespace(is_file=lambda:True)
        relay = ObservationRelay(monitor,evidence,database,WholeRunBudget(10),
            resource_seconds=.02,pair_seconds=.02,pair_reader=lambda _: {'complete_pair_records':10})
        relay.snapshot_store = SimpleNamespace(latest={'directory':'snapshot-20261014T010000123456Z-'+'a'*32,
            'database_sha256':'b'*64,'validated_complete_pair_records':10})
        relay.set_stage('01-colmap');relay.start()
        try:
            self.assertTrue(ready.wait(1))
            relay.check_health()
        finally:
            self.assertTrue(relay.close())
        events = [value for _,value in records if value.get('event_code')=='SNAPSHOT_COMPLETED']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['latest_snapshot_sha256'],'b'*64)
        self.assertEqual(events[0]['complete_pairs'],10)
        resources = [value for kind,value in records if kind=='resources'][0]
        self.assertEqual(resources['host_age_seconds'],1)
        self.assertNotIn('argv',str(records))

    def test_code_allowlist_comes_from_tool_literals_not_arbitrary_exception_text(self):
        codes = declared_failure_codes(Path(__file__).resolve().parents[2])
        self.assertIn('LONG_TREE_STOP_UNCONFIRMED',codes)
        self.assertIn('EXPERIMENT_STAGE_PATH_OUTSIDE_ROOT',codes)
        self.assertIn('EXPERIMENT_PROCESS_LOG_IO_TIMED_OUT',codes)
        self.assertNotIn('DATABASE_URL',codes)
        self.assertNotIn('ARBITRARY_USER_TEXT',codes)


if __name__=='__main__': unittest.main()
