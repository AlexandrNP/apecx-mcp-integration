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
| **G4 (PARTIAL — see §8.7)** step-level provenance threading | Every tool invocation must produce a provenance record per `external_tool_integration.md §6.2`. Recorder primitive shipped 2026-05-09; the `_execute_process` wrap that delivers automatic per-step capture is **deferred** (eval_03 Round 2). Until G4-completion, the recorder is opt-in by hand-call. | apecx-mcp wraps every tool call in a custom recorder step |
| **G11 (PARTIAL — see §8.7)** tool-step taxonomy | `ToolExecutionStep` base class with declared cost / capability surface. Abstract base shipped; **LocalParslAdapter not in-tree** (eval_03 Round 2). For apecx-mcp Phase 3 the LocalParslAdapter is a hard dependency for non-Rhea tool routing. | apecx-mcp implements ToolExecutionStep as an apecx-mcp BaseStep; promote when G11-completion ships |
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
| **G9 (PARTIAL — see §8.7)** first-class skeleton primitive | Production skeleton catalog needs framework-level versioning + content-addressing. Skeleton + SkeletonRegistry shipped; the **`Workflow.from_skeleton(skeleton_path, bindings)` ergonomic loader is absent** (eval_03 Round 2). Without it, agent-authored workflows cannot bind a skeleton in one call — they must hand-assemble PlanLoweringStep + SkeletonLoaderStep YAML. | apecx-mcp ships its own skeleton catalog; promote when G9-completion ships |
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
| Cost envelope enforcement + per-deployment-per-day ceiling (deployment side) | Runaway-autonomy protection (T-AU-1 mitigation). **Note:** the framework-side cost-envelope enforcement primitive (G26, see §8.8) is the load-bearing piece; deployment ceiling alone cannot stop a single runaway run. | MC-AU-05 |
| Deferred-HITL fields on `Approval` model (data side) | A2U via the existing approvals table. **Note:** the framework-side suspend-and-resume Step primitive (G27, see §8.8) is required for the approval row to actually pause and resume a run. Without G27, the Approval row exists but the workflow either polls or stalls. | MC-AU-06 |

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

## 8.6 Phase 0+ — Adoption-Gap Closure (eval_03 Tier 0)

**Trigger:** `eval_03_nanobrain_gap_inventory.md` (2026-05-09) — Round 4-5 surfaced four
silent-failure / production-workaround items that cut across Phases 0-6 and must close
before any new phase work proceeds. Each is a single-PR change.

**Milestone:** Every `Workflow.from_config` ⇒ `process()` path through the canonical
`LocalExecutor` runs cascades correctly; default log dir works under read-only cwd; the
two known P0 silent-failure shapes (G7-class `auto_transfer=False` and G44-class unscoped
namespace fallback) FAIL-FAST or WARN; the framework's published code is free of debug
print residue.

**Deliverables:**

| Component | Description | Owner | Estimate |
|---|---|---|---|
| **G35** — `LocalExecutor.execute` adopts `Workflow.run(...)` (or pairs `process()` + `wait_for_cascade()`) | `apecx-mcp-integration/src/apecx_integration/control_plane/executors/local.py:240-260`; today calls `await workflow.process({})` and returns the first-step value, silently dropping cascade outputs for any composed multi-step workflow | apecx-mcp-integration | 5-line code + 50-line test (1 day) |
| **G33** — Default log directory under project state dir, not cwd | `nanobrain/core/async_logging.py:103` and `logging_system.py:1051` both default to `Path('logs')`. Crashes when cwd is read-only (Claude Desktop on macOS launches MCP servers with cwd=`/`). Removes the `os.chdir(log_root)` workaround in `synonym_dictionary/workflow/bootstrap.py:194-208`. | nanobrain | 10-line code (0.5 day) |
| **G44** — `data_unit.py:2199` unscoped namespace fallback emits WARNING + opt-in FAIL-FAST | Same shape as G7's `auto_transfer=False` silent failure. An operator can run for months with namespace isolation effectively off and never know. Gap doc proposed WARNING; not seen in code. | nanobrain | 10-line code (0.5 day) |
| **G43** — Remove 11 `print(f"DEBUG: ...")` lines from `nanobrain/core/mcp_support.py:833-876` | Code-review hygiene; would fail any review gate that exists. | nanobrain | sed one-liner (15 min) |

