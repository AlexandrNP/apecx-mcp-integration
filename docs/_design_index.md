# APECx Design Package — Master Index

**Status:** Living document — update when any referenced doc changes status
**Audience:** All engineers, framework contributors, HPC operators, reviewers
**Maintained by:** Whoever last touched a referenced doc (update the status column)

---

## 1. Purpose

This index is the entry point for the APECx multi-agent + nanobrain design package.
It does three things:

1. Lists every design document with its scope, status, and line count.
2. Provides role-based reading guides — what to read first depending on what you
   are trying to do.
3. Captures the cross-reference matrix so you can find where a given concept is
   authoritatively defined.

It does **not** contain design decisions itself. All decisions live in the individual
documents. If this index and a referenced doc disagree, the referenced doc wins.

---

## 2. Document Inventory

### 2.1 Current-state documentation (describes what is built today)

| Document | What it covers | Status | Lines |
|---|---|---|---|
| `architecture.md` | Canonical current-state map: 3-tier topology, synthesis pipeline, MCP tools, ontologies, resolution strategies, test surface, config flow, brutal-truth list | Current | 917 |
| `mcp_surface.md` | Per-tool MCP reference (inputs, outputs, error shapes) | Current | — |
| `mcp_integration.md` | Operator setup: Claude Desktop config, env vars, troubleshooting | Current | — |
| `QUICKSTART.md` | Operator quickstart | Current | — |
| `domain_workflow.md` | Domain DB × Genomics DB synonym gate workflow | Current | — |
| `tutorial/` | Four-chapter tutorial series | Current | — |

### 2.2 Target-state design documents (describes the system being built)

These six documents define the tiered multi-agent architecture. They are
**complementary** — each covers one concern. Read them together; no single
document is self-sufficient.

| Document | What it defines | Status | Lines |
|---|---|---|---|
| `multiagent_architecture.md` | Four-tier architecture overview: Tier 0 (MCP frontend), Tier 1 (orchestrators), Tier 2A/B/C (retrieval / tool / synthesis agents), Tier 3 (data + HPC layer); component inventory; what exists vs. what must be built | Design | 534 |
| `workflow_output_contract.md` | The 7-phase output every analytical workflow must produce; JSON schemas for ExecutionPlan, LayerResult, EvidenceBundle, FinalResponse, FollowupQuestions; grounding gate rules; conversational chaining contract | Design | — |
| `nanobrain_workflow_design.md` | How nanobrain implements the output contract: static DAG with ConditionalLink gating, 9-step skeleton descriptions, HypothesisTournamentStep, session state flow, HPC bundle provenance, YAML config structure | Design | — |
| `external_tool_integration.md` | Rhea and GalaxyMCP integration architecture; ToolExecutionAgent abstract contract; ProxyStore reference-based I/O model; failure degradation table; security constraints | Design | — |
| `development_roadmap.md` | 5-phase delivery plan; per-phase deliverables, dependencies, integration-test requirements, open questions | Design | — |

### 2.3 New design documents (this package — added 2026-05-08)

**Cohort 1 — Core analytical workflow + tool contract (original package):**

| Document | What it defines | Status | Lines |
|---|---|---|---|
| `agent_workflow_authoring.md` | How an orchestrator agent turns a scientist question into a runnable nanobrain workflow: three authoring strategies (A/B/C), ExecutionPlan JSON schema, skeleton library, plan-to-YAML lowering, 5-gate validation pipeline, repair contract, conversation chaining. Nanobrain DataUnit/Step/LoopController framing for all components. | Design | ~720 |
| `hpc_reproducibility_spec.md` | What makes a workflow "HPC-ready and reproducible": three R-tiers, reproducibility manifest schema, HPC bundle v2 layout, provenance JSONL graph, deterministic-environment contract, replay protocol, executor profiles (Polaris/Aurora), stochasticity budget | Design | 756 |
| `tool_descriptor_contract.md` | Unified Tool Descriptor (UTD) v1 schema; three backend adapters (Rhea, native nanobrain, GalaxyMCP); discovery + execution protocol; result-typing into DataUnits; cost declarations; catalog governance; migration from today's 23 MCP tools | Design | 899 |
| `nanobrain_capability_gaps.md` | 20 nanobrain framework gaps (G1–G20): G1–G13 original gaps + G14 PromptTemplate, G15 UnifiedToolDescriptor, G16 ExecutionPlanConfig, G17 PlanLoweringStep/SkeletonLoaderStep, G18 LoopController, G19 SignedConfig, G20 class-path whitelist. Each gap: Symptom / Root cause / Proposal / API sketch / Impact / Migration. | Design | 1558 |
| `reasoning_patterns_library.md` | 10 reusable multi-agent reasoning patterns (P1–P10): Decompose & Fan-out, Hypothesis Tournament, Refinement Loop, Debate, Manager/Worker/CEO, Branch-and-Prune, Retry-with-Feedback, Evidence Concordance, Capability Gap Declaration, Conversation Chaining. Each with YAML skeleton, failure modes, cost profile | Design | 1118 |
| `hitl_safety_gates.md` | Catalog of 11 HITL/safety/cost approval gates (GATE-A1–A2, R1–R3, C1, D1–D3, P1–P2); gate taxonomy; approval lifecycle; approver payloads; default policies by role; capability tokens; cost accounting; audit/compliance | Design | 591 |

