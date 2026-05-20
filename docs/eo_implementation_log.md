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

## Next

- EO-13 `WorkflowResultStep` adapter (non-invasive terminal step wrapping a step's output
  into a WorkflowResult, optionally stashing structured payload via the handle store) +
  the headline handle-chaining integration test (A→B via handle, no structured data through
  the LLM context). The deterministic adapter + chaining test does NOT need a live LLM; the
  rag_e2e wiring (live-LLM, Ollama-gated) is a separate sub-step.
- EO-01/02 surface (reconcile `discovery.py` + `workflow_registry` catalogs).
- EO-40/41 provenance wiring (parallel track; verify G4 ProvenanceContext interface first).
