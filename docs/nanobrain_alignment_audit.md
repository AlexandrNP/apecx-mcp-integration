# APECx ↔ Nanobrain Alignment Audit

**Status:** Living document — re-run when any design doc materially changes
**Audience:** Framework reviewers, design-doc maintainers, anyone deciding "where does this code live?"
**Supersedes:** Nothing — this is a meta-doc about the design package
**Read first:** `nanobrain_capability_gaps.md`, `_design_index.md`

---

## 1. Why this audit exists

The user directive is unambiguous:

> *"Utilize Nanobrain capabilities to the fullest, do not duplicate functionality;
> decide which changes should go to nanobrain and which should go to the apecx-mcp.
> Ensure that all workflow-building is Nanobrain-compatible — everything is in YAML files."*

Every concept introduced by the design package falls into one of four buckets:

1. **USE-AS-IS:** the concept is already a nanobrain primitive; the apecx-mcp doc
   should reference it, not redefine it.
2. **EXTEND-NANOBRAIN:** the concept requires a new nanobrain primitive (gap
   proposal); apecx-mcp should not work around it with apecx-side code.
3. **APECX-SPECIFIC:** the concept is genuinely outside nanobrain's scope (data
   ingestion, MCP transport, scientific catalog, deployment topology); it lives in
   apecx-mcp and uses nanobrain primitives where they apply.
4. **DUPLICATES — REWRITE:** the concept reinvents a nanobrain primitive in
   apecx-mcp code or docs; this is a defect to be fixed.

This audit catalogues every load-bearing concept across the 12 design documents,
tags it with one of the four labels, and proposes the corrective action.

**Honest disclosure:** the docs I (Claude) authored in the prior session contain
several DUPLICATES-REWRITE findings. These are called out explicitly. The point of
this audit is not to defend the prior work but to harden the package.

---

## 2. The split rule — when does code live in nanobrain vs. apecx-mcp?

A primitive belongs in **nanobrain** when **all three** of the following hold:

1. **Domain-neutral.** Useful to any analytical workflow, not just APECx queries.
   (DataUnitMemory is domain-neutral; "lookup_violin" is not.)
2. **Composable.** Other steps/workflows can use it without depending on apecx-mcp
   data, services, or vocabularies.
3. **Worth the lifecycle cost.** The primitive will be used by ≥2 workflows or
   ≥2 unrelated tasks; otherwise it stays apecx-side until a second consumer appears.

A primitive belongs in **apecx-mcp** when **any** of:

- It carries APECx vocabulary (entity classes, ontology IDs, scientific catalogs).
- It depends on a specific data source (Globus index, FAISS index, synonym dict).
- It is an MCP-surface tool (FastMCP integration, tool-name conventions).
- It encodes deployment policy (which executor maps to which user role).

**Corollary:** a primitive that *starts* APECx-specific can be promoted to
nanobrain when its second non-APECx consumer materializes. The promotion is a
mechanical move (code transplant + import update). Nothing else changes.

---

## 3. Concept catalogue — the complete table

Each row is a load-bearing concept across the design package. Source column points
to the doc/section that introduces it; Tag column applies the four-bucket
classification. Action column is the concrete fix-up (if any).

### 3.1 Workflow construction concepts

