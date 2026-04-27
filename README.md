# apecx-mcp-integration

MCP server + Control Plane + composition + execution integration for the
APECx scientific platform. Exposes 11 scientist-facing tools to Claude
Desktop (or any MCP client) over stdio: compose a workflow from a
natural-language description, review the diff, execute locally,
optionally export to HPC.

> **License: All Rights Reserved (proprietary, source-available).**
> See [`LICENSE`](LICENSE). The repo is public for transparency; reuse,
> redistribution, and derivative works require explicit written permission.

## Install in one line

```bash
uv tool install --python 3.12 git+https://github.com/AlexandrNP/apecx-mcp-integration.git@day2-rag-synthesis-agent
```

That single command pulls `apecx-mcp-integration` + `nanobrain @ academy-integration` + `apecx-harvesters @ main` and exposes the `apecx-mcp` and `apecx-cp` binaries on your `PATH`. No manual clones, no Docker required (SQLite default).

Don't have `uv`? `curl -LsSf https://astral.sh/uv/install.sh | sh`.

After install, paste the snippet from [`INSTALL.md`](INSTALL.md) into
`claude_desktop_config.json`, fully quit and relaunch Claude Desktop, and
the 11 apecx tools appear in the picker.

## Pointers

- **Install reference:** [`INSTALL.md`](INSTALL.md) — one-liner, prerequisites, troubleshooting, update / uninstall.
- **MCP integration reference:** [`docs/mcp_integration.md`](docs/mcp_integration.md) — Claude Desktop snippet, per-tool input/output, env vars, troubleshooting (incl. the two pitfalls that cause silent failure).
- **New scientist? Start here:** [`docs/tutorial/`](docs/tutorial/README.md) — 5-chapter walkthrough from clean laptop to reproducible run (T15 Phase-2 draft).
- **Architectural source of truth:** `../architectural_plan.md` (R3 revisions in §R3).
- **Implementation plan:** `../implementation_plan.md`.
- **Existing-asset inventory (T00.5):** [`docs/existing_assets_inventory.md`](docs/existing_assets_inventory.md).

## Status

Phase 2 in progress. Phase 0 blockers cleared. Critical-path tasks landed
on `main`; this session's `day2-rag-synthesis-agent` branch ships the
RAG synthesis pipeline + MCP autostart + 17-batch adversarial-probe
campaign:

| Task    | Status    | Notes                                                |
|---------|-----------|------------------------------------------------------|
| T02     | ✅ done   | component library + manifests for VIOLIN × BV-BRC    |
| T03     | ✅ done   | FAISS RAG index over T02 manifests                   |
| T06     | ✅ done   | differential-review UX (AC1+AC2+AC4)                 |
| T07     | ✅ done   | `/hpc/estimate` API wired                            |
| T09     | ✅ done   | run persistence + provenance (TX1)                   |
| T10     | ✅ done   | HITL ApprovalStep                                    |
| T11     | ✅ done   | artifact store + generated-artifact rows             |
| T12     | ✅ done   | reproducibility harness + 3 placeholder fixtures     |
| T13     | ✅ done   | sandbox scanner + composer-wired suggestions         |
| T14     | ✅ done   | mocks-in-nanobrain policy audit + 6 fix rows         |
| T-COMP  | ✅ done   | LLM composer, phases 1-5                             |
| T01     | ⚠️ P1     | `/workflows/start` wired; P2 local executor open    |
| T04/T05 | optional  | HPC export lane — gated on user opt-in               |
| T13b    | post-12w  | Docker sandbox (runtime isolation)                   |

**Day 2 (this session, 2026-04-27):**

- `apecx_integration.agents.rag_synthesis` — LLM synthesis with inline-citation grounding, fail-fast on hallucinated IDs / curtailed responses / empty retrieval.
- `RagSynthesisStep` — nanobrain `BaseStep` wrapper, YAML-loadable, registered in the violin_bvbrc workflow.
- `apecx-harvesters` DataCite → publication-dict adapter (probe-916 boundary invariant: DataCite knowledge stays inside `agents/rag_synthesis/`).
- MCP server **autostarts the Control Plane backend** on launch — no separate `apecx-cp serve` needed; SQLite by default.
- **Adversarial probe streak: 300/300 post-1066** — stop criterion satisfied across batches 35–51 (426 distinct probes total this session).
- **Two production silent-failure bugs surfaced + fixed**: probe 955 (`SynthesisConfig` silently accepted YAML typos → `extra='forbid'`), probe 1066 (citation regex greedy-matched across tokens → tightened character class).

