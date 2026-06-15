# Port plan — canonical resolver trio (reasoning-agent-surface → main)

**Status:** PLAN ONLY (no code touched). Investigation deliverable, 2026-06-15.
**Source branch:** `reasoning-agent-surface` (worktree `wt-reasoning-agent`).
**Target:** `main` (worktree `wt-main`).

Goal: move the *self-contained* canonical-resolution bundle onto main WITHOUT
pulling the entangled GENERATE arc. This plan is file-by-file, names the one
load-bearing integration risk, and explicitly excludes two files that look part
of the bundle but are not safe to port.

---

## 1. Why (value)

Main resolves entities two ways that have already drifted:
- `HarmonizedResolveStep.process()` carries ~150 lines of **inline** ambiguity
  detection (curated `ambiguous_surface_forms` table + `lookup_any_type`
  fallback + multi-candidate override).
- `canonical_entity.py`, `metrics.py`, `workflow/resolve_step.py` each call
  `lookup_entity()` directly and re-derive "is this ambiguous?" their own way.

The branch centralizes this into one `resolve_term() → ResolutionResult` with
unambiguous `.is_resolved` / `.is_miss` / `.needs_disambiguation` verdicts, and
adds a **writable verified-synonym overlay** so a user's disambiguation choice
("RSV means *human respirovirus*") persists and is consulted first on re-query
— closing the HITL loop that today re-prompts every time.

**Key de-risking fact:** `resolver.resolve_term` is built *on top of*
`lookup_entity` (it imports it). So the port is **ADDITIVE, not a breaking
replacement** — `lookup_entity` stays; `resolve_term` is a new layer above it.
Only call sites we *choose* to migrate change. This is what makes the minimal
port low-risk.

---

## 2. Scope

### IN — minimal additive port (recommended)
| File | Type | Notes |
|---|---|---|
| `synonym_dictionary/overlay.py` | NEW | writable SQLite overlay; dep `normalize_surface_form` **exists on main** ✓ |
| `synonym_dictionary/disambiguation.py` | NEW | pure Pydantic envelope models; zero deps |
| `synonym_dictionary/resolver.py` | NEW | `resolve_term`; imports `lookup_entity` + `overlay` (additive) |
| `mcp_surface/tools/confirm_synonym.py` | NEW | the WRITE side of HITL disambiguation; register in `server.py` |
| 6 unit tests + 1 temp-SQLite test | NEW | all pure-unit / temp-SQLite — **no Ollama, no Globus, no real dict** |

### IN — but gated behind the one real risk (see §4)
| File | Type | Notes |
|---|---|---|
| `composition/steps/harmonized_resolve_step.py` | MODIFIED | swap inline logic → `resolve_term()`; **changes `resolution_status` enum→string** |

### OUT — deliberately excluded (NOT safe to port now)
| File | Why excluded |
|---|---|
| `mcp_surface/tools/viral_immunology_analysis.py` | Re-introduces a tool **main deliberately RETIRED** (apecx `7b01236`, for bypassing harmonized search — see memory `audit-standalone-mcp-tools-against-architecture`). Also drags `ViralImmunologyQueryClassifierStep` (generate-arc-adjacent). Porting it would re-open a closed architectural decision. |
| `composition/steps/_organism_context.py` | Its consumers on main (`SynthesisContextAssemblyStep`) don't call it yet — porting it alone is dead code. Its deps DO exist on main (`_HARMONIZED_FILTER`, `_INDEX_UUIDS`, `UnlimitedSynthesisAssemblyStep` ✓), so it ports cleanly LATER, paired with wiring a consumer. |
| The entire GENERATE arc | Confirmed entangled (workflow_gate / dry_run / generate / promotion / find_workflow + control-plane routes). Separate bulk port. |

---

## 3. Phased plan with verify gates

### Phase A — the leaf layer (zero production risk)
1. Copy `overlay.py`, `disambiguation.py` verbatim. Both are leaves (overlay
   imports only stdlib + `normalize_surface_form`; disambiguation is pure
   Pydantic).
   → **verify:** `pytest tests/unit/test_synonym_overlay.py
   tests/unit/test_disambiguation_envelope.py` green under `.venv`.
