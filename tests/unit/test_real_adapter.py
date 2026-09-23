from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from backend.re3d_adapter.io import atomic_write_json
from backend.re3d_adapter.real import (
    RealDryRunRunner,
    map_re3d_step,
    parse_dry_run_steps,
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


class RealAdapterTests(unittest.TestCase):
    def test_maps_re3d_steps_to_platform_stages(self) -> None:
        self.assertEqual(map_re3d_step("00-prepare-input"), ("preparing", "shared"))
        self.assertEqual(map_re3d_step("11-c-densify"), ("c_geometry", "C"))
        self.assertEqual(map_re3d_step("24a-a-verify-handoff"), ("a_fusion", "A-v4"))
        self.assertEqual(map_re3d_step("36-b-normalize"), ("b_texturing", "B-v2"))

    def test_parses_all_three_branches_from_dry_run_output(self) -> None:
        stdout = "\n".join(f"[run] {name}" for name in FAKE_STEPS)
        steps = parse_dry_run_steps(stdout)
        self.assertEqual(len(steps), len(FAKE_STEPS))
        self.assertEqual({step["branch"] for step in steps}, {"shared", "A-v4", "B-v2", "C"})
        self.assertEqual(
            [step["sequence"] for step in steps],
            list(range(1, len(steps) + 1)),
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


if __name__ == "__main__":
    unittest.main()
