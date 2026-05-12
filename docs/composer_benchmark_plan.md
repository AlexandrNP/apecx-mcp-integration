# Composer Code-Generation Improvement Plan

**Branch**: feature work on `main` (composer hardening)
**Started**: 2026-05-12
**Authoritative spec**: this document; deviations recorded in commit
bodies.

## Brutal-truth framing

The user's ask, paraphrased: "make the composer produce SOTA-comparable
code by adding more scaffolds, more models, and benchmark-driven
iteration." This document deliberately reframes that into something
achievable:

- **SOTA-comparable on SciCode with mistral-nemo + nemotron-3-nano is
  not a deliverable.** GPT-4o gets ~25% on SciCode subproblems; Claude
  Sonnet 3.5 gets ~26%. With a 12B + 4B local stack we should aim for
  the 10–15% subproblem range as a stretch goal. Anything we ship in
  that band is honest, valuable, and reproducible — which is what
  "adoption-ready" actually requires.
- **The deliverable is the measurement infrastructure + iteration
  loop**, not a SOTA number. Each phase ships independently and
  produces a reproducible benchmark number. The improvement story is
  cumulative; the artifact is the harness.
- **No silent failures** — every benchmark task must (a) load via
  `Workflow.from_config`, (b) execute end-to-end against real data,
  (c) compare full output to ground truth. Substring matches and
  partial-credit shortcuts are forbidden in scoring.
- **De-prioritize code quality, prioritize correctness** — pass@1 is
  the only metric. No ruff/black/policy checks during benchmark scoring.

## Why this matters (the adoption argument)

If the composer can build a 2-step workflow with a `DirectLink` and
`auto_transfer: true` 70% of the time, scientists will use it.
If it succeeds 30% of the time and fails silently the other 70%
(workflow loads, cascade fires, no data flows because of the
`auto_transfer=False` default — gap G7), they will burn one session
on it, file an issue, and stop trusting the tool. Reliability is the
whole product. Correctness on a benchmark proxies for reliability in
the wild.

## Models

| Role | Model | Why |
|---|---|---|
| Drafter (default) | `mistral-nemo:latest` (12B) | Already locked in for T01 AC1 strict-YAML emission; long track record in this workspace. |
| Planner | `nemotron-3-nano:4b` | Has explicit thinking tokens. 30B-MoE variants were considered but `:30b-a3b-q4` doesn't have an Ollama manifest, and the plain `:30b` (~18 GB) would crowd the Mac alongside mistral-nemo. 4B planner + 12B drafter is the right pair for this hardware. |
| Reviewer | `nemotron-3-nano:4b` (shared with planner) | Small, fast, instruction-following — same model, different prompt + role. Saves a model slot in VRAM. |
| Fallback | `gemma4:latest` (8B) | Already pulled. Documented in CLAUDE.md as 2× worse on T01 AC1 than mistral-nemo — kept only as a model-pluggability smoke test. |

All four go through Ollama's OpenAI-compatible endpoint on
`http://localhost:11434/v1`. Selection is YAML-config-driven; no model
name is hardcoded.

## Phase 0 — Multi-model foundation

**Status**: in progress.

- Pull `nemotron-3-nano:4b` (DONE) and `:30b-a3b-q4` (in flight).
- Extend `ComposerConfig` to support a named-model dict keyed by role
  (`drafter`, `planner`, `reviewer`). Existing single-model config
  stays as a default to preserve T01 AC1.
- Add a model-role router on `Composer` that returns the right
  per-role chat-LLM handle.
- Acceptance: T01 AC1 strict path still passes; a new unit test
  verifies the router selects the right model per role.

## Phase 1 — Benchmark harness

The harness is the foundation. Everything else measures against it.

### P1a — Harness scaffold

`tests/benchmarks/` package containing:

- `BenchmarkProblem` dataclass: `id`, `prompt`, `tests`,
  `ground_truth`, `dataset_source`.
- `BenchmarkRunner`: takes a problem + a "code-gen function" (so we
  can plug in: direct LLM, plan-then-code, review-revise, self-test)
  and returns a `RunResult` with `generated_code`, `tests_passed`,
  `tests_failed`, `wall_time`.
- `BenchmarkScorer`: aggregates pass@1; emits per-problem and
  per-dataset summaries.
- Per-problem timeout (60s default); per-suite timeout.
- Execution sandbox — wire up the existing `docker_sandbox.py`
  scaffold rather than running benchmark code in-process. Real
  isolation, real reliability signal.

### P1b — MBPP loader + baseline

- MBPP via HuggingFace `datasets.load_dataset("mbpp", "sanitized")`.
- Run mistral-nemo direct as baseline. Document pass@1 in
  `docs/composer_baseline.md` honestly.
- Floor for "did the scaffolding help?" comparisons.

### P1c — SciCode loader + baseline

- SciCode is harder: each problem has subproblems with code-context
  dependencies. The loader has to thread the dependency graph.
- Baseline expectation: near-zero on full problems, single-digit
  percent on subproblems. Document anyway.

### P1d — Nanobrain-native benchmark

20–30 hand-crafted tasks that exercise framework competencies:

1. "Write a `BaseStep` subclass that returns `{output: input.upper()}`
   from `process`." (trivial)
2. "Build a 2-step workflow YAML with one `DirectLink` and
   `auto_transfer: true`." (load + run check)
3. "Build a 3-step branching workflow with `ConditionalLink`."
4. "Write a workflow that classifies a string then routes to one of
   two sub-workflows using the lightweight `WorkflowBuilder`."
