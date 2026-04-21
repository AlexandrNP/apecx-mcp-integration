# T00.1b — Workflow Spec: VIOLIN × BV-BRC with Verified-Synonym Caching

**Date:** 2026-04-21 (v2 with user modifications 2026-04-21)
**Status:** Modified per user directive; awaiting final sign-off.
**Derived from:** `apecx-db-integration/` (the Explore subagent's walk found ~900 lines of existing LLM synonym matching in `apecx-db-integration/src/agent.py`) + existing-asset inventory + user directive 2026-04-21 ("pause/resume for manual review of LLM-proposed synonyms").

### v2 changes (user directive 2026-04-21)

1. **Persistent `VerifiedSynonym` entity** — approved synonym mappings persist across runs. Second-and-later runs hit the cache for previously-verified terms; only genuinely novel terms reach the LLM + HITL gate.
2. **Pre-processing Step 0** — one-time (or snapshot-refresh-triggered) extraction of unique IDs and name variants from the BV-BRC and VIOLIN snapshots into separate vocabulary artifacts. These vocabularies ground the synonym search: deterministic fuzzy match first, LLM reasoning only for genuinely novel cases.
3. **Step 3 split into deterministic + LLM passes** — the previous single-step synonym proposal now has: (a) check `VerifiedSynonym` cache, (b) fuzzy-match against the Step-0 vocabulary artifact, (c) LLM propose for residuals, (d) HITL approve. Each layer reduces what flows into the next.

---

## 1. One-sentence description

Given a user query about vaccines/pathogens, propose VIOLIN ↔ BV-BRC synonym mappings with LLM-derived confidence scores, pause for human approval of those mappings, then return a ranked entity-relationship table enriched with genomic annotation — end-to-end on a laptop, reading local snapshots only.

---

## 2. Inputs and outputs

### Input

A natural-language query string, e.g.:
> "What vaccines target chikungunya? Show me matching genomes in BV-BRC and which genes are involved."

Plus local snapshot files (all already present):

| Source | Path | Format |
|---|---|---|
| BV-BRC genomes | `data/bvbrc_cache/alphavirus_genomes.tsv`, `chikungunya_virus_genomes.tsv` | TSV |
| BV-BRC proteins | `data/bvbrc_cache/alphavirus_proteins.tsv`, `chikungunya_virus_proteins.tsv` | TSV |
| BV-BRC protein seqs | `data/bvbrc_cache/*_proteins_annotated.fasta` | FASTA |
| VIOLIN vaccines | `data/violin/Vaccine_Information.csv` | CSV |
| VIOLIN pathogens | `data/violin/Pathogen_Information.csv` | CSV |
| VIOLIN genes | `data/violin/Gene_Information.csv` | CSV |
| VIOLIN vaccine↔pathogen | `data/violin/Vaccine_Pathogen_Information.csv` | CSV |
| VIOLIN gene↔vaccine↔pathogen | `data/violin/Gene_Vaccine_Pathogen_Information.csv` | CSV |

### Output

`results/<run_id>/ranked_entities.csv` with columns:

```
vaccine_name, pathogen_name, gene_name, genome_strain, host,
protective_immunity, protein_match_score, composite_score, sources
```

Plus `results/<run_id>/ranked_entities.json` with the same data plus provenance (run_id, model_version, approved_synonym_decisions).

### Target laptop wall-time

**~15–20s of compute + variable human review time (30s–5m).** Measured on the existing `apecx-db-integration/agent.py:process_query()` flow against the same snapshots — not guessed.

---

## 3. Steps (8 main + 1 preprocessing)

### Step 0 (preprocessing — separate workflow, run on snapshot refresh)

`vocabulary_extraction` — reads `data/bvbrc_cache/*.tsv` and `data/violin/*.csv`; produces two artifacts:

- `bvbrc_vocabulary.jsonl` — unique genome/strain/protein IDs and every name variant observed. Schema: `{id, type, canonical_name, aliases: [...], source_file, source_row}`.
- `violin_vocabulary.jsonl` — unique vaccine/pathogen/gene IDs and every name variant. Same schema.

These are *artifacts*, not a live DB: they are hashed, stored in the Control Plane artifact store, and referenced by `library_version` so the main workflow can pin its dependencies. They regenerate only when the snapshot files change (detected by mtime + content hash).

This step is **NOT** part of the main workflow. It is its own tiny workflow (single step + file-write). The main workflow consumes its output artifacts read-only.

### Main workflow (7 steps)

| # | Step name | Disposition | Existing asset | Wall-time |
|---|---|---|---|---|
| 1 | `entity_extraction` | reuse | `apecx-db-integration/src/agent.py:extract_entities_llm()` | ~2–3s (1 LLM call) |
| 2 | `bvbrc_snapshot_match` | wrap | nanobrain `enhanced_bv_brc_data_acquisition_step.py` (needs real TSV loader) | ~1s file + ~3s LLM |
| 3a | `synonym_cache_lookup` | **new (small)** | queries `VerifiedSynonym` via Control Plane; no LLM | <0.1s |
| 3b | `synonym_fuzzy_match` | **new (small)** | deterministic fuzzy match against the Step-0 vocabulary artifacts for terms that missed the cache; emits a `likely_synonyms` set plus a `novel_terms` set | <0.2s |
| 3c | `synonym_llm_proposals` | reuse (smaller inputs) | `apecx-db-integration/src/agent.py:consolidated_synonym_search()` — now only called for `novel_terms` | ~2–4s (scales with residual count) |
| 4 | **`synonym_approval_gate`** | **new (T10, in nanobrain per scope-decision memo 02)** | `ApprovalStep` in `nanobrain/nanobrain/library/steps/` | 30s–5m (human); short-circuits to 0s for cache hits |
| 4p | `verified_synonym_writeback` | **new (small)** | POSTs approved synonyms to Control Plane so future runs hit the cache | <0.1s |
| 5 | `violin_entity_lookup` | reuse + small new | join logic in `enrich_matches_with_database_data()` | ~0.5s |
| 6 | `genomic_annotation` | wrap | nanobrain `bv_brc_data_acquisition_step.py` + `ProteinSynonymAgent` | ~2–4s |
| 7 | `result_ranking` | reuse | nanobrain `result_collection_step.py` + `ResponseFormattingStep` | <0.5s |

### Cache-hit / cache-miss economics

On the **first run** of a novel query, the flow is:
`entity_extraction → snapshot_match → cache_lookup (miss) → fuzzy_match (candidates) → llm_proposals → approval_gate (human) → writeback → ...`

On the **second run** of the same query (or a structurally similar one), the flow becomes:
`entity_extraction → snapshot_match → cache_lookup (hit) → (skip fuzzy / llm / approval) → violin_lookup → ...`

This is the core win of the v2 design: repeat-query cost drops from "~5s LLM + human review" to "<0.2s DB lookup."

### Conditional short-circuit

The `synonym_approval_gate` (Step 4) is only invoked when `novel_terms` is non-empty after Steps 3a and 3b. If everything hit the cache or fuzzy-matched with high confidence, the workflow skips the gate entirely and proceeds to Step 5.

This is an explicit design choice: we trade "always ask the user to confirm" for "ask the user only when we cannot confirm algorithmically." The first policy is safer against drift; the second policy respects the user's attention.

**Safety net:** a SOFT gate variant is available for paranoid mode — auto-approve cache-and-fuzzy hits after a short timeout. Configurable per run.

### 3.1 Data flow (condensed, v2)

```
              (separate preprocessing workflow, runs on snapshot refresh)
              [0 vocabulary_extraction]
                     └─> bvbrc_vocabulary.jsonl, violin_vocabulary.jsonl  (pinned artifacts)

query
  └─> [1 entity_extraction]
         └─> detected_entities
               ├─> [2 bvbrc_snapshot_match] ────> matched_genomes
               └─> [3a synonym_cache_lookup]
                     ├─> cache_hits (already-verified mappings)
                     └─> cache_misses
                           └─> [3b synonym_fuzzy_match]  (uses Step 0 artifacts)
                                 ├─> confident_fuzzy_hits
                                 └─> residuals
                                       └─> [3c synonym_llm_proposals] (only residuals)
                                             └─> llm_proposals
                                                   └─> [4 synonym_approval_gate] ── HITL pause ──> approved_novel
                                                         └─> [4p verified_synonym_writeback] ─> cache updated for next run
                                                               └─┐
                                                                 │
 (cache_hits + confident_fuzzy_hits + approved_novel) ──────────> resolved_synonyms
                                                                 │
                                                                 └─> [5 violin_entity_lookup]
                                                                       └─> resolved_entities
                                                                             └─> [6 genomic_annotation] ─ uses matched_genomes
                                                                                   └─> [7 result_ranking]
                                                                                         └─> ranked_entities.{csv,json}
```

**If `residuals` is empty after 3b:** Steps 3c and 4 are skipped entirely. 4p is a no-op. The workflow completes without any LLM call for synonym work and without any human review.

### 3.2 Step 4 contract (the HITL hook)

The step calls `control_plane.create_approval(kind=HARD, summary=..., artifact_ids=[synonym_proposals_artifact])` and awaits decision. On:

- `APPROVED`: pass through the top-ranked synonym per entity.
- `APPROVED_WITH_MODIFICATIONS`: the `modifications` dict from the user replaces the proposed synonym set entirely (or per-entity).
- `REJECTED`: raise; workflow terminates with `Run.status = cancelled` and a provenance event explaining.

**Payload presented to the user (MCP tool output):**

```
Please review 3 proposed synonym mappings:

PATHOGEN "chikungunya" → top candidates:
  1. "Chikungunya virus"           (score 0.98)
  2. "CHIKV"                       (score 0.85)
  3. "CHIK"                        (score 0.82)

VACCINE "vaccines" → top candidates:
  1. "CHIKV vaccine candidate X"   (score 0.76)
  2. "Live attenuated chimeric"    (score 0.62)
  3. "Inactivated CHIKV"           (score 0.55)

Approve all top matches? Or modify?
```

The correction payload, if provided, is shaped as:

```json
{
  "synonyms": {
    "pathogen:chikungunya": "Chikungunya virus",
    "vaccine:vaccines": "CHIKV vaccine candidate X"
  }
}
```

---

## 4. What already exists vs. what must be authored

### Already exists — read-and-reuse (no nanobrain edits required)

- `apecx-db-integration/src/agent.py:extract_entities_llm()` — Step 1
- `apecx-db-integration/src/agent.py:consolidated_synonym_search()` — Step 3
- `apecx-db-integration/src/agent.py:enrich_matches_with_database_data()` — backing logic for Step 5

### Must be authored in `apecx-mcp-integration/` (Option B default from scope-decision 01)

1. A nanobrain step wrapper for each of the 7 steps (`apecx_integration/steps/violin_bvbrc/*.py`).
2. A workflow YAML (`apecx_integration/config/workflows/violin_bvbrc_integration.yml`).
3. 1–2 VIOLIN CSV reader steps (or `pd.read_csv` inlined in Step 5 for MVP).
4. **The `ApprovalStep` class** — T10 deliverable; this is the new architectural primitive the whole workflow turns on.

### Blocked on edit-nanobrain discussion (Option C escalation from scope-decision 01)

- Fixing the placeholder `_load_csv_data()` in `enhanced_bv_brc_data_acquisition_step.py` — currently returns empty DataFrame (line 1402).
- Decoupling `annotation_mapping_step.py` from its hardcoded `WorkQueue` executor call at line 344.

**Both can be worked around** by building parallel Step 2 and Step 6 wrappers in `apecx_integration/` that read the snapshots directly. Slightly duplicated code; no nanobrain edit required.

---

## 5. Acceptance criteria (for T01 vertical-slice integration test)

The workflow is "T01-ready" when:

- **AC1** — End-to-end runs locally with no HPC dependency on the sample query `"What vaccines target chikungunya?"`.
- **AC2** — Output has ≥1 matched VIOLIN vaccine × BV-BRC genome row.
- **AC3** — Step 4 pauses; the MCP surface shows a pending approval; `approve()` via MCP resumes the workflow.
- **AC4** — `reject()` via MCP aborts cleanly with a provenance record.
- **AC5** — `correct(modifications=...)` overrides the proposed synonyms and the workflow continues with the overrides.
- **AC6** — Re-running the same query produces a deterministic output **shape** (same columns, same file layout) even if LLM content varies.
- **AC7** — Killing the Control Plane during the approval pause and restarting it preserves the pending approval (from T09).
- **AC8** — Provenance log has 1 `run_started` + 1 per step + 1 `approval_requested` + 1 `approval_decided` + 1 `run_completed`, with a valid hash chain.
- **AC9** — No mocks in production code paths (workspace policy); LLM calls use the real API with temperature=0 for the composition path.

---

## 6. Brutal-truth commentary

1. **70% reuse is the *optimistic* read.** The Explore subagent read `agent.py` and concluded 70% of the workflow is already written there. That's true at the line-count level. What it does not measure is (a) integration friction — `agent.py` is a standalone script, not a `from_config`-compatible nanobrain step; wrapping it may take a full day per function. (b) quality — "it has comprehensive validation infrastructure" (subagent's words) does not mean it handles edge cases the laptop surface will hit.
2. **"CLI-stub for approval" is a tempting shortcut to avoid.** If Step 4 is stubbed with a CLI `input()` prompt, we test steps 1–3 and 5–7 against synthetic decisions. The integration-test value is in exercising the real pause/resume path with the real MCP surface. Do T00.2 spike now; do T10 real implementation first; skip the stub.
3. **LLM scoring drift is a real hazard.** The synonym-proposal step depends on consistent LLM confidence scores across vaccine vs. pathogen vs. gene entity types. Plan to pin the model to `claude-opus-4-7` with `temperature=0`, and assert on *structure* (top-3 per entity, scores in [0,1]) rather than on *content* in tests.
4. **The workflow assumes ONE query → ONE run.** Batch queries ("give me matches for 20 pathogens at once") are out of scope for T01. If the team wants batch later, Step 1 fans out and Step 4 becomes an N-way approval — a real design decision to defer explicitly.
5. **The VIOLIN "Curated References" file** (`VIOLIN_Curated_References.txt`) is not used in this workflow. It might be relevant for a provenance-enrichment extension but is not MVP.

