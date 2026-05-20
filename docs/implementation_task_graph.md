# APECx Implementation Task Graph

**Status:** Active (rewritten 2026-05-19).
**Supersedes:** the pre-2026-05-19 multi-agent-orchestrator task graph (165 tasks, 4 tracks).
That graph planned a server-side, local-LLM tiered orchestrator that was never built; it is
preserved in git history. This graph tracks the **external-orchestration** direction.
**Design source of truth:** `external_orchestration_design.md`.
**Audience:** implementers (apecx-mcp-integration, nanobrain, Rhea fork), reviewers, leads.
**Authoritative for:** file-level work, ownership, dependencies, definition of done.

---

## 1. Purpose and reading guide

`external_orchestration_design.md` says **what** to build and **why**. This document says
**which files, in what order, and when each task is done**. Architecture: a powerful
**external** LLM orchestrates over MCP; workflows are deterministic nanobrain scaffolds; a
**local** LLM decomposes a subtask into sub-workflows only as a bounded fallback when no
single workflow matches.

The design is ~85% assembly of already-built components (§2). New work is the six deltas
in `external_orchestration_design.md §10`, expanded into tasks below.

**Effort buckets:** S (≤1 day), M (1–3 days), L (3–7 days), XL (>1 week). Single-engineer,
±50% variance. **Cite the task ID (`EO-NN` / `T-RH-NN`) in commit bodies.**

Every task ships the workspace-mandatory bundle: a reproducible failure, the fix, recorded
verification (command + output), and — for any "done" component — a real-data integration
test (no mock-only coverage; see `../CLAUDE.md` mocks carve-out).

---

## 2. Reusable shipped toolkit (context — NOT tasks)

Verified built on 2026-05-19. Do **not** rebuild these; reuse them. Full framework gap
catalog (G1–G44) and contracts live in `CONTRACTS.md`.

| Built component | Path | Reused for |
|---|---|---|
| `UnifiedToolDescriptor` (UTD) | `nanobrain/core/unified_tool_descriptor.py` | Common-denominator tool record; `BackendKind` includes `rhea_mcp` (`CONTRACTS.md#td-vocab`) |
| `ExecutionPlan` / `PlanLoweringStep` / `Skeleton` / `SkeletonLoaderStep` | `nanobrain/library/orchestration/` | `compose_workflow` + local sub-workflow decomposition |
| `LoopController` | `nanobrain/library/steps/loop_controller.py` | Bounds local recursive decomposition |
| `ProvenanceContext` (G4) + step events (G37) | `nanobrain/core/provenance.py`, `nanobrain/core/step_events.py` | Execution visibility (`CONTRACTS.md#g4`) |
| `CostEnvelope` + capabilities | `nanobrain/core/cost_envelope.py`, `nanobrain/core/capabilities.py` | Bounds + gating on local recursion (`CONTRACTS.md#hitl-gate-*`) |
| `determinism_class` | in UTD | "Deterministic relative to versions" (`CONTRACTS.md#hpc-determinism`) |
| `rag_e2e_synthesis` pipeline (→ markdown) | `src/apecx_integration/composition/workflows/rag_e2e_synthesis/` | Reference single workflow; markdown output channel |
| `workflow_registry.py` + discovery tools | `src/apecx_integration/mcp_surface/workflow_registry.py`, `mcp_surface/tools/discovery.py` | The workflow catalog |
| Composer | `src/apecx_integration/composition/composer.py` | `compose_workflow`; substitution Tier 3 |
| RHEA adapter + workflows | `nanobrain/library/tools/_mcp_transport.py`, `RheaMCPDiscovery`/`RheaAdapter`, `src/.../workflows/rhea_muscle_alignment/`, `open_rosalind_rhea/` | Deterministic tool execution; first ingested tools |
| 23 MCP tools | `src/apecx_integration/mcp_surface/tools/` | Repointed/retired by the new surface (`EO-07`) |

**Not reused (retired direction):** the server-side tiered orchestrator, layered-reasoning
`list[Layer]` output, autonomy service. See git history of this file.

---

## 3. Track B1 — MCP workflow-object surface

