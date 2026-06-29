# Fresh-install (packaging / delivery) validation — findings + backlog

Harness: `scripts/validate_fresh_install.py` (builds the wheel, validates the DELIVERED package).
Companion to `docs/workflow_boundary_findings.md` (which validates BEHAVIOR via the editable dev tree).

## Why this loop exists

The boundary loop runs `PYTHONPATH=src` — the editable source tree — so every packaged-resource path
resolves and a **wheel-delivery gap is invisible**. That blind spot shipped a real bug: the PyMOL
build context (`docker/pymol/_pymol_job.py`) lived OUTSIDE `src/` and was absent from the wheel, so a
`uv tool install` user hit `FileNotFoundError` while **every dev test passed**. The loop validated the
product's behavior, never its delivery.

This harness closes that gap: it builds the wheel and asserts the load-bearing, module-relative
resources are actually inside it AND resolve in the install layout, then (in `--full`) does a real
`uv tool install` + `apecx-setup --non-interactive` + the C1-C6 boundary contract from the DELIVERED
venv (not `PYTHONPATH=src`).

## Tiers
- **default (deterministic, no network — CI-safe):** build wheel → delivery manifest → install-layout
  resolution → entry points → package-data globs match.
- **--full (env-gated, heavy):** real `uv tool install` into an isolated dir → `apecx-setup
  --non-interactive` → boundary e2e from the delivered venv. Deps are git URLs (nanobrain,
  apecx-harvesters) so the README install is genuinely resolvable; the dict (~735MB) is skipped via
  `APECX_SKIP_DICT_BUILD=1`.

## The harness's OWN blind spot (caught in review) → the derived delivery gate

The first cut of this harness used a **hand-maintained manifest** of load-bearing resources. The
review-gate correctly flagged that as *false confidence*: a hand-list only catches a REGRESSION of a
resource someone already listed — it would NOT have caught the original PyMOL bug prospectively, and
it printed "ALL CLEAR" while `seqtest.fasta` (a bundled workflow default, read by
`fasta_collection_step`) was absent from the wheel — the IDENTICAL FileNotFoundError class.

Fix: the primary gate is now **derived from the source tree** — `check_all_resources_ship` asserts
that EVERY non-`.py` resource under `apecx_integration/` ships in the wheel (minus a small, explicit,
justified dev-only denylist: `*.example`). Switching to the derived gate immediately surfaced THREE
un-shipped resources the hand-list missed: `composition/workflows/rhea_muscle_alignment/data/seqtest.fasta`,
`_alembic/migrations/script.py.mako`, `_alembic/migrations/README`. All three are now shipped
(pyproject `**/*.fasta` + `_alembic/**/*`); the harness verifies **all 198** non-`.py` resources ship.
Lesson recorded: a packaging loop must DERIVE its contract from the code, never hand-list it.

## Scorecard (this branch — atop the PyMOL packaging fix)
- **default tier: ALL CLEAR.** The delivered wheel carries every load-bearing resource
  (`_pymol_container/{Dockerfile,_pymol_job.py}`, `_pymol_sasa.py`, `composer_config.yml`, alembic
  versions, the viral_epitope_analysis workflow) + all four entry points (apecx-mcp/cp/setup/globus).
- **Regression proof (the bug this loop was built to catch):** the inverted self-test injects the
  PRE-FIX PyMOL location (`docker/pymol/_pymol_job.py`, outside `src/`) into the manifest and the
  harness correctly reports it **MISSING from wheel** — i.e. on `origin/main` before the PyMOL fix
  this loop would have FAILED, exactly the `uv tool install` user's crash.
- Benign: the `**/*.yaml` package-data glob matches nothing (the repo uses `.yml`) — a WARNING, not a
  failure (intentional future-proofing).

## --full / --e2e tier results (real fresh install, recorded)
- `uv tool install --from <checkout>`: **✅** (deps resolve from git: nanobrain @ academy-integration,
  apecx-harvesters @ main).
