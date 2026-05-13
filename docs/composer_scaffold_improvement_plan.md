# Scaffold Improvement Plan — what to add next, gated by measured evidence

**Status**: 2026-05-13, drafted from `composer_codegen_uplift_findings.md` F1-F9.
**Branch**: `cgu-codegen-uplift`.

## Premise

The current matrix tells us three things with high confidence:

1. **Rules-v2 saturates step-authoring** on nanobrain-native (7/7 step
   problems pass 3/3 at temperature=0). No additional scaffolding
   improves the 70% headline because the remaining 3 failures
   (`builder`, `config`, `tool`) fail *deterministically* on a different
   axis — the LLM gets the *shape* wrong, not the details.
2. **Plan-then-code helps MBPP, hurts nanobrain-native.** Composition
   is not additive; the right scaffold depends on the task class.
3. **The three deterministic nanobrain-native failures share a pattern**:
   the LLM produces code that looks plausible but violates a framework
   invariant the prompt cannot fully convey (overriding `from_config`,
   subclassing `StepConfig` without the `_strip_framework_keys`
   validator, putting an `eval` inside a class that has no `name` attribute).

The improvement plan is structured around what each failure mode
NEEDS — not around "implement every scaffold in the zoo."

## Plan structure

Three tracks, each with explicit hypotheses and measurement plans.
A track does not advance to the next gate until the current gate's
result lands within ±5pp of its expected band.

---

## Track A — review-revise (the most-promising-next, AUTHORED THIS SESSION)

### A1. Hypothesis

The 3 deterministic nanobrain-native failures (builder, config, tool)
share a pattern: the LLM produces code that *looks* plausible but
violates a framework invariant on the first attempt. A reviewer step
that critiques the candidate before the final output gives the model
a second chance to fix shape-level errors.

### A2. Scaffold shape

Linear three-step chain via `DirectLinks` with `auto_transfer: true`:

```
input → drafter (with rules) → reviewer → reviser → output
```

* Drafter: existing `BenchmarkDrafterStep` with `nanobrain_rules.md`.
* Reviewer: new `BenchmarkReviewerStep` — emits critique + passthrough
  of `code_spec` + `previous_attempt = drafter.code_source`.
* Reviser: same class as drafter (`BenchmarkDrafterStep`) but with a
  reviser-specific prompt. The drafter's `_build_user_message` already
  handles `previous_attempt` + `critique` when present.

No `ConditionalLink` in v0 — the reviser's prompt tells it to emit
the previous attempt unchanged if the critique is exactly `PASS`. Cost:
1 wasted LLM call per already-correct problem. Benefit: simpler
workflow, framework-native, no conditional routing edge cases.

### A3. Acceptance gate

* Smoke: workflow loads via `Workflow.from_config`; one MBPP-trivial
  problem produces non-empty code via the cascade end-to-end.
  ✅ Achieved (workflow loads; smoke pending — needs Ollama free).
* nanobrain-native n=10 pass@1 ≥ 80% (vs current 70% with rules-v2 alone).
  Expected lift: 1 of 3 currently-failing problems recovers; the other
  2 may need deeper composition.
* MBPP n=50 pass@1 ≥ 65% (acceptable if not below the 64% procedural
  baseline; review-revise is overhead-heavy and may not lift MBPP).

### A4. Expected costs

* Wall time: ~3x wrapped direct (three LLM calls per problem) =
  ~25-30s/problem on this hardware.
* Token cost: ~3x.
* Failure modes:
  - Reviewer hallucinates problems and the reviser "fixes" them
    away from a correct solution. Mitigation: the reviewer prompt
    asks for `PASS` when correct.
  - Cascade timeout doubles. Mitigation: 60s `request_timeout` per
    LLM call (already shipped); cascade timeout default at 120s.
  - Same `wait_for_cascade swallows step errors` shape from F3:
    if the reviewer fails (empty critique → ValueError), the
    drafter's output never reaches the reviser. Already-shipped
    workaround: cache-invalidate on cascade-drain-False.

---

## Track B — self-test (for MBPP-class lift beyond 78%)

### B1. Hypothesis

Plan-then-code's 78% on MBPP leaves 11/50 fail_assertion problems —
the model produces parseable code that returns the wrong value. A
self-test scaffold would (a) have the LLM emit pytest cases alongside
the code, (b) run the cases in the sandbox, (c) on failure, repair.
This is exactly the failure-shape the failures match.

### B2. Scaffold shape

```
input → test-writer → code-writer → in-sandbox exec
                                       ↓
                            (conditional) all_pass → output
                                              fail → repair → exec → output
```

This requires `ConditionalLink` with a predicate on a "test results"
data unit, AND an in-cascade sandbox execution step. The existing
`IsolatedPyExecStep` would be the canonical fit; needs cascade wiring
and a `max_repair_iterations: 2` bound to prevent runaway.

### B3. Acceptance gate

* MBPP n=50 pass@1 ≥ 81% (vs 78% wrapped plan-then-code v2).
* Repair count metadata > 0 across the suite (otherwise the loop is
  dead).
* SciCode validation n=35 pass@1 ≥ 25% (vs 20% direct).

### B4. Blockers

