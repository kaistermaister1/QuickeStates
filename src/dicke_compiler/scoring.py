"""Transparent calibration projections for candidate ranking."""

from __future__ import annotations

import math
from collections.abc import Collection

import cirq

from .models import CalibrationData, DickeTarget


def projected_log_loss(
    circuit: cirq.AbstractCircuit,
    output_qubits: Collection[cirq.Qid],
    target: DickeTarget,
    calibration: CalibrationData,
) -> float:
    """Return uncertainty-adjusted independent gate/readout log loss.

    This is a ranking proxy, not a prediction of an official hardware score.
    It models multiplicative survival by adding ``-log(1-error)`` for each
    physical operation and the target's expected asymmetric readout mixture.
    """
    loss = 0.0
    for operation in circuit.all_operations():
        if len(operation.qubits) == 1:
            error = calibration.single_qubit_rate(operation.qubits[0])
        elif len(operation.qubits) == 2:
            error = calibration.two_qubit_rate(*operation.qubits)
        else:
            raise ValueError(
                "projected survival supports only one- and two-qubit gates"
            )
        loss -= math.log1p(-error)

    excitation_probability = (
        target.num_excitations / target.num_qubits
    )
    for qubit in output_qubits:
        zero_to_one, one_to_zero = calibration.readout_rates(qubit)
        effective = (
            (1.0 - excitation_probability) * zero_to_one
            + excitation_probability * one_to_zero
        )
        loss -= math.log1p(-effective)
    return loss


def projected_survival(
    circuit: cirq.AbstractCircuit,
    output_qubits: Collection[cirq.Qid],
    target: DickeTarget,
    calibration: CalibrationData,
) -> float:
    """Convert projected log loss to a multiplicative survival proxy."""
    return math.exp(
        -projected_log_loss(
            circuit,
            output_qubits,
            target,
            calibration,
        )
    )
