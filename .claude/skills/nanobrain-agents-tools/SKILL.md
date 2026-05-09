---
name: nanobrain-agents-tools
description: How to author Agents and Tools in nanobrain, including LLM model selection, system-prompt-in-YAML rule, Tool subclassing (`async def execute`), tool registration via config (NOT programmatic), the A2A (Agent-to-Agent) protocol surface, and the MCP (Model Context Protocol) client integration. Read whenever you create an Agent, a Tool, or an external-system integration.
---

# nanobrain-agents-tools

## Why this skill exists

Agents are the LLM-bearing components in nanobrain. Tools are the
capabilities they call. Both must be configured (not constructed) and both
have strict rules about prompt provenance, async signatures, and protocol
metadata. Get this wrong and the agent fails at initialization or at first
LLM tool-call dispatch.

> **Tier alignment (per `nanobrain_alignment_audit.md` F-4):** "Tool
> dispatch" is a **Step concern, not an Agent concern.** If you're tempted
> to write `class FooToolExecutionAgent(Agent)`, stop — make it
> `class FooToolExecutionStep(BaseStep)`. `Agent` is reserved for LLM
> dispatch. Tool execution = Step that consumes a tool descriptor and
> dispatches to a backend adapter.

> **Future framework primitives that subsume current patterns:** Gap **G14**
> proposes a `PromptTemplate` framework primitive (carrying content_hash +
> hole substitution + few-shot bundling); gap **G15** proposes promoting
> `UnifiedToolDescriptor` (UTD) into `nanobrain.core.tool` so the same
> descriptor schema serves Rhea, Galaxy, and native tools. Until G14/G15
> ship, `system_prompt:` in YAML and `tool_card:` on `ToolConfig` are the
> existing surfaces.

## File:line ground truth

| Concern | File | Approx. line |
|---|---|---|
| `Agent` / `AgentConfig` | `nanobrain/nanobrain/core/agent.py` | 49–485 |
| Agent `_init_from_config` | `nanobrain/nanobrain/core/agent.py` | 785–887 |
| Agent prompt-template handling | `nanobrain/nanobrain/core/agent.py` | 891–923 |
| `ToolBase` / `ToolConfig` | `nanobrain/nanobrain/core/tool.py` | 32–571 |
| Tool `execute` (abstract) | `nanobrain/nanobrain/core/tool.py` | 525 |
| `ExternalTool` | `nanobrain/nanobrain/core/external_tool.py` | full file |
| `A2ASupportMixin`, `A2AClient` | `nanobrain/nanobrain/core/a2a_support.py` | 389–1221 |
| A2A card schema | `nanobrain/nanobrain/core/a2a_support.py` | 145–222 |
| `MCPSupportMixin` | `nanobrain/nanobrain/core/mcp_support.py` | 668–981 |
| `PromptTemplateManager` | `nanobrain/nanobrain/core/prompt_template_manager.py` | 47–283 |

## Agent inheritance

- `Agent` (abstract base, `agent.py:485`) — base for all agents
- `ConversationalAgent` — conversation-with-history agent
- `EnhancedCollaborativeAgent` — adds A2A and MCP collaboration

`COMPONENT_TYPE = "agent"`, `REQUIRED_CONFIG_FIELDS = ['name']`.

## AgentConfig schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | str | ✅ | — | Agent identifier |
| `description` | str | | "" | |
| `model` | str | | "gpt-3.5-turbo" | LLM model id |
| `temperature` | float | | 0.7 | Validated 0.0–2.0 |
| `max_tokens` | Optional[int] | | None | |
| `system_prompt` | str | | "" | **Live in YAML, not Python** |
| `prompt_templates` | Optional[Dict] | | None | Inline templates |
| `prompt_template_file` | Optional[str] | | None | YAML file with templates |
| `executor_config` | Optional[ExecutorConfig] | | None | |
| `tools` | List[Dict] | | [] | Inline tool refs (`class:` + `config:`) |
| `tools_config_path` | Optional[str] | | None | External tools YAML |
| `auto_initialize` | bool | | true | |
| `debug_mode` | bool | | false | |
| `enable_logging` | bool | | true | |
| `log_conversations` | bool | | true | |
| `log_tool_calls` | bool | | true | |
| `agent_card` | Optional[Dict] | | None | **Required if using A2A** |

## Working agent YAML

