"""Public data models for Dicke compilation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import cirq

ErrorValue = float | tuple[float, float]


class Objective(str, Enum):
    """Built-in candidate-selection objectives."""

    DEPTH = "depth"
    CZ_DEPTH = "cz_depth"
    CZ_COUNT = "cz_count"
    PROJECTED_SURVIVAL = "projected_survival"


class EquivalenceMode(str, Enum):
    """Contract under which an optimization is valid."""

    UNITARY = "unitary"
    STATE_PREPARATION = "state_preparation"
    SUBSPACE = "subspace"


class VerificationStatus(str, Enum):
    """Strength of the evidence attached to a compilation result."""

    EXACT = "exact"
    ANALYTIC = "analytic"
    UNVERIFIED = "unverified"
    FAILED = "failed"


@dataclass(frozen=True)
class DickeTarget:
    """A normalized symmetric Dicke-state target."""

    num_qubits: int
    num_excitations: int

    def __post_init__(self) -> None:
        if not isinstance(self.num_qubits, int) or self.num_qubits < 1:
            raise ValueError("num_qubits must be a positive integer")
        if (
            not isinstance(self.num_excitations, int)
            or not 0 <= self.num_excitations <= self.num_qubits
        ):
            raise ValueError(
                "num_excitations must be an integer between 0 and num_qubits"
            )


@dataclass(frozen=True)
class CalibrationData:
    """Optional current-hardware error data used for candidate ranking.

    Values may be raw error probabilities or ``(mean, uncertainty)`` pairs.
    The uncertainty is added to the mean using ``uncertainty_weight``.
    """

    single_qubit: Mapping[cirq.Qid, ErrorValue] = field(default_factory=dict)
    two_qubit: Mapping[tuple[cirq.Qid, cirq.Qid], ErrorValue] = field(
        default_factory=dict
    )
    readout_zero: Mapping[cirq.Qid, ErrorValue] = field(default_factory=dict)
    readout_one: Mapping[cirq.Qid, ErrorValue] = field(default_factory=dict)
    uncertainty_weight: float = 1.0

    def risk_rate(self, value: ErrorValue | None, default: float) -> float:
        """Return a clipped uncertainty-adjusted probability."""
        if value is None:
            return default
        if isinstance(value, tuple):
            mean = float(value[0])
            uncertainty = float(value[1])
        else:
            mean, uncertainty = float(value), 0.0
        adjusted = mean + self.uncertainty_weight * uncertainty
        return min(max(adjusted, 0.0), 1.0 - 1e-12)

    def single_qubit_rate(self, qubit: cirq.Qid) -> float:
        return self.risk_rate(self.single_qubit.get(qubit), 0.001)

    def two_qubit_rate(self, first: cirq.Qid, second: cirq.Qid) -> float:
        value = self.two_qubit.get((first, second))
        if value is None:
            value = self.two_qubit.get((second, first))
        return self.risk_rate(value, 0.01)

    def readout_rates(self, qubit: cirq.Qid) -> tuple[float, float]:
        return (
            self.risk_rate(self.readout_zero.get(qubit), 0.01),
            self.risk_rate(self.readout_one.get(qubit), 0.01),
        )


@dataclass(frozen=True)
class CircuitMetrics:
    """Hardware-independent circuit-size and depth metrics."""

    moments: int
    operations: int
    single_qubit_gates: int
    two_qubit_gates: int
    cz_count: int
    cz_depth: int


@dataclass(frozen=True)
class VerificationReport:
    """Verification evidence for a compiled circuit."""

    status: VerificationStatus
    fidelity: float | None
    tolerance: float
    method: str
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status in {
            VerificationStatus.EXACT,
            VerificationStatus.ANALYTIC,
        }


@dataclass(frozen=True)
class CompilationResult:
    """Circuit plus provenance, metrics, projection, and verification."""

    circuit: cirq.Circuit
    output_qubits: frozenset[cirq.Qid]
    target: DickeTarget
    objective: Objective
    equivalence: EquivalenceMode
    candidate_family: str
    applied_passes: tuple[str, ...]
    before_metrics: CircuitMetrics
    after_metrics: CircuitMetrics
    projected_survival: float | None
    runtime_seconds: float
    verification: VerificationReport
    metadata: Mapping[str, Any] = field(default_factory=dict)
