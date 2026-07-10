# Harmonization Precision + Recall Eval — Findings (v2)

**Status:** re-run of the non-circular eval with five hardening changes applied — the production acronym
fix now live (mirrored in the eval), **full-corpus recall** (fetch depth = production's 10k ceiling, not
pool@1500), **un-restrained LLM cross-validation** (`devstral:24b`, was a 4B model capped at n=43), a
**per-index 0-coverage root-cause matrix**, and a fixed miss-verdict mislabel. Run on the deployed
`multiclade-species-2026-06-09` dictionary (a fresh rebuild was produced + verified but is a
coverage-inferior base dict — see the note under "Dictionary"). 140 queries → 1206 cells, `fetch_limit=10000`.

**The v1 → v2 headline movement (what the changes bought):**

| metric | v1 | v2 | why |
|---|---|---|---|
| `abbreviations` precision | 0.724 | **0.889** | acronyms now resolve instead of serving raw text |
| `miss_raw_fallback` cells | 153 | **99** | DENV/LASV/MARV/NiV/RABV moved out of the miss regime |
| `resolved_species` cells | 1044 | **1107** | more queries resolve cleanly (same 0.93 precision) |
| all false positives that are `raw_substitution` | 1370 (92%) | 1012 (**89%**) | fewer miss cells → less raw fallback |
| recall basis | pool@1500 | **full-corpus** (98% of cells) | fetch depth 1500→10000 |
| judge-confidence κ | 0.13 (n=43) | **0.23 (n=400)** | un-restrained `devstral:24b` (6× params, 9× sample) |

## Headline numbers (v2)

| dimension | precision | cells | notes |
|---|---|---|---|
| by category — mu_virus | 0.784 | — | |
| by category — abbreviations | 0.889 | — | up from 0.724 (acronym fix) |
| by category — real_world | 0.931 | — | |
| **by regime — resolved_species** | **0.930** | 1107 | non-circular, clean resolution |
| **by regime — miss_raw_fallback** | **0.000** | 99 | genuine group terms only (acronyms gone) |

**False-positive attribution:** `raw_substitution` 1012 (**89%**), `multi_subject_incidental` 125 (11%).

---

## HF1 — the miss→raw-fallback path is still precision-0.0, but the miss set SHRANK to genuine group terms (CODE-GROUNDED)

The resolution-miss corpus (served as raw full-text, `harmonized_search_execute_step.py::_run_miss_
envelope`) still measures **precision 0.00** — when a query does not resolve to a taxon, the served
records are taxon-imprecise and off-target. What changed: the miss set dropped from 153 → 99 cells (17 →
11 unresolved terms). The remaining misses are **legitimately un-resolvable group/umbrella terms** with
no single species taxon: `arbovirus`, `coronavirus`, `coronavirus spike`, `hepatitis virus`, `herpes
simplex`, `herpesvirus`, `poxvirus`, `papilloma and polyoma viruses`, `hemorrhagic fever virus`,
`Crimean-Congo hemorrhagic fever`, `tuberculosis genome`. `raw_substitution` remains 89% of all false
positives — the raw fallback is still the dominant precision leak, now confined to genuine group terms.

## HF2 — CORRECTED root cause: the acronym miss was a PRODUCTION CODE gap, now FIXED (CODE-GROUNDED)

**v1's HF2 was wrong.** It attributed the DENV/LASV/MARV/NiV/RABV miss to a "stale dictionary that predates
the acronym fix, never republished." That is false: the deployed dict **already maps all five canonical
species** (`lookup_entity("Lassa mammarenavirus")` → `NCBITaxon_3052310`); the acronyms are absent from the
dict **by design** because they expand in *code*. The real gap was that
`composition/steps/harmonized_resolve_step.py::build_resolution_plan` called `lookup_entity(term)` on the
**bare** query and never applied the existing `extract_virus_names` acronym expansion.

**Fix (merged to main, commit `06250ff`):** on a resolution MISS (pathogen-type only), retry via
`extract_virus_names(term)` and adopt the first expansion that resolves; fail-safe (a non-resolving
expansion is discarded, so a genuine junk term stays a miss). Verified real-data: LASV→`NCBITaxon_3052310`,
MARV→`3052505`, NiV→`3052225`, RABV→`11292`, DENV→`12637`. The eval mirrors this, so the v2 numbers measure
the **fixed** product — which is exactly why `abbreviations` precision rose 0.72→0.89 and the acronyms
vanished from the miss set (HF1). **Republishing the dictionary would have fixed nothing** — the gap was
never in the data.

## HF3 — resolved-species precision is 0.93, non-circular, and holds at a LARGER sample (INFORMATIONAL)

For cleanly-resolved species (1107 cells, up from 1044), precision is **0.930** — measured by the
non-circular judge (source `NCBI-Taxonomy` id ∈ queried subtree ∨ title/organism synonym text, never the
filtered `subjects.valueUri`). The result is stable as more queries entered this regime via the acronym
fix: clean resolution genuinely IS precise, and the harmonized `subjects.valueUri` filter is doing its job.

