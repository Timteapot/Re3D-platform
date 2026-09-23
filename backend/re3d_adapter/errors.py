"""Expected adapter failures with messages safe for local operator logs."""


class AdapterError(RuntimeError):
    """Base class for controlled adapter failures."""


class ContractValidationError(AdapterError):
    """A request, event, result, or evaluation violates its contract."""


class PathBoundaryError(AdapterError):
    """A path would escape the configured task directory."""


class IntegrityError(AdapterError):
    """A persisted file does not match its recorded identity or hash."""


class SimulatedCrash(AdapterError):
    """Development-only interruption used to verify recovery behavior."""
