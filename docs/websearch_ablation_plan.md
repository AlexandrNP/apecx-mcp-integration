# Web Search Mode + OR/BixBench Ablation + Rhea Code-Use Agent — plan

**Date**: 2026-05-14. **Status**: in progress.

This is the working artifact for the chain requested on 2026-05-14:
add a web-search mode, run an ablation study on the codegen-adapted
Open-Rosalind + BixBench subsets, and stand up a Rhea code-use agent.

## Decisions locked in

1. **Web search backend** — pluggable; no API key currently available;
   **default keyless DuckDuckGo** (`ddgs` package, lazy-imported,
   FAIL-LOUD if absent). API-key backends (Tavily/Brave) configurable
   later. **Query-hash on-disk result cache** for reproducibility +
   to dodge DDG rate limits.
2. **BixBench** — download proceeds. Ablation runs on the
   **Python-answerable subset only** (`str_verifier` + `range_verifier`).
   R-native questions + `llm_verifier` questions stay gated; the
   R-sandbox expansion is **deferred** (its own multi-day arc).
3. **Ablation scope** — the ablation matrix (incl. web search) runs on
   the **codegen-adapted subsets only** (OR `sequence_basic`,
   BixBench Python subset). **Plus**: stand up + run a **Rhea
   code-use agent** (the Agent path).
4. **Rhea fork is writable** (`rhea/` at workspace root) — registering
   tools into Rhea is no longer out-of-scope.
5. **The Rhea-dependent pipeline spawns its own Rhea MCP worker** — a
   nanobrain-native lifecycle component, not a manual `docker run`.
6. **Real blockers** — surface them; if genuinely blocked, skip that
   part (last resort) and continue the rest of the plan.

## Standing predictions (on the record before runs)

- Web search ≈ **null on accuracy** for both benchmarks. OR
  `sequence_basic` is pure computation (the answer is an algorithm,
  not a lookup); BixBench's drafter still cannot search the capsule
  data files. Web search's value is reliability/coverage in diverse
  settings, not an accuracy lever — the ablation's job is to measure
  that honestly.
- The Rhea agent will work as a *mechanism* end-to-end; tool-selection
  quality with the local model (mistral-nemo) is the real risk.

## Architecture decisions

- `WebSearchTool` → `nanobrain/library/tools/web_search.py` — a
  generic `ToolBase`, belongs in nanobrain proper (precedent: the
  Rhea components were promoted there 2026-05-14).
- `WebSearchContextStep` → apecx-side `BaseStep` — benchmark-
  composition concern, mirrors the `memory_reader` node pattern.
  **Non-deterministic step** (web results drift) — honestly labelled,
  not under the determinism contract.
- Rhea MCP worker lifecycle → a nanobrain-native spawner component
  (subprocess or Docker-backed) the Rhea pipeline owns.
- `RheaCodeUseAgent` → a nanobrain `Agent` (YAML `from_config`)
  holding Rhea MCP tools (`RheaMCPDispatcher`) + the `WebSearchTool`.

## Phases + tasks

### Phase A — Web search tool (framework-native)
- A1. `WebSearchTool` (`nanobrain/library/tools/web_search.py`):
  `ToolBase`, pluggable `backend`, default keyless DDG, FAIL-LOUD on
  backend error / configured-but-keyless API backend, query-hash
  on-disk cache. Unit tests vs. mock backend + gated real-DDG test.
- A2. `WebSearchContextStep` (apecx `BaseStep`): wraps the tool,
  mirrors `memory_reader`, emits a search-context blob into the
  drafter. Non-deterministic. Unit tests.

### Phase B — Codegen composition + modes
- B1. `benchmark_max_power_websearch/workflow.yml` + step configs.
- B2. `benchmark_max_power_websearch_lightweight_builder.py` (the
  lightweight `WorkflowBuilder` construction path).
- B3. `cli.py` codegen modes: `nanobrain_max_power_websearch` +
  `nanobrain_ablation_websearch_only`.
- B4. Integration test: loads + cascades with a fake search backend.

### Phase C — BixBench data
- C1. Download BixBench from HF, extract capsules, set
  `$APECX_BIXBENCH_CAPSULES`.
- C2. Verify the loader yields real problems; enumerate the
  Python-answerable subset.

### Phase D — Ablation runs (codegen surface)
- D1. Open-Rosalind ablation sweep — baselines + 5 existing ablation
  modes + `nanobrain_ablation_websearch_only` + `nanobrain_max_power`
  + `nanobrain_max_power_websearch`, on the `sequence_basic` subset.
- D2. BixBench ablation sweep — same matrix, Python-answerable subset.
- D3. Aggregate + mechanism analysis.

### Phase E — Rhea code-use agent
- E1. Pipeline-managed Rhea MCP worker spawner (nanobrain-native
  lifecycle component; uses the writable `rhea/` fork).
- E2. `RheaCodeUseAgent` — nanobrain `Agent` + Rhea MCP tools +
  `WebSearchTool`. Expand Agent-path framework capacity if needed.
- E3. Integration test (gated): agent picks + calls a real Rhea tool
  and a web search end-to-end; honest tool-selection-quality note.

### Phase F — Documentation
- F1. This plan doc.
- F2. `docs/findings_websearch_ablation.md` — matrices + mechanism
  analysis + brutal-truth read.
- F3. `docs/rhea_code_use_agent.md` — agent design + honesty notes.
- F4. `code_writing_prompts/websearch_workflow_rules.md` — LLM
  guidance.
- F5. Update `nanobrain/CLAUDE.md`, `nanobrain-agents-tools` skill,
  `docs/_design_index.md`.

## What will NOT be done

- No fake search results, no faked Rhea worker, no silent empty
  returns — all FAIL LOUD.
- No claimed lift that isn't in the data.
- BixBench R-sandbox expansion — deferred.
