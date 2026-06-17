# Desktop streaming contract (E3-5)

How a desktop MCP client consumes the per-stage reasoning stream that
`run_workflow_streaming` emits, and an honest assessment of whether
`send_log_message` is the right MCP mechanism for stage CONTENT.

This document is the client-side counterpart to the server tool in
`src/apecx_integration/mcp_surface/tools/eo_primitives.py::run_workflow_streaming`.
The reference client is `scripts/mcp_stream_client.py`; the proving integration
test is `tests/integration/test_mcp_stream_client.py`.

## What is PROVEN vs notional

- **PROVEN (E3-5):** a real MCP `ClientSession` over a real stdio transport — a
  subprocess `apecx-mcp` server, full `initialize` handshake — consumes the stream.
  Per completed reasoning stage the client receives a progress notification AND a
  structured log notification, IN ARRIVAL ORDER, and the concatenation of the
  streamed stage reports is byte-for-byte present as the `## Analysis steps` in
  the final headless document (streamed == headless, no divergence over the wire).
  This closes the E2-S gap, where the tool had only ever been driven by a fake
  `Context` object — no real client had consumed the wire.
- **Still notional:** an actual GUI desktop application that renders these
  notifications into live panes. The wire contract is proven; the pixels are not.
  Nothing below assumes a specific GUI framework — it describes the data a GUI
  would bind to.

## The client contract

A desktop client that wants live per-stage progress does four things:

1. **Launch the server over stdio and `initialize`.** Standard MCP handshake via
   `mcp.client.stdio.stdio_client` + `ClientSession`.
2. **Register a `logging_callback`** on the `ClientSession`
   (`ClientSession(read, write, logging_callback=...)`). Signature (mcp 1.27):
   `async def logging_callback(params: LoggingMessageNotificationParams)`. The
   stage payload is on `params.data` (see schema below).
3. **Register a `progress_callback`** by passing it to `call_tool`
   (`session.call_tool(name, args, progress_callback=...)`). Signature:
   `async def progress_callback(progress: float, total: float | None, message: str | None)`.
   Passing this callback makes the mcp client mint a `progressToken` automatically
   (it uses the JSON-RPC request id) — you do NOT construct the token by hand. That
   token is what the server's `report_progress` needs; without a `progress_callback`
   the server's progress notifications no-op cleanly.
4. **Call the streaming tool**:
   `call_tool("run_workflow_streaming", {"name": "viral_epitope_analysis", "params": {"query": ...}})`.
   The server's injected `Context` parameter is hidden from the tool's input schema —
   the client supplies only `name` + `params`.

### `set_logging_level` is now SAFE to call (E3-S-followup — fragility CLOSED)

Originally found in E3-5: the apecx FastMCP server did **not** register the MCP
`logging` capability, so a `logging/setLevel` request was rejected with
`McpError: Method not found`, which **tore down the entire client session** before
the streaming tool ever ran. The streaming "worked" only because the reference
client deliberately skipped the call — fragile against any spec-conformant client
(Claude Desktop) that negotiates a log level during init.

**Fixed (E3-S-followup):** `server.py::_register_logging_capability` registers a
no-op `SetLevelRequest` handler on the underlying low-level server (the same
`_mcp_server` seam FastMCP uses for its own `list_tools` / `call_tool` handlers).
This is the real, supported FastMCP/`mcp` mechanism — `get_capabilities()`
advertises `logging` iff a `SetLevelRequest` handler exists (mcp 1.27,
`server/lowlevel/server.py:220`). Two effects:

- the `initialize` result now advertises the `logging` capability, so a client's
  negotiation sees the method as supported;
- `logging/setLevel` resolves to the handler and returns `EmptyResult` (ok)
  instead of "Method not found".

The level is **accepted-but-ignored**: `send_log_message` (mcp/server/session.py)
does not gate on a configured level — it always emits the notification — so the
per-stage stream reaches `logging_callback` whether or not a level was set, and
the streaming contract is unchanged. The reference client now CALLS
`session.set_logging_level("info")` during init, and the E3-5 integration test
drives that standards-compliant path end-to-end — the regression guard for this
fix. Proof: `tests/unit/test_server_logging_capability.py` (protocol-level,
fast) + `tests/integration/test_mcp_stream_client.py` (real wire, end-to-end).

### The two notifications per stage

| Channel | Server call | Client callback | Carries |
|---|---|---|---|
| progress | `ctx.report_progress(progress=n, message="stage complete: <stage>")` | `progress_callback(progress, total, message)` | a monotonically increasing counter + the stage name in `message` |
| log (info) | `ctx.session.send_log_message(level="info", data={...}, logger="apecx.eo.streaming")` | `logging_callback(params)` → `params.data` | the FULL stage report (markdown + structured data) |

Both fire once per completed stage, in stage-completion order. The progress channel
is the cheap "advance the bar / name the stage" signal; the log channel carries the
renderable CONTENT.

### The log `data` schema

`params.data` for a stage notification is a JSON object:

```jsonc
{
  "event": "stage_report",   // discriminator — filter on this
  "stage": "structural_evidence",
  "order": 2,                 // render position (ascending); NOT arrival order
  "markdown": "…short human-readable fragment for this stage…",
  "data": { … },              // machine-readable detail (counts, notes)
  "step_name": "structural",  // the emitting nanobrain step
  "run_id": "run-…"           // ties every stage to the final WorkflowResult.run_id
}
```

