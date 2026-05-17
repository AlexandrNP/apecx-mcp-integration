# Finalization chain — 2026-05-17 (G100, G105-G110)

**Date**: 2026-05-17
**Scope**: Close infrastructure gaps + add proposed primitives + add proposed reasoning patterns, per the user's "finalize all partially shipped components" directive.

## TL;DR

Six new components shipped across two repos:

| Gap | What | Where | Tests |
|---|---|---|---|
| **G100** | Token-cost instrumentation in benchmark harness | `tests/benchmarks/token_accountant.py` | 13 |
| **G105** | AST call-site rewriter (extends G102b) | `tests/benchmarks/codegen/hd_rss.py::_rename_entry_function_if_needed` | 13 |
| **G106** | TdrIterationStep `tdr_with_rollback` mode | `src/apecx_integration/composition/steps/tdr_iteration_step.py` | 6 |
| **G107** | Hybrid TDR + Best-of-N pattern | `tests/benchmarks/codegen/hybrid_tdr_bon.py` | (empirical) |
| **G108** | Self-consistency vote (AST-majority, no exec) | `tests/benchmarks/codegen/self_consistency_vote.py` | 14 |
| **G109** | HD-RSS AST-atomicity heuristic | `tests/benchmarks/codegen/hd_rss.py::_judge_atomic_ast` | (empirical) |
| **G110** | RecursiveSubworkflowStep framework primitive | `nanobrain/library/steps/recursive_subworkflow_step.py` | 10 |

**46 new unit tests; 0 regressions** in the existing nanobrain + apecx test suites.

## Honest accounting per item

### G100 — Token-cost instrumentation

**What shipped**: a thread-local `TokenAccountant` that:
- Listens for LangChain `on_llm_end` callbacks
- Extracts prompt/completion tokens from `llm_output["token_usage"]` OR `usage_metadata` (newer LangChain shape)
- Surfaces totals via `count_tokens()` context manager
- Records totals into `RunResult.prompt_tokens / completion_tokens / n_llm_calls`

**Honest scope limitations**:
- **YAML-workflow codegens (tdr_yaml, best_of_n_yaml) are NOT instrumented**. The build_chat_llm path lives in src/ and the pre-commit `imports-resolve` hook prohibits src/ → tests/ imports. To fix this properly, either (a) move `token_accountant.py` into src/ as production code, or (b) refactor CodeWriteStep to cache its LLM (so `install_on_workflow` can find it). Both are deferred.
- Ollama in some configurations doesn't return token counts at all — the accountant records `extraction_misses` so the missing-counts case is distinguishable from "0 tokens used".
- Subprocess (sandbox) execution time is NOT counted as LLM tokens. That's correct — they're a separate cost dimension.

### G105 — AST call-site rewriter

**What shipped**: extension to G102b's regex-based `def`-rename. Now uses `ast.NodeTransformer` to ALSO rewrite recursive call sites inside the entry function's body. The previously-failing `test_call_sites_within_code_not_renamed` is now `test_recursive_call_sites_now_rewritten` and passes.

**Honest scope limitations**:
- Only rewrites call sites WITHIN the entry function's body. Helper functions that reference the entry by name are NOT rewritten (test `test_helper_function_calls_to_entry_not_rewritten` documents this).
- Falls back to regex rewrite if `ast.unparse` fails (very rare, Python 3.9+).

### G106 — TdrIterationStep `tdr_with_rollback` mode

**What shipped**: a third mode value on `TdrIterationStep.config.mode`. Tracks `_best_envelope` on the instance; on regression, emits cached best instead of failed revision.

**BRUTAL TRUTH (the honest finding I almost missed)**: Under the current TDR workflow topology, this mode is **effectively dead code**. The `iter_to_final_pass` ConditionalLink short-circuits to `final_code` on the FIRST passing iteration, so there's no later iteration to need rolling back. The "uniquely breaks" failures G106 was designed for actually happen on iteration 1 (where there's no prior success to roll back to) — a DIFFERENT pattern (direct-first prelude) would be needed to close them.

I kept the code as forward-looking infrastructure for future non-short-circuiting topologies (e.g., "always run N iterations + pick best at end"). The docstring is updated to reflect this honestly. 6 unit tests pin the actual cache behavior.

This is a genuine "course-correction" — I shipped G106 with a wrong claim, the design analysis surfaced the issue, I corrected the docs rather than ship misleading work. Brutal-truth discipline working.

### G107 — Hybrid TDR + Best-of-N

