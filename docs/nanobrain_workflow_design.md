# APECx Nanobrain Workflow Design — Layered Reasoning Architecture

**Status:** Design / pre-implementation
**Audience:** Nanobrain workflow and step authors
**Supplements:** `multiagent_architecture.md`, `workflow_output_contract.md`
**Read first:** `.claude/skills/nanobrain-step-authoring/SKILL.md`,
`.claude/skills/nanobrain-workflow-authoring/SKILL.md`,
`.claude/skills/nanobrain-agents-tools/SKILL.md`

---

## 1. The Central Design Challenge

The `workflow_output_contract.md` requires that the number of active domain-reasoning
layers be **determined at runtime** by Phase 0 (planning), not statically at workflow
definition time. A question requiring sequence-level analysis activates the sequence
layer. A design question activates all retrieval layers plus a design layer and tool
execution. The layer set is query-driven.

Nanobrain workflows are defined in YAML with a static DAG. This appears to conflict with
the dynamic layer requirement. The resolution is:

> **Define the maximum possible DAG statically (all layers present).
> Use ConditionalLinks from Phase 0 to gate each layer's execution.
> Layers that Phase 0 does not activate produce empty bundles and are
> excluded from evidence accumulation.**

This approach keeps all workflow behavior in YAML (no runtime DAG modification), preserves
nanobrain's static validation (cycle detection, orphan detection), and delivers dynamic
behavior through conditional data flow rather than dynamic structure.

---

## 2. The LayeredReasoningWorkflow — Static DAG Shape

```
Phase0PlanningStep
  ├──[ConditionalLink: "sequence" in plan]────► SequenceLayerStep
  ├──[ConditionalLink: "structural" in plan]──► StructuralLayerStep
  ├──[ConditionalLink: "functional" in plan]──► FunctionalLayerStep
  ├──[ConditionalLink: "evidence" in plan]────► EvidenceLiteratureLayerStep
  ├──[ConditionalLink: "design" in plan]──────► DesignLayerStep
  └──[DirectLink: always]─────────────────────► EvidenceAccumulationStep

SequenceLayerStep ──[DirectLink]──────────────► EvidenceAccumulationStep
StructuralLayerStep ──[DirectLink]────────────► EvidenceAccumulationStep
FunctionalLayerStep ──[DirectLink]────────────► EvidenceAccumulationStep
EvidenceLiteratureLayerStep ──[DirectLink]─────► EvidenceAccumulationStep
DesignLayerStep ──[DirectLink]────────────────► EvidenceAccumulationStep
(ToolExecutionStep invoked inline within active layer steps)

EvidenceAccumulationStep ──[DirectLink]───────► CrossSourceIntegrationStep
CrossSourceIntegrationStep ──[DirectLink]──────► HypothesisTournamentStep (design only)
                                              OR
                            ──[DirectLink]──────► ResponseSynthesisStep (all others)
HypothesisTournamentStep ──[DirectLink]───────► HITLGateStep
HITLGateStep ──[DirectLink: approved]─────────► ResponseSynthesisStep
ResponseSynthesisStep ──[DirectLink]──────────► FollowupGenerationStep
```

**Key constraints:**

1. Each layer step runs at most once per workflow execution.
2. The `EvidenceAccumulationStep` uses an `AllDataReceived` trigger that fires only after
   all active layer steps have produced output (or been gated out via ConditionalLink).
   Inactive layers do not contribute output — the trigger must be configured with the
   active layer set from the Phase 0 output.
3. The `DesignLayerStep` → `HypothesisTournamentStep` → `HITLGateStep` path is only
   activated for design-type queries. Retrieval and reasoning queries bypass the tournament
   and go directly to synthesis.

---

## 3. Step Contracts

### 3.1 Phase0PlanningStep

**Class:** `BaseStep` subclass
**Config:** `phase0_planning_step.yml`

```
process(input: QueryBundle) → ExecutionPlan
```

`QueryBundle` contains:
- `query: str` — the raw user question
- `session_context: SessionContext?` — accumulated session evidence

The step:
1. Calls the intent classifier (lightweight LLM call against `APECX_LLM_*`)
2. Determines which layers are needed based on intent
3. Identifies which layers can reuse session evidence vs. must be freshly retrieved
4. Returns `ExecutionPlan` (schema in `workflow_output_contract.md §3.2`)

Output data unit: `ExecutionPlanDataUnit` (Memory data unit, `auto_transfer=True`)

