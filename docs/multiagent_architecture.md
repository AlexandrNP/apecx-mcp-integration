# APECx Tiered Multi-Agent Architecture — Design Document

**Status:** Design / pre-implementation
**Supersedes:** This document extends `docs/architecture.md` (current-state map). That doc
remains authoritative for what exists today. This doc defines the target state.

---

## 1. Problem statement

The current MCP server is a flat collection of 23 tools with three
overlapping synthesis paths, entity resolution duplicated across three
modules, and no routing logic. Claude (the MCP client) must reason about
which path to take for every query. The result is unpredictable latency,
inconsistent evidence quality, and no clear upgrade path as the database
footprint grows from 2 sources (DomainDB, Genomics DB) to 8+ (InteractionDB, PDB, EMDB,
BioactivityDB, ProtaBank, …).

**Goal:** Replace the flat tool surface with a tiered multi-agent system
where MCP is purely a front door. The system decomposes scientist questions
into sub-tasks, fans out to specialized retrieval and tool-execution agents,
accumulates structured evidence, and produces grounded, cited answers —
without requiring the MCP client to orchestrate any of it.

---

## 2. Representative use cases (design drivers)

These use cases come directly from the structured biology reasoning demo
(`docs/Demo Scenario for Data Repository`). They represent the complexity
ceiling the architecture must handle.

### 2.1 Iterative multi-database entity reasoning

> "Identify conserved, structurally accessible binding sites on
> target biomarker with experimental validation across variants."

Required reasoning steps (in dependency order):

1. Resolve "target biomarker" across Genomics DB (multi-variant sequences),
   PDB (3D structures), and InteractionDB (validated binding interactions).
2. Compute conservation scores by aligning sequences from Genomics DB across variants.
3. Map InteractionDB binding site positions onto PDB 3D coordinates to assess surface
   accessibility.
4. Filter by binding / inhibition data from BioactivityDB and ProtaBank
   (binding kinetics).
5. Cross-validate with PubMed literature for clinical evidence.
6. Return: binding site evidence table with cross-database evidence, confidence scores,
   and inline citations.

No single tool can execute this. It requires a coordinating agent that owns
the decomposition and an evidence accumulator that merges partial results.

### 2.2 Peptide design with multi-platform optimization

> "Design a consensus peptide that maintains molecular binding while
> optimizing for formulation, delivery, and production platforms."

Adds tool execution on top of retrieval:

- Sequence alignment tools (MUSCLE, ClustalW) via an external executor.
- Structural prediction tools (AlphaFold, Rosetta) for binding energy.
- Platform feasibility checkers.

This motivates the Rhea integration: rather than hard-coding these tools,
the system should discover and execute them through a standardized interface.

### 2.3 Multi-target construct design

> "Combine multiple binding targets into a single construct that prevents
> competitive interference while achieving broad inhibitory coverage."

Adds hypothesis generation and ranking:

- Multiple competing construction strategies must be evaluated.
- Evidence from structural + functional + literature agents feeds a
  tournament-style ranking step (inspired by StructBioReasoner's
  multi-agent pattern).
- HITL gate before expensive HPC simulation.

---

## 3. Architecture overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Tier 0 — MCP Frontend                                                  │
│  FastMCP server · intent classifier · HITL surface                      │
│  ≤6 user-visible tools (ask, approve, status, discover, tool_exec, …)   │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  structured request + context
┌──────────────────────────────▼──────────────────────────────────────────┐
│  Tier 1 — Orchestrator Agents                                           │
│  Domain-specific agents that decompose requests into sub-task graphs    │
│  BioTargetOrchestrator · StructuralOrchestrator · GenomicsOrchestrator    │
│  GenericQueryOrchestrator (fallback)                                    │
└──────┬───────────────┬──────────────────┬───────────────────────────────┘
       │               │                  │
┌──────▼──────┐  ┌─────▼──────┐  ┌───────▼──────────────────────────────┐
│  Tier 2A    │  │  Tier 2B   │  │  Tier 2C                              │
│  Retrieval  │  │  Tool Exec │  │  Reasoning / Synthesis                │
│  Agents     │  │  Agents    │  │  Agents                               │
└──────┬──────┘  └─────┬──────┘  └───────┬──────────────────────────────┘
       │               │                  │
┌──────▼───────────────▼──────────────────▼──────────────────────────────┐
│  Tier 3 — Resource Layer                                                │
│  Data Access Interface (Spherical→Globus adapter)                      │
│  Rhea tool executor · FAISS domain RAG · DomainDB/Genomics DB · HPC bundles   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Tier 0 — MCP Frontend

