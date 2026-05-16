# Ablation attribution memo — the +10pp lift mechanism on nanobrain-native

**Date**: 2026-05-13.
**Scope**: items 2 (`PromptPerturbingDrafterStep`) + 3 (`SolutionMemoryStep.similarity_read`).
**Question answered**: WHICH specific component(s) of `benchmark_integrated_similarity` cause the +10pp lift over F17 on nanobrain-native?

## TL;DR

**The +10pp lift is caused by within-sweep cross-problem memory cross-pollination via a closed read↔record loop.** All three components — `memory_reader` (similarity_read mode), `ConsensusAggregatorStep` (single-candidate AST voter), and `memory_recorder` (record_only_if_pass=true) — must be co-present in the same workflow. ANY partial subset (5 ablations tested) reproduces F17's baseline exactly.

The same closed-loop mechanism REGRESSES MBPP by -5pp and is non-deterministic on SciCode val n=5. The mechanism is **dataset-conditional**, not a universal win.

## Ablation matrix

Each ablation differs from F17 (router + drafter) by adding 1 or 2 of the 3 candidate components.

### nanobrain-native n=10

| Ablation | Components added | pass@1 |
|---|---|---|
| F17 (baseline) | — | 0.80 |
| A | memreader only | 0.80 (same fails) |
| B | aggregator only | 0.80 (same fails) |
| C | memrecorder only | 0.80 (same fails) |
| D | memreader + aggregator | 0.80 (same fails) |
| E | aggregator + memrecorder | 0.80 (same fails) |
| **integrated_similarity** | **all three (closed loop)** | **0.90** |

### MBPP n=20

| Ablation | Components added | pass@1 |
|---|---|---|
| F17 (baseline) | — | 0.65 |
| A | memreader only | 0.65 (identical fail set) |
| B | aggregator only | 0.65 (identical fail set) |
| C | memrecorder only | 0.65 (identical fail set) |
| D | memreader + aggregator | 0.65 (identical fail set) |
| E | aggregator + memrecorder | 0.65 (identical fail set) |
| **integrated_similarity** | **all three (closed loop)** | **0.60** |

**ALL partial configurations reproduce F17 baseline.** Only the 3-way co-presence shifts pass@1.

## Mechanism walk-through

Consider an n=10 sweep through `benchmark_integrated_similarity` with empty initial memory store.

**Problem 1**:
1. `task_router_similarity` enriches `code_spec` with the router-selected worked example (same as F17).
2. `memory_reader` (similarity_read mode): the store is empty. Cosine similarity returns no hits. Falls back to tier-1 exact-category read; also empty. Emits `memory_hit=False`, `memory_mode=exact_fallback`. `code_spec` passes through unchanged.
3. `drafter` (BenchmarkDrafterStep, T=0): receives **byte-identical** `code_spec` as F17 would. Emits code.
4. `aggregator` (ConsensusAggregatorStep, single-candidate AST voter): wraps the drafter's `code_source` as a 1-element candidate list; AST-validates; emits `voted_passes=1` (assuming AST-valid). Output identical to drafter's output plus the `voted_passes` field.
5. `memory_recorder` (record_only_if_pass=true): gate opens because `voted_passes ≥ 1`. Writes problem 1's solution to the store under category=`step` (or whichever the router assigned).
6. Workflow_output produced.

**Problem 2**:
1. Router enriches code_spec.
2. `memory_reader`: the store now contains problem 1's solution. similarity_read encodes the current `code_spec` + the cached solution; computes cosine similarity. If above threshold (default 0.3), emits an enriched `code_spec` containing problem 1's solution as a worked example.
3. `drafter`: receives a **different** code_spec from F17's drafter. Specifically, the prompt now contains a "Previously-passing solution similar to this problem:" block with problem 1's correctly-structured code.
4. The drafter, biased by the retrieved example, writes code closer to the retrieved structure.
5. `aggregator` votes. `memory_recorder` writes problem 2's solution.

**Problem 3..10**: same pattern with progressively more memory.

By problem 10, the memory contains 5 entries (FIFO bound `max_per_category=5`, the most recent 5). The drafter's prompt is consistently enriched with class-structured examples on this benchmark.

**The +10pp on nanobrain-native specifically**:

The single recovery is `tool_calculator`. Under F17 the model writes:

```python
result = eval(compile(tree), {}, {})
```

This is buggy — `compile` requires `(source, filename, mode)`, not just `(source,)`. Test suite fails.

Under integrated_similarity, by the time tool_calculator is solved, the memory contains correctly-structured `step_*` solutions. The retrieved example shows `eval(...)` patterns that work directly. The drafter writes:

```python
result = eval(expression, {}, {})
```