---

## 7. Effort estimate (v2, post user directive)

| Block | Days | Notes |
|---|---|---|
| **Step 0 preprocessing workflow** (vocabulary extraction) | 1d | Single step + file-write; small |
| Step wrappers 1, 2, 5, 6, 7 (existing logic) | 2.5d | Each ~0.5d |
| Step 3a (cache lookup) — new | 0.5d | DB query via Control Plane client |
| Step 3b (fuzzy match) — new | 1d | Uses `rapidfuzz` or similar against vocab artifacts |
| Step 3c (LLM proposals) — reuse (smaller input) | 0.5d | Same `consolidated_synonym_search` but called on residuals only |
| Step 4 `ApprovalStep` (T10, in nanobrain) | 3d | Per scope-decision memo 02 |
| Step 4p writeback | 0.5d | POST verified synonyms to Control Plane |
| Workflow YAML + links (main workflow) | 0.5d | Standard nanobrain pattern |
| VIOLIN readers | 1d | Defer to inline pandas in MVP if needed |
| Snapshot-loader wrapper (Step 2) | 1d | Real TSV reader replaces placeholder |
| `VerifiedSynonym` T09 migration + routes | 1d | New table + GET/POST endpoints |
| Integration test (real data, full workflow) | 2d | Per workspace mocks-policy (mocks must also have real-data integration coverage) |

