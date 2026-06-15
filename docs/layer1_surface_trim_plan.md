# Layer-1 MCP surface trim — action plan

**Goal:** bring the exposed MCP tool surface in line with
`external_orchestration_design.md` §4 — a lean set of orchestration PRIMITIVES +
workflows-as-objects, with operational tools OFF the agentic surface. Today the
static surface is **24 tools**; §4 specifies ~6 primitives ("primitives, not
super-tools"; "operational tools … out of the agentic loop"). Target: **~11–13
static** + catalog workflows.

Authoritative source: `external_orchestration_design.md §4` (surface), `§8`
(tiered substitution), `return_of_control_design.md` (composer HITL),
`architecture.md §14` (synthesis pipeline).

## Target surface (after)

**Keep — agentic primitives:**
- `list_workflows`, `inspect_workflow`, `run_workflow`, `run_workflow_streaming`,
  `inspect_run`, `apecx_context`, `apecx_capabilities`
- `harmonized_search` — the canonical retrieval primitive
- `compose_workflow` — ONE tool, replaces `start_workflow`/`show_diff`/`execute_workflow`
- `approve_design` — stays: it is the HITL gate of the `viral_epitope_analysis`
  WORKFLOW, not a standalone operational tool
- (decision) `describe_workflow`, `database_statistics`, `infrastructure_status` — meta

**Remove from the agentic surface:**
- `synthesize_query` (retire — super-tool, legacy local-CSV retrieval)
- `list_pending_approvals`, `approve`, `reject`, `correct` (operational control-plane)
- `estimate_cost`, `confirm_allocation`, `export_hpc_bundle`, `ingest_hpc_bundle`
  (operational → re-expose as ONE workflow)

Net: 24 → ~11 static (plus the catalog workflows, which gain an HPC-export entry).

---

## Phase 1 — Retire `synthesize_query` (LOW risk)

`synthesize_query` drives `rag_e2e_synthesis`; its retrieval
(`SynthesisContextAssemblyStep`) pulls local VIOLIN/BV-BRC CSVs + raw Globus —
NOT harmonized search. It is a super-tool that duplicates `run_workflow`.

- **Code:** remove `server.tool()(synthesis_tools.synthesize_query)` (server.py).
  Keep the underlying Python functions (the composer + internal pipelines import
  them — verify with grep before deleting `tools/synthesis.py`).
- **Decision D1:** does the generic-synthesis capability stay?
  - (a) **Retire fully** — `viral_epitope_analysis` already does grounded
    synthesis WITH harmonized search; recommended.
  - (b) Register `rag_e2e_synthesis` as a catalog workflow (run_workflow target).
    Only worthwhile if its retrieval is first rewired to `harmonized_search`
    (otherwise we re-expose the legacy local-CSV path) → folds into a later
    "harmonized synthesis workflow" task, not this trim.
- **Tests/docs:** `tests/unit/test_synthesize_query_tool.py` (remove or repoint);
  `docs/{architecture.md §14.2, mcp_integration.md, clean_install_capabilities_scoring.md,
  return_of_control_implementation_plan.md}`.
- **Verify:** server imports; `list_workflows` unchanged; suite green.

## Phase 2 — Operational tools off the surface (MEDIUM — test-heavy)

Remove the 8 operational registrations (4 approvals + 4 HPC) from `server.py`.
The control-plane **routes + client + schemas REMAIN** — these operations still
exist; they just leave the agentic MCP wire (per §4 "out of the agentic loop").
The scientist/operator drives them via the control-plane API / `apecx-cp`.

- **Keep `approve_design`** (workflow gate). Distinguish it from the 4
  control-plane `approvals_*` tools (there are two approval systems on the wire
  today — this collapses to one on the agentic surface).
- **Code:** remove 8 `server.tool()` lines + the now-unused imports
  (`approvals_tools`, `hpc_tools`) IF nothing else in server.py uses them.
- **Tests:** the integration tests that exercise the control-plane **client/routes**
  (`test_async_approval_fifo`, `test_client_happy_paths`, probe batches 19/20/37/51)
  are UNAFFECTED — they call the client, not the MCP registration. Tests asserting
  the tools are **registered on the MCP surface** (`test_mcp_server.py`, probe-batch
  surface audits) must be updated to the new surface.
- **Verify:** server imports; the 8 tools absent from `tools/list`; control-plane
  client/route tests still green.

## Phase 3 — HPC as ONE catalog workflow (HIGH — new work)

Replace the 4 removed HPC primitives with a single discoverable `hpc_export`
catalog workflow. The current flow is a HITL sequence: `estimate_cost` →
`confirm_allocation` (operator approves core-hours) → `export_hpc_bundle` →
[operator runs qsub manually] → `ingest_hpc_bundle`.

- **Shape:** a lightweight-builder workflow whose steps wrap the existing
  control-plane client calls, with a **HITL allocation-confirm gate** (reuse the
  `needs_prerequisite` / approval-token pattern proven in the design-gate) and a
  **pause/resume** for the manual qsub + ingest. This is a cycle/pause workflow →
  G99/G127 discipline applies (integration test must drive real `Workflow.run`
  and assert on OUTPUT values, not status).
- **Decision D2:** do this now, or just remove HPC from the surface (Phase 2) and
  defer the replacement workflow? Removing-now leaves HPC operator-only (CLI/API)
  until the workflow lands. Recommended: Phase 2 now, Phase 3 as a tracked follow-up.
- **Files:** `composition/workflows/hpc_export/` (builder + steps); catalog entry;
  `tests/integration/test_hpc_export_workflow.py`.

## Phase 4 — Collapse the composer split (MEDIUM — ROC reconciliation)

`start_workflow` / `show_diff` / `execute_workflow` (3 tools) compete with
`run_workflow`: the LLM composes a NEW workflow when it should run an existing
one. §4 specifies ONE `compose_workflow(intent, base?)`.

- **Fix the competition (two parts):**
  1. **Collapse 3 → 1** `compose_workflow` that returns the proposed workflow +
     the T06 diff in one envelope; execution is a follow-up `run_workflow(run_id)`
     or an explicit approve-then-run (the diff-review HITL moves to the
     control-plane approval, off the agentic surface — consistent with Phase 2).
  2. **Description discipline:** `compose_workflow` must state "use ONLY when
     `list_workflows` finds no matching workflow" so the LLM tries run-first.
- **Decision D3:** how to preserve the `return_of_control` diff-review HITL when
  going 3→1 — fold the diff into the compose envelope (recommended) vs keep a
  separate review step. Must reconcile with `return_of_control_design.md`.
- **Files:** `tools/workflows.py` (collapse), server.py, ROC docs + tests.

## Cross-cutting — lock the surface (do with Phase 1)

Add `tests/unit/test_mcp_tool_surface.py` that asserts the EXACT set of registered
MCP tool names == the approved Layer-1 set. This is the regression guard that
would have caught `analyze_viral_immunology` sneaking on as a standalone tool.
Every future surface change must update this list deliberately.

## Order & risk

1. **Phase 1** (synthesize_query) — low, immediate.
2. **Cross-cutting surface-lock test** — low, high-value.
3. **Phase 2** (operational off surface) — medium; the win is biggest here (−8).
4. **Phase 4** (composer collapse) — medium; ROC reconciliation.
5. **Phase 3** (HPC workflow) — high; follow-up after the surface is trimmed.

Recommended first cut: **Phases 1 + 2 + surface-lock test** → 24 → ~14 static
immediately, design-aligned, low-to-medium risk. Then 4, then 3.

## Decisions needed before execution
- **D1** rag_e2e_synthesis: retire fully (recommended), or re-register as a
  harmonized catalog workflow (separate task)?
- **D2** HPC: remove-from-surface-now + workflow as follow-up (recommended), or
  build the replacement workflow before removing?
- **D3** Composer: fold diff-review into a single `compose_workflow` envelope
  (recommended), or keep a separate review step?
- **D4** Meta tools (`database_statistics`, `infrastructure_status`,
  `describe_workflow`): keep on the surface, or also trim?
