# Workflow MCP-tool-boundary — validation findings + fix backlog

Harness: `scripts/validate_workflow_boundary.py` (recording-ctx, real stack, C1-C6 at the tool boundary).
Baseline run: `viral_epitope_analysis` query=`influenza` (bare virus name), desktop locus.
Dump: `/tmp/wf_boundary_influenza.json`.

## Baseline scorecard (influenza, bare)
- C1 every-step-valid: **FAIL** — report carries `not available` / `n/a` placeholders.
- C2 progress-in-result: ~OK — 13 stage notifications + report has a steps section.
- C3 artifacts-in-result: **FAIL** — only local server paths (`~/.apecx/artifacts/<run>/`); **content NOT in result**.
- C4 full-report-in-result: report is 30 KB (full evidence) BUT the **narrative answer is WITHHELD** (citation gate).
- C5 every-leg-non-empty: **FAIL** — conservation + 5 downstream legs empty (see cascade).
- C6 prereq-honesty: SASA RAN here (Docker up + image pulled) — the false-negative is the image-absent case.

## Fix backlog (by impact)
1. **No-protein → conservation cascade.** `protein` is a SEPARATE workflow param (builder.py:10
   `{query, taxon_id?, protein?}`), NOT parsed from the query string. With no protein,
   `sequence_conservation` skips ("needs a protein to fetch per-strain sequences") and cascades to
   alignment_viz / clade_grouping / cross_clade_breadth / rhea_genomic_analysis / functional_validation
   — half the report empty. (NOTE: the first harness test `"chikungunya E1"` was a HARNESS BUG — it put
   the protein in the query string, not the param; FIXED — the harness now takes `virus/protein` and
   passes `protein` separately. Re-running `chikungunya/E1` to get the clean protein-given baseline.)
   Open question once the clean baseline lands: should a BARE virus name (no protein) AUTO-PICK a
   representative protein (influenza→HA, chikv→E1) so it doesn't return a half-empty report? — likely yes.
