from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from backend.evaluation import SimulationEvaluator
from backend.jobs.artifacts import (
    ArtifactNotFoundError,
    ArtifactReader,
    ArtifactUnavailableError,
)
from backend.re3d_adapter.contracts import load_json_contract
from backend.re3d_adapter.io import atomic_write_json, sha256_file
from backend.re3d_adapter.simulation import SimulationRunner
from tests.unit.test_worker_simulation import create_task


class ArtifactReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.layout, self.request = create_task(self.base)
        SimulationRunner(self.layout, worker_id="artifact-test-worker").run()
        SimulationEvaluator(self.layout).run()
        self.user_id = uuid.UUID(self.request["requested_by"]["user_id"])
        self.queue = Mock()
        self.queue.get_job.return_value = {
            "id": uuid.UUID(self.request["job_id"]),
            "user_id": self.user_id,
            "status": "succeeded",
            "execution_mode": "simulated",
        }
        self.reader = ArtifactReader(
            self.queue,
            data_root=self.layout.data_root,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def read_a_glb(self):
        return self.reader.read(
            uuid.UUID(self.request["job_id"]),
            user_id=self.user_id,
            branch="A-v4",
            kind="glb",
        )

    def test_resolves_manifest_artifact_and_rechecks_integrity(self) -> None:
        artifact = self.read_a_glb()

        self.assertEqual(artifact.path.read_bytes()[:4], b"glTF")
        self.assertEqual(artifact.content_type, "model/gltf-binary")
        self.assertEqual(artifact.size_bytes, artifact.path.stat().st_size)
        self.assertEqual(artifact.sha256, sha256_file(artifact.path))
        self.assertEqual(
            artifact.filename,
            f"re3d-{self.request['job_id']}-A-v4-mesh.glb",
        )

        artifact.path.write_bytes(b"tampered")
        with self.assertRaises(ArtifactUnavailableError):
            self.read_a_glb()

    def test_rejects_absent_kind_and_contract_declared_non_output_file(self) -> None:
        with self.assertRaises(ArtifactNotFoundError):
            self.reader.read(
                uuid.UUID(self.request["job_id"]),
                user_id=self.user_id,
                branch="A-v4",
                kind="obj",
            )

        input_path = self.layout.resolve("input/images/000000.jpg")
        result = load_json_contract(self.layout.result_path, "pipeline-result")
        result["branches"]["A-v4"]["artifacts"][0].update(
            {
                "path": "input/images/000000.jpg",
                "size_bytes": input_path.stat().st_size,
                "sha256": sha256_file(input_path),
            }
        )
        atomic_write_json(self.layout.result_path, result)
        evaluation = load_json_contract(self.layout.evaluation_path, "evaluation")
        evaluation["source_result_sha256"] = sha256_file(self.layout.result_path)
        atomic_write_json(self.layout.evaluation_path, evaluation)

        with self.assertRaises(ArtifactUnavailableError):
            self.read_a_glb()


if __name__ == "__main__":
    unittest.main()
