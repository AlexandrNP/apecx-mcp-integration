# MCP Surface — Developer Documentation

This document describes the MCP-facing tier of apecx-mcp from the perspective of someone reading the code or onboarding to the project. It has three sections:

1. **What the MCP Surface is for the user** — the surface as a feature, what a scientist or LLM sees, how it is launched.
2. **What it contains** — the code layout, the FastMCP tool-registration pattern, how each tool's schema is derived, what is intentionally not exposed.
3. **How it interacts with the tool registry** — the call flow from Claude Desktop through the surface into the control plane, plus a clear distinction between the *MCP tool registry* and the *component catalog*.

A companion document, `violin_bvbrc_workflow.md`, describes the workflow that the surface drives. Diagrams `01`, `02`, `03`, and `08` are the visual companions.

---

## 1. What the MCP Surface is for the user

### One paragraph

The MCP Surface is the layer that makes apecx-mcp visible to Claude Desktop. It is a small Python program — a FastMCP server — that runs on the user's laptop, advertises a fixed set of tools to whatever MCP host launches it, and translates each tool call into an HTTP request against the apecx control plane. It owns no state, runs no workflows, and stores no data. It is roughly 200 lines of orchestration plus thin wrapper functions; everything substantive happens behind it.

### What the user sees

A scientist using Claude Desktop sees a set of named tools that Claude can invoke during conversation. As of the current build, twenty tools across five categories are advertised: `start_workflow`, `show_diff`, `execute_workflow`, `list_workflows`, `describe_workflow`, four database-query tools, four approval tools, four HPC tools, and two more discovery tools. Each tool has a name, a description, and a typed parameter list — these come from the function's docstring and Python type hints, and Claude uses them to decide when and how to call.

The user does not need to learn which tools exist; Claude inspects the tool list and picks based on the conversation. The user's interaction is in plain English (or whichever language they use). Tool calls happen as a side effect of the assistant's reasoning.

### Where it runs

The MCP Surface is a stdio MCP server. When Claude Desktop is configured to launch it, the host spawns the server as a subprocess and communicates over stdin/stdout using the MCP protocol. There is no port to open, no service to register, no firewall rule. The server lives only as long as the host that spawned it.

By default, the surface runs on the user's laptop. If the user wants the heavy backend (composition, execution, database) to run on a different machine, they set `APECX_CONTROL_PLANE_URL` to point at that host. The surface itself is light enough that running it on the laptop is essentially free — the user's MCP host is on the laptop anyway.

See `diagrams/08_deployment_topology.svg` for the three valid host topologies.

### Lifecycle and autostart

Two environment variables control the surface's behavior at startup:

- **`APECX_CONTROL_PLANE_URL`** — the HTTP URL of the control plane. Defaults to `http://localhost:8000`.
- **`APECX_MCP_AUTOSTART_BACKEND`** — when set to `1`, the MCP surface checks whether the control plane is reachable at startup and, if not, spawns `apecx-cp serve` as a child process. It polls `/healthz` until the backend answers, then proceeds with normal startup. An `atexit` hook terminates the child on MCP-server exit.

The autostart behavior was added on 2026-04-27 and is the recommended default for Claude Desktop installs because it removes a class of "the backend isn't running" failures from the first-run experience. With autostart on, a user can install the package, configure Claude Desktop, and start using the system without remembering to launch a separate backend process.

### What the user does not see (and should not need to)

Provenance recording, hash chains, run-state databases, executor sandboxes, allocation accounting — none of this is visible at the MCP layer. The user sees tools and results. Everything else is the control plane's responsibility, behind the HTTP boundary.

---

## 2. What the MCP Surface contains

### File layout

The surface is one Python package under `apecx-mcp-integration/src/apecx_integration/mcp_surface/`:

```
mcp_surface/
├── __init__.py
├── server.py                       # FastMCP entry point, tool registration, autostart
├── control_plane_client.py         # async HTTP client that talks to apecx-cp
├── data/                           # local fixtures used by data-lookup tools
│   └── database.py
└── tools/
    ├── __init__.py
    ├── _shared.py                  # get_client() singleton, parse_run_id() validator
    ├── workflows.py                # start_workflow, show_diff, execute_workflow
    ├── discovery.py                # list_workflows, describe_workflow
    ├── approvals.py                # list_pending_approvals, approve, reject, correct
    ├── hpc.py                      # estimate_cost, confirm_allocation, export/ingest_hpc_bundle
    └── database_tools.py           # query_vaccines, query_pathogens, ... (7 tools)
```

The categorization in `tools/` mirrors the five categories the user sees in the catalog (see `diagrams/03_mcp_tool_catalog.svg`). One file per category is convention, not requirement.

### The FastMCP server entry point

