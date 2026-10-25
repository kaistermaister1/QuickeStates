"""Generic hardware descriptions and calibration-aware path placement."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import cirq

from .models import CalibrationData


@dataclass(frozen=True)
class HardwareTarget:
    """A qubit set and undirected two-qubit coupling graph."""

    qubits: tuple[cirq.Qid, ...]
    couplers: frozenset[frozenset[cirq.Qid]]
    calibration: CalibrationData | None = None
    name: str = "custom"
    native_cz_phxz: bool = True

    @classmethod
    def from_edges(
        cls,
        qubits: Iterable[cirq.Qid],
        couplers: Iterable[tuple[cirq.Qid, cirq.Qid]],
        *,
        calibration: CalibrationData | None = None,
        name: str = "custom",
        native_cz_phxz: bool = True,
    ) -> HardwareTarget:
        return cls(
            tuple(qubits),
            frozenset(frozenset(pair) for pair in couplers),
            calibration,
            name,
            native_cz_phxz,
        )

    def validate(self, circuit: cirq.AbstractCircuit) -> tuple[str, ...]:
        """Return structural violations for this target."""
        violations: list[str] = []
        available = set(self.qubits)
        if not circuit.all_qubits().issubset(available):
            violations.append("circuit uses qubits outside the hardware target")
        for operation in circuit.all_operations():
            if len(operation.qubits) == 2:
                if frozenset(operation.qubits) not in self.couplers:
                    violations.append(
                        f"operation uses unavailable coupler {operation.qubits!r}"
                    )
                if self.native_cz_phxz and operation.gate != cirq.CZ:
                    violations.append(
                        f"non-native two-qubit operation {operation!r}"
                    )
            elif len(operation.qubits) == 1:
                if (
                    self.native_cz_phxz
                    and not isinstance(operation.gate, cirq.PhasedXZGate)
                ):
                    violations.append(
                        f"non-native single-qubit operation {operation!r}"
                    )
            else:
                violations.append(
                    f"unsupported operation arity for {operation!r}"
                )
        return tuple(dict.fromkeys(violations))

    def adjacency(self) -> dict[cirq.Qid, tuple[cirq.Qid, ...]]:
        neighbors: dict[cirq.Qid, list[cirq.Qid]] = {
            qubit: [] for qubit in self.qubits
        }
        for pair in self.couplers:
            first, second = tuple(pair)
            neighbors[first].append(second)
            neighbors[second].append(first)
        return {
            qubit: tuple(sorted(values, key=str))
            for qubit, values in neighbors.items()
        }


def line_target(
    num_qubits: int,
    *,
    calibration: CalibrationData | None = None,
) -> HardwareTarget:
    """Return a generic linear CZ/PhasedXZ target."""
    qubits = tuple(cirq.LineQubit.range(num_qubits))
    return HardwareTarget.from_edges(
        qubits,
        zip(qubits, qubits[1:], strict=False),
        calibration=calibration,
        name=f"line-{num_qubits}",
    )


def willow_target(
    calibration: CalibrationData | None = None,
) -> HardwareTarget:
    """Return Google Willow105 without making cirq-google a core dependency."""
    try:
        import cirq_google
    except ImportError as error:  # pragma: no cover - optional dependency
        raise ImportError(
            "Willow support requires `pip install dicke-compiler[willow]`"
        ) from error
    device = cirq_google.Willow105
    return HardwareTarget.from_edges(
        sorted(device.metadata.qubit_set),
        (tuple(pair) for pair in device.metadata.qubit_pairs),
        calibration=calibration,
        name="willow-105",
    )


def candidate_paths(
    hardware: HardwareTarget,
    length: int,
    *,
    limit: int = 16,
    beam_width: int = 1024,
) -> list[tuple[cirq.Qid, ...]]:
    """Find deterministic low-risk simple paths using a bounded beam search."""
    if length < 1:
        raise ValueError("length must be positive")
    if length > len(hardware.qubits):
        return []
    adjacency = hardware.adjacency()
    calibration = hardware.calibration

    def node_cost(qubit: cirq.Qid) -> float:
        if calibration is None:
            return 0.0
        zero, one = calibration.readout_rates(qubit)
        return calibration.single_qubit_rate(qubit) + (zero + one) / 2

    states = [
        (node_cost(qubit), (qubit,))
        for qubit in sorted(hardware.qubits, key=str)
    ]
    for _ in range(1, length):
        expanded: list[tuple[float, tuple[cirq.Qid, ...]]] = []
        for cost, path in states:
            for neighbor in adjacency[path[-1]]:
                if neighbor in path:
                    continue
                edge_cost = (
                    calibration.two_qubit_rate(path[-1], neighbor)
                    if calibration is not None
                    else 0.0
                )
                expanded.append(
                    (cost + node_cost(neighbor) + edge_cost, path + (neighbor,))
                )
        if not expanded:
            return []
        expanded.sort(
            key=lambda item: (item[0], tuple(map(str, item[1])))
        )
        states = expanded[:beam_width]
    distinct: list[tuple[cirq.Qid, ...]] = []
    seen: set[tuple[cirq.Qid, ...]] = set()
    for _, path in sorted(
        states,
        key=lambda item: (item[0], tuple(map(str, item[1]))),
    ):
        reversed_path = tuple(reversed(path))
        canonical = min(
            path,
            reversed_path,
            key=lambda item: tuple(map(str, item)),
        )
        if canonical not in seen:
            seen.add(canonical)
            distinct.append(path)
            if len(distinct) >= limit:
                break
    return distinct
