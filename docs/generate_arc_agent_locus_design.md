# Integration design — GENERATE arc as the agent-locus reasoning engine

**Status:** DESIGN (no code). 2026-06-15. Companion to
`docs/resolver_trio_port_plan.md`.
**Source:** `reasoning-agent-surface`. **Target:** `main`.
**Decision input:** user confirmed a real headless/autonomous backend use case →
the generate arc is wanted, "gated/announced behind `--locus agent` + the
requires_llm gate."

---

## 1. What the generate arc IS (and why it ≠ `compose_workflow`)

Main already exposes `compose_workflow` — a **one-shot** call to `Composer.compose`
that authors a workflow YAML and hands it back. The generate arc wraps that raw
composition in a **trust boundary**: a generated workflow is born a DRAFT that may
only dry-run, passes a deterministic gate, and runs for real ONLY after a human
promotes it. The lifecycle (spine: `generate.generate_gated_draft`):

1. **COMPOSE** — `Composer.compose(prompt)` *(on main ✓)*
2. **STATIC VALIDATE** — `validate_workflow_against_framework` *(on main ✓, via `workflow_validator`)*
3. **REQUIRES-LLM AUDIT** — `compute_requires_llm` *(on main ✓ — ported in the locus work)*
4. **DRY-RUN GATE** — `workflow_dry_run.dry_run_draft`: load + cascade on a trivial
   sample input, **LLM-FREE**, catches the G127/G99 swallowed-exception class;
   `dry_run_policy` classifies each step PURE/LLM/SANDBOX/SIDE_EFFECTING and
   **blocks side-effecting steps** in draft mode.
5. **DRAFT MINT** — `_mint_validation_token` (SHA-256 over YAML + LLM model hash);
   the artifact is stamped DRAFT, `validation_token` null until the gate passes.
6. **HUMAN PROMOTE** — `promotion.promote_draft` re-gates and flips
   `promotion_status` DRAFT → REUSE_LEVEL (D5: user-only, never self-promote).
7. **REUSE** — `find_workflow` surfaces promoted workflows alongside catalog +
   manifests; `draft_execution.route_generated_execution` routes a draft to
   dry-run-only and a reuse-level workflow to full execution.

**The value is the gate, not the composition.** Stages 2–4 are deterministic
(no LLM) — the safety property ("a machine-authored workflow cannot run for real
until it has loaded cleanly AND a human approved it") holds without an LLM. This
is exactly the autonomy guard a headless agent needs.

---

## 2. THE decision — locus gating (branch parity vs. user's agent-gate)

This is the one real architectural choice, and the branch and the user **disagree**:

- **Branch author chose UNCONDITIONAL parity.** `server.py` registers
  `generate_workflow` / `execute_draft` / `promote_draft` with no locus guard, and
  `tests/.../test_locus_tool_parity.py` *actively forbids* a per-locus feature gate
  (`test_exactly_one_locus_flag_and_no_feature_gate` source-scans for and rejects
  any `ENABLE/DISABLE_GENERAT*` flag; `test_deterministic_tool_set_is_identical_across_loci`
  asserts a 21-tool surface identical in both loci). Rationale: locus steers the
  *prompt/face*, not the tool set; in desktop the tools simply loudly-refuse via
  requires_llm when no Ollama is present.
- **User endorsed gating on `--locus agent`.** Rationale (the architecture we
  shipped): in desktop the frontier host out-reasons a local 12B composer; exposing
  a weaker server-side generator invites the host to delegate reasoning *downward*.
  Desktop = host composes primitives; backend = generate arc.

**Recommendation: gate generate-tool REGISTRATION on `--locus agent`.** Follow the
user. Concretely:
- In `build_server`, register `generate_workflow` / `execute_draft` /
  `promote_draft` / `find_workflow` only when `resolved_locus == AGENT`.
- `compose_workflow` (the one-shot, already on main) STAYS available in both loci as
  the desktop last-resort — it carries no draft/promote lifecycle and no autonomy
  surface to hide.
- **Adapt, don't delete, `test_locus_tool_parity`:** its contract changes from
  "identical tool set across loci" to "the *deterministic* tools are identical; the
  generate arc is agent-only." Re-pin it to assert exactly that (desktop set ⊂ agent
  set, and the difference is exactly the 4 generate tools). Keep the
  "no hidden env feature-gate" half — locus is still the *only* switch, it just now
  gates registration, not merely the prompt.

This diverges from the branch deliberately; record it as a decision (DECISION-GEN1)
with this rationale so a future reader doesn't "restore parity" by reflex.

