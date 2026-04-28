# VIOLIN × BV-BRC Workflow — Developer Documentation

This document describes the project's first-release workflow, `violin_bvbrc_synonym_gate`, for developers onboarding to apecx-mcp. It has three sections:

1. **Application structure** — what the workflow does, how the manifest organizes it, and what data it touches.
2. **Nanobrain framework usage** — what nanobrain primitives the workflow depends on, how steps are authored, and how execution is dispatched.
3. **Integration with the MCP tool registry** — how the workflow is reached from Claude Desktop, which tools fire at which stage, and how HITL and HPC export plug in.

The diagrams in `diagrams/` (especially `04`, `05`, `09`, `10`, `11`) are the visual companion to this text. The canonical source of truth for the workflow is the manifest at `apecx-mcp-integration/src/apecx_integration/composition/workflows/violin_bvbrc/manifest.yml`; this document explains it, but does not replace it.

---

## 1. Application Structure

### What the workflow does

The workflow takes a free-text scientific query — for example, *"find genes related to EEEV vaccine studies"* — and returns a ranked, provenance-annotated set of vaccine / pathogen / gene records that the query maps to. It is a synonym-resolution-and-enrichment pipeline that combines local snapshot data (BV-BRC and VIOLIN), a synonym cache that improves over time, an LLM step for novel terms, and a human-in-the-loop gate for the LLM's proposed synonyms.

The workflow is registered with the composer under the name `violin_bvbrc_synonym_gate`. Its first-release variant is `hard_only` — fuzzy-match was deliberately deferred per the 2026-04-21 directive recorded in the manifest, on the grounds that cache + LLM + HARD gate covers the use case without the threshold-tuning risk of fuzzy matching.

### The manifest as source of truth

Every workflow has a manifest describing its components. Each entry carries:

| Field | Purpose |
|---|---|
| `step_id` | Ordering identifier; referenced by DataUnit wiring. |
| `step_name` | Human-readable identifier. |
| `disposition` | `wrap` / `new` / `reuse` / `deferred` (see §2). |
| `class` | Fully-qualified Python class path; resolved at run time via `from_config`. |
| `yaml` | Path to the per-step configuration YAML. |
| `status` | `ready` / `in-progress` / `pending` / `done-ext`. |
| `wrap_notes` | Engineering notes about the wrapping discipline (free-text). |
| `rag_description` | One-sentence retrieval description used by the component RAG index. |
| `rag_examples` | Two example user prompts that should retrieve this step. |

The composer reads this manifest to know what exists, what is ready, and how to compose a workflow run. The differ uses `disposition` and `class` to categorize each step into one of `composed_standard / composed_parameterized / composed_wrapped / novel`. The component RAG index uses `rag_description` and `rag_examples` for similarity retrieval.

The manifest also declares a coverage target — `minimum_reuse_plus_wrap_share: 0.80` — that the workflow is required to meet. This enforces the reuse-not-reinvent constraint set out in the architectural plan §R3.3.

### Steps in execution order

In execution order, with their disposition and what they do:

**Step 1 · `entity_extraction`** *(LLM, wrap)*
Wraps an entity-extraction function from `apecx-db-integration` via `EntityExtractionStep` in `composition/steps/db_integration_wrappers.py`. One LLM call per `process()`. Input DataUnit `user_query_input` (str), output `entity_candidates_output` (list of `{name, type, confidence}`). Reads `APECX_LLM_BASE_URL` for the LLM endpoint via `apecx_db_integration._build_chat_llm`.

**Step 2 · `bvbrc_snapshot_match`** *(deterministic + LLM verification, wrap)*
Wraps `nanobrain.library.workflows.viral_protein_analysis.steps.enhanced_bv_brc_data_acquisition_step.EnhancedBVBRCDataAcquisitionStep`. The wrapper YAML injects a `BVBRCSnapshotTool` so the step reads `data/bvbrc_cache/*.tsv` and `*.fasta` instead of the live BV-BRC API. Two species/taxonomic-verification LLM agents are wired through the same `tools:` block; their LLM calls fire at `process()` time, so `from_config` runs without an API key.

