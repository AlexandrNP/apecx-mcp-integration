# Next Tasks — authored 2026-04-22

This is the task queue handed to the next session. It assumes the
state captured in `session_recap_2026_04_22.md` (memos 07+08 merged,
Task 1 partially in progress on `apecx-db-integration` branch
`package-and-strip-creds`).

---

## Constraint reminder

**No live-LLM roundtrips from within Claude Code sessions.** Anywhere
below the chain says "verify end-to-end," that means: operator runs
the LLM-touching test, Claude runs only the import-graph / lint /
non-LLM unit tests. The integration tests that do hit Ollama stay in
the tree but are operator-invoked and Claude-skippable (they auto-skip
when the daemon isn't reachable, which is the sentinel Claude honors
by not starting the daemon).

---

## Task 1 (finish) — apecx-db-integration packaging

**Branch:** `package-and-strip-creds` (in `apecx-db-integration`,
not `apecx-mcp-integration`).

### 1.1 Migrate the two remaining `ChatOpenAI` call sites

**File:** `src/apecx_db_integration/agent.py`.

Both `initialize_csv_agent` (~line 537) and `initialize_bvbrc_agent`
(~line 574) still call `ChatOpenAI(temperature=0, model="gpt-4o-mini",
max_tokens=16384, request_timeout=600)` directly and gate on a
`OPENAI_API_KEY` env-var warning.

Rewrite both to:

```python
llm = _build_chat_llm(temperature=0, max_tokens=16384,
                     request_timeout=600)
```

Drop the two `if not os.environ.get("OPENAI_API_KEY"): print("Warning: ...")`
blocks — stale now that the credential is stripped and the factory
resolves keys from three env vars with fallback to `"EMPTY"`.

**Brutal-truth caveat:** these are the CSV agents (`create_csv_agent`
from `langchain_experimental`) using `AgentType.OPENAI_FUNCTIONS`.
Function-calling support on local models is uneven. `mistral-small:24b`
may or may not obey the function schema. We don't exercise these CSV
agents in Step 1 / 3c / 5 so this path is currently dormant — keep
it working but don't verify it end-to-end here. Flag as a known
degradation risk in `future_work.md` when merging.

### 1.2 Write `pyproject.toml` at repo root

