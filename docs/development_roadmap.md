# APECx Development Roadmap — Phased Delivery Plan

**Status:** Active design reference
**Audience:** Project leads, engineers, external collaborators
**Updates:** Amend this document when open questions are resolved or priorities shift.
**Supersedes:** The component table in `multiagent_architecture.md §10.2` (this doc is
authoritative for sequencing; that doc remains authoritative for component contracts).

---

## 1. Orientation

The roadmap implements the architecture defined across four design documents:

| Document | What it defines |
|---|---|
| `docs/multiagent_architecture.md` | Four-tier component architecture, agent inventory |
| `docs/workflow_output_contract.md` | What every workflow must produce |
| `docs/nanobrain_workflow_design.md` | How nanobrain implements the contract |
| `docs/external_tool_integration.md` | Rhea and GalaxyMCP integration |

The roadmap is organized in phases. Each phase has:
- A **single deployable milestone** (the system is more capable after the phase than before)
- An **integration test requirement** (no phase is complete without a test against real data)
- A set of **open questions** that must be resolved before implementation starts

---

## 2. Current State Baseline

The system today (`docs/architecture.md`) provides:
- 23 flat MCP tools with no routing layer
- `synthesize_query` (RAG synthesis over configured local data sources and literature)
- `query_*` direct-lookup tools for configured local databases
- HITL approval tools
- HPC bundle export/ingest
- Synonym dictionary (nanobrain workflow build)
- PBS bundle generation (manual qsub)

What today's system cannot do:
- Cross-source reasoning (limited to locally indexed sources)
- Multi-source evidence accumulation with cross-source concordance
- Dynamic query decomposition (no intent classifier, no orchestrators)
- Conversational chaining (no session state)
- External tool execution (no Rhea, no Galaxy)
- Design-type workflows (no hypothesis generation, no tournament)
- HPC-ready provenance bundles connected to workflows

---

## 3. Phase 0 — Foundation Layer

**Milestone:** A user can ask any question; the system routes it to the right
retrieval path and returns a grounded answer using at least one data source.

**Deliverables:**

| Component | Description | Depends on |
|---|---|---|
| `DataAccessInterface` | Abstract base class for all data source access; two concrete implementations | None |
| `SphericalAdapter` | Implements `DataAccessInterface` against Spherical REST API | `DataAccessInterface` |
| `GlobusAdapter` (stub) | Interface-compliant stub; logs calls; returns empty results | `DataAccessInterface` |
| `IntentClassifier` | LLM-based classifier; maps query → orchestrator intent | `APECX_LLM_*` env vars |
| `GenericQueryOrchestrator` | Single-source lookup path; migrates existing 23-tool behavior | `DataAccessInterface`, `IntentClassifier` |
| `CanonicalEntityResolver` (unified) | Replaces 3 current implementations; single `resolve()` API | Existing synonym dict |
| `ask` MCP tool | Accepts query; returns task ID; routes to classifier + orchestrator | `IntentClassifier`, `GenericQueryOrchestrator` |
| `status` MCP tool | Polls or streams task progress | Control plane |

**Integration test requirement:**
A real user question submitted via `ask` must produce a grounded answer within 60 seconds
using the existing configured data sources. The `GenericQueryOrchestrator` path must be
tested end-to-end against the configured data API with at least two different source types.

**Framework gap dependencies (Phase 0):** None. Phase 0 uses only existing
nanobrain primitives (`BaseStep`, `Workflow`, `DataUnitMemory`, `DirectLink`,
`Agent`). It is deliberately structured to ship before any framework gap fix.

**Open questions blocking Phase 0:**

1. **Data API access credentials.** The `SphericalAdapter` requires Spherical API
   credentials. Are these available in the development environment?

2. **Intent classifier LLM.** Should the classifier use the same `APECX_LLM_*` backend
   as synthesis, or a separate smaller model? The synthesizer uses `mistral-nemo:latest`
   in development. Confirm whether that is appropriate for classification latency.