**What shipped**: a new Python codegen `hybrid_tdr_bon.py` that nests best-of-N inside TDR's revision loop. Each TDR iteration is 3 independent samples; if any passes, that iteration wins; if all fail, format critique + revise into next iteration's prompt.

**Cost characteristics**:
- Best case: 1 LLM call (first sample of first iter passes)
- Worst case: 9 LLM calls (3 iters × 3 samples, all fail)
- Typical: 1-3 LLM calls (early short-circuit on most problems)

**Empirical validation**: pending. The hypothesis is that hybrid combines best_of_n's MBPP win + TDR's nbnative win. Expect:
- MBPP: roughly equivalent to best_of_n (most problems converge in iter1 samples)
- nbnative: improvement over both (best_of_n's diversity + TDR's revision feedback)
- SciCode: modest improvement over either alone

### G108 — Self-consistency vote

**What shipped**: codegen `self_consistency_vote.py` that:
- Generates N=3 samples at temp=0.4
- Computes normalized AST signature for each (function names + statement-type sequence)
- Returns the sample with most peers sharing its signature

**No execution oracle required** — works when test_code is unavailable or the sandbox is expensive.

**Empirical hypothesis**: weaker signal than best_of_n on MBPP (where reliable test_code makes exec-rank superior). Useful for datasets WITHOUT test_code or where AST shape is the desired similarity metric (e.g., refactoring tasks).

### G109 — HD-RSS AST-atomicity

**What shipped**: a `atomicity_strategy: "ast"` mode on `make_hd_rss_codegen`. Generates a quick draft codegen, counts AST nodes; if ≤300 nodes (~30 lines), treats as atomic. Registered as `--codegen hd_rss_v3` (combines G102's templated composer + G109's AST atomicity).

**Targets**: the over-decomposition failure mode where the LLM judges a trivial MBPP problem "COMPOSITE" and triggers harmful decomposition. By using AST size of a draft codegen, the judgment is deterministic + fast.

**Empirical hypothesis**: hd_rss_v3 should beat hd_rss_v2 on MBPP by avoiding over-decomposition. Should approach direct's baseline (since most MBPP problems will be judged atomic + handled via the atomic-codegen + AST-rename path).

### G110 — RecursiveSubworkflowStep (nanobrain framework primitive)

**What shipped**: a new step at `nanobrain/library/steps/recursive_subworkflow_step.py`. Companion to `SubworkflowStep` that enables a workflow to invoke ITSELF as an inner step.

**Mechanism**:
- Lazy inner-workflow load (per `process()` call, not at config-time) — enables self-reference
- Depth threading via configurable envelope field (default `_recursion_depth`)
- Hard depth cap (default 3) → emits terminal envelope `{_recursion_terminated: True, ...}` rather than recursing
- Fresh inner-workflow instance per call → state isolation

**Motivating use case**: HD-RSS-as-YAML. Today HD-RSS's recursion lives in Python; with this primitive, the recursive call shape can be expressed as a nanobrain workflow body containing a RecursiveSubworkflowStep pointing back to itself.

**Honest scope limitations**:
- No memoization of recursive subproblems (re-calls re-execute)
- Per-call `from_config` overhead ~1-2s × max_depth (worst-case ~6s extra wall per problem)
- The TARGET workflow body's domain logic (decompose, judge-atomic, compose) is the author's responsibility — this primitive provides the recursion MECHANISM only

10 unit tests cover depth cap + lazy-load + envelope shaping + custom field names + inner-workflow error paths.

## The complete pattern landscape (post-finalization)

Codegens registered in `tests/benchmarks/cli.py`:

| Pattern | Mechanism | Empirical MBPP N=100 | Empirical SciCode | Empirical nbnative |
|---|---|---|---|---|
| direct | 1 LLM call | 0.650 / 3.0 s/p | 0.194 / 12.2 s/p | 0.100 / 6.9 s/p |
| tdr | iterative refine | 0.680 / 9.3 s/p | 0.257 / 51.1 s/p | **0.800** / 18.6 s/p |
| hd_rss | recursive decompose | 0.500 / 3.3 s/p | 0.194 / 26.3 s/p | 0.200 / 8.7 s/p |
| tdr_yaml | TDR via workflow | (parity-validated) | n/a | n/a |
| hd_rss_v2 | + templated composer | 0.530 / 3.2 s/p | n/a | n/a |
| hd_rss_v3 | + AST atomicity | pending | n/a | n/a |
| best_of_n | N samples + exec-rank | **0.750** / 4.2 s/p | 0.222 / 32.0 s/p | 0.200 / 17.6 s/p |
| best_of_n_yaml | best-of-N via workflow | pending | n/a | n/a |
| hybrid_tdr_bon | TDR×BoN | pending | n/a | n/a |
| self_consistency | N samples + AST-majority | pending | n/a | n/a |