2. Copy `resolver.py`. It wraps `lookup_entity` + `overlay.get_synonym_overlay`.
   → **verify:** `pytest tests/unit/test_resolver_overlay.py` green; plus a smoke
   `resolve_term("RSV")` returns `needs_disambiguation=True` and
   `resolve_term("chikungunya virus")` returns `is_resolved=True`.

### Phase B — the write surface (new MCP tool, additive)
3. Copy `confirm_synonym.py`; register `confirm_entity_synonym` in
   `mcp_surface/server.py` (one `server.tool()(...)` line, next to
   `harmonized_search`).
   → **verify:** `pytest tests/unit/test_confirm_synonym_tool.py` green; server
   boots (`apecx-mcp --help` / a smoke that builds the server) and the tool is
   on the wire.

### Phase C — the one production refactor (GATED — see §4)
4. Port `harmonized_resolve_step.py`: replace inline ambiguity logic with
   `resolve_term()`, add `_compute_filter_iris` (NCBITaxon-descendant expansion).
   → **verify (unit):** `pytest tests/unit/test_harmonized_resolve_step.py`.
   → **verify (integration — THE gate):** drive the real harmonized_search
   resolve→execute path on an AMBIGUOUS term ("RSV") and a clean term
   ("Chikungunya virus") against a real synonym dict, and assert: (a) the HITL
   pause STILL fires for "RSV" (candidate IRIs returned, not a silent pick),
   (b) `_hitl_gate.py` and `canonical_entity.py` still read `resolution_status`
   correctly after the enum→string change. See §4.

---

## 4. The one load-bearing risk — `resolution_status` enum → string

Branch `harmonized_resolve_step` emits `resolution_status` as a **string**;
main emits it as an **enum value**. On main, `resolution_status` is read by:
- `mcp_surface/tools/_hitl_gate.py` — decides whether to PAUSE for disambiguation.
- `mcp_surface/tools/canonical_entity.py` — the `resolve_canonical_entity` tool.

If either compares against an enum member (`== ResolutionStatus.AMBIGUOUS`), a
string value silently fails the comparison → **the HITL pause stops firing and an
ambiguous term gets silently resolved to one taxon** — exactly the
dominant-silent-failure shape this codebase guards against.

**Mitigation (pick one, decide at port time):**
- **(a) Preserve the enum** in the ported step's output (convert the resolver's
  string back to the existing enum at the step boundary) — smallest blast radius,
  no consumer changes. **Recommended.**
- **(b) Migrate both consumers** to the string contract + add `extra='forbid'`-
  style guards. Larger, but aligns fully with the branch's single-contract intent.

Either way the Phase-C integration test (real "RSV" → pause still fires) is the
gate that proves it. Do NOT merge Phase C on unit tests alone — this is precisely
the "smoke passes, runtime cascade silently wrong" class (cf. G99/G127 in the
root CLAUDE.md).

---

## 5. Test burden (verified light)

7 branch test files cover the bundle; **none need Ollama, Globus, or a real dict
artifact**:
- pure unit: `test_disambiguation_envelope.py`, `test_resolver_overlay.py`,
  `test_confirm_synonym_tool.py`, `test_harmonized_resolve_step.py` (stubs
  `lookup_entity`), `synonym_dictionary/test_resolvers.py` (fake OLS).
- temp-SQLite "integration": `test_synonym_overlay.py` (real SQLite on a temp
  path — workspace mocks-carve-out compliant).

The ONE test this plan ADDS that the branch lacks: the Phase-C real-dict
ambiguous-term integration test (§4). The branch's `test_harmonized_resolve_step`
stubs `lookup_entity`, so it would NOT catch the enum→string consumer break.

---

## 6. Effort + sequencing

- Phases A+B (leaf layer + write tool): low risk, mechanical copy + one tool
  registration. The bulk of the value (persistent user disambiguation) lands here.
- Phase C (the resolve-step refactor): the only phase with real risk; gated on
  §4's integration test. Can be deferred independently — A+B are useful without it.
- One branch + worktree (`git worktree add ../wt-resolver-trio -b resolver-trio-port`).
- Cite this plan + the source branch commits in the PR body.

**Recommendation:** land Phases A+B as one PR (additive, near-zero risk, immediate
HITL-loop-closing value), then do Phase C as a separate gated PR once the §4
mitigation is chosen and its integration test is written.
