"""Analytic broadcast-tree synthesis for W and anti-W states."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cirq
import numpy as np

from .hardware import HardwareTarget

_H = np.array([[1, 1], [1, -1]], dtype=np.complex128) / math.sqrt(2)
_X = np.array([[0, 1], [1, 0]], dtype=np.complex128)
_IDENTITY = np.eye(2, dtype=np.complex128)


def _ry(angle: float) -> np.ndarray:
    cosine = math.cos(angle / 2)
    sine = math.sin(angle / 2)
    return np.array(
        [[cosine, -sine], [sine, cosine]],
        dtype=np.complex128,
    )


@dataclass(frozen=True)
class WTree:
    """A round-scheduled broadcast tree."""

    root: cirq.Qid
    calls: tuple[tuple[int, cirq.Qid, cirq.Qid], ...]
    nodes: tuple[cirq.Qid, ...]

    @property
    def rounds(self) -> int:
        return max((round_index for round_index, _, _ in self.calls), default=0)

    def subtree_sizes(self) -> dict[cirq.Qid, int]:
        sizes = {qubit: 1 for qubit in self.nodes}
        for _, parent, child in reversed(self.calls):
            sizes[parent] += sizes[child]
        return sizes


def _node_cost(hardware: HardwareTarget, qubit: cirq.Qid, weight: int) -> float:
    calibration = hardware.calibration
    if calibration is None:
        return 0.0
    zero, one = calibration.readout_rates(qubit)
    fraction = weight / len(hardware.qubits)
    return (
        calibration.single_qubit_rate(qubit)
        + (1 - fraction) * zero
        + fraction * one
    )


def _grow_tree(
    hardware: HardwareTarget,
    num_qubits: int,
    root: cirq.Qid,
    weight: int,
) -> WTree | None:
    adjacency = hardware.adjacency()
    informed = [root]
    informed_set = {root}
    calls: list[tuple[int, cirq.Qid, cirq.Qid]] = []
    round_index = 0
    while len(informed) < num_qubits:
        round_index += 1
        callers = [root] if round_index == 1 else list(informed)
        edges = []
        for parent in callers:
            for child in adjacency[parent]:
                if child in informed_set:
                    continue
                edge_cost = (
                    hardware.calibration.two_qubit_rate(parent, child)
                    if hardware.calibration is not None
                    else 0.0
                )
                edges.append(
                    (
                        edge_cost + _node_cost(hardware, child, weight),
                        str(parent),
                        str(child),
                        parent,
                        child,
                    )
                )
        edges.sort()
        used_parents: set[cirq.Qid] = set()
        used_children: set[cirq.Qid] = set()
        additions = []
        for _, _, _, parent, child in edges:
            if parent in used_parents or child in used_children:
                continue
            used_parents.add(parent)
            used_children.add(child)
            additions.append((parent, child))
            if len(informed) + len(additions) >= num_qubits:
                break
        if not additions:
            return None
        for parent, child in additions:
            calls.append((round_index, parent, child))
            informed.append(child)
            informed_set.add(child)
    return WTree(root, tuple(calls), tuple(informed))


def best_w_tree(
    hardware: HardwareTarget,
    num_qubits: int,
    *,
    complement: bool = False,
) -> WTree:
    """Find a deterministic shallow broadcast tree on the coupling graph."""
    weight = num_qubits - 1 if complement else 1
    candidates = []
    for root in sorted(
        hardware.qubits,
        key=lambda qubit: (_node_cost(hardware, qubit, weight), str(qubit)),
    ):
        tree = _grow_tree(hardware, num_qubits, root, weight)
        if tree is not None:
            candidates.append(tree)
    if not candidates:
        raise ValueError(
            f"{hardware.name!r} cannot grow a {num_qubits}-qubit W tree"
        )
    return min(
        candidates,
        key=lambda tree: (
            tree.rounds,
            sum(
                hardware.calibration.two_qubit_rate(parent, child)
                if hardware.calibration is not None
                else 0.0
                for _, parent, child in tree.calls
            ),
            tuple(map(str, tree.nodes)),
        ),
    )


def w_tree_circuit(
    tree: WTree,
    *,
    complement: bool = False,
) -> cirq.Circuit:
    """Build an exact CZ/PhasedXZ W or anti-W preparation."""
    num_qubits = len(tree.nodes)
    subtree_sizes = tree.subtree_sizes()
    remaining = dict(subtree_sizes)
    cz_layers = 0 if tree.rounds == 0 else 2 * tree.rounds - 1
    one_qubit = [dict() for _ in range(cz_layers + 1)]
    czs: list[list[tuple[cirq.Qid, cirq.Qid]]] = [
        [] for _ in range(cz_layers)
    ]

    def add_matrix(layer: int, qubit: cirq.Qid, matrix: np.ndarray) -> None:
        one_qubit[layer].setdefault(qubit, []).append(matrix)

    if num_qubits == 1:
        add_matrix(0, tree.root, _X)
    for round_index, parent, child in tree.calls:
        child_size = subtree_sizes[child]
        if round_index == 1:
            beta = 2 * math.asin(
                math.sqrt((num_qubits - child_size) / num_qubits)
            )
            add_matrix(0, parent, _ry(beta))
            add_matrix(0, child, _H)
            czs[0].append((parent, child))
            add_matrix(1, child, _H)
            add_matrix(1, child, _X)
        else:
            layer = 2 * round_index - 3
            phi = math.asin(
                math.sqrt(child_size / remaining[parent])
            )
            angle = math.pi / 2 - phi
            add_matrix(layer, child, _ry(angle))
            add_matrix(layer, child, _H)
            czs[layer].append((parent, child))
            add_matrix(layer + 1, child, _H)
            add_matrix(layer + 1, child, _ry(-angle))
            add_matrix(layer + 1, parent, _H)
            czs[layer + 1].append((parent, child))
            add_matrix(layer + 2, parent, _H)
        remaining[parent] -= child_size
    if complement:
        for qubit in tree.nodes:
            add_matrix(cz_layers, qubit, _X)

    moments = []
    for layer in range(cz_layers + 1):
        operations = []
        for qubit, matrices in one_qubit[layer].items():
            matrix = _IDENTITY
            for item in matrices:
                matrix = item @ matrix
            if not cirq.equal_up_to_global_phase(
                matrix,
                _IDENTITY,
                atol=1e-12,
            ):
                operations.append(
                    cirq.PhasedXZGate.from_matrix(matrix).on(qubit)
                )
        if operations:
            moments.append(cirq.Moment(operations))
        if layer < cz_layers:
            moments.append(
                cirq.Moment(cirq.CZ(*pair) for pair in czs[layer])
            )
    return cirq.Circuit(moments)


def compile_w_state(
    hardware: HardwareTarget,
    num_qubits: int,
    *,
    complement: bool = False,
) -> tuple[cirq.Circuit, tuple[cirq.Qid, ...]]:
    """Compile a W/anti-W state directly onto a hardware target."""
    tree = best_w_tree(hardware, num_qubits, complement=complement)
    return w_tree_circuit(tree, complement=complement), tree.nodes
