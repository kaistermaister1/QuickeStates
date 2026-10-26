"""Willow Dicke preparation: choose an exact circuit, then adapt its placement.

Five steps in create_dicke_state(N, k, CZ errors, 1q errors, readout 0/1 errors):
1. Normalize: validate N,k and keep calibrated Willow105 qubits/couplers. Use
   weight min(k,N-k); append X on every output when the requested k is larger.
2. Get candidates: load saved native circuits (249 across 58 reduced (N,k)
   cases; five for D(8,4)). Offline work combined W trees, unary/SCS recursion,
   divide-and-conquer count splitting, and numerical synthesis. Numerical
   synthesis fits rotation angles to the ideal target; delete-and-refit removes
   gates, then fits again. Local compression preserves only reachable block
   inputs, with their relative phases; global compression fits the whole state.
   These searches ran offline, not on each call. If templates are missing or
   cannot be placed, build our analytic sliding-unary variation: five-CNOT
   reflect blocks, fused four-CZ top rows, and a six-CZ weight-two end block.
3. Compile/place: saved circuits already contain full CZ + PhasedXZ gates and
   parallel time layers (Cirq moments). The analytic fallback decomposes local
   blocks, merges 1q gates and packs operations EARLIEST. Try grid translations,
   rotations/reflections, the original placement, and a bounded beam search
   (keep 384 promising partial assignments; fallback 1024), not all mappings.
   Required logical edges must exist physically; no routing SWAPs are added.
   ALAP means delaying preparation until needed to reduce exposed idle time;
   DD means echo pulses during idles to refocus slow phase errors. Both were
   explored upstream. This module preserves saved moments and adds neither
   an ALAP pass nor new DD pulses; its runtime noise model has no idle decay.
4. Rank: fast custom cost = gate-count-weighted error + readout survival loss
   + a penalty for unequal readout asymmetry across outputs. Placement uses
   p + 0.25*sigma (clipped), not 0.8*p_CZ or 0.67*p_1q. For N<=8, refine up
   to 12 choices with density-matrix estimates of 10,000-shot notebook loss,
   comparing every X-correlator cutoff by worst relative loss. This uses the
   nominal p: independent depolarizing faults, equal p/3 for X,Y,Z or p/15
   for the 15 nonidentity two-qubit Paulis, plus asymmetric readout flips.
   This is an assumed error channel, not a learned prior over all noise types.
   No T1, Tphi, coherent phases, or correlations are inferred from the inputs.
   Expected notebook loss is a score denominator, not state infidelity; larger
   N uses only the fast cost. Neither estimate guarantees a hardware score.
5. Finalize: optionally apply an offline model-tested Pauli/native-phase recipe
   only when both the circuit fingerprint and physical placement match. Frames
   change intermediate gates while closing to the same ideal unitary (up to
   global phase). Recheck native gates/connectivity and the full complex Dicke
   amplitudes; fall back to the unmodified choice if the rewrite fails.

Special cases: main API supports 1<=N<=20, 0<=k<=N. k=0/N needs one product
layer, placed by readout error; k=1/N-1 is W/anti-W; k=2/N-2 benefits from
special unary blocks. Other nontrivial states use the same selection flow.
The separate adaptive API simply decodes one saved depth winner for each of
56 cases (2<=N<=15, 1<=k<=N//2); some use ancillas, measurements and feedforward.
It takes no error maps and does not run the five-step calibrated selector.

Our roles: humans set the research direction, studied papers, ran experiments,
and supplied mathematical insights and validation criteria. LLM agents did
most implementation and intensive optimization/search, driving the largest
improvements. PROMPTS.md records the collaboration; no LLM runs in this API.

Method lineage (adaptations, not verbatim implementations):
- Bärtschi & Eidenbenz (2019), https://arxiv.org/abs/1904.07358:
  deterministic unary/SCS recursion; adapted into a sliding nearest-neighbor sweep.
- Mukherjee et al. (2020), https://arxiv.org/abs/2007.01681:
  partially specified unitaries; simplify blocks on reachable inputs only.
- Aktar et al. (2022), https://arxiv.org/abs/2112.12435:
  divide excitation counts between blocks, then decode; offline candidate lineage.
- Buhrman et al. (2024), https://arxiv.org/abs/2307.14840:
  shallow circuits with measurement/feedforward; background for adaptive variants.
- Hahn (1950), https://doi.org/10.1103/PhysRev.80.580:
  spin-echo principle behind upstream DD experiments, not a runtime pass here.

Keep willow_dicke_data.py beside this module; it holds the lossless packed tables.
"""


import base64
import functools
import itertools
import json
import math
import operator
import time
import zlib
from collections import Counter

import cirq
import cirq_google
import numpy as np

if __package__:
    from .willow_dicke_data import (
        _ADAPTIVE_CIRCUITS_PACKED,
        _FROZEN_FRAME_RECIPES,
        _FROZEN_NATIVE_REWRITES,
        _MAIN_TEMPLATES,
    )
else:
    from willow_dicke_data import (
        _ADAPTIVE_CIRCUITS_PACKED,
        _FROZEN_FRAME_RECIPES,
        _FROZEN_NATIVE_REWRITES,
        _MAIN_TEMPLATES,
    )


__all__ = ['create_dicke_state', 'create_dicke_state_adaptive', 'create_dicke_states_adaptive']


# Emit the five-CNOT reflect block on the reachable three-wire subspace.
def _reflect3(a, b, c, n, ell):
    """Purpose: Emit the five-CNOT reflect block on the reachable three-wire subspace.

    Args:
        a, b, c: Ordered wires; n: remaining size; ell: excitation index.
    Returns:
        An iterator of CNOT and Ry operations.

    Reachable action: 000->000; 100->sqrt(ell/n)100+sqrt(1-ell/n)010;
    101->011; 110->110; 111->111.
    """
    alpha = 0.5 * math.asin(math.sqrt(ell / n))
    yield cirq.CNOT(b, a)
    yield cirq.ry(alpha)(b)
    yield cirq.CNOT(c, b)
    yield cirq.ry(alpha)(b)
    yield cirq.CNOT(a, b)
    yield cirq.ry(-alpha)(b)
    yield cirq.CNOT(c, b)
    yield cirq.ry(-alpha)(b)
    yield cirq.CNOT(b, a)