## HF4 — 0-coverage is dominated by GENUINE ABSENCE, not stamping failure — 0 stamping mismatches (CODE-GROUNDED)

Root-causing every 0-coverage cell (harm==0) from its raw-leg records (`coverage_rootcause.py`, reusing the
`_datacite` readers + `judges.source_taxon_ids`) gives a clean, corpus-wide decomposition:

| cause | cells | meaning |
|---|---|---|
| `genuinely_absent` | **376** | raw==0 & harm==0 — the index holds no record for the organism (with the stale-dict-masquerade caveat) |
| `offtarget_raw_match` | **131** | raw>0 & harm==0, but the raw records are about OTHER organisms (source id not in subtree) — the index has no record about THIS organism; the harmonized filter correctly returned 0 |
| `missing_source_id` | **5** | raw records carry NO taxon id at all (no valueUri, no NCBI-Taxonomy) — nothing to stamp (the "missing UniProt/source id" class) |
| `stamping_mismatch` | **0** | records about the organism that were never stamped — **none found** |

**This corrects a v1 assumption.** The 80 `broken` cells (raw>0 & harm==0) are NOT harmonization/stamping
failures — every one is an `offtarget_raw_match`: the raw text leg matched records of *different*
organisms, and the taxon filter correctly excluded them. **The harmonized stamping is not the coverage
bottleneck; genuine absence is.** Only 5 cells corpus-wide are the "missing source identifier" class the
user asked to quantify (antiviraldb ×1, protabank ×4). There is no re-stamping work to do for coverage.

## HF5 — per-index coverage + precision + root-cause matrix (INFORMATIONAL — the per-index deliverable)

Coverage = fraction of the 125 probed pathogens for which the index returns ≥1 harmonized record.
`prec` is non-circular; `*` marks a 1.0 on a thin judged base (high unjudgeable and/or low coverage).

| index | coverage | precision | recall-lift | unjudg | 0-coverage root causes (absent / offtarget / missing-id) |
|---|---|---|---|---|---|
| bvbrc_protein_structure | **0.89** | 0.83 | +0.14 | 0.11 | 4 / 10 / 0 |
| bvbrc_epitope | 0.74 | 0.66 | +0.33 | 0.00 | 16 / 17 / 0 |
| violin_gene | 0.73 | 1.00* | +0.35 | 0.15 | 28 / 6 / 0 |
| bvbrc_genome | 0.70 | 0.76 | +0.53 | 0.00 | 24 / 14 / 0 |
| violin_pathogen | 0.66 | **0.51** | +0.13 | 0.00 | 28 / 16 / 0 |
| antiviraldb | 0.56 | 1.00* | +0.12 | 0.28 | 43 / 12 / 1 |
| violin_vaccine | 0.49 | 1.00* | **−0.18** | 0.44 | 27 / 40 / 0 |
| bvbrc_protein | 0.22 | 1.00* | +0.30 | 0.08 | 97 / 7 / 0 |
| protabank | **0.10** | 1.00* | **−0.41** | 0.41 | 109 / 9 / 4 |

Reading it: `violin_pathogen` (0.51) is the genuinely-low-precision index; the 1.0s
(`violin_gene`/`antiviraldb`/`violin_vaccine`/`bvbrc_protein`/`protabank`) are honest-but-thin (high
unjudgeable or tiny coverage). `violin_vaccine` (−0.18) and `protabank` (−0.41) show **negative** recall
lift — harmonization retrieved fewer relevant records than raw there. Their 0-coverage is genuine absence
+ off-target raw matches, **not** stamping (HF4): protabank simply holds few viral protein records
(109 genuinely_absent of 125 probed).

## HF6 — judge confidence: un-restrained `devstral:24b` cross-validation (GATED)

The v1 κ (0.13, n=43) was untrustworthy — a 4B model on 43 samples, capped by a 180s wall-clock deadline.
v2 removes the deadline + per-regime cap and uses `devstral:24b` (6× params), validating a deduped
stratified sample. **Result: accuracy 0.645, Cohen κ 0.234 at n=400** (0 LLM abstains) — up from
0.558 / 0.134 / n=43. The stronger model + 9× the sample lift κ from "slight" to **"fair" agreement**
(Landis-Koch 0.21–0.40), but it is still modest, and that is the honest read: a 24B model judging virology
relevance from title/organism text is itself an imperfect validator, so κ=0.23 reflects the LLM's own
limits as much as the automated judge's. The taxonomy-grounded automated judge (Judge A: source id ∈ NCBI
subtree) is the **more authoritative** signal — it validated correctly on spot checks
(Chikungunya-genome→relevant; West-Nile-queried-as-Chikungunya→not-relevant). Read the precision numbers as
directional-but-solid (grounded in NCBI taxonomy), with the LLM as a corroborating-not-arbitrating check.
(This session ran a bounded n=400 pass for wall-clock; the default is unbounded — the deduped set is
~7,800 pairs, re-run with `--val-cap` unset for the full pass. At ~4.5s/judgment on `devstral:24b` that is
~10 h; n=400 already gives a κ standard error of ~0.05, so the point estimate is stable.)

