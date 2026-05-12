# Composer Codegen Uplift — Detailed Plan

**Branch**: TBD (`uplift-codegen-scaffolds` recommended; one worktree).
**Started**: 2026-05-12
**Supersedes (extends)**: `docs/composer_benchmark_plan.md` (v1).
**Authoritative spec**: this document. Cite task IDs (`CGU-Px-Tn`) in
commits and PRs. Deviations recorded in commit bodies.

## 0. Framing — aspirational target, exploratory research question

The user's clarified ask, in their own words: SOTA-comparable is the
**aspiration**; the work is an exploration of *how much LLM-internal
planning and reasoning can be absorbed by external nanobrain
scaffolds*. The bar is high; we will explore as many scaffold
strategies as possible, create new ones, and measure the influence
of scaffold **depth × combination** on correctness. Nanobrain
framework expansion is permitted where it removes friction.

The grounding rules that bound the exploration:

1. **Aspiration vs. floor.** SOTA-comparable on MBPP (~85 %), SciCode
   subproblems (~25 %), DS-1000 (~50 %) is the **aspirational
   ceiling** — we will not pretend it is unreachable, but we will
   not pretend it is easy either. The **realistic floor** with
   trivial scaffolds (plan-then-code, review-revise) is mid-30s on
   MBPP-class tasks and mid-teens on SciCode subproblems. The
   research question is the **shape** of the curve between floor
   and ceiling as scaffold depth and composition grow. We report
   the curve honestly, not a single flattering point on it.
2. **Reproducibility is non-negotiable.** A reproducible 28 % is
   worth more than a one-shot 50 % we cannot recreate.
   Headline numbers come from N=3 runs, median + range reported.
3. **Nanobrain workflow structure wraps every codegen, not only
   the "scaffolded" ones.** Today
   `tests/benchmarks/codegen/{direct,plan_then_code}.py` are
   procedural Python that calls Ollama directly. The existing
   `plan_then_code.py:18-21` docstring concedes this. The new
   plan makes the workflow wrap a P1 blocking task. If it does
   not happen first, every later "the framework helped" claim
   is confounded — we would be measuring raw Ollama, not the
   composer system.
4. **No silent failures.** Every codegen must (a) load via
   `Workflow.from_config` (or `WorkflowBuilder.load()` for the
   lightweight path), (b) execute end-to-end through real
   triggers/links with `auto_transfer: true`, (c) produce a value
   that is scored against ground-truth tests — no partial credit,
   no substring matches, no "the output looked plausible".
5. **De-prioritize code quality, prioritize correctness.** Pass@1
   is the only metric in the headline matrix. No ruff/black/policy
   gates during scoring; those live in the composer's own
   `reviewer.py` path, which is orthogonal to benchmarking.
6. **Framework expansion is permitted, evidence-gated.** A
   benchmark failure traced to an LLM struggling against a
   framework workaround becomes a candidate
   `nanobrain_capability_gaps.md` entry. Gaps that recur ≥ 3×
   across the benchmark suite earn a framework PR.
7. **Multiple authoring paths exercised.** YAML +
   `Workflow.from_config`, lightweight `WorkflowBuilder`, and
   `Workflow.from_skeleton` (G9) must each appear as at least
   one scaffold in the sweep matrix. Behaviour differences
   between paths are first-class observations.

## 1. Why this matters (the adoption argument)

A composer that succeeds on 3 of 10 user prompts and *visibly* fails
on the other 7 is more useful than one that succeeds on 5 of 10 and
silently produces a workflow that loads, fires triggers, and never
transfers data (the dominant `auto_transfer=False` failure mode — gap
G7). Reliability + observable failure beats higher headline numbers
with silent failures. Pass@1 on these benchmarks proxies for
reliability in the wild.

## 2. Real state of the repo (2026-05-12)

What is **already shipped** (and therefore NOT a task in this plan):

- `tests/benchmarks/{runner.py,cli.py,sandbox.py,exclusions.py}` —
  130 + 178 + 163 + 60 LOC. Harness scaffold, in-process exec, CLI.
- `tests/benchmarks/datasets/mbpp.py` — MBPP loader via HF.
- `tests/benchmarks/codegen/{direct.py,plan_then_code.py}` — two
  procedural codegens.
- One real baseline:
  `tests/benchmarks/results/mbpp_baseline_mistral_nemo_n50.json`
  → 64 % pass@1, n=50, mistral-nemo:latest, direct.
