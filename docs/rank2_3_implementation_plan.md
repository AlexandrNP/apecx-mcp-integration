# Ranks 2–3 — detailed implementation plan (Phase 1b): mandatory layered validators + on-demand rhea tool autogen

> **STATUS: FUTURE WORK — detailed plan, not executed.** Phase-1b expansion of Ranks 2–3 from `workflow_crafting_intelligence_roadmap.md`, same standards as `rank1_semantic_retrieval_implementation_plan.md`. No code changed. Execution runs the full `/feature` flow with the per-workstream review gate (§4).
>
> **Resolution honesty (the "same standards" caveat).** Where a workstream attaches to code that EXISTS today, edits are given at `file:line` with before→after snippets. Where a workstream creates a NEW module, it is specified at **interface/contract level only** and marked **[NEW — no line refs until it exists]** — fabricating line numbers against non-existent code is the speculation this plan refuses. Both carry real-data ACs, named tests, and a review gate.
>
> **Blocked-by:** R2/R3 assume R1 (the planner/retrieval seam, `inner_workflow_name`, the run_workflow hook point) — see the task graph (§3). They are planned now but execute after R1 lands.

---

## RANK 2 — Mandatory, layered validator construction

**Goal (roadmap R2):** every composed workflow ships validators across four layers — structural / interface-contract / semantic-fit / execution-behavioural — and **no gate trusts an LLM as the sole oracle** (SELF-[IN]CORRECT; LLM-as-judge survey, §roadmap 8.2). Reuse the proven gated-review-revise topology.

