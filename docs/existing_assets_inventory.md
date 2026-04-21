# Existing-Asset Inventory (T00.5) — Source-Read Pass

**Date:** 2026-04-21
**Status:** **SOURCE-READ COMPLETE** for 34 assets across 4 directories. Three Explore subagents ran in parallel on 2026-04-21; results merged below.
**Purpose:** Before T02 writes a single new component, catalog what exists and classify each asset as `reuse`, `wrap`, `refactor`, `skip`, or `new`.

---

## Executive summary

| Directory | Files read | `reuse` | `wrap` | `refactor` | `skip` |
|---|---|---|---|---|---|
| `nanobrain/.../workflows/viral_protein_analysis/steps/` | 13 | 7 | 4 | 1 | 1 |
| `nanobrain/.../agents/specialized/` (excluding `viral_protein_analysis/`) | 8 | 4 | 2 | 0 | 2 |
| `nanobrain/.../workflows/chatbot_viral_integration/steps/` | 7 | 5 | 0 | 1 | 0* |
| `nanobrain/.../workflows/rag/` | 8 (6 steps + 2 agents) | 7 | 1 | 0 | 0 |
| **Total** | **36** | **23** | **7** | **2** | **3** |

*genome_search_step is a dual disposition (`reuse` + `wrap`) counted once as `reuse`.

**Bottom line:** 23 of 36 assets (~64%) are directly reusable. 7 need light wrapping (snapshot path injection, API modernization). Only 2 need real refactoring and 3 are off-scope. **T02 "write new components" reduces to (a) BV-BRC snapshot loader, (b) VIOLIN CSV readers, (c) light adapters — estimated ~5–7 code-days, down from the original 30d.**

---

## 1. `nanobrain/.../workflows/viral_protein_analysis/steps/`

### alignment_step.py — `reuse`
- **Class:** `AlignmentStep`
- **Purpose:** MSA on clustered protein sequences via MUSCLE.
- **I/O:** inputs `clusters` → outputs `aligned_clusters`, `alignment_quality_stats`.
- **Executor coupling:** none. MUSCLE tool via workflow-local config with fallback placeholder.
- **Snapshot:** not BV-BRC specific.
- **Why reuse:** executor-agnostic, laptop-compatible.

### annotation_mapping_step.py — `wrap`
- **Class:** `AnnotationMappingStep`
- **Purpose:** Cache-based synonym resolution with LLM fallback.
- **I/O:** `cache_directory`, `species_name`, `annotated_fasta`, `protein_annotations` → `standardized_annotations`, `canonical_fasta_path`.
- **Executor coupling:** **WorkQueue — `self.executor.submit(self._execute_task, input_data)` at line 344–347.** This is the hidden Aurora coupling.
- **Snapshot:** reads TSV/JSON from cache_directory; local-file aware already.
- **Why wrap:** WorkQueue binding must be abstracted (go through Link/Trigger, not direct `submit`). LLM agent wrapping needs modern input/output dict contract.

### bv_brc_data_acquisition_step.py — `wrap`
- **Class:** `BVBRCDataAcquisitionStep`
- **Purpose:** Download Alphavirus genomes (8–15 KB), filter, extract proteins, build annotated FASTA.
- **I/O:** target species → `filtered_genomes`, `unique_proteins`, `protein_sequences`, `annotated_fasta`.
- **Executor coupling:** none.
- **Snapshot:** **No hardcoded snapshot path — depends on how `BVBRCTool` is configured.** Needs injection of snapshot-aware tool.
- **Why wrap:** needs a `BVBRCSnapshotTool` that reads `data/bvbrc_cache/*.tsv` and `*.fasta` instead of calling the BV-BRC API.

### clustering_step.py — `reuse`
- **Class:** `ClusteringStep`
- **Purpose:** Cluster proteins by product name (default) or MMseqs2 (optional).
- **I/O:** `curated_sequences` → `protein_clusters`, `clustering_analysis`.
- **Executor coupling:** none on default path. MMseqs2 lazily loaded only if selected.
- **Why reuse:** product-based default is executor-independent and works immediately.

