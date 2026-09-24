from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base
from backend.db.queue import JobQueue
from backend.db.state_machine import JobStatus
from backend.re3d_adapter.contracts import load_json_contract
from backend.worker import QueuedRealWorker
from tests.unit.test_real_adapter import create_fake_real_re3d, make_real_request
from tests.unit.test_worker_simulation import create_task, read_events


def create_queued_real_job(root: Path, *, slow: bool = False):
    layout, request = create_task(root)
    request = make_real_request(layout.request_path, request)
    engine = create_engine(f"sqlite:///{(root / 'queue.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    queue = JobQueue(sessions)
    queue.ensure_resource("gpu:test-real")
    queue.enqueue(
        job_id=uuid.UUID(request["job_id"]),
        user_id=uuid.UUID(request["requested_by"]["user_id"]),
        execution_mode="real",
        pipeline_tag=request["pipeline"]["tag"],
        pipeline_commit=request["pipeline"]["commit"],
        config_sha256=request["pipeline"]["config_sha256"],
        input_manifest_sha256=request["input"]["manifest_sha256"],
        idempotency_key=request["idempotency_key"],
    )
    fake_re3d = root / "Re3D"
    create_fake_real_re3d(fake_re3d, slow=slow)
    return layout, request, engine, queue, fake_re3d


class QueuedRealWorkerTests(unittest.TestCase):
    def test_real_worker_closes_queue_result_evaluation_and_events_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout, request, engine, queue, fake_re3d = create_queued_real_job(
                root
            )
            try:
                outcome = QueuedRealWorker(
                    queue,
                    data_root=root / "data",
                    re3d_root=fake_re3d,
                    driver_python=Path(sys.executable),
                    worker_id="real-worker-test",
                    resource_key="gpu:test-real",
                    lease_seconds=5,
                    heartbeat_seconds=1,
                    process_poll_seconds=0.01,
                    installation_verifier=lambda _root, _pipeline: None,
                ).run_once()

                self.assertTrue(outcome.claimed)
                self.assertEqual(outcome.status, JobStatus.SUCCEEDED.value)
                snapshot = queue.get_job(uuid.UUID(request["job_id"]))
                self.assertEqual(snapshot["status"], JobStatus.SUCCEEDED)
                self.assertEqual(snapshot["progress"], 100)
                self.assertEqual(
                    load_json_contract(
                        layout.result_path,
                        "pipeline-result",
                    )["execution_mode"],
                    "real",
                )
                self.assertEqual(
                    load_json_contract(
                        layout.evaluation_path,
                        "evaluation",
                    )["overall"]["status"],
                    "pass",
                )
                events = read_events(layout.events_path)
                self.assertEqual(events[0]["type"], "job_started")
                self.assertEqual(events[-1]["type"], "job_succeeded")
                self.assertTrue(
                    any(event["type"] == "stage_started" for event in events)
                )
            finally:
                engine.dispose()

    def test_real_worker_cancels_running_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout, request, engine, queue, fake_re3d = create_queued_real_job(
                root,
                slow=True,
            )
            job_id = uuid.UUID(request["job_id"])
            outcomes = []
            failures: list[BaseException] = []
            worker = QueuedRealWorker(
                queue,
                data_root=root / "data",
                re3d_root=fake_re3d,
                driver_python=Path(sys.executable),
                worker_id="real-worker-cancel-test",
                resource_key="gpu:test-real",
                lease_seconds=5,
                heartbeat_seconds=0.1,
                process_poll_seconds=0.02,
                installation_verifier=lambda _root, _pipeline: None,
            )

            def run_worker() -> None:
                try:
                    outcomes.append(worker.run_once())
                except BaseException as exc:  # pragma: no cover - diagnostics
                    failures.append(exc)

            thread = threading.Thread(target=run_worker)
            try:
                thread.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if queue.get_job(job_id)["status"] in {
                        JobStatus.SFM,
                        JobStatus.DENSE_RECONSTRUCTION,
                    }:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("real worker did not start before cancellation")

                queue.request_cancel(
                    job_id,
                    user_id=uuid.UUID(request["requested_by"]["user_id"]),
                )
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0].status, JobStatus.CANCELLED.value)
                self.assertEqual(queue.get_job(job_id)["status"], JobStatus.CANCELLED)
                self.assertFalse(layout.result_path.exists())
                self.assertEqual(read_events(layout.events_path)[-1]["type"], "job_cancelled")
            finally:
                thread.join(timeout=1)
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