3. **`ask` tool return shape.** Should `ask` return immediately with a task ID (async),
   or block until the answer is ready (sync)? The async form requires the control plane
   to store task state; the sync form is simpler but has latency implications for long
   multi-source queries.

---

## 4. Phase 1 — Full Retrieval Layer

**Milestone:** A user's question that spans multiple data sources returns evidence from
all relevant sources with cross-source concordance and entity resolution.

**Deliverables:**

| Component | Description | Depends on |
|---|---|---|
| Retrieval agents (per source) | One `RetrievalAgent` per configured data source | `DataAccessInterface` |
| `EvidenceAccumulationStep` | Extended bundle aggregating all active retrieval agents | All retrieval agents |
| `CrossSourceIntegrationStep` | Entity resolution across sources, concordance scoring | `EvidenceAccumulationStep`, `CanonicalEntityResolver` |
| `DomainOrchestrator` (primary) | Tier-1 orchestrator for the primary domain query type | All retrieval agents |
| `StructuralOrchestrator` | Tier-1 orchestrator for 3D/structural queries | Structural source agents |

**Integration test requirement:**
A multi-source query must produce a `FinalResponse` that includes findings from at least
4 distinct data sources. The test must run against real data via the configured API.
Cross-source entity resolution must be demonstrably active (canonical IDs must appear
across ≥2 layer results).

**Framework gap dependencies (Phase 1):**

| Gap | Why Phase 1 needs it | Workaround if not yet shipped |
|---|---|---|
| **G6** typed result schemas | Per-source retrieval results must conform to a typed `LayerResult` schema rather than free-form dicts | Hand-rolled Pydantic models in apecx-mcp |

Phase 1 is mostly orchestration glue and does not need the framework gaps to ship.

**Open questions blocking Phase 1:**

4. **Globus migration timeline.** All Phase 1 retrieval agents are tested against the
   Spherical API. When does the Globus Search endpoint become testable? This determines
   when `GlobusAdapter` moves from stub to real implementation.

5. **Orchestrator granularity.** Should there be one primary domain orchestrator or two
   (e.g., one for retrieval-type queries, one for design-type queries)? The granularity
   affects how many P0 orchestrators to build and how the intent classifier routes between
   them.

---

## 5. Phase 2 — Nanobrain Workflow Patterns

**Milestone:** The `LayeredReasoningWorkflow` (defined in `nanobrain_workflow_design.md`)
is fully implemented and passes the workflow output contract for multi-turn conversations.

**Deliverables:**

| Component | Description | Depends on |
|---|---|---|
| `Phase0PlanningStep` | Intent + data readiness, produces ExecutionPlan | `IntentClassifier` |
| `SequenceLayerStep` | Sequence source retrieval + alignment tool invocation | `DataAccessInterface`, Rhea (Phase 3) |
| `StructuralLayerStep` | Structure source retrieval + analysis tool invocation | `DataAccessInterface`, Rhea (Phase 3) |
| `FunctionalLayerStep` | Experimental source retrieval | `DataAccessInterface` |
| `EvidenceLiteratureLayerStep` | Literature + domain knowledge base retrieval | Existing harvester steps |
| `ResponseSynthesisStep` | Extended `RagSynthesisStep` with full evidence bundle | `EvidenceAccumulationStep` |
| `FollowupGenerationStep` | Three data-grounded follow-up questions | `ResponseSynthesisStep` |
| Session state store | `SessionContext` persisted in control plane | Control plane |
| `LayeredReasoningWorkflow` YAML | Full DAG with ConditionalLinks | All above steps |

**Note on tool invocations in Phase 2:** Layer steps that invoke tools are scaffolded
in Phase 2 with **no-op stubs** for the tool calls. Real tool execution via Rhea is
wired in Phase 3. Phase 2 verifies the workflow DAG, data flow, and evidence accumulation
logic using only retrieval results.

