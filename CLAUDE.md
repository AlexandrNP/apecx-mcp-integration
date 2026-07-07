# apecx-mcp-integration — repo-local Claude instructions

Workspace-root `../CLAUDE.md` carries cross-repo rules (git discipline,
mocks policy, nanobrain constraints, session distillation). This file
is repo-specific imperatives + pointers — keep it lean (probe 230 caps
at 16 KB).

**Session-start reads (this repo):** this file, then `docs/global_index.md`
(generated code map — every source file → one-line purpose). Consult
`docs/detailed_index.md` (generated per-class/function digest) on demand, not
front-to-back. Regenerate both after code changes with
`.venv/bin/python scripts/gen_index.py`; a pre-commit gate blocks a commit whose
index is stale. NOTE: `CLAUDE.md` is auto-loaded from cwd upward but NOT from
subdirectories — when working from the workspace root, THIS file is not in
context until you read it. See `../CLAUDE.md` → "Multi-repo context loading".

## Python interpreter — MUST use the venv

**`.venv/bin/python` is authoritative. `/opt/anaconda3/bin/python`
is NOT.** Editable installs in the venv: `apecx_integration`,
`apecx_db_integration`, `nanobrain`.

Run pytest as:
```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/...
# or the canonical runner:
scripts/run_tests.sh [path]
```

A `ModuleNotFoundError` for a sibling repo is almost always wrong-Python,
not a real missing dep. See `../_workspace_notes/.../session_friction_log.md`
#14, #15.

## Live-LLM test recipe

Ollama-gated tests (auto-skip when unreachable): see
`tests/integration/test_composer_*_against_ollama.py` and
`tests/integration/test_t01_ac1_against_ollama.py`. Env vars
(`APECX_LLM_BASE_URL`, `APECX_LLM_MODEL`, `APECX_LLM_TEMPERATURE`,
`APECX_LLM_MAX_TOKENS`, `APECX_LLM_API_KEY`) override
`composer_config.yml` at load — see
`composition/composer.py::_apply_llm_env_overrides`.

## Clean-install: never module-scope-import an optional extra

pytest collection IS import — it imports every `test_*.py` to discover tests,
**before** marker deselection. So an unguarded module-scope `import` of an
optional extra (`rag`: faiss/sentence-transformers; `hpc`: globus-sdk/keyring;
`academy`) in `src/` OR a test module aborts the WHOLE `make unit` run on a
clean `pip install -e .[dev]` (which has none of those extras) — `-m "not
integration"` can't save it. **Rule:** lazy-import extras inside the
function/method that uses them; in tests use `pytest.importorskip` at module
top. An optional feature must IMPORT without its extra and degrade LOUDLY (e.g.
`DomainRagIndex.search` → `[]` + a `pip install '.[rag]'` warning), never crash
on import. A base-dep import can still drift — guard imported *symbols* too
(`Transform` was removed from `apecx-harvesters`). **Detection:**
`grep -rnE '^(import|from) (sentence_transformers|faiss|globus_sdk|globus_compute_sdk|keyring|academy)\b' src tests`.
Eval scaffolding must NOT be named `test_*.py` under `tests/` (the bench
`problems/**/test_code.py` templates are excluded via a `tests/conftest.py`
`pytest_ignore_collect` hook). **Source:** 2026-05-21 clean-install audit
(`docs/clean_install_collection_audit_2026-05-21.md`).

## New supervisor onboarding

If you are taking over apecx-composer supervision from a previous
agent or human, start at `docs/supervisor_handbook.md` (marker:
`SUPERVISOR HANDBOOK`). The handbook captures: scope, day-one
checklist, drift patterns D1-D8 observed in real sessions, gates and
rules currently shipped, signals to monitor (`reuse_ratio`,
`compose_retries`, T01 AC1 wall time), termination conditions, and
session-end distillation policy. Pinned by
`tests/unit/test_supervisor_handbook_pinned.py` (28 structural
assertions). DO NOT silently remove sections — see the pin file's
docstring for the right way to retire a section.

## Composer prompt is load-bearing

