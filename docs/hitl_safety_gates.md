# HITL, Safety, and Cost Gates

**Status:** Draft v1 — design contract for the gate surface
**Owners:** apecx-mcp-integration core
**Audience:** orchestrator agent authors, control-plane operators, capability admins, data-protection officers
**Last updated:** 2026-05-08

---

## 1. Why this document exists

Today the apecx-mcp-integration HITL surface is functionally correct but informally specified. `architecture.md §4.5` lists four approval MCP tools (`list_pending_approvals`, `approve`, `reject`, `correct`) and `§4.6` lists four HPC tools (`estimate_cost`, `confirm_allocation`, `export_hpc_bundle`, `ingest_hpc_bundle`). `multiagent_architecture.md §4` describes Tier 0 as the place where "HITL gates for long-running or expensive operations" surface. `agent_workflow_authoring.md §2.3` mentions that Strategy B and C plans require an HITL gate before any lowered YAML reaches an executor. None of these documents say:

- **Which** gates fire for which authoring strategy, intent, or executor target.
- **When** in the lifecycle they fire (before authoring completes, before execution starts, mid-execution, after execution).
- **What** the approver sees — the structured payload behind the existing `show_diff` tool.
- **Who** approves — operator, scientist, capability admin, data-protection officer.
- **What** the audit record contains and how it composes with the provenance chain (`hpc_reproducibility_spec.md §11` — referenced; canonical doc forthcoming).

The gap is not academic. Without a formal gate contract, agent-authored workflows can fail in two opposite directions:

1. **Over-blocking.** Every step pauses for approval, latency explodes, the operator stops reading payloads carefully, approval becomes rubber-stamp. Rubber-stamp approvals are worse than no approval — they create a false audit signal.
2. **Under-blocking.** The orchestrator emits a Strategy C plan that a capable LLM has no business shipping unsupervised, the executor accepts it because the YAML parses, the operator sees only the final bundle and an aggregate cost. Cost-blind execution and silent capability escalation both live here.

This document defines the gate taxonomy, the eleven concrete gates the v1 system needs, the approver's view for each, the lifecycle, the audit shape, and the opinionated default policies that set the safety floor for new deployments. It is the design contract that `agent_workflow_authoring.md`, `tool_descriptor_contract.md`, and `external_tool_integration.md` already cross-reference.

This document is **not** an implementation plan. It does not pick a control-plane database engine, a polling interval for `list_pending_approvals`, or a wire format for the audit log. Those are deployment decisions; the contract is identical across them.

---

## 2. Gate taxonomy — five categories

Every gate in the system belongs to exactly one of five categories. The category determines **when** in the workflow lifecycle the gate fires, which determines what evidence the approver can see and what the orchestrator can do with the decision. The five categories, in lifecycle order:

| # | Category | Fires | Cancel cost if rejected | Typical approver |
|---|---|---|---|---|
| 1 | **Authoring** | Before workflow YAML reaches an executor | Zero (no compute consumed) | Operator |
| 2 | **Resource** | Before execution starts (after authoring is approved) | Zero (no compute consumed) | Operator |
| 3 | **Capability** | Before a step that needs a privileged capability runs | Zero or partial (prior steps already ran) | Capability admin |
| 4 | **Decision** | Mid-execution at user-defined checkpoints | Partial (upstream work preserved) | Scientist |
| 5 | **Post-execution** | After execution, before result returns to MCP client | Full (compute already spent) | Scientist or data-protection officer |

The categories are not arranged by severity — they are arranged by recoverability. Authoring gates are the cheapest to reject and the most likely place to catch design problems. Post-execution gates exist because some classes of error (PHI leakage, fabricated citations, broken provenance) cannot be detected statically and must be checked against the realized output. The designer should push gates as early in the lifecycle as the trigger allows: a cost gate that fires mid-execution wastes the operator's attention on already-committed compute.

A single workflow run will, in practice, fire two to four gates. A simple Strategy A retrieval-only run typically fires zero (Strategy A skeletons are pre-approved, costs are bounded, capabilities are default). A Strategy C HPC-bundle run fires GATE-A1, GATE-R1, GATE-R2, GATE-R3, and GATE-P2. Section §6 specifies the default policies that set this baseline.

### 2.1 Authoring gates

**Trigger window:** between the orchestrator agent emitting an `ExecutionPlan` (`agent_workflow_authoring.md §3`) and the lowering pipeline producing the executable workflow YAML. The plan is JSON; the lowered output is YAML; the gate sees both.