`server.py` is the orchestration script. The relevant fragment is short enough to reproduce:

```python
from mcp.server.fastmcp import FastMCP

from apecx_integration.mcp_surface.tools import (
    approvals as approvals_tools,
    database_tools,
    discovery as discovery_tools,
    hpc as hpc_tools,
    workflows as workflow_tools,
)

def build_server() -> FastMCP:
    """Construct the FastMCP server with every tool registered."""
    server: FastMCP = FastMCP("apecx-mcp")

    server.tool()(workflow_tools.start_workflow)
    server.tool()(workflow_tools.show_diff)
    server.tool()(workflow_tools.execute_workflow)

    server.tool()(discovery_tools.list_workflows)
    server.tool()(discovery_tools.describe_workflow)

    server.tool()(database_tools.query_vaccines)
    server.tool()(database_tools.query_pathogens)
    server.tool()(database_tools.query_genes)
    server.tool()(database_tools.query_bvbrc_genomes)
    server.tool()(database_tools.get_vaccine_pathogen_genes)
    server.tool()(database_tools.resolve_entity)
    server.tool()(database_tools.database_statistics)

    server.tool()(approvals_tools.list_pending_approvals)
    server.tool()(approvals_tools.approve)
    server.tool()(approvals_tools.reject)
    server.tool()(approvals_tools.correct)

    server.tool()(hpc_tools.estimate_cost)
    server.tool()(hpc_tools.confirm_allocation)
    server.tool()(hpc_tools.export_hpc_bundle)
    server.tool()(hpc_tools.ingest_hpc_bundle)

    return server
```

The pattern is: one Python function per tool, registered with `server.tool()(<func>)`. FastMCP introspects each function and produces an MCP tool advertisement automatically.

### How a tool is structured

A tool function has four ingredients that FastMCP turns into the MCP tool schema:

1. **The function name** — becomes the tool name the LLM sees.
2. **The function signature with type hints** — becomes the JSON schema for the parameters.
3. **The function docstring** — becomes the tool description shown to the LLM.
4. **The function return type** — used by FastMCP for output schema.

Concrete example, from `tools/workflows.py`:

```python
async def start_workflow(
    description: str,
    user_id: str,
    preferred_executor: str = "local",
) -> dict:
    """Compose a workflow from a natural-language description.

    Returns the newly-created Run (with status PAUSED or RUNNING
    depending on the approval policy) and the generated workflow
    artifact id.
    """
    if preferred_executor not in _VALID_EXECUTORS:
        raise ValueError(
            f"preferred_executor={preferred_executor!r} is not a "
            f"valid executor; expected one of {sorted(_VALID_EXECUTORS)}."
        )
    body = StartWorkflowRequest(
        description=description,
        user_id=user_id,
        preferred_executor=ExecutorKind(preferred_executor),
    )
    client = get_client()
    result = await client.start_workflow(body)
    return result.model_dump(mode="json")
```

Five things to note about this shape:

- **MCP-friendly types in, dicts out.** Parameters are plain Python types (`str`, `int`, `bool`, etc.) — easy for the LLM to produce as JSON. Return values are converted to plain dicts via `model_dump(mode="json")` so MCP can serialize them without extra encoders.
- **Pre-validation that produces friendly errors.** The `preferred_executor` string is checked against `ExecutorKind` enum values *before* it goes into `StartWorkflowRequest`. This is the audit §3.10 pattern: catch invalid values at the surface and return a useful error, rather than letting a Pydantic deep-coercion failure escape to the LLM.
- **Pydantic envelope at the boundary.** `StartWorkflowRequest` is the typed body that the control plane expects. The surface assembles it from the loose parameters, then ships it via the client. The control plane never sees the raw MCP arguments; it sees a validated envelope.
- **Singleton client.** `get_client()` returns a shared `ControlPlaneClient` instance kept alive across tool calls. There is no per-call client construction; HTTP keep-alive is preserved.
- **Async all the way.** The tool function is `async`. FastMCP awaits it. The client method is `async`. The call to the control plane is non-blocking.

The same shape applies to every other tool. `show_diff` takes a `run_id` string, parses it via `parse_run_id()` (a UUID validator that produces a friendly error on bad input), packages it into `ShowYamlDiffRequest`, calls `client.show_yaml_diff(body)`, and returns the dict.

### Schemas, descriptions, and structure — what the LLM sees

For each tool, FastMCP generates an MCP advertisement that includes:

- **Tool name** (from the function name).
- **Tool description** (from the docstring's first paragraph, occasionally the whole docstring depending on FastMCP's version).
- **Input schema** — a JSON Schema object describing each parameter's type, default, and description (parameter descriptions can be added via Pydantic `Field` annotations; the current codebase keeps signatures minimal and lets type hints do the work).
- **Output schema** (optional; FastMCP generates this from the return type if it is a Pydantic model or a typed dict).