**Step 3a · `synonym_cache_lookup`** *(deterministic, new)*
`apecx_integration.composition.steps.synonym_cache.SynonymCacheLookupStep`. Batched lookup against the Control Plane's `VerifiedSynonym` table. If every term in the input is already cached, the workflow short-circuits past Steps 3c, 4, and 4p and goes directly to Step 5. Otherwise, only the cache-miss terms (the *novel* ones) flow to Step 3c.

**Step 3b · `synonym_fuzzy_match`** *(deferred)*
Listed in the manifest with `disposition: deferred` per the HARD-synonym directive. Not composed at run time. Reserved in case the directive is reversed; deletion would lose the traceability.

**Step 3c · `synonym_llm_proposals`** *(LLM, wrap)*
Wraps `SynonymLLMProposalsStep` in `db_integration_wrappers.py`. Two LLM round-trips per `process()` (entity extraction + synonym matching against target vocabulary hints). Fires only on cache miss. Input `novel_terms_input` (list[str]), output `llm_proposals_output` (list of `{query_entity, synonym, score}`). The wrapper joins the novel-terms list with spaces because the wrapped function takes a single query string; per-term batching is filed as future work.

**Step 4 · `synonym_approval_gate`** *(HITL, new wrapper around framework class)*
Uses `nanobrain.library.steps.approval_step.ApprovalStep` with a workflow-specific YAML at `steps/synonym_approval_gate.yml`. POSTs to the Control Plane's `/approvals/` endpoint with the LLM proposals as the payload. The run pauses here. The Step's `process()` suspends until an approval decision arrives.

**Step 4p · `verified_synonym_writeback`** *(deterministic, new)*
`apecx_integration.composition.steps.synonym_cache.VerifiedSynonymWritebackStep`. Persists approved synonyms to the `VerifiedSynonym` table so future runs short-circuit at Step 3a. Tolerates HTTP 409 (concurrent-run race) by treating the conflict as already-cached.

**Step 5 · `violin_entity_lookup`** *(deterministic, wrap)*
Wraps `ViolinEntityLookupStep` in `db_integration_wrappers.py`. No LLM calls — pure pandas joins. Input `resolved_matches_input` (list of match dicts). Output `enriched_matches_output` (same dicts plus a `relevant_data` key with VIOLIN/BV-BRC row joins). Reads `APECX_DB_DATA_DIR` for the VIOLIN CSV path or accepts an explicit `data_dir` override.

**Step 6 · `genomic_annotation`** *(deterministic + LLM, wrap)*
Wraps `nanobrain.library.workflows.viral_protein_analysis.steps.bv_brc_data_acquisition_step.BVBRCDataAcquisitionStep`. Same `BVBRCSnapshotTool` injection as Step 2 but configured through a different field (`bvbrc_config_file` at the top level rather than `tools.bv_brc_tool`). Pairs with a `SynonymDetectionAgent` wired via `synonym_detection_agent`. Emits canonical protein annotations for matched genome IDs.

**Step 7 · `result_ranking`** *(reuse, no wrapper)*
`nanobrain.library.workflows.viral_protein_analysis.steps.result_collection_step.ResultCollectionStep`, used unchanged. Final ranking + JSON formatting. Output is keyed by VIOLIN and BV-BRC IDs and includes provenance fields: `run_id`, `model_version`, `approved_synonym_decisions`.

### The cache-hit short-circuit

The manifest's design hinges on Step 3a being able to bypass Steps 3c, 4, and 4p when every term in the input is already in `VerifiedSynonym`. Concretely, the workflow YAML wires the data flow so that:

- 3a's output names the cached terms and the residual novel terms.
- The composer-generated workflow has a conditional link: if `novel_terms` is empty, control flows directly to Step 5; otherwise control flows through 3c → 4 → 4p and then to Step 5.

Over time, as users approve synonyms, the cache grows and the short-circuit fires more often. This is the project's first cumulative-knowledge artifact: the workflow gets faster (and cheaper, in LLM tokens) the more it is used.

### Local data sources

The workflow has four data sources, all local:

