"""No-compute CLI and exact-file verifier-loading regressions."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType
from unittest.mock import Mock

import pytest


REPOSITORY = Path(__file__).absolute().parents[2]


@pytest.fixture
def entry(monkeypatch):
    # The algorithm environment has an unrelated installed ``scripts`` package.
    # Test the actual CLI by file identity, not by importing that namespace.
    monkeypatch.setattr(sys, "path", list(sys.path))
    filename = REPOSITORY / "scripts/run_sky_mask_experiment.py"
    spec = importlib.util.spec_from_file_location("sky_mask_entry_unit", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def forbid_side_effects(monkeypatch, entry):
    forbidden = Mock(side_effect=AssertionError("Preparation/execution side effect is forbidden"))
    for name in ("ordinary", "inspect_masks", "prepare_frozen_dataset", "prepare_frozen_openmvs",
                 "read_json", "write_new", "file_sha", "PreparationGuard", "runtime_verifier",
                 "build_plan", "verify_prepared"):
        monkeypatch.setattr(entry, name, forbidden)
    for name in ("open", "read_bytes", "read_text", "stat", "lstat", "iterdir", "mkdir"):
        monkeypatch.setattr(Path, name, forbidden)
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    return forbidden


@pytest.mark.parametrize("argv", [[], ["--mode", "status"]])
def test_status_is_no_io_no_threads_no_process_no_budget(entry, forbid_side_effects, argv):
    result = entry.main(argv)
    assert result == {"status": "not_executed", "scope": "post_sfm_c_dense_texture_only",
                      "reconstruction_started": False, "execution_authorized": False}
    forbid_side_effects.assert_not_called()


def test_execute_mode_is_not_an_entrypoint(entry, forbid_side_effects):
    with pytest.raises(SystemExit) as error:
        entry.main(["--mode", "execute"])
    assert error.value.code == 2
    forbid_side_effects.assert_not_called()


def test_prepare_requires_explicit_prepare_flag_before_any_io(entry, forbid_side_effects):
    with pytest.raises(entry.SkyMaskError, match="SKY_PREPARE_FLAG_MODE_MISMATCH"):
        entry.main(["--mode", "prepare"])
    forbid_side_effects.assert_not_called()


@pytest.mark.parametrize("mode", ["status", "inspect", "preflight", "prepare-openmvs"])
def test_prepare_flag_rejected_in_other_modes(entry, forbid_side_effects, mode):
    with pytest.raises(entry.SkyMaskError, match="SKY_PREPARE_FLAG_MODE_MISMATCH"):
        entry.main(["--mode", mode, "--prepare-inputs"])
    forbid_side_effects.assert_not_called()


def _shadow_installed_scripts(monkeypatch):
    unrelated = ModuleType("scripts")
    unrelated.__path__ = []
    monkeypatch.setitem(sys.modules, "scripts", unrelated)
    monkeypatch.delitem(sys.modules, "scripts.run_local_c", raising=False)
    return unrelated


def test_runtime_verifier_ignores_shadowing_scripts_package(entry, monkeypatch):
    unrelated = _shadow_installed_scripts(monkeypatch)
    forbidden = Mock(side_effect=AssertionError("Runtime reads or execution are forbidden"))
    # Loading the actual small verifier source is allowed. Any attempt to inspect
    # runtime files through Path, execute a native program, or start a sampler is
    # forbidden; the returned verifier itself must never be called in this test.
    for name in ("open", "read_bytes", "read_text", "stat", "lstat", "iterdir"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    verifier = entry.runtime_verifier()
    assert callable(verifier)
    assert verifier.__name__ == "verify_runtime_manifest"
    assert verifier.__code__.co_filename == str(REPOSITORY / "scripts/run_local_c.py")
    assert verifier.__module__ == "re3d_sky_runtime_verifier"
    assert sys.modules["scripts"] is unrelated
    forbidden.assert_not_called()


def test_runtime_verifier_uses_explicit_repository_file(entry, monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    helper = scripts / "run_local_c.py"
    helper.write_text(
        "def verify_runtime_manifest(*args, **kwargs):\n"
        "    raise AssertionError('Must load only, never run runtime verification')\n",
        encoding="utf-8",
    )
    _shadow_installed_scripts(monkeypatch)
    monkeypatch.setattr(entry, "REPOSITORY", tmp_path)
    verifier = entry.runtime_verifier()
    assert callable(verifier)
    assert verifier.__code__.co_filename == str(helper)
    assert verifier.__module__ == "re3d_sky_runtime_verifier"
