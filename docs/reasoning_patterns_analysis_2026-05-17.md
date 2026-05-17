# Agentic reasoning patterns: project inventory + paper analysis + identified gaps

**Date**: 2026-05-17
**Purpose**: Inventory existing reasoning-pattern benchmarks in apecx-mcp-integration, analyze the 3 papers the user provided, identify which patterns are genuinely **NOT yet covered**, and propose synthesis ideas not in the source papers.

## TL;DR

* The project has **25+ benchmark workflows** covering CoT, decomposition, single-shot self-refine, RAG, ensembles (self-consistency), and ablations of memory/aggregator/web-search.
* The 3 source papers cover: (a) RecursiveMAS — latent-space agent communication (REQUIRES MODEL TRAINING), (b) Agentic Reasoning Survey — catalog of patterns, (c) Haidemariam — philosophical framework for goal-maintained recursive loops.
* **Genuinely novel for this project**: (i) Reflexion-style cross-round failure memory, (ii) test-driven recursive refinement with execution feedback, (iii) goal-maintained recursive loops with explicit revisable goal state, (iv) hierarchical decomposition with recursive subgoal solving, (v) mixture-of-experts with domain-specialized agents.
* **Recommended first implementation**: **Test-Driven Recursive Refinement (TDR)** — a synthesis of TDD + Reflexion + execution-grounded reflection that doesn't appear in any of the 3 source papers as a single named pattern.

## Source paper analysis

### Paper 1: RecursiveMAS (arXiv 2604.25917, Yang et al., 2026-04)

**Title**: Recursive Multi-Agent Systems
**Core contribution**: Agents pass LATENT EMBEDDINGS (last-layer hidden states) through learned linear projection layers instead of TEXT. Maintains gradient flow during training, saves 34-75% tokens, 1.2-2.4× speedup, 8.3% accuracy gain.

**Algorithmic core**:
* **Inner Link** (within one agent): `R_in(h) = h + W_2 · σ(W_1 · h)` (residual MLP recursing last-layer embedding)
* **Outer Link** (between agents with different hidden dims): `R_out(h) = W_3 · h + W_2 · σ(W_1 · h)` (residual + projection)
* **Training**: 2-stage — inner-loop per-agent first, then outer-loop end-to-end

**4 collaboration patterns evaluated**:
1. Sequential (Planner → Critic → Solver)
2. Mixture (Math + Code + Science specialists → Summarizer)
3. Distillation (Expert + Learner pair)
4. Deliberation (multi-round with tools)

**Brutal-truth on implementability**: REQUIRES MODEL FINE-TUNING. Hooking into last-layer hidden states + training projection matrices is a 100+ hour undertaking. Out of scope for this project's "prompt + LLM" pattern category. The COLLABORATION PATTERNS (1-4) are implementable via text-based communication, but those approximations are already present in our existing benchmarks (sequential = `plan_then_code` + `review_revise`; mixture = `structural_consensus`; deliberation = `max_power_websearch`).

### Paper 2: Agentic Reasoning Survey (arXiv 2601.12538, Wei et al., 2026-01)

**Title**: Agentic Reasoning for Large Language Models
**Length**: 135 pages, 28 co-authors. A survey, not a method paper.

**Three-layer taxonomy**:
1. **Foundational** — planning, tool use, search (single-agent capabilities)
2. **Self-evolving** — reflective feedback, memory-driven adaptation
3. **Collective intelligence** — multi-agent coordination

**Two orthogonal axes**:
* In-context orchestration (prompt-time)
* Post-training optimization (model fine-tuning)

**Specific patterns cataloged that are RELEVANT to code-gen**:
* **Chain-of-Thought** (CoT) — sequential decomposition
* **Reflexion** — verbal self-reflections stored as in-context examples for future reasoning
* **Tree-of-Thought** (ToT) — branching exploration with backtracking
* **Hierarchical task decomposition** — recursive subgoal splitting with verification
* **Self-Critique loops** — iterative refinement with quality signals
* **Subgoal verification** — verify intermediate solutions before proceeding

