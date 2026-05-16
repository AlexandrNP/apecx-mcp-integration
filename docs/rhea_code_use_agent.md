# Rhea code-use agent — design, status, honest blockers

**Date**: 2026-05-14. **Status**: agent + spawner complete and tested;
the live Rhea *tool catalog* (beyond `find_tools`) is gated on a
Rhea-backend infra arc (documented below).

This is the **Agent path** counterpart to the Rhea **Step path**
(`ToolExecutionStep` + `RheaAdapter`, shipped earlier). Where the Step
path dispatches ONE pinned Rhea tool inside a workflow, the agent does
open-ended **discover-then-use** tool calling.

## Components shipped this chain

| Component | Location | Role |
|---|---|---|
| `DockerMCPWorker` | `nanobrain/library/runtime/mcp_worker.py` | Generic: spawn + health-check + tear down a Docker-hosted MCP worker. FAIL-LOUD at every step. |
| `RheaCodeUseAgent` | `nanobrain/library/agents/rhea_code_use_agent.py` | A nanobrain `Agent` with a **multi-round** tool-use loop, holding `WebSearchTool` + the live Rhea MCP catalog. |
| `WebSearchTool.get_schema()` | `nanobrain/library/tools/web_search.py` | OpenAI tool-spec — makes `WebSearchTool` a first-class Agent tool-calling citizen. |
| 3 framework bug fixes | `nanobrain/core/agent.py`, `agent_logging.py` | `None`-content crashes in the agent LLM-call/logging path (a pure tool-call message correctly has `content: null`). |

Tests: `nanobrain/tests/integration/test_rhea_code_use_agent.py`
(8 tests — 7 unconditional with a fake LLM + fake backend, 1 gated on
`$RHEA_MCP_URL`). All green.

## Architecture

### The pipeline spawns its own Rhea MCP worker

Per the directive *"the Rhea-dependent pipeline is responsible for
spawning Rhea MCP"*, `DockerMCPWorker` owns the worker lifecycle:

```python
worker = DockerMCPWorker(
    image="rhea-server:apecx-integration",
    container_name="apecx-rhea-server",
    mcp_url="http://localhost:3001/mcp/",
    host_port=3001,
    env={"HOST": "0.0.0.0",
         "REDIS_HOST": "host.docker.internal",
         "AGENT_REDIS_HOST": "host.docker.internal"},
)
await worker.ensure_running()   # reuse if already up; else spawn + wait for handshake
# ... use the worker ...
await worker.stop()             # tears down ONLY if we spawned it
```

`ensure_running` returns **only** after an MCP `tools/list` round-trip
actually succeeds — a container that "looks up" but never answers the
handshake is the silent-failure shape this guards against.

**The minimum-viable spawn recipe** (empirically established this
chain): `rhea-server:apecx-integration` + a reachable Redis. Two env
overrides matter and were found by testing, not assumption:

* `HOST=0.0.0.0` — the Rhea server's `Settings.host` defaults to
  `localhost`, so without this it binds the *container's* loopback
  and the `-p 3001:3001` forward cannot reach it.
* `REDIS_HOST=host.docker.internal` — the only EAGER startup
  dependency. `RedisConnector(...)` is built at module import; Postgres
  (`create_async_engine`) and the embedding `OpenAI()` client are both
  lazy.

### The agent is a live MCP client (not per-tool RheaMCPDispatcher)

Rhea's catalog is **dynamic**: a fresh worker exposes exactly one
tool, `find_tools` — a meta-tool that semantic-searches Rhea's
registry and *populates* relevant tools on demand. A static per-tool
`RheaMCPDispatcher` (materialized once from a fixed UTD) cannot track
a catalog that grows at runtime.

So `RheaCodeUseAgent` holds an `MCPTransport` and **re-queries Rhea's
`tools/list` on every tool-use round**: after the LLM calls
`find_tools`, the next round's tool list includes whatever Rhea just
surfaced. `RheaMCPDispatcher` remains correct for a *known, fixed*
Rhea tool; this agent is correct for *discover-then-use*.

