# External-Orchestration Surface — Design

**Status:** Active design (2026-05-19). Authoritative for the agentic surface direction.
**Supersedes:** the server-side multi-agent orchestrator roadmap (Track B Phase 0–2 +
autonomy sub-track in `implementation_task_graph.md`). That roadmap was an unbuilt
work-in-progress; this document replaces it.
**Reuse posture:** ~85% assembly of already-built components. New code is the small
delta in §10.

---

## 1. Purpose

One paragraph: a scientist's query enters a powerful **external** LLM (Claude/GPT) over
MCP. The external LLM is the **primary orchestrator** — it decomposes the query, selects
workflows from a catalog, sequences them, glues their outputs, and synthesizes the final
answer. Workflows are **deterministic, rule-based nanobrain scaffolds**. A **local** LLM
appears only inside a workflow with **bounded discretion**: when a (sub)task is not solved
by a single workflow but is decomposable into workflows, it decomposes and dispatches —
otherwise it does nothing. Match-a-single-workflow-first; decompose only as fallback.

This inverts the retired roadmap, which placed orchestration server-side on a local
(Ollama) LLM. The frontier LLM is more capable at decomposition and synthesis; the local
LLM's job shrinks to deterministic execution plus bounded fallback decomposition.

---

## 2. Architecture — two decomposition layers

```
   ┌──────────────────────────────────────────────────────┐
   │ EXTERNAL frontier LLM (Claude/GPT) — PRIMARY, always   │
   │   decompose query → select workflows → sequence →      │
   │   glue (pass handles) → synthesize final answer        │
   └───────────────────────────┬──────────────────────────┘
                               │ MCP (workflow-as-object tools)
                               ▼
   ┌──────────────────────────────────────────────────────┐
   │ Workflow catalog (deterministic nanobrain scaffolds)   │
   │   data acquisition · transformation · analysis ·       │
   │   aggregation · synthesis (markdown) · tool execution  │
   └───────────────────────────┬──────────────────────────┘
                               │ a workflow MAY contain:
                               ▼
   ┌──────────────────────────────────────────────────────┐
   │ LOCAL LLM decomposition step — FALLBACK only           │
   │   single workflow matches subtask?                     │
   │     yes → run it (deterministic)                       │
   │     no + decomposable → decompose into sub-workflows,   │
   │        dispatch (bounded by LoopController + cost cap)  │
   └──────────────────────────────────────────────────────┘
```

Coarse decomposition at the top (external, always). Fine decomposition at the bottom
(local, fallback only). Determinism is the default path; LLM discretion is the exception.

---

## 3. Reuse map — built components → role (verified 2026-05-19)

| Built component (path) | Role |
|---|---|
| `src/.../composition/workflows/rag_e2e_synthesis/` (→ markdown) | Reference "single workflow"; the markdown output channel |
| `src/.../mcp_surface/workflow_registry.py` + `tools/discovery.py` | The catalog the external LLM (and local fallback) matches against |
| `src/.../composition/composer.py` + nanobrain `library/orchestration/plan_lowering_step.py`, `skeleton.py`, `skeleton_loader_step.py` | `compose_workflow` MCP tool **and** the local LLM's sub-workflow decomposition mechanism |
| nanobrain `library/steps/loop_controller.py` | Bounds local recursive decomposition (depth/repeat caps) |
| nanobrain `core/unified_tool_descriptor.py` (UTD; `BackendKind` includes `rhea_mcp`) | Common-denominator tool record (`CONTRACTS.md#td-vocab`) |
| `rhea/` fork + `RheaMCPDiscovery`/`RheaAdapter` + nanobrain `library/tools/_mcp_transport.py` + `rhea/.../apecx_utd_extension/utd_producer.py` | Deterministic tool-execution layer; first ingested tools (MUSCLE e2e-tested) |
| ProxyStore Key I/O (`CONTRACTS.md#ext-tool-dispatch`) | The **data handle** for workflow-to-workflow chaining |
| nanobrain `core/provenance.py` (G4; `CONTRACTS.md#g4`) + step events (G37) | Execution visibility — wire into workflow entry, do not rebuild |
| nanobrain `core/cost_envelope.py` + `core/capabilities.py` | Bounds on local-LLM recursion (cost ceiling, capability gating) |
| nanobrain `core/unified_tool_descriptor.py::determinism_class` (`CONTRACTS.md#hpc-determinism`) | "Deterministic relative to versions" classification |

---

## 4. MCP surface — workflows as first-class objects

