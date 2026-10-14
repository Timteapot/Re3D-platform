from pathlib import Path
import threading
from types import SimpleNamespace
import unittest

from backend.experiments.long_runner import declared_failure_codes
from backend.experiments.preflight import ExperimentGateError, IsolationGuard, SERVICES, validate_isolation
from backend.experiments.supervision import ObservationRelay, WholeRunBudget


class ObservationIntegrationTests(unittest.TestCase):
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
