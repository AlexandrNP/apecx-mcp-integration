# EO-* Implementation Log

Running record of the external-orchestration surface build
(`external_orchestration_design.md` + `implementation_task_graph.md`).
Worktree: `wt-eo-mvp`, branch `eo-mvp-output-surface`.

**Worktree test command** (the worktree shares the main checkout's `.venv` but NOT its
editable install; `PYTHONPATH` overrides the install — verified 2026-05-20 resolving to
the worktree `src`, so tests exercise worktree code, not the main checkout):

```bash
MAIN=/Users/onarykov/Downloads/apecx-cowork/apecx-mcp-integration
WT=/Users/onarykov/Downloads/apecx-cowork/wt-eo-mvp
PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m pytest "$WT/tests/..."
```

---

## ⭐ RESUME HERE — integration capstone (handoff 2026-05-20)

**State:** branch `eo-mvp-output-surface` (worktree `wt-eo-mvp`), 8 commits, **67 tests green**,
clean tree. The full deterministic core + the COMPLETE decomposition feature (matcher +
dispatcher + real-LLM decomposer) are done. Dated sections below give each component's location +
contract.

**Setup (do first in a fresh context):**
- The worktree shares the main `.venv` via a gitignored symlink
  `wt-eo-mvp/.venv -> ../apecx-mcp-integration/.venv` — recreate with `ln -s` if missing.
- Test: `PYTHONPATH=$WT/src $MAIN/.venv/bin/python -m pytest $WT/tests/...` (PYTHONPATH override
  resolves to the worktree src — verified).
- **Commit discipline (learned the hard way):** pre-run `ruff format` + `ruff check --fix` on
  changed files BEFORE `git add`; then commit; then ALWAYS verify `git log -1` advanced — a
  pre-commit ruff abort is invisible if you `| tail -N` the commit output. Ollama is up
  (`mistral-nemo`, `gemma4`, `nemotron-3-nano:4b`).

**Reusable building blocks (committed, tested):**
`composition/schemas/{workflow_result,data_shapes}.py`, `composition/handles/store.py`,
`composition/steps/envelope_step.py` (+`.yml`), `composition/inspection/workflow_inspector.py`,
`composition/runtime/{provenance_wiring,observed_run}.py`, `composition/decomposition/*`
(`LocalDecomposer` + `KeywordWorkflowMatcher` + `RunWorkflowDispatcher` + `LLMTaskDecomposer` +
`prompts/decompose.md`).

**Capstone work, in order:**
1. **End-to-end `query_answering` entry** — wire `LocalDecomposer(matcher, LLMTaskDecomposer,
   RunWorkflowDispatcher)` where the dispatcher's loader resolves a workflow name → runnable
   `Workflow` via the existing `workflow_registry` (`load_catalog` + `_load_workflow_for_entry`),
   and the matcher runs over that catalog. Ollama-gated integration over ≥2 real registered workflows.
2. **EO-03/04/05 MCP tools** ✅ DONE 2026-06-12 (commit `37fdb28`) — `run_workflow` /
   `inspect_run` / `inspect_workflow` / `apecx_context` registered in `mcp_surface/server.py`;
   `composition/runtime/run_store.py` is the in-memory session run store (decision below: in-memory
   chosen). 11 tests incl. a live `run_workflow` over a real `WorkflowBuilder` workflow. See the
   dated section "EO-03/04/05" below.
3. **EO-13c** ✅ DONE 2026-06-12 (commit `caf413f`) — `EnvelopeStepConfig` adds
   `markdown_input_key`/`data_input_key`; rag_e2e_synthesis now ends in an `EnvelopeStep`
   (rag_synthesis.synthesis_output → envelope → workflow_output, a WorkflowResult). Proven in
   a real `WorkflowBuilder` cascade (no LLM). Blast-radius audited: `synthesize_query` calls
   steps directly; the end-to-end test reads the intermediate `synthesis_output` DU — both
   unaffected.
4. **EO-01** ✅ DONE 2026-06-12 (commit `acdf3bb`) — `list_workflows` now returns BOTH the
   runnable catalog (new `runnable` key: `run_workflow` targets, each with an `available`
   flag + `missing_prerequisites`) AND the composer manifests (`workflows` key, back-compat).
   Open decision RESOLVED: the two catalogs stay distinct, tagged by `invoke_with`
   (run_workflow vs start_workflow) — neither is "authoritative"; they're different roles.

