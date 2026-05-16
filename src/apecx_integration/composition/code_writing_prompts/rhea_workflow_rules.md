# Rhea-backed workflow rules

Imperatives for composing nanobrain workflows that dispatch tools to a
Rhea MCP server. Pairs with `nanobrain_rules.md` and
`post_f17_components_rules.md`.

## When to use Rhea-backed tool dispatch

Use a Rhea-backed workflow when the task is answered by INVOKING a
hosted tool (a bioinformatics lookup, a sequence analysis, a database
query) — NOT by writing a Python function. Open-Rosalind problems are
the canonical example: "What is BRCA1?" is a `uniprot.search` tool
call, not a code-generation task.

## The components (all in nanobrain proper)

| Component | Path | Role |
|---|---|---|
| `RheaAdapter` | `nanobrain.library.tools.rhea_adapter.RheaAdapter` | `ToolBackendAdapter` (BACKEND_NAME="rhea"). Dispatches a UTD tool to a Rhea MCP worker. Register once via `RheaAdapter.from_env()`. |
| `RheaMCPDiscovery` | `nanobrain.library.tools.rhea_discovery.RheaMCPDiscovery` | The codegen-as-MCP-client: `tools/list` against Rhea → UTD dicts the generator wires into a workflow. |
| `ToolExecutionStep` | `nanobrain.library.steps.tool_execution_step.ToolExecutionStep` | The workflow step. The framework step — it self-unwraps the `{<input_du>: payload}` trigger envelope, so it works directly in a cascade. (No apecx-side subclass; the former `RheaToolStep` is retired.) |

## REQUIREMENTS

1. **Use the framework `ToolExecutionStep` directly in a workflow.**
   It self-unwraps the `{<input_du>: payload}` trigger envelope via a
   UTD-aware discriminator (a single-key dict whose key is not a
   declared UTD input is the envelope). No subclass is needed.

2. **The UTD's `descriptor_id` MUST be `rhea:<tool_id>@<version>`.**
   The `rhea:` backend prefix is how `ToolExecutionStep` resolves
   `RheaAdapter` from `ToolBackendRegistry`. The `<version>` suffix is
   mandatory (grammar: `<backend>:<tool_id>@<version>`).

3. **If the MCP-side tool name differs from the sanitized `tool_id`,
   set `provenance_pin.mcp_support.rhea_tool_name`.** `RheaMCPDiscovery`
   sanitizes names like `UniProt-Search` into the UTD grammar
   (`uniprot_search`) but records the original in `mcp_support` so
   `RheaAdapter` dispatches the real name.

4. **The UTD's `provenance_pin.class_path` MUST point at
   `nanobrain.library.tools.rhea_adapter.RheaAdapter`.** This is the
   tool's framework-native dispatch pin.

5. **Register the adapter before running.** `RheaAdapter.from_env()`
   reads `$RHEA_MCP_URL` and registers the `rhea` backend in one call.
   A workflow LOADS without it (adapter resolved at `process()` time)
   but a RUN fails loud at the `ToolExecutionStep` without it.

6. **Inline the UTD into the step config (`tool_descriptor:`), not
   `tool_descriptor_path:`.** `ToolExecutionStep` resolves
   `tool_descriptor_path` relative to CWD, not the config dir — inline
   keeps the workflow portable.

7. **NEVER fabricate tool results or skip-silently when Rhea is
   unreachable.** `RheaAdapter` and `RheaMCPDiscovery` FAIL LOUD on:
   `$RHEA_MCP_URL` unset, non-200 HTTP, JSON-RPC error, `isError`,
   empty tool catalog. A Rhea-backed workflow that produced empty
   answers because the worker was down is a forbidden silent failure.

## Topology template

    workflow_input ({<utd_input>: value})
        -> rhea_tool (ToolExecutionStep, UTD descriptor_id=rhea:<tool>@<ver>)
        -> workflow_output (the tool result)

Reference: `composition/workflows/open_rosalind_rhea/workflow.yml`.

## Three legitimate construction paths

1. **Hand-authored YAML** — `open_rosalind_rhea/workflow.yml`.
2. **Lightweight `WorkflowBuilder`** —
   `open_rosalind_rhea_lightweight_builder.py`.
3. **Generated per-problem** — the `rhea_workflow` codegen
   (`tests/benchmarks/codegen/rhea_workflow.py`) discovers Rhea's tool
   catalog at generation time via `RheaMCPDiscovery`, then generates a
   workflow wiring the matching tool. This is the "code generator uses
   Rhea as an MCP server" path.

## Pin: forbidden patterns

- DO NOT author or reference an apecx-side `RheaToolStep` — it is
  retired. The framework `ToolExecutionStep` self-unwraps the trigger
  envelope; use it directly.
- DO NOT hardcode a Rhea endpoint in a workflow YAML — the endpoint is
  per-deployment plumbing; it flows via `$RHEA_MCP_URL` →
  `RheaAdapter.from_env()`.
- DO NOT author a UTD whose `descriptor_id` lacks the `@<version>`
  suffix — the framework rejects it.
- DO NOT assume the Rhea worker hosts a given tool — `RheaMCPDiscovery`
  reports the actual catalog; match against it and FAIL LOUD on a
  miss rather than dispatching a wrong tool.
