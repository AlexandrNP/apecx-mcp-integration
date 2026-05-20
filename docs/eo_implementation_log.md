# EO-* Implementation Log

Running record of the external-orchestration surface build
(`external_orchestration_design.md` + `implementation_task_graph.md`).
Worktree: `wt-eo-mvp`, branch `eo-mvp-output-surface`.

**Worktree test command** (the worktree shares the main checkout's `.venv` but NOT its
editable install; `PYTHONPATH` overrides the install — verified 2026-05-20 resolving to
the worktree `src`, so tests exercise worktree code, not the main checkout):

```bash
MAIN=/Users/onarykov/Downloads/apecx-cowork/apecx-mcp-integration
WT=/Users/onarykov/Downloads/apecx-cowork/wt-eo-mvp
PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m pytest "$WT/tests/..."
```

---

## EO-10 — `WorkflowResult` envelope ✅ 2026-05-20

- `src/apecx_integration/composition/schemas/workflow_result.py`
- Plain Pydantic `BaseModel(extra="forbid")` — NOT a `from_config` component (per the
  nanobrain compliance brief: result envelopes are data, not framework components).
- Fields: `markdown` / `status`(`ok|partial|error`) / `data_handle` / `data_preview` /
  `run_id` / `error`.
- Loud invariants (no-silent-failure discipline): `status=="error"` requires a non-empty
  `error`; `error` forbidden when status is not error; `data_preview` requires
  `data_handle`. `.failed()` ergonomic constructor for the loud-error path.
- Tests: `tests/unit/test_workflow_result.py` — **9 passed**.

## EO-12 — Canonical data shapes ✅ 2026-05-20

- `src/apecx_integration/composition/schemas/data_shapes.py`
- `RecordSet` / `Evidence` / `Bundle` / `Artifact`; `kind`-discriminated union `DataShape`;
  `parse_data_shape()` is loud on unknown/missing `kind` or typo'd field. Every shape has a
  uniform `.preview(limit)` that feeds `WorkflowResult.data_preview`.
- Tests: `tests/unit/test_data_shapes.py` — **9 passed**.

Combined run: **18 passed in 0.05s**.

---

## Decisions / findings

- **Worktree testing verified.** `PYTHONPATH=<wt>/src` resolves `apecx_integration` to the
  worktree (plain `.pth` install entry; PYTHONPATH wins). Silent-wrong-src risk ruled out.
