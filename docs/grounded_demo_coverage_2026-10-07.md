# Grounded demo coverage — findings & changes (2026-10-07)

**Branch:** `feat/grounded-demo-coverage` (commits `3f18f3a`, `5a69164`, `9aadbe3`; local, unpushed)

**Problem reported.** On the grounded-vs-web demo, (1) agent+web-search found *more* PDB
structures than the grounded tool, and (2) the grounded literature answers were weak because
they only ever saw PubMed **abstracts**, not full text.

---

## Findings (root causes, verified live against the real indices/APIs)

### A. PDB structures — TWO distinct causes
1. **Silent query bug (P1).** `structural_query.search_one_source` (PDB branch) applied the
   structural-keyword residual (e.g. `"glycoprotein"`) as a **required free-text `q`** ANDed on
   top of the already taxon-locked `match_any` on `pdb.polymer_entities.scientific_name`. Records
   whose DataCite payload lacked the literal keyword were silently dropped.
   Proof (live): Nipah `q='"glycoprotein"'` + taxon filter → **0**; `q='*'` + same filter → **4**
   real records (7SKT/7SKU/9IV9/9KNZ). Chikungunya only survived because its larger corpus
   happens to carry the keyword.
2. **Genuine harvest gap (P2).** The APECx Globus "aggregate" index (`e74bf12a`) is a **partial
   harvest of PDB — ~27K of PDB's ~230K entries.** Taxon-correct structures are simply absent:
   the index held **4** of Nipah's **76** PDB structures, and **zero** of the ~21 G/F glycoprotein
   structures (2VSM, 3D11, 5EVM, 6T3F, …). No query change can surface records that were never
   harvested in.

### B. Full text — the literature path was abstracts-only
`literature_qa` → `literature_rag` retrieved PubMed **abstracts** only (confirmed by running it:
"desktop locus — handing 25 ontology-filtered records to the host"). Abstracts omit the
epitope-residue / animal-model / cross-lineage detail the specialized questions need. The PMCID
needed for full-text retrieval was **already in the eFetch response** but was being dropped in
`container_to_dict`.

---

## Changes

| Commit | Scope | What it does |
|---|---|---|
| `3f18f3a` (P1) | `structural_query.py` | When the keyword-focused PDB query returns nothing, fall back to `q='*'` (taxon-lock only) instead of a silent zero. `note` stays `None` (the `match_any` still guarantees the taxon), so the lockstep consumer keeps the records. Recovers silently-dropped records corpus-wide. |
| `5a69164` (P2) | `harmonized_search.py`, new `agents/functional/rcsb_live.py` | For `index="pdb"`, augment the (partial) aggregate result with the authoritative **live RCSB** set (Search API + Data API GraphQL), scoped to the **same taxon-resolved organism spellings** the facet enumerated. Exact `ncbi_scientific_name` match = the taxon-lock, so no off-organism bleed. Degrades loud to aggregate-only on an RCSB outage. MCP-tool boundary only — the workflow leg is untouched. EMDB unchanged. |
| `9aadbe3` (F1) | new `agents/literature/fulltext.py`, `_pubmed_helpers.py`, `harvest.py`, `pipeline.py`, `literature_rag_step.py` | Carry the PMCID (stop dropping it); fetch open-access full text from **Europe PMC** (`/{PMCID}/fullTextXML`) into `record["full_text"]`, best-effort + degrade-loud; synthesis grounds on full text (capped 12K desktop / 4K backend) when present, else abstract. Retrieval ranking unchanged. |

### Design choices worth remembering
- **Partial-harvest index → live-authoritative fallback.** A stale/partial curated snapshot is
  compensated at the tool boundary by a taxon-scoped query to the authoritative source (RCSB),
  not by re-harvesting. Still grounded (taxon-lock), just current + complete.
- **Grounding is preserved, not traded for recall.** Both P2 and F1 keep the organism taxon-lock;
  the RCSB filter is an exact organism-name match, so "more coverage" never means "contamination."
- **Self-contained API clients** (`rcsb_live.py`, `fulltext.py`) rather than bending the shared
  `functional/_http.AsyncHttpClient` (GET-JSON-only, raises on 404) — RCSB needs 204-on-empty +
  GraphQL POST; Europe PMC needs 404 → `""` ("not open-access" is a normal miss).
- **Degrade-loud everywhere.** A live-API outage → named note + the prior (aggregate / abstract)
  behavior; never a crash, never a silent empty. Full-text fetch raises only when *every* paper
  errors (systemic), tolerating per-paper 404s/500s.

---

## Before → after (verified live)

| Query | Before (grounded) | After (grounded) |
|---|---|---|
| Nipah glycoprotein structures (PDB) | 0 usable / 4 in-index | **76** (incl. all G/F glycoproteins) |
| Chikungunya envelope structures (PDB) | 11 | **100** |
| Lassa glycoprotein structures (PDB) | 31 | **86** (demo re-run) |
| RVFV epitope question (full text) | 25 abstracts | **19 of 25 papers with full-text body** |

---

## Tests & verification
- P1: `tests/integration/test_structural_query_live.py` (before/after + no-regression; live Globus).
- P2: `tests/integration/test_rcsb_live.py` (live RCSB — real data, taxon-grounding on every hit,
  G/F recovery, provenance note; + a no-network degrade-loud unit test).
- F1: `tests/integration/test_fulltext_retrieval.py` (live Europe PMC + no-network degrade) and a
  PMCID-carry unit test in `tests/unit/test_pubmed_helpers.py`.
- All three passed **review-gate** (PASS-WITH-NOTES; notes addressed). Directly-affected tests green.

## Caveats / not done
- **Not pushed / not merged** — three local commits on `feat/grounded-demo-coverage`.
- **EMDB** has the same partial-harvest gap; only **PDB** got the live fallback (follow-up).
- **Pre-existing, unrelated:** `test_literature_qa_real_chikv` fails on a stale allowed-status set
  (`host_synthesizes`, from the earlier desktop-locus work) — not caused by these changes; left
  untouched to keep them surgical.