**Cohort 2 — Infrastructure, alignment, and cross-cutting concerns (added 2026-05-08):**

| Document | What it defines | Status | Lines |
|---|---|---|---|
| `nanobrain_alignment_audit.md` | Catalogue of 56 APECx concepts against nanobrain framework primitives; 4-bucket classification (USE-AS-IS / EXTEND-NANOBRAIN / APECX-SPECIFIC / DUPLICATES-REWRITE); 6 DUPLICATES findings (F-1 to F-6) and corrective rewrites; 7 new gap proposals (G14–G20); split rule (what belongs in nanobrain vs. apecx-mcp) | Design | 360 |
| `meta_workflow_orchestration.md` | The orchestrator that builds analytical workflows IS itself a nanobrain workflow — Phase 0 through Phase 4 as Steps/DataUnits/Links; annotated orchestrator.yml (290 lines); G14/G16/G17/G18 dependencies; "unification anchor" design rationale | Design | 1062 |
| `llm_prompt_contracts.md` | 8 prompt families (PROMPT-P0/SS/PB/TS/RP/SY/TM/JG); PromptTemplate YAML examples; 3-tier output enforcement; regression tests; injection threat model; anti-drift rules | Design | ~910 |
| `deployment_architecture.md` | 3 deployment modes (L/C/H); service catalogue; secrets management; HPC integration; 14 failure modes; 5-phase L→C→H migration; infrastructure-as-code sketches | Design | 899 |
| `agent_communication_protocol.md` | A2A protocol formalization; 5 communication patterns (CP-1 to CP-5); message envelope schema; error catalog; 3 Mermaid sequence diagrams | Design | 579 |
| `security_threat_model.md` | STRIDE model; 8 threat deep-dives (T-PI-1/2/3, T-DP-1, T-SK-1, T-EX-1, T-PS-1, T-CL-1); 12 mitigations; G19 SignedConfig and G20 class-path whitelist proposals | Design | 679 |
| `data_layer_evolution.md` | 14 data sources; lifecycle workflows as nanobrain workflows; Globus migration phases; versioning/snapshot/schema-evolution; data source registry | Design | 843 |

**Cohort 3 — Implementation planning (added 2026-05-08):**

| Document | What it defines | Status | Lines |
|---|---|---|---|
| `implementation_task_graph.md` | 165 file-level tasks across 4 tracks: Track A nanobrain framework (G1–G22 implementation, 87 tasks), Track B apecx-mcp-integration (57 tasks across phases 0–5 + autonomy sub-track), Track C Rhea fork (10 tasks for UTD support), Track D cross-track integration (11 e2e tests + deployments). Each task has stable ID, file targets, dependencies, effort estimate, and DoD. Includes critical-path analysis (Phase 4 + autonomous-agent sub-path) and a maintenance protocol. | Implementation | ~780 |

**Cohort 4 — Autonomous operation (added 2026-05-09):**

