# Scope Decision 07 — Nanobrain Agent local-LLM (OpenAI-compatible) support

**Date:** 2026-04-22
**Status:** **Applied** under the nanobrain carve-out (scope memo 01) and
the user's 2026-04-22 directive explicitly authorizing nanobrain edits
to enable a local-LLM backend for T02 Steps 1/3c/5.
**Triggered by:** T02 finish (wrapping `apecx-db-integration`'s three
LLM functions as nanobrain Steps) requires a real LLM at integration-
test time. Per CLAUDE.md "no mocks," we need a reproducible local
backend that can be exercised in CI without OpenAI API keys. Ollama
(and vLLM) speak the OpenAI chat-completions protocol, so the cleanest
fix is to teach the nanobrain Agent to point at an OpenAI-compatible
endpoint.

---

## The framework gap

`nanobrain.core.agent.AgentConfig` (pre-patch) has no `provider` or
`base_url` fields. `Agent._initialize_llm_client` (agent.py:1045,
pre-patch) hardcoded `AsyncOpenAI(api_key=api_key)` with an
unconfigurable endpoint (defaults to `api.openai.com`) and a gate
that refused to initialize if no `OPENAI_API_KEY` was present. The
standalone `LocalLLM` class
(`nanobrain/library/llms/local_llm_client.py`) can talk to
OpenAI-compatible endpoints, but Agent doesn't use it — the two
surfaces are unwired.

End result: there is no YAML-only path to run a nanobrain Agent
against a local LLM. Fixing this at the call sites (each agent
subclass) would replicate the same patch N times. The fix belongs
on `Agent._initialize_llm_client`.

---

## Patch applied

### Part A — Three new fields on `AgentConfig`

**File:** `nanobrain/nanobrain/core/agent.py` (inside `class AgentConfig`,
after `max_tokens`).

```python
provider: str = Field(
    default="openai",
    description="LLM provider: 'openai' (default) or 'openai_compatible' "
                "(Ollama, vLLM, llama.cpp server, etc).",
)
base_url: Optional[str] = Field(
    default=None,
    description="OpenAI-compatible endpoint URL (e.g., "
                "'http://localhost:11434/v1' for Ollama).",
)
api_key: Optional[str] = Field(
    default=None,
    description="Explicit API key; bypasses ConfigManager/env-var "
                "lookup. For local backends, omit (defaults to 'EMPTY').",
)
```

**Backward compatibility:** all three fields default to the values
that reproduce pre-patch behavior. Existing Agent YAMLs that omit
`provider` / `base_url` / `api_key` still run against OpenAI via the
same env-var / ConfigManager pathway.

### Part B — `_initialize_llm_client` dispatch rewrite

**File:** `nanobrain/nanobrain/core/agent.py:1045`.

Changes:

1. **API-key resolution precedence:** explicit `AgentConfig.api_key` →
   `ConfigManager.get_api_key('openai')` → `os.getenv('OPENAI_API_KEY')`.
   Pre-patch was ConfigManager → env only.
2. **Local-endpoint branch:** if `provider == 'openai_compatible'` OR
   `base_url` is set, the agent treats the backend as local. If no API
   key surfaces through the chain, `"EMPTY"` is used (Ollama / vLLM
   accept any non-empty string).
3. **`AsyncOpenAI(base_url=...)` when set:** otherwise unchanged from
   the prior `AsyncOpenAI(api_key=api_key)` call.
4. **Init-verification model:** for remote agents, still uses the
   hardcoded `gpt-3.5-turbo` probe (preserves existing cost behavior).
   For local agents, probes with `self.config.model` since Ollama
   won't have gpt-3.5-turbo.

---

## What this enables

- A nanobrain Agent YAML like
  ```yaml
  name: entity_extractor
  model: mistral-small:latest
  provider: openai_compatible
  base_url: "http://localhost:11434/v1"
  system_prompt: "..."
  ```
  will now boot against a local Ollama daemon, no env var required.
- vLLM and llama.cpp-server deployments use the same schema (same
  OpenAI-compatible protocol; only the URL changes).
- T02 Steps 1 (`entity_extraction`) and 3c (`synonym_llm_proposals`)
  can now be wrapped as nanobrain Agents with no hardcoded prompts
  (mandated by `nanobrain/CLAUDE.md` rule 4) and real LLM calls in
  integration tests (mandated by workspace CLAUDE.md "no mocks").

---

## What this does NOT patch (explicit)