* `wait_for_cascade swallows step errors` (F3) is a real problem
  for any conditional repair loop — if the test-runner step raises,
  the loop never iterates and the cascade settles silently. Track B
  is blocked on the upstream `wait_for_cascade` fix OR an adapter-side
  step-error-detection workaround that promotes step errors to
  workflow errors.

---

## Track C — task-type routing (the productionization step)

### C1. Hypothesis

The composer is most useful when it picks the right scaffold for the
task automatically. A simple keyword-based router covers most cases:

| Prompt contains | Route to |
|---|---|
| `BaseStep`, `Workflow`, `Tool`, `ToolBase`, `WorkflowBuilder` | rules-v2 + (review-revise or direct) |
| algorithmic phrases ("function that returns", "given X compute Y") | plan-then-code |
| both / neither | plan-then-code with optional rules |

### C2. Scaffold shape

`BenchmarkTaskRouterStep` — a thin LLM-free step that inspects
`code_spec` for framework keywords and emits a routing decision. The
workflow uses `ConditionalLink` predicates on the router's output to
select the downstream scaffold.

### C3. Acceptance gate

* The router's choices match a manual classification of all 95
  problems (MBPP 50 + SciCode val 35 + nanobrain-native 10) with
  ≥ 90% accuracy.
* End-to-end sweep across the 3 benchmarks shows ≥ the per-benchmark
  best from the scaffold matrix (i.e., the router never picks a
  worse scaffold than the optimum for that task class).

### C4. Cost-benefit

* Zero LLM-call cost for routing (keyword match).
* Adds 2 conditional links and 1 router step to every codegen sweep.
* Worth shipping once Track A + Track B have measured the per-task
  ceilings.

---

## Track D — expand benchmark coverage

### D1. Hypothesis

The current 4-benchmark matrix (MBPP, SciCode val, nanobrain-native,
+ cross-task slots) is enough for first findings but too narrow for
scaffold-vs-scaffold comparisons. Specifically:

* MBPP heavily favors plan-then-code; doesn't measure framework
  competency.
* SciCode is the canonical "scientific Python" benchmark but only
  the validation split is usable without the gated `test_data.h5`.
* Nanobrain-native at n=10 has ±10pp noise per cell; the 30pp lift
  from rules-v2 is robust but smaller deltas (5-10pp) need n>20.

### D2. Additions

1. **DS-1000 loader** — closest to scientific-pipeline use case.
   ~1000 problems, library-heavy. Plan said "MBPP-equivalent loader
   work."
2. **HumanEval+ loader** — 164 problems, well-trodden ground for
   calibrating scaffolds against published numbers.
3. **Nanobrain-native v1**: expand from 10 to 25 problems per the
   original CGU-P1-T5 target. Add categories the current v0 missed:
   3 conditional-routing problems (using ConditionalLink), 3 skeleton-
   based (G9), 3 multi-step workflows with custom data units. The
   harder problems will give scaffolds room to differentiate.

### D3. Acceptance gate

* Each new loader: direct mistral-nemo baseline within ±5pp of the
  published baseline number.
* Nanobrain-native v1: at least 5 of the 15 new problems pass under
  rules-v2 (otherwise the new problems are too hard and we re-tune).

---

## Sequencing

Track A is ready (authored this session). Run it next; iterate twice
on the reviewer prompt if needed.

Track D's nanobrain-native v1 (5-10 new harder problems) is the next
highest-value because the current 10 problems are saturated by
rules-v2; we cannot measure further scaffold improvements without
harder problems to differentiate on.

Track B and Track C are blocked on:
* Track B: F3 wait_for_cascade fix (framework PR, P6 territory) OR
  step-error-promotion in the adapter.
* Track C: needs Track A + Track B headline numbers to know what to
  route TO.

## Non-goals (this plan)

* Implementing all 8 scaffold-zoo patterns from the original plan
  (CGU-P5-T1). Most of them are not directly motivated by measured
  failure patterns. Add a pattern only when a measured failure
  pattern indicates it would help.
* SOTA-comparable on MBPP. Local 12B at 78% closes ~14pp of the gap
  to large-model SOTA (~88%); the next 5pp likely needs either a
  larger drafter OR retrieval-grounded codegen (which is its own
  Track E if we get there).
* Framework PRs for the three silent-failure shapes (F3). They're
  documented but require user approval to push. Adapter-side
  workarounds shipped instead.

## Brutal-truth open questions

* **Is the 70% saturation on nanobrain-native a true ceiling for
  mistral-nemo 12B?** Or are we 1 prompt iteration away from 80%?
  Track A's reviewer is the test — if review-revise stays at 70%,
  the ceiling is real and we need a bigger drafter, not more
  scaffolding.
* **Why do `builder` / `config` / `tool` fail deterministically?**
  Inspecting one specific candidate per problem would tell us
  whether the LLM is missing concept (needs better rules), context
  (needs in-prompt examples), or capacity (needs bigger model).
  Three hours of failure-mode analysis would inform the next
  iteration much more than another scaffold.
* **Cross-benchmark consistency**: rules-v2 helps nanobrain-native
  +30pp but hurts MBPP -8pp. Is there a positive-only rules variant
  that's neutral on MBPP AND helps nanobrain-native? That would
  ship as default-on. The current task-specific binding is fine
  but suboptimal.