When Claude Desktop starts the surface, it issues an MCP `tools/list` request. The surface responds with the twenty advertisements. Claude uses these to decide which tool to call when the user's intent maps to one. From that point on, every tool invocation is an MCP `tools/call` request to the surface.

### What is intentionally not exposed

`server.py`'s docstring explicitly enumerates two things the surface does not advertise, with reasons:

- **`/hpc/submit`** — this control-plane endpoint still returns 501; the live HPC submission path is not implemented (the bundle path is the implemented HPC story). A tool that always errors is strictly worse than no tool, because the LLM does not see "this is a stub" — it just gets failure responses. So the tool is not registered until the endpoint actually works.
- **`create_approval`** — this is the internal API call that nanobrain's `ApprovalStep` uses to register a pending approval during workflow execution. It is not user-facing; the user sees only the read-and-decide tools (`list_pending_approvals`, `approve`, `reject`, `correct`). Exposing `create_approval` would let an LLM forge approvals; not exposing it preserves the integrity of the approval primitive.

This discipline — surface only what is implemented, hide what is internal — is part of the broader rule that the MCP layer is a façade, not a passthrough.

### Other discipline points

- **Type-hint discipline.** Every tool's parameters use Python primitives that the LLM can reliably produce as JSON. No tool takes a Pydantic model as a direct parameter; the model is constructed inside the function. This keeps the MCP-visible schema simple.
- **Error discipline.** Every validation error is raised with an actionable message that echoes the offending value. The audit §3.10 fix to `start_workflow`'s executor validation is the canonical example — pre-validating before the Pydantic boundary so the LLM gets a useful correction signal.
- **Truthful return values.** The `execute_workflow` docstring explicitly notes that the returned `status` field is the actual database status, not a fabricated "completed". Cluster AJ (2026-04-26) was specifically about making this field truthful so the LLM can distinguish executor-driven completion from concurrent-writer outcomes. The `reason` field is the source of truth for "did THIS executor drive the transition."

---

## 3. How the MCP Surface interacts with the tool registry

### Two registries — the disambiguation that matters

The phrase *"tool registry"* in the apecx context can mean two distinct things, and an onboarding reader benefits from seeing them spelled out side by side:

| Registry | Lives where | Holds what | Used by |
|---|---|---|---|
| **MCP tool registry** | In-memory in the FastMCP server process | The 20 tool functions registered via `server.tool()` | Claude Desktop / any MCP host, to know which tools exist and how to call them |
| **Component catalog** (the workflow registry) | YAML manifests under `composition/workflows/` plus an optional FAISS RAG index | Workflow components that the LLM composer can compose into runs | The composer in Tier 3 to build workflow YAMLs at run time |

The MCP tool registry is what this document is about. The component catalog is what the composer uses behind the scenes. The two registries are unrelated in code; the only place they meet is when the LLM calls `start_workflow`, the surface forwards the request, and the composer (which has access to the component catalog) drafts a YAML.

### The call flow, end to end

When the scientist asks Claude something that warrants a workflow run, the flow is:

```
1. Scientist (chat)              "find genes related to EEEV vaccine studies"
        ↓
2. Claude Desktop                  decides to call start_workflow with description=<query>, user_id=<the scientist>
        ↓ stdio MCP tools/call
3. MCP Surface · workflows.py     start_workflow(description, user_id, preferred_executor)
        ↓ HTTP POST /workflows/start
4. Control Plane · routes        StartWorkflowRequest validated, run + composer invoked
        ↓ in-process call
5. Tier 3 · Composition          composer drafts YAML referencing component catalog entries
        ↓ persists Run + GeneratedArtifact
6. Control Plane                  responds with new Run (status=PAUSED if approval policy gates pre-execute)
        ↓ HTTP response
7. MCP Surface                     marshals response into a dict
        ↓ MCP tools/call response
8. Claude Desktop                  sees the run id, can call show_diff next, or describe to user
```

Subsequent tool calls (`show_diff`, `execute_workflow`, `approve`, etc.) follow the same pattern: the surface translates an MCP call into an HTTP call against the control plane, awaits the response, and returns a dict. The control plane is the system of record; the surface is its façade.

### The control-plane client

`mcp_surface/control_plane_client.py` is the HTTP client that every tool function shares. It is built on `httpx.AsyncClient` (exact library subject to change; the principle is async HTTP). Each control-plane endpoint has a corresponding method on the client — `start_workflow(body)`, `show_yaml_diff(body)`, `execute_workflow(body)`, `list_pending_approvals()`, etc. The client takes typed Pydantic request bodies, returns typed Pydantic response bodies, and surfaces HTTP/connection errors as exceptions the surface tools can re-raise as user-readable errors.