`composition/composer_prompts/system.md` makes T01 AC1 pass/fail.
Three locked-in constraints (drift patterns, 2026-04-22 + 2026-05-12):
- **No TransformLink** — LLMs hallucinate `transform_function` paths.
  Use DirectLink + novel Python for shape-bridging.
- **Path-reference `config:`** for library components — inline
  `config: {...}` forces hallucinated `input_data_units` /
  `output_data_units` / `triggers` / class paths.
- **Closed-class rule (2026-05-12)** — library component classes
  and their wrapper YAMLs are CLOSED to LLM-authored edits. The
  composer references them; it does NOT edit them. When existing
  components don't fit, the path is (1) pick a different component,
  (2) compose two via DirectLink, or (3) author a NEW class in the
  ``novel_python`` fence with a NEW class path under ``steps:``. The
  marker phrase ``CLOSED-CLASS RULE`` is pinned in 7 prompts by
  `tests/unit/test_closed_class_rule_pinned_in_prompts.py`. Adoption
  rationale: editing a shared class to fit one workflow silently
  breaks every other workflow that depends on it — the YAML loads,
  the cascade fires, downstream shape assumptions diverge.
- **Reuse-first rule (2026-05-12)** — before authoring NEW code,
  check whether an existing library component, stdlib utility, or
  pytest feature already covers the task. Marker ``REUSE-FIRST RULE``
  pinned in 8 prompts (the closed-class 7 plus the composer reviewer)
  by `tests/unit/test_reuse_first_rule_pinned_in_prompts.py`. The
  composer's ``CompositionSummary.reuse_ratio`` property
  (``steps_reused / (steps_reused + steps_generated)``) is the
  single-number adoption signal; ``is_reuse_dominated(threshold=0.8)``
  is the default-policy predicate. Both surface via the existing
  `CompositionSummary` payload so downstream consumers (telemetry,
  reviewer prompt, future quality gates) can read the adoption
  signal without re-deriving it.

**Prompt-budget caps (2026-05-12) — silent-failure guard**

``composer_config.yml`` carries ``prompt_soft_cap_kb`` (default 14.0)
and ``prompt_hard_cap_kb`` (default 16.0). ``Composer.from_config``
applies them to ``system.md`` at load time:

- Hard cap exceeded → ``ComposerConfigurationError`` raise (FAIL-FAST
  per nanobrain discipline). The composer does NOT start.
- Soft cap exceeded → ``log.warning`` + composer starts. Operator
  schedules a consolidation pass.

Defaults are tuned for mistral-nemo (12B), the workspace baseline.
Bigger models (Llama-3-70B, Claude-Sonnet) tolerate larger prompts;
operators override per deployment. ``Composer.prompt_budgets`` is a
read-only dict of ``PromptBudget`` snapshots for telemetry. Regression
catch: ``tests/unit/test_prompt_budget.py::test_current_system_md_is_within_soft_cap_regression``
fails a future PR that pushes system.md past 14 KB BEFORE that PR
lands in main — the silent-failure mode this guards against is "PR
adds a rule, T01 AC1 still passes on a single sample, multi-step
reasoning degrades in production".

**Rule-content asymmetry (2026-05-12, lesson from CW-CO1 consolidation):**

LLM-facing prompts carry imperatives + remedies only. Human-facing
docs (CLAUDE.md, SKILL.md) carry rationale. Adding "Why this is
non-negotiable" prose to a system prompt costs tokens without
changing LLM behavior. Trimmed system.md by 1.18 KB across the
CLOSED-CLASS + REUSE-FIRST blocks; T01 AC1 unaffected.

If AC1 flaps: check this file BEFORE blaming LLM/executor.

## FAISS / sentence-transformers import order

`nanobrain/lightweight/component_index.py` MUST import
`sentence_transformers` BEFORE `faiss` (macOS-ARM segfault otherwise).
File carries `# ruff: noqa: I001, E402` — don't let auto-sort "fix" it.
Session friction log #13.

## RAG index build

### Composer RAG index (small, optional but recommended)