### 4.1 Responsibility

The MCP surface is a thin router. It does NOT perform retrieval,
synthesis, or reasoning. It:

1. Accepts natural-language requests from the MCP client (Claude Desktop,
   IDE, API).
2. Classifies intent and hands off to the correct Tier-1 orchestrator.
3. Surfaces HITL gates (approve / reject / correct) for long-running or
   expensive operations.
4. Returns structured status updates and final answers.

### 4.2 Consolidated tool surface (target)

| Tool | Purpose |
|---|---|
| `ask` | Submit a scientist question; returns a task ID |
| `status` | Poll or stream progress of a running task |
| `approve` / `reject` / `correct` | HITL gates (same semantics as today) |
| `discover` | List available orchestrators + capabilities |
| `execute_tool` | Request a specific external tool by name (Rhea dispatch) |

The current 23 tools collapse into this surface. The model no longer
needs to choose synthesis path — the intent classifier does it.

### 4.3 Intent classifier

A lightweight LLM call (or fine-tuned classifier) that maps a free-text
query to one of:

| Intent | Routed to |
|---|---|
| Binding site / structural biology question | `BioTargetOrchestrator` |
| Structural / 3D question | `StructuralOrchestrator` |
| Genomic / sequence question | `GenomicsOrchestrator` |
| Single-database lookup | `GenericQueryOrchestrator` |
| Tool execution request | `ToolExecutionOrchestrator` |

The classifier uses the same LLM backend as synthesis (APECX_LLM_*).
For well-structured queries the overhead is one short prompt; for
ambiguous queries it can ask the user for clarification before routing.

---

## 5. Tier 1 — Orchestrator Agents

### 5.1 Design contract

Each orchestrator is a nanobrain `Agent` (or `Workflow` + `Step` set)
that:

- Receives a structured intent + raw query.
- Decomposes it into a sub-task graph (which retrieval agents, which
  tools, what order, what evidence is needed).
- Dispatches to Tier-2 agents in parallel where possible.
- Collects and validates results.
- Hands a structured evidence bundle to the Tier-2C synthesis agent.
- Returns a final grounded answer to Tier 0.

### 5.2 BioTargetOrchestrator (primary for demo use cases)

Implements the reasoning loop from section 2.1:

```
BioTargetOrchestrator.process(query)
  ├── parallel fan-out:
  │   ├── InteractionDBRetrievalAgent(binding site query)
  │   ├── PDBRetrievalAgent(structural query)
  │   ├── GenomicsDBRetrievalAgent(sequence/variant query)
  │   └── BioactivityDBRetrievalAgent(inhibitory concentration / effective concentration query)
  ├── cross-link results (entity resolution)
  ├── optional: StructuralToolAgent(alignment / accessibility calc)
  ├── EvidenceAccumulationStep (merge, deduplicate, score)
  └── SynthesisAgent(evidence bundle) → grounded Markdown
```

For use case 2.3 (multi-target design), the orchestrator adds a
`HypothesisTournamentStep` (StructBioReasoner pattern): multiple
competing construction strategies proposed by specialized sub-agents,
ranked by accumulated evidence scores, with the top-N surfaced to
the HITL gate before HPC submission.

### 5.3 GenericQueryOrchestrator (today's direct tools, preserved)

Single-database lookups that need no decomposition fall through to
this orchestrator, which delegates directly to the appropriate
retrieval agent. This is the migration path for the current 23-tool
surface — existing behavior is preserved, complexity is encapsulated.

---

## 6. Tier 2A — Retrieval Agents

### 6.1 Database abstraction (Globus-first)

All retrieval agents talk to a `DataAccessInterface` (abstract base).
Two concrete implementations:

| Implementation | When active | Config |
|---|---|---|
| `SphericalAdapter` | Now (temporary) | `APECX_DB_BACKEND=spherical` |
| `GlobusAdapter` | Target state | `APECX_DB_BACKEND=globus` |

The adapter swap is a config change, not a code change. No retrieval
agent knows about Spherical or Globus directly.

**Spherical API query surface (captured for adapter implementation):**

Each of the 8 Spherical databases exposes two endpoint types:
- `POST /{DB}/elastic_search` with `{"search_texts": "string"}` →
  free-text search
- `POST /{DB}/multi_input_query` with structured filter objects →
  field-level queries

