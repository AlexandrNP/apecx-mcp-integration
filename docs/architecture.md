# APECx MCP Integration — End-to-End Architecture

This document is the canonical map of the apecx-mcp-integration system.
It covers the three runtime tiers (MCP surface, control plane, executor),
the synthesis pipeline, all 23 MCP tools, all six ontologies, the
mapping/resolution strategies, the test surface, and external service
dependencies.

Audience: a fresh engineer onboarding to the project, or an operator
deploying the system to a new environment. The text is structured to be
read top-down; Mermaid diagrams render in GitHub, GitLab, and most
Markdown viewers (including Claude Desktop's preview).

---

## 1. System purpose in one paragraph

APECx-MCP-integration takes free-text scientist questions about
viral pathogens, vaccines, genes, and genomes, and turns them into
**grounded Markdown answers with inline citations**. It does this by
fanning out across local FAISS-indexed knowledge (domain RAG),
local VIOLIN/BV-BRC tabular data (substring lookup), live PubMed
publications, and the APECx Globus Search index of harvested records,
then driving one LLM call to weave the retrieved evidence into a
structured response. The system is exposed through the Model Context
Protocol (MCP) so it appears as a tool surface inside Claude Desktop
and any MCP-compatible client.

---

## 2. Three-tier runtime topology

```mermaid
flowchart TB
    subgraph Client["MCP Client"]
        CD["Claude Desktop / IDE / CLI<br/>(stdio transport)"]
    end

    subgraph Tier1["Tier 1 — MCP surface (this repo)"]
        SRV["FastMCP server<br/>apecx-mcp-integration<br/>23 tools"]
        ToolsW["workflow tools (3)<br/>start_workflow / show_diff / execute_workflow"]
        ToolsD["discovery tools (2)<br/>list_workflows / describe_workflow"]
        ToolsDB["database tools (7)<br/>query_vaccines / pathogens / genes / genomes / etc."]
        ToolsCE["resolve_canonical_entity (1)"]
        ToolsSy["synthesize_query (1)"]
        ToolsGS["query_globus_search (1)"]
        ToolsAp["approval tools (4)"]
        ToolsHpc["HPC tools (4)"]
    end

    subgraph Tier2["Tier 2 — Control Plane (sibling)"]
        CP["apecx-cp serve<br/>FastAPI / SQLite<br/>workflows · runs · approvals · artifacts"]
    end

    subgraph Tier4["Tier 4 — Executors (nanobrain)"]
        Exec["LocalExecutor / Parsl / Academy<br/>drives Workflow.process()"]
    end

    subgraph Data["Data sources"]
        FAISS["domain_rag<br/>FAISS + sentence-transformers"]
        VIOLIN["VIOLIN CSVs<br/>data/violin/"]
        BVBRC["BV-BRC TSVs<br/>data/bvbrc_cache/"]
        PUBMED["NCBI PubMed eUtils<br/>(network)"]
        GLOBUS["Globus Search index<br/>e74bf12a... (network, public)"]
        DICT["synonym_dictionary<br/>SQLite"]
        LLM["APECX_LLM_BASE_URL<br/>OpenAI-compatible API"]
    end

    CD <-- "stdio JSON-RPC" --> SRV
    SRV --> ToolsW & ToolsD & ToolsDB & ToolsCE & ToolsSy & ToolsGS & ToolsAp & ToolsHpc
    ToolsW --> CP
    ToolsAp --> CP
    ToolsHpc --> CP
    ToolsW --> Exec
    ToolsDB --> VIOLIN
    ToolsDB --> BVBRC
    ToolsDB --> DICT
    ToolsCE --> DICT
    ToolsGS --> GLOBUS
    ToolsSy --> FAISS & VIOLIN & BVBRC & PUBMED & GLOBUS & LLM
    Exec --> FAISS & VIOLIN & BVBRC & PUBMED & GLOBUS & LLM
```

**Key facts:**
- **Tier 1 (this repo)** ships 22 MCP tools registered via FastMCP.
  Source: `src/apecx_integration/mcp_surface/server.py:96-138` (verified
  count: 22 `server.tool()` calls).
- **Tier 2** is the control plane (`apecx-cp serve`, separate package).
  Auto-starts on first MCP startup unless `APECX_MCP_AUTOSTART_BACKEND=0`.
- **Tier 4** is the nanobrain executor that drives composed workflows
  through the trigger graph.
- **Tier 3** (HPC bundle export) is not a runtime component — it
  produces qsub-able artifacts for offline submission. Re-ingest
  happens via `ingest_hpc_bundle`.

---

## 3. Synthesis pipeline (the headline feature)

The synthesis pipeline answers a single scientist question by fanning
out across three retrieval sources, then producing a Markdown answer
with inline citations.

### 3.1 Data flow

```mermaid
flowchart LR
    Q["scientist query<br/>str"]

    subgraph Assembly["SynthesisContextAssemblyStep"]
        direction LR
        Q --> AS["assembly_input<br/>DataUnitMemory"]
        AS --> Gather["asyncio.gather&lt;br/&gt;return_exceptions=True"]
        Gather --> RAG["DomainRagIndex.search<br/>(FAISS)"]
        Gather --> VBL["lookup_violin / lookup_bvbrc<br/>(pandas)"]
        Gather --> PUB["pubmed eSearch + eFetch<br/>(network, optional)"]
        Gather --> GS["globus_search.search<br/>(network, optional)"]
        RAG --> Bundle
        VBL --> Bundle
        PUB --> Bundle
        GS --> Bundle
        Bundle["synthesis_bundle_output<br/>DataUnitMemory"]
    end

    Bundle -- "DirectLink<br/>assembly_to_synthesis" --> SI["synthesis_input<br/>DataUnitMemory"]

    subgraph Synthesis["RagSynthesisStep"]
        SI --> LLM["synthesize_response<br/>(one LLM call)"]
        LLM --> Gates["validation gates<br/>size · grounded · empty-retrieval"]
        Gates --> SO["synthesis_output<br/>DataUnitMemory"]
    end

    SO --> Out["Markdown response<br/>{synthesis: str}"]
```

**Bundle shape** (verified at
`src/apecx_integration/composition/steps/synthesis_context_assembly_step.py`):

```python
{
    "query":           str,           # the original question
    "rag_chunks":      list[dict],    # FAISS hits: id, text, score, source, metadata
    "bvbrc_genomes":   list[dict],    # genome_id, genome_name
    "violin_mappings": list[dict],    # synonym_id, canonical_term, query_term, entity_type, source
    "publications":    list[dict],    # doi, title, authors[], year, journal, pmid
    "globus_results":  list[dict],    # subject, content, score (added 2026-05-05)
}
```

### 3.2 Failure contract per branch

| Branch | What can fail | Effect | Where caught |
|---|---|---|---|
| Domain RAG | Missing FAISS index, corrupted bin file | `rag_chunks=[]`, WARNING logged | `asyncio.gather(return_exceptions=True)` in `synthesis_context_assembly_step.py` |
| VIOLIN/BV-BRC | Missing CSV, missing required column | both bundles `[]`, WARNING logged | same gather |
| PubMed | Network error, eUtils 5xx, timeout | `publications=[]`, WARNING logged | inner try/except in `_pubmed_harvest` + outer gather |
| Globus Search | SDK missing, network failure, invalid index UUID | `globus_results=[]`, WARNING logged | `GlobusSearchUnavailableError` → outer gather |
| Synthesis LLM | Endpoint down, model rejects request | `ValueError` raised | caller (synthesize_query MCP tool catches and returns `{"error": ...}`) |
| All branches empty | Synthesizer's `fail_on_empty_retrieval` gate | `ValueError` raised | synthesis_config.yml gate |

### 3.3 Three invocation paths

```mermaid
flowchart TB
    Q["scientist query"]

    subgraph "Path A — MCP tool (canonical)"
        TOOL["synthesize_query MCP tool<br/>tools/synthesis.py"]
        Q --> TOOL
        TOOL -- "module-level singleton<br/>(loaded once)" --> A1["assembly.process()"]
        A1 --> A2["synthesis.process()"]
        A2 --> A3["{synthesis: markdown}"]
    end

    subgraph "Path B — Workflow runtime"
        WF["Workflow.from_config(rag_e2e_synthesis_workflow.yml)"]
        Q --> WF
        WF -- "data-driven<br/>process(input)" --> B1["assembly_input.set()"]
        B1 -. "trigger cascade<br/>(async background)" .-> B2["assembly fires"]
        B2 -. "DirectLink<br/>auto-transfer" .-> B3["synthesis fires"]
        B3 -. "" .-> B4["{synthesis: markdown}"]
    end

    subgraph "Path C — Direct step instantiation (test code)"
        S1["BaseStep.from_config(assembly.yml)"]
        S2["BaseStep.from_config(synthesis.yml)"]
        Q --> S1 --> S2 --> C1["{synthesis: markdown}"]
    end
```

**Pick one:** Path A is the canonical operator path (one LLM call, no
composer overhead, cached steps). Path B is for the case where the
composer plans the synthesis as part of a larger workflow. Path C is
exclusive to test code.

### 3.4 Trigger cascade — `Workflow.wait_for_cascade`

Path B above (workflow runtime) is data-driven:
`Workflow.process(input_data)` writes to the first step's input data
unit and **returns immediately** with
`{"status": "data_flow_initiated", ...}`. The actual work happens
in async background tasks the trigger executor spawns; the caller
needs a way to await cascade completion synchronously.

**The framework primitive (added 2026-05-05):**

```python
wf = Workflow.from_config('rag_e2e_synthesis_workflow.yml')
await wf.initialize()                                 # resolves triggers
await wf.process({'assembly_input': {'query': '...'}})
ok = await wf.wait_for_cascade(timeout=90.0, settle_ms=100)
assert ok
output = await wf.child_steps['rag_synthesis'] \
    .step_output_data_units['synthesis_output'].get()
```

`wait_for_cascade` delegates to
`AsyncTriggerExecutor.wait_for_all_tasks(timeout, settle_ms)`,
which loops over the executor's background-task set and re-snapshots
on each iteration to catch transitively-spawned tasks (a snapshot
taken once at call time misses task A spawning task B during its
execution).

**Why this matters — four silent-failure bugs caught by this test:**

1. **`DirectLink.auto_transfer` defaults to `False`.** Without
   `auto_transfer: true` in the link config, the link only
   transfers on explicit `transfer()` calls; the trigger cascade
   silently no-ops. The workflow YAML loads, validators pass, no
   exception fires — and no work happens.
2. **Workflow-level `input_data_units` / `output_data_units` are
   required.** The framework's integrity validator
   (`workflow.py::_validate_workflow_integrity`) raises
   `ComponentConfigurationError` at `Workflow.initialize()` time
   if a step input has no source or a step output has no consumer.
   The required workflow-level data units are DIFFERENT from the
   forbidden bare `data_units:` key.
3. **Step input wrapping.** `Step._execute_on_trigger` wraps
   trigger inputs as `{unit_name: payload}`, but direct callers
   pass payload raw. Steps that only handle the raw shape silently
   no-op in trigger mode.
4. **Single-output fallback parity.** When a step returns a dict
   that does NOT contain the output data unit's name as a key,
   trigger mode used to skip writing while imperative mode wrote
   the entire result. Brought into parity in nanobrain
   `step.py::_update_output_data_units`.

All four are now pinned by `test_workflow_runtime_executes_via_trigger_cascade`
in `tests/integration/test_rag_e2e_workflow_yaml.py`.

---

## 4. The 23 MCP tools

Source: `src/apecx_integration/mcp_surface/server.py`. Every tool
returns either a result dict or `{"error": "..."}`; tools never raise
to the MCP transport.

### 4.1 Workflow tools (3)

| Tool | Purpose | Input | Backend |
|---|---|---|---|
| `start_workflow` | Compose a workflow from a natural-language description (T-COMP) | `description, user_id, preferred_executor` | Composer LLM + Control Plane |
| `show_diff` | Surface the differential-review payload (HITL approval prep) | `run_id` | Control Plane |
| `execute_workflow` | Run a composed workflow locally (synchronous) | `run_id` | LocalExecutor + Control Plane |

### 4.2 Discovery tools (2)

| Tool | Purpose | Input | Backend |
|---|---|---|---|
| `list_workflows` | Enumerate workflows the composer can build | none | Manifest YAML parse |
| `describe_workflow` | Per-component view of one workflow | `name` | Manifest YAML parse |

### 4.3 Database tools (7) — direct VIOLIN + BV-BRC lookup

| Tool | Purpose | Resolution | Backend |
|---|---|---|---|
| `query_vaccines` | Search VIOLIN vaccine DB (~3,500 rows) | dict fast → substring slow | DatabaseStore (pandas) |
| `query_pathogens` | Search VIOLIN pathogen DB (~220 rows) | dict fast → ancestor walk → strict descendant expansion | DatabaseStore + DictionaryIndex |
| `query_genes` | Search VIOLIN gene DB (~4,000 rows) | dict fast → substring slow | DatabaseStore |
| `query_bvbrc_genomes` | Search BV-BRC alphavirus genomes (~17,000 rows) | dict fast → substring slow | DatabaseStore |
| `get_vaccine_pathogen_genes` | Traverse VIOLIN junction tables | direct lookup | DatabaseStore |
| `resolve_entity` | Multi-table substring scan + dict lookup | dict + substring | DatabaseStore + DictionaryIndex |
| `database_statistics` | Row counts + columns for all loaded tables | none | DatabaseStore metadata |

### 4.4 Entity resolution + synthesis + Globus search (3)

| Tool | Purpose | Output |
|---|---|---|
| `resolve_canonical_entity` | Stage 2 fast path: lookup → ancestor → slow → miss | `{path, canonical_iri, canonical_label, confidence, ...}` |
| `synthesize_query` | Drive the rag_e2e_synthesis pipeline directly | `{synthesis: markdown, retrieved: {counts}}` |
| `query_globus_search` | Free-text query of the APECx harvested-corpus index | `{results: [{subject, content, score}], count, query}` |

### 4.5 Approval tools (4) — HITL gate

| Tool | Purpose |
|---|---|
| `list_pending_approvals` | List pending HITL gates for a user |
| `approve` | Approve a gate |
| `reject` | Reject with required justification |
| `correct` | Approve with reviewer modifications |

### 4.6 HPC tools (4) — Polaris/Aurora bundle export

| Tool | Purpose |
|---|---|
| `estimate_cost` | Estimate core-hours for a composed run |
| `confirm_allocation` | Lock in operator-approved allocation |
| `export_hpc_bundle` | Write qsub-able bundle to disk (no submission) |
| `ingest_hpc_bundle` | Re-ingest provenance after offline run completes |

---

## 5. Synthesis pipeline components (deep view)

### 5.1 Step inventory

| Class | File | Purpose | I/O contract |
|---|---|---|---|
| `SynthesisContextAssemblyStep` | `composition/steps/synthesis_context_assembly_step.py` | Fan-in retrieval (3 branches concurrently) | in: `{query, entities?, query_terms?}` → out: bundle dict (5 keys) |
| `RagSynthesisStep` | `composition/steps/rag_synthesis_step.py` | One LLM call → Markdown with citations | in: bundle dict → out: `{synthesis: str}` |
| `DomainRagSearchStep` | `composition/steps/domain_rag_step.py` | FAISS semantic search over pre-built index | in: `{query}` → out: `{rag_chunks: [...]}` |
| `VIOLINBVBRCContextStep` | `composition/steps/violin_bvbrc_context_step.py` | Pure-pandas substring lookup | in: `{entities? \| query_terms?}` → out: `{violin_mappings, bvbrc_genomes}` |
| `PubMedHarvesterStep` | `composition/steps/pubmed_harvester_step.py` | NCBI eSearch + eFetch | in: `{query, entities?}` → out: `{publications: [...]}` |

### 5.2 Stateless utility modules (extracted 2026-05-05)

To avoid `object.__new__` shortcuts, two helper modules carry the
shared logic that both the step classes and `SynthesisContextAssemblyStep`
need:

| Module | Provides | Used by |
|---|---|---|
| `composition/steps/_violin_bvbrc_lookup.py` | `lookup_violin`, `lookup_bvbrc` | VIOLINBVBRCContextStep + SynthesisContextAssemblyStep |
| `composition/steps/_pubmed_helpers.py` | `build_term`, `entity_name`, `container_to_dict`, `harvest` | PubMedHarvesterStep + SynthesisContextAssemblyStep |

Both are pure functions; `owner_name` parameter flows into log
prefixes for caller correlation.

### 5.3 Workflow YAML (rag_e2e_synthesis)

```mermaid
flowchart TB
    subgraph "rag_e2e_synthesis_workflow.yml"
        WF["name: rag_e2e_synthesis_workflow<br/>version: 0.2.0"]
        SCA["synthesis_context_assembly:<br/>class: SynthesisContextAssemblyStep<br/>config: steps/synthesis_context_assembly.yml"]
        RS["rag_synthesis:<br/>class: RagSynthesisStep<br/>config: steps/rag_synthesis.yml"]
        LINK["assembly_to_synthesis:<br/>DirectLink<br/>source: assembly.synthesis_bundle_output<br/>target: rag_synthesis.synthesis_input"]
    end

    SCA --> LINK
    LINK --> RS
```

**Loadability is pinned by:** `tests/integration/test_rag_e2e_workflow_yaml.py`
(6 tests: 5 static + 1 runtime). The runtime test drives the
framework-instantiated step instances via direct `process()` calls
because nanobrain's `Workflow.process(input)` is fire-and-forget
(populates the first input data unit and returns immediately;
the actual cascade runs in async background tasks).

---

## 6. Ontologies

Source: `src/apecx_integration/synonym_dictionary/enums.py`.

| Ontology | Code | IRI prefix | Hierarchy walk | Used by |
|---|---|---|---|---|
| **NCBITaxon** | `ncbitaxon` | `http://purl.obolibrary.org/obo/NCBITaxon_<id>` | ✅ ancestor + descendant CTEs | pathogen/genome lookup, `query_pathogens`, taxonomy expansion |
| **Vaccine Ontology (VO)** | `vo` | `http://purl.obolibrary.org/obo/VO_<id>` | ❌ flat | vaccine entity resolution, `query_vaccines` |
| **Disease Ontology (DOID)** | `doid` | `http://purl.obolibrary.org/obo/DOID_<id>` | reserved (no hierarchy load yet) | disease entity resolver |
| **Gene Ontology (GO)** | `go` | `http://purl.obolibrary.org/obo/GO_<id>` | reserved | gene/protein function annotation |
| **NCBI Gene** | `ncbigene` | `http://identifiers.org/ncbigene/<id>` (NOT OBO) | ❌ flat | gene entity resolver, `query_genes` |
| **APECx Local** | `apecx_local` | `http://apecx.local/...` | ❌ flat | lab strains, project-private entities (per analysis doc §4.9(3)) |

**Resolution-status taxonomy** (orthogonal to ontology source):

| Status | Confidence | Meaning |
|---|---|---|
| `id_anchored` | 1.0 | Source row carried an authoritative ID; synonyms fetched from OLS |
| `ols_exact` | ~0.9 | OLS exact-match search hit (label or synonym) |
| `ols_fuzzy` | <0.9 | OLS multi-match disambiguated by row context |
| `project_local` | varies | Project-private IRI (no external ontology mapping exists) |
| `unresolved` | 0.0 | No mapping; row stays in dictionary with `canonical_iri = None` (surfaces explicitly per §4.10) |

---

## 7. Mapping / resolution strategies

### 7.1 Stage 1 — dictionary build (offline, one-time per release)

```mermaid
flowchart LR
    VIN["VIOLIN CSVs<br/>(Pathogen / Vaccine / Gene)"] --> XR["resolvers.py<br/>per-entity-type extractors"]
    OLS["OLS REST API<br/>(EBI)"] --> XR
    NTAX["NCBI taxdump<br/>(optional, --ncbitaxon-nodes)"] --> HL["hierarchy_loader.py"]
    XR --> ENT["DictionaryEntry<br/>per (entity_type, IRI)"]
    HL --> H["taxon_hierarchy<br/>edges"]
    ENT --> SQW["sqlite_writer.py"]
    H --> SQW
    SQW --> DICT["apecx_synonym_dict.sqlite"]
```

Built by the internal `dictionary_build_workflow` (nanobrain workflow at
`synonym_dictionary/workflow/configs/dictionary_build_workflow.yml`,
driven lazily at apecx-mcp startup by `bootstrap.ensure_dictionary`).
The output SQLite carries:
- `dictionary_entries` — one row per `(entity_type, canonical_iri)`
- `synonym_synonyms_index` — inverse index `(entity_type, normalized) → IRI`
- `taxon_hierarchy` — parent/child edges for NCBITaxon (when `TaxdumpFetchStep` ran)
- `merged_taxons` — old→new ID redirect table for deprecated NCBITaxon IDs
- `ambiguous_surface_forms` — when two IRIs share a normalized form, the loser is recorded here

### 7.2 Stage 2 — runtime lookup (every query)

```mermaid
flowchart TB
    Q["surface form + entity_type?"]
    Q --> NORM["normalize_surface_form<br/>lower / strip / squash whitespace"]
    NORM --> FAST{"inverse_index lookup<br/>O(1) hash"}
    FAST -- hit --> FE["DictionaryEntry<br/>path=fast"]

    FAST -- "miss<br/>+ NCBITaxon IRI" --> ANC["taxon_hierarchy<br/>recursive CTE upward"]
    ANC -- ancestor in dict --> AE["DictionaryEntry<br/>path=ancestor<br/>confidence × 0.9"]
    ANC -- no ancestor in dict --> SLOW

    FAST -- "miss<br/>+ NOT IRI" --> SLOW{"DatabaseStore<br/>substring scan"}
    SLOW -- match --> SE["LookupResult<br/>path=slow<br/>confidence ≈ 0.3"]
    SLOW -- no match --> MISS["LookupResult<br/>path=miss<br/>confidence = 0.0"]
```

**Special case — descendant expansion (NCBITaxon only):**

`query_pathogens` calls `lookup_descendant_taxon_ids(iri)` after a fast
or ancestor hit. A family-level IRI like Coronaviridae expands to its
~20+ species; a species-level IRI like SARS-CoV-2 expands to itself
plus any strain-level children. The pandas filter then matches by
`NCBI_Taxonomy_ID.isin(expanded_set)`.

| Query | IRI resolved | Descendant expansion | Filter set |
|---|---|---|---|
| `"Coronaviridae"` | `NCBITaxon_11118` (family) | walks down 4 levels | ~20+ species |
| `"covid-19"` | `NCBITaxon_2697049` (species) | empty (leaf) | just `[2697049]` |
| `"SARS-CoV-2 strain X"` | `NCBITaxon_2697049` (via ancestor walk from strain) | `[2697049]` | just SARS-CoV-2 |

This is the **strict hierarchy contract** (user directive 2026-05-05).

---

## 8. External services + data dependencies

### 8.1 Required at runtime

| Service | What it provides | How to configure | Failure mode |
|---|---|---|---|
| **APECX_LLM_BASE_URL** | LLM completion (synthesis, composition, entity extraction) | env var; default `http://localhost:11434/v1` (Ollama) | Synthesis raises ValueError → MCP returns `{"error": ...}` |
| **NCBI eUtils** (network) | PubMed eSearch + eFetch | hard-coded `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/`; rate-limit 3/s | Branch returns `[]`, WARNING logged |
| **Globus Search** (network) | APECx harvested-corpus index — public, no auth | default index UUID `e74bf12a-d0dd-4d19-a965-03f4936db851`; `APECX_GLOBUS_SEARCH_INDEX_UUID` overrides; `APECX_GLOBUS_SEARCH_DISABLED=1` skips | Branch returns `[]`, WARNING logged |
| **Control Plane** | workflow + approval + HPC state | `APECX_CONTROL_PLANE_URL` env var (default `http://localhost:8000`); auto-starts on first MCP call | Server exits(2) with remediation hint if unreachable |

### 8.2 Required offline data

| Asset | Where | Builder | Resolution order |
|---|---|---|---|
| Domain RAG FAISS index | `<workspace>/data/apecx_domain_rag/{faiss_index.bin, metadata.json}` | `scripts/build_domain_rag_index.py` | YAML override → workspace default |
| VIOLIN CSVs | `<workspace>/data/violin/Pathogen_Information.csv` etc. | `apecx-setup` (downloads from private repo) | YAML → APECX_DB_DATA_DIR → APECX_WORKSPACE_ROOT |
| BV-BRC TSV | `<workspace>/data/bvbrc_cache/alphavirus_genomes.tsv` | bundled with `apecx-setup` | YAML → APECX_WORKSPACE_ROOT |
| Synonym dictionary | `APECX_SYNONYM_DICT_PATH` (defaults to `~/.apecx/dictionary/dictionary.sqlite`) | `dictionary_build_workflow` (lazy at apecx-mcp startup; `APECX_SKIP_DICT_BUILD=1` to opt out) | env var only; missing → fast path disabled |

`<workspace>` resolves via `apecx_integration._workspace.resolve_workspace_root`
in this order: `APECX_WORKSPACE_ROOT` env var → marker-walk for
`apecx-mcp-integration/` + `nanobrain/_workspace_notes/data` siblings →
`Path(__file__).parents[N]` fallback for the standard checkout.

### 8.3 Cross-repo dependencies

```mermaid
flowchart LR
    AMI["apecx-mcp-integration<br/>(this repo)"]
    NB["nanobrain<br/>(framework)"]
    AH["apecx-harvesters<br/>(PubMed loaders + harvest writer — write-side OUT OF SCOPE)"]
    ADB["apecx-db-integration<br/>(LLM-driven entity functions)"]
    ARG["apecx-rag<br/>(synthesis prompts/config)"]

    AMI -- "BaseStep, StepConfig, ApprovalStep, viral_protein_analysis steps,<br/>wait_for_cascade (added 2026-05-05)" --> NB
    AMI -- "pubmed.search, pubmed.retrieve, DataCite container" --> AH
    AMI -- "extract_entities_llm, get_candidate_terms (via wrappers)" --> ADB
    AMI -- "synthesize_response, SynthesisConfig, prompt files" --> ARG
```

**Harvester boundary (2026-05-05 user directive):** The harvester
side of `apecx-harvesters` runs as a STAND-ALONE process: it
populates two outputs (the APECx synonym dictionary AND the Globus
Search index) and is invoked offline. This repo only consumes:

- **Synonym dictionary** via `apecx_integration.synonym_dictionary`
  (Stage 2 lookup, `APECX_SYNONYM_DICT_PATH`).
- **Globus Search index** via
  `apecx_integration.agents.globus_search.search` (public, no auth).
- **PubMed loaders** via `apecx_harvesters.loaders.pubmed.*` for
  the live PubMed retrieval branch in the synthesis pipeline.

Harvester WRITE code is explicitly out of scope. Any change to how
harvesters populate the index belongs in the apecx-harvesters repo.

All sibling repos must be `pip install -e ../<repo>`'d into the project venv.

---

## 9. Composer catalog — workflows the LLM can build

The composer (T-COMP) reads `composition/composer_config.yml`'s
`component_catalog_paths` list. Two manifests are registered:

### 9.1 `workflows/violin_bvbrc/manifest.yml` — VIOLIN × BV-BRC synonym gate

13 components (1 deferred). The full HARD-synonym workflow plus the
four Day-2 retrieval/synthesis steps. Verbatim from the manifest:

| Step ID | Step | Disposition | Status |
|---|---|---|---|
| 1 | entity_extraction | wrap | ready |
| 2 | bvbrc_snapshot_match | wrap | ready |
| 3a | synonym_cache_lookup | new | ready |
| 3b | synonym_fuzzy_match | deferred | per HARD-synonym 2026-04-21 |
| 3c | synonym_llm_proposals | wrap | ready |
| 4 | synonym_approval_gate | new | ready |
| 4p | verified_synonym_writeback | new | ready |
| 5 | violin_entity_lookup | wrap | ready |
| 6 | genomic_annotation | wrap | ready |
| 7 | result_ranking | reuse | ready |
| 8a | domain_rag_search | new | ready |
| 8b | violin_bvbrc_context | new | ready |
| 8c | pubmed_harvester | new | ready |
| 8d | rag_synthesis | new | ready |

### 9.2 `workflows/rag_e2e_synthesis/manifest.yml` — pure synthesis

| Step ID | Step | Status |
|---|---|---|
| A1 | synthesis_context_assembly | ready |
| A2 | rag_synthesis | ready |

The `system.md` composer prompt explicitly tells the LLM: prefer the
`SynthesisContextAssemblyStep` fan-in over wiring three retrieval
steps directly into `RagSynthesisStep` (its input data unit
expects a single pre-assembled bundle).

---

## 10. Test surface

### 10.1 Counts (verified 2026-05-05)

| Suite | Files | Tests | Notes |
|---|---|---|---|
| `tests/unit/` | 38 | **504** | All run against pure pandas / SQLite / mocks; no external services |
| `tests/integration/` | 152 | **2,097** | Many gated on env vars (Ollama, control plane, GitHub); auto-skip when absent |

Full unit run: ~3.3s. Integration including Ollama subset: ~3 min when
Ollama is reachable. The trigger-cascade runtime test alone takes
~69s (real LLM round-trip).

### 10.2 Synthesis-pipeline-specific test coverage

| File | Tests | What it covers |
|---|---|---|
| `tests/unit/test_synthesis_assembly_branch_failures.py` | 6 | Per-branch failure → empty bundle (gather degradation) |
| `tests/unit/test_synthesize_query_tool.py` | 7 | MCP tool input validation, load-error caching, gate marshaling, skip_pubmed restoration |
| `tests/unit/test_violin_bvbrc_lookup_helpers.py` | 12 | Stateless lookup utility — substring match, vaccine override, missing files, dedupe |
| `tests/unit/test_pubmed_helpers.py` | 26 | Stateless PubMed helpers — entity_name, build_term, container_to_dict, 25-author cap |
| `tests/unit/test_globus_search.py` | 19 | Globus client, MCP tool, synthesizer renderer, citation pattern |
| `tests/unit/test_composer_prompt_correctness.py` | 7 | Pin substantive content of composer's system prompt |
| `tests/unit/test_workspace_root_resolver.py` | 6 | Env var override > marker walk > parents[N] fallback |
| `tests/unit/test_descendant_traversal.py` | 9 | Strict NCBITaxon hierarchy expansion |
| `tests/integration/test_rag_e2e_workflow_yaml.py` | 7 | 5 static loadability + 1 imperative-drive + 1 trigger-cascade runtime |
| `tests/integration/test_rag_e2e_pipeline.py` | 25 | E2E with real Ollama, real FAISS, real CSVs (gated; auto-skip) |
| `tests/integration/test_violin_bvbrc_workflow_yaml.py` | 8 | Loadability of the violin_bvbrc workflow + each step YAML |

### 10.3 Test gating

- `pytest.importorskip("sentence_transformers")` (BEFORE faiss — load-bearing on macOS ARM)
- `_ollama_reachable()` — checks `http://localhost:11434/api/tags`
- `(DOMAIN_RAG_INDEX / "faiss_index.bin").exists()` — auto-skips when not built
- `(VIOLIN_DIR / "Pathogen_Information.csv").exists()` — auto-skips when not provisioned
- `APECX_SKIP_LIVE_LLM=1` — operator-side opt-out

---

## 11. Data quality assessment

The harmonization pipeline produces a synonym dictionary; the user-facing
workflow consumes it. Both ends need a yardstick — "how often does the
mapping return the right canonical IRI?" — that is meaningful, fast to
run, and honest about its limits. The answer in this repo is a
**three-test pattern** backed by an `AccuracyMetrics` dataclass and
enforced as CI floors.

### 11.1 The `AccuracyMetrics` dataclass

`src/apecx_integration/synonym_dictionary/metrics.py` defines a single
dataclass that all accuracy tests roll up to:

| Field | Meaning |
|---|---|
| `total_rows` | rows examined in the slice or full corpus |
| `rows_with_ground_truth` | rows where a canonical IRI is known a-priori |
| `fast_count` / `ancestor_count` / `slow_count` / `miss_count` | which lookup path each row resolved through |
| `correct` / `incorrect` | matched-vs-expected canonical IRI |
| `recall()` | `correct / rows_with_ground_truth` |
| `precision()` | `correct / (correct + incorrect)` |
| `f1()` | harmonic mean |
| `summary()` | one-line printable string used in pytest assertion messages |

Per-class wrappers exist for Pathogen, Vaccine, Gene, and Disease.
Behavior is pinned by `tests/unit/test_metrics_invariants.py` (recall
denominators include misses; precision denominators do not include
unresolved rows).

### 11.2 Three-test pattern

| Stage | Sample size | Wall-clock budget | Gate |
|---|---|---|---|
| **Slice baseline** | 60 deterministic rows per class | Seconds | `APECX_SYNONYM_DICT_LIVE_OLS=1` |
| **Full corpus** | 13,238 rows total (218 / 3,507 / 4,063 / 5,450) | Minutes | `APECX_SYNONYM_DICT_LIVE_OLS=1` AND `APECX_SYNONYM_DICT_FULL_CORPUS=1` |
| **Probe-batch sampling** | 50 / 300 spot-checks at decision boundaries | Seconds | `APECX_SYNONYM_DICT_LIVE_OLS=1` |

The slice stays green on every commit; the full corpus runs nightly /
on release. Probe batches catch regressions in specific decision
boundaries (e.g. ancestor-walk depth limits) without paying the
full-corpus cost.

### 11.3 CI-enforced floors (lower bounds)

Source of truth: `tests/integration/test_synonym_accuracy.py`.

| Class | Slice (60 rows) | Full corpus | F1 floor |
|---|---|---|---|
| Pathogen | recall ≥ 0.95, precision ≥ 0.95 | recall ≥ 0.90, precision ≥ 0.95 | slice ≥ 0.95, full ≥ 0.92 |
| Vaccine | recall ≥ 0.80, precision ≥ 0.80 | recall ≥ 0.75, precision ≥ 0.85 | not separately enforced |
| Gene | recall ≥ 0.70, precision ≥ 0.95 | recall ≥ 0.65, precision ≥ 0.95 | not separately enforced |
| Disease | search-only | search-only | n/a — no recall floor |

These are **lower bounds**, not target observed values. A build that
misses any floor fails CI; the actual observed numbers are typically
above the floor by 2–10 percentage points. We deliberately do not
publish the observed numbers in this doc — they drift with every OLS
update and ontology release, and a stale "we hit 97% recall last
quarter" claim is more harmful than the floor.

Disease has no recall floor by design: the ontology surface
(DOID + cross-refs) is open-ended and recall is not the right metric
for an open-vocabulary search target. Vaccine and Gene F1 floors are
omitted because recall + precision floors mathematically constrain F1
already.

### 11.4 Mocks-only-for-smoke (workspace policy applied)

The accuracy tests do not mock OLS. When `APECX_SYNONYM_DICT_LIVE_OLS=1`
is unset they auto-skip; this matches the workspace rule that mocks are
only allowed when they back a smoke test, never when they substitute for
a real integration. A passing accuracy test always implies a real OLS
round-trip happened.

### 11.5 Harmonization statistics (verified 2026-05-05)

Source corpus row counts (from `wc -l` on the data files):

| Dataset | Rows | Source |
|---|---|---|
| VIOLIN Pathogens | 218 | `data/violin/Pathogen_Information.csv` |
| VIOLIN Vaccines | 3,507 | `data/violin/Vaccine_Information.csv` |
| VIOLIN Genes | 4,063 | `data/violin/Gene_Information.csv` |
| BV-BRC Genomes | 5,450 | `data/bvbrc/genomes.tsv` |
| **Total** | **13,238** | |

Resolution status taxonomy (from
`src/apecx_integration/synonym_dictionary/enums.py`). Every dictionary
row carries one status value and one confidence value; the synthesis
pipeline surfaces both to the caller so downstream code can filter by
quality:

| Status | Confidence | Meaning |
|---|---|---|
| `id_anchored` | 1.0 | NCBI Taxon / VO ID / etc. resolved by ID, not name |
| `ols_exact` | 0.9 | OLS returned an exact label match |
| `ols_fuzzy` | < 0.9 | OLS fuzzy match (similarity < 1.0) |
| `project_local` | 0.5 | apecx_local IRI; lab-private entity |
| `unresolved` | 0.0 | No mapping; row stays in dictionary with `canonical_iri = None` |

See `docs/figures/12_accuracy_thresholds.png` and
`docs/figures/13_harmonization_stats.png` for the visualizations
embedded in `architecture_slides.pptx`.

---

## 12. Configuration flow

```mermaid
flowchart TB
    subgraph EnvVars["Environment variables"]
        EV1["APECX_LLM_BASE_URL"]
        EV2["APECX_LLM_MODEL"]
        EV3["APECX_LLM_API_KEY"]
        EV4["APECX_LLM_TEMPERATURE"]
        EV5["APECX_LLM_MAX_TOKENS"]
        EV6["APECX_DATA_ROOT / APECX_DB_DATA_DIR"]
        EV7["APECX_SYNONYM_DICT_PATH"]
        EV8["APECX_CONTROL_PLANE_URL"]
        EV9["APECX_WORKSPACE_ROOT"]
        EV10["APECX_SKIP_LIVE_LLM"]
        EV11["APECX_GLOBUS_SEARCH_INDEX_UUID"]
        EV12["APECX_GLOBUS_SEARCH_DISABLED"]
    end

    EV1 & EV2 & EV3 & EV4 & EV5 --> LLM["build_chat_llm()<br/>OpenAI-compatible client"]
    EV6 --> DBS["DatabaseStore<br/>(VIOLIN + BV-BRC)"]
    EV7 --> DICT["DictionaryIndex<br/>singleton"]
    EV8 --> CP["ControlPlaneClient"]
    EV9 --> WS["resolve_workspace_root()"]
    EV10 --> Tests["test gating"]
    EV11 & EV12 --> GS["globus_search.search()"]

    LLM --> SYN["RagSynthesisStep, EntityExtractionStep, Composer"]
    DBS --> DT["query_* MCP tools, VIOLINBVBRCContextStep"]
    DICT --> RT["resolve_canonical_entity, query_pathogens (descendant)"]
    CP --> WT["start_workflow, approvals, HPC tools"]
    WS --> DR["DomainRagIndex, VIOLINBVBRCContextStep defaults"]
    GS --> GST["query_globus_search MCP tool, SynthesisContextAssemblyStep"]
```

---

## 13. Things that will surprise you (brutal-truth section)

1. **`Workflow.process(input_data)` is fire-and-forget. Use
   `wait_for_cascade` to await completion.** Process returns
   `{"status": "data_flow_initiated", ...}` immediately; the
   trigger cascade fires asynchronously. The framework now exposes
   `Workflow.wait_for_cascade(timeout, settle_ms)` — added 2026-05-05
   alongside the runtime test that proved it necessary.

2. **`BaseStep.execute()` takes only kwargs.** `wf.execute(input)`
   raises `TypeError`. The data-driven entry point is `process(input)`.

3. **DirectLink defaults to `auto_transfer=False`.** Without
   explicit `auto_transfer: true` in the link config, the workflow
   YAML loads cleanly but every link is a runtime no-op. The
   composer prompt now mandates this; manually-authored YAMLs must
   set it. This was one of FOUR silent-failure bugs uncovered by
   the trigger-cascade test.

4. **Workflows need both step-level AND workflow-level data units.**
   The framework's integrity validator requires every step input/
   output to have an external source/consumer. Workflow-level
   `input_data_units` / `output_data_units` provide them. Bare
   `data_units:` (singular) is forbidden; the plural variants are
   REQUIRED for any multi-step workflow.

5. **Step trigger inputs are wrapped as `{unit_name: payload}`.**
   `Step._execute_on_trigger` wraps; direct callers pass raw.
   Steps must detect both shapes. Synthesis steps now do.

6. **The FAISS / sentence-transformers import order is load-bearing.**
   `sentence_transformers` MUST import before `faiss` on macOS ARM
   or you get a silent segfault. There's a `# ruff: noqa: I001, E402`
   directive at the top of `domain_rag/index.py` to prevent auto-sort.

7. **`object.__new__(StepClass)` was the prior shortcut for sharing
   logic between steps.** Gone as of 2026-05-05 — replaced by
   stateless utility modules (`_violin_bvbrc_lookup`, `_pubmed_helpers`).

8. **Synthesis branch failures degrade gracefully but the all-empty
   case still raises.** A single corrupt FAISS or PubMed 5xx → empty
   slot in the bundle. ALL FIVE retrieval branches empty → the
   synthesizer's `fail_on_empty_retrieval` gate fires `ValueError`.

9. **Globus Search is read-only at the ingest boundary.** The
   harvester (in `apecx-harvesters`) populates the index as a
   stand-alone offline process. This repo NEVER writes to the
   index. The harvester boundary is enforced by code review, not
   by mechanical guard.

10. **The composer is not on the synthesize_query path.** The
    `synthesize_query` MCP tool drives the rag_e2e_synthesis workflow
    directly via `from_config + process()`. No LLM-driven workflow
    composition. Use `start_workflow` when the operator wants
    composer-planned multi-step workflows.

11. **`extra='forbid'` is mandatory on every step config.** YAML typos
    in step configs would otherwise be silently dropped. The workspace
    rule is enforced; auditors check for this on every PR.

12. **Path resolution honors `APECX_WORKSPACE_ROOT` first.** If the
    env var is set, marker-walk + `parents[5]` fallback are skipped.
    Use this in non-standard checkout layouts (vendored, monorepo,
    container).

13. **The slow path does NOT use the synonym dictionary.** It scans
    the DatabaseStore (VIOLIN + BV-BRC pandas frames) for substring
    matches. Confidence ≈ 0.3. Only used as last resort.

14. **`_workspace_notes/` is a separate sibling repo.** Friction logs
    and dev-history docs live there, NOT in this repo. Workspace
    rules are documented in `../CLAUDE.md` (also outside this repo).

---

## 14. Backend vs. user-facing — two pipelines, one bridge

Two distinct lifecycles converge on the same data. Calling them out
separately is the only way to keep the operator's mental model
straight: **one runs periodically and offline, the other runs every
time a scientist asks a question.**

### 14.1 Backend harmonization (offline, periodic)

See `docs/figures/10_backend_harmonization.png`.

Two parallel sub-pipelines refresh the artifacts that the user-facing
workflow consumes:

1. **Harvester pipeline** (`apecx-harvesters` repo, OUT OF SCOPE
   here): 9 source loaders (PubMed, PDB, DataCite, Crossref, OpenAlex,
   bioRxiv, EMDB, DOI, …) → unified DataCite-shaped record →
   `scripts/aggregate_gsearch.py` → batch ingestion to the **APECx
   Globus Search index** (UUID `e74bf12a-d0dd-4d19-a965-03f4936db851`,
   public).
2. **Dictionary builder** (`apecx-mcp-integration`): the internal
   `dictionary_build_workflow` (nanobrain workflow at
   `synonym_dictionary/workflow/configs/dictionary_build_workflow.yml`)
   wraps `taxdump_fetcher.fetch_taxdump()` and
   `synonym_dictionary/build.py`. Triggered lazily at apecx-mcp startup
   via `bootstrap.ensure_dictionary` (long-term: triggered by an
   apecx-harvesters sink after a harvest run completes — see migration
   note at the top of `bootstrap.py`). Harvests entity names from VIOLIN
   + BV-BRC source rows,
   resolves them via the **EBI Ontology Lookup Service**, and writes
   `apecx_synonym_dict.sqlite` plus the `taxon_hierarchy` table built
   from the NCBI taxdump.

Both are synchronous one-shot processes — never on the per-query hot
path. Run cadence: when source data is refreshed (monthly cycle).

**Resolution status taxonomy** (per dictionary entry; written at
build time, surfaced at query time as `confidence`):

| Status | Confidence | Meaning |
|---|---|---|
| `id_anchored` | 1.0 | source row carried authoritative ID; OLS provided synonyms |
| `ols_exact` | 0.9 | OLS exact-match search hit (label or synonym) |
| `ols_fuzzy` | <0.9 | OLS multi-match disambiguated by row context |
| `project_local` | varies | private IRI in `apecx_local` namespace |
| `unresolved` | 0.0 | no mapping; row stays with `canonical_iri = None` (surfaced explicitly) |

### 14.2 User-facing workflow (online, per-query)

See `docs/figures/11_user_facing_workflow.png`.

Three vertical bands:

1. **MCP entry** — scientist query → MCP client → FastMCP server →
   one of 23 tools (this figure expands `synthesize_query`).
2. **`synthesize_query` expanded** — `SynthesisContextAssemblyStep`
   fans out to four retrieval branches via
   `asyncio.gather(return_exceptions=True)`:

   | Branch | Source | Latency |
   |---|---|---|
   | FAISS RAG search | in-memory `domain_rag/faiss_index.bin` | ~5 ms |
   | VIOLIN/BV-BRC pandas | offline CSV/TSV | ~50 ms |
   | PubMed eSearch + eFetch | network | 1–3 s |
   | Globus Search | network (harvester index) | ~500 ms |

   Bundle is fed to `RagSynthesisStep` → 1 LLM round-trip (~30–60 s
   on Ollama) → `synthesis_output: {synthesis: <markdown>}`.

3. **Artifacts consumed** (read-only): FAISS index (built by
   `scripts/build_domain_rag_index.py`), VIOLIN+BV-BRC files,
   Globus Search index (built by harvester), synonym dictionary
   (built by the internal `dictionary_build_workflow` at apecx-mcp
   startup).

Total wall-clock budget: ~5–10 s retrieval (mostly PubMed) +
~30–60 s LLM = **~70 s end-to-end** on local Ollama.

### 14.3 Bridge between the two

Every artifact the backend writes, the user-facing workflow reads —
and **never** writes to. This is the load-bearing rule that lets the
backend pipeline be batch / offline / asynchronous without coupling
to per-query latency:

| Artifact | Written by (backend) | Read by (user-facing) |
|---|---|---|
| Globus Search index | harvester `aggregate_gsearch.py` | `query_globus_search` MCP tool, synthesis Globus branch |
| `synonym_dict.sqlite` | `dictionary_build_workflow` (lazy at apecx-mcp startup) | `resolve_canonical_entity`, fast path of every database tool |
| FAISS index | `scripts/build_domain_rag_index.py` | synthesis FAISS branch |
| VIOLIN/BV-BRC files | `apecx-setup` (download) | synthesis pandas branch + database tools |

---

## 15. References

| Topic | File |
|---|---|
| Workspace policy | `../CLAUDE.md` (workspace root, not in this repo) |
| Repo policy | `CLAUDE.md` (this repo) |
| MCP setup for Claude Desktop | `docs/mcp_integration.md` |
| MCP surface reference | `docs/mcp_surface.md` |
| Operator quickstart | `docs/QUICKSTART.md` |
| Tutorial (4 chapters) | `docs/tutorial/` |
| VIOLIN × BV-BRC workflow | `docs/violin_bvbrc_workflow.md` |
| Composer task spec | `../_workspace_notes/.../composer_task_spec.md` |
| Synonym dictionary contract | `../_workspace_notes/.../synonym_dictionary_contract.md` |
| Friction log (recurring time-sinks) | `../_workspace_notes/.../session_friction_log.md` |
