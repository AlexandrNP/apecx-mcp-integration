# Nanobrain mock-contamination audit — 2026-04-23

Produced under **carve-out #1** (read-only) from
`docs/nanobrain_carveout_proposal.md`. User approved the audit carve-out
in turn 2026-04-23 with scope "T14 audit only; estimator work is not a
priority and can be deferred."

**Zero nanobrain files modified in producing this audit.** All findings
came from grep + `sed -n` reads of the nanobrain tree at
`/Users/onarykov/Downloads/apecx-cowork/nanobrain/`.

---

## 0. Brutal-truth summary

**The nanobrain framework ships with production-path mock fallbacks,
NOT just unit-test mocks.** The 2026-04-21 mocks policy (workspace
CLAUDE.md) forbids exactly this pattern: "no mock branches, no
fallback returns, no synthetic data substitution" in production code.

The top three violations:

1. **Academy integration hard-codes mock responses** — every
   `AcademyLink` call returns synthetic data today. The real Academy
   call is a commented "would go here" stub (`academy_integration.py`
   line ~170).
2. **A2A agent protocol silently falls back to mock responses** when
   `aiohttp` isn't installed (`a2a_support.py` 9 `_mock_*` methods).
   Operators have NO way to turn this off — the gate is a library-
   present check, not a config flag.
3. **The shipped `global_config.yml` sets `use_mock_clients: true` as
   the default** (line 151). `is_development_mode()` in
   `config_manager.py` reads this flag. Callers branching on it are
   an unknown; need follow-up (see §3).

Plus:

4. **PubMed client returns empty-placeholder results** today marked
   "Phase 4A infrastructure testing" (`pubmed_client.py` line 621).
5. **Workflow `_rollback_workflow` is a placeholder** that at least
   re-raises the original error. Not a mock per se — documented
   stub. Acceptable until full rollback lands.

**Zero `unittest.mock` / `MagicMock` imports were found in
`nanobrain/nanobrain/`.** One hit at
`nanobrain/scripts/test_phase4_configuration_updates.py` (scripts
tree, not production source). The "legitimate unit-test mock
without matching integration test" category is effectively empty.
Nanobrain's test tree is also empty — only 2 unit-test files total
(`test_approval_step.py` + the `test_transform_link.py` I authored
2026-04-23).

---

## 1. Methodology

Four grep patterns were run against
`/Users/onarykov/Downloads/apecx-cowork/nanobrain/nanobrain/`
and, for pattern 1, the broader nanobrain root:

```
1.  unittest\.mock | from unittest import mock | MagicMock | AsyncMock | Mock\(
2.  is_mock | _mock_ | MOCK | dev_mode | DEV_MODE
3.  # stub | TODO.*stub | fake_ | placeholder
4.  synthetic | fallback.*data | hardcoded.*return      (core/ only)
```

Each hit was then classified per the carve-out proposal's four
categories:

- **A** — Legitimate unit-test mock with matching integration test. OK.
- **B** — Unit-test mock without matching integration test. Needs
  integration test OR T-ticket.
- **C** — Production-path mock fallback. **Must be removed** (or
  replaced with raise).
- **D** — Developer-mode convenience. **Only OK if gated behind**
  `NANOBRAIN_DEV_MODE` env var OR equivalent explicit opt-in.
