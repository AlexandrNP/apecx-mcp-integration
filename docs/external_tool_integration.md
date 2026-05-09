# APECx External Tool Integration — Rhea and GalaxyMCP

**Status:** Design / pre-implementation
**Audience:** Tier-2B implementors, workflow authors
**Supplements:** `multiagent_architecture.md §7`

---

## 1. Overview

APECx's tool execution tier (Tier 2B) connects the multi-agent reasoning system to
external computational tools: sequence analysis, structural computation, property prediction,
simulation, and any other domain-specific computation not implemented as a retrieval step.

Rather than hard-coding wrappers for individual tools, the system delegates to two
external services that provide tool discovery and isolated execution:

- **Rhea** (primary): RAG-indexed scientific tool catalog + Parsl/Academy distributed execution
- **GalaxyMCP** (conditional): Galaxy platform MCP server (pending availability)

Both services are accessed through a single `ToolExecutionStep` interface. The routing
layer chooses the backend at runtime based on configuration.

---

## 2. The ToolExecutionStep Contract

`ToolExecutionStep` is a nanobrain `BaseStep` subclass that consumes a Unified
Tool Descriptor (UTD) reference and dispatches the call to the matching backend
adapter (Rhea, native, GalaxyMCP). The recasting from "ToolExecutionAgent" (the
prior framing) to "ToolExecutionStep" is per `nanobrain_alignment_audit.md §4.1`
finding **F-4**: tool dispatch is a Step concern, not an `Agent` concern.
`Agent` is reserved for LLM dispatch.

The Step's input data unit carries the resolved UTD (per `tool_descriptor_contract.md §2`,
formalized as the `UnifiedToolDescriptor` nanobrain primitive proposed as **G15**
in `nanobrain_capability_gaps.md`). The Step's output data unit carries the typed
result, mapped through the UTD's output schema into the appropriate `DataUnit`
subclass (per `tool_descriptor_contract.md §7`).

The orchestrator never instantiates `ToolExecutionStep` directly — it appears as
a step in the *target* workflow YAML produced by the meta-workflow's
`PlanLoweringStep` (per `meta_workflow_orchestration.md`). The orchestrator's
Phase 0 emits a `tool_invocations` list in the `ExecutionPlanConfig` (G16); the
lowering step substitutes each entry into a `ToolExecutionStep` configured with
the resolved UTD.

### 2.1 Input

The Step's input data unit carries (in addition to the UTD reference):

```json
{
  "tool_description": "string",
  "tool_name": "string?",
  "inputs": {
    "parameter_name": "value | ProxyStoreKey"
  },
  "execution_context": {
    "session_id": "string",
    "layer_id": "string",
    "hpc_eligible": "bool",
    "timeout_seconds": "int"
  }
}
```

- `tool_description`: Natural-language description used for RAG-based tool discovery.
  The agent selects the best-matching tool from its catalog.
- `tool_name`: Optional exact tool name. When provided, RAG discovery is skipped.
- `inputs`: Tool-specific parameters. Values may be raw (small data) or ProxyStore keys
  (large data — files, datasets, intermediate results). The backend resolves keys before
  execution.
- `hpc_eligible`: Whether the tool may be submitted to an HPC resource if local
  capacity is insufficient.

### 2.2 Output

```json
{
  "tool_name": "string",
  "tool_version": "string?",
  "inputs_resolved": {},
  "result": {
    "output_key": "ProxyStoreKey | value",
    "result_summary": "string"
  },
  "provenance": {
    "tool_catalog_entry": "string",
    "execution_backend": "rhea | galaxy | parsl_local",
    "container_image": "string?",
    "execution_time_seconds": "float",
    "resource_used": "string?"
  },
  "errors": []
}
```

All large outputs are returned as ProxyStore keys, not raw data. Small scalar results
(a score, a short identifier) may be returned inline.

### 2.3 Failure modes

| Failure | Degradation | Action |
|---|---|---|
| Tool not found in catalog | `tool_not_found` | Return empty result; mark layer finding as `tool_skipped` |
| Tool execution timeout | `execution_timeout` | Return partial result if available; log to evidence package |
| Backend unavailable | `backend_unavailable` | Fall through to next configured backend; if all fail, mark layer finding as `tool_unavailable` |
| Input resolution failure | `input_error` | Surface as capability gap; do not retry |