# Build a Dicke circuit with a sliding unary register and reduced local blocks.
def raw_circuit(n: int, k: int, *, use_width2=True, use_top_row=True) -> cirq.Circuit:
    """Purpose: Build a Dicke circuit with a sliding unary register and reduced local blocks.

    Args:
        n, k: Size and weight; use_width2, use_top_row: enable block shortcuts.
    Returns:
        A logical Cirq circuit on n LineQubits.
    """
    if not isinstance(n, int) or not isinstance(k, int) or n < 1 or (not 0 <= k <= n):
        raise ValueError('Require integer n >= 1 and 0 <= k <= n')
    q = cirq.LineQubit.range(n)
    complement = k > n // 2
    weight = n - k if complement else k
    ops = list(cirq.X.on_each(q[:max(0, weight - 1)]))
    for start in range(n - 1):
        remaining = n - start
        if use_width2 and weight == 2 and (start > 0):
            if remaining == 3:
                ops.extend(_unary_three_weight_two(q[start:start + 3]))
                break
            if remaining >= 4:
                ops.extend(_width_two_row(q[start:start + 3], remaining))
                continue
        width = min(weight, remaining - 1)
        if not width:
            break
        co = math.sqrt(width / remaining)
        si = math.sqrt((remaining - width) / remaining)
        givens = np.array([[1, 0, 0, 0], [0, co, si, 0], [0, -si, co, 0], [0, 0, 0, 1]])
        top_ell = width - 1
        if use_top_row and weight >= 3 and (start > 0) and (remaining > weight):
            ops.extend(_combined_top_row(q[start + width - 2:start + width + 1], remaining, width))
            top_ell = width - 2
        elif start == 0:
            a, b = q[width - 1:width + 1]
            ops.extend([cirq.ry(2 * math.acos(co))(a), cirq.CNOT(a, b), cirq.X(a)])
        else:
            ops.append(cirq.MatrixGate(givens)(q[start + width - 1], q[start + width]))
        for ell in range(top_ell, 0, -1):
            a, b, c = q[start + ell - 1:start + ell + 2]
            if ell < weight - start:
                ops.extend([cirq.CNOT(b, a), cirq.CNOT(a, b), cirq.X(a)])
            else:
                ops.extend(_reflect3(a, b, c, remaining, ell))
    if complement:
        ops.extend(cirq.X.on_each(q))
    if not ops:
        ops = list(cirq.I.on_each(q))
    return cirq.Circuit(ops)


# Decode three unary wires using a six-CZ block for weights zero through two.
def _unary_three_weight_two(q):
    """Purpose: Decode three unary wires using a six-CZ block for weights zero through two.

    Args:
        q: Three ordered wires; input 111 is outside the supported subspace.
    Returns:
        A list of exact CZ and Ry operations.

    The six-CZ identity supports unary weights 0,1,2 only; never input 111.
    """
    if len(q) != 3:
        raise ValueError('Expected exactly three line qubits')
    a = math.asin(1 / math.sqrt(3))
    angles = [-math.pi / 2, math.pi, a - math.pi, -math.pi / 2, a - math.pi, math.pi / 2 + a, a, -math.pi / 2, -math.pi / 2, -math.pi / 2, 0, -math.pi / 4, math.pi / 4, math.pi / 2, math.pi / 2]
    edges = [(1, 2), (0, 1), (1, 2), (0, 1), (1, 2), (1, 2)]
    ops = [cirq.ry(angles[i])(q[i]) for i in range(3)]
    for j, (u, v) in enumerate(edges):
        ops.append(cirq.CZ(q[u], q[v]))
        if angles[3 + 2 * j]:
            ops.append(cirq.ry(angles[3 + 2 * j])(q[u]))
        if angles[4 + 2 * j]:
            ops.append(cirq.ry(angles[4 + 2 * j])(q[v]))
    return ops


# Fuse the top Givens rotation and reflect block into four CZ gates.
def _combined_top_row(q, n, width):
    """Purpose: Fuse the top Givens rotation and reflect block into four CZ gates.

    Args:
        q: Three ordered wires; n, width: remaining size and active unary width.
    Returns:
        Operations correct on columns 000, 100 and 110, with a common phase.

    Requires a trailing zero in the active unary register (n>width); input 111
    is unsupported. Promised columns share the same positive phase.
    """
    if len(q) != 3 or not 2 <= width < n:
        raise ValueError('Require three ordered qubits and 2<=width<n')
    a = math.acos(math.sqrt(width / n))
    b = math.asin(math.sqrt((width - 1) / n))
    p = math.pi
    return [cirq.ry(-3 * p / 2)(q[0]), cirq.ry(a - p)(q[2]), cirq.CZ(q[1], q[2]), cirq.ry(p / 2)(q[1]), cirq.ry(p - a)(q[2]), cirq.CZ(q[0], q[1]), cirq.ry(b + p / 2)(q[0]), cirq.ry(p - b)(q[1]), cirq.CZ(q[1], q[2]), cirq.ry(-p / 2)(q[1]), cirq.CZ(q[0], q[1]), cirq.ry(p / 2)(q[0]), cirq.ry(p / 2)(q[1])]


# Specialize the fused top-row block to an active unary width of two.
def _width_two_row(q, n):
    """Purpose: Specialize the fused top-row block to an active unary width of two.

    Args:
        q: Three ordered wires; n: remaining register size.
    Returns:
        The four-CZ row as a list of operations.
    """
    return _combined_top_row(q, n, 2)


# Decompose local blocks into full CZs, merge 1q gates and pack parallel layers.
def native_compile(raw: cirq.AbstractCircuit) -> cirq.Circuit:
    """Purpose: Decompose local blocks into full CZs, merge 1q gates and pack parallel layers.

    Args:
        raw: Logical circuit containing one- and two-qubit gates.
    Returns:
        A native CZ/PhasedXZ circuit packed with EARLIEST insertion.
    """
    ops = []
    for op in raw.all_operations():
        if op.gate == cirq.CNOT:
            a, b = op.qubits
            ops.extend([cirq.H(b), cirq.CZ(a, b), cirq.H(b)])
        elif len(op.qubits) == 1 or op.gate == cirq.CZ:
            ops.append(op)
        else:
            ops.extend(cirq.two_qubit_matrix_to_cz_operations(*op.qubits, cirq.unitary(op), allow_partial_czs=False))
    result = cirq.Circuit(ops)
    result = cirq.merge_single_qubit_gates_to_phxz(result, atol=1e-14)
    result = cirq.drop_negligible_operations(result, atol=1e-14)
    return cirq.Circuit(result.all_operations())


# Cache the compiled analytic circuit for one size and excitation count.
@functools.lru_cache(maxsize=256)
def _logical_frozen(n: int, k: int) -> cirq.FrozenCircuit:
    """Purpose: Cache the compiled analytic circuit for one size and excitation count.

    Args:
        n, k: Dicke-state size and excitation count.
    Returns:
        An immutable native circuit on logical LineQubits.
    """
    raw = raw_circuit(n, k)
    native = native_compile(raw)
    return cirq.FrozenCircuit(native)


