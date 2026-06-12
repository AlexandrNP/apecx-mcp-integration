# Viral Epitope Evidence Workflow - Detailed Action Plan

**Status:** Plan (2026-06-12). Worktree `wt-eo-mvp`.
**Scope:** add a new evidence workflow and extend harmonized structural reference ingestion.
This document is the implementation plan only; no production code has been changed by this plan.

## 0. Clarifying assumptions

1. The current implementation has **nine** harmonized logical indices, and `protabank` is already
   one of them:
   `violin_pathogen`, `violin_vaccine`, `violin_gene`, `bvbrc_genome`, `bvbrc_protein`,
   `bvbrc_protein_structure`, `bvbrc_epitope`, `antiviraldb`, `protabank`.
2. The requested PDB and EMDB references are available through the aggregate Globus Search index
   `e74bf12a-d0dd-4d19-a965-03f4936db851`.
3. The requested "three sources" are the structural/protein-engineering source family:
   `pdb`, `emdb`, and `protabank`.
4. The current ProtaBank destination index,
   `9e902471-9c77-49d3-a12c-516cc0808c3b`, should be used as the harmonized write destination for
   all three logical sources.
5. Because `protabank` already exists in the current nine, the implementation target is **eleven**
   logical sources after adding `pdb` and `emdb`. If the intended target is "nine existing plus
   three new" as twelve logical sources, resolve that before code changes.

The most important design constraint is that the workflow must follow the existing external
orchestration/return-of-control paradigm: the same workflow capability must be callable by a
desktop/user-provided LLM interface and by an independent backend. The MCP surface should expose
the workflow, its required inputs, and any return-of-control needs; it should not become a separate
logic path.

## 1. Target capability

Add a catalog workflow tentatively named `viral_epitope_evidence_review`.

The workflow should produce an evidence bundle for a viral epitope, antigen, protein, or design
question by reusing existing retrieval and analysis components:

- harmonized VIOLIN, BV-BRC, AntiviralDB, ProtaBank, PDB, and EMDB search;
- PubMed and RAG synthesis context assembly;
- optional conserved-site analysis through the shipped `viral_conserved_sites` workflow;
- structural-reference lookup through the shared ProtaBank destination index;
- explicit approval before any design or optimization output is generated.

The user-facing behavior should be:

1. If required inputs are missing or ambiguous, return `WorkflowResult(status="needs_input")` with a
   typed `control_transfer`.
2. If the user asks only for evidence, return evidence, citations, limitations, and next actions.
3. If the user asks for design or optimization output, pause until explicit approval is present.
4. After approval, produce design/optimization suggestions only as evidence-bound hypotheses, with
   provenance and limitations attached.

The backend behavior should be the same contract, not a bypass. Backend callers can satisfy the
same input schema directly and can provide the same approval token or approval decision through the
control-plane approval tools.

## 2. Deliverable shape

The terminal workflow output should be an envelope-compatible dict that can be rendered by the
existing MCP surface:

```yaml
workflow: viral_epitope_evidence_review
status: ok | partial | error | needs_input
query:
  original: string
  normalized_entities: [...]
evidence:
  pathogens: [...]
  proteins_or_antigens: [...]
  epitopes: [...]
  structures:
    pdb: [...]
    emdb: [...]
    protabank: [...]
  conservation:
    enabled: boolean
    summary: string | null
  publications: [...]
  rag_context: [...]
assessment:
  claims: [...]
  support_matrix: [...]
  limitations: [...]
design_or_optimization:
  approval_required: boolean
  approval_id: string | null
  approved: boolean
  outputs: [...]
provenance:
  indices_queried: [...]
  globus_index_uuid: e74bf12a-d0dd-4d19-a965-03f4936db851
  harmonized_destination_indices: [...]
```

Acceptance criteria:

- every claim in `assessment.claims` points to at least one evidence record or is explicitly marked
  as unsupported;
- PDB, EMDB, and ProtaBank records can be distinguished even though they share one destination
  index;