**Integration test requirement (Phase 0+ exit criterion):**

1. A two-step composed workflow (e.g., one canonical YAML from
   `composition/workflows/violin_bvbrc/`) must run via `LocalExecutor.execute` and the
   *second* step's output must reach the executor return value. This pins G35.
2. `apecx-mcp` launched from a process whose cwd is `/tmp/readonly` (or an actual
   read-only mount) must initialize without crashing. This pins G33.
3. A workflow whose YAML omits `WorkflowRunContext` must emit a WARNING at run-start
   and (in opt-in strict mode) refuse to load. This pins G44.

**Framework gap dependencies (Phase 0+):** none — all four items ARE the framework
fixes; nothing further upstream is required.

**Open questions blocking Phase 0+:**

P0+a. **G33 default location.** `nanobrain.log_dir` defaults to `~/.apecx/logs/`,
`~/.nanobrain/logs/`, or `XDG_STATE_HOME`? Cross-platform discipline matters here
because the symptom only fires under Claude Desktop on macOS — Linux dev usually
has writable cwd.

P0+b. **G44 strict-mode rollout.** Default-warning-with-strict-opt-in (proposed) vs.
default-strict-with-opt-out. Eval_03 takes no position; nanobrain author should
choose based on whether any existing nanobrain workflow currently relies on the
unscoped fallback (grep the nanobrain repo before flipping the default).

---

## 8.7 Phase 0++ — Framework Completions (eval_03 Tier 1)

**Trigger:** eval_03 Round 2 — three of the 22 G-shipments are partial; the
"deliverable's value depends on framework-side wiring that did not happen." Plus the
integration repo's outstanding `config_version: 2` migration (G39) and the framework's
own un-audited library workflows (G45).

**Milestone:** All 22 originally-numbered gap-doc items are *fully* shipped (primitive
+ wiring + adoption); the integration repo declares `config_version: 2` everywhere;
the framework's own library-workflows are migrated to v2 too.

**Deliverables:**

| Component | Description | Owner | Estimate |
|---|---|---|---|
| **G4-completion** | Framework wraps `BaseStep._execute_process` so the recorder sees every invocation, including raises (the entire stated value of G4). After this, integration's hand-called recorder steps in Phase 3 collapse to "drop the wrap." | nanobrain | 1-2 days |
| **G9-completion** | Implement `Workflow.from_skeleton(skeleton_path, bindings)` — the ergonomic loader that lets an agent pick a skeleton + bind holes in one call instead of hand-assembling PlanLoweringStep + SkeletonLoaderStep YAML. **Track B's agent-authored-workflows arc depends on this**. | nanobrain | 1-2 days |
| **G11-completion** | Ship LocalParslAdapter in-tree (the `LocalExecutor` case). Galaxy adapter stays deferred per `tool_execution_step.py` docstring. Without LocalParslAdapter, every non-Rhea tool call in apecx-mcp Phase 3 routes through a custom adapter shim. | nanobrain | 3-5 days |
| **G39** — `config_version: 2` migration in apecx-mcp-integration | `grep -rn "config_version: 2" apecx-mcp-integration/src/` returns zero hits today. Every `DirectLink` in every YAML hardcodes `auto_transfer: true` with a 7-line warning comment. Migrate every YAML in `composition/workflows/` and `synonym_dictionary/workflow/configs/`. Retire the warning comments. Ship `scripts/lint_workflow_yamls.py` and add it to CI to fail on any v1 YAML without explicit pin. | apecx-mcp-integration | 1 day |
| **G45** — Framework `library/workflows/` audited for v2 | The G7 migration plan called this out; commit evidence missing. Audit and pin or migrate ~12 framework example workflows. | nanobrain | 1 day |

**Integration test requirement (Phase 0++ exit criterion):**

1. A step that raises mid-`process()` produces a provenance record. (G4-completion.)
2. A workflow built via `Workflow.from_skeleton('synthesis_skeleton.yml',
   bindings={...})` runs end-to-end with the same outputs as the hand-authored
   equivalent. (G9-completion.)
3. A `ToolExecutionStep` configured with `backend: local_parsl` invokes a real Parsl
   `bash_app` and the result reaches `tool_outputs`. (G11-completion.)