### WS2a — Compose-time interface/contract validation (extend the existing validator)  ·  *S–M*
**Necessity:** today data-unit existence + type compatibility are checked only at runtime; an orphan I/O survives compose. The composer validator already exists and is the right home.
**Attach point (EXISTS):** `composition/workflow_validator.py:215` `validate_workflow_against_framework(...)` → `_validate_links_block()` already checks dangling/workflow-level/step-qualified refs; `WorkflowViolation` (`:41-56`) + `to_feedback_payload()` (`:91-112`) already feed the C1 retry loop.
**Edit:** add an interface-contract check that, for every link `source→target`, the target step's declared input data-unit exists AND (where both declare a type) types are compatible — emitting a `WorkflowViolation(rule_id="interface.unresolved_target", ...)` reusing the existing dataclass + feedback path. Before→after is localized to a new `_validate_interface_contracts(workflow_dict, catalog_*)` called alongside the existing two block-validators at `:259-260`.
**Real-data AC:** take a REAL composed workflow dict (from `Composer.compose` on a real prompt) and a copy with one link's target data-unit renamed to a non-existent name → the validator returns a violation for the broken copy and zero new violations for the intact original. (Decide on the violation list, on real composer output.)
**Tests:** `test_interface_contract_catches_renamed_target` (real composed dict, mutated copy) · `test_interface_contract_clean_on_valid_real_workflow` (no false positives on the 2 real manifest workflows). No synthetic workflows — mutate real ones.
**Review gate:** self-review (DRY — reuse `WorkflowViolation`/`to_feedback_payload`, don't add a parallel violation type); review-gate agent (adversarial: does the type-compat check produce false positives on untyped data units? — must default-allow when type is absent); `Reviewed:` trailer.

### WS2b — Auto-attached execution/behavioural gate (reuse the gated-review-revise topology)  ·  *M*
**Necessity:** novel Python is import-scanned but **never run** before acceptance; a step that loads but crashes on `process({})` ships.
**Attach point (EXISTS):** the proven topology `composition/workflows/benchmark_runtime_gated_review_revise/workflow.yml` (DirectLink-always-to-output + ConditionalLink-on-`decision==fix`-to-reviser) and the deterministic gate `FrameworkComplianceRunnerStep` whose `process()` returns `{decision: "pass"|"fix", critique, ...}` (`framework_compliance_runner_step.py:418-427`). `CodeStructureValidatorStep` returns the same shape (`:220-229`).
**Edit [partly NEW]:** an auto-attach step that, after compose, wraps the composed workflow's novel steps with the existing `FrameworkComplianceRunnerStep` gate using the existing topology — i.e. emit the validator wiring into the composed YAML, not new validator code. The wiring generator is **[NEW — interface only]**: `attach_execution_gate(workflow_dict) -> workflow_dict` that inserts a `runtime_validator` node + the DirectLink/ConditionalLink pair around novel steps, reusing the existing step class + link shapes verbatim. No new validator logic — only assembly of proven parts.
**Real-data AC:** compose a workflow whose novel step raises in `process` (a real composer run that emits broken novel Python) → after auto-attach + `Workflow.run`, the terminal output reflects the `fix` path (decide on the output VALUE per G127, not status) and the `step_failed` event is captured; a workflow whose novel step is correct passes straight through. Real nanobrain execution, real composer output.
**Tests:** `test_execution_gate_routes_broken_novel_step_to_fix` (Ollama-gated e2e — real run) · `test_attach_execution_gate_emits_proven_topology` (deterministic — assert the generated YAML matches the gated-review-revise link shapes; unconditional). Unit/integration parity recorded in docstrings.
**Review gate:** self-review (necessity: closes the "loads-but-crashes" gap; minimality: assemble existing parts, write no new gate logic; DRY: reuse the existing step + link classes); review-gate agent (adversarial: does auto-attach corrupt a workflow with no novel steps? must be a no-op); `Reviewed:` trailer.

### WS2c — The "no LLM as sole oracle" rule + independent-critic stage  ·  *M*
**Necessity:** the hard rule from the evidence — a generated check is a hypothesis, not a proof. Each composed workflow class must declare ≥1 **non-LLM deterministic invariant** as a release gate; an LLM critique may supplement, never substitute.
**Attach point (EXISTS):** the deterministic gates (WS2b) ARE the non-LLM invariant; the optional `WorkflowReviewer` (composer's opt-in semantic review) is the LLM critique. Today the reviewer is non-blocking and can regress correct work (finding F10).
**Edit [partly NEW]:** a release-gate policy `require_deterministic_invariant(composition_summary) -> None` **[NEW — interface only]** that REFUSES to mark a composed workflow "validated" unless ≥1 deterministic gate (structural/interface/execution) is present; and route the LLM reviewer to a **different role/model** than the drafter (the config already supports per-role bindings, `composer_config.yml:65-78` `model_roles`) so the critic is not the generator self-judging.
**Real-data AC:** a composed workflow with only an LLM-reviewer verdict and no deterministic gate is REFUSED promotion (raises/flags); the same workflow with WS2b's execution gate attached is accepted. Real composer outputs.
**Tests:** `test_promotion_refused_without_deterministic_invariant` · `test_critic_role_differs_from_drafter_role` (assert the resolved critic model ≠ drafter model from real `model_roles`).
**Review gate:** self-review (this is a policy gate — confirm it can't be trivially satisfied by an LLM "deterministic-looking" check); review-gate agent (adversarial: can the rule be bypassed?); `Reviewed:` trailer.

---

## RANK 3 — On-demand rhea tool-step autogen (productionised, git-persisted)

**Goal (roadmap R3 + §0):** when a plan needs a capability not in the catalog, synthesize the rhea tool step **on demand**, **validate it**, **persist the generated `.py`+`.yml` to git** (the repo is the cache — no eager bulk synthesis, no separate UTD DB), and register it for reuse.

### WS3a — On-demand synthesis hook in the run path  ·  *M*
**Necessity:** `synthesize_rhea_step` exists but is a standalone helper; nothing invokes it when a workflow needs a missing tool.
**Attach point (EXISTS):** `mcp_surface/tools/eo_primitives.py:271` `run_workflow(name, params, ctx)` → `_run_resolved_entry(entry, params)` (`:321`) → `_load_workflow_for_entry` (`:369`); the synthesizer `nanobrain/.../rhea_step_synthesizer.py:217` `synthesize_rhea_step(tool_name, ...) -> RheaStepSpec` (`:67-93`) emits `step_class` + `step_config`.
**Edit [partly NEW]:** a resolver `ensure_tool_step(capability_query) -> step_ref` **[NEW — interface only]** invoked from the planner/load path when a referenced tool step is absent: `find_tools(query)` → `synthesize_rhea_step` → WS3b persist → return the now-cataloged step. Lazy, per §0 — only fires on a real miss.
**Real-data AC (live-gated):** request a workflow needing MUSCLE when its step file is absent → `ensure_tool_step("multiple sequence alignment")` synthesizes a real `RheaStepSpec` (step_class `RheaFileToolStep`, real `descriptor_id` pinned) against a live rhea; a second request reuses the persisted step WITHOUT re-synthesizing (assert no second `find_tools` call). Real rhea, real tool.
**Tests:** `test_ensure_tool_step_synthesizes_real_muscle` (live-gated on `$RHEA_MCP_URL`) · `test_ensure_tool_step_reuses_persisted` (assert cache hit — no re-synth). Unit fake-MCP for the cache-hit logic paired with the live synth test (parity in docstrings).
**Review gate:** self-review (lazy/on-demand per §0 — confirm it never bulk-synthesizes; fail-loud on synth failure, never guess); review-gate agent (adversarial: race on concurrent first-use of the same tool → must not double-write); `Reviewed:` trailer.

### WS3b — Git-persist the synthesized step (.py + .yml)  ·  *S–M*
**Necessity:** §0 — the git repo IS the persistence layer; a synthesized step must become a reviewable, version-controlled, discoverable catalog entry (so WS1b/R1 retrieval finds it next time).
**Edit [NEW — interface only]:** `persist_rhea_step(spec: RheaStepSpec) -> Path` writes a wrapper `.yml` (the `step_config`) + a thin `.py` (if a subclass is needed) under `composition/steps/generated/<tool>/`, with a provenance header (tool version, container digest, determinism class from `spec.descriptor_id`/`spec.utd`). It does NOT auto-commit — generated files are staged for human review (matches "machine-original → reviewed before reuse", roadmap R7).
**Real-data AC:** after WS3a synthesizes MUSCLE, `composition/steps/generated/muscle/*.{py,yml}` exist, carry the real pinned `descriptor_id`, and load via `Workflow.from_config`/the framework validator (WS2a) without violations. Real synthesized output.
**Tests:** `test_persisted_step_loads_and_validates` (real spec → write → load → validate clean) · `test_persisted_step_carries_provenance_pin` (assert the real version/digest is in the file, not a placeholder).
**Review gate:** self-review (provenance is real, not synthetic; files are git-reviewable, not auto-committed); review-gate agent (adversarial: does a regenerated step overwrite a human-edited one? must detect divergence); `Reviewed:` trailer.

### WS3c — Validate-before-register (the reviewing-agent loop)  ·  *M*
**Necessity:** ToolLibGen/AutoTools (roadmap 8.2): a synthesized tool is not trusted until a reviewing pass validates it. Reuse WS2b's execution gate + a sandboxed smoke invocation.
**Attach point (EXISTS):** the deterministic gates from WS2b + the existing `docker_sandbox.py` scaffold (`composition/docker_sandbox.py`, gated by `APECX_T13B_SANDBOX_EXECUTE=1`).
**Edit [partly NEW]:** `validate_synthesized_step(spec) -> verdict` runs (a) schema completeness on the UTD, (b) framework-load via the existing runner gate, (c) a sandboxed smoke `process` on a minimal real input — registering only on pass, FAIL-LOUD otherwise.
**Real-data AC (live-gated):** a real synthesized MUSCLE step passes all three checks and registers; a deliberately broken synth (e.g. missing required `file_input_arg`) is REFUSED with a loud reason. Real synth, real sandbox.
**Tests:** `test_synthesized_step_validated_before_register` (live-gated) · `test_broken_synth_refused_loud` (deterministic — malformed spec → refusal).
**Review gate:** self-review (no register-on-failure path; sandbox is on-demand per §0, not pre-provisioned); review-gate agent (adversarial: does smoke-test failure leak a half-registered step?); `Reviewed:` trailer.

---

## 3. Task graph (R2–R3, with R1 dependency)

```
R1 (WS1c name-binding, WS1b corpus, WS1a retrieval) ── must land first
        │
        ├─→ R2.WS2a (interface validation — extends existing validator; LEAST R1-coupled, can start early)
        ├─→ R2.WS2b (execution gate auto-attach) ─→ R2.WS2c (no-LLM-oracle rule; needs WS2b's deterministic gate)
        │                                   │
        │                                   └─────────────→ R3.WS3c (validate-before-register reuses WS2b gate)
        └─→ R3.WS3a (on-demand hook; needs R1 run_workflow seam) ─→ R3.WS3b (git-persist) ─→ R3.WS3c
                                                                         │
                                                                         └─→ feeds R1.WS1b (persisted step → corpus)  [cycle closed by review, not runtime]
```
- **WS2a is the least-coupled** — it only extends the existing composer validator; it could even precede full R1.
- **WS3c depends on WS2b** (reuses the deterministic execution gate).
- **WS3b closes the loop to R1.WS1b** (a persisted step becomes a corpus entry) — but only after human review (R7), so it's a process edge, not a runtime cycle.

## 4. Mandatory review structure (per workstream — non-negotiable)
Identical to the Rank-1 plan: every WS = Phase 3 self-review (necessity/minimality/readability/DRY/deleted) + `review_policy_check.py` → Phase 4 **`review-gate` agent on the diff** → Phase 5 commit with a `Reviewed:` trailer (commit-msg hook enforced). Cross-repo WS (WS3a touches nanobrain's synthesizer call path) land nanobrain-side first. No WS commits without its own gate pass.

## 5. Real-data test policy (no synthetic)
- Validators are tested by **mutating REAL composed workflows** (rename a real link target; emit real broken novel Python via the real composer), never fabricating a workflow dict.
- Rhea autogen is tested against a **live rhea** (`$RHEA_MCP_URL`-gated) on a **real tool** (MUSCLE), with a deterministic fake-MCP unit for cache/refusal logic paired to the live test.
- All success decisions read OUTPUT VALUES, never `status` (G127).

## 6. Brutal-truth notes
- **Most of R2 is assembly, not invention:** WS2b/WS2c reuse the proven gated-review-revise topology + existing validator steps + existing `model_roles`. The genuinely new code is small (the auto-attach wiring generator + the promotion-policy gate). I rate R2 lower-risk than the roadmap's "L" — the hard parts already shipped for codegen.
- **R3's risk is operational, not architectural** (the synthesizer is proven): git-persistence hygiene (don't overwrite human-edited steps), concurrent first-use races, and live-rhea flakiness. All addressed in the review gates, none requiring new framework design.
- **The honest resolution limit:** the `[NEW — interface only]` items (auto-attach generator, ensure_tool_step, persist_rhea_step, the promotion gate) cannot get real line numbers until R1 + these modules exist. Specifying them at contract level is the ceiling of non-speculative detail today; line-level snippets would be invented.
