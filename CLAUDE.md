# apecx-mcp-integration — repo-local Claude instructions

Workspace-root `../CLAUDE.md` carries the cross-repo rules (git
discipline, mocks policy, nanobrain framework constraints, session
distillation directive). This file carries repo-specific details
that a fresh Claude session needs to skip friction already paid for.

## Python interpreter — use the venv

**`.venv/bin/python` is authoritative. `/opt/anaconda3/bin/python`
is NOT.** The project's editable installs live in the venv:

- `apecx_integration`        (this repo)
- `apecx_db_integration`     (sibling repo `../apecx-db-integration`)
- `nanobrain`                (sibling repo `../nanobrain`)

Running `python -m pytest ...` from the shell picks up whichever
interpreter is first on `PATH`. On this laptop that is the system
anaconda, which does not see any of the editable installs. Symptoms:
`ModuleNotFoundError: No module named 'apecx_db_integration'` or
`... 'nanobrain'` on a test file that clearly imports them.

**Always invoke pytest via the venv explicitly:**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/...
```

Or — easier — use the canonical runner:

```bash
scripts/run_tests.sh              # full suite
scripts/run_tests.sh tests/unit   # a subset
```

It sets ``PYTHONPATH=src``, uses ``.venv/bin/python``, and runs from
the repo root. Pass any pytest args after the path.

See `docs/session_friction_log.md` #14, #15.

## Live-LLM test recipe

Three test files are gated on a reachable Ollama (auto-skip when it
isn't). Recipe:

```bash
# Ollama must be running + mistral-nemo:latest pulled.
APECX_LLM_BASE_URL=http://localhost:11434/v1 \
APECX_LLM_MODEL=mistral-nemo:latest \
APECX_LLM_TEMPERATURE=0.0 \
APECX_LLM_MAX_TOKENS=2048 \
APECX_LLM_API_KEY=unused \
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/integration/test_composer_phase2_against_ollama.py \
  tests/integration/test_composer_ac7_composition_bias.py \
  tests/integration/test_t01_ac1_against_ollama.py \
  tests/integration/test_composer_ac8_walltime.py -v
```

The env vars override `composer_config.yml` at load time — see
`src/apecx_integration/composition/composer.py::_apply_llm_env_overrides`.

## Composer prompt engineering is load-bearing

`src/apecx_integration/composition/composer_prompts/system.md` is
not documentation — it's the file that makes T01 AC1 pass or fail.
Two drift patterns broke AC1 during development (2026-04-22) and
are now explicitly constrained:

1. **No TransformLink.** LLMs hallucinate `transform_function`
   import paths. The prompt forbids TransformLink; use DirectLink
   + novel Python when shape-bridging is required.
2. **Path-reference `config:` for library components.** Inline
   `config: {...}` forces the LLM to reproduce `input_data_units`
   / `output_data_units` / `triggers` blocks and hallucinate their
   class paths. The prompt mandates `config: "<wrapper_yaml>"`.

If AC1 starts flapping again, check this file BEFORE blaming the
LLM or the executor.

## FAISS / sentence-transformers import order

`nanobrain/nanobrain/lightweight/component_index.py` imports
`sentence_transformers` BEFORE `faiss`. Load-bearing — reversing
the order causes a silent segfault on macOS ARM. The file carries
a `# ruff: noqa: I001, E402` + a comment explaining why. Do not
let an auto-sort "fix" that. See `docs/session_friction_log.md` #13.

## RAG index build

The composer's RAG backend loads from `rag_index_dir` if set. Build
the index out-of-band:

```bash
PYTHONPATH=../nanobrain:src .venv/bin/python \
  scripts/build_rag_index.py \
  src/apecx_integration/composition/composer_config.yml
```

Output is `<config_dir>/rag_index/{faiss.bin,metadata.json}` by
default (override with `--out`). Without a built index, the
composer falls back to the Phase-2 linear-scan ComponentCatalog.

## PBS bundle export

`/hpc/export` writes a full qsub-able bundle to disk for a Run. The
bundle layout matches AP §5.5 exactly (submit.pbs / run.sh /
workflow.yml / staging_plan.yml / provenance_seed.json / README.md).
The route does NOT submit via qsub — scientist runs qsub manually.
Tier-2 ingest on completion consumes `provenance_seed.json`
(consumer route is T05 follow-up scoped when an operator exercises
the AC2 round-trip on Polaris / Aurora).

See `src/apecx_integration/execution/pbs_bundle.py`.

## Academy integration (real, as of G5 — 2026-04-24)

`nanobrain/core/academy_integration.py` now has a working real
Academy path. Before G5, ``AcademyAgentHandle.__call__`` raised
``AcademyNotImplementedError`` in the non-demo branch; today it
dispatches through a real ``academy.handle.Handle`` via a
process-level ``AcademyManagerWrapper`` that owns the
``academy.manager.Manager`` async context.

**Use the ``academy`` extra:**

```bash
.venv/bin/pip install -e '.[academy]'
# or, if the venv already exists and you just want to add it:
.venv/bin/pip install academy-py
```

**Lifecycle rules:**

1. First call to any dispatched action enters the Manager context.
   The context is held process-wide until ``shutdown_academy_manager()``
   is called.