The `ConditionalLink` from this step to each domain layer step checks
`plan.layers[*].layer_type` and gates the downstream step accordingly.

**Nanobrain constraint:** The conditional evaluation in `ConditionalLink` is a
pure Python function declared in the link config. It receives the `ExecutionPlan`
and returns `bool`. No LLM call inside the conditional function.

**Gap dependency:** Today the predicate is a free-form Python callable, which
is unsafe for agent-authored YAML (an LLM can hallucinate import paths). Gap
**G1** in `nanobrain_capability_gaps.md` proposes a declarative predicate DSL
(`{op, field, value}` + `{op: all|any|not, of: [...]}`) that the LLM can
synthesize safely. Once G1 ships, every example in this document expressing
"sequence in plan" or similar should be re-expressed in the DSL form.

The `ExecutionPlanDataUnit` itself is gap **G16** (`ExecutionPlanConfig` +
`ExecutionPlanDataUnit` primitives). Until G16 ships, apecx-mcp uses a
hand-rolled `pydantic.BaseModel` and a plain `DataUnitMemory`.

### 3.2 DomainLayerStep (generic base)

All domain layer steps share a common base class `DomainLayerStep(BaseStep)`:

```
process(input: LayerInput) → LayerResult
```

`LayerInput` contains:
- `layer_config: LayerConfig` — from the `ExecutionPlan` (which sources, which tools)
- `prior_findings: List[Finding]` — findings from dependency layers (passed via DirectLink)
- `session_evidence: LayerResult?` — cached result from prior session turn (if reusing)

Each concrete subclass overrides `_retrieve_and_reason(layer_config, prior_findings)`:

| Step | Primary source category | Typical tool invocations |
|---|---|---|
| `SequenceLayerStep` | Sequence repositories | Alignment, conservation scoring, phylogeny |
| `StructuralLayerStep` | 3D structure repositories | Surface analysis, binding calculations, spatial mapping |
| `FunctionalLayerStep` | Experimental assay databases | Measurement extraction, assay validation |
| `EvidenceLiteratureLayerStep` | Literature, domain knowledge base | Semantic search, meta-analysis synthesis |
| `DesignLayerStep` | All sources + tools | Variant optimization, property prediction |

Each step fails gracefully: source failures populate `LayerResult.errors[]` and return
a partial or empty `LayerResult`. The step never raises an exception for source failures
(only for configuration errors that fire FAIL-FAST at initialization).

Output data unit: `LayerResultDataUnit` (Memory data unit, `auto_transfer=True`)

### 3.3 EvidenceAccumulationStep

**Extends:** `SynthesisContextAssemblyStep` (existing)
**Config:** `evidence_accumulation_step.yml`

```
process(input: AllLayerResults) → EvidenceBundle
```

Trigger: `AllDataReceived` — fires when all active layer steps have produced output.
The trigger's `data_units` list is configured at workflow instantiation time based on
the active layer set from `ExecutionPlan`.

**Design note on AllDataReceived vs. DataUnitChange:**
Using `AllDataReceived` is intentional. `DataUnitChange` (which fires on each arrival)
cannot be used here because `EvidenceAccumulationStep` needs all layer results before it
can compute cross-source concordance. The trigger is configured to wait for only the
active layers (not all possible layers), so inactive layers do not cause an indefinite wait.

**Gap dependency:** "configured to wait for only the active layers" requires
gap **G2** in `nanobrain_capability_gaps.md` — today, `AllDataReceivedTrigger`'s
expected set is bound at trigger init, not at run time. Without G2, every
gated-off layer step has to publish a sentinel "empty bundle" so the trigger
fires anyway. G2's `expected_set_source: workflow.execution_plan` +
`expected_set_field: active_layers` is the clean solution. The combined
ConditionalLink-gated + AllDataReceived-narrowed pattern also depends on gap
**G10** (deadlock-pattern resolution) — without G10, a workflow that gates
all layers off can deadlock the trigger. G10 specifies the framework-level
fix (gate-to-bottom semantics).

The step:
1. Collects `LayerResult` from all active layers
2. Merges findings, deduplicates, computes per-finding confidence
3. Runs `CanonicalEntityResolver` across all source entities
4. Produces `EvidenceBundle` (superset of current `SynthesisContextAssemblyStep` bundle)

**Extended bundle fields** (additions to current shape):