| # | Concept | Source | Tag | Action |
|---|---|---|---|---|
| C-1 | "Skeleton" — pre-validated workflow YAML with typed holes | `agent_workflow_authoring.md §4` | EXTEND-NANOBRAIN | Already proposed as **G9** in `nanobrain_capability_gaps.md`. The skeleton primitive belongs in nanobrain (domain-neutral; multiple consumers). APECx ships skeleton *content*, nanobrain ships the loader. |
| C-2 | "ExecutionPlan" JSON object | `agent_workflow_authoring.md §3` | DUPLICATES — REWRITE | The plan is currently described as "JSON, not YAML" and emitted by an LLM. **It should be a nanobrain DataUnit shape with a Pydantic schema.** Recast as `ExecutionPlanConfig` + `DataUnitMemory` carrying the plan. The shape is domain-neutral. |
| C-3 | "Plan-to-YAML lowering function" — code-owned | `agent_workflow_authoring.md §5` | DUPLICATES — REWRITE | The lowering is described as a "deterministic code-owned function". **It should be a nanobrain Step.** The Step's `process()` reads the ExecutionPlan + skeleton DataUnits and writes a fully-lowered Workflow YAML to its output. This is the meta-workflow concept (see `meta_workflow_orchestration.md`). |
| C-4 | "5-gate validation pipeline" | `agent_workflow_authoring.md §6` | DUPLICATES — REWRITE | Each gate is described as a procedural call. **They should be 5 nanobrain Steps** wired in sequence with FAIL-FAST exceptions on rejection. Gate 4 already wraps `Workflow.from_config()` + `Workflow.initialize()` — that's a nanobrain primitive, the wrapper is APECx-specific. |
| C-5 | "Repair contract — bounded retry loop" | `agent_workflow_authoring.md §7` | EXTEND-NANOBRAIN | A bounded loop with explicit termination is exactly the **P3 (Refinement Loop)** pattern from `reasoning_patterns_library.md`, which **requires** the cycle-detection relaxation flagged as part of the same gap surface. Wire as `LoopController` step + `ConditionalLink`. |
| C-6 | Conversation-chaining decision (reuse / delta / fresh) | `agent_workflow_authoring.md §8` | APECX-SPECIFIC | Heuristic depends on EvidenceBundle shape (APECx-defined). Implement as a Step in the meta-workflow. |
| C-7 | Workflow-integrity validation | `architecture.md §13`, gap docs | USE-AS-IS | Already shipped in `workflow.py` — cycle detection, orphan detection, plural-data-units enforcement. Apecx docs reference it; do not reimplement. |

### 3.2 Output-contract concepts

| # | Concept | Source | Tag | Action |
|---|---|---|---|---|
| C-8 | 7-phase workflow output template | `workflow_output_contract.md §2` | APECX-SPECIFIC | The 7-phase shape is APECx's analytical convention; it is not a framework concept. Implemented via nanobrain Steps + DataUnits. |
| C-9 | LayerResult, EvidenceBundle, FinalResponse, FollowupQuestions schemas | `workflow_output_contract.md` | APECX-SPECIFIC | Pydantic schemas owned by apecx-mcp. Carried by `DataUnitMemory`. |
| C-10 | "Capability gap declaration" record | `workflow_output_contract.md §6` (and P9) | APECX-SPECIFIC | A meta-evidence type APECx invents. Lives in apecx-mcp. |
| C-11 | Grounding gate (citation enforcement) | `workflow_output_contract.md §8` | APECX-SPECIFIC | A synthesis-level validation; lives in apecx-mcp's `RagSynthesisStep`. |
| C-12 | Conversational chaining session contract | `workflow_output_contract.md §10` | APECX-SPECIFIC | The session protocol is APECx's domain. Backed by `DataUnitFile` for cross-turn persistence. |

### 3.3 Nanobrain workflow-design concepts

| # | Concept | Source | Tag | Action |
|---|---|---|---|---|
| C-13 | Static DAG with ConditionalLink gating | `nanobrain_workflow_design.md §1–§2` | USE-AS-IS + EXTEND | The pattern uses `ConditionalLink` (shipped). Two extensions are required: G1 (declarative predicate language — agents cannot synthesize Python) and G10 (gate-to-bottom sentinel — to avoid `AllDataReceivedTrigger` deadlock when a gated branch never publishes). |
| C-14 | 9-step skeleton (Phase0Planning, layer steps, accumulation, synthesis, follow-up) | `nanobrain_workflow_design.md §3` | APECX-SPECIFIC | The step catalog is APECx-specific; each step is a `BaseStep` subclass. |
| C-15 | HypothesisTournamentStep | `nanobrain_workflow_design.md §3.5` and `reasoning_patterns_library.md P2` | EXTEND-NANOBRAIN | The tournament shape (N parallel proposers + scoring + ranking) is domain-neutral. Should ship as a nanobrain *generic* step (`TournamentStep`); APECx provides the proposer-agent configurations. Promotion candidate. |
| C-16 | "Workflow-level data units" requirement | `architecture.md §13` brutal-truth #4 | USE-AS-IS | Already enforced by the workflow integrity validator. Doc references; do not duplicate. |
| C-17 | `wait_for_cascade` await semantics | `architecture.md §3.4` | USE-AS-IS + EXTEND | `wait_for_cascade` shipped 2026-05-05 (verified at `trigger.py:156`). G8 proposal (`Workflow.run()` synchronous wrapper) makes the canonical entry point obvious. |