2. Tests that touch Academy MUST call ``shutdown_academy_manager()``
   in teardown (see the ``academy_manager`` fixture in
   ``tests/integration/test_academy_real_integration.py``) —
   leaving the singleton entered bleeds state across tests.
3. ``ACADEMY_DEMO_MODE=1`` preserved: the aurora demo
   (``demos/academylink_aurora_demo``) still gets synthesized
   mock responses. A warning log line is emitted on every call so
   operators cannot miss that they are on the demo path.
4. Direct instantiation of ``AcademyManagerWrapper`` is supported
   for tests but the canonical accessor is
   ``AcademyIntegration.setup_academy_manager()`` — it returns the
   process singleton.

**Registration API:**

- ``await mgr.register_agent_class(name, Cls)`` — launches a fresh
  Academy agent and stores its real Handle under ``name``.
- ``mgr.register_agent_handle(name, real_handle)`` — registers a
  pre-launched ``academy.handle.Handle`` under ``name`` (use when
  the agent was launched by another process).
- ``mgr.register_agent(name, instance)`` — removed. Academy owns
  agent lifecycle; the old inline-instance API made no sense. Calls
  now raise ``AcademyNotImplementedError`` with a migration hint.

Positive-path coverage:
``tests/integration/test_academy_real_integration.py`` —
six tests launching a real local Academy agent, no mocks, clean
shutdown.

nanobrain is not a git repo on this workspace, so the integration
test is the durable artifact: a re-fetch of nanobrain that reverts
``core/academy_integration.py`` turns every test in that file red.

## T13b Docker sandbox (scaffold only)

`src/apecx_integration/composition/docker_sandbox.py` is the
Phase-2 runtime-isolation scaffold that backstops T13's static
import-whitelist scanner. **It is not yet wired into the composer's
execution path — that is Phase-3 work.**

- `build_docker_sandbox_command(...)` — pure argv construction.
  Every hardening flag (`--network=none`, `--read-only`,
  `--cap-drop=ALL`, seccomp default, memory / cpus / pids caps,
  read-only bind mount) is pinned by
  `tests/unit/test_docker_sandbox_command.py`. Weakening a flag
  there requires updating the threat-model table in the design
  doc in lockstep.
- `DockerSandboxRunner.run(...)` — real `docker run` invoker. Refuses
  to execute unless `APECX_T13B_SANDBOX_EXECUTE=1` is set, so CI
  runs of the full test suite do NOT shell out to Docker.
- Live-sandbox tests in `tests/integration/test_docker_sandbox_runtime.py`
  are double-gated (env var + Docker daemon reachable) and skip by
  default.

Design doc: `docs/t13b_sandbox_design.md` (threat model, flag
rationale, open Phase-3 design questions).

## MCP surface (Tier 1)

`src/apecx_integration/mcp_surface/server.py` is a FastMCP server
exposing 20 scientist-facing tools. Entry point:

```bash
apecx-mcp                                   # stdio transport
APECX_CONTROL_PLANE_URL=http://.../  apecx-mcp   # override CP URL
APECX_DATA_ROOT=/path/to/data apecx-mcp          # enable DB tools
```

Tools by module:

- `tools/workflows.py` (3): start_workflow, show_diff, execute_workflow
- `tools/discovery.py` (2): list_workflows, describe_workflow
- `tools/database_tools.py` (7): query_vaccines, query_pathogens,
  query_genes, query_bvbrc_genomes, get_vaccine_pathogen_genes,
  resolve_entity, database_statistics
- `tools/approvals.py` (4): list_pending_approvals, approve, reject, correct
- `tools/hpc.py` (4): estimate_cost, confirm_allocation, export_hpc_bundle,
  ingest_hpc_bundle

`list_workflows` / `describe_workflow` are the discovery surface
(2026-04-27): they read the composer config's
`component_catalog_paths` so the model can see which workflows /
components the composer can build BEFORE calling start_workflow.

`query_vaccines`, `query_pathogens`, `query_genes`,
`query_bvbrc_genomes`, `get_vaccine_pathogen_genes`, `resolve_entity`,
`database_statistics` are direct-lookup tools (2026-04-27, B-1
vendor): bypass the composer for one-shot VIOLIN + BV-BRC queries.
Data layer is vendored from `apecx-mcp/src/apecx_mcp/database.py`
into `mcp_surface/data/database.py` (pure pandas, no LLM). Requires
APECX_DATA_ROOT or APECX_ROOT to point at the workspace data dir;
when unset the tools return `{"error": "..."}` rather than raising.

Deliberately NOT exposed: `/hpc/submit` (still 501),
`create_approval` (internal — called by nanobrain's ApprovalStep
during execution).

Full operator-facing install + reference: `docs/mcp_integration.md`
(Claude Desktop config snippet, env vars, per-tool input/output
shapes, troubleshooting).

## Key reference docs

- `../architectural_plan.md` — project-level source of truth.
- `../implementation_plan.md` — task table + scoreboard.
- `docs/composer_task_spec.md` — T-COMP phased delivery + ACs.
- `docs/workflow_spec.md` — the VIOLIN × BV-BRC workflow definition.
- `docs/session_friction_log.md` — what burned time before.
- `docs/nanobrain_mock_audit.md` — T14 audit + fix rows.