- `composer_schemas.ModelRoleConfig` + `Composer.from_config`
  model-role plumbing (drafter / planner / reviewer), with a
  passing unit test (`tests/unit/test_composer_model_roles.py`).
- Scaffold building blocks as nanobrain steps:
  `composition/steps/{code_write_step,code_review_step,
  code_verification_step,code_reflection_step,
  code_with_tests_step,test_write_step,isolated_py_exec_step}.py`.
- Two skeleton YAMLs for scaffold workflows:
  `composition/skeletons/{code_write_and_review.yml,
  code_write_review_and_run.yml}`.
- `composition/docker_sandbox.py` scaffold (hardened command
  builder, refuses to run unless `APECX_T13B_SANDBOX_EXECUTE=1`).

What is **missing** and addressed by this plan:

- A nanobrain-workflow wrap of *every* benchmark codegen.
- SciCode, DS-1000, HumanEval+, BigCodeBench loaders.
- A nanobrain-native benchmark with 20–30 hand-crafted problems.
- Review-revise + self-test codegens as nanobrain workflows.
- LLM-facing guidance compaction (system.md + skill-condensate).
- Per-model prompt adapters (think-token stripping for nemotron,
  no-think suppression for mistral-nemo).
- Docker sandbox wired into the benchmark runner with a
  silent-failure regression test (real exec vs. mock).
- The sweep + honest gap report.

## 3. Models (no change from v1, repeated for one-stop reference)

| Role | Model | Notes |
|---|---|---|
| Drafter | `mistral-nemo:latest` (12B) | Workspace baseline; locked-in for T01 AC1. |
| Planner | `nemotron-3-nano:4b` | `<think>...</think>` tokens; stripped before reaching drafter. |
| Reviewer | `nemotron-3-nano:4b` (shared) | Same model, role-specific prompt. |
| Fallback / smoke | `gemma4:latest` | Documented as ~2× worse on T01 AC1. Kept only as a pluggability test target. |

All four reach Ollama at `http://localhost:11434/v1`. Selection is
YAML-config-driven. **No model name is hardcoded anywhere except
documented fallback constants in `tests/benchmarks/codegen/`.**

## 4. Phase plan (task IDs are stable; cite in commits)

Phases are sequential. Tasks **within** a phase may be parallel where
called out. Each task has: scope, specific actions, acceptance
criterion (AC). Wall-clock estimates are intentionally omitted (the
user has explicitly de-prioritized time).

---

### Phase 0 — Foundations (mostly already shipped)

#### CGU-P0-T1 — Pull and smoke-test all models
- **Action**: `ollama pull nemotron-3-nano:4b` (confirm cached
  weights), `ollama list | grep -E '(mistral-nemo|nemotron|gemma)'`.
  One-shot `ollama run nemotron-3-nano:4b "Reply with: OK"` to
  confirm the OpenAI-compatible endpoint returns 200.
- **AC**: a new test
  `tests/integration/test_ollama_model_inventory_against_ollama.py`
  iterates the three expected models, calls the chat endpoint, and
  asserts a non-empty response. Auto-skips when Ollama unreachable.

#### CGU-P0-T2 — Surface model-roles config to benchmark codegens
- **Action**: replace the env-var resolution in
  `tests/benchmarks/codegen/{direct.py,plan_then_code.py}` with a
  read from `ComposerConfig.model_roles` (lazy-loaded via
  `Composer.from_config`). The codegens stay procedural for now —
  T6 wraps them in workflows — but they read the same config
  source as production.
- **AC**: when `composer_config.yml` declares
  `model_roles.drafter.model: foo`, the direct codegen sends
  requests to `foo`. Verified by a unit test with a fake
  `llm_factory`.

---

### Phase 1 — Benchmark portfolio (the measurement infrastructure)

The end-state matrix is **codegen × benchmark**:

|                | MBPP | SciCode | HumanEval+ | DS-1000 | BigCodeBench | nanobrain-native |
|---|---|---|---|---|---|---|
| direct         | ✅ (64 %) | ❌ | ❌ | ❌ | ❌ | ❌ |
| plan-then-code | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| review-revise  | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| self-test      | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

Phase 1 only fills the **columns** (loaders + direct-baseline per
benchmark). Phase 2 fills the **rows** (scaffolds). Phase 4 fills
the rest of the matrix.

#### CGU-P1-T1 — SciCode loader + baseline (P1c carryover)
- **Why first**: the user prioritized SciCode. It is also the
  hardest loader (subproblem dependency graph).