### Multi-round tool use (framework-capacity expansion)

nanobrain's `SimpleAgent` / `ConversationalAgent` do **single-round**
tool use (call LLM → run tool calls once → call LLM once more →
return). Discover-then-use needs MULTIPLE rounds. `RheaCodeUseAgent`
implements the multi-round loop on top of the framework's existing
`_call_llm` primitive — native, no LangChain dependency. Each round:
assemble `WebSearchTool` specs + the live Rhea catalog → `_call_llm`
→ dispatch tool calls → feed results back → repeat until the LLM
answers tool-free or `max_tool_rounds` is hit (the cap is explicitly
reported, never silently truncated).

A tool *dispatch* failure (Rhea HTTP error, web backend error) is fed
back to the LLM as the tool result text — the model sees
`"Tool X failed: ..."` and can react. What is never hidden is a tool
that silently returns nothing.

## What works, verified

* `DockerMCPWorker` spawns the Rhea worker from the pre-built image +
  the existing Redis, and reaches a healthy MCP handshake in ~11s.
  Reuse path + spawn-from-scratch path both verified.
* `RheaCodeUseAgent` end-to-end against **real Ollama** (mistral-nemo)
  + **real DuckDuckGo**: given *"What does the FASTA format store?
  Use web search"*, the agent ran the multi-round loop — round 1 the
  LLM called `web_search` (real DDG queries), round 2 it synthesized a
  grounded answer. "finished after 2 round(s)."
* Against the live Rhea worker, the agent's `_rhea_tool_specs()`
  returns the live catalog — `rhea__find_tools` is discovered and
  becomes an LLM-callable tool.

## `find_tools` + tool execution — now working (2026-05-14 update)

The earlier version of this doc listed `find_tools` returning real
tools as a deferred "Rhea-backend infra arc." **That arc was
subsequently done** — see the dedicated writeup
`docs/rhea_tool_execution_findings.md`. In brief:

- The **ingestion path is fixed** — `rhea/preprocess/update_tools.py`
  was rewritten to actually fetch (ToolShed API for discovery +
  GitHub for content, since the ToolShed's own file endpoints are
  auth-gated) → parse → embed → insert. 20 real Galaxy tools ingested.
- **`find_tools` works** — semantic search populates the live catalog.
- The **Parsl worker-connectivity issue is fixed** — `parsl_config.py`
  gained a `backend="local"` mode (local-subprocess worker, no
  container) for Docker-Desktop-for-Mac.
- The **execution substrate is driven to working** — a tool's command
  executes end-to-end via the local Parsl worker + a conda env.
- The one **remaining gap** is driving real *inputs* into a tool to
  get a non-null *output* — Rhea's Galaxy `<repeat>`/`<conditional>`
  input-schema construction. That is a separate Rhea-internals arc,
  diagnosed in `rhea_tool_execution_findings.md`.

So `RheaCodeUseAgent` against a properly-configured Rhea worker now
sees a real, populated `find_tools` catalog — not just the bare
`find_tools` meta-tool. The agent code was correct all along; what
changed is the Rhea backend now has tools to discover.

## Brutal-truth assessment

**What's genuinely done**: the pipeline-managed worker spawner (real,
proven), the multi-round code-use agent (real, runs end-to-end
against Ollama + DDG + a live Rhea worker), the framework-capacity
expansions (native multi-round tool use; `WebSearchTool.get_schema`;
3 `None`-content framework bug fixes), 8 passing tests.

**What's honestly gated**: `find_tools` *returning bioinformatics
tools*. That needs the Rhea backend's embedding service + a populated
tool registry — a Rhea-deployment infra arc, explicitly out of this
chain's scope. The agent demonstrates the discover-then-use
*mechanism* end-to-end; what it cannot demonstrate without that infra
is Rhea actually *having* tools to discover.

**Tool-selection quality** is model-dependent. With mistral-nemo the
agent reliably picked `web_search` for a lookup task in the smoke
test, but small local models are known to mis-select or skip tools;
a larger model would be more reliable. This is a real adoption-
reliability caveat, not a defect — measured, not hidden.
