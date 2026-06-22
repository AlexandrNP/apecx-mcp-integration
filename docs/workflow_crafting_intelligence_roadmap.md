# From Guided Composition to a Workflow-Crafting Intelligence — A Ranked Roadmap

> **STATUS: FUTURE WORK — proposal, not scheduled.** This is a forward-looking roadmap for evolving the composer into an autonomous, validated, grounded workflow-crafting system. Nothing here is implemented, approved, or committed to a milestone. It is a planning artifact to be reviewed, challenged (see the panels in §6 and the `[original — scrutinise]` tags), and selectively turned into `/feature`-scoped work later. Do not treat any rank as in-flight.

**Scope:** planning only. No code is changed by this document.
**Subject:** `apecx-mcp-integration` (composition layer) + the `rhea` tool fork + the `nanobrain` framework.
**Goal (restated):** turn today's *human-guided, semi-automatic* workflow composer into a system that can **autonomously craft, validate, and ground workflows that answer biological questions** — reusing steps/patterns wherever possible, auto-minting tool steps from Rhea's repertoire **on demand**, treating validator construction as mandatory, and citing real sources (never inventing them).

**Provenance of this document.** It merges two analyses: a scientific-rigor-led roadmap and an engineering/architecture capability audit performed by reading the composition layer with parallel exploration agents + cited web research. Every external claim is tagged **[verified-assistant]** (re-checked on the web during the audit session, 2026-06-22 — see §8.2), **[verified-plan]** (carried from the source roadmap's own verification, §8.1 — *not* independently re-checked by the assistant), or **[established]** (well-known prior work cited by name/venue). Design ideas with no external precedent are labelled **[original — scrutinise]** so they are never mistaken for received wisdom. Per the brief, fabricated sources are a release-blocking defect; where a citation could not be independently confirmed it is attributed to its origin rather than asserted as fact.

---

## 0. The resource-discipline mandate (cross-cutting, governs every rank)

**Nothing infrastructural is pre-built. Everything is synthesized/instantiated on demand, and resource usage is minimized by default.** Concretely:

- **Rhea tool steps are synthesized on demand** — when a plan needs a capability not already in the catalog, and only then. There is **no bulk pre-synthesis** of Rhea's ~7000-tool repertoire.
- **Synthesized steps are persisted as committed git artifacts** — the generated step files (`.py` + the wrapper `.yml`) are written into the repo so they become first-class, reviewable, version-controlled, reusable catalog entries. The git repo *is* the persistence layer for synthesized tools; there is **no separate eager UTD index/database to maintain**. (This directly resolves the "where do synthesized descriptors live?" gap — answer: as reviewed step files discovered by the registry in Rank 1.)
- **The same principle generalises** to every heavy resource: conda envs, container images, RAG indices, the Rhea server itself — build/pull/index *when first needed*, cache, and never provision speculatively. (This matches the already-shipped on-demand patterns: per-tool conda envs built on first call + cached in Redis; the rhea-server image built by `apecx-setup rhea` only when the container backend is selected.)

This mandate is why the tool-autogen workstream (Rank 3) is framed as *lazy, query-driven synthesis with git-committed reuse* rather than a 7000-tool batch job, and why an autonomous loop (Rank 6) must reuse-or-synthesize-then-cache rather than pre-stage.

---

## 1. Executive summary

The system is much further along than "semi-automatic" suggests. Composition is already an autonomous LLM pipeline (retrieve → spec → expand → validate → repair → categorise → optional review); Rhea tool-step *synthesis* already exists in code (`synthesize_rhea_step`, unit-proven + live-gated); and ~30 workflows run today against live curated data with loud degradation. The mature building blocks are unusually broad — what is *guided* is not the composing, it is the **trust boundary**: a human approves before anything novel executes, retrieval is a substring match, validators are hand-written and structural-only, reasoning patterns can't be referenced by name, and the composer cannot yet judge whether a workflow actually *answers the science question*.

So the work is **not** "build an autonomous composer from scratch." It is **closing the trust gaps so autonomy is *earned* rather than asserted** — and adding the few *seams* that make the existing assets composable:

1. **Retrieval & reuse** — semantic search over a unified registry (steps, workflows, **reasoning patterns**, synthesized Rhea steps) so reuse beats regeneration; and a **name-based binding seam** so reasoning patterns can be referenced and combined (today `SubworkflowStep` needs a hardcoded inner-workflow *path*).
2. **Validation** — mandatory, layered, partly *auto-constructed-per-workflow* validators that **never trust an LLM as the sole oracle**.
3. **Semantic grounding** — a notion of "did this answer the question?" tied to **cited, real evidence**, not "is this valid YAML."
4. **Tool-step autogen** — promote the existing single-file Rhea synthesizer into a first-class, **on-demand, git-persisted, validated** path (per §0).

The framing that ties this to the external literature: this is **AFLOW's "Operators-as-reusable-bundles" idea applied to assets we already have** — TDR, best-of-N, review-revise, consensus, RAG-synthesis are exactly the kind of reusable node-bundles AFLOW searches over [verified-assistant]. We do not need to build the meta-search first; we need to make the operators *referenceable* and the outputs *trustworthy*.

The single highest-leverage truth, on which both panels converged: **none of this counts until a real scientist runs an autonomously-composed workflow and trusts the result.** The roadmap is ranked by *distance to that moment*, not by engineering elegance.

---

## 2. What exists today (grounded capability map)

Verified by live introspection (`apecx_capabilities` — Postgres/Redis/MinIO/Ollama/Rhea all `ready`, ~31/33 workflows runnable) and by reading the composition layer with file:line precision.

### 2.0 Four-pillar audit (assistant exploration, file:line anchors)

| Pillar | What exists (anchors) | Key gap for autonomy |
|---|---|---|
| **Composition** | `composer.py::compose()` — LLM-driven retrieve→spec→expand→validate(`workflow_validator.py`)→C1 retry→class-path repair→categorise(`differ.py`)→optional review. Spec mode (`spec_system.md`) + skeletons (`skeletons/`, ~13). | Single-shot; **no plan-before-code**; no execution feedback; retry cap default 1. |
| **Reuse / reasoning patterns** | TDR, best-of-N, RAG-synthesis are **100% reusable** (framework primitives only); review-revise/consensus are templates. `SubworkflowStep`/`MapSubworkflowStep`/`RecursiveSubworkflowStep` nest workflows. | **`SubworkflowStep` requires a hardcoded `inner_workflow_path`** — no name→workflow registry, so patterns can't be referenced/combined by name. Composer corpus is **only 2 manifests, manually registered** (vs auto-discovery for runnable workflows). |
| **Validators** | Static: `workflow_graph.py` (cycle/orphan/self-link), `workflow_validation.py`, lint R1–R3 (`lint_workflow_yamls.py`), pydantic `extra='forbid'`, composer `workflow_validator.py`. Runtime: `FrameworkComplianceRunnerStep`, `CodeStructureValidatorStep` (AST), G127 value-not-status. Output: `PromptRegressionHarness`, `RagSynthesisStep` citation/size gates. Proven gated review-revise topology (`benchmark_runtime_gated_review_revise`). | **No auto-generated validator** for a composed workflow; validation is hand-authored + structural-only; LLM reviewer is non-blocking and measured to *regress correct code* (finding F10). |
| **Rhea tool→step** | `synthesize_rhea_step()` (nanobrain `library/tools/rhea_step_synthesizer.py`) — discovers UTD via `tools/list`, branches file/JSON, maps params from schema, **fails loud** on required-no-default/multi-file/ambiguity. 16 unit tests + live-gated MUSCLE test. `RheaMCPDiscovery`, `RheaAdapter`, `RheaFileToolStep`. | Single-tool helper (no on-demand wiring into the planner); **no git-persistence of synthesized steps** (per §0 this is the chosen design, not a DB); single-file only; Galaxy `<repeat>`/`<conditional>` gaps; old-worker provenance gaps. |

### 2.1 Composition pipeline (`src/apecx_integration/composition/`)
- **Entry:** `Composer.compose(prompt, context)`. Autonomous end-to-end once a prompt is given.
- **Flow:** retrieve top-k catalog components → re-rank by named class → build LLM messages → LLM emits full YAML (`system.md`) or a **minimal JSON spec** (`spec_system.md`) → expander injects boilerplate (`auto_transfer`, data units) → **framework validator** (`workflow_validator.py`) → **catalog-grounded path repair** (CPR — fixes a hallucinated module path when exactly one catalog match exists) → **import-scanner sandbox** for novel Python → **categoriser** (`differ.py`: standard / parameterised / wrapped / novel / missing) → optional **semantic reviewer** (LLM, non-blocking) → `ComposedWorkflow` with a `CompositionSummary` (`reuse_ratio`, `is_reuse_dominated(0.8)`).
- **Reuse ladder already encoded** (`system.md` + `composition_bias.md`, the CLOSED-CLASS + REUSE-FIRST rules pinned in 7–8 prompts): library steps → sub-workflow steps → skeletons → two-step compositions → *only then* novel Python.
- **Retry:** parse/validation failures retried with structured feedback (C1 loop), default cap **1**.

### 2.2 Reasoning patterns (already catalogued, mostly benchmarked)
~30 workflows spanning CoT/direct, plan-then-code, RAG-grounded, review-revise, perturbed/structural consensus, memory loops, and the team's own syntheses **TDR** (test-driven recursive refinement — shipped) and **HD-RSS / GMR** (designed; HD-RSS measured *negative* on MBPP). Patterns live both as procedural factories (`tests/benchmarks/codegen/*`) and YAML workflows (`composition/workflows/benchmark_*`), with a deterministic `TaskCategoryRouterStep` to route by task type. **These are this repo's "Operators" in the AFLOW sense** [verified-assistant] — the reuse seam (Rank 1/5) is what makes them composable rather than hand-picked.

### 2.3 Tool integration (Rhea)
- **Rhea** serves Galaxy-ToolShed bioinformatics tools, executed via Parsl + conda + S3/MinIO, with an APECx extension wrapping tools as **Unified Tool Descriptors (UTDs)** carrying version, container digest, a file-vs-JSON discriminator, and a determinism class.
- **Auto-synthesis already exists:** `synthesize_rhea_step(tool_name)` reads `tools/list`, converts to a UTD, and emits a `ToolExecutionStep` or `RheaFileToolStep` config — **failing loud** on required-no-default params, multi-file inputs, or file/JSON ambiguity. Catalogued workflows auto-register as MCP tools from `mcp_workflow_catalog.yml`.
- **Known blockers to full autogen:** multi-file-input tools (v1 handles exactly one), Galaxy `<repeat>`/`<conditional>` parsing gaps, older Rhea workers missing the provenance annotation.
- **§0 design choice:** synthesized steps are written to the repo as `.py`+`.yml` and reused; synthesis happens lazily, per question.

### 2.4 Validators today
- **Structural/framework only** and **all hand-authored** (see §2.0). Plus an AST/runtime `CodeStructureValidatorStep` for codegen and the deterministic `FrameworkComplianceRunnerStep` (subprocess load + `process({})`).
- **Documented gaps:** no compose-time *semantic* validation; data-unit existence checked only at runtime; substring-only retrieval; import-scan but **no runtime isolation** for novel Python; novel code is **not tested before acceptance**; LLM reviewers measured to *hallucinate problems and regress correct code* (F10).

### 2.5 The external-orchestration design (already drafted)
`docs/external_orchestration_design.md` proposes the frontier LLM as the *primary* orchestrator (decompose → select → sequence → synthesize), a **bounded local-LLM fallback** (LoopController depth caps + cost envelope, fail-loud if not decomposable), a `WorkflowResult` envelope (markdown + data handle + provenance), and **tiered tool substitution** (Tier-1 config patch → Tier-3 composer re-bridge). Estimated ~85% reuse, ~6 genuine deltas.

**Implication:** the autonomy substrate is largely *designed*; the missing pieces are the ones that make autonomy *safe, grounded, and composable* — exactly what the ranking below prioritises.

---

## 3. Target capability model

Five pillars the system needs to craft biological-question workflows autonomously:

1. **Question understanding & decomposition** — map a natural-language biological question to a typed information need (entities, data sources, analysis class, expected evidence shape).
2. **Reuse-first retrieval** — semantic search over a registry of steps, workflows, and *reasoning patterns* (referenceable by name) so the default is composition of existing assets.
3. **On-demand tool-step autogen** — mint validated steps from Rhea's repertoire when a question needs them, with provenance, persisted to the repo (§0).
4. **Mandatory layered validation** — structural + interface + semantic + execution gates; every workflow ships auto-constructed validators; *no* gate trusts an LLM as sole oracle.
5. **Grounded synthesis & provenance** — every answer carries cited, real evidence and a replayable run record; "original" (newly-composed, un-precedented) structures are flagged for scrutiny.

---

## 4. Ranked workstreams

Ranking criterion: **how much each item shortens the path to a scientist trusting an autonomously-composed answer**, weighted by (a) unlocks-other-work, (b) risk-if-skipped, (c) reuse of what already exists. The assistant audit concurs with this ordering, with one refinement folded in (the registry/name-binding *seam* is the concrete unlock under Ranks 1 & 5). Effort is rough T-shirt sizing.

### Rank 1 — Semantic retrieval over a unified reuse registry (+ name-based pattern binding)  ·  *Unlocks everything · M*
**Why #1:** "Reuse steps and patterns" is impossible to do well on substring matching (`component_catalog.py` is a linear scan). Every downstream decision — reuse vs. regenerate, which pattern to apply, which tool to mint — depends on *finding* the right existing asset. The Phase-4 K-NN interface is already stubbed.
**Do:** (a) build an embeddings index over steps + sub-workflows + skeletons + **reasoning patterns** + synthesized Rhea steps; return calibrated similarity with a reuse threshold; expose via `list_workflows(query)` / `inspect_workflow`. (b) **Auto-discover the composition corpus** (mirror the runnable-workflow scanner) so every cataloged asset — including newly git-committed synthesized steps (§0) — is visible to the composer without hand-editing `composer_config.yml`. (c) **[original — scrutinise]** add a **name-based binding seam**: a registry mapping pattern name → workflow, and an extension to `SubworkflowStep`/`from_skeleton` to bind a pattern **by name with parameter closures** (today it needs a hardcoded `inner_workflow_path`). This is the concrete unlock for "use/combine/reuse reasoning patterns."
**Validator mandate:** a *reuse-decision* check — if a retrieved asset clears threshold, regeneration must be justified, not default.
**Grounding:** retrieval-augmented composition is the established mechanism for reuse [established: RAG]; ToolLibGen shows ad-hoc tools must be *refactored into a structured, searchable library* to scale [verified-plan]; AFLOW's reusable "Operators" are the analogue for reasoning patterns [verified-assistant].
**Risk if skipped:** the composer keeps re-authoring near-duplicates → drift, no reuse, validation surface explodes; reasoning patterns stay hand-picked.

### Rank 2 — Mandatory, layered validator construction (incl. auto-built per-workflow validators)  ·  *Trust gate · L*
**Why #2:** The brief makes validators mandatory; the team's own data shows one structural validator + a non-blocking, hallucination-prone LLM reviewer is not enough (F10). Autonomy without validation is unsupervised code execution.
**Do — four obligatory layers, every composed workflow:**
1. **Structural** (exists) — schema, links, `auto_transfer`.
2. **Interface/contract** — data-unit existence + type compatibility at *compose* time (currently runtime-only); reject orphan I/O before execution.
3. **Semantic fit** — does the workflow address the decomposed question? (see Rank 4).
4. **Execution/behavioural** — dry-run / sandboxed execution of novel steps before acceptance (today novel Python is import-scanned but never run).
**Auto-construction [original — scrutinise]:** from the question's typed information need + each step's I/O contract, *generate* the contract and behavioural checks for that specific workflow — but treat them as **necessary, not sufficient**. Reuse the proven `benchmark_runtime_gated_review_revise` topology (DirectLink-always + ConditionalLink-on-fail) and the G127 value-not-status honesty contract.
**Hard rule from the evidence:** **never let an LLM-generated test be the sole oracle.** LLMs struggle to discriminate their own outputs [verified-assistant: SELF-[IN]CORRECT]; self-judging has blind spots and dual-role bias and is "insufficient without orchestration" [verified-assistant: LLM-as-judge survey]; 34–62% of LLM-generated tests are invalid and test-writers align assertions to observed output, manufacturing false-positive passes [verified-plan]. Pair generated checks with deterministic invariants (AST, type, schema, conservation/known-fact constraints) and cross-model/consistency checks [verified-plan]. Schema-constrained decoding should enforce well-formed specs/tool-calls at generation time [verified-plan: XGrammar].
**Risk if skipped:** silent wrong answers — the worst failure for a science tool and the most corrosive to trust.

### Rank 3 — On-demand tool-step autogen from Rhea's repertoire (productionised, git-persisted)  ·  *Capability expansion · M*
**Why #3:** Directly named in the brief; the hard part already exists (`synthesize_rhea_step`). Promoting it from a single-file helper to a **catalogued, validated, on-demand, repo-persisted** path is what lets the system answer questions whose tools aren't yet wrapped — without violating §0.
**Do (lazy, query-driven — §0):** the planner detects a needed capability → `find_tools(query)` (semantic search over Rhea) → `synthesize_rhea_step` → **validate** (schema completeness, I/O typing, a sandboxed smoke invocation — Rank 2 layer 4) → **write the generated `.py`+`.yml` into the repo** and **register** into the Rank-1 registry with provenance (tool version, container digest, determinism class) → reuse thereafter. No bulk synthesis; the git-committed step *is* the cache.
**Close the documented blockers:** generalise `RheaFileToolStep` to N file inputs; parse Galaxy `<repeat>`/`<conditional>`; require/override the provenance annotation (fail-loud, never guess).
**Grounding:** the "tool maker / tool user" split + automatic tool-library construction with a coding-agent↔reviewing-agent loop [verified-plan: ToolLibGen, AutoTools/Tool-Learning-in-the-Wild; established: LATM]; tool count is itself a reliability bottleneck, so lazy synthesis is the right resource posture [verified-assistant: biomedicine-agent reviews]. Reviewing-agent validation is not optional in those results.
**Risk if skipped:** the catalog stays static; "autonomy" is limited to recombining a fixed toolset.

### Rank 4 — Semantic / question-fit grounding at compose time  ·  *Answer quality · M*
**Why #4:** Today's composer optimises *validity*, not *answering the question*. For biology the deliverable is cited evidence, so "fit" must be defined against retrievable, real sources — extending the existing `rag_e2e_synthesis` + `WorkflowResult` machinery.
**Do:** turn the question into an explicit *evidence contract* (entities resolved against the synonym dictionary; expected data sources: BV-BRC/VIOLIN/PDB/PubMed/Globus; expected output shape). Make semantic review **blocking-but-advisory-with-appeal** rather than silently non-blocking, grounded in *whether the cited evidence exists*, not the LLM's opinion. Reuse the loud-degradation pattern already in the analysis workflows (a no-hit is stated, never silent).
**Provenance/citation mandate:** answers cite real records or say "no evidence found." No fabricated citations, ever. Provenance must be replayable (`inspect_run`, G4). **Split the citation gate** into *existence* (mandatory, mechanical — every citation must resolve to a real retrieved record ID, ships first) and *support/entailment* (best-effort, **flagged as a known limitation, not claimed solved**). KG/verification-layer grounding reduces fabricated intermediate results in multistep science agents [verified-assistant: biomedicine reviews; biorxiv KG+LLM].
**Risk if skipped:** confident, well-formed, *wrong-question* answers.

### Rank 5 — Reasoning-pattern reuse as first-class, routed assets  ·  *Quality multiplier · M*
**Why #5:** Patterns exist but are picked implicitly. The team's strongest finding (F1/F39) is that **there is no universal scaffold** — fit is task-dependent, and a closed memory loop *amplifies errors* on low-accuracy domains. Pattern selection must be explicit, routed, and gated. (Mechanically this rides on the Rank-1 name-binding seam.)
**Do:** make the deterministic `TaskCategoryRouterStep` choose patterns from the Rank-1 registry by question type; record per-pattern empirical performance; **gate** error-amplifying patterns (memory loops) behind a measured base-accuracy threshold (the team's ≥70% rule). Patterns the composer assembles with no catalogued precedent are **[original — scrutinise]**: labelled, flagged for review, not promoted to "validated" until measured.
**Grounding:** compiling workflows into reusable {DAG, prompts, deterministic code} with consensus gains is externally demonstrated [verified-plan: SGDe]; AFLOW operators + Anthropic's pattern taxonomy (prompt-chaining / routing / parallelization / orchestrator-workers / evaluator-optimizer) give the reusable vocabulary [verified-assistant].
**Risk if skipped:** the system applies the wrong pattern confidently; memory loops entrench early mistakes.

### Rank 6 — Bounded autonomous decomposition + execution loop  ·  *The "autonomous" surface · L*
**Why #6 (not higher):** It is the most visible part of "autonomy" but *unsafe without* Ranks 1–5. The design already exists (external-orchestration doc); building it first would just automate ungrounded composition faster — and Anthropic's guidance is explicit: *start simple; add agentic complexity only when simpler solutions fall short* [verified-assistant].
**Do:** implement the local bounded-decomposition fallback (match-first → decompose → dispatch sub-workflows) with LoopController depth/repeat caps + a cost envelope; **fail loud** when not decomposable. Outer loop: decompose → retrieve/reuse (R1) → autogen tools on demand if needed (R3, §0) → validate (R2) → run → ground (R4) → synthesize with citations. Topology = Anthropic orchestrator-workers + evaluator-optimizer [verified-assistant].
**Explicit gate (panel resolution):** R6 ships when R1, R2-layers-1–3, and R4 are demonstrably working on the R9 eval set — not "when validation is perfect," and not before. **Deliberately defer** AFLOW/ADAS-style meta-search over workflows: it presupposes a reliable per-candidate reward we won't have until R6+R9 exist [verified-assistant: AFLOW/ADAS].
**Risk if skipped:** composition stays a one-shot helper, not a question-answering agent.

### Rank 7 — Provenance, reproducibility & "original-composition" ledger  ·  *Scientific defensibility · S–M*
**Why #7:** Mostly wiring of existing pieces (G4 provenance, G37 events, `inspect_run`, `WorkflowResult`), but it is the difference between a demo and a tool a scientist will cite.
**Do:** every run emits a replayable record (inputs, resolved entities, tool versions + container digests, pattern used, validator verdicts, sources cited, **full model-call manifest** — model id, params, temperature). Maintain an **original-composition ledger [original — scrutinise]**: any workflow/step/pattern with no catalogued precedent is recorded with three provenance classes (**human-authored / reused-validated / machine-original**), dated, and queued for scrutiny; promotion from *machine-original* → *reusable-validated* requires passing R2 gates **plus** a human sign-off **at promotion time only** (not per run). Adopt FAIR-style identifiers [established: FAIR]. **Reproducibility-vs-defensibility split (state honestly):** deterministic/containerised legs (data fetch, Rhea tools) are reproducible; the LLM synthesis leg is *defensible + traceable*, not bit-reproducible — and **deterministic settings (temperature 0) are mandatory on validation/decomposition/citation steps**, creativity allowed only in drafters.
**Risk if skipped:** results aren't defensible/reproducible; "original" structures silently become load-bearing without ever being checked.

### Rank 8 — Runtime isolation for novel/auto-generated code  ·  *Safety hardening · M*
**Why #8:** Real and necessary, but the import-scanner + human-approval gate is a working interim control, so it can trail the trust-unlockers. Becomes **urgent the moment Rank 6 removes the human from the inner loop.**
**Do:** finish the deferred container sandbox (`docker_sandbox.py` scaffold exists) so novel steps and freshly-minted Rhea tools execute isolated; tie acceptance to the Rank-2 behavioural gate. Consistent with §0: the sandbox is invoked on demand for the step under test, not pre-provisioned.
**Risk if skipped (once R6 ships):** autonomous execution of un-isolated generated code — unacceptable.

### Rank 9 — Evaluation harness for *composition quality* (biology-grounded)  ·  *Measures the whole thing · M, ongoing*
**Why #9:** Cross-cutting and continuous. The codegen benchmark machinery exists; what's missing is a **biology-question → expected-evidence** eval set so "did we answer correctly?" is measurable, not vibes. The team's own kill criterion is a scientist running a workflow.
**Do:** curate a small set of biological questions with known-good evidence; measure end-to-end (decomposition correctness, reuse rate, validator catch-rate on seeded-error workflows, citation validity, answer fidelity); track step-level error propagation [verified-plan: AgentEval-style DAG step evaluation]. Treat a scientist's verdict as the north-star metric.
**Risk if skipped:** improvements are unfalsifiable; the brutal-panel critique (much design, no external validation) recurs.

---

## 5. Cross-cutting mandates (apply to every rank)

- **On-demand only; never pre-build (§0).** Synthesize tool steps when needed and commit them to git; build conda envs / images / indices lazily; minimize resource usage by default.
- **Reuse before regeneration.** Regeneration requires a logged justification when a registry asset clears the reuse threshold (R1).
- **Validators are non-negotiable.** No composed workflow is "done" without all four layers passing (R2). LLM-as-oracle is forbidden as a *sole* gate; each workflow class must declare ≥1 **non-LLM deterministic invariant** as a release gate [verified-assistant + verified-plan].
- **Sources are real or absent.** Citations point to retrievable records; "no evidence found" is a valid, required answer; fabricated sources are a release-blocking defect; *existence* is enforced mechanically, *entailment* is an acknowledged open limitation.
- **Original work is labelled and scrutinised.** Anything with no catalogued precedent is tagged **[original — scrutinise]**, logged in the ledger (R7), machine-checked, and human-reviewed before it earns "validated" status or becomes reusable — but novelty is *not* treated as a defect.
- **Degrade loud, never silent.** Extends the existing loud-degradation convention to composition and validation, not just data fetching.
- **Determinism in the gates.** Temperature 0 on validation/decomposition/citation steps; full model-call manifest in every run record (R7).

---

## 6. Panel debates

Two panels argued the plan from complementary angles. Genuine disagreements and their resolutions are recorded, not smoothed over. **The virology domain is excluded from both panels by request.**

### 6.1 Scientific-rigor panel
**Panelists:** **R** — Reproducibility & Methods Engineer · **P** — Provenance / Citation Auditor · **M** — Philosophy-of-Science / Methodology · **X** — Red-Teamer / Adversary.

**On the ranking itself**
**X:** You ranked the autonomous loop (R6) sixth. That's the deliverable. Ranking it below "retrieval" looks like avoiding the hard thing.
**R:** It's the deliverable *only if trustworthy*. An autonomous loop over substring retrieval + one structural validator is a faster way to produce wrong answers. Sequencing safety-enablers first is the rigorous choice, not the timid one.
**M:** "Crafting workflows that answer biological questions" is an *epistemic* claim, not a throughput one. Optimise for *justified true answers* → grounding and validation precede automation.
**X (concedes, with a hook):** Then R6 must not slip forever behind "one more validator." Put a gate on it.
**Resolution:** R6 keeps rank 6 but ships the moment R1, R2-layers-1–3, and R4 work on the R9 eval set — not "when validation is perfect."

**On auto-constructed validators (R2)**
**X:** You mandate validators, then *generate* them with an LLM — circular. I can make any workflow pass by generating a lenient test.
**P:** Strongest objection in the room, and backed by data: LLM test-writers align assertions to observed output; a third to two-thirds of generated tests are invalid [verified-plan].
**R:** Which is why generated checks are *necessary, not sufficient*, paired with deterministic invariants + a behavioural dry-run. The auto-built part is the *scaffolding* (what to check, from the I/O + evidence contract), not the *oracle*.
**M:** An LLM-generated test is a *hypothesis about correctness*, never a proof. Trust comes from independent deterministic anchors — types, schemas, known biological constraints, execution.
**Resolution:** R2 amended — each workflow class must declare ≥1 **non-LLM deterministic invariant** as a release gate; generated checks supplement, never substitute; cross-model/consistency verification added as a recommended second anchor [verified-plan].

**On citations / "never invent sources"**
**P:** A retrieval-grounded synthesiser will still hallucinate a citation if the prompt rewards confidence. Enforce mechanically: every citation string must resolve to a real record ID or be rejected pre-display.
**X:** I'll break it: the model cites a *real* record that doesn't support the claim. Resolution ≠ relevance.
**M:** Two distinct failures: (1) fabricated source — handled by construction; (2) real source, misattributed claim — needs claim-evidence entailment, which is harder and must not be over-promised.
**Resolution:** R4 splits the gate into *existence* (mandatory, mechanical, ships first) and *support/entailment* (best-effort, flagged as a known limitation). The doc must not overstate (2).

**On "original composition" and novelty**
**M:** Label original + scrutinise — good, but don't treat *novel* as *suspect by default*; novelty isn't a defect.
**R:** The ledger handles this: original ≠ rejected; it means *not-yet-validated*. It runs, labelled, behind the validators; it just can't be *reused as if proven* until reviewed.
**X:** Who reviews originals? Same LLM → circular; human → reintroduced bottleneck.
**M:** Both, staged: deterministic + cross-model checks first (cheap, automatic); human scrutiny reserved for items that clear the machine gates and get *promoted to reusable*.
**Resolution:** R7 ledger encodes three provenance classes; promotion machine-original → reusable-validated requires R2 gates **plus** human sign-off at promotion time only.

**On reproducibility of an LLM-in-the-loop system**
**R:** Nondeterministic LLMs make runs non-reproducible; a scientist who can't reproduce won't cite.
**M:** Distinguish *reproducibility* (same in → same out; promised for deterministic/containerised legs) from *defensibility* (inspectable evidence chain; promised for the synthesis leg). Say exactly that.
**X:** Pin every model call; temperature 0 on validators + decomposition; creativity only in drafters.
**Resolution:** folded into R7.

**Consensus on the biggest risk:** the failure that matters is a **fluent, well-cited, validated answer that is wrong** — the one a scientist will believe. Every rank is justified by how much it lowers that probability; the only real proof is R9 + a domain expert. Unanimous: **do not let R6 outpace R2+R4 on the calendar.**

### 6.2 Engineering / architecture panel (assistant audit)
**Panelists:** **Arch** — framework/composition architect · **ML** — agentic-systems engineer · **Verify** — verification & reliability lead · **Know** — knowledge/provenance & tool-synthesis engineer · **Skeptic** — adoption/risk/PM. *(No virology seat.)*

**On how much "intelligence" to build**
**ML:** AFLOW is compelling — workflows as graphs, reusable Operators, MCTS search; we already *have* the operators (TDR, best-of-N, consensus, review-revise). Build the search.
**Skeptic:** Anthropic says start simple. MCTS over workflows needs a per-candidate reward; we have no execution-feedback signal (R6) and no biology benchmark (R9). Search with no reward is a token bonfire.
**Arch:** Both right on sequencing, but ML names the prize: AFLOW Operators ≅ our reasoning-pattern workflows. The unlock isn't search — it's making patterns **referenceable by name** so they can be *combined* (today `SubworkflowStep` can't). Foundational regardless.
**Resolution:** build the deterministic plan→retrieve→compose→validate loop first; AFLOW-style search is a *later, optional* optimizer once R6+R9 give a reward. **[Grounded: AFLOW, Anthropic.]** (Mirrors §6.1's R6 gate.)

**On the registry seam as the linchpin**
**Arch + ML (agree):** The single highest-leverage *code seam* is name-based sub-workflow binding + auto-discovered corpus (Rank 1c/1b). Without it, "reuse/combine reasoning patterns" is aspirational; with it, the existing patterns become composable Lego.
**Verify:** And it must fail-loud on unknown pattern names — a silent "pattern not found → empty sub-workflow" would be the dominant new silent-failure shape.
**Resolution:** Rank 1 carries the registry + fail-loud name resolution.

**On Rhea synthesis posture (settled by the brief)**
**Know:** Earlier I weighed eager bulk-synthesis vs on-demand. The brief settles it: **on-demand only, persist generated `.py`+`.yml` to git, never pre-build (§0).** This also removes the "where do 7000 UTDs live?" gap — they don't; only the steps a real question needed exist, as reviewed commits.
**Skeptic:** Which keeps resource usage proportional to actual demand and keeps the catalog curated by use, not by scraping.
**Resolution:** Rank 3 reframed as lazy, query-driven, git-persisted synthesis.

**On validators (echoing §6.1, engineering form)**
**Verify:** The generator must not be its own judge — deterministic anchors first, independent-model critique second, HITL last. This is non-negotiable and is the line between "intelligence" and "confident hallucination engine."
**Resolution:** identical to §6.1's R2 amendment; no daylight between the panels here.

---

## 7. Risks, anti-goals, and kill criteria

- **Anti-goal:** automating composition faster than it can be validated or grounded. Mitigation: the R6 gate (§4, §6.1).
- **Anti-goal:** pre-building infrastructure "to be ready." Mitigation: §0 — on-demand only, git-persist what was actually synthesized.
- **Risk — validator theatre:** layers that exist but catch nothing. Mitigation: R9 measures validator catch-rate on seeded-error workflows.
- **Risk — silent name-resolution miss:** a referenced reasoning pattern not found → empty sub-workflow. Mitigation: fail-loud name resolution (Rank 1).
- **Risk — citation hallucination:** addressed mechanically for *existence*; *entailment* is explicitly an open limitation, not solved.
- **Risk — error-amplifying patterns:** memory loops gated behind the ≥70% base-accuracy rule (F39).
- **Kill criterion (inherited and endorsed):** if no domain scientist trusts an autonomously-composed, cited answer on the R9 eval set within the target window, the autonomy thesis is unproven regardless of how much infrastructure exists.

---

## 8. References

### 8.1 Carried from the source roadmap (verified by the plan's author; NOT independently re-checked by the assistant)
- SGDe — *Compiling Deterministic Structure into SLM Harnesses*, arXiv:2604.17450.
- *Compiling Agentic Workflows into LLM Weights: Near-Frontier Quality at Two Orders of Magnitude Less Cost*, arXiv:2605.22502.
- *AgentEval: DAG-Structured Step-Level Evaluation for Agentic Workflows with Error Propagation Tracking*, arXiv:2604.23581.
- *Agint: Agentic Graph Compilation for Software Engineering Agents*, arXiv:2511.19635.
- XGrammar — *Flexible and Efficient Structured Generation Engine for LLMs*, arXiv:2411.15100; XGrammar-2 (MLC blog, 2026-05-04; arXiv:2601.04426).
- ToolLibGen — *Scalable Automatic Tool Creation and Aggregation for LLM Reasoning*, arXiv:2510.07768.
- *Tool Learning in the Wild / AutoTools*, arXiv:2405.16533.
- *Rethinking Verification for LLM Code Generation: From Generation to Testing*, arXiv:2507.06920.
- *Large Language Models for Unit Test Generation: Achievements, Challenges, and Opportunities*, arXiv:2511.21382 (34–62% of generated tests invalid).
- *Consistency Meets Verification: Enhancing Test Generation Quality Without Ground-Truth Solutions*, arXiv:2602.10522.

> Provenance note: the assistant did **not** independently re-verify the arXiv IDs above (several are 2026 preprints). They are reproduced from the source roadmap, which states it re-checked them. Treat as author-attributed, not assistant-verified.

### 8.2 Additionally verified by the assistant this session (re-checked on the web, 2026-06-22)
- **AFLOW — Automating Agentic Workflow Generation**, [arXiv:2410.10762](https://arxiv.org/pdf/2410.10762) (ICLR 2025). Workflows as graphs of LLM nodes; reusable **Operators** (Ensemble, Review&Revise); MCTS search.
- **A²Flow — Automating Agentic Workflow Generation via Self-Adaptive Abstraction Operators**, [arXiv:2511.20693](https://arxiv.org/html/2511.20693v1). Auto-extracts abstraction operators from examples.
- **ADAS — Automated Design of Agentic Systems** ([preprint review](https://www.preprints.org/frontend/manuscript/681d95e4c67e8f1c7370bbc8d39f887a/download_pub)). Meta-agent programs new agent architectures.
- **Anthropic — Building Effective AI Agents**, [anthropic.com/research/building-effective-agents](https://www.anthropic.com/research/building-effective-agents). The five workflow patterns; "start simple."
- **SELF-[IN]CORRECT: LLMs Struggle with Discriminating Self-Generated Responses**, [AAAI](https://ojs.aaai.org/index.php/AAAI/article/view/34603/36758).
- **LLMs-as-Judges: A Comprehensive Survey**, [arXiv:2412.05579](https://arxiv.org/pdf/2412.05579); and [*A survey on LLM-as-a-judge*, ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2666675825004564). Self-judging blind spots; "insufficient without orchestration."
- **LLM Agents for Biomedicine: A Comprehensive Review**, [MDPI Information 16(10):894](https://www.mdpi.com/2078-2489/16/10/894); **LLM agents for biological intelligence**, [Briefings in Bioinformatics](https://academic.oup.com/bib/article/27/2/bbag110/8540361). Multistep hallucination; tool-reliability bottleneck; KG/verification/provenance guardrails.
- **Automating AI Discovery for Biomedicine Through Knowledge Graphs and LLM Agents**, [bioRxiv 2025.05.08.652829](https://www.biorxiv.org/content/10.1101/2025.05.08.652829.full.pdf).
- **SafeScientist: Toward Risk-Aware Scientific Discoveries by LLM Agents**, [arXiv:2505.23559](https://arxiv.org/pdf/2505.23559).

### 8.3 Established prior work (cited by name/venue; not re-verified this session)
- Reflexion (Shinn et al., 2023); Tree of Thoughts (Yao et al., 2023); Self-Consistency (Wang et al., 2023); ReAct (Yao et al., 2023).
- Toolformer (Schick et al., 2023); Gorilla / ToolLLM (2023); Voyager skill library (Wang et al., 2023); "LLMs as Tool Makers" (LATM).
- FAIR Guiding Principles (Wilkinson et al., *Scientific Data*, 2016).
- Scientific workflow standards: Common Workflow Language; Nextflow (Di Tommaso et al., 2017); Galaxy / Interactive Workflow Composer.

### 8.4 Internal sources (this repo, consulted for the capability map and prior decisions)
`docs/external_orchestration_design.md`, `docs/implementation_task_graph.md`, `docs/reasoning_patterns_analysis_2026-05-17.md`, `docs/composer_codegen_uplift_findings.md` (F1/F10/F39), `docs/papers_vs_our_scaffolds_analysis.md`, `eval_02_brutal_panel.md`, and the `composition/` source tree (`composer.py`, `workflow_validator.py`, `component_catalog.py`, `differ.py`, `skeletons/`, `workflows/`), plus nanobrain `library/tools/rhea_step_synthesizer.py`, `library/steps/{subworkflow_step,map_subworkflow_step,rhea_file_tool_step}.py`, and `core/{workflow_graph,workflow_validation}.py`. Where internal docs cite 2026 arXiv IDs that are load-bearing here, the assistant re-verified a complementary set in §8.2 rather than trusting the internal citation.

### 8.5 [original — scrutinise] — this document's own synthesis (no single external precedent; challenge before adoption)
The four-layer mandatory-validator stack as specified; the auto-constructed per-workflow validator derived from planner-emitted acceptance criteria; the name-based reasoning-pattern binding seam (Rank 1c); the on-demand-synthesis-with-git-persistence design for Rhea steps (§0/Rank 3); the original-composition provenance ledger with staged promotion (R7); and the specific R1→R9 rank ordering. These are reasoned from the sources above but have no single external precedent.