Honest caveat: parity is genuinely simpler (one surface, requires_llm does the
availability talking). If the cost of agent-gating (a locus branch in registration +
the parity-test rewrite) ever outweighs the "don't tempt the desktop host" benefit,
parity + a strong reuse-first prompt is a defensible fallback. The reasoning prompts
we just shipped already push reuse-first, which softens the parity risk.

---

## 3. Dependency map (what main has vs. needs)

**Already on main (satisfied):** `composer.py`, `workflow_validator.py`
(`validate_workflow_against_framework`), `workflow_requires_llm.py`
(`compute_requires_llm`), `composer_schemas.py`, `artifact_store.py` (base),
`control_plane/executors/local.py`.

**Branch-new modules to port (composition layer), in dependency order:**
1. `_workflow_staging.py` (leaf — absolutize step configs for temp-file load)
2. `dry_run_policy.py` (leaf — PURE/LLM/SANDBOX/SIDE_EFFECTING table)
3. `workflow_dry_run.py` (uses staging + policy)
4. `workflow_gate.py` (`validate_workflow_for_execution` — NEW entry point; chains
   validator + requires_llm + dry-run)
5. `generate.py` (`generate_gated_draft`, `gate_composed_workflow`)
6. `promoted_registry.py` (append-only JSON registry)
7. `promotion.py` (`promote_draft`, D5 guard)
8. `draft_execution.py` (route + execute)

**MCP + control-plane:**
9. `mcp_surface/tools/find_workflow.py` + the generate/draft/promote tools in
   `mcp_surface/tools/workflows.py`
10. `control_plane`: 3 routes (`/generate`, `/execute-draft`, `/promote-draft`),
    `schemas/api.py` (6 request/response models), `models/entities.py`
    (`PromotionStatus`, `GeneratedArtifact.promotion_status`/`.validation_token`),
    `artifact_store` methods (`stamp_validation_token`, `set_promotion_status`,
    `get_generated_state`).

**Migration:** `0007_generated_artifact_draft_state.py` — adds `promotion_status`
(String(32), NOT NULL, backfill `"draft"`) + `validation_token` (String(64),
nullable). Standard additive Alembic migration.

---

## 4. State + leak-safety

Triple persistence, all **shared + on-disk, append-only** (no process-lifetime
store to leak — addresses this repo's `long_lived_server_unbounded_stores` history):
- control-plane SQLite `generated_artifact` table (the two new columns);
- artifact YAML files under `~/.apecx_cp/artifacts/` (keyed by UUID, SHA-256 in DB
  for tamper detection);
- promoted-workflow JSON registry (`~/.apecx_cp/promoted_workflows.json`,
  `$APECX_PROMOTED_WORKFLOWS_PATH`), append-only, idempotent on `artifact_id`.

No unbounded-growth risk flagged: regeneration mints a NEW artifact (not a
replacement), and the registry upsert is idempotent. Bounded column widths.

---

## 5. Test burden (23 branch tests)

The **core safety property is provable without an LLM**: 7 pure-unit + 5
integration-no-LLM tests cover the gate, dry-run side-effect blocking, draft state
machine, promotion guards, and artifact store. 7 tests need real Ollama (the
compose→persist→promote roundtrips) and auto-skip when unreachable.

Port the no-LLM tests with their modules (they are the regression backbone). Run
the real-LLM roundtrips once against a live Ollama to record the end-to-end
verification, then let them auto-skip in CI. One critical pin to keep:
`test_no_self_promotion` (source-scans that nothing but the promote path writes
`REUSE_LEVEL` — the D5 autonomy guard).

---

## 6. Phased sequencing

- **Phase 1 — composition core (no MCP, no DB):** modules 1–8 + their unit/no-LLM
  integration tests. Verifies the gate + draft lifecycle in isolation. Near-zero
  product risk (nothing wired to the wire yet).
- **Phase 2 — control-plane:** migration 0007 + entities + schemas + 3 routes +
  artifact_store methods. Gated by `test_artifact_store` + promotion-guard
  integration tests. Needs a control-plane DB in the test env.
- **Phase 3 — MCP surface (agent-locus-gated):** `find_workflow` + generate/draft/
  promote tools, registered ONLY in `--locus agent`; rewrite `test_locus_tool_parity`
  per §2; run the real-LLM roundtrip once.
- **Phase 4 — agent face:** point the reasoning prompts' MATCH step at `find_workflow`
  (they currently use `list_workflows` per the desktop adaptation) when served under
  agent locus; record DECISION-GEN1.

Each phase is its own branch + worktree + PR, citing this doc. Phase 1 is safe to
start immediately; Phases 2–4 each gate on the prior phase's tests green.

**Recommended first step:** Phase 1 only. It delivers the deterministic gate (the
actual value) with no surface-area risk, and proves the LLM-free safety property
before any of it is exposed.
