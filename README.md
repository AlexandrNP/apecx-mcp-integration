# apecx-mcp-integration

MCP surface, control plane, composition, and execution integration for the APECx project.

- **New scientist? Start here:** [`docs/tutorial/`](docs/tutorial/README.md) — 5-chapter walkthrough from clean laptop to reproducible run (T15 Phase-2 draft, 2026-04-23).
- **Connecting Claude Desktop / MCP clients:** [`docs/mcp_integration.md`](docs/mcp_integration.md) — install path, `claude_desktop_config.json` snippet, per-tool input/output reference, troubleshooting.
- **Architectural source of truth:** `../architectural_plan.md` (§R3 for current Round 3 revisions)
- **Implementation plan:** `../implementation_plan.md`
- **Scoping answers (Round 3):** `docs/scoping_answers.md`
- **Existing-asset inventory (T00.5):** `docs/existing_assets_inventory.md`

## Status

**Phase 2 in progress.** Phase 0 blockers all ✅ cleared. Critical-path
tasks landed on main as of 2026-04-22:

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
| T-COMP  | ✅ done   | LLM composer, phases 1-5 (AC8 operator-pending)      |
| T01     | ⚠️ P1    | `/workflows/start` wired; P2 local executor open     |
| T04/T05 | optional  | HPC export lane — gated on user opt-in               |
| T13b    | post-12w  | Docker sandbox (runtime isolation)                   |

**Control Plane API surface now live** (all routes real, not 501):

- `POST /workflows/start`   — T01 P1 (composes + persists + gates)
- `POST /workflows/plan`    — preview-mode composition (CANCELLED run)
- `POST /workflows/execute` — T01 P2 (LocalExecutor → terminal state)
- `POST /workflows/diff`    — T06 categorization + novel Python
- `POST /hpc/estimate`      — T07 pre-submission cost estimate
- `POST /hpc/confirm`       — T07 user acknowledgement gate
- `POST /hpc/export`        — T05 PBS bundle generator
- `POST /hpc/ingest`        — T05 AC3 bundle reconciliation
- `POST /approvals/*`       — TX1 HITL approval lifecycle
- `GET  /metrics/*`         — TX3 review-UX telemetry
- `GET  /runs/*`, verified-synonyms, status — all TX1-backed

Still stubbed 501: `/hpc/submit` (genuinely blocked on T04 Globus
or T05 qsub runtime — requires operator HPC access).

**MCP tool surface** (`apecx-mcp` entry point exposes 11 tools over
stdio for Claude Desktop):

- `start_workflow`, `show_diff`, `execute_workflow`
- `list_pending_approvals`, `approve`, `reject`, `correct`
- `estimate_cost`, `confirm_allocation`,
  `export_hpc_bundle`, `ingest_hpc_bundle`

Set `APECX_CONTROL_PLANE_URL` to point at the Control Plane
(default `http://localhost:8000`).

Operator-pending: T06 AC3 scientist-review session, T12 AC1 final 7
live-LLM fixtures. T01 AC1 strict bar met 3/3 on mistral-nemo
post-prompt-uplift. T-COMP Phase 5 AC8 wall-time: measured
2026-04-22 — see `tests/integration/test_composer_ac8_walltime.py`
and `docs/composer_task_spec.md` for real numbers vs the spec's 60s
target.

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
review-gate subagent (`.claude/agents/review-gate.agent.md`) sits on
top of these and is invoked via Claude's `Agent` tool when a PR is
"code-complete" per AC4.

CI wiring (running the same checks on every GitHub PR) is gated on
TX2 AC2 — the repo isn't on GitHub yet. Scripts work standalone
today; when CI ships, the same commands go into the workflow YAML.

## Security — no runtime sandbox in Phase 1

**LLM-generated Python is scanned at generation time and runs unisolated
in the Tier-2 process.** The T13 Phase-1 sandbox
(`src/apecx_integration/composition/sandbox.py`) is a static
import-whitelist + banned-construct scanner only. Once an artifact
passes the scan, it executes with the same filesystem, network, and
process privileges as the control-plane host. Human review (Step 4
HITL gate + operator review) is the only runtime safety control.

