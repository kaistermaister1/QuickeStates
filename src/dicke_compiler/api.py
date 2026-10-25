"""Stable public synthesis and optimization API."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

import cirq

from .hardware import HardwareTarget, candidate_paths, line_target
from .models import (
    CalibrationData,
    CompilationResult,
    DickeTarget,
    EquivalenceMode,
    Objective,
    VerificationReport,
    VerificationStatus,
)
from .passes import circuit_metrics, optimize_unitary
from .scoring import projected_survival
from .synthesis import BUILTIN_METHODS
from .templates import (
    DEFAULT_REGISTRY,
    TemplateRegistry,
    template_embeddings,
)
from .verification import verify_dicke_preparation
from .w_tree import compile_w_state


@dataclass(frozen=True)
class _Candidate:
    family: str
    circuit: cirq.Circuit
    outputs: tuple[cirq.Qid, ...]
    before: cirq.Circuit
    applied_passes: tuple[str, ...]
    survival: float | None
    verification: VerificationReport


def _objective_key(
    candidate: _Candidate,
    objective: Objective,
) -> tuple[float, ...]:
    metrics = circuit_metrics(candidate.circuit)
    if objective is Objective.DEPTH:
        return (metrics.moments, metrics.cz_depth, metrics.cz_count)
    if objective is Objective.CZ_DEPTH:
        return (metrics.cz_depth, metrics.moments, metrics.cz_count)
    if objective is Objective.CZ_COUNT:
        return (metrics.cz_count, metrics.cz_depth, metrics.moments)
    if candidate.survival is None:
        raise ValueError(
            "projected_survival objective requires calibration data"
        )
    return (
        -candidate.survival,
        metrics.cz_count,
        metrics.moments,
    )


def _map_to_path(
    circuit: cirq.AbstractCircuit,
    path: Sequence[cirq.Qid],
) -> cirq.Circuit:
    logical = cirq.LineQubit.range(len(path))
    return cirq.Circuit(
        circuit.transform_qubits(dict(zip(logical, path, strict=True)))
    )


def compile_dicke(
    num_qubits: int,
    num_excitations: int,
    *,
    hardware: HardwareTarget | None = None,
    calibration: CalibrationData | None = None,
    objective: Objective | str = Objective.DEPTH,
    methods: Sequence[str] = ("line_shallow", "line_basic"),
    verify: bool = True,
    atol: float = 1e-8,
    max_exact_qubits: int = 15,
    path_limit: int = 16,
    template_registry: TemplateRegistry = DEFAULT_REGISTRY,
    deadline_seconds: float | None = None,
) -> CompilationResult:
    """Compile an exact Dicke-state preparation and select the best candidate."""
    started = time.monotonic()
    target = DickeTarget(num_qubits, num_excitations)
    objective = Objective(objective)
    if hardware is None:
        hardware = line_target(num_qubits, calibration=calibration)
    elif calibration is not None:
        hardware = replace(hardware, calibration=calibration)
    calibration = hardware.calibration
    if objective is Objective.PROJECTED_SURVIVAL and calibration is None:
        raise ValueError(
            "projected_survival objective requires calibration data"
        )

    paths = candidate_paths(
        hardware,
        num_qubits,
        limit=path_limit,
    )

    candidates: list[_Candidate] = []
    for family in methods:
        if (
            deadline_seconds is not None
            and time.monotonic() - started >= deadline_seconds
        ):
            break
        try:
            builder = BUILTIN_METHODS[family]
        except KeyError as error:
            raise ValueError(f"unknown synthesis method {family!r}") from error
        before = builder(target)
        optimized, applied = optimize_unitary(before)
        logical_outputs = tuple(cirq.LineQubit.range(num_qubits))
        report = (
            verify_dicke_preparation(
                optimized,
                logical_outputs,
                target,
                atol=atol,
                max_exact_qubits=max_exact_qubits,
                analytic_family=family,
            )
            if verify
            else VerificationReport(
                VerificationStatus.UNVERIFIED,
                None,
                atol,
                "disabled",
            )
        )
        if report.status is VerificationStatus.FAILED:
            continue
        for path in paths:
            mapped = _map_to_path(optimized, path)
            violations = hardware.validate(mapped)
            if violations:
                continue
            survival = (
                projected_survival(
                    mapped,
                    path,
                    target,
                    calibration,
                )
                if calibration is not None
                else None
            )
            candidates.append(
                _Candidate(
                    family,
                    mapped,
                    tuple(path),
                    before,
                    applied,
                    survival,
                    report,
                )
            )

    if num_excitations in (1, num_qubits - 1):
        family = "w_tree" if num_excitations == 1 else "anti_w_tree"
        try:
            before, outputs = compile_w_state(
                hardware,
                num_qubits,
                complement=num_excitations == num_qubits - 1,
            )
            optimized, applied = optimize_unitary(before)
            report = (
                verify_dicke_preparation(
                    optimized,
                    outputs,
                    target,
                    atol=atol,
                    max_exact_qubits=max_exact_qubits,
                    analytic_family=family,
                )
                if verify
                else VerificationReport(
                    VerificationStatus.UNVERIFIED,
                    None,
                    atol,
                    "disabled",
                )
            )
            if (
                report.status is not VerificationStatus.FAILED
                and not hardware.validate(optimized)
            ):
                survival = (
                    projected_survival(
                        optimized,
                        outputs,
                        target,
                        calibration,
                    )
                    if calibration is not None
                    else None
                )
                candidates.append(
                    _Candidate(
                        family,
                        optimized,
                        tuple(outputs),
                        before,
                        applied,
                        survival,
                        report,
                    )
                )
        except ValueError:
            pass

    for template in template_registry.find(target):
        if (
            deadline_seconds is not None
            and time.monotonic() - started >= deadline_seconds
        ):
            break
        before = cirq.Circuit(template.circuit)
        optimized, applied = optimize_unitary(before)
        report = (
            verify_dicke_preparation(
                optimized,
                template.output_qubits,
                target,
                atol=atol,
                max_exact_qubits=max_exact_qubits,
                analytic_family=template.name,
            )
            if verify
            else VerificationReport(
                VerificationStatus.UNVERIFIED,
                None,
                atol,
                "disabled",
            )
        )
        if report.status is VerificationStatus.FAILED:
            continue
        for mapping in template_embeddings(
            template,
            hardware,
            limit=path_limit,
        ):
            mapped = cirq.Circuit(optimized.transform_qubits(mapping))
            violations = hardware.validate(mapped)
            if violations:
                continue
            outputs = tuple(
                mapping[qubit] for qubit in template.output_qubits
            )
            survival = (
                projected_survival(
                    mapped,
                    outputs,
                    target,
                    calibration,
                )
                if calibration is not None
                else None
            )
            candidates.append(
                _Candidate(
                    template.name,
                    mapped,
                    outputs,
                    before,
                    applied,
                    survival,
                    report,
                )
            )
    if not candidates:
        raise ValueError("no valid verified Dicke circuit was produced")
    selected = min(
        candidates,
        key=lambda candidate: (
            _objective_key(candidate, objective),
            candidate.family,
            tuple(map(str, candidate.outputs)),
        ),
    )
    return CompilationResult(
        circuit=selected.circuit,
        output_qubits=frozenset(selected.outputs),
        target=target,
        objective=objective,
        equivalence=EquivalenceMode.STATE_PREPARATION,
        candidate_family=selected.family,
        applied_passes=selected.applied_passes,
        before_metrics=circuit_metrics(selected.before),
        after_metrics=circuit_metrics(selected.circuit),
        projected_survival=selected.survival,
        runtime_seconds=time.monotonic() - started,
        verification=selected.verification,
        metadata={
            "hardware": hardware.name,
            "candidate_count": len(candidates),
            "path": tuple(map(str, selected.outputs)),
        },
    )


def optimize_dicke(
    circuit: cirq.AbstractCircuit,
    target: DickeTarget | tuple[int, int],
    *,
    output_qubits: Sequence[cirq.Qid] | None = None,
    hardware: HardwareTarget | None = None,
    calibration: CalibrationData | None = None,
    objective: Objective | str = Objective.DEPTH,
    equivalence: EquivalenceMode | str = EquivalenceMode.STATE_PREPARATION,
    verify: bool = True,
    atol: float = 1e-8,
    max_exact_qubits: int = 15,
) -> CompilationResult:
    """Optimize an existing Dicke preparation under an explicit contract."""
    started = time.monotonic()
    if not isinstance(target, DickeTarget):
        target = DickeTarget(*target)
    objective = Objective(objective)
    equivalence = EquivalenceMode(equivalence)
    if equivalence is EquivalenceMode.SUBSPACE:
        raise NotImplementedError(
            "custom subspace certificates are not implemented in version 0.1"
        )
    original = cirq.Circuit(circuit)
    if output_qubits is None:
        output_qubits = tuple(sorted(original.all_qubits(), key=str))
    else:
        output_qubits = tuple(output_qubits)
    if len(output_qubits) != target.num_qubits:
        raise ValueError("output_qubits must contain exactly N qubits")
    optimized, applied = optimize_unitary(original)
    baseline_report = (
        verify_dicke_preparation(
            optimized,
            output_qubits,
            target,
            atol=atol,
            max_exact_qubits=max_exact_qubits,
        )
        if verify and equivalence is EquivalenceMode.STATE_PREPARATION
        else VerificationReport(
            VerificationStatus.ANALYTIC,
            None,
            atol,
            "unitary_safe_passes",
        )
    )
    if baseline_report.status is VerificationStatus.FAILED:
        raise ValueError("input circuit does not prepare the declared target")

    active_calibration = calibration or (
        hardware.calibration if hardware is not None else None
    )
    baseline_survival = (
        projected_survival(
            optimized,
            output_qubits,
            target,
            active_calibration,
        )
        if active_calibration is not None
        else None
    )
    baseline = _Candidate(
        "input_optimized",
        optimized,
        tuple(output_qubits),
        original,
        applied,
        baseline_survival,
        baseline_report,
    )
    selected = baseline
    state_replacement = False
    if equivalence is EquivalenceMode.STATE_PREPARATION:
        compiled = compile_dicke(
            target.num_qubits,
            target.num_excitations,
            hardware=hardware,
            calibration=calibration,
            objective=objective,
            verify=verify,
            atol=atol,
            max_exact_qubits=max_exact_qubits,
        )
        replacement_circuit = compiled.circuit
        replacement_outputs = tuple(
            sorted(compiled.output_qubits, key=str)
        )
        if hardware is None:
            replacement_circuit = _map_to_path(
                replacement_circuit,
                output_qubits,
            )
            replacement_outputs = tuple(output_qubits)
        replacement = _Candidate(
            compiled.candidate_family,
            replacement_circuit,
            replacement_outputs,
            original,
            ("state_aware_resynthesis",) + compiled.applied_passes,
            compiled.projected_survival,
            compiled.verification,
        )
        selected = min(
            (baseline, replacement),
            key=lambda candidate: (
                _objective_key(candidate, objective),
                candidate.family,
            ),
        )
        state_replacement = selected is replacement

    return CompilationResult(
        circuit=selected.circuit,
        output_qubits=frozenset(selected.outputs),
        target=target,
        objective=objective,
        equivalence=equivalence,
        candidate_family=selected.family,
        applied_passes=selected.applied_passes,
        before_metrics=circuit_metrics(original),
        after_metrics=circuit_metrics(selected.circuit),
        projected_survival=selected.survival,
        runtime_seconds=time.monotonic() - started,
        verification=selected.verification,
        metadata={
            "state_aware_replacement": state_replacement,
            "hardware": hardware.name if hardware is not None else None,
        },
    )
