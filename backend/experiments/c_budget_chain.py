"""Append-only local C continuation accounting; no execution or elevation.

The historical one-shot claim is left intact. A continuation needs its closed
claim/charge pair and *every* later canonical pair in the same protected ledger.
Preflight only reads; the explicit claim helper is reserved for execution.
Byte pins authenticate identity only relative to the trusted caller/ledger ACL,
not by a new cryptographic signing key. A proof digest is not a signature.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from backend.experiments.cpu_runner import ordinary
from backend.experiments.supervision import SupervisionError

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SCENE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_TOTAL = Decimal("14400")
_EPS = Decimal("0.001")
_MAX_BYTES = 64 * 1024
_TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}


def _require(value, code="LOCAL_C_BUDGET_CHAIN_INVALID"):
    if not value:
        raise SupervisionError(code)


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _number(value):
    _require(type(value) in (int, float), "LOCAL_C_BUDGET_NUMBER_INVALID")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise SupervisionError("LOCAL_C_BUDGET_NUMBER_INVALID") from None
    _require(result.is_finite() and result >= 0, "LOCAL_C_BUDGET_NUMBER_INVALID")
    return result


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("ascii")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "LOCAL_C_BUDGET_DOCUMENT_INVALID")
        result[key] = value
    return result


@dataclass(frozen=True)
class PinnedBudgetDocument:
    """Exact original bytes, never a reserialization of a historical receipt."""
    raw: bytes
    sha256: str

    @property
    def size_bytes(self):
        return len(self.raw)

    @property
    def data(self):
        try:
            value = json.loads(self.raw.decode("utf-8"), object_pairs_hook=_unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise SupervisionError("LOCAL_C_BUDGET_DOCUMENT_INVALID") from None
        _require(type(value) is dict, "LOCAL_C_BUDGET_DOCUMENT_INVALID")
        return value


def parse_budget_document(raw, expected_sha256):
    _require(type(raw) is bytes and 0 < len(raw) <= _MAX_BYTES and _sha(expected_sha256),
             "LOCAL_C_BUDGET_DOCUMENT_INVALID")
    _require(hashlib.sha256(raw).hexdigest() == expected_sha256,
             "LOCAL_C_BUDGET_DOCUMENT_CHANGED")
    result = PinnedBudgetDocument(raw, expected_sha256)
    result.data  # Fail on duplicate keys, non-finite constants or non-object JSON.
    return result


def _identity(info):
    # Windows lstat/fstat expose different ctime meanings on some supported
    # Python builds. Compare ctime within each family, not across primitives.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def read_budget_document(path, expected_sha256=None):
    """Bounded, reparse-safe read with exact-byte digest and stable stat proof.

