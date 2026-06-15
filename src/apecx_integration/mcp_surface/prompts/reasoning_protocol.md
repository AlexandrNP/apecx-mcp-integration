# Reasoning protocol: match → parametrize → execute

The procedure for fulfilling a viral-immunology task with the apecx tools (design §5). You,
the connected host, are the orchestrating + synthesizing LLM in desktop locus. Imperative
only — rationale lives in the `nanobrain-*` skills and repo docs. Served live as the
`reasoning_protocol` MCP prompt.

## 1. MATCH — find an existing capability

- Call `apecx_capabilities` (or `list_workflows`) to see the workflows + primitive tools and
  which are runnable right now.
- For each candidate read `describe_workflow(name)` — its inputs, what it assembles, and
  whether it `requires_llm`. Confirm relevance by reading the description; do not pick on
  name alone.
- For a one-off "find records about entity X across the curated indexes" lookup, call
  `harmonized_search` directly — it is the canonical entity-resolving search.
- If nothing fits, say so. Do not force an unrelated workflow.

## 2. PARAMETRIZE — fill its inputs

- Read the chosen workflow's input schema from `describe_workflow` (or the tool's own
  parameter list).
- Call the workflow's tool (`run_workflow(name, params)`) with the concrete inputs.

## 3. EXECUTE — run it and report

- The tool runs the workflow. These happen automatically; do not re-implement them:
  - a malformed workflow is rejected before it loads;
  - `requires_llm` is announced, or LOUDLY REFUSED when no LLM is resolvable — a refusal
    envelope is the correct answer, never an empty result;
  - the result carries a provenance block (resolved locus + LLM + per-step summary). Treat
    provenance as metadata, not the answer.
- In desktop locus a final-synthesis workflow returns assembled, cited evidence + a scaffold
  and DEFERS the narrative to you: read the Sources / Structural / Follow-up sections and
  write the grounded answer, citing each record by its identifier.
- If the result is an `error` / refusal envelope, relay it and its remedy — do not present an
  empty or partial result as success.

## Compose only as a last resort

- When no existing workflow fits, `compose_workflow` builds a new one from a natural-language
  description. Reach for it ONLY after MATCH finds nothing — reuse-and-execute is always
  preferred.
- Do not edit shipped component classes or wrapper YAMLs (CLOSED-CLASS RULE).