- **E** (new sub-category, found during audit) — Placeholder stub
  that returns synthetic success data. Same failure mode as C but
  the intent differs ("we'll implement this later" vs. "we fall back
  when real thing is unavailable"). Treated as C for T14 purposes.

---

## 2. Findings — per category

### Category A — legitimate unit-test mocks with integration coverage

**Count: 0.**

The only `unittest.mock` hit in the entire nanobrain tree is at
`nanobrain/scripts/test_phase4_configuration_updates.py:27`, a
scripts-tree test. No integration counterpart is required for
scripts-tree tests, so this is noise, not a T14 concern.

### Category B — unit-test mocks without matching integration tests

**Count: 0.**

Nanobrain's test tree (`nanobrain/tests/`) contains only two files
total: `test_approval_step.py` (pre-existing) and
`test_transform_link.py` (I authored 2026-04-23 as part of the
TransformLink carve-out). Neither imports `unittest.mock`.

**Secondary finding not in T14's scope:** nanobrain has essentially
no test coverage. This is a systemic issue (implementation_plan
§T02 partially; architectural_plan §5.14 speaks to test-coverage
broadly) but not what T14 is tasked to fix.

### Category C — production-path mock fallbacks (T14-blocking)

**Count: 2 distinct violations, 10+ call sites between them.**

#### C1 — `core/a2a_support.py` silent mock fallback

**Pattern:** when `AIOHTTP_AVAILABLE` is False OR the agent session
isn't active, A2A methods return mock responses instead of raising.

Example at `a2a_support.py:626`:
```python
if AIOHTTP_AVAILABLE and agent_name in self.agent_sessions:
    # real HTTP call
    ...
else:
    # Mock execution
    return await self._mock_send_task(agent_name, ...)
```

**Affected methods:** `connect_to_agent` (creates a `MockA2ASession`),
`discover_agent_capabilities` (`_create_mock_agent_card`), `send_task`
(`_mock_send_task`), `get_task` (`_mock_get_task`), `cancel_task`
(`_mock_cancel_task`).

**Why this is a T14 violation:**
- **The gate is "aiohttp installed."** Operators who deploy without
  aiohttp (accidentally or intentionally) get mock responses
  silently. No warning, no error.
- **The mock methods (`_mock_*`) are defined as async methods on the
  main `A2AIntegration` class, not in a test module.** They're
  production code paths.
- **The caller cannot distinguish a successful real call from a
  successful mock call.** Same return types; no flag.

**Proposed fix for carve-out #2:**
- Replace every `else: return await self._mock_*(...)` with
  `raise A2ANotAvailableError(reason=...)`.
- Move `_mock_*` methods to a separate test-helper module at
  `tests/helpers/a2a_mocks.py` (currently nonexistent).
- Callers who WANT a mock for testing construct it explicitly.

#### C2 — `core/academy_integration.py` always returns mock

**Pattern:** the Academy agent proxy always calls
`_generate_mock_response` regardless of input.

`academy_integration.py:165-171`:
```python
if not hasattr(self.manager_wrapper.manager, 'agents') or not self.manager_wrapper.manager.agents:
    self.logger.info(f"🎭 Using mock response for Academy agent ... - no agents deployed")
    return self._generate_mock_response(action_name, args, kwargs)

# Real Academy agent call would go here
# For now, always use mock response
return self._generate_mock_response(action_name, args, kwargs)
```

**Reading of the code:** there is no real Academy-agent call path.
Both branches return mock data. The comment "Real Academy agent
call would go here" is a TODO that's shipped.

**Why this is a T14 violation:**
- Every Academy-backed workflow in nanobrain is running on synthetic
  data today.
- Callers building on Academy (workspace has `examples/academylink_*`)
  are building on a mock foundation.
- Distinct from C1: C1 at least tries the real call first; C2 never
  does.

**Proposed fix for carve-out #2:**
- Two-option fork:
  - **(a) Implement the real Academy call.** Out of scope for T14;
    requires Academy integration expertise.
  - **(b) Raise `AcademyNotImplementedError` until the real call
    lands.** In scope; removes the contamination without breaking
    what's never worked.
- **Recommendation: (b).** The workspace CLAUDE.md policy "no
  synthetic data substitution" is stronger than the code's
  "pretend Academy works."

### Category D — developer-mode convenience (partially gated)

**Count: 1 config surface, multiple config callers.**

#### D1 — `config_manager.py` / `global_config.yml` `use_mock_clients` flag

**Pattern:**
- `global_config.yml:151` ships with `use_mock_clients: true` as the
  default.
- `config_manager.py:857` defines the DEFAULT config dict with
  `'use_mock_clients': True`.
- `config_manager.py:1091` — `is_development_mode()` returns
  `self._config.get('development', {}).get('use_mock_clients', False)`.
  Defaults to `False` when the flag is absent, but `True` when the
  YAML is loaded.

**Callers (grep `is_development_mode`):** only 1 caller of the
flag-reader function shows up in the immediate grep; a deeper audit
(per-caller spot-read) is needed to enumerate what `use_mock_clients:
true` actually enables at runtime.

**Why this is borderline:**
- The flag itself could be a legitimate gate (pattern matches T14
  category D — dev-mode convenience).
- BUT the default is `true`, not `false`. That's the problem: a
  flag that's ON by default isn't really a gate.
- And the YAML comments don't tell the operator what to do with it.

**Proposed fix for carve-out #2:**
- Enumerate all callers of `is_development_mode()`.
- For each caller: document what "dev mode = on" does (presumably
  routes to one of the C1/C2 mock paths).
- Flip the default to `use_mock_clients: false` in `global_config.yml`.
- Rename the flag to `use_mock_clients_DANGEROUS` or at minimum add
  a shipped comment warning.
- Add a startup-log message when the flag is active: "⚠️  nanobrain
  running in dev-mode — real clients replaced with mock responses."

### Category E — placeholder stubs (T14-adjacent)

**Count: 2 distinct violations.**

#### E1 — `library/tools/bioinformatics/pubmed_client.py` placeholder

**At line 621:**
```python
# Phase 4A implementation: Return placeholder for infrastructure testing
# TODO: Implement actual PubMed API calls in Phase 4B
placeholder_references = []

if self.pubmed_config.cache_results:
    self.search_cache[cache_key] = placeholder_references

return placeholder_references
```

**Why this is E and not C:** it's not gated on a runtime condition;
it's an unconditional placeholder. The caller always gets `[]`.

**Fix:** raise `NotImplementedError("PubMed search is Phase 4B; returning empty list silently is not acceptable per workspace mocks policy")` until Phase 4B. Then the cache-wrapping also needs removal (currently caches the empty list).

#### E2 — `core/workflow.py:_rollback_workflow` placeholder

**At line 2899:**
```python
async def _rollback_workflow(self, failed_step_id: str, error: Exception) -> None:
    """Rollback workflow state after step failure."""
    # This is a placeholder for rollback logic
    # In a full implementation, this would: ...
    raise error
```

**Why this is OK:** the function RAISES the original error. It
doesn't pretend to have rolled back. The stub is cosmetic — the
behavior is equivalent to having no rollback handler.

**Recommendation:** leave as-is. The docstring is honest about the
stub status; no synthetic-success risk.

---

## 3. Follow-up questions the audit didn't answer

These require additional investigation (either as part of carve-out
#2 or as separate spot-audits):

1. **Who calls `is_development_mode()`?** The grep found the function
   definition but an unknown number of callers. Each caller likely
   has its own mock-path; those paths need the same C-category
   treatment.
2. **Does `MockA2ASession` at `a2a_support.py:~530` have other call
   sites?** It's instantiated in `connect_to_agent`; unclear whether
   it's used elsewhere.
3. **Is the `aiohttp` import guard intentional or accidental?**
   (Is aiohttp supposed to be optional, or is this a "if it happens
   to be missing" pattern?)
4. **Academy integration examples** — `demos/academylink_aurora_demo/`
   — do they rely on the mock behavior? Breaking the mock may break
   documented demos that operators have been running.
5. **`nanobrain/library/__init__.py:294`** — "Define placeholder
   exports" comment; need to verify it's a conditional-import guard
   and not another E-category stub.

---

## 4. Proposed carve-out #2 scope (for your approval)

Based on the findings above, carve-out #2 (WRITE access to specific
nanobrain files) would cover:

| File | Change type | Risk |
|------|-------------|------|
| `nanobrain/core/a2a_support.py` | Replace `_mock_*` fallbacks with `A2ANotAvailableError`; move mock helpers to `tests/helpers/a2a_mocks.py` | Medium — breaks any caller that's been silently relying on the mock fallback |
| `nanobrain/core/academy_integration.py` | Replace `_generate_mock_response` returns with `AcademyNotImplementedError` | High — every Academy-backed workflow / demo breaks until the real integration lands |
| `nanobrain/config/global_config.yml` | Flip `use_mock_clients: true` → `false`; add warning comment | Low — operators who want the old behavior set it explicitly |
| `nanobrain/core/config/config_manager.py` | Flip the default-dict value; add startup-warning log when flag is True | Low |
| `nanobrain/library/tools/bioinformatics/pubmed_client.py` | Raise `NotImplementedError("Phase 4B")` instead of returning placeholder | Low |
| `nanobrain/tests/` | Author integration tests for a2a + academy (OR file T-tickets in `tests/integration/TODO.md` if authoring blocked on environment) | Medium |

**Total effort estimate: 2–3 engineer-days.**

**The Academy case (high-risk row) is the one that needs the most
deliberation.** Options before ripping out the mock:
- **(a) Extract the mock helper into `demos/` so the demos keep
  running.** Demos explicitly acknowledge "this is a demo mock."
- **(b) Gate the mock behind an `ACADEMY_DEMO_MODE=1` env var.**
  Demos set it; production doesn't.
- **(c) Remove the mock entirely and accept demo breakage.**

Honest recommendation: **(b)**. It's the minimal-blast-radius change
that honors the workspace policy.

---

## 5. Decision needed

For **carve-out #2** (the write-phase to nanobrain based on this
audit), please approve or modify:

- **Approve the full table in §4.** I proceed file-by-file.
- **Approve partial.** Name which rows to include ("approve a2a +
  config flag rows only; defer academy and pubmed").
- **Defer.** I document findings as this audit + stop; no writes.
- **Deny.** I remove T14 from my candidate list.

**Follow-up questions from §3** — if you'd like those resolved
before deciding on carve-out #2, I can run a **carve-out #1b**
(still read-only) to answer them. ~0.25d additional.

---

## 6. What this audit deliberately did NOT find

The audit scoped itself to mock contamination per T14's policy
definition. It did NOT check for:

- **Broader code quality** (dead code, TODOs, commented-out blocks)
  beyond mock-adjacent patterns.
- **Security issues** (credentials in code, insecure defaults beyond
  the mock flag).
- **Architectural debt** (whether the framework's abstractions make
  sense).
- **Test coverage shortfalls** beyond the "no tests import mock"
  observation.

Any of these could be a separate audit with its own carve-out
scope. Flag if you want one.

---

## 7. Appendix — raw grep output summary

```
grep 1 (unittest.mock / MagicMock / Mock() in nanobrain/nanobrain/):
  0 hits
grep 1 (same in full nanobrain/ tree):
  1 hit at scripts/test_phase4_configuration_updates.py:27

grep 2 (is_mock / _mock_ / MOCK / dev_mode in nanobrain/nanobrain/):
  25 hits. Classified:
  - 9 in a2a_support.py (Category C)
  - 3 in academy_integration.py (Category C)
  - 4 in workflow_orchestrator.py + workflow_divergence.py —
    "NO MOCKS!" comments, defensive, not violations
  - 2 in config_manager.py (Category D)
  - 1 in global_config.yml (Category D)
  - remainder in config/elasticsearch_tool.yml and similar —
    low-risk config fields

grep 3 (# stub / TODO.*stub / fake_ / placeholder):
  ~15 hits, most are docstring mentions of "placeholder" as
  LangChain prompt-template terminology (agent.py, workflow.py).
  Real stubs: pubmed_client.py (Category E), workflow.py rollback
  (Category E but acceptable).

grep 4 (synthetic / fallback.*data / hardcoded.*return in core/):
  2 hits, both in async_logging.py — fallback log path when
  structured logging fails. Not a mock; acceptable fallback.
```