**Integration test requirement:**
A two-turn conversation (turn 1 = retrieval query, turn 2 = follow-up design question
drawn from turn 1's follow-up list) must demonstrate session context reuse: data retrieved
in turn 1 must not be re-retrieved in turn 2. The test verifies
`SessionContext.accumulated_evidence` contains the turn 1 layer results that turn 2 reuses.

**Framework gap dependencies (Phase 2):** This is the gap-heavy phase.

| Gap | Why Phase 2 needs it | Workaround if not yet shipped |
|---|---|---|
| **G1** declarative ConditionalLink predicate DSL | LLM-safe synthesis of layer-gating predicates | Hand-authored callable predicates in apecx-mcp; LLM never authors the predicate |
| **G2** dynamic `AllDataReceivedTrigger.expected_set_source` | EvidenceAccumulationStep must wait for only the *active* layer set, not all possible layers | Each gated layer publishes a sentinel "empty bundle" so the trigger fires; per-layer boilerplate carried in apecx-mcp |
| **G7** DirectLink `auto_transfer` default flip | Eliminates the silent-failure shape | Every DirectLink in apecx-mcp YAMLs must declare `auto_transfer: true` explicitly; lint rule enforces this |
| **G10** gate-to-bottom semantics for ConditionalLink + AllDataReceived | Prevents trigger deadlock when all layers are gated off | apecx-mcp's Phase0PlanningStep refuses to emit empty `active_layers` |
| **G14** `PromptTemplate` primitive | Phase 0 planning prompt loaded from YAML carrier | Hand-rolled prompt files at `composer_prompts/` (current pattern) |
| **G16** `ExecutionPlanConfig` + `ExecutionPlanDataUnit` | Typed Phase 0 output rather than dict | Hand-rolled `pydantic.BaseModel` + `DataUnitMemory` in apecx-mcp |
| **G18** `LoopController` step | Repair-loop primitive used by `agent_workflow_authoring.md §7` | apecx-mcp implements the back-edge with a custom step + ConditionalLink |

**Open questions blocking Phase 2:**

6. **`AllDataReceived` trigger configuration.** Resolved via gap **G2**
   (`expected_set_source: workflow.execution_plan` +
   `expected_set_field: active_layers`). Until G2 ships, apecx-mcp uses the
   "publish sentinel empty bundle" workaround listed in the gap-dependencies
   table above. **The migration to G2 should happen before any new layer
   types are added** because the workaround scales linearly with layer count.

7. **ConditionalLink implementation.** Resolved via gap **G1** (declarative
   predicate DSL). Today's callable predicate is sufficient for hand-authored
   YAML but not for agent-authored YAML. Phase 2 ships with hand-authored
   predicates; agent-authored predicates wait for G1.

---

## 6. Phase 3 — External Tool Execution

**Milestone:** A workflow can invoke computational tools via Rhea and incorporate their
results into the evidence bundle with full provenance.

**Deliverables:**

| Component | Description | Depends on |
|---|---|---|
| `RheaToolAgent` | Wraps Rhea MCP HTTP+SSE; handles discovery + invocation | Running Rhea service |
| `GalaxyToolAgent` | Wraps GalaxyMCP (conditional; see open questions) | GalaxyMCP service |
| ProxyStore client | Uploads/resolves large data via ProxyStore (Redis) | APECX_RHEA_PROXYSTORE_URL |
| Wire tool stubs → Rhea | Replace no-op tool stubs in layer steps with real Rhea calls | `RheaToolAgent` |
| `ToolExecutionOrchestrator` | Routes tool requests to Rhea/Galaxy/local | `RheaToolAgent` |

**Integration test requirement:**
`SequenceLayerStep` must invoke a computation tool via Rhea on a real retrieved dataset
and return a `LayerResult` with `tool_outputs` containing the alignment provenance key.
The ProxyStore key must resolve to the output file. No mocks.

**Framework gap dependencies (Phase 3):**

| Gap | Why Phase 3 needs it | Workaround if not yet shipped |
|---|---|---|
| **G3** `DataUnitProxyRef` | HPC-scale tool I/O cannot ride a Python dict between steps | apecx-mcp uses link-level `proxystore_enabled: true` for transport (defers full storage-layer fix to G3) |
| **G4** step-level provenance threading | Every tool invocation must produce a provenance record per `external_tool_integration.md §6.2` | apecx-mcp wraps every tool call in a custom recorder step |
| **G11** tool-step taxonomy | `ToolExecutionStep` base class with declared cost / capability surface | apecx-mcp implements ToolExecutionStep as an apecx-mcp BaseStep; promote when G11 ships |
| **G13** multi-tenant ProxyStore namespacing | Per-run namespace isolation in shared ProxyStore Redis | apecx-mcp prefixes keys with `<run_id>/`; promote when G13 ships |
| **G15** `UnifiedToolDescriptor` primitive | UTD as a framework primitive (per `tool_descriptor_contract.md §2`) | Hand-rolled Pydantic `UTD` model in apecx-mcp |

**Open questions blocking Phase 3:**

8. **Rhea deployment model.** Does Rhea run as a sidecar in the same Docker Compose stack
   as `apecx-mcp`, or as a standalone service? If sidecar, how does `apecx-mcp` health-check
   Rhea at startup? If standalone, what are the service discovery requirements?

9. **ProxyStore backend.** Rhea uses Redis for ProxyStore. Does APECx deploy its own Redis
   instance, share Rhea's Redis, or use a different ProxyStore connector? This affects
   data lifetime management (who expires keys, when).

10. **GalaxyMCP availability.** Blocked until Galaxy provides a testable MCP endpoint.
    `GalaxyToolAgent` implementation is deferred until this is confirmed.

---

## 7. Phase 4 — Hypothesis Tournament and HPC-Ready Bundles

**Milestone:** Design-type queries route through the tournament step, surface top-N
hypotheses for HITL approval, and export PBS bundles that are reproducible and
self-contained.

**Deliverables:**

| Component | Description | Depends on |
|---|---|---|
| `DesignLayerStep` | Domain-specific design and optimization layer | Phase 3 tool execution |
| `HypothesisTournamentStep` | Parallel proposer agents + evidence-scored ranking | Phase 2 workflow, Phase 3 tools |
| Proposer agents (configurable N) | Domain-specific analytical lens agents | Agent YAML configs |
| `HITLGateStep` | Surfaces top-N to MCP `approve`/`reject`/`correct` tools | Existing approval tools |
| `HPCBundleExportStep` | ProxyStore key resolution + PBS bundle generation | Phase 3 ProxyStore |
| Bounded history tracking | Session-scoped proposer history for adaptive refinement | Session state (Phase 2) |

**Integration test requirement:**
A design-type query must complete end-to-end: configurable proposer agents produce
hypotheses, the tournament ranks them, the HITL gate surfaces the top 3, and after
approval the system generates a valid PBS bundle. The bundle must be inspectable:
`workflow.yml`, `inputs/hypothesis.json`, and `provenance_seed.json` must all be
present and internally consistent.

**Framework gap dependencies (Phase 4):**

| Gap | Why Phase 4 needs it | Workaround if not yet shipped |
|---|---|---|
| **G5** `WorkflowCheckpoint` / `ResumeStep` | Long-running tournament runs need to checkpoint/resume across operator-paused HITL gates | apecx-mcp persists tournament state in the control plane DB; resume reconstructs from DB |
| **G12** declarative resource envelope on a Step | HypothesisTournamentStep declares per-proposer cost ceiling | Hand-rolled cost-cap dict in step config |
| **G17** `PlanLoweringStep` + `SkeletonLoaderStep` | Skeleton-based authoring (per `agent_workflow_authoring.md §4-§5`) | apecx-mcp implements these as apecx-side BaseStep subclasses; promote when G17 ships |
| **G19** `SignedConfig` loader | HPC bundles ship signed YAML for tamper-evident replay | Bundle exporter produces a detached signature file; loader is operator-trusted |

**Open questions blocking Phase 4:**

11. **Evidence scoring function.** The tournament ranker uses: `confidence × evidence_coverage`
    with an orthogonality diversity penalty. Is this the right function, or should the
    ranking be LLM-judged? LLM judging adds latency and cost; confidence × coverage is
    deterministic and fast. **Confirm scoring approach before implementing tournament.**

12. **Tournament with missing proposers.** If a proposer agent fails, the tournament
    proceeds with the remaining proposers (graceful degradation). What is the minimum
    viable number of proposers? One proposer alone bypasses the tournament and goes
    directly to synthesis — confirm whether that is acceptable or should surface a
    capability gap.

---

## 8. Phase 5 — Globus Migration and Production Readiness

**Milestone:** `SphericalAdapter` is replaced by `GlobusAdapter` via config change only.
No changes to any retrieval agent or orchestrator.

**Deliverables:**

| Component | Description | Depends on |
|---|---|---|
| `GlobusAdapter` | Full implementation replacing stub from Phase 0 | Globus endpoint testable |
| `GlobusSearchRetrievalAgent` | Existing tool wrapped as a proper retrieval agent | `GlobusAdapter` |
| Remove `SphericalAdapter` | Once Globus is stable and tested | `GlobusAdapter` integrated |

**Integration test requirement:**
All Phase 1 integration tests must pass with `APECX_DB_BACKEND=globus`. No retrieval
agent may be modified. Config swap is the only change.

**Framework gap dependencies (Phase 5):**

| Gap | Why Phase 5 needs it | Workaround if not yet shipped |
|---|---|---|
| **G8** `Workflow.process()` await semantics clarification | Production deployments need explicit await rather than fire-and-forget | apecx-mcp wraps every `Workflow.process()` call in an explicit `wait_for_cascade()` |
| **G9** first-class skeleton primitive | Production skeleton catalog needs framework-level versioning + content-addressing | apecx-mcp ships its own skeleton catalog; promote when G9 ships |
| **G20** `class:` path import whitelist | Production-mode YAML loaders must reject any `class:` outside an operator-approved allowlist (per `security_threat_model.md §5 T-PI-3`) | apecx-mcp implements a wrapping loader that performs the check before calling `Workflow.from_config()` |

**Open question blocking Phase 5:**

4 (from Phase 1): **Globus migration timeline.** This phase cannot start until the Globus
Search endpoint supports the same query surface as the Spherical API.

---

## 8.5 Phase 6 — Autonomous Operation (parallel to Phases 4/5)

**Milestone:** A scheduled / event-triggered / queued-task autonomous task
runs end-to-end through the meta-workflow orchestrator, pauses for HITL via
the deferred-HITL channel when configured, and reports through the
operator-facing MCP audit tools.

**Note on phase numbering:** Phase 6 is logically parallel to Phases 4 and
5, not strictly after them. Phase 6 depends on Phase 2 (the meta-workflow
orchestrator code) and on Track A G21 + G22; it does NOT depend on Phase 4
(tournament workflows) or Phase 5 (Globus migration). A team may ship
Phase 6 between Phase 2 and Phase 3 if autonomous operation matters more
than tool execution for their use case.

**Deliverables:**

| Component | Description | Depends on |
|---|---|---|
| `WorkflowRunner` (G21) | Detached / long-running run with heartbeat, persistence, and resume | NB-G21-* |
| `WorkflowEntryTrigger` + `EventTrigger` (G22) | Schedule / event / queue triggers that start fresh workflow runs | NB-G22-* |
| `autonomous_task` + `autonomous_task_run` tables | Control-plane persistence for multi-session task identity | MC-AU-01 |
| `apecx-cp serve --role autonomous` service | Long-lived service running the WorkflowRunner | MC-AU-02 |
| Five autonomy MCP tools | `start_autonomous_task` / `list_autonomous_tasks` / `pause_autonomous_task` / `cancel_autonomous_task` / `show_autonomous_audit` | MC-AU-04 |
| Cost envelope enforcement + per-deployment-per-day ceiling | Runaway-autonomy protection (T-AU-1 mitigation) | MC-AU-05 |
| Deferred-HITL fields on `Approval` model | A2U via the existing approvals table | MC-AU-06 |

**Framework gap dependencies (Phase 6):**

| Gap | Why Phase 6 needs it | Workaround if not yet shipped |
|---|---|---|
| **G21** WorkflowRunner | Long-running detached workflow runs; survives caller exit | apecx-mcp implements a custom runner without G5 checkpoint integration; tasks fail closed on restart |
| **G22** WorkflowEntryTrigger / EventTrigger | Schedule and event triggers that start fresh workflows | apecx-mcp implements a separate scheduler service that calls the existing `start_workflow` MCP tool synchronously |
| **G14** PromptTemplate | Deferred-HITL request body is composed via a PromptTemplate (T-AU-2 mitigation requires content_hash auditing) | Hand-rolled prompt files in apecx-mcp until G14 ships |

**Integration test requirement:** XT-11 — schedule-triggered autonomous
task pauses for HITL via deferred-HITL request; user responds via Claude
Desktop's `approve` MCP tool; task resumes and completes; audit log shows
full transition record (per `autonomous_workflow_agent.md §10`).

**Open questions blocking Phase 6:**

13. **Daemon vs. workflow framing for the autonomous loop.** Per
    `autonomous_workflow_agent.md §13 Q1`. **Working hypothesis:**
    workflow itself, using G18 LoopController + G22 trigger; resolve before MC-AU-02.
14. **Per-deployment-per-day cost ceiling.** Per `autonomous_workflow_agent.md §13 Q2`.
    Resolve before XT-11.
15. **Missed-schedule trigger replay policy.** Per `autonomous_workflow_agent.md §13 Q5`.
    Resolve before MC-AU-02.

---

## 9. Consolidated Open Questions

All open questions from phases above, for triage:

| # | Question | Blocking phase | Priority |
|---|---|---|---|
| 1 | Data API credentials in dev | Phase 0 | P0 — immediate |
| 2 | Intent classifier LLM choice | Phase 0 | P0 — immediate |
| 3 | `ask` tool async vs. sync | Phase 0 | P0 — immediate |
| 4 | Globus migration timeline | Phase 1 / Phase 5 | P1 |
| 5 | Orchestrator granularity (retrieval vs. design) | Phase 1 | P1 |
| 6 | `AllDataReceived` trigger with dynamic active-layer set | Phase 2 | P0 — must resolve before Phase 2 |
| 7 | ConditionalLink sufficiency check | Phase 2 | P0 — must resolve before Phase 2 |
| 8 | Rhea deployment model (sidecar vs. standalone) | Phase 3 | P1 |
| 9 | ProxyStore backend ownership | Phase 3 | P1 |
| 10 | GalaxyMCP availability | Phase 3 | P2 — deferred |
| 11 | Tournament scoring function | Phase 4 | P1 |
| 12 | Minimum viable proposer count | Phase 4 | P2 |
| 13 | Daemon vs. workflow framing for autonomous loop | Phase 6 | P0 — must resolve before Phase 6 starts |
| 14 | Per-deployment-per-day autonomy cost ceiling | Phase 6 | P0 — must resolve before XT-11 |
| 15 | Missed-schedule trigger replay policy | Phase 6 | P1 |

---

## 10. Success Criteria

A phase is complete when:

1. All listed deliverables are implemented and reviewed.
2. The integration test requirement passes against real data (no mocks for the tested paths).
3. The test command and its output are recorded in the commit body or PR description.
4. Open questions for the next phase are triaged (answered, deferred, or owner assigned).

A component is not complete if it has integration test coverage only via mocks. The
unit-mock / integration-test parity rule applies: any behavior verified by mock must
have a corresponding integration test against real data.

---

## 11. What This Roadmap Does Not Cover

The following are out of scope for this roadmap and require separate planning:

- **apecx-harvesters changes.** New data sources may require new harvesters if the Globus
  corpus does not already index them. This is a separate project within `apecx-harvesters/`.

- **nanobrain framework changes — historical context.** When this roadmap was first
  drafted, framework changes were declared out of scope. That position has been
  superseded: `nanobrain_capability_gaps.md` now catalogues 20 framework proposals
  (G1–G20), each with a per-phase dependency declared in the per-phase
  "Framework gap dependencies" tables above. The current roadmap is explicit
  about which gaps each phase depends on AND about the workaround apecx-mcp
  implements until each gap ships. Coordinating gap delivery with the nanobrain
  maintainer is now an explicit, scheduled activity rather than out of scope.

- **UI/Frontend.** A chatbot UI consuming the MCP tool surface is separate from this
  roadmap. It becomes relevant when Phase 0 is complete.

- **Production deployment.** Docker Compose, Kubernetes configs, secrets management,
  and monitoring are not roadmap items. They become relevant when Phase 2 is complete.

---

## 12. Framework Gap × Phase Map (consolidated)

A flat lookup of which gap is consumed in which phase. Use this to negotiate
gap-delivery sequencing with the nanobrain maintainer.

| Gap | Title | Priority | Consumed in |
|---|---|---|---|
| G1  | ConditionalLink predicate DSL                 | P0 | Phase 2 |
| G2  | Dynamic AllDataReceived expected_set          | P0 | Phase 2 |
| G3  | DataUnitProxyRef                              | P0 | Phase 3 |
| G4  | Step-level provenance threading               | P1 | Phase 3 |
| G5  | WorkflowCheckpoint / ResumeStep               | P1 | Phase 4 |
| G6  | Typed result schemas at framework boundary    | P0 | Phase 1 |
| G7  | DirectLink auto_transfer default flip         | P0 | Phase 2 |
| G8  | Workflow.process() await semantics            | P0 | Phase 5 |
| G9  | First-class skeleton primitive                | P0 | Phase 5 |
| G10 | ConditionalLink + AllDataReceived deadlock fix| P0 | Phase 2 |
| G11 | Tool-step taxonomy                            | P1 | Phase 3 |
| G12 | Declarative resource envelope on Step         | P1 | Phase 4 |
| G13 | Multi-tenant ProxyStore namespacing           | P1 | Phase 3 |
| G14 | PromptTemplate primitive                      | P1 | Phase 2 |
| G15 | UnifiedToolDescriptor primitive               | P0 | Phase 3 |
| G16 | ExecutionPlanConfig + DataUnit                | P0 | Phase 2 |
| G17 | PlanLoweringStep + SkeletonLoaderStep         | P0 | Phase 4 |
| G18 | LoopController step                           | P1 | Phase 2 |
| G19 | SignedConfig loader                           | P2 | Phase 4 |
| G20 | class: path import whitelist                  | P2 | Phase 5 |
| G21 | WorkflowRunner / detached run                 | P1 | Phase 6 |
| G22 | WorkflowEntryTrigger + EventTrigger           | P1 | Phase 6 |

**Gap-delivery sequencing principle:** Phase N's P0 gaps must ship (or be
explicitly declared workaround-acceptable) before Phase N starts. P1 and P2
gaps may arrive concurrently with the consuming phase; their workarounds are
documented in `nanobrain_capability_gaps.md §5`.

**Reading the table:** "Consumed in Phase 2" means the gap's delivery unblocks
optional functionality or eliminates a workaround in that phase. It does NOT
mean the gap is *required* for the phase to ship — every phase's gap-dependency
table above lists the workaround apecx-mcp implements when the gap is absent.

---

## 13. Reference

| Resource | Location |
|---|---|
| Component architecture | `docs/multiagent_architecture.md` |
| Workflow output contract | `docs/workflow_output_contract.md` |
| Nanobrain workflow design | `docs/nanobrain_workflow_design.md` |
| External tool integration | `docs/external_tool_integration.md` |
| Current MCP surface | `docs/architecture.md` |
| Workspace task table | `../implementation_plan.md` |
| Session friction log | `../_workspace_notes/apecx-mcp-integration_dev_history/session_friction_log.md` |