| Document | What it defines | Status | Lines |
|---|---|---|---|
| `autonomous_workflow_agent.md` | Long-lived autonomous orchestrator: same code as the interactive meta-workflow, different lifecycle. Three autonomy modes (`strict_hitl` / `opt_in_hitl` / `pure_autonomous`); trigger model (TimerTrigger / ManualTrigger / EventTrigger); deferred-HITL via approvals-table polling (no MCP push in v1; reserved for v2 G23); multi-session task identity (`task_id`); cost envelope; operator MCP tools; failure modes; T-AU-1 / T-AU-2 threats. | Design | ~700 |

---

## 3. Reading Guide by Role

### 3.1 New engineer onboarding

Read in this order:

1. `architecture.md` — understand what is built and running today
2. `multiagent_architecture.md` — understand the target architecture
3. `workflow_output_contract.md` — understand what every workflow must produce
4. `development_roadmap.md` — understand sequencing and what is next

Then pick a lane based on what you are working on (see §3.2–§3.6).

### 3.2 Workflow author (building a new analytical workflow)

1. `nanobrain_workflow_design.md` — the nanobrain DAG patterns
2. `agent_workflow_authoring.md` — how the orchestrator constructs a workflow
3. `reasoning_patterns_library.md` — which pattern shape fits the problem
4. `tool_descriptor_contract.md` — how to declare and bind tools
5. `hitl_safety_gates.md` — which gates fire on your workflow type
6. `.claude/skills/nanobrain-workflow-authoring/SKILL.md` — implementation rules

### 3.3 Framework contributor (extending nanobrain)

1. `nanobrain_alignment_audit.md` — what's already right, what's a duplicate, what gaps to fill (read first)
2. `nanobrain_capability_gaps.md` — the 20 gaps (G1–G20), their proposals, sequencing, and API sketches
3. `implementation_task_graph.md` §3 (Track A) — the file-level tasks that turn each gap into commits
4. `nanobrain/CLAUDE.md` — nanobrain repo policy
5. `nanobrain_workflow_design.md` §1–§2 — the static-DAG-with-conditional-gating premise that drives most gaps
6. `agent_workflow_authoring.md` §6 — the 5-gate validation pipeline that consumes framework primitives
7. `.claude/skills/nanobrain-*/SKILL.md` — all 8 skills (authoritative framework reference)

### 3.4 HPC operator (deploying workflows to Polaris/Aurora)

1. `hpc_reproducibility_spec.md` — the full reproducibility contract, bundle layout, executor profiles
2. `deployment_architecture.md` — 3 deployment modes; HPC mode (H) infrastructure; secrets management
3. `hitl_safety_gates.md` §8 — cost and resource accounting
4. `architecture.md` §4.6 — existing HPC tools (`estimate_cost`, `export_hpc_bundle`, `ingest_hpc_bundle`)
5. `multiagent_architecture.md` §7.3 — HITL gate before HPC submission
6. `apecx-mcp-integration/CLAUDE.md` §PBS bundle export and Academy integration sections

### 3.5 Orchestrator / agent author (building Tier-1 agents)

1. `agent_workflow_authoring.md` — the full authoring contract
2. `workflow_output_contract.md` — what Phase 0 must emit
3. `reasoning_patterns_library.md` — which pattern to instantiate for the query type
4. `tool_descriptor_contract.md` — tool discovery and binding
5. `hitl_safety_gates.md` — which gates the agent must respect
6. `multiagent_architecture.md` §5 — orchestrator design contract

### 3.6 Tool integration author (adding a new Rhea or native tool)

1. `tool_descriptor_contract.md` — the full UTD schema and backend adapter contract
2. `external_tool_integration.md` — Rhea architecture details
3. `nanobrain_capability_gaps.md` G11 — the proposed ToolStep base class
4. `hitl_safety_gates.md` §7 — capability token declarations

---

## 4. Cross-Reference Matrix

Each row is a concept; the column is the document that is authoritative for it.
"Ref" means the concept is used but not defined there — follow the link for the definition.

