"""Composable, equivalence-preserving circuit optimization passes."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import cirq

from .models import CircuitMetrics

CircuitPass = Callable[[cirq.Circuit], cirq.Circuit]


def circuit_metrics(circuit: cirq.AbstractCircuit) -> CircuitMetrics:
    """Measure total depth, entangling depth, and operation counts."""
    operations = list(circuit.all_operations())
    cz_moments = sum(
        any(operation.gate == cirq.CZ for operation in moment.operations)
        for moment in circuit
    )
    return CircuitMetrics(
        moments=len(circuit),
        operations=len(operations),
        single_qubit_gates=sum(
            len(operation.qubits) == 1 for operation in operations
        ),
        two_qubit_gates=sum(
            len(operation.qubits) == 2 for operation in operations
        ),
        cz_count=sum(operation.gate == cirq.CZ for operation in operations),
        cz_depth=cz_moments,
    )


def compress_moments(circuit: cirq.Circuit) -> cirq.Circuit:
    """Move operations to their earliest dependency-safe moments."""
    return cirq.Circuit(
        circuit.all_operations(),
        strategy=cirq.InsertStrategy.EARLIEST,
    )


def fuse_single_qubit_gates(
    circuit: cirq.Circuit,
    *,
    atol: float = 1e-12,
) -> cirq.Circuit:
    """Merge uninterrupted one-qubit runs into PhasedXZ operations."""
    result = cirq.merge_single_qubit_gates_to_phxz(circuit, atol=atol)
    result = cirq.drop_negligible_operations(result, atol=atol)
    return compress_moments(result)


def propagate_virtual_phases(
    circuit: cirq.Circuit,
    *,
    atol: float = 1e-12,
) -> cirq.Circuit:
    """Eject virtual Z and phased-Pauli frames, then normalize."""
    result = cirq.eject_z(circuit, atol=atol)
    result = cirq.eject_phased_paulis(result, atol=atol)
    result = cirq.eject_z(result, atol=atol)
    return fuse_single_qubit_gates(result, atol=atol)


def cancel_adjacent_czs(circuit: cirq.Circuit) -> cirq.Circuit:
    """Cancel same-pair CZs with no intervening touch on either qubit."""
    result = circuit.copy()
    while True:
        last_touch: dict[
            cirq.Qid,
            tuple[int, cirq.Operation],
        ] = {}
        removals: list[tuple[int, cirq.Operation]] = []
        removed = False
        for moment_index, moment in enumerate(result):
            for operation in moment.operations:
                if operation.gate == cirq.CZ:
                    first, second = operation.qubits
                    previous_first = last_touch.get(first)
                    previous_second = last_touch.get(second)
                    if (
                        previous_first is not None
                        and previous_first == previous_second
                        and previous_first[1].gate == cirq.CZ
                        and frozenset(previous_first[1].qubits)
                        == frozenset(operation.qubits)
                    ):
                        removals.extend(
                            [previous_first, (moment_index, operation)]
                        )
                        removed = True
                        break
                for qubit in operation.qubits:
                    last_touch[qubit] = (moment_index, operation)
            if removed:
                break
        if not removed:
            return cirq.drop_empty_moments(result)
        result.batch_remove(removals)
        result = cirq.drop_empty_moments(result)


DEFAULT_PASSES: tuple[tuple[str, CircuitPass], ...] = (
    ("cancel_adjacent_czs", cancel_adjacent_czs),
    ("propagate_virtual_phases", propagate_virtual_phases),
    ("fuse_single_qubit_gates", fuse_single_qubit_gates),
    ("compress_moments", compress_moments),
)


def optimize_unitary(
    circuit: cirq.AbstractCircuit,
    *,
    passes: Sequence[tuple[str, CircuitPass]] = DEFAULT_PASSES,
) -> tuple[cirq.Circuit, tuple[str, ...]]:
    """Apply unitary-safe passes and report which passes changed the circuit."""
    result = cirq.Circuit(circuit)
    applied: list[str] = []
    for name, circuit_pass in passes:
        candidate = circuit_pass(result)
        if candidate != result:
            applied.append(name)
            result = candidate
    return result, tuple(applied)