- design/optimization outputs are empty unless approval is explicit;
- `needs_input` is driven by workflow input schema and control-transfer builders, matching the
  shipped return-of-control path.

## 3. Track A - Source and destination registry update

Repository likely touched: `../apecx-harvesters-work` or the active harvester repo selected for the
implementation branch. This plan does not edit that sibling repo yet.

### A1. Inventory the current registry

Verify and record:

- source and destination registries used by `republish_with_canonical.py`;
- the current nine-source assumptions in `tests/test_harmonize.py`;
- fields available on PDB and EMDB records inside Globus index
  `e74bf12a-d0dd-4d19-a965-03f4936db851`;
- whether PDB, EMDB, and ProtaBank records expose a stable source discriminator, such as source
  name, publisher, collection, type, or a normalized record namespace.

Verification:

- run a small real Globus query against the aggregate index for PDB and EMDB terms;
- save representative record shapes in the implementation notes or test fixtures derived from real
  records, not synthetic examples.

### A2. Split logical source identity from read index UUID

Current harvester code appears to assume a source UUID can uniquely identify one source parser.
That assumption is no longer valid if PDB and EMDB are both read from the aggregate index
`e74bf12a-d0dd-4d19-a965-03f4936db851`.

Implement a source descriptor model rather than a UUID-only map:

```yaml
pdb:
  source_uuid: e74bf12a-d0dd-4d19-a965-03f4936db851
  source_filter: <PDB discriminator>
  parser: pdb
  dest_uuid: 9e902471-9c77-49d3-a12c-516cc0808c3b
emdb:
  source_uuid: e74bf12a-d0dd-4d19-a965-03f4936db851
  source_filter: <EMDB discriminator>
  parser: emdb
  dest_uuid: 9e902471-9c77-49d3-a12c-516cc0808c3b
protabank:
  source_uuid: <existing ProtaBank source UUID>
  source_filter: <ProtaBank discriminator if needed>
  parser: protabank
  dest_uuid: 9e902471-9c77-49d3-a12c-516cc0808c3b
```

Acceptance criteria:

- PDB and EMDB can share the aggregate read index without sharing parser behavior;
- PDB, EMDB, and ProtaBank can share the destination UUID without becoming indistinguishable;
- tests no longer assert every destination UUID is distinct;
- tests do assert every logical source has a destination and a source discriminator when the
  destination is shared.

### A3. Harmonize structural subjects

For every republished PDB, EMDB, and ProtaBank record, populate canonical subject fields in the same
shape used by the other harmonized indices:

- `subjects.valueUri` for ontology/canonical IRIs where resolvable;
- `subjects.value` for labels;
- `subjects.classificationCode` or an equivalent source namespace for source-specific IDs where
  appropriate;
- a source marker sufficient to filter `pdb`, `emdb`, and `protabank` records after they land in
  the shared ProtaBank destination index.

Acceptance criteria:

- a canonical pathogen/protein query can retrieve structural records through `subjects.valueUri`
  when the canonical URI is known;
- a label-only query degrades to source-specific text fields without pretending to be harmonized;
- the published record preserves original IDs such as PDB ID and EMDB accession.

### A4. Update harvester tests

Replace current count and uniqueness assumptions with explicit shared-destination assertions:

- logical source count becomes 11 under the current codebase interpretation;
- `pdb`, `emdb`, and `protabank` all map to destination
  `9e902471-9c77-49d3-a12c-516cc0808c3b`;
- all other existing destinations remain unchanged;
- PDB and EMDB have source filters and parsers;
- republish is idempotent on a small real sample.

Required verification:

- unit tests for registry shape;
- integration test against a small real PDB and EMDB sample from the aggregate Globus index;
- recorded command and output in the implementation commit body.

## 4. Track B - Harmonized search surface update

Repository touched: `apecx-mcp-integration` worktree.

### B1. Extend logical index validation

Update the MCP harmonized search surface to accept:

- `pdb`
- `emdb`
- existing `protabank`

Concrete code targets:

- `src/apecx_integration/mcp_surface/tools/harmonized_search.py`
- `src/apecx_integration/composition/steps/harmonized_search_execute_step.py`
- any mirrored demo/skill query maps in the harvester search demo, if that demo remains a supported
  user path.

Acceptance criteria:

- `harmonized_search(index="pdb", ...)` validates;
- `harmonized_search(index="emdb", ...)` validates;
- `harmonized_search(index="protabank", ...)` continues to validate;
- unknown index behavior remains fail-loud.

### B2. Add shared-destination source filters

Mapping `pdb`, `emdb`, and `protabank` to the same destination UUID is not enough. The query layer
must also pass a logical source discriminator so a PDB query does not return EMDB or ProtaBank
records solely because the destination index matches.

Planned shape:

```python
_INDEX_UUIDS = {
    "pdb": "9e902471-9c77-49d3-a12c-516cc0808c3b",
    "emdb": "9e902471-9c77-49d3-a12c-516cc0808c3b",
    "protabank": "9e902471-9c77-49d3-a12c-516cc0808c3b",
}

_SOURCE_FILTERS = {
    "pdb": {...},
    "emdb": {...},
    "protabank": {...},
}
```

Acceptance criteria:

- same destination UUID can serve multiple logical indices;
- result sets are filtered by logical source, not destination UUID alone;
- tests prove that the filter is included in the Globus Search query payload or applied in a
  deterministic post-filter when Globus field filtering is insufficient.

### B3. Prefer canonical subject matching

Once Track A populates `subjects.valueUri`, structural lookups should prefer the uniform harmonized
field:

```yaml
pdb:
  field: subjects.valueUri
  shape: iri
emdb:
  field: subjects.valueUri
  shape: iri
protabank:
  field: subjects.valueUri
  shape: iri
```

Temporary fallback is allowed during migration only if it is explicit and visible in the result:

- fallback field used;
- source label matched;
- confidence or limitation note included.

Acceptance criteria:

- canonical IRI query hits structural records when present;
- fallback label query works for unharmonized records and reports that it was a fallback;
- no query silently claims canonical matching when it used label text.

## 5. Track C - New evidence workflow

### C1. Define the input schema

The first workflow step must declare `step_input_schema` so the existing return-of-control surface
can derive required inputs.

Proposed required fields:

- `query`: natural-language evidence question or requested task;

Proposed optional fields:

- `pathogen_label`
- `taxon_id`
- `canonical_iri`
- `protein`
- `epitope`
- `include_conservation`
- `alignment_engine`: `mafft` or `muscle`
- `evidence_indices`: list of logical indices, defaulting to the relevant harmonized set;
- `requested_outputs`: `evidence_only` or `evidence_plus_design`;
- `design_approval_id`

Input behavior:

- if neither `taxon_id`, `canonical_iri`, nor enough text for entity resolution is present, return
  `needs_input(missing_param)` or `needs_input(ambiguous_entity)`;
- if `requested_outputs` includes design/optimization and no approval is present, return
  `needs_input(needs_prerequisite)` with the approval next action.

### C2. Reuse existing components

Preferred reuse sequence:

1. Query classification/enhancement concept from the existing viral immunology path, without
   reviving its stale YAML directly.
2. `SynthesisContextAssemblyStep` for PubMed, RAG, VIOLIN, BV-BRC, and Globus context.
3. Harmonized search execution for canonical index lookup.
4. `viral_conserved_sites` for optional conservation evidence.
5. `RagSynthesisStep` for evidence-bound markdown synthesis.
6. `EnvelopeStep` for terminal output.

Do not call the MCP tool from inside the backend workflow. Use shared Python workflow/step code so
the desktop MCP path and backend path remain the same capability with different callers.

### C3. Add an evidence integration step

Create a small step that turns heterogeneous records into a normalized evidence table:

```yaml
record_id: string
source: violin | bvbrc | antiviraldb | pubmed | rag | pdb | emdb | protabank
entity: string
canonical_iri: string | null
evidence_type: pathogen | vaccine | gene | protein | epitope | structure | publication | design
support_level: direct | indirect | background | conflicting | unsupported
citation_or_accession: string
summary: string
limitations: string
```

