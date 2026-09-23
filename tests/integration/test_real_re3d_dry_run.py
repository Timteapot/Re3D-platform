from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.re3d_adapter.io import atomic_write_json, sha256_file
from backend.re3d_adapter.paths import TaskLayout
from backend.re3d_adapter.real import RealDryRunRunner
from backend.re3d_adapter.settings import Re3DSettings


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


class RealRe3DDryRunIntegrationTests(unittest.TestCase):
    def test_real_re3d_entrypoint_uses_isolated_task_directories(self) -> None:
        re3d_root_value = os.environ.get("RE3D_INTEGRATION_ROOT")
        images_value = os.environ.get("RE3D_INTEGRATION_IMAGES")
        data_root_value = os.environ.get("RE3D_INTEGRATION_DATA_ROOT")
        if not re3d_root_value or not images_value or not data_root_value:
            self.skipTest("Re3D integration environment is not configured")

        re3d_root = Path(re3d_root_value).resolve()
        source_images = Path(images_value).resolve()
        temp_parent = Path(data_root_value).resolve() / "tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        candidates = sorted(
            path
            for path in source_images.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )[:3]
        self.assertEqual(len(candidates), 3)

        with tempfile.TemporaryDirectory(dir=temp_parent) as temporary:
            data_root = Path(temporary)
            job_id = str(uuid.uuid4())
            layout = TaskLayout.from_data_root(data_root, job_id, create=True)
            layout.ensure_worker_directories()
            target_images = layout.resolve("input/images")
            target_images.mkdir(parents=True)
            records = []
            total_bytes = 0
            for source in candidates:
                target = target_images / source.name
                shutil.copy2(source, target)
                records.append({"name": target.name})
                total_bytes += target.stat().st_size
            manifest_path = layout.resolve("input/input-manifest.json")
            atomic_write_json(manifest_path, {"images": records})

            request = copy.deepcopy(
                json.loads(REQUEST_EXAMPLE.read_text(encoding="utf-8"))
            )
            request["execution_mode"] = "real"
            request["job_id"] = job_id
            request["request_id"] = str(uuid.uuid4())
            request["requested_by"]["user_id"] = str(uuid.uuid4())
            request["input"]["image_count"] = 3
            request["input"]["total_bytes"] = total_bytes
            request["input"]["manifest_sha256"] = sha256_file(manifest_path)
            atomic_write_json(layout.request_path, request)

            settings = Re3DSettings.from_environment(root=str(re3d_root))
            outcome = RealDryRunRunner(
                layout,
                re3d_root=settings.root,
                driver_python=settings.driver_python,
            ).run()
            self.assertEqual(outcome.report["status"], "pass")
            self.assertGreater(outcome.report["step_count"], 20)
            self.assertEqual(
                {step["branch"] for step in outcome.report["steps"]},
                {"shared", "A-v4", "B-v2", "C"},
            )


if __name__ == "__main__":
    unittest.main()
