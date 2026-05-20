# Session Checkpoint — External-Orchestration Surface (EO-*)

**Date:** 2026-05-20
**Branch:** `eo-mvp-output-surface` (git worktree `apecx-cowork/wt-eo-mvp`) — **local, unpushed**
**Status:** deterministic core + full decomposition feature DONE — 9 commits, 67 tests green, clean tree
**Purpose of this file:** a single, self-contained handoff so a clean session can safely resume.
Read this top-to-bottom; it links to the authoritative design + the per-component log.

> Authoritative design: `docs/external_orchestration_design.md`.
> Per-component build log: `docs/eo_implementation_log.md` (has a "⭐ RESUME HERE" section too).
> Task graph (rewritten for this direction): `docs/implementation_task_graph.md` (`EO-*` / `T-RH-*`).

---

## 1. TL;DR

The agentic surface was **pivoted** (2026-05-19) from an unbuilt server-side multi-agent
orchestrator to **external-LLM orchestration**: a powerful external LLM (Claude/GPT) orchestrates
over MCP; nanobrain workflows are deterministic scaffolds; a local LLM decomposes a task into
sub-workflows ONLY as a bounded fallback when no single workflow matches. We then **built the
entire LLM-free deterministic core + the complete local-decomposition feature** (including a
real local-LLM decomposer verified against Ollama). What remains is integration-heavy wiring +
a few decisions that need the user.

---

## 2. How to resume (operational — do this first)

**Worktree + venv:** the worktree `wt-eo-mvp` shares the main checkout's venv via a **gitignored
symlink** `wt-eo-mvp/.venv -> ../apecx-mcp-integration/.venv`. If missing, recreate:
```bash
ln -s /Users/onarykov/Downloads/apecx-cowork/apecx-mcp-integration/.venv \
      /Users/onarykov/Downloads/apecx-cowork/wt-eo-mvp/.venv
```
Why it matters: the worktree has no editable install of its own. `PYTHONPATH` overrides the main
venv's editable install so tests/hooks exercise the **worktree** `src` (verified). Always test as:
```bash
MAIN=/Users/onarykov/Downloads/apecx-cowork/apecx-mcp-integration
WT=/Users/onarykov/Downloads/apecx-cowork/wt-eo-mvp
PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m pytest "$WT/tests/..."
```

**Commit discipline (learned the hard way — see §8):** before `git add`, pre-run
`"$MAIN/.venv/bin/python" -m ruff format <files>` and `... ruff check --fix <files>`; then commit;
then **always verify `git log -1` advanced**. A pre-commit ruff abort is invisible if you pipe the
commit through `| tail -N` — that caused two false "committed" claims this session.

**Environment:** Ollama is up at `localhost:11434` with chat models `mistral-nemo:latest`,
`gemma4:latest`, `nemotron-3-nano:4b` (+ embed models). `build_chat_llm()` defaults to
`mistral-small:latest` which is NOT pulled — set `APECX_LLM_MODEL=mistral-nemo:latest` for live tests.

---

## 3. The decided architecture (condensed; full version in `external_orchestration_design.md`)

- **External frontier LLM = primary orchestrator** (always): decomposes the query, selects
  workflows, sequences, glues, synthesizes.
- **Workflows = deterministic nanobrain scaffolds.** Local LLM appears only inside a workflow as
  a **bounded fallback**: match-a-single-workflow-first; decompose-into-sub-workflows only if no
  single workflow matches; bounded by depth + dispatch counts; loud "cannot solve" otherwise.
- **Output contract = two channels:** `markdown` (what the LLM/scientist reads) + `data_handle`
  (+ small `data_preview`) for chaining structured payloads workflow-to-workflow WITHOUT routing
  them through the LLM context.
- **Tiered tool substitution:** (1) deterministic config-swap when tools share an interface tag,
  (2) declared param mapping, (3) composer rebridge — LLM only for genuinely incompatible swaps.
- **Provenance:** reuse G4 `ProvenanceContext` + G37 step events (auto-recorded by `step.py`) +
  a flat `RunSummary`; static composition visibility via recursive YAML inspection.