The `_shared.get_client()` helper returns a singleton instance of this client. Sharing the client across tool calls preserves HTTP keep-alive and avoids the cost of TCP handshakes on every call.

`_shared.parse_run_id()` is the other shared helper — it parses a string into a `uuid.UUID` and produces a friendly error on bad input. Tools that take a `run_id` parameter run it through `parse_run_id` before constructing their request body.

### Autostart, again, in the call-flow context

When `APECX_MCP_AUTOSTART_BACKEND=1` is set:

- On startup, `server.py` issues a `_ping_control_plane(base_url)` call against `/healthz`.
- If the ping succeeds, the surface assumes a backend is already running and proceeds normally.
- If the ping fails (connection refused, timeout), the surface spawns `apecx-cp serve` as a subprocess via `subprocess.Popen(...)`, polls `/healthz` until it answers, and only then accepts the first MCP `tools/list` request.
- An `atexit` hook (`_terminate_child_backend`) is registered. When the MCP server exits — typically because Claude Desktop closes — the hook signals the child backend to terminate cleanly.

The result is a one-process-tree user experience: the scientist closes Claude Desktop, the MCP surface exits, the control plane shuts down, no orphan processes. SQLite WAL ensures the database is in a recoverable state on every clean exit.

### What the surface deliberately is not

It is worth being explicit about three things the surface is not, because each is a temptation worth resisting:

- **Not a workflow runtime.** No step is ever executed inside the surface. Even simple lookups go through the control plane's database routes. Keeping all execution behind the HTTP boundary preserves the deployment topology flexibility (see `diagrams/08`).
- **Not a state store.** The surface is stateless. Every tool call is independent. Run state, approval state, artifact metadata — all live in the control plane's database. If the surface dies and is respawned, no state is lost; the next tool call reads everything from the control plane.
- **Not a translation layer for arbitrary HTTP APIs.** Each surface tool is hand-written with the right type signatures, validation, and error handling for its specific control-plane endpoint. There is no generic "here's the OpenAPI spec, generate me MCP tools" path. This is deliberate — the surface is the design point where MCP-friendly types and Pydantic envelopes are negotiated, and that negotiation does not auto-generate well.

### Adding a new tool

The pattern, summarized:

1. Decide the tool's category and add its function to the appropriate `tools/<category>.py` module.
2. Write the function as `async def`, with primitive-typed parameters and a `dict` (or typed-Pydantic) return.
3. Write the docstring as if speaking to Claude — describe what the tool does, what it returns, and what error conditions to expect. The first paragraph is what the LLM sees most prominently.
4. Construct the appropriate Pydantic request body inside the function; call the corresponding `ControlPlaneClient` method.
5. Marshal the response with `.model_dump(mode="json")`.
6. Register the function in `server.py`'s `build_server()` with `server.tool()(<func>)`.
7. Add an integration test in `tests/integration/` that spins up the surface and the control plane, calls the new tool, and verifies the response.

If the underlying control-plane endpoint does not exist yet, do not register the surface tool. The "tool that always errors is worse than no tool" rule applies.

---

## References

### Code paths

- `apecx-mcp-integration/src/apecx_integration/mcp_surface/server.py` — entry point, tool registration, autostart logic.
- `apecx-mcp-integration/src/apecx_integration/mcp_surface/tools/` — per-category tool modules.
- `apecx-mcp-integration/src/apecx_integration/mcp_surface/tools/_shared.py` — `get_client()`, `parse_run_id()`.
- `apecx-mcp-integration/src/apecx_integration/mcp_surface/control_plane_client.py` — shared async HTTP client.
- `apecx-mcp-integration/src/apecx_integration/control_plane/schemas/api.py` — Pydantic request/response envelopes.
- `apecx-mcp-integration/src/apecx_integration/control_plane/schemas/enums.py` — `ExecutorKind`, `ApprovalKind`, `ApprovalStatus`, `RunStatus`, `StepStatus`, `ArtifactKind`, `ProvenanceEventType`, `StepCategory`.

### Diagrams

- `diagrams/01_system_architecture.svg` — where the MCP Surface sits in the four-tier architecture.
- `diagrams/02_workflow_lifecycle.svg` — the lifecycle stages with their MCP tool calls.
- `diagrams/03_mcp_tool_catalog.svg` — the 20 tools organized by category.
- `diagrams/08_deployment_topology.svg` — three valid host topologies and which tools live where.

### Companion documents

- `docs/violin_bvbrc_workflow.md` — the workflow that this surface drives in §3 of that document.
- `architectural_plan.md` (workspace root) — §3 (tier definitions), §4 (data model), §5.1 (vertical slice), and the §R3 Round-3 revisions.