The external LLM is the orchestrator, so it needs primitives, not super-tools. Reuse
`workflow_registry.py` + discovery; shape the rest around the built `rag_e2e_synthesis`
pipeline. Functional needs (final tool count is an implementation detail):

- **discover** — `list_workflows(category?, query?)`: RAG over the registry; returns
  names + 1-line descriptions + match scores. Reuses discovery tools + apecx-rag.
- **inspect** — `inspect_workflow(name, depth?)`: recursive resolution of the workflow
  YAML tree (nanobrain `class:`/`config:` references). Visibility is YAML-grounded.
- **run** — `run_workflow(name, params)`: returns the §5 result envelope + a `run_id`.
- **inspect_run** — `inspect_run(run_id, detail?)`: G4 provenance + effective config + logs.
- **compose** — `compose_workflow(intent, base?, ...)`: the composer; `base` set ⇒
  derivation/substitution (§8).
- **context** — `apecx_context()`: session re-orientation (runs, handles, derived
  workflows this session). Scoped, not general memory.

Operational tools (HPC export, approvals, setup) stay where they are — out of the agentic
loop.

---

## 5. Output contract — markdown + handle

A workflow result is a small envelope:

```
WorkflowResult:
  markdown: str          # presentation; author controls granularity (generalizes rag_e2e_synthesis)
  data_handle: str|null  # ProxyStore Key to structured output; chains A→B without entering LLM context
  data_preview: dict|null# small structured peek for the external LLM to reason about
  run_id: str            # provenance pointer (§9)
  status: ok | partial | error
```

The external LLM reads `markdown` to reason/synthesize; chains workflows by passing
`data_handle` (the structured payload never round-trips through its context); uses
`data_preview` to decide next steps. Behind the handle, a small controlled set of
shapes (RecordSet / Evidence / Bundle / Artifact) keeps workflow-to-workflow
compatibility typed.

**Relationship to `CONTRACTS.md#output-layer`:** the retired roadmap's `list[Layer]`
contract (fixed biology vocabulary) was for the server-side layered-reasoning workflow,
which is not built. This generic markdown+handle envelope is the surface contract for the
external-orchestration direction. `#output-layer` is not deleted (it is code-cited); it
simply does not govern this surface.

### 5b. Desktop RE-INGESTION contract (the host LLM re-renders the result)

The envelope above is the INTERNAL shape (headless/agent callers + the run-store + tests get it
verbatim). But in **desktop** locus the user-facing host LLM RE-INGESTS the tool result and
re-renders it for the user — and it is often a WEAK model (e.g. Haiku) that, given markdown alone,
**crops it** and never surfaces the generated images (they were file paths it cannot read). So at the
MCP **tool boundary** (and ONLY there — `eo_primitives.maybe_desktop_payload`, applied by
`run_workflow_tool` + `workflow_registry._live_dispatch`), a completed (`status in {ok, partial}`)
desktop result is returned as **MCP content**, not a dict:

1. a TextContent = explicit **rendering INSTRUCTIONS for the host** (reproduce every section in full,
   do not crop, include every attached image, keep citations, use the structured block for exact
   numbers) + the **full** markdown report;
2. each generated figure (PyMOL surface render, conservation plot) as a FastMCP **`Image`** content
   block → base64 → Claude Desktop renders it **inline** (the SASA/structural images actually reach
   the user, not as a path);
3. a TextContent = a **lean** structured-data JSON block (`run_id` / `data_handle` / `data_preview` /
   `provenance` + a name·kind·path artifact manifest). The per-tool JSON `text` is STRIPPED — dumping
   hundreds of KB would blow a weak host's context and re-introduce the crop.

Degrade-loud: any adapter or per-figure failure returns the dict / skips that figure (the tool never
breaks). `error` / `needs_input` (which carry an error / a `control_transfer` gate, not a report) and
all headless/agent paths keep the raw dict. Implementation spec: `docs/desktop_reingestion_spec.md`.

---

## 6. Local decomposition step (bounded, fallback)

A workflow step implementing the §2 bottom layer:

1. Given a subtask, query the registry for a matching single workflow (RAG + threshold).
2. Match above threshold → dispatch that workflow (deterministic).
3. No match but decomposable → local LLM decomposes into sub-workflows; dispatch each;
   integrate. Bounded by `LoopController` (depth/repeat caps), `cost_envelope`
   (cumulative cost ceiling), `capabilities` (capability gating).