- **`data/bvbrc_cache/`** — BV-BRC snapshots: `alphavirus_genomes.tsv`, `alphavirus_proteins.tsv`, `chikungunya_virus_genomes.tsv`, plus annotated FASTAs. Read by Steps 2 and 6 via `BVBRCSnapshotTool`. No live BV-BRC API access; per the architectural plan §R3.4, a workflow needing data outside the snapshot fails with a clear error rather than silently falling back to the live API.
- **`data/violin/`** — VIOLIN tables: `Gene_Information.csv`, `Gene_Vaccine_Pathogen_Information.csv`, `Pathogen_Information.csv`, `Vaccine_Information.csv`, `Vaccine_Pathogen_Information.csv`, `VIOLIN_Curated_References.txt`. Read by Step 5 via pandas joins.
- **`VerifiedSynonym` table** — apecx-cp database table. Read by Step 3a, written by Step 4p. Persists across runs.
- **LLM endpoint** — configured via `APECX_LLM_BASE_URL`. Used by Steps 1, 3c, and the inner verification agents in Steps 2 and 6.

### Output

The terminal artifact is a `ranked_entities` JSON file, content-hashed and written to the artifact store. The provenance fields embedded in the JSON allow a reader to reconstruct the run:

- `run_id` — apecx-cp Run UUID.
- `model_version` — pinned LLM model identifier from the run's GeneratedArtifact.
- `approved_synonym_decisions` — list of `{term, synonym, decided_by, decided_at}` from the HITL gate.

For local-default runs this is the final deliverable. For HPC export, the same artifact appears in the bundle alongside `provenance_seed.json` for round-trip ingest.

---

## 2. Nanobrain Framework Usage

### Architectural dependence: deep but narrow

Apecx-mcp-integration depends on nanobrain *architecturally* — every workflow runs through nanobrain's Step / Workflow lifecycle — but consumes only a small surface of nanobrain's library. The imports the integration actually uses across the VIOLIN × BV-BRC workflow are:

- `nanobrain.core.step.BaseStep`, `StepConfig` — Step subclassing for new steps (3a, 4p) and for the wrapper classes in `db_integration_wrappers.py` and `composition/steps/synonym_cache.py`.
- `nanobrain.core.workflow.Workflow` — the entry point: `Workflow.from_config(staged_yaml)` followed by `await workflow.process({})`.
- `nanobrain.library.steps.approval_step.ApprovalStep` — the framework-level HITL primitive used unchanged by Step 4.
- `nanobrain.library.workflows.viral_protein_analysis.steps.*` — `EnhancedBVBRCDataAcquisitionStep` (Step 2), `BVBRCDataAcquisitionStep` (Step 6), `ResultCollectionStep` (Step 7).
- `nanobrain.lightweight.component_index.ComponentIndex` — optional FAISS RAG index loader used by the composer (Phase 4).

The integration **does not** consume nanobrain's executor classes (`ExecutorBase`, `LocalExecutor`, `ThreadExecutor`, `ProcessExecutor`, `ParslExecutor` defined in `nanobrain/core/executor.py`). The apecx-cp control plane has its own workflow-level executor (`apecx_integration/control_plane/executors/local.py`) that calls `Workflow.from_config(...).process({})` directly. Nanobrain's executors operate at the step level inside the workflow; apecx's `LocalExecutor` operates at the workflow level above. The two layers are intentionally distinct.

### The four dispositions, explained

The manifest's `disposition` field tells you what kind of class is on the other end:

- **`wrap`** — There is an existing class (in nanobrain or apecx-db-integration) that does the underlying work. A thin wrapper class in apecx-mcp-integration adapts inputs and outputs to apecx's DataUnit conventions, configures the wrapped class via `from_config`, and otherwise leaves the wrapped logic untouched. Steps 1, 2, 3c, 5, 6 in this workflow are wrap-disposition.
- **`reuse`** — An existing class is used directly with no wrapper. Step 7 is the only example here.
- **`new`** — Net-new class authored in apecx-mcp-integration. Used for the synonym cache pattern (3a, 4p). Step 4 is also marked `new` in the disposition column because its workflow-specific YAML is new, even though the Python class itself (`ApprovalStep`) is from nanobrain — the wrap-vs-new distinction is at the configuration level, not just the code level.
- **`deferred`** — Listed in the manifest for traceability but not composed at run time. Step 3b is the example.

### Step authoring discipline

For `new` and `wrap` classes that subclass `BaseStep`, the discipline is:

```python
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

class SynonymCacheLookupStepConfig(StepConfig):
    """Pydantic config; loaded from YAML by from_config."""
    cp_base_url: str = Field(...)
    batch_size: int = Field(default=20)

class SynonymCacheLookupStep(BaseStep):
    """Batch lookup in the Control Plane's VerifiedSynonym cache."""

    @classmethod
    def from_config(cls, config_path: str) -> "SynonymCacheLookupStep":
        # framework-provided pattern; never call __init__ directly
        ...

    async def process(self, inputs: dict) -> dict:
        terms = inputs["entity_candidates_output"]
        ...
        return {
            "cached_synonyms_output": ...,
            "novel_terms_output": ...,
        }
```

Three rules apply throughout:

1. **`from_config` only.** Direct constructors are forbidden by the framework — `FromConfigBase.__init__` raises `RuntimeError`. The component is instantiated by the framework's loader via `from_config`.
2. **Implement `process`, never override `execute`.** The framework validates this at step initialization and raises `ComponentConfigurationError` with a `FAIL-FAST:` message if `execute` is overridden.
3. **DataUnit names in YAML must match keys read and written in `process`.** The framework wires inputs and outputs by DataUnit name; mismatches surface as runtime KeyErrors.

These rules are documented in `nanobrain/CLAUDE.md` and in the eight `nanobrain-*` skills under `.claude/skills/`. They apply to every wrapper and every new step in this workflow.

### Configuration loading and the YAML hierarchy

Each step has a YAML in `composition/workflows/violin_bvbrc/steps/`. The composed workflow YAML — produced by the composer at run time — references step YAMLs by path:

```yaml
# composer-generated workflow YAML (sketch)
name: violin_bvbrc_synonym_gate__run_<uuid>
steps:
  - step_id: "1"
    config: composition/workflows/violin_bvbrc/steps/entity_extraction.yml
  - step_id: "2"
    config: composition/workflows/violin_bvbrc/steps/bvbrc_snapshot_match.yml
  ...
links:
  - from: step_1.entity_candidates_output
    to: step_2.entity_input
  ...
```

When `Workflow.from_config(staged_yaml)` runs, nanobrain recursively loads each step config, resolves the registered class via the `class:` field in each step YAML, and instantiates via `from_config`. Each step's YAML also contains its `tools:` block (for steps that wire sub-tools, like `BVBRCSnapshotTool`) and any inner agent YAMLs (for steps that use specialized agents internally).

### Step ↔ executor decoupling (R3.3 refactor)

The architectural plan §R3.3 names a refactor task that affects this workflow: **executor configuration must be separable from step configuration**. Historically, some nanobrain viral-protein-analysis steps (notably the PSSM steps) pulled Parsl-on-Aurora executor configuration into their step YAMLs via `executor_config_path`. For the local-default execution model (R3.2), the workflow YAML must reference steps independently of which executor runs them.

In this workflow, Step 6 (`genomic_annotation`) is the most affected — `BVBRCDataAcquisitionStep` in nanobrain's library historically pulled in PSSM-related executor config. The wrapper YAML at `steps/genomic_annotation.yml` references the BVBRCSnapshotTool via `bvbrc_config_file` and the `SynonymDetectionAgent` via `synonym_detection_agent` without pulling Parsl config. The step runs on the apecx-cp `LocalExecutor` for the local-default path.

When HPC export is exercised (the PBS bundle path), the bundle's `submit.pbs` invokes the workflow on Polaris/Aurora. The executor used by the steps inside the workflow at that point is determined by the bundle's runtime configuration, not by the step YAMLs in this repo.

### What runs where, end to end

Concretely, when a scientist asks the composer to plan and run this workflow:

1. The composer (Tier 3) instantiates a `ComposedWorkflow` referencing the manifest.
2. The differ categorizes each step. All steps in this workflow are `composed_standard` or `composed_parameterized` — none are `novel`. The workflow is a pure composition over registered components.
3. The approval policy (in `_configs/approval_policy.yml`) decides whether any pre-execute review is needed. For `composed_standard`, it auto-approves; for `composed_parameterized`, it requests review.
4. The control plane's `LocalExecutor` stages the run-root directory (symlinks the step YAMLs and the workflow YAML into a per-run staging directory), calls `Workflow.from_config(staged_yaml)`, then `await workflow.process({})`.
5. nanobrain's Step lifecycle drives each step's `process()` in topological order. Step 4's `ApprovalStep` suspends the workflow at the HITL gate.
6. The apecx-cp recorder emits provenance events at every Step transition; the recorder's hash chain spans the run (see diagram 09).
7. On completion, Step 7 emits the final artifact; apecx-cp records it in the artifact store and emits `RUN_COMPLETED`.

