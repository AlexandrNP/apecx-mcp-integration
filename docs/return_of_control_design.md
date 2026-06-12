# Return-of-Control — the user-facing-LLM ↔ internal-workflow boundary

**Status:** Design (2026-06-12). Refines `external_orchestration_design.md` §1/§2/§6 after a
direction note: *"the decomposer should have a slightly different role… return of control is the
first question that needs answering."* This doc answers it.

---

## 1. The separation (why two LLMs, never blurred)

| | **User-facing LLM** (frontier; Claude/GPT, over MCP) | **Internal execution** (nanobrain workflows + a bounded local LLM) |
|---|---|---|
| Owns | orchestration *decisions*: decomposition strategy, **parameter values**, ambiguity resolution, final synthesis | deterministic *execution*: given complete, unambiguous input, run a rule-based scaffold to a result |
| Has | the user's conversation context; frontier reasoning | a versioned, reproducible pipeline; at most bounded, capped discretion |
| Must not | reach inside a workflow to micro-manage steps | invent parameter values, resolve ambiguity, or synthesize the final answer |

The frontier LLM is the **primary orchestrator** (it is more capable at decomposition + synthesis,
and it alone has the user's intent). The internal side is **deterministic by default**; a local
LLM appears only inside a workflow for narrow, bounded fallback — and it **never owns
orchestration**.

## 2. Return of control — the first question, answered

**Control returns to the user-facing LLM whenever progress requires a decision the internal system
should not make.** The internal side never *guesses* across that boundary; it **transfers control
back with a structured statement of what it needs and how to get it.** Three terminal states for
any surface call:

- **`ok` / `partial`** — a deterministic result. Control returns with the answer (the `partial`
  flag is honest about a degraded branch).
- **`needs_input`** — the deterministic path cannot proceed without something only the frontier LLM
  (or, through it, the user) can supply. NOT an error, NOT a guess — a **control-transfer envelope**
  describing the gap.
- **`error`** — a genuine failure (loud).

This **generalizes the disambiguation HITL envelope** already shipped (`paused_awaiting_disambiguation`
+ `next_action`) into one uniform control-transfer contract. Disambiguation becomes one *reason*
among several:

```
WorkflowResult.status ∈ { ok, partial, error, needs_input }
when needs_input:
  control_transfer:
    reason: missing_param | ambiguous_entity | needs_prerequisite | decomposition_choice
    next_action: { kind, param_name?, options?, schema?, obtain_via? }
    message: <guidance the frontier LLM acts on, then re-calls>
```

**This dissolves the "free-text → parameters" gap by design.** We do NOT build an internal
parameter-extractor (the weak local LLM guessing `taxon_id`). When `run_workflow` is called with a
missing/ill-typed required param, it returns `needs_input(missing_param)` naming the param, its
schema, and how to obtain it (e.g. *"resolve the virus name to an NCBI taxon_id via
harmonized_search, then re-call"*). The frontier LLM — which has the context and already knows how
to call `harmonized_search` — fills it and re-invokes. Control crossed the boundary explicitly.

## 3. The decomposer's clarified (narrower) role

Previously built as an autonomous solver (match → dispatch → recurse → synthesize via the internal
LLM). That over-trusts the local LLM and **keeps control inside** when it should hand back. New
role — a **bounded planner + deterministic executor**, never a free orchestrator:

1. Task maps to **one** workflow with **complete** params → execute it (pure determinism).
2. Params missing/ambiguous → **`needs_input`** (control returns; frontier LLM supplies them).
3. Task is compound → propose a decomposition **plan** and return it as
   **`needs_input(decomposition_choice)`** for the frontier LLM to approve/sequence — it does
   *not* auto-execute a multi-workflow chain on the local LLM's say-so. (A future, explicitly
   opted-in "autonomous fallback" may execute a chain, but only under hard depth/cost caps and with
   a loud `error`("cannot solve") on any dead-end — never a fabricated answer.)

The local LLM's job shrinks to *proposing structure*, never *choosing values* or *writing the final
answer* — both of those return control to the frontier LLM.

## 4. Reprioritized roadmap (supersedes the prior "remaining items")

1. **RoC-1 — control-transfer envelope.** Add `needs_input` status + a typed `control_transfer`
   to `WorkflowResult` (loud-invariant: `needs_input` requires a non-empty `control_transfer`).
   Re-express the disambiguation envelope as `reason: ambiguous_entity`.
2. **RoC-2 — `run_workflow` returns `needs_input` on missing/ill-typed required params** (validated
   against the catalog `input_schema`; `obtain_via` hints per param). This is the param-gap fix.
3. **RoC-3 — reframe the decomposer** per §3 (return a plan as `needs_input(decomposition_choice)`;
   execute only the single-match-complete-params case).
4. **EO-54 — Rhea Tier-1 aligner substitution, now UNBLOCKED.** Rhea ships in `../rhea/`
   (`docker compose -f deploy/docker-compose.yaml up -d` → MCP server at `:3001`). Stand it up,
   point `RHEA_MCP_URL` at it, register `rhea_muscle_alignment` as the heavy alignment path
   alongside local MAFFT, and add interface-tag (MUSCLE↔MAFFT) substitution.
5. **Cleanup — retire `epitope_analysis` (one fake step) + the empty `viral_immunology_analysis`
   stub**, and drop their `composer_config.yml` references. Reuse audit verdict: no salvageable
   code (the real version is `viral_conserved_sites`); keep the concept, delete the husks.

Deprioritized per direction: branch reconciliation (single vision; build on this line).

## 5. Implementation challenges (honest)

- **C1 — WorkflowResult contract churn.** `needs_input` + `control_transfer` touch every producer
  (run_workflow, EnvelopeStep, dispatcher) and the `_check_consistency` validator. Mitigation:
  additive status + a nested model; the disambiguation envelope maps onto it 1:1 (low risk, but a
  pin-test update).
- **C2 — required-param detection from JSON-Schema.** Need a small, correct "which `required`
  fields are missing or ill-typed" check over the catalog `input_schema`. Edge: nested objects,
  type coercion ("37124" vs 37124). Keep it shallow + explicit; FAIL-LOUD on ambiguity.
- **C3 — `obtain_via` convention.** Encoding *"resolve virus→taxon_id via harmonized_search"* needs
  a per-param annotation (a custom `obtain_via` key in the schema) — a new, documented convention.
  Risk: it's advisory text the frontier LLM must honor; keep it precise.
- **C4 — decomposer behavior change breaks its current tests** (they assume auto-execution). Need
  to redefine the contract + re-test, and decide the execute-vs-return-plan policy precisely so it
  isn't a silent half-measure.
- **C5 — Rhea stack is operationally heavy.** `docker compose up` brings up Galaxy + embedding +
  redis + per-tool conda envs; first MUSCLE install is slow; GPU is optional but the embedding
  model wants it. Needs Docker running; first-run latency; the §8 interface tags only exist on
  Rhea's rich in-process metadata path (design R2). Real, but not blocking — it's setup, not a wall.
- **C6 — two-LLM testing.** Verifying RoC end-to-end means simulating the frontier LLM's
  re-call-with-filled-params loop in tests (the external LLM isn't present in CI). Mitigation:
  deterministic test driver that consumes `needs_input`, fills from a fixture, re-calls.
- **C7 — consistency with the shipped disambiguation HITL.** The viral/harmonized disambiguation
  envelope already exists (on the other line). Re-expressing it as `needs_input(ambiguous_entity)`
  here is clean, but if the two lines ever converge the shapes must match — design the contract now
  so they do.
