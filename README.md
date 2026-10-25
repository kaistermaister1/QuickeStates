# QuickeStates

Exact, hardware-aware Dicke-state preparation circuits for Google's Willow processor.

Built for DuQuantum 2026 (Google Quantum AI Dicke-state challenge) by Kai Sandberg, Will and Ben.

## What's in this repository

| Path | Contents |
|---|---|
| `src/dicke_compiler/`, `pyproject.toml`, `LICENSE` | The `quicke-states` Python library ([PyPI](https://pypi.org/project/quicke-states/)). |
| `willow_dicke.py` | Our challenge submission: a standalone `create_dicke_state(...)` and `create_dicke_state_adaptive(...)` (bonus). |
| `DuQuantum_Google_Standard.ipynb` | The official challenge notebook with both functions implemented, using the same code as `willow_dicke.py`. |

## Inspiration

Dicke states are useful in quantum algorithms. Researchers have developed several ways to prepare them on both noiseless and NISQ devices ([Bärtschi & Eidenbenz](https://arxiv.org/abs/1904.07358), [Aktar et al.](https://arxiv.org/abs/2112.12435), [Mukherjee et al.](https://arxiv.org/abs/2007.01681)). We wanted to understand these methods and adapt them to Google's Willow chip, taking its error rates and connectivity into account.

## What it does

QuickeStates is an importable Python library that gives users efficient Dicke-state preparation circuits tuned to their hardware and error rates. The user specifies:

- the hardware device;
- the desired Dicke state;
- error rates for CZ gates, single-qubit unitaries and qubit measurements.

Across 27 benchmark cases, our circuits averaged 70.2% fewer CZ gates and 80.7% less circuit depth than our naive Bärtschi-SCS baseline. They also improved fidelity indicators threefold.

## Installation

```bash
pip install quicke-states
```

Or install from this repository:

```bash
pip install "git+https://github.com/kaistermaister1/QuickeStates.git"
```

Google Willow support needs `cirq_google.Willow105`, which is only in Cirq pre-releases:

```bash
pip install --pre cirq cirq-google
```

## Using the library

```python
from dicke_compiler import compile_dicke, willow_target

result = compile_dicke(6, 3, hardware=willow_target())
print(result.circuit)
print(result.after_metrics)
print(result.verification)
```

To compile against your own coupling graph and calibration data:

```python
from dicke_compiler import CalibrationData, HardwareTarget, compile_dicke

calibration = CalibrationData(
    single_qubit=sq_errors,
    two_qubit=cz_errors,
    readout_zero=zero_to_one_errors,
    readout_one=one_to_zero_errors,
)
hardware = HardwareTarget.from_edges(device_qubits, device_couplers, calibration=calibration)
result = compile_dicke(6, 3, hardware=hardware, objective="projected_survival")
```

From the command line:

```bash
quicke-states 6 3 --objective depth --print-circuit
```

Every result records the construction it used, before/after metrics and verification evidence. Circuits with up to 15 qubits are checked by exact complex128 statevector simulation. `optimize_dicke` re-optimizes an existing preparation circuit. `TemplateRegistry` lets you add your own verified constructions.

## Using the challenge submission

`willow_dicke.py` imports only `cirq`, `cirq_google`, their dependencies and the Python standard library, as the challenge requires. The calibration dictionaries use the notebook's format; the notebook's `generate_*` functions produce fake ones for testing.

```python
from willow_dicke import create_dicke_state, create_dicke_state_adaptive

circuit, qubits = create_dicke_state(
    10, 3, cz_pauli_error_rate, sq_pauli_error_rate, zero_errors, one_errors
)
circuit, qubits = create_dicke_state_adaptive(10, 3)  # bonus: fewest moments
```

`create_dicke_state` returns a measurement-free circuit of CZ gates on calibrated couplers and `cirq.PhasedXZGate`s on calibrated qubits. It works as follows:

1. **Candidates.** It gathers exact constructions for `D(N, min(n, N−n))`. These include:
   - W-state broadcast trees;
   - numerically synthesized templates and peephole-optimized circuits;
   - divide-and-conquer constructions;
   - a support-pruned nearest-neighbour Bärtschi–Eidenbenz chain.

   For `n > N/2` it prepares the complement and flips every output qubit.
2. **Placement.** A beam search embeds each candidate on the supplied hardware graph. It minimizes CZ, single-qubit and readout error costs, and skips broken qubits and couplers.
3. **Scheduling.** CZ and single-qubit layers alternate, scheduled as late as possible so qubits stay in |0⟩ longer. Z rotations are pushed through, and dynamical decoupling is tried on idle qubits.
4. **Selection.** Candidates are ranked by an estimate of gate, readout and idle errors. The best one that passes a noiseless statevector check and the 1750-moment limit is returned.

`create_dicke_state_adaptive` returns the circuit with the fewest moments among two options:

- adaptive constructions that use mid-circuit measurement and feed-forward;
- depth-minimal brickwork circuits.

### Validation

We ran the notebook's own code cells on a fake calibration from its `generate_*` functions. `create_dicke_state` produced the exact Dicke state for all 56 challenge instances (2 ≤ N ≤ 15, 1 ≤ n ≤ ⌊N/2⌋), with 1 − F below 1e-13. Every circuit:

- passed `cirq_google.Willow105.validate_circuit`;
- used CZ only on calibrated couplers and `PhasedXZGate` only on calibrated qubits;
- had no measurements and at most 127 moments.

The slowest build took 11.4 s, well under the one-minute limit.

For all 56 bonus instances, the notebook's `create_dicke_state_adaptive` returns the same circuits as `willow_dicke.py`. We also simulated 54 of them without noise and confirmed they are exact, using four random measurement seeds for the adaptive ones. D(10,5) and D(13,6) were too large to simulate: they use 69 and 20 qubits.

## How we built it

We reviewed five papers and ran several experiments to find out which techniques held up under errors. We then handed those techniques to LLMs for heavy, specialized optimization. Approaches we explored include:

- Dijkstra's algorithm for Bärtschi's 1D Dicke-state construction;
- recursive and divide-and-conquer constructions;
- delete-and-tune optimization loops.

Several agentic models ran for more than 15 hours while we fed them our data and the insights from our experiments.

## Challenges we ran into

One of our biggest challenges was that improvements stayed marginal despite a wide range of ideas and a lot of compute. Keeping the models working was not enough: their progress was impressive but quickly plateaued. We had to take a step back (admittedly at 3 a.m.) and review the core mathematical ideas, which led to more productive approaches.

## Accomplishments that we're proud of

We're proud that QuickeStates produces Willow-specialized Dicke-state preparation circuits for arbitrary error rates. We're also proud of the agentic workflow we built to iterate quickly between human insight and numerical optimization. The result is a working implementation of state-of-the-art preparation techniques on a specific quantum chip.

## What's next

We plan to apply our Dicke-state adaptations and optimization loop to other quantum chips and add them to QuickeStates. We also want to automate the process so that new quantum devices can be optimized for as soon as they become available.

## References

- A. Bärtschi and S. Eidenbenz, "Deterministic Preparation of Dicke States," [arXiv:1904.07358](https://arxiv.org/abs/1904.07358) (2019).
- S. Aktar, A. Bärtschi, A.-H. A. Badawy and S. Eidenbenz, "A Divide-and-Conquer Approach to Dicke State Preparation," [arXiv:2112.12435](https://arxiv.org/abs/2112.12435) (2021).
- C. S. Mukherjee, S. Maitra, V. Gaurav and D. Roy, "On Actual Preparation of Dicke State on a Quantum Computer," [arXiv:2007.01681](https://arxiv.org/abs/2007.01681) (2020).

## License

BSD 3-Clause. See [LICENSE](LICENSE).