4. `scripts/lint_workflow_yamls.py` returns nonzero if any DirectLink in any YAML in
   `composition/workflows/` or `synonym_dictionary/workflow/configs/` is missing
   `auto_transfer: true` OR is on `config_version: 1` without explicit pin. (G39.)
5. CI re-runs the framework's own library workflows under `config_version: 2` with
   no behavioral diff. (G45.)

**Framework gap dependencies (Phase 0++):** Phase 0+ (G35, G33, G44, G43).

**Open questions blocking Phase 0++:**

P0++a. **G9-completion API surface.** `from_skeleton(path, bindings)` vs.
`from_skeleton(skeleton_id, bindings)` (registry-resolved). Eval_03 takes no
position; nanobrain author should pick the form that pairs with the existing
SkeletonRegistry.

P0++b. **G11-completion: which Parsl executor preset?** ThreadPool, ProcessPool,
HighThroughputExecutor, or "follow the workflow's executor: field if compatible"?
Decide before LocalParslAdapter ships so adapter behavior is predictable.

P0++c. **G39 lint-script CI gate severity.** Warning that breaks build, or warning
that lands as PR comment but does not block merge? Recommend the former; a
silent-failure-prevention lint that doesn't block is decoration.

---

## 8.8 Phase 6+ — Autonomy-Mode Preconditions (eval_03 Tier 2)

**Trigger:** eval_03 Round 3 — two autonomy-mode blockers (G26, G27) plus two
adjacent primitives (G24, G25) that the autonomy doc and the prompt-contracts doc
each rely on but do not yet have framework support for. Without G26 + G27 the
autonomous orchestrator cannot run a single long task safely; until they ship, the
"I'll go to lunch and come back to a result" experience does not exist.

**Milestone:** Phase 6 (autonomous operation, §8.5) can credibly ship — every
component listed in §8.5's deliverables table has the framework primitive it
silently depends on.

**Deliverables:**

| Component | Description | Owner | Estimate |
|---|---|---|---|
| **G27** — Deferred-HITL approval **Step primitive** | Step type that emits an approval row, suspends the workflow run, and resumes when the approval is resolved (approved / rejected / corrected). Composes with G21 `run_detached`. The Phase 6 `Approval` model fields by themselves cannot pause-and-resume — they only persist data. | nanobrain | 1-2 weeks |
| **G26** — Workflow-level cost envelope **enforcement primitive** | Framework executors emit cost events; envelope enforcement (the runner halts the task when the cap is hit) is uniformly framework-side. Required by GATE-R1 in `hitl_safety_gates.md §8`. The deployment-side per-day ceiling in §8.5 cannot stop a single runaway long-running call. | nanobrain | 1 week |
| **G24** — DataSourceRegistry primitive | Versioned data-source manifest with refresh-cadence + content-hash policy. `data_layer_evolution.md §3-4` describes 14 data sources each with custom version pin policy; today every consumer rolls their own. Blocks R2/R3 reproducibility for any RAG/FAISS-consuming workflow. APECx contributes catalog content; primitive itself is domain-neutral. | nanobrain | 1 week |
| **G25** — `PromptRegressionTestHarness` | Schema-aware regression suite tied to `PromptTemplate` (G14). G14 is the primitive; G25 is the harness that catches the AC1-breaking class of regressions. `llm_prompt_contracts.md §1` lists two AC1-breaking regressions on 2026-04-22 from prose-level edits to `system.md`; without G25, G14 is an incomplete cure. | nanobrain | 3-5 days |

**Integration test requirement (Phase 6+ exit criterion):**

1. An autonomous task pauses at a deferred-HITL gate, the operator approves via the
   existing `approve` MCP tool, and the same `WorkflowRunner` instance resumes the
   run from the suspension point. (G27.)
2. A workflow declared with `cost_envelope: {usd: 1.00, tokens: 1_000_000}` halts
   mid-run when either cap is reached, with a structured `CostEnvelopeBreach` event
   in the audit log. (G26.)
3. A workflow that consumes `viper_v3` (data-source manifest entry) refuses to load
   if the manifest's `content_hash` does not match the on-disk index. (G24.)
4. A change to a `PromptTemplate` that breaks the schema of any prompt in
   `composer_prompts/` produces a structured regression diff under
   `pytest tests/regression/prompt_contracts/`. (G25.)

