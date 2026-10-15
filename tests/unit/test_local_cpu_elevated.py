from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_local_cpu_elevated as wrapper


class FakeAPI:
    def __init__(self, *, admin=True, reader=True, exists=False):
        self.admin, self.reader, self.exists = admin, reader, exists
        self.calls, self.records = [], []
    def is_admin(self):
        self.calls.append("admin")
        return self.admin
    def create_public(self):
        self.calls.append("create_public")
        if self.exists:
            raise wrapper.ElevatedCPUError("LOCAL_CPU_PUBLIC_TARGET_EXISTS")
    def write_status(self, value):
        self.records.append(dict(value))
    def keep_awake(self, enabled):
        self.calls.append(("awake", enabled))
    def create_ready_event(self):
        self.calls.append("event_create")
        return wrapper.READY_EVENT_PREFIX + "f" * 32, 7
    def await_reader(self, handle):
        self.calls.append(("reader_wait", handle))
        return self.reader
    def close_event(self, handle):
        self.calls.append(("event_close", handle))


class FakePublisher:
    def __init__(self, api):
        self.api, self.closed = api, False
    def publish(self, fields):
        self.api.write_status(fields)
    def start(self):
        self.api.calls.append("publisher_start")
    def close(self):
        self.api.calls.append("publisher_close")
        self.closed = True
        return True


class ElevatedCPUTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.initialize_calls = []
        self.cli_calls = []
        self.proof_calls = []
        self.initializer = SimpleNamespace(perform=self.initialize)
        self.cli = SimpleNamespace(main=self.compute)
        self.output = io.StringIO()

    def initialize(self, *, operation):
        self.initialize_calls.append(operation)
        return {"status": "initialized", "administrator": True, "acl_verified": True, "parent_ready": True}

    def compute(self, args):
        self.cli_calls.append(args)
        print('secret raw runtime stdout with token=never-public')
        return 0

    def proof(self):
        self.proof_calls.append(True)
        return {"status": "succeeded", "phase": "completed", "process_tree_stopped": True}

    def run_wrapper(self, **changes):
        args = {"_api": self.api, "_initializer": self.initializer, "_loader": lambda: self.cli,
                "_publisher_factory": FakePublisher, "_final_verifier": self.proof}
        args.update(changes)
        with redirect_stdout(self.output):
            result = wrapper.main(**args)
        return result, json.loads(self.output.getvalue().splitlines()[-1])

    def test_success_requires_reader_and_private_result_before_terminal(self):
        result, status = self.run_wrapper()
        self.assertEqual(result, 0)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(self.initialize_calls, ["initialize"])
        self.assertEqual(len(self.cli_calls), 1)
        self.assertIn("--execute-cpu", self.cli_calls[0])
        self.assertIn(wrapper.TOOL_SHA256, self.cli_calls[0])
        self.assertEqual(self.proof_calls, [True])
        self.assertLess(self.api.calls.index(("reader_wait", 7)), self.api.calls.index("publisher_start"))
        self.assertEqual(self.api.records[0]["phase"], "ready_waiting_reader")
        self.assertEqual(self.api.records[0]["control_nonce"], "f" * 32)
        self.assertEqual(self.api.records[-1]["phase"], "completed")
        self.assertEqual(self.api.calls[-1], ("awake", False))
        self.assertNotIn("never-public", self.output.getvalue())

    def test_non_admin_does_not_initialize_or_create_or_compute(self):
        self.api.admin = False
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_ADMIN_REQUIRED")
        self.assertEqual(self.initialize_calls, [])
        self.assertEqual(self.cli_calls, [])
        self.assertNotIn("create_public", self.api.calls)

    def test_untrusted_caller_sid_rejected_before_native_calls(self):
        result, status = self.run_wrapper(caller_sid="S-1-5-18")
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_CALLER_SID_MISMATCH")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.cli_calls, [])

    def test_existing_public_directory_rejected_without_relaunch(self):
        self.api.exists = True
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_PUBLIC_TARGET_EXISTS")
        self.assertEqual(self.cli_calls, [])
        self.assertNotIn("event_create", self.api.calls)

    def test_bad_initializer_proof_never_creates_status_or_runs_cpu(self):
        bad = SimpleNamespace(perform=lambda **_: {"status": "initialized", "administrator": True,
                                                   "acl_verified": False, "parent_ready": True})
        result, status = self.run_wrapper(_initializer=bad)
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_EVIDENCE_PARENT_NOT_READY")
        self.assertEqual(self.cli_calls, [])
        self.assertNotIn("create_public", self.api.calls)

    def test_reader_timeout_and_unreadable_channel_do_not_run_cpu(self):
        self.api.reader = False
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["error_code"], "LOCAL_CPU_PUBLIC_READER_UNCONFIRMED")
        self.assertFalse(status["reconstruction_started"])
        self.assertEqual(self.cli_calls, [])
        self.assertIn(("event_close", 7), self.api.calls)
        self.assertEqual(self.api.calls[-1], ("awake", False))

    def test_failed_ready_publish_never_runs_cpu(self):
        class BrokenPublisher(FakePublisher):
            def publish(self, fields):
                raise PermissionError("private path or password not public")
        result, status = self.run_wrapper(_publisher_factory=BrokenPublisher)
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_WRAPPER_FAILED")
        self.assertEqual(self.cli_calls, [])
        self.assertNotIn("password", self.output.getvalue())
        self.assertIn(("event_close", 7), self.api.calls)

    def test_invalid_event_name_never_runs_cpu(self):
        self.api.create_ready_event = lambda: ("Global\\unrelated", 7)
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_READY_EVENT_NAME_INVALID")
        self.assertEqual(self.cli_calls, [])

    def test_zero_cli_without_private_terminal_proof_is_failure(self):
        def missing_proof():
            raise wrapper.ElevatedCPUError("LOCAL_CPU_FINAL_PROOF_INCOMPLETE")
        result, status = self.run_wrapper(_final_verifier=missing_proof)
        self.assertEqual(result, 1)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error_code"], "LOCAL_CPU_FINAL_PROOF_INCOMPLETE")
        self.assertEqual(self.api.records[-1]["status"], "failed")

    def test_unconfirmed_publisher_close_is_not_waited_on_or_published_again(self):
        class UnconfirmedPublisher(FakePublisher):
            def close(self):
                self.api.calls.append("publisher_close")
                return False
        result, status = self.run_wrapper(_publisher_factory=UnconfirmedPublisher)
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_PUBLIC_PUBLISHER_NOT_STOPPED")
        self.assertTrue(status["public_failure_unconfirmed"])
        self.assertEqual(self.api.calls.count("publisher_close"), 1)
        self.assertEqual(self.proof_calls, [])
        self.assertEqual([record["phase"] for record in self.api.records],
                         ["ready_waiting_reader", "preflight"])
        self.assertEqual(self.api.calls[-1], ("awake", False))

    def test_nonzero_cli_never_promotes_raw_stdout_to_success(self):
        self.cli.main = lambda args: (print('{"status":"succeeded","secret":"token"}') or 1)
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_TOOL_RUN_FAILED")
        self.assertEqual(self.proof_calls, [])
        self.assertNotIn("token", self.output.getvalue())

    def test_real_timeout_or_ram_failure_remains_public_without_raw_output(self):
        self.cli.main = lambda args: (print(json.dumps({"mode": "cpu-sfm", "status": "timed_out",
            "error_code": "LONG_BUDGET_EXHAUSTED", "return_code": 1,
            "process_tree_stopped": True, "env": {"TOKEN": "secret"}})) or 1)
        with patch.object(wrapper, "_read_document", side_effect=FileNotFoundError):
            result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["status"], "timed_out")
        self.assertEqual(status["error_code"], "LONG_BUDGET_EXHAUSTED")
        self.assertTrue(status["execution_entry_invoked"])
        self.assertTrue(status["reconstruction_started"])
        self.assertEqual(status["wrapper_error_code"], "LOCAL_CPU_TOOL_RUN_FAILED")
        self.assertEqual(self.api.records[-1]["status"], "timed_out")
        self.assertNotIn("secret", self.output.getvalue())

    def test_preflight_failure_is_not_claimed_as_real_reconstruction_started(self):
        self.cli.main = lambda args: (print(json.dumps({"mode": "cpu-sfm", "status": "failed",
            "error_code": "LOW_RAM_AUTO_STOPPED"})) or 1)
        with patch.object(wrapper, "_read_document", side_effect=FileNotFoundError):
            result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertTrue(status["execution_entry_invoked"])
        self.assertIsNone(status["reconstruction_started"])
        self.assertEqual(status["error_code"], "LOW_RAM_AUTO_STOPPED")

    def test_failed_valid_final_snapshot_replaces_last_sample_without_claiming_success(self):
        result = {"mode": "cpu-sfm", "status": "failed", "error_code": "LOW_RAM_AUTO_STOPPED",
            "complete_pair_records": 27039, "process_tree_stopped": True, "snapshot_worker_stopped": True,
            "terminal_evidence_confirmed": True, "source_checkpoint_unchanged": True,
            "observation_relay_stopped": True, "final_snapshot_status": "valid",
            "final_snapshot": {"validated_complete_pair_records": 27160, "database_sha256": "e" * 64,
                "sqlite_backup_completed": True, "sqlite_integrity_and_mapping_validation": True,
                "database_hash_checked_before_and_after_validation": True}}
        with patch.object(wrapper, "_read_document", return_value=(result, b"{}")):
            selected = wrapper._failure_fields(SimpleNamespace(tail=""), self.cli)
        self.assertEqual(selected["status"], "failed")
        self.assertEqual(selected["complete_pair_records"], 27160)
        self.assertEqual(selected["complete_pairs"], 27160)
        self.assertEqual(selected["latest_snapshot_sha256"], "e" * 64)
        self.assertEqual(selected["database_sha256"], "e" * 64)
        for key in ("process_tree_stopped", "snapshot_worker_stopped", "terminal_evidence_confirmed",
                    "source_checkpoint_unchanged", "observation_relay_stopped"):
            invalid = {**result, key: False}
            with self.subTest(key=key), patch.object(wrapper, "_read_document", return_value=(invalid, b"{}")):
                selected = wrapper._failure_fields(SimpleNamespace(tail=""), self.cli)
            self.assertEqual(selected["complete_pair_records"], 27039)
            self.assertNotIn("database_sha256", selected)
        invalid = {**result, "final_snapshot_status": "failed"}
        with patch.object(wrapper, "_read_document", return_value=(invalid, b"{}")):
            selected = wrapper._failure_fields(SimpleNamespace(tail=""), self.cli)
        self.assertEqual(selected["complete_pair_records"], 27039)
        self.assertNotIn("latest_snapshot_sha256", selected)

    def test_cleanup_failure_is_public_not_only_stdout(self):
        original = self.api.keep_awake
        def awake(enabled):
            original(enabled)
            if not enabled:
                raise wrapper.ElevatedCPUError("LOCAL_CPU_SLEEP_GUARD_FAILED")
        self.api.keep_awake = awake
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error_code"], "LOCAL_CPU_CLEANUP_FAILED")
        self.assertEqual(self.api.records[-1]["error_code"], "LOCAL_CPU_CLEANUP_FAILED")

    def test_public_replace_sharing_retry_is_bounded_and_does_not_mask_other_errors(self):
        temporary = wrapper.PUBLIC_ROOT / ("status-" + "a" * 32 + ".tmp")
        sharing = PermissionError("temporary reader sharing")
        sharing.winerror = 32
        with patch.object(wrapper.os, "replace", side_effect=[sharing, sharing, None]) as replace, \
                patch.object(wrapper.time, "sleep") as sleep:
            wrapper._replace_status_file(temporary)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        with patch.object(wrapper.os, "replace", side_effect=sharing) as replace, \
                patch.object(wrapper.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                wrapper._replace_status_file(temporary)
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        with patch.object(wrapper.os, "replace", side_effect=PermissionError("not a sharing lock")) as replace:
            with self.assertRaises(PermissionError):
                wrapper._replace_status_file(temporary)
        self.assertEqual(replace.call_count, 1)

    def test_awake_state_is_restored_on_compute_exception(self):
        def failed(args):
            raise RuntimeError("private raw environment token")
        self.cli.main = failed
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_WRAPPER_FAILED")
        self.assertEqual(self.api.calls[-1], ("awake", False))
        self.assertNotIn("token", self.output.getvalue())

    def test_unknown_error_code_is_not_public(self):
        class Unknown(Exception):
            code = "SECRET_PASSWORD_AS_UPPERCASE"
        self.cli.main = lambda _: (_ for _ in ()).throw(Unknown())
        result, status = self.run_wrapper()
        self.assertEqual(result, 1)
        self.assertEqual(status["error_code"], "LOCAL_CPU_WRAPPER_FAILED")
        self.assertNotIn("SECRET_PASSWORD", self.output.getvalue())

    def test_sanitizer_drops_paths_logs_env_invalid_values_and_nested_unknowns(self):
        selected = wrapper.sanitize({"phase": "matching", "status": "running", "stage": "01-colmap",
            "complete_pair_records": 26842, "latest_snapshot_sha256": "a" * 64,
            "argv": ["secret"], "env": {"TOKEN": "secret"}, "raw_log": "secret",
            "path": "C:/private/secret", "resources": {"cpu": {"utilization_percent": 50.2,
            "private_note": "secret"}, "memory": {"used_bytes": 10}}, "peaks": {"ram_used_bytes": 100},
            "error_code": "SECRET", "duration_seconds": float("nan")})
        self.assertEqual(selected["complete_pair_records"], 26842)
        self.assertEqual(selected["resources"]["cpu"], {"utilization_percent": 50.2})
        self.assertNotIn("secret", json.dumps(selected).lower())
        self.assertEqual(wrapper.sanitize({"phase": [], "status": {}, "stage": [],
            "complete_pair_records": True, "complete_pairs": 44851}), {})

    def test_read_only_observer_diagnostics_are_sanitized_without_text_or_sql(self):
        selected = wrapper.sanitize({"observation": {"pair_attempt_count": 5, "pair_retry_count": 2,
            "pair_consecutive_failures": 1, "pair_last_success_elapsed_seconds": 7.5,
            "pair_observation_age_seconds": 5.0, "pair_freshness_limit_seconds": 45,
            "pair_retry_interval_seconds": 5, "observation_closed": True,
            "last_failure": {"probe_kind": "pairs", "source_frame": "read_pair_counts",
                "exception_type": "OperationalError", "error_code": "LONG_PAIR_PROBE_BUSY",
                "sqlite_errorcode": 5, "sqlite_primary_code": 5,
                "deadline_triggered": False, "elapsed_seconds": 10.0, "sql": "SELECT secret",
                "message": "private source path", "env": {"TOKEN": "secret"}}}})
        failure = selected["observation"]["last_failure"]
        self.assertEqual(failure["sqlite_primary_code"], 5)
        self.assertEqual(failure["exception_type"], "OperationalError")
        self.assertEqual(failure["error_code"], "LONG_PAIR_PROBE_BUSY")
        self.assertEqual(selected["observation"]["pair_attempt_count"], 5)
        self.assertNotIn("secret", json.dumps(selected).lower())
        self.assertNotIn("private source", json.dumps(selected))
        self.assertEqual(wrapper.sanitize({"last_failure": {"error_code": "SECRET_PASSWORD",
            "exception_type": "secret exception text", "sqlite_primary_code": None}}),
            {"last_failure": {"sqlite_primary_code": None}})

    def test_fixed_journal_reader_is_tail_and_row_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "journal.jsonl"
            rows = [{"kind": "pairs", "payload": {"complete_pair_records": index,
                "phase": "matching", "raw_log": "secret"}} for index in range(300)]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            with patch.object(wrapper, "PRIVATE_JOURNAL", path):
                selected = wrapper.read_public_observation()
            self.assertEqual(selected["complete_pair_records"], 299)
            self.assertEqual(selected["phase"], "matching")
            self.assertNotIn("raw_log", selected)

    def test_real_result_reader_requires_all_proofs_and_immutable_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "reports").mkdir()
            profile = {"target_experiment": {"scene": wrapper.SCENE, "root": wrapper.EXPERIMENT_ROOT.as_posix()},
                       "source_checkpoint": {"snapshot_database_sha256": "e" * 64}}
            profile_raw = (json.dumps(profile) + "\n").encode()
            profile_path = root / "profile.json"
            profile_path.write_bytes(profile_raw)
            (root / "reports/effective-cpu-profile.json").write_bytes(profile_raw)
            plan = {"mode": "cpu-sfm", "tool_manifest_sha256": wrapper.TOOL_SHA256,
                "pipeline_commit": "e517a161b4c0895f8be4258fb339272e4f99e1d6",
                "source_database_sha256": "e" * 64,
                "gpu_sampling_enabled": False, "queue_accessed": False,
                "profile_sha256": hashlib.sha256(profile_raw).hexdigest()}
            (root / "reports/executed-cpu-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            result = {"mode": "cpu-sfm", "status": "succeeded", "source_job_modified": False,
                "platform_job_resume": False, "gpu_stage_started": False, "process_tree_stopped": True,
                "snapshot_worker_stopped": True, "terminal_evidence_confirmed": True,
                "source_checkpoint_unchanged": True, "observation_relay_stopped": True,
                "final_snapshot_status": "valid",
                "sfm_gate": {"database_images": 300, "registered_images": 300}}
            result_path = root / "reports/cpu-result.json"
            with patch.object(wrapper, "EXPERIMENT_ROOT", root), patch.object(wrapper, "RESULT_PATH", result_path), \
                    patch.object(wrapper, "PROFILE", profile_path), \
                    patch.object(wrapper, "PROFILE_SHA256", hashlib.sha256(profile_raw).hexdigest()):
                result_path.write_text(json.dumps(result), encoding="utf-8")
                self.assertEqual(wrapper.verify_final_result()["status"], "succeeded")
                for proof in ("process_tree_stopped", "snapshot_worker_stopped", "terminal_evidence_confirmed",
                              "source_checkpoint_unchanged", "observation_relay_stopped"):
                    bad = {**result, proof: False}
                    result_path.write_text(json.dumps(bad), encoding="utf-8")
                    with self.subTest(proof=proof), self.assertRaisesRegex(wrapper.ElevatedCPUError, "FINAL_PROOF"):
                        wrapper.verify_final_result()
                result_path.write_text(json.dumps(result), encoding="utf-8")
                plan["source_database_sha256"] = "f" * 64
                (root / "reports/executed-cpu-plan.json").write_text(json.dumps(plan), encoding="utf-8")
                with self.assertRaisesRegex(wrapper.ElevatedCPUError, "FINAL_IDENTITY"):
                    wrapper.verify_final_result()
                profile_path.write_bytes(profile_raw + b" ")
                with self.assertRaisesRegex(wrapper.ElevatedCPUError, "PROFILE_CHANGED"):
                    wrapper.verify_final_result()


if __name__ == "__main__":
    unittest.main()