Response shape: `{isFunctionName: bool, TotalNumberOfEntries: int,
{DB}Basic: [{field: value}]}`. The Globus adapter will expose the
same logical interface once the migration completes.

### 6.2 Retrieval agent inventory

| Agent | Database(s) | Primary query types |
|---|---|---|
| `InteractionDBRetrievalAgent` | InteractionDB | Binding site position, modification, interaction data |
| `PDBRetrievalAgent` | PDB | Crystal structure, resolution, atomic coords |
| `EMDBRetrievalAgent` | EMDB | Cryo-EM reconstructions, fitted PDB refs |
| `BioactivityDBRetrievalAgent` | BioactivityDB | inhibitory concentration / effective concentration / CC50, drug-protein pairs |
| `ProtaBankRetrievalAgent` | ProtaBank | Antibody-target affinity, kinetics |
| `GenomicsDBRetrievalAgent` | Genomics DB | Multi-variant genomes, phylogeny, geographic meta |
| `DomainDBRetrievalAgent` | DomainDB | Vaccine-gene-pathogen relationships |
| `PubMedRetrievalAgent` | PubMed (NCBI eUtils) | Literature, abstracts, citations |
| `GlobusSearchRetrievalAgent` | APECx Globus index | Harvested cross-source records |
| `FAISSRetrievalAgent` | Domain RAG FAISS | Semantic search over indexed domain knowledge |

All agents produce a typed evidence dict with: `source_db`, `records`,
`query`, `confidence`, `retrieved_at`.

### 6.3 Entity resolution (unified)

A single `CanonicalEntityResolver` service replaces the three current
implementations (`resolve_entity`, `resolve_canonical_entity`, and the
implicit entity extraction in the composer). It exposes:

```python
resolver.resolve(surface_form, entity_type) → ResolvedEntity
```

with the existing stage-2 lookup strategy (fast dict → ancestor walk →
slow substring). Every retrieval agent uses this; no agent normalizes
entity names itself.

---

## 7. Tier 2B — Tool Execution Agents

### 7.1 Rhea integration

Rhea provides RAG-based discovery + Parsl-based isolated execution of
biomedical tools (sequence alignment, structural prediction, etc.).
The integration surface:

```
ToolExecutionOrchestrator.execute(tool_name_or_description, inputs)
  └── RheaToolAgent
       ├── query Rhea's embedding-indexed tool catalog
       ├── retrieve best-matching tool + deps
       ├── execute in isolated Parsl Academy agent
       │   (deps installed, I/O via ProxyStore/Redis)
       └── return structured result + provenance
```

**Integration point:** Rhea exposes an MCP-compatible transport
(Claude Desktop + HTTP streaming). The `RheaToolAgent` connects via
HTTP/SSE to a running Rhea service. The tool catalog is Rhea's
embedded biomedical tool collection; APECx-specific tools can be
registered by adding them to Rhea's tool repository.

**Configuration:**

```
APECX_RHEA_URL      — URL of running Rhea service (default: disabled)
APECX_RHEA_INDEX    — embedding index for tool discovery (Rhea-managed)
```

When `APECX_RHEA_URL` is unset, the `ToolExecutionOrchestrator`
falls back to the existing `export_hpc_bundle` / Parsl paths.

### 7.2 GalaxyMCP integration (conditional)

If Galaxy provides a local MCP server, `GalaxyToolAgent` wraps it with
the same interface as `RheaToolAgent`. The orchestrator can route to
either without knowing which backend is active. Deferred until Galaxy
MCP availability is confirmed.

### 7.3 HPC execution (existing path, preserved)

`export_hpc_bundle` / `ingest_hpc_bundle` remain as the offline HPC
path. The HITL gate before HPC submission (use case 2.3) is now
surfaced explicitly:

```
HypothesisTournamentStep → top-N ranked hypotheses
  → HITL gate (approve/reject/modify)
  → approved hypothesis → HPC bundle export → qsub (manual)
  → ingest_hpc_bundle (re-ingest provenance)
```

---

## 8. Tier 2C — Reasoning and Synthesis Agents

### 8.1 EvidenceAccumulationStep

Collects outputs from all active retrieval agents and merges them
into a single structured evidence bundle (extending the current
`SynthesisContextAssemblyStep` bundle shape):

