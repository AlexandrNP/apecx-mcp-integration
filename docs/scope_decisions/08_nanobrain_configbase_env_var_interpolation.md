# Scope Decision 08 — Nanobrain ConfigBase env-var interpolation

**Date:** 2026-04-22
**Status:** **Applied** under the nanobrain carve-out (scope memo 01)
and the user's 2026-04-22 directive: *"Nanobrain should have its own
getenv/read YAML for API keys."*
**Triggered by:** follow-up on memo 07. With the new `AgentConfig.api_key`
field in place, the next step was to let YAML authors write
`api_key: "${OPENAI_API_KEY}"` without hardcoding secrets. An
exploration of the nanobrain config pipeline found that env-var
interpolation at the YAML-load level is **partially broken and
silently so.**

---

## The framework gap (before this patch)

Two distinct config loaders live in nanobrain and handle env vars
differently:

| Loader | Used by | Env-var interpolation |
|---|---|---|
| `ConfigBase._load_yaml_file` | `from_config()` everywhere | **None.** `yaml.safe_load` then direct return. `${VAR}` ends up as the literal seven-character string. |
| `ConfigManager.load_config` | Global API-key config only | **Partial.** Regex `\$\{([^}]+)\}`, `os.getenv(name, '')`. Missing env var → silent empty string. `${VAR:-default}` syntax does *not* work (captures `VAR:-default` as a literal var name). |

Net effect before the patch:

- An Agent YAML with `api_key: "${OPENAI_API_KEY}"` loaded via
  `Agent.from_config(...)` stored the literal string `"${OPENAI_API_KEY}"`
  as the api_key. Runtime error — but not at load time.
- A ConfigManager-loaded global config with `${OPENAI_API_KEY}` quietly
  became `""` when the env var was unset. Runtime auth-failure that
  looks like a network issue.

Neither failure mode surfaces at the point that would let an operator
correct it. CLAUDE.md advertises env-var interpolation as a
first-class feature — this memo makes the docs accurate.

---

## Patch applied

**File:** `nanobrain/nanobrain/core/config/config_base.py`

### Part A — Module-level interpolation helper

Added near the top of the file:

```python
_ENV_VAR_PATTERN = re.compile(
    r'(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}'
)

def _interpolate_env_vars(value):
    """Recursively substitute ${VAR} / ${VAR:-default} in all string
    leaves of a loaded YAML structure. Fail-loud on missing var + no
    default. Escape as $${VAR} for a literal."""
    ...
```

Grammar:

- **`${VAR}`** — required. If `VAR` is unset, raises `ValueError` at
  load time.
- **`${VAR:-default}`** — optional. If `VAR` is unset or empty, uses
  `default`. POSIX `:-` semantics (matches docker-compose, GitHub
  Actions, shell).
- **`$${VAR}`** — escape. Lookbehind prevents the match; after
  substitution, the leading `$$` is collapsed to `$`. Needed for
  system_prompts that legitimately contain `${…}` text (LaTeX,
  shell examples in docs).
- **`${VAR-default}`** (plain dash) — not supported. Deliberately
  narrow grammar; if we added it, users would need to know POSIX's
  unset-vs-empty distinction to avoid confusion.

Recursion: strings get substituted, dicts descend, lists descend,
other leaf types pass through untouched.

### Part B — Call site in `_load_yaml_file`

Replaced the trailing `return config_data` with
`return _interpolate_env_vars(config_data)`. One-line change at the
surface, all downstream consumers (nested-object resolution, Pydantic
validation) see interpolated values transparently.

---

## What this enables

A canonical LLM config YAML can now look like:

```yaml
name: extraction_agent
provider: openai_compatible
base_url: "${APECX_LLM_BASE_URL:-http://localhost:11434/v1}"
model: "${APECX_LLM_MODEL:-mistral-small:latest}"
api_key: "${APECX_LLM_API_KEY:-EMPTY}"
temperature: 0.0
max_tokens: 256
```

One YAML file, three env-var knobs:

- Operator running locally with Ollama: no env vars, everything
  defaults. `EMPTY` as api_key — Ollama doesn't validate.
- Operator running against vLLM on port 8000: set
  `APECX_LLM_BASE_URL=http://localhost:8000/v1`. Same YAML.
- Operator running against api.openai.com: set
  `APECX_LLM_BASE_URL=https://api.openai.com/v1`,
  `APECX_LLM_MODEL=gpt-4o-mini`,
  `APECX_LLM_API_KEY=sk-...`. Same YAML.

No YAML fork, no hardcoded secrets, no per-deployment edits.

---

## What this does NOT patch (explicit)

- **`ConfigManager._substitute_env_variables`.** Still uses the
  partial regex and silent-empty-string substitution. Leaving it
  alone intentionally: changing it could break existing downstream
  consumers that rely on the quiet behavior. When those consumers
  migrate to `ConfigBase.from_config`, they pick up the new
  semantics automatically. Not worth the blast-radius to touch.
- **Escaping for literal `$`** (not followed by `{`). Bare `$`
  characters pass through unchanged — only `${...}` sequences are
  inspected. LaTeX math expressions like `$\alpha$` are safe.
- **Type coercion.** An env var that expands to the string `"60"`
  stays a string. If a YAML field typed `int` expects an int,
  Pydantic's strict/non-strict coercion rules apply downstream —
  we don't try to re-parse numerics here.

---

## Risk + mitigation

- **Risk:** existing configs that happened to contain `${...}` in
  system prompts or other strings will now either interpolate or
  raise. Breaking change for any codebase that had `${foo}` as
  literal text and unset `foo` env var.
  **Mitigation:** users escape with `$${foo}`; error message is
  explicit about the remediation; the breakage is loud at load
  time, not silent at runtime.
- **Risk:** the negative-lookbehind escape (`$$`) is clever and
  surprising to people who haven't seen it before.
  **Mitigation:** documented in this memo, in the docstring, in
  the test file.
- **Risk:** fail-loud on missing `${VAR}` will regress any existing
  YAML in the nanobrain repo that expected silent empty-string.
  **Mitigation:** full apecx-mcp-integration test suite (222+16
  tests) run clean. If downstream nanobrain consumers surface real
  breakage, they migrate to `${VAR:-}` for the old behavior.

---

## Verification

- 16 unit tests covering every grammar corner:
  `tests/unit/test_nanobrain_configbase_env_vars.py`. 16 pass in 0.19s.
- Full apecx-mcp-integration suite (unit + integration) run after
  the patch — no regressions (see commit body).
- Integration-test parity: `test_nanobrain_agent_against_ollama.py`
  will be updated in the next commit to use `${APECX_LLM_*}` env
  vars, proving the patch reaches the Agent YAML loading path.
