from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from backend.re3d_adapter.contracts import load_json_contract, validate_contract
from backend.re3d_adapter.errors import AdapterError


MEBIBYTE = 1024 * 1024
DEFAULT_ROUNDING_BYTES = 256 * MEBIBYTE
EXPECTED_BRANCHES = ("A-v4", "B-v2", "C")
FINAL_TIERS = (
    ("input", "input_bytes"),
    ("runtime", "runtime_bytes"),
    ("output", "output_bytes"),
    ("reports", "reports_bytes"),
    ("manifests", "manifests_bytes"),
    ("other", "other_bytes"),
)


@dataclass(frozen=True)
class StorageCalibrationSettings:
    minimum_samples: int = 5
    safety_factor: Decimal = Decimal("1.25")
    rounding_bytes: int = DEFAULT_ROUNDING_BYTES

    def __post_init__(self) -> None:
        if not 1 <= self.minimum_samples <= 10_000:
            raise ValueError("minimum_samples must be between 1 and 10000")
        if (
            not self.safety_factor.is_finite()
            or not Decimal("1") <= self.safety_factor <= Decimal("5")
        ):
            raise ValueError("safety_factor must be between 1 and 5")
        if not 1 <= self.rounding_bytes <= 1024**4:
            raise ValueError("rounding_bytes must be between 1 byte and 1 TiB")

    @classmethod
    def from_values(
        cls,
        *,
        minimum_samples: int | None = None,
        safety_factor: str | None = None,
        rounding_mib: int | None = None,
    ) -> "StorageCalibrationSettings":
        try:
            parsed_factor = (
                Decimal(safety_factor)
                if safety_factor is not None
                else Decimal("1.25")
            )
        except InvalidOperation as exc:
            raise ValueError("safety_factor must be a decimal number") from exc
        return cls(
            minimum_samples=minimum_samples if minimum_samples is not None else 5,
            safety_factor=parsed_factor,
            rounding_bytes=(
                rounding_mib * MEBIBYTE
                if rounding_mib is not None
                else DEFAULT_ROUNDING_BYTES
            ),
        )


