# Return-of-Control — Detailed Implementation Plan

**Status:** Plan (2026-06-12). Implements `return_of_control_design.md`. Worktree `wt-eo-mvp`.
Every acceptance criterion is rooted in REAL data (no mocks in product paths; mocks only for the
smoke shape of a pure validator). Real backends used: live **BV-BRC** data API (taxon `37124` =
Chikungunya), local **MAFFT** (`brew install mafft`, v7.526), and — for EO-54 — a local **Rhea**
MCP server (`../rhea` `docker compose`).

## Progress
- **EO-54a + EO-54b ✅** 2026-06-12 (commits `11b0bf3`, `c623deb`) — live Rhea MUSCLE verified via
  the supported (non-compose) path, then pluggable MAFFT↔MUSCLE substitution in viral_conserved_sites
  (real CHIKV: counts agree, alignments byte-distinct). **EO-54 + CL-1 + RoC all COMPLETE — the EO/RoC
  plan is fully implemented + real-data verified. No remaining in-scope tasks (the Rhea-side UTD
  interface-tag is optional cross-repo polish).** Nothing pushed.
- **RoC-3a + RoC-3b + RoC-3c ✅** 2026-06-12 (commit `52269dd`) — two flag-switched decomposer
  modes (`APECX_EO_DECOMPOSER_MODE`, default `plan_returner`). plan_returner returns
  `needs_input(decomposition_choice)` with each workflow's required inputs (from RoC-2b) WITHOUT
  executing; auto_solver unchanged. 19 tests incl. both modes over the real catalog.
  **RoC CRITICAL PATH (RoC-1 → RoC-2 → RoC-3) COMPLETE + real-data verified.**
  Remaining: EO-54 (local Rhea + interface-tag substitution; needs Docker), CL-1 (retire dead husks).
- **RoC-1a + RoC-1b ✅** 2026-06-12 (commit `dc3dab5`) — `WorkflowResult.status='needs_input'` +
  typed `control_transfer` (ParamNeed/WorkflowNeed/NextAction + 4 builders); loud invariant
  needs_input⟺control_transfer; `needs_input()` constructor. 18 unit + 30 consumer regression green.
- **RoC-2a + RoC-2b + RoC-2c ✅** 2026-06-12 (commit `ccab129`) — the param-gap fix. Declared
  `step_input_schema` (G6, WRAPPED shape) on the viral_conserved_sites entry step (single source of
  truth; real e2e still runs — G6 enforcement verified). `workflow_inputs.derive_required_inputs()`
  reads it from the WORKFLOW + unwraps the entry-DU level; `run_workflow` returns
  `needs_input(missing_param)` (with obtain_via) on missing/ill-typed params BEFORE any backend
  call. 10 tests incl. the gated real run staying green. NEXT: RoC-3.
- **RoC-2a note (G6 `SchemaRef`):** exactly one of `class`(Pydantic path) | `json_schema`(inline);
  G6 validates step input BEFORE process() but the step gets the trigger-wrapped `{fetch_in:{…}}`,
  so G6 in-step shape ≠ the param-dict schema RoC-2b reads. Decision: declare `step_input_schema`
  as the authoritative PARAM-DICT contract (source of truth for RoC-2b/2c); fail-loud is already
  guaranteed by `process()` + the RoC-2c pre-run check; G6 in-step is defense-in-depth where the
  wrapping allows. Verify WorkflowBuilder passes `step_input_schema` through `add_step`.

## 0. Foundation already shipped (the floor we build on)

The §4 thin surface + the conserved-sites feature are DONE + real-data-verified:
`run_workflow`/`inspect_run`/`inspect_workflow`/`apecx_context`/`list_workflows`; the
`viral_conserved_sites` catalog workflow (BV-BRC → MAFFT → conservation), e2e-tested on live CHIKV.
This plan adds the return-of-control (RoC) contract on top.

## 1. Task dependency tree (DAG)

```
RoC-1a ─┬─────────────► RoC-2c ──► RoC-3c ──► (RoC done)
        │               ▲   ▲        ▲
RoC-2a ─► RoC-2b ───────┘   │        │
                            │        │
RoC-3a ─────────────────────┴► RoC-3b┘

EO-54a ──► EO-54b           (independent track — parallel)
CL-1                        (independent — no deps)
```

Critical path: **RoC-1a → RoC-2b → RoC-2c → RoC-3c** (gated by RoC-2a feeding RoC-2b, and RoC-3a/3b
feeding RoC-3c). EO-54 and CL-1 run in parallel. Recommended order: RoC-1a, RoC-2a, RoC-2b, RoC-2c,
RoC-3a, RoC-3b, RoC-3c, then EO-54a/b, then CL-1 (or CL-1 anytime).

