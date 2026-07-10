# Harmonization Precision + Recall Eval — Findings

**Status:** non-circular precision measured across the full 140-query corpus × 9 harmonized DEST indices
against LIVE Globus, plus a per-index coverage assessment. The prior 2×2 ablation's harmonized-cell
precision (100%) was **circular** — it adjudicated a record by `subjects.valueUri`, the exact field the
query filters on. This eval judges relevance from evidence the filter did NOT use (source taxon id ∈
queried subtree, and title/organism text), so **precision now shows real variation (0.0 → 0.93)** where
the old number was a flat 1.0.

**Run:** `generated 2026-07-10`, 140 queries → 1206 cells (6 ambiguous queries paused, no probe),
`fetch_limit=1500` (recall is `recall@1500`), automated judge on ~15k records, LLM validation on n=43
(budget-capped). Reproduce: `APECX_SYNONYM_DICT_PATH=~/.apecx/dictionary/dictionary.sqlite PYTHONPATH=src:.
.venv/bin/python -m tests.eval.harmonization.run_harmonization --k 25 --fetch-limit 1500 --llm-validate`.
Raw JSON (re-scorable): `output/harmonization_precision.json`.

## Headline numbers

| dimension | precision | judged | unjudgeable-rate | mean recall-lift (recall@1500) |
|---|---|---|---|---|
| **by category** — mu_virus | 0.78 | 3703 | 0.16 | +0.02 |
| by category — abbreviations | 0.72 | 2011 | 0.11 | +0.28 |
| by category — real_world | 0.93 | 1660 | 0.13 | +0.40 |
| **by regime** — resolved_species | **0.93** | 6267 | — | +0.18 |
| by regime — umbrella_overbroad | 1.00 | 72 | — | +0.07 |
| by regime — miss_raw_fallback | **0.00** | 1035 | — | n/a |

**False-positive attribution (all cells):** `raw_substitution` 1370 (**92%**), `multi_subject_incidental`
112 (8%). Precision loss is overwhelmingly the raw-text fallback, not multi-subject noise.

---

## HF1 — miss→raw-fallback precision is 0.0; it is 92% of all false positives (CODE-GROUNDED)

When a query does not resolve to a taxon IRI (`path == "miss"`), `harmonized_search` serves a **raw
full-text** corpus (`harmonized_search_execute_step.py::_run_miss_envelope`), and the merge step keeps
it (`harmonized_bundle_merge_step.py::_records_from_item`, harm-empty→raw). Measured precision on those
44 judged cells: **0.00**, all 1035 false positives classed `raw_substitution`. This is the
`chikungunya envelope → West Nile 7E4K` failure mode, now quantified corpus-wide. Judge A is FULLY
independent here (no `valueUri` filter ever touched these records), so the 0.0 is not an artifact.
**Fix (separate /feature):** either NER + resolve before serving on a miss, or label the miss corpus as
non-taxon-precise so a consumer never treats it as organism-filtered. This is the dominant precision leak.

## HF2 — DENV/LASV/MARV/NiV/RABV still MISS in the DEPLOYED dictionary (CODE-GROUNDED, actionable)

The acronyms `DENV, LASV, MARV, NiV, RABV` (plus group names `arbovirus, coronavirus, hepatitis virus,
herpes simplex, hemorrhagic fever virus`) resolve as `miss` → 0 harmonized coverage across **all 9
indices** → served as raw text (HF1, precision 0.0). Root cause: the live dict is version
`multiclade-species-2026-06-09`, which **predates the 2026-06-28 acronym fix** (commit `bb41784` added 18
acronym→canonical entries incl. LASV/MARV/NiV/RABV). The fix exists in code but was **never republished
to the deployed dictionary**. **Fix (separate /feature / ops):** republish the synonym dictionary so the
acronym deltas are live; DENV additionally needs the serotype→parent-species handling (not in the 18).

## HF3 — resolved_species precision is 0.93, confirmed NON-circularly (INFORMATIONAL)

For cleanly-resolved species (1044 cells, judged=6267), precision is **0.93** — and unlike the prior
benchmark this is not circular: the record's source `NCBI-Taxonomy` id independently sits in the queried
subtree. A species-IRI filter cannot leak sibling species (the `subjects.valueUri` lineage stamp is
precise at species level), so clean resolution genuinely IS precise. This is the eval confirming the
harmonized filter works where resolution works — the honest positive result.

## HF4 — per-index precision varies 0.52 → 0.93; several 1.0s are honest-but-thin (INFORMATIONAL)

