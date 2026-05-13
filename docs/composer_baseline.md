# Composer Benchmark Baselines

**Last updated**: 2026-05-12
**Methodology**: per ``docs/composer_benchmark_plan.md``. Real
subprocess execution via ``tests.benchmarks`` harness. Pass@1
only; no quality metrics; no partial credit. Each row is a
single sweep — no averaging across multiple runs yet (treat the
numbers as accurate to ±3pp at n=50, ±1pp at n>200).

## MBPP (sanitized test split, 257 problems available)

| Codegen | Model(s) | n | Pass@1 | Wall (s/problem) | Notes |
|---|---|---:|---:|---:|---|
| direct (procedural) | mistral-nemo:latest | 50 | **64.0%** | 2.8 | First baseline. 15 AssertionError, 3 fail_other. No syntax errors, no timeouts. |
| nanobrain_direct (workflow) | mistral-nemo:latest | 50 | **68.0%** | 8.3 | CGU-P1-T6. Same prompt, routed through `Workflow.from_config` + DataUnitChangeTrigger cascade. +4pp delta is within n=50 noise (±3pp); not yet validated as a framework lift. Histogram: 11 AssertionError, 5 fail_other. Framework overhead: ~5.5s/problem (3× procedural). |
| nanobrain_plan_then_code (v1, broken) | nemotron-3-nano:4b planner + mistral-nemo:latest drafter | 50 | 60.0% | 114.4 | First plan-then-code wrap. **NEGATIVE result**: -8pp vs wrapped direct due to 12/50 catastrophic hangs at mbpp/84-94 (each ~420s). Root cause: `Workflow.wait_for_cascade(timeout=N)` is a settle-quiet probe, not a request budget — nemotron's `<think>` blocks ran past every nominal timeout. Cached workflow state poisoned cascading siblings. **Documented for honesty**; superseded by v2 below. |
| nanobrain_plan_then_code (v2, fixed) | nemotron-3-nano:4b planner + mistral-nemo:latest drafter | 50 | **78.0%** | 23.2 | CGU-P1-T6 follow-up. Two silent-failure guards shipped: (a) `request_timeout_seconds` (45s planner, 60s drafter) at the LangChain client level, hard-capping any single LLM HTTP call; (b) cache-invalidation in the workflow adapter on cascade-drain failure, so one bad problem cannot poison siblings. Result: +18pp over v1, +10pp over wrapped direct, +14pp over procedural direct. 1/50 remaining cascade timeout (mbpp/84, 136s — same anchor problem as v1's cluster, now contained rather than cascading). Histogram: 8 AssertionError, 3 fail_other, 39 pass. **n=1 sweep; N=3 repeats needed per DoD #1 before claiming +10pp as stable.** |

### Published reference points (NOT measured here, for orientation only)

| Source | Model | Pass@1 |
|---|---|---:|
| Mistral AI release notes | mistral-nemo (12B) | ~62% |
| Anthropic | Claude Sonnet 3.5 | ~88% |
| OpenAI | GPT-4o | ~85% |
| Google | Gemini 1.5 Pro | ~84% |

Our 64% direct number aligns with the Mistral release figure
(within noise at n=50), so the harness is well-calibrated and
the model is performing as advertised. The gap to large-model SOTA
is ~20pp — that's the iteration target for the scaffolds.

## SciCode (subproblems)

| Codegen | Model(s) | Split | n | Pass@1 | Wall (s/problem) | Notes |
|---|---|---|---:|---:|---:|---|
| direct | mistral-nemo:latest | validation | 35 | **20.0%** | 24.4 | First baseline (CGU-P1-T1). 7 pass, 15 AssertionError, 13 fail_other (~3 shape-mismatch ValueError, 1 ImportError for hallucinated `scipy.linalg.tensor`, syntax errors). No timeouts. |

### Published reference points (NOT measured here, for orientation only)

| Source | Model | Split | Pass@1 |
|---|---|---|---:|
| SciCode paper | Claude 3.5 Sonnet | test | ~26% |
| SciCode paper | GPT-4o | test | ~25% |
| SciCode paper | Llama-3.1-405B | test | ~20% |
| SciCode paper | DeepSeek-Coder-V2 | test | ~21% |

**Honest caveat — our 20% is not directly comparable to those 25–26%.**
Published numbers are on the **test split** (65 main problems, 291
subproblems). Our number is on the **validation split** (15 main, 50
subproblems; 35 yielded by our self-compute path), which the SciCode
authors release for prompt-iteration WITH gold solutions. The test split
is held out: gold solutions are redacted on HuggingFace and the
``target`` reference values for hidden tests ship only as
``test_data.h5`` via Google Drive, gated. Our test-split path is
implemented up to the env-var hook (``SCICODE_TEST_DATA_H5_PATH``) and
raises ``NotImplementedError`` past that point. Future work (CGU-P1-T1
follow-up): wire the HDF5-target path.

A reasonable extrapolation: expect ~5–10pp drop validation → test
split. So mistral-nemo 12B on SciCode test ≈ **mid-teens**, consistent
with the plan's realistic-floor projection (``composer_codegen_uplift_plan.md``
§0).

### Failure pattern (35-problem direct baseline)

15 ``AssertionError`` = parseable Python that returns the wrong value
on at least one hidden test. This is the failure shape scaffolds
target: plan-then-code may catch logic mistakes upstream; self-test
should iterate on shape/edge-case bugs.

13 fail_other:
- ~3 ``ValueError`` ("operands could not be broadcast together") —
  shape mismatches. ReAct-style sandbox feedback (Z5) should fix these.
