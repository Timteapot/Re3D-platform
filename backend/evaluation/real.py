from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.re3d_adapter.contracts import load_json_contract, validate_contract
from backend.re3d_adapter.errors import IntegrityError
from backend.re3d_adapter.events import utc_now
from backend.re3d_adapter.io import atomic_write_json, sha256_file
from backend.re3d_adapter.paths import TaskLayout


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = ROOT / "config" / "evaluation-rules-v1.json"
STATUS_RANK = {
    "not_applicable": -1,
    "pass": 0,
    "warning": 1,
    "fail": 2,
}


@dataclass(frozen=True)
class RealEvaluationOutcome:
    report_path: Path
    report: dict[str, Any]
    reused: bool


class RealEvaluator:
    """Evaluate observable structural health without claiming geometric accuracy."""

    def __init__(
        self,
        layout: TaskLayout,
        *,
        rules_path: Path = DEFAULT_RULES_PATH,
    ) -> None:
        self.layout = layout
        try:
            self.rules = json.loads(rules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError("cannot load real evaluation rules") from exc
        self._validate_rules()

    def run(self) -> RealEvaluationOutcome:
        result = load_json_contract(self.layout.result_path, "pipeline-result")
        request = load_json_contract(self.layout.request_path, "pipeline-request")
        if (
            result["job_id"] != self.layout.job_id
            or request["job_id"] != self.layout.job_id
            or result["request_id"] != request["request_id"]
            or result["execution_mode"] != "real"
            or request["execution_mode"] != "real"
            or result["status"] != "succeeded"
        ):
            raise IntegrityError("real evaluation input identity is invalid")
        self._validate_artifacts(result)

        source_sha256 = sha256_file(self.layout.result_path)
        if self.layout.evaluation_path.exists():
            report = load_json_contract(self.layout.evaluation_path, "evaluation")
            if (
                report["job_id"] != self.layout.job_id
                or report["source_result_sha256"] != source_sha256
                or report["rules_version"] != self.rules["rules_version"]
            ):
                raise IntegrityError("existing real evaluation is stale or mismatched")
            return RealEvaluationOutcome(
                self.layout.evaluation_path,
                report,
                reused=True,
            )

        report = self._build_report(result, request, source_sha256)
        validate_contract("evaluation", report)
        atomic_write_json(self.layout.evaluation_path, report)
        return RealEvaluationOutcome(
            self.layout.evaluation_path,
            report,
            reused=False,
        )

    def _validate_artifacts(self, result: dict[str, Any]) -> None:
        for branch, branch_result in result["branches"].items():
            for artifact in branch_result["artifacts"]:
                path = self.layout.resolve(artifact["path"])
                if path.is_symlink() or not path.is_file():
                    raise IntegrityError(f"{branch} evaluation artifact is missing")
                if path.stat().st_size != artifact["size_bytes"]:
                    raise IntegrityError(f"{branch} evaluation artifact size changed")
                if sha256_file(path) != artifact["sha256"]:
                    raise IntegrityError(f"{branch} evaluation artifact hash changed")

    def _build_report(
        self,
        result: dict[str, Any],
        request: dict[str, Any],
        source_sha256: str,
    ) -> dict[str, Any]:
        image_count = result["input_summary"]["image_count"]
        registered = result["input_summary"].get("registered_images", 0)
        registration_rate = registered / image_count
        reprojection = result["input_summary"].get(
            "mean_reprojection_error_px"
        )
        registration_status = self._minimum_status(
            registration_rate,
            self.rules["registration_rate"],
        )
        reprojection_status = self._maximum_status(
            reprojection,
            self.rules["mean_reprojection_error_px"],
        )
        sfm_status = _worst_status(registration_status, reprojection_status)
        performance_status = (
            "pass"
            if result["duration_seconds"] <= request["limits"]["timeout_seconds"]
            else "fail"
        )
        branches = {
            name: self._branch_report(name, value)
            for name, value in result["branches"].items()
        }
        overall_status = _worst_status(
            "pass",
            sfm_status,
            performance_status,
            *(branch["status"] for branch in branches.values()),
        )
        summary = {
            "pass": "输入、SfM、三分支网格和产物完整性均通过当前结构健康规则。",
            "warning": "重建已完成，但部分结构健康指标处于需关注范围。",
            "fail": "重建产物已生成，但至少一项结构健康规则未通过。",
        }[overall_status]
        return {
            "contract_version": "1.0",
            "rules_version": self.rules["rules_version"],
            "job_id": self.layout.job_id,
            "evaluated_at": utc_now(),
            "source_result_sha256": source_sha256,
            "scope": "structural_health",
            "overall": {
                "status": overall_status,
                "score": None,
                "summary": summary,
            },
            "shared_dimensions": {
                "input": {
                    "status": "pass",
                    "summary": "输入图片数量满足固定任务契约。",
                    "checks": [
                        {
                            "id": "input.image_count",
                            "label": "输入图片数量",
                            "status": "pass",
                            "actual": image_count,
                            "unit": "images",
                            "thresholds": {"minimum": 3, "maximum": 150},
                            "evidence_refs": ["input/input-manifest.json"],
                        }
                    ],
                },
                "sfm": {
                    "status": sfm_status,
                    "summary": "根据图片注册率和平均重投影误差判断相机重建健康度。",
                    "checks": [
                        {
                            "id": "sfm.registration_rate",
                            "label": "SfM 图片注册率",
                            "status": registration_status,
                            "actual": registration_rate,
                            "unit": "ratio",
                            "thresholds": self.rules["registration_rate"],
                            "evidence_refs": [
                                "runtime/work/shared/colmap/reconstruction_metrics.json"
                            ],
                        },
                        {
                            "id": "sfm.mean_reprojection_error",
                            "label": "平均重投影误差",
                            "status": reprojection_status,
                            "actual": reprojection,
                            "unit": "pixels",
                            "thresholds": self.rules[
                                "mean_reprojection_error_px"
                            ],
                            "evidence_refs": [
                                "runtime/work/shared/colmap/reconstruction_metrics.json"
                            ],
                        },
                    ],
                },
                "performance": {
                    "status": performance_status,
                    "summary": "检查任务是否在请求规定的总超时内完成。",
                    "checks": [
                        {
                            "id": "performance.duration",
                            "label": "总运行时长",
                            "status": performance_status,
                            "actual": result["duration_seconds"],
                            "unit": "seconds",
                            "thresholds": {
                                "timeout": request["limits"]["timeout_seconds"]
                            },
                            "evidence_refs": ["manifests/pipeline-result.json"],
                        }
                    ],
                },
            },
            "branches": branches,
            "limitations": [
                "该报告只评估输入、流程指标、网格结构和产物完整性；没有真值网格或激光扫描，不能证明绝对几何精度。",
                "当前规则阈值尚需使用弱纹理、反光、遮挡和视角不足等数据集继续校准。",
            ],
        }

    def _branch_report(
        self,
        branch: str,
        branch_result: dict[str, Any],
    ) -> dict[str, Any]:
        identifier = {"A-v4": "a", "B-v2": "b", "C": "c"}[branch]
        faces = branch_result["metrics"]["faces"]
        mesh_status = (
            "pass" if faces >= self.rules["mesh_faces"]["minimum"] else "fail"
        )
        if branch == "C":
            depth_status = "not_applicable"
            depth_actual = None
            depth_summary = "C 分支使用 OpenMVS PatchMatch，不套用学习深度保留率规则。"
        else:
            depth_actual = branch_result["metrics"]["retained_valid_fraction"]
            depth_status = self._minimum_status(
                depth_actual,
                self.rules["retained_valid_fraction"],
            )
            depth_summary = "检查跨视角过滤后的有效深度保留比例。"
        branch_status = _worst_status("pass", depth_status, mesh_status)
        return {
            "status": branch_status,
            "score": None,
            "summary": (
                f"{branch} 分支结构健康检查通过。"
                if branch_status == "pass"
                else f"{branch} 分支存在需要关注或未通过的结构指标。"
            ),
            "dimensions": {
                "depth": {
                    "status": depth_status,
                    "summary": depth_summary,
                    "checks": [
                        {
                            "id": f"{identifier}.depth_retained_fraction",
                            "label": "深度有效像素保留率",
                            "status": depth_status,
                            "actual": depth_actual,
                            "unit": "ratio",
                            "thresholds": self.rules["retained_valid_fraction"],
                            "evidence_refs": ["manifests/pipeline-result.json"],
                        }
                    ],
                },
                "mesh": {
                    "status": mesh_status,
                    "summary": "检查最终 GLB 的网格面数量。",
                    "checks": [
                        {
                            "id": f"{identifier}.mesh_faces",
                            "label": "网格面数量",
                            "status": mesh_status,
                            "actual": faces,
                            "unit": "faces",
                            "thresholds": self.rules["mesh_faces"],
                            "evidence_refs": ["manifests/output-validation.json"],
                        }
                    ],
                },
                "artifacts": {
                    "status": "pass",
                    "summary": "结果契约中的产物均通过路径、大小和 SHA-256 校验。",
                    "checks": [
                        {
                            "id": f"{identifier}.artifact_integrity",
                            "label": "归一化产物完整性",
                            "status": "pass",
                            "actual": True,
                            "unit": None,
                            "evidence_refs": [
                                f"output/{branch}/mesh.glb",
                                "manifests/pipeline-result.json",
                            ],
                        }
                    ],
                },
            },
        }

    def _validate_rules(self) -> None:
        if (
            not isinstance(self.rules, dict)
            or self.rules.get("schema_version") != "1.0"
            or not isinstance(self.rules.get("rules_version"), str)
        ):
            raise IntegrityError("real evaluation rules have an invalid identity")
        minimums = (
            self.rules.get("registration_rate"),
            self.rules.get("retained_valid_fraction"),
        )
        for rule in minimums:
            if (
                not isinstance(rule, dict)
                or not 0 <= rule.get("warning_minimum", -1) <= 1
                or not 0 <= rule.get("pass_minimum", -1) <= 1
                or rule["warning_minimum"] > rule["pass_minimum"]
            ):
                raise IntegrityError("minimum evaluation threshold is invalid")
        maximum = self.rules.get("mean_reprojection_error_px")
        if (
            not isinstance(maximum, dict)
            or maximum.get("pass_maximum", -1) < 0
            or maximum.get("warning_maximum", -1) < maximum["pass_maximum"]
        ):
            raise IntegrityError("maximum evaluation threshold is invalid")
        mesh = self.rules.get("mesh_faces")
        if not isinstance(mesh, dict) or mesh.get("minimum", 0) < 1:
            raise IntegrityError("mesh evaluation threshold is invalid")

    @staticmethod
    def _minimum_status(actual: float, rule: dict[str, float]) -> str:
        if actual >= rule["pass_minimum"]:
            return "pass"
        if actual >= rule["warning_minimum"]:
            return "warning"
        return "fail"

    @staticmethod
    def _maximum_status(actual: float, rule: dict[str, float]) -> str:
        if actual <= rule["pass_maximum"]:
            return "pass"
        if actual <= rule["warning_maximum"]:
            return "warning"
        return "fail"


def _worst_status(*statuses: str) -> str:
    applicable = [status for status in statuses if status != "not_applicable"]
    if not applicable:
        return "not_applicable"
    return max(applicable, key=lambda status: STATUS_RANK[status])
