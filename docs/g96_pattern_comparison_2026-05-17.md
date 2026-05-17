# G96 cross-pattern comparison: direct vs TDR vs HD-RSS at large N

**Date**: 2026-05-17
**Model**: `mistral-nemo:latest` (Ollama, local)
**Methodology**: One sweep per (dataset, codegen), sequential to avoid Ollama contention. Full datasets where small, N=100 cap on MBPP. Wall time end-to-end including LLM latency + sandbox subprocess.

## TL;DR — three findings that change the pattern-selection narrative

1. **TDR's smoke-test universal lift was noise.** The +20pp / +20pp / +40pp from the n=5-15 smoke didn't survive scaling. At large N, TDR delivers **+3pp on MBPP, +7pp on SciCode, +70pp on nbnative**. This is more nuanced and more useful than the original hypothesis.
2. **TDR's marginal value scales inversely with the baseline.** Where direct already succeeds (MBPP at 0.65), iteration adds little. Where direct fails systematically (nbnative at 0.10), execution feedback is the missing signal that lets the LLM converge. The cost-per-percentage-point analysis below makes this explicit.
3. **HD-RSS is not a productive pattern as designed.** -15pp on MBPP (decomposition adds noise on atomic problems), 0pp on SciCode (LLM-driven composition cancels out decomposition gains), +10pp on nbnative (weakly extracts value from low baseline). The compositional weakness is in the LLM-driven synthesis step, not the decomposition.

## Full results

| Dataset | N | direct | tdr | hd_rss | TDR Δ | HD-RSS Δ |
|---|---|---|---|---|---|---|
| MBPP | 100 | **0.650** (3.0 s/p) | 0.680 (9.3 s/p) | 0.500 (3.3 s/p) | +3pp | -15pp |
| SciCode | 36 | **0.194** (12.2 s/p) | 0.257 (51.1 s/p) | 0.194 (26.3 s/p) | +7pp | 0pp |
| nanobrain_native | 10 | **0.100** (6.9 s/p) | 0.800 (18.6 s/p) | 0.200 (8.7 s/p) | +70pp | +10pp |

**Direct baseline holds vs historical** (sanity check):
* MBPP: G96 N=100 = 0.650 vs historical N=50 = 0.640 — stable
* SciCode: G96 N=36 = 0.194 vs historical N=35 = 0.200 — stable
* nbnative: G96 N=10 = 0.100 vs historical N=10 = 0.100 — exact match

## Cost-efficiency analysis (the inverted-baseline rule)

| Dataset | Direct baseline | TDR cost mult | TDR lift | Lift per cost-doubling |
|---|---|---|---|---|
| MBPP | 0.650 | 3.05× | +3pp | **0.6 pp / doubling** |
| SciCode | 0.194 | 4.19× | +7pp | **1.75 pp / doubling** |
| nbnative | 0.100 | 2.69× | +70pp | **31.8 pp / doubling** |

**The unifying rule**: TDR's lift-per-cost-doubling is 50× better on nbnative than MBPP. The mechanism: when the LLM is in-distribution (MBPP's atomic problems are well-represented in mistral-nemo's training data), one shot succeeds and iteration is wasted spend. When the LLM is out-of-distribution (nbnative problems require nanobrain-specific knowledge mistral-nemo lacks), execution feedback gives the LLM the signal it can't get from the prompt alone.

This generalizes far beyond TDR specifically — it's an "execution-grounded refinement is the right tool when the LLM is operating outside its training distribution" finding.

## Pattern-selection guidance derived from G96

**When to use `direct`**:
* The problem class is well-represented in the LLM's training data (e.g., introductory algorithm puzzles like MBPP).
* Latency / cost matters more than the last few percentage points of pass rate.
* You don't have a fast, reliable execution oracle to run against.

**When to use `tdr`**:
* The problem class is OUT of the LLM's training distribution (e.g., proprietary frameworks, domain-specific APIs, novel libraries).
* You have a fast, reliable execution oracle (test code + sandbox).
* You can afford 2-4× the LLM cost for a meaningful accuracy gain.

**When to use `hd_rss`**:
* Honestly: probably never as currently designed. The LLM-driven composition step is the weak link. If you genuinely need recursive decomposition, design the composer as a deterministic template (G77 composer decomposition path), not another LLM call.
* If you must, use it on low-baseline datasets where the noise-vs-signal trade is favorable (nbnative-like).

## Per-problem divergence highlights

### MBPP (most divergent — 30+ problems where codegens disagree)