**Why authoring gates are special.** The authoring boundary is the only place where the LLM's output is fully visible as data, not as side effects. Once the YAML is handed to a `Workflow.from_config()` runtime, the only signal of an unsafe plan is whatever the framework's static FAIL-FAST checks catch — and those checks are necessary but not sufficient (`architecture.md §13` brutal-truth #3 documents four silent-failure shapes that pass every static check). Authoring gates are the system's last fully-observable checkpoint before the trust boundary inverts and the framework starts executing on faith.

### 2.2 Resource gates

**Trigger window:** after authoring is approved (or skipped) and before any executor begins running steps. The cost estimator has already produced rolled-up estimates from each `tool_invocation`'s `cost_estimate` (`tool_descriptor_contract.md §2.6`) and from LLM-call accounting; the executor selector has chosen `local`, `parsl_local`, or `parsl_hpc`.

**Why resource gates fire here.** The control plane has all the information it needs: a finalized plan, a known executor, a population of historical actuals. Firing earlier (during authoring) means estimating against a plan that may still change; firing later (mid-execution) means the operator's choice is constrained by sunk-cost. The window between authoring and dispatch is the unique moment where the cost is known and zero compute has been spent.

### 2.3 Capability gates

**Trigger window:** immediately before a step that requires a privileged capability dispatches its tool. Capability tokens are defined in §7. A capability gate is *not* the same as a resource gate even when the same step would trip both — capability gates are role-gated (a different approver, often distinct from the operator), and they cannot be globally lowered (the cost gate's threshold can be raised per-user; a capability token cannot).

**Why fire just-in-time.** Capability tokens can be granted, denied, or revoked between authoring and execution (a session-bounded token may expire mid-workflow). Validating capability at authoring time and again at dispatch time is a deliberate two-check: authoring rejects plans whose holder will *never* have the capability; dispatch rejects when the capability was held at authoring but was lost since (token expired, session changed, role downgraded).

### 2.4 Decision gates

**Trigger window:** at user-declared checkpoints inside an executing workflow. These are the only gates the orchestrator agent can declare in its ExecutionPlan; the others are policy-enforced and not opt-in. Decision gates are how a workflow author says "stop here, ask the human, then continue with their input."

**Examples.** After a retrieval phase but before LLM synthesis (so the operator can prune evidence). After a hypothesis tournament but before HPC submission (so the scientist can reject all top-N and request a re-run with feedback). After a Phase 0 dry-run produces tool invocations but before any actually dispatch.

**Distinguishing feature.** Decision gates have a non-trivial *upstream cost*. Rejecting one does not avoid the work that produced its payload — that work is already done. Decision gates trade pre-execution latency for a chance to save downstream cost when the upstream output looks wrong. They are the most expensive gates to over-use.

### 2.5 Post-execution gates

**Trigger window:** after the workflow's terminal step has produced an output, before that output is returned to the MCP client. Post-execution gates check policy invariants that depend on the realized output — the kind of check a pre-execution validator cannot perform because the output does not yet exist.

**The two failure shapes.** Post-execution gates either (a) reject the output and require re-synthesis or human correction (a hard policy violation, e.g., PHI in the response), or (b) attach a flag to the output and pass it through anyway (a soft warning, e.g., partial provenance). Both shapes are recorded in the audit log, so a downstream consumer of the result can see whether the bundle was clean, warned, or post-hoc corrected.

---

## 3. Gate catalog

The eleven gates below cover the v1 surface. Each is documented with the same uniform sub-section template:

- **ID** — stable, used in audit records and cross-references.
- **Category** — one of the five from §2.
- **Trigger condition** — exactly when the gate fires.
- **Blocking semantics** — `hard-block`, `timeout-default-deny`, `timeout-default-approve`, or `soft-block` (advisory; record the warning, do not stop the workflow).
- **Approver** — the role allowed to decide.
- **What the approver sees** — the structured payload (the data behind `show_diff`).
- **Decision options** — approve, reject, correct (where applicable).
- **Audit record** — the additional fields recorded beyond the standard envelope (§9).
- **Failure handling** — what happens if the approver rejects, corrects, or times out.
- **Open questions** — known unresolved design questions.

### 3.1 Overview table

| ID | Category | Trigger | Blocking | Approver |
|---|---|---|---|---|
| GATE-A1 | Authoring | `strategy ∈ {B, C}` ExecutionPlan emitted | hard | Operator |
| GATE-A2 | Authoring (capability pre-check) | `tool_invocation.requires_capability` not held by user | hard | Capability admin |
| GATE-R1 | Resource | rolled-up cost > per-user threshold | hard | Operator |
| GATE-R2 | Resource | expected walltime > per-executor cap | hard | Operator |
| GATE-R3 | Resource | `hpc_eligible: true` but a step disqualifies HPC | hard | Operator |
| GATE-C1 | Capability (PHI / restricted egress) | restricted-data source + `side_effects: network` | hard, two-key | Data-protection officer |
| GATE-D1 | Decision | optional pre-execution dry-run | timeout-default-deny | Operator |
| GATE-D2 | Decision | post-tournament HITL | timeout-default-deny | Scientist |
| GATE-D3 | Decision | layer step emits `CapabilityGap` | soft (continue without layer if accepted) | Scientist |
| GATE-P1 | Post-execution | output policy violations or warnings | hard on violation, soft on warning | Scientist or DPO |
| GATE-P2 | Post-execution | EvidenceBundle provenance integrity | soft | Scientist |

### 3.1.1 Gate behavior under autonomy modes

`autonomous_workflow_agent.md §3` introduces an `autonomy_level` policy axis
(`strict_hitl` / `opt_in_hitl` / `pure_autonomous`) that modulates each gate's
blocking semantics. The matrix below pins which gates may auto-resolve under
which autonomy mode. **Hard-floor gates** are never auto-resolved regardless
of autonomy mode — these are the gates whose entire purpose is to require a
human signature.

| Gate | strict_hitl | opt_in_hitl | pure_autonomous |
|---|---|---|---|
| GATE-A1 (authoring elevation) | hard-block | timeout-default-approve (24h) | timeout-default-approve (configurable to 0s) |
| GATE-A2 (capability pre-check) | **hard-floor** | **hard-floor** | **hard-floor** |
| GATE-R1 (cost approval) | hard-block | timeout-default-approve when within 80% of envelope; hard-block above | timeout-default-approve within envelope; hard-fail above (no auto-extend without HITL) |
| GATE-R2 (walltime) | hard-block | timeout-default-approve | timeout-default-approve |
| GATE-R3 (HPC eligibility) | hard-block | timeout-default-approve | timeout-default-approve |
| GATE-C1 (PHI / restricted egress) | **hard-floor** | **hard-floor** | **hard-floor** |
| GATE-D1 (pre-tool-execution dry-run) | timeout-default-deny | timeout-default-approve | skipped |
| GATE-D2 (tournament-result HITL) | timeout-default-deny | timeout-default-approve | timeout-default-approve |
| GATE-D3 (capability-gap) | soft (current) | soft (current) | soft (current) |
| GATE-P1 (output policy) | hard on violation | hard on violation; soft on warning may auto-acknowledge | **hard-floor when classification = `redact`** ; soft otherwise |
| GATE-P2 (provenance integrity) | soft (current) | soft (current) | soft (current) |

**Hard-floor reading.** A hard-floor gate blocks indefinitely until a human
decides; the autonomous task transitions to `paused` (per
`autonomous_workflow_agent.md §5.1`) and waits. The autonomy mode does NOT
reduce the gate's strictness. The justification per gate:

- **GATE-A2 (capability pre-check):** A capability the user does not hold
  cannot be auto-granted. Auto-resolving would silently elevate the agent's
  scope.
- **GATE-C1 (PHI / restricted egress):** Compliance review is a regulatory
  requirement. A "DPO is asleep" is not a justification for unreviewed PHI
  egress.
- **GATE-P1 with classification `redact`:** The output classifier flagged
  content for redaction. Auto-resolving the redaction means publishing
  unredacted content. The gate's purpose is to require a redactor signature.

**Configurable timeout windows under autonomy.** Default windows from §11
apply when `autonomy_level = strict_hitl`. Under `opt_in_hitl` and
`pure_autonomous`, the per-task config may override windows down to a floor
of 60s (preventing accidental "fire and forget instantly" misconfigurations).
A task config that requests a 0s window is rejected at task creation unless
the operator also has `instant_autonomy_admin` capability (a token reserved
for genuinely fire-and-forget batch workloads).

**Audit obligation.** Every auto-resolved gate (timeout-default-approve under
opt_in_hitl or pure_autonomous) writes an audit record per §10 with
`decision: timeout_approve_autonomous` and the autonomy_level that licensed
the auto-resolution. Hard-floor gates that paused a task and were eventually
decided record `decision: human_approve` regardless of mode.

### 3.2 GATE-A1 — Authoring strategy elevation gate

- **Category:** Authoring.
- **Trigger condition:** the orchestrator emits an `ExecutionPlan` whose `strategy` field is `skeleton_compose` (B) or `yaml_synthesis` (C). Strategy A plans skip this gate by construction.
- **Blocking semantics:** hard-block. The lowering pipeline stops; the lowered YAML is computed for review but is not handed to any executor until the gate decides.
- **Approver:** workflow operator (the user who initiated the run, by default; see §6 for role overrides).
- **What the approver sees:** `{plan_diff, lowered_yaml, validation_report, risk_summary}`. `plan_diff` is a JSON Patch (RFC 6902) against the most recently approved plan in the same session, or against the empty document if this is the session's first plan. `lowered_yaml` is the *exact* YAML that will execute — not a sketch, not a summary; if the operator approves, this is what runs. `validation_report` is the output of validation gates 1–5 from `agent_workflow_authoring.md §6` (catalog membership, link compatibility, trigger reachability, executor capability, capability-token possession). `risk_summary` is a short LLM-generated paragraph that is **advisory only** — the operator must read the YAML, not the risk summary.
- **Decision options:** approve, reject, correct. `correct` accepts a JSON Patch from the operator that is applied to the ExecutionPlan; the lowering pipeline re-runs and the gate re-fires until either approved or rejected. A correction that changes `strategy` from C to B (or B to A) is allowed and lowers the gate strictness on re-fire.
- **Audit record:** `plan_hash`, `lowered_yaml_hash`, `prior_plan_hash` (or null), `correction_patch` (if any), `validation_report_hash`.
- **Failure handling:** rejection terminates the workflow; the agent is informed and may attempt re-authoring with the rejection's `justification` as feedback (subject to the open question in §12 on whether rejection context propagates).
- **Open questions:** does the LLM see `correction_patch` on retry, or only the rejection justification? When B becomes A on correction, does the audit record both strategies or only the executed one?

### 3.3 GATE-A2 — Tool capability gate (pre-execution)

- **Category:** Authoring (capability pre-check; the just-in-time pair is described per-step at dispatch).
- **Trigger condition:** the ExecutionPlan declares a `tool_invocation` whose UTD has a `requires_capability` token (`tool_descriptor_contract.md §2.1`, §6) that the requesting user does not hold.
- **Blocking semantics:** hard-block.
- **Approver:** capability admin. This is a deliberately distinct role from operator: granting `aurora_compute_credits` or `phi_data_access` is an administrative act, not an operator decision.
- **What the approver sees:** `{user_id, missing_token, missing_token_description, granting_authority, requested_scope, requested_expiry, justification_from_user, prior_grants, plan_summary}`. `plan_summary` is a one-paragraph view of the plan, not the full YAML — the capability admin is approving a token grant, not the workflow.
- **Decision options:** approve (issue token with declared scope and expiry), reject, correct (grant a narrower token — e.g., scope to a single descriptor instead of all members of a token class).
- **Audit record:** `token_id`, `token_class`, `scope`, `expiry`, `granting_authority_id`, `prior_grant_count`.
- **Failure handling:** rejection terminates the workflow. A rejected GATE-A2 does *not* record a `plan_hash` failure on the orchestrator's record — the plan was authoring-correct; the user simply lacked the capability. This distinction matters for the rolling drift telemetry in §8.
- **Open questions:** when the same `missing_token` would be needed by multiple `tool_invocations`, does the gate fire once (token grant covers all) or per-invocation (each requires its own scope)? Default in v1: once.

### 3.4 GATE-R1 — Cost approval gate

- **Category:** Resource.
- **Trigger condition:** rolled-up cost estimate (sum of per-`tool_invocation` `cost_estimate` from UTDs, plus LLM-call cost from the orchestrator's accounting, plus executor overhead) exceeds the per-user `cost_threshold`. Default threshold: `$10/run` for the scientist role (see §6); operators can raise per-session, capability admins set the per-user ceiling.
- **Blocking semantics:** hard-block.
- **Approver:** operator.
- **What the approver sees:** `{cost_breakdown_by_step, total_estimate, rolling_actual_p95, threshold, user_month_spend, user_month_threshold, descriptor_drift_flags}`. `cost_breakdown_by_step` is an ordered list of `{step_id, descriptor_id, p50, p95, source}`. `rolling_actual_p95` is the historical p95 of *actual* costs for the same descriptor mix (not the same plan — plans are unique; the descriptor mix is the comparable unit); when historical data is sparse the field is null and a `low_confidence_estimate` flag is raised. `user_month_spend` and `user_month_threshold` together let the operator see budget runway, not just per-run cost. `descriptor_drift_flags` highlights any descriptor whose recent actuals exceed catalog `cost_estimate.cpu_seconds_p95` by more than 2× (see §8).
- **Decision options:** approve, reject, correct (apply a hint that asks the orchestrator to re-bundle with a cheaper executor or a smaller resource envelope; the gate then re-fires with the new estimate).
- **Audit record:** `total_estimate`, `breakdown_hash`, `threshold_at_decision`, `user_month_spend_at_decision`, `correction_hint` (if any).
- **Failure handling:** rejection terminates the run. Timeout default-deny: a cost gate that times out is treated as rejection (the converse — silent approval of an over-budget run — is the failure mode that motivated this whole document).
- **Open questions:** should the cost estimator account for the LLM tokens spent on retry-after-rejection? Should it amortize cached-result reuse against historical actuals? v1: yes to the first, no to the second.

### 3.5 GATE-R2 — Walltime gate

- **Category:** Resource.
- **Trigger condition:** the expected walltime (max of any per-step walltime, plus serial-dependency walltime over the longest path) exceeds the configured cap for the targeted executor. Caps in v1: `LocalExecutor: 10 min`, `Parsl LocalProvider: 1 h`, `Aurora PBS: 24 h`, `Polaris PBS: 12 h`.
- **Blocking semantics:** hard-block.
- **Approver:** operator.
- **What the approver sees:** `{expected_walltime, walltime_breakdown_by_step, executor_cap, executor_id, downgrade_options, queue_state_estimate}`. `downgrade_options` enumerates alternative executors that *would* fit (e.g., "if you accept 4× latency, this run fits the LocalProvider cap"). `queue_state_estimate` is a hint from the executor's queue model — an Aurora PBS run with a 6-hour estimated wait time may be acceptable, an 18-hour estimated wait probably is not.
- **Decision options:** approve (accept the cap miss, upgrade to a higher-tier executor if available), reject, correct (`downgrade_executor` — re-bundle for a lower-cost executor at the cost of latency).
- **Audit record:** `expected_walltime`, `executor_at_decision`, `downgrade_chosen` (if any), `queue_state_estimate`.
- **Failure handling:** rejection terminates. Timeout default-deny.
- **Open questions:** should walltime gate fire on the *p50* or *p95* estimate? v1: p95 (operator decides on the worst plausible case). Should queue wait time count toward walltime for the cap comparison? v1: no, but it is shown to the operator.

### 3.6 GATE-R3 — HPC eligibility gate

- **Category:** Resource.
- **Trigger condition:** the ExecutionPlan declares `hpc_eligible: true` (i.e., the operator intends to export an HPC bundle for offline submission), and at least one step has a property that disqualifies HPC: a UTD with `side_effects: network` (Aurora compute nodes have no egress), a step requiring a model digest that is not pre-staged on the target machine, or a `tool_invocation` whose bundle export is forbidden by the descriptor.
- **Blocking semantics:** hard-block.
- **Approver:** operator.
- **What the approver sees:** `{disqualifying_steps, reason_per_step, alternative_executors, can_strip_step}`. `can_strip_step` is per-step boolean — some disqualifying steps can be removed without breaking the workflow's contract (e.g., a PubMed enrichment step is optional); others cannot.
- **Decision options:** approve (override — only available if the operator has the `hpc_override` token, which is not granted by default), reject, correct (strip the disqualifying step, or re-target a non-HPC executor and re-run §3.4 / §3.5).
- **Audit record:** `disqualifying_steps`, `decision`, `corrected_target` (if any).
- **Failure handling:** rejection terminates. The orchestrator may re-author with `hpc_eligible: false`.
- **Open questions:** can a workflow declare a step "HPC-skippable" in the plan so the gate can correct automatically? v1: no, operator must opt in per run.

### 3.7 GATE-C1 — PHI / restricted-data egress gate

- **Category:** Capability (PHI / restricted egress).
- **Trigger condition:** the workflow touches a data source classified as `restricted` (PHI, embargoed publications, vendor-proprietary corpora) AND a step in the same workflow has `side_effects: network`. The conjunction matters: PHI-touching workflows that are network-sealed are fine; network-using workflows that touch only public data are fine; both together is the failure mode.
- **Blocking semantics:** hard-block, two-key. The operator cannot override; only the data-protection officer (DPO) role can approve.
- **Approver:** data-protection officer. This is a deliberate two-key control: a single role cannot both submit a workflow and authorize its egress.
- **What the approver sees:** `{restricted_sources, network_steps, network_destinations, justification_from_user, retention_policy_summary, prior_grants_for_user}`. `network_destinations` is the set of remote hostnames the network-using steps will contact; if a step's destination is dynamic (LLM-decided), the descriptor must declare a destination *class* (e.g., `pubmed.ncbi.nlm.nih.gov`) and the gate refuses to approve a star-class.
- **Decision options:** approve (one-shot per workflow run; never persisted across runs), reject, correct (narrow the egress destinations).
- **Audit record:** `restricted_sources_hash`, `destinations_hash`, `dpo_id`, `retention_policy_at_decision`. Audit records for GATE-C1 carry an extra retention flag — they are retained *longer* than baseline (see §9).
- **Failure handling:** rejection terminates. Timeout default-deny *with escalation*: if the DPO does not respond within the configured window, the gate notifies a configured backup DPO; if no DPO responds, the workflow terminates and an incident record is filed.
- **Open questions:** does GATE-C1 fire per-step or per-workflow-run? v1: per-run. Should a session that has approved GATE-C1 keep that approval across follow-up workflows in the same conversational chain? v1: no — re-auth per workflow.

### 3.8 GATE-D1 — Pre-tool-execution dry-run gate

- **Category:** Decision.
- **Trigger condition:** opt-in. After Phase 0 produces `tool_invocations` (`agent_workflow_authoring.md §3.1`), the operator can request a "show me what you would run" payload before any actual tool dispatch. Default off for low-cost runs (rolled-up estimate < 25% of GATE-R1 threshold), default on for HPC-eligible runs.
- **Blocking semantics:** timeout-default-deny.
- **Approver:** operator.
- **What the approver sees:** `{tool_invocations, bound_inputs, expected_outputs, side_effects_per_step, descriptor_versions}`. `bound_inputs` is the resolved input data — references where the value is large, inline where it is small. The operator can see *what* would be sent to each tool.
- **Decision options:** approve, reject, correct (reject specific invocations; the orchestrator re-authors without them).
- **Audit record:** `invocations_hash`, `decision_per_step` (if `correct`).
- **Failure handling:** rejection terminates. A correction that removes a required step terminates with an actionable message.
- **Open questions:** should GATE-D1 default to on for *all* Strategy C runs, regardless of cost? v1: no — Strategy C runs already pass through GATE-A1 which shows the YAML.

### 3.9 GATE-D2 — Tournament-result HITL gate

- **Category:** Decision.
- **Trigger condition:** a `HypothesisTournamentStep` (`multiagent_architecture.md §8.2`, `nanobrain_workflow_design.md §3.5`) produces a top-N hypothesis ranking. The gate fires before the workflow continues to whatever step consumes the ranking (typically `HITLGateStep` followed by `ResponseSynthesisStep` or HPC bundle export).
- **Blocking semantics:** hard-block (the consuming step has no useful default behavior without a chosen hypothesis).
- **Approver:** scientist.
- **What the approver sees:** `{top_n_hypotheses_with_evidence, ranking_confidences, proposer_agents, judge_model_id, judge_prompt_hash}`. Each hypothesis includes the supporting evidence (with source IDs into the EvidenceBundle), the proposing agent's identity (which orchestrator generated this hypothesis), and the LLM-judged ranking confidence (a calibrated probability, not a raw logit). `judge_model_id` and `judge_prompt_hash` make the ranking reproducible.
- **Decision options:** approve (pick a single hypothesis; the chosen one becomes `ApprovedHypothesis`), reject (reject all top-N — the workflow re-runs the tournament with the rejection feedback as additional context, per `reasoning_patterns_library.md P7`), correct (pick a subset, ask the workflow to merge or pick the strongest single hypothesis from the subset).
- **Audit record:** `chosen_hypothesis_id`, `tournament_size`, `judge_model_id`, `judge_prompt_hash`, `feedback_text` (if reject).
- **Failure handling:** rejection re-runs the tournament with feedback context; after a configured retry cap (v1: 2), the workflow terminates and surfaces the unresolved tournament to the user.
- **Open questions:** how is the rejection feedback prompt-engineered into the next tournament? Free-text vs. structured constraints? v1: structured constraints preferred; free-text fallback.

### 3.10 GATE-D3 — Capability-gap gate

- **Category:** Decision (soft).
- **Trigger condition:** a layer step emits a `CapabilityGap` record (`workflow_output_contract.md §4.4`, `reasoning_patterns_library.md P9`). A gap means the layer could not produce findings because a required capability is missing — a data source is unreachable, a tool descriptor is unavailable, an external service is degraded.
- **Blocking semantics:** soft-block (the workflow can continue without the gap-affected layer if the user accepts; the final response carries the gap declaration explicitly).
- **Approver:** scientist.
- **What the approver sees:** `{gap_layer_id, gap_reason, gap_evidence, downstream_impact, can_continue_without}`. `downstream_impact` says which downstream steps consume this layer's output and what they will produce on the empty-bundle path (cross-references the failure contract from `workflow_output_contract.md §4.4`).
- **Decision options:** continue (accept the gap; final response will declare it), wait (pause; the operator will retry the gap-affected step manually after fixing whatever caused the gap), abort.
- **Audit record:** `gap_layer_id`, `gap_reason_code`, `decision`.
- **Failure handling:** abort terminates. Wait holds the workflow open for a configured window; on window expiry the gate downgrades to abort and terminates.
- **Open questions:** can `wait` be infinite, or must it have a window? v1: must have a window (control-plane resources are finite).

### 3.11 GATE-P1 — Output policy gate

- **Category:** Post-execution.
- **Trigger condition:** the workflow's terminal step has produced an output (typically Markdown synthesis from `RagSynthesisStep` or a structured `FinalResponse`). The output is run through the policy validator: PHI detection, provider-name leakage detection, fabricated-citation detection (every cited reference must resolve to a record in the EvidenceBundle).
- **Blocking semantics:** hard-block on policy *violation*; soft-block on *warning*. The split is deliberate: a violation is "this output cannot ship"; a warning is "this output ships, but with a flag".
- **Approver:** scientist (warnings) or data-protection officer (violations).
- **What the approver sees:** `{output_excerpt, violation_class, violation_evidence, suggested_redaction, policy_version}`. `violation_evidence` is the specific span of the output that tripped the policy (offset + length + matched pattern); `suggested_redaction` is a candidate corrected output that can be approved with one click in the `correct` flow.
- **Decision options:** approve (ship as-is — only available for warnings), reject (force re-synthesis with the violation as feedback), correct (apply the suggested redaction or a manual edit).
- **Audit record:** `violation_class`, `violation_evidence_hash`, `redaction_applied` (boolean), `redaction_diff_hash`.
- **Failure handling:** rejection re-runs synthesis once with the violation context; after that, the workflow terminates and the operator must re-author.
- **Open questions:** how does the policy validator avoid false positives that block legitimate use of restricted terms (e.g., a vaccine name that happens to match a provider name)? v1: term-list with allowlist exceptions; exceptions are themselves audited.

### 3.12 GATE-P2 — Provenance integrity gate

- **Category:** Post-execution.
- **Trigger condition:** every output cell in the EvidenceBundle is checked against its declared provenance chain (`hpc_reproducibility_spec.md §5` — canonical doc forthcoming; the contract today is "every external-tool result records `descriptor_id@version`, `inputs_hash`, `container_digest`/`executable_digest`, and a `bundle_id` linking to upstream provenance"). The gate fires when one or more provenance chains are incomplete.
- **Blocking semantics:** soft-block.
- **Approver:** scientist.
- **What the approver sees:** `{incomplete_cells, missing_fields_per_cell, downstream_replay_impact}`. `downstream_replay_impact` says whether the missing provenance prevents replay of the bundle on a different machine; sometimes it does, sometimes the missing fields are decorative.
- **Decision options:** ship (output goes to the user with a `non_reproducible` flag attached), block (refuse to return the bundle; surface the missing-provenance error and end the run), repair (operator supplies the missing fields manually — only available for fields the operator can plausibly know, e.g., a forgotten `descriptor_id` for a hand-invoked tool).
- **Audit record:** `incomplete_cell_ids`, `decision`, `repair_supplied` (boolean), `non_reproducible_flag_set` (boolean).
- **Failure handling:** block terminates the run; ship returns the output with the flag.
- **Open questions:** should `non_reproducible` propagate transitively to downstream consumers of this bundle? v1: yes, the flag is sticky.

---

## 4. Approval flow lifecycle

Every gate, regardless of category, goes through the same lifecycle. The orchestrator emits a candidate, the control plane runs gate evaluators in declared order, the first failing evaluator produces an `Approval` record, the MCP client polls for pending approvals, the approver decides, the control plane records the decision, the orchestrator resumes (or terminates).

> **Framework grounding (audit U-3).** The lifecycle is implemented through nanobrain's existing `ApprovalStep` (referenced from `apecx-mcp-integration/CLAUDE.md`; nanobrain primitive). Each gate is an `ApprovalStep` instance; the gate's payload schema, approver-role policy, and decision options are configuration on top of the primitive — not a new framework concept. Cross-reference `nanobrain_alignment_audit.md §3.6 C-35` and `§4.3 U-3`.

```mermaid
sequenceDiagram
    participant Agent as Orchestrator Agent
    participant Orch as Workflow Orchestrator
    participant CP as Control Plane
    participant MCP as MCP Surface
    participant User as Approver (operator/scientist/admin/DPO)

    Agent->>Orch: ExecutionPlan + lowered YAML
    Orch->>Orch: Run authoring gate evaluators in order
    alt Gate fires
        Orch->>CP: emit Approval record (gate_id, payload, plan_hash)
        CP-->>Orch: ack (run paused)
        loop Approver polls
            MCP->>CP: list_pending_approvals(user_id)
            CP-->>MCP: [Approval]
        end
        User->>MCP: approve / reject / correct (justification)
        MCP->>CP: record decision
        CP->>Orch: notify decision
        alt approved
            Orch->>Orch: continue lifecycle (next gate or dispatch)
        else rejected
            Orch->>Agent: terminate with rejection context
        else corrected
            Orch->>Agent: re-author with patch; restart lifecycle
        end
    else No gate fires
        Orch->>Orch: dispatch next phase
    end
    Note over CP,User: timeout policy: default-deny;<br/>per-gate window in §11
```

**Lifecycle invariants.**

1. **Sequential evaluation.** Gates within a category fire in the declared order from §3 (A1 before A2, R1 before R2 before R3, etc.). The first failing gate emits an Approval record; subsequent gates do not fire until the failing gate is resolved. This is how the operator avoids being asked to approve a cost gate for a plan that will be rejected by an HPC eligibility gate.
2. **One pending Approval per run.** A workflow run has at most one pending Approval at any time. This is a deliberate simplification — the alternative (parallel approvals across categories) is operationally messy and the cross-category gates almost always have data dependencies anyway.
3. **Default-deny on timeout.** Every gate has a configured window. On window expiry the Approval is rejected and the workflow terminates. This is the global default; specific gates can opt into `timeout-default-approve` only when both (a) the cost of false-deny is high *and* (b) the cost of false-approve is bounded — none of the v1 gates qualify, so all are default-deny.
4. **Idempotent decisions.** A submitted decision is final. The MCP surface rejects a second decision on the same Approval ID. Audit records capture the first decision; subsequent attempts are logged as `duplicate_decision` events but do not change state.

---

## 5. The approver's view — payloads in detail

The four approval MCP tools (`list_pending_approvals`, `approve`, `reject`, `correct`) and the `show_diff` tool form the entire approver UI. The payload schema returned by `show_diff` is gate-specific, but every payload shares an envelope:

**Approval envelope.** Every payload starts with the same fields:

| Field | Type | Description |
|---|---|---|
| `approval_id` | string (uuid) | Stable handle for `approve` / `reject` / `correct` calls |
| `gate_id` | string | One of `GATE-A1` … `GATE-P2` |
| `gate_category` | enum | `authoring` / `resource` / `capability` / `decision` / `post_execution` |
| `run_id` | string (uuid) | The workflow run this gate belongs to |
| `plan_hash` | string (sha256) | Content hash of the ExecutionPlan that produced this gate |
| `created_at` | ISO 8601 | Wall-clock time the Approval was created |
| `expires_at` | ISO 8601 | When the timeout policy will fire |
| `requesting_user` | string | The user the workflow run belongs to |
| `eligible_approvers` | array of string | Roles that may decide this gate |
| `payload` | object | Gate-specific structured payload (the rest of this section) |

**GATE-A1 payload.**

| Field | Type | Description |
|---|---|---|
| `plan_diff` | array of JSON Patch ops | Diff against the most recently approved plan in this session (or against `{}` if first) |
| `lowered_yaml` | string | The exact YAML that will execute on approval |
| `lowered_yaml_hash` | string (sha256) | Content hash of `lowered_yaml` |
| `validation_report` | object | `{gate_1: pass\|fail, gate_2: …, …, gate_5: …}` per `agent_workflow_authoring.md §6` |
| `risk_summary` | string | Advisory paragraph; the operator must read the YAML, not the summary |
| `prior_approved_plan_hash` | string \| null | Cross-reference into the audit log |

**GATE-A2 payload.**

| Field | Type | Description |
|---|---|---|
| `missing_token` | string | Capability token name (one of §7) |
| `missing_token_description` | string | Human description, copied from the registry |
| `granting_authority` | string | Role allowed to grant this token |
| `requested_scope` | string \| array | What scope the orchestrator asks for (e.g., `descriptor_id` list, "session", "session+1h") |
| `requested_expiry` | ISO 8601 \| null | When the requested scope ends; null = session-bound |
| `justification_from_user` | string | The user's stated reason; required field on the request |
| `prior_grants` | array | Compact log of past grants for this user/token pair |
| `plan_summary` | string | One-paragraph view of the ExecutionPlan |

**GATE-R1 payload.**

| Field | Type | Description |
|---|---|---|
| `cost_breakdown_by_step` | array of object | `{step_id, descriptor_id, p50, p95, source: static\|telemetry\|static+telemetry}` |
| `total_estimate` | number | Currency units; sum of breakdown |
| `rolling_actual_p95` | number \| null | Historical p95 actual for this descriptor mix; null on sparse data |
| `threshold` | number | The currently-applicable cost threshold |
| `user_month_spend` | number | Cumulative actual spend, this calendar month |
| `user_month_threshold` | number | Monthly ceiling; informational unless reached |
| `descriptor_drift_flags` | array of string | `descriptor_id` values whose recent actuals exceed catalog p95 by ≥ 2× |
| `low_confidence_estimate` | boolean | True when telemetry sample size is below the configured floor |

**GATE-R2 payload.**

| Field | Type | Description |
|---|---|---|
| `expected_walltime` | number (seconds) | p95 across the longest path |
| `walltime_breakdown_by_step` | array of object | `{step_id, p50, p95}` |
| `executor_cap` | number (seconds) | Cap for the current executor |
| `executor_id` | string | One of `local`, `parsl_local`, `parsl_aurora`, `parsl_polaris` |
| `downgrade_options` | array of object | `{executor_id, would_fit, latency_multiple}` |
| `queue_state_estimate` | number (seconds) | Estimated queue wait; informational |

**GATE-D2 payload.**

| Field | Type | Description |
|---|---|---|
| `top_n_hypotheses_with_evidence` | array of object | `{hypothesis_id, statement, supporting_evidence_ids, ranking_confidence, proposer_agent_id}` |
| `ranking_confidences` | array of number | Calibrated probabilities for the top-N (sums to ≤ 1.0; the residual is "none of these") |
| `proposer_agents` | array of string | Distinct agent IDs that proposed any of the top-N |
| `judge_model_id` | string | LLM model that produced the ranking |
| `judge_prompt_hash` | string (sha256) | Hash of the judge prompt template, for reproducibility |
| `tournament_size` | integer | Total proposed before ranking |
| `prior_rejection_count` | integer | Within this run, how many times this gate has been rejected |

The remaining gates' payloads (R3, C1, D1, D3, P1, P2) follow the same envelope; their per-gate fields are described in the per-gate sub-sections in §3 and are not repeated here.

---

## 6. Default policies — opinionated defaults

Every deployment can override these per project, but the defaults set the safety floor. New users land on these defaults; relaxing any of them is a deliberate administrative act with its own audit trail.

### 6.1 Role definitions

| Role | Granted by | Default scope | Notes |
|---|---|---|---|
| `scientist` | self-registration | every new user | The default user role. Sees scientist gates; cannot override resource gates. |
| `operator` | system admin | one or more scientists per project | Approves authoring and resource gates. May lower GATE-A1 strictness for the session (e.g., turn Strategy C from hard-block to log-only — for the session only). |
| `capability_admin` | system admin | typically one per project | Owns capability registry; approves GATE-A2. Not in the day-to-day approval loop. |
| `data_protection_officer` | external compliance authority | shared across projects | Required for GATE-C1. Otherwise hands-off. Two-key partner to the operator. |
| `system_admin` | bootstrap | one per deployment | Owns role grants. Not in any approval loop; granting is itself audited. |

### 6.2 Default policy table

| Gate | Scientist (default) | Operator | Cap admin | DPO | Notes |
|---|---|---|---|---|---|
| GATE-A1 | always on for B/C | can lower to log-only for session | n/a | n/a | Strategy A bypasses the gate entirely |
| GATE-A2 | always on | n/a | only approver | n/a | Token grants are audited regardless of approver |
| GATE-R1 | threshold = $10/run | can raise per session | sets per-user ceiling | n/a | Monthly ceiling defaults to $200/user |
| GATE-R2 | always on | only approver | n/a | n/a | Caps per-executor are deployment config |
| GATE-R3 | always on | can override only with `hpc_override` token | grants `hpc_override` | n/a | Token defaults to ungranted |
| GATE-C1 | always on | cannot override | cannot override | only approver | Two-key by design |
| GATE-D1 | off for low-cost, on for HPC-eligible | toggles | n/a | n/a | Cost threshold for "low-cost" = 25% of GATE-R1 threshold |
| GATE-D2 | always on for design queries | n/a | n/a | n/a | Retrieval-only workflows do not invoke the tournament |
| GATE-D3 | always on | can pre-accept gaps for the session | n/a | n/a | Pre-acceptance still records the gap in the final response |
| GATE-P1 | warnings shipped with flag, violations hard-block | only approver for warnings | n/a | only approver for violations | Violation policy version is part of the audit |
| GATE-P2 | soft-block; ship-with-flag is default | can elevate to hard-block per project | n/a | n/a | Flag is sticky downstream |

### 6.3 Override semantics

Every override (raising a threshold, lowering strictness, granting a token) is itself a control-plane operation that produces an audit record (§9). An override is never silent. The override audit record carries `override_target_gate`, `prior_value`, `new_value`, `override_scope` (`session` / `run` / `user` / `project`), and `expires_at`.

The session-scoped overrides expire when the session ends; this is the **least-surprising default** and is the right scope for "I trust this Strategy C plan once because I just inspected it" without quietly persisting the relaxation across runs.

---

## 7. Capability tokens

A capability token is a signed assertion that a specific user (or session, or run) is allowed to use a privileged capability. Tokens are referenced by GATE-A2 (pre-execution) and GATE-C1 (egress) and by every UTD's `requires_capability` field (`tool_descriptor_contract.md §2.1`, §6).

### 7.1 Initial token set

| Token | Description | Granting authority | Default expiry |
|---|---|---|---|
| `network_egress` | Allows a step to perform unrestricted outbound HTTP/HTTPS | capability admin | session |
| `filesystem_persistent` | Allows a step to write outside the run's scratch directory | capability admin | session |
| `gpu_a100` | Allows a step to request an A100 GPU through Parsl | capability admin | session |
| `aurora_compute_credits` | Allows submission of bundles targeting Aurora PBS | capability admin | run |
| `polaris_compute_credits` | Allows submission of bundles targeting Polaris PBS | capability admin | run |
| `phi_data_access` | Allows a step to read records from a PHI-classified data source | DPO | per-workflow re-auth |
| `external_publication` | Allows a step to write to an external publication channel (e.g., a public Globus index) | DPO | per-workflow re-auth |
| `hpc_override` | Allows the operator to override GATE-R3 disqualifiers | system admin | session |
| `low_confidence_cost_approval` | Allows GATE-R1 to approve when telemetry sample is below the floor | operator | run |

Tokens whose default expiry is "session" are revoked the moment the conversational session ends (the MCP client disconnects or the control plane ages out the session). Tokens whose default expiry is "per-workflow re-auth" require a fresh approval flow on every workflow run; carrying a previously-issued PHI token across workflows is **not** allowed regardless of session continuity.

### 7.2 Grant lifecycle

A token grant has the shape `{token_id, user_id, granted_at, granted_by, granting_authority, scope, expires_at, justification, prior_grant_count}`. Grants are append-only; revocation is a new record with `revoked_at`. The combined view of grants + revocations is the authoritative answer to "does user X currently hold token T".

The grant lifecycle composes with the gate lifecycle as follows:

1. GATE-A2 fires when a UTD's `requires_capability` is not satisfied by the user's current grants.
2. The capability admin issues a grant (or denies).
3. The grant is stored; GATE-A2 re-evaluates and passes.
4. The workflow proceeds. At dispatch, the orchestrator checks the same grant (just-in-time; the grant might have expired or been revoked).
5. If the grant is no longer valid at dispatch, GATE-A2 re-fires — this is the just-in-time pair noted in §3.3.

### 7.3 Why tokens, not roles

Roles define *who can do what kinds of things*; tokens define *what specific things have been authorized*. The split matters: a user can be in the `scientist` role *and* hold a one-shot `phi_data_access` token, without permanently elevating their role. Tokens are the right granularity for capabilities that are inherently scoped (one workflow, one session, one descriptor); roles are the right granularity for steady-state authority (who approves what category of gate).

---

## 8. Cost and resource accounting

GATE-R1 is only as good as the cost estimator it consumes. The estimator has three inputs (UTD `cost_estimate`, LLM-call accounting, executor overhead) and produces one output (a rolled-up estimate with confidence). The estimator is also the source of truth for telemetry that updates the catalog.

### 8.1 Authoring-time roll-up

At authoring time, for each `tool_invocation` in the ExecutionPlan:

1. Look up the UTD's `cost_estimate` (`tool_descriptor_contract.md §2.6`). The descriptor carries `cpu_seconds_p50`, `cpu_seconds_p95`, `wall_seconds_p50`, `wall_seconds_p95`, `memory_bytes_p95`, `estimate_source` (`static`, `telemetry`, or `static+telemetry`), `telemetry_run_count`, and `telemetry_window_days`.
2. Convert resource units to currency using the deployment's per-resource rate card. The rate card is configuration, not code; rates can differ per-executor (Aurora compute is priced differently than local).
3. Multiply by the run's expected invocation count (most steps run once; tournament-style steps run K times).

Then add LLM-call cost: `expected_tokens × per_token_rate × expected_n_calls`. The orchestrator agent must declare the expected token count in the plan; if it does not, the estimator uses a deployment-configured default with a `low_confidence_estimate` flag.

Then add executor overhead: a per-executor flat fee that captures startup, container pull time, and idle keepalive. Defaults: `local: $0`, `parsl_local: $0.05`, `parsl_aurora: $0.50`.

The output is a structured object that becomes the GATE-R1 payload's `cost_breakdown_by_step` plus `total_estimate`.

### 8.2 Execution-time actuals

Every executed step produces an actual cost record: `{step_id, descriptor_id, descriptor_version, cpu_seconds, wall_seconds, memory_peak_bytes, currency_units, finished_at}`. These are appended to the descriptor catalog's telemetry buffer. The catalog updates `cost_estimate.cpu_seconds_p95` (and siblings) on a rolling basis — typically a sliding 30-day window with a configurable run-count floor below which the buffer is sticky to the prior value.

Telemetry updates are *append-only*. A descriptor's `cost_estimate` snapshot is captured in every audit record (§9), so a past gate decision can be replayed against the cost picture as it existed at the time, even after the descriptor's `cost_estimate` has drifted.

### 8.3 Drift detection

If a descriptor's recent actuals exceed its cataloged `cpu_seconds_p95` by ≥ 2×, the next GATE-R1 evaluation against any plan using that descriptor surfaces a `descriptor_drift_flags` entry. Operators see the flag in the GATE-R1 payload; the descriptor is also automatically queued for catalog review (the review itself is a workflow, not a manual process).

Drift detection is itself a soft gate: it does not block, it warns. The point is to surface a stale estimate before the operator approves a plan whose true cost is materially higher than the screen says. The 2× threshold is configurable per-descriptor.

---

## 9. Audit and compliance

Every gate decision produces an audit record with the following envelope:

| Field | Type | Description |
|---|---|---|
| `audit_id` | string (uuid) | Stable handle; primary key |
| `gate_id` | string | One of `GATE-A1` … `GATE-P2` |
| `fired_at` | ISO 8601 | When the gate emitted the Approval |
| `decided_at` | ISO 8601 | When the decision was recorded |
| `run_id` | string (uuid) | Cross-reference into the workflow run record |
| `plan_hash` | string (sha256) | Snapshot of the ExecutionPlan at the time the gate fired |
| `payload_hash` | string (sha256) | Hash of the §5 payload as the approver saw it |
| `approver` | string | User ID of the deciding user |
| `approver_role` | string | The role under which the decision was made |
| `decision` | enum | `approve` / `reject` / `correct` / `timeout_deny` / `timeout_approve` |
| `justification` | string | Required for `reject` and `correct`; optional otherwise |
| `correction_patch` | object \| null | JSON Patch (or gate-specific structured edit) applied on `correct` |
| `descriptor_snapshot_hash` | string (sha256) | For resource gates: snapshot of all relevant UTD `cost_estimate` blocks |
| `policy_version` | string | For policy gates: the policy ruleset version at decision time |
| `signature` | string | Cryptographic signature over the entire record |

Records are append-only; correction or revision is a new record with a back-pointer to the prior `audit_id`. The audit log is signed: signature verification is part of bundle replay. A bundle whose audit chain fails signature verification is treated as `non_reproducible` and triggers GATE-P2 on next replay.

**Retention.** Default retention is 18 months for routine audit records; GATE-C1 records (PHI / restricted egress) are retained for 7 years per typical compliance policies. Retention is configurable per deployment but cannot be set below the GATE-C1 floor without explicit DPO override (which is itself audited).

**Cross-reference.** The audit log composes with the provenance chain documented in `hpc_reproducibility_spec.md §11` (canonical doc forthcoming). Every workflow run's terminal bundle includes `audit_chain_hash`, a Merkle-root over the run's audit records; this is what makes the bundle verifiable offline.

---

## 10. Sandbox execution as a gate

When the static authoring validation cannot guarantee that a step is safe to execute on the host runtime (user-uploaded code, an unfamiliar Rhea descriptor, a tool whose UTD `provenance_pin.executable_digest` is null), the workflow can be auto-routed to a sandbox runtime. The sandbox is the existing T13b Docker scaffold (`apecx-mcp-integration/CLAUDE.md` §"T13b Docker sandbox"), which pins `--network=none`, `--read-only`, `--cap-drop=ALL`, a default seccomp profile, memory and CPU caps, and a read-only bind mount.

Sandbox routing is itself a gate, **GATE-S1** (deferred to v1.1):

- **Trigger condition:** any `tool_invocation` whose UTD has `provenance_pin.executable_digest = null` AND `side_effects ∈ {filesystem_persistent, external_compute}`, OR the operator has opted into "sandbox-by-default" for the project.
- **Blocking semantics:** soft-block. The operator is informed: "this step will execute in sandbox-mode (slower, no persistent fs, no network)". The operator may approve the sandbox path, reject it (which terminates), or correct (e.g., supply a missing `executable_digest` so the step can run on the host).
- **Approver:** operator.
- **What the approver sees:** which steps will be sandboxed, what the sandbox restrictions imply for each step's expected output, an estimate of latency overhead.
- **Default:** off; opt-in per workflow via the `sandbox_mode` parameter on the run request. Once Phase 3 wires the sandbox into the composer execution path, the default becomes "on for all Strategy C runs lacking pinned digests".

GATE-S1 is documented here for completeness but is **not part of v1**. The threat-model and flag rationale live in the T13b design doc; this gate's behavior cannot be finalized until that wiring lands.

---

## 11. Failure modes and escalations

The gate machinery itself is a system with failure modes. The following table enumerates what can go wrong with the gating layer and what the system does in response.

| Failure | Detection signal | Automated response | Manual escalation |
|---|---|---|---|
| Approval timeout (window expires, no decision) | wall-clock > `expires_at` | Default-deny: workflow terminates with `timeout_deny` audit record | Operator notified through MCP; workflow can be re-submitted |
| No eligible approver online | `eligible_approvers` set is empty when gate fires | Workflow paused; `approval_queue_unmanned` event raised | Backup approver notified (configured per deployment); if no backup, system admin paged |
| Control plane unreachable | gate evaluator receives transport error | Workflow paused, retried with exponential backoff; if backoff cap reached, terminate | Run state preserved; operator can re-submit when control plane recovers |
| Audit log signature verification fails | replay-time signature check returns false | Bundle marked `non_reproducible`; GATE-P2 fires on attempted reuse | Incident filed; system admin investigates signing key compromise |
| Duplicate decision attempted | second `approve`/`reject`/`correct` on same `approval_id` | Reject second call; record `duplicate_decision` event | None automatic; operator is informed the decision was already made |
| Capability admin grants token after revocation | `revoked_at` < new `granted_at` for same user/token pair | Allowed (a revocation is not permanent); recorded as `re-grant` event | None |
| GATE-A2 fires for unknown token | `requires_capability` references a token not in §7.1 | Hard-block; workflow terminates with `unknown_capability_token` error | Capability admin must register the token (registration is a new audit record) |
| GATE-C1 fires but DPO is unreachable | timeout window expires with no DPO decision | Default-deny + escalate to backup DPO; if no backup, terminate and file incident | Compliance team notified |
| GATE-R1 estimate is `low_confidence` and approves anyway | post-execution actual ≥ 2× estimate | Soft post-execution warning; descriptor flagged for catalog review | Operator informed; descriptor's telemetry is updated automatically |
| Mid-execution decision gate (D2/D3) fires after upstream step crashed | crashed-step output absent | Gate evaluator sees no input; fires `gate_eval_blocked` event | Workflow terminates; the upstream crash is the actionable error |
| Audit record write fails | control plane returns error on append | Workflow paused; retry append with backoff; on cap, hold workflow open until intervention | System admin paged; workflow is **not** auto-terminated to avoid losing work |

The asymmetry in the last row is deliberate: a transient audit-write failure should not destroy in-flight work. Conversely, a persistent audit-write failure must not let work *complete* without a record — so the workflow holds open until the admin resolves it.

---

## 12. Open questions

Items here are intentionally unresolved. Each is a decision that v1 implementation will need to make explicitly; we document the alternatives so the choice is conscious, not accidental.

1. **Rejection context propagation.** When GATE-A1 rejects a plan, does the rejection's `justification` propagate back to the orchestrator agent for re-authoring, or is the agent informed only that the plan was rejected? Propagating context risks prompt-injection from a hostile operator into the agent's next attempt; not propagating means the agent re-tries blind. Working hypothesis: propagate, but sanitize and constrain the propagated text to a structured `rejection_reason_code` plus a free-text field that is escaped before reaching the LLM.

2. **Mid-execution gate pre-emption.** Can a decision gate (D1/D2/D3) be pre-emptable — i.e., can the operator interrupt a long-running upstream step to surface the gate early? v1 says no (gates fire when their trigger condition is met, not before), but this means an operator who realizes the workflow is on the wrong track must wait for the next natural checkpoint. The alternative requires step-level pause primitives in the executor, which we do not have.

3. **Cost actuals — granularity.** Do we record actuals per-step or per-workflow? Per-step is more useful for descriptor calibration; per-workflow is cheaper to record and easier to bill. Working hypothesis: per-step for the descriptor catalog, with a per-workflow roll-up for billing.

4. **Capability token expiration on long Aurora jobs.** A token granted with `expires_at = run` could be technically expired by the time the Aurora bundle finishes (24-hour job, session-bound token). Does the bundle export pin a snapshot of the token grant, or does it carry a mid-execution re-auth requirement? Working hypothesis: pin a snapshot (the bundle is an attested record of "the user was authorized at submission time"); re-auth on ingest if the pin is older than a configured staleness floor.

5. **Default-deny as global default.** Is timeout-default-deny the right default for new users? The arguments for: a non-decision should not silently authorize. The arguments against: a scientist who steps away during a long retrieval may return to a terminated workflow they did not intend to abandon. Working hypothesis: yes, default-deny — but extend the default expiry windows (24 hours for most gates, 72 hours for GATE-C1) so the asymmetry of "termination on inattention" is unlikely to bite.

6. **GATE-D2 retry cap.** The current cap is 2 retries (re-runs of the tournament with rejection feedback). Is this the right number? Higher caps risk rejection-feedback loops that converge to worse hypotheses (the LLM over-fits to the rejection signal). Lower caps risk forcing the scientist into a manual re-author path. v1 keeps 2 and instruments the cap; tuning is a follow-up.

7. **Override audit visibility.** Override records (raising a threshold, lowering strictness) are audited (§6.3). Should those records be visible to the user whose run is affected by the override, or only to system admins? The transparency argument is strong (the user should know they ran under a relaxed regime); the operational argument against (revealing operator decisions about other users) is non-trivial. Working hypothesis: visible, but only the *fact* of the override (the override target gate and that an override existed), not the operator's identity or justification.

8. **Soft-gate fatigue.** If GATE-D3 (capability gap) fires every run because some peripheral source is consistently degraded, operators will start auto-accepting it. Is there a fatigue-detection heuristic that escalates a soft gate to a louder warning when it has been auto-accepted N times in a row? v1: no, not yet. v1.1 candidate.

---

## 13. Cross-references

| Topic | Document |
|---|---|
| Existing approval MCP tools (4) | `architecture.md §4.5` |
| Existing HPC tools (4) | `architecture.md §4.6` |
| Tier 0 HITL surface and intent classifier | `multiagent_architecture.md §4` |
| Tier 1 orchestrator design, HypothesisTournamentStep | `multiagent_architecture.md §5`, §8.2 |
| Authoring strategies A/B/C/D and the lowering pipeline | `agent_workflow_authoring.md §2`, §6 |
| ExecutionPlan schema | `agent_workflow_authoring.md §3` |
| Unified Tool Descriptor (UTD) v1, `requires_capability` | `tool_descriptor_contract.md §2`, §6 |
| `cost_estimate` schema fields | `tool_descriptor_contract.md §2.6` |
| LayeredReasoningWorkflow, HITLGateStep, ResponseSynthesisStep | `nanobrain_workflow_design.md §3.6`, §3.7 |
| LayerResult, CapabilityGap, failure contract | `workflow_output_contract.md §4.4` |
| Reasoning patterns P7 (tournament) and P9 (capability gap) | `reasoning_patterns_library.md` |
| Rhea / GalaxyMCP / native nanobrain tool integration | `external_tool_integration.md` |
| HPC bundle export, provenance seed | `apecx-mcp-integration/CLAUDE.md` ("PBS bundle export") |
| Docker sandbox scaffold (T13b) | `apecx-mcp-integration/CLAUDE.md` ("T13b Docker sandbox") |
| Provenance chain integrity (canonical doc forthcoming) | `hpc_reproducibility_spec.md §5`, §11 |