- `apecx-mcp --help` / `apecx-setup --help`: **✅ rc=0**.
- `apecx-setup --non-interactive` (APECX_SKIP_DICT_BUILD=1): **✅ rc=0**. The DELIVERED package's verify
  shows **no data/violin rows** (the cleanup shipped in the wheel) and every optional honest-skips
  (globus/data/rag/rhea/pymol).
- **`--e2e` (the real end-to-end): workflow ran from the delivered `uv tool` venv → C1 status `ok`,
  all 13 stages.** Decisive: `structural_reasoning available=True, n_exposed=31` — the PyMOL job script
  was FOUND (packaged), the image ran, real SASA computed. **The FileNotFoundError is gone end-to-end
  in a real install** — the exact regression this whole packaging/delivery arc was about. Conservation
  also ran (`available=True, n_conserved_regions=47`). Only degrade: RHEA (`available=None`, honest
  "NOT available" — the optional additive MUSCLE leg; not a crash).
- **Caveat that confirms backlog #1:** the conservation leg ran only because THIS host has the `mafft`
  binary installed. A truly clean env (no host `mafft`) would fail it — exactly why MAFFT must become
  a self-provisioning container (next arc). This harness's e2e on a mafft-less host would surface it.

## Setup-validation cleanup (server-side / no-local-files), shipped this arc
- `cli/setup.py::_step_verify` no longer checks or lists local BV-BRC/VIOLIN CSVs, and the `optional`
  set drops `data`/`violin`. harmonized_search uses the public Globus index anonymously and the
  primary workflow pulls data over the network; the `query_*` DB tools degrade-loud at call time.
- `mcp_surface/server.py` no longer runs the `_check_data_root_or_warn()` startup banner (it told a
  no-local-data install it was "missing" something it does not need — misleading under Globus-first).
- Regression pin: `tests/unit/test_verify_no_local_data.py`.

## Integrated end-to-end proof (2026-06-27, origin/main c41c4f3)

The full desktop chain was validated against REAL data + REAL containers (not a unit harness):
`viral_epitope_analysis` for chikungunya E1 (taxon 37124) in **desktop** locus →

- status `ok`, 32 KB markdown report; artifacts on disk: `figures/2XFB.png` (PyMOL surface render),
  `figures/conservation_37124_E1_*.png` + `.pdf` (conservation plot), report / structured_data /
  tool_output. Both the MAFFT (sequence conservation) and PyMOL (structural SASA) legs ran in their
  SELF-PROVISIONED containers (`apecx-mafft:7.505`, `apecx-pymol:3.1.0`) — no host bio-tool install.
- `eo_primitives.maybe_desktop_payload(result, ctx)` → a 4-item content list → REAL FastMCP
  `_convert_to_content` → **2 `ImageContent` blocks** (both `image/png`, both with base64 data —
  the surface render + the conservation PNG; the vector PDF is correctly NOT inlined) + the host
  instructions TextContent + a lean 7.5 KB structured-data block.

**Diverse-input confirmation (the "test diverse inputs, not one example" lesson).** Re-ran the same
e2e across THREE viruses spanning light→heavy + three different viral folds — to flush the silent
timeout-truncation that once hit heavy viruses. All `status: ok`, conservation + structural legs both
ran with ZERO degrade/unavailable notes, each a different real PDB structure, each 2 base64
`ImageContent` blocks:

| Virus | Taxon | Protein | Wall time | Structure |
|---|---|---|---|---|
| Chikungunya (alphavirus) | 37124 | E1 | (baseline) | 2XFB |
| Dengue (flavivirus) | 12637 | envelope E | 182 s | 5N0A |
| Influenza A (orthomyxovirus) | 11320 | hemagglutinin HA | 227 s | 3GBN |

The heavy-virus silent-degrade regression is NOT present even for the most heavily-sequenced virus
(influenza). (Script: `scratchpad/e2e_diverse.py <taxon> <protein> "<query>"`.)