- **Action**: add `tests/benchmarks/datasets/scicode.py` loading
  from the official SciCode v0/v1 release (Hugging Face mirror or
  the `scicode` GitHub release). Each problem includes ordered
  subproblems with `prior_code` context. The loader yields one
  `BenchmarkProblem` per subproblem; subproblem IDs encode parent.
- **Score policy**: subproblem pass@1 only (full-problem is
  reported as a derived %). Use the same `runner.run_one`.
- **AC**: `scripts/bench.sh --dataset scicode --codegen direct
  --limit 30` produces a results JSON with non-zero `total`,
  non-empty `status_histogram`. Baseline number written into
  `docs/composer_baseline.md`. Realistic floor: 5–10 % subproblem
  pass@1; if we land outside `[0%, 20%]`, debug the loader before
  trusting the number.

#### CGU-P1-T2 — HumanEval+ loader + baseline
- **Action**: add `tests/benchmarks/datasets/humaneval_plus.py`
  using the EvalPlus release (more rigorous tests than vanilla
  HumanEval). 164 problems, well-trodden ground.
- **AC**: direct-baseline number written into
  `composer_baseline.md`. Expected ~50 % for mistral-nemo (close
  to published ~52 %); if we land below 35 % the harness has a
  prompt-injection bug.

#### CGU-P1-T3 — DS-1000 loader + baseline (data-science, library-heavy)
- **Why**: of the public benchmarks, DS-1000 maps closest to the
  workspace's actual scientific use case (NumPy / pandas /
  scipy / sklearn). It is the benchmark where the nanobrain
  composer should plausibly outperform a raw LLM, because the
  workspace catalog already carries domain-specific steps.
- **Action**: `tests/benchmarks/datasets/ds1000.py`. Each problem
  is a single function/snippet against numerical-output equality
  checks; the scorer must call the EvalPlus-style
  `check_correctness` semantics (compare values, not strings).
  This may require a new `score_strategy` field on
  `BenchmarkProblem`.
- **AC**: direct-baseline written. Expected 15–25 % for
  mistral-nemo; below 10 % indicates a scorer bug.

#### CGU-P1-T4 — BigCodeBench loader + baseline (multi-library)
- **Action**: `tests/benchmarks/datasets/bigcodebench.py`. Use the
  `bigcode-project/bigcodebench` HF dataset, `Instruct` split.
- **AC**: direct-baseline written. Expected 10–20 % for
  mistral-nemo.

#### CGU-P1-T5 — Nanobrain-native benchmark
- **Why critical**: this is the only benchmark that measures
  *the actual product*. The other four are necessary external
  comparators. This one tells us whether the composer is helping
  on its real use case.
- **Action**: `tests/benchmarks/datasets/nanobrain_native.py`
  + a sibling `problems/` directory with 25 hand-crafted YAML
  problem files. Categories and exact counts:
  1. **Step authoring (5)** — write a `BaseStep` subclass that
     implements `process()` with a stated input → output shape.
     Verifier loads it via `from_config`, runs it on a fixed
     input, checks output equality.
  2. **YAML wiring (5)** — emit a workflow YAML with N steps
     and `DirectLink(auto_transfer=true)`. Verifier loads via
     `Workflow.from_config`, runs end-to-end, checks final
     output. **This is the G7 silent-failure regression**:
     if the generated YAML omits `auto_transfer: true`, the
     workflow loads and *appears* to run but produces no
     output. The verifier must distinguish "ran and produced
     nothing" from "ran and produced the right thing".
  3. **Conditional routing (3)** — workflow with a
     `ConditionalLink` that routes by a predicate.
  4. **Lightweight builder (4)** — Python program using
     `nanobrain.lightweight.WorkflowBuilder` that constructs
     and executes a workflow; verifier `exec`s it and inspects
     the result. Exercises the **third legit authoring path**
     the user called out.
  5. **Skeleton-based (3)** — emit `bindings` for an existing
     skeleton (`composition/skeletons/code_write_and_review.yml`
     etc.) and load via `Workflow.from_skeleton`.
  6. **Custom config field (2)** — subclass `StepConfig`, add a
     custom field, instantiate via `from_config`.
  7. **Tool calling (3)** — write a `Tool` subclass with an
     `async def execute`, register it on an `Agent`, agent
     answers a yes/no question that requires the tool.
- **Score policy**: real execution against real data subsets;
  no mocks. Each problem ships with a small fixture under
  `problems/<id>/fixture/`.
