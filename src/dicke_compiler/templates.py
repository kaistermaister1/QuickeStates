"""Versioned registry for verified state-preparation templates."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from importlib import resources

import cirq

from .hardware import HardwareTarget
from .models import DickeTarget, EquivalenceMode


@dataclass(frozen=True)
class CircuitTemplate:
    """A verified circuit and the contract under which it may be substituted."""

    name: str
    target: DickeTarget
    circuit: cirq.FrozenCircuit
    output_qubits: tuple[cirq.Qid, ...]
    equivalence: EquivalenceMode
    source: str
    preferred_qubits: tuple[cirq.Qid, ...] | None = None


class TemplateRegistry:
    """Mutable research registry with deterministic lookup."""

    def __init__(self) -> None:
        self._templates: dict[tuple[int, int], list[CircuitTemplate]] = {}

    def register(self, template: CircuitTemplate) -> None:
        key = (
            template.target.num_qubits,
            template.target.num_excitations,
        )
        if len(template.output_qubits) != template.target.num_qubits:
            raise ValueError("template output count does not match its target")
        if (
            template.preferred_qubits is not None
            and len(template.preferred_qubits) != len(template.output_qubits)
        ):
            raise ValueError(
                "preferred qubit count does not match template outputs"
            )
        existing = self._templates.setdefault(key, [])
        if any(item.name == template.name for item in existing):
            raise ValueError(f"template {template.name!r} is already registered")
        existing.append(template)
        existing.sort(key=lambda item: item.name)

    def find(self, target: DickeTarget) -> tuple[CircuitTemplate, ...]:
        return tuple(
            self._templates.get(
                (target.num_qubits, target.num_excitations),
                (),
            )
        )


def template_embeddings(
    template: CircuitTemplate,
    hardware: HardwareTarget,
    *,
    limit: int = 32,
    search_budget: int = 100_000,
) -> tuple[dict[cirq.Qid, cirq.Qid], ...]:
    """Enumerate bounded subgraph embeddings for a template interaction graph."""
    if search_budget < 0:
        raise ValueError("search_budget must be non-negative")
    logical = template.output_qubits
    logical_neighbors = {qubit: set() for qubit in logical}
    for operation in template.circuit.all_operations():
        if len(operation.qubits) == 2:
            first, second = operation.qubits
            logical_neighbors[first].add(second)
            logical_neighbors[second].add(first)
    ordered = tuple(
        sorted(
            logical,
            key=lambda qubit: (-len(logical_neighbors[qubit]), str(qubit)),
        )
    )
    physical_neighbors = {
        qubit: set(neighbors)
        for qubit, neighbors in hardware.adjacency().items()
    }
    mappings: list[dict[cirq.Qid, cirq.Qid]] = []
    if template.preferred_qubits is not None:
        preferred = dict(
            zip(logical, template.preferred_qubits, strict=True)
        )
        if all(qubit in physical_neighbors for qubit in preferred.values()):
            valid = all(
                preferred[neighbor] in physical_neighbors[preferred[qubit]]
                for qubit, neighbors in logical_neighbors.items()
                for neighbor in neighbors
            )
            if valid:
                mappings.append(preferred)
                if len(logical) > 10 or len(mappings) >= limit:
                    return tuple(mappings)
    search_steps = 0

    def search(
        index: int,
        mapping: dict[cirq.Qid, cirq.Qid],
        used: set[cirq.Qid],
    ) -> None:
        nonlocal search_steps
        search_steps += 1
        if search_steps > search_budget:
            return
        if len(mappings) >= limit:
            return
        if index == len(ordered):
            mappings.append(dict(mapping))
            return
        logical_qubit = ordered[index]
        candidates = sorted(
            hardware.qubits,
            key=lambda qubit: (
                -len(physical_neighbors[qubit]),
                str(qubit),
            ),
        )
        for physical_qubit in candidates:
            if physical_qubit in used:
                continue
            if len(physical_neighbors[physical_qubit]) < len(
                logical_neighbors[logical_qubit]
            ):
                continue
            compatible = all(
                mapping[neighbor] in physical_neighbors[physical_qubit]
                for neighbor in logical_neighbors[logical_qubit]
                if neighbor in mapping
            )
            if not compatible:
                continue
            mapping[logical_qubit] = physical_qubit
            used.add(physical_qubit)
            search(index + 1, mapping, used)
            used.remove(physical_qubit)
            del mapping[logical_qubit]

    search(0, {}, set())
    return tuple(mappings)


def _square_dicke_4_2() -> CircuitTemplate:
    qubits = tuple(cirq.GridQubit.rect(2, 2))
    first, second, third, fourth = (
        qubits[0],
        qubits[1],
        qubits[3],
        qubits[2],
    )

    def ry(qubit: cirq.Qid, angle: float) -> cirq.Operation:
        return cirq.PhasedXZGate(
            x_exponent=angle / math.pi,
            z_exponent=0,
            axis_phase_exponent=0.5,
        ).on(qubit)

    circuit = cirq.FrozenCircuit(
        cirq.Moment(
            [
                ry(first, math.acos(-1 / 3)),
                ry(second, math.pi / 4),
                ry(third, math.pi / 2),
                ry(fourth, -math.pi / 2),
            ]
        ),
        cirq.Moment(
            [cirq.CZ(first, second), cirq.CZ(third, fourth)]
        ),
        cirq.Moment([ry(second, math.pi / 4), cirq.CZ(fourth, first)]),
        cirq.Moment(
            [
                cirq.CZ(second, third),
                ry(fourth, math.pi / 2),
                ry(first, -math.pi / 2),
            ]
        ),
        cirq.Moment([ry(second, math.pi / 2)]),
        cirq.Moment([cirq.CZ(first, second)]),
        cirq.Moment([ry(first, math.pi / 2)]),
    )
    return CircuitTemplate(
        name="analytic_square_4_2",
        target=DickeTarget(4, 2),
        circuit=circuit,
        output_qubits=(first, second, third, fourth),
        equivalence=EquivalenceMode.STATE_PREPARATION,
        source="DuQuantum analytic square construction",
    )


def _decode_compact_template(
    name: str,
    target: DickeTarget,
    moments: list[list[tuple]],
    *,
    source: str = "DuQuantum offline synthesis; complex128 regression verified",
    preferred_qubits: tuple[cirq.Qid, ...] | None = None,
) -> CircuitTemplate:
    qubits = tuple(cirq.LineQubit.range(target.num_qubits))
    decoded = []
    for entries in moments:
        operations = []
        for entry in entries:
            if entry[0] == "cz":
                operations.append(cirq.CZ(qubits[entry[1]], qubits[entry[2]]))
            else:
                operations.append(
                    cirq.PhasedXZGate(
                        x_exponent=entry[2],
                        z_exponent=entry[3],
                        axis_phase_exponent=entry[4],
                    ).on(qubits[entry[1]])
                )
        decoded.append(cirq.Moment(operations))
    return CircuitTemplate(
        name=name,
        target=target,
        circuit=cirq.FrozenCircuit(decoded),
        output_qubits=qubits,
        equivalence=EquivalenceMode.STATE_PREPARATION,
        source=source,
        preferred_qubits=preferred_qubits,
    )


_BALANCED_6_3 = [
    [("phxz", 0, 1376.3040867239847, 6203.0, 0.5), ("phxz", 1, -0.3040867239846963, 1.0, 0.5), ("phxz", 2, -3326.6309898804348, 682.5, 0.5), ("phxz", 3, 0.227185525828505, 4.180927621920264e-17, 0.5), ("phxz", 4, 0.5, -7.742942702846523e-17, 0.5), ("phxz", 5, 0.5, -0.5, 0.5)],
    [("cz", 0, 2)],
    [("phxz", 0, -1764.3040867239847, -1590.0000000000002, 0.5), ("cz", 2, 3)],
    [("phxz", 2, -0.5, -0.25000000000000006, 0.5), ("phxz", 3, -0.227185525828505, 1.0, 0.5)],
    [("cz", 1, 3), ("cz", 2, 4)],
    [("phxz", 1, 0.6959132760153038, -2.778751687005279e-17, 0.5), ("phxz", 3, 0.2499999999999999, -1.0, 0.5), ("phxz", 2, 0.5, 0.49999999999999994, 0.5)],
    [("cz", 0, 2), ("cz", 3, 1)],
    [("phxz", 0, 0.5000000000000001, 0.5000000000000001, 0.5), ("phxz", 2, -0.25, -2.5, 0.5), ("phxz", 3, 0.7500000000000001, -1.0, 0.5)],
    [("cz", 2, 4), ("cz", 5, 3)],
    [("phxz", 3, 0.5, 0.5000000000000001, 0.5), ("cz", 5, 4)],
    [("cz", 3, 1), ("phxz", 5, 0.5, 4536.370029688775, 0.5), ("phxz", 4, 0.5, 4536.870029688775, 0.5)],
    [("phxz", 1, 0.5, 0.5, 0.5), ("cz", 3, 2)],
    [("cz", 1, 0), ("phxz", 3, -0.49999999999999994, -4537.629970311225, 0.5), ("phxz", 2, 3813.5, 2459.870029688775, 0.5)],
    [("phxz", 1, 0.49999999999999994, 4537.370029688775, 0.5), ("phxz", 0, 0.5, 4536.870029688775, 0.5)],
]

_BALANCED_8_4 = [
    [("phxz", 0, 0.21859111351161822, -0.5000000000000016, 0.5), ("phxz", 1, 0.5, -5991.363961377644, 0.5), ("phxz", 2, 0.5, -1.0, 0.5), ("phxz", 3, 0.6459721624073824, -2.0, 0.5), ("phxz", 4, 0.5, -2516.846488170638, 0.5), ("phxz", 5, 0.5, -0.5, 0.5), ("phxz", 6, 0.6959132760153036, -4.3401850930671934e-17, 0.5), ("phxz", 7, 0.5, -0.5, 0.5)],
    [("cz", 0, 1)],
    [("phxz", 0, -1.0038758162973733, -2.0000000000000013, 0.5), ("cz", 1, 4)],
    [("cz", 0, 3), ("phxz", 1, 0.5228141721046287, -649.6702398776288, 0.5), ("phxz", 4, 1614.5060204698057, 6550.344745910827, 0.5)],
    [("phxz", 0, 0.34546860330888607, -1.0, 0.5), ("phxz", 3, -1.153912167686454, 6822.407375740574, 0.5), ("cz", 1, 2)],
    [("cz", 6, 3), ("phxz", 1, 0.7730855641139139, -0.12687635107644896, 0.5), ("phxz", 2, 0.5, -649.9042862624343, 0.5)],
    [("phxz", 6, 0.19591327601530362, 0.5000000000000001, 0.5), ("phxz", 3, -1.0000000000000002, 6822.407375740574, 0.5), ("cz", 0, 1)],
    [("cz", 3, 4), ("phxz", 0, 0.5, 0.5000000000000001, 0.5), ("phxz", 1, 0.2858525318292365, -59.09692417965118, 0.5)],
    [("phxz", 3, 0.5, 24901.491702879117, 0.5), ("phxz", 4, 0.24063253699046155, 3670.7500000000005, 0.5), ("cz", 1, 0)],
    [("cz", 3, 4), ("phxz", 0, 0.3213834548125703, 0.75, 0.5), ("cz", 1, 2)],
    [("phxz", 3, -0.9999999999999996, 24901.741702879117, 0.5), ("phxz", 1, 0.75, -1.0000000000000002, 0.5), ("phxz", 2, -1.0, -1299.0932286993045, 0.5)],
    [("cz", 3, 6), ("cz", 2, 5)],
    [("phxz", 3, 0.5000000000000001, -0.5, 0.5), ("phxz", 6, 0.24999999999999992, -1.0000000000000002, 0.5), ("phxz", 2, 0.5, 18021.71484241765, 0.5), ("cz", 4, 5)],
    [("cz", 1, 2), ("phxz", 4, 0.5, -1.0, 0.5), ("phxz", 5, 0.5, -0.5, 0.5)],
    [("phxz", 1, 0.5, -0.5, 0.5), ("phxz", 2, -1.0, 18020.71484241765, 0.5), ("cz", 3, 4)],
    [("cz", 1, 0), ("phxz", 3, 0.5, -1343.4996989341191, 0.5), ("phxz", 4, 0.5, -0.5000000000000001, 0.5)],
    [("phxz", 1, 0.5, -0.5, 0.5), ("phxz", 0, 0.5, -1.7476160733215138e-17, 0.5), ("cz", 6, 3), ("cz", 7, 4)],
    [("phxz", 6, 0.5, -0.5, 0.5), ("phxz", 3, -1.0, -1342.4996989341191, 0.5), ("phxz", 4, 0.5, -13358.71404856638, 0.5)],
    [("cz", 7, 6), ("cz", 4, 1)],
    [("phxz", 7, 0.5, 0.6936596704594525, 0.5), ("phxz", 6, 0.5, 0.19365967045945254, 0.5), ("phxz", 4, -1.0, -13359.21404856638, 0.5), ("phxz", 1, 0.5, -0.5, 0.5)],
    [("cz", 4, 3), ("cz", 1, 0)],
    [("phxz", 4, 0.5, -0.30634032954054746, 0.5), ("phxz", 3, 0.5, 0.19365967045945254, 0.5), ("phxz", 1, 0.5, 0.6936596704594525, 0.5), ("phxz", 0, 0.5, 0.19365967045945254, 0.5)],
    [("cz", 4, 5), ("cz", 1, 2)],
    [("phxz", 5, 0.5, 0.19365967045945254, 0.5), ("phxz", 2, 0.5, 0.19365967045945254, 0.5)],
]


def _load_best_known_templates() -> tuple[CircuitTemplate, ...]:
    asset = resources.files("dicke_compiler").joinpath(
        "data/best_known_templates.json"
    )
    document = json.loads(asset.read_text(encoding="utf-8"))
    if document.get("format_version") != 1:
        raise ValueError("unsupported best-known template format")
    records = document.get("templates", [])
    if len(records) != document.get("template_count"):
        raise ValueError("best-known template count is inconsistent")
    templates = []
    for record in records:
        payload = {
            key: value
            for key, value in record.items()
            if key != "sha256"
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        if digest != record.get("sha256"):
            raise ValueError(
                f"template provenance hash failed for {record.get('name')}"
            )
        templates.append(
            _decode_compact_template(
                record["name"],
                DickeTarget(
                    record["num_qubits"],
                    record["num_excitations"],
                ),
                record["moments"],
                source=record["source"],
                preferred_qubits=tuple(
                    cirq.GridQubit(row, col)
                    for row, col in record.get("grid_qubits", [])
                )
                or None,
            )
        )
    return tuple(templates)


DEFAULT_REGISTRY = TemplateRegistry()
DEFAULT_REGISTRY.register(_square_dicke_4_2())
DEFAULT_REGISTRY.register(
    _decode_compact_template(
        "balanced_template_6_3",
        DickeTarget(6, 3),
        _BALANCED_6_3,
    )
)
DEFAULT_REGISTRY.register(
    _decode_compact_template(
        "balanced_template_8_4",
        DickeTarget(8, 4),
        _BALANCED_8_4,
    )
)
for _template in _load_best_known_templates():
    DEFAULT_REGISTRY.register(_template)