- **Three workflow-authoring paths** exist and two are exercised here: hand-authored YAML and the
  lightweight `WorkflowBuilder` (the third, `from_skeleton`, is untouched).

---

## 4. What is BUILT (committed, tested) — component inventory

All under `wt-eo-mvp/src/apecx_integration/composition/` unless noted.

| Component | Path | Contract |
|---|---|---|
| `WorkflowResult` | `schemas/workflow_result.py` | Pydantic `extra='forbid'`; markdown/status/data_handle/data_preview/run_id/error; loud invariants (error-status requires a message; preview requires a handle); `.failed()` ctor |
| Data shapes | `schemas/data_shapes.py` | `RecordSet`/`Evidence`/`Bundle`/`Artifact`, `kind`-discriminated `DataShape`; `parse_data_shape()` loud on bad kind; uniform `.preview()` |
| Handle store | `handles/store.py` | `HandleStore.put(DataShape)->str` / `get(str)->DataShape`; `HandleNotFound` (never silent None); `HandleBackend` protocol, in-memory v1; ProxyStore is the documented HPC swap-in (deferred) |
| `EnvelopeStep` | `steps/envelope_step.py` (+`.yml`) | nanobrain `BaseStep`; wraps a step's output into a `WorkflowResult`; stashes `data` (a DataShape) behind a handle, keeping it out of the markdown channel |
| `inspect_workflow` | `inspection/workflow_inspector.py` | recursive static YAML-tree resolver (steps/links/data-units, depth-capped); loud on dangling config refs |
| Provenance wiring | `runtime/provenance_wiring.py` | `run_with_provenance()` (G4 in-memory sink + G37 subscribe around `Workflow.run`); `summarize_run()` -> `RunSummary` |
| Observed run | `runtime/observed_run.py` | `run_workflow_observed()` -> `WorkflowRunOutcome(raw_result, workflow_result, run_summary)` (the core the MCP `run_workflow` tool will wrap) |
| Decomposition | `decomposition/` | `LocalDecomposer` (match-first, bounded, loud cannot-solve) + `KeywordWorkflowMatcher` + `RunWorkflowDispatcher` + `LLMTaskDecomposer` (+ `prompts/decompose.md`) |

**Commits (since the docs commit `f34adfd`):**
`03585d0` EO-10+12 · `0dec4d1` EO-11 · `13f0e2d` EO-13+40/41/42 · `888c08c` EO-02 ·
`1af5b53` EO-03 core · `90f05c7` EO-20 control · `8664d4e` EO-20 matcher+dispatcher ·
`e7dcbfc` EO-20 LLM decomposer · `0c70075` resume-handoff docs.

**Tests (67):** unit in `tests/unit/test_{workflow_result,data_shapes,handle_store,envelope_step,
workflow_inspector,local_decomposer,workflow_matcher,llm_decomposer}.py`; integration in
`tests/integration/test_{envelope_chaining,provenance_wiring,observed_run,decomposition_dispatch,
llm_decomposer_against_ollama}.py`. The Ollama test auto-skips when Ollama is unreachable.

---

## 5. Capstone work remaining (ordered)

1. **End-to-end `query_answering` entry.** Wire `LocalDecomposer(matcher, LLMTaskDecomposer,
   RunWorkflowDispatcher)` where the dispatcher's `workflow_loader` resolves a workflow name →
   runnable `Workflow` via the existing `mcp_surface/workflow_registry.py` (`load_catalog` +
   `_load_workflow_for_entry`), and the matcher runs over that catalog. Ollama-gated integration
   over ≥2 real registered workflows. This is the integration capstone.
2. **`EO-03/04/05` MCP tools.** Register into `mcp_surface/server.py` (pattern: `server.tool()(fn)`):
   `run_workflow` (wraps `run_workflow_observed`), `inspect_run` (surfaces `RunSummary` — needs a
   run store keyed by `run_id`), `inspect_workflow` (wraps the inspector), `apecx_context` (session
   re-orientation). **Needs an MCP-client integration loop to verify** (not just unit tests).
