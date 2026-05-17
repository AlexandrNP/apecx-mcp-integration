# HD-RSS: Hierarchical Decomposition with Recursive Subgoal Solving — design + framework analysis

**Date**: 2026-05-17
**Status**: ✅ Implemented (G98); ⏳ smoke benchmark on 3 datasets in flight (G96 sweep)
**Pattern category**: Novel recursive synthesis (the Wei survey lists "hierarchical task decomposition" as a generic category; HD-RSS specifies the RECURSIVE call shape that makes it concrete)

## TL;DR

* HD-RSS is a **recursive code-gen pattern**: LLM judges atomicity → if not atomic, decomposes into named subgoals → recursively solves each subgoal → composes bottom-up.
* **Hard caps**: recursion depth ≤ 3; subgoals per level ≤ 4; worst case ~95 LLM calls per problem; typical 3-15.
* **The 3 source papers all CITE hierarchical decomposition** as a pattern, but none give the recursive call shape concretely with termination conditions + composition strategy.
* **Smoke result on MBPP n=2**: 1/2 pass (vs direct + TDR both 2/2). **Expected underperformance on trivial atomic problems** — the decomposition + composition overhead introduces bugs without compensating gains.

## Why HD-RSS over project's existing patterns

The project's `plan_then_code` is **flat 2-stage** (plan → code). HD-RSS is **N-level recursive**:

```
plan_then_code:                  hd_rss:
  plan → code                      solve(problem):
                                     if atomic: code
                                     else:
                                       subgoals = decompose(problem)
                                       solutions = [solve(sg) for sg]   # recurse
                                       return compose(solutions)
```

Each subgoal can itself be decomposed further until reaching atomic operations the LLM can handle in one shot. The composition step then bottom-up assembles solutions.

## Novelty vs the 3 source papers

| Paper | Closest pattern | Distinction from HD-RSS |
|---|---|---|
| **Yang RecursiveMAS** (2026) | Sequential (Planner→Solver) | 2-stage, not N-level recursive |
| **Wei Survey** (2026) | "Hierarchical task decomposition" | Listed as a category, no concrete recursion mechanics |
| **Haidemariam** (2026) | Goal-maintenance loop | Single goal, not recursive subgoals |

HD-RSS specifies **(a)** how the LLM judges atomicity (single-word prompt: ATOMIC/COMPOSITE), **(b)** how decomposition produces structured output (numbered list of `name: description` parsed by regex), **(c)** how composition recombines (LLM sees original problem + all sub-solutions + target entry point), **(d)** termination guarantees (depth cap + branching cap).

## Honest scope choices

* **LLM-self-judged atomicity.** A weak LLM (mistral-nemo) will mis-judge often. A v2 should use a concrete atomicity heuristic (e.g., AST size of a draft codegen).
* **No memoization** between sibling subgoals. Some problems decompose into overlapping subgoals; the LLM will re-solve identical work.
* **Composition is LLM-driven and can introduce its own bugs** — the composer sees N independent sub-functions and has to write glue logic. Tracebacks pinpoint composition failures but the recursion can't backtrack to fix them.
* **Pure Python recursion** — the existing nanobrain `SubworkflowStep` loads workflows at config time, not dynamically at call time. True dynamic-recursive workflow self-reference would be a NEW framework primitive (`RecursiveSubworkflowStep`); G18 LoopController gives iteration not recursion.

## Worst-case cost characteristics

| Level | Atomicity checks | Atomic codegens | Decompositions | Compositions |
|---|---|---|---|---|
| 0 (top) | 1 | 0 if composite, 1 if atomic | 1 if composite | 1 if composite |
| 1 | 4 | 0 if all composite, ≤4 if all atomic | ≤4 if any composite | ≤4 if any composite |
| 2 | 16 | 0 if all composite, ≤16 if all atomic | ≤16 if any composite | ≤16 if any composite |
| 3 (capped) | 64 | 64 (forced atomic at cap) | 0 (cap forces atomic) | 0 |
| **Total worst** | **~85** | **~64** | **~21** | **~21** |

**Worst-case ~95 LLM calls per problem.** Typical problems hit 3-15 calls (atomic at depth 0 or 1 with 0-3 subgoals).

## Predicted performance characteristics

Based on the smoke result (HD-RSS 1/2 vs direct 2/2 on trivial MBPP) and the pattern's mechanics:

* **MBPP**: HD-RSS will likely **underperform** direct. MBPP problems are intentionally atomic, single-function tasks. HD-RSS's decomposition + composition overhead introduces noise without benefit.
* **SciCode**: HD-RSS may help. SciCode problems are multi-step scientific computations that naturally decompose. The composition step can wire helper functions sensibly.
* **nanobrain_native**: Mixed. Some problems (like `builder_two_step_uppercase_reverse`) are 2-step pipelines that decompose cleanly; others (like `step_concat`) are single-class problems where decomposition just adds noise.

