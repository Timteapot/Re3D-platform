"""Synthetic receipts only: no private ledger, UAC, server or reconstruction."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

import backend.experiments.c_budget_chain as ledger
from backend.experiments.supervision import SupervisionError

CPU, PROFILE, TERMINAL = "a" * 64, "b" * 64, "c" * 64
SOURCE = {"original_total_seconds": 14400, "consumed_sfm_seconds": 4920.218,
          "remaining_seconds": 9479.782}


def document(value):
    raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    return ledger.parse_budget_document(raw, hashlib.sha256(raw).hexdigest())


def original():
    claim = {"schema_version": "1.0", "mode": "local-c-only", "cpu_result_sha256": CPU,
        "profile_sha256": PROFILE, "scene": "original-c", "remaining_seconds_at_admission": 9479.779,
        "created_at_utc": "2026-10-15T10:10:21+00:00", "automatic_retry_allowed": False}
    charge = {"schema_version": "1.0", "mode": "local-c-only", "status": "failed",
        "profile_sha256": PROFILE, "prior_sfm_charge_seconds": 4920.218,
        "elapsed_seconds": 2000.123, "remaining_budget_seconds": 7479.659,
        "cumulative_charge_seconds": 6920.341, "automatic_retry_allowed": False}
    return document(claim), document(charge)


def reconcile(root=None, charge=None, **kwargs):
    first, last = original()
    return ledger.reconcile_c_budget(source_budget=SOURCE, cpu_result_sha256=CPU,
        root_profile_sha256=PROFILE, source_terminal_receipt_sha256=TERMINAL,
        root_claim=root or first, root_charge=charge or last, **kwargs)


@pytest.fixture
def directory(tmp_path_factory):
    # Immutable SHA names must not inherit a long parameterized node basename.
    return tmp_path_factory.mktemp("bc")


def seed(directory):
    claim, charge = original()
    (directory / f"c-budget-{CPU}.json").write_bytes(claim.raw)
    (directory / f"c-budget-{CPU}-charge.json").write_bytes(charge.raw)
    return claim, charge


def read(directory, **kwargs):
    claim, charge = original()
    return ledger.read_c_budget_chain(directory, source_budget=SOURCE, cpu_result_sha256=CPU,
        root_profile_sha256=PROFILE, source_terminal_receipt_sha256=TERMINAL,
        root_claim_sha256=claim.sha256, root_charge_sha256=charge.sha256, **kwargs)


def append_attempt(directory, proof, *, scene="sky-c-one", spent=50, profile="d" * 64):
    path, claim = ledger.claim_c_continuation_budget(directory, proof,
        source_budget=SOURCE, profile_sha256=profile, scene=scene)
    _, charge = ledger.write_c_continuation_charge(path, claim,
        elapsed_seconds=spent, remaining_seconds=proof["remaining_seconds"] - spent,
        status="succeeded")
    return path, claim, charge


def test_exact_original_bytes_pinned_without_reserialization(directory):
    raw = b'{  "value" : 5 }\r\n'
    sha = hashlib.sha256(raw).hexdigest()
    parsed = ledger.parse_budget_document(raw, sha)
    assert parsed.data == {"value": 5} and parsed.raw == raw and parsed.size_bytes == len(raw)
    path = directory / "receipt.json"
    path.write_bytes(raw)
    assert ledger.read_budget_document(path, sha) == parsed
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_DOCUMENT_CHANGED"):
        ledger.read_budget_document(path, "0" * 64)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                                    b'[]', b'{', b'{}' + b' ' * 65536],
                         ids=["duplicate", "nan", "infinity", "array", "invalid", "oversize"])
def test_bad_or_oversize_json_is_rejected(raw):
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_DOCUMENT_INVALID"):
        ledger.parse_budget_document(raw, hashlib.sha256(raw).hexdigest())


def test_pure_reconciliation_preserves_prior_consumption_and_creates_nothing(directory):
    before = list(directory.iterdir())
    result = reconcile()
    assert result["consumed_sfm_seconds"] == 4920.218
    assert result["consumed_c_seconds"] == 2000.123
    assert result["cumulative_charge_seconds"] == 6920.341
    assert result["remaining_seconds"] == 7479.659
    assert result["next_sequence"] == 1 and result["complete_closed_chain"]
    assert not result["budget_claimed"] and not result["budget_charged"]
    assert list(directory.iterdir()) == before


def test_millisecond_disagreement_is_accepted_but_never_replenished():
    _, charge = original()
    value = charge.data
    value.update(elapsed_seconds=2000.1231, cumulative_charge_seconds=6920.3414,
                 remaining_budget_seconds=7479.6585)
    result = reconcile(charge=document(value))
    assert result["cumulative_charge_seconds"] >= 6920.3415 - 1e-10
    assert result["remaining_seconds"] <= 7479.6585


@pytest.mark.parametrize("field,value", [
    ("elapsed_seconds", -1), ("remaining_budget_seconds", -1),
    ("elapsed_seconds", True), ("prior_sfm_charge_seconds", "4920.218"),
    ("prior_sfm_charge_seconds", 4900), ("remaining_budget_seconds", 9999),
    ("cumulative_charge_seconds", 6921), ("elapsed_seconds", 1),
    ("profile_sha256", "e" * 64), ("automatic_retry_allowed", True),
    ("status", "running"), ("status", "incomplete")])
def test_original_charge_rollback_bad_numbers_or_unclosed_status(field, value):
    _, charge = original()
    changed = charge.data
    changed[field] = value
    with pytest.raises(SupervisionError):
        reconcile(charge=document(changed))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_source_nonfinite_negative_or_boolean_cannot_reset_budget(value):
    claim, charge = original()
    source = {**SOURCE, "remaining_seconds": value}
    with pytest.raises(SupervisionError):
        ledger.reconcile_c_budget(source_budget=source, cpu_result_sha256=CPU,
            root_profile_sha256=PROFILE, source_terminal_receipt_sha256=TERMINAL,
            root_claim=claim, root_charge=charge)


@pytest.mark.parametrize("field,value", [("remaining_seconds_at_admission", 10000),
    ("cpu_result_sha256", "e" * 64), ("profile_sha256", "e" * 64),
    ("scene", "../same-scene"), ("automatic_retry_allowed", True)])
def test_original_claim_identity_or_extra_allowance_rejected(field, value):
    claim, _ = original()
    changed = claim.data
    changed[field] = value
    with pytest.raises(SupervisionError):
        reconcile(root=document(changed))


def test_read_preflight_does_not_claim_or_change_historical_bytes(directory):
    seed(directory)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    result = read(directory)
    assert result == reconcile()
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_continuation_chain_spends_once_and_keeps_old_receipts(directory):
    seed(directory)
    previous = {path.name: path.read_bytes() for path in directory.iterdir()}
    first = read(directory)
    append_attempt(directory, first)
    second = read(directory)
    assert second["next_sequence"] == 2
    assert second["cumulative_charge_seconds"] >= first["cumulative_charge_seconds"] + 50
    assert second["remaining_seconds"] <= first["remaining_seconds"] - 50
    assert len(second["evidence"]) == 2
    for name, raw in previous.items():
        assert (directory / name).read_bytes() == raw
    append_attempt(directory, second, scene="sky-c-two", spent=30, profile="e" * 64)
    third = read(directory)
    assert third["next_sequence"] == 3 and len(third["evidence"]) == 3
    assert third["remaining_seconds"] <= first["remaining_seconds"] - 80


def test_a_stale_root_or_latest_tip_pin_cannot_hide_later_spend(directory):
    seed(directory)
    previous = read(directory)
    append_attempt(directory, previous)
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_STALE_TIP"):
        read(directory, expected_tip_sha256=previous["parent_charge_sha256"])
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_STALE_TIP"):
        ledger.claim_c_continuation_budget(directory, previous, source_budget=SOURCE,
            profile_sha256="e" * 64, scene="sky-c-two")


def test_unclosed_claim_blocks_every_further_attempt(directory):
    seed(directory)
    prior = read(directory)
    ledger.claim_c_continuation_budget(directory, prior, source_budget=SOURCE,
        profile_sha256="d" * 64, scene="sky-c-one")
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_CHARGE_REQUIRED"):
        read(directory)
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_CHARGE_REQUIRED"):
        ledger.claim_c_continuation_budget(directory, prior, source_budget=SOURCE,
            profile_sha256="e" * 64, scene="sky-c-two")


@pytest.mark.parametrize("name,code", [
    (f"c-budget-{CPU}-other.json", "LOCAL_C_BUDGET_FORK_OR_UNKNOWN_ENTRY"),
    (f"c-budget-{CPU}-continuation-000002.json", "LOCAL_C_BUDGET_CHAIN_GAP"),
    (f"c-budget-{CPU}-continuation-000001-charge.json", "LOCAL_C_BUDGET_CHARGE_REQUIRED"),
    (f"c-budget-{CPU}-continuation-000000.json", "LOCAL_C_BUDGET_CHAIN_GAP")])
def test_unknown_fork_gap_and_orphan_entries_fail_closed(directory, name, code):
    seed(directory)
    (directory / name).write_bytes(b'{}\n')
    with pytest.raises(SupervisionError, match=code):
        read(directory)


@pytest.mark.parametrize("which,field,value", [
    (0, "sequence", 2), (0, "parent_charge_sha256", "0" * 64),
    (0, "cpu_result_sha256", "0" * 64), (0, "source_terminal_receipt_sha256", "0" * 64),
    (0, "root_claim_sha256", "0" * 64), (0, "scene", "original-c"),
    (1, "profile_sha256", "0" * 64), (1, "claim_sha256", "0" * 64),
    (1, "cumulative_charge_before_seconds", 0), (1, "remaining_budget_seconds", 9000),
    (1, "status", "running")])
def test_pure_chain_rejects_source_profile_link_rollback_or_reused_scene(directory, which, field, value):
    seed(directory)
    proof = read(directory)
    _, claim, charge = append_attempt(directory, proof)
    pair = [claim, charge]
    changed = pair[which].data
    changed[field] = value
    pair[which] = document(changed)
    with pytest.raises(SupervisionError):
        reconcile(continuations=[pair])


def test_two_racing_claimants_cannot_fork(directory):
    seed(directory)
    proof = read(directory)
    def claimant(scene):
        try:
            return ledger.claim_c_continuation_budget(directory, proof, source_budget=SOURCE,
                profile_sha256="d" * 64, scene=scene)[0]
        except SupervisionError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claimant, ["first-racer", "second-racer"]))
    assert sum(value is not None for value in results) == 1
    assert len(list(directory.glob("*-continuation-*.json"))) == 1


def test_existing_charge_is_never_overwritten(directory):
    seed(directory)
    path, claim, charge = append_attempt(directory, read(directory))
    charge_path = path.with_name(path.stem + "-charge.json")
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_ALREADY_CLAIMED"):
        ledger.write_c_continuation_charge(path, claim, elapsed_seconds=10,
            remaining_seconds=1, status="failed")
    assert charge_path.read_bytes() == charge.raw


def test_modified_proof_cannot_grant_more_budget(directory):
    seed(directory)
    proof = read(directory)
    proof["remaining_seconds"] += 10
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_PROOF_CHANGED"):
        ledger.claim_c_continuation_budget(directory, proof, source_budget=SOURCE,
            profile_sha256="d" * 64, scene="sky-c-one")


def test_admission_spend_cannot_be_recovered_by_next_attempt(directory):
    seed(directory)
    proof = read(directory)
    path, claim = ledger.claim_c_continuation_budget(directory, proof, source_budget=SOURCE,
        profile_sha256="d" * 64, scene="sky-c-one", remaining_seconds=proof["remaining_seconds"] - 2)
    ledger.write_c_continuation_charge(path, claim, elapsed_seconds=50,
        remaining_seconds=proof["remaining_seconds"] - 52, status="failed")
    result = read(directory)
    assert result["remaining_seconds"] <= proof["remaining_seconds"] - 52
    assert result["cumulative_charge_seconds"] >= proof["cumulative_charge_seconds"] + 52


def test_zero_remaining_is_terminal_not_a_new_budget():
    _, charge = original()
    value = charge.data
    value.update(status="timed_out", elapsed_seconds=9480, remaining_budget_seconds=0,
                 cumulative_charge_seconds=14400.218)
    result = reconcile(charge=document(value))
    assert result["remaining_seconds"] == 0 and result["cumulative_charge_seconds"] >= 14400


def test_exact_source_identity_pin_is_required(directory):
    seed(directory)
    claim, charge = original()
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_DOCUMENT_CHANGED"):
        ledger.read_c_budget_chain(directory, source_budget=SOURCE, cpu_result_sha256=CPU,
            root_profile_sha256=PROFILE, source_terminal_receipt_sha256=TERMINAL,
            root_claim_sha256=claim.sha256, root_charge_sha256="0" * 64)


def test_read_detects_file_change_during_read(directory, monkeypatch):
    path = directory / "mutable.json"
    path.write_bytes(b'{"value":1}\n')
    original_identity = ledger._identity
    calls = 0
    def changed(info):
        nonlocal calls
        calls += 1
        value = original_identity(info)
        return (*value[:-1], value[-1] + 1) if calls >= 3 else value
    monkeypatch.setattr(ledger, "_identity", changed)
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_DOCUMENT_CHANGED"):
        ledger.read_budget_document(path)


def test_whole_chain_recheck_detects_earlier_receipt_changes(directory, monkeypatch):
    seed(directory)
    original = ledger._inventory_identities
    count = 0
    def changed(parent, names):
        nonlocal count
        count += 1
        value = original(parent, names)
        if count > 1:
            first = next(iter(value))
            identity, change_time = value[first]
            value[first] = (identity, change_time + 1)
        return value
    monkeypatch.setattr(ledger, "_inventory_identities", changed)
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_CHAIN_CHANGED"):
        read(directory)


def test_alternate_claim_name_cannot_produce_a_fork_charge(directory):
    seed(directory)
    path, claim = ledger.claim_c_continuation_budget(directory, read(directory),
        source_budget=SOURCE, profile_sha256="d" * 64, scene="sky-c-one")
    forged_path = directory / "different-lineage.json"
    forged_path.write_bytes(claim.raw)
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_CHAIN_LINK_MISMATCH"):
        ledger.write_c_continuation_charge(forged_path, claim,
            elapsed_seconds=1, remaining_seconds=1, status="failed")
    assert not (directory / "different-lineage-charge.json").exists()


def test_increasing_current_claim_allowance_is_rejected(directory):
    seed(directory)
    proof = read(directory)
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_ROLLBACK"):
        ledger.claim_c_continuation_budget(directory, proof, source_budget=SOURCE,
            profile_sha256="d" * 64, scene="sky-c-one",
            remaining_seconds=proof["remaining_seconds"] + .000001)


def test_claim_may_not_reuse_a_prior_scene(directory):
    seed(directory)
    with pytest.raises(SupervisionError, match="LOCAL_C_BUDGET_CLAIM_INVALID"):
        ledger.claim_c_continuation_budget(directory, read(directory), source_budget=SOURCE,
            profile_sha256="d" * 64, scene="original-c")


def test_no_continuation_can_start_when_budget_is_exhausted(directory):
    claim, charge = original()
    value = charge.data
    value.update(status="timed_out", elapsed_seconds=9480, remaining_budget_seconds=0,
                 cumulative_charge_seconds=14400.218)
    charge = document(value)
    (directory / f"c-budget-{CPU}.json").write_bytes(claim.raw)
    (directory / f"c-budget-{CPU}-charge.json").write_bytes(charge.raw)
    proof = ledger.read_c_budget_chain(directory, source_budget=SOURCE, cpu_result_sha256=CPU,
        root_profile_sha256=PROFILE, source_terminal_receipt_sha256=TERMINAL,
        root_claim_sha256=claim.sha256, root_charge_sha256=charge.sha256)
    with pytest.raises(SupervisionError, match="LONG_BUDGET_EXHAUSTED"):
        ledger.claim_c_continuation_budget(directory, proof, source_budget=SOURCE,
            profile_sha256="d" * 64, scene="sky-c-one")
    assert len(list(directory.iterdir())) == 2
