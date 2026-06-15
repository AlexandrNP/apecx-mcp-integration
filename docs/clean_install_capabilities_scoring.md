# Clean-install capabilities & end-to-end scoring

**What works on a fresh install, with what infrastructure, proven by which test.**
Date: 2026-06-15. Author run: macOS, Ollama up, MAFFT present, BV-BRC + Globus
reachable, **Docker DOWN** (so this run also proves the no-Docker degrade paths).

This is the operator-facing companion to `apecx-setup capabilities` (the live
probe) — that command tells you what *your* machine can do right now; this doc
records what each mode *requires* and the test evidence that it works.

## Execution modes → infrastructure → status

| Capability | Infra required | Zero-infra? | Status (this run) | Evidence (real-data tests) |
|---|---|---|---|---|
| **Entity resolution** (synonym harmonization) | synonym dict (auto-downloads anonymously on first launch) | ✅ | ✅ available | `tests/unit/synonym_dictionary/test_loader_lookup.py` (39), lookup suite; dict resolves from default path with no env var |
| **Harmonized multi-source search** (BV-BRC, ProtaBank, PDB, EMDB, …) | **none** — anonymous public Globus index (`SearchClient()`, no creds) | ✅ | ✅ available | `test_harmonized_search_aggregate_served_live.py` (PDB/EMDB source-distinct), `test_harmonized_search_workflow.py::test_chikv_live_globus_harmonized_search` (harmonized > raw, total ≥ 1000) |
| **LLM analysis of harmonized data** (evidence reviews, 5-section synthesis) | LLM endpoint — local Ollama **or** remote `APECX_LLM_BASE_URL` | ✅ (remote endpoint needs no local infra) | ✅ available | `test_viral_epitope_evidence_review.py`: evidence-only, **5-section output contract**, structural-no-hit-named, streaming==headless, design-without-approval, design HITL loop — **12 passed, 1 skip** (see below) |
| **Sequence conservation** (MSA + per-column conservation) | `mafft` binary (no Docker) | ➖ one binary | ✅ available | `test_viral_conserved_sites_workflow.py` (5) + `test_conserved_sites_cascade.py` (2) = **7 passed**; `test_sequence_conservation_stage_e2e` |
| **Structural reasoning with real SASA** | Docker + PyMOL image (`apecx-setup pymol`) | ❌ | 🔒 locked → **LLM-only degrade** | `test_structural_no_hit_is_named_e2e` (degrade is a NAMED limitation, never silent); degrade message states the review continues LLM-only |
| **Rhea/MUSCLE bioinformatics tools** | Docker + Rhea + `RHEA_MCP_URL` (`apecx-setup rhea`) | ❌ | 🔒 locked | catalog `unavailable_hint` directs to the MAFFT path (`viral_conserved_sites`) or LLM-only analysis |
| **Domain-RAG synthesis** (FAISS) | FAISS index (`apecx-setup rag`, ~689 MB) | ❌ | opt-in | `test_rag_*` (not exercised this run) |
| **Offline local-DB tools** (`query_*`) | local CSVs (`apecx-setup data`, needs Globus creds) | ❌ | superseded by harmonized search | — |

## The zero-infra baseline is real and verified

A fresh install with **no Docker, no local data, no credentials** — only the
auto-downloaded synonym dictionary and an LLM endpoint (local or remote) — has a
fully working analysis path:

> **entity resolution → harmonized multi-source search (anonymous) → LLM analysis
> of the harmonized data, emitted in the deterministic 5-section format.**

This is proven end-to-end by `test_evidence_output_contract_five_sections_e2e`
and `test_evidence_only_e2e_has_structural_section` passing on a **no-Docker**
machine. Structural SASA and Rhea/MUSCLE are *enhancements* layered on Docker;
their absence degrades **loud and named**, never silently.

## Evidence-review e2e run (2026-06-15, full suite, no Docker)

`pytest tests/integration/test_viral_epitope_evidence_review.py` →
**12 passed, 1 skipped, 1 order-contamination false-failure (fixed)** in ~11.7 min.

- **Skipped**: `test_provenance_record_has_real_values_e2e` — needs the control
  plane; honest skip, not a failure.
- **Fixed**: `test_design_hitl_loop_e2e` failed *only in full-suite order* with
  `KeyError: 'status'`. Root cause: an earlier test runs byte-identical input
  without clearing the design-approval store, so the module-cached workflow
  deterministically SKIPS re-execution (G117) and returns the prior cached
  output rendering the *earlier* token; this test had cleared the store, so
  `approve()` couldn't find that token. **Passes in isolation (62 s).** Fix:
  added `_clear_workflow_cache()` to the test (same guard the streaming test
  already uses). **Not a product bug** — in production the store is never cleared,
  so a cached-output token stays valid.

## Cross-mode test runs (this session)

| Suite | Result |
|---|---|
| `tests/unit` (full) | 1738 passed, 4 skipped, 1 pre-existing env-only failure (`test_rhea_mcp_probe_against_live_localhost`: a stale process answers `:3001/mcp/` with HTTP 500, slipping past the test's skip gate — does not run in CI) |
| harmonized search (live, anonymous) | green |
| conserved sites (MAFFT) | 7 passed |
| evidence review (LLM analysis) | 12 passed, 1 skip, 1 fixed |

## How a user checks their own machine

```bash
apecx-setup capabilities   # feature-by-feature: available now vs locked + unlock command
apecx-setup verify         # component health; only the synonym dict is REQUIRED
```