class StorageCapacityReporter:
    """Aggregate private task summaries without exposing job identities or paths."""

    def __init__(
        self,
        data_root: Path,
        *,
        settings: StorageCalibrationSettings | None = None,
    ) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.settings = settings or StorageCalibrationSettings()

    def build(self) -> dict[str, Any]:
        summaries, selection = self._load_complete_summaries()
        complete_count = len(summaries)
        ready = complete_count >= self.settings.minimum_samples
        peak_values = [summary["peaks"]["total_bytes"] for summary in summaries]
        peak_statistics = _statistics(peak_values)
        recommendation = None
        if ready and peak_statistics is not None:
            observed_maximum = peak_statistics["maximum_bytes"]
            scaled_bytes = int(
                (Decimal(observed_maximum) * self.settings.safety_factor).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            rounded_bytes = _round_up(scaled_bytes, self.settings.rounding_bytes)
            recommendation = {
                "basis": "OBSERVED_MAXIMUM_PEAK",
                "observed_maximum_bytes": observed_maximum,
                "scaled_bytes": scaled_bytes,
                "recommended_reservation_bytes": rounded_bytes,
            }

        final_total_bytes = sum(
            summary["final"]["total_bytes"] for summary in summaries
        )
        final_tiers = [
            {
                "name": name,
                **(_statistics([summary["final"][field] for summary in summaries]) or {}),
                "aggregate_share_percent": _share_percent(
                    sum(summary["final"][field] for summary in summaries),
                    final_total_bytes,
                ),
            }
            for name, field in FINAL_TIERS
        ]
        final_output_bytes = sum(
            summary["final"]["output_bytes"] for summary in summaries
        )
        branches = [
            {
                "name": branch,
                **(
                    _statistics(
                        [
                            _branch_bytes(summary["final"]["branches"], branch)
                            for summary in summaries
                        ]
                    )
                    or {}
                ),
                "aggregate_output_share_percent": _share_percent(
                    sum(
                        _branch_bytes(summary["final"]["branches"], branch)
                        for summary in summaries
                    ),
                    final_output_bytes,
                ),
            }
            for branch in EXPECTED_BRANCHES
        ]

        report = {
            "contract_version": "1.0",
            "operation": "report-storage-capacity",
            "generated_at": _utc_now(),
            "status": "ready" if ready else "insufficient_data",
            "selection": {
                **selection,
                "complete_samples": complete_count,
                "minimum_samples": self.settings.minimum_samples,
            },
            "percentile_method": "NEAREST_RANK",
            "safety_factor": float(self.settings.safety_factor),
            "rounding_bytes": self.settings.rounding_bytes,
            "task_peak_bytes": peak_statistics,
            "final_tiers": final_tiers if summaries else [],
            "branches": branches if summaries else [],
            "recommendation": recommendation,
            "limitations": [
                "ONLY_COMPLETE_STORAGE_USAGE_SUMMARIES_INCLUDED",
                "PERIODIC_SAMPLING_MAY_UNDERESTIMATE_SHORT_LIVED_FILES",
                "WORKLOAD_REPRESENTATIVENESS_REQUIRES_OPERATOR_REVIEW",
            ],
        }
        validate_contract("storage-capacity-report", report)
        return report

    def _load_complete_summaries(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        jobs_root = self.data_root / "jobs"
        selection = {
            "discovered_summaries": 0,
            "excluded_non_complete_samples": 0,
            "invalid_samples": 0,
        }
        if not jobs_root.exists():
            return [], selection
        if _is_link(jobs_root) or not jobs_root.is_dir():
            raise ValueError("jobs directory must be a real directory")
        try:
            with os.scandir(jobs_root) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError("cannot read jobs directory") from exc

        summaries: list[dict[str, Any]] = []
        for entry in entries:
            if not _is_canonical_uuid(entry.name):
                continue
            task_root = Path(entry.path)
            try:
                if (
                    entry.is_symlink()
                    or _is_link(task_root)
                    or not entry.is_dir(follow_symlinks=False)
                ):
                    continue
            except OSError:
                continue
            reports_root = task_root / "reports"
            summary_path = reports_root / "storage-usage.json"
            try:
                if _is_link(reports_root) or not reports_root.is_dir():
                    continue
                if _is_link(summary_path):
                    selection["discovered_summaries"] += 1
                    selection["invalid_samples"] += 1
                    continue
                if not summary_path.exists():
                    continue
                selection["discovered_summaries"] += 1
                if not summary_path.is_file():
                    selection["invalid_samples"] += 1
                    continue
                summary = load_json_contract(summary_path, "storage-usage")
            except (AdapterError, OSError, UnicodeError):
                selection["invalid_samples"] += 1
                continue
            if summary["status"] != "complete":
                selection["excluded_non_complete_samples"] += 1
                continue
            if not _is_semantically_valid(summary, expected_job_id=entry.name):
                selection["invalid_samples"] += 1
                continue
            summaries.append(summary)
        return summaries, selection


def _is_semantically_valid(summary: dict[str, Any], *, expected_job_id: str) -> bool:
    final = summary["final"]
    if (
        summary["job_id"] != expected_job_id
        or final is None
        or summary["errors"]
        or summary["storage_observation_count"] < 1
    ):
        return False
    category_total = sum(final[field] for _, field in FINAL_TIERS)
    final_branches = {item["name"]: item["used_bytes"] for item in final["branches"]}
    peak_branches = {
        item["name"]: item["used_bytes"] for item in summary["peaks"]["branches"]
    }
    if (
        category_total != final["total_bytes"]
        or len(final["branches"]) != len(EXPECTED_BRANCHES)
        or len(summary["peaks"]["branches"]) != len(EXPECTED_BRANCHES)
        or set(final_branches) != set(EXPECTED_BRANCHES)
        or set(peak_branches) != set(EXPECTED_BRANCHES)
        or sum(final_branches.values()) > final["output_bytes"]
        or summary["peaks"]["total_bytes"] < final["total_bytes"]
        or summary["peaks"]["total_bytes"] < summary["peaks"]["runtime_bytes"]
        or summary["peaks"]["total_bytes"] < summary["peaks"]["output_bytes"]
        or summary["peaks"]["runtime_bytes"] < final["runtime_bytes"]
        or summary["peaks"]["output_bytes"] < final["output_bytes"]
    ):
        return False
    return all(
        peak_branches[branch] >= final_branches[branch]
        and summary["peaks"]["total_bytes"] >= peak_branches[branch]
        for branch in EXPECTED_BRANCHES
    )


def _statistics(values: list[int]) -> dict[str, int] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "p50_bytes": _nearest_rank(ordered, Decimal("0.50")),
        "p95_bytes": _nearest_rank(ordered, Decimal("0.95")),
        "maximum_bytes": ordered[-1],
    }


def _nearest_rank(ordered: list[int], percentile: Decimal) -> int:
    rank = int(
        (Decimal(len(ordered)) * percentile).to_integral_value(rounding=ROUND_CEILING)
    )
    return ordered[max(rank, 1) - 1]


def _branch_bytes(branches: list[dict[str, Any]], name: str) -> int:
    return next(item["used_bytes"] for item in branches if item["name"] == name)


def _share_percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    value = (Decimal(part) * Decimal(100) / Decimal(total)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(value)


def _round_up(value: int, quantum: int) -> int:
    return ((value + quantum - 1) // quantum) * quantum


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or getattr(path, "is_junction", lambda: False)()
    except OSError:
        return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