- **AC**: direct-baseline written. Expected floor: 30–45 %.
  Anything below 25 % means the composer is fighting framework
  gravity and we surface those as `nanobrain_capability_gaps.md`
  candidates (see CGU-P6).

#### CGU-P1-T6 — Workflow-wrap the existing codegens
- **Why critical**: makes the user's "use nanobrain workflow
  structure to solve all problems" directive real.
- **Action**: two new artifacts:
  - `composition/workflows/benchmark_direct_codegen/`
    (`workflow.yml` + per-step YAMLs). One step: a
    `CodeWriteStep` reading the problem from a `MemoryDataUnit`
    and emitting code to another `MemoryDataUnit`. `DirectLink`
    with `auto_transfer: true`.
  - `composition/workflows/benchmark_plan_then_code/`. Two-step:
    `PlanStep` → `CodeWriteStep`. `DirectLink` between them.
    Each step uses the model-role config from CGU-P0-T2.
- Wire a new `tests/benchmarks/codegen/nanobrain_workflow.py`
  that takes a workflow path + bindings, executes via
  `Workflow.from_config`, and returns the emitted code string.
  This becomes the **single** runner-side adapter for every
  workflow-based codegen.
- **AC**:
  - The wrapped direct codegen reproduces 64 % pass@1 on MBPP
    n=50 within ±5pp of the procedural baseline. Larger drift =
    bug in the wrap, not "the framework adding cost".
  - The wrapped plan-then-code lands within ±5pp of the
    procedural plan-then-code on the same sample (assuming the
    procedural version is run for the comparator number).

---

### Phase 2 — Scaffold patterns as nanobrain workflows

Every scaffold is a workflow YAML loaded via `Workflow.from_config`
(YAML path) or via `WorkflowBuilder` (lightweight path). Each
scaffold ships with a smoke test on one MBPP-trivial problem
**before** running the full suite — silent-failure guard.

#### CGU-P2-T1 — Review-revise scaffold (workflow)
- **Action**: `composition/workflows/benchmark_review_revise/`.
  Steps: `CodeWriteStep` → `CodeReviewStep` (uses
  `composer_prompts/reviewer_system.md`) →
  `ConditionalLink(pass | revise)`. The `revise` branch loops
  back to `CodeWriteStep` with the review feedback prepended to
  the user prompt, max 2 iterations. Iteration counter lives in
  a `MemoryDataUnit` on the workflow; `ConditionalLink` reads it.
- Reviewer model: `nemotron-3-nano:4b` (cheap, fast, instruction
  following).
- **AC**:
  - Smoke test on MBPP/11 passes.
  - Sweep on MBPP n=50 produces a non-zero `revise` count in
    the artifact metadata (otherwise the conditional link is
    dead and we silently fell back to direct).
  - Pass@1 ≥ baseline (a regression is a bug; an improvement of
    less than 3pp is within noise — do not over-claim).

#### CGU-P2-T2 — Self-test scaffold (workflow)
- **Action**: `composition/workflows/benchmark_self_test/`.
  Steps: `CodeWriteStep` (emits BOTH code AND its own pytest
  cases) → `IsolatedPyExecStep` (runs the *self*-tests in a
  subprocess) → `ConditionalLink(all_pass | failures)`. The
  `failures` branch routes to `CodeReflectionStep` (existing
  building block) which re-runs `CodeWriteStep` with failure
  output in context, max 3 iterations.
- **CRITICAL CLARIFICATION**: the self-tests are NOT the
  benchmark's hidden tests. The benchmark scorer is run by the
  **outer** harness on the workflow's final output. Confusing
  the two is the failure mode that turns into "100 % pass@1 on
  my fake tests, 12 % on the real ones".
- **AC**:
  - Smoke test confirms the loop iterates at least once on a
    deliberately-buggy seed problem.
  - Sweep on MBPP n=50 shows `repair_count` metadata >0 across
    a non-trivial fraction (otherwise the loop is dead).
  - Pass@1 should be the strongest scaffold; if it underperforms
    plan-then-code by >3pp, the self-test prompt is
    over-generating tests that pass trivially and never trigger
    the repair branch — fix the prompt before iterating.

#### CGU-P2-T3 — Lightweight scaffold (programmatic path)
- **Why**: the user explicitly asked us to exercise multiple
  legit authoring paths, including the lightweight builder.