### 3.4 External tool integration concepts

| # | Concept | Source | Tag | Action |
|---|---|---|---|---|
| C-18 | "ToolExecutionAgent" abstract interface | `external_tool_integration.md §2` | DUPLICATES — REWRITE | nanobrain already has `ToolBase` with multiple `ToolType` values (FUNCTION, AGENT, STEP, EXTERNAL, LANGCHAIN). The `ToolExecutionAgent` should be a concrete `ToolBase` subclass for `tool_type=external`, NOT a parallel interface. Recast `external_tool_integration.md §2`. |
| C-19 | Unified Tool Descriptor (UTD) v1 schema | `tool_descriptor_contract.md §2` | EXTEND-NANOBRAIN | The UTD shape is domain-neutral and serves as the standard tool-card content. Should be elevated to a nanobrain primitive (`UnifiedToolDescriptor` in nanobrain core). The `tool_card` field on `ToolConfig` is the natural carrier. |
| C-20 | UTD backend adapters (Rhea, native, Galaxy) | `tool_descriptor_contract.md §3` | APECX-SPECIFIC | Adapter implementations are APECx-side. They produce `ToolBase` subclass instances populated from the backend metadata. |
| C-21 | ProxyStore reference-based I/O | `external_tool_integration.md §4`, gaps doc | EXTEND-NANOBRAIN | Already proposed as **G3 (DataUnitProxyRef)** and **G13 (multi-tenant namespacing)**. Belongs in nanobrain core. AcademyLink already uses ProxyStore at link level — the DataUnit-level abstraction is the gap. |
| C-22 | Result-typing into DataUnits (UTD output → DataUnit subclass) | `tool_descriptor_contract.md §7` | EXTEND-NANOBRAIN | A small mapping function (UTD output type → DataUnit class) belongs in nanobrain core alongside the UTD primitive. The mapping itself is domain-neutral. |
| C-23 | Tool discovery protocol (`discover_tools`) | `tool_descriptor_contract.md §4` | APECX-SPECIFIC | Discovery is a query against APECx's tool-catalog index (Rhea/native/Galaxy aggregation). The catalog is APECx infrastructure. |
| C-24 | Tool capability tokens (`requires_capability`) | `tool_descriptor_contract.md §6`, `hitl_safety_gates.md §7` | EXTEND-NANOBRAIN (small) | The `tool_card` should accept a `requires_capability` list at the framework level so that gating is enforced uniformly. The capability *vocabulary* is APECx-specific. |

### 3.5 HPC and reproducibility concepts