3. **`EO-13c`.** Append `EnvelopeStep` to the shipped `rag_e2e_synthesis` workflow. Needs a
   `synthesis`→`markdown` key bridge: give `EnvelopeStep` a configurable input key via an
   `EnvelopeStepConfig(StepConfig)` subclass (mirror `RagSynthesisStepConfig`: `extra='forbid'`,
   strip the framework `class:` key in a `model_validator(mode="before")`). Ollama-gated.
4. **`EO-01`.** `list_workflows` catalog reconciliation — see open decision §6.
5. **Tool ingestion / substitution (`EO-30..34`, `T-RH-*`).** Reuse the SHIPPED nanobrain pieces:
   `UnifiedToolDescriptor` (UTD), `RheaMCPDiscovery`/`RheaAdapter`/`MCPTransport`, RHEA fork
   `utd_producer.py`. Interface tags for Tier-1 substitution come from RHEA's rich in-process
   path, NOT the MCP wire. `EO-30` is gated on a decision (§6).

---

## 6. OPEN DECISIONS — these need the user before proceeding

1. **`EO-30` — generic `mcp` `BackendKind`.** Supporting arbitrary (non-RHEA) MCP servers needs a
   generic `mcp` value added to UTD's `BackendKind`, whose literal set is **deliberately fixed**
   (`CONTRACTS.md#td-vocab`). Adding it is a framework version bump + catalog sweep. **Do NOT do it
   without explicit OK.**
2. **`EO-01` catalog merge shape.** Two catalogs exist: `mcp_surface/tools/discovery.py`
   (composer-buildable manifests) and `mcp_surface/workflow_registry.py`
   (`mcp_workflow_catalog.yml` runnable MCP-tool workflows). Which is authoritative for
   `list_workflows`? How to surface maturity (`validated`/`experimental`)? Product call.
3. **`inspect_run` run-record persistence.** Where do per-run records live so `inspect_run(run_id)`
   can retrieve them — in-memory session store (dies on restart) or a durable store? Affects whether
   a new run-store component is needed.
4. **Can `query_answering` return a first-class "I don't know"?** (Raised mid-design.) The
   decomposer already does this (`LocalDecomposer` returns a loud failed `WorkflowResult`); confirm
   the surface should propagate that honestly rather than always synthesizing an answer.
5. **Push / PR?** The branch is local/unpushed. No remote action has been taken. Decide whether to
   push `eo-mvp-output-surface` and/or open a PR. (Workspace rule: pushes need explicit approval.)
6. **Planner LLM model.** The decomposer was verified against `mistral-nemo` (local). If a larger
   model (or the external frontier LLM via MCP sampling) is acceptable for decomposition, quality
   improves substantially — see the risk in §8.

---

## 7. RECOMMENDATIONS (consolidated — my standing advice)

- **Markdown + handle (not markdown-only).** Keep structured payloads out of the LLM context via
  handles; the LLM stays the orchestrator, not the data bus. (Built.)
- **Tiered substitution (not composer-only).** Deterministic config-swap for same-interface tools;
  reserve the LLM composer for genuinely incompatible swaps. The common "use my preferred aligner"
  case must not depend on LLM reliability.
- **Tags at the boundary, not in nanobrain core.** Keep the workflow/tool-discovery tag concept in
  the apecx surface; don't tax every nanobrain consumer. (Reversibility.)
- **`main`/profiles must NOT be privileged in code** if the tag/profile system is built later —
  make them ordinary, operator-tunable.
- **Reuse-first, verified.** This session repeatedly found existing built components (UTD, G4, the
  composer, `workflow_registry`); read the actual code before authoring. ProxyStore deferred for
  the handle store because the contract scopes it to HPC-scale; an in-memory v1 is correct for now.
- **Recursion bound = depth counter, NOT `LoopController`** (which is for workflow back-edges).
- **Every LLM-touching component gets unit (stub) + integration (real-LLM) parity** — done
  throughout; keep it for the capstone.
