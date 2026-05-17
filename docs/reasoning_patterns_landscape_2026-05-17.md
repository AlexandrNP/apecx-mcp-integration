# Reasoning patterns landscape — comprehensive cross-pattern comparison

**Date**: 2026-05-17 (G96 sweep complete; all tables populated)
**Source data**: `tests/benchmarks/results/*.json` (historical) + `/tmp/bench_*.json` (G96 sweep)
**Coverage**: every codegen / workflow-driven reasoning pattern benchmarked against `mistral-nemo:latest` on MBPP, SciCode, and nanobrain_native to date.

## TL;DR

Two categories of pattern share the benchmark harness: **raw codegen functions** (single Python entry point called by `tests/benchmarks/cli.py --codegen <name>`) and **workflow-driven patterns** (composition-layer workflows that hit the LLM via nanobrain steps). Historical methodology is mixed-N (10/20/35/50), so the G96 sweep is the first apples-to-apples ground truth at N=100/full.

**Headline plateau before G96 (mistral-nemo on real datasets)**:

| Dataset | Best pass@1 (pattern, N) | Worst pass@1 |
|---|---|---|
| MBPP | 0.780 (nanobrain_plan_then_code, N=50) | 0.550 (multiple, N=20) |
| nanobrain_native | 0.800 (nanobrain_retrieval_grounded, N=10) | 0.000 (nanobrain_plan_then_code, N=10) |
| SciCode | 0.200 (3-way tie, N=35) | 0.143 (edge_case, review_revise; N=35) |

**SciCode is the bottleneck**: every pattern ever tried tops out at 0.200 pass@1. If TDR's smoke result (+20pp on SciCode n=5) holds at larger N, it breaks a previously-stable ceiling.

## Methodology caveats

* Sample sizes are inconsistent across historical runs (10, 20, 35, 50). Pass@1 differences of ±5pp at N=20 are likely noise; meaningful differences need N≥50.
* All historical patterns use `mistral-nemo:latest` (12B params) as the base LLM. Stronger models would shift the entire landscape; the relative ordering of patterns is the durable signal.
* "Wall time per problem" includes LLM latency, sandbox subprocess overhead, and any retry loops. It is NOT a fair proxy for token cost — multi-round patterns (review_revise, TDR, HD-RSS) burn more tokens than wall-time alone suggests.
* The `nanobrain_*` prefixed patterns run through the composition layer (workflow → steps → LLM); the bare names (`direct`, `tdr`, `hd_rss`) are raw codegen functions called directly. Both produce the same JSON schema; the difference is the surrounding orchestration.

## Pattern catalog

### Raw codegen functions (`--codegen <name>` in `cli.py`)

| Pattern | What it does | Cost char | Source |
|---|---|---|---|
| `direct` | One LLM call → parse → execute | 1× LLM | `tests/benchmarks/codegen/direct.py` |
| `tdr` | TDR refinement loop (≤3 iters, execution feedback as critique) | 1–4× LLM | `tests/benchmarks/codegen/tdr.py` |
| `hd_rss` | Recursive decomposition + composition (depth ≤3, ≤4 subgoals/level) | 3–95× LLM | `tests/benchmarks/codegen/hd_rss.py` |

### Workflow-driven patterns (composition-layer workflows)

| Pattern | What it does | Source workflow |
|---|---|---|
| `nanobrain_direct` | Composer → CodeWriteStep | code_writing |
| `nanobrain_direct_with_rules` | + nanobrain coding rules in prompt | code_writing |
| `nanobrain_plan_then_code` | Planner step → CodeWriteStep | code_writing |
| `nanobrain_plan_then_code_with_rules` | Above + rules | code_writing |
| `nanobrain_review_revise` | Write → review → revise (single pass) | code_writing |
| `nanobrain_ast_gated_review_revise` | + AST size gate to decide whether to revise | code_writing |
| `nanobrain_runtime_gated_review_revise` | + runtime-exec gate (IsolatedPyExecStep) to decide | code_writing |
| `nanobrain_edge_case_then_code` | Edge-case enumeration → CodeWriteStep | code_writing |
| `nanobrain_structural_consensus` | N samples → AST-similarity vote | code_writing |
| `nanobrain_perturbed_consensus` | N samples with prompt perturbation → vote | code_writing |
| `nanobrain_retrieval_grounded` | RAG over similar past problems → CodeWriteStep | retrieval_grounded |

