from __future__ import annotations

import pytest
from backend.experiments import hybrid_handoff as handoff
from tests.unit.test_hybrid_handoff import fixture
from tests.unit.test_experiment_checkpoint import document


def test_supervised_import_accepts_only_reserved_root(fixture):
    f = fixture
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    target = f.case.experiments / 'monitored-sfm-import'
    (target/'reports').mkdir(parents=True)
    document(target/'operation-reservation.json', {'kind':'independent_long_pipeline'})
    (target/'reports/resource-samples.jsonl').write_text('')
    imported = handoff.import_sfm_handoff(saved, target, input_source=f.source, target_work_root=target/'work')
    assert imported.first_stage == '02-extract-cameras'
    assert imported.provenance_path.is_file()


def test_supervised_import_rejects_preexisting_work_or_extra_files(fixture):
    f = fixture
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **f.arguments)
    target = f.case.experiments / 'must-not-overwrite'
    target.mkdir(); (target/'old-result.obj').write_text('preserve')
    with pytest.raises(Exception, match='MONITOR_ROOT_NOT_EMPTY'):
        handoff.import_sfm_handoff(saved, target, input_source=f.source, target_work_root=target/'work')
    assert (target/'old-result.obj').read_text() == 'preserve'