**Edge-case e2e CAUGHT A REAL SILENT BUG (2026-06-27).** A 4th run — **Mayaro nsP1 (taxon 59301)**, a
virus/protein with NO taxon-matched PDB structure — surfaced a wrong-structure bug: `StructuralEvidenceStep`
fell back to an UNRELATED organism's free-text hit (an influenza HA `3GBN`) and rendered + PyMOL-SASA-computed
it AS IF it were Mayaro structural evidence (silent false evidence, worse than a crash; the "not taxon-locked"
warning was logged but the hit was still used). Root cause: `structural_query.search_one_source` returns
`note=None` ONLY on the taxon-locked path; every `_free_text_degrade` sets a note + carries unreliable
free-text hits. **Fix** (StructuralEvidenceStep): include a source's hits in `structural_records` only when
`result.note is None`; drop non-taxon-locked hits + name the specific degrade. After the fix the Mayaro run
correctly produces **0** structural figures (3GBN gone), 1 ImageContent (conservation only), and an honest
"Structural-level reasoning unavailable: No loadable PDB structure" degrade — `status: ok`, no crash.
Regression test: `tests/unit/test_structural_evidence_step.py::test_non_taxon_locked_hits_are_dropped_not_rendered`.
The sibling `harmonized_search` call site is correct as-is (it SHOWS such hits with a visible caveat — a human
reads it; only the auto-render workflow leg needed the drop).

**Parallel-path check (the "no match → wrong match" bug CLASS).** The conservation leg has an analogous
too-few-sequences fallback (`bvbrc_protein_fasta_step.py:133-167`) — but it is TAXON-SAFE by construction:
`_query_available_proteins(taxon_id, …)` + `_fetch(taxon_id, candidate, …)` hold `taxon_id` constant, so it
substitutes a different PROTEIN OF THE SAME VIRUS, never a different taxon. The structural bug was unique to
the structural leg's free-text degrade (which dropped the taxon filter). No analogous fix needed.

**5th-virus e2e CAUGHT A SECOND BUG — SARS-CoV-2 got NO structural evidence (2026-06-27).** A SARS-CoV-2
spike (taxon 2697049) run — a 5th viral family + the post-Mayaro-fix regression check — degraded its
structural leg ("could not resolve a species … no virus name in the query text"), so SARS, one of the most
important viruses, silently got 0 structures. Root cause (NOT the Mayaro fix over-dropping): the structural
taxon-lock needs an ORGANISM NAME, which it gets from the curated `_TAXON_SPECIES` map, `resolved_species_name`,
or a "<X> virus" query phrase. SARS-CoV-2 + influenza A are named so the query parser can't resolve them, were
NOT in the 4-entry curated map, and `resolved_species_name` (the upstream BV-BRC canonical label) is not carried
in every run — influenza only worked earlier because it happened to get the label; SARS didn't. **Fix**: added
SARS-CoV-2 + influenza A to `_TAXON_SPECIES`, bridging taxon_id → the FULL PDB scientific name (facet-precise —
verified live: SARS → 16 real structures `6XEY/7VHN/8G70/7TPI`, note=None, excludes SARS-CoV-1; "influenza a
virus" → the A-variants not the 84-org A/B/C mix). Regression test
`test_structural_query_resolution.py::test_curated_bridge_resolves_sars_and_influenza_by_taxon_id`. **Deeper
follow-up — ✅ NOW DONE (general root-cause fix):** the general gap was `resolved_species_name` (the upstream
BV-BRC canonical_label) not being forwarded for arbitrary directly-passed taxa. The dict HAS every taxon's label
(`entries.canonical_label`, e.g. 2697049 → "Severe acute respiratory syndrome coronavirus 2"). `StructuralEvidenceStep`
now routes taxon_id → NCBITaxon IRI → `lookup_by_iri().canonical_label` (`_species_name_from_dict`, the docstring's
anticipated path) when `resolved_species_name` is absent — covering ALL dict-backed viruses, not just curated ones.
Verified live through the real step: **HIV-1 (taxon 11676, non-curated) → 16 real structures `1CE0/2NXY/1UTS`** (was
0); Ebola resolves too. Degrade-loud (any dict miss/outage → the existing named degrade). The curated `_TAXON_SPECIES`
SARS/influenza entries are now superseded on the normal path, retained only as a no-dict fallback. Regression tests
`test_species_name_from_dict_resolves_label` + `test_dict_routing_taxon_locks_arbitrary_virus`.

Conclusion: the PyMOL render + SASA conservation plot **actually reach the host LLM as inline images
after re-ingestion** — the loop's core directive, proven on real data. (Script:
`scratchpad/e2e_reingestion.py`; this is a manual e2e gated by Docker + network + the dict.)