The external LLM is the orchestrator; it needs workflow-as-object primitives. Reuse
`workflow_registry.py` + discovery; shape the rest around the built pipeline.

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| EO-01 | `list_workflows(category?, query?)` — RAG over the registry; returns names + 1-line descriptions + match score + maturity | `mcp_surface/tools/workflows.py` (extend); reuse `workflow_registry.py` + apecx-rag | — | M | Integration test against the real catalog returns ranked results; descriptions kept to 1 line (context budget) |
| EO-02 | `inspect_workflow(name, depth?)` — recursive resolution of the workflow YAML tree (`class:`/`config:`), lazy depth | `mcp_surface/tools/workflows.py` (extend) | — | M | Unit: recursion + depth cap; integration: a real nested workflow renders tools+params |
| EO-03 | `run_workflow(name, params)` → `WorkflowResult` (§Track B2) | `mcp_surface/tools/workflows.py` (extend) | EO-10 | M | Integration: a real workflow returns the envelope + a `run_id` |
| EO-04 | `inspect_run(run_id, detail?)` — G4 records + effective-config run-summary + logs | `mcp_surface/tools/workflows.py` (extend) | EO-42 | M | Integration: run then inspect; summary reflects effective (post-override) config |
| EO-05 | `apecx_context()` — session re-orientation (runs, handles, derived workflows this session); scoped, not general memory | `mcp_surface/tools/context.py` (new) | EO-10 | M | Integration: multi-call session; context reflects history |
| EO-06 | `compose_workflow(intent, base?)` — wrap the built composer; `base` set ⇒ substitution (Track B4) | `mcp_surface/tools/workflows.py` (extend); reuse `composition/composer.py` | — | M | Greenfield: composer canary stays green; output persistent + registered (maturity=`experimental`) |
| EO-07 | Repoint/retire the 23 legacy RPC tools to the workflow-object surface | `mcp_surface/tools/*`, `mcp_surface/server.py` | EO-01..06 | M | Golden-file of the exposed surface; domain tools (`eeev_epitope_analysis`, `viral_immunology_analysis`) become workflows discoverable via EO-01 |

---

## 4. Track B2 — Output contract + handles

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| EO-10 | `WorkflowResult` envelope: `markdown`, `data_handle`, `data_preview`, `run_id`, `status`; Pydantic `extra='forbid'` | `composition/schemas/workflow_result.py` (new) | — | S | Unit: validation + typo rejection |
| EO-11 | Handle store + lifecycle (creation, retrieval-by-id, GC/expiry); reuse ProxyStore Key I/O | `composition/handles/store.py` (new); reuse ProxyStore | EO-10 | M | Unit: store/retrieve round-trip; GC policy tested |
| EO-12 | Canonical data shapes behind handles: `RecordSet` / `Evidence` / `Bundle` / `Artifact` | `composition/schemas/data_shapes.py` (new) | EO-10 | S | Each shape (de)serializes; cross-workflow typing enforced |
| EO-13 | Generalize `rag_e2e_synthesis` to emit `WorkflowResult` (markdown + handle) | `composition/workflows/rag_e2e_synthesis/` (extend) | EO-12 | M | **Key test:** workflow A handle → `run_workflow(B, {input: handle})`; structured data never enters LLM context |

---

## 5. Track B3 — Local bounded decomposition (the fallback layer)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| EO-20 | Decomposition step: match-single-workflow-first (RAG + threshold); else decompose into sub-workflows via composer/plan-lowering/skeleton | `composition/steps/local_decomposition.py` (new); reuse `plan_lowering_step`, `skeleton`, composer | EO-01, EO-06 | L | Integration: a subtask with a single match dispatches it; a non-matching decomposable subtask splits + dispatches |
| EO-21 | Bounds: `LoopController` depth/repeat caps + `CostEnvelope` ceiling + capability gating | `composition/steps/local_decomposition.py` (extend); reuse `loop_controller`, `cost_envelope`, `capabilities` | EO-20 | M | Unit: depth cap halts; cost breach raises `CostEnvelopeBreach`; repeated-match guard fires |
| EO-22 | First-class "cannot solve" result (loud, not fabricated) when no match and not decomposable | `composition/steps/local_decomposition.py` (extend) | EO-20 | S | Returns `status=error` + reason; never fabricates an answer |
| EO-23 | Integration test: a query requiring decomposition into ≥2 sub-workflows, end to end | `tests/integration/test_local_decomposition.py` (new) | EO-22 | M | Real run produces a non-None integrated result with the expected shape (G99 cycle-bearing rule) |

---

