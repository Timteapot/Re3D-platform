from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from backend.evaluation import RealEvaluator
from backend.re3d_adapter.contracts import load_json_contract
from backend.re3d_adapter.errors import (
    IntegrityError,
    PipelineCancelled,
    PipelineTimedOut,
)
from backend.re3d_adapter.io import atomic_write_json
from backend.re3d_adapter.process import run_managed_process
from backend.re3d_adapter.real import (
    RealDryRunRunner,
    RealPipelineRunner,
    build_re3d_environment,
    map_re3d_step,
    parse_dry_run_steps,
    parse_re3d_step_line,
)
from tests.unit.test_worker_simulation import create_task


FAKE_STEPS = (
    "00-prepare-input",
    "01-colmap",
    "10-c-interface",
    "13-c-texture",
    "20-a-mapanything",
    "21-a-scale-calibration",
    "22-a-crossview-v4",
    "23-a-export-dmap",
    "25-a-reconstruct",
    "26-a-texture",
    "30-b-mvsanywhere",
    "31-b-scale-calibration",
    "32-b-crossview-v2",
    "33-b-export-dmap",
    "35-b-reconstruct",
    "36-b-texture",
)


def make_real_request(layout: Path, request: dict) -> dict:
    real_request = copy.deepcopy(request)
    real_request["execution_mode"] = "real"
    atomic_write_json(layout, real_request)
    return real_request


def create_fake_re3d(root: Path) -> None:
    script = root / "scripts" / "run_pipeline.py"
    script.parent.mkdir(parents=True)
    lines = repr(list(FAKE_STEPS))
    script.write_text(
        f"for name in {lines}:\n    print(f'[run] {{name}}')\n",
        encoding="utf-8",
    )