Later chain files acquire their identity from canonical names and protected ACLs;
the root and the optionally pinned latest tip additionally have external pins.
The caller must establish that the ledger parent is its protected authority.
"""
    path = ordinary(Path(path))
    before = path.lstat()
    before_identity = _identity(before)
    _require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= _MAX_BYTES,
             "LOCAL_C_BUDGET_DOCUMENT_INVALID")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        _require(before_identity == _identity(opened), "LOCAL_C_BUDGET_DOCUMENT_CHANGED")
        raw = stream.read(_MAX_BYTES + 1)
        closed = os.fstat(stream.fileno())
    after = path.lstat()
    _require(before_identity == _identity(closed) == _identity(after)
             and before.st_ctime_ns == after.st_ctime_ns
             and opened.st_ctime_ns == closed.st_ctime_ns,
             "LOCAL_C_BUDGET_DOCUMENT_CHANGED")
    actual = hashlib.sha256(raw).hexdigest()
    return parse_budget_document(raw, actual if expected_sha256 is None else expected_sha256)


def _validated(document):
    _require(type(document) is PinnedBudgetDocument, "LOCAL_C_BUDGET_DOCUMENT_INVALID")
    return parse_budget_document(document.raw, document.sha256).data


def _base_fields(document, profile_sha256):
    _require(document.get("schema_version") == "1.0"
             and document.get("mode") == "local-c-only"
             and document.get("profile_sha256") == profile_sha256
             and document.get("automatic_retry_allowed") is False,
             "LOCAL_C_BUDGET_IDENTITY_MISMATCH")


def _charged_values(charge, *, sfm, before, admitted):
    _require(charge.get("status") in _TERMINAL, "LOCAL_C_BUDGET_CHARGE_NOT_TERMINAL")
    elapsed = _number(charge.get("elapsed_seconds"))
    remaining = _number(charge.get("remaining_budget_seconds"))
    cumulative = _number(charge.get("cumulative_charge_seconds"))
    _require(remaining <= admitted + _EPS and cumulative + _EPS >= before,
             "LOCAL_C_BUDGET_ROLLBACK")
    _require(abs(cumulative - (before + elapsed)) <= _EPS,
             "LOCAL_C_BUDGET_CHARGE_MISMATCH")
    # Legacy runner samples elapsed/remaining/cumulative at slightly different
    # instants. Accept <=1 ms disagreement, but take every conservative bound.
    _require(abs(cumulative + remaining - _TOTAL) <= _EPS
             or (remaining == 0 and cumulative >= _TOTAL - _EPS),
             "LOCAL_C_BUDGET_CHARGE_MISMATCH")
    if "prior_sfm_charge_seconds" in charge:
        _require(abs(_number(charge["prior_sfm_charge_seconds"]) - sfm) <= _EPS,
                 "LOCAL_C_BUDGET_SOURCE_MISMATCH")
    used = max(before + elapsed, cumulative, _TOTAL - remaining, _TOTAL - admitted)
    return used, min(remaining, admitted, max(Decimal(0), _TOTAL - used))


def continuation_names(cpu_result_sha256, sequence):
    _require(_sha(cpu_result_sha256) and type(sequence) is int and 1 <= sequence <= 999999)
    stem = f"c-budget-{cpu_result_sha256}-continuation-{sequence:06d}"
    return stem + ".json", stem + "-charge.json"


def reconcile_c_budget(*, source_budget, cpu_result_sha256, root_profile_sha256,
                       source_terminal_receipt_sha256, root_claim, root_charge,
                       continuations=()):
    """Pure arithmetic and linkage gates; no files, claims or compute started.

