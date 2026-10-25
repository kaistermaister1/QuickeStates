"""Command-line interface for quick compilation and inspection."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .api import compile_dicke
from .models import Objective


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile a Dicke-state preparation circuit."
    )
    parser.add_argument("num_qubits", type=int)
    parser.add_argument("num_excitations", type=int)
    parser.add_argument(
        "--objective",
        choices=[objective.value for objective in Objective],
        default=Objective.DEPTH.value,
    )
    parser.add_argument(
        "--print-circuit",
        action="store_true",
        help="Print the resulting Cirq circuit after the JSON summary.",
    )
    arguments = parser.parse_args()
    result = compile_dicke(
        arguments.num_qubits,
        arguments.num_excitations,
        objective=arguments.objective,
    )
    summary = {
        "target": asdict(result.target),
        "candidate_family": result.candidate_family,
        "objective": result.objective.value,
        "before_metrics": asdict(result.before_metrics),
        "after_metrics": asdict(result.after_metrics),
        "projected_survival": result.projected_survival,
        "verification": asdict(result.verification),
        "runtime_seconds": result.runtime_seconds,
    }
    print(json.dumps(summary, indent=2, default=str))
    if arguments.print_circuit:
        print(result.circuit)


if __name__ == "__main__":
    main()