### data_acquisition_step.py — `refactor`
- **Class:** `DataAcquisitionStep`
- **Purpose:** Generic data acquisition with dedup by MD5.
- **Executor coupling:** none.
- **Snapshot:** inherits from `BVBRCTool` config (same as bv_brc_data_acquisition_step).
- **Why refactor:** **near-duplicate of `bv_brc_data_acquisition_step.py`.** Consolidation required before integration; otherwise we maintain two parallel acquisition paths.

### data_aggregation_step.py — `reuse`
- **Class:** `DataAggregationStep`
- **Purpose:** Collect and standardize output from all workflow steps.
- **I/O:** results dict (keyed by step_id) → `aggregated_data`, `data_summary`.
- **Why reuse:** generic; no coupling.

### elasticsearch_indexing_step.py — `skip`
- **Class:** `ElasticsearchIndexingStep`
- **Why skip:** Elasticsearch is out of scope for the local laptop MVP. Revisit if VIOLIN × BV-BRC extends to a search UI.

### enhanced_bv_brc_data_acquisition_step.py — `wrap`
- **Class:** `EnhancedBVBRCDataAcquisitionStep`
- **Purpose:** Multi-stage CSV-based validation pipeline with taxonomic verification agent; ZERO-tolerance contamination prevention.
- **I/O:** `ultra_high_confidence_synonyms`, `species_validation_criteria` → `verified_entries`, `validation_summary`, `validation_audit_trail`.
- **Snapshot:** **`_load_csv_data()` at line 1402 returns empty DataFrame — placeholder.** Needs real loader pointing to `data/bvbrc_cache/`.
- **Why wrap:** the validation infrastructure is sound; the CSV source is a placeholder. Wire `data/bvbrc_cache/*.tsv` as the real source.

### protein_synonym_agent_step.py — `wrap`
- **Class:** `ProteinSynonymAgentStep`
- **Purpose:** Wrap `ProteinSynonymAgent` via Link-based communication.
- **I/O:** DataUnit with `stage: synonym_request` → DataUnit with `synonym_groups`, `protein_classifications`.
- **Why wrap:** uses outdated `DataUnit.get()/set()` API; modernize to dict I/O. Agent wrapping pattern is otherwise correct.

### pssm_analysis_step.py — `reuse`
- **Class:** `PSSMAnalysisStep`
- **Purpose:** Generate PSSM matrices from aligned clusters; produce `viral_pssm.json`.
- **I/O:** `aligned_clusters` (or `protein_sequences`) → `pssm_matrices`, `viral_pssm_json`.
- **Why reuse:** placeholder PSSM generation works; real algorithm pluggable.

### result_collection_step.py — `reuse`
- **Class:** `ResultCollectionStep`
- **Purpose:** Organize output files (FASTA, JSON, manifest) into timestamped directory.
- **Why reuse:** generic I/O, no coupling.

### sequence_curation_step.py — `reuse`
- **Class:** `SequenceCurationStep`
- **Purpose:** QC of protein sequences (stop codons, ambiguous AA, length outliers), generate quality scores.
- **I/O:** `mapped_proteins`, `standardized_annotations` → `curated_sequences`, `curation_report`.
- **Why reuse:** pure statistical logic; no coupling.

### viral_pssm_generation_step.py — `reuse`
- **Class:** `ViralPSSMGenerationStep`
- **Purpose:** Produce final `viral_pssm.json` with metadata, genome stats, analysis results.
- **Why reuse:** post-processing; no coupling.

---

## 2. `nanobrain/.../agents/specialized/` (excluding the `viral_protein_analysis/` subdirectory which was covered in §1)

### base.py — `wrap`
- **Classes:** `SpecializedAgentBase`, `SimpleSpecializedAgent`, `ConversationalSpecializedAgent`
- **Purpose:** Mixin + base classes for specialized agents.
- **LLM:** OpenAI in examples; framework-agnostic via `SimpleAgent`/`ConversationalAgent` parents.
- **Prompts:** YAML-driven. Compliant.
- **Why wrap:** good infrastructure, but inherits heavy conversational overhead that VIOLIN × BV-BRC may not need. Lightweight wrapper pattern.

### code_writer.py — `skip` + **prompt-violation flag**
- **Classes:** `CodeWriterAgent`, `ConversationalCodeWriterAgent`
- **Prompts:** **HARDCODED** (~23 lines in `CodeWriterAgent.__init__`, ~32 lines in `ConversationalCodeWriterAgent.__init__`). **Violates the framework's "no hardcoded prompts" rule.**
- **Why skip:** code generation is peripheral to VIOLIN × BV-BRC, and the prompt violation makes it a cleanup liability we shouldn't inherit.