Whitelist: `configs/sandbox/import_whitelist.txt`. Proposing a new
whitelist entry requires a PR + justification; narrow whitelist is the
whole point of Phase 1. Dynamic-import constructs
(`importlib.import_module`, `__import__`, `exec`, `eval`, `compile`)
are rejected outright so the whitelist can't be bypassed.

Runtime isolation lands in T13b (Phase-2 Docker sandbox) — see
`../implementation_plan.md`.

## Layout

```
src/apecx_integration/
  mcp_surface/           # Tier 1 — MCP server, tool stubs
  control_plane/         # Tier 2 — FastAPI, SQLAlchemy, provenance, gates, notifications, accounting, schemas
  composition/           # Tier 3 — RAG, composer, differ, sandbox, artifact store
  execution/             # Tier 4 — local executor (primary), PBS bundle (export), Globus Compute (optional)
  config/
docs/                    # Phase 0 artifacts
spikes/                  # T00.x prototypes
tests/
  unit/                  # pytest -m "not (integration or smoke)"
  smoke/                 # pytest -m smoke — mocks OK
  integration/           # pytest -m integration — real data only
scripts/                 # helper scripts (backup, restore, review-gate)
prompts/                 # versioned LLM prompts (composer.system.md, etc.)
```

## Running things

```bash
pip install -e ".[dev]"
pytest -m smoke                 # wiring-shape tests
pytest -m "not integration"     # unit + smoke
pytest -m integration           # real-data (requires data/ snapshots + env)
pre-commit run --all-files
```

## Execution model (Round 3)

- **Local is default.** The vertical slice runs on a laptop.
- **HPC is optional export.** ALCF Polaris / Aurora bundles are a feature scientists opt into via MCP tool `export_hpc_bundle(run_id)`.
- **BV-BRC via local snapshot only** — no live queries. Data lives under `../data/bvbrc_cache/`.

## Review-UX telemetry (TX3)

`GET /metrics/approvals?since=<iso-timestamp>` returns aggregate
approval-decision timings sourced from the hash-chained
`ProvenanceEvent` log (no extra Approval columns). Fields:

- `count` — approvals with both REQUESTED and DECIDED events in window.
- `median_time_to_decide_seconds` / `p95_time_to_decide_seconds`
  (p95 null below 20 samples).
- `percent_auto_approved` / `percent_rejected`.
- `rubber_stamping_suspected` — the canary: **true iff `count > 5` and
  `median_time_to_decide_seconds < 5`**, matching the AP §7 risk #4
  threshold.

**Check this at every retro.** The HITL review is the project's most
subtle failure mode; if the median drops below 5s over a week of >5
decisions, someone is rubber-stamping and the "human-approved
synonyms" audit trail becomes hollow. Flagging is the responsibility
of whoever runs the retro — the endpoint does not alert on its own.

## Contribution and review

All code in `src/` is agent-authored (Round 3 staffing: Q4). Every PR passes through `review-gate` before the single human engineer merges. The review harness (TX5) enforces:

1. No `unittest.mock` / `MagicMock` in non-test code (workspace CLAUDE.md).
2. All imports resolve.
3. Steps follow the `nanobrain-step-authoring` rules: implement `process()`, never override `execute()`.
4. The agent's PR summary matches the actual diff.

## Git and worktree discipline

Per workspace `CLAUDE.md`: one non-trivial task per branch; one branch per worktree when the task is non-trivial. Use `git-worktree-guardian` for all git operations.

## Related repos (read-mostly)

- `../nanobrain/` — framework and existing viral-protein-analysis pipeline
- `../apecx-mcp/` — reference MCP server
- `../apecx-harvesters/` — publication/metadata loaders (not BV-BRC/VIOLIN)
- `../apecx-rag/` — RAG primitives
- `../data/` — BV-BRC + VIOLIN local snapshots
