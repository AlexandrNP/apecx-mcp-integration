# G101 — per-pattern failure-mode taxonomy from G96 sweep results

**Date**: 2026-05-17
**Source**: `tests/benchmarks/results/g96_bench_*.json` (9 result JSONs)
**Methodology**: Aggregated `error_class` per (dataset, codegen) + per-problem transitions vs direct baseline.

## TL;DR — three actionable findings

1. **TDR's failure signature on nbnative is the cleanest case for "execution feedback matters"**: 6 of 7 problems direct fails with `RuntimeError` (using wrong APIs / nonexistent modules) → TDR fixes ALL 6 + 1 more. The runtime error message tells the LLM exactly which API is wrong; revision corrects it. **0 unique-break regressions** on nbnative.
2. **HD-RSS's distinctive failure mode is `NameError` and `AttributeError`** — symptoms of the LLM-driven composition step calling helper functions that weren't actually defined / accessing attributes that don't exist. MBPP: 17 NameErrors (vs 0-1 for direct/tdr). nbnative: 4 AttributeErrors (vs 0 for others). **This pinpoints the weak link to the composer prompt, not the decomposer.**
3. **TDR's regressions on MBPP/SciCode are nearly all `AssertionError`** — the revision LLM "fixes" code that was already correct, then breaks it. Suggests adding a "do not revise if previous attempt passed" guard at the codegen layer would tighten the Pareto curve.

## Per-(dataset, codegen) error-class histograms

### MBPP (N=100 each)

| Error class | direct | tdr | hd_rss |
|---|---|---|---|
| **PASS** | **65** | **68** | **50** |
| AssertionError | 29 | 28 | 25 |
| TypeError | 3 | 3 | 5 |
| NameError | 1 | 0 | **17** |
| AttributeError | 1 | 0 | 1 |
| ValueError | 0 | 0 | 2 |
| SyntaxError | 0 | 1 | 0 |
| Timeout | 1 | 0 | 0 |

Read: HD-RSS's 50-pass shortfall vs direct (-15pp) is dominated by the 17 NameErrors that direct/tdr never produce. These are composition-step bugs (the LLM writes a top-level function that calls `helper_a()`, but the helper wasn't actually defined in the composed source).

### SciCode (N=36 each, except tdr=35)

| Error class | direct | tdr | hd_rss |
|---|---|---|---|
| **PASS** | **7** | **9** | **7** |
| AssertionError | 16 | 13 | 16 |
| ValueError | 8 | 4 | 3 |
| TypeError | 0 | 4 | 0 |
| IndexError | 1 | 3 | 1 |
| SyntaxError | 1 | 1 | 3 |
| NameError | 0 | 0 | 3 |
| ImportError | 1 | 0 | 1 |
| NonZeroExit | 1 | 0 | 2 |
| AttributeError | 1 | 0 | 0 |
| Timeout | 0 | 1 | 0 |

Read: TDR's +2 pass improvement on SciCode is consistent with the AssertionError reduction (-3) but the trade-off is +4 TypeError + +2 IndexError (revision overcorrects boundary conditions / argument types).

### nanobrain_native (N=10 each)

| Error class | direct | tdr | hd_rss |
|---|---|---|---|
| **PASS** | **1** | **8** | **2** |
| RuntimeError | 6 | 0 | 1 |
| TypeError | 1 | 1 | 1 |
| ImportError | 1 | 1 | 1 |
| ModuleNotFoundError | 1 | 0 | 0 |
| AttributeError | 0 | 0 | 4 |
| RecursionError | 0 | 0 | 1 |

Read: nbnative is the dataset where the LLM is most out-of-distribution. Direct mostly hits RuntimeError (wrong API usage). TDR eliminates ALL 6 RuntimeErrors via iterative refinement on stderr feedback. HD-RSS introduces 4 AttributeErrors that direct never had — same composition-step weakness as MBPP NameErrors.