## 6. Track B4 — Tool ingestion + tiered substitution

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| EO-30 | **Framework change:** add generic `mcp` to UTD `BackendKind` (currently fixed `{local_python, local_parsl, http, rhea_mcp}`) — version bump + catalog sweep per `CONTRACTS.md#td-vocab` | `nanobrain/core/unified_tool_descriptor.py`; `CONTRACTS.md#td-vocab` (lockstep) | — | M | Vocab bump documented; existing UTDs sweep-validated; **flagged decision (design R1) — confirm before doing** |
| EO-31 | Generalize `RheaMCPDiscovery`/`RheaAdapter` to arbitrary MCP servers via `MCPTransport` | `nanobrain/library/tools/` (extend) | EO-30 | M | Integration: ingest tools from a non-RHEA MCP server as UTDs |
| EO-32 | Extract interface tags from RHEA's rich in-process path (Galaxy metadata lost on the MCP wire) | depends on `T-RH-02` (`utd_producer.py`) | T-RH-02 | M | UTDs for RHEA tools carry an interface tag (e.g., `multiple-alignment`) |
| EO-33 | Substitution **Tier 1** — deterministic config-swap gated on interface-tag match; produces a persistent derived workflow | `composition/substitution/tier1_config_swap.py` (new) | EO-32, EO-06 | M | Integration: swap MUSCLE→MAFFT on a real alignment workflow; new workflow runs, output shape unchanged |
| EO-34 | Substitution **Tier 2** (declared mapping table) + **Tier 3** (composer rebridge; design R2 fallback for untagged tools) | `composition/substitution/` (extend) | EO-33 | M | Integration: a mapped cross-tool swap (Tier 2) + an incompatible swap via composer (Tier 3) each produce a working workflow |

---

## 7. Track B5 — Provenance wiring (reuse, don't build)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| EO-40 | Activate G4 `ProvenanceContext` at the workflow entry point | `composition/runtime/provenance_wiring.py` (new); reuse `nanobrain/core/provenance.py` | — | M | Every step in a real run produces a record (incl. failures); G4 default redaction applied |
| EO-41 | Subscribe to G37 step events for execution order/status | `composition/runtime/provenance_wiring.py` (extend); reuse `step_events.py` | — | S | Subscriber buffers events; order/status reconstructable; subscriber failure never breaks the run |
| EO-42 | Effective-config run-summary (resolved tools+params post-override, data-source versions, status) — the one genuinely missing piece | `composition/runtime/run_summary.py` (new) | EO-40 | M | Run with an override → summary shows the effective config + the delta from canonical |
| EO-43 | Redaction reuse for sensitive params (keys/creds/home paths) | `composition/runtime/run_summary.py` (extend); reuse G4 redaction | EO-42 | S | A secret-bearing param is redacted in the summary |

---

## 8. Track C — Rhea fork (tool platform)

Carried over from the prior graph; still relevant under the reuse direction. The fork lives
at `apecx-cowork/rhea/` and already has an `apecx_utd_extension` (audited 2026-05-19).

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| T-RH-01 | `apecx_utd_extension` module in the fork (exists — verify + document) | `rhea/rhea/extensions/apecx_utd_extension/` | — | S | Module loads; no behavior change to baseline endpoints |
| T-RH-02 | UTD producer — emit a UTD (with interface tag, §EO-32) per registered tool | `rhea/rhea/extensions/apecx_utd_extension/utd_producer.py` | T-RH-01 | M | Every catalog tool has a UTD matching `CONTRACTS.md#td-vocab` |
| T-RH-03 | UTD discovery endpoint `GET /apecx/utds` | `rhea/rhea/server/routes/apecx_utd.py` | T-RH-02 | M | Paginated UTD list; `?descriptor_id=` query |
| T-RH-04 | UTD-aware invocation `POST /apecx/invoke` → ProxyStore keys | `rhea/rhea/server/routes/apecx_utd.py` (extend) | T-RH-03 | M | Matches existing `/invoke` semantics with UTD ref; SSE preserved |
| T-RH-05 | ProxyStore namespace cooperation — prefix returned keys with `run_<run_id>/` | `rhea/rhea/proxystore/namespace.py` | T-RH-04 | M | `X-Apecx-Run-Id` header prefixes keys; integration test |
| T-RH-06 | Per-invocation provenance JSONL (cooperates with EO-40) | `rhea/rhea/extensions/apecx_utd_extension/provenance.py` | T-RH-04 | M | Each invocation writes the required-fields JSONL line |
| T-RH-09 | Fork-side e2e: discover → invoke via UTD → resolve keys → provenance | `rhea/tests/integration/test_apecx_utd_e2e.py` | T-RH-06 | M | End-to-end; smoke + real-Redis variants |

---

## 9. Track D — Cross-track integration (shipping milestones)