`order` controls render position and is stable; arrival order is the DAG's
step-completion order and may differ (e.g. `structural_evidence` completes before
`sequence_conservation`). A renderer that wants the same layout as the final
document's reasoning trace should sort by `order` — this is exactly what
`composition/steps/_stage_report.py::render_stage_reports` does, and why the
streamed reports render identically to the headless trace.

## How a desktop app renders it

1. On `call_tool`, open a "reasoning" pane and show an indeterminate spinner.
2. On each **progress** notification: advance a step counter / breadcrumb and label
   it with the stage name from `message`.
3. On each **log** notification with `data.event == "stage_report"`: append a card to
   the pane keyed by `data.stage`, rendering `data.markdown`; keep `data.data` for a
   "details" disclosure. Sort cards by `data.order` if you want final-document layout.
4. On tool return: replace the live pane with the final `WorkflowResult.markdown`
   (the 5-section document). The streamed cards and the final `## Analysis steps`
   are the same content — no second fetch needed; the live view simply becomes
   authoritative.
5. `run_id` lets the app cross-link a streamed card to `inspect_run(run_id)` for the
   per-step "what ran" view.

### Reliability (both halves)

The server guarantees a notification failure never changes the run — the returned
`WorkflowResult` is identical whether or not anyone is listening (the notification
emit is wrapped + swallowed). E3-5 verifies the **client** half too: the reference
client wraps each callback body so a handler bug is recorded
(`StreamRun.handler_errors`) and swallowed rather than tearing down the session.
A desktop renderer should do the same — never let a render exception propagate into
the MCP read loop.

## Is `send_log_message` the right mechanism? (brutally honest)

`send_log_message` is the **pragmatic** choice and it works today, but it is a
**semantic mismatch** for stage CONTENT. The honest tradeoff:

**Why it works / why we shipped it**
- It is the only server→client structured-push primitive FastMCP exposes today
  without defining a custom protocol extension. `report_progress` carries a number
  and a string — not enough for a markdown card. `send_log_message`'s `data` is
  free-form JSON, which fits the stage report exactly.
- It rides the existing request's notification channel, correlated to the call — no
  extra connection, no resource lifecycle to manage.

**Why it is the wrong semantic home**
- `logging/*` is meant for diagnostics, gated by `logging/setLevel`. We are
  (ab)using an `info` log as the primary content transport. A spec-conformant client
  that filters logs by level, or that routes `logging` to a debug console rather than
  a content pane, would silently drop the stage stream. The server now ignores level
  by an EXPLICIT no-op handler (E3-S-followup) rather than by not implementing the
  method — so the session-teardown fragility is closed, but the *semantic* mismatch
  (content riding a diagnostic channel) remains. The capability fix makes the channel
  robust; it does not make it the right home.
- There is no schema contract: `data` is `Any`. The `event == "stage_report"`
  discriminator is a private convention, not anything a generic MCP client knows to
  look for.

**The cleaner alternatives, ranked**
1. **A streamed/updating MCP resource** (`resources/updated` notifications on a
   `run://<run_id>/trace` resource). This is the spec-blessed home for evolving
   server-side content a client subscribes to and re-reads. It gives a typed
   resource URI, natural correlation via `run_id`, and a client that already knows
   "this is content, render it" — not "this is a log line." **Recommended target.**
2. **A custom typed notification** (a server-defined `notifications/apecx/stage`
   method with a published params schema). Cleaner typing than logging, but it is a
   bespoke extension every client must special-case — same coupling cost as the
   current `event` discriminator, just more honest about being an extension.
3. **MCP sampling** — **not applicable.** Sampling is client→server→LLM for the
   server to request a completion; it is the wrong direction for pushing finished
   stage reports to the client. Listed only to record that it was considered and
   rejected.

**Recommendation.** Keep `send_log_message` for now — it is proven end-to-end, the
session-teardown fragility is CLOSED (the server advertises + handles the `logging`
capability, so `set_logging_level` is safe), and the migration is not urgent — but
treat it as **interim**. When a real desktop GUI is built, move stage CONTENT to a
**streamed resource** (`run://<run_id>/trace`) and demote `send_log_message` to
what it should be: optional diagnostic breadcrumbs. The progress channel
(`report_progress`) is correct as-is and stays. Until then, document loudly (this
file + the tool docstring) that `data.event == "stage_report"` on
`logger == "apecx.eo.streaming"` is a private content channel — clients MAY now
call `set_logging_level` safely, but a spec-conformant client that routes
`logging` to a debug console (rather than a content pane) would still misplace the
stage stream; only the streamed-resource migration fixes that semantic mismatch.

## Risk closed (E3-S-followup)

- **Session teardown on `set_logging_level`.** CLOSED. The server now advertises +
  handles the MCP `logging` capability; a standards-compliant client that calls
  `set_logging_level` during init no longer gets `Method not found` + teardown.
  Proven over the real wire (`test_mcp_stream_client.py`, which now calls
  `set_logging_level("info")` before driving the stream) and at the protocol level
  (`test_server_logging_capability.py`).

## Risk not closed

- **No real GUI.** The wire is proven; an actual rendering app is unbuilt. A GUI
  could still mis-handle the `logging`-as-content convention (e.g. route it to a
  debug console). The streamed-resource migration above is the durable fix — the
  capability fix removes the teardown fragility but not the semantic mismatch.
- **Single workflow exercised.** E3-5 proves the split on
  `viral_epitope_analysis`. Any future streaming workflow inherits the same
  wiring (the tool is generic), but only this one is verified over the wire.
