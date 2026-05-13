# Scaffold Designs — per-benchmark, multi-option

**Status**: 2026-05-13. Draft. Iterating with measured results.
**Predecessors**: `composer_codegen_uplift_findings.md` (F1–F13),
`composer_scaffold_improvement_plan.md` (Tracks A–D).

The lesson from F1, F8, F10–F12: scaffold-task fit dominates
scaffold depth. The same scaffold that lifts MBPP +14pp can
regress nanobrain-native -30pp. So this doc is structured by
benchmark family. Each family has 2-3 candidate scaffold designs;
the implementation order is "highest predicted lift first, with
the deterministic-friendly option first when both are equally
viable."

---

## Nanobrain-native — test-driven, deterministic-friendly

The framework's invariants ARE the spec. We can write deterministic
runtime checks that are *correct by construction*: try to load the
candidate via `from_config`, try to invoke `process({})`, observe
the exception class. No LLM judgement needed for these checks.

### NN-1. Runtime Framework Compliance Runner (highest priority)

**Architecture**:

```
drafter (LLM, rules)
  → FrameworkComplianceRunnerStep (deterministic; subprocess exec)
       → DirectLink → workflow_output                 [always]
       → ConditionalLink (decision == fix) → reviser  [conditional]
            → DirectLink → workflow_output             [overwrite]
```

The validator subprocess imports the candidate's module, finds every
class inheriting `BaseStep` / `ToolBase` / `Workflow`, attempts
`from_config(<temp_yaml>)`, then attempts `await instance.process({})`
or `await instance.execute({})` with empty input. The exception
class + message is captured.

Failure shapes caught (that AST validator misses):

* `RuntimeError: Direct instantiation prohibited` — fires when
  the candidate has a subtle `from_config` override the AST validator
  didn't recognize.
* `ComponentConfigurationError: FAIL-FAST: process() override required`
  — fires when the candidate defines `execute()` AND `process()` but
  the class signature is wrong.
* `pydantic.ValidationError: extra_forbidden` — fires when the
  candidate's `StepConfig` subclass uses `extra='forbid'` but
  doesn't strip the framework's `class:` injection.
* `AttributeError: 'NoneType' has no attribute X` — fires when
  `process()` accesses a key the framework wraps differently.

**Why this is the right next step**: the 3 deterministic
nanobrain-native failures under AST-gated (builder, config, tool)
all produce non-empty revised code (693, 915, 557 chars) but still
fail the benchmark. The AST validator says PASS (no shape violation)
or critiques generically (e.g., "import not in whitelist"). The
runtime runner would catch their actual failure modes by ACTUALLY
TRYING TO INSTANTIATE them, surfacing the real error message to
the reviser.

**Predicted lift**: +10-20pp on nanobrain-native (1-2 of the 3 hard
problems recover; the third may need behavior-specific guidance).

**Cost**: ~5s extra per problem (subprocess + import + from_config).

### NN-2. Framework-invariant test bank (medium priority)

Pre-author a YAML test bank per problem class. For step-authoring
problems, the bank has tests like:

```yaml
- name: from_config_loads
  code: |
    BaseStep.from_config({{class_yaml}})
- name: process_returns_dict
  code: |
    out = await step.process({"text": "x"})
    assert isinstance(out, dict)
```

A test-bank-runner step parametrizes `{{class_yaml}}` per problem
and runs each test. Output: `{decision, critique, failing_tests}`.

**Why not first**: requires authoring + maintaining the test bank;
NN-1 dynamically derives the same checks from the candidate's AST
+ exception classes without manual curation.

### NN-3. Symbolic execution + property-based check (lowest priority)

Use `hypothesis` to generate random inputs to `process()` and check
framework invariants (return type, no side effects on framework
data units, etc.). Powerful but heavyweight; the framework doesn't
yet have hypothesis as a dependency, and the failure shapes are
mostly caught by NN-1.

---

## MBPP — LLM-heavy, multi-agent