```yaml
# config/chat_agent.yml
class: "nanobrain.library.agents.enhanced_collaborative_agent.EnhancedCollaborativeAgent"
name: chat_agent
description: "Helpful assistant with tool calling"

model: gpt-4
temperature: 0.3
max_tokens: 2000

system_prompt: |
  You are a helpful assistant. Use the tools available to answer questions.
  Always cite the tool you used.

tools:
  - class: "nanobrain.library.tools.WebSearchTool"
    config: "config/web_search.yml"
  - class: "nanobrain.library.tools.DocumentAnalyzer"
    config: "config/doc_analyzer.yml"

executor:
  class: "nanobrain.core.executor.LocalExecutor"
  config:
    executor_type: local
    name: chat_agent_executor
    max_workers: 1
    timeout: 30

agent_card:
  name: chat_agent
  description: "Conversational helper agent"
  url: "http://localhost:8000/agents/chat"
  version: "1.0.0"
  capabilities:
    streaming: true
    pushNotifications: false
    stateTransitionHistory: false
  authentication:
    schemes: ["bearer"]
  defaultInputModes: ["text"]
  defaultOutputModes: ["text"]
  skills:
    - id: chat
      name: chat
      description: General-purpose conversational assistance
      tags: ["conversation", "qa"]
      examples: ["What is photosynthesis?", "Summarize this document."]
```

## Prompts in YAML, not Python

Hardcoded prompts in Python source are forbidden by workspace policy and
flagged by `.claude/scripts/review_policy_check.py`. Three legal sources:

1. **`system_prompt:` field in agent YAML** — for static system prompts.
2. **`prompt_template_file:` referencing a YAML** — for templated prompts.
3. **`prompt_templates:` dict inline in agent YAML** — for small sets.

The `PromptTemplateManager` loads via `from_config` and prohibits
constructor calls. Path resolution follows the same rules as the
component_base path resolver.

```yaml
# config/agent_prompts.yml
prompts:
  greeting: |
    Hello, I am {agent_name}. How can I help with {topic}?
  summarize: |
    Summarize the following text in {n} bullet points: {text}
```

```yaml
# config/agent.yml
name: my_agent
prompt_template_file: config/agent_prompts.yml
```

## LLM integration

The agent calls an LLM via an internal client (provider chosen by `model:`
field). Supported providers (inferred from model strings):

- OpenAI (`gpt-3.5-turbo`, `gpt-4`, `gpt-4-turbo`)
- Anthropic (`claude-3-opus`, `claude-3-sonnet`, `claude-3-haiku`)
- Open-source (`llama-2`, etc.) via custom executor

API keys come from environment variables: `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`. Never hardcode.

LLM calls are async; timeouts default to 300s.

Token tracking: `agent_response.processing_metadata.tokens_used` collects
per-call usage. Session totals are on the agent: `_total_tokens_used`,
`_total_llm_calls`.

## Tool authoring

```python
# my_project/tools/my_tool.py
from nanobrain.core.tool import ToolBase, ToolConfig

class MyTool(ToolBase):
    @classmethod
    def _get_config_class(cls):
        return ToolConfig

    async def execute(self, **kwargs):
        # Validate kwargs against self.config.parameters
        query = kwargs['query']
        result = await self._call_real_backend(query)
        return result
```

Rules:

- `execute` MUST be `async def`. Sync `def` causes
  `TypeError: object NoneType can't be used in 'await' expression`
  at first call.
- Tool runtime metadata (name, description, parameters JSON Schema) is in
  `ToolConfig`. The agent uses this to advertise the tool to the LLM.

### ToolConfig schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | str | ✅ | Tool name |
| `tool_type` | str | ✅ | "function" / "external" / etc. |
| `description` | str | ✅ | Used by LLM to decide whether to call |
| `parameters` | dict | ✅ | JSON Schema for arguments |
| `async_execution` | bool | | Default true |
| `timeout` | int | | Default 30 |
| `tool_card` | dict | ✅ | Discovery metadata; required for A2A |

### Working tool YAML

```yaml
# config/web_search.yml
class: "my_project.tools.WebSearchTool"
name: web_search
tool_type: function
description: "Searches the web and returns top N results."
async_execution: true
timeout: 30
parameters:
  type: object
  properties:
    query:
      type: string
      description: "Search query"
    n:
      type: integer
      description: "Max number of results"
      default: 5
  required: ["query"]
tool_card:
  capabilities:
    - "web_search"
    - "snippet_extraction"
  data_sources:
    - "https://api.example.com/search"
```

The framework raises if `tool_card` is missing:

```
Missing mandatory 'tool_card' section in configuration for {ClassName}.
All tools must include tool card metadata for proper discovery and usage.
```

## Tool registration

**Config-based only.** You do not call `agent.register_tool(...)` from code.
You list tools in the agent's YAML under `tools:`. The agent's `_init_from_config`
resolves each, calls `ToolClass.from_config(config_path)`, and binds it to
the internal `tool_registry`.

```yaml
tools:
  - class: "nanobrain.library.tools.WebSearchTool"
    config: "config/web_search.yml"
```

For tool sets shared across agents, use `tools_config_path:` to point at a
single YAML defining many tools.

## Tool execution flow