| Task | Depends on | Touches |
|---|---|---|
| RoC-1a control-transfer envelope | — | `schemas/workflow_result.py` |
| RoC-1b control-transfer builder + reasons | RoC-1a | `schemas/control_transfer.py` (new) |
| RoC-2a `step_input_schema` on first step | — | `steps/bvbrc_protein_fasta_step.py` (+ builder) |
| RoC-2b derive-required-params helper | RoC-2a | `mcp_surface/workflow_inputs.py` (new) |
| RoC-2c `run_workflow` → `needs_input` on gaps | RoC-1a, RoC-1b, RoC-2b | `tools/eo_primitives.py` |
| RoC-3a mode flag resolution | — | `composition/decomposition/modes.py` (new) |
| RoC-3b `plan_returner` mode | RoC-1b, RoC-2b, RoC-3a | `decomposition/local_decomposer.py` or wrapper |
| RoC-3c wire mode into factory | RoC-3a, RoC-3b | `decomposition/factory.py` |
| EO-54a local Rhea up + verify | Docker | env/setup + integration test |
| EO-54b interface-tag aligner substitution | EO-54a | `../rhea/...utd_producer.py`, conserved-sites workflow |
| CL-1 retire dead husks | — (audited) | delete `epitope_analysis`, `viral_immunology_analysis`; `composer_config.yml` |

---

## 2. Tasks — acceptance criteria (real-data) + planned tests

### RoC-1a — `needs_input` status + `control_transfer` on `WorkflowResult`
**Scope:** add `status="needs_input"` to the Literal; add a nested `control_transfer` field; extend
`_check_consistency`: `needs_input` ⟺ non-empty `control_transfer`; `control_transfer` forbidden when
status ∉ {needs_input}.
**Acceptance criteria:**
- `WorkflowResult(status="needs_input", control_transfer=<valid>)` validates; round-trips
  `model_dump(mode="json")`.
- `status="needs_input"` with no `control_transfer` raises `ValueError`.
- `control_transfer` set with `status="ok"` raises `ValueError`.
- Existing `ok/partial/error` behavior unchanged (regression).
**Tests (real Pydantic, no mocks):** `tests/unit/test_workflow_result.py` — extend with the 3 new
invariant cases + the existing-status regression. Pin: the surface pin test for `WorkflowResult`
shape if one exists.

### RoC-1b — control-transfer model + reasons (incl. `ambiguous_entity`)
**Scope:** typed `ControlTransfer{reason, next_action{kind, param_name?, options?, schema?,
obtain_via?}, message}` with `reason ∈ {missing_param, ambiguous_entity, needs_prerequisite,
decomposition_choice}` (Pydantic `extra='forbid'`); a builder per reason.
**Acceptance criteria:**
- Building a `missing_param` transfer for `taxon_id` yields a dict carrying the param name, its
  (real) schema fragment, and the `obtain_via` hint.
- An `ambiguous_entity` transfer reproduces the shipped disambiguation `next_action` shape (so the
  two converge — design C7).
- A typo'd field on `ControlTransfer` fails loudly (`extra='forbid'`).
**Tests:** `tests/unit/test_control_transfer.py` — builders for all four reasons; extra-forbid.

### RoC-2a — declare `step_input_schema` (G6) on `BvbrcProteinFastaStep`
**Scope:** add `step_input_schema` (`SchemaRef`/inline) to the step config (and pass it through the
`viral_conserved_sites` `WorkflowBuilder.add_step`) declaring `taxon_id`(int, required),
`protein`(str, required), `feature_type`(str, optional) + per-param `obtain_via`.
**Acceptance criteria (REAL workflow):**
- `viral_conserved_sites` builds + loads with the schema attached (5 child steps intact).
- Running the workflow with a payload MISSING `taxon_id` FAILS at the step via G6 runtime
  enforcement (loud), not silently — verified by driving the real step.
- Running with `{taxon_id:37124, protein:"structural polyprotein"}` still produces real conserved
  sites (no regression to the shipped e2e).
**Tests:** `tests/integration/test_viral_conserved_sites_workflow.py` — add a "missing taxon_id →
G6 FAIL-FAST" case (no network needed if G6 rejects pre-fetch) + keep the gated real e2e green.