Acceptance criteria:

- structural records from PDB, EMDB, and ProtaBank use source-specific IDs;
- citations/accessions are preserved;
- unsupported or conflicting evidence is represented rather than dropped.

### C4. Workflow composition choice

Use a regular nanobrain workflow or lightweight builder, following the shipped patterns. If the
workflow embeds `viral_conserved_sites`, prefer one of:

- a subworkflow adapter that calls the existing builder-created workflow with `Workflow.run`; or
- a YAML-exported version of the conserved-sites workflow usable by `SubworkflowStep`.

Do not create a separate MCP-only code path.

Nanobrain constraints:

- instantiate via `from_config`;
- implement `process()`, never override `execute()`;
- workflow owns links; steps own their data units/triggers;
- every `DirectLink` must have `auto_transfer: true` unless the framework default has changed and a
  test proves it;
- any cycle or `ConditionalLink` branch needs a real `Workflow.run()` integration test that asserts
  terminal output values, not only `status`.

## 6. Track D - Approval-gated design/optimization outputs

Design and optimization outputs are out of scope until explicitly approved by the user.

### D1. Preflight gate

Before synthesis, inspect `requested_outputs` and the query text for design/optimization intent.
Examples:

- "design"
- "optimize"
- "mutate"
- "improve binding"
- "propose sequence"
- "engineering"

If detected without approval:

- desktop/MCP caller receives `WorkflowResult(status="needs_input")`;
- `control_transfer.reason == "needs_prerequisite"`;
- `next_action` names the approval requirement and available approval tools.

### D2. Backend approval path

Backend callers can satisfy the same gate with an approval token or approval state from the existing
approval control plane:

- `list_pending_approvals`
- `approve`
- `reject`
- `correct`

The workflow should not infer approval from vague text. Approval must be explicit data.

### D3. Approved output limits

After approval, generated design/optimization output must:

- be labeled as hypothesis or candidate, not validation;
- reference the evidence records that motivated it;
- include limitations and missing validation experiments;
- avoid unsupported wet-lab or clinical claims.

Acceptance criteria:

- evidence-only requests never create approval records;
- design requests without approval pause;
- design requests with approval proceed and carry approval provenance in the output.

## 7. Track E - Tests and verification

### E1. Unit tests

- registry accepts shared destination index for PDB, EMDB, ProtaBank;
- harmonized search validates `pdb` and `emdb`;
- shared destination source filters are present;
- evidence integration normalizes structural records;
- approval gate pauses design/optimization requests;
- first-step schema exposes required input shape for return-of-control.

### E2. Integration tests with real data

Required real-data checks:

1. Query the aggregate Globus index
   `e74bf12a-d0dd-4d19-a965-03f4936db851` for one PDB reference and one EMDB reference.
2. Republish/harmonize a small real subset into the ProtaBank destination index or a test-safe
   destination configured for the same schema.
3. Query `harmonized_search(index="pdb", ...)` and `harmonized_search(index="emdb", ...)` and prove
   they return source-distinct records.
4. Run `viral_epitope_evidence_review` on a small real viral example and assert:
   - terminal output is non-empty;
   - evidence table contains at least one publication or RAG record;
   - structural track is queried and reports either records or an explicit no-hit limitation;
   - design output is absent without approval.
5. Run the same workflow with explicit design approval and assert design output appears with approval
   provenance and evidence links.

Tests can be gated on credentials or external service availability, but they must skip honestly and
must not replace integration coverage with mocks.

### E3. Suggested first real scenario

Use a constrained viral query already close to existing tested paths:

```yaml
query: "Review evidence for conserved chikungunya structural polyprotein epitopes and relevant structural references."
taxon_id: 37124
protein: structural polyprotein
include_conservation: true
requested_outputs: evidence_only
```

This reuses the existing CHIKV conserved-sites verification while adding PubMed/RAG/harmonized
structural search.

## 8. Implementation sequence

### Phase 1 - Plan and inventory