## Comprehensive landscape table

### MBPP

| Pattern | N | Pass | Pass@1 | Wall/problem | Cost class |
|---|---|---|---|---|---|
| nanobrain_plan_then_code | 50 | 39 | 0.780 | 23.2 s | 2-stage |
| nanobrain_direct | 50 | 34 | 0.680 | 8.3 s | 1-stage |
| **direct (G96, NEW)** | **100** | **65** | **0.650** | **3.0 s** | **1-stage** |
| direct | 50 | 32 | 0.640 | 2.8 s | 1-stage |
| nanobrain_structural_consensus | 20 | 13 | 0.650 | 14.6 s | N-sample |
| nanobrain_edge_case_then_code | 20 | 13 | 0.650 | 17.5 s | 2-stage |
| nanobrain_retrieval_grounded | 20 | 13 | 0.650 | 13.7 s | 1-stage + RAG |
| nanobrain_direct_with_rules | 50 | 30 | 0.600 | 4.0 s | 1-stage + rules |
| nanobrain_plan_then_code (v1) | 50 | 30 | 0.600 | 114.4 s | 2-stage (slow v1) |
| nanobrain_review_revise | 20 | 12 | 0.600 | 39.2 s | 2-pass refine |
| nanobrain_direct_with_rules | 20 | 11 | 0.550 | 4.4 s | 1-stage + rules |
| nanobrain_ast_gated_review_revise | 20 | 11 | 0.550 | 3.7 s | conditional refine |
| nanobrain_retrieval_grounded_mbpp | 20 | 11 | 0.550 | 6.9 s | 1-stage + RAG |
| **tdr (G96, ACTUAL)** | **100** | **68** | **0.680** | **9.3 s** | **iterative** |
| **hd_rss (G96, ACTUAL)** | **100** | **50** | **0.500** | **3.3 s** | **recursive** |

G96 result: TDR +3pp lift vs direct (within noise); HD-RSS -15pp regression (decomposition adds noise on atomic problems). See `docs/g96_pattern_comparison_2026-05-17.md`.

Earlier-session smoke (n=15): direct 9/15 (0.60), tdr 12/15 (0.80, +20pp), hd_rss 1/2 (0.50, small sample). Smoke result did NOT survive scaling — typical regression-to-mean signal.

### nanobrain_native

| Pattern | N | Pass | Pass@1 | Wall/problem | Cost class |
|---|---|---|---|---|---|
| nanobrain_retrieval_grounded | 10 | 8 | 0.800 | 18.0 s | 1-stage + RAG |
| nanobrain_plan_then_code_with_rules | 10 | 7 | 0.700 | 42.6 s | 2-stage + rules |
| nanobrain_direct_with_rules (N3 run2) | 10 | 7 | 0.700 | 11.3 s | 1-stage + rules |
| nanobrain_direct_with_rules (N3 run3) | 10 | 7 | 0.700 | 11.4 s | 1-stage + rules |
| nanobrain_ast_gated_review_revise | 10 | 7 | 0.700 | 12.7 s | conditional refine |
| nanobrain_structural_consensus | 10 | 7 | 0.700 | 49.6 s | N-sample |
| nanobrain_direct_with_rules (v2) | 10 | 7 | 0.700 | 12.0 s | 1-stage + rules |
| nanobrain_runtime_gated_review_revise | 10 | 7 | 0.700 | 13.5 s | conditional refine |
| nanobrain_direct | 10 | 4 | 0.400 | 15.7 s | 1-stage |
| nanobrain_review_revise | 10 | 4 | 0.400 | 85.5 s | 2-pass refine |
| nanobrain_plan_then_code | 10 | 3 | 0.300 | 23.0 s | 2-stage |
| nanobrain_plan_then_code (E2) | 10 | 1 | 0.100 | 12.6 s | 2-stage |
| nanobrain_edge_case_then_code | 10 | 1 | 0.100 | 80.8 s | 2-stage |
| direct | 10 | 1 | 0.100 | 8.3 s | 1-stage |
| nanobrain_direct_with_rules (v1) | 10 | 1 | 0.100 | 15.8 s | 1-stage + rules |
| nanobrain_plan_then_code (v1) | 10 | 0 | 0.000 | 37.2 s | 2-stage |
| **direct (G96, ACTUAL)** | **10** | **1** | **0.100** | **6.9 s** | **1-stage** |
| **tdr (G96, ACTUAL)** | **10** | **8** | **0.800** | **18.6 s** | **iterative** |
| **hd_rss (G96, ACTUAL)** | **10** | **2** | **0.200** | **8.7 s** | **recursive** |

