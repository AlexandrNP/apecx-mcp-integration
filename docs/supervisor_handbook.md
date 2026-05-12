# apecx composer — supervisor handbook

**SUPERVISOR HANDBOOK** (marker pinned by
`tests/unit/test_supervisor_handbook_pinned.py`).

You are the supervisor of the apecx composer (the LLM-backed workflow
authoring system in `composition/composer.py`). The composer is
mistral-nemo (12B local Ollama) by default; production deployments
may swap models. Your job is not to write code FOR the model —
it's to catch the model's drift patterns, enforce the framework's
silent-failure gates, and keep the library's reuse surface trustworthy.

This handbook captures patterns observed in 2026-04 through
2026-05-12 supervision sessions plus the gates that came out of
them. It is intentionally a **scannable reference**, not a tutorial.
Grep for the section heading that matches your problem.

---

## Scope — what supervision IS and IS NOT

**IS:**

- Watching the model's authoring output and catching drift that
  the existing gates do not catch (then either patching the gate
  or proposing a new one).
- Running the **canonical adoption-grade integration tests**
  (`test_t01_ac1_against_ollama.py`, `test_code_writing_against_ollama.py`,
  `test_bug_fix_and_documentation_against_ollama.py`) at every
  prompt-rule rollout and reading the result before committing.
- Reading `CompositionSummary.reuse_ratio` and
  `compose_retries` after a composer run to spot quality drift
  before it shows up as a user-visible failure.
- Routing genuinely-new work through the existing framework
  primitives instead of letting the model invent shadow primitives
  (the **CLOSED-CLASS RULE** + **REUSE-FIRST RULE**).
- Session-end distillation per the workspace CLAUDE.md policy.

**IS NOT:**

- Writing the model's code for it. If you find yourself authoring
  a function the model could have authored, you have crossed into
  the model's lane — back out.
- Approving novel_python by hand because "it looks right". Every
  novel_python block goes through the HITL gate at the approval UI
  AND (when `APECX_T13B_SANDBOX_EXECUTE=1`) the Docker sandbox.
  Bypassing those is unauthorized.
- Pushing to remote without explicit user approval (workspace
  CLAUDE.md "Pushes to remotes require explicit user approval").
- Editing shipped library classes to make a workflow pass. That
  is the closed-class violation the rule exists to prevent.

---

## Day-one checklist (do before any composer-related edit)

Run in order. Stop at the first failure and surface it.

1. **Read context.**
   ```bash
   cat CLAUDE.md                                    # repo-local rules
   cat ../CLAUDE.md                                 # workspace rules
   cat memory/MEMORY.md                             # auto-memory pointers
   ```

2. **Verify the venv is the authoritative Python.**
   ```bash
   .venv/bin/python -c "import apecx_integration, nanobrain; print('OK')"
   ```
   If this raises `ModuleNotFoundError`, you are on the wrong
   Python — see CLAUDE.md "Python interpreter — MUST use the venv".
   Do NOT add `--ignore=` to pytest until the venv resolves the imports.

3. **Run the full unit suite.**
   ```bash
   PYTHONPATH=src .venv/bin/python -m pytest tests/unit -q
   ```
   Expected: 836+ passing, 0 failed, 33 warnings (pydantic
   serializer warnings on `WorkflowConfig.validate_graph` are
   known; ignore). Regression vs. this baseline = stop and
   investigate before editing.

4. **Probe Ollama reachability.**
   ```bash
   curl -s http://localhost:11434/api/tags | head -1
   ```
   If unreachable, the integration tests will auto-skip. That is
   acceptable for unit-level work; it is NOT acceptable for
   prompt-level work — every prompt change needs a real-Ollama AC1.

