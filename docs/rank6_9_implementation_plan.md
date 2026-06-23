# Ranks 6–9 — detailed implementation plan (Phase 1b): autonomy loop · provenance ledger · runtime isolation · eval harness

> **STATUS: FUTURE WORK — detailed plan, not executed.** Phase-1b expansion of Ranks 6–9 (the final chunk) from `workflow_crafting_intelligence_roadmap.md`, same standards as the Rank-1..5 plans. No code changed.
>
> **Resolution honesty — strongest caveat of the set.** R6 (the autonomy loop) and R9 (the biology eval set + harness) are **mostly new code/data** that assembles R1–R5; they attach to few existing anchors, so most workstreams here are **[NEW — interface/contract only; no line refs until it exists]**. R7/R8 attach more to existing code (`inspect_run`/G4, `docker_sandbox.py`). Fabricating line numbers for the autonomy loop would be pure speculation — refused.
>
> **Blocked-by:** R6 needs R1–R5 (it orchestrates them). R9 is cross-cutting but gates R5.WS5b and R6's ship-gate. R8 becomes urgent only once R6 removes the human from the inner loop. See §5.

---

## RANK 6 — Bounded autonomous decomposition + execution loop

**Goal (roadmap R6):** the visible "autonomy" surface — decompose a question → retrieve/reuse (R1) → autogen tools on demand (R3, §0) → validate (R2) → run → ground (R4) → synthesize with citations. **Ships only when R1, R2-layers-1–3, and R4 work on the R9 eval set** (panel gate). AFLOW/ADAS meta-search is **explicitly deferred** (no per-candidate reward until R6+R9 exist).

### WS6a — Bounded local-LLM decomposition fallback  ·  *L*
**Necessity:** the desktop/frontier LLM is the primary orchestrator (two-modes design); the backend needs a bounded fallback that fails loud rather than looping unboundedly.
**Attach point (EXISTS):** `docs/external_orchestration_design.md` already specifies this (match-first → decompose → dispatch sub-workflows, LoopController depth caps + cost envelope, fail-loud-if-not-decomposable); `LoopController` exists in nanobrain (`library/steps/loop_controller.py`); `run_workflow` (`eo_primitives.py:271`) is the entry.
**Edit [NEW — interface only]:** `decompose_bounded(question, *, max_depth, cost_envelope) -> list[SubTask] | FailLoud`. Reuses `LoopController` for the depth/repeat cap and the existing cost-envelope primitive (G26, currently unused by the composer). FAIL-LOUD (not silent partial) when the question isn't decomposable within bounds.
**Real-data AC (Ollama-gated):** a real multi-part biology question decomposes into ≥2 sub-tasks each mapping to a real catalog capability; a deliberately ill-posed question hits the bound and FAILs LOUD with the reason, never returns a half-plan. Real questions, real backend.
**Tests:** `test_bounded_decompose_real_question` (Ollama-gated) · `test_undecomposable_fails_loud_at_bound` (deterministic bound check). No synthetic tasks — real questions.
**Review gate:** self-review (bounded + fail-loud; reuse LoopController/cost-envelope, no new loop primitive); review-gate agent (adversarial: unbounded recursion? silent partial plan?); `Reviewed:` trailer.

### WS6b — The outer compose→run→ground loop (assembles R1–R4)  ·  *L*
**Necessity:** this is the orchestration that turns the pieces into a question-answering agent.
**Attach point [NEW — interface only]:** `answer_question(question) -> WorkflowResult` wiring: `decompose_bounded` (WS6a) → R1 retrieval/name-binding → R3 on-demand synth if a capability is missing → R2 validate → `Workflow.run` → R4 ground+cite → emit `WorkflowResult`. Pure assembly of R1–R4 contracts; no new analysis logic.
**Real-data AC (Ollama-gated, end-to-end):** a real biology question from the R9 eval set yields a `WorkflowResult` whose markdown cites ≥1 real retrieved record per required source class (R4 existence gate) and whose terminal output VALUE is non-empty (G127, decide on value not status). Real e2e on the real stack.
**Tests:** `test_answer_question_e2e_on_eval_item` (Ollama-gated, one real eval item).
**Ship-gate (explicit, non-negotiable):** do NOT enable WS6b until R1 + R2-layers-1–3 + R4 pass on the R9 set. Enabling earlier = automating ungrounded composition.
**Review gate:** self-review (assembly only; the ship-gate is honored); review-gate agent (adversarial: does it run unvalidated/ungrounded if a sub-stage is absent? must refuse); `Reviewed:` trailer.