1. Add this plan to the docs index.
2. Inventory real PDB/EMDB record shapes in aggregate index
   `e74bf12a-d0dd-4d19-a965-03f4936db851`.
3. Confirm the ProtaBank destination index UUID and write permissions.
4. Decide whether current "nine indices" means 11 or 12 final logical sources.

Exit criteria:

- representative PDB and EMDB records captured;
- source discriminators identified;
- no ambiguity remains about logical source count.

### Phase 2 - Harvester harmonization

1. Replace UUID-only registry assumptions with logical source descriptors.
2. Add `pdb` and `emdb` descriptors.
3. Route PDB, EMDB, and ProtaBank writes to
   `9e902471-9c77-49d3-a12c-516cc0808c3b`.
4. Populate canonical `subjects.valueUri` where possible.
5. Update tests from "nine distinct destinations" to "eleven logical sources, structural trio
   shares one destination by design."

Exit criteria:

- small real PDB and EMDB subset republished;
- idempotent rerun;
- destination records remain source-distinguishable.

### Phase 3 - Harmonized search update

1. Add `pdb` and `emdb` to MCP validation.
2. Map both to the shared ProtaBank destination index.
3. Add logical source filters for the shared destination.
4. Prefer `subjects.valueUri` matching for structural records.
5. Add unit and real integration tests.

Exit criteria:

- `harmonized_search` can query `pdb`, `emdb`, and `protabank` separately;
- shared-destination filtering is verified.

### Phase 4 - Evidence workflow skeleton

1. Add `viral_epitope_evidence_review` workflow scaffold.
2. Define first-step input schema.
3. Wire entity/query normalization, harmonized search, context assembly, synthesis, and envelope.
4. Add `run_workflow`/catalog visibility.

Exit criteria:

- missing inputs return `needs_input`;
- evidence-only run completes on a small real example.

### Phase 5 - Structural and conservation integration

1. Add structural lookup track for PDB, EMDB, and ProtaBank.
2. Add optional conserved-sites subworkflow/adapter.
3. Normalize records into the evidence table.
4. Ensure output explains no-hit structural cases.

Exit criteria:

- structural track is exercised in a real workflow run;
- conservation evidence is included when requested and absent when not requested.

### Phase 6 - Approval-gated design/optimization

1. Add design-intent preflight detection.
2. Add approval gate using existing control-transfer/approval primitives.
3. Add approved design-output branch.
4. Add tests for paused and approved paths.

Exit criteria:

- no design/optimization output without explicit approval;
- approved output includes approval provenance and evidence links.

### Phase 7 - End-to-end verification and documentation

1. Run targeted unit tests.
2. Run real Globus/harmonized-search integration tests.
3. Run the evidence workflow on the selected real viral scenario.
4. Update user-facing docs with the new workflow, inputs, approval behavior, and known limitations.
5. Commit with exact verification commands and outputs in the commit body.

Exit criteria:

- one evidence-only real run recorded;
- one approved design real run recorded, if approval is available;
- no mock-only claim of completion.

## 9. Open implementation questions

1. Does the aggregate Globus index expose a reliable source discriminator for PDB versus EMDB? If
   not, the first implementation task is to add one during ingest before shared-destination queries
   are safe.
2. Should PDB/EMDB harmonization write directly to the production ProtaBank destination during
   testing, or should the first integration run use a staging destination with the same schema?
3. Should `viral_epitope_evidence_review` always run conservation analysis when `taxon_id` and
   `protein` are present, or only when `include_conservation=true`? The conservative default is
   opt-in.
4. Should design/optimization approval be stored as a workflow input token, an approval-store
   lookup, or both? The plan allows both as long as approval is explicit.

## 10. Non-goals

- Do not revive the stale `viral_immunology_analysis` YAML as-is.
- Do not add a new MCP-only primitive that bypasses backend workflow execution.
- Do not emit design or optimization suggestions without explicit approval.
- Do not claim PDB/EMDB harmonization is complete from mocked records or synthetic fixtures.
- Do not rely on destination UUID alone to distinguish PDB, EMDB, and ProtaBank records.