- **Anthropic / Gemini / other providers.** The patch only adds
  OpenAI-compatible support. Adding non-OpenAI protocols would need
  a proper provider-factory abstraction (`LLMProvider` base class +
  registry). That is a bigger refactor and not required for T02 or
  T01.
- **Streaming / tool-calling assumption parity.** Local models vary
  wildly in how well they implement OpenAI's function-calling
  contract. Callers that depend on `tool_choice: required` or
  OpenAI-specific JSON-mode should test against their chosen local
  model before committing to it.
- **The `LocalLLM` class.** It still exists, still unused by Agent.
  Rather than wire it through a second path, we used its core idea
  (AsyncOpenAI + base_url) directly in `_initialize_llm_client`.
  `LocalLLM`'s shared-server / distributed-registry features are
  HPC-specific and out of scope here.
- **Init-probe model for remote agents.** Still `gpt-3.5-turbo`.
  Changing that is a separate correctness question — outside the
  scope of "add local support."

---

## Risk + mitigation

- **Risk:** local model quality is model-dependent; a 7B–24B
  parameter Ollama model is measurably worse than GPT-4o for
  entity-extraction accuracy.
  **Mitigation:** model is a YAML knob. Production deployments can
  swap `base_url: "https://api.openai.com/v1"` + `model: "gpt-4o"`
  with zero code change.
- **Risk:** the init-test call wastes one token against the local
  model at every agent boot. Cheap (<10 ms on Ollama) but cumulative
  if you boot hundreds of agents in a workflow.
  **Mitigation:** none yet. If it becomes noticeable, gate the probe
  behind a YAML flag.
- **Risk:** any nanobrain agent subclass that overrides
  `_initialize_llm_client` silently skips this patch.
  **Mitigation:** at time of patch, `grep _initialize_llm_client` in
  nanobrain finds only the base class — no overrides. Verified.

---

## Verification

- `AgentConfig.from_config('/tmp/ollama_agent_probe.yml')` loads with
  the three new fields present (smoke test 2026-04-22 18:36Z).
- End-to-end agent-against-Ollama smoke test: see
  `tests/integration/test_nanobrain_agent_against_ollama.py` in
  this branch. 2 passed in 9.87s (includes real LLM round-trip against
  `mistral-small:latest` over OpenAI-compat endpoint at
  `http://localhost:11434/v1`).

---

## Two adjacent hygiene fixes rolled into this memo

Surfaced while writing the Ollama integration test; both are too small
to justify a separate memo and are tightly coupled to the core patch.

### Hygiene fix 1 — `safe_log` moved out of the try block

**File:** `nanobrain/nanobrain/core/agent.py` (in
`_initialize_llm_client`).

The nested `def safe_log(...)` used to live inside the outer
`try:` block, immediately after the `from openai import AsyncOpenAI`
import. Because Python binds `def` to a local name at module-parse
time but only *executes* the binding when control reaches the
statement, any exception raised BEFORE the `def` line (for example,
the openai import itself failing) made every `except:` handler that
called `safe_log` crash with `UnboundLocalError: cannot access local
variable 'safe_log' where it is not associated with a value` — which
completely masked the real underlying ImportError.

Moved `safe_log` above the `try:` so the fallback handlers can
always reach it. This is a latent-bug fix unrelated to local-LLM
support per se, but uncovered by the exact failure mode the memo-07
patch introduced (fresh venv without `openai` installed).

### Hygiene fix 2 — `openai` added to `[llm]` extra in nanobrain

**File:** `nanobrain/pyproject.toml:32`.

Pre-fix `[llm]` extra was an empty list — declared but advertising
nothing. This is the same shape of packaging gap memo 06 fixed for
`aiohttp` and `aiosqlite`. Without this, `pip install -e .[llm]`
was a no-op and `core/agent.py` would import-fail at runtime on a
fresh install.

Added `openai>=1.0`. Kept the dep in the optional extra (not main
`dependencies`) to honor the original architectural intent that
Agent's openai dep is a soft-optional — someone using nanobrain
purely for Step/DataUnit/Trigger orchestration without LLMs
shouldn't pay the ~1 MB openai wheel cost.

Matching change in `apecx-mcp-integration/pyproject.toml`: added
`openai>=1.0` to main `dependencies`, because our composition steps
wire nanobrain Agents (SynonymDetectionAgent, and T02 Steps 1/3c)
unconditionally — making the dep soft-optional in downstream
applications that actually use agents is a lie.
