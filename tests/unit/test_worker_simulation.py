from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from apps.worker.main import main as worker_main
from backend.evaluation import SimulationEvaluator
from backend.re3d_adapter.contracts import load_json_contract, validate_contract
from backend.re3d_adapter.errors import (
    ContractValidationError,
    IntegrityError,
    PathBoundaryError,
    SimulatedCrash,
)
from backend.re3d_adapter.events import EventJournal
from backend.re3d_adapter.io import atomic_write_json, sha256_file
from backend.re3d_adapter.paths import TaskLayout
from backend.re3d_adapter.simulation import SimulationRunner, minimal_glb


ROOT = Path(__file__).resolve().parents[2]
REQUEST_EXAMPLE = (
    ROOT
    / "packages"
    / "contracts"
    / "examples"
    / "v1"
    / "valid"
    / "pipeline-request.json"
)


def read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def create_task(base: Path, *, image_count: int = 3) -> tuple[TaskLayout, dict]:
    data_root = base / "data"
    job_id = str(uuid.uuid4())
    layout = TaskLayout.from_data_root(data_root, job_id, create=True)
    layout.ensure_worker_directories()
    images_path = layout.resolve("input/images")
    images_path.mkdir(parents=True)
    image_records = []
    total_bytes = 0
    for index in range(image_count):
        image_path = images_path / f"{index:06d}.jpg"
        payload = f"simulated-image-{index}".encode("ascii")
        image_path.write_bytes(payload)
        total_bytes += len(payload)
        image_records.append(
            {
                "name": image_path.name,
                "sha256": sha256_file(image_path),
                "size_bytes": len(payload),
            }
        )
    manifest_path = layout.resolve("input/input-manifest.json")
    atomic_write_json(manifest_path, {"images": image_records})

    request = copy.deepcopy(json.loads(REQUEST_EXAMPLE.read_text(encoding="utf-8")))
    request["job_id"] = job_id
    request["request_id"] = str(uuid.uuid4())
    request["requested_by"]["user_id"] = str(uuid.uuid4())
    request["input"]["image_count"] = image_count
    request["input"]["total_bytes"] = total_bytes
    request["input"]["manifest_sha256"] = sha256_file(manifest_path)
    validate_contract("pipeline-request", request)
    atomic_write_json(layout.request_path, request)
    return layout, request


class TaskLayoutTests(unittest.TestCase):
    def test_resolves_only_paths_inside_one_uuid_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            resolved = layout.resolve("output/A-v4/mesh.glb")
            self.assertEqual(resolved.parent.name, "A-v4")
            self.assertTrue(resolved.is_relative_to(layout.root))

            unsafe_paths = (
                "../other-job/file",
                "D:/private/file",
                "/root/file",
                "\\\\server\\share",
            )
            for unsafe in unsafe_paths:
                with self.subTest(path=unsafe):
                    with self.assertRaises(PathBoundaryError):
                        layout.resolve(unsafe)

    def test_rejects_noncanonical_or_missing_job_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            with self.assertRaises(PathBoundaryError):
                TaskLayout.from_data_root(data_root, "not-a-uuid")
            with self.assertRaises(PathBoundaryError):
                TaskLayout.from_data_root(data_root, str(uuid.uuid4()))


class EventJournalTests(unittest.TestCase):
    def test_reloads_contiguous_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, request = create_task(Path(temporary))
            journal = EventJournal(
                layout.events_path,
                job_id=request["job_id"],
                attempt=1,
                worker_id="test-worker",
            )
            journal.append(
                "job_started",
                message_code="JOB_STARTED",
                safe_message="started",
            )
            reloaded = EventJournal(
                layout.events_path,
                job_id=request["job_id"],
                attempt=1,
                worker_id="test-worker",
            )
            self.assertEqual(reloaded.next_sequence, 2)

    def test_rejects_incomplete_final_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, request = create_task(Path(temporary))
            layout.events_path.write_text('{"incomplete":', encoding="utf-8")
            with self.assertRaises(IntegrityError):
                EventJournal(
                    layout.events_path,
                    job_id=request["job_id"],
                    attempt=1,
                    worker_id="test-worker",
                )