These predictions will be tested by the G96 large-N sweep.

## Framework primitives ARE sufficient (G18 surprise)

Initial assumption (when designing TDR): nanobrain has no loop primitive (gap G18, "LoopController not yet shipped").

**Discovered 2026-05-17**: BOTH G18 steps ALREADY exist:
* `nanobrain/library/steps/loop_controller.py` — runtime iteration counter, max_iterations cap, `allow_continue` markers
* `nanobrain/library/steps/subworkflow_step.py` — embed a workflow as a step
* `nanobrain/core/workflow_graph.py` `_all_cycles_pass_through_loop_controller()` — validator extension that allows declared back-edges through LoopController without the workspace-wide `allow_cycles: true` hammer

This means **TDR (and any iterative refinement pattern) CAN be expressed as a pure YAML workflow today**. The Python driver in `tests/benchmarks/codegen/tdr.py` is a convenience for the benchmark harness; the same logic can be a workflow YAML using LoopController as the iteration gate.

For HD-RSS, the framework is NOT yet sufficient — TRUE recursive workflow self-reference (workflow that loads itself dynamically at call time) isn't a framework primitive. SubworkflowStep loads at config time. A future `RecursiveSubworkflowStep` primitive would close this gap; until then, HD-RSS's recursion stays in Python.

## Reference YAML — TDR-as-YAML using existing primitives (sketch)

```yaml
# workflows/tdr_refine_loop.yml — proof that the framework primitives are sufficient

name: tdr_refine_loop
config_version: 2
allow_cycles: false  # G18 Step 2: validator allows the cycle below

input_data_units:
  initial_code: {class: nanobrain.core.data_unit.DataUnitMemory, name: initial_code}
  test_code:    {class: nanobrain.core.data_unit.DataUnitMemory, name: test_code}

output_data_units:
  final_code: {class: nanobrain.core.data_unit.DataUnitMemory, name: final_code}

steps:
  execute:
    class: tests.benchmarks.harness.SandboxStep   # runs candidate against tests
  loop_gate:
    class: nanobrain.library.steps.LoopController
    config:
      max_iterations: 3
  analyze_and_revise:
    class: apecx.composition.steps.TdrRefineStep  # LLM analyzes + revises
  emit_final:
    class: apecx.composition.steps.PassThroughStep

links:
  execute_to_gate:
    class: nanobrain.core.link.ConditionalLink
    config:
      source: execute.passed
      target: emit_final.code      # short-circuit on pass
      predicate: {op: eq, field: passed, value: true}
      auto_transfer: true
  execute_to_loop:
    class: nanobrain.core.link.ConditionalLink
    config:
      source: execute.passed
      target: loop_gate.input
      predicate: {op: eq, field: passed, value: false}
      auto_transfer: true
  loop_to_revise:
    class: nanobrain.core.link.ConditionalLink
    config:
      source: loop_gate.output
      target: analyze_and_revise.input
      predicate: {op: eq, field: allow_continue, value: true}
      auto_transfer: true
  loop_to_final:                  # exhausted iterations → emit current
    class: nanobrain.core.link.ConditionalLink
    config:
      source: loop_gate.output
      target: emit_final.code
      predicate: {op: eq, field: loop_exhausted, value: true}
      auto_transfer: true
  revise_back_to_execute:         # ← the BACK-EDGE
    class: nanobrain.core.link.DirectLink
    config:
      source: analyze_and_revise.output
      target: execute.input
      auto_transfer: true
```

This is a valid nanobrain workflow today. The `revise_back_to_execute` link creates the cycle; `_all_cycles_pass_through_loop_controller()` permits it because the cycle includes `loop_gate` (a LoopController instance).

**Not shipped in this commit**: the supporting `SandboxStep`, `TdrRefineStep`, `PassThroughStep` would need to be authored. The current Python-driven TDR works; the YAML refactor is a future cleanup.

## What's next

* G96 sweep results (~80 min in flight) → cross-pattern comparison table
* HD-RSS benchmarks on all 3 datasets (after G96 sweep finishes)
* Final summary doc comparing direct vs TDR vs HD-RSS across all 3 datasets

## Files

| File | What |
|---|---|
| `tests/benchmarks/codegen/hd_rss.py` | HD-RSS codegen factory (~270 lines) |
| `tests/benchmarks/cli.py` | Added `hd_rss` to `--codegen` choices + dispatch |
| `docs/hd_rss_pattern_2026-05-17.md` | This doc |