5. **Run the canonical AC.**
   ```bash
   APECX_LLM_MODEL=mistral-nemo:latest PYTHONPATH=src \
     .venv/bin/python -m pytest \
     tests/integration/test_t01_ac1_against_ollama.py -q
   ```
   Expected: 1 passed in ~45-50s. This is the **load-bearing
   composer test** (repo CLAUDE.md "Composer prompt is
   load-bearing"). If AC1 flaps:
   - First suspect: `composer_prompts/system.md` (check git log).
   - Do NOT blame the LLM/executor without that check.

---

## Drift patterns observed (with detection signals)

Patterns the model exhibits, what the gate catches them as, and how
to investigate when a gate does NOT catch one.

### D1 — Prose prefix on code output

**Behavior:** mistral-nemo prefixes its code with "Here's the bug
analysis:..." or "The corrected version is:..." despite the
system-prompt rule forbidding prose.

**Detection signal:** AST gate raises with "unterminated string
literal" or "invalid syntax" on line 1 of the model's output. The
raw output starts with English, not `def` / `class` / `import` / `from` / `@`.

**Gate:** `CodeWriteStep._strip_leading_prose` (composition/steps/code_write_step.py).
Single-pass recovery — scans for the first line starting with a Python
keyword and drops everything before it. Idempotent for clean output.

**Pinned by:** `tests/unit/test_code_write_step.py::test_leading_prose_paragraph_is_stripped_to_recover_valid_code`.

**If the gate doesn't catch it:** the model added markdown headers
(`## Fix`) or non-code blocks that interleave with the code. Extend
the regex in `_strip_leading_prose` to handle the new shape —
SINGLE PASS (per the framework's drift-masking discipline). Multiple
strip passes mask real drift.

### D2 — Code fences in the output

**Behavior:** model wraps code in ` ```python ... ``` ` despite the
"no markdown fences" rule.

**Detection signal:** raw output starts with ` ``` `. AST parse
fails on the first ` ``` ` line.

**Gate:** `CodeWriteStep._strip_fences` runs BEFORE prose-strip and
BEFORE the AST gate. Strip-once-and-reparse — do not try a second
strip if the result still doesn't parse.

### D3 — Wrong function name

**Behavior:** model authors `def my_function(...)` when the prompt
required `def expected_function_name(...)`.

**Detection signal:** AST passes, but `function_name_verified` is
not the requested name.

**Gate:** `CodeWriteStep._validate_ast` checks `expected_function_name`
against module-scope function definitions. Raises `ValueError`.

**Pinned by:** `test_wrong_function_name_raises`.

### D4 — Vacuous bug-fix (model writes correct code despite "introduce a bug" instruction)

**Behavior:** mistral-nemo writes correct fizzbuzz / fibonacci /
factorial on ~50% of prompts asking it to introduce a deliberate
bug. Some specs land in a "well-trained" region of the model's
weights where it cannot follow the bug-injection instruction.

**Detection signal:** the broken-code `IsolatedPyExecStep` returns
`exec_succeeded=True` — i.e., the "broken" code passes the test.

**Gate:** the integration test `pytest.skip`s with reason
"Ollama ignored the 'introduce a bug' instruction" rather than
failing. This is **observed behavior, not a regression** — measurement,
not bug.

**Do not** "fix" this by prompting harder. The model's well-trained
region is not pliable to a single prompt; force-injecting bugs would
require a fine-tune. Skip + log is the right behavior.

### D5 — Trigger-binding gap (DirectLink with auto_transfer:False)

**Behavior:** workflow YAML loads cleanly, all steps initialize,
trigger cascade fires, every `process()` runs — but no data ever
transfers between steps. No exception.

**Detection signal:** a workflow with N steps shows N-1 steps
producing output but the last step's output data unit is empty;
runtime is suspiciously short (no real LLM call happened).

**Gate:** the workspace pre-commit hook `Workflow YAMLs are v2 +
every DirectLink has auto_transfer (G39)` rejects commits with
DirectLinks missing `auto_transfer: true`. The cross-cutting silent-
failure awareness section of workspace CLAUDE.md carries the full
warning.

**Investigation grep:**
```bash
grep -A 2 'class:.*DirectLink' <your-workflow.yml> | grep -c 'auto_transfer: true'
# count must equal the number of DirectLinks
```

### D6 — Nested-cascade hang (now resolved, watch for regression)

**Behavior:** outer workflow with `SubworkflowStep` instances chained
hangs indefinitely. Inner cascade fires but data shape mismatch
trap re-routes output through a wrapper key.

**Detection signal:** outer workflow LLM test exceeds 60s wall;
inner subworkflow logs show data unit values that are dicts with a
single `"code_source"` key wrapping the actual code.

**Gate:** `_collect_last_step_outputs` flattens single-workflow-output
dicts (nanobrain commit 9e2d55f) + workflows in
`composition/workflows/code_writing/` use single-output topology
(`reflection_result`, `documentation_result`, `fix_result`).

**Pinned by:** `tests/integration/test_outer_workflow_stub_llm.py`
+ topology pins in `tests/unit/test_compositional_structure.py`.

**If it regresses:** check that the workflow YAML's `output_data_units:`
block has exactly ONE entry, not N parallel entries.

### D7 — Composer hallucinates inline `config: {...}`

**Behavior:** composer emits a step block with `config:` as an
inline dict instead of a path string.

**Detection signal:** `nanobrain.core.config.config_base` raises
`❌ FRAMEWORK VIOLATION: Inline dict configuration not supported`.

**Gate:** composer pre-execution validator surfaces this BEFORE
runtime as structured retry feedback. The system prompt has the
exact error string in it so the LLM recognizes its own violation.

**Investigation:** if the LLM keeps emitting inline dicts despite
the rule, check whether the `composer_prompts/system.md` size has
grown past ~13.5 KB — past that threshold mistral-nemo starts
dropping later-prompt instructions.

### D8 — Composer hallucinates class-path suffixes

**Behavior:** composer emits `nanobrain.core.steps.EntityExtractionStep`
when the real path is `apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep`.

**Detection signal:** import fails at workflow load.

**Gate:** the deterministic `ClassPathResolver` repairs the leaf-class
name against the catalog; repair record is persisted in
`CompositionSummary.class_path_repairs`. Sustained nonzero repair
counts mean the LLM keeps drifting — that's a prompt-quality signal.

---

## Gates and rules currently shipped

| Rule | Marker phrase | Pinned in | Test |
|---|---|---|---|
| Closed-class | `CLOSED-CLASS RULE` | 7 prompts | `test_closed_class_rule_pinned_in_prompts.py` |
| Reuse-first | `REUSE-FIRST RULE` | 8 prompts | `test_reuse_first_rule_pinned_in_prompts.py` |
| Auto-transfer on DirectLink | n/a (YAML-level) | every workflow YAML | pre-commit hook G39 |
| Process()-not-execute() | n/a (Python-level) | every step subclass | pre-commit hook TX5 AC3 |
| Imports resolve | n/a | every Python file | pre-commit hook TX5 AC2 |
| No `unittest.mock` in src/ | n/a | src/ tree | pre-commit hook |
| Composer system prompt budget | `<16 KB` | composer_prompts/system.md | manual probe (`Composer.from_config + len`) |
| Reuse ratio threshold | `0.8` (default) | `CompositionSummary.is_reuse_dominated()` | `test_composition_summary_reuse_ratio.py` |

**How to add a new rule:**

1. Write the rule canonically in `composer_prompts/system.md` and/or
   the relevant `code_writing_prompts/*.md`. Use a marker phrase
   (UPPERCASE-HYPHEN-RULE style) that downstream tests can grep.
2. Pin the marker in `tests/unit/test_<marker>_pinned_in_prompts.py`
   following the existing two-axis pattern (marker presence +
   a structural / target-list invariant).
3. If the composer telemetry payload (`CompositionSummary`,
   `ComposedWorkflow`) already contains the signal needed to detect
   the rule's violation, expose it as a *derived property* (not a
   stored field).
4. Update CLAUDE.md and the workflow-authoring SKILL.
5. Run the full unit suite + T01 AC1. T01 AC1 is the load-bearing
   adoption signal — if it flaps after a rule rollout, the rule is
   degrading the LLM, not improving it.

---

## Signals to monitor

After every composer run, read:

- **`CompositionSummary.reuse_ratio`** — fraction of steps drawn
  from the library. Target: `>= 0.8`. Use `is_reuse_dominated()`
  for the binary predicate.
- **`CompositionSummary.compose_retries`** — how many compose-retry
  rounds the LLM needed. Target: `0` on first composer call.
  Sustained nonzero counts in a session = prompt quality is drifting.
- **`CompositionSummary.class_path_repairs`** — how many class-path
  suffix repairs the deterministic resolver applied. Target: `0`.
  Each repair is a clue that the LLM is hallucinating the
  shipped class layout.
- **`CompositionSummary.review_notes`** — the reviewer prompt's
  concerns. Read every entry. Non-blocking concerns are still
  signals.
- **T01 AC1 wall time** — baseline ~45-50s on mistral-nemo. > 70s
  on the same hardware = model is sampling more tokens than it
  should, usually due to prompt-budget overrun.

For code-writing workflows (the CW-series):

- **`exec_succeeded` from `IsolatedPyExecStep`** — adoption-grade
  pass/fail for any code-writing workflow with an exec gate.
- **`function_name_verified` from `CodeWriteStep`** — the AST gate's
  positive signal.
- **`approved` from `CodeReviewStep`** — the reviewer prompt's
  binary verdict.

---

## When to stop and ask

Per workspace CLAUDE.md:

- **Three-attempt cap.** If you've tried three substantively different
  approaches to fix the same failure and none worked, stop. Cosmetic
  re-tries do not count. Surface to the user with: what you tried,
  why each failed, what evidence you'd need to make progress.
- **Conflict-stop rule.** If your next action would violate any prior
  rule or earlier user instruction, stop and ask. The cost of pausing
  is small; the cost of silently breaking the contract is large.

Supervision-specific stop conditions:

- **AC1 flaps after a prompt change.** Roll back the prompt change
  before continuing. Do NOT add a workaround in code to make AC1
  pass with a degraded prompt.
- **A new drift pattern appears that no existing gate catches.**
  Document the pattern (detection signal + minimal reproducer) and
  surface to the user before patching. New gates are framework
  changes; they need scrutiny.
- **The model's output gets visibly worse on a familiar test.**
  Possible model-version drift (Ollama auto-pulled a new tag), or
  prompt-budget threshold crossed. Don't tweak the prompt to compensate
  — investigate the cause first.

---

## Session-end distillation (the virtuous cycle)

Per workspace CLAUDE.md "Session-End Distillation". After any
substantial session:

1. **Workspace-wide rule** (every repo) → workspace CLAUDE.md.
2. **Agent-specific rule** (one agent) → that agent's file in
   `.claude/agents/`.
3. **Project-specific observation** (recurring friction with a
   measured cost) →
   `_workspace_notes/apecx-mcp-integration_dev_history/session_friction_log.md`
   (unversioned per workspace policy).

**Distill criterion:** worth ≥5 minutes of session time OR recurred
≥2 turns OR a concrete recognizable pattern (not "try harder").

**Format:** Rule (imperative sentence). Detection signal. Source
(file:line or session date).

**Examples from this session that earned distillation:**

- Prose-strip rule in `code_write_step.py:_strip_leading_prose`
  (D1 above) — went into the file's docstring + a pinned test.
- Closed-class rule (D7-D8 area, broader) — went into 7 prompts +
  pinned in a test file.
- Reuse-first rule (D8 area, related) — went into 8 prompts +
  pinned + the `reuse_ratio` derived property surfaced existing
  telemetry.

**Do NOT** distill "be more careful" or "think harder" — those are
wishes, not rules.

---

## Anti-patterns

These are mistakes I (the prior supervisor) made or nearly made.
Read them.

- **Skipping the integration test "because the unit tests pass".**
  Wrong instinct. Unit tests with mocked LLMs caught structural
  issues but NEVER caught the prose-prefix drift (D1) — only the
  real Ollama call surfaced it. Integration tests are slow but
  irreplaceable.
- **Adding a "second strip pass" when the first failed.** Multiple
  recovery passes mask real LLM drift. Single-pass strip per the
  framework's drift-masking discipline; if the single pass fails,
  raise — let the upstream visibility surface the issue.
- **Editing a shared library class to make a new workflow pass.**
  Closed-class violation. Author a NEW class in the workflow's own
  directory; reference it by a new class path.
- **Adding `--ignore=<test>` to pytest because the test failed.**
  Almost always wrong-Python (the system Python vs `.venv/bin/python`
  split). Run the canonical test runner first; only `--ignore=` after
  you have justification in a commit message.
- **Approving novel_python without checking whether a library
  component covers it.** The composer's reviewer prompt does this
  check now; the supervisor's job is to read its verdict, not to
  re-do it manually.
- **Squeezing a new rule into `system.md` past the 16 KB cap.**
  At ≥ 14 KB the 12B model starts dropping later-prompt instructions.
  Future rule additions should consolidate existing rule blocks
  rather than appending.

---

## Cross-references

- **Repo-local CLAUDE.md** — composer prompt is load-bearing;
  closed-class + reuse-first rules; venv discipline.
- **Workspace-root CLAUDE.md** — non-negotiable rules, mocks
  carve-out, git/worktree discipline, session distillation.
- **`docs/architecture.md`** — canonical end-to-end map.
- **`docs/_design_index.md`** — design master index.
- **`docs/CONTRACTS.md`** — G1-G22, G44, decision records, prompt contracts.
- **`docs/implementation_task_graph.md`** — 165 file-level tasks with
  stable IDs; cite IDs in PR/commit body.
- **`.claude/skills/nanobrain-*`** — nine skills covering
  framework-native authoring patterns. Required reading before
  authoring any nanobrain code.
- **`memory/code_writing/CITATIONS.md`** — paper + project citations
  for every self-improvement / bug-fix / documentation pattern shipped.
- **`_workspace_notes/apecx-mcp-integration_dev_history/session_friction_log.md`**
  (unversioned) — recurring time-sink inventory.

---

## Honest unfiltered notes from the prior supervisor

These are observations about the framework + the model that did not
earn a rule but are worth knowing.

- **mistral-nemo 12B is surprisingly competent at single-function
  authoring.** First-pass fizzbuzz, fibonacci, add, similar are
  consistently correct. The framework's gates catch what little
  drift there is.
- **mistral-nemo struggles with multi-step reasoning across a long
  system prompt.** Past ~12 KB of system prompt, instruction
  drop-off becomes measurable (model emits inline dicts despite the
  rule against them). The 16 KB cap is a real ceiling for this model.
- **The composer's RAG matcher works well enough that the LLM rarely
  needs to invent class paths from scratch** — but it inflicts
  drift via suffix-drops (D8). The deterministic `ClassPathResolver`
  catches these.
- **Real Ollama tests cost ~30-50s wall each but are the only
  adoption-grade signal.** Run them on every prompt change. The
  cost compounds across a session if you batch the runs.
- **The prose-strip recovery added for the bug-fix workflow is
  load-bearing — without it the integration test would have
  silently emitted broken patches.** This is the kind of behavioral
  fix that integration tests catch and unit tests don't.
- **The framework's silent-failure surface is well-mapped now.**
  D1-D8 above plus the `auto_transfer:False` gap are the main
  shapes. New drift modes are likely to be variations of these,
  not entirely new shapes.

Good luck. The library is trustworthy when the closed-class +
reuse-first discipline holds; the supervisor's job is to make
sure that discipline holds across every authoring-side prompt
change.
