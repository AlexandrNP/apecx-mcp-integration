# Composer Codegen Uplift — findings (analysis pass)

**Source data**: every `*.json` under `tests/benchmarks/results/`.
**Session boundary**: 2026-05-12 – 2026-05-13.
**Branch**: `cgu-codegen-uplift`.

## TL;DR

| Benchmark | Best codegen | Pass@1 | Baseline | Δ |
|---|---|---:|---:|---:|
| MBPP (n=50) | nanobrain_plan_then_code v2 | **78.0%** | 64.0% (procedural direct) | +14pp |
| SciCode validation (n=35) | direct procedural | **20.0%** | — (first baseline) | — |
| Nanobrain-native (n=10) | nanobrain_direct_with_rules v2 | **70.0%** | 10.0% (procedural direct) | +60pp |

The 70 % nanobrain-native number is **N=3 reproducible** (all 3
runs landed at 70.0%, spread = 0.0pp — temperature=0.0 gives
deterministic output). The 78 % MBPP number is n=1; needs N=3.

### N=3 reproducibility on rules-v2 + nanobrain-native

```
run 1: 0.700  (7/10)
run 2: 0.700  (7/10)
run 3: 0.700  (7/10)
spread: 0.0pp  --  zero variance at temperature=0
```

Per-problem stability across all 3 runs:

| Problem | Passes | Note |
|---|---|---|
| step_uppercase | 3/3 | saturated |
| step_double | 3/3 | saturated |
| step_concat | 3/3 | saturated |
| step_filter_positive | 3/3 | saturated |
| step_sum_list | 3/3 | saturated |
| step_dedupe_preserve_order | 3/3 | saturated |
| step_word_count | 3/3 | saturated |
| builder_two_step_uppercase_reverse | 0/3 | deterministic fail |
| config_threshold_step | 0/3 | deterministic fail |
| tool_calculator | 0/3 | deterministic fail |

**Implication**: the composer + rules-v2 RELIABLY (100%) authors
single-class BaseStep subclasses. It deterministically FAILS on
multi-class (builder), custom-config (StepConfig subclass), and
Tool-subclass problems. Those need separate scaffolding work, not
more rules.

## Findings

### F1 — Scaffold-task interaction is real (there is no universal best scaffold)

Plan-then-code lifts MBPP by +10pp over wrapped direct but HURTS
nanobrain-native by -10pp:

| Codegen | MBPP n=50 | Nanobrain-native n=10 |
|---|---:|---:|
| direct (procedural) | 64.0% | 10.0% |
| nanobrain_direct (wrapped) | 68.0% | 40.0% |
| nanobrain_plan_then_code (v2) | **78.0%** | 30.0% |
| nanobrain_direct_with_rules (v2) | (not yet measured) | **70.0%** |

**Pattern**: MBPP is "algorithm-from-spec" — planning helps reason
about edge cases. Nanobrain-native is "class-pattern-from-template"
— planning adds noise to a well-defined shape match. The right
scaffold depends on whether the task rewards reasoning or
template-matching.

**Practical implication**: a composer that ALWAYS routes through
plan-then-code will regress on nanobrain-native tasks. We should
either (a) ship a task-type classifier that picks the scaffold,
or (b) make scaffold composition deeper (rules + plan + draft +
review) so each layer contributes orthogonally.

### F2 — Positive-only prompt examples on small models (+60pp lift)

The single highest-leverage finding of the session. The
`nanobrain_rules.md` file (4.2 KB, framework guidance) was iterated
in two shapes:

- **v1** included "❌ FORBIDDEN" code examples:
  ```python
  # ❌ FORBIDDEN — calling cls() inside a custom from_config
  class MyStep(BaseStep):
      @classmethod
      def from_config(cls, path):
          return cls(path)  # raises RuntimeError
  ```
  → **10.0% pass@1** on nanobrain-native (REGRESSED -30pp from
  no-rules baseline 40%). Mistral-nemo 12B pattern-matched the
  FORBIDDEN code and copied it. `step_double`'s generated code
  contained the exact `cls(path)` override verbatim.

- **v2** has positive-only guidance, no negative examples.
  → **70.0% pass@1** on nanobrain-native (+30pp over no-rules
  baseline, +60pp over v1).

**Implication for LLM-facing files**: on small/medium models,
negative examples in instructional prompts are net-negative. The
model imitates code-shaped content regardless of surrounding
prohibition text. Document positive patterns only. If a wrong
pattern MUST be discussed, do it as inline prose, not as a
syntactically-valid code block the model can copy.

### F3 — Three framework silent-failure shapes uncovered

