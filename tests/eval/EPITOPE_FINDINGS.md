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
taxids the `11320` IRI doesn't cover (**but the `1001772`/`11309` samples first cited here were a MISREAD —
Influenza B / unidentified influenza; see the CORRECTED root-cause below**).
So "884 bvbrc_protein records" looked like influenza-specific taxon hits but were un-taxon-filtered free-text
matches (could include wrong organisms) — the user was never told. VERIFIED real (probed the live indices).

**Fix (shipped, safe, NO production write):** thread the per-index verdict into
`harmonized_search_summary.per_index_health` (`HarmonizedBundleMergeStep._health_from_item`) → `DataReadinessStep`
discloses `"<index>: N record(s) via taxon-IMPRECISE raw free-text (taxon-harmonization broken, not
taxon-filtered)"`. The eval surfaces the count (`check_harmonization_disclosed`). Unit-tested
(`test_taxon_imprecise_harmonization_disclosed`) + verified e2e (influenza report).

**Underlying cause — CORRECTED 2026-06-25 (taxonomy-version skew; my earlier analysis in this section was
WRONG, twice).** The "different lineages / rule-out the descendant filter" text was built on a MISREAD:
`1001772` is *Influenza B virus (B/Chiba/1/2005)* and `11309` is *unidentified influenza virus* — NOT
Influenza A; the species filter correctly excluded them. The real Influenza A strain taxids (e.g. `1000354`)
ARE proper descendants of the species. The verified root cause is **taxonomy-version skew**:
- The dict resolves "influenza A virus" → `11320`. After the ICTV rename, `11320 "Influenza A virus"` is now a
  SUB-species node UNDER the species `2955291 "Alphainfluenzavirus influenzae"`.
- **0** of 24,902 bvbrc_protein records carry `11320`. Records carry strain taxids + (inconsistently — only
  884) the current species `2955291`. The dict's name→taxid snapshot differs from the records' taxon-stamp
  snapshot. Chikungunya works ONLY because its query taxid `37124` happens to be exactly what its records carry.
- A `taxon_species` strain→species map WAS built (nanobrain `TaxonSpeciesMapStep` on a dict COPY;
  `species_iri_for(1000354)==2955291`) — kept as the prerequisite for the real fixes.

**Why a one-shot re-ingest does NOT fix it (the 2026-06-25 plan was invalidated at its hard gate):** the
harvester's Pass 3 stamps the species *ancestor* (`2955291`), but the query resolves to `11320` (a node BELOW
the species), so species-stamping never yields `11320`, and stamping `2955291` everywhere still wouldn't match
an `11320` query without ALSO normalizing the query side. The three real fixes — (A) full-lineage re-publish
[harvester code change + write], (B) consumer query-normalize-to-species + species re-publish, (C) pure
consumer facet-descendant-expansion [no write] — are all non-trivial. **EF4 is PARKED (2026-06-25)** pending a
taxonomy-alignment decision; NO production write was made (live dict + indices untouched). The EF4 disclosure
(above) remains the correct interim — and is now MORE valuable, since the skew is deeper than first thought.

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
  it needs the design owner's call** (don't flip it silently). **Fix is a small change** —
  `RheaMuscleAlignStep` (`rhea_muscle_align_step.py`) ALREADY has a local-MAFFT code path (`viral_conserved_sites`
  chose RHEA-muscle *over* it); on Rhea-unreachable, fall back to that path + emit a loud proceed_note instead
  of `raise`. BUT the step's docstring explicitly states *"NO silent degradation: if the Rhea server is
  unreachable, the rhea [step] raises"* — the author considered degradation and chose fail-closed. A LOUD
  degrade satisfies the "no SILENT failures" rationale; whether to override the author's explicit choice is
  the design owner's decision. NOT changed here (deliberately — won't reverse a documented design unilaterally).
- **Test hygiene (separable, safe):** these chain tests gate only on `@needs_globus`, not RHEA — so they
  false-RED in any no-RHEA env instead of skipping like `needs_llm`/`needs_globus` tests do. A `needs_rhea`
  skip gate (probe `:3001`) fixes the false red without hiding the design fact (documented here).

