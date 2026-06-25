# viral_epitope_analysis eval — findings

Real findings surfaced by the self-refining epitope eval (`tests/eval/epitope_eval_loop.py`) over a 9-virus
set (1 loop iteration, real runs against Ollama + local data; RHEA down). Verdict: 3/6 train viruses PASS
(reason-aware graceful degrade); the gated worklist holds the real bugs below. GATED = a human/code fix;
INFORMATIONAL = environment.

## EF1 — ProtaBank "never retrieved" was a FALSE verdict, CONFOUNDED by EF2 (CORRECTED)
**Initial (WRONG) verdict:** ProtaBank returned 0 on all 7 completing viruses → `protabank_never_retrieved`,
"the taxon-IRI bridge is dead, needs a production re-ingest." **Two things were wrong, both caught by
fixing EF2 + re-running — recorded here as the lesson:**

1. **The cross-virus verdict was computed over a BIASED sample.** The 7 viruses that COMPLETED were the
   light, ProtaBank-data-less ones (chikungunya/dengue/Zika/Lassa/WNV). The heavily-published viruses that
   ACTUALLY have ProtaBank stability data (influenza A, SARS-CoV-2, HIV-1) were halting at the EF2 assemble
   timeout BEFORE the ProtaBank count was recorded (`protabank=None`, excluded from the verdict). So one bug
   (EF2) silently biased the eval's verdict about another (EF1). After the EF2 fix, **influenza A retrieves
   `protabank 1/1`** (`1 available / 1 used`) — ProtaBank DOES surface data.
2. **My index-probe led to a WRONG root cause.** I probed `be999b57` (1643 records, 0 `subjects.valueUri`)
   and concluded "the taxon-IRI filter matches nothing → stamp taxon IRIs + re-publish." But the workflow
   does NOT filter ProtaBank by taxon IRI — influenza got 1 record DESPITE 0 taxon IRIs, i.e. ProtaBank is
   retrieved by **free-text** (virus name). So the "production write to stamp taxon IRIs" was addressing a
   non-problem. The genuinely-low counts reflect ProtaBank's **sparse viral coverage** (mostly non-viral
   stability data), NOT a broken bridge. **NO production write is needed.**

**Eval lesson (the real, reusable finding):** a cross-virus aggregate verdict is only valid if the
completing sample is representative. When failed/incomplete runs are silently dropped from the denominator,
the aggregate becomes a false signal — here the data-RICH viruses were exactly the ones excluded. The eval
must surface "verdict computed over N of M viruses; K excluded (incomplete)" so a biased sample can't read
as a clean conclusion. (Corrective code change: `protabank_verdict` should require a representative sample
/ flag exclusions — see the controller.)

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