| # | Concept | Source | Tag | Action |
|---|---|---|---|---|
| C-25 | Three reproducibility tiers (R1/R2/R3) | `hpc_reproducibility_spec.md §2` | APECX-SPECIFIC | A policy classification, not a primitive. Lives in apecx-mcp. |
| C-26 | Reproducibility manifest schema | `hpc_reproducibility_spec.md §3` | EXTEND-NANOBRAIN (partial) | The *manifest* is an apecx-mcp concern (manifests aggregate APECx-specific provenance). However the *per-step provenance records* it embeds belong to nanobrain (G4 `ProvenanceContext`). Recast: manifest is APECx; the JSONL records it consumes are a nanobrain primitive. |
| C-27 | HPC bundle v2 file layout | `hpc_reproducibility_spec.md §4` | APECX-SPECIFIC | Bundle layout is APECx's deliverable. Uses nanobrain's `WorkflowConfig` (the `workflow.yml` inside the bundle) but adds APECx-specific files (manifest, plan, prompts, snapshots). |
| C-28 | Provenance JSONL graph + record schema | `hpc_reproducibility_spec.md §5` | EXTEND-NANOBRAIN | **G4** in gaps doc. The framework should write a JSONL record per step invocation. APECx defines the manifest that aggregates them. |
| C-29 | Replay protocol | `hpc_reproducibility_spec.md §7` | APECX-SPECIFIC | A runbook over apecx-mcp tooling; consumes nanobrain's `Workflow.from_config()` + executor primitives. |
| C-30 | HPC executor profiles (Polaris, Aurora) | `hpc_reproducibility_spec.md §8` | USE-AS-IS + APECX-SPECIFIC | Nanobrain provides `ParslExecutor` and `AcademyManagerWrapper` (verified shipped, G5 2026-04-24). The cluster-specific Parsl configs (`/lus/eagle/`, `/lus/gila/`, apptainer flags) live in apecx-mcp's deployment package. |
| C-31 | Stochasticity budget | `hpc_reproducibility_spec.md §9` | APECX-SPECIFIC | Policy decision per workflow. Lives in apecx-mcp. |
| C-32 | CheckpointStep / ResumeStep | `nanobrain_capability_gaps.md G5`, `hpc_reproducibility_spec.md §11` | EXTEND-NANOBRAIN | **G5** in gaps doc. Domain-neutral; belongs in nanobrain core. |
| C-33 | Resource envelope per step (CPU/mem/walltime/GPU) | `tool_descriptor_contract.md §2`, `hitl_safety_gates.md §3 GATE-R2` | EXTEND-NANOBRAIN | **G12** in gaps doc. Belongs in `StepConfig`. |
| C-34 | Bundle signing (ed25519) | `hpc_reproducibility_spec.md §10` | APECX-SPECIFIC | Signing key management is deployment-policy. Lives in apecx-mcp. |

### 3.6 HITL & safety concepts

| # | Concept | Source | Tag | Action |
|---|---|---|---|---|
| C-35 | Approval gate primitive | `hitl_safety_gates.md`, existing approval tools | USE-AS-IS | `ApprovalStep` exists in nanobrain (referenced from `apecx-mcp-integration/CLAUDE.md`). Apecx-mcp adds gate categories (Authoring, Resource, Capability, Decision, Post-execution) as configuration policies, not new primitives. |
| C-36 | The 11 specific gates (GATE-A1, R1, etc.) | `hitl_safety_gates.md §3` | APECX-SPECIFIC | Each gate is an `ApprovalStep` instance with APECx-specific payload schemas, approver-role policy, and approval payload renderers. |
| C-37 | Capability tokens (`network_egress`, `phi_data_access`, …) | `hitl_safety_gates.md §7` | APECX-SPECIFIC vocabulary; framework just stores them | The token *vocabulary* is APECx-specific (cluster, tenant, dataset roles). The framework just needs the `requires_capability` field on `tool_card` (C-24). |
| C-38 | Cost & resource accounting | `hitl_safety_gates.md §8` | APECX-SPECIFIC | The cost catalog, telemetry collection, and rolling p95 calculations live in apecx-mcp's control plane. |
| C-39 | Audit log (immutable, ed25519-signed) | `hitl_safety_gates.md §9` | APECX-SPECIFIC | Apecx-mcp control plane responsibility. |
| C-40 | Sandbox execution gate (T13b) | `hitl_safety_gates.md §10` | APECX-SPECIFIC + framework-aware | Docker sandbox scaffold is apecx-mcp; the *Step* that invokes it is a `BaseStep` subclass. |

### 3.7 Reasoning patterns

The 10 patterns from `reasoning_patterns_library.md` are framework-neutral
*shapes* expressed in nanobrain primitives. Each pattern's classification:

| # | Pattern | Tag | Required nanobrain extensions |
|---|---|---|---|
| C-41 | P1 Decompose & Fan-out | USE-AS-IS | None — already expressible. |
| C-42 | P2 Hypothesis Tournament | EXTEND-NANOBRAIN | C-15 (`TournamentStep` promotion). |
| C-43 | P3 Refinement Loop with Explicit Termination | EXTEND-NANOBRAIN | Loop primitive (G5/G10 partial); needs framework-supported `LoopController` step + ConditionalLink termination check. Cycle-detection relaxation flagged. |
| C-44 | P4 Debate | USE-AS-IS | None — pair of agents + judge step. |
| C-45 | P5 Manager / Worker / CEO | USE-AS-IS | None — three Agent instances with distinct system_prompts. |
| C-46 | P6 Branch-and-Prune | USE-AS-IS | None — fan-out + scoring step + ConditionalLink. |
| C-47 | P7 Retry-with-Feedback | EXTEND-NANOBRAIN | Same as P3 — needs `LoopController`. |
| C-48 | P8 Evidence Accumulation with Cross-Source Concordance | USE-AS-IS | None — fan-in + concordance scoring step. |
| C-49 | P9 Capability Gap Declaration | APECX-SPECIFIC | A meta-evidence type APECx defines (C-10). |
| C-50 | P10 Conversation Chaining | APECX-SPECIFIC | Session protocol (C-12). |

### 3.8 New gap docs (this iteration)

| # | Concept | Doc | Tag | Action |
|---|---|---|---|---|
| C-51 | LLM prompt contracts (templates, versioning, regression tests) | `llm_prompt_contracts.md` | EXTEND-NANOBRAIN (small) | nanobrain has `prompt_template_manager.py` (file confirmed; structure TBD). The framework should expose a `PromptTemplate` primitive with: content-addressing, few-shot bundling, hole substitution. APECx ships *prompt content* and the regression-test suite. |
| C-52 | Agent communication protocol | `agent_communication_protocol.md` | USE-AS-IS | nanobrain ships A2A (`a2a_support.py`). Apecx doc formalizes how Tier-1/Tier-2 use it: message envelopes, error catalog, streaming patterns. **Do NOT invent a new protocol.** |
| C-53 | Deployment architecture | `deployment_architecture.md` | APECX-SPECIFIC | Service topology, secrets, scaling — all apecx-mcp deployment policy. Uses nanobrain executors but doesn't extend the framework. |
| C-54 | Security threat model | `security_threat_model.md` | APECX-SPECIFIC + EXTEND | Threat catalogue is apecx-mcp; mitigations partially extend nanobrain (e.g., signed configs — extension of ConfigBase loader). |
| C-55 | Data layer evolution | `data_layer_evolution.md` | APECX-SPECIFIC | Globus migration, FAISS lifecycle, ontology refresh — all APECx data-plane policy. Implemented as nanobrain workflows. |
| C-56 | Meta-workflow orchestration | `meta_workflow_orchestration.md` | EXTEND-NANOBRAIN (G9 promotion) | The orchestrator-as-workflow concept is the unification anchor. Requires G9 (skeleton primitive) at minimum; ideally G1, G2, G6, G9, G10 together. |

---

## 4. Findings — what must change

### 4.1 DUPLICATES — REWRITE (must-fix in existing docs)

These findings represent concepts in the existing docs that reinvent nanobrain
primitives. Each requires a targeted edit:

| ID | Doc affected | Symptom | Fix |
|---|---|---|---|
| F-1 | `agent_workflow_authoring.md §3` | ExecutionPlan described as free-form "JSON object emitted by LLM" | Recast as `ExecutionPlanConfig` (Pydantic ConfigBase) carried in a `DataUnitMemory`. The schema lives in apecx-mcp; the carrier and validation are nanobrain. |
| F-2 | `agent_workflow_authoring.md §5` | Plan-to-YAML lowering described as "code-owned function" | Recast as `PlanLoweringStep` (nanobrain `BaseStep` subclass). Inputs: `ExecutionPlanConfig` DataUnit + `SkeletonRefConfig` DataUnit. Output: `WorkflowYamlConfig` DataUnit. The Step IS the lowering. |
| F-3 | `agent_workflow_authoring.md §6` | 5-gate validation pipeline as procedural pseudocode | Recast as 5 nanobrain Steps connected by `DirectLink`s with `auto_transfer: true`. Each gate's failure triggers FAIL-FAST `ComponentConfigurationError`. |
| F-4 | `external_tool_integration.md §2` | "ToolExecutionAgent" abstract interface described as parallel to nanobrain Agent | Recast as `ToolExecutionStep` (BaseStep subclass) that consumes a UTD reference. The "agent" framing was a category error — nanobrain `Agent` is for LLM dispatch, not tool dispatch. Tool dispatch is a Step concern. |
| F-5 | `tool_descriptor_contract.md §1` | UTD positioned as APECx contract | UTD is domain-neutral; promote to nanobrain primitive. APECx-side is the catalog (Rhea aggregation, native tool catalog overlay). |
| F-6 | `agent_workflow_authoring.md §7` | Repair loop as procedural retry | Recast as `LoopController` step + `ConditionalLink` predicate; max-iteration enforced by step config. |

### 4.2 EXTEND-NANOBRAIN (gap proposals to add or strengthen)

These augment `nanobrain_capability_gaps.md`'s 13 gaps. Proposed additions:

| ID | New gap | Justification | Priority |
|---|---|---|---|
| G14 | **`PromptTemplate` primitive** | Per the inventory, `prompt_template_manager.py` exists but its structure is undocumented for skeleton-style hole substitution + content-addressing + few-shot bundling. C-51 needs this. | P1 |
| G15 | **`UnifiedToolDescriptor` primitive** + `ToolBase.from_descriptor()` | The UTD is the canonical tool-card content; promote into nanobrain core (C-19, C-22). | P0 (blocks tool integration) |
| G16 | **`ExecutionPlanConfig` shape** | The plan is a workflow-construction primitive; needs to be a first-class config (C-2). | P0 (blocks meta-workflow) |
| G17 | **`PlanLoweringStep` + `SkeletonLoaderStep` built-ins** | Required for the meta-workflow (C-3). Companion to G9. | P0 (blocks meta-workflow) |
| G18 | **`LoopController` step + bounded-cycle relaxation** | Required for P3, P7 patterns and the repair loop (C-5, C-43, C-47). The framework currently rejects all cycles; LoopController provides a controlled exception. | P1 |
| G19 | **`SignedConfig` loader option** | Threat-model mitigation: signed skeletons + signed UTDs (C-54 + security_threat_model.md). | P2 |

(Renumbered G14–G19 to extend the existing G1–G13 inventory; cross-link in
`nanobrain_capability_gaps.md` to reference these.)

### 4.3 USE-AS-IS (concepts that just need cross-references, not redefinition)

| ID | Concept | Doc that should add a cross-reference |
|---|---|---|
| U-1 | `wait_for_cascade` | `agent_workflow_authoring.md` (note in §6 Gate 4) |
| U-2 | A2A protocol (`a2a_support.py`) | `agent_communication_protocol.md` (entire doc anchors here) |
| U-3 | `ApprovalStep` | `hitl_safety_gates.md §4` (the lifecycle uses it) |
| U-4 | `ParslExecutor` + `AcademyManagerWrapper` | `hpc_reproducibility_spec.md §8` (already cited; verify) |
| U-5 | Workflow integrity validator (cycles/orphans) | `agent_workflow_authoring.md §6 Gate 4` |
| U-6 | `ConfigBase` constructor prohibition + `extra: forbid` | `tool_descriptor_contract.md §10` (catalog governance — descriptor configs MUST inherit ConfigBase) |
| U-7 | `system_prompt` mandatory in YAML | `llm_prompt_contracts.md` (entire prompt-engineering surface respects this) |

