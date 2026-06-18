# Per-run artifacts + streaming: the contract every workflow inherits for free

Every workflow invoked through `run_workflow` (`mcp_surface/tools/eo_primitives.py`) gets two
cross-cutting capabilities with **no per-workflow wiring** — they live at the MCP boundary, not in
any one workflow. A new workflow inherits both by satisfying a small contract.

## What you get for free

1. **A durable per-run artifacts folder** at `~/.apecx/artifacts/<run_id>/` (override:
   `$APECX_ARTIFACTS_DIR`), written by `_attach_artifact` for *every* run that returns a
   `WorkflowResult`:
   - `report.md` — the markdown report (inline image refs rewritten to `figures/<name>`);
   - `figures/` — each inlined PNG (plus a vector `.pdf` sibling when one exists);
   - `data.json` — the full structured DataShape (handle-resolved);
   - `tool_outputs/<key>.json` — **one file per bundle `parts` key**, data-driven
     (`_write_tool_outputs`): no hardcoded keys, so any workflow's bundle yields openable native
     files. The content-addressed `alignment.fasta` is materialized when present.
2. **Per-stage streaming to the desktop** — `run_workflow` auto-streams when a desktop `ctx` is
   present. Two channels reach the client: incremental `emit_progress` messages, and richer
   per-stage markdown via `stage_reports`.

## The contract a workflow must satisfy

1. **End in `EnvelopeStep`** (markdown + optional `data` bundle). The envelope produces the
   standard `WorkflowResult` that `_attach_artifact` consumes. (A workflow with no EnvelopeStep
   still gets its raw output stashed behind a handle, but no narrative report.)
2. **Call `self.emit_progress("…")`** at logical milestones in each step's `process()` — these
   stream as progress to the desktop and cost nothing headless (no-op when no subscriber).
3. **(Optional, for a per-stage markdown stream)** append a stage report:
   ```python
   from apecx_integration.composition.steps._stage_report import append_stage_report
   append_stage_report(bundle, stage="my_stage", order=N, markdown="…", data={…})
   ```
   Surface it at the **top** of the step's returned dict so the G37 `step_complete` event carries
   it: `return {OUTPUT_KEY: bundle, "stage_reports": bundle["stage_reports"]}`. The streamer
   (`_make_stage_streamer`) dedups by `(stage, order)` across events, so each step may emit its own
   report; threading the list through the bundle accumulates the cumulative trace.

## Invariants (silent-failure guards)

- An intermediate (non-terminal) payload must **not** carry a `markdown` key and must **not** be a
  single-key dict — both are used as discriminators by the linear-pipeline terminal pass-through
  (`steps/_combination_common.py`) and by trigger-envelope unwrap.
- A part written to `tool_outputs/` must be JSON-serializable; non-serializable parts fall back to
  `str` (`default=str`) — never crash the run (`_attach_artifact` never raises).
- Decide a run's success from an OUTPUT VALUE, never `status` alone (G127).

## Reference implementations

- `viral_epitope_analysis` — full artifacts (figures + 13 `tool_outputs/` files) + per-stage stream.
- `epitope_combination_feasibility_assessment` — `intake → classify → release` each emit a stage
  report; terminal pass-through preserves the invariants above.
