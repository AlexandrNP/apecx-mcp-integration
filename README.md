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