| Concept | Authoritative | Referenced in |
|---|---|---|
| 4-tier architecture | `multiagent_architecture.md` | All design docs |
| 7-phase workflow output contract | `workflow_output_contract.md` | `nanobrain_workflow_design.md`, `agent_workflow_authoring.md`, `reasoning_patterns_library.md` |
| Static-DAG + ConditionalLink gating | `nanobrain_workflow_design.md §1–§2` | `agent_workflow_authoring.md §5`, `nanobrain_capability_gaps.md G1` |
| ExecutionPlan JSON schema | `agent_workflow_authoring.md §3` | `hpc_reproducibility_spec.md §3`, `hitl_safety_gates.md GATE-A1` |
| Skeleton library | `agent_workflow_authoring.md §4` | `hpc_reproducibility_spec.md §3`, `reasoning_patterns_library.md` |
| Plan-to-YAML lowering | `agent_workflow_authoring.md §5` | `hpc_reproducibility_spec.md §3` (plan_hash), `nanobrain_capability_gaps.md G9` |
| 5-gate validation pipeline | `agent_workflow_authoring.md §6` | `hitl_safety_gates.md §10` |
| Repair / retry contract | `agent_workflow_authoring.md §7` | `reasoning_patterns_library.md P7` |
| Reproducibility tiers (R1/R2/R3) | `hpc_reproducibility_spec.md §2` | `tool_descriptor_contract.md §2` (determinism field), `hitl_safety_gates.md §11` |
| Reproducibility manifest | `hpc_reproducibility_spec.md §3` | `agent_workflow_authoring.md §5`, `tool_descriptor_contract.md §10` |
| HPC bundle v2 layout | `hpc_reproducibility_spec.md §4` | `multiagent_architecture.md §7.3` |
| Provenance graph / JSONL | `hpc_reproducibility_spec.md §5` | `tool_descriptor_contract.md §5`, `hitl_safety_gates.md §9` |
| Unified Tool Descriptor (UTD) | `tool_descriptor_contract.md §2` | `agent_workflow_authoring.md §3` (tool_invocations), `hpc_reproducibility_spec.md §3`, `hitl_safety_gates.md GATE-A2` |
| Tool discovery protocol | `tool_descriptor_contract.md §4` | `agent_workflow_authoring.md §3` |
| ProxyStore I/O model | `external_tool_integration.md §4` | `tool_descriptor_contract.md §5`, `hpc_reproducibility_spec.md §8`, `nanobrain_capability_gaps.md G3/G13` |
| Rhea integration | `external_tool_integration.md §3` | `tool_descriptor_contract.md §3.1` |
| Nanobrain capability gaps | `nanobrain_capability_gaps.md` | All design docs (each gap is referenced by the doc whose requirement drives it) |
| ConditionalLink predicate DSL (G1) | `nanobrain_capability_gaps.md G1` | `agent_workflow_authoring.md §5`, `nanobrain_workflow_design.md §2` |
| Dynamic AllDataReceived (G2) | `nanobrain_capability_gaps.md G2` | `nanobrain_workflow_design.md §4` |
| DataUnitProxyRef (G3) | `nanobrain_capability_gaps.md G3` | `tool_descriptor_contract.md §7`, `hpc_reproducibility_spec.md §6` |
| auto_transfer default flip (G7) | `nanobrain_capability_gaps.md G7` | `architecture.md §13` brutal-truth #3 |
| Reasoning patterns (P1–P10) | `reasoning_patterns_library.md` | `agent_workflow_authoring.md §8`, `multiagent_architecture.md §5–§8` |
| Hypothesis tournament | `reasoning_patterns_library.md P2` | `multiagent_architecture.md §8.2`, `nanobrain_workflow_design.md §3.5` |
| HITL gate catalog | `hitl_safety_gates.md §3` | `agent_workflow_authoring.md §6 Gate 5`, `multiagent_architecture.md §4.3` |
| Capability tokens | `hitl_safety_gates.md §7` | `tool_descriptor_contract.md §6` |
| Cost accounting | `hitl_safety_gates.md §8` | `tool_descriptor_contract.md §8`, `hpc_reproducibility_spec.md §9` |
| Session / conversation chaining | `workflow_output_contract.md §10` | `agent_workflow_authoring.md §8`, `reasoning_patterns_library.md P10` |
| DirectLink auto_transfer silent failure | `architecture.md §13` brutal-truth #3 | `nanobrain_capability_gaps.md G7`, `agent_workflow_authoring.md §9` |
| wait_for_cascade | `architecture.md §3.4` | `nanobrain_capability_gaps.md G8` |
| Nanobrain alignment audit findings (F-1–F-6) | `nanobrain_alignment_audit.md` | `agent_workflow_authoring.md §3/§5/§6/§7`, `external_tool_integration.md §2`, `tool_descriptor_contract.md header` |
| Meta-workflow orchestrator as nanobrain workflow | `meta_workflow_orchestration.md` | `nanobrain_alignment_audit.md §5`, `agent_workflow_authoring.md §3` |
| LLM prompt contracts (8 prompt families) | `llm_prompt_contracts.md` | `meta_workflow_orchestration.md §3`, `agent_workflow_authoring.md §3` |
| Deployment modes (L/C/H) | `deployment_architecture.md §2` | `hpc_reproducibility_spec.md §10`, `multiagent_architecture.md §11` |
| A2A communication protocol | `agent_communication_protocol.md` | `multiagent_architecture.md §6`, `agent_workflow_authoring.md §8` |
| Security threat model (STRIDE 8 threats) | `security_threat_model.md` | `nanobrain_capability_gaps.md G19/G20`, `hitl_safety_gates.md §5` |
| Data layer lifecycle (14 sources) | `data_layer_evolution.md` | `multiagent_architecture.md §9`, `hpc_reproducibility_spec.md §8` |
| ExecutionPlanConfig / ExecutionPlanDataUnit (G16) | `nanobrain_capability_gaps.md G16` | `agent_workflow_authoring.md §3`, `meta_workflow_orchestration.md §3` |
| PlanLoweringStep / SkeletonLoaderStep (G17) | `nanobrain_capability_gaps.md G17` | `agent_workflow_authoring.md §5` |
| LoopController (G18) | `nanobrain_capability_gaps.md G18` | `agent_workflow_authoring.md §7`, `reasoning_patterns_library.md P7` |
| PromptTemplate primitive (G14) | `nanobrain_capability_gaps.md G14` | `llm_prompt_contracts.md`, `meta_workflow_orchestration.md §3` |
| Autonomous workflow agent (long-lived orchestrator) | `autonomous_workflow_agent.md` | `meta_workflow_orchestration.md §7.5`, `agent_workflow_authoring.md §2.3` (autonomy_level), `hitl_safety_gates.md §3.1.1` (gate behavior under autonomy modes), `agent_communication_protocol.md §12.3` (A2U), `deployment_architecture.md §3` (autonomous service entry) |
| WorkflowRunner / detached run (G21) | `nanobrain_capability_gaps.md G21` | `autonomous_workflow_agent.md`, `meta_workflow_orchestration.md §7.5` |
| WorkflowEntryTrigger / EventTrigger (G22) | `nanobrain_capability_gaps.md G22` | `autonomous_workflow_agent.md §4`, `meta_workflow_orchestration.md §7.5` |
| Autonomy threats (T-AU-1, T-AU-2) | `security_threat_model.md §5.9, §5.10` | `autonomous_workflow_agent.md §10` |