```python
{
    "query":           str,
    "intent":          str,                  # classified intent
    "entities":        list[ResolvedEntity], # canonical entity set
    "rag_chunks":      list[dict],           # FAISS hits
    "interaction_db_data": list[dict],        # NEW
    "pdb_structures":  list[dict],           # NEW
    "emdb_maps":       list[dict],           # NEW
    "bioactivity_data": list[dict],          # NEW
    "protabank_data":  list[dict],           # NEW
    "genomics_db_results": list[dict],
    "domain_db_mappings": list[dict],
    "publications":    list[dict],
    "globus_results":  list[dict],
    "tool_outputs":    list[dict],           # Rhea/Galaxy results
    "confidence":      dict[str, float],     # per-source confidence
}
```

Branch failure contract is inherited from the current assembly step:
each branch degrades to `[]` on error, with a WARNING log. The
`fail_on_empty_retrieval` gate fires only when ALL branches are empty.

### 8.2 HypothesisTournamentStep (StructBioReasoner pattern)

For design-type queries (use cases 2.2, 2.3), after evidence accumulation:

1. Multiple specialized agents propose competing strategies
   (structural agent, evolutionary agent, energetic agent, design agent).
2. Each proposal includes supporting evidence and a confidence score.
3. Proposals are ranked by a scoring function (configurable per orchestrator).
4. Top-N proposals are surfaced to the HITL gate or directly to synthesis.

This is a design consideration, not a StructBioReasoner dependency.
The tournament step is generic; its specialized proposers are
pluggable nanobrain steps.

### 8.3 SynthesisAgent (existing, extended)

`RagSynthesisStep` is preserved as-is for the synthesis step.
Its prompt template is extended to handle the richer evidence bundle
(interaction database records, PDB structures, etc.). The grounding and citation
gates already enforce evidence-backed output.

---

## 9. Data access layer — Spherical → Globus migration

### 9.1 Migration path

```
Phase 0 (now):     SphericalAdapter   — POST to Spherical REST API
Phase 1 (near):    GlobusAdapter      — Globus Search + Transfer
Phase 2 (target):  GlobusAdapter only — SphericalAdapter removed
```

The `DataAccessInterface` abstraction ensures that the swap from
Phase 0 to Phase 1 is:
- A new `GlobusAdapter` class (≤200 lines).
- A config change (`APECX_DB_BACKEND=globus`).
- Zero changes to any retrieval agent.

### 9.2 Spherical API shape (for adapter implementation)

Eight databases, two endpoint patterns each:

```
POST /BioactivityDB/bioactivitydb_basic_elastic_search_for_bioactivitydb_basic
  body: {"search_texts": "target protein"}
  response: {isFunctionName: bool, TotalNumberOfEntries: int,
             BioactivityDBBasic: [{PathogenName, ProteinName, DrugName, ...}]}

POST /BioactivityDB/bioactivitydb_basic_query_by_multiple_inputs_for_bioactivitydb_basic
  body: {"pathogen_name": "...", "protein_name": "...", "drug_type": "..."}
  response: same shape
```

Full field mapping per database is documented in `docs/Spherical.pdf`
(kept for reference; not reproduced here to avoid staleness).

**Important:** Do not implement auth or harvesting features from
Spherical — only the query interface is relevant, and only until
Globus is available.

---

## 10. What exists today vs. what needs to be built

### 10.1 Existing components (reuse as-is or with minor extension)

| Component | Status | Notes |
|---|---|---|
| `SynthesisContextAssemblyStep` | Existing | Extend bundle shape for new DBs |
| `RagSynthesisStep` | Existing | Extend prompt template |
| `CanonicalEntityResolver` (stage 2) | Existing | Expose as shared service |
| HITL approval tools | Existing | Surface at Tier 0 gate |
| HPC bundle export/ingest | Existing | Wire into HypothesisTournamentStep |
| `FAISSRetrievalAgent` | Existing (DomainRagIndex) | Thin agent wrapper |
| `DomainDBRetrievalAgent` | Existing (pandas tools) | Thin agent wrapper |
| `GenomicsDBRetrievalAgent` | Existing (pandas tools) | Thin agent wrapper |
| `PubMedRetrievalAgent` | Existing (PubMedHarvesterStep) | Thin agent wrapper |
| `GlobusSearchRetrievalAgent` | Existing (query_globus_search) | Thin agent wrapper |

### 10.2 New components (build)

