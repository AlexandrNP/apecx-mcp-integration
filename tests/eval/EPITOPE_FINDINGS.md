# viral_epitope_analysis eval — findings

Real findings surfaced by the self-refining epitope eval (`tests/eval/epitope_eval_loop.py`) over a 9-virus
set (real runs against Ollama + local data; RHEA down). GATED = a human/code fix; INFORMATIONAL = environment.

**Status after fixes (re-run):** 4/6 train PASS + ALL held PASS, **0 gated findings**, only informational
`rhea_unavailable` (the protein probe). The first run had gated EF2 (incomplete) + a confounded
`protabank_never_retrieved`; both resolved — EF2 fixed (heavy viruses complete), EF4 disclosure makes the
taxon-imprecise harmonization VISIBLE (no longer silent), and the ProtaBank verdict is sample-aware + now
sees influenza's `1`. HONEST caveat: "green" = no SILENT failures (the mandate), NOT "retrieval is perfect" —
strain-heavy viruses still get taxon-imprecise free-text, now DISCLOSED; the underlying resolver fix
(species→strain taxid expansion) is deferred to the harmonization arc.

## EF1 — ProtaBank's harmonized (taxon-IRI) retrieval IS broken; raw free-text is an imprecise fallback
**CODE-GROUNDED (authoritative). HONEST NOTE: I mis-stated this 3× — "dead bridge" → "works via free-text,
no fix" → this. Each flip came from INFERRING the retrieval mechanism from black-box run counts. Reading the
retrieval code settled it. Lesson: read the retrieval code before claiming a retrieval bug.**

The harmonized search filters ProtaBank by `subjects.valueUri` (taxon IRI) —
`harmonized_search_execute_step.py:104` `{"field": "subjects.valueUri", "shape": "iri"}`. The ProtaBank DEST
index `be999b57` has 1643 records, **0 with `subjects.valueUri`** (probed). So the **harmonized (taxon-
correct) ProtaBank leg returns 0 for EVERY virus.** The code ALREADY diagnoses this: parity status `broken`
(line 252, "harm filter returned 0 but raw matched some") / `zero_floor_unclear` (line 293, both 0).

The small non-zero counts (influenza `1/1`) are the **raw free-text leg** (`q=canonical_label`) — taxon-
IMPRECISE (not taxon-filtered; can match wrong-organism records) AND label-FRAGILE: it matches "influenza"
but NOT SARS-CoV-2's long NCBI canonical label, so SARS-CoV-2 + HIV-1 return `protabank=0` even though
ProtaBank HAS their data (P06654 → 60 records). So the taxon-IRI bridge gap is **REAL** — my original
finding's CORE was right; my mid-arc "works via free-text, no production write needed" was the over-
correction (the free-text hits are an incidental, imprecise fallback, not correct retrieval).

**Fix (valid; harmonization-arc; needs direction — a Globus production write):** stamp the ProtaBank DEST
records with taxon IRIs via the UniProt→PDB→taxon bridge (I own `be999b57`; ~13% of records are viral). Then
the harmonized leg surfaces the correct viral ProtaBank stability data for the right taxon. This belongs to
the separate PDB/EMDB/ProtaBank harmonization arc, NOT the eval.

**Two eval improvements (real):** (1) the protabank check should read the code's PARITY STATUS for ProtaBank
(`broken`/`zero_floor_unclear`), NOT the raw count — the count is the flaky free-text fallback; the parity
status is the authoritative "taxon bridge dead" signal. (2) the cross-virus verdict must be SAMPLE-AWARE —
EF2 had biased it by excluding the data-rich heavy viruses (failed runs report `protabank=None`); now fixed
(`protabank_verdict` flags exclusions). Both still hold; only the "no fix needed" conclusion was wrong.

## EF2 — heavily-sequenced viruses don't complete the pipeline (GATED)
SARS-CoV-2 and influenza A halted at **8/23 steps** (incomplete: align_viz / assemble / clade_grouping /
clade_map / cross_clade never completed; streamed 9/23), while the lighter viruses (chikungunya, dengue,
Zika, …) completed. These two are the HEAVILY-sequenced viruses (SARS-CoV-2 ~6k genomes, influenza A huge).
The pipeline stalls partway for them — consistent with the known heavily-sequenced timeout class (a prior
finding: a too-short execution_timeout vs the inner alignment budget gives a no-envelope result for
heavily-sequenced viruses). **Fix (gated):** raise/scope the per-step timeout for the high-volume path, or
cap the genome set fed to alignment for very-high-count viruses; the workflow must COMPLETE (loud degrade)
rather than stall, for the most important viruses.

## EF3 — the protein/sequence leg requires RHEA (INFORMATIONAL, environment)
With a `protein`, the sequence-conservation `align` step is a fail-closed RheaMuscleAlignStep; RHEA down →
it raises ("rhea subworkflow produced no workflow_output. Is the Rhea server reachable?"), the run errors,
no artifacts. Classified `rhea_unavailable` (environment, informational — out of the gated worklist), the
same reason-aware distinction as the proceed-note degrades. To exercise the FULL pipeline (conserved_regions
+ figures + the full artifact count), RHEA must be up (`apecx-setup infra` + `apecx-setup rhea`). Without a
protein, the sequence leg degrades gracefully (proceed_note) and the run PASSES the reason-aware checks.