**Thin §4 surface now functional:** discover (`list_workflows`) · inspect (`inspect_workflow`)
· run (`run_workflow`) · inspect_run · context (`apecx_context`) · compose (`start_workflow`).

**Conserved-sites feature (the user's actual goal) — progress:**
- **EO-51 (Phase 2)** ✅ DONE 2026-06-12 (commit `fa540d1`) — `BvbrcProteinFastaStep`: real
  per-strain AA sequences from the live BV-BRC data API (genome_feature → feature_sequence,
  both probed + test-verified). NO mocks; FAIL-LOUD. 6 tests incl. a live CHIKV-E1 fetch.
  ⚠ FINDING: `composition/steps/sequence_analysis_step.py` (`SequenceAnalysisStep`, used by
  the `epitope_analysis` workflow) is mock-laden fake-data scaffolding — it writes
  `"ATCGATCG"*20` placeholder "sequences" + copies input as a "mock alignment", manufacturing
  meaningless conserved regions. It is DEAD (the live `eeev_epitope_analysis` tool uses the
  RAG path, not it). Should be RETIRED, not reused. EO-51 replaces it.
- **EO-52 (Phase 3)** ✅ DONE 2026-06-12 (commit `00ce03d`) — `ConservationScoreStep`:
  deterministic per-column conservation over an MSA (identity-fraction primary + Shannon
  secondary), conserved sites + contiguous regions with consensus motifs. Pure Python, no
  deps, FAIL-LOUD on malformed alignment. 8 tests on known-conserved fixtures.
- **EO-53 (Phase 4) — scientific cascade CORE ✅ VERIFIED ON REAL DATA** 2026-06-12 (commit
  `762caf4`). `LocalMafftAlignStep` (real local MAFFT, FAIL-LOUD, no mock fallback — the
  lightweight aligner path; Rhea is the heavy §8 path). The 3-step cascade is proven
  end-to-end against real data (`test_conserved_sites_cascade`): BV-BRC (5 real CHIKV
  polyproteins) → MAFFT (len 2582) → conservation (74 sites / 64 regions). Inter-step bridges
  hold with NO TransformLink (each step reads its key from the prior output dict). Aligner
  blocker resolved by `brew install mafft` (v7.526, arm64).
  **Refinement (TODO):** partial/variable-length CDS records inflate gaps (2582 cols for
  ~1248aa) → lower apparent identity; add a length-filter to `BvbrcProteinFastaStep`.
  **Remaining EO-53 (next):** package the cascade as a nanobrain WORKFLOW — a report step
  (conservation_result → markdown + Bundle data) + EnvelopeStep, wire the YAML, register in
  `mcp_workflow_catalog.yml` (requires: mafft OR rhea + network) so it's discoverable via
  list_workflows + runnable via run_workflow. Explore lightweight WorkflowBuilder vs hand-YAML.
- **EO-53 (Phase 4)** — the `viral_conserved_sites` catalog workflow tying it together:
  resolve virus→taxon_id → BvbrcProteinFastaStep (EO-51) → SubworkflowStep(rhea_muscle_alignment)
  → ConservationScoreStep (EO-52) → EnvelopeStep. Register in `mcp_workflow_catalog.yml`;
  discoverable via list_workflows, runnable via run_workflow. **Design notes for next turn:**
  (a) virus→taxon_id bridge — extract NNNN from NCBITaxon IRI (`_iri_to_taxon_id` exists in
  harmonized_search_execute_step); decide whether to resolve in-workflow or take taxon_id as
  input. (b) Shape bridges (no TransformLink): protein_fasta.fasta_text → rhea
  fasta_collection_input.fasta_text; rhea alignment_fasta → conservation alignment_fasta —
  likely need tiny novel-python adapter steps or careful DU naming. (c) `requires`: RHEA_MCP_URL
  + rhea module + network (BV-BRC). (d) check what resolve capability exists on eo-mvp (the
  newer HarmonizedResolveStep is on reasoning-agent, may differ here).
- **EO-54 (Phase 5)** — pluggable aligners via §8 Tier-1 interface tags (coordinated RHEA).

Off the conserved-sites critical path: capstone item 1 (query_answering decomposition) +
Phase 0 (surface reconciliation / demote confirm_entity_synonym, retire old super-tools incl.
the dead SequenceAnalysisStep/epitope_analysis).

**Open decisions (need the user):**
- **EO-30** — generic `mcp` `BackendKind` touches the deliberately-fixed `CONTRACTS.md#td-vocab`
  (framework version bump). Needs explicit OK.
- **EO-01** merge shape (which catalog is authoritative + maturity tagging).
- ~~**`inspect_run`** run-record persistence (in-memory session vs durable store).~~ RESOLVED
  2026-06-12 — **in-memory session** chosen as the default (`run_store.py`); durable backend can
  replace the singleton later without touching callers. Revisit if cross-process inspect is needed.

---

## EO-10 — `WorkflowResult` envelope ✅ 2026-05-20

- `src/apecx_integration/composition/schemas/workflow_result.py`
- Plain Pydantic `BaseModel(extra="forbid")` — NOT a `from_config` component (per the
  nanobrain compliance brief: result envelopes are data, not framework components).
- Fields: `markdown` / `status`(`ok|partial|error`) / `data_handle` / `data_preview` /
  `run_id` / `error`.
- Loud invariants (no-silent-failure discipline): `status=="error"` requires a non-empty
  `error`; `error` forbidden when status is not error; `data_preview` requires
  `data_handle`. `.failed()` ergonomic constructor for the loud-error path.
- Tests: `tests/unit/test_workflow_result.py` — **9 passed**.

## EO-12 — Canonical data shapes ✅ 2026-05-20

- `src/apecx_integration/composition/schemas/data_shapes.py`
- `RecordSet` / `Evidence` / `Bundle` / `Artifact`; `kind`-discriminated union `DataShape`;
  `parse_data_shape()` is loud on unknown/missing `kind` or typo'd field. Every shape has a
  uniform `.preview(limit)` that feeds `WorkflowResult.data_preview`.
- Tests: `tests/unit/test_data_shapes.py` — **9 passed**.

Combined run: **18 passed in 0.05s**.

---

## Decisions / findings

- **Worktree testing verified.** `PYTHONPATH=<wt>/src` resolves `apecx_integration` to the
  worktree (plain `.pth` install entry; PYTHONPATH wins). Silent-wrong-src risk ruled out.
- **Stale doc/memory finding.** `scripts/checks/wait_for_cascade_use.py` (referenced by the
  repo CLAUDE.md and the `g124` memory) does NOT exist in this checkout — only
  `imports_resolve.py` and `step_authoring.py` are present. The G124 work may live on an
  unmerged branch; to reconcile (do not re-trust the lint's existence).
- **EO-30 deferred.** Adding a generic `mcp` `BackendKind` touches the deliberately-fixed
  `CONTRACTS.md#td-vocab` vocabulary — needs explicit confirmation before implementing.

## EO-11 — Handle store ✅ 2026-05-20

- `src/apecx_integration/composition/handles/store.py`
- `HandleStore.put(DataShape) -> str` / `get(str) -> DataShape` (typed via the `kind`
  discriminator). `HandleNotFound` raised loudly on an unknown handle (never a silent None).
- Backend behind a `HandleBackend` Protocol: v1 `InMemoryBackend` (thread-safe, process/
  session lifetime). ProxyStore (installed — `proxystore 1.0.0`; nanobrain `DataUnitProxyRef`
  at `core/data_unit.py:2039`) is the documented HPC-scale swap-in — **deferred** (no MVP
  HPC-scale payloads; `CONTRACTS.md#ext-tool-dispatch` scopes ProxyStore to HPC-scale).
  Deliberate, documented deviation from the design's "reuse ProxyStore" line.
- `default_handle_store()` — process-wide singleton shared across MCP tool calls.
- Tests: `tests/unit/test_handle_store.py` — **7 passed**.

Spine so far: **25 passed** (EO-10 9 + EO-12 9 + EO-11 7).

## EO-13a — EnvelopeStep (nanobrain BaseStep) ✅ 2026-05-20

- `src/apecx_integration/composition/steps/envelope_step.py`
- Reusable terminal step: wraps a step's output into a `WorkflowResult`. nanobrain-compliant
  (BaseStep + `_get_config_class`, `async process`, module-level `log` not `self.logger`, no
  `execute` override). Builds via `from_config` with just `name:`.
- When the input carries a serialized `DataShape` under `data`, it parses it (loud on bad
  `kind`), stashes it via the handle store, and attaches `data_handle` + `.preview()` to the
  envelope — keeping the structured payload OUT of the markdown channel. Missing/empty
  `markdown` raises loudly.
- Tests: `tests/unit/test_envelope_step.py` — **7 passed**. The headline test stashes a
  50-record payload with a sensitive value and asserts that value is **absent from the
  markdown channel** yet fully retrievable via the handle (channel separation proven).

Spine so far: **32 passed** (EO-10 9 + EO-12 9 + EO-11 7 + EO-13a 7).

## EO-13b — Headline chaining integration test ✅ 2026-05-20

- `tests/integration/test_envelope_chaining.py` + `src/.../composition/steps/envelope_step.yml`
- Builds a real workflow via the lightweight `WorkflowBuilder` (wiring copied from the shipped
  `tdr_loop_lightweight.py`), runs it through `Workflow.run()` (real trigger/link cascade,
  ~2s). Demonstrates A→B chaining: a 50-record payload with a sensitive value is stashed
  behind a handle by workflow A, is **absent from A's markdown channel**, and the full payload
  round-trips via the handle into workflow B — structured data flows out-of-band while only
  markdown + preview reach the orchestrating LLM. Exercises the lightweight authoring path.
- **1 passed.** Full EO suite now **33 passed** (EO-10 9 + EO-12 9 + EO-11 7 + EO-13a 7 + EO-13b 1).

### Noted (deferred, not my code)
- `Workflow.run()` emits a pydantic `UserWarning` (`validate_graph` field is a function being
  model_dump'd) — nanobrain-internal `WorkflowConfig` serialization, cosmetic, pre-existing.
  Candidate framework cleanup; not blocking.

## EO-40/41/42/43 — Provenance + events wiring ✅ 2026-05-20

- `src/apecx_integration/composition/runtime/provenance_wiring.py`
- **Verified first** (the subagent flagged it as unverified): `BaseStep._execute_process`
  DOES auto-call `record_step_invocation` (step.py:2032/2080) + `publish_step_event`
  (2010/2051/2093) when a provenance context is active — so activation actually records, not
  silently no-op.
- `run_with_provenance(workflow, input_data, redact=None, **run_kwargs)` — activates a G4
  `ProvenanceContext` with an injected in-memory `MemorySink` (no built-in in-memory sink
  exists) + subscribes to G37 step events, around `Workflow.run` (not manual
  process+wait_for_cascade, per G124/G125). Returns `ProvenanceRun(result, step_records,
  step_events)`. `redact=None` ⇒ nanobrain default (`prompts` + `executor_env` elided) —
  EO-43; override per call.
- `summarize_run()` → `RunSummary` (EO-42): per-step name/status/duration/n_tool_calls/
  n_llm_calls — the scientist-facing "what ran" view.
- Test: `tests/integration/test_provenance_wiring.py` — **1 passed**. Asserts records are
  NON-empty (loud guard: a context that activates but records nothing is a silent failure).
- Full EO suite now **34 passed**.

## EO-02 — Recursive workflow inspector ✅ 2026-05-20

- `src/apecx_integration/composition/inspection/workflow_inspector.py`
- `inspect_workflow(yaml_path, max_depth=3)` → `WorkflowInspection` tree: name, config_version,
  workflow-level + per-step data units, steps (class/config_path/DUs), links (class/source/
  target/condition), recursing into nested-workflow step configs (depth-capped, `truncated`
  flag). Pure static analysis — no component instantiation. Loud on a dangling step-config
  reference (FileNotFoundError) and non-mapping YAML (ValueError).
- Serves "static composition visibility" (design §4/§9): the scientist sees what a workflow is
  configured to run, recursively, grounded in the YAML.
- Tests: `tests/unit/test_workflow_inspector.py` — **6 passed** (incl. against the real
  `tdr_refine_workflow.yml`, recursion, depth cap, loud failures).
- Full EO suite now **40 passed**.

## EO-03 (core) — Observed workflow run ✅ 2026-05-20

- `src/apecx_integration/composition/runtime/observed_run.py`
- `run_workflow_observed(workflow, input_data, ...)` → `WorkflowRunOutcome(raw_result,
  workflow_result, run_summary)`: composes `run_with_provenance` + `summarize_run` + best-effort
  `WorkflowResult` extraction. The core the MCP `run_workflow` tool will wrap (tool adds
  name-lookup + FastMCP registration). Returns `workflow_result=None` honestly when a workflow
  emits no envelope (not a silent empty one).
- Test: `tests/integration/test_observed_run.py` — **1 passed**. Full EO suite **41 passed**.

## EO-20 — Local bounded decomposition control structure ✅ 2026-05-20

- `src/apecx_integration/composition/decomposition/local_decomposer.py`
- `LocalDecomposer.solve(task)`: match-single-workflow-first; else (if decomposable) decompose
  into sub-workflows + dispatch recursively + integrate; else a loud `WorkflowResult.failed`
  ("cannot solve"). Bounds: `max_depth` (recursion) + `max_dispatches` (fan-out) as plain
  counters — **corrected the design's "bounded by LoopController"** (LoopController is for
  workflow back-edges, not recursion depth). Injectable async matcher/decomposer/dispatcher
  protocols; the LLM/RAG/runtime parts are the only non-deterministic boundary.
- Caught + fixed a self-inflicted silent-failure in `_integrate`: a failed child's error message
  (in `.error`, not `.markdown`) was being dropped from the aggregate — now surfaced as
  `**ERROR:** ...` and the aggregate is `partial` (never silently `ok`).
- Tests: `tests/unit/test_local_decomposer.py` — **7 passed** (match-first, decompose,
  cannot-solve, depth cap, dispatch budget, partial propagation, threshold). Full EO suite **48 passed**.

### Remaining for the full decomposition feature
- Real `WorkflowMatcher` (RAG over the workflow catalog), real `TaskDecomposer` (local LLM —
  Ollama-gated integration), real `WorkflowDispatcher` (`run_workflow_observed`). A nanobrain
  Step wrapper if decomposition must run *inside* a workflow cascade.

## EO-20 (real impls) — matcher + dispatcher ✅ 2026-05-20

- `decomposition/matchers.py` `KeywordWorkflowMatcher` (Jaccard token-overlap baseline; semantic
  RAG is the richer swap-in via the same `WorkflowMatcher` protocol) + `decomposition/dispatchers.py`
  `RunWorkflowDispatcher` (wraps `run_workflow_observed`; loud on unknown workflow + no-envelope).
- **2 of 3 decomposition boundaries now REAL + tested without an LLM**; only `TaskDecomposer`
  remains LLM-gated.
- Tests: `tests/unit/test_workflow_matcher.py` (5) + `tests/integration/test_decomposition_dispatch.py`
  (3, incl. `LocalDecomposer` match→dispatch end-to-end on a real workflow). Full EO suite **56 passed**.

## EO-20 (LLM decomposer) — TaskDecomposer ✅ 2026-05-20

- `decomposition/llm_decomposer.py` `LLMTaskDecomposer` + `prompts/decompose.md` (LLM-guiding
  file — imperative + schema only, no hardcoded prompt, per the rule-content-asymmetry lesson).
  Asks a local LLM whether a task is decomposable + for sub-tasks; robust parse (fence-strip +
  prose-salvage); loud on empty/unparseable response (a parse failure is NOT silently treated as
  "not decomposable" — that would mask a broken model).
- **All 3 decomposition boundaries are now real.** Unit/integration parity: stub-LLM unit tests
  (10, deterministic) + a real-Ollama integration test (`mistral-nemo`, ~16s; asserts a valid
  shape, not flaky content).
- Tests: `tests/unit/test_llm_decomposer.py` (10) + `tests/integration/test_llm_decomposer_against_ollama.py`
  (1, Ollama-gated, auto-skips without Ollama). Full EO suite **67 passed**.

## Next

- Wire the full decomposition stack into an end-to-end `query_answering`-style entry (catalog
  matcher + LLM decomposer + observed dispatch over real registered workflows).
- EO-03/04/05 MCP-tool registration (needs MCP-client integration loop).
- EO-01 catalog reconciliation; EO-13c rag_e2e wiring (Ollama-gated).
