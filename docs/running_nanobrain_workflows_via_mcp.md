# Running pre-made nanobrain workflows via the APECx MCP server

This guide covers the **general mechanism** for exposing pre-built
nanobrain workflows as MCP tools that Claude Desktop (or any MCP
client) can invoke. It is the operator-facing complement to
`src/apecx_integration/mcp_surface/workflow_registry.py`.

## 1. What this is

The APECx MCP server (`apecx-mcp`) ships a generalized **workflow
catalog** that maps pre-made nanobrain workflows onto MCP tools. Each
entry in the catalog becomes one tool visible in `tools/list` with a
catalog-declared name, description, and input schema. When the tool
is called, the registrar drives the underlying `nanobrain.Workflow`
via its canonical `Workflow.run(...)` entry point and returns the
workflow's output data units.

**This is not the same as the composer-catalog tools**
(`list_workflows`, `describe_workflow` in `tools/discovery.py`). Those
read the composer's component manifests — descriptions of components
the composer can stitch together via `start_workflow`. The workflow
registry here is a registry of **already-built, runnable workflows**.
Different concept, different surface, different catalog.

The two coexist intentionally:

| Surface | Purpose | Catalog | Tools |
|---|---|---|---|
| Composer catalog | "What can I build?" | `composer_config.component_catalog_paths` | `list_workflows`, `describe_workflow`, `start_workflow` |
| Workflow registry (this doc) | "What can I run as-is?" | `mcp_surface/configs/mcp_workflow_catalog.yml` | One MCP tool per catalog entry |

## 2. Catalog format

The catalog file is a single YAML document with a top-level
`workflows:` list. Each entry is a `WorkflowCatalogEntry` validated by
Pydantic with `extra='forbid'` — unknown fields raise at load.

### Full field reference

| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `tool_name` | str | yes | — | MCP tool name shown to Claude. Identifies the tool in `tools/list` and `tools/call`. |
| `description` | str | yes | — | Tool description shown in `tools/list`. Multi-line is fine; the LLM reads it before deciding to call the tool. |
| `source` | object | yes | — | Either `{kind: yaml, path: ...}` or `{kind: lightweight, module: ..., function: ...}`. |
| `input_schema` | object | yes | — | JSON Schema for the MCP tool's parameters. See § 2.1. |
| `requires` | object | no | `{env: [], modules: []}` | Prerequisites checked at registration AND at every call (env can change). |
| `timeout_seconds` | float | no | 600.0 | Maximum wall time for `Workflow.run(timeout=...)` to drain the cascade. |
| `input_envelope_key` | str | no | null | When set, the registrar wraps the MCP-tool kwargs as `{input_envelope_key: kwargs}` before calling `Workflow.run`. Use when the workflow's first step has a single input data unit that takes a dict payload and you want to expose the dict's fields flat in the MCP schema. See § 2.2. |
| `output_envelope_key` | str | no | null | OUTPUT mirror of `input_envelope_key`. When set, the registrar returns `result[output_envelope_key]` directly (flat), instead of the data-unit-keyed dict. Use for a workflow with one output data unit whose value is itself a dict. See § 2.3. |
| `settle_ms` | int | no | 200 | How long `Workflow.run` waits after the last cascade activity before declaring the cascade drained. Bump higher (500–2000) for workflows with multi-second gaps between trigger fires (remote tool calls, file I/O). See § 2.4. |

### 2.1 `input_schema` — JSON Schema, surfaced to the MCP client

The `input_schema` describes the tool's MCP-facing parameters using
JSON Schema. The registrar synthesizes a Python function whose
signature mirrors `properties` + `required`, and FastMCP regenerates
the schema from that signature for `tools/list`. The synthesized
function's parameters preserve:

- name (the JSON-Schema property name)
- type (`string` → `str`, `integer` → `int`, `number` → `float`,
  `boolean` → `bool`, `object` → `dict`, `array` → `list`, with
  `... | None` for optional)
- default (the JSON-Schema `default`, else `None` for optional fields)

A property that isn't in `required` becomes optional with default
`None`.

### 2.2 `input_envelope_key` — bridging MCP args to a workflow input

`Workflow.run(input_data, ...)` deposits `input_data` keyed by the
**first step's input data unit name** — NOT by workflow-level
input-data-unit names. This is a subtle nanobrain contract that the
catalog has to bridge: a workflow YAML declaring
`input_data_units.workflow_input` and a link `workflow_input ->
first_step.first_step_input` documents the entry port, but `run`
writes directly into `first_step_input`. Mis-keying silently
populates 0 data units and the workflow returns nothing useful — a
silent-failure shape the envelope-key field exists to bridge.