## Scientific-protein-name probes (2026-06-27) — entity recognition + decomposition

Probed `viral_epitope_analysis` with SCIENTIFIC protein names (protease/kinase/neuraminidase/
glycoprotein, not abbreviations), cross-referenced with the BV-BRC schema (`genome_feature
select(product)`) + web search.

- **REAL BUG FIXED — structural leg analyzed the WRONG protein.** With an explicit protein, the
  structural record selector (`structural_reasoning_step.rank_structural_records`) let the
  surface-antigen vocabulary OVERRIDE the requested protein: `_PROTEIN_WEIGHT=5.0` (one term) lost to
  surface keywords ACCUMULATING at 2.0 each — so a "neuraminidase" query selected hemagglutinin
  (`3GBN`, 8 surface keywords = 16 > 5) and a "main protease" query selected spike (`6WPS`), then
  mapped the (correct-protein) conserved residues onto the WRONG structure → "all buried" garbage SASA.
  Fix: `_PROTEIN_WEIGHT=100.0` (must exceed `len(_SURFACE_KEYWORDS)*_SURFACE_WEIGHT`) so an explicit
  protein is categorically dominant; surface/internal vocab is now only a tie-breaker / the sole signal
  when no protein is given. Verified on the real influenza corpus + a synthetic repro; preserves the
  no-protein heuristic and the original CHIKV E1-vs-capsid-protease case. Regression
  `test_ranking_explicit_protein_dominates_famous_surface_antigen`. **CONFIRMED e2e + web-cross-referenced
  on the real broad corpus:** influenza "neuraminidase" now selects `8G3P` (N2 neuraminidase + FNI9 Fab —
  was `3GBN` hemagglutinin) from the SAME 776 candidates; HIV-1 "protease" selects `3OU1` (MDR769 HIV-1
  protease — protein-dominance correctly overcame the internal-protein penalty). Both BV-BRC-matched (no
  substitution), so conservation also ran on the correct protein. Figure-mislabel residual fixed
  separately (781cbda: alignment_viz labels with `substituted_protein`).
- **By-design (NOT bugs):** `protein` is an optional CALLER param (the host LLM passes it), so query-ONLY
  conservation degrading "no protein/antigen name on the query" is correct; the spike pick under
  query-only was just the no-protein heuristic.
- **Honest LIMITATION (BV-BRC annotation granularity):** polyprotein viruses (corona/flavi) annotate
  ONLY the polyprotein, so mature scientific names can't match BV-BRC `product` — SARS-CoV-2 "main
  protease" / dengue "NS3 protease" (and "spike" ≠ "surface glycoprotein") get the too-few-sequences
  SUBSTITUTION (loud "Low confidence — auto-substituted 'surface glycoprotein'" caveat ✓). Segmented /
  separate-gene viruses match cleanly (influenza `neuraminidase`, HIV-1 `protease`, HSV-1 `thymidine
  kinase`). The figure-mislabel residual was FIXED (781cbda).