MBPP problems are short algorithmic functions ("write a function
that returns the nth prime"). The model needs to think about
edge cases (empty input, negative numbers, very large inputs).
A multi-agent scaffold where one agent thinks about edge cases
and another writes code constrained to handle them is the
canonical pattern.

### MB-1. Edge-case agent + test writer + repair loop (highest priority)

**Architecture**:

```
drafter (LLM, no rules — MBPP doesn't need them)
edge_case_agent (LLM): reads problem, emits {edge_cases: [str]}
test_writer (LLM): reads problem + edge_cases, emits {test_code: pytest str}
sandbox_runner (deterministic): runs test_code against drafter's code_source
   → DirectLink → workflow_output            [always]
   → ConditionalLink (decision == fail) → repair_drafter
        → DirectLink → workflow_output       [overwrite]
```

The crucial piece: the test_writer emits LLM-DRAFTED tests (NOT the
benchmark's hidden tests). The repair_drafter sees the actual test
failure traceback (`AssertionError: expected 5, got 4`) and revises.

Two LLM calls in the happy path (drafter + edge_case_agent +
test_writer + sandbox; if pass, done). Three calls in the unhappy
path (+ repair_drafter).

**Predicted lift**: +3-7pp on MBPP. Catches the 11/50 fail_assertion
problems that plan-then-code v2 leaves on the table.

**Cost**: ~3x wall time vs plan-then-code v2.

**Risks**:
* `wait_for_cascade` step-error swallowing (F3) bites the
  ConditionalLink repair branch — already worked around in the
  adapter (cache-invalidate on cascade drain False).
* Test writer hallucinates wrong tests — well-emitted tests should
  catch most off-by-one bugs; wrong tests waste a repair iteration.

### MB-2. Hierarchical task decomposition (medium priority)

For multi-step problems (rarer in MBPP, common in DS-1000):

```
planner: decomposes into 2-3 sub-tasks
for each sub-task:
  sub_drafter emits sub_code
assembler: stitches sub_codes together
sandbox_runner: runs the stitched code
```

**Why not first**: MBPP problems are mostly single-function, so
decomposition adds latency without lift. Reserve for DS-1000 /
BigCodeBench when we add those benchmarks.

### MB-3. Shared pitfall memory + drafter

Maintain a memory data unit with common MBPP pitfalls (off-by-one,
mutable defaults, integer division semantics). A pre-drafter step
matches the problem statement against the memory keys and prepends
relevant pitfalls to the drafter's prompt.

**Why not first**: MB-1's test_writer already gives the model
explicit edge-case guidance. The memory adds value mainly when the
edge cases are subtle and unmatched by the test_writer.

---

## SciCode — LLM-heavy, domain-knowledge-bound

SciCode failures are dominated by:
* Hallucinated scipy/numpy APIs (e.g., `scipy.linalg.tensor` doesn't
  exist).
* Shape mismatches in numpy operations.
* Subproblem dependency confusion.

### SC-1. Domain-knowledge memory + drafter (highest priority)

A pre-drafter step looks up the problem's `required_dependencies`
(SciCode field listing `import numpy as np`, `from scipy.special
import erfc`, etc.) and prepends a small "valid functions in these
modules" cheatsheet to the drafter's prompt. The cheatsheet is
deterministically generated from the import statement by introspecting
the imported module (using stdlib inspect).

**Architecture**:

```
deps_cheatsheet_step (deterministic): reads problem.setup_code,
  imports each module, lists valid public names.
drafter (LLM, rules + cheatsheet appended)
sandbox_runner (deterministic): runs the problem's actual tests
   → DirectLink → workflow_output
   → ConditionalLink (fail) → reviser → workflow_output
```

The cheatsheet keeps the LLM from inventing `scipy.linalg.tensor`
or `np.array_equal_nan` — it sees the real exported names.

**Predicted lift**: +5-10pp on SciCode validation. Specifically
catches the "hallucinated API" failure pattern from the F1 analysis.

**Cost**: cheatsheet step is ~100ms (deterministic introspection);
zero LLM cost beyond drafter + reviser.

### SC-2. Subproblem-aware drafter

SciCode's prior_code chain means subproblem N depends on the gold
solutions to 1..N-1. A scaffold that EXPLICITLY tells the LLM "your
solution will be called by subproblem N+1 which expects ..." might
help.

**Why not first**: requires SciCode-specific test-graph parsing;
benefit is constrained to multi-subproblem main-problems.

### SC-3. Test-driven (mirror of MB-1)

Edge case agent + test writer + repair loop, same as MB-1 but with
scientific edge cases (NaN, very small values, dtype coercion).

**Why not first**: SciCode tests reference `target` which the
scaffold's test_writer doesn't have access to. The framework would
have to materialize `target` deterministically (which we already
do at loader time) but the scaffold's intermediate tests can't
reuse it without leakage.

---

## Implementation order

1. **NN-1: FrameworkComplianceRunnerStep** for nanobrain-native.
2. **MB-1: EdgeCase + TestWriter + Repair** for MBPP.
3. **SC-1: Deps cheatsheet** for SciCode.

Each is independently measurable; chain results inform whether to
add the medium-priority alternatives.

---

## Update after NN-1 + F14 — SC-1 likely diminishing returns

NN-1 (runtime compliance) on nanobrain-native produced byte-for-byte
identical revised code as the AST validator on the 3 hard problems
— F14: the 70% ceiling is model-bound, not scaffold-bound. The
analogous prediction for SC-1 (deps cheatsheet) on SciCode:

* SciCode val failure breakdown (n=35): ~50% AssertionError
  (correct shape, wrong values — scaffolds don't fix these);
  ~30% numpy/scipy errors; ~20% other.
* SC-1 targets the hallucinated-API subset of the ~30%
  numpy/scipy bucket. Upper bound ~3pp absolute lift on n=35
  (±9pp noise band) → within noise.
* F14 strongly implies the reviser would IGNORE the cheatsheet
  for AssertionError problems regardless.

**Recommendation**: defer SC-1. The honest next plays are:

1. Expand nanobrain-native from 10 → 20-25 problems with harder
   categories so scaffolds can differentiate.
2. N=3 on MBPP plan-then-code to firm up 78%.
3. Test-driven scaffold v2 with problem-specific tests in
   `meta.yml` (NOT the hidden `test_code`). Tests whether
   executable feedback breaks F14's "model-bound" claim.
4. Bigger drafter experiment (`nemotron-3-nano:30b-a3b` or
   similar) to validate F14 against a larger model.

---

## Brutal-truth open questions

* Will NN-1 actually fix the 3 hard nanobrain-native problems? The
  AST-gated reviser saw the AST critique and STILL didn't fix them.
  Adding a runtime traceback may not be enough — the issue may be
  the LLM lacking semantic understanding, not framework-shape
  detection. Realistic outcome: 1-of-3 recovers, not all 3.
* MB-1's test_writer is itself an LLM — does it produce tests that
  exercise the right edge cases? If the test_writer's tests are
  wrong, repair iterates against wrong tests, possibly making
  things worse.
* SC-1's cheatsheet is a static list of `dir(module)` outputs. The
  LLM may still pick the wrong function name from a valid list. The
  cheatsheet prevents hallucination but doesn't fix function
  misuse.
* **Composition with rules-v2** on nanobrain-native is already
  saturating step problems at 100%; NN-1 only adds value on the 3
  hard problems. Maximum theoretical lift: 30pp (10/10), realistic:
  10-20pp.