---

## 5. The unification anchor — orchestrator IS a nanobrain workflow

The single most important unification consequence of this audit:

> **The orchestrator that builds analytical workflows IS a nanobrain workflow.**

Today's design package (specifically `agent_workflow_authoring.md`) treats the
orchestrator as a procedural pipeline of LLM calls and code-owned functions.
After applying findings F-1 through F-6, the orchestrator becomes a real
nanobrain `Workflow` whose YAML lives at
`apecx-mcp-integration/composition/workflows/orchestrator/orchestrator.yml`. Its
shape:

```mermaid
flowchart LR
    Q[scientist_query<br/>DataUnitMemory] --> P0[Phase0PlanningStep<br/>Agent inside]
    P0 --> SS[SkeletonSelectorStep]
    SS --> G1[Gate1_PlanSchema<br/>fail-fast]
    G1 --> G2[Gate2_SkeletonExists]
    G2 --> G3[Gate3_HoleBindings]
    G3 --> PL[PlanLoweringStep]
    PL --> G4[Gate4_StaticValidation]
    G4 --> G5[Gate5_ResourceEnvelope]
    G5 --> OUT[lowered_workflow_yaml<br/>DataUnitFile]

    G1 -. ConditionalLink<br/>repair? .-> RP[RepairStep<br/>Agent inside]
    RP --> P0
    G2 -. .-> RP
    G3 -. .-> RP
    G4 -. .-> RP
    G5 -. .-> RP
```

Every box is a nanobrain Step. Every arrow is a nanobrain Link. The repair loop
is a `ConditionalLink` driven by an `AllDataReceivedTrigger`-style aggregator.
The Phase 0 LLM call lives inside `Phase0PlanningStep` as a nanobrain `Agent`
(with `system_prompt` in YAML per the workspace rule).

**The lowered workflow YAML produced as the orchestrator's output is then
loaded by `Workflow.from_config()` and executed.** Two nanobrain workflows in
sequence: meta-workflow constructs target-workflow; target-workflow runs.

This shape is specified in detail in `meta_workflow_orchestration.md`. It is
the apecx-mcp pattern for Strategy A and Strategy B authoring (Strategy C —
free-form synthesis — uses the same orchestrator skeleton with a different
Phase 0 prompt and a different `PlanLoweringStep` adapter).

---

## 6. The split, summarized

| Layer | Lives in | Owns |
|---|---|---|
| **Framework primitives** | `nanobrain/` | Steps, Workflows, DataUnits, Triggers, Links, Agents, Tools, Executors, ConfigBase, A2A, prompt templates, loop controllers (proposed), provenance hooks (proposed), checkpoints (proposed), UTD primitive (proposed) |
| **Apecx scientific catalog** | `apecx-mcp-integration/composition/` | Skeleton library content; specific step subclasses (RagSynthesisStep, EvidenceAccumulationStep, etc.); agent system_prompts; tool descriptor catalog |
| **Apecx MCP surface** | `apecx-mcp-integration/src/.../mcp_surface/` | FastMCP server, MCP tools, control plane integration, HITL approval surface |
| **Apecx data plane** | `apecx-mcp-integration/src/.../data/` | DatabaseStore, FAISS index loaders, synonym dictionary, Globus client, ontology resolvers |
| **Apecx deployment policy** | `apecx-mcp-integration/deploy/` (proposed) | Cluster Parsl configs, container images, secrets templates, runbooks |

The boundary is clean. Every concept in §3 maps to exactly one of these layers.
A concept that wants to live in two layers is a defect.

---

## 7. Action plan derived from this audit

In priority order. Each item references the doc(s) it touches:

1. **Author `meta_workflow_orchestration.md`** — the unification anchor (§5).
   Specifies the orchestrator as a real nanobrain workflow.
2. **Recast `agent_workflow_authoring.md`** — apply F-1 through F-3, F-6.
   Reference `meta_workflow_orchestration.md` for the canonical YAML shape.