**Brutal-truth**: most patterns the survey catalogs are well-known. The survey's value is the taxonomy + the 135-page reference list, not novel patterns.

### Paper 3: Haidemariam (frai-8-1728738, 2026)

**Title**: From the logic of coordination to goal-directed reasoning: the agentic turn in AI
**Domain**: Philosophy of AI / agent theory

**Core concept**: **Synthetic teleology** — engineered capacity for artificial systems to generate and regulate their own goals through ongoing self-evaluation, formalized as a **recursive goal-maintenance equation**.

**Three required elements** for genuinely purposive systems:
1. **Intrinsic goal dynamics** — goals evolve endogenously, not by external scripts
2. **Own objective** — goal as internally maintained reference variable
3. **Internal evaluation** — system computes discrepancy ∆ between current goal G and perceived situation S

**Operationalizable metrics**:
* GP (goal persistence under perturbation)
* TC (teleological coherence — alignment between goal revisions and evidence)
* RE (reflective efficiency — discrepancy reduction per reflection step)
* AD (adaptivity — time-to-recover after environment shifts)
* NF (normative fidelity)
* IY (innovation yield)

**Brutal-truth**: heavy theory, light on concrete algorithms. The OPERATIONAL takeaway for code-gen: an iteration loop that maintains an explicit GOAL state, evaluates progress against the goal each round, and can REVISE the goal if it's not converging. This is distinct from Reflexion (which keeps notes on FAILURES) — Haidemariam emphasizes maintenance of the GOAL itself.

## Existing project benchmarks (25+ workflows)

Inventory from `src/apecx_integration/composition/workflows/benchmark_*`:

| Benchmark | What it tests |
|---|---|
| `direct_codegen` | Single LLM call (baseline) |
| `direct_with_rules` | + framework-rules condensate |
| `plan_then_code` | Decomposition (plan → code) |
| `plan_then_code_with_rules` | + rules |
| `edge_case_then_code` | TDD-ish: enumerate edge cases first, then code |
| `review_revise` | Single-round self-refine |
| `runtime_gated_review_revise` | + runtime check |
| `ast_gated_review_revise` | + AST validity check |
| `retrieval_grounded` | RAG over component catalog |
| `retrieval_grounded_skeleton` | + skeleton selection |
| `retrieval_grounded_mbpp` | RAG tuned for MBPP |
| `integrated_full` | Combination of components |
| `integrated_similarity` | MemFlow tier-2 |
| `max_power` | Kitchen sink |
| `max_power_websearch` | Kitchen sink + web search |
| `perturbed_consensus` | Self-consistency (prompt-variance fan-out) |
| `structural_consensus` | Self-consistency (structural fan-out) |
| 7× ablation variants | (memreader/memrecorder/aggregator/websearch alone or in pairs) |

**What IS covered**:
* CoT (direct)
* Decomposition (plan_then_code)
* RAG (retrieval_grounded)
* Single-shot self-refine (review_revise)
* Self-consistency / ensembling (consensus variants)
* Memory + retrieval combinations
* Tool use via web search

**What is NOT YET covered**:
1. **Multi-round refinement** with cross-round **failure memory** (Reflexion-style)
2. **Execution-grounded refinement** (run tests, refine based on which tests failed)
3. **Goal-maintained recursive loops** with explicit goal state per round
4. **Hierarchical (recursive) decomposition** — current `plan_then_code` is flat 2-stage
5. **Mixture-of-domain-experts** with specialist agents (not just prompt-variance ensemble)
6. **Tree-of-Thoughts** branching with backtracking
7. **Algorithm-of-Thoughts** (sequential exploration of multiple algorithmic approaches)

## Synthesis: 3 novel patterns not in any source paper as a single named pattern

### A. Test-Driven Recursive Refinement (TDR)

