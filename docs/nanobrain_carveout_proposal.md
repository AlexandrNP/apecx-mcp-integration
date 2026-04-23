# Nanobrain carve-out proposal — 2026-04-23

Per workspace CLAUDE.md: *"`nanobrain/` is read-mostly. Edits to
those repos are out-of-scope unless the user explicitly asks for
them, in which case treat each such repo as its own short work."*

This doc enumerates the nanobrain access I'm requesting, split into
**discrete carve-outs** you can approve or deny independently. Each
carries a stated scope, expected diffs, rollback story, and risk.

**This is a menu, not a blanket.** Approving #1 does not approve
#2. I'll surface each future carve-out as a fresh request.

---

## Already-authorized carve-outs (for reference)

### CO-T13x (historical, authorized 2026-04-23): TransformLink YAML loader

**Status:** ✅ shipped. You authorized this explicitly in an earlier
turn by asking "fix TransformLink capabilities."

**What I touched in `nanobrain/`:**

- `nanobrain/core/link.py` — added `parse_transform_from_config()`
  helper; rewrote `TransformLink` to the mandatory-from_config
  pattern (direct init blocked, `from_config` + `extract_component_config`
  + `resolve_dependencies` + `_init_from_config` mirroring
  ConditionalLink). ~130 added lines, ~50 modified lines.
- `nanobrain/tests/unit/test_transform_link.py` — new file, 14
  tests covering the resolver + direct-init block + transfer path.