### file_writer.py — `wrap` + **prompt-violation flag**
- **Classes:** `FileWriterAgent`, `ConversationalFileWriterAgent`
- **Prompts:** **HARDCODED** (~22 lines + ~9 lines). **Violates the framework's "no hardcoded prompts" rule.**
- **Why wrap:** file I/O is generically useful, but the hardcoded prompts must first be migrated to YAML before reuse.

### parsl_agent.py — `skip`
- **Class:** `ParslAgent`
- **Purpose:** Distributed processing via Parsl (HPC).
- **Why skip:** HPC-specific; not applicable to local-default execution. May revisit if HPC-export lane is built later.

### protein_synonym_agent.py — `reuse`
- **Class:** `ProteinSynonymAgent`
- **Purpose:** Protein product synonym resolution using LLM + ICTV standards.
- **Prompts:** YAML (`config/protein_synonym_prompts.yml`). Compliant.
- **Maturity:** production-grade ICTV caching (30-day TTL, batch-200 processing, confidence scoring).
- **Why reuse:** directly applicable to BV-BRC viral protein annotations.

### query_analysis_agent.py — `reuse`
- **Class:** `QueryAnalysisAgent`
- **Purpose:** Analyze biological research queries; classify intent; extract species/genes/proteins.
- **Prompts:** YAML. Compliant.
- **Why reuse:** essential for user query parsing in the MCP entry flow.

### viral_expert_agent.py — `reuse`
- **Class:** `ViralExpertConversationalAgent`
- **Purpose:** Expert conversational responses on viral biology, vaccines, alphaviruses.
- **Prompts:** YAML-expected. Compliant.
- **Why reuse:** useful for non-analysis queries; minimal coupling.

### virus_extraction_agent.py — `reuse`
- **Class:** `VirusExtractionAgent`
- **Purpose:** Extract virus species from natural language queries with confidence scoring.
- **Prompts:** YAML (`virus_extraction_agent.yml`). Compliant.
- **Why reuse:** primary entry point for virus identification from user queries.

---

## 3. `nanobrain/.../workflows/chatbot_viral_integration/steps/`

### virus_name_resolution_step.py — `reuse`
- **Class:** `EnhancedVirusNameResolutionStep`
- **Purpose:** Ultra-high-confidence virus synonym detection with multi-agent validation.
- **I/O:** `user_query` or `extracted_virus_species` → `virus_species`, `ultra_high_confidence_synonyms`, `species_validation_criteria`.
- **Why reuse:** agent-driven, no hardcoded lists, confidence filtering prevents contamination.

### genome_search_step.py — `reuse` + (wrap if using MCP-managed ES)
- **Class:** `GenomeSearchStep`
- **Purpose:** ES-backed virus genome search with CSV fallback.
- **Deps:** Elasticsearch + pandas (fallback) + fuzzywuzzy.
- **Why reuse:** **the CSV-fallback path is directly usable for local-default.** The ES path can be wrapped later if/when T03 uses MCP-managed ES.

### annotation_job_step.py — `refactor`
- **Class:** `AnnotationJobStep`
- **Purpose:** Job submission/monitoring for annotation pipeline with 3-level fallback (full workflow → synthetic PSSM stubs → mock).
- **Why refactor:** tightly coupled to `AlphavirusWorkflow`; fallback logic is hardcoded with literature-derived stubs. Needs isolation layer for pluggable analysis backends.

### query_classification_step.py — `reuse`
- **Class:** `QueryClassificationStep`
- **Purpose:** Classify queries and route to analysis vs. conversational branch.
- **Why reuse:** clean separation of concerns; agent-driven.

### elasticsearch_search_step.py — `reuse`
- **Class:** `ElasticsearchSearchStep`
- **Purpose:** Semantic + keyword search via MCP Elasticsearch server.
- **Deps:** sentence-transformers (embeddings) + MCPClient.
- **Why reuse:** **already MCP-integrated.** Hybrid search with RRF. Drop-in for T03 if we choose MCP-managed ES.

### conversational_response_step.py — `reuse`
- **Class:** `ConversationalResponseStep`
- **Purpose:** Educational LLM responses about alphaviruses with literature refs.
- **Why reuse:** literature references are PMID-keyed and solid; HTTP-compatible output.

