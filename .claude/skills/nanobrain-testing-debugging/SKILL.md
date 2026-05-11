---
name: nanobrain-testing-debugging
description: How to write tests for nanobrain components without mocking the framework into uselessness, and how to debug the framework's most common failure modes (FAIL-FAST at init, silent never-fires-trigger, link reference typos, Parsl worker import errors). Read whenever you author or fix a test, or when a step/workflow refuses to run.
---

# nanobrain-testing-debugging

## ⚠️ Two failure modes that look like missing dependencies but aren't

**(1) Wrong Python interpreter.** A `ModuleNotFoundError: No module named 'nanobrain'`
(or `apecx_db_integration`, or `apecx_integration`) on a test you know imports
the module is **almost always wrong-Python**, not a real missing dep. The
workspace's editable installs live in `apecx-mcp-integration/.venv/`. Running
plain `pytest` picks up whichever Python is first on `PATH` — typically
system anaconda — which has none of the editable installs. Diagnose with:

```bash
.venv/bin/python -c "import nanobrain; print(nanobrain.__file__)"
```

If that succeeds but `pytest` fails to import, you're using the wrong Python.
Fix by invoking pytest through the venv:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/...
# OR — easier — use the canonical runner:
scripts/run_tests.sh                 # full suite
scripts/run_tests.sh tests/unit      # subset
```

This failure mode cost an entire session of false-blocker calls before
being documented (workspace `CLAUDE.md` rule #7). Do NOT add `--ignore=`,
skip the test, or declare it a "blocker" until you've tried under the venv.

**(2) `auto_transfer=False` silent-failure.** Your integration test runs to
completion, all steps fire, no exception — but the workflow's output data
unit holds its initial value (`None` / `[]` / `{}`). The cause is almost
always a `DirectLink` whose YAML omits `auto_transfer: true`. The link
silently no-ops on every fire. Diagnose:

```bash
grep -A 2 'class:.*DirectLink' <your-workflow.yml> | grep -c 'auto_transfer: true'
# count must equal the number of DirectLinks in the file
```

See `nanobrain-data-units-triggers-links` for the full warning.

## Why this skill exists

Two opposing pressures collide in nanobrain testing:

- The framework's own architecture demands real configs, real LLMs (or real
  fake-LLM endpoints), real executors. Half-mocked tests pass and then
  production fails.
- The workspace policy says **"no mocks that have nothing to do with reality.
  Mocks are acceptable for smoke tests, but the component is never regarded
  as fully tested without real-data integration testing on a small subset."**

This skill resolves the tension: smoke tests for wiring, integration tests
for correctness, no third category called "unit tests with comprehensive mocks
that still claim to verify behavior".

## Categories of test you may write

| Category | Where mocks allowed? | Filename / path convention | What it proves |
|---|---|---|---|
| Smoke test | Yes (mock external calls) | `**/smoke/*.py` or `**/test_*_smoke.py` | The wiring assembles; configs load; constructors don't raise |
| Integration test | **No** — must hit real backends on real (small) data | `**/integration/*.py` or `**/test_*_integration.py` | The component does the right thing end-to-end |
| Unit test of pure transforms | OK if no external system involved | `**/test_*.py` | A pure function maps input X to output Y |

The policy checker (`.claude/scripts/review_policy_check.py`) flags
`unittest.mock` imports outside files whose path or name contains `smoke`.

## Smoke test pattern

```python
# tests/smoke/test_workflow_smoke.py
import pytest
from nanobrain.core.workflow import Workflow

def test_workflow_loads():
    """Wiring smoke: workflow YAML loads without error."""
    wf = Workflow.from_config('config/my_workflow.yml')
    assert wf is not None
    assert 'prep' in wf.child_steps
    assert 'agg' in wf.child_steps
    assert len(wf.step_links) >= 2

def test_step_signatures():
    """Every step exposes async process()."""
    import inspect
    from my_project.steps import PrepStep, AggStep
    for cls in (PrepStep, AggStep):
        assert inspect.iscoroutinefunction(cls.process), \
            f"{cls.__name__}.process must be async"
