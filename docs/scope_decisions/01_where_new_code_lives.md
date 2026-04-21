# Scope Decision 01 — Where does new code live?

**Date:** 2026-04-21
**Status:** **Draft, awaiting user sign-off**
**Triggered by:** User directive 2026-04-21 — "Edits to `nanobrain/` are possible, but should be discussed separately."

---

## The decision

The integration project needs new steps (VIOLIN reader, ApprovalStep), new executors (Globus Compute, PBS bundle), and possibly refactors to existing nanobrain steps that bake in Aurora Parsl executor config. The question: do these live **inside `nanobrain/`** (framework extension) or **inside `apecx-mcp-integration/`** (integration layer)?

This is a pre-Phase-1 architectural call. Once made, every task that adds framework-level code inherits its boundary.

---

## Options

### Option A — Extend `nanobrain/` directly

Put new code under `nanobrain/nanobrain/library/domain/` (per architectural_plan.md §5.2), `nanobrain/nanobrain/library/infrastructure/executors/` (per §5.4, §5.5), and the `ApprovalStep` class in `nanobrain/nanobrain/library/steps/` (per §5.10).

**Pros:**
- Matches the architectural plan literally.
- New components are discoverable by any nanobrain consumer.
- Clean framework-level abstractions (ApprovalStep becomes a reusable primitive).

**Cons:**
- Every PR requires the `nanobrain/` carve-out discussion (user's 2026-04-21 directive).
- The nanobrain repo is "read-mostly" per workspace CLAUDE.md; legitimate edits clash with the policy signal.
- Coupling: changes to nanobrain core (e.g., if ApprovalStep surfaces a pause/resume gap per T00.2) affect all nanobrain users, not just this project.

### Option B — All new code in `apecx-mcp-integration/`, nanobrain is an untouched dependency

Put new components under `apecx_integration/steps/`, new executors under `apecx_integration/execution/`, the ApprovalStep under `apecx_integration/steps/approval_step.py`. Reference existing nanobrain components via YAML wrapper configs in `apecx_integration/config/library/`. Our pyproject.toml declares nanobrain as a dependency; we inherit and extend, never patch.

**Pros:**
- No nanobrain edits needed. Workspace CLAUDE.md "read-mostly" rule honored without exceptions.
- Clean boundary: apecx-integration is the integration layer; nanobrain is the framework.
- Faster iteration: no cross-repo PR dance, no per-file approval.
- Cleaner blast radius: a bug in our code cannot break other nanobrain users.

**Cons:**
- Our new steps are not discoverable by other nanobrain consumers. If the VIOLIN reader ever turns out to be broadly useful, we'd migrate it to nanobrain later.
- Cannot fix the executor-decoupling problem (T02r) this way — existing nanobrain steps hard-code their executors; without editing nanobrain, we either live with the Aurora-baked config or write a parallel wrapper step that reads from the BV-BRC snapshot and routes to a configurable executor.
- If nanobrain ever changes a step's public contract, our wrappers break. Versioning risk.

### Option C — Hybrid: Option B by default, nanobrain edits only when there is no alternative

Default to Option B. For each case where a nanobrain edit is genuinely necessary (e.g., T02r if there's no way to wrap around the executor coupling), write a discrete proposal and get user approval for that specific edit.

Approved edits get a dedicated `docs/scope_decisions/0N_nanobrain_edit_<name>.md` memo with: (a) why Option B fails for this case, (b) the exact files touched, (c) a rollback plan, (d) user sign-off line.

**Pros:**
- Honors the "discussed separately" directive cleanly — each edit gets its own discussion.
- Prefers the low-commitment path; nanobrain changes are rare and deliberate.
- Generates an audit trail for every cross-repo edit.

**Cons:**
- More process per edit; friction for small refactors.
- Risk that the overhead of approval discourages legitimate refactors, leading to workaround crust in apecx-integration/.

---

## Recommendation: **Option C**

Default to Option B (apecx-integration-first). Escalate to individual nanobrain edits only when Option B produces obvious ugliness. For the current task list, this means:

| Task | Default path | Expected outcome |
|---|---|---|
| T02 — component library (wrappers) | Option B — wrapper YAMLs under `apecx_integration/config/library/` | No nanobrain edits needed |
| T02r — executor-decoupling | **Escalate to user.** The existing steps hard-code Parsl-on-Aurora. Wrapping around this without touching nanobrain produces brittle parallel-step duplication. This is the first genuine edit-nanobrain proposal. | Separate `docs/scope_decisions/02_...md` memo if we proceed |
| T10 — ApprovalStep | Option B — under `apecx_integration/steps/approval_step.py` | Subclass `nanobrain.core.step.Step`; no framework edit |
| T04 — Globus Compute executor | Option B — under `apecx_integration/execution/globus_compute_executor.py` | Subclass `nanobrain.core.executor.ExecutorBase` |
| T05 — PBS bundle generator | Option B — under `apecx_integration/execution/pbs_bundle.py` | Pure generator, no executor subclass |

**Rationale:** Option B is the cheapest policy-compatible path for ~4 of 5 tasks. Option C's escalation gate only fires when we hit a real structural problem (T02r). That is the correct signal-to-noise ratio.

---

## Implications if the user accepts Option C

1. **Update architectural_plan.md R3 section** to note that new code lives under `apecx_integration/`, not `nanobrain/library/domain/`. The original AP §5.1 tree description becomes accurate only for `apecx_integration/`, not for nanobrain extensions.
2. **Update implementation_plan.md** T02, T10, T04, T05 specs to reflect Option B paths. T02r retains its "requires nanobrain edit discussion" gate.
3. **T02 scope shrinks further.** Writing wrappers (~2d) is cheaper than authoring domain components (~10d). Revised estimate: **T02 at ~5–7 code-days** if Option B covers everything. Further honest shrinkage.
4. **T02r stalls until the separate edit-nanobrain discussion.** If user decides "we'll live with Aurora-baked steps and use our own replacement steps for the local path," T02r is replaced by a simpler `apecx_integration/steps/viral_analysis_local/` module that reads the same BV-BRC snapshots but uses a configurable executor.

---

## Open questions

1. **Option C acceptable?** If not, which option should we adopt?
2. **T02r resolution.** If Option C, are you open to a separate edit-nanobrain discussion for executor decoupling, or would you prefer the "build parallel local-path steps in apecx-integration" alternative?
3. **Backporting.** If a component we write under Option B turns out to be broadly useful, is there an appetite to later upstream it to nanobrain? Or should those stay as apecx-integration-specific permanently?

---

## Sign-off

_User: please mark one option accepted and sign/date below._

- [ ] Option A (extend nanobrain directly)
- [ ] Option B (apecx-integration-first, nanobrain untouched)
- [ ] Option C (recommended — Option B by default, nanobrain edits case-by-case)

Signature / date: ___________________________
