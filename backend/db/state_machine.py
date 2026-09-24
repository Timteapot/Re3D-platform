from __future__ import annotations

from enum import StrEnum

from .errors import InvalidTransitionError


class JobStatus(StrEnum):
    DRAFT = "draft"
    UPLOADING = "uploading"
    VALIDATING_INPUT = "validating_input"
    QUEUED = "queued"
    PREPARING = "preparing"
    SFM = "sfm"
    DENSE_RECONSTRUCTION = "dense_reconstruction"
    MESHING = "meshing"
    TEXTURING = "texturing"
    VALIDATING_OUTPUT = "validating_output"
    EVALUATING = "evaluating"
    SUCCEEDED = "succeeded"
    FAILED_INPUT = "failed_input"
    FAILED_PIPELINE = "failed_pipeline"
    FAILED_EVALUATION = "failed_evaluation"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED_INPUT,
        JobStatus.FAILED_PIPELINE,
        JobStatus.FAILED_EVALUATION,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
    }
)

_NORMAL_FLOW = (
    JobStatus.DRAFT,
    JobStatus.UPLOADING,
    JobStatus.VALIDATING_INPUT,
    JobStatus.QUEUED,
    JobStatus.PREPARING,
    JobStatus.SFM,
    JobStatus.DENSE_RECONSTRUCTION,
    JobStatus.MESHING,
    JobStatus.TEXTURING,
    JobStatus.VALIDATING_OUTPUT,
    JobStatus.EVALUATING,
    JobStatus.SUCCEEDED,
)

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    status: frozenset({next_status, JobStatus.CANCELLED})
    for status, next_status in zip(_NORMAL_FLOW, _NORMAL_FLOW[1:])
}
ALLOWED_TRANSITIONS[JobStatus.UPLOADING] |= {JobStatus.FAILED_INPUT}
ALLOWED_TRANSITIONS[JobStatus.VALIDATING_INPUT] |= {JobStatus.FAILED_INPUT}
for status in (
    JobStatus.PREPARING,
    JobStatus.SFM,
    JobStatus.DENSE_RECONSTRUCTION,
    JobStatus.MESHING,
    JobStatus.TEXTURING,
    JobStatus.VALIDATING_OUTPUT,
):
    ALLOWED_TRANSITIONS[status] |= {JobStatus.FAILED_PIPELINE}
ALLOWED_TRANSITIONS[JobStatus.EVALUATING] |= {JobStatus.FAILED_EVALUATION}
ALLOWED_TRANSITIONS[JobStatus.SUCCEEDED] = frozenset({JobStatus.EXPIRED})
for status in TERMINAL_STATUSES - {JobStatus.SUCCEEDED}:
    ALLOWED_TRANSITIONS[status] = frozenset()


def can_transition(current: JobStatus | str, target: JobStatus | str) -> bool:
    current_status = JobStatus(current)
    target_status = JobStatus(target)
    return target_status in ALLOWED_TRANSITIONS[current_status]


def assert_transition(current: JobStatus | str, target: JobStatus | str) -> None:
    current_status = JobStatus(current)
    target_status = JobStatus(target)
    if not can_transition(current_status, target_status):
        raise InvalidTransitionError(
            f"job status cannot transition from {current_status.value} "
            f"to {target_status.value}"
        )