- **Action**: re-implement the plan-then-code scaffold using
  `nanobrain.lightweight.WorkflowBuilder` in
  `composition/workflows/benchmark_plan_then_code_lightweight.py`.
  Same step classes, same prompts, different construction
  ergonomics. The benchmark adapter
  (`codegen/nanobrain_workflow.py`) must accept BOTH a YAML path
  and a Python builder callable.
- **AC**: produces identical pass@1 (±2pp) to the YAML version on
  MBPP n=50. Larger drift signals a behaviour difference between
  the two authoring paths — *that is a useful bug report* worth
  filing as a framework gap.

#### CGU-P2-T4 — Skeleton-based scaffold (G9 path)
- **Action**: take the existing
  `composition/skeletons/code_write_review_and_run.yml`, write a
  bindings template, load via `Workflow.from_skeleton`. Wire as
  the third codegen-route variant.
- **AC**: produces pass@1 numbers within ±2pp of the YAML scaffold
  in CGU-P2-T1 / T2 on MBPP n=50. Drift > 2pp = a difference
  between the skeleton-based and YAML-based authoring paths
  that should be tracked in `nanobrain_capability_gaps.md`.

---

### Phase 3 — LLM-facing guidance compaction

#### CGU-P3-T1 — Audit `composer_prompts/system.md` against caps
- **Action**: run `prompt_budget` regression test; if at or near
  the soft cap (14 KB), trim per the rule-content asymmetry
  rule from CLAUDE.md (LLM-facing prompts carry imperatives +
  remedies, NOT rationale). Move rationale to CLAUDE.md docs.
- **AC**: `system.md` ≤ 13.5 KB after the pass; T01 AC1 still
  passes.

#### CGU-P3-T2 — Per-model prompt adapters
- **Action**: in
  `composition/composer_prompts/_model_adapters.py` (new),
  build a `for_model(model_name: str) -> str` that selects a
  short suffix appended to `system.md`:
  - For `nemotron-*`: instruct "Use `<think>...</think>` for
    your scratchpad; the system will strip it before downstream
    consumption."
  - For `mistral-nemo:latest`: instruct "Do NOT include reasoning
    or `<think>` blocks. Emit only the requested artifact."
  - Default: no suffix.
- Wire through `Composer._build_system_prompt`.
- **AC**: a unit test confirms the right suffix is appended per
  model and the total stays under the hard cap.

#### CGU-P3-T3 — LLM-facing skill condensate
- **Action**: condense the nine `nanobrain-*/SKILL.md` files
  (human-facing) into a single
  `composition/composer_prompts/nanobrain_rules.md` that is
  LLM-facing rules only (imperatives + remedies + verbatim
  framework error messages). Target ≤ 4 KB. Prepended to the
  drafter's prompt when emitting workflow YAML or `BaseStep`
  Python.
- **AC**:
  - Compaction test fails the PR if `nanobrain_rules.md` exceeds
    4 KB.
  - At least 3 nanobrain-native benchmark problems improve by
    ≥ 5pp pass@1 with the condensate prepended versus without
    (otherwise the condensate is dead weight and we cut it).

#### CGU-P3-T4 — Few-shot exemplar bank
- **Action**: pick 5 known-passing MBPP solutions and 3 known-
  passing nanobrain-native problems; store as a YAML exemplar
  bank under `composition/composer_prompts/exemplars/`. Prepend
  conditionally on prompt shape (function-from-spec vs. workflow-
  YAML).
- **AC**: ≥ 3pp pass@1 improvement on MBPP n=50 for the
  exemplar-augmented direct codegen vs. plain direct codegen.

---

### Phase 4 — Sandbox + silent-failure guards

#### CGU-P4-T1 — Wire Docker sandbox into the benchmark runner
- **Action**: replace the in-process `sandbox.py` with a router
  that uses Docker (`composition/docker_sandbox.py`) when
  `APECX_T13B_SANDBOX_EXECUTE=1`, falls back to in-process
  otherwise, and **logs a warning** on the fallback. The
  in-process path stays for local dev iteration; the published
  baseline numbers are produced under Docker.
- **AC**:
  - With sandbox enabled, MBPP n=10 produces the same pass@1 as
    the in-process baseline (within ±5pp).
  - A test asserts that a deliberately-malicious snippet
    (`import socket; socket.socket().connect(("1.1.1.1", 80))`)
    fails under Docker (network is `--network=none`) and
    succeeds in-process.