1. LLM emits `tool_calls` with `name` and `arguments` (OpenAI function-calling format).
2. Agent looks up the tool: `self.tool_registry.get(name)`.
3. Arguments validated against `tool.get_schema()` (built from
   `ToolConfig.parameters`).
4. `await tool.execute(**arguments)` runs.
   - If `async_execution: true` and `func` is a coroutine: `await func(**kwargs)`.
   - If sync: `await loop.run_in_executor(None, func, **kwargs)`.
   - Wrapped in `asyncio.wait_for(..., timeout=...)` if timeout set.
5. Result returned to agent; logged in `processing_metadata.tools_used`.
6. Agent feeds tool result back to LLM for next-turn synthesis.

## A2A (Agent-to-Agent) protocol

`A2ASupportMixin` adds A2A surface to an agent. The agent advertises an
`A2AAgentCard` that describes its capabilities and skills (Google A2A spec
v1.0).

Use cases: another agent (or a UI / orchestrator) discovers this agent's
capabilities, sends a task, and polls for completion.

Methods (when mixed in):

- `call_a2a_agent(card_url, task_payload)` — call a remote A2A agent
- `discover_and_register_a2a_agents(registry_url)` — register peers
- `get_a2a_status()` — current task statuses

Task lifecycle states: `SUBMITTED → WORKING → INPUT_REQUIRED → COMPLETED | FAILED`.

To expose your agent via A2A, the simplest path is to inherit from
`EnhancedCollaborativeAgent` and provide an `agent_card:` in YAML — the
mixin handles registration.

## MCP (Model Context Protocol)

Nanobrain's MCP support is **client-side only**: agents connect to external
MCP servers, discover tools, and register them as native tools.

Wiring (`mcp_support.py:725–758`):

```python
self.mcp_client = MCPClient.from_yaml_config(
    self.mcp_config_path,
    base_path=base_path,
    logger=self.mcp_logger,
)
await self.mcp_client.initialize()
await self._discover_and_register_mcp_tools()
```

Each discovered MCP tool is wrapped in an `MCPTool` (a `Tool` subclass) and
registered with the agent's tool registry. From the LLM's perspective, an
MCP tool looks identical to a native tool.

**Watch out:** if `aiohttp` is missing, the MCP client falls back to a mock
that doesn't actually communicate. URLs starting with `mock://` are also
auto-mocked. **For production, ensure aiohttp is installed and you're
connecting to real MCP server URLs.**

## ExternalTool vs. Tool

| Aspect | Tool | ExternalTool |
|---|---|---|
| Execution | In-process Python | Subprocess / external binary |
| Initialization | Sync | Lazy async (use `ensure_initialized`) |
| Install verification | None | Required at init |
| Examples | LangChain wrappers, Python functions | bioinformatics CLIs (muscle, mmseqs2) |

`ExternalTool.ensure_initialized()` is mandatory before use; do not call
async work in `__init__`.

## Verbatim error messages

```
Missing mandatory 'tool_card' section in configuration for {ClassName}.
All tools must include tool card metadata for proper discovery and usage.
```
> Add `tool_card:` block to the tool YAML.

```
ComponentDependencyError: FunctionTool requires 'func' parameter
```
> Pass `func=` when constructing a `FunctionTool` from config.

```
FileNotFoundError: Agent config file not found: {yaml_path}
```
> Path resolution failed; check the path and the resolution order.

```
RuntimeError: FunctionTool {self.name} execution failed: {e}
```
> The wrapped function raised. Read the underlying error in the chain.

```
Failed to load MCP configuration: {e}
```
> MCP YAML missing or malformed. Verify the path and YAML syntax.

## Pitfalls

1. **Hardcoded prompt in Python source.** Move to YAML; the policy checker
   warns on long string literals assigned to `system_prompt`/`prompt`/`template`.
2. **Sync `def execute(...)` on a Tool.** `TypeError` at first call.
3. **Missing `tool_card:` in tool YAML.** Framework rejects.
4. **Direct construction of `Agent`/`Tool`/`ToolConfig`.** RuntimeError;
   use `from_config`.
5. **MCP "works" silently because aiohttp missing → mock.** Verify with
   `python -c "import aiohttp"` before claiming MCP integration is real.
6. **Async work inside `__init__`.** Use `_init_from_config` and lazy
   initialization (`ensure_initialized` for ExternalTool).
7. **Inline tool list in code.** Never; tools are config-listed.

## Checklist

- [ ] Agent YAML has `name:`, `model:`, and (if A2A) `agent_card:`.
- [ ] System prompt in YAML, not Python.
- [ ] Tool YAML has required `tool_card:`.
- [ ] Tool's `execute` is `async def`.
- [ ] No direct `Class(...)` for any agent or tool.
- [ ] If using MCP, `aiohttp` is installed and the server URL is real (not `mock://`).
- [ ] Smoke test loads the agent + at least one tool; integration test against
      real LLM call recorded.