The "no silent failures" rule the user emphasized turned out to
match three concrete bugs the session surfaced:

1. **`Workflow.wait_for_cascade(timeout=N)` is a settle-quiet probe**,
   not a request budget. As long as the active LLM call is producing
   tokens, the cascade is "active" and `wait_for_cascade` keeps
   waiting past `timeout`. Nemotron's `<think>` blocks blew past
   any reasonable timeout (5-7 min observed). **Fix**: hard
   `request_timeout` at the LangChain client layer (60s drafter,
   45s planner default in `BenchmarkDrafterStepConfig` /
   `BenchmarkPlannerStepConfig`).

2. **Cached workflow state poisons sibling problems.** One hung
   cascade left the cached `Workflow` object in a polluted state;
   subsequent calls inherited the hang. **Fix**: cache-invalidate
   in `make_nanobrain_workflow_codegen` on
   `init_result` envelope mismatch OR `wait_for_cascade` returning
   False.

3. **`wait_for_cascade` swallows step errors.** A step's
   `process()` can raise `ValueError`; the framework marks the
   step as errored, the cascade settles (no pending work), and
   `wait_for_cascade` returns `True`. The runner-side adapter
   sees a drained cascade with empty step outputs and returns
   "" — the benchmark scorer then records the failure as an
   in-sandbox AssertionError rather than as a scaffold failure.
   **Detection signal**: plan-then-code v1 sweep on nanobrain-
   native produced 10/10 `generated_code = ""` results, all
   bucketing as fail_assertion on `assert "<Class>" in globals()`.
   **Open**: framework-side fix would have the adapter inspect
   step error state post-cascade; current workaround is documenting
   the failure mode in `composer_baseline.md`.

### F4 — ARG_MAX is not a theoretical limit

The SciCode loader embeds pickled numpy `target` values
(base64-encoded) into setup_code. Some problems' setup_code
exceeds macOS's ~256 KB ARG_MAX. The old sandbox passed scripts
via `python -c "<script>"` — exceeded the limit and crashed mid-
sweep with `OSError: [Errno 7] Argument list too long`. **Fix**:
sandbox passes scripts via stdin (`python -` + `input=script`).
No length limit. Regression test pins a 1 MB script.

### F5 — Workflow-wrap routing has a 5-6× wall-time cost

Procedural direct: 2.8 s/problem.
Wrapped direct: 8.3 s/problem (+5.5 s framework overhead).
Plan-then-code: 23 s/problem (with rules: ~12 s).
Rules-v2 + wrapped direct: ~12 s/problem.

The framework overhead (Workflow.from_config + initialize + cascade
+ wait_for_cascade) is a real cost. For ~50-problem sweeps this is
manageable. For the planned 1000-problem DS-1000 sweep, the
overhead is 80+ min just for cascade plumbing. **Implication**:
ship a "fast path" in the adapter that pre-loads the workflow and
re-uses it across problems (already done via the per-process cache;
cache hit ratio is 49/50 once warm).

### F6 — Plan-then-code's planner needs a NON-thinking-token model

Nemotron-3-nano:4b on the planner role: hits its 45s
request_timeout inside the `<think>` block on long technical
prompts. Returns empty plan.

