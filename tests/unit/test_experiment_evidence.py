from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.experiments.evidence import (
    ALL_TERMINAL_RESERVE_BYTES, EvidenceSink, EvidenceTrip, GIB, MAX_EVIDENCE_BYTES,
    _ordinary_directory, _private_acl, create_private_directory,
)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.free = 20 * GIB
        self.sinks = []
        self.releases = []
        self.counter = 0

    def tearDown(self):
        for release in self.releases:
            release.set()
        for sink in self.sinks:
            sink.close()
            # Tests release their deliberately blocked OS seams before cleanup.
            for thread in [*sink._threads.values(), *sink._terminal_threads]:
                thread.join(timeout=2)
        self.temporary.cleanup()

    def sink(self, **kwargs):
        self.counter += 1
        sink = EvidenceSink.create_for_test(
            self.parent / ("run-" + str(self.counter)),
            free_reader=kwargs.pop("free_reader", lambda _root: self.free), **kwargs,
        )
        self.sinks.append(sink)
        return sink

    def wait_trip(self, sink, code):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                sink.check_health()
            except EvidenceTrip as error:
                self.assertEqual(error.code, code)
                return error
            time.sleep(0.005)
        self.fail("missing trip")

    def wait_written(self, sink):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            value = sink.summary()
            if value["journal_bytes"] == value["journal_reserved_bytes"]:
                return
            sink.check_health()
            time.sleep(0.005)
        self.fail("journal did not finish")

    def test_live_journal_and_terminal_are_whitelisted_fsynced_and_separate(self):
        sink = self.sink(known_codes=["SUPERVISOR_STAGE_FAILED"])
        sink.start()
        with patch("backend.experiments.evidence.os.fsync", wraps=os.fsync) as fsync:
            sink.append("event", {"event_code": "RUN_START", "phase": "sfm", "run_id": "new-scene-1",
                                  "argv": ["secret"], "password": "password-that-must-not-escape"})
            sink.append("resources", {"latest": {"cpu": {"utilization_percent": 50, "logical_count": 40},
                "memory": {"used_bytes": 2 * GIB, "available_bytes": 32 * GIB, "total_bytes": 34 * GIB},
                "disk": {"free_bytes": 30 * GIB, "total_bytes": 100 * GIB},
                "gpu": {"index": 0, "memory_free_bytes": 5 * GIB, "name": "private-machine"}},
                "peaks": {"ram_used_bytes": 3 * GIB}, "observation_ages_seconds": {"host": 1, "gpu": 2}})
            sink.append("pairs", {"elapsed_seconds": 1.2, "complete_pair_records": 25998, "matches_rows": 25998,
                                  "geometry_rows": 25998, "private_log": "private-input-path"})
            self.wait_written(sink)
            initial = sink.finish_terminal({"status": "failed", "error_code": "SUPERVISOR_STAGE_FAILED", "tree_stopped": True,
                                            "source_job_modified": False, "exception": "secret exception"})
            initial_bytes = initial.read_bytes()
            final = sink.write_forensic_summary({"status": "failed", "snapshot_attempted": True,
                                                 "latest_snapshot_sha256": "a" * 64, "checkpoint_status": "valid"})
            self.assertEqual(initial.read_bytes(), initial_bytes)
            self.assertGreaterEqual(fsync.call_count, 5)
        sink.close()
        records = [json.loads(line) for line in (sink.root / "journal.jsonl").read_text().splitlines()]
        self.assertEqual([r["kind"] for r in records], ["event", "resources", "pairs"])
        terminal = json.loads(initial_bytes)
        self.assertEqual(terminal["last_event"]["payload"]["event_code"], "RUN_START")
        self.assertEqual(terminal["summary"]["error_code"], "SUPERVISOR_STAGE_FAILED")
        evidence = json.dumps([records, terminal, json.loads(final.read_bytes()), sink.summary()])
        for forbidden in ("secret", "private-machine", "private-input-path", str(self.parent)):
            self.assertNotIn(forbidden, evidence)

    def test_unknown_codes_and_nonfinite_or_invalid_values_never_escape(self):
        sink = self.sink()
        value = sink.sanitize({"error_code": "secret password", "event_code": "PASSWORD_SECRET",
            "errors": ["secret", "LOW_RAM_AUTO_STOPPED"], "forensic_notes": ["secret path"],
            "cpu_utilization_percent": float("nan"), "ram_used_bytes": 10**500,
            "phase": {"nested": "secret"}, "status": ["secret"], "pid": True,
            "config_sha256": "password", "source_job_id": "secret"})
        self.assertEqual(value["error_code"], "UNCLASSIFIED_ERROR")
        self.assertEqual(value["event_code"], "UNCLASSIFIED_EVENT")
        self.assertEqual(value["errors"], ["UNCLASSIFIED_ERROR", "LOW_RAM_AUTO_STOPPED"])
        self.assertNotIn("secret", json.dumps(value))
        self.assertNotIn("pid", value)
        self.assertNotIn("ram_used_bytes", value)

    def test_pair_probe_diagnostics_are_closed_whitelisted_not_exception_text_or_sql(self):
        sink = self.sink()
        value = sink.sanitize({'observation':{'pair_attempt_count':4, 'pair_retry_count':1,
            'pair_consecutive_failures':1, 'pair_last_success_elapsed_seconds':76.875,
            'pair_observation_age_seconds':15, 'pair_freshness_limit_seconds':45, 'pair_retry_interval_seconds':5,
            'observation_closed':True, 'last_failure':{'probe_kind':'pairs', 'source_frame':'read_pair_counts',
            'error_code':'LONG_PAIR_PROBE_BUSY', 'exception_type':'OperationalError',
            'sqlite_errorcode':773, 'sqlite_primary_code':5, 'deadline_triggered':False,
            'elapsed_seconds':91.875, 'message':'PRIVATE_TOKEN', 'sql':'SELECT private', 'path':'D:/private'}}})
        observation = value['observation']
        self.assertEqual(observation['last_failure']['sqlite_primary_code'], 5)
        self.assertEqual(observation['last_failure']['error_code'], 'LONG_PAIR_PROBE_BUSY')
        self.assertTrue(observation['observation_closed'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(value))
        self.assertNotIn('SELECT', json.dumps(value))
        self.assertNotIn('D:/private', json.dumps(value))
        untrusted = sink.sanitize({'observation':{'last_failure':{'probe_kind':['private'],
            'source_frame':'secret.py:42', 'exception_type':'SecretClass', 'error_code':'PASSWORD_SECRET'}}})
        self.assertEqual(untrusted['observation']['last_failure'], {'error_code':'UNCLASSIFIED_ERROR'})

    def test_explicit_stage_registry_and_snapshot_id_do_not_accept_paths(self):
        sink = self.sink(known_stages=["01-colmap", "23b-a-stage-openmvs", "../../secret"])
        snapshot_id = "snapshot-20261014T010000123456Z-" + "a" * 32
        self.assertEqual(sink.sanitize({"stage": "23b-a-stage-openmvs", "latest_snapshot_id": snapshot_id}),
                         {"stage": "23b-a-stage-openmvs", "latest_snapshot_id": snapshot_id})
        self.assertEqual(sink.sanitize({"stage": "../../secret", "latest_snapshot_id": "D:/jobs/secret"}), {})
        self.assertEqual(sink.sanitize({"stage": "unregistered-stage", "phase": "23b-a-stage-openmvs"}), {})

    def test_nested_payload_and_error_arrays_have_global_work_bound(self):
        sink = self.sink()
        leaf = {"errors": ["LOW_RAM_AUTO_STOPPED"] * 100000, "complete_pair_records": 25997}
        nested = leaf
        for _ in range(4):
            nested = {key: nested for key in ("latest", "resources", "metrics", "memory", "gpu", "disk")}
        began = time.monotonic()
        value = sink.sanitize(nested)
        self.assertLess(time.monotonic() - began, 0.1)
        self.assertLess(len(json.dumps(value)), 16000)

    def test_total_capacity_never_exceeds_fixed_maximum(self):
        with self.assertRaises(EvidenceTrip):
            self.sink(capacity_bytes=MAX_EVIDENCE_BYTES + 1)
        sink = self.sink(capacity_bytes=256 * 1024)
        sink.start()
        with sink._lock:
            sink._bytes_reserved = sink.capacity_bytes - ALL_TERMINAL_RESERVE_BYTES
        with self.assertRaises(EvidenceTrip) as caught:
            sink.append("event", {"event_code": "RUN_START"})
        self.assertEqual(caught.exception.code, "EVIDENCE_CAPACITY_EXCEEDED")
        initial = sink.finish_terminal({"status": "failed", "error_code": caught.exception.code})
        final = sink.write_forensic_summary({"status": "failed", "checkpoint_status": "skipped"})
        self.assertLess(initial.stat().st_size + final.stat().st_size, ALL_TERMINAL_RESERVE_BYTES)

    def test_running_low_c_disk_is_inclusive_and_sticky(self):
        sink = self.sink()
        sink.start()
        self.free = 2 * GIB
        first = self.wait_trip(sink, "EVIDENCE_LOW_DISK")
        self.free = 20 * GIB
        with self.assertRaises(EvidenceTrip) as caught:
            sink.check_health()
        self.assertIs(caught.exception, first)
        # Sticky running trip does not prevent C terminal once C itself is safe.
        path = sink.finish_terminal({"status": "failed", "error_code": "LOW_DISK_AUTO_STOPPED", "tree_stopped": True})
        self.assertEqual(json.loads(path.read_bytes())["summary"]["error_code"], "LOW_DISK_AUTO_STOPPED")

    def test_c_admission_requires_10_gib(self):
        self.free = 10 * GIB - 1
        with self.assertRaises(EvidenceTrip) as caught:
            self.sink()
        self.assertEqual(caught.exception.code, "EVIDENCE_ADMISSION_FAILED")
        self.free = 10 * GIB
        self.sink()

    def test_invalid_free_probe_is_sanitized(self):
        calls = 0
        def free(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 20 * GIB
            raise OSError("password private-file")
        sink = self.sink(free_reader=free)
        with self.assertRaises(EvidenceTrip) as caught:
            sink.start()
        self.assertEqual(caught.exception.code, "EVIDENCE_FREE_PROBE_FAILED")
        self.assertNotIn("password", str(caught.exception))

    def test_blocked_probe_cannot_block_health_or_startup(self):
        release = threading.Event()
        self.releases.append(release)
        calls = 0
        def free(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                release.wait()
            return 20 * GIB
        sink = self.sink(free_reader=free, intervals=(0.02, 0.2))
        began = time.monotonic()
        with self.assertRaises(EvidenceTrip) as caught:
            sink.start()
        self.assertEqual(caught.exception.code, "EVIDENCE_PROBE_STALE")
        self.assertLess(time.monotonic() - began, 0.7)
        began = time.monotonic()
        self.wait_trip(sink, "EVIDENCE_PROBE_STALE")
        self.assertLess(time.monotonic() - began, 0.05)

    def test_blocked_journal_writer_preserves_last_event_in_independent_terminal(self):
        release = threading.Event()
        self.releases.append(release)
        def write(fd, raw):
            release.wait()
            return os.write(fd, raw)
        sink = self.sink(write_bytes=write, intervals=(0.02, 0.2))
        sink.start()
        sink.append("event", {"event_code": "STAGE_START", "phase": "sfm"})
        self.wait_trip(sink, "EVIDENCE_PROBE_STALE")
        began = time.monotonic()
        terminal = sink.finish_terminal({"status": "failed", "tree_stopped": True})
        self.assertLess(time.monotonic() - began, 0.2)
        self.assertEqual(json.loads(terminal.read_bytes())["last_event"]["payload"]["event_code"], "STAGE_START")
        began = time.monotonic()
        sink.close()
        self.assertLess(time.monotonic() - began, 0.4)

    def test_queue_overflow_is_nonblocking_and_failclosed(self):
        release = threading.Event()
        self.releases.append(release)
        def write(fd, raw):
            release.wait()
            return os.write(fd, raw)
        sink = self.sink(write_bytes=write)
        sink.start()
        began = time.monotonic()
        with self.assertRaises(EvidenceTrip) as caught:
            for _ in range(150):
                sink.append("pairs", {"complete_pair_records": 25997})
        self.assertEqual(caught.exception.code, "EVIDENCE_QUEUE_FULL")
        self.assertLess(time.monotonic() - began, 0.15)

    def test_incomplete_write_and_fsync_failure_trip_without_raw_exception(self):
        sink = self.sink(write_bytes=lambda _, raw: len(raw) - 1)
        sink.start()
        sink.append("pairs", {"complete_pair_records": 1})
        self.wait_trip(sink, "EVIDENCE_WRITE_FAILED")
        self.assertNotIn("exception", json.dumps(sink.summary()))

    def test_fsync_failure_failsclosed_and_close_rejects_new_records(self):
        sink = self.sink()
        sink.start()
        with patch("backend.experiments.evidence.os.fsync", side_effect=OSError("password private-path")):
            sink.append("event", {"event_code": "RUN_START"})
            self.wait_trip(sink, "EVIDENCE_WRITE_FAILED")
        self.assertNotIn("password", json.dumps(sink.summary()))
        safe = self.sink()
        safe.start()
        safe.close()
        with self.assertRaises(EvidenceTrip) as caught:
            safe.append("pairs", {"complete_pair_records": 1})
        self.assertEqual(caught.exception.code, "EVIDENCE_NOT_STARTED")

    def test_existing_terminal_never_overwritten_and_repeat_is_rejected(self):
        sink = self.sink()
        (sink.root / "terminal.json").write_text("existing-test-data")
        with self.assertRaises(EvidenceTrip) as caught:
            sink.finish_terminal({"status": "failed"})
        self.assertEqual(caught.exception.code, "EVIDENCE_TERMINAL_FAILED")
        self.assertEqual((sink.root / "terminal.json").read_text(), "existing-test-data")
        with self.assertRaises(EvidenceTrip) as caught:
            sink.finish_terminal({"status": "failed"})
        self.assertEqual(caught.exception.code, "EVIDENCE_TERMINAL_EXISTS")

    def test_terminal_low_c_disk_and_blocked_probe_are_bounded(self):
        sink = self.sink()
        self.free = 2 * GIB
        with self.assertRaises(EvidenceTrip) as caught:
            sink.finish_terminal({"status": "failed"})
        self.assertEqual(caught.exception.code, "EVIDENCE_LOW_DISK")
        self.assertFalse((sink.root / "terminal.json").exists())
        release = threading.Event()
        self.releases.append(release)
        sink.free_reader = lambda _: (release.wait(), 20 * GIB)[1]
        began = time.monotonic()
        with self.assertRaises(EvidenceTrip) as caught:
            sink.write_forensic_summary({"status": "failed"}, timeout_seconds=0.05)
        self.assertEqual(caught.exception.code, "EVIDENCE_TERMINAL_TIMED_OUT")
        self.assertLess(time.monotonic() - began, 0.15)

    def test_exclusive_test_root_and_direct_constructor_gate(self):
        self.sink()
        with self.assertRaises(EvidenceTrip):
            EvidenceSink.create_for_test(self.parent / "run-1", free_reader=lambda _: 20 * GIB)
        with self.assertRaises(EvidenceTrip):
            EvidenceSink(self.parent, _factory_token=object(), free_reader=lambda _: 20 * GIB)

    def test_reparse_attribute_is_rejected(self):
        plain = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=plain):
            with self.assertRaises(EvidenceTrip) as caught:
                _ordinary_directory(self.parent)
        self.assertEqual(caught.exception.code, "EVIDENCE_PRIVATE_ROOT_INVALID")

    @unittest.skipUnless(os.name == "nt", "Windows-specific ACL invocation")
    def test_acl_script_verifies_exact_admin_system_owner_and_inheritance(self):
        with patch("backend.experiments.evidence.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            _private_acl(self.parent)
        import base64
        script = base64.b64decode(run.call_args.args[0][-1]).decode("utf-16-le")
        self.assertIn("S-1-5-18", script)
        self.assertIn("S-1-5-32-544", script)
        self.assertIn("AreAccessRulesProtected", script)
        self.assertIn("GetOwner", script)
        self.assertIn("ContainerInherit, ObjectInherit", script)
        self.assertIn("$r.Count-ne 2", script)
        self.assertEqual(script.count("Set-Acl"), 1)
        self.assertIn(str(self.parent), script)

    @unittest.skipUnless(os.name == "nt", "Windows-specific production gate")
    def test_production_factory_only_new_fixed_parent_child_and_no_parent_acl(self):
        with patch("backend.experiments.evidence.PRIVATE_PARENT", self.parent), \
             patch("backend.experiments.preflight.require_admin"), \
             patch("backend.experiments.evidence._free_bytes", return_value=20 * GIB), \
             patch("backend.experiments.evidence._private_acl") as acl:
            root = create_private_directory("new-scene-1")
            self.assertEqual(root, self.parent / "private-evidence-new-scene-1")
            acl.assert_called_once_with(root)
            for name in ("../job", "D:\\old-job", "", "a/b", "Uppercase"):
                with self.assertRaises(EvidenceTrip):
                    create_private_directory(name)
            with self.assertRaises(EvidenceTrip):
                create_private_directory("new-scene-1")

    @unittest.skipUnless(os.name == "nt", "Windows-specific ACL failure")
    def test_acl_failure_never_exposes_powershell_output(self):
        with patch("backend.experiments.evidence.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="password", stderr="private-file")):
            with self.assertRaises(EvidenceTrip) as caught:
                _private_acl(self.parent)
        self.assertEqual(str(caught.exception), "EVIDENCE_PRIVATE_ACL_FAILED")


if __name__ == "__main__":
    unittest.main()