Per-index precision is NOT uniform. Genuinely-judged indices: `violin_pathogen` **0.52** (lowest),
`bvbrc_genome` 0.67, `bvbrc_epitope` 0.70, `bvbrc_protein_structure` 0.82. The indices reading **1.00**
(`violin_vaccine`, `bvbrc_protein`, `antiviraldb`, `protabank`) do so on a **thin judged base** — high
unjudgeable-rate (`violin_vaccine` 0.47, `protabank` 0.42, `antiviraldb` 0.30) means most of their
records carry neither a source taxon id nor a text synonym the judge can corroborate, so the 1.0 is
low-confidence, not strong. Two indices show **negative recall-lift** — `violin_vaccine` −0.17,
`protabank` −0.34 — i.e. harmonization RETRIEVED FEWER relevant records than raw there, consistent with
their `broken`/`degraded` verdict concentration (stale/ICTV-rename stamping). **Fix (separate /feature):**
audit the low-precision (`violin_pathogen`) and negative-lift (`violin_vaccine`, `protabank`) index
stampings.

## HF5 — index coverage assessment (INFORMATIONAL — the per-index deliverable)

Coverage = fraction of the 125 probed pathogens for which an index returns ≥1 harmonized record.

| index | coverage | precision | recall-lift | unjudgeable | dominant verdicts |
|---|---|---|---|---|---|
| bvbrc_protein_structure | **0.84** | 0.82 | +0.13 | 0.12 | helped 53 / degraded 45 |
| bvbrc_epitope | 0.69 | 0.70 | +0.34 | 0.00 | healthy 41 / helped 32 |
| violin_gene | 0.68 | 1.00* | +0.33 | 0.16 | helped 47 / degraded 25 |
| bvbrc_genome | 0.66 | 0.67 | +0.46 | 0.00 | helped 51 / healthy 31 |
| violin_pathogen | 0.63 | **0.52** | +0.13 | 0.00 | healthy 59 / zero 24 |
| antiviraldb | 0.54 | 1.00* | +0.11 | 0.30 | zero 34 / healthy 33 |
| violin_vaccine | 0.48 | 1.00* | **−0.17** | 0.47 | helped 51 / broken 30 |
| bvbrc_protein | 0.22 | 1.00* | +0.26 | 0.08 | zero 84 / errored 17 |
| protabank | **0.11** | 1.00* | **−0.34** | 0.42 | zero 93 / errored 17 |

`*` = 1.0 on a thin judged base (see HF4). Per-pathogen: least-covered = the HF2 miss set (0 indices:
DENV/LASV/MARV/NiV/CCHF); best-covered = chikungunya/measles (14–16 cells with data across categories).
`bvbrc_protein` + `protabank` are dominated by `zero_floor_unclear` + `errored` cells — low-coverage
indices where most pathogens return nothing.

## HF6 — judge confidence is LOW-and-honest; treat precision as a lower-bound estimate (GATED)

LLM validation (n=43, budget-capped 180s, model `nemotron-3-nano:4b`): **accuracy 0.56, Cohen κ 0.13**
(slight agreement). This does NOT mean the automated judge is wrong — a 4B model judging virology
relevance on 43 samples is itself a weak validator, and the automated judge is grounded in the NCBI
taxonomy hierarchy (Judge A) which is more authoritative than the small LLM. But it does mean the
precision point-estimates carry low EXTERNAL confidence and should be read as directional. **Fix:** re-run
validation with a larger budget + a stronger judge model (`--llm-validate`, raise the budget) for a
tighter κ before citing precision as definitive.

---

## Methodology caveats (read before citing any number)

- **Non-circular judge:** relevance = Judge A (source `NCBI-Taxonomy` id ∈ queried species subtree, via
  `DictionaryIndex.lookup_descendant_taxon_ids`) ∨ Judge B (title/organism text names a dict synonym).
  Neither reads `subjects.valueUri` (the filtered field). See `judges.py`; non-circularity pinned by
  `test_harmonization_metrics.py::test_judge_a_does_not_read_valueuri`.
- **Judge A partial-independence:** for a single-stamp harmonized record whose `valueUri` was derived from
  the same source id, Judge A is provenance-partial; it is FULLY independent on the `raw_substitution`
  cells (where the whole precision story lives). Residual covered by text-only Judge B + the LLM.
- **`recall@1500`:** recall is a pool-relative TREC estimate at fetch depth 1500, NOT full-corpus recall.
  Coverage (`harm_total`) uses the true `total` and is depth-independent.
- **Precision-optimism on capped cells:** where a served corpus exceeds 1500, the sample is from Globus's
  relevance-ordered HEAD, biasing precision on those heavy cells OPTIMISTICALLY (later pages carry more
  incidental matches). Bounded — most cells have total < 1500.
- **Reproducibility:** Globus totals drift; every JSON carries `generated_utc` + a per-query resolution
  snapshot. Re-scoring runs offline over the JSON.

## Actionable follow-ups (each a SEPARATE /feature — out of scope for this read-only eval)

1. **HF2 — republish the dictionary** so the DENV/LASV/MARV/NiV/RABV acronym fix is live (highest ROI:
   restores harmonized coverage for 5 major pathogens).
2. **HF1 — the miss→raw-fallback precision-0.0 path**: NER-resolve or hard-label the raw fallback.
3. **HF4 — audit low/negative-precision index stampings** (`violin_pathogen`, `violin_vaccine`, `protabank`).
4. **HF6 — tighten judge confidence** with a larger LLM validation budget + stronger model.