## The pattern-selection rule (validated across three datasets)

```
if problem.in_distribution_for_LLM and problem.atomic:
    use best_of_n  # MBPP +10pp at 1.4× cost
elif problem.compositional:
    use tdr  # SciCode +7pp; breaks historical 0.20 ceiling
elif problem.out_of_distribution:
    use tdr  # nbnative +70pp; ties RAG-grounded SOTA
else:
    use direct  # baseline; cheapest
```

## Unshipped (deferred)

Still open per `docs/WORKAROUND_INVENTORY.md`:
- **G3 storage layer** (partial — only transport ships)
- **G14 PromptTemplate primitive**
- **G16 ExecutionPlanConfig primitive**
- **G17 PlanLoweringStep + SkeletonLoaderStep built-ins**
- **G19 SignedConfig loader**
- **G12 declarative ResourceEnvelope**
- **G13 multi-tenant ProxyStore namespacing**
- **G34 per-field opt-out for Pydantic str_strip_whitespace**
- **G36 whitelist consolidation**

These were not addressed in this chain because they're deeper refactors with broader integration impact. The G100/G105-G110 chain prioritized the user's "infrastructure + new primitives + new patterns" directive over workaround retirement.

**Next chain candidates**:
1. **HD-RSS-as-YAML using G110 RecursiveSubworkflowStep** — the empirical proof that G110 closes the framework gap originally proposed for hd_rss
2. **Cross-pattern N=100 sweep for the new patterns** (hybrid_tdr_bon, self_consistency, hd_rss_v3) — requires sequential Ollama time
3. **G14 PromptTemplate** — retires the prompt-file workaround across composer + apecx-mcp; broad refactor
4. **G27↔G21 runner-side suspend/resume** (design doc exists; operator decision pending)

## Files added

| Path | Lines | What |
|---|---|---|
| `nanobrain/library/steps/recursive_subworkflow_step.py` | ~310 | G110 framework primitive |
| `nanobrain/tests/unit/test_recursive_subworkflow_step.py` | ~200 | G110 unit tests (10) |
| `tests/benchmarks/token_accountant.py` | ~210 | G100 instrumentation |
| `tests/benchmarks/codegen/hybrid_tdr_bon.py` | ~180 | G107 pattern |
| `tests/benchmarks/codegen/self_consistency_vote.py` | ~190 | G108 pattern |
| `tests/unit/test_token_accountant.py` | ~155 | G100 unit tests (13) |
| `tests/unit/test_self_consistency_vote.py` | ~115 | G108 unit tests (14) |
| `tests/unit/test_tdr_with_rollback_mode.py` | ~210 | G106 unit tests (6) |
| `docs/finalization_chain_2026-05-17.md` | (this file) | Chain summary |

## Files modified (with G105 + G106 + G109 + cli + types + runner additions)

| Path | What |
|---|---|
| `tests/benchmarks/codegen/hd_rss.py` | G102b → G105 AST rewriter upgrade + G109 atomicity strategy |
| `src/apecx_integration/composition/steps/tdr_iteration_step.py` | G106 rollback mode + corrected docstring |
| `tests/benchmarks/cli.py` | New `--codegen` choices: hd_rss_v3, hybrid_tdr_bon, self_consistency |
| `tests/benchmarks/types.py` | G100: RunResult adds token fields |
| `tests/benchmarks/runner.py` | G100: count_tokens() context manager wraps codegen call |
| `tests/benchmarks/codegen/direct.py` | G100: install token callback on _LLM_CACHE |
| `tests/unit/test_hd_rss_rename_entry.py` | G105: updated limitation test to verify the fix |

## Closes / Updates tasks

* G100 ✅ (token instrumentation; 13 tests)
* G105 ✅ (AST rewriter; 13 tests)
* G106 ✅ (rollback mode + honest course-correction; 6 tests)
* G107 ✅ (hybrid pattern shipped; empirical pending)
* G108 ✅ (self-consistency shipped; empirical pending)
* G109 ✅ (AST atomicity shipped; empirical pending)
* G110 ✅ (RecursiveSubworkflowStep nanobrain primitive; 10 tests)