---

## 5. Key Design Decisions

These decisions span multiple docs. Recorded here so they are findable in one place.

| Decision | Rationale | Where specified |
|---|---|---|
| Static DAG + ConditionalLink gating (not runtime DAG modification) | Preserves nanobrain's static validation (cycle detection, orphan detection); delivers dynamic behavior through conditional data flow | `nanobrain_workflow_design.md §1` |
| Three authoring strategies (A/B/C); free-form YAML forbidden | Silent success of a syntactically-valid but semantically-broken workflow is unrecoverable; `auto_transfer=False` alone can sink a run | `agent_workflow_authoring.md §2` |
| ExecutionPlan is JSON, not YAML; plan-to-YAML lowering is code-owned | LLM-emitted YAML has no auditable diff; code-owned lowering is deterministic and hash-anchored | `agent_workflow_authoring.md §3–§5` |
| Unified Tool Descriptor (single schema across Rhea, Galaxy, native) | Orchestrator must reason uniformly about tools; three incompatible surfaces break authoring | `tool_descriptor_contract.md §1` |
| ProxyStore keys (not data) cross step boundaries for HPC-scale I/O | Copying 5 GB between in-process steps via DataUnitMemory is not viable; keys are O(1) | `external_tool_integration.md §4`, `nanobrain_capability_gaps.md G3` |
| Reproducibility is a contract between bundle and runtime | Neither the bundle alone nor the runtime alone can guarantee replay; the manifest pins the bridge | `hpc_reproducibility_spec.md §1` |
| Default-deny HITL (hard-block on authoring strategy elevation) | Strategy B/C may produce structurally-valid but unintended workflows; an operator must see the lowered YAML before it executes | `hitl_safety_gates.md §6` |
| Capability tokens expire at session end (except PHI, which is per-workflow) | Prevents ambient accumulation of sensitive capabilities across unrelated workflows | `hitl_safety_gates.md §7` |
| 13 nanobrain gaps are additive except G7 (auto_transfer default flip) | Breaking changes to the framework require a config-version bump; all other proposals extend without modifying existing behavior | `nanobrain_capability_gaps.md §4` |

