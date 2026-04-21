# Scoping Answers — Round 3

**Date of Round 3 answers:** 2026-04-21
**Answered by:** Project owner (verbal, recorded by Claude Code agent)
**Supersedes:** `../../../architectural_plan.md` §1.1 (which listed the questions as open)

This document records the answers to the five blocking questions in `architectural_plan.md` §1.1, plus a Round 3 derived blocker that emerged when the workflow question was only half-answered.

---

## Q1 — What is the first workflow?

**Answer:** VIOLIN × BV-BRC integration.

**Data access constraints:**
- BV-BRC is accessed **via local snapshot only** — files live under `../../data/bvbrc_cache/` (path relative to this file). No live queries to bv-brc.org. No browser automation. If a workflow needs data not in the snapshot, the workflow fails loudly — no silent fallback.
- VIOLIN is accessed via `../../data/violin/` CSV files.

**What is still missing (Round 3 derived blocker):**

"VIOLIN × BV-BRC integration" names a topic, not a workflow spec. Before task T01 (vertical-slice integration test) can be written, the team must commit to:

1. A specific scientific question (example candidate: "given a vaccine ID from VIOLIN, retrieve the matching pathogen's BV-BRC genomes and proteins, cluster, and produce PSSMs for the top N clusters").
2. Which specific files feed the workflow (alphavirus? chikungunya? both? cross-family?).
3. The 3–7 named steps in order.
4. Output artifact shape (columns + types, or file format + schema).
5. Expected laptop wall-time, measured against the closest existing script.

See `implementation_plan.md` task **T00.1b** for the spec-writing task.

---

## Q2 — Who are the first scientists?

**Answer:** Team-owned. The engineer does not need to recruit scientists.

**Implications:**
- Task T15 (end-to-end tutorial) still requires a real scientist to validate it.
- The release gate ("a scientist outside the dev team completes the tutorial and runs the workflow") is still the actual release criterion.
- If the team has not produced a validating scientist by the end of Phase 3 planning, **surface the risk early** rather than discovering it at release time.

---

## Q3 — Which HPC endpoint?

**Answer:** ALCF Polaris + Aurora, **as an optional export target.** Not the default execution path.

**Round 3 reversal:** The architectural plan originally treated HPC as a first-class execution target with local-only for dev. Round 3 flips this:
- **Local execution is default.** Vertical slice runs on a laptop.
- **HPC is an optional export product.** The system produces a PBS bundle (T05) or Globus Compute invocation (T04 if endpoint available) for scientists who opt in.

**Still unknown:**
- Allocation account status on Polaris / Aurora.
- Globus Compute endpoint registration (for T04, which is now optional anyway).
- Queue and filesystem conventions per system.

**Impact on critical path:** none. HPC-export work (T04/T05/T07) is a separate lane in Phase 3 that can be deferred without blocking the local release.

---

## Q4 — Staffing

**Answer:** Agents do the work. Claude Code subagents (orchestrator, python-coder, nanobrain-coder, test-debug-guardian, git-worktree-guardian, review-gate) implement and test; the single human engineer orchestrates and reviews.

**Implications for effort estimation:**
- The unit of work shifts from "engineer-days" to "orchestrator-hours × agent-iterations."
- Code authoring can parallelize (multiple agents concurrently).
- Review, real-data integration testing, and scope decisions do not parallelize and are throttled by the single human engineer's attention.
- New agent-specific risks (R11–R14 in `architectural_plan.md` §R3.5) require an explicit review harness — see task TX5.

---

## Q5 — Testing owner

**Answer:** Single engineer is responsible for verification on real data.

**Implications:**
- That engineer is the single point of failure for every integration test, every scientist conversation, every scope decision, and any HPC interaction requiring credentials.
- One week of engineer unavailability (illness, vacation, competing project) delays every gate.
- This is the **most under-appreciated risk** of the Round 3 staffing model.

---

## Remaining open questions (Round 3)

1. **T00.1b — concrete workflow spec.** See Q1 above.
2. **T00.2 — nanobrain async pause/resume spike.** Still not run. Blocks T10.
3. **T00.4 — mocks policy decision.** Still a human call. Blocks T14.
4. **Integration authorship for `nanobrain/` edits.** Workspace `CLAUDE.md` treats `nanobrain/` as read-mostly. Tasks T02, T02r, T10, T04, T05 all need to edit files under `nanobrain/`. **The user must approve this as a batch carve-out up front**, not file-by-file, or the project stalls at every commit.

---

## Traceability

| Field | Value |
|---|---|
| Source of truth (questions) | `../../../architectural_plan.md` §1.1 |
| Source of truth (answers) | this file |
| Recorded by | Claude Code agent, 2026-04-21 |
| Signed off by | _pending — project owner must sign below_ |

Signature / date: ___________________________