---

## 3. Integration with the MCP Tool Registry

The workflow is reachable from Claude Desktop entirely through MCP tools. There is no out-of-band command-line interface required for the typical scientist use case.

### Discovery: see what's available

Two read-only tools let the LLM (or the scientist) inspect what the system can do before invoking anything:

- **`list_workflows`** — returns the composer's workflow catalog. The VIOLIN × BV-BRC workflow appears as `violin_bvbrc_synonym_gate`, with its first-release variant flag.
- **`describe_workflow`** — given a workflow ID, returns the step list, expected inputs, and the `rag_description` strings from the manifest. This lets Claude explain the workflow to the scientist before composition.

These tools query the manifest catalog directly; they do not start a run.

### Composition and review: the pre-execute loop

Three tools handle the compose-then-review loop:

- **`start_workflow`** — given a free-text user description, the composer drafts a YAML referencing this workflow's steps. The composer ranks workflows by RAG similarity over the user's description; for typical VIOLIN × BV-BRC use cases (entity-mapping queries against vaccines / pathogens / genes), the `violin_bvbrc_synonym_gate` workflow is selected.
- **`show_diff`** — returns the per-step categorization for the proposed run: which are `composed_standard`, which are `composed_parameterized`, etc. For VIOLIN × BV-BRC, the diff is typically all-`composed_standard` (every step uses its registered config unchanged) unless the user asks for parameter overrides.
- **`execute_workflow`** — kicks off the local execution.

The differential review machinery (see diagram 04) is what lets Claude explain to the scientist *before* execution exactly what will run, with what risk classification. For this workflow, the diff is short and reassuring: zero `novel` steps means no LLM-generated novel-Python code in the run.

### HITL approvals: durable across sessions

Step 4 is an `ApprovalStep`; when the workflow reaches it, the run pauses and an `Approval` row is written to the apecx-cp database. Four MCP tools handle the user decision:

- **`list_pending_approvals`** — Claude (or the scientist) polls for pending approvals. The result includes the structured approval payload — for this workflow, the LLM-proposed synonyms requiring decision.
- **`approve`** — record approval; the workflow resumes.
- **`reject`** — record rejection with a reason; the workflow terminates with the rejection captured in provenance.
- **`correct`** — record approval-with-modifications; the modified synonyms flow downstream as if the LLM had proposed them, and the modifications are recorded in provenance.

Critically, the approval record persists in the database. The MCP server can be restarted, the user can reconnect from a different session, hours or days can pass — when the user approves, the suspended workflow resumes from where it paused. This is the durable-HITL primitive (see diagram 05).

After an approval, Step 4p writes the approved synonyms to the `VerifiedSynonym` table. Future runs find these terms in the cache at Step 3a and short-circuit past Steps 3c, 4, and 4p. The workflow gets cheaper and faster over time.

### HPC export (opt-in)

When the user explicitly asks to export the workflow to HPC instead of running locally, four tools handle the round-trip:

- **`estimate_cost`** — projects core-hours and wall time using the per-step cost estimators in `apecx_integration/control_plane/accounting/cost_estimator.py`. The estimate is shown to the user.
- **`confirm_allocation`** — hard gate. The user must explicitly confirm before the bundle is produced; this is the allocation approval primitive in `Approval.kind = allocation`.
- **`export_hpc_bundle`** — produces a portable submission directory (`submit.pbs`, `run.sh`, `workflow.yml`, `staging_plan.yml`, `provenance_seed.json`, `environment/{container.sif | conda.lock}`, `README.md`). The operator runs `qsub submit.pbs` on Polaris or Aurora.
- **`ingest_hpc_bundle`** — round-trips the results back into the apecx-cp artifact store after the HPC job completes.