| Component | Priority | Depends on |
|---|---|---|
| `DataAccessInterface` + `SphericalAdapter` | P0 | Spherical API access |
| `GlobusAdapter` | P0 | Globus SDK |
| `InteractionDBRetrievalAgent` | P0 | DataAccessInterface |
| `PDBRetrievalAgent` | P0 | DataAccessInterface |
| `EMDBRetrievalAgent` | P1 | DataAccessInterface |
| `BioactivityDBRetrievalAgent` | P0 | DataAccessInterface |
| `ProtaBankRetrievalAgent` | P1 | DataAccessInterface |
| `IntentClassifier` | P0 | APECX_LLM_* |
| `BioTargetOrchestrator` | P0 | All Tier-2A agents |
| `StructuralOrchestrator` | P1 | PDB, EMDB agents |
| `GenericQueryOrchestrator` | P0 | Entity resolver + any one agent |
| `EvidenceAccumulationStep` | P0 | Extends assembly step bundle |
| `HypothesisTournamentStep` | P2 | Evidence accumulation |
| `RheaToolAgent` | P1 | Running Rhea service |
| Tier-0 `ask` / `status` tools | P0 | Intent classifier + orchestrators |
| Unified `CanonicalEntityResolver` | P0 | Replaces 3 current impls |

### 10.3 Components to remove / consolidate

| Today | After |
|---|---|
| 3 entity resolution implementations | 1 `CanonicalEntityResolver` |
| 3 synthesis invocation paths (A/B/C) | Path A only (canonical); Path B preserved for workflow runtime; Path C test-only |
| 23 MCP tools | ≤6 Tier-0 tools; remainder become internal agent methods |

---

## 11. Key design decisions and rationale

**1. Globus-first data layer.**
The Spherical REST API is explicitly temporary per the user. Isolating
it behind a `DataAccessInterface` now costs ~200 lines and saves a
future codebase-wide replacement. Every new retrieval agent is written
against the interface, not Spherical.

**2. Rhea for external tool execution.**
Rhea provides what we would otherwise have to build: RAG-based tool
discovery, isolated execution environments (Parsl Academy), and
standardized I/O via ProxyStore. The alternative is hard-coding tool
wrappers one by one.

**3. StructBioReasoner-inspired tournament, not StructBioReasoner itself.**
The tournament pattern (competing specialized agents, evidence-scored
ranking, top-N to HITL) is the transferable design insight.
StructBioReasoner's actual codebase (GPT-4, PyMOL wrappers, ProtoGnosis)
is not a dependency.

**4. Intent classification at Tier 0, not in each agent.**
Routing logic at the top prevents each orchestrator from implementing
its own "am I the right agent?" guard. One classifier, one routing
decision, logged for observability.

**5. Nanobrain framework throughout.**
All steps and workflows continue to use nanobrain's `from_config` +
`process()` contract. The `DataAccessInterface` is a plain Python ABC,
not a nanobrain component — it has no trigger or data-unit lifecycle.

---

## 12. Open questions (blocking decisions)

1. **Rhea deployment model.** Does Rhea run as a sidecar alongside
   `apecx-mcp`, or as a separate long-lived service? Affects
   `APECX_RHEA_URL` and startup sequencing.

2. **Galaxy MCP availability.** The user mentioned "if they have a
   local tool usage." Confirm before implementing `GalaxyToolAgent`.

3. **Evidence scoring function.** The `HypothesisTournamentStep`
   needs a scoring contract. Use confidence × coverage, or LLM-judged
   ranking? Depends on target use case latency budget.

4. **Globus migration timeline.** Until Globus is available, all
   new retrieval agents must be tested against the Spherical API.
   When does the Globus endpoint become testable?

5. **Orchestrator granularity.** `BioTargetOrchestrator` handles the
   pathogen X demo. Should there be a `VaccineOrchestrator` at the same
   tier, or is epitope discovery a sub-task of vaccine design? The
   answer determines how many P0 orchestrators to build.

---

## 13. References

| Resource | Location |
|---|---|
| Current-state architecture | `docs/architecture.md` |
| Spherical API spec | `docs/Spherical.pdf` |
| Demo use cases (pathogen X) | `docs/Demo Scenario for Data Repository - Google Docs.pdf` |
| Rhea tool execution | https://github.com/chrisagrams/rhea/tree/main |
| StructBioReasoner design patterns | https://github.com/IDeA-ANL-ORNL/StructBioReasoner |
| Nanobrain step authoring | `.claude/skills/nanobrain-step-authoring/SKILL.md` |
| Nanobrain agents/tools | `.claude/skills/nanobrain-agents-tools/SKILL.md` |
| Workspace policy | `../CLAUDE.md` |