- **Loud failures by construction** — every component here raises/marks-error rather than returning
  a plausible empty; keep this discipline in the wiring layer.
- **Resume integration-heavy work in a fresh context** (this checkpoint exists because of that).

---

## 8. ISSUES & RISKS RAISED (brutal-truth log)

- **Process failure: ~80% of the early design was re-derived from scratch** before I read the
  existing design package (`CONTRACTS.md`, `_design_index.md`, the task graph). Lesson saved to
  memory (`feedback-check-design-package-first`): read the design package + verify built-vs-planned
  BEFORE greenfield-designing in this repo.
- **Commit-verification failure: two false "committed" claims.** `EO-13a/b` commits aborted on a
  real ruff lint (`B017` blind-except) + a pre-commit stash conflict; I had piped commit output
  through `| tail -N` and missed it. No work was lost (staged). Fix: §2 commit discipline.
- **Self-inflicted silent-failure (caught before commit):** `LocalDecomposer._integrate` dropped a
  failed child's error message (it lives in `.error`, not `.markdown`). Fixed to surface it and
  mark the aggregate `partial`. A reminder to audit every aggregation/merge for dropped error info.
- **Reliability degrades at extreme context depth.** The two failures above clustered late in a
  very long session. Strong recommendation: keep build sessions bounded; checkpoint + resume fresh.
- **Local-LLM decomposition quality is the architecture's weakest link.** `mistral-nemo`-class
  models are weak at multi-step reasoning (per workspace memory: G99–G126, codegen canary). The
  bounded fallback + loud cannot-solve + deterministic control structure mitigate the classic
  "60% acceptable / 40% confidently-wrong" HTN-on-local-LLM failure mode, but do not eliminate it.
  Mitigations to consider: prefer a larger planner model; mandatory canonical-example coverage per
  workflow to improve matching; a planner-decision eval suite separate from end-to-end accuracy.
- **Stale doc/memory:** `scripts/checks/wait_for_cascade_use.py` (referenced by the repo CLAUDE.md
  and the `g124` memory) does NOT exist in this checkout — only `imports_resolve.py` +
  `step_authoring.py`. The G124 lint work may live on an unmerged branch. Don't re-trust the lint's
  existence; verify the branch.

---

## 9. FRAMEWORK OBSERVATIONS (nanobrain-side; candidates, not blockers)

- **`Workflow.run()` pydantic serializer warning:** `validate_graph` (a `WorkflowConfig` field that
  is a function) is `model_dump`'d somewhere and emits a `UserWarning` on every run. Cosmetic,
  pre-existing, nanobrain-internal — candidate cleanup.
- **G126 — `ConfigBase.model_config` is `extra='allow'`** (`nanobrain/core/config/config_base.py`),
  which silently absorbs YAML typos at WorkflowConfig load. Known/deferred (cross-repo breaking
  change); the apecx `scripts/lint_workflow_yamls.py` mitigates one typo class at pre-commit.
- **"Expand framework capacity if required" was honored conservatively** — no nanobrain edits were
  needed for the deterministic core (everything reused shipped primitives). The one place a
  framework change IS required (`EO-30` `mcp` BackendKind) is gated on a decision (§6).

---

## 10. POINTERS

- Design: `docs/external_orchestration_design.md` · Build log: `docs/eo_implementation_log.md`
- Task graph: `docs/implementation_task_graph.md` · Contracts: `docs/CONTRACTS.md`
- Reused nanobrain pieces: `nanobrain/core/{provenance,step_events,cost_envelope,unified_tool_descriptor}.py`,
  `nanobrain/library/steps/{loop_controller,recursive_subworkflow_step,subworkflow_step}.py`,
  `nanobrain/library/tools/{_mcp_transport,rhea_adapter,rhea_discovery}.py`, `nanobrain/lightweight/`
- LLM factory: `src/apecx_integration/agents/_llm_factory.py::build_chat_llm`
- Memory: `eo_external_orchestration_impl_state.md` (project), `feedback_check_design_package_first.md`
- This checkpoint supersedes nothing; it consolidates the session for resume.