# Return a mutable copy of the cached analytic Dicke circuit.
def logical_circuit(n: int, k: int) -> cirq.Circuit:
    """Purpose: Return a mutable copy of the cached analytic Dicke circuit.

    Args:
        n, k: Dicke-state size and excitation count.
    Returns:
        A fresh Cirq circuit on LineQubit.range(n).
    """
    return cirq.Circuit(_logical_frozen(n, k))


# Add a small calibration-uncertainty penalty and clamp the ranking rate.
def rate(value, uncertainty_weight=0.25):
    """Purpose: Add a small calibration-uncertainty penalty and clamp the ranking rate.

    Args:
        value: p or (p, sigma); uncertainty_weight: multiplier of sigma, default 0.25.
    Returns:
        The adjusted rate clipped to [0, 0.49].
    """
    if isinstance(value, (tuple, list)):
        return max(0.0, min(0.49, float(value[0]) + uncertainty_weight * float(value[1])))
    try:
        return max(0.0, min(0.49, float(value[0]) + uncertainty_weight * float(value[1])))
    except (TypeError, IndexError):
        return max(0.0, min(0.49, float(value)))


# Build an undirected connectivity graph weighted by calibrated CZ error.
def calibration_graph(cz_rates, sq_rates):
    """Purpose: Build an undirected connectivity graph weighted by calibrated CZ error.

    Args:
        cz_rates, sq_rates: Coupler and qubit calibration maps.
    Returns:
        A qubit-to-neighbor dictionary; duplicate edges use the worse rate.
    """
    nodes = sorted(sq_rates)
    graph = {q: {} for q in nodes}
    for (a, b), value in cz_rates.items():
        if a == b or a not in graph or b not in graph:
            continue
        p = rate(value)
        p = max(p, graph[a].get(b, 0.0))
        graph[a][b] = graph[b][a] = p
    return graph


# Propagate a density matrix through native gates and independent Pauli noise.
def native_density(circuit, qubits, cz_rates, sq_rates):
    """Purpose: Propagate a density matrix through native gates and independent Pauli noise.

    Args:
        circuit, qubits: Native circuit and wire order; cz_rates, sq_rates: (p, sigma) maps.
    Returns:
        The final complex128 density matrix, using nominal p and no idle noise.
    """
    n = len(qubits)
    positions = {q: i for i, q in enumerate(qubits)}
    rates = {}
    for pair, value in cz_rates.items():
        key = frozenset(pair)
        rates[key] = max(rates.get(key, 0.0), float(value[0]))
    rho = np.zeros((2,) * (2 * n), dtype=np.complex128)
    rho[(0,) * (2 * n)] = 1.0
    layouts = {}
    for op in circuit.all_operations():
        targets = tuple(sorted((positions[q] for q in op.qubits)))
        if len(targets) == 1 and isinstance(op.gate, cirq.PhasedXZGate):
            q = targets[0]
            unitary = cirq.unitary(op.gate)
            rho = _density_single(rho, unitary, q)
            rho = _density_single(rho, unitary.conj(), n + q)
            p = float(sq_rates[op.qubits[0]][0])
        elif len(targets) == 2 and op.gate == cirq.CZ:
            shape = [1] * (2 * n)
            for q in targets:
                shape[q] = 2
            diagonal = np.array([[1.0, 1.0], [1.0, -1.0]])
            rho *= diagonal.reshape(shape)
            shape = [1] * (2 * n)
            for q in targets:
                shape[n + q] = 2
            rho *= diagonal.reshape(shape)
            p = rates[frozenset(op.qubits)]
        else:
            raise ValueError('Proxy accepts native PhasedXZ and full CZ gates only')
        p = min(1.0, max(0.0, p))
        if p == 0:
            continue
        if targets not in layouts:
            rest = tuple((i for i in range(n) if i not in targets))
            perm = targets + rest + tuple((n + i for i in targets)) + tuple((n + i for i in rest))
            inverse = tuple(np.argsort(perm))
            layouts[targets] = (perm, inverse, 1 << len(targets), 1 << len(rest))
        perm, inverse, d, remainder = layouts[targets]
        block = rho.transpose(perm).reshape(d, remainder, d, remainder)
        partial = np.einsum('iaib->ab', block)
        block = (1 - d * d * p / (d * d - 1)) * block
        for i in range(d):
            block[i, :, i, :] += d * p / (d * d - 1) * partial
        rho = block.reshape((2,) * (2 * n)).transpose(inverse)
    return rho.reshape(1 << n, 1 << n)


# Apply asymmetric readout flips, optionally including noisy X-basis rotations.
def _readout(probabilities, qs, zero, one, basis_noise=None):
    """Purpose: Apply asymmetric readout flips, optionally including noisy X-basis rotations.

    Args:
        probabilities, qs: Ideal probabilities and order; zero, one: flip maps; basis_noise: optional 1q map.
    Returns:
        A flat vector of observed bit-string probabilities.
    """
    p = probabilities.reshape((2,) * len(qs))
    for i, q in enumerate(qs):
        e0, e1 = (float(zero.get(q, (0.01, 0.0))[0]), float(one.get(q, (0.02, 0.0))[0]))
        if basis_noise is not None:
            extra = 2 * float(basis_noise[q][0]) / 3 * (1 - e0 - e1)
            e0, e1 = (e0 + extra, e1 + extra)
        m = np.array([[1 - e0, e1], [e0, 1 - e1]])
        p = np.moveaxis(np.tensordot(m, p, axes=(1, i)), 0, i)
    return p.reshape(-1)