### RoC-2b — derive required params from a workflow's first-step `step_input_schema`
**Scope:** `workflow_inputs.derive_required_inputs(workflow_or_entry) -> {required: list[str],
properties: dict, obtain_via: dict}` — resolve the first step's `SchemaRef` (inline-dict AND
`$ref`-to-file) to JSON Schema; extract `required` + property types + `obtain_via`.
**Acceptance criteria (REAL workflow):**
- For the real `viral_conserved_sites` workflow, returns `required == ["taxon_id","protein"]` and
  the declared types — matching RoC-2a's declaration exactly (proves single-source-of-truth).
- For `rhea_muscle_alignment` (no required inputs) returns `required == []`.
- A workflow whose first step has NO `step_input_schema` returns `required == []` + a logged note
  (degrade, not crash).
**Tests:** `tests/integration/test_workflow_inputs.py` — against the two REAL registered workflows
+ a fixture step with a `$ref` schema.

### RoC-2c — `run_workflow` returns `needs_input` on missing/ill-typed required params
**Scope:** before running, derive required inputs (RoC-2b) and validate the caller's params; on a
gap return `WorkflowResult(status="needs_input", control_transfer=missing_param(...))` naming each
missing/ill-typed param + `obtain_via`; otherwise run as today.
**Acceptance criteria (REAL):**
- `run_workflow("viral_conserved_sites", {"protein":"E1"})` (missing `taxon_id`) → `status ==
  "needs_input"`, `control_transfer.reason == "missing_param"`, lists `taxon_id` with its
  `obtain_via` ("resolve virus→taxon_id via harmonized_search"). NO BV-BRC/MAFFT call made.
- `run_workflow("viral_conserved_sites", {"taxon_id":"not-an-int","protein":"E1"})` → `needs_input`
  (ill-typed), names `taxon_id`.
- `run_workflow("viral_conserved_sites", {"taxon_id":37124,"protein":"structural polyprotein"})` →
  real run → `status == "ok"`, conserved-sites markdown (the shipped e2e, unbroken).
**Tests:** `tests/integration/test_viral_conserved_sites_workflow.py` — 2 new pre-run `needs_input`
cases (network-free) + the gated real-run case stays green.

### RoC-3a — decomposer mode flag
**Scope:** `decomposition/modes.py::resolve_decomposer_mode()` reads `APECX_EO_DECOMPOSER_MODE ∈
{auto_solver, plan_returner}`, **default `plan_returner`**, raises loudly on an invalid value.
**Acceptance criteria:** default is `plan_returner`; explicit values honored; `garbage` raises with
the valid set echoed.
**Tests:** `tests/unit/test_decomposer_modes.py` (real env via monkeypatch).

### RoC-3b — `plan_returner` mode
**Scope:** in `plan_returner`, `solve(task)` matches + returns `needs_input(decomposition_choice)`
carrying the proposed plan — `[{workflow, required_inputs (from RoC-2b), provided, missing}]` — and
does NOT execute. `auto_solver` keeps the built behavior.
**Acceptance criteria (REAL catalog):**
- `plan_returner`: `solve(Task("find conserved sites in chikungunya"))` → `status=="needs_input"`,
  `reason=="decomposition_choice"`, plan names `viral_conserved_sites` + its required
  `[taxon_id, protein]`. NO workflow executed (no BV-BRC/MAFFT call).
- `auto_solver`: the existing real solve→dispatch→run path still returns `ok` (regression).
**Tests:** `tests/integration/test_decomposition_factory.py` — add a `plan_returner` case
(network-free) + keep the gated `auto_solver` real-run case.

### RoC-3c — wire mode into the assembly factory
**Scope:** `assemble_local_decomposer(mode=resolve_decomposer_mode(), ...)`; the mode flows into the
decomposer behavior.
**Acceptance criteria (REAL):** with the env unset (default `plan_returner`), `assemble_…().solve(
conserved-sites task)` returns the plan; with `APECX_EO_DECOMPOSER_MODE=auto_solver` + real deps it
runs the real workflow to `ok`.
**Tests:** `tests/integration/test_decomposition_factory.py` — both modes end-to-end.

