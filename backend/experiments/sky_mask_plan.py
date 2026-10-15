"""Pure post-SfM C-only planning for the temporary sky-mask experiment.

The existing SfM is explicitly unmasked provenance. This plan changes only the
OpenMVS preparation adapter; masks apply to subsequent dense/texture inputs.
It grants no execution authorization, claims no budget, and launches no process.
The frozen Re3D source and the default web three-branch behavior are unchanged.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

from backend.experiments.c_stages import C_STAGE_NAMES, build_c_stage_plan
from backend.experiments.high_profile import require
from backend.experiments.stages import PipelineStagePlan


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _argument(argv: tuple[str, ...], name: str, error: str) -> str:
    require(argv.count(name) == 1, error)
    index = argv.index(name)
    require(index + 1 < len(argv), error)
    return argv[index + 1]


def build_sky_c_plan(
    *, binding_path: Path, binding_sha256: str, pipeline_root: Path,
    experiment_root: Path, dimensions: tuple[int, int], map_python: Path,
    adapter_script: Path, config_path: Path, paths_path: Path,
) -> PipelineStagePlan:
    """Record the frozen eight C stages, replacing only stage 03's argv.

    The caller independently verifies the immutable binding, source handoff and
    preparation artifacts. Planning does not read/alter that handoff, import a
    reconstruction, create markers, reserve resources, or authorize execution.
    Native OpenMVS mask consumption still requires a separately authorized run.
    """
    require(isinstance(binding_sha256, str) and bool(_SHA256.fullmatch(binding_sha256)),
            "SKY_C_PLAN_BINDING_SHA_INVALID")
    require(isinstance(binding_path, Path) and binding_path.is_absolute()
            and ".." not in binding_path.parts,
            "SKY_C_PLAN_BINDING_PATH_INVALID")
    require(isinstance(adapter_script, Path) and adapter_script.is_absolute()
            and ".." not in adapter_script.parts and adapter_script.suffix == ".py",
            "SKY_C_PLAN_ADAPTER_PATH_INVALID")
    plan = build_c_stage_plan(
        pipeline_root=pipeline_root, config_path=config_path, paths_path=paths_path,
        experiment_root=experiment_root, dimensions=dimensions, map_python=map_python,
        openmvs_threads=8, texture_threads=8,
    )
    require(tuple(stage.name for stage in plan.stages) == C_STAGE_NAMES
            and plan.dimensions == tuple(dimensions), "SKY_C_PLAN_FROZEN_STAGES_CHANGED")
    dense = plan.stages[C_STAGE_NAMES.index("11-c-densify")]
    texture = plan.stages[C_STAGE_NAMES.index("13-c-texture")]
    require(_argument(dense.argv, "--mask-path", "SKY_C_PLAN_DENSE_MASK_PATH_CHANGED")
            == str(experiment_root / "work/shared/openmvs_input/images"),
            "SKY_C_PLAN_DENSE_MASK_PATH_CHANGED")
    require(_argument(dense.argv, "--ignore-mask-label", "SKY_C_PLAN_DENSE_MASK_LABEL_CHANGED") == "0",
            "SKY_C_PLAN_DENSE_MASK_LABEL_CHANGED")
    require(_argument(texture.argv, "--ignore-mask-label", "SKY_C_PLAN_TEXTURE_MASK_LABEL_CHANGED") == "0",
            "SKY_C_PLAN_TEXTURE_MASK_LABEL_CHANGED")
    require(_argument(dense.argv, "--max-threads", "SKY_C_PLAN_DENSE_THREADS_CHANGED") == "8"
            and _argument(texture.argv, "--max-threads", "SKY_C_PLAN_TEXTURE_THREADS_CHANGED") == "8",
            "SKY_C_PLAN_THREAD_BUDGET_CHANGED")
    replacement = tuple(str(value) for value in (
        map_python, adapter_script, "--mode", "prepare-openmvs",
        "--binding", binding_path, "--binding-sha256", binding_sha256,
        "--pipeline-root", pipeline_root,
        "--images", experiment_root / "work/input/images",
        "--sparse", experiment_root / "work/shared/colmap/refined_text",
        "--output", experiment_root / "work/shared/openmvs_input",
    ))
    stages = tuple(replace(stage, argv=replacement) if stage.name == "03-prepare-openmvs-input"
                   else stage for stage in plan.stages)
    require(all(stage is original for stage, original in zip(stages, plan.stages)
                if stage.name != "03-prepare-openmvs-input"), "SKY_C_PLAN_NONPREP_STAGE_CHANGED")
    return PipelineStagePlan(stages, plan.environment, plan.dimensions)