```python
{
    # Existing fields (preserved):
    "query": str,
    "rag_chunks": list[dict],
    "publications": list[dict],

    # New fields:
    "execution_plan": ExecutionPlan,
    "layer_results": dict[str, LayerResult],   # keyed by layer_id
    "source_records": list[dict],              # records from all configured sources
    "computational_results": list[dict],       # tool-computed intermediate results
    "experimental_records": list[dict],        # experimental assay data
    "tool_outputs": list[dict],                # Rhea/Galaxy results with provenance
    "entity_registry": dict[str, ResolvedEntity],
    "cross_source_concordance": dict[str, float],
    "capability_gaps": list[str],
}
```

Failure contract: each layer branch degrades to empty on error; `fail_on_empty_retrieval`
fires only if all branches are empty.

### 3.4 CrossSourceIntegrationStep

**Class:** `BaseStep` subclass
**Config:** `cross_source_integration_step.yml`

```
process(input: EvidenceBundle) → IntegrationResult
```

This step implements the cross-source integration phase from the output contract:

1. Entity reconciliation across sources (aligning identifiers, canonical name resolution)
2. Cross-source concordance scoring
3. Conflict detection and resolution attempts (flagging unresolved conflicts)
4. Produces `IntegrationResult` with merged findings sorted by confidence

The `CanonicalEntityResolver.resolve()` call here is the canonical single call —
layer steps must not perform their own entity normalization.

### 3.5 HypothesisTournamentStep (design-type queries only)

Adapted from the StructBioReasoner multi-agent pattern. Activated only when
`ExecutionPlan.intent == "design"`.

```
process(input: IntegrationResult) → RankedHypotheses
```

The step spawns N specialized proposer agents in parallel (using nanobrain Academy agents).
The number and type of proposer agents is configurable per workflow deployment — they are
not fixed in the framework. Each agent focuses on a distinct analytical lens (e.g.,
geometric feasibility, comparative analysis, thermodynamic stability, application constraints).

Each proposer returns a structured `Hypothesis`:

```python
{
    "hypothesis_id": str,
    "title": str,
    "description": str,
    "strategy": str,
    "rationale": str,
    "confidence": float,              # proposer's self-assessed confidence
    "supporting_findings": list[str], # finding_ids from IntegrationResult
    "design_variants": dict[str, str],
}
```

**Ranking:** The tournament ranker scores hypotheses by:
1. Evidence coverage: `len(supporting_findings) / total_available_findings`
2. Confidence: `hypothesis.confidence` (proposer-assessed)
3. Orthogonality: diversity penalty for hypotheses with overlapping `supporting_findings`

The top-N hypotheses (configurable, default: 3) are passed to the HITL gate.

**Bounded history tracking:**
Each proposer agent maintains a bounded history of prior hypotheses it generated in
the current session (`num_history` configurable, default: 5 turns). This enables adaptive
refinement across conversation turns — proposer agents have context on what was already
explored when follow-up questions are asked.

**Graceful degradation:** If a proposer agent fails, it returns a structured error
object (not an exception), and the tournament proceeds with the remaining proposers.
A tournament with fewer than 2 proposers producing output bypasses ranking and surfaces
whatever is available, with a warning in the capability gaps.

### 3.6 HITLGateStep

```
process(input: RankedHypotheses) → ApprovedHypothesis
```

This step blocks execution and surfaces the top-N hypotheses to the MCP client via
the existing `approve` / `reject` / `correct` tool surface. When the user approves
a hypothesis (or corrects it), execution resumes with the approved hypothesis.

The HITL gate is only present in the design-type workflow path. Retrieval and reasoning
workflows do not require HITL before synthesis.

### 3.7 ResponseSynthesisStep

**Extends:** `RagSynthesisStep` (existing)

```
process(input: EvidenceBundle | ApprovedHypothesis) → FinalResponse
```

The existing `RagSynthesisStep` is extended to:
1. Handle the richer evidence bundle (source records, computational results, tool outputs)
2. Enforce the grounding gate from `workflow_output_contract.md §6.1`
3. Generate `cross_source_reasoning` and `integrated_insight` sections when evidence
   spans multiple layer types
4. Generate design variant sections for design-type queries

The prompt template (`system.md` or `system_prompt` in YAML) is updated to reflect
the new bundle structure. The extension must not break existing Path A synthesis behavior
(regression-tested in `tests/integration/test_rag_e2e_pipeline.py`).

### 3.8 FollowupGenerationStep

**Class:** `BaseStep` subclass
**Config:** `followup_generation_step.yml`

```
process(input: FinalResponse + EvidenceBundle) → FollowupQuestions
```

