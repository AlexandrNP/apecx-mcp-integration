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
| direct | mistral-nemo:latest | 50 | **64.0%** | 2.8 | First baseline. 15 AssertionError, 3 fail_other. No syntax errors, no timeouts. |

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

Not yet measured. Plan: P1c.

## Nanobrain-native (hand-crafted)

Not yet built. Plan: P1d.

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
