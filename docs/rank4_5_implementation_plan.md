# Ranks 4–5 — detailed implementation plan (Phase 1b): semantic/question-fit grounding + reasoning-pattern routing

> **STATUS: FUTURE WORK — detailed plan, not executed.** Phase-1b expansion of Ranks 4–5 from `workflow_crafting_intelligence_roadmap.md`, same standards as the Rank-1/2-3 plans. No code changed. Execution runs the full `/feature` flow with the per-workstream review gate (§4).
>
> **Resolution honesty:** `file:line` + snippets where a workstream attaches to EXISTING code; **[NEW — no line refs until it exists]** for genuinely new modules — no fabricated line numbers. Real-data ACs (no synthetic), named tests, task graph, review gate per WS.
>
> **Blocked-by:** R4 extends the existing synthesis/citation machinery (mostly independent of R1). R5 depends on **R1.WS1c** (`inner_workflow_name`) — pattern routing selects reasoning-pattern workflows by name. See §3.

---

## RANK 4 — Semantic / question-fit grounding at compose time

**Goal (roadmap R4):** the composer optimises "answers the question with cited, real evidence," not just "valid YAML." Citations resolve to real retrieved records or the answer says "no evidence found." Split the citation gate into **existence** (mandatory, mechanical, ships first) and **support/entailment** (best-effort, flagged limitation — never over-claimed).

