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
| **Harmonized-data analysis — desktop/MCP** (frontier LLM reasons over tool output) | **none** — the MCP client (Claude Desktop) IS the analysis LLM | ✅ | ✅ available | architectural: the deterministic tools return data + scaffolds; the desktop LLM synthesizes. Surfaced by `list_workflows` + `apecx_capabilities` |
| **Internal-synthesis workflows — backend/headless** (`viral_epitope_analysis`, `synthesize_query`) | apecx LLM backend: local Ollama (`localhost:11434` default) **or** `APECX_LLM_BASE_URL` — **no remote default** | ❌ (needs the backend LLM configured) | ✅ available (Ollama up this run) | `test_viral_epitope_analysis.py`: evidence-only, **5-section output contract**, structural-no-hit-named, streaming==headless, design-without-approval, design HITL loop — **12 passed, 1 skip** (see below) |
| **Sequence conservation** (MSA + per-column conservation) | `mafft` binary (no Docker) | ➖ one binary | ✅ available | `test_viral_conserved_sites_workflow.py` (5) + `test_conserved_sites_cascade.py` (2) = **7 passed**; `test_sequence_conservation_stage_e2e` |
| **Structural reasoning with real SASA** | Docker + PyMOL image (`apecx-setup pymol`) | ❌ | 🔒 locked → **LLM-only degrade** | `test_structural_no_hit_is_named_e2e` (degrade is a NAMED limitation, never silent); degrade message states the review continues LLM-only |
| **Rhea/MUSCLE bioinformatics tools** | Docker + Rhea + `RHEA_MCP_URL` (`apecx-setup rhea`) | ❌ | 🔒 locked | catalog `unavailable_hint` directs to the MAFFT path (`viral_conserved_sites`) or LLM-only analysis |
| **Domain-RAG synthesis** (FAISS) | FAISS index (`apecx-setup rag`, ~689 MB) | ❌ | opt-in | `test_rag_*` (not exercised this run) |
| **Offline local-DB tools** (`query_*`) | local CSVs (`apecx-setup data`, needs Globus creds) | ❌ | superseded by harmonized search | — |

## Two LLM modes — don't conflate them

This is the correction to an earlier framing error. There are **two distinct
LLM roles**, and "is an LLM available" has two different answers:

- **Desktop / MCP mode (primary):** the connected MCP client — Claude Desktop —
  **is** the orchestrating + synthesizing LLM. apecx tools return deterministic
  data + scaffolds; the desktop LLM reasons over them. **No apecx-side LLM
  endpoint is required.** So on a fresh install the analysis path works with
  zero LLM configuration.
- **Backend / headless mode:** a few bundled workflows synthesize markdown
  *internally* (`run_workflow`, `synthesize_query`). These need the apecx LLM
  backend — local Ollama (`localhost:11434` default) or `APECX_LLM_BASE_URL`.
  **There is no remote default**; it is OFF until you install Ollama
  (`apecx-setup llm`) or set the env var.

## The zero-infra baseline is real and verified

A fresh install with **no Docker, no local data, no credentials, no apecx LLM
endpoint** — only the auto-downloaded synonym dictionary — already supports the
primary path in desktop mode:

> **entity resolution → harmonized multi-source search (anonymous) → the desktop
> LLM analyzes the returned harmonized data.**

The *backend* internal-synthesis path (the bundled `viral_epitope_analysis`
5-section document) additionally needs the backend LLM. That path is proven
end-to-end by `test_evidence_output_contract_five_sections_e2e` and
`test_evidence_only_e2e_has_structural_section` passing on a **no-Docker** machine
with local Ollama. Structural SASA and Rhea/MUSCLE are *enhancements* layered on
Docker; their absence degrades **loud and named**, never silently.

## Evidence-review e2e run (2026-06-15, full suite, no Docker)

`pytest tests/integration/test_viral_epitope_analysis.py` →
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

From the terminal:

```bash
apecx-setup capabilities   # runnable-now vs needs-config (+ fallback) + backend roster
apecx-setup verify         # component health; only the synonym dict is REQUIRED
```

From inside the desktop app, the same view is queryable over MCP via the
**`apecx_capabilities`** tool — a thin aggregator over the existing
`list_workflows` (per-workflow `available` + `missing_prerequisites` +
`unavailable_hint`) and `infrastructure_status` (backend roster) tools. It runs
no probes of its own and states both LLM modes explicitly.
