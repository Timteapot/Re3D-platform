"""Controlled failures raised by the persistent job scheduler."""


class SchedulerError(RuntimeError):
    """Base class for expected scheduler failures."""


class InvalidTransitionError(SchedulerError):
    """A requested job status transition is not allowed."""


class JobNotFoundError(SchedulerError):
    """The requested reconstruction job does not exist."""


class LeaseLostError(SchedulerError):
    """A worker no longer owns the lease used for an operation."""


class QueueConflictError(SchedulerError):
    """A queue identity or idempotency constraint was violated."""