### WS4a — Evidence contract from the question  ·  *M*
**Necessity:** today nothing captures "what would answering this question require" — there's no target to grade fit against.
**Attach point (EXISTS):** `synonym_dictionary/lookup.py:119` `lookup_entity(surface_form, entity_type)` → `LookupResult` (`resolution_status`, `canonical_iri`) resolves entities; the data sources are the known harmonized indices + PubMed + RAG.
**Edit [NEW — interface only]:** `build_evidence_contract(question) -> EvidenceContract` that (a) extracts entity surface forms and resolves each via `lookup_entity` (reusing it, not re-implementing resolution), (b) declares expected data sources, (c) declares the expected output shape. Emitted alongside the composed workflow for WS4c to grade against.
**Real-data AC:** for a real question naming a real organism (e.g. "What is known about chikungunya envelope conservation?"), the contract resolves that organism to its **real** NCBITaxon IRI (via `lookup_entity`, `resolution_status` ∈ {ID_ANCHORED, OLS_EXACT}) and lists the structural+sequence+literature sources; an unresolvable term yields `resolution_status=UNRESOLVED` surfaced LOUD, never a fabricated IRI.
**Tests:** `test_evidence_contract_resolves_real_entity` (real `lookup_entity` on a real term) · `test_evidence_contract_surfaces_unresolved_loud` (real miss → UNRESOLVED, no fabrication). No synthetic entities.
**Review gate:** self-review (DRY — reuse `lookup_entity`, don't add a 2nd resolver; necessity: the contract is the grading target); review-gate agent (adversarial: does it silently drop an unresolved entity?); `Reviewed:` trailer.

### WS4b — Citation-existence gate (mechanical, mandatory)  ·  *S–M*
**Necessity:** the brief's hardest rule — never invent sources. A retrieval-grounded synthesiser still fabricates a citation if the prompt rewards confidence.
**Attach point (EXISTS):** `RagSynthesisStep` already has citation gates — `min_distinct_citations`, `validate_citations_against_inputs`, `fail_on_empty_retrieval` (`rag_synthesis_step.py:138-141`) and **raises `ValueError` on violation** (degrade-loud, not silent). The retrieved record IDs are available in the assembled context (`SynthesisContextAssemblyStep`).
**Edit:** strengthen `validate_citations_against_inputs` so every citation string in the synthesis must **resolve to a real retrieved record ID** present in the assembled context — not merely "looks like a citation." Localized to the existing citation-validation branch; reuse the existing `ValueError` raise path.
**Real-data AC:** run `RagSynthesisStep` on a real retrieved bundle; mutate the synthesis to add a citation to a record ID **not** in the bundle → the step raises `ValueError` (existence gate fires); the unmutated real synthesis passes. A real no-retrieval question → `fail_on_empty_retrieval` yields "no evidence found," never a fabricated cite.
**Tests:** `test_citation_to_absent_record_rejected` (real bundle, mutated synthesis) · `test_no_retrieval_says_no_evidence` (real empty-retrieval path). No synthetic citations — mutate real outputs.
**Review gate:** self-review (necessity: closes fabrication; minimality: tighten the existing check, no new gate; DRY: reuse the ValueError path); review-gate agent (adversarial: can a real-but-irrelevant record ID slip through? — yes, that's WS4c/entailment, must be documented as out-of-scope here); `Reviewed:` trailer.

### WS4c — Question-fit review: existence-grounded, entailment-flagged  ·  *M*
**Necessity:** "did this answer the question?" must be graded on whether the cited evidence EXISTS and covers the contract's sources, not on an LLM's opinion (which can regress correct work — F10).
**Attach point (EXISTS):** the composer's opt-in `WorkflowReviewer` (LLM, non-blocking today); `WorkflowResult` envelope (markdown + data_handle + provenance); `inspect_run` (G4).
**Edit [partly NEW]:** make question-fit review **blocking-but-advisory-with-appeal** and ground it in the EvidenceContract (WS4a): a deterministic check that the answer cites ≥1 real record per required source class (existence — mandatory); the LLM critic only *supplements* (entailment — **flagged as a known limitation, not claimed solved**, per the §6.1 panel resolution).
**Real-data AC:** a real synthesis missing any citation for a required source class is flagged (appealable); claim-evidence **entailment** (real-but-misattributed citation) is explicitly reported as "not verified" — the doc/UX must not claim entailment is solved.
**Tests:** `test_fit_flags_missing_required_source_class` (real contract + real synthesis) · `test_entailment_marked_unverified` (assert the surfaced limitation string).
**Review gate:** self-review (the honesty split existence-vs-entailment is the headline — confirm the doc/UX never over-claims); review-gate agent (adversarial: does "advisory-with-appeal" let a fabrication through? existence must be hard, not appealable); `Reviewed:` trailer.

---

## RANK 5 — Reasoning-pattern reuse as first-class, routed assets

**Goal (roadmap R5):** pattern selection is explicit, routed by question type from the R1 registry, with per-pattern empirical performance recorded and error-amplifying patterns (memory loops) gated behind a measured base-accuracy threshold (≥70%, F39). No universal scaffold (F1/F39).

### WS5a — Route reasoning patterns by question type (extend the deterministic router)  ·  *M*
**Necessity:** patterns exist but are picked implicitly; routing must be explicit + deterministic.
**Attach point (EXISTS):** `TaskCategoryRouterStep` (deterministic task-type router) + R1.WS1c `inner_workflow_name` (bind the selected pattern by name) + R1.WS1b corpus (the patterns are now discoverable).
**Edit [partly NEW]:** extend the router to map question-type → reasoning-pattern name (resolved via the R1 registry), then bind it via `SubworkflowStep(inner_workflow_name=...)`. **Blocked-by R1.WS1c** — without name-binding the router can only return a hardcoded path. FAIL-LOUD on an unknown pattern name (reuse WS1c's resolver guard).
**Real-data AC:** a real code-refinement question routes to the real `tdr_loop` pattern (deterministic, no LLM); a real retrieval-synthesis question routes to `rag_e2e_synthesis`; an unmapped type degrades loud (explicit "no pattern", not a silent default).
**Tests:** `test_router_selects_real_tdr_for_code_task` (deterministic) · `test_unknown_type_degrades_loud`. Real patterns on disk.
**Review gate:** self-review (deterministic routing — no LLM in the gate; reuse WS1c resolver); review-gate agent (adversarial: silent fallback to a default pattern? must be loud); `Reviewed:` trailer.

### WS5b — Per-pattern performance record + error-amplification gate  ·  *M*
**Necessity:** F39 — a closed memory loop *amplifies errors* on low-accuracy domains; such patterns must be gated behind measured base accuracy (≥70%).
**Attach point (EXISTS):** the R9 eval harness (when it lands) produces per-pattern accuracy; the benchmark machinery (`tests/benchmarks/codegen/*`) already measures pass rates.
**Edit [NEW — interface only]:** a `pattern_performance` record (per pattern: measured base accuracy on the eval set) + a gate `allow_pattern(name, domain_accuracy) -> bool` that refuses memory-loop-class patterns below the 0.70 threshold. **Blocked-by R9** for real numbers; until then the gate defaults closed for error-amplifying patterns (fail-safe).
**Real-data AC:** with a real measured domain accuracy < 0.70, the memory-loop pattern is refused (gate returns False) and the router falls back to a non-amplifying pattern; ≥ 0.70 allows it. Numbers from real eval runs, never invented.
**Tests:** `test_memory_loop_gated_below_threshold` · `test_memory_loop_allowed_above_threshold` (both fed REAL measured accuracies from the eval harness, not synthetic constants).
**Review gate:** self-review (fail-safe default; threshold sourced from F39, cited); review-gate agent (adversarial: can an unmeasured pattern bypass the gate? must default closed); `Reviewed:` trailer.

### WS5c — Label un-precedented compositions [original — scrutinise]  ·  *S*
**Necessity:** patterns the composer assembles with no catalogued precedent must be flagged + not promoted to "validated" until reviewed (ties to R7 ledger).
**Attach point (EXISTS):** `CompositionSummary` already tracks `steps_generated`/novel (composer schemas) + `is_reuse_dominated`.
**Edit [partly NEW]:** tag a composed pattern with no registry match as `provenance_class="machine-original"` in the CompositionSummary; gate its promotion to reusable on R2 deterministic gates + (R7) human sign-off.
**Real-data AC:** a real composed workflow that introduces a novel topology is tagged machine-original; a fully-reused one is tagged reused-validated. Real composer outputs.
**Tests:** `test_novel_composition_tagged_machine_original` · `test_reused_composition_tagged_validated`.
**Review gate:** self-review (novelty ≠ defect — confirm tagging doesn't block running, only promotion); review-gate agent; `Reviewed:` trailer.

---

## 3. Task graph (R4–R5, with dependencies)
```
R1.WS1c (name-binding) ─→ R5.WS5a (pattern routing)
R1.WS1b (corpus)       ─┘
R9 (eval harness)      ─→ R5.WS5b (performance gate; defaults-closed until real numbers)
R4.WS4a (evidence contract) ─→ R4.WS4c (question-fit grades against it)
R4.WS4b (citation existence) ──┘ (independent of R1; can ship early — LEAST coupled)
R2 (deterministic gates) ─→ R5.WS5c (promotion needs a deterministic gate)
```
- **R4.WS4b is the least-coupled, highest-value-per-effort** — it tightens an existing gate to kill citation fabrication, independent of R1. Could ship before most of R1.
- **R5 is the most R1-dependent** — routing is meaningless without name-binding (WS1c) + a populated corpus (WS1b).

## 4. Mandatory review structure (per workstream)
Identical to the prior plans: Phase 3 self-review (5 Qs) + `review_policy_check.py` → Phase 4 `review-gate` agent on the diff → Phase 5 commit with `Reviewed:` trailer (hook-enforced). No WS commits without its gate.

## 5. Real-data test policy (no synthetic)
- Grounding tests run on **real `lookup_entity` resolutions** and **real retrieved bundles**, with fabrication tested by **mutating real synthesis outputs** (adding an absent record ID), never building a synthetic citation set.
- Performance-gate tests are fed **real measured accuracies from the eval harness** (R9), not invented thresholds — until R9 lands, WS5b defaults closed (fail-safe), which is itself the testable behavior.
- All decisions read OUTPUT VALUES (G127).

## 6. Brutal-truth notes
- **WS4b is the single highest-integrity item in the whole roadmap** and it's cheap — it tightens an existing `RagSynthesisStep` gate to make citation *fabrication* mechanically impossible. If only one thing ships from R4/R5, ship this.
- **The entailment honesty is load-bearing:** WS4c can guarantee a citation *exists* but NOT that it *supports the claim*. The plan (and any UX) must say so plainly; claiming "grounded" when only existence is checked would be the exact over-claim the brief forbids.
- **R5 cannot deliver value before R1** — routing patterns you can't bind by name is theatre. Sequencing R5 after R1.WS1c is not optional.
- **WS5b is honest about its own emptiness:** without R9's real numbers it can only default-closed. That's correct (fail-safe), but it means "gate error-amplifying patterns" is aspirational until the eval harness exists — don't claim the gate is "working" on invented accuracies.
