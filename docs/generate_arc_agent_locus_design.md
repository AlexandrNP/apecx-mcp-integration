# Integration design — GENERATE arc + workflow co-authoring across loci

**Status:** DESIGN (no code). 2026-06-15, REVISED. Companion to
`docs/resolver_trio_port_plan.md`.
**Source:** `reasoning-agent-surface`. **Target:** `main`.
**Decision input:** user confirmed a real headless/autonomous backend use case
AND pushed back on excluding generation from desktop ("how do we co-author
workflows with the agent?"). That pushback corrected the design — see §2.

**REVISION NOTE (supersedes the first cut):** the first version recommended
gating the WHOLE arc on `--locus agent`. That was wrong: it conflated the LLM
*composition* step (where host-vs-local-LLM matters) with the deterministic
*gate → draft → promote → reuse* lifecycle (locus-agnostic, the actual value).
The branch already split these — `gate_composed_workflow(composed)` is documented
as "split out … so a caller can gate a workflow it composed once **without
re-invoking the LLM**." The corrected model (§2) applies the locus INVERSION to
composition, exactly as we did for synthesis.

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

## 2. THE decision — composition obeys the locus inversion; the lifecycle is shared

The question that drove this revision: *"How do we co-author workflows with the
agent?"* Answer: the same way the host already co-produces ANSWERS — the locus
inversion. Composition is just another LLM task carrying an `LLM_ROLE`; the
deterministic gate/draft/promote/reuse lifecycle is locus-agnostic infrastructure.

**Two separable concerns (the branch already split them in code):**
- **(a) COMPOSE** — author the workflow (`Composer.compose`, an LLM call). Here
  host-vs-local-LLM matters.
- **(b) GATE → DRAFT → PROMOTE → REUSE** — `gate_composed_workflow(composed)` /
  `promote_draft(composed)` / `execute_draft(state)`. Deterministic, LLM-free.
  `gate_composed_workflow`'s own docstring: split out "so a caller can gate a
  workflow it composed once **without re-invoking the LLM**."

**The model — invert (a), share (b):**

| | Desktop locus | Agent locus |
|---|---|---|
| **(a) Compose** | **host authors** the `ComposedWorkflow` — the local `Composer.compose` LLM call is OMITTED (inversion, exactly like `final_synthesis`) | local `Composer.compose` authors |
| **(b) Gate/draft/promote/reuse** | server (deterministic) | server (deterministic) |

So BOTH loci expose the lifecycle; they differ only in WHO composes. This is why
the earlier "gate the whole arc on agent" was wrong — it would hide a deterministic,
desktop-appropriate lifecycle behind the one LLM step that should instead invert.

**Tool surface (the only real delta):**
- `submit_workflow(authored_artifact)` — **NEW, both loci, desktop-primary.** Takes a
  HOST-authored workflow (YAML + any novel python), lowers it deterministically into a
  `ComposedWorkflow`, runs `gate_composed_workflow` → DRAFT. This is the desktop
  co-authoring entry: the frontier host is the composer.
- `generate_workflow(description)` — branch tool, **AGENT-locus only.** The local
  `Composer.compose` authors from a description. In desktop this is omitted (the host
  authors via `submit_workflow` instead); calling it in desktop would delegate
  authoring DOWN to the weak local model — the one thing to avoid.
- `execute_draft` / `promote_draft` / `find_workflow` — **both loci** (deterministic
  lifecycle + reuse discovery).
- `compose_workflow` (one-shot, already on main) — keep both loci as a convenience;
  it is the un-gated predecessor of this lifecycle.

**`test_locus_tool_parity` — adapt, don't delete.** Its contract becomes: the
deterministic + lifecycle tools are identical across loci; the SINGLE difference is
`generate_workflow` (agent-only) vs `submit_workflow` being the desktop compose entry.
Keep its "no hidden env feature-gate" half — locus stays the only switch.

Record as **DECISION-GEN1 (revised):** composition is locus-inverted (host composes
in desktop, local Composer in agent); the gate/draft/promote/reuse lifecycle is
shared. A future reader must not "simplify" by either (i) re-welding compose to the
lifecycle, or (ii) exposing `generate_workflow(description)` in desktop.

### The co-authoring loop (desktop)
1. Host `find_workflow` / `list_workflows` — reuse-first.
2. Nothing fits → host AUTHORS a `ComposedWorkflow` (reuse existing components by
   path + novel python it writes itself; CLOSED-CLASS + path-reference rules make the
   lowering deterministic).
3. Host → `submit_workflow(artifact)` → server gate (static + requires_llm + dry-run
   on trivial input) → DRAFT + verdict. No server LLM.
4. Host relays the verdict; user `promote_draft` (D5 user-only) → REUSE_LEVEL.
5. `find_workflow` surfaces it next session. Iteration = re-submit after reading the
   gate feedback.

**Open sub-question for the lowering step (Phase 3):** does the host emit (i) full
nanobrain YAML + novel python, lowered by a deterministic adapter into
`ComposedWorkflow`, or (ii) a higher-level step+link spec that a deterministic
builder materializes? (i) is more expressive and matches `Composer`'s own output
shape; (ii) is safer (smaller authoring surface for the host to get wrong). Decide
when Phase 3 lands; (i) is the default since it reuses the existing `ComposedWorkflow`
schema with no new spec language.

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
- **Phase 3 — MCP surface (the locus-inverted compose entry):** register the SHARED
  lifecycle tools (`find_workflow`, `execute_draft`, `promote_draft`) in BOTH loci;
  add `submit_workflow(authored_artifact)` (both loci, the host-as-composer entry)
  with the deterministic lowering (§2 open sub-question); register
  `generate_workflow(description)` in `--locus agent` ONLY. Rewrite
  `test_locus_tool_parity` per §2 (single difference = `generate_workflow` agent-only).
  Run the real-LLM roundtrip once. Record **DECISION-GEN1 (revised)**.
- **Phase 4 — agent face:** point the reasoning prompts' MATCH step at `find_workflow`
  (they currently use `list_workflows` per the desktop adaptation), and add a desktop
  co-authoring section to the protocol prompt (author → `submit_workflow` → relay gate
  verdict → user promote).

Each phase is its own branch + worktree + PR, citing this doc. Phase 1 is safe to
start immediately; Phases 2–4 each gate on the prior phase's tests green.

**Recommended first step:** Phase 1 only. It delivers the deterministic gate +
draft/promote/reuse lifecycle (the actual value, locus-agnostic) with no surface-area
risk, and proves the LLM-free safety property before any of it is exposed. The
co-authoring surface (`submit_workflow`) and the locus split land in Phase 3 on top
of that proven core.