## Per-pattern unique-transition analysis (vs direct baseline)

### TDR transitions

| Dataset | Unique fixes | Unique breaks | Net |
|---|---|---|---|
| MBPP | 7 (mbpp/16, /56, /61, /77, /79, /85, /145) | 4 (mbpp/92, /100, /120, /135) | **+3** |
| SciCode | 4 (1.1, 38.2, 47.2, 70.3) | 2 (47.3, 49.1) | **+2** |
| nbnative | 7 (step_concat, _double, _filter_positive, _sum_list, _uppercase, _word_count, tool_calculator) | 0 | **+7** |

**Read**: TDR's uplift mechanism is asymmetric. On nbnative it's clean — 7 fixes, 0 breaks. On MBPP/SciCode the picture is muddier — each fix comes with a ~57% chance of an unrelated regression. Suggests an unconditional "always revise on first attempt" loop is suboptimal; a "revise only if exec failed, never re-revise a passing attempt" gate (which is what TDR does today) is necessary; adding "if revision produces a NEW failure, restore the previous attempt" rollback would close the gap.

### HD-RSS transitions

| Dataset | Unique fixes | Unique breaks | Net |
|---|---|---|---|
| MBPP | 8 | **23** | **-15** |
| SciCode | 2 | 2 | 0 |
| nbnative | 2 | 1 | **+1** |

**Read**: HD-RSS breaks 23 MBPP problems for every 8 it fixes. The 23 breaks are clustered on problems where the LLM (incorrectly) judged the simple problem "composite" and then the composition step introduced NameError/AttributeError/AssertionError bugs that direct never had. The 8 fixes are problems where the decomposition genuinely helped.

## What this taxonomy suggests for the NEXT pattern

Looking at the failure-class distribution, the highest-leverage next pattern targets a specific weakness:

1. **Composer-with-static-glue** (replaces HD-RSS's LLM composer with a deterministic template combining `def <parent>(...): return <subgoal_1>(...) + <subgoal_2>(...)`). Predicted impact: eliminates the 17 MBPP NameErrors + 4 nbnative AttributeErrors. Net HD-RSS would move from -15pp to ~0pp on MBPP.

2. **TDR with rollback** — track the previous-iteration's exec_succeeded; if the revised version newly fails and the previous succeeded, restore the previous version. Predicted impact: removes the 4 MBPP unique-breaks + 2 SciCode unique-breaks. Net TDR would move from +3pp / +7pp to ~+7pp / ~+10pp.

3. **Direct-with-stderr-prompt** — single-pass codegen where the LLM gets the test_code AND a "common failure modes for this dataset" prompt addition. Cheaper than TDR (1× LLM cost) but might capture some of the AssertionError reductions. Worth probing on MBPP where AssertionError dominates.

## What this taxonomy does NOT say

* **Cause not just shape**. The error_class tells us the LLM's output shape, not the LLM's reasoning. Two AssertionErrors can stem from totally different mistakes (wrong algorithm vs wrong edge case vs wrong return type). A deeper analysis would diff the generated_code field between direct/tdr/hd_rss versions.
* **Token-level cost not factored**. Wall time is the only cost the result JSONs record. A 20-line LLM revision costs ~1× the original direct call's tokens; a 200-line revision costs ~10×. The "TDR uniquely breaks" cases might be cheaper to live with than to fix.

## Files

| File | What |
|---|---|
| `tests/benchmarks/results/g96_bench_*.json` | Raw G96 sweep result data |
| `docs/g96_pattern_comparison_2026-05-17.md` | Cross-pattern accuracy + cost comparison |
| `docs/g101_failure_mode_taxonomy_2026-05-17.md` | **This doc** — failure-class breakdown |
| `docs/reasoning_patterns_landscape_2026-05-17.md` | Historical pattern landscape |

## Closes

* G101 (per-pattern failure-mode taxonomy from G96 result JSONs)