**Framework gap dependencies (Phase 6+):** Phase 0++ (G4-completion). G27's
suspend-resume must thread through whatever provenance recorder G4-completion
exposes.

**Open questions blocking Phase 6+:**

P6+a. **G27 suspend-state persistence.** Reuses `Run` table or new `SuspendedRun`?
Affects schema, migration, and recovery semantics on control-plane restart.
P6+b. **G26 enforcement granularity.** Per-step cost cap, per-workflow cap, both?
`autonomous_workflow_agent.md §8` implies both; pick one as the primary surface.
P6+c. **G24 manifest format.** YAML, JSON, or `pyproject.toml`-style TOML? Affects
authoring ergonomics and toolchain coupling.

---

## 8.9 Phase 4+ / Future — Meta-Workflow Preconditions (eval_03 Tier 3)

**Trigger:** eval_03 Round 3 — three primitives the meta-workflow orchestrator
design (`meta_workflow_orchestration.md`) and the security threat model
(`security_threat_model.md`) depend on but that are not yet in framework code.

**Milestone:** The 9-Step meta-workflow orchestrator described in
`meta_workflow_orchestration.md` can be assembled from framework primitives
without consumer-side reinvention; Strategy B "skeleton composition" (nested
workflows) becomes runnable; UTD-G15 capability tokens are enforceable.

**Deliverables:**

| Component | Description | Owner | Estimate |
|---|---|---|---|
| **G31** — Workflow-as-substep / nested workflow primitive | Strategy B in `meta_workflow_orchestration.md §9.2` produces a YAML with K skeletons embedded as sub-workflows; framework loader currently has no nested-workflow lifecycle (verified by absence in `nanobrain/core/`). Without it, agent-authored multi-skeleton workflows must flatten or hand-orchestrate. | nanobrain | 1-2 weeks |
| **G28** — Capability-token verification at framework boundary | The `requires_capability` field on UnifiedToolDescriptor (G15) needs an enforcement hook in the workflow loader / step dispatcher. Per `tool_descriptor_contract.md §6` and `hitl_safety_gates.md §7`. Framework should enforce uniformly so tool authors don't reimplement. | nanobrain | 3-5 days |
| **G37** — Cascade-aware step-level provenance hook | Surface a hook on `Workflow.run()` so consumers' provenance recorders can subscribe to step-start / step-complete events; framework emits. Cousin of G4-completion: even after `_execute_process` ships the recorder wrap, the integration's executor still needs hooks to subscribe. Required for hash-chained provenance to be granular enough for HPC-bundle audit (Phase 4 PBS bundles). | nanobrain | 3-5 days |

**Integration test requirement (Phase 4+ exit criterion):**

1. A workflow YAML containing `steps: [{class: ..., config: nested_workflow.yml}, ...]`
   loads, runs, and the nested workflow's data-units namespace correctly under the
   parent workflow's `WorkflowRunContext`. (G31.)
2. A step declaring `requires_capability: hpc.submit` refuses to execute when the
   active execution context lacks that capability token; the refusal is auditable.
   (G28.)
3. The integration's `provenance/recorder.py` receives step-start and step-complete
   events for every nanobrain step inside a multi-step workflow run, without the
   workflow author wrapping anything by hand. (G37.)

**Framework gap dependencies (Phase 4+):** Phase 0++ (G4-completion as the inside-step
recorder; G37 is the outside-loop subscriber half).

**Open questions blocking Phase 4+:**