**Synthesis of**: TDD (agile/SE) + Reflexion (failure memory) + execution-grounded reflection
**Why novel**: Most reflection patterns in literature use LLM-judge as the critic. TDR uses TEST EXECUTION (concrete, ground-truth) as the critic + LLM-driven failure analysis.

**Loop**:
```
0. (optional) LLM generates additional edge-case tests
1. LLM writes initial code given (prompt, tests)
2. Sandbox: execute code against tests, capture pass/fail per test
3. If all pass → return code
4. For each failed test: collect (test, actual_output, expected, error_msg)
5. LLM analyzes failures + writes "failure modes" memory
6. LLM writes new code given (prompt, tests, failure_modes_memory)
7. Goto 2; cap at N iterations
```

### B. Goal-Maintained Recursive Refinement (GMR)

**Synthesis of**: Haidemariam's recursive goal-maintenance + classic self-refine
**Why novel**: Existing self-refine patterns keep the GOAL implicit. GMR maintains an explicit goal state that:
* Is evaluated each round (discrepancy ∆)
* Can be REVISED if discrepancy isn't shrinking (RE metric < threshold)
* Persists as a workflow data unit (introspectable from outside)

**Loop**:
```
0. Initial goal G_0 = problem.prompt
1. LLM proposes solution given G_t
2. Internal evaluator computes ∆_t (e.g., LLM-judge score 0-1 against G_t)
3. If ∆_t ≤ ε → return solution
4. If RE_t (= ∆_t - ∆_{t-1}) < min_threshold → revise goal G_t → G_{t+1}
5. Else continue with same G; goto 1
6. Cap at N iterations
```

### C. Hierarchical Decomposition with Recursive Subgoal Solving (HD-RSS)

**Synthesis of**: Hierarchical decomposition (survey) + recursive structure (RecursiveMAS philosophy)
**Why novel**: Existing `plan_then_code` is flat 2-stage. HD-RSS recursively decomposes each subgoal that an LLM marks "too complex" — until atomic — then composes results bottom-up.

**Loop**:
```
solve(problem):
  if LLM thinks problem is atomic:
    return LLM.code(problem)
  else:
    subgoals = LLM.decompose(problem)
    solutions = [solve(sg) for sg in subgoals]  # recurse
    return LLM.compose(solutions)
```

## Recommended first implementation: TDR

* Most concretely benchmarkable (MBPP has test cases natively).
* Pure prompt-level pattern (no model training required).
* Concrete novelty argument vs project's `review_revise` (single-shot, no execution feedback) AND vs Reflexion (which uses LLM critic, not test execution).
* Compatible with the existing benchmark harness via a new codegen factory.

GMR and HD-RSS are documented above but DEFERRED to subsequent commits — TDR alone is a multi-hour scope with implementation + smoke benchmark + analysis.

## Framework-capacity expansion required

TDR is implemented as a Python-driven loop because **nanobrain has no native loop primitive** (gap G18, LoopController, not yet shipped). The per-round logic IS a nanobrain workflow; the loop body itself is Python. Once G18 ships, TDR can become a purely-YAML workflow with a `LoopController` step gating the iteration. This commit documents the proposed G18 interface inline with the TDR implementation.

## Honest caveats

1. **Benchmark cost**: Each TDR run consumes up to N×(2-3) LLM calls per problem vs 1 call for `direct`. Smoke benchmarks will use N=3 + small problem set (5-10 MBPP problems).
2. **Sandbox security**: TDR executes LLM-generated code in the sandbox at every iteration. The existing `tests/benchmarks/sandbox.py` handles this; verify no resource leak across iterations.
3. **Ollama-only verification**: Smoke benchmarks run against the local Ollama (mistral-nemo). For SciCode + nanobrain_native benchmarks, runtime is significant.
4. **The 3 source papers don't all agree** on what "recursive" means — RecursiveMAS treats recursion as architectural (recurring latent embeddings), Reflexion treats it as memory-accumulating loop, Haidemariam treats it as goal-maintenance loop. TDR picks one interpretation (execution-grounded iteration) and is honest about the choice.