```bash
PYTHONPATH=../nanobrain:src .venv/bin/python \
  scripts/build_rag_index.py \
  src/apecx_integration/composition/composer_config.yml
```
Output: `<config_dir>/rag_index/{faiss.bin,metadata.json}`. Without
built index, composer falls back to Phase-2 linear-scan
ComponentCatalog.

### Domain RAG index (large, OPT-IN since G81 2026-05-16)

For synthesis workflows that wire the domain RAG branch
(`SynthesisContextAssemblyStep`, `UnlimitedSynthesisAssemblyStep`,
`DomainRagSearchStep`):

```bash
apecx-setup rag         # interactive, ~10 minutes; or
apecx-setup --with-rag  # include in the full install chain
```

Output: `data/apecx_domain_rag/{faiss_index.bin,metadata.json}`.
When missing, `DomainRagIndex.search` returns `[]` with a single
loud WARNING per process; the synthesis branch degrades to empty
chunks without crashing. The MCP server prints a `RAG DISABLED`
banner at startup. See `docs/architecture.md` + `docs/FAISS_SETUP_INSTRUCTIONS.md`.

## Globus data transfer — SOLE path (G82 2026-05-16; G127 2026-05-21)

`apecx-setup data` acquires the dataset ONLY via Globus. The legacy
`gh release download` fallback (and `--prefer-gh-release`) were retired
2026-05-21. When Globus is unconfigured AND no data is already present
locally, the data step **FAILS LOUD** with setup instructions — no silent
degradation.

```bash
# One-time credential store (keyring service "nanobrain-globus"):
apecx-globus-setup store --client-id <id> --client-secret <secret>

# Per-shell env vars (see .env.example). DEST is now a hard requirement:
export APECX_GLOBUS_SOURCE_ENDPOINT_ID=<source-uuid>
export APECX_GLOBUS_DEST_ENDPOINT_ID=<your-personal-uuid>   # Globus Connect Personal, running

apecx-setup
```

**Auth (default flipped 2026-05-22):** DEFAULT is NATIVE / web-based
device-code login (thick client, no secret) — `apecx-globus-setup login`
(zero-config; built-in public native client_id, override via
`$APECX_GLOBUS_NATIVE_CLIENT_ID`). The confidential secret path (thin client,
M2M) is OPT-IN via `APECX_GLOBUS_AUTH_MODE=client_credentials` — required for
headless/CI (no browser). `_resolve_auth_env` defaults native even when
confidential creds exist. nanobrain `build_globus_app` native path sets
`request_refresh_tokens=True` (commit `ae5262d`) so native tokens auto-refresh.

The data step drives a 2-step nanobrain workflow
(`configs/globus_transfers/violin_bvbrc_transfer_workflow.yml`):
`GlobusManifestVerifyStep` (G127, fail-loud source-existence gate) →
`GlobusTransferStep` (G28), via `Workflow.run`. **Honesty contract:**
`Workflow.run` swallows a step exception (returns `status:'completed'` with
empty outputs); the driver trusts ONLY `transfer_status=='SUCCEEDED'` and
reconstructs the error from captured `step_failed` events. **Keyring note:**
the preflight reads creds via nanobrain's `globus_credentials.load_credentials`
(service `nanobrain-globus`) — the same place `build_globus_app` resolves them
(fixed 2026-05-21; the two used to disagree). **Source map (both live-verified
2026-05-21, same endpoint 8d2e71d6):** BV-BRC at
`/apecx-ramanathan-anl/public/data/BV-BRC/`; VIOLIN at
`/apecx-ramanathan-anl/apecx-project-all/violin/` — ACL-gated by the
`apecx-project-all` Globus Group (the earlier "Path not allowed" was that ACL
gate, not a separate collection; Group membership unlocked the same path — so
NO per-dataset endpoint is needed). **Required vs optional:** BV-BRC is
REQUIRED; VIOLIN is OPTIONAL — an identity in the Group fetches it (canonical
client is a member → default install gets both), one not in the Group hits the
verify gate → `_step_data` returns `partial` (install completes, exit 0) with a
loud warning. Optionality is an apecx CLI policy layer; the nanobrain verify
step stays strict. Full operator guide: `docs/globus_data_transfer.md`.