If your workflow's first step has one input data unit named `query`
accepting a string, your catalog `input_schema` should declare
`query: {type: string}` and omit `input_envelope_key` — kwargs are
passed through as-is and `{query: "..."}` lands in the right unit.

If your workflow's first step has one input data unit named (say)
`fasta_collection_input` accepting a `{fasta_path | fasta_text}`
dict payload, and you want Claude to see flat `fasta_path` /
`fasta_text` fields rather than nest them under
`fasta_collection_input`, set
`input_envelope_key: fasta_collection_input`. The registrar will
wrap kwargs as
`{fasta_collection_input: {fasta_path: ..., fasta_text: ...}}` before
calling `Workflow.run`. This is the shape the bundled
`rhea_muscle_alignment` entry uses.

**Verify the right key** by inspecting the workflow:
```python
wf = Workflow.from_config("composition/workflows/<name>/workflow.yml")
# Find the first step + its input data unit name
print(list(wf.child_steps.keys())[0])              # first step name
print(list(wf.child_steps["<first>"].step_input_data_units.keys()))  # input DU names
```

### 2.3 `output_envelope_key` — flat MCP result from a single output DU

`Workflow.run` returns `{"status": "completed", <output_du_name>:
<value>, ...}`. The registrar strips `status`; what remains is keyed
by workflow-level output-data-unit names — awkward for an MCP client
that expects a flat result dict.

For a workflow with a single output data unit (the common case),
declare `output_envelope_key: <that_du_name>`. The registrar returns
`result[output_envelope_key]` directly, giving the MCP client a flat
shape (e.g. `{n_sequences: 2, alignment_length: 22, summary: "..."}`)
instead of `{workflow_output: {n_sequences: 2, ...}}`. If the
declared key isn't actually a workflow output, the registrar
FAIL-LOUDs in the response envelope rather than silently returning
the keyed shape — a config error must surface, not hide. The bundled
`rhea_muscle_alignment` entry sets
`output_envelope_key: workflow_output`.

For a multi-output-DU workflow, leave `output_envelope_key` unset —
the keyed shape is the right surface (Claude reads each DU's
contents under its name).

### 2.4 `settle_ms` — cascade-drain settle window