### response_formatting_step.py — `reuse`
- **Class:** `ResponseFormattingStep`
- **Purpose:** Format diverse response types into markdown-friendly presentation.
- **Why reuse:** generic formatting; supports PSSM/clustering output.

---

## 4. `nanobrain/.../workflows/rag/`

### steps/document_processor_step.py — `reuse`
- **Purpose:** Parse and chunk docs (PDF, TXT, DOCX, HTML, Markdown).
- **Deps:** built-in file I/O + optional PyPDF2/python-docx.
- **Why reuse:** deterministic, parallel-batch capable, incremental with change detection.

### steps/embedding_generator_step.py — `reuse` + wrap
- **Purpose:** Generate embeddings via sentence-transformers.
- **Deps:** `MockEmbeddingClient` in demo; **needs real sentence-transformers deployment** for T03.
- **Why reuse+wrap:** architecture is sound; swap mock for real client.

### steps/vector_storage_step.py — `reuse`
- **Purpose:** Vector DB ops (FAISS/Pinecone/Weaviate/Chroma abstraction).
- **Deps:** FAISS etc. **Note:** currently uses `MockVectorDatabase`; needs real FAISS instance for T03.
- **Why reuse:** multi-backend abstraction is production-pattern.

### steps/semantic_retrieval_step.py — `reuse`
- **Purpose:** Similarity search (dense/sparse/hybrid) with optional cross-encoder reranking.
- **Why reuse:** stateless, hybrid search, reranking with thresholds.

### steps/query_enhancement_step.py — `reuse`
- **Purpose:** `QueryEnhancementAgent` wrapper for LLM query optimization.
- **Why reuse:** clean AgentStep pattern; Jinja2 templates; no hardcoding.

### steps/response_enhancement_step.py — `reuse`
- **Purpose:** `ResponseSynthesisAgent` wrapper for multi-turn LLM synthesis.
- **Why reuse:** production-ready synthesis with theme analysis.

### agents/query_enhancement_agent.py — `reuse`
- **Purpose:** LLM-based query enhancement; synonym expansion.
- **Prompts:** Jinja2 templates. Compliant.
- **Why reuse:** 3-level graceful fallback; zero hardcoded prompts.

### agents/response_synthesis_agent.py — `reuse`
- **Purpose:** Multi-turn synthesis with theme analysis and contradiction resolution.
- **Prompts:** Jinja2 templates. Compliant.
- **Why reuse:** sophisticated synthesis, fully configurable.

---

## 5. VIOLIN — nothing exists

**No nanobrain step currently reads `data/violin/*.csv`.** This is the genuine `new` work:

| File | Schema (to be derived from file) | Nanobrain step needed |
|---|---|---|
| `Gene_Information.csv` | gene records | `ViolinGeneReader` |
| `Gene_Vaccine_Pathogen_Information.csv` | gene↔vaccine↔pathogen join | `ViolinGeneVaccinePathogenJoinReader` |
| `Pathogen_Information.csv` | pathogen records | `ViolinPathogenReader` |
| `Vaccine_Information.csv` | vaccine records | `ViolinVaccineReader` |
| `Vaccine_Pathogen_Information.csv` | vaccine↔pathogen join | `ViolinVaccinePathogenJoinReader` |
| `VIOLIN_Curated_References.txt` | curated citations | `ViolinReferencesReader` |

**Estimated scope:** 6 reader steps, each ~0.5d (schema inspection + `from_config` + unit test + integration test against the real file). Total **~3 code-days** to cover the VIOLIN side.

**Open question:** do we need all 6, or can the workflow be written with 2–3 composite readers? Deferred to T00.1b (workflow spec).

---

## 6. Cross-cutting findings

### 6.1 Prompt-rule violations (code cleanup task)
- `code_writer.py` lines 782–803 and 825–850 — hardcoded prompts.
- `file_writer.py` lines 904–925 and 947–958 — hardcoded prompts.

**Action:** These are in `nanobrain/` and editing them is gated on the "discussed separately" approval. Add to the scope-decision memo (`01_where_new_code_lives.md`) as an additional candidate for batch approval. We don't need these modules for VIOLIN × BV-BRC right now; logging as tech-debt for a future cleanup pass.