## PBS bundle export

`/hpc/export` → qsub-able bundle to disk matching AP §5.5. Route does
NOT submit qsub (scientist runs it). Source:
`execution/pbs_bundle.py`. Tier-2 ingest consumes `provenance_seed.json`
(T05 follow-up).

## Academy integration (real, G5 — 2026-04-24)

Install: `.venv/bin/pip install -e '.[academy]'`. Canonical accessor:
`AcademyIntegration.setup_academy_manager()` returns the process
singleton.

**Lifecycle rules** (load-bearing):
1. First dispatch enters Manager context; held until
   `shutdown_academy_manager()`.
2. Tests touching Academy MUST call `shutdown_academy_manager()` in
   teardown (see `academy_manager` fixture in
   `tests/integration/test_academy_real_integration.py`).
3. `ACADEMY_DEMO_MODE=1` → mock responses + warning log on every call.
4. `register_agent(name, instance)` was removed; use
   `register_agent_class(name, Cls)` or `register_agent_handle(name,
   real_handle)`.

Coverage: `tests/integration/test_academy_real_integration.py` —
6 tests, real local agent, no mocks.

## T13b Docker sandbox (scaffold, NOT wired into composer yet)

Source: `composition/docker_sandbox.py`. `build_docker_sandbox_command(...)`
pins hardening flags (`--network=none`, `--read-only`, `--cap-drop=ALL`,
memory / cpus / pids caps, ro bind mount) via
`tests/unit/test_docker_sandbox_command.py`. **Note**:
`--security-opt seccomp=default` was REMOVED — Docker Desktop on Mac
parses it as a file path; we keep only `--security-opt
no-new-privileges`. `DockerSandboxRunner.run(...)` refuses unless
`APECX_T13B_SANDBOX_EXECUTE=1`. Design doc:
`../_workspace_notes/apecx-mcp-integration_dev_history/t13b_sandbox_design.md`.

## MCP surface (Tier 1)

`mcp_surface/server.py` — FastMCP, **17** scientist-facing tools (the LIVE count — DERIVE it via
`await build_server().list_tools()`; never trust a hardcoded number, three disagreed once — this line
was one of them, F7). Entry:
```bash
apecx-mcp                                       # stdio
APECX_CONTROL_PLANE_URL=... apecx-mcp           # override CP URL
APECX_DATA_ROOT=/path apecx-mcp                 # enable the local DB tool (database_statistics)
APECX_SYNONYM_DICT_PATH=/path apecx-mcp         # enable fast lookup
```