## EF7 — G127 honesty check flags a DEGRADED-loud step failure as a run error (framework-level)
Surfaced building the full-artifact demo (CORRECTED root cause). With the EF5 sequence-`align` degrade
enabled + rhea forced unreachable, the run reached 23/23 but `run_workflow` returned status=error → artifacts
not persisted. NOT a product degrade bug: `RheaGenomicAnalysisStep` ALREADY degrades correctly
(`rhea_genomic_analysis_step.py:196` `except Exception` → "do NOT fail" → a proceed_note). The rhea_genomic
leg DID degrade. The error is `run_workflow`'s G127 honesty check (`eo_primitives.py:450-460`): it FAILS-LOUD
on ANY `step_failed` event (the guard against silently-swallowed step failures) — but it CANNOT distinguish
*swallowed-silently* (the real bug) from *caught-and-degraded-loud* (correct, here the inner `muscle_alignment`
step the rhea_genomic leg caught). So a correct degrade-loud is reported as a run error + blocks the artifact
write. **Fix (framework, careful):** teach G127 to ignore a step_failed that a degrade-loud handler caught
(e.g. a proceed_note for that stage), OR have the rhea_genomic inner muscle step degrade so it emits no
step_failed. **RESOLVED (apecx 6fe307e, review-gate PASS-WITH-NOTES):** `run_workflow`'s G127 check is now
nesting-aware (`_flagged_step_failures`): a TOP-LEVEL failure always counts; a NESTED-only failure counts
only when no envelope was produced (stalled); a caught nested failure with a produced envelope is honest
degradation, not flagged. `top_level` is derived from BOTH child_steps step_ids AND child `.name` so the
step_id==name precondition is not load-bearing. The eval's `check_completeness` was aligned to match (only
top-level failures fail it). **VERIFIED e2e:** the full demo (rhea forced unreachable + the EF5 sequence-leg
degrade) now returns **status=ok** with the FULL artifact dir persisted — **conserved_regions=5**,
**conservation figures (PNG + vector PDF)**, **19 files**. The layered rhea-coupling demo is unblocked.

## EF8 — the cascade timeout (600s default) is too short for the full RHEA-MUSCLE pipeline
The REAL-RHEA demo (RHEA up, healthy) hit `cascade_timeout` at 13/23 — the first-run RHEA MUSCLE builds a
conda env (~50s) + the distributed alignment + 23 steps exceeded the catalog entry's explicit
`timeout_seconds: 600.0`. **RESOLVED (catalog tune):** raised viral_epitope_analysis's `timeout_seconds`
600→1200 in `mcp_workflow_catalog.yml` (verified load_catalog returns 1200, settle_ms:3000 preserved) — the
RHEA-MUSCLE path gets headroom; the MAFFT-degrade + no-protein paths finish well under it. Also surfaced a
fragile-infra conflict:
run_epitope's own InfraOrchestrator SIGTERM'd the apecx-mcp-spawned rhea-server on exit (two managers of the
shared :3001 server) — a demo-harness issue, not a product bug. **Net:** the full-artifact (figures) demo is
blocked by the product's LAYERED rhea-coupling (EF5+EF7) + the RHEA-path timeout (EF8) — itself the
adoption-risk story; a clean run needs RHEA fully up + a raised timeout, or the degrade extended to the
rhea_genomic leg. RHEA bring-up itself WORKED (container healthy on :3001, MUSCLE leg ran to 13/23).

## What PASSED (the eval is not just a bug-finder)
chikungunya / dengue / Zika (bare-virus) PASS all 5 reason-aware checks: every step streams + completes,
the structural + literature artifacts are non-empty, the report carries the 5-LLM + deterministic sections
with real citations (89 markers on chikungunya), and ProtaBank is reported (0, but reported — not silently
dropped). The empty conserved_regions + missing figures are correctly EXCUSED by the sequence-conservation
proceed-note (honest degrade, no protein) — NOT flagged as bugs.
