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

## 3. The decomposer — TWO flag-switched modes (not one)

The decomposer has **two distinct modes of operation, selected by a flag in MCP settings**
(env var `APECX_EO_DECOMPOSER_MODE`, read at the boundary — same pattern as the server's existing
`APECX_MCP_AUTOSTART_*` flags and the reasoning-agent locus flag). Both modes honor return-of-control
(neither guesses parameter values across the boundary); they differ in **where control returns**:

- **`auto_solver`** (independent auto-solver). Bounded autonomous solving: match → dispatch →
  recurse → integrate, under hard depth/cost caps, with a loud `error`("cannot solve") on any
  dead-end (never a fabricated answer). Returns the final result when it can solve deterministically;
  returns **`needs_input`** the moment it hits a genuine gap (missing/ambiguous param it cannot
  obtain deterministically). This is the engine already built (`LocalDecomposer`).
- **`plan_returner`** (return of control to the frontier LLM). Does NOT execute multi-workflow
  chains. Matches + proposes a decomposition and returns it as **`needs_input(decomposition_choice)`**
  (which workflows, what each needs) for the frontier LLM to approve, fill, and sequence via
  `run_workflow`. Control returns at the planning stage.

In BOTH modes the local LLM's job shrinks to *proposing structure*, never *choosing parameter
values* or *writing the final answer* — those always return to the frontier LLM. The flag lets a
deployment choose autonomy (auto_solver) vs. frontier-LLM-driven orchestration (plan_returner).

## 4. Reprioritized roadmap (supersedes the prior "remaining items")

1. **RoC-1 — control-transfer envelope.** Add `needs_input` status + a typed `control_transfer`
   to `WorkflowResult` (loud-invariant: `needs_input` requires a non-empty `control_transfer`).
   Re-express the disambiguation envelope as `reason: ambiguous_entity`.
2. **RoC-2 — `run_workflow` returns `needs_input` on missing/ill-typed required params.** The input
   contract is the **nanobrain workflow's**, not the catalog's: declare `step_input_schema` (G6
   `SchemaRef` on `StepConfig`, runtime FAIL-FAST-enforced) on each workflow's FIRST step; derive the
   required params from it at run time. The catalog `input_schema` becomes a derived display hint
   (or is dropped), removing catalog↔workflow drift. `obtain_via` hints (per param) tell the frontier
   LLM how to get a value (e.g. *resolve virus→taxon_id via harmonized_search*). This is the param-gap fix.
3. **RoC-3 — two flag-switched decomposer modes** per §3: keep `auto_solver` (the built engine) AND
   add `plan_returner` (returns `needs_input(decomposition_choice)`), selected by
   `APECX_EO_DECOMPOSER_MODE`. Neither guesses parameter values.
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
- **C2 — required-param detection derived from the workflow's `step_input_schema` (G6).** Two
  sub-parts: (a) author `step_input_schema` (`SchemaRef`) on each workflow's first step — and have
  the lightweight `WorkflowBuilder` pass it through `add_step` (verify it survives `load()`); (b)
  resolve the `SchemaRef` → JSON Schema and read `required`/types to detect missing/ill-typed params.
  Edges: `SchemaRef` may be inline-dict OR a `$ref` to a file (must resolve both); type coercion
  ("37124" vs 37124). Bonus: G6 already FAIL-FASTs at run time on schema mismatch — so declaring it
  also hardens the step. Keep the missing-required check shallow + explicit.
- **C3 — `obtain_via` convention.** Encoding *"resolve virus→taxon_id via harmonized_search"* needs
  a per-param annotation (a custom `obtain_via` key in the step_input_schema) — a new, documented
  convention. Risk: it's advisory text the frontier LLM must honor; keep it precise.
- **C4 — two decomposer modes + the flag, not a behavior swap.** `auto_solver` already exists and is
  tested; add `plan_returner` + the `APECX_EO_DECOMPOSER_MODE` switch WITHOUT regressing auto_solver.
  Risk: a half-defined `plan_returner` (what exactly is "a plan"? the proposed (workflow, params-needed)
  list) — specify its shape as a `decomposition_choice` control-transfer precisely, and test both
  modes. The flag must default safely (recommend `plan_returner` — return-of-control is the safer
  default; auto_solver is opt-in autonomy).
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