**Control Plane API surface live** (all routes real, not 501):

- `POST /workflows/start` — composes + persists + gates (T01 P1)
- `POST /workflows/plan` — preview-mode composition
- `POST /workflows/execute` — LocalExecutor → terminal state
- `POST /workflows/diff` — T06 categorization + novel Python
- `POST /hpc/estimate` `/hpc/confirm` `/hpc/export` `/hpc/ingest` — T05/T07 export lane
- `POST /approvals/*` — TX1 HITL approval lifecycle
- `GET /metrics/*` — TX3 review-UX telemetry
- `GET /runs/*`, verified-synonyms, status — all TX1-backed

Still stubbed `501`: `/hpc/submit` (genuinely blocked on T04 Globus or T05 qsub runtime — requires operator HPC access).

**MCP tool surface** (`apecx-mcp` entry point, 11 tools over stdio):

- `start_workflow`, `show_diff`, `execute_workflow`
- `list_pending_approvals`, `approve`, `reject`, `correct`
- `estimate_cost`, `confirm_allocation`, `export_hpc_bundle`, `ingest_hpc_bundle`

Set `APECX_CONTROL_PLANE_URL` to point at the Control Plane (default `http://localhost:8000`); set `APECX_MCP_AUTOSTART_BACKEND=0` to disable autostart and run a backend manually.

## Security — no runtime sandbox in Phase 1

LLM-generated Python is scanned at generation time and runs **unisolated**
in the Tier-2 process. The T13 Phase-1 sandbox
(`src/apecx_integration/composition/sandbox.py`) is a static
import-whitelist + banned-construct scanner only. Once an artifact passes
the scan, it executes with the same filesystem, network, and process
privileges as the control-plane host. Human review (Step 4 HITL gate +
operator review) is the only runtime safety control.

Whitelist: `configs/sandbox/import_whitelist.txt`. Proposing a new
whitelist entry requires a PR + justification; narrow whitelist is the
whole point of Phase 1. Dynamic-import constructs
(`importlib.import_module`, `__import__`, `exec`, `eval`, `compile`) are
rejected outright so the whitelist can't be bypassed.

Runtime isolation lands in T13b (Phase-2 Docker sandbox) — see
`../implementation_plan.md`.

## Layout

```
src/apecx_integration/
  mcp_surface/           # Tier 1 — MCP server (autostarts backend), 11 tools
  control_plane/         # Tier 2 — FastAPI, SQLAlchemy, provenance, gates, notifications, accounting, schemas
  composition/           # Tier 3 — composer, differ, sandbox, artifact store, RagSynthesisStep
  agents/
    rag_synthesis/       # synthesizer + harvester adapter (Day 2)
    violin_bvbrc/        # migrated VIOLIN agents (Day 1)
  execution/             # Tier 4 — local executor (primary), PBS bundle (export), Globus Compute (optional)
  _alembic/              # bundled alembic.ini + migrations/ (so installed wheels can run apecx-cp without the repo on disk)
docs/                    # architectural artifacts + tutorials
spikes/                  # T00.x prototypes
tests/
  unit/                  # pytest -m "not (integration or smoke)"
  smoke/                 # pytest -m smoke — mocks OK
  integration/           # pytest -m integration — real data only; includes the 17-batch adversarial probe campaign
scripts/                 # run_tests.sh, build_rag_index.py, install.sh, backup_state.sh, restore_test.sh
prompts/                 # versioned LLM prompts (composer.system.md, etc.)
```

## Running things (developer / contributor)

For day-to-day use, the [one-liner install](INSTALL.md) is the
recommended path. The commands below are for **developers** working in
the repo:

```bash
# 1. Editable install with dev extras (only when contributing).
.venv/bin/pip install -e ".[dev]"

# 2. Run the test suite (unit / smoke / integration markers).
pytest -m smoke                 # wiring-shape tests
pytest -m "not integration"     # unit + smoke
pytest -m integration           # real-data (requires data/ snapshots + env)

# 3. Run the canonical test runner (sets PYTHONPATH + venv).
scripts/run_tests.sh tests/unit
scripts/run_tests.sh tests/integration/test_probe_batch_*.py -q

# 4. Pre-commit hooks (review harness checks).
pre-commit run --all-files
```

## Execution model (Round 3)

- **Local is default.** The vertical slice runs on a laptop.
- **HPC is optional export.** ALCF Polaris / Aurora bundles are a feature scientists opt into via the MCP tool `export_hpc_bundle(run_id)`.
- **BV-BRC via local snapshot only** — no live queries. Data lives under `../data/bvbrc_cache/`.
- **Backend autostart.** When `apecx-mcp` launches and the configured Control Plane URL is unreachable, it spawns `apecx-cp serve` as a child process and supervises it (terminate on MCP server exit). SQLite default; override with `APECX_CP_POSTGRES_URL` for Postgres.

## Review-UX telemetry (TX3)

`GET /metrics/approvals?since=<iso-timestamp>` returns aggregate
approval-decision timings sourced from the hash-chained
`ProvenanceEvent` log (no extra `Approval` columns). Key field:

- `rubber_stamping_suspected` — the canary: **true iff `count > 5` and
  `median_time_to_decide_seconds < 5`**, matching the AP §7 risk #4
  threshold.

**Check this at every retro.** The HITL review is the project's most
subtle failure mode; if the median drops below 5s over a week of >5
decisions, someone is rubber-stamping and the "human-approved synonyms"
audit trail becomes hollow.

## Agent-output review harness (TX5)

All agent-authored code passes through mechanical pre-commit checks
before the human engineer signs off:

| Check                         | AC  | Script                              |
|-------------------------------|-----|-------------------------------------|
| No `unittest.mock` in `src/`  | AC1 | inline in `.pre-commit-config.yaml` |
| Every import resolves         | AC2 | `scripts/checks/imports_resolve.py` |
| Step subclass compliance      | AC3 | `scripts/checks/step_authoring.py`  |

All three run as pre-commit hooks (via `pre-commit install`) and as
standalone CLI scripts for ad-hoc verification. The judgement-layer
review-gate subagent (`.claude/agents/review-gate.agent.md`) sits on top
of these and is invoked via Claude's `Agent` tool when a PR is
"code-complete" per AC4.

## Contribution and review

All code in `src/` is agent-authored (Round 3 staffing). Every PR passes
through `review-gate` before the single human engineer merges. The
review harness (TX5) enforces:

1. No `unittest.mock` / `MagicMock` in non-test code.
2. All imports resolve.
3. Steps follow the `nanobrain-step-authoring` rules: implement
   `process()`, never override `execute()`.
4. The agent's PR summary matches the actual diff.

## Git and worktree discipline

Per workspace `CLAUDE.md`: one non-trivial task per branch; one branch
per worktree when the task is non-trivial. The current `main` is the
canonical line; `day2-rag-synthesis-agent` is this session's feature
branch (RAG synthesis + autostart + 300/300 probe campaign).

## Sibling-repo dependencies

Per workspace policy, this repo's only sibling-repo runtime deps are:

- [`AlexandrNP/nanobrain`](https://github.com/AlexandrNP/nanobrain) at branch `academy-integration` — the framework (Steps, Workflows, Agents, Triggers, Links, Executors).
- [`abought/apecx-harvesters`](https://github.com/abought/apecx-harvesters) at branch `main` — DataCite-shaped publication metadata loaders.

Both are pulled directly from git via the install one-liner (see
`pyproject.toml` for the pinned branch refs). Earlier sibling repos
`apecx-db-integration` and `apecx-rag` were merged into this repo
during Day 1–Day 2 of this session per the consolidation directive.