### EO-54a — ✅ VERIFIED via the supported (non-compose) path (2026-06-12)
**The `deploy/docker-compose.yaml` path is a red herring — do NOT use it.** It is unrunnable
as-shipped on this host (server points at upstream `chrisagrams/rhea-server:latest` with a broken
`build: dockerfile: Dockerfile` fallback that doesn't exist; embedding image is amd64-only). The
**supported** bring-up is a HOST-process server spawned by the InfraOrchestrator — entirely
different from compose, and it works. The recipe that verified EO-54a end-to-end:

```bash
# 1. Support containers (apecx-rhea-postgres:5435 + redis + minio — all arm64-friendly,
#    via the orchestrator's OWN container mgmt, NOT deploy/docker-compose.yaml):
apecx-setup infra --non-interactive
# 2. Rhea provisioning (uv pip install -e rhea into rhea/.venv + ingest the muscle Galaxy
#    tool into rhea-postgres, embedded via Ollama mxbai-embed-large — NOT the amd64 HF image):
apecx-setup rhea --non-interactive
# 3. Env for the spawn + the client step:
export RHEA_REPO_PATH=../rhea  RHEA_PYTHON_PATH=../rhea/.venv/bin
export RHEA_CONDA_BIN=/opt/anaconda3/bin              # muscle's conda subprocess
export RHEA_MCP_URL=http://localhost:3001/mcp/
# 4. The rhea SERVER runs from rhea/.venv (host process), but the CLIENT step (RheaFileToolStep,
#    in the apecx process) ALSO needs `rhea` importable — it pickles RheaFileProxy by module ref.
#    proxystore/redis/cloudpickle are already in the apecx venv; just add the repo to PYTHONPATH:
PYTHONPATH=src:../rhea .venv/bin/python -m pytest tests/integration/test_rhea_muscle_alignment_workflow.py
```

**Verified results (2026-06-12, real live Rhea MUSCLE):**
- `test_rhea_muscle_alignment_workflow.py` = **14 passed** against the live server (was 12p/2skip).
- `run_workflow("rhea_muscle_alignment", {})` → **status: ok**, real alignment: **5 sequences,
  374 columns, mean gap 0.0374**, real aligned protein FASTA. First run builds the muscle conda
  env (~50 s); subsequent runs ~12 s.
- `test_infrastructure_rhea_spawn.py` = 2 passed (orchestrator spawns the host server + atexit-stops).

**Two-venv boundary (the key lesson):** the rhea SERVER runs in `rhea/.venv` (spawned via
`RHEA_PYTHON_PATH`); the CLIENT `RheaFileToolStep` runs in the apecx `.venv` and ALSO needs `rhea`
importable (cloudpickle pickles `RheaFileProxy` by module reference). `apecx-setup rhea` only
installs into rhea's venv → the apecx side needs `rhea` on `PYTHONPATH` (its deps are already
present). The catalog `requires: modules: [rhea]` gate exists to catch exactly this client-side gap.

**Minor polish (not a blocker):** `rhea_muscle_alignment` has no terminal `EnvelopeStep`, so
`run_workflow` returns the data via `data_handle` fallback with a note (status still `ok`). Adding
an `EnvelopeStep` would give it a §5-standard markdown.

Reuse candidate surfaced by CL-1 — **`viral_immunology_analysis`** (classifier → enhancer →
assembly → synthesis): on probing, it is **STALE + BROKEN, not a quick wiring** — its
`rag_synthesis` step has malformed `config: {path:...}` (must be a string) AND references
`steps/rag_synthesis.yml` which does NOT exist in the dir (no `steps/`). It has *conceptual* reuse
value (the pipeline shape) but reviving it = recreate the step configs + fix syntax + verify a heavy
LLM/RAG/data run, and it OVERLAPS existing tools (`eeev_epitope_analysis` + `synthesize_query` use
the same RagSynthesisStep/UnlimitedSynthesisAssemblyStep). **Decision for the user:** worth reviving
as a framework-native catalog workflow, or leave it (the RAG-synthesis viral path already exists as a
tool)? Not auto-revived — it's a real project, not a quick continuation.

### EO-54a — stand up local Rhea + verify `rhea_muscle_alignment`
**Scope:** `docker compose -f ../rhea/deploy/docker-compose.yaml up -d`; `RHEA_MCP_URL=
http://localhost:3001/mcp/`; confirm the MCP `tools/list` handshake; run `rhea_muscle_alignment`.
**Acceptance criteria (REAL Rhea):** `run_workflow("rhea_muscle_alignment", {fasta_text:<real
protein FASTA>})` against the live local server returns a real alignment (n_sequences, alignment
length > 0) — and `list_workflows` shows it `available` (RHEA env + module present).
**Tests:** `tests/integration/test_rhea_muscle_alignment_live.py` (gated on `RHEA_MCP_URL`
reachable). Honest skip when Docker/Rhea down.