### WS6c — Evaluator-optimizer execution feedback  ·  *M*
**Necessity:** "did it run + answer?" must feed back to refine the composition (Anthropic evaluator-optimizer; Reflexion).
**Attach point (EXISTS):** G37 `step_failed`/step events (`subscribe_to_step_events`) + G127 value-not-status; the existing review-revise topology (R2.WS2b).
**Edit [partly NEW]:** on a failed/empty run, capture the `step_failed` event + the empty terminal value, feed them as structured feedback into one bounded re-composition pass (reuse the C1 retry shape), then stop (bounded — no infinite refine).
**Real-data AC:** a real question whose first composition produces an empty terminal value triggers exactly one bounded refine that captures the real `step_failed` reason; a successful first run does not refine. Real runs.
**Tests:** `test_empty_result_triggers_one_bounded_refine` (Ollama-gated) · `test_success_does_not_refine`.
**Review gate:** self-review (bounded — one refine, not a loop); review-gate agent (adversarial: refine loop without a cap?); `Reviewed:` trailer.

**DEFERRED (explicit):** AFLOW/ADAS-style MCTS meta-search over workflow topologies — needs a reliable per-candidate reward (R6+R9). Not in this plan; revisit only after R9 produces stable scores.

---

## RANK 7 — Provenance, reproducibility & "original-composition" ledger

**Goal (roadmap R7):** every run is replayable + defensible; "original" structures are tracked and human-reviewed before reuse; the reproducibility-vs-defensibility split is stated honestly.

