# Scope Decision 04 — Mocks policy (T00.4 AC2)

**Date:** 2026-04-21 (parity rule addendum same day)
**Status:** **Decided** — the policy is live in workspace `CLAUDE.md` and is already being enforced by the `no-ungated-mocks-in-src` pre-commit hook.
**Triggered by:** implementation_plan.md T00.4 — formalize the mocks-rule
policy decision, which has been made but had never been written down as
a scope memo with explicit rules.

---

## The decision

Mocks are allowed **only** in two cases:
1. **Smoke tests** that exercise wiring shape (constructor signatures,
   imports, config loading).
2. **Unit tests of pure transformations** (dict → dict) or unit tests
   of a component that mocks an external dependency (DB, HTTP client,
   LLM, file I/O).

In case (2), the mock-only coverage is **not sufficient**. Every
mock-covered behavior must also have a matching **integration test**
that exercises the same behavior against the real dependency.

Production code (everything under `src/`) must not contain mocks of
any kind. No fallback branches, no synthetic data substitution, no
"return a placeholder if the real call fails."

---

## Scope of this memo

This memo formalizes the decision so implementation_plan.md T00.4 AC2
is satisfied. The substantive rules already live in two authoritative
places:

1. **Workspace `CLAUDE.md`** — Non-Negotiable Rule 1 (Real data, not
   synthetic), the **Mocks Carve-Out** table, and the **Unit-mock /
   integration-test parity rule (2026-04-21)**. These are the
   day-to-day reference.
2. **`.pre-commit-config.yaml`** — the `no-ungated-mocks-in-src` hook
   enforces the production-code restriction automatically on every
   commit; grep catches `unittest.mock`, `from unittest import mock`,
   bare `import mock`, or `MagicMock` under `src/`.

This memo exists so the plan's T00.4 AC2 ("Decision file exists with
the user's signature/date") is no longer false, and so that T14 ("Fix
mocks-in-nanobrain per the 2026-04-21 policy") has something
unambiguous to point at when it eventually lands.

---

## Carve-out table (verbatim from `CLAUDE.md`)

| Situation | Mocks allowed? |
|---|---|
| Smoke test that exercises the wiring shape (constructor signatures, imports, config loading) | Yes |
| Unit test of a pure transformation that takes a dict and returns a dict | Yes, with realistic fixtures |
| Unit test that mocks an external dependency (DB, HTTP client, LLM, file I/O) | **Yes — but the mocked behavior MUST also be exercised by a matching integration test against the real dependency.** Mock-only coverage is forbidden. |
| Integration test that claims a component works | **No** — must hit a real backend on a real (small) data subset |
| Production code path | **No** — no mock branches, no fallback returns, no synthetic data substitution |
| Debugging a real failure by replacing the failing call with a mock to "make it pass" | **No** — that is the failure mode this rule exists to prevent |

---

## How the unit-mock / integration-test parity rule is tracked

Per the 2026-04-21 addendum in `CLAUDE.md`:

> When authoring a unit test with a mock, record in the test docstring
> (or a sibling comment) which integration test covers the same code
> path. When the integration test is missing, create a TODO in
> `tests/integration/TODO.md` and file it as a T-ticket.

The `tests/integration/TODO.md` sink file is kept in-repo and is
expected to shrink over time as integration coverage grows. An empty
file is a legitimate state (no outstanding parity gaps).

---

## How T14 hooks into this

`T14 — Fix mocks-in-nanobrain` from `implementation_plan.md` scans
the ~15 mock references in `nanobrain/nanobrain/core/mcp_support.py`
(per AP §5.14) and classifies each against the three production-code
cases above:

- **Production-reachable fallback** → remove.
- **Test double that leaked into production** → remove, move to the
  test suite if useful.
- **Dev-mode convenience** → gate behind an explicit `if
  os.getenv("NANOBRAIN_DEV_MODE")` check and document.

The inventory step (T14 AC1) is blocked only by a current-state scan
of `nanobrain/` — the policy itself is now settled.

---

## Signatures

- **User directive establishing the policy:** 2026-04-21 (in-session
  confirmation; lives in workspace `CLAUDE.md` Non-Negotiable Rule 1
  and Mocks Carve-Out).
- **Parity-rule addendum:** 2026-04-21 (same day; `CLAUDE.md` under
  "Unit-mock / integration-test parity rule").
- **Memo authored:** 2026-04-21 as part of the plan-audit backlog
  clearing pass.

No further user sign-off required for the decision itself — this
memo is a faithful transcription of rules already in force. If T14
discovers that a specific mock in `nanobrain/core/mcp_support.py`
doesn't fit cleanly into the three categories above, re-open this
memo with the specific case and add a fourth option.