P4+a. **G31 nested-context isolation.** Does the nested workflow inherit the parent's
data-unit namespace, get its own scoped namespace, or take an explicit
`namespace_strategy:` field? Affects every multi-skeleton design downstream.
P4+b. **G28 token transport.** Capability tokens travel via `WorkflowRunContext`,
env var, or per-step config dict? Affects threat-model surface (`security_threat_model.md §6.5`).
P4+c. **G37 event schema versioning.** Step-event payloads evolve; freeze v1 schema
before integration's `provenance/recorder.py` subscribes, or the integration breaks
on every framework minor.

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
| G24 | DataSourceRegistry primitive                  | P1 | Phase 6+ (§8.8) |
| G25 | PromptRegressionTestHarness                   | P1 | Phase 6+ (§8.8) |
| G26 | Workflow cost envelope enforcement primitive  | P0 (autonomy) | Phase 6+ (§8.8) |
| G27 | Deferred-HITL approval Step primitive         | P0 (autonomy) | Phase 6+ (§8.8) |
| G28 | Capability-token verification at loader       | P1 | Phase 4+ (§8.9) |
| G31 | Workflow-as-substep / nested workflow         | P1 (Strategy B) | Phase 4+ (§8.9) |
| G33 | Default log-dir under project state dir       | P0 (silent failure) | Phase 0+ (§8.6) |
| G35 | LocalExecutor adopts Workflow.run/cascade     | P0 (silent failure) | Phase 0+ (§8.6) |
| G37 | Cascade-aware step-level provenance hook      | P1 | Phase 4+ (§8.9) |
| G39 | config_version: 2 integration migration + lint| P1 | Phase 0++ (§8.7) |
| G43 | Remove DEBUG print residue (mcp_support.py)   | P2 (CR hygiene) | Phase 0+ (§8.6) |
| G44 | data_unit unscoped-namespace WARN/strict      | **P0 (silent failure)** | Phase 0+ (§8.6) |
| G45 | Framework library/workflows v2 audit          | P1 | Phase 0++ (§8.7) |

**Gap-delivery sequencing principle:** Phase N's P0 gaps must ship (or be
explicitly declared workaround-acceptable) before Phase N starts. P1 and P2
gaps may arrive concurrently with the consuming phase; their workarounds are
documented in `nanobrain_capability_gaps.md §5`.

**Phases 0+ / 0++ are gating.** Sections §8.6 (Tier 0, four single-PR fixes)
and §8.7 (Tier 1, finish the three partials + integration v2 migration) gate
*all* later phase work. They were added 2026-05-09 in response to
`eval_03_nanobrain_gap_inventory.md` Round 4-5, which surfaced silent-failure
shapes and partial-shipment status in items previously listed as "shipped."

**Eval_03 scope note:** This roadmap absorbs eval_03's Tier 0-3 items as
§§8.6-8.9. Eval_03 Tier 4 (G34, G36, G38, G40, G41, G42, G46, G47) and
Deferred/v2 (G23, G29, G30, G32) are intentionally NOT pulled into the
roadmap — they remain in `eval_03_nanobrain_gap_inventory.md` Round 8 and
should be revisited only after §§8.6-8.9 close.

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
| Brutal-truth gap audit | `../eval_03_nanobrain_gap_inventory.md` |

---

## 14. Engineering Process Gaps (eval_03 Round 6)

These are not numbered Gxx items — they are framework-process issues surfaced by
eval_03 Round 6. Each blocks "third-party-verifiable shipped" claims regardless of
which G-id is on disk. They are listed here because the roadmap is the canonical
sequencing artifact and process work belongs in the same place as code work.

| ID | Discipline | What it fixes | Owner | Estimate |
|---|---|---|---|---|
| **PROC-1** | **PR review with cool-off** | Single-author single-machine attestation: every commit signed by one author, no second reviewer, no PR record. Even self-PR with a 24-hour cool-off and a fresh re-read closes the most-common kind of one-author bug. Without it the "0 regressions ever" claim is what one author saw on one machine on one date. | nanobrain | process change, ongoing |
| **PROC-2** | **Per-gap commit boundary** | Megacommits like `78b67cb` (G4 + G9 + G11 + G12 + G17 + G19 + G20 — 168 tests) cannot be reverted per-gap. If G19 SignedConfig has a vulnerability tomorrow, you cannot revert just G19 without taking down G4, G9, G11, G12, G17, G20. Bisect-friendly history requires one G-id per commit (or one logical change-set per commit at most). Workspace `CLAUDE.md` Non-Negotiable Rule #8 already requires this; megacommit `78b67cb` violates it. | nanobrain | process change, ongoing |
| **PROC-3** | **Public CI green badge** | `nanobrain/.github/workflows/tests.yml` was added in commit `55fbcc1` *during* the gap-shipment chain — the CI didn't gate the deliveries it was meant to evidence. Required for "0 regressions" to be third-party-verifiable. CI must run on PR with both Postgres + Redis up. | nanobrain | 1-2 days |
| **PROC-4** | **Gap doc as contract; symbol drift forbidden** | Gap doc proposed `ToolStep`; framework shipped `ToolExecutionStep`. Gap doc proposed `Workflow.from_skeleton`; framework shipped nothing. Gap doc proposed G13 location `core/`; actual is `library/orchestration/`. When code lands, the gap doc updates in the same commit (or the next). Symbol renames are forbidden silently — they require a migration note. | nanobrain | process change, ongoing |
| **PROC-5** | **`make verify` reproducible-green recipe** | Today the "557 / 712 / 1132 unit tests" counts in `nanobrain/CLAUDE.md` are local. A reviewer cannot reproduce them in one command. `make verify` must bring up the required environment (Postgres + Redis), run the full suite, and exit 0 only if all tests pass. | nanobrain | 1 day |