For the VIOLIN × BV-BRC workflow specifically, HPC export is rarely needed at typical query sizes — local execution is adequate. The HPC story exists for larger-scale variants of the workflow (bulk genomic annotation runs that exceed laptop compute) and as a reproducibility artifact (the bundle is a candidate publishable supplementary).

### Data lookup tools (bypass paths)

Seven additional MCP tools expose direct database queries that bypass the composer entirely:

- `query_vaccines`, `query_pathogens`, `query_genes`, `query_bvbrc_genomes`
- `get_vaccine_pathogen_genes`
- `resolve_entity`
- `database_statistics`

These are useful for one-shot factual questions during conversation — *"how many vaccines are in the snapshot?"*, *"what genes are associated with this pathogen?"* — that do not require running the full workflow. They read the same local data sources as the workflow steps but invoke them directly rather than through the workflow runtime. They are intentionally separate from the orchestration path: they answer questions, not run pipelines.

These bypass tools are particularly useful for *grounding* the LLM's understanding before composition. Claude can use them to confirm the user's vocabulary matches the database before triggering `start_workflow`, avoiding wasted runs over typos or out-of-snapshot terms.

### Provenance touchpoints

Every MCP tool call that affects state emits provenance events. For a VIOLIN × BV-BRC run, the recorder's hash chain typically contains (see diagram 09):

- `RUN_STARTED` from the system.
- `WORKFLOW_GENERATED` from `start_workflow` (the composer's draft is hashed with the pinned LLM model version).
- `APPROVAL_REQUESTED` and `APPROVAL_DECIDED` for each gate (pre-run YAML review and the mid-run synonym gate).
- `STEP_STARTED` and `STEP_COMPLETED` per step in the workflow.
- `ARTIFACT_CREATED` for intermediate artifacts and the final `ranked_entities` JSON.
- `RUN_COMPLETED` from the system.

A typical complete run produces approximately 20–30 events depending on how many synonyms required HITL decisions. Re-walking the chain reproduces every `event_hash`; mismatch raises `ChainBroken`. See diagram 06 for the chain mechanics.

---

## References

### Visual diagrams (in `diagrams/`)

- `01_system_architecture.svg` — where the workflow runtime sits in the four-tier architecture.
- `02_workflow_lifecycle.svg` — the end-to-end stages with their MCP tool calls.
- `04_composition_and_differ.svg` — how the composer categorizes the workflow's steps.
- `05_hitl_approval_flow.svg` — the durable approval primitive used by Step 4.
- `09_provenance_in_workflow.svg` — provenance events emitted during a real run.
- `10_state_and_artifacts.svg` — what the control plane records.
- `11_violin_bvbrc_workflow.svg` — this workflow specifically, including the cache short-circuit.

### Code (canonical entry points)

- `apecx-mcp-integration/src/apecx_integration/composition/workflows/violin_bvbrc/manifest.yml` — workflow manifest.
- `apecx-mcp-integration/src/apecx_integration/composition/workflows/violin_bvbrc/steps/*.yml` — per-step YAMLs.
- `apecx-mcp-integration/src/apecx_integration/composition/steps/db_integration_wrappers.py` — wrapper classes for Steps 1, 3c, 5.
- `apecx-mcp-integration/src/apecx_integration/composition/steps/synonym_cache.py` — new cache classes for Steps 3a, 4p.
- `apecx-mcp-integration/src/apecx_integration/control_plane/executors/local.py` — workflow-level local executor.
- `apecx-mcp-integration/src/apecx_integration/mcp_surface/server.py` — FastMCP tool registrations.
- `nanobrain/nanobrain/library/workflows/viral_protein_analysis/steps/` — the wrapped underlying classes for Steps 2, 6, 7.
- `nanobrain/nanobrain/library/steps/approval_step.py` — the framework-level ApprovalStep used by Step 4.
- `nanobrain/nanobrain/core/step.py` and `nanobrain/nanobrain/core/workflow.py` — the framework-level Step and Workflow primitives.

### Project documents

- `architectural_plan.md` (workspace root) — the full architectural plan, including §R3 Round-3 revisions that reshape this workflow.
- `nanobrain/CLAUDE.md` — framework-level rules for nanobrain code.
- `apecx-mcp-integration/CLAUDE.md` — repo-local discipline notes.