**Total: ~14.5 code-days.** +4d from v1. The preprocessing step, the cache lookup, the fuzzy match, and the writeback are new. In exchange, we trade "every run costs human time" for "novel terms cost human time; repeats are free."

**Break-even:** if the workflow is run ≥4 times with overlapping queries, v2 pays off in saved human review time. Below that, v1 would have been cheaper — but v2 is still the right call because the verified-synonym table has provenance value beyond the immediate workflow.

---

## 8. Open questions for user

1. **Is the sample query `"What vaccines target chikungunya?"`** the right starting query, or is there a different one that would better exercise the VIOLIN/BV-BRC cross-reference? (The output's usefulness depends on which query hits the most populated cells in both datasets.)
2. **HARD vs. SOFT gate for Step 4?** A HARD gate blocks indefinitely; a SOFT gate times out (default: auto-approve top matches). With v2's cache-hit short-circuit, the gate fires less often — HARD is probably the right default now.
3. **Batch query behavior — defer explicitly?** Current spec is single-query. Making this explicit in the scope document avoids scope creep later.
4. **Fuzzy-match threshold.** Step 3b emits `confident_fuzzy_hits` above some similarity threshold (say 0.92) and `residuals` below it. Where to draw the line is an empirical question — start at 0.92 and tune based on first-scientist feedback.
5. **Verified-synonym overrides.** Can a user revoke or correct a previously-approved `VerifiedSynonym`? Probably yes (mistakes happen). If so, the cache lookup (Step 3a) needs an "active" flag and a revocation path. Defer to T09 migration design, but flag now.

---

## 9. Sign-off

_User: please indicate sign-off or specific redirects below._

- [ ] Accept as-is.
- [ ] Accept with modifications (write them under each affected section).
- [ ] Replace with an alternative workflow (provide).

Signature / date: ___________________________