# Use a bounded beam search to place logical edges on calibrated couplers.
def rank_graph_embeddings(circuit, outputs, k, cz, sq, zero, one, width=1024, count=1, time_budget=3.0):
    """Purpose: Use a bounded beam search to place logical edges on calibrated couplers.

    Args:
        circuit, outputs, k: Candidate and weight; cz, sq, zero, one: error maps.
        width, count, time_budget: Beam size, returned placements and seconds allowed.
    Returns:
        Ranked (cost, mapped circuit, output set) tuples, or [] on failure/timeout.
    """
    started = time.monotonic()
    logical = sorted(outputs)
    n = len(logical)
    index = {q: i for i, q in enumerate(logical)}
    singles, pairs = (Counter(), Counter())
    neighbors = {i: set() for i in range(n)}
    for op in circuit.all_operations():
        ids = [index[q] for q in op.qubits]
        if len(ids) == 1:
            singles[ids[0]] += 1
        elif len(ids) == 2:
            a, b = sorted(ids)
            pairs[a, b] += 1
            neighbors[a].add(b)
            neighbors[b].add(a)
        else:
            raise ValueError('Embedding expects one- and two-qubit gates')
    graph = calibration_graph(cz, sq)
    if n > len(graph):
        return []
    weighted_degree = {i: sum((pairs[tuple(sorted((i, j)))] for j in neighbors[i])) for i in range(n)}
    order = []
    remaining = set(range(n))
    while remaining:
        chosen = max(remaining, key=lambda i: (len(neighbors[i].intersection(order)), len(neighbors[i]), weighted_degree[i], singles[i], -i))
        order.append(chosen)
        remaining.remove(chosen)
    position = {i: p for p, i in enumerate(order)}
    earlier = {i: sorted((j for j in neighbors[i] if position[j] < position[i])) for i in range(n)}
    readout, asymmetry, sq_cost = ({}, {}, {})
    for q in graph:
        e0 = rate(zero.get(q, (0.01, 0.0)))
        e1 = rate(one.get(q, (0.02, 0.0)))
        readout[q] = -(1 - k / n) * math.log1p(-e0) - k / n * math.log1p(-e1)
        asymmetry[q] = math.log1p(-e1) - math.log1p(-e0)
        sq_cost[q] = rate(sq[q])
    scale = 10000 / math.comb(n, k) * k * (n - k) / (n * max(1, n - 1))
    states = [(0.0, (), 0.0, 0.0, 0.0)]
    cap = max(8, width // max(1, len(graph)) * 3)
    for p, i in enumerate(order):
        expanded = []
        for _, assigned, additive, ds, d2 in states:
            prior = earlier[i]
            candidates = set(graph) if not prior else set(graph[assigned[position[prior[0]]]])
            for j in prior[1:]:
                candidates.intersection_update(graph[assigned[position[j]]])
            candidates.difference_update(assigned)
            for q in sorted(candidates):
                if len(graph[q]) < len(neighbors[i]):
                    continue
                cost = additive + readout[q] + singles[i] * sq_cost[q]
                cost += sum((pairs[tuple(sorted((i, j)))] * graph[q][assigned[position[j]]] for j in prior))
                new_ds = ds + asymmetry[q]
                new_d2 = d2 + asymmetry[q] ** 2
                score = cost + scale * max(0.0, new_d2 - new_ds ** 2 / (p + 1))
                expanded.append((score, assigned + (q,), cost, new_ds, new_d2))
        if not expanded:
            return []
        expanded.sort(key=lambda item: (item[0], item[1]))
        states, per_last = ([], Counter())
        for state in expanded:
            last = state[1][-1]
            if per_last[last] < cap:
                states.append(state)
                per_last[last] += 1
                if len(states) >= width:
                    break
        if time.monotonic() - started > time_budget and p + 1 < n:
            return []
    result = []
    for score, assigned, _, _, _ in states[:count]:
        mapping = {logical[i]: assigned[position[i]] for i in range(n)}
        result.append((score, circuit.transform_qubits(mapping), set(assigned)))
    return result


# Score a placement using gate error, readout survival and readout imbalance.
def circuit_calibration_cost(circuit, outputs, k, cz, sq, zero, one):
    """Purpose: Score a placement using gate error, readout survival and readout imbalance.

    Args:
        circuit, outputs, k: Placed candidate and weight; cz, sq, zero, one: error maps.
    Returns:
        A scalar ranking cost; smaller is better, not a measured infidelity.
    """
    n = len(outputs)
    pairs = {}
    for pair, v in cz.items():
        key = frozenset(pair)
        pairs[key] = max(pairs.get(key, 0.0), rate(v))
    result = 0.0
    for op in circuit.all_operations():
        result += rate(sq[op.qubits[0]]) if len(op.qubits) == 1 else pairs[frozenset(op.qubits)]
    ds = []
    for q in outputs:
        e0, e1 = (rate(zero.get(q, (0.01, 0.0))), rate(one.get(q, (0.02, 0.0))))
        result -= (1 - k / n) * math.log1p(-e0) + k / n * math.log1p(-e1)
        ds.append(math.log1p(-e1) - math.log1p(-e0))
    scale = 10000 / math.comb(n, k) * k * (n - k) / (n * max(1, n - 1))
    mean = sum(ds) / n
    return result + scale * sum(((d - mean) ** 2 for d in ds))


# Try legal grid translations, reflections and rotations of a saved layout.
def rank_template(c, qs, k, cz, sq, zero, one, count=4):
    """Purpose: Try legal grid translations, reflections and rotations of a saved layout.

    Args:
        c, qs, k: Grid circuit, outputs and weight; cz, sq, zero, one: maps; count: limit.
    Returns:
        The best (cost, mapped circuit, output set) tuples.
    """
    qs = sorted(qs)
    n = len(qs)
    graph = calibration_graph(cz, sq)
    index = {q: i for i, q in enumerate(qs)}
    singles = Counter()
    edges = Counter()
    for op in c.all_operations():
        if len(op.qubits) == 1:
            singles[index[op.qubits[0]]] += 1
        else:
            edges[tuple((index[q] for q in op.qubits))] += 1
    candidates = []
    seen = set()
    for flip in (False, True):
        for sr, sc in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            coords = [(sc * q.col, sr * q.row) if flip else (sr * q.row, sc * q.col) for q in qs]
            anchor = coords[0]
            for physical in graph:
                mapped = tuple((cirq.GridQubit(physical.row + r - anchor[0], physical.col + col - anchor[1]) for r, col in coords))
                if mapped in seen or not all((q in graph for q in mapped)):
                    continue
                seen.add(mapped)
                if not all((mapped[b] in graph[mapped[a]] for a, b in edges)):
                    continue
                cost = sum((w * rate(sq[mapped[i]]) for i, w in singles.items()))
                cost += sum((w * graph[mapped[a]][mapped[b]] for (a, b), w in edges.items()))
                ds = []
                for q in mapped:
                    e0, e1 = (rate(zero.get(q, (0.01, 0.0))), rate(one.get(q, (0.02, 0.0))))
                    cost -= (1 - k / n) * math.log1p(-e0) + k / n * math.log1p(-e1)
                    ds.append(math.log1p(-e1) - math.log1p(-e0))
                cost += 10000 / math.comb(n, k) * k * (n - k) / (n * max(1, n - 1)) * sum(((d - sum(ds) / n) ** 2 for d in ds))
                candidates.append((cost, mapped))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [(cost, c.transform_qubits(dict(zip(qs, mapped))), set(mapped)) for cost, mapped in candidates[:count]]


_LAST_SELECTION = {}


_RESIDUAL_LIMIT = 1e-10


# Apply a two-by-two matrix along one axis of a density tensor.
def _density_single(rho, unitary, axis):
    """Purpose: Apply a two-by-two matrix along one axis of a density tensor.

    Args:
        rho: Tensor; unitary: Local matrix; axis: Ket or bra index to transform.
    Returns:
        The transformed tensor.
    """
    a, b = (np.take(rho, 0, axis=axis), np.take(rho, 1, axis=axis))
    return np.stack((unitary[0, 0] * a + unitary[0, 1] * b, unitary[1, 0] * a + unitary[1, 1] * b), axis=axis)


# Normalize integer inputs and reject unsupported Dicke dimensions.
def _arguments(n, k, maximum=20):
    """Purpose: Normalize integer inputs and reject unsupported Dicke dimensions.

    Args:
        n, k: Requested size and weight; maximum: Largest supported n.
    Returns:
        (n, k) as Python integers; raises NotImplementedError for invalid inputs.
    """
    try:
        if isinstance(n, (bool, np.bool_)) or isinstance(k, (bool, np.bool_)):
            raise TypeError
        n, k = (operator.index(n), operator.index(k))
    except TypeError:
        raise NotImplementedError('Integer N and excitation count are required') from None
    if not 1 <= n <= maximum or not 0 <= k <= n:
        raise NotImplementedError('Unsupported Dicke-state dimensions')
    return (n, k)


# Reconstruct and cache a main template without changing its stored moments.
@functools.lru_cache(maxsize=256)
def _decode_saved(key, index):
    """Purpose: Reconstruct and cache a main template without changing its stored moments.

    Args:
        key: "N,k" table key; index: Candidate index within that case.
    Returns:
        (FrozenCircuit, ordered GridQubit tuple).
    """
    data = _MAIN_TEMPLATES[key][index]
    qubits = tuple((cirq.GridQubit(*q) for q in data['qubits']))
    moments = []
    for row in data['moments']:
        operations = []
        for gate in row:
            if gate[0] == 1:
                operations.append(cirq.CZ(qubits[gate[1]], qubits[gate[2]]))
            else:
                operations.append(cirq.PhasedXZGate(x_exponent=gate[2], z_exponent=gate[3], axis_phase_exponent=gate[4])(qubits[gate[1]]))
        moments.append(cirq.Moment(operations))
    return (cirq.FrozenCircuit(moments), qubits)


# Validate Willow connectivity and native gates, then canonicalize their representation.
def _native_contract(circuit, outputs, n, cz, sq):
    """Purpose: Validate Willow connectivity and native gates, then canonicalize their representation.

    Args:
        circuit, outputs, n: Candidate and required size; cz, sq: Valid calibration maps.
    Returns:
        (Canonical native circuit, output set), or raises NotImplementedError.
    """
    outputs = set(outputs)
    device = cirq_google.Willow105.metadata
    if len(outputs) != n or not outputs <= set(sq) or (not outputs <= device.qubit_set):
        raise NotImplementedError('Invalid or uncalibrated output qubits')
    if set(circuit.all_qubits()) - outputs or not 0 < len(circuit) <= 1750:
        raise NotImplementedError('Ancillary qubits or unsupported circuit depth')
    allowed = {frozenset(edge): tuple(edge) for edge in cz}
    moments = []
    for moment in circuit:
        operations = []
        for op in moment:
            if op.classical_controls or any((q not in sq for q in op.qubits)):
                raise NotImplementedError('Classical controls or uncalibrated gate qubits')
            gate = op.gate
            if len(op.qubits) == 1 and isinstance(gate, cirq.PhasedXZGate):
                values = (gate.x_exponent, gate.z_exponent, gate.axis_phase_exponent)
                if not all((math.isfinite(float(x)) for x in values)):
                    raise NotImplementedError('Non-finite native rotation')
                x, z, a = (math.remainder(float(x), 2.0) for x in values)
                op = cirq.PhasedXZGate(x_exponent=x, z_exponent=z, axis_phase_exponent=a)(op.qubits[0])
            elif len(op.qubits) == 2 and gate == cirq.CZ and (frozenset(op.qubits) in allowed):
                op = cirq.CZ(*allowed[frozenset(op.qubits)])
            else:
                raise NotImplementedError('Only calibrated full CZ and PhasedXZ gates are returned')
            operations.append(op)
        moments.append(cirq.Moment(operations))
    result = cirq.Circuit(moments)
    try:
        cirq_google.Willow105.validate_circuit(result)
    except (ValueError, TypeError) as error:
        raise NotImplementedError('Circuit is not valid on Willow105') from error
    return (result, outputs)


# Check every complex amplitude against the target Dicke state up to global phase.
def _state_residual(circuit, outputs, n, k):
    """Purpose: Check every complex amplitude against the target Dicke state up to global phase.

    Args:
        circuit, outputs, n, k: Candidate, output wires and target dimensions.
    Returns:
        The residual norm; raises NotImplementedError if it exceeds 1e-10.
    """
    state = cirq.Simulator(dtype=np.complex128).simulate(circuit, qubit_order=sorted(outputs)).final_state_vector
    indices = np.fromiter((sum((1 << j for j in bits)) for bits in itertools.combinations(range(n), k)), dtype=np.int64)
    normalization = math.sqrt(math.comb(n, k))
    overlap = state[indices].sum() / normalization
    state[indices] -= overlap / normalization
    residual = float(np.linalg.norm(state))
    if not math.isfinite(residual) or residual > _RESIDUAL_LIMIT or abs(abs(overlap) ** 2 - 1) > 1e-10:
        raise NotImplementedError('Noiseless Dicke amplitude check failed')
    return residual


# Estimate finite-shot notebook score denominators for every X cutoff.
def _expected_notebook_losses(circuit, outputs, k, cz, sq, zero, one):
    """Purpose: Estimate finite-shot notebook score denominators for every X cutoff.

    Args:
        circuit, outputs, k: Candidate and weight; cz, sq, zero, one: Error maps.
    Returns:
        N expected losses for cutoffs 1..N under the Pauli/readout model.
        Uses 10,000 shots per basis and an asymptotic Z inverse-occupancy expansion;
        the harmonic score targets 1/mean(denominator), not state fidelity.
    """
    order = sorted(outputs)
    n = len(order)
    rho = native_density(circuit, order, cz, sq)
    z = _readout(np.real(np.diag(rho)), order, zero, one)
    xrho = rho.reshape((2,) * (2 * n))
    for axis in range(2 * n):
        a, b = (np.take(xrho, 0, axis=axis), np.take(xrho, 1, axis=axis))
        xrho = np.stack(((a + b) / math.sqrt(2), (a - b) / math.sqrt(2)), axis=axis)
    x = _readout(np.real(np.diag(xrho.reshape(1 << n, 1 << n))), order, zero, one, basis_noise=sq)
    weights = np.fromiter((i.bit_count() for i in range(1 << n)), dtype=np.int64)
    shell = z[weights == k]
    f = float(shell.sum())
    if f <= 0:
        return np.full(n, np.inf)
    m = math.comb(n, k)
    delta = max(0.0, float(m * np.sum((shell / f) ** 2) - 1))
    sf = 10000 * f
    inv = 1 / sf + (1 - f) / sf ** 2 + (1 - f) * (2 - f) / sf ** 3
    zloss = 10000 / m * (delta + (m - 1 - delta) * inv)

    # Return a binomial coefficient, treating out-of-range choices as zero.
    def comb(a, b):
        """Purpose: Return a binomial coefficient, treating out-of-range choices as zero.

        Args:
            a, b: Population size and selected count.
        Returns:
            math.comb(a, b), or zero outside 0<=b<=a.
        """
        return math.comb(a, b) if 0 <= b <= a else 0
    losses, xloss = ([], 0.0)
    for ell in range(1, n + 1):
        expected = comb(ell, ell // 2) * comb(n - ell, k - ell // 2) / m if ell % 2 == 0 else 0.0
        by_weight = np.array([sum(((-1) ** j * comb(w, j) * comb(n - w, ell - j) for j in range(ell + 1))) / comb(n, ell) for w in range(n + 1)])
        values = by_weight[weights]
        mean = float(x @ values)
        variance = max(0.0, float(x @ (values * values)) - mean * mean)
        xloss += ((expected - mean) ** 2 + variance / 10000) / (expected ** 2 if expected else 1)
        losses.append(zloss + xloss)
    return np.array(losses)


# Keep finite calibration entries on Willow105 qubits and physical couplers.
def _clean_calibration(cz, sq, zero, one):
    """Purpose: Keep finite calibration entries on Willow105 qubits and physical couplers.

    Args:
        cz, sq, zero, one: Four (probability, uncertainty) dictionaries.
    Returns:
        Filtered maps requiring both readout rates for each retained qubit.
    """
    device = cirq_google.Willow105.metadata
    physical_pairs = {frozenset(p) for p in device.qubit_pairs}

    # Recognize a finite (probability, nonnegative uncertainty) pair.
    def finite_rate(value):
        """Purpose: Recognize a finite (probability, nonnegative uncertainty) pair.

        Args:
            value: Candidate calibration entry.
        Returns:
            True for a finite pair with 0<=p<=1 and sigma>=0.
        """
        try:
            return len(value) == 2 and all((math.isfinite(float(x)) for x in value)) and (0 <= float(value[0]) <= 1) and (float(value[1]) >= 0)
        except (TypeError, ValueError):
            return False
    sq = {q: v for q, v in sq.items() if q in device.qubit_set and finite_rate(v)}
    cz = {p: v for p, v in cz.items() if len(p) == 2 and frozenset(p) in physical_pairs and all((q in sq for q in p)) and finite_rate(v)}
    sq = {q: v for q, v in sq.items() if q in zero and q in one and finite_rate(zero[q]) and finite_rate(one[q])}
    cz = {p: v for p, v in cz.items() if all((q in sq for q in p))}
    return (cz, sq, {q: zero[q] for q in sq}, {q: one[q] for q in sq})


# Record physical wires in the order used by the frozen frame recipes.
def _frame_placement(circuit):
    """Purpose: Record physical wires in the order used by the frozen frame recipes.

    Args:
        circuit: Candidate native circuit.
    Returns:
        Ordered (row, col) coordinates, or None for non-grid wires.
    """
    wires = {}
    for op in circuit.all_operations():
        if len(op.qubits) == 1:
            q = op.qubits[0]
            if not isinstance(q, cirq.GridQubit):
                return None
            wires.setdefault(q, (q.row, q.col))
    return tuple(wires.values())


# Fingerprint moments and gates independently of absolute grid coordinates.
def _frame_structure(circuit):
    """Purpose: Fingerprint moments and gates independently of absolute grid coordinates.

    Args:
        circuit: Native candidate; first 1q use determines logical wire order.
    Returns:
        A SHA-256 digest with angles rounded to 12 digits, or None if unsupported.
    """
    import hashlib
    wires = {}
    for op in circuit.all_operations():
        if len(op.qubits) == 1 and op.qubits[0] not in wires:
            wires[op.qubits[0]] = len(wires)
    if set(wires) != set(circuit.all_qubits()):
        return None
    rows = []
    for moment in circuit:
        row = []
        for op in moment:
            if len(op.qubits) == 1 and isinstance(op.gate, cirq.PhasedXZGate):
                g = op.gate
                values = tuple((round(math.remainder(float(v), 2.0), 12) for v in (g.x_exponent, g.z_exponent, g.axis_phase_exponent)))
                values = tuple((0.0 if v == 0 else v for v in values))
                row.append((0, wires[op.qubits[0]], *values))
            elif len(op.qubits) == 2 and op.gate == cirq.CZ:
                row.append((1, *sorted((wires[q] for q in op.qubits))))
            else:
                return None
        rows.append(tuple(row))
    return hashlib.sha256(repr(tuple(rows)).encode('ascii')).hexdigest()


# Absorb a closed Pauli frame into existing gates while preserving the ideal unitary.
def _rewrite_frozen_frame(circuit, bits):
    """Purpose: Absorb a closed Pauli frame into existing gates while preserving the ideal unitary.

    Args:
        circuit: Native candidate; bits: One Pauli choice (0..3) per 1q gate.
    Returns:
        A circuit with identical wires, CZs and moments; rejects nonidentity boundaries.

    At a 1q gate U, use P_out U P_in^dagger. Across CZ, propagate
    (xa,za; xb,zb) -> (xa,za xor xb; xb,zb xor xa).
    """
    frames = {q: [0, 0] for q in circuit.all_qubits()}
    paulis = (np.eye(2), cirq.unitary(cirq.X), cirq.unitary(cirq.Z), cirq.unitary(cirq.Z) @ cirq.unitary(cirq.X))
    cursor = 0
    moments = []
    for moment in circuit:
        operations = []
        for op in moment:
            if len(op.qubits) == 1 and isinstance(op.gate, cirq.PhasedXZGate):
                if cursor >= len(bits) or bits[cursor] not in (0, 1, 2, 3):
                    raise ValueError('Invalid frozen Pauli-frame recipe')
                q = op.qubits[0]
                x, z = frames[q]
                chosen = bits[cursor]
                matrix = paulis[chosen] @ cirq.unitary(op) @ paulis[x + 2 * z].conj().T
                g = cirq.PhasedXZGate.from_matrix(matrix)
                g = cirq.PhasedXZGate(x_exponent=math.remainder(float(g.x_exponent), 2.0), z_exponent=math.remainder(float(g.z_exponent), 2.0), axis_phase_exponent=math.remainder(float(g.axis_phase_exponent), 2.0))
                operations.append(g(q))
                frames[q] = [chosen & 1, chosen >> 1]
                cursor += 1
            elif len(op.qubits) == 2 and op.gate == cirq.CZ:
                a, b = op.qubits
                frames[a][1] ^= frames[b][0]
                frames[b][1] ^= frames[a][0]
                operations.append(op)
            else:
                raise ValueError('Frozen frames require native unitary gates')
        moments.append(cirq.Moment(operations))
    if cursor != len(bits) or any((value != [0, 0] for value in frames.values())):
        raise ValueError('Frozen frame recipe has a nonidentity boundary')
    return cirq.Circuit(moments)


# Apply a saved Pauli frame only to its matching structure and physical placement.
def _apply_previous_frame(circuit, n, k, source):
    """Purpose: Apply a saved Pauli frame only to its matching structure and physical placement.

    Args:
        circuit: Candidate; n, k, source: Recipe lookup key.
    Returns:
        (Rewritten circuit, label), or (original circuit, None) when inapplicable.
    """
    recipe = _FROZEN_FRAME_RECIPES.get((n, k, source))
    if not isinstance(recipe, dict) or _frame_structure(circuit) != recipe.get('structure') or _frame_placement(circuit) != recipe.get('placement'):
        return (circuit, None)
    try:
        return (_rewrite_frozen_frame(circuit, recipe['bits']), recipe['label'])
    except (ValueError, TypeError, IndexError, KeyError):
        return (circuit, None)


# Replace only 1q parameters with a saved, previously evaluated phase rewrite.
def _rewrite_native_phases(circuit, parameters):
    """Purpose: Replace only 1q parameters with a saved, previously evaluated phase rewrite.

    Args:
        circuit: Native candidate; parameters: One (x, z, axis) triple per 1q gate.
    Returns:
        A circuit with the same CZs and moments; the caller checks exactness.
    """
    cursor = 0
    moments = []
    for moment in circuit:
        operations = []
        for op in moment:
            if len(op.qubits) == 1 and isinstance(op.gate, cirq.PhasedXZGate):
                if cursor >= len(parameters):
                    raise ValueError('Incomplete native phase recipe')
                values = parameters[cursor]
                if len(values) != 3 or not all((math.isfinite(float(v)) for v in values)):
                    raise ValueError('Invalid native phase parameters')
                x, z, axis = values
                operations.append(cirq.PhasedXZGate(x_exponent=x, z_exponent=z, axis_phase_exponent=axis)(*op.qubits))
                cursor += 1
            elif len(op.qubits) == 2 and op.gate == cirq.CZ:
                operations.append(op)
            else:
                raise ValueError('Native phase recipe requires CZ and PhasedXZ')
        moments.append(cirq.Moment(operations))
    if cursor != len(parameters):
        raise ValueError('Excess native phase parameters')
    return cirq.Circuit(moments)


# Prefer a matching native-phase recipe, falling back to the saved Pauli recipe.
def _apply_selected_frame(circuit, n, k, source):
    """Purpose: Prefer a matching native-phase recipe, falling back to the saved Pauli recipe.

    Args:
        circuit: Candidate; n, k, source: Recipe lookup key.
    Returns:
        (Rewritten circuit, label), or (original circuit, None).
    """
    recipe = _FROZEN_NATIVE_REWRITES.get((n, k, source))
    if isinstance(recipe, dict) and _frame_structure(circuit) == recipe.get('structure') and (_frame_placement(circuit) == recipe.get('placement')):
        try:
            return (_rewrite_native_phases(circuit, recipe['parameters']), recipe['label'])
        except (ValueError, TypeError, IndexError, KeyError):
            pass
    return _apply_previous_frame(circuit, n, k, source)


# Select, place and verify an exact native Dicke circuit using Willow calibration.
def create_dicke_state(num_qubits, num_excitations, cz_pauli_error_rate, sq_pauli_error_rate, zero_errors, one_errors):
    """Purpose: Select, place and verify an exact native Dicke circuit using Willow calibration.

    Args:
        num_qubits: N, from 1 through 20.
        num_excitations: k, from 0 through N.
        cz_pauli_error_rate: {(qubit, qubit): (Pauli probability, uncertainty)}.
        sq_pauli_error_rate: {qubit: (1q Pauli probability, uncertainty)}.
        zero_errors: {qubit: (P(read 1 | prepared 0), uncertainty)}.
        one_errors: {qubit: (P(read 0 | prepared 1), uncertainty)}.
    Returns:
        (cirq.Circuit, set[cirq.GridQubit]) preparing |D(N,k)> from all zeros.
        Raises NotImplementedError for unsupported inputs, absent legal candidates,
        failed exactness checks or exhausted construction budgets.
    """
    started = time.monotonic()
    n, k = _arguments(num_qubits, num_excitations)
    args = _clean_calibration(cz_pauli_error_rate, sq_pauli_error_rate, zero_errors, one_errors)
    cz, sq, zero, one = args
    _LAST_SELECTION.clear()
    if len(sq) < n:
        raise NotImplementedError('Insufficient calibrated Willow qubits')
    if k in (0, n):
        errors = zero if k == 0 else one
        outputs = set(sorted(sq, key=lambda q: (rate(errors[q]), q))[:n])
        gate = cirq.PhasedXZGate(x_exponent=int(k == n), z_exponent=0, axis_phase_exponent=0)
        circuit = cirq.Circuit(cirq.Moment((gate(q) for q in sorted(outputs))))
        circuit, outputs = _native_contract(circuit, outputs, n, cz, sq)
        _state_residual(circuit, outputs, n, k)
        return (circuit, outputs)
    kk = min(k, n - k)
    key = f'{n},{kk}'
    templates = []
    for index, data in enumerate(_MAIN_TEMPLATES.get(key, [])):
        frozen, qubits = _decode_saved(key, index)
        templates.append((cirq.Circuit(frozen), set(qubits), data['source']))
    if not templates:
        circuit = logical_circuit(n, kk)
        circuit = circuit.transform_qubits({q: cirq.GridQubit(0, q.x) for q in circuit.all_qubits()})
        templates.append((circuit, set(circuit.all_qubits()), 'v3_analytic_fallback'))
    choices = []
    fingerprints = set()
    for circuit, outputs, source in templates:
        if time.monotonic() - started > 22:
            break
        if k != kk:
            circuit = circuit + cirq.Circuit(cirq.Moment((cirq.PhasedXZGate(x_exponent=1, z_exponent=0, axis_phase_exponent=0)(q) for q in sorted(outputs))))
        placements = rank_template(circuit, outputs, k, *args, count=2)
        try:
            original, original_outputs = _native_contract(circuit, outputs, n, cz, sq)
            placements.append((circuit_calibration_cost(original, original_outputs, k, *args), original, original_outputs))
        except NotImplementedError:
            pass
        placements.extend(rank_graph_embeddings(circuit, outputs, k, *args, width=384, count=2, time_budget=min(1.0, max(0.1, 22 - (time.monotonic() - started)))))
        for proxy, candidate, placed in placements:
            fingerprint = cirq.FrozenCircuit(candidate)
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            try:
                candidate, placed = _native_contract(candidate, placed, n, cz, sq)
            except NotImplementedError:
                continue
            choices.append((proxy, candidate, placed, source))
    if not choices and time.monotonic() - started < 30:
        fallback = logical_circuit(n, kk)
        fallback = fallback.transform_qubits({q: cirq.GridQubit(0, q.x) for q in fallback.all_qubits()})
        fallback_outputs = set(fallback.all_qubits())
        if k != kk:
            fallback += cirq.Circuit(cirq.Moment((cirq.PhasedXZGate(x_exponent=1, z_exponent=0, axis_phase_exponent=0)(q) for q in sorted(fallback_outputs))))
        for proxy, candidate, placed in rank_graph_embeddings(fallback, fallback_outputs, k, *args, width=1024, count=2, time_budget=4.0):
            try:
                candidate, placed = _native_contract(candidate, placed, n, cz, sq)
                choices.append((proxy, candidate, placed, 'v3_analytic_path_fallback'))
            except NotImplementedError:
                continue
    if not choices:
        raise NotImplementedError('No legal placement found on the supplied calibrated graph')
    choices.sort(key=lambda row: (row[0], len(row[1])))
    if n <= 8:
        shortlist, remaining, families = ([], [], set())
        for row in choices:
            if row[3] not in families:
                families.add(row[3])
                shortlist.append(row)
            else:
                remaining.append(row)
        shortlist = (shortlist + remaining)[:12]
        refined = []
        for row in shortlist:
            if refined and time.monotonic() - started > 38:
                break
            losses = _expected_notebook_losses(row[1], row[2], k, *args)
            if np.all(np.isfinite(losses)):
                refined.append((losses, row))
        if refined:
            lower = np.maximum(np.min([row[0] for row in refined], axis=0), 1e-15)
            refined.sort(key=lambda row: (float(np.max(row[0] / lower)), float(np.mean(row[0] / lower)), row[1][0]))
            choices = [row[1] for row in refined]
            _LAST_SELECTION['estimated_denominators_all_X_cutoffs'] = refined[0][0].tolist()
    for proxy, circuit, outputs, source in choices:
        if time.monotonic() - started > 48:
            raise NotImplementedError('Construction budget exhausted')
        original_circuit, original_outputs = (circuit, outputs)
        recipe_label = None
        try:
            framed, recipe_label = _apply_selected_frame(circuit, n, k, source)
            if recipe_label is not None:
                circuit, outputs = _native_contract(framed, outputs, n, cz, sq)
            residual = _state_residual(circuit, outputs, n, k)
        except (NotImplementedError, ValueError):
            if recipe_label is None:
                continue
            circuit, outputs, recipe_label = (original_circuit, original_outputs, None)
            try:
                residual = _state_residual(circuit, outputs, n, k)
            except NotImplementedError:
                continue
        if time.monotonic() - started >= 58:
            raise NotImplementedError('Construction budget exhausted')
        _LAST_SELECTION.update(source=source, residual=residual, moments=len(circuit), calibrated_proxy=proxy, seconds=time.monotonic() - started)
        if recipe_label is not None:
            _LAST_SELECTION['frame_recipe'] = recipe_label
        return (circuit, set(outputs))
    raise NotImplementedError('No candidate passed the exact-state check')


# Restore a saved adaptive circuit, including measurement keys and classical controls.
def _decode_bonus_adaptive(data):
    """Purpose: Restore a saved adaptive circuit, including measurement keys and classical controls.

    Args:
        data: Record with qubits, moments and output indices.
    Returns:
        (cirq.Circuit, output-qubit set); ancillas may also occur in the circuit.
    """
    import cirq
    import sympy
    qubits = [cirq.GridQubit(*point) for point in data['qubits']]
    moments = []
    for layer in data['moments']:
        operations = []
        for item in layer:
            kind = item['gate']
            params = item.get('parameters', [])
            if kind == 'p':
                gate = cirq.PhasedXZGate(x_exponent=params[0], z_exponent=params[1], axis_phase_exponent=params[2])
            elif kind == 'cz':
                gate = cirq.CZ
            elif kind == 'x':
                gate = cirq.XPowGate(exponent=params[0], global_shift=params[1])
            elif kind == 'z':
                gate = cirq.ZPowGate(exponent=params[0], global_shift=params[1])
            elif kind == 'm':
                gate = cirq.MeasurementGate(len(item['qubits']), key=item['key'], invert_mask=tuple(item['invert']))
            else:
                raise ValueError('Unsupported stored gate')
            op = gate.on(*[qubits[i] for i in item['qubits']])
            conditions = []
            for condition in item.get('conditions', []):
                if condition[0] == 'key':
                    conditions.append(cirq.KeyCondition(cirq.MeasurementKey(condition[1]), index=condition[2]))
                elif condition[0] == 'zero':
                    conditions.append(cirq.SympyCondition(sympy.Eq(sympy.Symbol(condition[1]), 0)))
                elif condition[0] == 'parity':
                    conditions.append(cirq.SympyCondition(sympy.Eq(sympy.Mod(sum((sympy.Symbol(key) for key in condition[1])), 2), 1)))
                else:
                    raise ValueError('Unsupported stored condition')
            if conditions:
                op = op.with_classical_controls(*conditions)
            operations.append(op)
        moments.append(cirq.Moment(operations))
    return (cirq.Circuit(moments), {qubits[i] for i in data['outputs']})


# Decode and cache the losslessly packed adaptive-circuit table.
@functools.lru_cache(maxsize=1)
def _adaptive_circuit_records():
    """Purpose: Decode and cache the losslessly packed adaptive-circuit table.

    Args:
        None.
    Returns:
        A dictionary from "N,k" to saved circuit records.
    """
    return json.loads(zlib.decompress(base64.b85decode(_ADAPTIVE_CIRCUITS_PACKED)))


# Load the saved depth winner for a supported adaptive Dicke case.
def create_dicke_state_adaptive(num_qubits, num_excitations):
    """Purpose: Load the saved depth winner for a supported adaptive Dicke case.

    Args:
        num_qubits: 2<=N<=15; num_excitations: 1<=k<=N//2.
    Returns:
        (cirq.Circuit, output-qubit set), possibly with measured ancillas.
        Raises NotImplementedError outside the supported domain.
    """
    n, k = _arguments(num_qubits, num_excitations, maximum=15)
    if n < 2 or not 1 <= k <= n // 2:
        raise NotImplementedError('Bonus requires 2<=N<=15 and 1<=k<=N//2')
    record = _adaptive_circuit_records()[f'{n},{k}']
    return _decode_bonus_adaptive(record)


create_dicke_states_adaptive = create_dicke_state_adaptive
