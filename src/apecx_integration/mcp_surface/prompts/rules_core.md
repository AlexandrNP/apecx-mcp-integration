# Reasoning rules (lean, imperative)

Single canonical rules source for the desktop reasoning host (design §11, D7). The nine
`nanobrain-*` skills carry the rationale + detailed scaffolding; this file carries only the
imperatives. Served live as the `reasoning_rules` MCP prompt.

## REUSE-FIRST RULE

- Before composing anything new, call `apecx_capabilities` (or `list_workflows`) and read
  the candidates with `describe_workflow(name)`. Reuse an existing workflow when one fits.
- Prefer a runnable catalog workflow (`available: true`) over one with unmet prerequisites —
  it runs now without composition.
- Compose new only when no existing capability fits — and then via `compose_workflow`, never
  by hand-authoring outside the composer.

## CLOSED-CLASS RULE

- Library component classes and their wrapper YAMLs are CLOSED. Reference them; do not edit,
  rename, or shadow them.
- When an existing component is almost right, author a NEW step class in a `novel_python`
  fence with a NEW class name — never tweak a shared class.

## Framework rules (nanobrain-native, non-negotiable)

- Construct every component via `from_config` only — direct constructors raise.
- Implement `process()`; never override `execute()` — the framework FAIL-FASTs.
- Every `DirectLink` must declare `auto_transfer: true` — the dominant silent failure is a
  link that loads but never transfers.
- No hardcoded prompts — use `system_prompt` in agent YAML or a `prompt_template_file`.
- A step's returned dict keys must match its declared `output_data_units` names, or the
  output is silently dropped.

## requires_llm

- A workflow that needs an LLM announces the resolved model before running, and is LOUDLY
  REFUSED (not run to an empty result) when none is resolvable.
- In desktop locus a final-synthesis workflow defers synthesis to YOU (the host): it returns
  assembled, cited evidence + a scaffold, and you write the answer — no apecx LLM is needed.
  Only an in-DAG LLM step requires a configured `APECX_LLM_BASE_URL` / `APECX_LLM_MODEL` (or a
  local Ollama).
