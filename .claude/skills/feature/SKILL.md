---
name: feature
description: The phased flow for any NON-TRIVIAL code change — plan → implement → self-review → critical review → commit with a Reviewed trailer. Use this whenever a change touches more than one code file, adds or edits logic/control-flow, introduces a new function/class/abstraction, or exceeds ~20 changed lines. It exists because "tests pass" is not "code is good": this skill forces the up-front plan and the critical review that momentum tends to skip, and ends at a commit the review gate will accept.
---

# /feature — phased flow for non-trivial work

Run the phases **in order**. Do not collapse them. The cost is a few round-trips; the
payoff is no speculative code, no duplication, no unreviewed diff in history.

**Trivial work skips this skill entirely** — typos, renames, comments/docs, a single small
`.py` change, config-value tweaks. (Definition matches the workspace `CLAUDE.md` "Rigor
policy" and the `review_gate.py` classifier: non-trivial = >1 code file, OR logic/control-flow
change, OR a new function/class/abstraction, OR >~20 changed lines.)

## Phase 1 — Scope & plan (before touching any file)
- `EnterPlanMode`. Explore only what you need to design the change; reuse before you build
  (grep for an existing helper/constant/utility first).
- Write the plan to the plan file: the files you will change, the approach, a one-line
  **necessity justification** per change, and an explicit **"what I will NOT build"** list.
- `ExitPlanMode` for approval. Do not edit source before approval.

## Phase 1b — Detailed implementation plan (after high-level approval, before code)
Expand the approved plan into a DETAILED plan carrying ALL FIVE — high-level alone is not
enough to start coding:
1. **File:line refs** — `path:line` for every edit, not bare filenames.
2. **Code snippets** — the actual new/changed code (before→after for edits).
3. **Acceptance criteria rooted in real data** — runnable checks on real inputs/outputs (a
   command + the concrete expected value), never "works correctly".
4. **Tests** — named test functions + the case each pins, mapped to each change.
5. **Task graph** — ordered tasks with explicit `blocks` / `blocked-by` dependencies.
Re-confirm via `ExitPlanMode` only if the detail surfaces a material change to the approved
approach; otherwise proceed to Phase 2.

## Phase 2 — Implement
- Smallest change that satisfies the request. Match the surrounding code's style, naming,
  and comment density. No speculative fields, options, or abstractions.

## Phase 3 — Self-review (the part momentum skips)
Answer each, briefly and honestly — this becomes the commit's `Reviewed:` trailer:
- **necessity** — does every changed line trace to the request? what did you NOT build?
- **minimality** — could this be smaller? anything speculative to cut?
- **readability** — does it read like the surrounding code?
- **DRY** — any duplicated constant / helper / logic? (a list defined twice is a smell)
- **deleted** — what did you remove, or why was nothing removable?

Then run the advisory policy scan on changed Python:
`python <workspace>/.claude/scripts/review_policy_check.py <changed.py> …`

## Phase 4 — Critical review
Invoke the **review-gate** agent on the diff (a fresh, adversarial pass — not your own
narration). Address every FAIL/PASS-WITH-NOTES finding, or record a one-line justification
for why it stands. If review-gate says FAIL, you are not done.

## Phase 5 — Commit
- Commit with a `Reviewed:` trailer recording Phases 3–4 (the `review_gate.py` commit-msg
  hook BLOCKS a non-trivial commit without it). Example:
  ```
  feat(x): short imperative summary

  Body explaining the why.

  Reviewed: necessity ok (all lines trace to the task); minimality - cut the unused
  taxon_iris field; readability matches the _datacite helpers; DRY - reused
  datacite_primary_id rather than a 2nd precedence list; deleted the dead identifier branch.
  ```
- `git show --stat HEAD` and confirm the files + counts match the message (commit-integrity
  rule — a wrong-directory `git add -A` ships a misleading commit).
- Push only on explicit user request.

## Notes
- The commit-msg gate is the backstop; this skill is the spine. The gate catches a missing
  review even when the skill was not run — but it cannot author the plan or the critical
  pass, only enforce the trailer. Don't write the trailer without doing the review.
- One concern per branch/worktree (`git worktree add ../wt-<task> -b <branch>`).
