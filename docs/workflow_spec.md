# T00.1b — Workflow Spec: VIOLIN × BV-BRC with LLM Synonym Review

**Date:** 2026-04-21
**Status:** Draft; requires user sign-off before T01 fixtures are written.
**Derived from:** `apecx-db-integration/` (the Explore subagent's walk found ~900 lines of existing LLM synonym matching in `apecx-db-integration/src/agent.py`) + existing-asset inventory + user directive 2026-04-21 ("pause/resume for manual review of LLM-proposed synonyms").

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

## 3. Steps (7)

| # | Step name | Disposition | Existing asset | Wall-time |
|---|---|---|---|---|
| 1 | `entity_extraction` | reuse | `apecx-db-integration/src/agent.py:extract_entities_llm()` | ~2–3s (1 LLM call) |
| 2 | `bvbrc_snapshot_match` | wrap | nanobrain `enhanced_bv_brc_data_acquisition_step.py` (needs real TSV loader — the `_load_csv_data()` is currently a placeholder returning empty DataFrame) | ~1s file + ~3s LLM |
| 3 | `llm_synonym_proposals` | reuse | `apecx-db-integration/src/agent.py:consolidated_synonym_search()` + `enrich_matches_with_database_data()` | ~3–5s |
| 4 | **`synonym_approval_gate`** | **new (T10 scope)** | none — blocked on `ApprovalStep` class | 30s–5m (human) |
| 5 | `violin_entity_lookup` | reuse + small new | join logic in `enrich_matches_with_database_data()`; maybe 1–2 VIOLIN CSV readers | ~0.5s |
| 6 | `genomic_annotation` | wrap | nanobrain `bv_brc_data_acquisition_step.py` + `ProteinSynonymAgent` | ~2–4s |
| 7 | `result_ranking` | reuse | nanobrain `result_collection_step.py` + `ResponseFormattingStep` | <0.5s |

### 3.1 Data flow (condensed)

```
query
  └─> [1 entity_extraction]
         └─> detected_entities: [{name, type, confidence}, ...]
               └─> [2 bvbrc_snapshot_match] ───┐
               │                                ├─> matched_genomes: [{genome_id, strain, ...}, ...]
               └─> [3 llm_synonym_proposals]    │
                     └─> synonym_proposals: {entity -> [top-3 candidates with scores]}
                           └─> [4 synonym_approval_gate] ── HITL pause ──> approved_synonyms
                                 └─> [5 violin_entity_lookup]
                                       └─> resolved_entities: {vaccines, pathogens, genes, joins}
                                             └─> [6 genomic_annotation] ─ uses matched_genomes + resolved_entities
                                                   └─> genomic_annotations: {genome_id -> [{protein, violin_match, score}]}
                                                         └─> [7 result_ranking]
                                                               └─> ranked_entities.{csv,json}
```

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

## 7. Effort estimate (Round 3 post-derivation)

| Block | Days | Notes |
|---|---|---|
| 7 step wrappers (from agent.py + existing nanobrain steps) | 3d | One per step; each ~0.5d; Step 6 is the most involved at ~1d |
| Workflow YAML + links | 0.5d | Follows the nanobrain-workflow-authoring pattern |
| VIOLIN readers (composite ~2) | 1d | Or deferred; `pd.read_csv` inline is fine for MVP |
| `ApprovalStep` class (T10) | 3d | Assumes T00.2 spike is green |
| Snapshot-loader wrapper for Step 2 | 1d | Wraps the placeholder-CSV step with a real TSV reader |
| Tests: smoke + integration | 2d | Real-data integration test per workspace policy |

**Total: ~10.5 code-days.** Matches the Round 3 T02 + T10 envelope in `implementation_plan.md`.

---

## 8. Open questions for user

1. **Is the sample query `"What vaccines target chikungunya?"`** the right starting query, or is there a different one that would better exercise the VIOLIN/BV-BRC cross-reference? (The output's usefulness depends on which query hits the most populated cells in both datasets.)
2. **HARD vs. SOFT gate for Step 4?** A HARD gate blocks indefinitely; a SOFT gate times out (default: auto-approve top matches). HARD is safer for the first release; SOFT is what scientists will actually want after they get bored of approving every run.
3. **Batch query behavior — defer explicitly?** Current spec is single-query. Making this explicit in the scope document avoids scope creep later.

---

## 9. Sign-off

_User: please indicate sign-off or specific redirects below._

- [ ] Accept as-is.
- [ ] Accept with modifications (write them under each affected section).
- [ ] Replace with an alternative workflow (provide).

Signature / date: ___________________________
