from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from apps.maintenance.main import main as maintenance_main
from backend.monitoring import StorageCalibrationSettings, StorageCapacityReporter
from backend.re3d_adapter.contracts import validate_contract
from backend.re3d_adapter.io import atomic_write_json


ROOT = Path(__file__).resolve().parents[2]
STORAGE_EXAMPLE = (
    ROOT
    / "packages"
    / "contracts"
    / "examples"
    / "v1"
    / "valid"
    / "storage-usage.json"
)


class StorageCapacityReporterTests(unittest.TestCase):
    def test_aggregates_only_complete_semantically_valid_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            for peak_total in (100, 200, 1000):
                _write_summary(data_root, peak_total=peak_total)
            _write_summary(data_root, peak_total=300, status="partial")
            _write_summary(data_root, peak_total=400, mismatched_job_id=True)

            report = StorageCapacityReporter(
                data_root,
                settings=StorageCalibrationSettings(
                    minimum_samples=3,
                    safety_factor=Decimal("1.25"),
                    rounding_bytes=100,
                ),
            ).build()

        validate_contract("storage-capacity-report", report)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(
            report["selection"],
            {
                "discovered_summaries": 5,
                "excluded_non_complete_samples": 1,
                "invalid_samples": 1,
                "complete_samples": 3,
                "minimum_samples": 3,
            },
        )
        self.assertEqual(
            report["task_peak_bytes"],
            {"p50_bytes": 200, "p95_bytes": 1000, "maximum_bytes": 1000},
        )
        self.assertEqual(
            report["recommendation"],
            {
                "basis": "OBSERVED_MAXIMUM_PEAK",
                "observed_maximum_bytes": 1000,
                "scaled_bytes": 1250,
                "recommended_reservation_bytes": 1300,
            },
        )
        self.assertEqual(
            [tier["name"] for tier in report["final_tiers"]],
            ["input", "runtime", "output", "reports", "manifests", "other"],
        )
        self.assertEqual(report["final_tiers"][0]["aggregate_share_percent"], 14.29)
        self.assertNotIn(str(data_root), json.dumps(report))

    def test_insufficient_data_has_metrics_but_no_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            _write_summary(data_root, peak_total=100)

            report = StorageCapacityReporter(data_root).build()

        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(report["selection"]["complete_samples"], 1)
        self.assertIsNotNone(report["task_peak_bytes"])
        self.assertIsNone(report["recommendation"])

    def test_empty_data_root_produces_contract_valid_insufficient_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = StorageCapacityReporter(Path(temporary) / "missing").build()

        validate_contract("storage-capacity-report", report)
        self.assertEqual(report["status"], "insufficient_data")
        self.assertIsNone(report["task_peak_bytes"])
        self.assertEqual(report["final_tiers"], [])
        self.assertEqual(report["branches"], [])

    def test_maintenance_command_outputs_private_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            _write_summary(data_root, peak_total=2_000_000)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = maintenance_main(
                    [
                        "report-storage-capacity",
                        "--data-root",
                        str(data_root),
                        "--minimum-samples",
                        "1",
                        "--safety-factor",
                        "1",
                        "--rounding-mib",
                        "1",
                    ]
                )

        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        validate_contract("storage-capacity-report", report)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(
            report["recommendation"]["recommended_reservation_bytes"],
            2 * 1024 * 1024,
        )

    def test_settings_reject_unsafe_ranges_and_invalid_decimal(self) -> None:
        with self.assertRaisesRegex(ValueError, "minimum_samples"):
            StorageCalibrationSettings(minimum_samples=0)
        with self.assertRaisesRegex(ValueError, "safety_factor"):
            StorageCalibrationSettings(safety_factor=Decimal("0.99"))
        with self.assertRaisesRegex(ValueError, "safety_factor"):
            StorageCalibrationSettings.from_values(safety_factor="NaN")
        with self.assertRaisesRegex(ValueError, "decimal number"):
            StorageCalibrationSettings.from_values(safety_factor="not-a-number")


def _write_summary(
    data_root: Path,
    *,
    peak_total: int,
    status: str = "complete",
    mismatched_job_id: bool = False,
) -> None:
    directory_job_id = str(uuid.uuid4())
    summary = copy.deepcopy(json.loads(STORAGE_EXAMPLE.read_text(encoding="utf-8")))
    summary["job_id"] = str(uuid.uuid4()) if mismatched_job_id else directory_job_id
    summary["status"] = status
    summary["errors"] = ["TASK_STORAGE_SAMPLE_FAILED"] if status != "complete" else []
    summary["final"] = {
        "total_bytes": 70,
        "input_bytes": 10,
        "runtime_bytes": 20,
        "output_bytes": 30,
        "reports_bytes": 4,
        "manifests_bytes": 1,
        "other_bytes": 5,
        "branches": [
            {"name": "A-v4", "used_bytes": 10},
            {"name": "B-v2", "used_bytes": 10},
            {"name": "C", "used_bytes": 10},
        ],
    }
    summary["peaks"] = {
        "total_bytes": peak_total,
        "runtime_bytes": 25,
        "output_bytes": 36,
        "branches": [
            {"name": "A-v4", "used_bytes": 12},
            {"name": "B-v2", "used_bytes": 12},
            {"name": "C", "used_bytes": 12},
        ],
    }
    validate_contract("storage-usage", summary)
    atomic_write_json(
        data_root / "jobs" / directory_job_id / "reports" / "storage-usage.json",
        summary,
    )


if __name__ == "__main__":
    unittest.main()