```

What this test does: confirms the assembly is shaped correctly. What it does
**not** do: prove the workflow runs end-to-end on data.

## Integration test pattern

```python
# tests/integration/test_workflow_integration.py
import pytest
from nanobrain.core.workflow import Workflow

@pytest.mark.asyncio
async def test_workflow_processes_real_sample():
    """Run on a small real dataset and verify the output."""
    wf = Workflow.from_config('config/my_workflow.yml')
    await wf.initialize()

    sample = load_real_sample('tests/data/small_real_sample.json')

    await wf.input_data_units['raw_input'].set(sample)

    # Wait for the workflow output to populate, with a timeout.
    result = await wait_for_data_unit(wf.output_data_units['final_results'], timeout=30)

    assert result is not None
    assert len(result['records']) == len(sample['records'])
    # Spot-check one record's transformation
    expected = expected_transformation(sample['records'][0])
    assert result['records'][0] == expected

async def wait_for_data_unit(unit, timeout):
    import asyncio
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await unit.exists():
            return await unit.get()
        await asyncio.sleep(0.05)
    raise TimeoutError(f"data unit {unit.name} did not populate within {timeout}s")
```

Notes:
- `tests/data/small_real_sample.json` must come from real data, not synthetic.
- The integration test is what makes the component "complete" per policy.
- Record the exact `pytest` command and its output in the PR/commit.

## Common failure modes and how to debug them

### Failure: `RuntimeError: Direct instantiation of {Class} is prohibited.`

You called `Class(...)` instead of `Class.from_config(...)`. Search the
test/source for `Class(` (open paren) and replace with the from_config
pattern. The policy checker catches this; run it locally:

```
python3 .claude/scripts/review_policy_check.py path/to/test.py
```

### Failure: `FAIL-FAST: Step {name}.process() must be async.`

Add `async` to your `def process`. The framework runs this check at step
initialization, before any data flows.

### Failure: Step never runs (no error, just silence)

Almost always: no trigger watching the step's input data unit. Check:

```yaml
# step config
triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "<your input unit name>"
```

If the trigger exists but the step still doesn't run, check the link target:
the link's `target:` must exactly match the step's input unit reference,
e.g. `step_id.input_unit_name`.

Diagnostic: enable `debug_mode: true` on the step and inspect logs for
"trigger fired" / "trigger registered" entries.

### Failure: `Invalid reference: X. Must use 'step.data_unit' format`

Your link source/target uses wrong syntax. Always dot notation:
`step_id.data_unit_name`. For workflow-level units, just the name (no dot).

### Failure: `❌ ILLEGAL SELF-REFERENCING LINK`

Source and target are the same data unit. Re-read the YAML; usually a
copy-paste error.

### Failure: workflow validates but produces no output

The last step's output isn't linked to the workflow's `output_data_units`.
Add a link from `last_step.output` to the workflow-level output unit name.

### Failure: Parsl import error on worker

`worker_init` script doesn't include `export PYTHONPATH=...:$PYTHONPATH`.
Fix the `worker_init` block; ensure it activates the right conda env first.

### Failure: `Parsl execution failed: ...` with pickle error

Step holds a non-pickleable attribute (open file handle, lock, lambda).
Either restructure to avoid the attribute, or use a different executor.

### Failure: tests pass locally, fail in CI

Common: the path resolver picked a different file because the cwd differed.
Use absolute paths in tests, or set the cwd explicitly in conftest.py.

## Debugging mindset (workspace-policy aligned)

1. **Reproduce first.** Don't change code until you've seen the failure.
   If you can't reproduce locally, ask for the environment that produces it.
2. **Read the verbatim error.** The framework's FAIL-FAST messages tell you
   exactly what's wrong and where. Don't paraphrase them; copy them.
3. **One change at a time.** Bundling fixes makes it impossible to know
   which one worked.
4. **Three-attempt cap.** If you've tried three substantively different
   fixes for the same failure and none worked, stop. Surface to the user
   what you tried and why each failed.
5. **No mock-to-pass.** If the failure is "real backend call returned X
   instead of Y", do not mock the backend to return Y. That makes the test
   green and the bug invisible. Either fix the call or fix the assertion.
6. **No `try/except: pass`.** Swallowing an error is the most common form
   of mock-to-pass.

## Checklist for declaring a component "done"

- [ ] At least one smoke test exists and passes.
- [ ] At least one integration test exists, runs against real (small) data,
      and passes.
- [ ] The integration test's command and its full output are recorded
      (commit body, PR description, or a `tests/results/<date>.txt`).
- [ ] The policy checker (`.claude/scripts/review_policy_check.py`) reports
      no errors against changed Python files.
- [ ] No `unittest.mock` / `MagicMock` imports outside files in `**/smoke/**`.
- [ ] If the change touches `process()`, the integration test exercises it
      (not just a smoke test that loads the YAML).
- [ ] If the change touches a Parsl/HPC path, the integration test ran on
      a real cluster (or you've explicitly stated "not yet HPC-verified").

## When you're stuck

Three legitimate next moves:

1. **Read the framework source.** The error message tells you a file:line
   range. Open it; read 50 lines around it. Most "magic behavior" stops being
   magic after you read the code.
2. **Drop into a Python REPL.** Construct the configs by hand and call
   `from_config` yourself. The error will fire at construction time, far
   away from the workflow execution loop, and you'll see exactly which
   field is wrong.
3. **Ask the user.** With your three attempts, the verbatim errors, and a
   specific question, the user can usually point you at the missing piece in
   under a minute.

## Anti-patterns to refuse

- "Mock the LLM with a fixed response so the test is deterministic." → No,
  unless this is explicitly a smoke test of wiring. For correctness tests,
  use a real LLM (with a small prompt) or a real local model.
- "Skip the test on this platform." → No. Either fix the test or escalate.
- "Wrap the failing call in try/except so the workflow keeps going." → No,
  unless the user has explicitly approved a fault-tolerant policy for that
  step. The default is fail-loudly.
- "Replace the failing real-data call with a synthetic dataset for now." →
  No. Synthetic data hides bugs. Use a smaller real subset.

## New testing primitives (2026-05-09 -> 2026-05-11)

### PromptRegressionHarness (G25)

For G14 PromptTemplate-backed prompts, the framework ships a
regression harness at ``nanobrain.library.testing.prompt_regression``:

```python
from nanobrain.library.testing.prompt_regression import (
    PromptRegressionHarness,
)

harness = PromptRegressionHarness(
    template=my_g14_template,
    llm_callable=my_llm_wrapper,  # async def llm(*, system, user) -> str
    snapshot_path=Path("snapshots/"),
)
report = await harness.run()
assert report.all_passed, report.failures_summary()
```

Reads ``regression_fixtures`` off the PromptTemplate; supports
contains / not_contains / regex / json_schema / equals assertions;
snapshot mode content-addresses by template_id + fixture_index +
content_hash so a body change auto-invalidates.

### Step-event capture for assertion (G37)

To assert on what a Workflow did internally without inspecting
DataUnits:

```python
from nanobrain.core.step_events import (
    StepEvent, subscribe_to_step_events,
)

events: list[StepEvent] = []
with subscribe_to_step_events(events.append):
    await workflow.run(input_data)
# Now `events` is a list of step_start / step_complete / step_failed
# with payload + timing + run_id.
```

### Approval lifecycle harness (G27)

To exercise a DeferredHITLStep without an external approval system:

```python
from nanobrain.library.runtime.approval_store import (
    InMemoryApprovalStore,
)

store = InMemoryApprovalStore()
step = DeferredHITLStep.from_config(yaml, approval_store=store)
# First call raises:
with pytest.raises(ApprovalPendingError) as excinfo:
    await step.process(input_data)
# Resolve externally:
store.resolve(excinfo.value.approval_id, decision="approved", ...)
# Second call returns the decision:
result = await step.process(input_data)
```

The ``test_g27_g21_wiring.py`` test fixtures show the runner-side
soft-suspend + resume integration pattern.

### Cross-references

| Need | Skill |
|---|---|
| Pick a workflow-authoring path | ``nanobrain-workflow-authoring`` |
| Write a step that publishes events | ``nanobrain-step-authoring`` |
| Build a tool adapter for tests | ``nanobrain-agents-tools`` |
| Verify YAML doesn't strip whitespace | G34 in ``nanobrain-config-yaml`` |