#### CGU-P4-T2 — Silent-failure regression suite
- **Action**: add `tests/benchmarks/test_silent_failure_guards.py`
  with one assertion per known failure mode:
  - Empty generated string scored as fail (not pass).
  - `print("done")` with no return scored as fail.
  - Workflow YAML missing `auto_transfer: true` on a DirectLink
    must surface as a runtime warning AND as a benchmark fail
    (not a pass with empty data unit).
  - `IsolatedPyExecStep` timeout scored as fail, not pass.
  - Sandbox unavailable (Docker daemon down) must skip the test
    with a clear message, not score as pass.
- **AC**: all five cases assert failure outcomes in the
  benchmark scorer's bookkeeping.

---

### Phase 5 — Scaffold zoo + composition exploration (research core)

This is the central research phase. We do NOT stop at the four
baseline scaffolds in Phase 2. We treat scaffolds as composable
patterns and systematically measure (a) the marginal value of each
pattern alone, (b) the value of stacking patterns, (c) the
breaking point where stacking stops paying off (token cost,
latency, error compounding).

#### CGU-P5-T1 — Scaffold zoo: implement the remaining patterns

Each pattern is a nanobrain workflow (YAML, builder, or
skeleton). Add to the codegen registry:

| # | Pattern | Sketch | Authoring path |
|---|---|---|---|
| Z1 | **Decomposition** | Planner splits problem into ≥ 2 subproblems; drafter solves each; assembler concatenates / glues. | YAML |
| Z2 | **Test-driven (TDD)** | TestWriterStep emits tests FIRST; CodeWriterStep then writes code constrained to pass them; IsolatedPyExecStep verifies. | YAML |
| Z3 | **Reflection (single-turn self-critique)** | Single drafter call; same model re-reads its output, emits a critique; same model emits the revision in one extra round. | Lightweight builder |
| Z4 | **Debate / consensus** | Two drafters with different seeds (or different models — mistral-nemo vs. nemotron-4B); arbiter step (reviewer model) picks one or merges. | YAML |
| Z5 | **Tool-use mid-generation (ReAct-style)** | Drafter has access to a `PySandboxTool`; emits trial code, the sandbox executes, error feedback returns into the loop. Max 3 tool calls. | YAML (Agent + Tool) |
| Z6 | **Retrieval-grounded** | Before drafting, a retrieval step queries the component catalog + a few-shot exemplar bank (CGU-P3-T4) for problems similar to the prompt; results prepended to drafter context. | YAML; reuses existing RAG components. |
| Z7 | **Oracle-grounded** | After drafting, run `python -m py_compile`, `ruff --select=E`, `pyright --strict`. Feed each oracle's output back as feedback to a repair step. (NOTE: oracles are used as **signal**, not as scoring; pass@1 still comes only from the hidden tests.) | YAML |
| Z8 | **Symbolic-trace assist** | For algorithmic problems, planner emits a small input + expected output; drafter is constrained to dry-run its code on that input before returning. Detects most off-by-one classes. | Lightweight builder |

- **Action**: 8 workflow artifacts under
  `composition/workflows/benchmark_<pattern>/`. Each ships with
  a 1-problem smoke test on MBPP-trivial.
- **AC**: every pattern's smoke test passes. Patterns that fail
  smoke after 3 attempts at the prompt are documented as
  "did not converge" (negative results are valid) and skipped
  in the sweep — not retried indefinitely.

#### CGU-P5-T2 — Scaffold composition

Compose scaffolds into chains, each chain itself a nanobrain
workflow. The composition graph is `composer_prompts/system.md`-
shaped: a list of stages, each stage selecting a pattern.