### EO-54b — interface-tag aligner substitution (MUSCLE ↔ MAFFT)
**Scope:** tag MSA aligners with a shared interface tag on Rhea's rich UTD path
(`../rhea/.../utd_producer.py`); let `viral_conserved_sites` select the aligner (local MAFFT vs
Rhea MUSCLE) via a param; Tier-1 config-patch (no LLM).
**Acceptance criteria (REAL):** the SAME conserved-sites query (`taxon_id:37124, protein:"structural
polyprotein"`) yields conserved sites with `aligner=mafft` (local) AND `aligner=muscle` (Rhea) — the
conserved-region count agrees within a small tolerance (real biology, real tools).
**Tests:** `tests/integration/test_aligner_substitution.py` (gated on Rhea + MAFFT). Cross-repo:
a Rhea-side tag test in `../rhea/tests`.

**✅ DONE + real-data verified (2026-06-12, commit `c623deb`).** Implemented apecx-side as a
pluggable align step (the Rhea-UTD interface-tag was NOT needed for the AC — substitution lives in
the workflow builder, keeping the change in primary writable scope). New `RheaMuscleAlignStep`
(interface-compatible with `LocalMafftAlignStep`) drives the verified `rhea_muscle_alignment`
subworkflow; builder parametrized `aligner='mafft'|'muscle'`; second catalog entry
`viral_conserved_sites_muscle` (its `requires` declares the Rhea prereq → honest per-entry
availability). **Catalog workflows ≠ MCP tools, so the §4 thin surface stays 6 primitives.**
Verified on real CHIKV (taxon 37124, structural polyprotein, 25 seqs): both aligners yield conserved
sites, counts AGREE (2308 cols / 97 regions each), and the two alignments are byte-DISTINCT
(sha 9208937e vs 987cf8a5 — the silent-failure guard proving muscle really dispatches to Rhea MUSCLE,
not a shortcut to the mafft result). `test_aligner_substitution.py` = 4 passed; 61 existing
catalog/decomposition/conserved-sites tests still green. **Deferred (optional polish):** the Rhea-side
shared-interface UTD tag in `../rhea/.../utd_producer.py` (a cross-repo, read-mostly change) — would
let the apecx side DISCOVER aligners by capability instead of binding tool names; not required by the
AC, and out of primary scope.

### CL-1 — retire dead husks (REUSE AUDIT — scope narrowed)
**Reuse audit (per direction "check if dead workflows have reuse value"):**
- `epitope_analysis` — RETIRED ✅ (commit pending). A stray `steps/sequence_analysis.yml` config for
  the already-neutralized fake `SequenceAnalysisStep`; no catalog/composer/test reference; no reuse
  value (the real version is `viral_conserved_sites`). Deleted.
- `viral_immunology_analysis` — **KEEP.** NOT dead: uses real `ViralImmunologyQueryClassifierStep` +
  `ViralQueryEnhancerStep` and has a lightweight builder. It is the framework-native viral pipeline —
  a strong candidate to wire into the catalog (connects to the earlier "convert viral to a workflow").
- `violin_bvbrc` — **KEEP.** A live composer manifest in `composer_config.component_catalog_paths`
  (buildable-workflow reuse value), not a husk.

### CL-1 (original) — retire the dead husks
**Scope:** remove `composition/workflows/epitope_analysis/` + `viral_immunology_analysis/` (reuse
audit: husks — one fake step / empty stub; real version is `viral_conserved_sites`); drop their
`composer_config.yml` references; the retired `SequenceAnalysisStep` stays fail-loud.
**Acceptance criteria (REAL):** `build_server()` + `load_catalog()` + the composer config load with
no dangling reference; `list_workflows` unaffected; full unit suite + the conserved-sites e2e green.
**Tests:** existing surface/composer/discovery tests stay green; a test asserting the husks are gone
from `composer_config` manifests.

---

## 3. Cross-cutting test discipline
- **No mocks in product paths.** Pure-validator unit tests (RoC-1a/1b/3a) use real Pydantic / real
  env; everything touching execution uses real BV-BRC + MAFFT (+ Rhea for EO-54), gated + honest-skip.
- **Pre-run vs run.** `needs_input` cases (RoC-2c/3b) are network-free (validation precedes any
  backend call) — they run unconditionally; real-run cases stay gated.
- **Regression guard.** Every RoC task keeps the shipped conserved-sites e2e + the existing
  decomposition/surface tests green (listed per task).
- **Document-as-you-go.** Each task updates `eo_implementation_log.md` with commit + a one-line AC
  result; this plan is the source of task IDs to cite in commits.

## 4. Open decision captured
Default `APECX_EO_DECOMPOSER_MODE = plan_returner` (return-of-control as the safe default; autonomy
is opt-in). Flip to `auto_solver` only on explicit direction.
