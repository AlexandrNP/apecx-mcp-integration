# Web-search-backed workflow rules

Imperatives for composing nanobrain workflows that inject web-search
context into a drafter. Pairs with `nanobrain_rules.md` and
`post_f17_components_rules.md`.

## When to use a web-search context node

Use a `WebSearchContextStep` when the task may benefit from looking
something up — an unfamiliar library API, a method name, a parameter.
Do NOT add it when the task is pure computation (the answer is an
algorithm, not a lookup) — web search adds latency and prompt noise
with no upside there.

## The two components

| Component | Path | Role |
|---|---|---|
| `WebSearchTool` | `nanobrain.library.tools.web_search.WebSearchTool` | The generic `ToolBase`. Pluggable backend (default keyless DuckDuckGo). Configure via `parameters` in the tool YAML. |
| `WebSearchContextStep` | `apecx_integration.composition.steps.web_search_context_step.WebSearchContextStep` | The workflow node. Wraps `WebSearchTool`; sits between the router and the drafter; enriches `code_spec` with retrieved context. Mirrors the `memory_reader` node. |

## REQUIREMENTS

1. **The step references the tool by config path.** `WebSearchContextStep`'s
   `web_search_tool_config` is a path to a `WebSearchTool` YAML. Put
   the tool YAML as a sibling of the step YAML in the workflow's
   `steps/` dir — the path resolves relative to the step config's
   directory first.

2. **The tool YAML uses `parameters` for tool-specific config.**
   ```yaml
   name: web_search
   tool_type: external
   parameters:
     backend: duckduckgo        # duckduckgo (keyless) | tavily ($TAVILY_API_KEY)
     max_results: 5
     cache_dir: .web_search_cache   # relative -> resolved against workspace root
   tool_card:
     capabilities: ["web_search"]
   ```

3. **ALWAYS set `cache_dir`** for benchmark / ablation workflows. The
   query-hash on-disk cache makes re-runs reproducible (the cache, not
   the drifting live web, answers) and dodges DDG rate limits — each
   distinct query hits the network exactly once.

4. **The node sits router → web_search_context → drafter.** It enriches
   `code_spec` (the field the drafter consumes) and passes through
   `task_category`, `entry_point`, `test_hint`, `function_signature`.
   It composes with `memory_reader`: router → memory_reader →
   web_search_context → drafter (the drafter sees both enrichments).

5. **Every `DirectLink` declares `auto_transfer: true`.** Standard
   nanobrain rule — without it the link silently no-ops.

## Topology template

    workflow_input ({code_spec, entry_point, ...})
        -> task_router (TaskCategoryRouterStep)
        -> web_search_context (WebSearchContextStep)
        -> drafter (BenchmarkDrafterStep)
        -> workflow_output

Reference: `composition/workflows/benchmark_ablation_websearch_only/`
(F17 + websearch) and `composition/workflows/benchmark_max_power_websearch/`
(max_power + websearch). A lightweight `WorkflowBuilder` variant lives
at `benchmark_max_power_websearch_lightweight_builder.py`.

## Non-determinism — declare it honestly

`WebSearchContextStep` is a **non-deterministic node**: live web
results drift over time. It is NOT under the framework's
deterministic-step contract. The tool's `cache_dir` makes a *given
populated cache* reproducible, but the first population is live. Any
workflow containing this node is a non-deterministic workflow — say
so in the workflow's header comment.

## Pin: forbidden patterns

- DO NOT add a web-search node to a pure-computation pipeline expecting
  an accuracy lift — it is null-to-negative there. Web search helps
  with *lookups*, not *algorithms*.
- DO NOT make `WebSearchContextStep` swallow a backend failure into a
  silent "no context" degrade. A backend error (rate limit, network
  down) is a genuine fault — the step FAILS LOUD so the benchmark
  runner records it visibly. (A search that *succeeds but finds
  nothing* is the one honest non-failure: `websearch_hit=False`,
  `code_spec` passes through unenriched.)
- DO NOT hardcode an API key in the tool YAML — keyless DuckDuckGo is
  the default; API-key backends read `$TAVILY_API_KEY` from the env.
- DO NOT omit `cache_dir` in a benchmark workflow — an uncached sweep
  is non-reproducible and rate-limit-prone.
