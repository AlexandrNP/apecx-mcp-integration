# APECx Analytical Workflow Output Contract

**Status:** Design / pre-implementation
**Audience:** Workflow authors, nanobrain step implementors, MCP surface layer
**Supersedes:** Nothing — new document. Complements `multiagent_architecture.md`.

---

## 1. Purpose

This document defines the **standardized output every APECx analytical workflow must produce**.
It is the interface contract between the multi-agent backend and the user-facing MCP surface.
A workflow that does not produce this output shape is not complete, regardless of whether
its internal reasoning is correct.

The contract is domain-agnostic. The layer types, evidence schema, and grounding rules
apply equally to any domain (genomics, structural biology, materials science, literature
mining) by parameterizing the data sources and tool registry, not by modifying the contract
shape.

---

## 2. The Canonical Workflow Template

All APECx analytical workflows follow a seven-phase execution template. The template
is **parameterized** — the number of domain-reasoning layers and the set of active data
sources are determined at runtime during Phase 0, not hardcoded in the workflow definition.

```
workflow(query, session_context?) →
  phase_0:  planning(query, session_context)           → ExecutionPlan
  phase_1…N: for layer in ExecutionPlan.layers:
               DomainReasoningLayer(layer, prior_findings) → LayerResult
               accumulate(LayerResult)
  cross_src: cross_source_integration(all_LayerResults) → IntegrationResult
  response:  synthesis(IntegrationResult)              → FinalResponse
  evidence:  package_evidence(all_LayerResults)        → EvidencePackage
  followup:  generate_followups(EvidencePackage, gaps)  → FollowupQuestions
```

---

## 3. Phase 0 — Planning and Data Readiness Assessment

### 3.1 Input

| Field | Type | Description |
|---|---|---|
| `query` | str | Raw natural-language user question |
| `session_context` | SessionContext? | Prior session evidence (for chained questions) |

### 3.2 Output — ExecutionPlan

The schema below is the on-the-wire shape. The runtime carrier is
`ExecutionPlanConfig(ConfigBase)` wrapped in `ExecutionPlanDataUnit(DataUnitMemory)`
— gap **G16** in `nanobrain_capability_gaps.md`. Until G16 ships, apecx-mcp
loads this JSON through a hand-rolled `pydantic.BaseModel` with `extra='forbid'`
and passes it via a plain `DataUnitMemory`. The `agent_workflow_authoring.md`
spec documents the lowering pipeline that consumes the plan downstream.

The Phase 0 prompt that produces this plan is `PROMPT-P0` in `llm_prompt_contracts.md`,
delivered through a `PromptTemplate` carrier (gap **G14**). Until G14 ships,
the prompt body is loaded from a hand-rolled file (today's
`composer_prompts/system.md` pattern).

```json
{
  "intent": "string",
  "capability_gaps": ["string"],
  "layers": [
    {
      "layer_id": "string",
      "layer_type": "sequence | structural | functional | evidence | cross_source | design",
      "data_sources": ["source_name"],
      "expected_contribution": "string",
      "depends_on": ["layer_id"]
    }
  ],
  "tool_executions_required": ["tool_name"],
  "session_evidence_reused": ["layer_id"]
}
```

### 3.3 Rules

1. **Capability gaps must be declared explicitly.** If the system cannot satisfy a stated
   constraint (missing data source, unsupported computation), this is recorded in
   `capability_gaps` and surfaced verbatim in the final response. Gaps are never silently
   omitted.

2. **Session evidence is reused, not re-retrieved.** If `session_context` contains evidence
   from a prior question in the same session, Phase 0 must include those layer IDs in
   `session_evidence_reused` and exclude them from the active layer list. The workflow does
   not re-query sources already fetched in the same session.

3. **Layer order is a DAG, not a list.** `depends_on` expresses dependencies between layers.
   Independent layers may execute in parallel; dependent layers must wait for their
   predecessors to complete and contribute findings to the current layer's input.

---

## 4. Phases 1–N — Domain Reasoning Layers

### 4.1 Layer types

| Type | Typical data source category | Example computations |
|---|---|---|
| `sequence` | Sequence repositories | Alignment, conservation scoring, phylogeny |
| `structural` | 3D structure repositories | Surface accessibility, domain mapping, spatial coordinates |
| `functional` | Experimental assay databases | Measurement extraction, assay validation, kinetics |
| `evidence` | Literature, domain knowledge base | Semantic search, meta-analysis synthesis |
| `cross_source` | Multiple | Cross-source mapping, concordance scoring, entity resolution |
| `design` | Multiple + tools | Variant optimization, property prediction, manufacturability |

### 4.2 LayerResult schema

Each layer produces one `LayerResult`:

```json
{
  "layer_id": "string",
  "layer_type": "string",
  "execution_summary": {
    "sources_queried": ["string"],
    "records_retrieved": "int",
    "tools_invoked": ["string"],
    "scope": "string"
  },
  "findings": [
    {
      "finding_id": "string",
      "claim": "string",
      "quantitative_support": "string",
      "confidence": "float [0,1]",
      "source_refs": ["string"]
    }
  ],
  "validation": {
    "method": "string",
    "concordance": "float?",
    "notes": "string?"
  },
  "errors": [
    {
      "source": "string",
      "message": "string",
      "degradation": "empty_bundle | partial_results | tool_skipped"
    }
  ]
}
```

### 4.3 Grounding requirement

**Every claim in `findings` must be quantitative.** Unquantified claims are not acceptable
findings. The minimum acceptable form is: `<metric>: <value> (<sample size or basis>)`.

Example of non-compliant finding:
> "Property X showed strong conservation across samples."

Example of compliant finding:
> "Property X: 98% conservation (46 of 47 samples identical) across the full dataset."

This requirement exists because the downstream synthesis step uses findings as the sole
source of grounded citations. Unquantified findings produce ungrounded synthesis output.

### 4.4 Failure contract

Each layer must handle source failures without halting the workflow:

- Single source failure within a layer → `errors[]` entry with `degradation: partial_results`;
  findings from other sources in the same layer are still contributed.
- All sources in a layer fail → `errors[]` entry with `degradation: empty_bundle`; the layer
  contributes no findings but the workflow continues. The capability gap is surfaced in the
  response.
- Tool invocation failure → `errors[]` entry with `degradation: tool_skipped`; finding is
  marked with `confidence: 0.0` and `source_refs: ["tool_unavailable"]`.

---

## 5. Cross-Source Integration Phase

This phase runs after all domain layers complete. It merges findings across layers and
computes cross-source concordance.

### 5.1 IntegrationResult schema

```json
{
  "entity_mappings": [
    {
      "canonical_id": "string",
      "surface_forms": ["string"],
      "source_refs": ["string"]
    }
  ],
  "cross_source_concordance": {
    "method": "string",
    "overall_score": "float",
    "per_finding": {"finding_id": "float"}
  },
  "merged_findings": ["FindingRef"],
  "conflicting_findings": [
    {
      "finding_ids": ["string"],
      "conflict_description": "string",
      "resolution": "string | unresolved"
    }
  ]
}
```

### 5.2 Entity resolution requirement

All entities referenced across layers must be resolved to canonical IDs before this phase
completes. Entity resolution uses the `CanonicalEntityResolver` service (see
`multiagent_architecture.md §6.3`). Unresolved entities are flagged as conflicts.

---

## 6. Final Response — Output Schema

```json
{
  "answer": {
    "text": "string",
    "key_results": [
      {
        "result": "string",
        "quantitative_basis": "string",
        "finding_ids": ["string"]
      }
    ]
  },
  "cross_source_reasoning": "string?",
  "integrated_insight": "string?",
  "design_variants": {
    "variant_name": "string"
  },
  "capability_gaps": ["string"],
  "confidence_summary": {
    "overall": "high | medium | low | insufficient",
    "limiting_factors": ["string"]
  }
}
```

### 6.1 Answer grounding gate

The synthesis step must enforce:

1. **Citation coverage:** Every `key_result` must reference at least one `finding_id` from a
   completed layer. Results without finding references are rejected.
2. **Quantitative minimum:** Every `key_result.quantitative_basis` must be non-empty.
3. **Gap transparency:** All entries from `ExecutionPlan.capability_gaps` must appear in
   `FinalResponse.capability_gaps`. Gaps are never dropped silently.
4. **Confidence gate:** If no layer produced findings (all empty bundles), `confidence_summary.overall`
   must be `"insufficient"` and the answer must state this explicitly.

### 6.2 Optional sections

`cross_source_reasoning` and `integrated_insight` are optional but encouraged when the query
requires synthesis across multiple layer types. They are omitted for single-source lookups.
`design_variants` is only present for design-type queries where multiple deployment or
application configurations are relevant.

---

## 7. Evidence Package

The evidence package is a structured appendix returned alongside the final response.
It is categorized by evidence type — not by data source — to support downstream citation
rendering and HPC bundle packaging.

