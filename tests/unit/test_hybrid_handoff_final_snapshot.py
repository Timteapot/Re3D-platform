"""Static final-snapshot handoffs never inspect the old work DB or its sidecars."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from backend.experiments import hybrid_handoff as handoff
from backend.experiments.preflight import ExperimentGateError
from backend.experiments.stages import gate_sfm
from tests.unit.test_experiment_checkpoint import document, digest
from tests.unit.test_hybrid_handoff import fixture, repin, verify_arguments


@pytest.fixture
def final(fixture):
    f = fixture
    directory = 'snapshot-20261015T023422082504Z-a613b9f2fc9b47e4a92fe8a5cab00073'
    snapshot = f.root / 'checkpoints' / directory
    snapshot.mkdir(parents=True)
    database = snapshot / 'database.db'
    shutil.copyfile(f.root / 'work/shared/colmap/database.db', database)
    record = {'schema_version': '1.0', 'owner_id': 'e' * 32, 'directory': directory,
        'database_relative_path': directory + '/database.db', 'database_sha256': digest(database),
        'database_size_bytes': database.stat().st_size, 'validated_complete_pair_records': 2,
        'image_count': 300, 'created_at_utc': '2026-10-15T02:34:22.082504+00:00', 'reason': 'terminal',
        'committed_wal_included': True, 'source_write_operations': False, 'sqlite_backup_completed': True,
        'sqlite_integrity_and_mapping_validation': True, 'database_hash_checked_before_and_after_validation': True,
        'sfm_completion_certified': False}
    document(snapshot / 'snapshot-manifest.json', record)
    manifest_sha = digest(snapshot / 'snapshot-manifest.json')
    document(snapshot / 'DONE.json', {'schema_version': '1.0', 'owner_id': record['owner_id'],
        'snapshot_manifest_sha256': manifest_sha, 'database_sha256': record['database_sha256']})
    profile = {'profile_kind': 'local_cpu_sfm_candidate', 'autorun': False,
        'source_job_id': f.source.original.job_id,
        'pipeline': {'commit': f.source.original.pipeline_commit, 'config_sha256': f.case.config_sha,
                     'colmap_script_sha256': f.arguments['colmap_script_sha256']},
        'cpu': {'sfm_threads': 12, 'mapper_ba_threads': 16},
        'runtime': {'python_version': '3.12.14', 'pycolmap_version': '3.13.0', 'pycolmap_has_cuda': False},
        'target_experiment': {'root': str(f.root), 'scene': f.root.name},
        'source_checkpoint': {'original_manifest_sha256': f.source.original.manifest_sha256,
            'original_database_sha256': f.source.original.database_sha256,
            'snapshot_manifest_sha256': f.source.snapshot_manifest_sha256,
            'snapshot_database_sha256': f.source.selected_database_sha256}}
    profile_path = f.root / 'reports/effective-cpu-profile.json'
    document(profile_path, profile)
    profile_sha = digest(profile_path)
    f.options.update(profile_sha256=profile_sha, pipeline_script_sha256=f.arguments['pipeline_script_sha256'])
    document(f.root / 'reports/sfm-actual-options.json', f.options)
    f.report.update(schema_version='1.0', final_snapshot_status='valid',
        final_snapshot={**record, 'manifest_sha256': manifest_sha}, observation_relay_stopped=True,
        profile_sha256=profile_sha, pipeline_commit=f.source.original.pipeline_commit,
        config_sha256=f.case.config_sha, source_manifest_sha256=f.source.original.manifest_sha256,
        source_database_sha256=f.source.selected_database_sha256, source_job_id=f.source.original.job_id,
        sfm_threads=12, mapper_ba_threads=16, final_pair_counts={'complete_pair_records': 2})
    report_path = f.root / 'reports/cpu-result.json'
    document(report_path, f.report)
    arguments = {**f.arguments, 'final_snapshot_root': snapshot, 'expected_cpu_result_sha256': digest(report_path)}
    yield SimpleNamespace(f=f, snapshot=snapshot, database=database, record=record,
        profile=profile, report=f.report, report_path=report_path, arguments=arguments)


def repin_result(final):
    document(final.report_path, final.report)
    final.arguments['expected_cpu_result_sha256'] = digest(final.report_path)


def test_final_snapshot_export_verify_import_ignores_work_db_wal_shm(final, monkeypatch):
    f = final.f
    working = f.root / 'work/shared/colmap/database.db'
    forbidden = {working, working.with_name(working.name + '-wal'), working.with_name(working.name + '-shm')}
    for path in forbidden:
        path.write_bytes(b'old unreadable work files must remain byte-for-byte untouched')
    original_open, original_lstat = Path.open, Path.lstat
    def guarded_open(path, *args, **kwargs):
        assert path not in forbidden, 'old working database or sidecar was opened'
        return original_open(path, *args, **kwargs)
    def guarded_lstat(path, *args, **kwargs):
        assert path not in forbidden, 'old working database or sidecar was probed'
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded_open)
    monkeypatch.setattr(Path, 'lstat', guarded_lstat)
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **final.arguments)
    assert saved.manifest['database_source']['kind'] == 'validated_final_snapshot'
    assert saved.manifest['database_source']['cpu_result_sha256'] == final.arguments['expected_cpu_result_sha256']
    assert len(saved.manifest['files']) == 18
    target = f.case.experiments / 'static-import'
    imported = handoff.import_sfm_handoff(saved, target, input_source=f.source)
    assert imported.sfm_gate == f.gate and imported.first_stage == '02-extract-cameras'
    assert digest(imported.database_path) == final.record['database_sha256']
    for path in forbidden:
        with original_open(path, 'rb') as stream:
            assert stream.read() == b'old unreadable work files must remain byte-for-byte untouched'


@pytest.mark.parametrize('mutation,code', [
    ('bad_report_pin', 'CPU_RESULT_HASH_MISMATCH'), ('missing_pin', 'ARGUMENTS_REQUIRED'),
    ('outside', 'ROOT_INVALID'), ('wrong_name', 'ROOT_INVALID'),
    ('not_final', 'SNAPSHOT_UNCONFIRMED'), ('periodic', 'SNAPSHOT_IDENTITY_INVALID'),
    ('incomplete', 'INVENTORY_INVALID'), ('sidecar', 'INVENTORY_INVALID'),
    ('done_changed', 'DONE_IDENTITY_INVALID'), ('database_changed', 'DATABASE_HASH_MISMATCH'),
    ('profile_changed', 'EVIDENCE_CHANGED'), ('source_changed', 'CPU_RESULT_UNCONFIRMED'),
    ('proof_false', 'CPU_RESULT_UNCONFIRMED'), ('threads', 'CPU_RESULT_UNCONFIRMED'),
])
def test_final_snapshot_requires_terminal_pinned_evidence_before_creating_bundle(final, mutation, code):
    f = final.f
    if mutation == 'bad_report_pin':
        final.arguments['expected_cpu_result_sha256'] = '0' * 64
    elif mutation == 'missing_pin':
        final.arguments['expected_cpu_result_sha256'] = None
    elif mutation in {'outside', 'wrong_name'}:
        target = f.case.base / final.snapshot.name if mutation == 'outside' else final.snapshot.parent / 'latest-valid'
        shutil.copytree(final.snapshot, target)
        final.arguments['final_snapshot_root'] = target
    elif mutation == 'not_final':
        final.report['final_snapshot_status'] = 'failed'; repin_result(final)
    elif mutation == 'periodic':
        final.record['reason'] = 'periodic'
        document(final.snapshot / 'snapshot-manifest.json', final.record)
        sha = digest(final.snapshot / 'snapshot-manifest.json')
        document(final.snapshot / 'DONE.json', {'schema_version': '1.0', 'owner_id': final.record['owner_id'],
            'snapshot_manifest_sha256': sha, 'database_sha256': final.record['database_sha256']})
        final.report['final_snapshot'] = {**final.record, 'manifest_sha256': sha}; repin_result(final)
    elif mutation in {'incomplete', 'sidecar'}:
        (final.snapshot / ('INCOMPLETE.json' if mutation == 'incomplete' else 'database.db-wal')).write_bytes(b'bad')
    elif mutation == 'done_changed':
        document(final.snapshot / 'DONE.json', {'schema_version': '1.0'})
    elif mutation == 'database_changed':
        data = bytearray(final.database.read_bytes()); data[-1] ^= 1
        final.database.write_bytes(data)
    elif mutation == 'profile_changed':
        path = f.root / 'reports/effective-cpu-profile.json'
        path.write_bytes(path.read_bytes() + b'\n')
    else:
        if mutation == 'source_changed': final.report['source_database_sha256'] = '0' * 64
        if mutation == 'proof_false': final.report['observation_relay_stopped'] = False
        if mutation == 'threads': final.report['mapper_ba_threads'] = 24
        repin_result(final)
    with pytest.raises(ValueError, match=code):
        handoff.export_sfm_handoff(f.root, f.bundle, **final.arguments)
    assert not f.bundle.exists()


def test_snapshot_receive_never_follows_cpu_profile_original_machine_paths(final, monkeypatch):
    f = final.f
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **final.arguments)
    # These original-machine evidence files are intentionally absent at receive.
    shutil.rmtree(final.snapshot)
    final.report_path.unlink()
    (f.root / 'reports/effective-cpu-profile.json').unlink()
    original_open = Path.open
    def only_payload_and_input(path, *args, **kwargs):
        assert not path.is_relative_to(f.root), 'receiver followed source-machine evidence path'
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', only_payload_and_input)
    assert handoff.verify_sfm_handoff(f.bundle, saved.manifest_sha256, **verify_arguments(f)).manifest == saved.manifest


def test_snapshot_profile_path_forward_slashes_preserve_windows_identity(final):
    f = final.f
    final.profile['target_experiment']['root'] = f.root.as_posix()
    profile_path = f.root / 'reports/effective-cpu-profile.json'
    document(profile_path, final.profile)
    profile_sha = digest(profile_path)
    f.options['profile_sha256'] = profile_sha
    document(f.root / 'reports/sfm-actual-options.json', f.options)
    final.report['profile_sha256'] = profile_sha
    repin_result(final)
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **final.arguments)
    assert saved.manifest['database_source']['cpu_profile_sha256'] == profile_sha


@pytest.mark.parametrize('pipeline_field', [None, '0' * 64], ids=['genuine_cpu_producer_omits_field', 'wrong_explicit_field_rejected'])
def test_final_snapshot_respects_actual_cpu_options_pipeline_field_contract(final, pipeline_field):
    f = final.f
    f.options.pop('pipeline_script_sha256', None)
    if pipeline_field is not None:
        f.options['pipeline_script_sha256'] = pipeline_field
    document(f.root / 'reports/sfm-actual-options.json', f.options)
    if pipeline_field is None:
        saved = handoff.export_sfm_handoff(f.root, f.bundle, **final.arguments)
        assert handoff.verify_sfm_handoff(f.bundle, saved.manifest_sha256, **verify_arguments(f)).manifest == saved.manifest
        assert saved.manifest['scripts']['run_pipeline_sha256'] == f.arguments['pipeline_script_sha256']
    else:
        with pytest.raises(handoff.HandoffError, match='FINAL_CPU_PROFILE_MISMATCH'):
            handoff.export_sfm_handoff(f.root, f.bundle, **final.arguments)
        assert not f.bundle.exists()


@pytest.mark.parametrize('mutation', ['provenance_extra', 'cpu_pin', 'db_pin', 'profile', 'missing_evidence', 'legacy_with_evidence'])
def test_snapshot_receive_rejects_rehashed_provenance_or_evidence_tampering(final, mutation):
    f = final.f
    saved = handoff.export_sfm_handoff(f.root, f.bundle, **final.arguments)
    manifest = json.loads((f.bundle / handoff.MANIFEST).read_bytes())
    if mutation == 'provenance_extra': manifest['database_source']['path'] = str(final.database)
    if mutation == 'cpu_pin': manifest['database_source']['cpu_result_sha256'] = '0' * 64
    if mutation == 'db_pin': manifest['database_source']['database_sha256'] = '0' * 64
    if mutation == 'missing_evidence':
        manifest['files'] = [row for row in manifest['files'] if not row['relative_path'].endswith('/cpu-result.json')]
    if mutation == 'legacy_with_evidence': del manifest['database_source']
    if mutation == 'profile':
        relative = handoff._SNAPSHOT_PREFIX + 'effective-cpu-profile.json'
        path = f.bundle / 'payload' / relative
        profile = json.loads(path.read_bytes()); profile['cpu']['mapper_ba_threads'] = 24; document(path, profile)
        row = next(row for row in manifest['files'] if row['relative_path'] == relative)
        row.update(sha256=digest(path), size_bytes=path.stat().st_size)
        manifest['database_source']['cpu_profile_sha256'] = digest(path)
    document(f.bundle / handoff.MANIFEST, manifest)
    sha = repin(f.bundle)
    with pytest.raises(ValueError):
        handoff.verify_sfm_handoff(f.bundle, sha, **verify_arguments(f))


def test_gate_explicit_static_database_uses_immutable_ro_and_rejects_escape(final, monkeypatch):
    f = final.f
    original = sqlite3.connect
    observed = []
    def connect(database_uri, *args, **kwargs):
        observed.append(database_uri)
        return original(database_uri, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    assert gate_sfm(f.root, f.source.image_names, expected_mapper_threads=16, database_path=final.database) == f.gate
    assert observed == [final.database.as_uri() + '?mode=ro&immutable=1']
    with pytest.raises(ExperimentGateError, match='OUTSIDE_ROOT'):
        gate_sfm(f.root, f.source.image_names, expected_mapper_threads=16, database_path=f.case.database)
    (final.snapshot / 'database.db-shm').write_bytes(b'not static')
    with pytest.raises(ExperimentGateError, match='STATIC_DATABASE_SIDECAR'):
        gate_sfm(f.root, f.source.image_names, expected_mapper_threads=16, database_path=final.database)
