# Pre-existing test failures — analysis + resolution (2026-06-24)

Five unit tests failed on `epitope-artifacts-dir` (and therefore on every branch off it, incl.
`deploy-hardening`). They are **pre-existing** — unrelated to the deployment-hardening work — and
were already root-caused + fixed during Project A as commit **`e86ad06`** (on
`workflow-crafting-intelligence`). This records the analysis and the resolution path.

## The 5 failures → 3 root causes

**① `viral_epitope_analysis` wrongly "requires a server LLM in desktop locus"** (3 tests:
`test_product_workflows_both_modes::test_every_product_workflow_runs_in_desktop_without_a_server_llm`,
`test_llm_policy::test_epitope_needs_llm_only_in_agent_locus`,
`test_llm_policy::test_run_workflow_desktop_does_not_refuse_self_omitting_workflow`).
- Cause: `TaxonSynonymGenerationStep` + `TaxonCandidateReviewStep` are optional, degrade-loud LLM
  *fallbacks* (run only on a dict-resolver miss; never raise on LLM failure), but the
  `workflow_requires_llm` heuristic sees their LLM usage and flags them as in-DAG LLM steps → the
  policy decides the workflow needs a server LLM in desktop locus. Their docstrings say "does NOT
  set `LLM_ROLE`" — which is the bug (not setting it leaves the heuristic to mis-guess).
- Fix (`e86ad06`): declare `LLM_ROLE = "none"` on both classes (honored at
  `workflow_requires_llm.py:154` → excluded from the LLM-bearing set). Verified genuinely
  no-requirement (the workflow runs end-to-end in desktop locus with no LLM), not test-silencing.

**② MCP surface drift** (`test_mcp_tool_surface::test_mcp_surface_is_exactly_the_layer1_set`).
- Cause: `conserved_epitope_candidate_assessment` + `epitope_combination_feasibility_assessment`
  are registered as first-class catalog tools but absent from the test's `EXPECTED` set.
- Fix (`e86ad06`): **retire** them from the catalog (not add to EXPECTED) — they are handle-driven
  follow-ups (consume a prior run's `data_handle`, no free-text `{query}` entry), the same
  not-directly-invocable shape as the already-retired `viral_conserved_sites` precedent; they stay
  discoverable + runnable via `run_workflow`. (Product-surface call, review-gate-confirmed.)

**③ rhea test-ordering pollution**
(`test_rhea_container_backend::test_container_env_container_specific_overrides`; passes alone,
fails in-suite).
- Cause: `test_rhea_env_autodiscovery` sets `RHEA_CONDA_ENVS_DIR`, which
  `_compose_rhea_container_env` copies from `os.environ`, breaking this test's
  `assert "RHEA_CONDA_ENVS_DIR" not in env`.
- Fix (`e86ad06`): an autouse fixture clearing the ambient rhea env vars → order-independent.

## Resolution

`e86ad06` ports cleanly (the 5 touched files are byte-identical across the relevant bases). The
chosen path is to **land it via the branch merges to `main`**: merging `workflow-crafting-intelligence`
brings `e86ad06`, so the 5 go green on `main` by the merge itself — no separate fix branch needed.

Merge sequence (apecx-mcp-integration, in the `wt-main` worktree; push held):
1. Preserve `wt-main`'s pre-existing uncommitted evidence-review edits on a WIP branch
   (`wip-main-evidence-review`) first — the incoming `epitope-artifacts-dir` rewrites the same files.
2. `main` →(FF) `epitope-artifacts-dir` →(FF) `workflow-crafting-intelligence` (lands `e86ad06`).
3. Merge `deploy-hardening` (a sibling of wfc; disjoint file sets → conflict-free merge commit).
4. Verify the full unit suite on merged `main`: the 5 green, 0 regressions.

Cross-repo: the apecx contract code imports `nanobrain.core.data_contract`, which lives on
nanobrain's `e2r-rhea-dynamic-deterministic` branch — that nanobrain branch is merged to nanobrain
`main` in the same pass so `main` is self-coherent.
