"""Independent Dicke-state references and verification."""

from __future__ import annotations

import math
from collections.abc import Sequence

import cirq
import numpy as np

from .models import (
    DickeTarget,
    VerificationReport,
    VerificationStatus,
)


def dicke_state_vector(
    target: DickeTarget,
    *,
    dtype: np.dtype = np.complex128,
) -> np.ndarray:
    """Return the normalized computational-basis Dicke state."""
    dimension = 1 << target.num_qubits
    state = np.zeros(dimension, dtype=dtype)
    amplitude = 1 / math.sqrt(
        math.comb(target.num_qubits, target.num_excitations)
    )
    for index in range(dimension):
        if index.bit_count() == target.num_excitations:
            state[index] = amplitude
    return state


def state_preparation_fidelity(
    circuit: cirq.AbstractCircuit,
    output_qubits: Sequence[cirq.Qid],
    target: DickeTarget,
) -> float:
    """Simulate from all-zero input and return target-state fidelity."""
    ordered = tuple(output_qubits)
    if len(ordered) != target.num_qubits or len(set(ordered)) != len(ordered):
        raise ValueError("output_qubits must contain exactly N unique qubits")
    if not circuit.all_qubits().issubset(ordered):
        raise ValueError(
            "exact verification currently requires no ancillary qubits"
        )
    state = cirq.Simulator(dtype=np.complex128).simulate(
        circuit,
        qubit_order=ordered,
    ).final_state_vector
    ideal = dicke_state_vector(target)
    return float(abs(np.vdot(ideal, state)) ** 2)


def verify_dicke_preparation(
    circuit: cirq.AbstractCircuit,
    output_qubits: Sequence[cirq.Qid],
    target: DickeTarget,
    *,
    atol: float = 1e-8,
    max_exact_qubits: int = 15,
    analytic_family: str | None = None,
) -> VerificationReport:
    """Verify exactly when feasible, otherwise report analytic provenance."""
    if target.num_qubits <= max_exact_qubits:
        fidelity = state_preparation_fidelity(
            circuit,
            output_qubits,
            target,
        )
        passed = bool(np.isclose(fidelity, 1.0, rtol=0.0, atol=atol))
        return VerificationReport(
            status=(
                VerificationStatus.EXACT
                if passed
                else VerificationStatus.FAILED
            ),
            fidelity=fidelity,
            tolerance=atol,
            method="complex128_statevector",
            message="" if passed else "state fidelity is below tolerance",
        )
    if analytic_family is not None:
        return VerificationReport(
            status=VerificationStatus.ANALYTIC,
            fidelity=None,
            tolerance=atol,
            method=f"analytic_family:{analytic_family}",
            message=(
                "Exact statevector verification skipped above configured "
                "qubit limit"
            ),
        )
    return VerificationReport(
        status=VerificationStatus.UNVERIFIED,
        fidelity=None,
        tolerance=atol,
        method="not_run",
        message="Exact verification exceeds configured qubit limit",
    )