G96 result: TDR delivers **+70pp** on nbnative — ties historical SOTA (`nanobrain_retrieval_grounded` at 0.80) without needing a built RAG index. HD-RSS +10pp. Mechanism: nbnative problems are out-of-distribution for mistral-nemo; execution feedback is the missing signal. See `docs/g96_pattern_comparison_2026-05-17.md`.

### SciCode

| Pattern | N | Pass | Pass@1 | Wall/problem | Cost class |
|---|---|---|---|---|---|
| nanobrain_ast_gated_review_revise | 35 | 7 | 0.200 | 23.0 s | conditional refine |
| nanobrain_plan_then_code | 35 | 7 | 0.200 | 40.3 s | 2-stage |
| direct (historical) | 35 | 7 | 0.200 | 24.4 s | 1-stage |
| nanobrain_direct | 35 | 6 | 0.171 | 21.8 s | 1-stage |
| nanobrain_edge_case_then_code | 35 | 5 | 0.143 | 38.6 s | 2-stage |
| nanobrain_review_revise | 35 | 5 | 0.143 | 159.8 s | 2-pass refine |
| **direct (G96, ACTUAL)** | **36** | **7** | **0.194** | **12.2 s** | **1-stage** |
| **tdr (G96, ACTUAL)** | **35** | **9** | **0.257** | **51.1 s** | **iterative** |
| **hd_rss (G96, ACTUAL)** | **36** | **7** | **0.194** | **26.3 s** | **recursive** |

G96 result: **TDR breaks the historical 0.20 SciCode ceiling** — first reasoning pattern to do so against mistral-nemo. +7pp absolute lift over direct at 4.2× cost. HD-RSS matches direct exactly (decomposition doesn't help on SciCode; LLM-driven composition cancels out any gain). See `docs/g96_pattern_comparison_2026-05-17.md`.

## Hypotheses to test with G96 sweep results

1. **TDR will hold the +20pp MBPP lift at N=100.** If pass@1 lands 0.80–0.85, TDR becomes the new MBPP SOTA for raw codegens against this model. If it regresses below 0.70, the n=15 result was noise.
2. **TDR will lift SciCode above the historical 0.200 ceiling.** A pass@1 ≥ 0.250 on SciCode N=40 means execution-grounded refinement reasoning genuinely helps on compositional problems — a known-hard class.
3. **HD-RSS will underperform direct on MBPP** (expected; MBPP problems are atomic, decomposition adds noise) but should match or beat direct on SciCode (multi-step problems naturally decompose). nanobrain_native is mixed.
4. **The Pareto frontier will not have a single winner.** Different patterns will dominate different (pass@1, wall) corners. The right pattern depends on cost budget + dataset characteristics.

## What the comparison will NOT settle

* **Token cost ≠ wall time.** TDR's 1–4× wall is ~1–4× LLM token cost. HD-RSS's recursive structure can spike to 95× tokens worst-case. The benchmark harness records wall, not tokens; a follow-up using the Ollama prompt-eval / response-eval counters could close this.
* **Pattern robustness across models.** Every result is mistral-nemo. A stronger model (e.g., Llama-3-70B) may compress the gaps — small models benefit more from external scaffolding.
* **Failure-mode taxonomy.** `error_class` is recorded per result but the comparison table only counts pass/fail. A follow-up analysis grouping failures by mechanism (AssertionError vs Timeout vs ImportError vs Composition_glue_error) would inform which pattern targets which failure shape.

## Next steps

1. ✅ G96 sweep complete; tables populated above
2. ✅ HD-RSS sweep complete; tables populated above
3. ✅ Per-problem divergence analysis at `/tmp/comparison_{mbpp,scicode,nbnative}.md` and summarized in `docs/g96_pattern_comparison_2026-05-17.md`
4. 📋 Token-cost instrumentation follow-up (G100 candidate — proposed in g96 comparison doc)
5. 📋 TDR-as-YAML migration end-to-end verification (G99 #169 still pending)
6. 📋 Failure-mode taxonomy across the G96 result JSONs (G101 candidate — proposed in g96 comparison doc)