2. ✅ **FIXED (4336b1a, review-gate PASS) — Synthesis withheld in DESKTOP locus (#4) — DESIGN-vs-CODE MISMATCH.**
   Verified e2e: desktop now skips the local LLM, emits the full evidence report with a clean host-synthesis
   note (withheld=False). FOLLOW-UP (review-gate note): `rag_synthesis_step.py` is the OTHER
   `LLM_ROLE="final_synthesis"` step — still always runs the LLM in desktop; needs the same desktop-omit +
   its stale 2026-06-15 comment reconciled.
   `EvidenceReviewSynthesisStep` declares `LLM_ROLE="final_synthesis"` (should omit the apecx LLM in desktop
   per CLAUDE.md), BUT `evidence_review_synthesis_step.py:1173-1201` ALWAYS calls `synthesize_response` "in
   BOTH loci" (a 2026-06-15 change that removed the desktop scaffold because the host discarded it). So in
   desktop locus the local **nemotron-4b** runs and CAN'T satisfy the citation gate (0 distinct citations) →
   `except` at :1202 degrades to `render_evidence_fallback` → "# Answer: Narrative synthesis was withheld".
   FIX: in desktop locus, SKIP the apecx LLM (the connected host IS the synthesizer per the docs) but KEEP
   the full deterministic evidence report (the floor — NOT a stub, which was the 2026-06-15 concern) with a
   POSITIVE host-synthesis note, not a "withheld due to failure" note. In agent locus keep the current
   try-synthesize-then-degrade. This is a load-bearing change → next iteration does it with /feature rigor.
3. ✅ **FIXED (review-gate PASS-WITH-NOTES) — Artifact content not in result (#2).** `_attach_artifact` now
   builds `result["artifacts"]` — a manifest of every written file {name, path, kind}, with the per-tool
   native files (tool_outputs/*) embedding their text content (<=64KB); figures/report/data.json path-only
   (data.json would duplicate the tool_outputs). Verified e2e: real chikungunya/E1 result carries 19 artifacts
   (figure/report/structured_data/tool_output), 14 with embedded text. (residual: 64KB cap kept a local —
   single-use, not promoted to a configurable constant; f.stat() outside the inner try is benign per review.)
4. ✅ **FIXED (review-gate FAIL→addressed) — Docker probe false-negative (#3).** `_docker_available` kept the
   image-present fast path but now `docker pull`s the image once if absent (so SASA runs whenever Docker is
   up, not only when pre-pulled). review-gate caught a real bug: the 600s pull ran on the asyncio loop →
   offloaded via `await asyncio.to_thread(...)` at structural_reasoning_step.py:457 (matches the in-file
   pattern). Verified: 50 unit tests + a real-Docker bogus-image parity test (absent→pull→fail→False) +
   e2e influenza SASA still runs (C6=False, no regression). Pull-SUCCESS branch parity = TODO T-2026-06-26-01.
5. ✅ **FIXED (review-gate PASS-WITH-NOTES) — PubMed leg (#5).** Root cause: `extract_virus_names` matched
   only alias-table + spaced `<X> virus` phrases, so single-token suffix names (norovirus/ebolavirus/
   rotavirus) extracted to [] → `build_focused_term` fell back to the raw verbose query → PubMed eSearch
   ANDs every token → 0 hits. Added `_VIRUS_SUFFIX_RE` (one-word `<X>virus`) + a denylist (antivirus,
   provirus). Verified: norovirus/ebolavirus/rotavirus now extract; chikungunya/SARS unchanged; 27 resolver
   tests (3 new + a suffix-RE safety pin). e2e ✅ CONFIRMED: norovirus → "Retrieved 15 publications" (was 0).
6. **Nanobrain log flood (server hygiene).** ~95 MB/min of INFO/DEBUG ("BRUTAL TRUTH" logs) at default level —
   floods the server log, slows the run. Reduce the default verbosity.
7. ✅ **BENIGN (verified) — harmonized_index_search "Link source None" warnings.** These are the documented
   workflow-level input/output link pattern ([[nanobrain_workflow_input_link_not_dead]]): `search_in`/`search_out`
   are workflow-level DUs (builder.py:36-46) that don't map to a step in the STATIC graph → warns, but is
   required by the validator. Verified NOT degraded: norovirus searched all 9 indices (antiviraldb/epitope/
   genome/protein/protein_structure populated; violin/protabank genuinely sparse). Noise only; could be
   suppressed nanobrain-side (out of scope). No apecx change.

## Secondary findings (not the 5 user complaints; lower priority)
- **No-protein cascade (the one remaining correctness gap).** A BARE virus name (no protein param) → conservation
  skips → structural-reasoning "unavailable" (no conserved regions to map) + rhea "needs a protein". The legs
  degrade HONESTLY but the report is partial. Enhancement: auto-pick a representative protein early (before the
  conservation stage — needs an early protein source, e.g. a per-virus default or a BV-BRC protein lookup; the
  structural PDB is selected too late in the stage order). Non-trivial; needs design.
- **RHEA leg (chikungunya/E1): "ValueError: RHEA conserved-sites subworkflow produced no workflow_output".** RHEA
  is reachable (the subworkflow runs) but its backend produces no output → the apecx leg degrades-loud
  (additive, correct). This is RHEA bring-up/infra (out of scope), NOT an apecx code bug.
- **rag_synthesis_step desktop-omit (#4 follow-up).** The other final_synthesis step; review-gate flagged it
  still runs the LLM in desktop. Investigate whether the #4 desktop-omit applies (it may lack the deterministic
  floor EvidenceReviewSynthesisStep has — the divergence may be intentional).
- **Log flood (#6).** ~95 MB/min nanobrain INFO/DEBUG; server-log hygiene (set nanobrain logger → WARNING).

## Harness deepening (next)
- C5/C6 must be DATA-based (per-stage `data` from the stream's stage_reports), not regex-on-report: assert
  each leg's count (publications>0, conserved_regions>0, structures>0, SASA n_exposed>0).
- Test BOTH a bare virus name AND a virus+protein query to isolate the cascade from real failures.
- Run multiple viruses (chikungunya, dengue) — diverse inputs, not one example.
