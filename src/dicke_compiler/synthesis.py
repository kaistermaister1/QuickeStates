"""Analytic Dicke-state circuit generators.

The line construction follows Bärtschi and Eidenbenz (2019), with verified
reachable-subspace row templates adapted from the DuQuantum compiler.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import cirq
import networkx as nx
import numpy as np

from .models import DickeTarget

SynthesisMethod = Callable[[DickeTarget], cirq.Circuit]


def _reflect_three(
    first: cirq.Qid,
    second: cirq.Qid,
    third: cirq.Qid,
    remaining: int,
    ell: int,
) -> list[cirq.Operation]:
    alpha = 0.5 * math.asin(math.sqrt(ell / remaining))
    return [
        cirq.CNOT(second, first),
        cirq.ry(alpha)(second),
        cirq.CNOT(third, second),
        cirq.ry(alpha)(second),
        cirq.CNOT(first, second),
        cirq.ry(-alpha)(second),
        cirq.CNOT(third, second),
        cirq.ry(-alpha)(second),
        cirq.CNOT(second, first),
    ]


def _combined_top_row(
    qubits: Sequence[cirq.Qid],
    remaining: int,
    width: int,
) -> list[cirq.Operation]:
    """Four-CZ reachable-subspace replacement for two adjacent rows."""
    if len(qubits) != 3 or not 2 <= width < remaining:
        raise ValueError("require three qubits and 2 <= width < remaining")
    first, second, third = qubits
    angle_a = math.acos(math.sqrt(width / remaining))
    angle_b = math.asin(math.sqrt((width - 1) / remaining))
    half_pi = math.pi / 2
    return [
        cirq.ry(-3 * half_pi)(first),
        cirq.ry(angle_a - math.pi)(third),
        cirq.CZ(second, third),
        cirq.ry(half_pi)(second),
        cirq.ry(math.pi - angle_a)(third),
        cirq.CZ(first, second),
        cirq.ry(angle_b + half_pi)(first),
        cirq.ry(math.pi - angle_b)(second),
        cirq.CZ(second, third),
        cirq.ry(-half_pi)(second),
        cirq.CZ(first, second),
        cirq.ry(half_pi)(first),
        cirq.ry(half_pi)(second),
    ]


def _unary_three_weight_two(
    qubits: Sequence[cirq.Qid],
) -> list[cirq.Operation]:
    """Six-CZ isometry on reachable unary weights zero through two."""
    if len(qubits) != 3:
        raise ValueError("expected exactly three qubits")
    angle = math.asin(1 / math.sqrt(3))
    values = [
        -math.pi / 2,
        math.pi,
        angle - math.pi,
        -math.pi / 2,
        angle - math.pi,
        math.pi / 2 + angle,
        angle,
        -math.pi / 2,
        -math.pi / 2,
        -math.pi / 2,
        0,
        -math.pi / 4,
        math.pi / 4,
        math.pi / 2,
        math.pi / 2,
    ]
    edges = [(1, 2), (0, 1), (1, 2), (0, 1), (1, 2), (1, 2)]
    operations = [
        cirq.ry(values[index])(qubits[index]) for index in range(3)
    ]
    for edge_index, (first, second) in enumerate(edges):
        operations.append(cirq.CZ(qubits[first], qubits[second]))
        first_angle = values[3 + 2 * edge_index]
        second_angle = values[4 + 2 * edge_index]
        if first_angle:
            operations.append(cirq.ry(first_angle)(qubits[first]))
        if second_angle:
            operations.append(cirq.ry(second_angle)(qubits[second]))
    return operations


def _raw_line_circuit(
    target: DickeTarget,
    *,
    state_aware_rows: bool,
) -> cirq.Circuit:
    num_qubits = target.num_qubits
    num_excitations = target.num_excitations
    qubits = cirq.LineQubit.range(num_qubits)
    complement = num_excitations > num_qubits // 2
    weight = num_qubits - num_excitations if complement else num_excitations
    operations: list[cirq.Operation] = list(
        cirq.X.on_each(qubits[: max(0, weight - 1)])
    )

    for start in range(num_qubits - 1):
        remaining = num_qubits - start
        if state_aware_rows and weight == 2 and start > 0:
            if remaining == 3:
                operations.extend(
                    _unary_three_weight_two(qubits[start : start + 3])
                )
                break
            if remaining >= 4:
                operations.extend(
                    _combined_top_row(
                        qubits[start : start + 3],
                        remaining,
                        2,
                    )
                )
                continue

        width = min(weight, remaining - 1)
        if not width:
            break
        cosine = math.sqrt(width / remaining)
        sine = math.sqrt((remaining - width) / remaining)
        givens = np.array(
            [
                [1, 0, 0, 0],
                [0, cosine, sine, 0],
                [0, -sine, cosine, 0],
                [0, 0, 0, 1],
            ],
            dtype=np.complex128,
        )
        top_ell = width - 1
        if (
            state_aware_rows
            and weight >= 3
            and start > 0
            and remaining > weight
        ):
            operations.extend(
                _combined_top_row(
                    qubits[start + width - 2 : start + width + 1],
                    remaining,
                    width,
                )
            )
            top_ell = width - 2
        elif start == 0:
            first, second = qubits[width - 1 : width + 1]
            operations.extend(
                [
                    cirq.ry(2 * math.acos(cosine))(first),
                    cirq.CNOT(first, second),
                    cirq.X(first),
                ]
            )
        else:
            operations.append(
                cirq.MatrixGate(givens)(
                    qubits[start + width - 1],
                    qubits[start + width],
                )
            )
        for ell in range(top_ell, 0, -1):
            first, second, third = qubits[
                start + ell - 1 : start + ell + 2
            ]
            if ell < weight - start:
                operations.extend(
                    [
                        cirq.CNOT(second, first),
                        cirq.CNOT(first, second),
                        cirq.X(first),
                    ]
                )
            else:
                operations.extend(
                    _reflect_three(
                        first,
                        second,
                        third,
                        remaining,
                        ell,
                    )
                )
    if complement:
        operations.extend(cirq.X.on_each(qubits))
    if not operations:
        operations.extend(cirq.I.on_each(qubits))
    return cirq.Circuit(operations)


def compile_to_cz_phxz(circuit: cirq.AbstractCircuit) -> cirq.Circuit:
    """Compile one- and two-qubit operations to full CZ and PhasedXZ."""
    operations: list[cirq.Operation] = []
    for operation in circuit.all_operations():
        if operation.gate == cirq.CNOT:
            control, target = operation.qubits
            operations.extend(
                [cirq.H(target), cirq.CZ(control, target), cirq.H(target)]
            )
        elif len(operation.qubits) == 1 or operation.gate == cirq.CZ:
            operations.append(operation)
        elif len(operation.qubits) == 2:
            operations.extend(
                cirq.two_qubit_matrix_to_cz_operations(
                    *operation.qubits,
                    cirq.unitary(operation),
                    allow_partial_czs=False,
                )
            )
        else:
            raise ValueError(f"unsupported operation {operation!r}")
    result = cirq.Circuit(operations)
    result = cirq.merge_single_qubit_gates_to_phxz(result, atol=1e-12)
    result = cirq.drop_negligible_operations(result, atol=1e-12)
    return cirq.Circuit(
        result.all_operations(),
        strategy=cirq.InsertStrategy.EARLIEST,
    )


def line_dicke_circuit(
    target: DickeTarget,
    *,
    state_aware_rows: bool = True,
) -> cirq.Circuit:
    """Return an exact native Dicke circuit on ``LineQubit.range(N)``."""
    return compile_to_cz_phxz(
        _raw_line_circuit(target, state_aware_rows=state_aware_rows)
    )


def _native_ry(qubit: cirq.Qid, angle: float) -> cirq.Operation:
    return cirq.PhasedXZGate(
        x_exponent=angle / math.pi,
        z_exponent=0,
        axis_phase_exponent=0.5,
    ).on(qubit)


def _native_h(qubit: cirq.Qid) -> cirq.Operation:
    return cirq.PhasedXZGate(
        x_exponent=0.5,
        z_exponent=1,
        axis_phase_exponent=1.5,
    ).on(qubit)


def _native_cnot(
    control: cirq.Qid,
    target: cirq.Qid,
) -> list[cirq.Operation]:
    return [_native_h(target), cirq.CZ(control, target), _native_h(target)]


def _ccry(
    first_control: cirq.Qid,
    second_control: cirq.Qid,
    target: cirq.Qid,
    angle: float,
) -> list[cirq.Operation]:
    operations = [_native_ry(target, angle / 2)]
    operations.extend(_native_cnot(second_control, target))
    operations.append(_native_ry(target, -angle / 2))
    operations.extend(_native_cnot(first_control, target))
    operations.append(_native_ry(target, angle / 2))
    operations.extend(_native_cnot(second_control, target))
    operations.append(_native_ry(target, -angle / 2))
    operations.extend(_native_cnot(first_control, target))
    return operations


def _scs_block(
    qubits: Sequence[cirq.Qid],
    m: int,
    k: int,
) -> list[cirq.Operation]:
    active = qubits[:m]
    angle = math.acos(1 / math.sqrt(m))
    operations = _native_cnot(active[m - 2], active[m - 1])
    operations.append(_native_ry(active[m - 2], angle))
    operations.extend(_native_cnot(active[m - 1], active[m - 2]))
    operations.append(_native_ry(active[m - 2], -angle))
    operations.extend(_native_cnot(active[m - 1], active[m - 2]))
    operations.extend(_native_cnot(active[m - 2], active[m - 1]))
    for ell in range(2, min(k, m - 1) + 1):
        top = active[m - ell - 1]
        middle = active[m - ell]
        bottom = active[m - 1]
        angle = math.acos(math.sqrt(ell / m))
        operations.extend(_native_cnot(top, bottom))
        operations.extend(_ccry(middle, bottom, top, angle))
        operations.extend(_native_cnot(top, bottom))
    return operations


def scs_dicke_circuit(target: DickeTarget) -> cirq.Circuit:
    """Independent split-and-cyclic-shift path construction."""
    qubits = tuple(cirq.LineQubit.range(target.num_qubits))
    circuit = cirq.Circuit()
    for qubit in qubits[target.num_qubits - target.num_excitations :]:
        circuit.append(
            cirq.PhasedXZGate(
                x_exponent=1,
                z_exponent=0,
                axis_phase_exponent=0,
            ).on(qubit)
        )
    for m in range(
        target.num_qubits,
        target.num_excitations,
        -1,
    ):
        circuit.append(_scs_block(qubits, m, target.num_excitations))
    for m in range(target.num_excitations, 1, -1):
        circuit.append(_scs_block(qubits, m, m - 1))
    graph = nx.Graph()
    graph.add_nodes_from(qubits)
    graph.add_edges_from(zip(qubits, qubits[1:], strict=False))
    mapper = cirq.HardCodedInitialMapper(
        {qubit: qubit for qubit in qubits}
    )
    routed = cirq.RouteCQC(graph)(circuit, initial_mapper=mapper)
    native = cirq.optimize_for_target_gateset(
        routed,
        gateset=cirq.CZTargetGateset(allow_partial_czs=False),
    )
    return cirq.Circuit(
        native.all_operations(),
        strategy=cirq.InsertStrategy.EARLIEST,
    )


BUILTIN_METHODS: dict[str, SynthesisMethod] = {
    "line_shallow": lambda target: line_dicke_circuit(
        target,
        state_aware_rows=True,
    ),
    "line_basic": lambda target: line_dicke_circuit(
        target,
        state_aware_rows=False,
    ),
    "scs_path": scs_dicke_circuit,
}