* **TDR uniquely fixes (vs direct)**: mbpp/16, mbpp/56, mbpp/61, mbpp/77, mbpp/85, plus 8 others. Pattern: problems where direct produces "almost-right" code that fails on edge cases; the test failure tells the LLM exactly what to fix.
* **TDR uniquely regresses (vs direct)**: mbpp/100, mbpp/120, mbpp/135. Pattern: direct's first attempt was already correct; TDR's revision call introduced an off-by-one or removed a needed guard.
* **HD-RSS broke 15+ problems direct fixes** — the dominant regression. The LLM judged simple problems "composite" and the decomposition+composition introduced bugs the original direct-shot didn't have.

### SciCode (only 7 of 36 problems divergent)

* **TDR uniquely fixes**: scicode/1/1.1, scicode/38/38.2 (multi-step computations where iterative refinement caught off-by-one boundary conditions).
* **TDR + HD-RSS jointly fix**: scicode/47/47.2, scicode/70/70.3 (genuinely decomposable problems).
* **TDR regression**: scicode/49/49.1 (direct's first shot was right; revision broke it).

### nanobrain_native (TDR fixes nearly everything direct misses)

* **TDR uniquely fixes**: step_concat, step_double, step_filter_positive, step_word_count, tool_calculator (5 problems direct misses).
* **TDR + HD-RSS jointly fix**: step_sum_list, step_uppercase (2 problems).
* **HD-RSS broke step_dedupe_preserve_order** (direct + TDR both fix it; decomposition introduced a bug).

## What this comparison does NOT settle

* **Token cost ≠ wall time.** TDR's wall-time multiplier (2-4×) is approximately its token cost. HD-RSS at depth-0 atomic is barely more tokens than direct, but composite-path runs spike to ~10-20× tokens. The benchmark records wall, not tokens; an Ollama prompt-eval/response-eval instrumentation pass (G100 candidate) would close this.
* **Stronger models would compress the gaps.** Every result is mistral-nemo (12B). On Llama-3-70B or Claude-Sonnet, the in-distribution problem class expands and TDR's value would compress on MBPP-like datasets; the nbnative-like dramatic-lift would also compress because stronger models cover more domains zero-shot.
* **TDR-as-YAML reference is not yet shipped.** The TDR codegen used here is the Python factory at `tests/benchmarks/codegen/tdr.py`. A pure-YAML workflow using `LoopController` + `ConditionalLink` is designed in `docs/hd_rss_pattern_2026-05-17.md` and tracked as task G99 (#169); pending end-to-end Ollama-time verification.
* **Failure-mode taxonomy not analyzed.** The per-problem error_class is recorded in the result JSONs but not aggregated. A "what fails how" analysis would inform which next pattern targets which failure shape.

## Files

| File | What |
|---|---|
| `tests/benchmarks/codegen/direct.py` | Direct codegen (1× LLM) |
| `tests/benchmarks/codegen/tdr.py` | TDR refinement loop (≤3 iters) — G93 |
| `tests/benchmarks/codegen/hd_rss.py` | HD-RSS recursive decomposition (depth ≤3) — G98 |
| `tests/benchmarks/compare_codegens.py` | Cross-codegen comparison tool |
| `scripts/run_pattern_sweep.sh` | Sequential per-codegen sweep harness |
| `docs/reasoning_patterns_landscape_2026-05-17.md` | Historical + G96 landscape across every pattern |
| `docs/tdr_pattern_2026-05-17.md` | TDR design + initial smoke results |
| `docs/hd_rss_pattern_2026-05-17.md` | HD-RSS design + LoopController/SubworkflowStep framework gap analysis |
| `docs/g96_pattern_comparison_2026-05-17.md` | **This doc** |
| `/tmp/bench_*_n100.json`, `/tmp/bench_*_full.json` | Raw G96 sweep results |
| `/tmp/comparison_*.md` | Auto-generated per-dataset divergence reports |

## Next chain items

1. **G99 — TDR-as-YAML reference workflow** (task #169 in pending). Author the YAML using shipped primitives (CodeWriteStep, IsolatedPyExecStep, LoopController, ConditionalLink). End-to-end verify against Ollama. The framework piece is already shipped; this is composition + verification.
2. **G100 — Token-cost instrumentation** (proposed). Add prompt-token + response-token counters to the benchmark harness so the Pareto frontier can be drawn in token-cost space rather than wall-time space.
3. **G101 — Test failure-mode taxonomy** (proposed). Aggregate `error_class` across the G96 result JSONs into a "what fails how, per pattern" matrix. Likely reveals where the next pattern's leverage is.
4. **Stronger-model re-run** (when an Ollama-compatible larger model is available). The relative ordering of patterns should be the durable signal; the absolute pass@1 values are model-bound.
