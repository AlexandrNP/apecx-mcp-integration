# Codegen shipping recommendation (post-ablation, 2026-05-13)

**One-page executive summary**. For full data tables see
[`sweep_matrix_2026-05-13.md`](./sweep_matrix_2026-05-13.md). For mechanism
analysis see [`ablation_attribution_memo.md`](./ablation_attribution_memo.md).
For chronological reasoning trail see [`findings_p0_p1_p3.md`](./findings_p0_p1_p3.md).

## Per-benchmark winners on local `mistral-nemo:latest` at T=0

| Benchmark | Ship this | pass@1 | Why |
|---|---|---|---|
| **nanobrain-native n=10** | `nanobrain_integrated_similarity` | **0.90** | +10pp over F17 via closed memory loop; deterministic at N=3 |
| **MBPP n=20** | `nanobrain_plan_then_code` (v2) | **0.78** | F22 historical winner; closed memory loop REGRESSES MBPP to 0.60 |
| **SciCode val n=5** | `direct` | **0.20** | Closed memory loop is order-sensitive at small n; ship the simpler baseline |
| **Open-Rosalind v0 (codegen subset, n=8)** | `direct` or `retrieval_grounded` | **0.50** | "Smart" scaffolds (plan_then_code, integrated_similarity, max_power) COLLAPSE to 0% — see F37-F39 |

**There is no universal codegen winner on local mistral-nemo 12B.** F22's
per-benchmark verdict holds, now with 4 benchmarks of evidence. Every
codegen family has at least one benchmark where it is the WORST option.
The closed-memory-loop scaffolds win nanobrain-native (0.90) and score
ZERO on Open-Rosalind — the loop amplifies errors when the domain's
base accuracy is low (F39).

**Biology benchmarks** (added 2026-05-14): Open-Rosalind RAN (codegen-
adapted `sequence_basic` subset); BixBench / BioML-bench / BioProBench
are CONFIGURED with runs blocked or deferred. See
[`findings_biology_benchmarks.md`](./findings_biology_benchmarks.md) and
[`biology_benchmark_extension_plan.md`](./biology_benchmark_extension_plan.md).

## What ships in the catalog

All components and workflows are framework-native, tested, and ready to load.

### Components (Python classes, all `from_config`-only)

| Class | LOC | Status | Purpose |
|---|---|---|---|
| `BenchmarkDrafterStep` | 483 | shipped (pre-F17) | Single-shot LLM drafter |
| `BenchmarkPlannerStep` | 320 | shipped (pre-F17) | Plan-then-code planner |
| `BenchmarkReviewerStep` | 305 | shipped (pre-F17) | Review-revise reviewer |
| `BenchmarkEdgeCaseStep` | 267 | shipped (pre-F17) | Pre-drafter edge-case enumerator |
| `CodeStructureValidatorStep` | 348 | shipped (pre-F17) | Static AST validator |
| `FrameworkComplianceRunnerStep` | 433 | shipped (pre-F17) | Runtime validator (subprocess) |
| `TaskCategoryRouterStep` | 319 | shipped (F17) | Per-category worked-example enrichment |
| `MultiSampleDrafterStep` | 356 | shipped (post-F17) | Temperature-variance fan-out |
| `ConsensusAggregatorStep` | 352 | shipped (post-F17) | Deterministic AST voter |
| `SolutionMemoryStep` | ~430 | shipped + extended | tier-1 read / tier-2 similarity_read / record |
| `PromptPerturbingDrafterStep` | 355 | shipped (Item 2) | Prompt-variance fan-out (catalog-only; F24 null) |

### Workflows (YAML, all `config_version: 2`)