The registered tools (17 at present; the two epitope-assessment workflows landed via #2): run_workflow / inspect_run / inspect_workflow / compose_workflow /
apecx_context (run + compose); list_workflows / describe_workflow / apecx_capabilities /
infrastructure_status (discovery); viral_epitope_analysis / rhea_muscle_alignment / rag_e2e_synthesis
(promoted catalog workflows, registered as first-class tools); harmonized_search / database_statistics
(data); approve_design (operator HITL approval for the evidence workflow's design output — fail-closed +
scope-bound, see `composition/runtime/design_approval_store.py`). The 6 `query_*` DB tools were
deregistered 2026-06-15 (Globus-first); only `database_statistics` remains and needs `APECX_DATA_ROOT`
(returns `{"error": ...}` when unset, never raises).

`list_workflows` / `describe_workflow` read `composer_config.component_catalog_paths` so the model sees
buildable workflows before it drives one via `run_workflow`.

Deliberately NOT exposed: `/hpc/submit` (501), `create_approval`
(internal — nanobrain ApprovalStep).

Full operator reference: `docs/mcp_integration.md`.

## Two operating modes — desktop/MCP vs backend/headless (LOAD-BEARING)

apecx runs in TWO modes; the LLM role differs in each. **Do not conflate them.**

- **Desktop / MCP mode (primary).** The connected MCP client (Claude Desktop /
  IDE) **IS** the orchestrating + synthesizing LLM. apecx tools return
  DETERMINISTIC data + scaffolds for it to reason over — **no apecx-side LLM
  endpoint is required** for analysis. `run_workflow_streaming` streams per-stage
  reasoning to the client (`docs/desktop_streaming_contract.md`).
- **Backend / headless mode.** `run_workflow` synthesizes markdown **internally**
  via the apecx LLM backend — Ollama (`localhost:11434` default) or
  `APECX_LLM_BASE_URL`. **There is NO remote default**; this LLM is OFF until
  Ollama is installed or the env var is set. It is a FALLBACK (bounded local
  decomposition + the few internal-synthesis workflows), not the primary
  orchestrator.

**The mode is selected by the `--locus` startup flag** (NOT an env-primary
toggle): `apecx-mcp --locus desktop|agent` (default `desktop`;
`APECX_EXECUTION_LOCUS` is a fallback). `apecx-setup` writes `--locus desktop`
into the installed config. Single source: `composition/runtime/execution_locus.py`
(re-exported by `mcp_surface/locus.py`). A step's `LLM_ROLE='final_synthesis'`
class attr makes it **omit its apecx LLM call in desktop locus** (returns assembled
evidence + a host-synthesis scaffold — the inversion; no apecx LLM needed), while
`mcp_surface/llm_policy.workflow_needs_llm_at_run` uses locus+role so such a
workflow is not wrongly refused on a desktop with no Ollama. Full design:
`docs/desktop_routing_instructions.md`.

Consequence: "is an LLM available?" has two answers. The desktop frontier LLM
covers the primary analysis path with ZERO apecx LLM config. NEVER claim the
backend endpoint is "optional because you can set a remote URL" — there is no
remote default; it is optional because the DESKTOP LLM covers the primary path.

Design sources (the major architectural designs — read before touching the LLM
or orchestration path): `docs/external_orchestration_design.md` (frontier-LLM
orchestration — the PRIMARY architecture), `docs/architecture.md` §14 (backend vs
user-facing two pipelines), `docs/desktop_streaming_contract.md` (desktop stream).

## Canonical surfaces — reuse, don't rebuild (grep BEFORE you build)

Before adding any "capabilities / health / what's-available / probe" surface, use
the EXISTING ones (this has been rebuilt-by-mistake — don't):
- `mcp_surface/tools/discovery.py::list_workflows` — each workflow's `available` +
  `missing_prerequisites` + `unavailable_hint`, via `check_prerequisites`.
- `mcp_surface/tools/infrastructure_status.py` — live backend roster
  (ollama/postgres/redis/minio/rhea) from the InfraOrchestrator `status()`.
- `mcp_surface/tools/eo_primitives.py::apecx_capabilities` — thin aggregator over
  the two above; the CLI `apecx-setup capabilities` renders it. ONE source of truth.
- `mcp_surface/workflow_registry.py::check_prerequisites` — env/module/binary gate.

Rule: a new top-level module or a parallel `_probe_*` that re-stats
docker/mafft/dict is almost always duplication AND a layer violation — the MCP
server must NOT import the CLI installer. Layer order: `cli` → `mcp_surface` →
`composition` / `infrastructure` / `agents` / `synonym_dictionary`.

## Dev-loop discipline — finish the loop, then clean up

Completed + verified work on a task branch is NOT done until it is merged to main
and the worktree is cleaned up. Standing process (don't park finished arcs on
feature branches "pending approval"):
1. FF-merge the task branch to `main` (**clean fast-forward only** — divergent /
   force-merge still stop-and-ask per the workspace `CLAUDE.md` "Git and Worktree
   Discipline").
2. `git push origin main`.
3. Remove the task worktree (`git worktree remove`) + delete the merged branch.
4. Continue in the `main` worktree unless a NEW task spawns a new worktree.

## Synonym dictionary — nanobrain workflow, lazy at startup

NO console scripts (`apecx-build-dictionary`,
`apecx-fetch-taxdump` removed 2026-05-06). Workflow runs lazily at
`apecx-mcp` startup if artifact missing.

Components: `synonym_dictionary/workflow/{taxdump_fetch_step,
dictionary_build_step}.py` wired by `configs/dictionary_build_workflow.yml`
(DirectLink, `auto_transfer=true`).
`synonym_dictionary/workflow/bootstrap.py:ensure_dictionary` is the
**migration seam** — both MCP startup (a3, current) and future
harvester sink (a1) funnel through it.

`mcp_surface/server.py:_ensure_synonym_dict_or_warn` behavior:
- SQLite exists → skip + warm loader singleton.
- `APECX_SKIP_DICT_BUILD=1` → skip with warning.
- VIOLIN missing under `APECX_DATA_ROOT` → skip with "run apecx-setup".
- Else build (10–15 min first run; <1 s thereafter, idempotent).

Custom-shape tests use
`tests/integration/_dict_build_helper.py:build_dictionary_for_test`.

## E2E RAG synthesis (Day 2)

`composition/workflows/rag_e2e_synthesis/` — two-step "ask question →
grounded Markdown" pipeline.

- `SynthesisContextAssemblyStep` — `asyncio.gather` over 3 branches
  (FAISS / VIOLIN+BV-BRC / PubMed); branch failures degrade to empty
  bundles (test:
  `tests/unit/test_synthesis_assembly_branch_failures.py`).
- `RagSynthesisStep` — single LLM call (`APECX_LLM_*`). Gates
  (size / grounded-citation / empty-retrieval) raise `ValueError`.

Three invocation paths:
1. `synthesize_query` MCP tool (canonical, cached process-wide).
2. `Workflow.from_config(rag_e2e_synthesis_workflow.yml)` —
   nanobrain triggers + links runtime
   (test: `tests/integration/test_rag_e2e_workflow_yaml.py`).
3. Direct step `from_config` + `process()` calls (test:
   `tests/integration/test_rag_e2e_pipeline.py`).

Workspace-root resolution: `APECX_WORKSPACE_ROOT` env first, else
upward walk for markers. Source: 2026-05-05 audit Finding #16; now
delegated to `nanobrain.library.runtime.workspace_root.locate_workflow_root`
(G40-WA-1 retired).

Failure contract per branch: warning + empty list. All-empty →
synthesizer's `fail_on_empty_retrieval` ValueError → MCP tool returns
`{"error": "synthesis gate failed: ..."}`.

## Authoring nanobrain code — skills

9 skills at `.claude/skills/nanobrain-*/SKILL.md` (versioned here).
Load order:

| # | Skill | When |
|---|---|---|
| 1 | `nanobrain-from-config` | FIRST — mandatory pattern |
| 2 | `nanobrain-config-yaml` | Before writing any `*.yml` |
| 3 | `nanobrain-step-authoring` | Before subclassing BaseStep |
| 4 | `nanobrain-data-units-triggers-links` | **carries dominant `auto_transfer` silent-failure warning** |
| 5 | `nanobrain-workflow-authoring` | Before authoring a Workflow YAML |
| 6 | `nanobrain-agents-tools` | Agent/Tool work |
| 7 | `nanobrain-executors` | Changing a step's executor |
| 8 | `nanobrain-testing-debugging` | Writing tests / debugging silent failures |
| 9 | `nanobrain-lightweight` | WorkflowBuilder ergonomic path |

Skills cross-reference; each carries `file:line ground truth` tables.
Update the relevant skill in the same PR as any framework-side change.

## Multiple workflow-authoring paths

Three legit ways to construct workflows (use whichever fits the
task; all are framework-native):

1. **Hand-authored YAML** + `Workflow.from_config(path)` — full
   control over data units / triggers / links. Verbose; required
   when authoring nontrivial DAGs.
2. **`Workflow.from_skeleton(skeleton, bindings)`** (G9) — skeleton
   carries the topology with typed `{{name: type}}` placeholders;
   bindings provide concrete components. Collapses
   PlanLoweringStep + SkeletonLoaderStep into one call.
3. **Lightweight `WorkflowBuilder`** (`nanobrain.lightweight`) —
   programmatic API: `.add_step(name, ...)`, `.add_link(...)`,
   `.add_trigger(...)`, `.load()`. Best for code-generated / agent-
   composed workflows where the LLM produces Python, not YAML.

## Data-unit I/O contracts (Project A, 2026-06-23)

Workflow data units can declare an optional **`contract`** (gradual typing) so producer→consumer
interface drift is CAUGHT, not silently consumed (the G7/G127 silent-failure class). Authoring
guide: `docs/contract_authoring.md`. Algebra + runtime guard: nanobrain
`nanobrain/core/data_contract.py` (`parse_contract`, `compatible`, `validate_value`, `ContractViolationError`).

- **Kind lattice**: `text | file | record | collection | handle` (+ optional refinement). There is
  NO scalar bool/int/float kind — put such keys in `record` `required_keys` (presence, any), NOT
  typed (a bool typed as `record` is a false v3 violation).
- **Gradual + both-endpoints**: an undeclared side is `any` (compatible). A DirectLink boundary is
  *covered* only when BOTH endpoints declare a contract. Make them compatible (consumer `required`
  ⊆ producer guaranteed). **Passthrough inputs stay undeclared** — a key a generic producer only
  forwards (not from its own logic) can't be statically guaranteed (decl-vs-decl limit).
- **Enforcement is opt-in**: `config_version<3` WARNs at load (non-binding); `config_version: 3`
  RAISES (load-checker + runtime `DataUnitMemory.set()` guard on the actual value).
- **WARN-ratchet** (`tests/integration/test_contract_ratchet.py`, `BASELINE` — only ever LOWER it):
  counts boundaries lacking both-endpoint coverage (`composition/contract_coverage.py`); a PR adding
  an unannotated boundary fails. Corpus coverage is ~76% (baseline 34).
- **Documented-feed bug-detector** (`tests/integration/test_documented_feed_compatibility.py`): a
  wrapper that DOCUMENTS feeding a consumer's input DU must be contract-compatible with it. This
  caught 2 real composer bugs (entity_extraction→assembly, analysis→summarize, both fixed). When you
  add a component whose wrapper names another's input DU, ensure the shapes match.

## Design package

Index: `docs/_design_index.md`. Implementation plan:
`docs/implementation_task_graph.md` (165 file-level tasks across 4
tracks with stable IDs). Cite task ID in PR/commit body.

## Key reference docs

- `docs/architecture.md` — canonical end-to-end map (8 Mermaid
  diagrams, MCP tools, ontologies, invocation paths, test surface,
  failure contract). §14 = backend vs user-facing two pipelines.
- `docs/external_orchestration_design.md` — **PRIMARY architecture**:
  frontier-LLM (desktop) orchestration; deterministic nanobrain
  scaffolds; local LLM as bounded fallback. See "Two operating modes".
- `docs/desktop_streaming_contract.md` — desktop/MCP per-stage
  streaming contract (`run_workflow_streaming`).
- `docs/desktop_routing_instructions.md` — client-side routing rule
  that makes the desktop LLM call APECx FIRST for bio questions
  (server `instructions=` is advisory + insufficient; the trigger
  lives in the client's Custom/Project instructions).
- `docs/clean_install_capabilities_scoring.md` — capability matrix +
  the two LLM modes + per-mode end-to-end scoring.
- `docs/_design_index.md` — design master index.
- `docs/nanobrain_capability_gaps.md` — G1-G45 framework gap
  proposals (most shipped; rest paired with `docs/WORKAROUND_INVENTORY.md`).
- `docs/mcp_integration.md` — operator-facing MCP install/reference.
- `../architectural_plan.md` / `../implementation_plan.md` —
  project-level sources of truth.
- `../_workspace_notes/apecx-mcp-integration_dev_history/` —
  `composer_task_spec.md`, `workflow_spec.md`,
  `session_friction_log.md`, `nanobrain_mock_audit.md`,
  `t13b_sandbox_design.md` (moved out of repo 2026-04-28).