---

## Raw-fallback deep-dive + mitigation (the ask #3 deliverable)

**Mechanism (two paths, both precision-0.0 hazards):**
1. **Resolution-MISS fallback** (`_run_miss_envelope` → `_raw_query`): when a term doesn't resolve to a
   taxon IRI, the product runs a raw `q="<term>"` full-text query on the same index and serves those
   records with `harmonization_health="unharmonized_raw_fallback"` + a warning banner. No taxon filter is
   applied — the served corpus is whatever the text matched (HF1: precision 0.0).
2. **In-band `broken` fallback** (resolved query, `_records_from_item` harm-empty→raw): when a query
   resolves but the `subjects.valueUri` filter returns 0 while the raw leg returns some, the merge step
   serves the raw leg (verdict `broken`). HF4 shows these raw records are off-target (other organisms), so
   this fallback is a pure precision hazard with no coverage benefit.

**Mitigation strategy (layered, highest-ROI first):**
1. **Shrink the miss set at the source — DONE (HF2).** Wiring acronym/NER expansion into resolution moved
   the entire DENV/LASV/… class out of the raw fallback. This was the biggest lever and is now shipped.
2. **Genuine group-term misses (the residual 11 terms):** these have no single species taxon, so the raw
   fallback is legitimately taxon-imprecise. **Proposal (separate /feature):** tag each raw-fallback record
   `taxon_verified: false` in the bundle so a downstream consumer never counts it as organism-confirmed —
   today only a prose banner says so, and the precision-0.0 shows consumers still treat these as relevant.
   A structural flag beats a banner. (Design proposal; not built here — this eval is read-only.)
3. **`broken` in-band fallback:** since HF4 shows these are off-target (not fixable by re-stamping), the
   right move is to **not serve** the off-target raw leg on a `broken` verdict for a resolved query — or
   serve it behind the same `taxon_verified: false` flag. Also a separate /feature.

## Caveats (now mitigated; residual disclosed)

- **Full-corpus recall.** At fetch depth 10000 (production's `_MAX_RECORDS`), **1182 of 1206 cells
  (98%) are fully enumerated** — both legs = the whole corpus, so recall is TRUE full-corpus recall and
  precision is sampled from the entire served set (the v1 pool@1500 + head-sampling caveats are gone for
  these). **24 cells are `capped`** (all `bvbrc_genome`, corpus > 10k — no API pages past the Globus offset
  ceiling); for those, recall is recall@10k and precision is over the relevance head. A heavy-index
  aggregate blends the two — read `bvbrc_genome`'s headline as blended, not pure full-corpus.
- **Judge-A partial-independence** (single-stamp harmonized records) is unchanged from v1: Judge A is
  fully independent on the `raw_substitution` / off-target cells where the precision story lives; the
  residual is covered by the text-only Judge B and the `devstral:24b` validation (HF6).
- **`genuinely_absent` vs stale-dict.** The 376 genuinely_absent cells carry the product's own
  `zero_floor_unclear` caveat — a stale canonical label can look identical to a real absence when the raw
  query term also misses. HF4's `offtarget_raw_match`/`missing_source_id` split is precise; `genuinely_absent`
  is the honest residual.

## Dictionary (the "rebuild anyway" outcome)

Per the user's request, a fresh dictionary was rebuilt from taxdump + VIOLIN + BV-BRC. It is **NOT
swapped in**: the rebuild via the build workflow produces a *base* dict (324k inverse-index rows) that is
**4× smaller than the deployed `multiclade-species-2026-06-09` dict** (1.34M rows) — the deployed dict
carries multi-day SC-A/SC-B mining + strain→species enrichments a single build-workflow run does not
replay. Both resolve every species this eval touches (identical IRIs), so the eval ran on the
coverage-superior deployed dict. The fresh base dict's only delta is a newer NCBITaxon snapshot, which
changes no species IRI here.

## Actionable follow-ups (each a SEPARATE /feature — out of this read-only eval's scope)

1. **HF1/raw-fallback (mitigation #2/#3):** structural `taxon_verified: false` flag on raw-fallback records
   + stop serving the off-target `broken` in-band raw leg. The remaining precision lever now that acronyms
   are fixed.
2. **HF5 low-precision index:** audit `violin_pathogen` (0.51) stamping quality.
3. **HF6:** the JSON's `judge_agreement` carries the tightened κ; if a definitive number is wanted, re-run
   validation unbounded (`--val-cap` unset, ~7,800 pairs).
4. **Dictionary:** if a fresh NCBITaxon snapshot is ever needed, the rebuild must replay the SC-A/SC-B +
   strain→species enrichment passes, not just the base build workflow (else coverage regresses 4×).
