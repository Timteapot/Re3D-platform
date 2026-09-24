"""Safe adapter boundary between the platform worker and Re3D."""

from .errors import (
    AdapterError,
    ContractValidationError,
    IntegrityError,
    PathBoundaryError,
    PipelineCancelled,
    PipelineTimedOut,
    SimulatedCrash,
)
from .paths import TaskLayout
from .real import (
    RealDryRunOutcome,
    RealDryRunRunner,
    RealPipelineOutcome,
    RealPipelineRunner,
)
from .simulation import SimulationOutcome, SimulationRunner

__all__ = [
    "AdapterError",
    "ContractValidationError",
    "IntegrityError",
    "PathBoundaryError",
    "PipelineCancelled",
    "PipelineTimedOut",
    "RealDryRunOutcome",
    "RealDryRunRunner",
    "RealPipelineOutcome",
    "RealPipelineRunner",
    "SimulatedCrash",
    "SimulationOutcome",
    "SimulationRunner",
    "TaskLayout",
]