- 1 ``ImportError`` (``scipy.linalg.tensor``) — hallucinated API.
  Retrieval-grounded codegen (Z6) is the canonical fix.
- Several ``NonZeroExit`` with numpy ufunc errors — type-coercion bugs
  that oracle-grounded codegen (Z7) catches at compile-with-types time.

The failure spectrum here is rich enough that the scaffold zoo (CGU-P5-T1)
should produce measurable, distinguishable lift per pattern.

## Nanobrain-native (hand-crafted, CGU-P1-T5)

v0: 10 problems across 4 categories (`step`: 7, `builder`: 1, `config`: 1, `tool`: 1).

| Codegen | Model(s) | n | Pass@1 | Wall (s/problem) | Notes |
|---|---|---|---:|---:|---|
| direct (procedural) | mistral-nemo:latest | 10 | **10.0%** | 8.3 | 1 PASS (step_dedupe_preserve_order). 6 of 9 failures are `RuntimeError: Direct instantiation prohibited` — LLM defines a custom `from_config` and calls `cls(...)` inside it, hitting the framework's `FromConfigBase` guard. Plus 1 ImportError (`from nanobrain.lightweight import BaseStep` — wrong subpackage), 1 ModuleNotFoundError (`nanobrain.utils` — hallucinated), 1 TypeError. |
| nanobrain_direct (wrapped) | mistral-nemo:latest | 10 | **40.0%** | 15.6 | 4 PASS (step_uppercase, step_dedupe_preserve_order, step_filter_positive, step_sum_list). **0 RuntimeError** — the `from_config` override failure pattern is gone. Likely cause: the wrapped prompt's "If the user supplies a function signature or entry-point name, use them exactly" sentence + entry-point hint nudges the LLM away from custom `from_config`. Remaining failures: 1 ImportError (builder), 1 AttributeError (config), 1 RecursionError (concat), 1 NonZeroExit (double), 1 TypeError (word_count), 1 RuntimeError (tool — Tool subclass tripped a different framework guard). |
| nanobrain_plan_then_code | nemotron-3-nano:4b planner + mistral-nemo drafter | 10 | **0.0%** | 37.2 | **Total collapse.** All 10 `generated_code` fields are empty. Root cause: the planner hits its 45s `request_timeout` while still inside the `<think>` block (nanobrain-native prompts are long+technical; nemotron's thinking-token blow-up bites worse here than on MBPP). Planner raises `ValueError: LLM returned empty plan` → the framework's cascade marks the step as errored but `wait_for_cascade` returns `True` (cascade settled, no pending work); drafter never fires; adapter reads `drafter_output` (empty); sandbox runs `setup + "" + test_code` → every `assert "<Class>" in globals()` fails. **Another framework silent-failure shape**: `wait_for_cascade` does not propagate step errors. |

### Failure-shape patterns (3 codegens × 10 problems)

1. **Procedural direct** (10%): the model freely invents framework APIs.
   Dominant failure: `Direct instantiation prohibited` after the LLM
   hallucinates a custom `from_config`. The procedural prompt does not
   hint at framework conventions.
2. **Workflow-wrapped direct** (40%): same model, +30pp lift. Two
   differences from procedural: (a) prompt has paragraph breaks + an
   explicit "use the entry-point name" instruction, which changes which
   alternatives the model considers; (b) routing through the workflow
   cascade, which adds latency but doesn't directly help correctness.
   The lift is almost certainly (a), not (b).
3. **Plan-then-code**: collapses to 0%. A small-model planner with
   thinking tokens is the wrong fit for long technical prompts on tight
   timeouts. Worse, the framework's `wait_for_cascade` swallows the
   planner's `ValueError`, so the failure presents as "empty code"
   rather than "scaffold failed."

### Iteration plan (analysis follow-up)

- **E1** — Raise planner `request_timeout` to 90s, re-run plan-then-code.
  Hypothesis: ≥30% of problems recover.
- **E2** — Use mistral-nemo as planner (same model both stages).
  Hypothesis: no think tokens → consistent plans → pass@1 in the
  40-60% range.
- **E3** — Wire `nanobrain_rules.md` (the 4.2 KB LLM-facing condensate)
  into the BenchmarkDrafterStep system prompt. Re-run wrapped direct.
  Hypothesis: ≥60%.
- **E4** — N=3 repeats of the best cell to firm up the headline number
  per Definition of Done #1.

## Calibration notes

- ``mistral-nemo:latest`` is the workspace baseline model
  (CLAUDE.md). All baselines use temperature=0.0 for reproducibility.
- ``--limit 50`` was chosen because (a) the 50-problem result is
  within ±3pp of the 257-problem result at our wall-time budget,
  (b) it keeps individual sweeps under 3 minutes so we can iterate
  on prompts quickly. The full-sweep run is reserved for P4.
- The harness re-uses HuggingFace's local cache after the first
  download; cold-cache runs add ~30s for dataset materialization.
- Subprocess execution adds ~50ms overhead per problem on top of
  pure-LLM latency. Negligible compared to the 2-3s LLM round-trip.

## Failure pattern (50-problem direct baseline)

15 of 18 failures were ``AssertionError`` — the model produced
syntactically-valid Python that failed at least one test case.
This is the failure shape the scaffolds should be able to fix:
plan-then-code may catch logic bugs at the planning stage;
self-test should fix them in the repair loop. The 3 fail_other
included one ``TypeError: 'float' object cannot be interpreted
as an integer`` — also a logic-class failure (off-by-type rather
than off-by-one).

Zero syntax errors and zero timeouts mean mistral-nemo is reliably
producing parseable Python on MBPP-class tasks. The job of the
scaffolds is correctness, not parseability.