| Workflow | Topology | pass@1 (nb / mbpp / scicode) | Recommendation |
|---|---|---|---|
| `benchmark_direct_codegen` | router-free, drafter only | 0.10 / 0.64 / 0.20 | SciCode default |
| `benchmark_plan_then_code` | planner → drafter | 0.10 / **0.78** / — | **MBPP default** |
| `benchmark_retrieval_grounded` (F17) | router → drafter | 0.80 / 0.65 / 0.00 | superseded for nanobrain-native |
| `benchmark_perturbed_consensus` (Item 2) | router → perturbing × N → aggregator | 0.80 / 0.65 / 0.00 | catalog-only (F24 null) |
| `benchmark_integrated_similarity` (Item 3) | router → memreader → drafter → aggregator → memrecorder | **0.90** / 0.60 / 0-0.20 | **nanobrain-native default** |
| `benchmark_max_power` | router → memreader → perturbing × N → aggregator → memrecorder | 0.90 / 0.65 / 0.20 | superseded by integrated_similarity (Item 2 adds nothing) |
| `benchmark_ablation_*` (5 workflows) | partial component subsets | 0.80 / 0.65 / — | diagnostic; do not ship |

### Three legitimate workflow-creation paths (all parity-pinned)

1. **YAML** (`benchmark_*/workflow.yml` + `steps/*.yml`): canonical, diffable, version-controlled.
2. **Lightweight `WorkflowBuilder`** (`benchmark_structural_consensus_lightweight_builder.py`): programmatic Python, useful for LLM-composed workflows.
3. **`Workflow.from_skeleton`** (`benchmark_retrieval_grounded_skeleton/skeleton.yml`): templated topology with typed `{{name: type}}` holes.

## When to enable the closed memory loop

The +10pp on nanobrain-native is **caused by within-sweep cross-problem memory cross-pollination via the closed memreader↔memrecorder loop**. This mechanism:

- **HELPS** when benchmark problems share structural / API patterns and sweep n ≥ 10.
- **HURTS** when problems are algorithmically diverse (e.g., MBPP variety).
- **NON-DETERMINISTIC** at small n (e.g., SciCode val n=5) due to order sensitivity.

Concrete adoption rule:

```
# Closed memory loop = memory_reader (similarity_read) + ConsensusAggregatorStep + memory_recorder (record_only_if_pass=true).
# Enable ONLY when:
#   - problems within a single sweep share patterns (framework APIs, library calls, etc.)
#   - sweep size n >= 10 problems
#   - benchmark / production traffic admits "what other passes saw" enrichment
# Disable for:
#   - algorithmic diversity (MBPP-style)
#   - small batches (n < 10)
#   - cases where cross-pollination would leak across user contexts (privacy)
```

## What does NOT ship as a default

- **`PromptPerturbingDrafterStep`** (Item 2): null result across all 3 benchmarks at 3× wall-time. Catalog-only for problem domains with multi-modal correct distributions (math, code-search) — UNTESTED there.
- **`benchmark_max_power`** kitchen-sink: matches integrated_similarity on nanobrain-native at 3× wall-time. No benefit; do not ship.
- **`benchmark_perturbed_consensus`**: same shipping caveat as the perturbing drafter.

## What was deferred + why

- **Item 4 (constrained decoding via vLLM/SGLang/llama.cpp)**: see [`item4_serving_stack_decision.md`](./item4_serving_stack_decision.md). Defer indefinitely (option E). Items 2+3 already extracted what they could; the remaining 10% gap on nanobrain-native is semantic API-knowledge, not syntactic — constrained decoding does not fix it.
- **Item 5 (Confucius DX meta-agent)**: deferred per F21. Not a productizable nanobrain step; would converge to manually-tuned scaffolds on local 12B.

## Next-iteration backlog (highest-EV)

1. **Cross-pollination sensitivity study**: vary problem-ordering, similarity-threshold, examples_on_read on nanobrain-native to characterize the lift's stability envelope. ~2h compute.
2. **Wide MBPP sweep (n=50)**: confirm plan_then_code's 78% holds at larger n with N=2 reproducibility. ~2h compute.
3. **Model swap experiment**: `integrated_similarity` with `nemotron-3-nano:30b-a3b-q4` (already pulled) on all 3 benchmarks. Tests F14's model-bound-ceiling hypothesis. ~3h compute.
4. **Production memory hygiene**: when shipping closed-memory-loop scaffolds, document per-user / per-tenant memory scoping to avoid cross-user pollution (currently global to the deployment).
