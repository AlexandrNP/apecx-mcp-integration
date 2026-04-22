# Scope Decision 06 — Nanobrain framework patches

**Date:** 2026-04-22
**Status:** **Applied** under the nanobrain carve-out established by
scope memo 01 (case-by-case edits approved) and the user's
2026-04-22 directive to consolidate the framework-level workarounds.
**Triggered by:** five separate workaround commits across the
session stream all converged on the same underlying framework gaps;
left unfixed they produce compounding friction.

---

## Three patches applied

### Patch 1 — `str_strip_whitespace=False` on `ConfigBase.model_config`

**File:** `nanobrain/nanobrain/core/config/config_base.py:626`

**Before:** `str_strip_whitespace=True`.

**After:** `str_strip_whitespace=False`.

**Rationale:** With strip-on by default, any step YAML carrying a
whitespace-bearing string field (e.g., `delimiter: "\t"`) arrived at
the step's `_init_from_config` as an empty string. Surfaced during
T02 Phase 3 when `DelimitedFileReaderStep` hit it and had to
work around via a `Literal["csv","tsv"]` enum. Other steps that
want whitespace-bearing fields (separators, leading-whitespace
literals, templates) would each need their own workaround.

**Impact:** fields that genuinely expected trim-behavior (e.g., a
user-entered prompt with trailing spaces) now keep the whitespace.
Callers should strip explicitly in their validators when that's the
right semantics. Full test suite (218 tests, 5 skipped) still
passes.

**Workarounds that can now be simplified:** the `format` enum in
`DelimitedFileReaderStepConfig` could be replaced with a direct
`delimiter: str` field — but the enum is arguably better UX
(`format: tsv` reads clearer than `delimiter: "\t"`). Left as-is.

### Patch 2 — `aiohttp` declared as a runtime dependency

**File:** `nanobrain/pyproject.toml`

**Before:** not declared.

**After:** `"aiohttp>=3.9"` added to `project.dependencies`.

**Rationale:** `library/tools/bioinformatics/bv_brc_tool.py` imports
`aiohttp` at module top. Previously undeclared — module import
failed with `ModuleNotFoundError` on a fresh install. Same shape of
gap as the 2026-04-21 aiofiles fix (scope memo 03).

### Patch 3 — `aiosqlite` declared as a runtime dependency

**File:** `nanobrain/pyproject.toml`

**Before:** not declared.

**After:** `"aiosqlite>=0.19"` added to `project.dependencies`.

**Rationale:** `nanobrain/library/__init__.py` eagerly imports
workflows that pull in aiosqlite. Without it, `import
nanobrain.library` raises `ModuleNotFoundError`. Surfaced during
T10 (hit by the `nanobrain-coder` subagent on 2026-04-21).

---

## What is NOT patched (deliberately)

- **Env-var interpolation on nested dict values.** Step YAMLs that
  carry `control_plane.base_url: "${CONTROL_PLANE_URL}"` arrive at
  the step with the literal string, not the resolved value. Fix
  would live in the config loader's string-processing pass; ~0.5d
  to implement and test. Not done because (a) the callers that
  care override `_http_client_factory` in tests and use the real
  value at production runtime injection, and (b) the fix scope
  grows into "what about `${VAR:-default}` syntax?" which is a
  whole language. Logged in `docs/future_work.md` with the trigger
  being the first component that actually needs interpolated-at-
  load-time nested config.
- **Workflow `steps` public accessor.** The `Workflow` class stores
  composed step instances somewhere, but the attribute isn't
  documented publicly. Tests that want to assert on composed steps
  currently can only rely on `workflow.name`. Not a bug exactly —
  more a documentation gap. Skip for now.
- **aiosmtpd's Errno 49 bug on `port=0`**. Upstream aiosmtpd
  bug, not a nanobrain issue. Our workaround
  (`_pick_free_port()`) is local to the T08 test fixture. Filing
  upstream is not part of this memo.

---

## Why these patches and not others

The patches applied share a specific pattern: each one consumed
0.25d–1d of workaround time in a single task and would compound
across every future task that hit the same shape. Fixing at the
framework layer saves that compounded cost.

Patches NOT made here either have no current caller (so no
workaround to consolidate) or are not clearly bugs (framework
internals whose shape I can't decide without a larger design
conversation).

---

## Verification

- Test suite before patches: 218 passed.
- Test suite after patches: 218 passed, 5 skipped, 1 warning
  (pre-existing Pydantic serialization warning from the
  Workflow's `validate_graph` validator; unrelated).
- Pre-commit run --all-files: all 8 hooks green.

---

## Signatures

- **Patches authored:** 2026-04-22, under the nanobrain carve-out
  per scope memo 01.
- **Related memos:** 01 (where new code lives), 02 (ApprovalStep
  in nanobrain), 03 (nanobrain packaging fix for aiofiles).
- **User directive:** 2026-04-22 "Proceed with your recommendations
  and chain next tasks" — this memo is the first recommendation
  from my prior-turn report.