**Why these matter at all (brutal-truth note from eval_03 Round 6):** the
framework's self-graded "all 22 shipped" claim is in conflict with three concrete
findings (G4 partial, G9 partial, G11 partial). The conflict is detectable by any
reader who reads commit messages alongside `nanobrain/CLAUDE.md`. PROC-1 through
PROC-5 are the disciplines that prevent the next round of self-grading from drifting
again. They are cheaper than they look.

---

## 15. Coverage Matrix — Eval_03 Tier 0-3 ⇒ Roadmap Section

Verification that every in-scope item from `eval_03_nanobrain_gap_inventory.md`
Round 8 (Tier 0-3 + process gaps) is mapped to a roadmap section. Out-of-scope
items (Tier 4 + Deferred/v2) are intentionally not pulled in; they remain in
eval_03 itself.

| Eval_03 Item | Tier | Severity | Roadmap section | Task / row |
|---|---|---|---|---|
| G35 | 0 | P0 silent-failure | §8.6 | LocalExecutor.execute adopts Workflow.run |
| G33 | 0 | P0 silent-failure | §8.6 | Default log directory under project state dir |
| G44 | 0 | P0 silent-failure | §8.6 | data_unit unscoped-namespace WARN/strict |
| G43 | 0 | P2 CR hygiene | §8.6 | Remove DEBUG print residue |
| G4-completion | 1 | P1 | §6 row updated + §8.7 | _execute_process recorder wrap |
| G9-completion | 1 | P0 | §8 row updated + §8.7 | Workflow.from_skeleton loader |
| G11-completion | 1 | P1 | §6 row updated + §8.7 | LocalParslAdapter in-tree |
| G39 | 1 | P1 | §8.7 | config_version:2 migration + lint |
| G45 | 1 | P1 | §8.7 | Framework library/workflows v2 audit |
| G27 | 2 | P0 (autonomy) | §8.5 row updated + §8.8 | Deferred-HITL Step primitive |
| G26 | 2 | P0 (autonomy) | §8.5 row updated + §8.8 | Cost envelope enforcement |
| G24 | 2 | P1 | §8.8 | DataSourceRegistry primitive |
| G25 | 2 | P1 | §8.8 | PromptRegressionTestHarness |
| G31 | 3 | P1 (Strategy B) | §8.9 | Workflow-as-substep / nested workflow |
| G28 | 3 | P1 | §8.9 | Capability-token verification at loader |
| G37 | 3 | P1 | §8.9 | Cascade-aware step provenance hook |
| Process: PR review with cool-off | — | — | §14 | PROC-1 |
| Process: per-gap commit boundary | — | — | §14 | PROC-2 |
| Process: public CI green badge | — | — | §14 | PROC-3 |
| Process: gap doc as contract | — | — | §14 | PROC-4 |
| Process: `make verify` recipe | — | — | §14 | PROC-5 |

**Coverage: 21/21 in-scope items mapped.** Out-of-scope (Tier 4 + Deferred/v2):
G34, G36, G38, G40, G41, G42, G46, G47, G23, G29, G30, G32 — see
`eval_03_nanobrain_gap_inventory.md` Round 8 (intentionally excluded per user's
2026-05-09 scope-reduction).

**Honest caveat (per `CLAUDE.md` direct/critical-output rule):** This roadmap
update covers the gaps in the *plan*. None of the gaps are covered in *code*.
Per eval_03 Round 7 + Round 8 + Closing: the highest-value next action is one
PR shipping G35, not another planning increment. The roadmap is now consistent
with the eval_03 gap inventory; "consistent plan" is a precondition for shipping,
not a substitute for it.
