# viral_epitope_analysis eval — findings

Real findings surfaced by the self-refining epitope eval (`tests/eval/epitope_eval_loop.py`) over a 9-virus
set (1 loop iteration, real runs against Ollama + local data; RHEA down). Verdict: 3/6 train viruses PASS
(reason-aware graceful degrade); the gated worklist holds the real bugs below. GATED = a human/code fix;
INFORMATIONAL = environment.

## EF1 — ProtaBank is reported but NEVER retrieves (GATED) — the user's explicit ask
ProtaBank was searched + reported on ALL 7 completing-virus runs and returned **0 records every time**
(`protabank_counts` all 0). So ProtaBank is "wired in" (1 of 9 harmonized indices, filtered by
`subjects.valueUri` taxon IRI) but **reported-but-useless** — it never actually surfaces data for any
virus. Almost certainly the ProtaBank DEST index (`be999b57-…`) lacks the taxon IRIs the query filters on:
the harmonization arc published the PDB/EMDB DEST indices with taxon IRIs (+ UniProt alt-ids), but the
ProtaBank records (UniProt-keyed) were never stamped with taxon IRIs via the UniProt→PDB bridge. So
`data_readiness` perpetually names it "no protabank record" — a coverage gap that's never closed.
**Fix (gated, NOT a unilateral production write):** verify the ProtaBank DEST index has `subjects.valueUri`
taxon IRIs (probe it); if not, the UniProt→PDB→taxon bridge ingest must stamp them. Until then ProtaBank
contributes nothing to any epitope analysis despite being searched on every run.

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

## What PASSED (the eval is not just a bug-finder)
chikungunya / dengue / Zika (bare-virus) PASS all 5 reason-aware checks: every step streams + completes,
the structural + literature artifacts are non-empty, the report carries the 5-LLM + deterministic sections
with real citations (89 markers on chikungunya), and ProtaBank is reported (0, but reported — not silently
dropped). The empty conserved_regions + missing figures are correctly EXCUSED by the sequence-conservation
proceed-note (honest degrade, no protein) — NOT flagged as bugs.