Test suite passes. The recovery is fully attributable to the in-sweep retrieval.

## Why each partial subset fails to reproduce the lift

- **Ablation A (memreader only)**: no recorder → store stays empty for the entire sweep → memreader always misses → no enrichment → F17 baseline.
- **Ablation B (aggregator only)**: no memory at all → the only added effect is the AST-voting wrap, which passes through unchanged for syntactically-valid code → F17 baseline.
- **Ablation C (memrecorder only)**: writes accumulate but no memreader to surface them in-sweep → F17 baseline. (Future runs of THIS workflow with `mode: read` upstream would benefit, but the workflow doesn't have that reader.)
- **Ablation D (memreader + aggregator, NO recorder)**: store stays empty (no writer) → memreader misses → F17 baseline.
- **Ablation E (aggregator + memrecorder, NO memreader)**: writes happen but no reader → no in-sweep enrichment → F17 baseline.

The interaction is **non-additive**. Each component's marginal contribution is zero in isolation or in pairs. The combinatorial 3-way co-presence opens the closed loop, which is non-zero on benchmarks with structurally-related problems.

## Why MBPP regresses

Same mechanism, harmful direction. By the time problem N is being solved on MBPP, the memory contains solutions to N-1 prior MBPP problems. The similarity retrieval finds the "most-similar" prior — but MBPP problems are algorithmically diverse (string manipulation, list operations, integer arithmetic). Cross-pollination introduces wrong-pattern bias.

Concrete fail-set shift on MBPP (single integrated_similarity pass vs F17):
- **Recovered**: `mbpp/61` (1 problem).
- **Newly failing**: `mbpp/14`, `mbpp/65` (2 problems).
- **Net**: -1 problem = -5pp.

The drafter, given a misleading prior, blends two unrelated approaches and fails.

## Why SciCode val is non-deterministic

At n=5 the memory loop's order-sensitivity dominates. The first pass (empty memory at start) and the second pass (memory pre-populated from pass 1, then re-written FIFO during pass 2) produce different memory states by the time problem 5 is reached. A single problem flip = 20pp swing.

`integrated_similarity` on SciCode val: pass 1 = 0.20, pass 2 = 0.00. Same workflow, same dataset, different memory state across passes.

## Determinism re-framing

An earlier draft of this memo (and F31 in the chronological findings doc) framed the +10pp as "cascade non-determinism." Following the user's correction, that framing is wrong:

- The framework's contract is **deterministic primitives are deterministic**.
- Agentic (LLM-bearing) steps are **not deterministic by design** — they respond to their input distribution.
- The within-sweep memory loop **changes the agentic step's input distribution** (problem N+1's code_spec now contains problem N's solution). The LLM's response to the changed input is deterministic at T=0.

So the lift is fully explained by deterministic mechanics:
- Same workflow YAML + same dataset + same problem order → same memory state at problem N → same drafter input at problem N → same LLM output at problem N.

There is no contract violation. The "determinism" claim survives intact; what shifted is the agentic step's input, not the model's behavior given an input.

## Adoption guidance

**Enable the closed memory loop when**:
- Benchmark or production traffic has problems sharing structural / API patterns.
- Sweep size (or batch size) n ≥ 10 to amortize the cross-pollination.
- Cross-user pollution is acceptable (or memory is scoped per-user).

**Do NOT enable it when**:
- Algorithmic diversity dominates (MBPP-style problem variety).
- Sweep is small (n < 10) — order sensitivity dominates.
- Per-user privacy isolation is required and memory cannot be scoped.

**For the workflow author**: the rule for closed-loop memory is "all three components co-present, or none." A workflow with `memory_reader` but no `memory_recorder` (or vice versa) has the same pass@1 as F17 — there is no benefit to half-shipping the loop.

## What I did NOT test in this iteration

- Memory store across sweep runs (only across problems within a sweep). The recorder writes to a persistent store; subsequent sweeps could leverage that. F25 originally measured this and found it did not contribute additional lift on nanobrain-native; the in-sweep mechanism dominates.
- Different `similarity_threshold` values. Default 0.3 was used everywhere.
- Different `examples_on_read` values. Default 1 was used.
- Different `max_per_category` FIFO bounds. Default 5 was used.
- Problem ordering effects. Sweeps ran in dataset's natural order.
- Cross-benchmark memory (e.g., warm the store with MBPP solutions, then sweep nanobrain-native).
- Closed-loop memory with the perturbing drafter (max_power). The data shows max_power matches integrated_similarity on nanobrain-native; the perturbing drafter adds nothing. Whether it adds anything in CONJUNCTION with memory has effectively been tested (no it doesn't) but not separately ablated.

These are concrete follow-up experiments for the next iteration if mechanism understanding needs more refinement.