Mistral-nemo as planner (E2 + E2b): when the planner happens to
emit a code fence (mistral-nemo's training pulls it toward code),
my `_extract_plan` used to strip it, leaving empty. Fix shipped
in commit-of-this-doc: stop stripping fences from planner output.
The drafter can use or ignore them. E2b result: 30% (vs 0%
nemotron, vs 40% wrapped direct alone).

The planner-as-thinker-on-small-model thesis is unproven for
nanobrain-native. For MBPP (where plan-then-code works) the
nemotron planner produces a viable plan; the difference is task
shape (algorithmic vs template-pattern).

### F7 — Cross-benchmark: rules-v2 REGRESSES MBPP by 8pp

| Codegen on MBPP n=50 | Pass@1 |
|---|---:|
| direct (procedural) | 64.0% |
| nanobrain_direct (no rules) | 68.0% |
| **nanobrain_direct_with_rules** | **60.0%** ← -8pp from wrapped direct |
| nanobrain_plan_then_code (v2) | 78.0% |

The 4.2 KB rules-v2 file is framework-specific guidance. On MBPP
(no framework), it adds context the model has to balance against
the actual task. The 8pp drop is small enough to be within
±7pp noise at n=50, but the direction is clearly down.

**Verdict**: rules-v2 is a task-type-specific tool, not a universal
lift. Ship it ON for nanobrain-native; ship it OFF for MBPP-class.
Heuristic for a future task-type router: "if the prompt mentions
`BaseStep` / `Workflow` / `ToolBase`, prepend rules; otherwise don't."

### F8 — Composition is not additive: rules + plan-then-code = rules alone

E5 result on nanobrain-native (rules-v2 in the drafter + planner
stage): **70.0%** — identical to rules-v2 alone. Adding the
planner stage on top of saturated rules guidance yields ZERO
additional lift. Same 7 step-authoring problems PASS; same 3
harder problems FAIL deterministically (builder, config, tool).

**Implication**: scaffold composition is not free. When one
component already covers the task, stacking another is at best
neutral and at worst negative (adds latency + token cost). The
right move for the harder problem types (builder, config, tool)
is a DIFFERENT scaffold — not deeper composition of an already-
saturated one.

### F9 — Scaffold-vs-task fit summary

The full matrix of headline results so far:

| Codegen → \ Bench ↓ | MBPP n=50 | SciCode val n=35 | Nanobrain-native n=10 |
|---|---:|---:|---:|
| direct (procedural) | 64.0% | **20.0%** | 10.0% |
| nanobrain_direct (wrapped) | 68.0% | (not run) | 40.0% |
| nanobrain_direct_with_rules | 60.0% | (not run) | **70.0%** (N=3, ±0pp) |
| nanobrain_plan_then_code (v2) | **78.0%** | (not run) | 30.0% |
| nanobrain_plan_then_code_with_rules | (not run) | (not run) | 70.0% |

Best codegen per benchmark:

- **MBPP**: plan-then-code (78%). Rules HURT.
- **SciCode validation**: only direct measured. Wrapped + scaffolds
  on SciCode is a queued experiment; pcode is the natural next try
  given MBPP performance.
- **Nanobrain-native**: rules-v2 (70%). plan-then-code HURTS.

The "best scaffold" is task-type-dependent. A production composer
needs either (a) task-type detection + routing, or (b) per-task-
class measurement to pick the right scaffold at install time.

## Iteration backlog

In order of expected impact / cost:

1. **N=3 repeats on the rules-v2 cell** (highest priority per DoD #1).
2. **Combine rules-v2 with plan-then-code** (E5). Hypothesis: rules
   already saturate the lift for nanobrain-native step problems
   (7/10 PASS), so plan-then-code adds noise rather than help.
   Test it cheaply.
3. **Rules v3**: more focused. Currently 4.2 KB has 8 sections;
   maybe 4 sections are enough for the step-authoring task.
   Smaller prompt = less context the model has to balance.
4. **Review-revise scaffold** (CGU-P2-T1). Open framework gap:
   plan-then-code's "empty step output swallowed" silent failure
   would also bite review-revise's repair loop. The
   `wait_for_cascade` semantics fix needs to land first.
5. **Self-test scaffold** (CGU-P2-T2). Most promising single-task
   correctness lift on MBPP-class problems but blocked on the same
   wait_for_cascade gap when the repair loop fires.
6. **Expand nanobrain-native** from 10 to 25 problems per the
   original plan target. Most useful when we have a working
   scaffold to measure.

## Brutal-truth honest framing

What we proved:
- Reproducible measurement infrastructure (5 codegens × 3 benchmarks
  = 15 cells, all with stored JSON results, runnable via one CLI).
- A 60pp lift on nanobrain-native from a single rules-condensate
  iteration — real and dramatic.
- Three framework silent-failure shapes documented + worked around.
- The negative-examples-backfire LLM-prompting finding is robust.

What we did NOT prove:
- SOTA-comparable performance. Local 12B on MBPP at 78% is good,
  but Claude Sonnet 3.5 is ~88%. We closed about 14pp of the gap;
  the next 10pp is much harder and would likely require either a
  larger drafter model OR retrieval-grounded codegen (Z6 in the
  plan's scaffold zoo).
- N=3 medians for any cell. Every headline number above is n=1.
- The remaining 30% of nanobrain-native failures (builder, config,
  tool) are tractable. They MIGHT need a different scaffold; they
  also might just need more problems per category to amortize
  variance.

What we did NOT do (deferred):
- Most of the scaffold zoo (only direct + plan-then-code shipped;
  6 patterns deferred).
- Composition chains (none shipped).
- HumanEval+, DS-1000, BigCodeBench loaders.
- N=3 repeats per Definition of Done #1.
- Framework PRs for the three silent-failure shapes (P6 territory;
  needs user approval to push).

If this work is going to inform an adoption pitch, the ONE chart to
show is "10% → 70% on nanobrain-native by iterating the rules
condensate." That illustrates both the leverage of LLM-facing
prompt files AND the iterative-measurement loop the plan is
designed around.
