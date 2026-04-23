# apecx-mcp-integration

MCP surface, control plane, composition, and execution integration for the APECx project.

- **Architectural source of truth:** `../architectural_plan.md` (§R3 for current Round 3 revisions)
- **Implementation plan:** `../implementation_plan.md`
- **Scoping answers (Round 3):** `docs/scoping_answers.md`
- **Existing-asset inventory (T00.5):** `docs/existing_assets_inventory.md`

## Status

**Pre-Phase-1.** Repo is scaffolded; Phase 0 blockers remain:
- T00.1b — concrete VIOLIN × BV-BRC workflow spec
- T00.2 — nanobrain async pause/resume spike
- T00.4 — mocks-policy decision
- T00.5 — existing-asset inventory (initial pass done; source-read pass pending)

Do not start Phase 1 until these gates pass.

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