```json
{
  "source_records": [
    {
      "source_name": "string",
      "entry_id": "string",
      "description": "string",
      "record_type": "string",
      "used_for": "string"
    }
  ],
  "computational_results": [
    {
      "computation_type": "string",
      "dataset_description": "string",
      "n_records": "int",
      "result_summary": "string"
    }
  ],
  "experimental_records": [
    {
      "source_name": "string",
      "entry_count": "int",
      "measurement_types": ["string"],
      "key_metrics": {"metric_name": "value_string"}
    }
  ],
  "literature_records": [
    {
      "external_id": "string?",
      "citation": "string",
      "relevance": "string"
    }
  ],
  "tool_outputs": [
    {
      "tool_name": "string",
      "inputs": {},
      "result_summary": "string",
      "provenance_key": "string"
    }
  ],
  "quantitative_metrics": {
    "metric_name": "string"
  }
}
```

---

## 8. Follow-up Questions

The workflow must generate exactly three follow-up questions per response.

### 8.1 Follow-up schema

```json
[
  {
    "title": "string",
    "question": "string",
    "research_type": "optimization | comparative_analysis | cross_domain | design | ...",
    "data_availability": "fully_supported | partially_supported | requires_new_data",
    "estimated_new_sources": ["string"],
    "evidence_reuse": ["layer_id"]
  }
]
```

### 8.2 Generation rules

1. **Data-grounded:** Each follow-up must be answerable with data sources already available
   or clearly specified as additions. Follow-ups that cannot be grounded have
   `data_availability: requires_new_data` with `estimated_new_sources` populated.

2. **Progressive depth:** Follow-ups must increase in complexity. Q_n+1 must either:
   - Require a new layer type not activated in Q_n, or
   - Require a tool execution step not invoked in Q_n, or
   - Require generalization to a new domain, organism, or dataset not covered by Q_n.

3. **Chaining contract:** The question text in `followup_questions[k].question` must be
   suitable for use as the `query` input to a new workflow run. A downstream workflow run
   receiving this question is expected to reuse `evidence_reuse` layer IDs from the current
   session's context rather than re-querying.

---

## 9. Conversational Chaining Contract

APECx sessions are multi-turn. A user asks a question, receives a response, selects a
follow-up, and repeats. The session state that must be preserved between turns:

```json
{
  "session_id": "string",
  "turn": "int",
  "accumulated_evidence": {
    "layer_id": "LayerResult"
  },
  "entity_registry": {
    "canonical_id": "ResolvedEntity"
  },
  "execution_history": [
    {
      "turn": "int",
      "query": "string",
      "execution_plan": "ExecutionPlan",
      "layers_completed": ["layer_id"]
    }
  ]
}
```

**Rules:**

1. `accumulated_evidence` persists across turns within a session. A new turn's Phase 0
   must consult it before deciding which layers to execute.

2. `entity_registry` persists across turns. Canonical entity IDs resolved in turn 1
   are reused in turn 2 without re-querying.

3. Session context is owned by the session store (control plane), not by individual
   workflow runs. A workflow run receives session context as an input parameter and
   returns updated context as an output artifact.

4. Sessions expire after a configurable TTL (default: 24 hours). Expired sessions
   produce no context; the new turn starts fresh.

---

## 10. HPC-Readiness Requirements

For design-type queries that produce outputs requiring expensive simulation or validation,
the workflow must additionally produce an HPC bundle artifact:

```
hpc_bundle/
├── submit.pbs             ← qsub-ready PBS script
├── run.sh                 ← entrypoint
├── workflow.yml           ← the nanobrain workflow YAML that produced this output
├── inputs/
│   ├── evidence_package.json
│   ├── tool_outputs/      ← ProxyStore-resolved artifacts
│   └── hypothesis.json    ← ranked hypothesis from tournament step
├── provenance_seed.json   ← for re-ingest after HPC run
└── README.md
```

The HPC bundle is self-contained. It must include all resolved data (no live service
references) and must be reproducible: re-running `run.sh` with the same inputs must
produce the same outputs.

The HITL gate before HPC bundle export is defined in `multiagent_architecture.md §7.3`.

---

## 11. Reference

| Resource | Location |
|---|---|
| Multi-agent architecture (target state) | `docs/multiagent_architecture.md` |
| Nanobrain workflow design (how to implement this contract) | `docs/nanobrain_workflow_design.md` |
| External tool integration (Rhea, Galaxy) | `docs/external_tool_integration.md` |
| Development roadmap | `docs/development_roadmap.md` |
| Agent workflow authoring (how the orchestrator builds an ExecutionPlan and lowers it to YAML) | `docs/agent_workflow_authoring.md` |
| LLM prompt contracts (PROMPT-P0 produces this plan) | `docs/llm_prompt_contracts.md` |
| Nanobrain capability gaps (G14 PromptTemplate, G16 ExecutionPlanConfig + DataUnit, G17 PlanLoweringStep) | `docs/nanobrain_capability_gaps.md` |
| Nanobrain alignment audit (F-1 ExecutionPlan-as-DataUnit finding) | `docs/nanobrain_alignment_audit.md` |