Src-layout (`[tool.setuptools.packages.find] where = ["src"]`). Deps
lifted from `requirements.txt`; pin the langchain family to the
versions currently installed in the apecx-mcp-integration venv so we
don't get a surprise breaking change on fresh install. Add `build-system`
requires, `requires-python = ">=3.11"`, `license = {text = "BSD-3-Clause"}`
(match apecx-mcp-integration's pyproject).

### 1.3 Write `src/apecx_db_integration/__init__.py`

Re-export the three public functions the Step wrappers will consume:

```python
from .agent import (
    extract_entities_llm,
    consolidated_synonym_search,
    enrich_matches_with_database_data,
)

__all__ = [
    "extract_entities_llm",
    "consolidated_synonym_search",
    "enrich_matches_with_database_data",
]
```

Nothing more. Don't re-export CSV agents, dataframes, or the LLM
factory — keep the public surface narrow.

### 1.4 Install editable into the apecx-mcp-integration venv

```
cd /Users/onarykov/Downloads/apecx-cowork/apecx-mcp-integration
.venv/bin/pip install -e ../apecx-db-integration
```

Confirm `python -c "import apecx_db_integration; print(apecx_db_integration.extract_entities_llm)"`
works — this is the Claude-verifiable acceptance criterion.

### 1.5 Claude-side verification (no LLM calls)

- Import-graph check: `python -c "from apecx_db_integration.agent import extract_entities_llm, consolidated_synonym_search, enrich_matches_with_database_data"`
  with `APECX_DB_DATA_DIR` pointing at a tmp dir containing one tiny
  stub CSV. Loads clean → passes.
- Lint pass: `ruff check src/` in the apecx-db-integration repo.
- Verify no residual `sk-proj-` strings anywhere in the worktree
  (`rg "sk-proj-"` should return nothing).

### 1.6 Commit + merge

One commit on the branch. Merge to `apecx-db-integration/main` with
`--no-ff` (distinct branch history) OR `--ff-only` if linearity is
preferred. Do NOT push to `origin/main` without explicit user approval.

**Operator-side verification (not Claude's job):** with Ollama running,
run `python -c "from apecx_db_integration.agent import extract_entities_llm; print(extract_entities_llm('find EEEV vaccines'))"`
and confirm a non-empty entity list comes back.

---

## Task 2 — Author the three Step wrappers in apecx-mcp-integration

**New worktree:** `wt-t02-steps-1-3c-5` from `apecx-mcp-integration/main`.

Structure per step: a Python class under
`src/apecx_integration/composition/steps/` plus a wrapper YAML under
`src/apecx_integration/composition/workflows/violin_bvbrc/steps/`.

### 2.1 Step 1 — `entity_extraction`

- Class: `EntityExtractionStep(BaseStep)` — thin wrapper that calls
  `extract_entities_llm(query)`, returns structured result into an
  output DataUnit.
- YAML: `steps/entity_extraction.yml` — defines input/output DataUnits
  (input: `user_query`; output: `entity_candidates_output` matching
  the schema Step 2 already expects at its `entity_candidates_input`).
- Uses `DelimitedFileReaderStep` of query file OR direct input for the
  T01 slice.

### 2.2 Step 3c — `synonym_llm_proposals`

- Class: `SynonymLLMProposalsStep(BaseStep)` wrapping
  `consolidated_synonym_search`.
- YAML input: `novel_terms_input` (residual entities from cache-miss
  path); output: `llm_proposals_output` matching
  `synonym_approval_gate`'s `llm_proposals_input` schema.

### 2.3 Step 5 — `violin_entity_lookup`

- Class: `ViolinEntityLookupStep(BaseStep)` wrapping
  `enrich_matches_with_database_data`. **This is pure pandas, no LLM.**
- YAML: input `resolved_matches_input`; output `enriched_matches_output`.

### 2.4 Loadability tests

Add three per-step `from_config` loadability tests to
`tests/integration/test_violin_bvbrc_workflow_yaml.py` (following the
same shape as the Step 2 and Step 6 tests landed in `dac9536`).

### 2.5 Unit tests (no live LLM)

For Steps 1 and 3c: monkeypatch the three `apecx_db_integration`
functions to return fixture data, run `step.process(...)`, assert the
DataUnit output shape. This matches the workspace-CLAUDE.md parity
rule: mock + integration test together. The integration test for these
is in Task 3; it lives in the tree but Claude won't run it.

---

## Task 3 — Wire the three new steps into the workflow YAML + live test

**Same worktree as Task 2.**

### 3.1 `violin_bvbrc_workflow.yml`

Move `entity_extraction`, `synonym_llm_proposals`, and
`violin_entity_lookup` from the PENDING comments block to the active
`steps:` block. Update the coverage table in the YAML header. Update
`manifest.yml` — flip the three `status: pending` entries to `status: ready`
with `wrap_notes:` pointing at `apecx_db_integration`.

### 3.2 End-to-end integration test (operator-run)

New file: `tests/integration/test_violin_bvbrc_workflow_against_ollama.py`.
Shape:

- Skip markers: auto-skip if Ollama unreachable, if model not pulled,
  or if `APECX_DB_DATA_DIR` doesn't contain the required VIOLIN CSVs.
- Set env vars via `monkeypatch` pointing at Ollama + `mistral-nemo:latest`
  for the fast dev loop (that's the "(a)" choice from the 2026-04-22
  plan).
- Set `temperature: 0.0` and `max_tokens: 256` on the wrapper YAMLs
  (that's the "(b)" choice — tight bounds keep each call short).
- Runs the first three steps end-to-end: query → Step 1 → Step 2 →
  assert non-empty snapshot matches. That's the minimum T01 vertical
  slice.

**Claude runs only the import-graph + loadability side of this file.**
End-to-end execution is operator-run.

### 3.3 Commit + merge

One commit per task where convenient. Merge to `apecx-mcp-integration/main`.

---

## Task 4 — T01 vertical slice (~1d)

**Prerequisite:** Tasks 1–3 done and merged.

**Scope narrowing (brutal-truth correction of earlier framing):** "full
10-step workflow" isn't realistic as a single commit. The vertical
slice is:

```
user_query → Step 1 (entity_extraction, Ollama)
          → Step 2 (bvbrc_snapshot_match, BVBRCSnapshotTool)
          → Step 3a (synonym_cache_lookup, Control Plane)
          → Step 3c (synonym_llm_proposals, Ollama) — cache-miss branch
          → Step 4 (synonym_approval_gate, HITL)
          → Step 4p (verified_synonym_writeback)
          → Step 7 (result_ranking) → final JSON
```

Steps 5 (`violin_entity_lookup`) and 6 (`genomic_annotation`) are
enrichment steps; T01 slice can defer them behind a feature flag and
deliver results without VIOLIN cross-join or BV-BRC annotation. Step 0
(vocabulary extraction) and Step 3b (fuzzy matching) are deferred by
the HARD-synonym directive.

Links between steps are still `links: {}` in the workflow YAML. Task 4
is where that gets populated. This is genuinely new design work — the
data-flow between Step 1's entity output and Step 2's entity-candidates
input has to agree on a schema, and neither side has that schema
pinned yet.

### 4.1 Schema-pin the three cross-step DataUnit shapes

Before writing links: write dataclasses or TypedDicts that make the
expected shape of each cross-step DataUnit explicit, and have the
Steps validate on write (producer) and read (consumer). This is a
lot cheaper than debugging a mismatch during a live run.

### 4.2 Write the links block

One `DirectLink` per step-to-step edge. 6 links for the 7-step slice.
Each wires `source: "<step>.<output_du>"` to
`target: "<step>.<input_du>"`.

### 4.3 Live integration test (operator-run)

`tests/integration/test_t01_vertical_slice_against_ollama.py`:

- Inputs: a small VIOLIN query like "find EEEV vaccines"; the committed
  `BVBRC_genome_alphavirus.csv`; and the operator-provided VIOLIN CSVs.
- Auto-skips on missing Ollama / missing VIOLIN CSVs.
- Asserts the final JSON artifact has at least one ranked entity with
  VIOLIN + BV-BRC provenance fields populated.
- Expected runtime: 30–120s with `mistral-nemo`, 1–4 min with
  `mistral-small`.

### 4.4 Commit + merge

Final commit on the T01 branch. Merge to `apecx-mcp-integration/main`.

---

## Separate work — not on the Task 1–4 chain

### Optional: scope memo 09 — nanobrain library YAMLs with bare `${VAR}`

`library/steps/approval_step.yml` (`${CONTROL_PLANE_URL}`) and
`library/agents/specialized/viral_protein_analysis/config/pssm_parsl_executor.yml`
(`${PBS_JOBID}` inside a shell-script string) will fail-loud under
memo 08 interpolation. Currently not loaded by apecx-mcp-integration
tests so no regression here, but any downstream consumer that loads
them will hit the failure. Appropriate fix:

- `approval_step.yml`: migrate to `${CONTROL_PLANE_URL:-http://localhost:8000}`
  same as the apecx-mcp-integration wrapper YAMLs.
- `pssm_parsl_executor.yml`: escape the shell `${PBS_JOBID}` →
  `$${PBS_JOBID}` so memo 08 leaves it alone and the shell sees the
  intended expansion.

Low priority; file behind a scope memo only if a consumer reports
breakage.

### Optional: workspace ecological cleanup

`apecx-db-integration` is unreproducible without data. We fixed part of
that in Task 1 (env-var for data dir, pyproject). The remaining gaps:

- Data-provisioning docs (where do the VIOLIN CSVs come from? a
  published URL? a gdrive? a one-time internal upload?).
- Pinned deps (`requirements.txt` uses only `>=`, breaking on future
  langchain releases).

Flag these to the user when Task 1 merges; they aren't on this chain.
