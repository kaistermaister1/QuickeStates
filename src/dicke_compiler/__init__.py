"""Verified, hardware-aware Dicke-state compilation for Cirq."""

from .api import compile_dicke, optimize_dicke
from .hardware import HardwareTarget, line_target, willow_target
from .models import (
    CalibrationData,
    CircuitMetrics,
    CompilationResult,
    DickeTarget,
    EquivalenceMode,
    Objective,
    VerificationReport,
    VerificationStatus,
)
from .templates import CircuitTemplate, TemplateRegistry
from .verification import (
    dicke_state_vector,
    state_preparation_fidelity,
    verify_dicke_preparation,
)

__all__ = [
    "CalibrationData",
    "CircuitTemplate",
    "CircuitMetrics",
    "CompilationResult",
    "DickeTarget",
    "EquivalenceMode",
    "HardwareTarget",
    "Objective",
    "TemplateRegistry",
    "VerificationReport",
    "VerificationStatus",
    "compile_dicke",
    "dicke_state_vector",
    "line_target",
    "optimize_dicke",
    "state_preparation_fidelity",
    "verify_dicke_preparation",
    "willow_target",
]

__version__ = "0.1.0"
