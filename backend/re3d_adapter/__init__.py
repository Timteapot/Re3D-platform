"""Safe adapter boundary between the platform worker and Re3D."""

from .errors import (
    AdapterError,
    ContractValidationError,
    IntegrityError,
    PathBoundaryError,
    SimulatedCrash,
)
from .paths import TaskLayout
from .real import RealDryRunOutcome, RealDryRunRunner
from .simulation import SimulationOutcome, SimulationRunner

__all__ = [
    "AdapterError",
    "ContractValidationError",
    "IntegrityError",
    "PathBoundaryError",
    "RealDryRunOutcome",
    "RealDryRunRunner",
    "SimulatedCrash",
    "SimulationOutcome",
    "SimulationRunner",
    "TaskLayout",
]
