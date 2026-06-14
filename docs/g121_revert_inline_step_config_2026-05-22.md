# G121 revert — inline step configs forbidden again + WorkflowBuilder silent-failure fix (2026-05-22)

## What changed

`nanobrain` `ConfigBase._is_inline_config_supported` no longer returns
`True` for `BaseStep` subclasses. **Steps, Workflows, and Agents are
file-path-only again**; `DataUnit` / `Link` / `Trigger` remain
inline-tolerant. This unrolls G121 (2026-05-18), which had relaxed the
classifier to accept inline `steps[].config: {...}` dicts.

- nanobrain commit: `2dd7d7b` (branch `academy-integration`).
- File: `nanobrain/nanobrain/core/config/config_base.py`
  (`_is_inline_config_supported`, ~line 1200).

## Why

1. **A Step's config carries ownership semantics** — its data units,
   triggers, and identity. That belongs in a reviewable, diffable,
   path-referenced YAML, not an inline blob buried in a workflow file.
2. **G121 silently disabled a downstream policy guard.** The
   apecx-mcp-integration composer validator's `step_inline_config_forbidden`
   rule (`src/apecx_integration/composition/workflow_validator.py`)
   delegates to nanobrain's `_is_inline_config_supported`. When G121 made
   that return `True` for steps, the validator stopped flagging inline
   step configs — an anti-hallucination guard for LLM-authored workflows
   went quiet with no signal. This is a textbook silent-failure shape:
   the rule still existed, the tests still ran, but the behavior was
   inverted by a change two repos away.

## Blast radius (measured, not assumed)

The revert broke exactly **4 tests across the entire nanobrain unit
suite** — all in `tests/unit/test_g125_process_workflow_id_tag.py`, all
using one shared test fixture (`_build_workflow_yaml`) that hand-wrote an
inline `config: {...}` dict for its `_SlowEcho` step. Fixed by splitting
the step config into its own `echo_step.yml` file referenced by path.

`tests/unit/test_workflow_builder.py` passed untouched, because the
inline-config gate only fires on a nested `config:` *dict value* — and the
`WorkflowBuilder` emits **flat** step entries (`{name, class, ...kwargs}`
with no nested `config:` key). That observation led to the real finding
below.

## The real finding — WorkflowBuilder produced 0-step workflows (pre-existing)

While confirming the lightweight path still worked, an end-to-end probe
(`build → load() → run()`) revealed that `WorkflowBuilder.load()` was
**silently producing workflows with zero child steps**:

```
child_steps: []
run outputs: {'status': 'no_first_step', 'workflow': 'probe_wf'}
```

Root cause (independent of G121): `ConfigBase._resolve_nested_objects`
instantiates a nested component only when its entry has **both** `class`
and `config` keys (`config_base.py:1048`). The builder dumped flat step
entries (no `config:` key) to one YAML; the loader never resolved them
into step instances, so each flat dict was skipped at
`workflow.py:~1720` ("Step not resolved via class+config"). The workflow
loaded with no exception and ran to a no-op.

This was latent because `test_workflow_builder.py` only asserted
`get_config()` *dict shape* — there was **no `load()`+`run()` test at
all**. Two green test layers (builder unit tests + framework
`from_config` not raising), broken product.

### Fix

`WorkflowBuilder.load()` now materializes each step's config to its own
temp YAML file and rewrites the step entry to
`{class, config: <abs path>, [executor]}` before calling
`Workflow.from_config`. Links and triggers stay inline (they are not
excluded by `_is_inline_config_supported`). All temp files live under one
temp directory deleted on return.

This is the "expand framework capacity" path the revert anticipated:
programmatic builders construct steps without hand-written YAML by
writing per-step temp files, preserving the file-only invariant.

### Regression coverage added

`tests/unit/test_workflow_builder.py::TestBuilderLoadAndRun`:
- `test_load_materializes_child_steps` — asserts `load()` yields a
  workflow with the `echo` child step + its input/output data units.
- `test_run_drives_cascade_end_to_end` — drives a real `Workflow.run`
  cascade and asserts `status == 'completed'` and the workflow output
  propagated (`wf_out == "echoed:..."`). This is a real cascade against
  real `DataUnitMemory` / `DirectLink` / `DataUnitChangeTrigger`, not a
  mock.

## Verification

```bash
# nanobrain full unit suite (post-revert + builder fix)
PYTHONPATH=../nanobrain:src .venv/bin/python -m pytest ../nanobrain/tests/unit/ -q
# → 1201 passed, 9 skipped

# apecx full unit suite
PYTHONPATH=../nanobrain:src .venv/bin/python -m pytest tests/unit/ -q
# → 1211 passed, 4 skipped

# the 6 step_inline_config_forbidden tests specifically
PYTHONPATH=../nanobrain:src .venv/bin/python -m pytest tests/unit/test_workflow_validator.py -q
# → 22 passed
```

## CI caveat (open — needs user action)

apecx-mcp-integration CI installs nanobrain from
`git+https://…@academy-integration`. The G121 revert (and the earlier
G127 `GlobusManifestVerifyStep` + builder fix) are **local-only commits
on the nanobrain `academy-integration` branch — not pushed**. Until
nanobrain is pushed, apecx CI pulls the OLD nanobrain, where
`_is_inline_config_supported(Step)` still returns `True`, and the 6
`step_inline_config_forbidden` validator tests **fail in CI** even though
they pass locally. Pushing a remote requires explicit user approval
(workspace git-discipline rule), so this is surfaced rather than actioned.