Every XT task ends with a recorded run against real data.

| ID | Task | Depends on | Effort | DoD |
|---|---|---|---|---|
| XT-01 | Surface smoke: external LLM `list_workflows` → `run_workflow` → markdown answer | EO-07, EO-13 | S | Real query through an MCP client returns grounded markdown; daily CI |
| XT-02 | Handle chaining: workflow A → handle → workflow B, no data through LLM context | EO-13 | M | Real two-workflow chain; structured payload stays out of context |
| XT-03 | Local decomposition e2e: a query that needs ≥2 sub-workflows | EO-23 | M | Integrated result non-None + expected shape |
| XT-04 | RHEA tool execution e2e via UTD | EO-31, T-RH-09 | M | Real RHEA tool call; provenance + ProxyStore verified |
| XT-05 | Tiered substitution e2e: MUSCLE→MAFFT (Tier 1) + an incompatible swap (Tier 3) | EO-34 | M | Both produce working, runnable derived workflows |
| XT-06 | Provenance e2e: run → `inspect_run` shows effective tools+params | EO-04, EO-42 | S | Scientist-facing "what ran with what params" view verified |

---

## 10. Definition of "task complete"

A task is complete when ALL hold:
1. **Code lands** — all files in the `Files` column committed.
2. **Tests pass** — unit (where listed) + the integration test in DoD. No mocks for behaviors an integration test verifies (`../CLAUDE.md` mocks carve-out).
3. **Recorded verification** — exact test command + abbreviated output in the commit body or PR.
4. **Cross-references updated** — if the task ships something other docs reference as forthcoming, update them in the same PR.
5. **Reviewer sign-off** — `.claude/agents/review-gate.md` checklist passes.

NOT complete if: tests skipped without recorded justification; mocks substitute for integration tests; integration run against synthetic data; or the PR claims behavior the test output doesn't show.

---

## 11. Critical path

```
EO-10 (WorkflowResult, S)
  → EO-13 (rag_e2e emits envelope, M) → XT-01, XT-02
EO-01/02 (list/inspect, M) + EO-06 (compose, M)
  → EO-20 (local decomposition, L) → EO-21/22/23 → XT-03
EO-30 (mcp BackendKind, M — FLAGGED) → EO-31 (generalize adapter, M)
T-RH-02 (UTD producer, M) → EO-32 (interface tags, M) → EO-33/34 (substitution) → XT-05
EO-40 (G4 wiring, M) → EO-42 (run-summary, M) → EO-04 (inspect_run) → XT-06
```

MVP slice (validate the architecture before scaling): EO-10 + EO-13 + EO-01/02/03 +
one real query through an external LLM returning markdown + handle. ~3–4 weeks.

---

## 12. Cross-references

| Resource | Location | Used for |
|---|---|---|
| Design source of truth | `external_orchestration_design.md` | What/why this graph builds |
| Design contracts (G1–G44, UTD, output, autonomy, HPC) | `CONTRACTS.md` | Anchored contracts cited from code; the framework gap catalog |
| Canonical architecture map | `architecture.md` | End-to-end topology, MCP tools, invocation paths |
| Master design index | `_design_index.md` | Doc navigation |
| Active workarounds | `WORKAROUND_INVENTORY.md` | Per-ship workaround tracking |
| Workspace policy | `../CLAUDE.md` | Mocks carve-out, three-attempt rule, integration discipline |
| nanobrain rules | `../nanobrain/CLAUDE.md` | from_config-only, process()-not-execute(), no hardcoded prompts |

*(Cross-references to docs deleted in the 2026-05-11 consolidation — `development_roadmap.md`,
`tool_descriptor_contract.md`, `agent_workflow_authoring.md`, `hpc_reproducibility_spec.md`,
`data_layer_evolution.md`, `deployment_architecture.md`, `security_threat_model.md`,
`nanobrain_capability_gaps.md`, `multiagent_architecture.md` — were removed in this rewrite;
their content lives in `CONTRACTS.md`.)*

---

## 13. Open questions

1. **`mcp` BackendKind version bump (EO-30).** Touches the deliberately-fixed `#td-vocab`. Confirm before doing.
2. **Interface tags only from RHEA's rich path (EO-32).** Arbitrary baseline-MCP tools → Tier-3 substitution only. Acceptable?
3. **Handle GC policy (EO-11).** Session-scoped TTL vs. explicit release?
4. **Composer-authored workflow maturity.** New `compose_workflow` outputs default to `experimental`; what gates promotion to `validated`?