`Workflow.run` waits for the trigger cascade to drain before
returning. "Drained" means no trigger has fired for `settle_ms`
milliseconds. For a fast workflow (pure transforms, sub-ms steps),
the default 200ms is plenty. For a workflow with **multi-second gaps
between steps** (a remote MCP tool call, file I/O, a slow LLM), the
default is too short — `run` declares drained while later steps are
still pending and returns a partial result (the silent-failure shape
`output_envelope_key`'s FAIL-LOUD also guards against).

Rule of thumb: `settle_ms` should exceed the longest expected
quiet period between consecutive trigger fires in the workflow.
The bundled `rhea_muscle_alignment` entry sets `settle_ms: 2000`
because MUSCLE's MCP round-trip + ProxyStore fetch routinely creates
a 5+ second gap between `fasta_collection` finishing and
`muscle_alignment` finishing.

Symptom of an under-set `settle_ms`: the workflow logs show every
step running and the final output data unit getting set, but the
MCP tool response only contains the partial state at the moment
`run` declared drained. If you see this, raise `settle_ms`.

## 3. How to add a new workflow to the catalog

### 3.1 YAML-kind (the common case)

You already have a workflow YAML at
`composition/workflows/<name>/workflow.yml`. Append an entry:

```yaml
workflows:
  - tool_name: my_new_workflow
    description: |
      One-line description, plus enough context for the LLM to know
      when to call this tool versus another.
    source:
      kind: yaml
      path: composition/workflows/my_new_workflow/workflow.yml
    input_schema:
      type: object
      additionalProperties: false
      properties:
        query:
          type: string
          description: "The query to run."
      required: [query]
    requires:
      env: []
      modules: []
    timeout_seconds: 120.0
```

Relative `path` resolves against the `apecx_integration` package
root (the directory containing `mcp_surface/`).

### 3.2 Lightweight-kind (programmatic factory)

When the workflow is built programmatically via the lightweight
`WorkflowBuilder`, ship a factory function and reference it:

```python
# my_pkg/workflows/alpha.py
from nanobrain.lightweight.workflow_builder import WorkflowBuilder

def build_alpha():
    """Return a constructed Workflow."""
    b = WorkflowBuilder("alpha", "Alpha workflow")
    b.add_input("query", "DataUnitString")
    b.add_step("agent", "EnhancedCollaborativeAgent")
    b.add_output("answer", "DataUnitString")
    b.connect("query", "agent")
    b.connect("agent", "answer")
    return b.load()
```

Catalog entry:

```yaml
  - tool_name: alpha
    description: "Run the Alpha workflow programmatically."
    source:
      kind: lightweight
      module: my_pkg.workflows.alpha
      function: build_alpha
    input_schema:
      type: object
      properties:
        query: {type: string}
      required: [query]
```

The factory must be a no-argument callable that returns a
`nanobrain.core.workflow.Workflow`. Any other return type raises a
clear error at first call.

### 3.3 Loading + verifying

After editing the catalog:

```bash
PYTHONPATH=src .venv/bin/python -c "
from apecx_integration.mcp_surface.workflow_registry import load_catalog
print(load_catalog().workflows)
"
```

A parse / validation failure here is the FAIL-LOUD path. Fix any
errors before launching the MCP server.

## 4. Prerequisites

Each entry's `requires` block lists what must be present for the tool
to be usable:

- `env`: environment variable names that must be set AND non-empty.
- `modules`: Python module names that must be importable via
  `importlib.util.find_spec` (the modules are NOT actually imported —
  that's deliberate, so heavy modules don't run their side effects at
  startup).

Prerequisites are checked twice:

- **At server startup** — when prereqs are unmet, the tool is STILL
  registered, but its description is suffixed with `[UNAVAILABLE:
  <reason>]` so the operator immediately sees the misconfiguration in
  Claude Desktop's tool list. Calls return an actionable error.
- **At every call** — env can change between startup and the moment
  the LLM calls the tool. The runner re-checks; an unmet prereq at
  call time returns `{"error": "...prerequisites are not met: ..."}`.

### Why we don't silently hide misconfigured tools

The dominant silent-failure shape in MCP integrations is "tool
returned nothing useful, no error, operator doesn't know why." The
registry refuses both shapes:

- **Silent absence** (drop the tool from `tools/list` because prereqs
  aren't met) makes the operator wonder why Claude can't do the
  thing.
- **Silent fake success** (return an empty result) makes the operator
  wonder why the result is empty.

The `[UNAVAILABLE]` marker + actionable error envelope is the
operator-friendly middle ground: visible in `tools/list`, clearly
labeled, and explicit when called.

## 5. End-to-end execution

```
Claude Desktop
   │
   │  tools/call rhea_muscle_alignment {fasta_text: "..."}
   ▼
apecx-mcp (FastMCP, stdio)
   │
   │  dispatches to the synthesized fn(fasta_path: str|None, fasta_text: str|None)
   ▼
workflow_registry._runner(entry, **kwargs)
   │
   │  1. re-check prereqs at call time (env may have changed)
   │  2. _load_workflow_for_entry → Workflow.from_config(path)   [cached per-entry]
   │  3. input_data = {entry.input_envelope_key: kwargs}         [if set]
   │  4. await workflow.run(input_data, timeout=entry.timeout_seconds,
   │                         settle_ms=entry.settle_ms, await_cascade=True)
   │  5. outputs = result \ {"status"}
   │  6. if entry.output_envelope_key: outputs = outputs[that_key]  [unwrap]
   ▼
nanobrain.Workflow.run
   │
   │  process(input_data) → trigger cascade fires → links transfer →
   │  output data units populate → cascade drains to quiet
   ▼
{"status": "completed", "workflow_output": {summary, n_sequences, ...}}
   │
   │  _runner strips "status", surfaces non-success status as {"error": ...}
   ▼
MCP response (FastMCP wraps as ContentBlocks)
```

A failure at any step — prereq unmet, YAML file missing, factory
import error, `Workflow.run` raises, cascade times out, non-success
status — surfaces as `{"error": "<actionable message>"}`. The MCP
transport itself stays clean; the body carries the failure.

## 6. Claude Desktop config snippet

```jsonc
{
  "mcpServers": {
    "apecx": {
      "command": "apecx-mcp",
      "env": {
        // Required for the database tools (separate concern).
        "APECX_DATA_ROOT": "/Users/you/apecx-data",

        // Required for any workflow whose `requires.modules` lists a
        // sibling-repo package like rhea. Add the repo's parent dir to
        // PYTHONPATH so importlib.find_spec resolves it.
        "PYTHONPATH": "/Users/you/apecx-cowork/rhea",

        // Required for the rhea_muscle_alignment workflow specifically.
        "RHEA_MCP_URL": "http://127.0.0.1:3001/mcp/",

        // Optional — override the packaged catalog with your own.
        "APECX_MCP_WORKFLOW_CATALOG": "/Users/you/my_catalog.yml"
      }
    }
  }
}
```

After editing, fully quit and relaunch Claude Desktop.

## 7. Retirement note: `align_sequences_with_muscle` is gone

The hand-written `align_sequences_with_muscle` MCP tool
(`mcp_surface/tools/muscle_alignment.py`) was retired and replaced by
the catalog-driven `rhea_muscle_alignment` tool. The functionality is
identical — same workflow, same Rhea backend — but the path through
the code is now the general mechanism.

**Backward compatibility:** external callers that hardcoded the old
tool name MUST update to `rhea_muscle_alignment`. There is no alias —
keeping both would defeat the point of consolidating onto one
general mechanism.

## 8. Operational note — Rhea agent-cache wedge after long pauses

Rhea's `RheaToolAgent` caches per-tool agent state in two places:

- In-process (the Rhea server's Python state).
- In Redis (the `conda_envs` hash + `agent_handle:*-<tool>` keys).

After a long pause / a `docker restart` / a conda-env reshuffle, the
cached agent can produce errors like
`'Action "run_tool" was cancelled by the agent.'` even when the tool
itself works fine in a fresh process. The mitigation is operator-
level:

```bash
docker restart rhea-server
redis-cli HDEL conda_envs muscle    # or whichever tool
redis-cli --scan --pattern 'agent_handle:*-muscle' | xargs -r redis-cli DEL
```

Then retry the MCP tool call. This is a Rhea-side concern, not an
apecx-mcp concern, but it's the most common shape of "the
rhea_muscle_alignment tool used to work, now it returns
`{"error": "Action ... was cancelled"}`."

## 9. Verifying end-to-end

### 9.1 Catalog parses cleanly

```bash
PYTHONPATH=src .venv/bin/python -c "
from apecx_integration.mcp_surface.workflow_registry import load_catalog
catalog = load_catalog()
print(f'catalog ok — {len(catalog.workflows)} entries')
for entry in catalog.workflows:
  print(f'  - {entry.tool_name} ({entry.source.kind})')
"
```

### 9.2 Tool surface includes the catalog entries

```bash
PYTHONPATH=src .venv/bin/python -c "
import asyncio
from apecx_integration.mcp_surface.server import build_server
server = build_server()
tools = asyncio.run(server.list_tools())
print('total tools:', len(tools))
for t in sorted(tools, key=lambda t: t.name):
  print(' ', t.name, '   ', (t.description or '')[:60])
"
```

You should see `rhea_muscle_alignment` listed. When
`$RHEA_MCP_URL` is unset, its description ends with `[UNAVAILABLE:
env var $RHEA_MCP_URL is not set; ...]`.

### 9.3 Live end-to-end against Rhea

```bash
# Start Rhea (one-time setup; see rhea repo docs).
# Then:
export RHEA_MCP_URL=http://127.0.0.1:3001/mcp/
PYTHONPATH=src:/Users/you/apecx-cowork/rhea \
  .venv/bin/python -m pytest \
    tests/integration/test_mcp_workflow_surface.py::test_rhea_tool_call_against_live_rhea \
    -v
```

The gated test skips cleanly when `$RHEA_MCP_URL` is unset.

### 9.4 Unit tests + unconditional integration tests

```bash
scripts/run_tests.sh tests/unit/test_mcp_workflow_registry.py
scripts/run_tests.sh tests/integration/test_mcp_workflow_surface.py
```

Both should pass without any service running.

## 10. Reference

- Implementation: `src/apecx_integration/mcp_surface/workflow_registry.py`
- Default catalog: `src/apecx_integration/mcp_surface/configs/mcp_workflow_catalog.yml`
- Server wiring: `src/apecx_integration/mcp_surface/server.py::build_server`
- Unit tests: `tests/unit/test_mcp_workflow_registry.py`
- Integration tests: `tests/integration/test_mcp_workflow_surface.py`
- The underlying workflow: `src/apecx_integration/composition/workflows/rhea_muscle_alignment/`
- Workflow runtime contract: `nanobrain.core.workflow.Workflow.run` (CONTRACTS.md#g8)
- FastMCP API: `mcp.server.fastmcp.FastMCP.tool` (the
  registration entry point — accepts `name=`, `description=`; input
  schema is derived from the function signature).