4. No match and not decomposable → return a first-class "cannot solve" result (loud, not
   a fabricated answer).

Reuses built primitives end to end; the only new code is the wiring step + the match-first
policy.

---

## 7. Tool ingestion (reuse UTD + RHEA; one framework delta)

- Tools are **UTDs** (`unified_tool_descriptor.py`). `BackendKind` already covers
  `rhea_mcp`; RHEA tools ingest via the existing `RheaMCPDiscovery`/`RheaAdapter` path.
- **Framework delta (the one real new framework change):** arbitrary external MCP servers
  beyond RHEA need a generic `mcp` value in `BackendKind`. That literal set is
  deliberately fixed (`CONTRACTS.md#td-vocab`); adding `mcp` is a framework version bump
  with a catalog sweep. Decide consciously — do NOT add it casually.
- Galaxy/Toolshed rich metadata (needed for §8 interface tags) survives only in RHEA's
  in-process path (`utd_producer.py`), not on the MCP wire. Arbitrary baseline-MCP tools
  get no interface tag → degrade to composer substitution (§8 Tier 3).

---

## 8. Tiered substitution (scientist swaps a tool / param)

| Tier | When | Mechanism | LLM? |
|---|---|---|---|
| 1 | Replacement implements the **same interface** (MUSCLE↔MAFFT) | Config-patch on the resolved YAML tree, gated on interface-tag match | No |
| 2 | Different interface, **declared param/output mapping** exists | Deterministic mapping | No |
| 3 | Genuinely different I/O shape | `compose_workflow(base=...)` — composer rebridges via DirectLink + novel Python (no TransformLink, per composer rules) | Yes |

Substitution produces a **persistent** derived workflow (own YAML), registered and
discoverable. Tier 1 needs interface tags (§7 caveat). All tiers honor the composer's
CLOSED-CLASS rule: substitution selects a different component; it never edits a shared
component class.

---

## 9. Provenance (wire, don't build)

G4 `ProvenanceContext` is built but not auto-wired into `Workflow.run()`. Plan:
- Activate G4 at the workflow entry → per-step inputs/outputs/tool_calls/timing/run_id,
  with G4's built-in redaction.
- Subscribe to G37 step events for execution order/status.
- Capture **effective config** (post-override resolved tools+params) as a small run
  summary — the one genuinely missing piece (the scientist-facing "what ran with what
  params" view your visibility requirement needs).
- Static composition visibility = recursive YAML inspection (§4 inspect). Dynamic = G4
  records + run summary + logs.

---

## 10. Genuine deltas (the only new work)

1. **MCP workflow-object surface** (§4) — assemble around `workflow_registry.py` + discovery.
2. **`WorkflowResult` markdown+handle envelope** (§5) — generalize rag-synthesis + ProxyStore.
3. **Local bounded-decomposition step** (§6) — new wiring over LoopController + composer + cost/capability.
4. **Interface-tag substitution Tier 1** (§8) — needs interface tags from RHEA's rich path.
5. **Generic `mcp` BackendKind** (§7) — the one framework-contract change (version bump).
6. **G4 auto-wiring + effective-config run summary** (§9).

Everything else is reuse.

---

## 11. Open decisions / risks

- **R1 — `mcp` BackendKind version bump.** Touches a deliberately-fixed contract. Confirm before doing.
- **R2 — interface tags only from RHEA's rich path.** Arbitrary MCP tools can't do Tier-1 substitution → composer fallback. Acceptable?
- **R3 — local fallback decomposition is the one place local-LLM reasoning quality matters.** Bound it hard (LoopController depth, cost cap) and require first-class "cannot solve" output (§6.4) so failures are loud, not fabricated.
- **R4 — handle lifecycle/GC** for ProxyStore-backed results across a session.
- **R5 — context budget.** Many tool-call rounds consume external-LLM context; handles + previews keep payloads out, `apecx_context` re-orients after drops.

---

## 12. Relationship to the task graph

`implementation_task_graph.md` was **rewritten** on 2026-05-19 against this design. The
prior 165-task graph planned an unbuilt server-side multi-agent orchestrator (tiered
orchestrators, retrieval agents, layered-reasoning `list[Layer]` output, autonomy service);
that direction is dropped and preserved only in git history. The rewrite reframes the
shipped nanobrain primitives as a **reusable toolkit** (not a planned gap list), keeps the
RHEA UTD work (`T-RH-*`) and the composer/plan-lowering/skeleton machinery as reused
components, and expands the six §10 deltas into `EO-*` tasks.
