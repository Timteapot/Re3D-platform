from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.re3d_adapter.contracts import load_json_contract, validate_contract
from backend.re3d_adapter.errors import IntegrityError
from backend.re3d_adapter.events import utc_now
from backend.re3d_adapter.io import atomic_write_json, sha256_file
from backend.re3d_adapter.paths import TaskLayout


@dataclass(frozen=True)
class SimulationEvaluationOutcome:
    report_path: Path
    report: dict[str, Any]
    reused: bool


class SimulationEvaluator:
    """Write an honest, contract-valid report for development-only artifacts."""

    def __init__(self, layout: TaskLayout) -> None:
        self.layout = layout

    def run(self) -> SimulationEvaluationOutcome:
        result = load_json_contract(self.layout.result_path, "pipeline-result")
        if result["job_id"] != self.layout.job_id:
            raise IntegrityError("pipeline result does not belong to its task directory")
        if result["execution_mode"] != "simulated":
            raise IntegrityError("simulation evaluator only accepts simulated results")
        self._validate_artifacts(result)

        source_sha256 = sha256_file(self.layout.result_path)
        if self.layout.evaluation_path.exists():
            report = load_json_contract(self.layout.evaluation_path, "evaluation")
            if (
                report["job_id"] != self.layout.job_id
                or report["source_result_sha256"] != source_sha256
            ):
                raise IntegrityError(
                    "existing evaluation does not match the pipeline result"
                )
            return SimulationEvaluationOutcome(
                self.layout.evaluation_path,
                report,
                reused=True,
            )

        report = self._build_report(result, source_sha256)
        validate_contract("evaluation", report)
        atomic_write_json(self.layout.evaluation_path, report)
        return SimulationEvaluationOutcome(
            self.layout.evaluation_path,
            report,
            reused=False,
        )

    def _validate_artifacts(self, result: dict[str, Any]) -> None:
        for branch, branch_result in result["branches"].items():
            for artifact in branch_result["artifacts"]:
                artifact_path = self.layout.resolve(artifact["path"])
                if not artifact_path.is_file():
                    raise IntegrityError(f"{branch} evaluation artifact is missing")
                if artifact_path.stat().st_size != artifact["size_bytes"]:
                    raise IntegrityError(
                        f"{branch} evaluation artifact size does not match"
                    )
                if sha256_file(artifact_path) != artifact["sha256"]:
                    raise IntegrityError(
                        f"{branch} evaluation artifact hash does not match"
                    )

    def _build_report(
        self,
        result: dict[str, Any],
        source_sha256: str,
    ) -> dict[str, Any]:
        branches = {
            branch: self._branch_report(branch, branch_result)
            for branch, branch_result in result["branches"].items()
        }
        return {
            "contract_version": "1.0",
            "rules_version": "0.1.0",
            "job_id": self.layout.job_id,
            "evaluated_at": utc_now(),
            "source_result_sha256": source_sha256,
            "scope": "structural_health",
            "overall": {
                "status": "not_available",
                "score": None,
                "summary": (
                    "模拟任务已完成输入和产物完整性检查；模拟 GLB 不包含真实几何，"
                    "不生成质量分数。"
                ),
            },
            "shared_dimensions": {
                "input": {
                    "status": "pass",
                    "summary": "模拟输入数量符合开发任务约束。",
                    "checks": [
                        {
                            "id": "input.image_count",
                            "label": "输入图片数量",
                            "status": "pass",
                            "actual": result["input_summary"]["image_count"],
                            "unit": "images",
                            "thresholds": {"minimum": 3, "maximum": 150},
                            "evidence_refs": ["input/input-manifest.json"],
                        }
                    ],
                },
                "sfm": {
                    "status": "not_available",
                    "summary": "模拟任务不产生可用于质量判断的真实 SfM 指标。",
                    "checks": [
                        {
                            "id": "sfm.real_metrics",
                            "label": "真实 SfM 指标",
                            "status": "not_available",
                            "actual": None,
                            "unit": None,
                            "evidence_refs": ["manifests/pipeline-result.json"],
                        }
                    ],
                },
                "performance": {
                    "status": "not_available",
                    "summary": "模拟运行耗时不能代表真实 GPU 管线性能。",
                    "checks": [
                        {
                            "id": "performance.real_duration",
                            "label": "真实管线耗时",
                            "status": "not_available",
                            "actual": None,
                            "unit": "seconds",
                            "evidence_refs": ["manifests/pipeline-result.json"],
                        }
                    ],
                },
            },
            "branches": branches,
            "limitations": [
                "本报告来自开发模拟器；最小 GLB 不包含真实网格、材质或纹理，"
                "不能用于判断重建质量或几何精度。"
            ],
        }

    @staticmethod
    def _branch_report(
        branch: str,
        branch_result: dict[str, Any],
    ) -> dict[str, Any]:
        identifier = {"A-v4": "a", "B-v2": "b", "C": "c"}[branch]
        artifact = branch_result["artifacts"][0]
        return {
            "status": "not_available",
            "score": None,
            "summary": f"{branch} 模拟产物完整，但不包含可评估的真实几何。",
            "dimensions": {
                "depth": {
                    "status": "not_available",
                    "summary": "模拟任务不生成真实深度数据。",
                    "checks": [
                        {
                            "id": f"{identifier}.depth.real_metrics",
                            "label": "真实深度指标",
                            "status": "not_available",
                            "actual": None,
                            "unit": None,
                            "evidence_refs": ["manifests/pipeline-result.json"],
                        }
                    ],
                },
                "mesh": {
                    "status": "not_available",
                    "summary": "最小模拟 GLB 不含网格。",
                    "checks": [
                        {
                            "id": f"{identifier}.mesh.real_faces",
                            "label": "真实网格面数",
                            "status": "not_available",
                            "actual": None,
                            "unit": "faces",
                            "evidence_refs": [artifact["path"]],
                        }
                    ],
                },
                "artifacts": {
                    "status": "pass",
                    "summary": "模拟 GLB 已通过文件大小与 SHA-256 完整性校验。",
                    "checks": [
                        {
                            "id": f"{identifier}.artifact.integrity",
                            "label": "GLB 产物完整性",
                            "status": "pass",
                            "actual": True,
                            "unit": None,
                            "evidence_refs": [
                                artifact["path"],
                                "manifests/output-validation.json",
                            ],
                        }
                    ],
                },
            },
        }