- **REAL BUG FIXED — junk substitute on a poorly-annotated taxon (kinase probe).** "herpes simplex
  virus thymidine kinase" is AMBIGUOUS (HSV-1 vs HSV-2): the dict correctly MISSES the bare name
  (it has "HSV-1"/"herpes simplex virus 1" → taxon 10298, well-annotated, 18 TK seqs), so the
  LLM-fallback picked taxon 126283 "Herpes simplex virus unknown type" (poorly annotated, 12 CDS) —
  where "thymidine kinase" has <2 seqs, so the too-few-sequences fallback auto-substituted "**unnamed
  protein product**" (a generic catch-all → meaningless conservation). Fix: `_is_informative_product`
  gate so the substitution NEVER picks a generic name (unnamed/hypothetical/uncharacterized/predicted/
  putative-protein/"protein"/"product"); if no SPECIFIC alternate has ≥2 seqs it degrades loud ("no
  informative alternate") instead. Regression `tests/unit/test_bvbrc_product_filter.py`.
- **FEATURE — ambiguous request → CLARIFICATION (needs_input), not a silent guess (2026-06-27).** The
  earlier "defensible" behavior (resolve ambiguous "herpes simplex virus" to the "unknown type" taxon)
  was UPGRADED per user request: when the taxon fallback lands only on a non-specific UMBRELLA taxon,
  the workflow now returns `status=needs_input` with an `ambiguous_entity` ControlTransfer asking the
  host LLM to specify the organism (HSV-1 vs HSV-2) or a taxon_id — instead of analyzing a poorly-
  defined taxon. Wiring: `taxon_candidate_review_step._is_underspecified_taxon` (unknown/unclassified/
  unidentified/unspecified/untyped/sp.) + `_needs_clarification` sets `bundle["control_transfer"]` and
  marks a miss (legs fast-degrade); the control_transfer rides `gate.control_in` ← taxon_review (the
  direct link, epitope builder ~L557), and `design_gate_step` FORWARDS it → the terminal EnvelopeStep
  emits needs_input. CONFIRMED e2e: HSV thymidine kinase → `status=needs_input`, reason
  `ambiguous_entity`, message naming the under-specified taxon + HSV-1/HSV-2. Regressions
  `test_underspecified_taxon_requests_clarification`, `test_forwards_upstream_clarification_control_transfer`.
  VALIDATED: SARS-CoV-2 spike (specific, dict-resolved) stays `status=ok` — NO false trigger (the
  detector fires ONLY on the LLM-fallback umbrella-taxon path; dict hits bypass it). SCOPE LIMIT
  (documented, not a regression): the detector is NAME-based (umbrella markers), so it catches the
  HSV-class ("...unknown type" taxon) — see the two follow-ups now SHIPPED below.
- **FOLLOW-UP 1 — pollution cleanup (2026-06-27, 12696e4).** Candidate generation
  (`bvbrc_taxonomy_search_step`) searched BV-BRC `eq(taxon_name,<synonym>)` which is Solr
  keyword-matched, so a short synonym ("HSV"/"HHV") surfaced NON-viral taxa whose names merely contain
  the token — plants ("Radula sp. HSV18846"), synthetic ("Expression vector …/HSV1 tk"), environmental
  bacteria. Fix: server-side `eq(division,Viruses)` → only real viruses enter the candidate list.
  Regression `test_query_constrains_to_viral_division`.
- **FOLLOW-UP 2 — broadened ambiguity (2026-06-27).** The candidate now carries its SPECIES-rank
  taxon_id + lineage (from the BV-BRC taxonomy lineage). `taxon_candidate_review_step` drops nested
  ancestors (`_most_specific` — a genus + its OWN clade like Norovirus + Norovirus GII collapses, so
  the coverage-max pick is preserved) and, if the LLM-confirmed candidates span >1 distinct SIBLING
  viral species, returns a clarification LISTING the species (`_needs_clarification_multi`). Safe: SARS
  stays `ok` (no false trigger); norovirus genus+clade still picks GII. Regression
  `test_multiple_distinct_species_requests_clarification`. REMAINING limit (honest): this catches
  TAXONOMIC sibling ambiguity, NOT SYNDROME ambiguity — bare "hepatitis virus" spans UNRELATED viral
  families (HBV hepadnavirus / HCV flavivirus / HEV hepevirus), which the review LLM correctly does NOT
  confirm as "the same virus", so they aren't flagged as siblings (the candidate-gen DOES surface all
  three; the gap is the "same-virus" confirmation).
