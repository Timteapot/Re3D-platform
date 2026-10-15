from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.experiments import sky_mask_plan as sky
from backend.experiments.c_stages import C_STAGE_NAMES
from backend.experiments.preflight import ExperimentGateError
from backend.experiments.stages import PipelineStagePlan, StageSpec


@pytest.fixture
def planned(tmp_path, monkeypatch):
    root = tmp_path / "_experiments" / "synthetic-sky-c"
    inputs = {"binding_path": tmp_path / "binding.json", "binding_sha256": "a" * 64,
              "pipeline_root": tmp_path / "frozen", "experiment_root": root,
              "dimensions": (2048, 1080), "map_python": tmp_path / "map-python.exe",
              "adapter_script": tmp_path / "run_sky_mask_experiment.py",
              "config_path": tmp_path / "pipeline.json", "paths_path": tmp_path / "paths.json"}
    stages = []
    for name in C_STAGE_NAMES:
        argv = [str(inputs["map_python"]), name]
        if name == "11-c-densify":
            argv.extend(("--mask-path", str(root / "work/shared/openmvs_input/images"),
                         "--ignore-mask-label", "0", "--max-threads", "8"))
        if name == "13-c-texture":
            argv.extend(("--ignore-mask-label", "0", "--max-threads", "8"))
        stages.append(StageSpec(name, tuple(argv), root / ("marker-" + name),
                                inputs["pipeline_root"], (root / "owned-dir",)))
    baseline = PipelineStagePlan(tuple(stages), {"HF_HUB_OFFLINE": "1"}, (2048, 1080))
    builder = Mock(return_value=baseline)
    monkeypatch.setattr(sky, "build_c_stage_plan", builder)
    return inputs, baseline, builder


def test_only_prepare_argv_changes_without_files_or_processes(planned, monkeypatch, tmp_path):
    import subprocess

    inputs, baseline, builder = planned
    process = Mock(side_effect=AssertionError("No process may start while planning"))
    monkeypatch.setattr(subprocess, "Popen", process)
    result = sky.build_sky_c_plan(**inputs)
    builder.assert_called_once_with(
        pipeline_root=inputs["pipeline_root"], config_path=inputs["config_path"],
        paths_path=inputs["paths_path"], experiment_root=inputs["experiment_root"],
        dimensions=inputs["dimensions"], map_python=inputs["map_python"],
        openmvs_threads=8, texture_threads=8,
    )
    assert tuple(stage.name for stage in result.stages) == C_STAGE_NAMES
    assert result.environment is baseline.environment
    assert result.dimensions == baseline.dimensions
    for before, after in zip(baseline.stages, result.stages):
        if before.name != "03-prepare-openmvs-input":
            assert before is after
        else:
            assert replace(after, argv=before.argv) == before
            root = inputs["experiment_root"]
            assert after.argv == tuple(str(value) for value in (
                inputs["map_python"], inputs["adapter_script"], "--mode", "prepare-openmvs",
                "--binding", inputs["binding_path"], "--binding-sha256", "a" * 64,
                "--pipeline-root", inputs["pipeline_root"],
                "--images", root / "work/input/images",
                "--sparse", root / "work/shared/colmap/refined_text",
                "--output", root / "work/shared/openmvs_input",
            ))
    process.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage_name,flag,replacement,error", [
    ("11-c-densify", "--mask-path", "not-the-experiment", "DENSE_MASK_PATH"),
    ("11-c-densify", "--ignore-mask-label", "255", "DENSE_MASK_LABEL"),
    ("13-c-texture", "--ignore-mask-label", "-2", "TEXTURE_MASK_LABEL"),
    ("11-c-densify", "--max-threads", "24", "THREAD_BUDGET"),
    ("13-c-texture", "--max-threads", "24", "THREAD_BUDGET"),
])
def test_rejects_changed_mask_or_thread_contract(planned, stage_name, flag, replacement, error):
    inputs, baseline, builder = planned
    stages = list(baseline.stages)
    index = C_STAGE_NAMES.index(stage_name)
    argv = list(stages[index].argv)
    argv[argv.index(flag) + 1] = replacement
    stages[index] = replace(stages[index], argv=tuple(argv))
    builder.return_value = replace(baseline, stages=tuple(stages))
    with pytest.raises(ExperimentGateError, match=error):
        sky.build_sky_c_plan(**inputs)


@pytest.mark.parametrize("change", ["missing", "reordered", "extra-sfm"])
def test_rejects_nonfrozen_stage_sequence(planned, change):
    inputs, baseline, builder = planned
    stages = list(baseline.stages)
    if change == "missing":
        stages.pop()
    elif change == "reordered":
        stages[0], stages[1] = stages[1], stages[0]
    else:
        stages.insert(0, replace(stages[0], name="01-colmap"))
    builder.return_value = replace(baseline, stages=tuple(stages))
    with pytest.raises(ExperimentGateError, match="FROZEN_STAGES_CHANGED"):
        sky.build_sky_c_plan(**inputs)


@pytest.mark.parametrize("field,value,error", [
    ("binding_sha256", "not-a-sha", "BINDING_SHA"),
    ("binding_path", Path("relative.json"), "BINDING_PATH"),
    ("adapter_script", Path("relative.py"), "ADAPTER_PATH"),
])
def test_invalid_binding_or_adapter_rejected_before_planning(planned, field, value, error):
    inputs, _, builder = planned
    inputs[field] = value
    with pytest.raises(ExperimentGateError, match=error):
        sky.build_sky_c_plan(**inputs)
    builder.assert_not_called()