### 6.2 Executor-coupling violations
- `annotation_mapping_step.py` line 344–347: `self.executor.submit(self._execute_task, ...)` — direct WorkQueue call.
- `pssm_parsl_executor.yml`: Aurora-specific Parsl + PBS config referenced by `FastaClusterPSSMStep`.

**Action:** these are T02r. The scope-decision memo (`01_where_new_code_lives.md`) recommends addressing these via the separate edit-nanobrain discussion (Option C) or by writing parallel local-path steps in `apecx-mcp-integration/` (Option B's workaround).

### 6.3 BV-BRC acquisition duplication
- `bv_brc_data_acquisition_step.py` and `data_acquisition_step.py` are near-duplicates.
- `enhanced_bv_brc_data_acquisition_step.py` is a third, CSV-first variant with empty placeholder.

**Action:** the duplication is a design smell but not blocking. For T02 we wrap one (likely the `enhanced_` variant) around the real snapshot loader and defer consolidation to a later cleanup.

### 6.4 Chatbot ↔ RAG overlap: virus name normalization
- `virus_name_resolution_step.py` (chatbot) and `query_enhancement_agent.py` (rag) both do LLM-based term normalization.

**Action:** logged as consolidation opportunity; not blocking. The workflow spec (T00.1b) will decide if we consolidate now or later.

### 6.5 RAG stack is complete but mocked
- `MockEmbeddingClient` in `embedding_generator_step.py`
- `MockVectorDatabase` in `vector_storage_step.py`

**Action:** T03 (RAG index over components) must swap these mocks for real `sentence-transformers/all-mpnet-base-v2` + real FAISS. This is part of T03 scope, not T02.

### 6.6 MCP-managed Elasticsearch is already implemented
- `elasticsearch_search_step.py` uses `MCPClient` to talk to an ES MCP server.
- If the laptop MCP server manages an ES instance, we get search capability with no new code.

**Unexpected upside:** this is one of the Round 3 "hidden wins." Defer decision on whether to use it until we know if the workflow spec (T00.1b) needs search.

---

## 7. Impact on T02 scope (revised again)

Round 2 (original AP §5.2): 30 code-days, 15–20 new components.
Round 3 (estimate after AP §R3): 10 code-days, "reuse + wrap + gap-fill."
**Round 3 post-inventory (this doc):** 5–7 code-days, structured as follows:

| Sub-task | Effort | What |
|---|---|---|
| T02.a — BV-BRC snapshot loader | 1–2d | Wrap `enhanced_bv_brc_data_acquisition_step` (or its `BVBRCTool`) to read `data/bvbrc_cache/*.tsv` and `*.fasta` instead of the placeholder. Most of the validation infra is already built. |
| T02.b — VIOLIN readers | 3d | 6 new reader steps (or 2–3 composite readers, TBD by T00.1b). `from_config` + unit + integration tests. |
| T02.c — ApprovalStep (T10) integration wrappers | 1d | Wrap the existing steps in workflow YAMLs that insert an `ApprovalStep` (from T10) after the BV-BRC acquisition and after clustering, per the gate policy. |
| T02.d — RAG-friendly descriptions for T03 | 1d | Write a 1-sentence description + 2 keyword examples for each existing asset that the workflow uses. T03 uses these for composer retrieval. |

Total: **6 code-days (5–7 range).** This is ~40% of the Round 3 table estimate (10d) and ~20% of the original Round 2 estimate (30d).

**But the executor-coupling fix (T02r, 5d) is still separate and still gated on the edit-nanobrain discussion.**

---

## 8. Next actions

1. **User signs off on `docs/scope_decisions/01_where_new_code_lives.md`** (where new code lives). This unblocks T02 implementation path.
2. **User sign-off required for T02r** (executor-decoupling in `annotation_mapping_step.py` and the Parsl YAML) — part of the edit-nanobrain discussion.
3. **T00.1b workflow spec** — still the Phase-0 gate. Decides whether we need all 6 VIOLIN readers or 2–3 composites.
4. Proceed with TX1 (API contract) + T09 models stub in parallel while the above decisions are pending; those don't depend on any nanobrain edit.

---

## Appendix — raw subagent outputs

Full transcripts are preserved in the 3 Explore subagent runs on 2026-04-21 (see `.claude/` task records). This document is the consolidated summary.