Generates three follow-up questions per the contract in `workflow_output_contract.md §8`.
Uses a short LLM call constrained by:
1. The active layer set (Phase 0 plan) — follow-ups must not suggest layers already exhausted
2. The capability gaps list — follow-ups can suggest filling a gap as a next step
3. The evidence sources used — follow-ups should suggest new sources not yet queried

---

## 4. Session State and Conversational Chaining

### 4.1 SessionContext as workflow input

Every `LayeredReasoningWorkflow` execution accepts an optional `SessionContext` as a
top-level input parameter (passed through the workflow's input data unit). The session
state schema is defined in `workflow_output_contract.md §9`.

The session context is stored in the control plane (apecx-cp), keyed by session ID.
The MCP `ask` tool creates or retrieves the session context and injects it into the
workflow input before dispatching to the orchestrator.

### 4.2 Phase 0 session reuse logic

When `session_context.accumulated_evidence` is non-empty:

1. Phase0PlanningStep computes the required layers for the new query.
2. For each required layer, it checks whether a matching `LayerResult` exists in
   `accumulated_evidence` with a compatible query scope.
3. If a match exists, the layer's `ConditionalLink` passes the cached result directly
   to `EvidenceAccumulationStep`, bypassing re-retrieval.
4. Freshly required layers (no cache match) execute normally.

**What "compatible query scope" means:** The same entity (canonical ID), the same
data source, and a cache TTL that has not expired (default: 24 hours). If the entity
or source changed, the layer is re-retrieved.

### 4.3 Session context update

After each workflow run, the updated `SessionContext` (with new `LayerResult` entries
added to `accumulated_evidence`) is written back to the control plane. The
`FollowupGenerationStep` receives the full updated context to ensure follow-up questions
reference real evidence IDs.

---

## 5. HPC-Ready Workflow Outputs

### 5.1 Provenance at every step

Every step that invokes an external tool or data source must attach provenance to its
`LayerResult`:

```python
{
    "source": "source_name | Rhea/tool_name | ...",
    "query_params": {},     # the exact query issued
    "timestamp": "ISO-8601",
    "record_count": int,
    "result_keys": ["ProxyStoreKey"],  # for tool outputs
}
```

This is the data that populates `EvidencePackage.tool_outputs` and ultimately the
PBS bundle's `provenance_seed.json`.

### 5.2 HPC bundle export integration

For workflows that pass through `HITLGateStep` (design-type queries), after the
user approves a hypothesis, the workflow can produce an HPC bundle via the existing
`export_hpc_bundle` path:

```
ApprovedHypothesis + EvidenceBundle
  → HPCBundleExportStep
  → PBS bundle (see workflow_output_contract.md §10)
```

`HPCBundleExportStep` is a thin wrapper around the existing `pbs_bundle.py` that:
1. Resolves all ProxyStore keys in `EvidenceBundle.tool_outputs` to actual files
2. Writes the workflow YAML (`LayeredReasoningWorkflow` config) into the bundle
3. Packages the approved hypothesis as `inputs/hypothesis.json`
4. Generates `provenance_seed.json` for post-HPC re-ingest

### 5.3 Reproducibility requirement

A workflow is HPC-ready if and only if:
- All data inputs can be reproduced from the `provenance_seed.json` queries
- All tool inputs are stored as resolved files (not ProxyStore keys) in the bundle
- The `workflow.yml` in the bundle, run with the same inputs, produces the same
  `FinalResponse` (allowing for non-determinism in LLM calls within a tolerance window)

---

## 6. YAML Configuration Structure

The `LayeredReasoningWorkflow` is configured via a YAML hierarchy:

```
configs/
  layered_reasoning_workflow.yml      ← workflow root
  steps/
    phase0_planning_step.yml
    sequence_layer_step.yml
    structural_layer_step.yml
    functional_layer_step.yml
    evidence_literature_layer_step.yml
    design_layer_step.yml
    evidence_accumulation_step.yml
    cross_source_integration_step.yml
    hypothesis_tournament_step.yml
    hitl_gate_step.yml
    response_synthesis_step.yml
    followup_generation_step.yml
  agents/
    proposer_agent_A.yml              ← domain-specific; named per deployment
    proposer_agent_B.yml
    ...
  tools/
    rhea_tool_agent.yml
    galaxy_tool_agent.yml
```

The root `layered_reasoning_workflow.yml` wires steps via links, following the DAG
described in §2. Each layer step is wired with a `ConditionalLink` from `Phase0PlanningStep`
and a `DirectLink` to `EvidenceAccumulationStep`.

**Critical:** All agent configs must specify `system_prompt` in YAML. No hardcoded prompts
in Python code. All prompt content belongs in the agent's `.yml` file or a referenced
`prompt_template_file`. This applies to proposer agents, the intent classifier, and the
response synthesizer.

---

## 7. Extension Points

The layered architecture defines clear extension points for adding new data sources or
domains without modifying the core workflow:

| Extension | How to add |
|---|---|
| New data source | Add retrieval in the appropriate existing layer step; register source in data access config |
| New layer type | Add a `DomainLayerStep` subclass + YAML config; add a `ConditionalLink` from Phase 0 and a `DirectLink` to evidence accumulation |
| New proposer agent in the tournament | Add an agent YAML; register it in `hypothesis_tournament_step.yml` |
| New external tool | Register in Rhea's catalog; `ToolExecutionStep` handles dispatch without APECx changes |
| New domain orchestrator | Create a new `Workflow` YAML that reuses the same layer step classes with domain-specific configurations and data source bindings |

---

## 8. What NOT to Do

These patterns are explicitly prohibited:

1. **Do not modify `execute()` in any layer step.** Implement `process()` only.
2. **Do not hardcode prompts in step Python files.** System prompts belong in agent YAML.
3. **Do not move data units from layer steps to the workflow level.** Steps own their
   data units; the workflow owns links.
4. **Do not use TransformLink** in the layered reasoning DAG. Use DirectLink + a
   dedicated conversion step if shape bridging is required.
5. **Do not retry source failures inline.** Source failures produce `LayerResult.errors[]`
   entries and empty bundles. Retry logic belongs in the data access layer, not in steps.
6. **Do not instantiate steps directly.** Use `from_config()`. Direct constructors raise
   `RuntimeError`.
7. **Do not omit `auto_transfer: true` on a DirectLink.** This is the dominant
   silent-failure shape (`architecture.md §13` brutal-truth #3). The workflow
   loads cleanly and every link is a runtime no-op. Gap **G7** in
   `nanobrain_capability_gaps.md` proposes flipping the default in a v2 config
   schema; until G7 ships, every DirectLink in this design must declare
   `auto_transfer: true` explicitly.

---

## 9. Gap Dependencies (this document depends on these proposals)

| Gap | What it provides | What this document needs it for |
|---|---|---|
| G1 | Declarative ConditionalLink predicate DSL | Safe LLM synthesis of layer-gating predicates (§3.1) |
| G2 | Dynamic `expected_set_source` on AllDataReceivedTrigger | Avoiding the "publish empty bundle" anti-pattern in gated layers (§3.3) |
| G7 | DirectLink `auto_transfer` default flip (or explicit-on-write) | Eliminating the silent-failure shape (§8 rule 7) |
| G10 | Gate-to-bottom semantics for ConditionalLink + AllDataReceived | Preventing trigger deadlock when all layers are gated off (§3.3) |
| G14 | `PromptTemplate` primitive | Loading agent prompts from YAML carriers, not from hand-rolled files |
| G16 | `ExecutionPlanConfig` + `ExecutionPlanDataUnit` | Typed Phase 0 output rather than hand-rolled BaseModel + DataUnitMemory |

Until these gaps ship, apecx-mcp implements the equivalent functionality
locally (the substitutions are documented per-section above). The
`development_roadmap.md` sequencing table tracks which gaps are scheduled
in which delivery phase.

---

## 10. Reference

| Resource | Location |
|---|---|
| Workflow output contract | `docs/workflow_output_contract.md` |
| Multi-agent architecture | `docs/multiagent_architecture.md` |
| External tool integration | `docs/external_tool_integration.md` |
| Nanobrain capability gaps (G1, G2, G7, G10, G14, G16) | `docs/nanobrain_capability_gaps.md` |
| Nanobrain alignment audit (which APECx concepts map to which nanobrain primitives) | `docs/nanobrain_alignment_audit.md` |
| Nanobrain step authoring | `.claude/skills/nanobrain-step-authoring/SKILL.md` |
| Nanobrain workflow authoring | `.claude/skills/nanobrain-workflow-authoring/SKILL.md` |
| Nanobrain agents and tools | `.claude/skills/nanobrain-agents-tools/SKILL.md` |
| Existing RAG synthesis step | `src/apecx_integration/composition/workflows/rag_e2e_synthesis/` |
| Existing assembly step | `src/apecx_integration/composition/steps/synthesis_assembly.py` |