---

## 6. Consolidated Open Questions

Collected from all design docs. Resolve before starting the implementation phase that depends on the answer.

### Blocking Phase 0 / Phase 1

| # | Question | Lives in |
|---|---|---|
| OQ-1 | Skeleton versioning: SHA-256 of skeleton.yml or semver tag from a registry manifest? | `agent_workflow_authoring.md §10` |
| OQ-2 | Is Strategy B (skeleton composition) a separate composer LLM call or folded into Phase 0? | `agent_workflow_authoring.md §10` |
| OQ-3 | Rhea deployment model: sidecar alongside apecx-mcp or separate long-lived service? | `multiagent_architecture.md §12` |
| OQ-4 | Does G7 (auto_transfer default flip) require a framework-wide config-version bump, or can it be a per-link opt-in? | `nanobrain_capability_gaps.md G7` |
| OQ-5 | Does Galaxy MCP provide a local deployable server? (Required before GalaxyToolAgent is implemented) | `external_tool_integration.md §4.3`, `tool_descriptor_contract.md §3.3` |

### Blocking Phase 2 / Phase 3

| # | Question | Lives in |
|---|---|---|
| OQ-6 | Evidence scoring function for P2 (Hypothesis Tournament): LLM-judged vs. rule-based vs. hybrid? | `reasoning_patterns_library.md §7` |
| OQ-7 | Can AllDataReceived be dynamically configured per run (G2)? Verify against `trigger.py` before Phase 2. | `nanobrain_capability_gaps.md G2` |
| OQ-8 | Should every bundle be signed, or only HPC-eligible bundles? | `hpc_reproducibility_spec.md §12` |
| OQ-9 | How does ProxyStore persist across PBS job boundaries on Polaris / Aurora? | `hpc_reproducibility_spec.md §12` |
| OQ-10 | Cost actuals: recorded per-step or per-workflow? (Affects telemetry schema for UTD updates) | `hitl_safety_gates.md §12` |

---

## 7. What Is Not Yet Designed

These items are explicitly out of scope for this package but must be designed before
they are implemented:

- **GalaxyMCP adapter implementation** — deferred until Galaxy MCP availability confirmed
  (`external_tool_integration.md §4.3`, `tool_descriptor_contract.md §3.3`)
- **Globus → SphericalDB migration** — deferred; `multiagent_architecture.md §9`
  has the migration path but the Globus timeline is open (`multiagent_architecture.md §12`)
- **Docker sandbox Phase 3 wiring** — T13b scaffold exists
  (`apecx-mcp-integration/CLAUDE.md §T13b`) but is not yet wired into the execution path
- **StructBioReasoner integration** — the patterns are adopted (`reasoning_patterns_library.md §6`),
  no code dependency is planned; no further design needed
- **Fine-tuned intent classifier** — currently specified as an LLM call; whether a
  fine-tuned classifier is warranted depends on query volume (open question from
  `multiagent_architecture.md §4.3`)

---

## 8. Document Authorship and Change Protocol

All documents in §2.3 are design documents — they describe intent, not current state.
When implementation decisions diverge from the design, update the doc, not just the code.

**Change protocol:**
1. Edit the relevant design doc in a branch.
2. Update the cross-reference matrix in this file if a new concept or authoritative
   source changes.
3. Update the open-questions table if a question is resolved or a new one surfaces.
4. The `development_roadmap.md` is the sequencing authority — update it if a phase
   boundary or deliverable changes.

**This index does not need a PR of its own.** Update it in the same commit that
updates the referenced doc.