- **Stale doc/memory finding.** `scripts/checks/wait_for_cascade_use.py` (referenced by the
  repo CLAUDE.md and the `g124` memory) does NOT exist in this checkout — only
  `imports_resolve.py` and `step_authoring.py` are present. The G124 work may live on an
  unmerged branch; to reconcile (do not re-trust the lint's existence).
- **EO-30 deferred.** Adding a generic `mcp` `BackendKind` touches the deliberately-fixed
  `CONTRACTS.md#td-vocab` vocabulary — needs explicit confirmation before implementing.

## EO-11 — Handle store ✅ 2026-05-20

- `src/apecx_integration/composition/handles/store.py`
- `HandleStore.put(DataShape) -> str` / `get(str) -> DataShape` (typed via the `kind`
  discriminator). `HandleNotFound` raised loudly on an unknown handle (never a silent None).
- Backend behind a `HandleBackend` Protocol: v1 `InMemoryBackend` (thread-safe, process/
  session lifetime). ProxyStore (installed — `proxystore 1.0.0`; nanobrain `DataUnitProxyRef`
  at `core/data_unit.py:2039`) is the documented HPC-scale swap-in — **deferred** (no MVP
  HPC-scale payloads; `CONTRACTS.md#ext-tool-dispatch` scopes ProxyStore to HPC-scale).
  Deliberate, documented deviation from the design's "reuse ProxyStore" line.
- `default_handle_store()` — process-wide singleton shared across MCP tool calls.
- Tests: `tests/unit/test_handle_store.py` — **7 passed**.

Spine so far: **25 passed** (EO-10 9 + EO-12 9 + EO-11 7).

## EO-13a — EnvelopeStep (nanobrain BaseStep) ✅ 2026-05-20

- `src/apecx_integration/composition/steps/envelope_step.py`
- Reusable terminal step: wraps a step's output into a `WorkflowResult`. nanobrain-compliant
  (BaseStep + `_get_config_class`, `async process`, module-level `log` not `self.logger`, no
  `execute` override). Builds via `from_config` with just `name:`.
- When the input carries a serialized `DataShape` under `data`, it parses it (loud on bad
  `kind`), stashes it via the handle store, and attaches `data_handle` + `.preview()` to the
  envelope — keeping the structured payload OUT of the markdown channel. Missing/empty
  `markdown` raises loudly.
- Tests: `tests/unit/test_envelope_step.py` — **7 passed**. The headline test stashes a
  50-record payload with a sensitive value and asserts that value is **absent from the
  markdown channel** yet fully retrievable via the handle (channel separation proven).

Spine so far: **32 passed** (EO-10 9 + EO-12 9 + EO-11 7 + EO-13a 7).

## EO-13b — Headline chaining integration test ✅ 2026-05-20

- `tests/integration/test_envelope_chaining.py` + `src/.../composition/steps/envelope_step.yml`
- Builds a real workflow via the lightweight `WorkflowBuilder` (wiring copied from the shipped
  `tdr_loop_lightweight.py`), runs it through `Workflow.run()` (real trigger/link cascade,
  ~2s). Demonstrates A→B chaining: a 50-record payload with a sensitive value is stashed
  behind a handle by workflow A, is **absent from A's markdown channel**, and the full payload
  round-trips via the handle into workflow B — structured data flows out-of-band while only
  markdown + preview reach the orchestrating LLM. Exercises the lightweight authoring path.
- **1 passed.** Full EO suite now **33 passed** (EO-10 9 + EO-12 9 + EO-11 7 + EO-13a 7 + EO-13b 1).

### Noted (deferred, not my code)
- `Workflow.run()` emits a pydantic `UserWarning` (`validate_graph` field is a function being
  model_dump'd) — nanobrain-internal `WorkflowConfig` serialization, cosmetic, pre-existing.
  Candidate framework cleanup; not blocking.

## EO-40/41/42/43 — Provenance + events wiring ✅ 2026-05-20

- `src/apecx_integration/composition/runtime/provenance_wiring.py`
- **Verified first** (the subagent flagged it as unverified): `BaseStep._execute_process`
  DOES auto-call `record_step_invocation` (step.py:2032/2080) + `publish_step_event`
  (2010/2051/2093) when a provenance context is active — so activation actually records, not
  silently no-op.
- `run_with_provenance(workflow, input_data, redact=None, **run_kwargs)` — activates a G4
  `ProvenanceContext` with an injected in-memory `MemorySink` (no built-in in-memory sink
  exists) + subscribes to G37 step events, around `Workflow.run` (not manual
  process+wait_for_cascade, per G124/G125). Returns `ProvenanceRun(result, step_records,
  step_events)`. `redact=None` ⇒ nanobrain default (`prompts` + `executor_env` elided) —
  EO-43; override per call.
- `summarize_run()` → `RunSummary` (EO-42): per-step name/status/duration/n_tool_calls/
  n_llm_calls — the scientist-facing "what ran" view.
- Test: `tests/integration/test_provenance_wiring.py` — **1 passed**. Asserts records are
  NON-empty (loud guard: a context that activates but records nothing is a silent failure).
- Full EO suite now **34 passed**.

## EO-02 — Recursive workflow inspector ✅ 2026-05-20

- `src/apecx_integration/composition/inspection/workflow_inspector.py`
- `inspect_workflow(yaml_path, max_depth=3)` → `WorkflowInspection` tree: name, config_version,
  workflow-level + per-step data units, steps (class/config_path/DUs), links (class/source/
  target/condition), recursing into nested-workflow step configs (depth-capped, `truncated`
  flag). Pure static analysis — no component instantiation. Loud on a dangling step-config
  reference (FileNotFoundError) and non-mapping YAML (ValueError).
- Serves "static composition visibility" (design §4/§9): the scientist sees what a workflow is
  configured to run, recursively, grounded in the YAML.
- Tests: `tests/unit/test_workflow_inspector.py` — **6 passed** (incl. against the real
  `tdr_refine_workflow.yml`, recursion, depth cap, loud failures).
- Full EO suite now **40 passed**.

## EO-03 (core) — Observed workflow run ✅ 2026-05-20

- `src/apecx_integration/composition/runtime/observed_run.py`
- `run_workflow_observed(workflow, input_data, ...)` → `WorkflowRunOutcome(raw_result,
  workflow_result, run_summary)`: composes `run_with_provenance` + `summarize_run` + best-effort
  `WorkflowResult` extraction. The core the MCP `run_workflow` tool will wrap (tool adds
  name-lookup + FastMCP registration). Returns `workflow_result=None` honestly when a workflow
  emits no envelope (not a silent empty one).
- Test: `tests/integration/test_observed_run.py` — **1 passed**. Full EO suite **41 passed**.

## EO-20 — Local bounded decomposition control structure ✅ 2026-05-20

- `src/apecx_integration/composition/decomposition/local_decomposer.py`
- `LocalDecomposer.solve(task)`: match-single-workflow-first; else (if decomposable) decompose
  into sub-workflows + dispatch recursively + integrate; else a loud `WorkflowResult.failed`
  ("cannot solve"). Bounds: `max_depth` (recursion) + `max_dispatches` (fan-out) as plain
  counters — **corrected the design's "bounded by LoopController"** (LoopController is for
  workflow back-edges, not recursion depth). Injectable async matcher/decomposer/dispatcher
  protocols; the LLM/RAG/runtime parts are the only non-deterministic boundary.
- Caught + fixed a self-inflicted silent-failure in `_integrate`: a failed child's error message
  (in `.error`, not `.markdown`) was being dropped from the aggregate — now surfaced as
  `**ERROR:** ...` and the aggregate is `partial` (never silently `ok`).
- Tests: `tests/unit/test_local_decomposer.py` — **7 passed** (match-first, decompose,
  cannot-solve, depth cap, dispatch budget, partial propagation, threshold). Full EO suite **48 passed**.

### Remaining for the full decomposition feature
- Real `WorkflowMatcher` (RAG over the workflow catalog), real `TaskDecomposer` (local LLM —
  Ollama-gated integration), real `WorkflowDispatcher` (`run_workflow_observed`). A nanobrain
  Step wrapper if decomposition must run *inside* a workflow cascade.

## EO-20 (real impls) — matcher + dispatcher ✅ 2026-05-20

- `decomposition/matchers.py` `KeywordWorkflowMatcher` (Jaccard token-overlap baseline; semantic
  RAG is the richer swap-in via the same `WorkflowMatcher` protocol) + `decomposition/dispatchers.py`
  `RunWorkflowDispatcher` (wraps `run_workflow_observed`; loud on unknown workflow + no-envelope).
- **2 of 3 decomposition boundaries now REAL + tested without an LLM**; only `TaskDecomposer`
  remains LLM-gated.
- Tests: `tests/unit/test_workflow_matcher.py` (5) + `tests/integration/test_decomposition_dispatch.py`
  (3, incl. `LocalDecomposer` match→dispatch end-to-end on a real workflow). Full EO suite **56 passed**.

## Next

- `TaskDecomposer` real impl (local LLM) + Ollama-gated integration — the last decomposition boundary.
- EO-03/04/05 MCP-tool registration into the FastMCP server (needs MCP-client integration loop).
- EO-01 catalog reconciliation; EO-13c rag_e2e wiring (Ollama-gated).