- **SYNDROME ambiguity — family-spread discriminator is UNSAFE (2026-06-28 NO-GO), shipped the SAFE
  alternative instead.** Feasibility (gated before building): measured whether "candidates span ≥2
  distinct FAMILY-rank lineages" cleanly separates a syndrome term from a specific virus. It does NOT —
  the real nemotron synonym step emits SYNDROME synonyms for SPECIFIC hemorrhagic-fever viruses
  (Crimean-Congo HF → "hemorrhagic fever virus", "fever virus"; Rift Valley fever → "Valley fever
  virus", "fever virus"; Lassa → "Lassa fever virus"), which keyword-match `eq(taxon_name)` across
  families → a SPECIFIC-virus query would false-trigger. Also, the synonym step silently collapses bare
  "hepatitis virus" → Hepatitis B, so family-spread wouldn't even help it. So family-spread was NOT
  built. SHIPPED instead: a curated **bare-syndrome-term** clarification in `taxon_candidate_review_step`
  (`_syndrome_category` / `_needs_clarification_syndrome`) — a stopword-anchored regex
  (hepatitis | encephalitis | (viral) h(a)emorrhagic fever | respiratory | gastroenteritis) `+ virus`
  → `needs_input` with per-category examples, instead of the LLM fallback silently picking one member.
  SAFE by construction: only reached on a dict MISS (specific viruses — JEV/HBV/CCHF/RVF/TBEV/RSV —
  dict-resolve + short-circuit before this step), and the stopword anchor means a QUALIFIED name never
  matches (verified: "Japanese encephalitis virus"/"Crimean-Congo hemorrhagic fever virus"/"Hepatitis B
  virus" → None). Regressions `test_bare_syndrome_term_requests_clarification` +
  `test_qualified_disease_name_not_flagged_as_syndrome`.
- **BUG — virus-name extraction captured leading article / sentence context (2026-06-27 alphavirus
  probe).** `extract_virus_names` (taxonomy_resolver.py) used a greedy ≤4-word "<X> virus" phrase
  window, so natural phrasing "...epitopes on the Eastern equine encephalitis virus" returned "the
  Eastern equine encephalitis virus" (and a 1-word name like "...on the Sindbis virus" returned
  "epitopes on the Sindbis"). Both MISS the article-free dict key → `resolved_taxon_id=null` → the
  whole workflow degraded (no conservation, no taxon-locked structures) for EVERY NON-ALIASED virus in
  natural phrasing. ALIASED viruses (CHIKV/dengue/WNV, alias table consulted first) MASKED it — why
  earlier probes passed. Found ONLY by probing alphaviruses (none aliased) with full natural-language
  queries; the cheap resolution probe missed it because it passed clean "Virus protein" strings that
  bypass extract_virus_names. FIX: drop a leading run of articles/prepositions, then emit each trailing
  SUFFIX (longest-first) so the most-specific real "<name> virus" resolves (first-resolving-wins
  contract). VERIFIED e2e: EEEV "...on the Eastern equine encephalitis virus E2 glycoprotein" went from
  resolved_taxon_id=null + 0 structures → 11021 + PDB 8XI5. Resolution confirmed for EEEV/WEEV/VEEV/
  Sindbis/Ross River/O'nyong-nyong/Barmah/Getah/Madariaga/Una/Whataroa (entity recognition +
  decomposition 12/12); CHIKV alias regression intact. Regression `tests/unit/test_extract_virus_names.py`.
  Residual edge (resolves correctly, primary candidate suboptimal): a 1-word name with a content-word in
  the 4-window (e.g. "epitopes on the Sindbis") keeps a junk names[0] but still resolves via a suffix —
  assessed NON-harmful (epitope_resolve_step loops candidates and sets `term` to the first that RESOLVES;
  PubMed OR-anchors all candidates), so NOT fixed.
- **BUG — abbreviated name with a period dropped (2026-06-28, 7a50e97).** The phrase-window char class
  was `[a-z0-9'-]` (no period), so "St. Louis encephalitis virus" extracted as "Louis encephalitis
  virus" → MISS (dict key IS "St. Louis encephalitis virus", BV-BRC taxon 11080). Fix: add `.` to the
  class. Regression in `test_extract_virus_names.py`.