5. "Subclass `StepConfig`, add a custom field, instantiate via
   `from_config`."
6. ... (escalating complexity to ~ "build a 5-step viral-immunology
   workflow")

Each task includes a verifier that runs the generated artifact and
compares against expected output. **No mocks** in verifiers — every
generated workflow runs against real (small) data.

This is the most direct measure of whether the composer actually
helps users in this codebase.

## Phase 2 — Scaffold patterns

Each scaffold is itself a nanobrain workflow.

### P2a — Plan-then-code

```
PlanStep (nemotron-3-nano:30b-a3b, thinking enabled)
    → produces a structured plan (steps, links, data units)
    → DirectLink auto_transfer:true
CodeEmitStep (mistral-nemo)
    → reads plan + user prompt
    → emits YAML + novel_python per existing system.md
```

The nemotron thinking-tokens output is consumed by the next step but
NOT shown to the human. Plan output is structured (JSON or YAML)
so the emit step doesn't have to parse free-form reasoning.

### P2b — Generate-review-revise loop

Reviewer infrastructure already exists (`composition/reviewer.py`,
`reviewer_system.md`). Wire it as a workflow:

```
CodeEmitStep
    → ReviewerStep (nemotron-3-nano:4b)
    → ConditionalLink (pass | needs_revision)
        pass → terminal
        needs_revision → RevisionStep → loop back to ReviewerStep
                         (max 2 iterations, then accept whatever we have)
```

The `max 2` is to prevent runaway loops on un-fixable code. After
2 revisions we accept and report "review flagged remaining issues"
to the benchmark scorer — that's a fail in the metric.

### P2c — Self-test loop

Highest-correctness-leverage scaffold:

```
CodeEmitStep emits BOTH the code AND its own pytest cases
    → ExecutorStep runs the tests in sandbox
    → ConditionalLink (all_pass | failures)
        all_pass → terminal
        failures → RepairStep (sees failure output, emits fix)
                   → loop back to ExecutorStep (max 3 iterations)
```

Critical: the tests the LLM emits are NOT the benchmark's hidden
tests. They're scratch tests. The benchmark hidden tests still run
at the end. This pattern teaches the composer to do its own
verification, which is the failure mode the user complained about.

## Phase 3 — Prompt engineering + guidance compaction

- Audit `system.md` against the 14 KB soft cap, 16 KB hard cap.
- Per-model adapters: nemotron has `<think>...</think>` tokens; we
  want to capture-and-discard them. Mistral-nemo doesn't think;
  the prompt should not encourage it to (waste of tokens).
- Few-shot examples: pick 3–5 known-passing MBPP solutions, format
  as in-context examples for nemotron-as-drafter when MBPP-class
  tasks come in. Use a router on the user prompt — if it looks like
  a Python-function task, prepend the few-shots; else don't.
- **Compact the 9 nanobrain skill files into a single LLM-facing
  reference under 4 KB.** The skills are human-facing — they have
  rationale, examples, mitigations. The LLM-facing version is rules
  only. Anti-sycophancy from the workspace `Rule-content asymmetry`
  rule (2026-05-12).

## Phase 4 — Sweep, document gap, iterate

- 4 scaffolds × 3 benchmarks = 12 cells. Run all. Pass@1 matrix.
- Compare to published SOTA where available:
  - MBPP: SOTA ~95%+ (large models, Claude Sonnet level)
  - SciCode subproblem: SOTA ~26% (Claude Sonnet 3.5), ~25% (GPT-4o)
  - Nanobrain-native: no SOTA, we're setting the benchmark
- Find the worst-performing scaffold × benchmark cell. Single
  iteration pass — usually a prompt fix or a scaffold tweak. Re-run.
  Honest delta in the doc.

## Non-goals (explicit)

These are NOT in scope, in case there's any doubt:

- Fine-tuning models. We use stock weights.
- Cloud LLMs. Local-only via Ollama.
- Beating SOTA. We measure honestly.
- Code-quality metrics (lint, format, type-check) as benchmark signal.
- Multi-language benchmarks. Python only.

## Risk register

| Risk | Detection signal | Mitigation |
|---|---|---|
| Nemotron 30B MoE OOM on user's Mac | `ollama pull` succeeds but `ollama run` returns 500 | Fall back to `nemotron-3-nano:4b` for the planner role. Document the swap in the config. |
| Scaffold workflows hit nanobrain silent-failure modes themselves | Workflow loads cleanly, benchmark shows 0% pass@1 across the board | Run each scaffold workflow's smoke test (single MBPP-trivial problem) BEFORE running full suite. |
| Pass@1 from one run is noise | Two consecutive runs differ by >5% on the same scaffold | Run pass@1 at N=3, report median + spread. |
| Sandbox flakiness | `docker_sandbox.py` returns non-zero on known-good code | Per-problem timeout + retry-once policy; if retry also fails, count as failure (don't paper over). |

## Definition of done (this multi-session effort)

1. Harness ships, with documented pass@1 baselines for mistral-nemo
   direct on MBPP, SciCode, nanobrain-native.
2. At least one scaffold (plan-then-code, review-revise, or
   self-test) shows a measurable, non-noise improvement over the
   baseline on at least one benchmark.
3. The improvement is reproducible: re-run produces the same number
   ± 5%.
4. A single document — this one — records the gap to SOTA honestly
   and the iteration path to close it.

Anything beyond this is bonus.