def create_fake_real_re3d(root: Path, *, slow: bool = False) -> None:
    script = root / "scripts" / "run_pipeline.py"
    script.parent.mkdir(parents=True)
    if slow:
        script.write_text(
            "import time\n"
            "print('[run] 00-prepare-input', flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        return
    steps = repr(list(FAKE_STEPS) + ["90-validate"])
    script.write_text(
        "import argparse, json\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--work-dir', type=Path, required=True)\n"
        "parser.add_argument('--output-dir', type=Path, required=True)\n"
        "args, _ = parser.parse_known_args()\n"
        f"for name in {steps}:\n"
        "    print(f'[run] {name}', flush=True)\n"
        "sfm = args.work_dir / 'shared' / 'colmap'\n"
        "sfm.mkdir(parents=True, exist_ok=True)\n"
        "(sfm / 'reconstruction_metrics.json').write_text(json.dumps({\n"
        "    'registered_images': 3, 'mean_reprojection_error_px': 0.8\n"
        "}), encoding='utf-8')\n"
        "for folder in ('a_mapanything/depth-v4', 'b_mvsanywhere/depth-v2'):\n"
        "    target = args.work_dir / 'branches' / folder\n"
        "    target.mkdir(parents=True, exist_ok=True)\n"
        "    (target / 'consistency_manifest.json').write_text(\n"
        "        json.dumps({'retained_valid_fraction': 0.75}), encoding='utf-8')\n"
        "validation = {'all_passed': True, 'branches': {}}\n"
        "for branch in ('A-v4', 'B-v2', 'C'):\n"
        "    target = args.output_dir / branch\n"
        "    target.mkdir(parents=True, exist_ok=True)\n"
        "    (target / 'mesh.glb').write_bytes(b'glTF-real')\n"
        "    (target / 'mesh.obj').write_text('v 0 0 0\\nf 1 1 1\\n', encoding='utf-8')\n"
        "    (target / 'mesh.mtl').write_text('map_Kd texture.jpg\\n', encoding='utf-8')\n"
        "    (target / 'texture.jpg').write_bytes(b'jpeg-real')\n"
        "    validation['branches'][branch] = {\n"
        "        'status': 'pass', 'errors': [], 'glb_vertices': 12, 'glb_faces': 4\n"
        "    }\n"
        "args.output_dir.mkdir(parents=True, exist_ok=True)\n"
        "(args.output_dir / 'validation.json').write_text(\n"
        "    json.dumps(validation), encoding='utf-8')\n",
        encoding="utf-8",
    )


class RealAdapterTests(unittest.TestCase):
    def test_maps_re3d_steps_to_platform_stages(self) -> None:
        self.assertEqual(map_re3d_step("00-prepare-input"), ("preparing", "shared"))
        self.assertEqual(map_re3d_step("11-c-densify"), ("c_geometry", "C"))
        self.assertEqual(map_re3d_step("24a-a-verify-handoff"), ("a_fusion", "A-v4"))
        self.assertEqual(map_re3d_step("36-b-normalize"), ("b_texturing", "B-v2"))

    def test_real_process_environment_excludes_platform_credentials(self) -> None:
        environment = build_re3d_environment(
            {
                "PATH": "safe-path",
                "SYSTEMROOT": "safe-system-root",
                "CUDA_VISIBLE_DEVICES": "0",
                "RE3D_MAP_PYTHON": "map-python",
                "DATABASE_URL": "postgresql://secret",
                "JWT_SECRET": "secret",
                "SMTP_PASSWORD": "secret",
            }
        )
        self.assertEqual(environment["PATH"], "safe-path")
        self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "0")
        self.assertEqual(environment["RE3D_MAP_PYTHON"], "map-python")
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("JWT_SECRET", environment)
        self.assertNotIn("SMTP_PASSWORD", environment)

    def test_parses_all_three_branches_from_dry_run_output(self) -> None:
        stdout = "\n".join(f"[run] {name}" for name in FAKE_STEPS)
        steps = parse_dry_run_steps(stdout)
        self.assertEqual(len(steps), len(FAKE_STEPS))
        self.assertEqual({step["branch"] for step in steps}, {"shared", "A-v4", "B-v2", "C"})
        self.assertEqual(
            [step["sequence"] for step in steps],
            list(range(1, len(steps) + 1)),
        )
        self.assertEqual(
            parse_re3d_step_line("[skip] 24a-a-verify-handoff: marker"),
            {
                "action": "skip",
                "re3d_step": "24a-a-verify-handoff",
                "platform_stage": "a_fusion",
                "branch": "A-v4",
            },
        )

    def test_builds_isolated_command_and_writes_dry_run_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            layout, request = create_task(base)
            request = make_real_request(layout.request_path, request)
            fake_re3d = base / "Re3D"
            create_fake_re3d(fake_re3d)
            runner = RealDryRunRunner(
                layout,
                re3d_root=fake_re3d,
                driver_python=Path(sys.executable),
                installation_verifier=lambda _root, _pipeline: None,
            )

            command = runner.build_command(request)
            self.assertIn(str(layout.resolve("runtime/work")), command)
            self.assertIn(str(layout.resolve("runtime/logs")), command)
            self.assertIn(str(layout.resolve("output")), command)
            self.assertEqual(command[-1], "--dry-run")

            outcome = runner.run()
            self.assertEqual(outcome.report["status"], "pass")
            self.assertEqual(outcome.report["operation"], "real_pipeline_dry_run")
            self.assertEqual(outcome.report["step_count"], len(FAKE_STEPS))
            self.assertTrue(outcome.report_path.is_file())
            self.assertTrue(runner.log_path.is_file())
            serialized = outcome.report_path.read_text(encoding="utf-8")
            self.assertNotIn(str(layout.root), serialized)
            self.assertNotIn(str(fake_re3d), serialized)

    def test_real_pipeline_collects_artifacts_and_writes_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            layout, request = create_task(base)
            make_real_request(layout.request_path, request)
            fake_re3d = base / "Re3D"
            create_fake_real_re3d(fake_re3d)
            observed_steps: list[dict[str, str]] = []
            runner = RealPipelineRunner(
                layout,
                re3d_root=fake_re3d,
                driver_python=Path(sys.executable),
                installation_verifier=lambda _root, _pipeline: None,
            )

            outcome = runner.run(
                on_step=observed_steps.append,
                poll_seconds=0.01,
            )

            self.assertFalse(outcome.reused)
            result = load_json_contract(outcome.result_path, "pipeline-result")
            self.assertEqual(result["execution_mode"], "real")
            self.assertEqual(result["input_summary"]["registered_images"], 3)
            self.assertEqual(len(observed_steps), len(FAKE_STEPS) + 1)
            for branch in ("A-v4", "B-v2", "C"):
                self.assertEqual(len(result["branches"][branch]["artifacts"]), 4)
                self.assertEqual(result["branches"][branch]["metrics"]["faces"], 4)

            evaluation = RealEvaluator(layout).run()
            self.assertEqual(evaluation.report["overall"]["status"], "pass")
            self.assertIsNone(evaluation.report["overall"]["score"])
            self.assertEqual(evaluation.report["branches"]["C"]["status"], "pass")

            repeated = runner.run(poll_seconds=0.01)
            self.assertTrue(repeated.reused)
            self.assertTrue(RealEvaluator(layout).run().reused)

    def test_real_pipeline_rejects_mismatched_recovered_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            layout, request = create_task(base)
            make_real_request(layout.request_path, request)
            fake_re3d = base / "Re3D"
            create_fake_real_re3d(fake_re3d)
            runner = RealPipelineRunner(
                layout,
                re3d_root=fake_re3d,
                driver_python=Path(sys.executable),
                installation_verifier=lambda _root, _pipeline: None,
            )
            runner.run(poll_seconds=0.01)
            result = load_json_contract(layout.result_path, "pipeline-result")
            result["input_summary"]["manifest_sha256"] = "f" * 64
            atomic_write_json(layout.result_path, result)

            with self.assertRaisesRegex(
                IntegrityError,
                "existing real result identity does not match request",
            ):
                runner.run(poll_seconds=0.01)

    def test_real_pipeline_cancellation_terminates_managed_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            layout, request = create_task(base)
            make_real_request(layout.request_path, request)
            fake_re3d = base / "Re3D"
            create_fake_real_re3d(fake_re3d, slow=True)
            runner = RealPipelineRunner(
                layout,
                re3d_root=fake_re3d,
                driver_python=Path(sys.executable),
                installation_verifier=lambda _root, _pipeline: None,
            )
            checks = 0

            def cancel_requested() -> bool:
                nonlocal checks
                checks += 1
                return checks >= 3

            started = time.monotonic()
            with self.assertRaises(PipelineCancelled):
                runner.run(
                    cancel_requested=cancel_requested,
                    poll_seconds=0.05,
                )
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(layout.result_path.exists())
            self.assertIn(
                "managed Re3D process started",
                runner.log_path.read_text(encoding="utf-8"),
            )

    def test_real_pipeline_timeout_terminates_managed_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            log_path = base / "timeout.log"

            started = time.monotonic()
            with self.assertRaises(PipelineTimedOut):
                run_managed_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=base,
                    log_path=log_path,
                    timeout_seconds=1,
                    poll_seconds=0.05,
                )
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(log_path.is_file())


if __name__ == "__main__":
    unittest.main()