- **BUG — bare acronyms produced NO extraction candidate (2026-06-28, bb41784).** `extract_virus_names`
  returned [] for an acronym-only query ("...on the LASV glycoprotein") — no alias, no "<X> virus"
  phrase, no suffix — so the workflow MISSED, even for acronyms the dict knows (EEEV/HPV/JEV resolve via
  direct lookup but were never emitted). Fix: 18 UNAMBIGUOUS acronym→canonical alias entries
  (EEEV/WEEV/VEEV/ONNV/RRV/LASV/MARV/NiV/RABV/HTNV/RVFV/CCHFV/JEV/TBEV/YFV/VARV/HPV/MPXV; SFV+HEV omitted
  as ambiguous). e2e: LASV→3052310 + structure. \b boundaries verified to avoid in-word false matches.
- **NOT bugs (assessed 2026-06-28, defensible behavior, no change):** (a) serotype/subtype queries
  resolve to the parent SPECIES (dengue serotype 2 / DENV-2 → Dengue virus 12637; HPV type 16 → Human
  papillomavirus) — species-level conservation captures pan-serotype conserved epitopes; serotype-specific
  resolution is a possible future ENHANCEMENT, not a fix. (b) multi-virus queries ("shared epitopes
  between Zika and dengue") extract BOTH viruses correctly but the workflow resolves the first —
  viral_epitope_analysis is single-virus BY DESIGN; cross-virus comparison is a feature, not a bug.
  Entity-recognition probing has hit diminishing returns for clear bugs (path now robust: aliases +
  acronyms + phrase + suffix + period + article-strip + suffix-emission).
- **POLYPROTEIN mature-peptide conservation — partial fix shipped, normalization is the follow-up
  (2026-06-29).** A mature protein of a polyprotein virus (alphavirus capsid/E1/E2/6K, flavivirus
  NS3/NS5/envelope, coronavirus nsps) is annotated in BV-BRC as a `mat_peptide` feature, NOT a CDS (the
  CDS is the whole polyprotein) — so the conservation leg's CDS fetch finds <2 sequences and SUBSTITUTES
  a different product (the structural leg still works via PDB). mat_peptide is ABUNDANT (verified via the
  CORRECT BV-BRC query — my first probes gave false zeros: `-G --data-urlencode` malforms BV-BRC RQL,
  and `-I` HEAD is unreliable; use a raw-URL GET, encode spaces as %20): EEEV E2 1396, EEEV capsid 1212,
  Sindbis E1 501, WNV NS3 2000, Zika NS5 2000, dengue NS5 63. SHIPPED (`bvbrc_protein_fasta_step`): when
  CDS yields <2, retry the SAME protein as `mat_peptide` BEFORE substituting — real per-mature-protein
  conservation when the request NAME substring-matches the BV-BRC product. Real e2e: EEEV "capsid
  protein" → `feature_type=mat_peptide`, 50 sequences, no substitution (was: substitute). Zero
  regression (only the existing <2 path). Tests `test_bvbrc_mat_peptide_retry.py`. REMAINING (feature-
  scale follow-up, NOT done): protein-NAME NORMALIZATION — a short/variant request name that is NOT a
  substring of the verbose BV-BRC product still misses ("E2 glycoprotein" vs "E2 envelope glycoprotein";
  "NS3" vs "nonstructural protein 3"; "6K membrane protein" multi-space match → 0). This needs a
  token/synonym protein-name resolver (analogous to the virus alias table) + a relaxed token-subset
  match, with care to avoid aligning the wrong protein (the existing word-boundary filter guards against
  'structural' ⊂ 'nonstructural'). Recommended as its own `/feature` flow, not an autonomous-loop edit.

## Backlog (next, loop-driven)
1. **MAFFT self-provisioning (container-only).** ✅ DONE (origin/main c41c4f3) — `_mafft_container/` +
   `apecx-mafft:7.505` built on first use via `ensure_docker_image_built`; the host
   `shutil.which('mafft')` path is removed; degrade-loud without Docker. Verified by the integrated
   e2e above (a mafft-less host now self-provisions instead of failing the conservation leg).
2. **Wire the harness into the loop cadence** — run the default tier alongside the boundary loop on
   every packaging-affecting change; run `--full` before a release.
3. Consider extending the manifest as new module-relative resources are added (the manifest is the
   delivery contract; a new `Path(__file__).parent / <data>` resource must be added here + to
   package-data).