3. **Add G14–G19 to `nanobrain_capability_gaps.md`** — the new framework gaps
   surfaced by this audit (§4.2).
4. **Recast `external_tool_integration.md §2`** — apply F-4 (ToolExecutionAgent
   is a Step, not an Agent).
5. **Recast `tool_descriptor_contract.md §1`** — apply F-5 (UTD is a nanobrain
   primitive); add the framework-side companion section.
6. **Add cross-references per §4.3** to the listed docs.
7. **Author the five gap docs** (`llm_prompt_contracts.md`,
   `agent_communication_protocol.md`, `deployment_architecture.md`,
   `security_threat_model.md`, `data_layer_evolution.md`) using audit findings
   as ground rules.
8. **Update `_design_index.md`** with the new doc inventory and refreshed
   cross-reference matrix.

---

## 8. Brutal truth — what this audit reveals about the prior session's docs

Honest assessment of the docs I authored in the previous session:

- `nanobrain_capability_gaps.md` — **good.** Holds up under audit. The 13 gaps
  are accurate; G14–G19 are additions, not corrections.
- `tool_descriptor_contract.md` — **mixed.** UTD shape is sound, but the framing
  positions it as APECx-specific when it should be promoted to nanobrain. F-5 fix.
- `agent_workflow_authoring.md` — **needs major recasting.** The biggest defect:
  treating the orchestrator as procedural code instead of as a nanobrain
  workflow. F-1 through F-3 and F-6 all apply here. ~40% of the doc surface
  changes.
- `hpc_reproducibility_spec.md` — **good.** The provenance/manifest split is
  honest: framework owns per-step records (G4), apecx-mcp owns the manifest.
- `reasoning_patterns_library.md` — **good.** Framework-neutral throughout.
- `hitl_safety_gates.md` — **good.** Correctly grounds in `ApprovalStep` rather
  than inventing a new gate primitive.
- `external_tool_integration.md` — **inherited defect (F-4).** "ToolExecutionAgent"
  was a category error. Lives in a previous-session doc I did not author, but
  needs the same recasting.

The recasting work is concentrated in `agent_workflow_authoring.md` and
`external_tool_integration.md`. The other docs need only cross-reference updates.

---

## 9. Open questions

1. **Promotion timing for G15 (`UnifiedToolDescriptor`).** Does it ship with the
   first Rhea integration, or after a second consumer (Galaxy or native catalog)
   materializes? The "two-consumer" rule from §2 suggests waiting; the
   architectural cleanliness of the alignment suggests promoting now.
2. **`PromptTemplate` (G14) vs. `prompt_template_manager.py`.** The manager file
   exists in nanobrain but its API is not yet inventoried. If it already
   provides the substitution + content-addressing surface, G14 reduces to a
   contract documentation effort, not a framework change.
3. **LoopController (G18) vs. relaxing cycle detection.** Two paths exist: a
   dedicated `LoopController` step that the cycle detector recognizes and
   permits, vs. a per-workflow flag (`allow_cycles`). The audit prefers the
   former (explicit > implicit).
4. **Should this audit re-run on every PR that touches `docs/`?** A minimal
   auto-check could grep for "code-owned function", "abstract interface", and
   "agent" used outside of `Agent` to flag potential duplications.

---

## 10. Cross-references

| Document | Why it matters |
|---|---|
| `_design_index.md` | This audit is consumed by the index; index links back |
| `nanobrain_capability_gaps.md` | G14–G19 are added to its catalog (§4.2) |
| `meta_workflow_orchestration.md` | The unification anchor (§5); written as a direct consequence of this audit |
| `agent_workflow_authoring.md` | Recast per F-1, F-2, F-3, F-6 |
| `external_tool_integration.md` | Recast per F-4 |
| `tool_descriptor_contract.md` | Recast per F-5 |
| `nanobrain/CLAUDE.md` | The nanobrain repo policy that governs G14–G19 implementation |
| `.claude/skills/nanobrain-*/SKILL.md` | Authoritative framework-behavior references |