| Chain ID | Composition | Hypothesis |
|---|---|---|
| C1 | plan-then-code → review-revise | Catches logic bugs the planner missed. |
| C2 | plan-then-code → self-test | Strongest expected baseline. |
| C3 | decomposition → self-test-per-subproblem → assembly | Targeted at SciCode (subproblem structure matches). |
| C4 | retrieval-grounded → plan-then-code → review-revise → self-test | "Full stack". Expected token cost ~ 5× direct; we measure whether the pass@1 lift justifies the cost. |
| C5 | TDD → self-test (loop on TDD's tests) | Constrained generation: tests are the spec. |
| C6 | debate (2 drafters) → consensus → review-revise | Tests whether disagreement signal helps. |
| C7 | oracle-grounded → tool-use mid-generation | Pure feedback-loop chain; no planner. |
| C8 | symbolic-trace assist → self-test | For algorithmic problems specifically. |

- **Action**: 8 composition workflows under
  `composition/workflows/benchmark_chain_<id>/`. Each composition
  smoke-tests on 1 MBPP-trivial AND 1 SciCode-subproblem
  before joining the sweep.
- **AC**: every composition's smoke test passes. Compositions
  where smoke either (a) fails after 3 honest attempts or
  (b) takes > 10× the wall time of the cheapest scaffold are
  flagged as "too expensive to sweep at n=50" and run only at
  n=20 in CGU-P5-T4.

#### CGU-P5-T3 — Depth × combination ablation matrix

The core scientific output. For each `(pattern, benchmark)`
cell, also record:

- **token cost** (sum of prompt + completion tokens across all
  stages, per problem)
- **latency** (wall time per problem, median)
- **iteration count** (how often the repair / revise / tool-use
  branch actually fired)
- **failure shape** (`fail_assertion`, `fail_syntax`,
  `fail_timeout`, `fail_other` histogram)

This gives us the **Pareto frontier**: which scaffolds give
the most pass@1 per token / per second. Adoption depends on
this frontier, not on the headline.

- **AC**: a CSV at
  `tests/benchmarks/results/ablation_<date>.csv` with one row
  per `(codegen × benchmark × problem)` triple. Plotted (matplotlib)
  as 2D Pareto scatter at `docs/figures/ablation_pareto_<date>.png`.

#### CGU-P5-T4 — Full sweep at n=50 + n=20-expensive

- **Action**: run every `(codegen, benchmark)` cell. Cheap cells
  at n=50; expensive compositions at n=20. N=3 repeats for the
  median + range column on every cell.
- **AC**: single Markdown matrix in `docs/composer_baseline.md`
  with median pass@1 ± range per cell.

#### CGU-P5-T5 — Gap-vs-SOTA narrative + breaking points

- **Action**: write a `gap_vs_sota` section in
  `composer_baseline.md` comparing each row to the published
  large-model numbers. Identify and document:
  - The **best chain** and which patterns contributed most
    (cross-reference the ablation matrix).
  - The **breaking point**: where adding the next stage stopped
    helping or hurt (token cost vs. pass@1 lift).
  - The **honest gap** to SOTA per benchmark.
  - **Per-benchmark surprise table**: cells where we are closer
    or further from SOTA than the floor expectation. Each
    flagged with a 1-line hypothesis.
- **AC**: at least 3 cells flagged with hypothesis; best chain
  + breaking point named explicitly.

#### CGU-P5-T6 — Single targeted iteration on the highest-leverage cell

- **Action**: pick the `(chain, benchmark)` cell that is closest
  to SOTA AND has plausible headroom (e.g. the next pattern in
  the chain has been measured to help on a sibling benchmark).
  Form ONE hypothesis. Re-run at n=50. Record honest delta.
- **AC**: a single commit that names the cell, the hypothesis,
  the before/after numbers, and whether the hypothesis was
  confirmed. Negative results are valid acceptance.

---

### Phase 6 — Framework capacity expansion (conditional)

Only entered if Phase 1–5 surface a genuine framework gap, NOT to
land speculative features.

#### CGU-P6-T1 — Triage gaps found during P1–P5
- **Action**: every benchmark failure that traces to a framework
  workaround (LLM had to emit an awkward construct to satisfy a
  silent-failure guard) is logged as a candidate gap in
  `docs/nanobrain_capability_gaps.md` (the existing G1–G45 file).
  Pick the top 1–2 by frequency of occurrence in the failure
  population.
- **AC**: 1–2 new gap entries with `frequency`, `workaround`,
  `proposal`.

#### CGU-P6-T2 — Ship a single framework PR (only if a gap clears the bar)
- **Bar**: gap must surface in ≥ 5 benchmark problems AND have a
  workaround LLMs reliably get wrong.
- **Action**: PR against the nanobrain source repo (read-mostly
  per workspace policy — requires explicit user approval before
  pushing).
- **AC**: gap entry status flipped to `shipped`; re-run the
  sweep matrix and document the delta.

---

## 5. Risk register (extends v1)

| Risk | Detection signal | Mitigation |
|---|---|---|
| Wrapping codegens in workflows adds latency that masks the scaffold's quality win | Workflow-wrap MBPP wall time > 2× procedural baseline | Profile; if framework overhead is the cause, file a gap. If not, document the cost and proceed. |
| Self-test scaffold's emitted tests pass trivially and never trigger the repair branch | Repair-count metadata is zero across the suite | Inspect 5 generated test files manually; harden the test-emission prompt to require ≥ 1 edge case. |
| Nanobrain-native benchmark verifiers themselves are buggy | Direct codegen pass@1 = 100 % or 0 % | Manually verify 3 random problems pass and 3 fail by inspection before trusting the number. |
| Docker sandbox flakes on macOS arm64 (Docker Desktop quirks) | Sandbox returns 137 / 125 on known-good code | Per-problem retry-once, count second failure as honest fail (do not retry until pass). |
| Pass@1 from one run is noise | Two runs on the same scaffold differ by > 5pp | Run pass@1 at N=3 for the headline matrix; report median + range. |
| LLM-facing skill condensate over-trims and hurts performance | nanobrain-native pass@1 drops > 3pp after condensate is added | Roll back to the longer SKILL.md content for the drafter prompt; document the floor. |
| Composer prompts grow as the user iterates and quietly push past 14 KB | Soft-cap regression test starts warning | Treat the warning as a P3 task trigger, not a low-priority log line. |
| Benchmarks with library imports (DS-1000, BigCodeBench) hit missing-package errors in the sandbox | `ModuleNotFoundError` clusters in failure histogram | Build a per-benchmark image with the dataset's `requirements.txt` pinned. Update gap entry if a benchmark needs > 30 packages. |

## 6. Definition of done (this multi-session effort)

1. The sweep matrix is filled across **all 12 codegens** (direct +
   plan-then-code + review-revise + self-test + skeleton +
   lightweight + 8 zoo patterns) **× all 6 benchmarks** (MBPP,
   SciCode, HumanEval+, DS-1000, BigCodeBench, nanobrain-native),
   with median + range over N=3 runs per cell. Cells skipped for
   smoke-failure or wall-time reasons are explicitly marked.
2. The depth × combination ablation matrix (CGU-P5-T3) is published
   with a Pareto frontier plot.
3. At least one scaffold composition shows a measurable, non-noise
   (≥ 5pp, median over N=3) improvement over the direct baseline on
   at least 2 benchmarks. Compositions that DO NOT help (cost more,
   gain less) are reported in the matrix — that is also useful
   science.
4. Every codegen is a nanobrain workflow (YAML, lightweight builder,
   or skeleton). Each has a smoke-test entry that runs before
   joining the sweep. Multiple authoring paths are exercised.
5. The silent-failure regression suite (CGU-P4-T2) covers the five
   named failure modes and is green.
6. `docs/composer_baseline.md` records the gap to published SOTA
   per cell, names the **best chain**, names the **breaking point**
   where stacking stopped paying off, and explains the **shape of
   the depth × pass@1 curve**.
7. `nanobrain_capability_gaps.md` carries any genuine framework
   gaps surfaced by the work, with frequency evidence (≥ 3
   occurrences across the suite).
8. LLM-facing guidance is compact: `system.md` ≤ 13.5 KB,
   `nanobrain_rules.md` ≤ 4 KB, per-model prompt adapters
   wired and tested.

## 7. Non-goals (explicit)

- **Fine-tuning models.** Stock weights only.
- **Cloud LLMs.** Local-only via Ollama.
- **Cherry-picking favorable subsets to inflate the headline
  number.** Sweep is on the full or random-seed sample; no
  selecting "easy" problems retroactively.
- **Code-quality lint/format/type-check as pass@1 signal.** Oracles
  feed scaffold loops; they do NOT score.
- **Multi-language.** Python only.
- **Modifying sibling repos (nanobrain, apecx-mcp, apecx-rag)
  without explicit user approval.** Framework changes go through
  P6 with the user reviewing the proposed PR before publishing.

## 8. Working-rhythm rules (lessons from prior sessions)

- One worktree per phase boundary. Branch name: `cgu-p<n>-...`.
- Commit at every CGU task close. Cite the task ID in the body.
  Never let one commit straddle two tasks.
- Run the canonical runner (`scripts/run_tests.sh`) before
  declaring a task done; do NOT cargo-cult `--ignore=` flags.
- Re-read `system.md` after every prompt edit; the soft-cap
  warning is not a notification you can defer.
- Before running a full sweep, run the smoke test for that
  scaffold on a single MBPP-trivial problem. The 30s smoke catches
  silent-failure bugs that would waste hours of the sweep.
- Three-attempt cap applies per failing operation. If the third
  attempt to fix the same scaffold still fails, stop and report
  with what was tried + evidence needed to proceed.