---

## 3. Rhea Integration

Rhea (https://github.com/chrisagrams/rhea) provides a scalable MCP server exposing
scientific tools through dynamic RAG-based discovery and distributed Parsl/Academy execution.

### 3.1 Architecture

Rhea's architecture has four layers relevant to APECx integration:

```
APECx ToolExecutionStep
       │
       │ HTTP + SSE (MCP protocol)
       ▼
Rhea MCP Server
  ├── find_tools(query) → tools/list notification
  └── invoke(tool_name, params) → progress stream → result
       │
       │ Parsl task dispatch
       ▼
Academy Container
  ├── auto-install tool dependencies
  ├── execute tool (stateless, idempotent)
  └── I/O via ProxyStore (Redis) — reference keys, not data
```

### 3.2 Tool Discovery Cycle

Rhea uses a continuous discovery loop rather than a static tool manifest:

```
1. RheaToolAgent.find_tools(description)
   → Rhea embeds the query (pgvector) and returns ranked tool candidates
   → Client receives a `tools/list` notification updating the active tool registry
   → The agent selects the best match and invokes it

2. RheaToolAgent.invoke(tool_name, inputs)
   → Inputs are uploaded to ProxyStore; keys are passed to Rhea
   → Rhea spawns a Parsl task → Academy container with auto-installed deps
   → Progress notifications stream 0% → 100%
   → On completion, result keys are returned; agent resolves keys to get outputs
```

**Implication for APECx:** The `RheaToolAgent` must maintain a short-lived tool registry
per workflow run (refreshed by `find_tools` calls). It must not cache tool registries
across sessions because Rhea's catalog is updated as new tools are registered.

### 3.3 ProxyStore I/O Model

Rhea uses **reference-based I/O**: all non-trivial inputs and outputs are passed as
ProxyStore keys (Redis-backed opaque references), not as raw data. This enables:

- Distributed execution without data transfer bottlenecks (the Parsl worker reads
  data directly from Redis, not from the APECx node)
- Large file handling (datasets, intermediate computational results) without HTTP payload limits
- Provenance tracking (each key is a content-addressed reference)

**APECx must adopt this model for tool I/O.** When a `DomainLayerStep` produces a
result that will be passed to a tool (e.g., a retrieved dataset for analysis), it must
upload the data to ProxyStore and pass the key to `ToolExecutionStep`. The tool's result
is returned as a ProxyStore key, which the evidence accumulation step resolves when
packaging the evidence bundle.

```
DomainLayerStep(data) → ProxyStore.upload(data) → key
ToolExecutionStep(tool="analysis_tool", inputs={data: key})
  → execute → ProxyStore.upload(result) → result_key
EvidenceAccumulationStep: ProxyStore.resolve(result_key) → result
```

### 3.4 Parsl and Academy Execution

Rhea's execution backend uses:

- **Parsl** for task scheduling — supports local execution, HPC (PBS, SLURM), and
  Kubernetes. APECx workflows that need HPC-scale tool execution can configure Rhea
  to submit Parsl tasks to HPC clusters using the same PBS configurations already used
  for nanobrain's `export_hpc_bundle` path.
- **Academy containers** for dependency isolation — each tool runs in a container with
  its dependencies auto-installed from the tool's metadata. This eliminates the need
  for APECx to manage tool dependency trees.

**APECx does not manage Parsl or Academy directly through the Rhea path.** These are
internal to Rhea. APECx only sees the MCP HTTP+SSE interface.

### 3.5 Configuration

```yaml
# apecx-mcp-integration configs/tool_execution.yml

rhea:
  enabled: true
  url: "${APECX_RHEA_URL}"                         # e.g., http://rhea-service:8080
  proxystore_url: "${APECX_RHEA_PROXYSTORE_URL}"   # Redis URL for ProxyStore
  discovery_cache_ttl_seconds: 300                  # tool registry refresh interval
  request_timeout_seconds: 600                      # max per tool invocation
  progress_poll_interval_seconds: 5

galaxy_mcp:
  enabled: false                                    # enabled when APECX_GALAXY_MCP_URL is set
  url: "${APECX_GALAXY_MCP_URL}"
```

When `APECX_RHEA_URL` is unset, `ToolExecutionStep` falls back to the existing
`export_hpc_bundle` / Parsl local path (preserving backward compatibility).

### 3.6 Registering domain-specific tools in Rhea's catalog

Tools specific to APECx's configured domain that are not already in Rhea's catalog can be
registered by adding tool definitions to Rhea's tool repository. This is the preferred
mechanism over hard-coding tool wrappers in APECx. Registration is out-of-band and
tracked separately from the APECx codebase.

The registration process is:
1. Author a tool definition (name, description, parameters, container spec)
2. Submit to Rhea's tool repository
3. Rhea re-indexes; the tool appears in `find_tools` queries without any APECx changes

---

## 4. GalaxyMCP Integration (Conditional)

Galaxy (https://galaxyproject.org) is a widely-used scientific workflow platform with a
large tool catalog covering sequence analysis, data transformation, and domain-specific
computation.

### 4.1 Integration model

If Galaxy provides a local MCP server (`APECX_GALAXY_MCP_URL` is set), `GalaxyToolAgent`
wraps it with the same `ToolExecutionStep` interface as `RheaToolAgent`. The
`ToolExecutionOrchestrator` can route to either backend without knowing which is active.

```
ToolExecutionOrchestrator.execute(tool_description, inputs)
  → if APECX_RHEA_URL set:     RheaToolAgent(tool_description, inputs)
  → elif APECX_GALAXY_MCP_URL: GalaxyToolAgent(tool_description, inputs)
  → else:                       LocalParslAgent(tool_description, inputs)
```

### 4.2 Key differences from Rhea

| Dimension | Rhea | GalaxyMCP |
|---|---|---|
| Tool discovery | RAG over embedded catalog | Galaxy tool registry (structured) |
| I/O model | ProxyStore reference-based | Galaxy dataset IDs (reference-based equivalent) |
| Execution | Parsl + Academy containers | Galaxy job queue |
| Dependency management | Container-based auto-install | Galaxy tool shed |
| HPC support | Parsl backends (configurable) | Galaxy cluster queues |
| MCP transport | HTTP + SSE | TBD (pending Galaxy MCP availability) |

### 4.3 Deferral condition

`GalaxyToolAgent` is **not implemented until `APECX_GALAXY_MCP_URL` is confirmed
deployable.** The interface is defined here for design consistency; the implementation
ticket is blocked on confirmation of Galaxy MCP availability.

---

## 5. Local Parsl Fallback (Existing Path)

When no external tool service is configured, `ToolExecutionStep` falls back to the
existing local Parsl path used by `export_hpc_bundle`. In this mode:

- Tools must be pre-installed in the execution environment (no auto-installation)
- I/O is file-based (no ProxyStore)
- HPC submission is manual (user runs qsub on exported bundle)
- This path is suitable for development and for offline HPC workflows

The fallback is explicitly a **lower capability tier**, not a production equivalent of
Rhea. New tool integrations should always target Rhea first.

---

## 6. Tool Execution in the Workflow Context

### 6.1 Where tool execution fits in the layered reasoning cascade

Tool execution is invoked within domain reasoning layers, not as a standalone phase:

```
Sequence layer:
  retrieve_data(source_A) → dataset
  ToolExecutionStep("sequence alignment", {dataset: key}) → alignment
  compute_conservation(alignment) → conservation_findings

Structural layer:
  retrieve_data(source_B) → structure
  ToolExecutionStep("surface analysis", {structure: key}) → accessibility
  map_conservation_to_structure(conservation_findings, accessibility) → spatial_findings

Design layer:
  ToolExecutionStep("property prediction", {structure: key, variants: key})
  → predicted_properties
```

Tool outputs are `LayerResult.findings` entries with `source_refs` pointing to
`EvidencePackage.tool_outputs` entries.

### 6.2 Provenance requirement

Every tool invocation must produce a `provenance` record (see §2.2) that is stored in
`EvidencePackage.tool_outputs`. This is non-negotiable for HPC-ready workflows: the
PBS bundle must include the provenance of all tool results so that any reader of the
bundle can reproduce the computation independently.

**What specifically gets recorded** (binds to gap **G4** in
`nanobrain_capability_gaps.md` — step-level provenance threading):

| Field | Required | Source | Notes |
|---|---|---|---|
| `tool_name` | yes | UTD `display_name` | Resolved at dispatch time |
| `tool_descriptor_id` | yes | UTD `descriptor_id` | `<backend>:<tool_id>@<version>` |
| `tool_descriptor_hash` | yes | UTD `descriptor_hash` | Pinned at workflow lower-time so a UTD update doesn't invalidate replay |
| `inputs_resolved` | yes | The actual inputs after ProxyStore key resolution | Subject to G4 `redact: tool_args` if the tool's args carry sensitive parameters |
| `result_keys` | yes | ProxyStore keys returned by the tool | NOT the materialized payload — keys only |
| `result_hashes` | yes | SHA-256 of each result payload | Computed at write time; preserved under G4's `payload` redaction |
| `execution_backend` | yes | `rhea`, `galaxy`, or `parsl_local` | Per §2.2 |
| `container_image` | when applicable | Rhea Academy container digest | Pinned by digest, not tag |
| `execution_time_seconds` | yes | Wall time from dispatch to result | Excludes ProxyStore round-trip time |
| `resource_used` | when applicable | Per-executor resource (e.g., HPC node count, memory peak) | Optional for local Parsl |
| `errors` | when applicable | List of `{code, detail, source}` per the G6 escape valve | Empty list when invocation succeeded cleanly |
| `partial` | when applicable | `true` if the tool returned partial results per G6 escape valve | Forwarded to consumer steps |

**Default redaction.** When the workflow's provenance config sets `redact:` to
the default `["prompts", "executor_env"]` (per G4), the tool-invocation
provenance record is unaffected — neither prompts nor executor env-vars are
present in tool provenance. Workflows that handle sensitive tool args MUST
explicitly add `tool_args` to the redact list; the framework does NOT default
to `tool_args` redaction because the args are typically the most useful field
for replay.

**Bundle-level guarantees.** The PBS bundle exporter stitches every
tool-invocation provenance record (above) with the upstream step provenance
(G4) into a single JSONL graph (per `hpc_reproducibility_spec.md §5`).
A bundle reader walking the graph can reconstruct every tool call and verify
its inputs match the declared `tool_descriptor_hash`. If the descriptor has
changed since the run, the reader sees the original descriptor body inside
the bundle's `manifest.data_sources` (per `data_layer_evolution.md §6` —
descriptors are content-addressed in the snapshot archive).

---

## 7. Security Considerations

External tool execution introduces a code execution surface. The following controls apply:

1. **No arbitrary code injection.** `ToolExecutionStep` accepts structured parameters only.
   Tool selection is by name or description through Rhea's catalog, not by code upload.

2. **Input validation at boundary.** All inputs to `ToolExecutionStep` are validated
   against the tool's declared parameter schema before dispatch. Rhea enforces this
   on the server side; APECx enforces it on the client side as well.

3. **Container isolation.** Rhea's Academy containers provide execution isolation. APECx
   does not need to sandbox tool execution independently for the Rhea path.

4. **Network egress.** `RheaToolAgent` connects only to `APECX_RHEA_URL` (configured
   per deployment). No other egress is introduced by the tool execution path.

---

## 8. Open Questions

1. **Rhea deployment model.** Does Rhea run as a sidecar service alongside `apecx-mcp`,
   or as a separate long-lived service? This affects startup sequencing and the health
   check that `RheaToolAgent` performs at initialization.

2. **ProxyStore backend for APECx.** Rhea uses Redis for ProxyStore. Does APECx deploy
   a shared Redis instance, or does each `RheaToolAgent` instance connect to Rhea's
   Redis? The distinction matters for session-scoped data lifetime.

3. **Galaxy MCP availability.** The GalaxyMCP integration is deferred until Galaxy
   provides a deployable MCP server. Confirm before allocating implementation capacity.

4. **Tool catalog overlap.** Rhea and Galaxy may have overlapping tool coverage for
   common operations. The routing logic should prefer Rhea for all tools unless a
   Galaxy-specific tool is requested by name. Define the de-duplication policy.

---

## 9. Reference

| Resource | Location |
|---|---|
| Rhea repository | https://github.com/chrisagrams/rhea |
| Tier-2B architecture overview | `docs/multiagent_architecture.md §7` |
| Workflow output contract (tool_outputs evidence) | `docs/workflow_output_contract.md §7` |
| Nanobrain workflow design (tool invocation in layers) | `docs/nanobrain_workflow_design.md` |
| HPC bundle format | `apecx-mcp-integration/CLAUDE.md §PBS bundle export` |