``source_budget`` must already be bound to the accepted CPU terminal receipt by
``validate_terminal_binding``. Continuations are ordered ``(claim, charge)``
PinnedBudgetDocument pairs. Use ``read_c_budget_chain`` at actual preflight to
discover all closed entries, rather than supplying an arbitrary stale subset.
"""
    _require(type(source_budget) is dict and _sha(cpu_result_sha256)
             and _sha(root_profile_sha256) and _sha(source_terminal_receipt_sha256))
    total = _number(source_budget.get("original_total_seconds"))
    sfm = _number(source_budget.get("consumed_sfm_seconds"))
    unused = _number(source_budget.get("remaining_seconds"))
    _require(total == _TOTAL and 0 < sfm < total and 0 < unused < total
             and abs(sfm + unused - total) <= _EPS, "LOCAL_C_BUDGET_SOURCE_MISMATCH")
    remaining = min(unused, total - sfm)
    sfm = max(sfm, total - remaining)
    claim, charge = _validated(root_claim), _validated(root_charge)
    _base_fields(claim, root_profile_sha256)
    _base_fields(charge, root_profile_sha256)
    _require(claim.get("cpu_result_sha256") == cpu_result_sha256
             and isinstance(claim.get("scene"), str) and _SCENE.fullmatch(claim["scene"]),
             "LOCAL_C_BUDGET_SOURCE_MISMATCH")
    admitted = _number(claim.get("remaining_seconds_at_admission"))
    _require(0 < admitted <= remaining + _EPS, "LOCAL_C_BUDGET_ROLLBACK")
    _require(abs(_number(charge.get("prior_sfm_charge_seconds")) - sfm) <= _EPS,
             "LOCAL_C_BUDGET_SOURCE_MISMATCH")
    cumulative, remaining = _charged_values(charge, sfm=sfm, before=sfm,
                                             admitted=min(remaining, admitted))
    evidence = [{"sequence": 0, "claim_sha256": root_claim.sha256,
                 "claim_size_bytes": root_claim.size_bytes,
                 "charge_sha256": root_charge.sha256, "charge_size_bytes": root_charge.size_bytes}]
    parent_sha = root_charge.sha256
    seen_scenes = {claim["scene"]}
    for sequence, pair in enumerate(continuations, 1):
        _require(type(pair) in (tuple, list) and len(pair) == 2)
        claim_document, charge_document = pair
        claim, charge = _validated(claim_document), _validated(charge_document)
        profile_sha = claim.get("profile_sha256")
        _require(_sha(profile_sha))
        _base_fields(claim, profile_sha)
        _base_fields(charge, profile_sha)
        _require(isinstance(claim.get("scene"), str) and _SCENE.fullmatch(claim["scene"])
                 and claim["scene"] not in seen_scenes, "LOCAL_C_BUDGET_SCENE_REUSED")
        seen_scenes.add(claim["scene"])
        for row in (claim, charge):
            _require(row.get("kind") in {"c_budget_continuation_claim", "c_budget_continuation_charge"}
                     and type(row.get("sequence")) is int and row["sequence"] == sequence
                     and row.get("cpu_result_sha256") == cpu_result_sha256
                     and row.get("source_terminal_receipt_sha256") == source_terminal_receipt_sha256
                     and row.get("root_claim_sha256") == root_claim.sha256
                     and row.get("root_charge_sha256") == root_charge.sha256
                     and row.get("parent_charge_sha256") == parent_sha,
                     "LOCAL_C_BUDGET_CHAIN_LINK_MISMATCH")
        _require(claim["kind"] == "c_budget_continuation_claim"
                 and charge["kind"] == "c_budget_continuation_charge"
                 and charge.get("claim_sha256") == claim_document.sha256,
                 "LOCAL_C_BUDGET_CHAIN_LINK_MISMATCH")
        before = _number(claim.get("cumulative_charge_before_seconds"))
        admitted = _number(claim.get("remaining_seconds_at_admission"))
        _require(abs(before - cumulative) <= _EPS and 0 < admitted <= remaining + _EPS
                 and abs(_number(charge.get("cumulative_charge_before_seconds")) - before) <= _EPS,
                 "LOCAL_C_BUDGET_ROLLBACK")
        cumulative, remaining = _charged_values(charge, sfm=sfm,
            before=max(before, cumulative), admitted=min(admitted, remaining))
        evidence.append({"sequence": sequence, "claim_sha256": claim_document.sha256,
                         "claim_size_bytes": claim_document.size_bytes,
                         "charge_sha256": charge_document.sha256,
                         "charge_size_bytes": charge_document.size_bytes})
        parent_sha = charge_document.sha256
    proof = {"schema_version": "1.0", "kind": "c_budget_closed_chain",
        "cpu_result_sha256": cpu_result_sha256,
        "source_terminal_receipt_sha256": source_terminal_receipt_sha256,
        "root_profile_sha256": root_profile_sha256, "root_claim_sha256": root_claim.sha256,
        "root_charge_sha256": root_charge.sha256, "parent_charge_sha256": parent_sha,
        "original_total_seconds": int(total), "consumed_sfm_seconds": float(sfm),
        "consumed_c_seconds": float(max(Decimal(0), cumulative - sfm)),
        "cumulative_charge_seconds": float(cumulative), "remaining_seconds": float(remaining),
        "closed_attempt_count": len(evidence), "next_sequence": len(evidence),
        "evidence": evidence, "previous_scenes": sorted(seen_scenes),
        "budget_claimed": False, "budget_charged": False,
        "budget_reset_or_extended": False, "complete_closed_chain": True}
    proof["proof_sha256"] = hashlib.sha256(_json_bytes(proof)).hexdigest()
    return proof


def _ledger_inventory(parent, cpu_result_sha256):
    prefix = "c-budget-" + cpu_result_sha256
    # Reject alternate names instead of silently ignoring a competing lineage.
    return sorted(path.name for path in parent.iterdir() if path.name.startswith(prefix))


def _inventory_identities(parent, names):
    result = {}
    for name in names:
        path = ordinary(parent / name)
        info = path.lstat()
        _require(stat.S_ISREG(info.st_mode), "LOCAL_C_BUDGET_DOCUMENT_INVALID")
        result[name] = (_identity(info), info.st_ctime_ns)
    return result


def read_c_budget_chain(parent, *, source_budget, cpu_result_sha256,
                        root_profile_sha256, source_terminal_receipt_sha256,
                        root_claim_sha256, root_charge_sha256, expected_tip_sha256=None):
    """Read-only authoritative directory scan; an old tip pin cannot hide spend."""
    _require(_sha(cpu_result_sha256) and _sha(root_claim_sha256) and _sha(root_charge_sha256))
    parent = ordinary(Path(parent))
    initial = _ledger_inventory(parent, cpu_result_sha256)
    identities = _inventory_identities(parent, initial)
    stem = "c-budget-" + cpu_result_sha256
    root_names = [stem + ".json", stem + "-charge.json"]
    _require(all(name in initial for name in root_names), "LOCAL_C_BUDGET_CHARGE_REQUIRED")
    names = set(initial) - set(root_names)
    pattern = re.compile(re.escape(stem) + r"-continuation-([0-9]{6})(-charge)?\.json\Z")
    parsed = [pattern.fullmatch(name) for name in sorted(names)]
    _require(all(parsed), "LOCAL_C_BUDGET_FORK_OR_UNKNOWN_ENTRY")
    sequences = sorted({int(match[1]) for match in parsed})
    _require(sequences == list(range(1, len(sequences) + 1)), "LOCAL_C_BUDGET_CHAIN_GAP")
    pairs = []
    for sequence in sequences:
        claim_name, charge_name = continuation_names(cpu_result_sha256, sequence)
        _require(claim_name in names and charge_name in names, "LOCAL_C_BUDGET_CHARGE_REQUIRED")
        pairs.append((read_budget_document(parent / claim_name),
                      read_budget_document(parent / charge_name)))
    result = reconcile_c_budget(source_budget=source_budget, cpu_result_sha256=cpu_result_sha256,
        root_profile_sha256=root_profile_sha256,
        source_terminal_receipt_sha256=source_terminal_receipt_sha256,
        root_claim=read_budget_document(parent / root_names[0], root_claim_sha256),
        root_charge=read_budget_document(parent / root_names[1], root_charge_sha256), continuations=pairs)
    _require(initial == _ledger_inventory(parent, cpu_result_sha256)
             and identities == _inventory_identities(parent, initial), "LOCAL_C_BUDGET_CHAIN_CHANGED")
    if expected_tip_sha256 is not None:
        _require(_sha(expected_tip_sha256) and result["parent_charge_sha256"] == expected_tip_sha256,
                 "LOCAL_C_BUDGET_STALE_TIP")
    return result


def _proof_valid(proof):
    _require(type(proof) is dict and _sha(proof.get("proof_sha256")))
    unsigned = {key: value for key, value in proof.items() if key != "proof_sha256"}
    _require(hashlib.sha256(_json_bytes(unsigned)).hexdigest() == proof["proof_sha256"],
             "LOCAL_C_BUDGET_PROOF_CHANGED")
    _require(proof.get("complete_closed_chain") is True and proof.get("budget_reset_or_extended") is False)


def _exclusive_document(path, value):
    ordinary(path, missing_leaf=True)
    raw = _json_bytes(value)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise SupervisionError("LOCAL_C_BUDGET_ALREADY_CLAIMED") from None
    return parse_budget_document(raw, hashlib.sha256(raw).hexdigest())


def claim_c_continuation_budget(parent, reconciliation, *, source_budget,
                                profile_sha256, scene, remaining_seconds=None):
    """Future explicit execution only. Atomically reserve the one next slot.