**Rollback story:** nanobrain is not a git repo on this workspace
(see `session_friction_log.md` #6). On-disk edits only. To revert:
`git checkout` from an upstream clone of nanobrain or restore from
backup. This is an unresolved durability issue; flagged but not in
my authority to fix.

---

## Proposed carve-out #1 — T14 audit (READ-ONLY + documentation)

**Ask:** grep + read access across `nanobrain/nanobrain/` for
audit purposes. Zero writes to nanobrain. All findings written to
**apecx-mcp-integration/docs/nanobrain_mock_audit.md** (my own repo).

**Scope:**

- Run these greps against `nanobrain/nanobrain/`:

  ```
  grep -rn "unittest.mock\|MagicMock\|AsyncMock\|Mock(" nanobrain/nanobrain/
  grep -rn "is_mock\|_mock_\|MOCK\|dev_mode" nanobrain/nanobrain/
  grep -rn "return None  # mock\|# stub\|TODO.*stub\|NotImplemented" nanobrain/nanobrain/
  grep -rn "if.*test\|if.*mock\|fallback" nanobrain/nanobrain/core/ | head -100
  ```

- For each hit, classify per implementation_plan.md §T14 goal:
  1. **Legitimate unit-test mock** with matching integration test — OK.
  2. **Legitimate unit-test mock** without matching integration test — needs an integration test authored OR a T-ticket filed.
  3. **Production-path mock fallback** — must be removed.
  4. **Developer-mode convenience** — must be gated behind `NANOBRAIN_DEV_MODE`.

- Write findings to
  `apecx-mcp-integration/docs/nanobrain_mock_audit.md` with:
  - One row per hit: file:line, classification, proposed action,
    blast-radius estimate.
  - A summary section: totals by classification, estimated total
    effort for the implementation carve-out.

**Time estimate:** 0.5–1d depending on how many mock hits exist.

**Blast radius:** zero — this is read + document-in-my-own-repo
only.

**Expected output if approved:** a PR-ready audit doc that lets
you decide which carve-out #2 slices to approve.

**Proposed approval text (copy-paste if you want):**
> "Approved — carve-out #1 read-only audit."

---

## Proposed carve-out #2 — T14 fixes (WRITE to specific nanobrain files)

**Deferred until carve-out #1 produces the audit.** Scope cannot be
known in advance because it depends on what the audit finds.

**What the approval would need to include:**

- Per-file list of files I'd touch under
  `nanobrain/nanobrain/core/` (or `nanobrain/nanobrain/library/` if
  the audit finds mocks there).
- Per-file diff summary (not a full diff, but "remove X; replace
  with Y").
- Per-file rollback story.

**Approximate shape I expect from the audit:**

- 2–5 files under `nanobrain/nanobrain/core/` with production-path
  mock fallbacks to remove.
- Some number (unknown) of unit tests whose mocks need integration-
  test counterparts authored in
  `nanobrain/tests/integration/` (or T-tickets filed if authoring
  is blocked).
- CI rule added at `nanobrain/scripts/check_mock_parity.py` + wired
  into `pre-commit` on the nanobrain side.

**Time estimate:** 3–4d total effort; if the audit shows ≤3 files
with mock contamination, a smaller carve-out (e.g. "those 3 files
only") is possible.

**This carve-out will be proposed as a separate doc once #1 lands.**

---

## Proposed carve-out #3 — follow-on TransformLink bug fixes

**Ask:** authorization to fix bugs in the TransformLink path
(`nanobrain/core/link.py`) that surface during operator-run
integration testing, without requesting approval each time.

**Rationale:** I shipped the TransformLink YAML loader in CO-T13x.
Phase 2 of T-COMP will emit workflow YAMLs that reference
TransformLinks. If the operator finds edge cases (e.g., a
TransformLink that should retry on target-unavailable, a
diagnostic message that's unclear), I'd like to fix them without
a per-fix approval round-trip.

**Scope boundary:**

- Only files touched: `nanobrain/core/link.py` and
  `nanobrain/tests/unit/test_transform_link.py`.
- Only changes: bug fixes + error-message improvements +
  additional tests. No new features; no changes to existing public
  surface.
- Each fix committed as a separate on-disk edit with a commit-
  message-style comment in the code naming the symptom fixed.

**Time estimate:** 0.25–0.5d per operator-reported issue; expected
≤3 issues over the life of Phases 2–4 of T-COMP.

**Why not wait and ask each time:** a Phase-2 test failure is
cheaper to fix within the same session than to queue for a later
approval round-trip.

**Counter-argument I want to flag honestly:** "standing
authorization to edit a read-mostly repo" is exactly the kind of
scope creep workspace CLAUDE.md exists to prevent. You may
reasonably decline and require per-fix approval.

---

## Proposed carve-out #4 — async-contract warning silencer (SMALL)

**Ask:** one tiny addition to `nanobrain/core/step.py`: an
optional `suppress_async_warning: bool = False` field on
`StepConfig` that silences the
``VALIDATION WARNING: Step {name}.process() is async but contains
no 'await' statements`` heuristic warning when the step author
knows the wrapped function is intentionally sync.

**Rationale:** the warning fires for every sync-wrapping step in
apecx_integration (three of mine: `EntityExtractionStep`,
`SynonymLLMProposalsStep`, `ViolinEntityLookupStep`, plus others).
The warning is heuristic-level noise for a valid pattern.

**Expected diff:** ~20 lines across `step.py` (add the field +
short-circuit check) + a 2-test unit coverage addition.

**Time estimate:** 0.5d.

**Blast radius:** very low — opt-in; default behavior unchanged.

**Counter-argument I want to flag honestly:** the "right" fix
might be to change the heuristic itself (check if the wrapped
function is sync and skip the warning automatically). That's a
bigger change; the opt-in field is a safe incremental step.

---

## What I'm NOT asking for

- **Write access to `nanobrain/nanobrain/library/`.** The library
  has domain-specific step implementations; changes there need their
  own per-task carve-out (same process as core).
- **Changes to `nanobrain/nanobrain/academy_integration/`.** HPC
  territory; not in any of my current task dependencies.
- **Deletion of existing nanobrain files.** No current need.
- **Rewriting `nanobrain/core/workflow.py` or `step.py` beyond
  the narrow additions named in carve-outs #3 and #4.** Framework
  surface changes need a broader conversation than a single
  carve-out.

---

## Decision format (what to do with this doc)

For each carve-out above, pick one:

- **Approve.** I'll proceed when it's the next chain item.
- **Approve with modifications.** Name the modifications.
- **Defer.** I won't start it; I'll keep flagging when its
  dependencies come due.
- **Deny.** I'll remove the carve-out from my candidate list and
  not propose it again.

You can do this inline in the next user turn ("approve #1 and #4;
defer #2 and #3") and I'll act on it immediately.

---

## Why I'm proposing this now

Three signals converged:

1. Friction log #6 — nanobrain ungit'd — is growing risk; any
   carve-out I ask for is also an opportunity to surface that risk.
2. T14 has been the "one unblocked plan task" for several turns
   now; scoping it properly is overdue.
3. The TransformLink work (CO-T13x) proved I can ship in
   `nanobrain/core/` without breaking framework-wide tests. That's
   evidence the carve-out pattern works when scoped tightly.

If the whole menu is too much, approving just #1 (read-only audit)
is the cheapest next step and unblocks the concrete carve-out #2.