### WS7a — Replayable run record  ·  *S–M*
**Attach point (EXISTS):** G4 provenance + `inspect_run` + the `WorkflowResult` envelope (markdown + data_handle + provenance) already capture much of this.
**Edit [partly NEW]:** extend the run record to also carry resolved entities (from R4.WS4a), tool versions + container digests (from R3 `descriptor_id`/UTD), the reasoning pattern used (R5), validator verdicts (R2), cited sources (R4), and a **full model-call manifest** (model id, params, temperature per call).
**Real-data AC:** after a real run, `inspect_run(run_id)` returns a record containing the real resolved taxon IRI, the real tool version/digest, the pattern name, the validator verdicts, and the real cited record IDs — every field populated from the real run, none placeholder.
**Tests:** `test_run_record_carries_real_provenance_fields` (real run → assert each field non-placeholder).
**Review gate:** self-review (extend the existing record, don't fork a parallel one); review-gate agent (adversarial: any field silently defaulted/faked?); `Reviewed:` trailer.

### WS7b — Original-composition ledger with staged promotion  ·  *M*
**Attach point (EXISTS):** `CompositionSummary` (provenance classes from R5.WS5c: human-authored / reused-validated / machine-original).
**Edit [NEW — interface only]:** a `composition_ledger` recording each composed structure's provenance class + date; promotion `machine-original → reusable-validated` requires R2 deterministic gates **plus** a human sign-off **at promotion time only** (not per run). Novelty runs (labelled), but isn't reused-as-proven until promoted.
**Real-data AC:** a real machine-original composition runs (labelled) but is refused reuse-as-validated until it passes R2 gates + a recorded human sign-off; a reused-validated one is reusable immediately. Real composer outputs.
**Tests:** `test_machine_original_runs_but_not_reusable_until_promoted` · `test_promotion_requires_deterministic_gate_plus_signoff`.
**Review gate:** self-review (novelty ≠ blocked; promotion is the only gated transition); review-gate agent; `Reviewed:` trailer.

### WS7c — Reproducibility/defensibility split + determinism in the gates  ·  *S*
**Edit [partly NEW]:** set temperature 0 on validation/decomposition/citation steps (the config supports per-role bindings, `composer_config.yml model_roles`); document that deterministic/containerised legs (data fetch, rhea tools) are reproducible while the LLM synthesis leg is defensible+traceable, not bit-reproducible.
**Real-data AC:** two real runs of the same data-fetch/rhea leg on the same input produce identical outputs (reproducible); the synthesis leg's run record makes the chain inspectable even when text varies. Real runs.
**Tests:** `test_deterministic_legs_reproduce` (real rhea/data leg twice) · `test_gate_steps_are_temperature_zero` (assert the resolved config).
**Review gate:** self-review (the doc/UX states the split honestly, no "fully reproducible" over-claim); review-gate agent; `Reviewed:` trailer.

---

## RANK 8 — Runtime isolation for novel/auto-generated code

**Goal (roadmap R8):** novel steps + freshly-minted rhea tools execute isolated; acceptance ties to the R2 behavioural gate. Urgent once R6 removes the human from the inner loop.

### WS8a — Finish the on-demand container sandbox  ·  *M*
**Attach point (EXISTS):** `composition/docker_sandbox.py` scaffold — `build_docker_sandbox_command(...)` already pins hardening flags (`--network=none`, `--read-only`, `--cap-drop=ALL`, mem/cpu/pids caps, ro bind); `DockerSandboxRunner.run(...)` refuses unless `APECX_T13B_SANDBOX_EXECUTE=1`.
**Edit [partly NEW]:** wire the sandbox into the R2.WS2b execution gate + the R3.WS3c validate-before-register path so novel steps + minted tools run isolated; invoked **on demand per §0** (per step under test), not pre-provisioned.
**Real-data AC (Docker-gated):** a real novel step's `process` runs inside the sandbox with the pinned flags (assert the actual `docker run` argv via the existing command builder) and acceptance follows the R2 verdict; a step that would touch the network fails closed (`--network=none`). Real docker, real step.
**Tests:** `test_novel_step_runs_in_sandbox` (Docker-gated) · `test_sandbox_command_pins_hardening_flags` (deterministic — reuses the existing `test_docker_sandbox_command.py` pins).
**Review gate:** self-review (reuse the existing command builder + its pins; on-demand not pre-provisioned); review-gate agent (adversarial: any escape — writable mount, network leak?); `Reviewed:` trailer.

---

## RANK 9 — Evaluation harness for composition quality (biology-grounded)

**Goal (roadmap R9):** a biology-question → expected-evidence eval set so "did we answer correctly?" is measurable. The kill-criterion measurement; without it the autonomy thesis is unfalsifiable.

### WS9a — Curate the biology eval set (REAL questions + REAL evidence)  ·  *M, ongoing*
**Necessity:** there is no biology-grounded eval set today (only codegen benchmarks).
**Edit [NEW — data, not code]:** a small set of real biological questions, each with **known-good real evidence** (real record IDs / real expected sources), authored/validated with a domain expert. **No synthetic questions or fabricated expected answers** — the eval is worthless if the ground truth isn't real.
**Real-data AC:** ≥N real questions, each with ≥1 verifiable real evidence record; a domain expert signs off on the ground truth. (N small to start; honesty over coverage.)
**Tests:** the eval set IS the test fixture; `test_eval_set_evidence_resolves` asserts every expected record ID resolves to a real record (no dangling ground truth).
**Review gate:** self-review (ground truth is real + expert-validated); review-gate agent (adversarial: any fabricated/unverifiable expected evidence?); `Reviewed:` trailer.

### WS9b — Measure end-to-end + per-pattern + validator catch-rate  ·  *M, ongoing*
**Attach point (EXISTS):** the codegen benchmark machinery (`tests/benchmarks/codegen/*`) — reuse the runner shape.
**Edit [partly NEW]:** measure decomposition correctness, reuse rate, **validator catch-rate on seeded-error workflows** (real workflows with injected real defects), citation validity (R4), answer fidelity vs the WS9a ground truth, and step-level error propagation. Feeds R5.WS5b (performance gate) + R6's ship-gate.
**Real-data AC:** the harness reports, on the real eval set, a validator catch-rate on seeded real defects > 0 (the validators actually catch something — guards against "validator theatre") and a citation-validity rate; numbers are real measurements, never invented.
**Tests:** `test_validators_catch_seeded_real_defects` · `test_eval_reports_citation_validity_on_real_set`.
**Review gate:** self-review (measures real outcomes; no invented metrics); review-gate agent (adversarial: does any metric default to a flattering value when a stage is missing?); `Reviewed:` trailer.

---

## 5. Task graph (R6–R9 + upstream deps)
```
R1..R5 ───────────────────────────────────────────────┐
R9.WS9a (real eval set) ─→ R9.WS9b (measure) ──────────┤
                                  │                      ├─→ R6.WS6b ship-gate (needs R1+R2(1-3)+R4 green on R9)
R6.WS6a (bounded decompose) ──────┼─→ R6.WS6b (outer loop) ─→ R6.WS6c (eval-optimizer feedback)
R9.WS9b ─→ R5.WS5b (perf gate)    │
R2 (gates) ─→ R7.WS7b (promotion) ─┘
R2.WS2b + R3.WS3c ─→ R8.WS8a (sandbox wires into both)
G4/inspect_run ─→ R7.WS7a (run record) ─→ R7.WS7c (determinism split)
```
- **R9 is the spine of the back half** — it gates R5.WS5b AND R6's ship. Build the (small, real) eval set early even though it's "Rank 9."
- **R6.WS6b is the last thing enabled**, behind the explicit ship-gate. Enabling it before R1+R2+R4 are green on R9 is the cardinal anti-goal.
- **R8 can trail** until R6 removes the human, but must precede any unattended autonomous execution.

## 6. Mandatory review structure (per workstream)
Identical to all prior plans: Phase 3 self-review (5 Qs) + `review_policy_check.py` → Phase 4 `review-gate` agent on the diff → Phase 5 commit with `Reviewed:` trailer (hook-enforced). Cross-repo WS (WS6a/WS6c touch nanobrain LoopController/event paths) land nanobrain-side first.

## 7. Real-data test policy (no synthetic)
- The R9 eval set's ground truth is **real, expert-validated evidence** — the one place synthetic data would silently invalidate everything downstream (R5.WS5b's gate, R6's ship-gate). Forbidden.
- Validator catch-rate is measured on **real workflows with injected real defects**, not synthetic broken YAML.
- Autonomy e2e (R6) runs **real questions on the real stack** (Ollama-gated), deciding on output VALUES (G127).

## 8. Brutal-truth notes
- **R6 and R9 are the real, heavy, new work** — everything before them is seam-additions on mature code; these two build the agent and the way to know if it's any good. They are correctly LAST because they depend on R1–R5, and detailing them past interface level now would be inventing code against code that doesn't exist.
- **R9 is the honesty backstop for the whole roadmap.** Without a real biology eval set, R5.WS5b's "≥70% gate" and R6's "ship when green" are both vapor — there are no numbers to gate on. Build the small real eval set first; a roadmap whose success criterion is unmeasured is a wish.
- **The cardinal anti-goal lives here:** shipping R6's autonomy loop before R2+R4 are green on R9 automates ungrounded, unvalidated composition — exactly the "fluent, well-cited, validated, and WRONG answer" both panels said is the failure that matters. The ship-gate is the load-bearing constraint of the entire roadmap.
- **R8 is deliberately low-urgency now** (import-scan + human approval is a working interim control) but becomes non-negotiable the instant R6 runs unattended.

---

## ROADMAP PLANNING COMPLETE
All nine ranks now have Phase-1b plans: `rank1_…`, `rank2_3_…`, `rank4_5_…`, `rank6_9_implementation_plan.md`. Each workstream carries file:line-or-interface grounding, real-data ACs, named tests (no synthetic), a task graph, and a **mandatory per-workstream review gate**. Execution order across ranks: **R1 → (R2, R4-WS4b early) → R3 → R5 → R9-eval → R6 (ship-gated) → R7 → R8**.