Re-read the entire live chain before the exclusive claim: neither a stale
preflight nor a competing caller can fork from an earlier charge. No reset or
delete helper exists; an unclosed claim deliberately blocks future attempts.
"""
    _proof_valid(reconciliation)
    _require(_sha(profile_sha256) and isinstance(scene, str) and _SCENE.fullmatch(scene)
             and scene not in reconciliation["previous_scenes"], "LOCAL_C_BUDGET_CLAIM_INVALID")
    live = read_c_budget_chain(parent, source_budget=source_budget,
        cpu_result_sha256=reconciliation["cpu_result_sha256"],
        root_profile_sha256=reconciliation["root_profile_sha256"],
        source_terminal_receipt_sha256=reconciliation["source_terminal_receipt_sha256"],
        root_claim_sha256=reconciliation["root_claim_sha256"],
        root_charge_sha256=reconciliation["root_charge_sha256"],
        expected_tip_sha256=reconciliation["parent_charge_sha256"])
    _require(live == reconciliation, "LOCAL_C_BUDGET_CHAIN_CHANGED")
    allowed = _number(live["remaining_seconds"])
    if remaining_seconds is not None:
        requested = _number(remaining_seconds)
        _require(requested <= allowed, "LOCAL_C_BUDGET_ROLLBACK")
        allowed = requested
    _require(allowed > 0, "LONG_BUDGET_EXHAUSTED")
    sequence = live["next_sequence"]
    claim_name, _ = continuation_names(live["cpu_result_sha256"], sequence)
    claim = {key: live[key] for key in ("cpu_result_sha256", "source_terminal_receipt_sha256",
        "root_claim_sha256", "root_charge_sha256", "parent_charge_sha256")}
    claim.update(schema_version="1.0", mode="local-c-only", kind="c_budget_continuation_claim",
        sequence=sequence, scene=scene, profile_sha256=profile_sha256,
        cumulative_charge_before_seconds=live["cumulative_charge_seconds"],
        remaining_seconds_at_admission=float(allowed),
        prior_sfm_charge_seconds=live["consumed_sfm_seconds"],
        created_at_utc=datetime.now(timezone.utc).isoformat(), automatic_retry_allowed=False)
    path = Path(parent) / claim_name
    document = _exclusive_document(path, claim)
    return path, document


def make_c_continuation_charge(claim_document, *, elapsed_seconds, remaining_seconds, status):
    """Pure terminal charge builder; elapsed is not inferred from stage success."""
    claim = _validated(claim_document)
    _require(claim.get("kind") == "c_budget_continuation_claim" and status in _TERMINAL)
    _require(_sha(claim.get("profile_sha256"))
             and all(_sha(claim.get(key)) for key in ("cpu_result_sha256",
                 "source_terminal_receipt_sha256", "root_claim_sha256", "root_charge_sha256",
                 "parent_charge_sha256"))
             and isinstance(claim.get("scene"), str) and _SCENE.fullmatch(claim["scene"])
             and type(claim.get("sequence")) is int and 1 <= claim["sequence"] <= 999999,
             "LOCAL_C_BUDGET_CLAIM_INVALID")
    _base_fields(claim, claim["profile_sha256"])
    elapsed = _number(elapsed_seconds)
    previous = _number(claim.get("cumulative_charge_before_seconds"))
    admitted = _number(claim.get("remaining_seconds_at_admission"))
    reported = _number(remaining_seconds)
    _require(reported <= admitted + _EPS, "LOCAL_C_BUDGET_ROLLBACK")
    remaining = min(reported, max(Decimal(0), admitted - elapsed),
                    max(Decimal(0), _TOTAL - previous - elapsed))
    # Admission may have consumed allowance before the claim was written.
    # Charge that time too instead of making the next attempt recover it.
    cumulative = max(previous + elapsed, _TOTAL - remaining)
    result = {key: claim[key] for key in ("schema_version", "mode", "sequence", "scene",
        "profile_sha256", "cpu_result_sha256", "source_terminal_receipt_sha256",
        "root_claim_sha256", "root_charge_sha256", "parent_charge_sha256",
        "cumulative_charge_before_seconds", "prior_sfm_charge_seconds", "automatic_retry_allowed")}
    result.update(kind="c_budget_continuation_charge", claim_sha256=claim_document.sha256,
        status=status, elapsed_seconds=float(cumulative - previous),
        reported_compute_elapsed_seconds=float(elapsed), remaining_budget_seconds=float(remaining),
        cumulative_charge_seconds=float(cumulative), finished_at_utc=datetime.now(timezone.utc).isoformat())
    return result


def write_c_continuation_charge(claim_path, claim_document, **values):
    """Exclusive terminal receipt; never replace a historical charge."""
    actual = read_budget_document(claim_path, claim_document.sha256)
    _require(actual.raw == claim_document.raw, "LOCAL_C_BUDGET_DOCUMENT_CHANGED")
    claim = actual.data
    _require(Path(claim_path).name == continuation_names(claim.get("cpu_result_sha256"),
                                                         claim.get("sequence"))[0],
             "LOCAL_C_BUDGET_CHAIN_LINK_MISMATCH")
    path = Path(claim_path).with_name(Path(claim_path).stem + "-charge.json")
    document = _exclusive_document(path, make_c_continuation_charge(actual, **values))
    return path, document