class SimulationRunnerTests(unittest.TestCase):
    def test_minimal_glb_has_valid_header_and_length(self) -> None:
        payload = minimal_glb()
        self.assertEqual(payload[:4], b"glTF")
        self.assertEqual(int.from_bytes(payload[4:8], "little"), 2)
        self.assertEqual(int.from_bytes(payload[8:12], "little"), len(payload))

    def test_simulator_rejects_real_execution_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, request = create_task(Path(temporary))
            request["execution_mode"] = "real"
            atomic_write_json(layout.request_path, request)
            with self.assertRaises(ContractValidationError):
                SimulationRunner(layout, worker_id="test-worker").run()

    def test_simulation_creates_contract_valid_result_and_three_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, request = create_task(Path(temporary))
            outcome = SimulationRunner(layout, worker_id="test-worker").run()

            self.assertFalse(outcome.reused)
            self.assertEqual(outcome.result["execution_mode"], "simulated")
            self.assertEqual(outcome.result["job_id"], request["job_id"])
            load_json_contract(outcome.result_path, "pipeline-result")

            for branch in ("A-v4", "B-v2", "C"):
                artifact = layout.resolve(f"output/{branch}/mesh.glb")
                self.assertTrue(artifact.is_file())
                self.assertEqual(artifact.read_bytes()[:4], b"glTF")

            events = read_events(layout.events_path)
            self.assertEqual(events[-1]["type"], "job_succeeded")
            self.assertEqual(
                [event["sequence"] for event in events],
                list(range(1, len(events) + 1)),
            )
            for event in events:
                validate_contract("pipeline-event", event)

    def test_second_run_reuses_result_without_appending_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            runner = SimulationRunner(layout, worker_id="test-worker")
            first = runner.run()
            event_bytes = layout.events_path.read_bytes()
            second = runner.run()

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(layout.events_path.read_bytes(), event_bytes)
            self.assertEqual(first.result, second.result)

    def test_recovers_after_interruption_without_duplicate_artifact_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            runner = SimulationRunner(layout, worker_id="test-worker")
            with self.assertRaises(SimulatedCrash):
                runner.run(crash_after_branch="A-v4")
            self.assertFalse(layout.result_path.exists())

            outcome = runner.run()
            self.assertEqual(outcome.result["status"], "succeeded")
            events = read_events(layout.events_path)
            a_artifacts = [
                event
                for event in events
                if event["type"] == "artifact_created"
                and event.get("branch") == "A-v4"
            ]
            self.assertEqual(len(a_artifacts), 1)
            self.assertTrue(any(event.get("message_code") == "WORKER_RESUMED" for event in events))

    def test_rejects_input_changed_after_request_was_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            first_image = layout.resolve("input/images/000000.jpg")
            first_image.write_bytes(first_image.read_bytes() + b"changed")
            with self.assertRaises(IntegrityError):
                SimulationRunner(layout, worker_id="test-worker").run()

    def test_rejects_equal_size_input_content_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            first_image = layout.resolve("input/images/000000.jpg")
            original = first_image.read_bytes()
            first_image.write_bytes(b"x" * len(original))
            with self.assertRaises(IntegrityError):
                SimulationRunner(layout, worker_id="test-worker").run()

    def test_rejects_manifest_names_that_do_not_match_stored_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, request = create_task(Path(temporary))
            manifest_path = layout.resolve("input/input-manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["images"][0]["name"] = "different.jpg"
            atomic_write_json(manifest_path, manifest)
            request["input"]["manifest_sha256"] = sha256_file(manifest_path)
            atomic_write_json(layout.request_path, request)
            with self.assertRaises(IntegrityError):
                SimulationRunner(layout, worker_id="test-worker").run()

    def test_reused_result_rejects_tampered_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            runner = SimulationRunner(layout, worker_id="test-worker")
            runner.run()
            layout.resolve("output/A-v4/mesh.glb").write_bytes(b"tampered")
            with self.assertRaises(IntegrityError):
                runner.run()

    def test_worker_cli_runs_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, request = create_task(Path(temporary))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = worker_main(
                    [
                        "simulate",
                        "--job-id",
                        request["job_id"],
                        "--data-root",
                        str(layout.data_root),
                        "--worker-id",
                        "cli-test-worker",
                    ]
                )
            self.assertEqual(exit_code, 0)
            response = json.loads(stdout.getvalue())
            self.assertEqual(response["job_id"], request["job_id"])
            self.assertEqual(response["status"], "succeeded")
            self.assertEqual(response["execution_mode"], "simulated")


class SimulationEvaluatorTests(unittest.TestCase):
    def test_writes_contract_valid_report_without_fake_quality_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, request = create_task(Path(temporary))
            SimulationRunner(layout, worker_id="test-worker").run()

            first = SimulationEvaluator(layout).run()
            second = SimulationEvaluator(layout).run()

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(first.report["job_id"], request["job_id"])
            self.assertEqual(first.report["overall"]["status"], "not_available")
            self.assertIsNone(first.report["overall"]["score"])
            load_json_contract(first.report_path, "evaluation")

    def test_rejects_evaluation_reuse_after_result_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            SimulationRunner(layout, worker_id="test-worker").run()
            evaluator = SimulationEvaluator(layout)
            evaluator.run()

            result = json.loads(layout.result_path.read_text(encoding="utf-8"))
            result["duration_seconds"] += 1
            atomic_write_json(layout.result_path, result)

            with self.assertRaises(IntegrityError):
                evaluator.run()

    def test_rejects_tampered_artifact_before_reporting_integrity_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_task(Path(temporary))
            SimulationRunner(layout, worker_id="test-worker").run()
            layout.resolve("output/A-v4/mesh.glb").write_bytes(b"tampered")

            with self.assertRaises(IntegrityError):
                SimulationEvaluator(layout).run()


if __name__ == "__main__":
    unittest.main()