## EF4 — taxon-harmonization silently degrades to raw free-text for strain-heavy viruses (FIXED: disclosed)
The biggest silent failure, surfaced while fixing EF1's eval-side. The product COMPUTES a per-index
`harmonization_health` verdict (`broken` = taxon-IRI leg 0, raw matched some) but was DISCARDING it — the
report showed clean counts. For influenza A, the taxon-IRI filter (species `NCBITaxon_11320`) returns 0 on
**5 of 9 indices** (bvbrc_protein, protabank, violin_gene/pathogen/vaccine) because the records are keyed by
STRAIN taxids (probed: bvbrc_protein sample `1001772`; violin_pathogen `11309`; the species IRI matches 0).
So "884 bvbrc_protein records" looked like influenza-specific taxon hits but were un-taxon-filtered free-text
matches (could include wrong organisms) — the user was never told. VERIFIED real (probed the live indices).

**Fix (shipped, safe, NO production write):** thread the per-index verdict into
`harmonized_search_summary.per_index_health` (`HarmonizedBundleMergeStep._health_from_item`) → `DataReadinessStep`
discloses `"<index>: N record(s) via taxon-IMPRECISE raw free-text (taxon-harmonization broken, not
taxon-filtered)"`. The eval surfaces the count (`check_harmonization_disclosed`). Unit-tested
(`test_taxon_imprecise_harmonization_disclosed`) + verified e2e (influenza report).

**Underlying fix — the naive approach is RULED OUT (investigated, dict-grounded):** a descendant post-filter
(species → strain/child taxids via the dict's `taxon_hierarchy`, then keep raw records whose taxon is in that
set) does NOT work for influenza and would make coverage WORSE. The resolved species `11320` has 112,867
descendants, but the actual record taxids are NOT among them: `1001772` (bvbrc_protein) sits under parent
11520, `11309` (violin_pathogen) under 35324 — DIFFERENT lineages (influenza ICTV-rename churn;
`merged_taxons` has no mapping). The records use taxids incompatible with the modern species the query
resolves to. The real fix is TAXONOMY-RECONCILIATION + re-ingest (map the legacy/divergent record taxids onto
the modern species IRI, or re-stamp `subjects.valueUri` at harvest) — harmonization-arc, production-write, NOT
a consumer-side change. The EF4 disclosure is the correct interim until that lands.

## EF5 — viral_epitope_analysis HARD-FAILS without RHEA → breaks every downstream chain (adoption risk)
Surfaced by extending the reliability probe to the OTHER product workflows. 3 of their real-chain e2e tests
FAIL (`test_conserved_epitope_candidate_assessment::test_real_upstream_handle_can_chain...`,
`test_epitope_combination_real_chain::test_full_chain[dengue|mayaro]`) — ALL with the SAME upstream error:
`RheaMuscleAlignStep 'align': rhea subworkflow produced no 'workflow_output'. Is the Rhea server reachable?`
(confirmed in the log — NOT a regression from my EF2/EF4 changes). When the sequence leg RUNS (a protein is
given, or a well-covered virus), the `align` step is fail-closed RHEA-required; RHEA down → the WHOLE run
errors (status error, no artifacts) → every downstream consumer that chains off it also fails. So the entire
epitope product is **unusable without RHEA** (heavy infra) for protein/well-covered queries — a major
reliability/adoption blocker, and exactly the "diverse settings / edge cases" fragility the mandate targets.

- **The design tension:** "RHEA mandatory" was a DELIBERATE 2026-06-18 decision (degrade→raise). But MAFFT
  conservation ALREADY exists in the pipeline (the nested `viral_conserved_sites` leg); the RHEA MUSCLE leg
  is ADDITIVE (large-scale). So degrade-LOUD-to-MAFFT (a clear "RHEA unavailable — large-scale conservation
  skipped, MAFFT used" note) would satisfy "no SILENT failures" (it's loud) AND restore reliability without
  RHEA. **RECOMMENDATION: revert RHEA-mandatory to degrade-loud — but it reverses a deliberate decision, so
  it needs the design owner's call** (don't flip it silently).
- **Test hygiene (separable, safe):** these chain tests gate only on `@needs_globus`, not RHEA — so they
  false-RED in any no-RHEA env instead of skipping like `needs_llm`/`needs_globus` tests do. A `needs_rhea`
  skip gate (probe `:3001`) fixes the false red without hiding the design fact (documented here).

## What PASSED (the eval is not just a bug-finder)
chikungunya / dengue / Zika (bare-virus) PASS all 5 reason-aware checks: every step streams + completes,
the structural + literature artifacts are non-empty, the report carries the 5-LLM + deterministic sections
with real citations (89 markers on chikungunya), and ProtaBank is reported (0, but reported — not silently
dropped). The empty conserved_regions + missing figures are correctly EXCUSED by the sequence-conservation
proceed-note (honest degrade, no protein) — NOT flagged as bugs.
