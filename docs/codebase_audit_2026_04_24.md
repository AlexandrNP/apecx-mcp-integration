# Codebase audit — 2026-04-24

Comprehensive review of brittle patterns and refactoring opportunities
across `apecx-mcp-integration` and the sibling `nanobrain` framework.
**No code was modified.** This document is the deliverable.

## Executive summary

The codebase is in genuinely good shape on the architectural axis:
dependency direction is clean (apecx → nanobrain, no reverse imports),
the from_config contract is mostly enforced, the MCP/Control-Plane/
Composer subsystems are coherent and testable, and recent additions
(Academy real path, Docker sandbox scaffold, A2A happy-path coverage,
T12 fixture diversity) shipped without regressions.

The brittleness is concentrated in three patterns that recur across
both repos:

1. **Silent error swallowing.** `except Exception: pass`,
   `except Exception: log.warning(...)`, `try: ...; except: continue`,
   and `print(f"Error: {e}")`-then-return-success. These appear in
   the composer's suggestion fallback, several nanobrain library
   steps, A2A initialization, and the categorization-row reader on
   `/workflows/diff`. Cumulative effect: pipelines silently produce
   poisoned data instead of failing loudly.
2. **Diagnostic poverty.** Errors are caught but their messages
   don't pinpoint the offending file/component/line. Component
   imports try N strategies and report only "all failed"; config
   resolution swallows the actual exception and rolls to the next
   strategy; class-path strings in YAML manifests aren't validated
   on load. Together these turn a 30-second config typo into a
   2-hour debugging session.
3. **Implicit lifecycle assumptions.** Singletons (Academy manager,
   composer, FastAPI Depends sessions) rely on convention rather
   than contract. The Academy manager has documented teardown but
   no atexit guard; the FastAPI session is held across an
   `await composer.compose(...)` boundary in `/workflows/start`;
   the threading.Lock in the provenance recorder is async-unsafe.

The single highest-impact finding is a **Python-version mismatch
between the two repos**: apecx-mcp-integration's `pyproject.toml`
declares `requires-python = ">=3.11"` but nanobrain's declares
`>=3.12`. Anyone installing apecx into a 3.11 venv will see the
nanobrain editable install fail at install time with a confusing
"requires different Python" error. That is a one-character fix and
should land before anything else in this list.

The audit produced **51 findings** after dedup and downgrade
(86 raw findings before review). Severity distribution:
4 CRITICAL · 13 HIGH · 26 MEDIUM · 8 LOW. Per-area breakdown is in
the **Findings by area** section.

The `nanobrain` repo is upstream and not owned by the apecx team;
findings in §6 and §7 are advisory observations for the upstream
maintainer, not action items for apecx-mcp-integration. The
exception is the Python-version pin (§9.1), which is on apecx's
side of the boundary.

## Audit methodology and limitations

**Coverage.** Eight specialized read-only subagents ran in parallel
across the two repos. Each was given a thematic slice (composer,
control plane, MCP surface, tests, nanobrain core, nanobrain
library, cross-repo coupling, configs/ops/docs) and a structured
output template (severity-tagged finding with `file:line` evidence,
plus a "leave alone" list). Subagent outputs totalled ~6000 words.

**Synthesis.** I reviewed every finding individually rather than
copying the agent reports into this doc verbatim. Three subagent
reasoning errors were caught and corrected in the synthesis:

- An agent inverted the threading.Lock failure mode (claimed it
  "doesn't block coroutines"; it actually does block — that's
  exactly the deadlock risk). Conclusion was right, rationale was
  wrong; rewritten in §2.7.
- An agent's "execute() override has no enforcement" finding
  contradicted its own evidence (the framework DOES validate at
  init); reframed as "validation message could be louder" in §6.5.
- A "writability check" suggested fix introduced a TOCTOU race;
  rewritten in §3.4 to use try/except OSError.

**What this audit does NOT cover.** Performance / latency profiling.
Security review of the LLM-generated-Python execution path beyond
the import-whitelist (Phase-3 work). The apecx-db-integration
sibling (only its imports were checked, not its internals). Live
behavior of the MCP surface against Claude Desktop. Database
migration safety on real production data.

**Confidence calibration.** Findings tagged with ✅ in §10
were directly verified by reading the cited code during synthesis
(11 of 51 findings). The other 40 carry the subagent's evidence as
file:line citations; spot-check before acting on them.

---

## 1. Composer + RAG + Scanner

The composer subsystem (apecx_integration/composition/) is the most
complex piece of apecx code and carries the most load-bearing
contract: a fence regex that has to match an LLM's output format,
a RAG retrieval path that has to fail-loud-not-silent, and a T13
import-scanner whose violations need to surface fixable suggestions.
Most of it is solid; the brittleness is at the edges.

### 1.1 [CRITICAL] Silent exception in retrieval suggestion fallback ✅

**Where:** `src/apecx_integration/composition/composer.py:274-280`
**Pattern:** `_suggest_for_violation()` wraps `_retrieve()` in a bare
`except Exception` that returns an empty tuple.
**Why brittle:** If the RAG index fails to load mid-run (disk full
during a faiss read, or a metadata.json corrupt), the user sees a
ScanViolation with no suggestions and no log line indicating that
retrieval also failed. Two failures merge into one misleading error.
**Fix direction:** Log at WARNING with the original exception and a
"retrieval also failed; suggestions unavailable" message. Keep the
empty-tuple fallback; just don't be silent about it.

### 1.2 [HIGH] LLM response `content=None` is not validated before parsing

**Where:** `composer.py:365`
**Pattern:** `raw_content = getattr(response, "content", str(response))`
falls back to the object repr instead of raising. If a future LangChain
change makes `content=None` legal, the regex parser is fed a string
that will never contain a fence and the error is "no ```yaml block"
instead of "LLM returned empty content."
**Fix direction:** Add a `if not isinstance(raw_content, str) or not
raw_content.strip(): raise ComposerResponseError(...)` immediately
after extraction.

### 1.3 [HIGH] Fence regex won't match a code block with trailing blank line

**Where:** `composer.py:646-650`
**Pattern:** `_FENCE_RE` requires `\n```\n` at end — a closing fence
on a line of its own, with no blank line between it and the body.
**Why brittle:** LLMs trained on GitHub Markdown sometimes emit
`...code\n\n```\n` (blank line before closing fence — valid CommonMark).
Today's prompt prevents it most of the time but model upgrades
could change emission style and silently break parsing.
**Fix direction:** Change to `r"\n\s*```"` or normalize the input
by stripping trailing whitespace before regex match.

### 1.4 [MEDIUM] Whitelist re-loaded from disk on every novel-Python compose

**Where:** `composer.py:375-376`
**Pattern:** `load_whitelist(self._config.sandbox_whitelist_path)` is
called inside `compose()` every time novel Python is detected. The
whitelist file is static config.
**Why brittle:** Not a correctness bug — wasteful I/O and exposes
the composer to transient FS errors (e.g., file-permission flap during
a deploy) that have nothing to do with the composition request.
**Fix direction:** Cache the whitelist in `Composer.__init__` once
and reuse `self._whitelist` per call.

### 1.5 [MEDIUM] Catalog deduplication is silently last-write-wins

**Where:** `component_catalog.py:99-102`
**Pattern:** When two manifests declare the same component id, the
later one silently wins.
**Why brittle:** In Phase-4 multi-library setups the silent override
will make "why isn't component X showing up?" un-debuggable.
**Fix direction:** `log.warning(f"Component {id!r} from {first_path}
shadowed by {new_path}")`.

### 1.6 [MEDIUM] RAG index loader hardcodes `faiss.bin` + `metadata.json` filenames

**Where:** `composer.py:150-157` (and the matching writer in
`scripts/build_rag_index.py`)
**Pattern:** Two-sided string contract with no shared constant or
version field. A rename in one side fails silently in the other.
**Fix direction:** Extract the filenames into a shared constant
(e.g., `nanobrain.lightweight.component_index.INDEX_FILES`) and add
a `manifest.json` with a schema-version field.

### 1.7 [MEDIUM] Hardcoded 5s `docker kill` timeout is non-configurable

**Where:** `docker_sandbox.py:258` (inside `DockerSandboxRunner.run`)
**Pattern:** When the outer container `timeout_seconds` fires, the
fallback `docker kill` has its own hardcoded 5s timeout. Slow Docker
daemons get a cascade of timeout exceptions, and the kill itself can
silently fail.
**Fix direction:** Add `kill_timeout_seconds` to `SandboxConfig`
with a default of 5.0; surface kill-failure in `SandboxResult` so
the caller knows the container may still be running.

### 1.8 [MEDIUM] `except Exception: pass` on the kill fallback

**Where:** `docker_sandbox.py:261-262`
**Pattern:** Bare-Exception swallow on `docker kill`. Kill-failure
returns `killed=True` even when the kill itself errored.
**Fix direction:** Catch `subprocess.TimeoutExpired` and `OSError`
explicitly; let other exceptions propagate. Add a `kill_succeeded`
field to `SandboxResult`.

### 1.9 [LOW] Context dict passed to `yaml.safe_dump` without serializability check

**Where:** `composer.py:594-605`
**Pattern:** Caller-supplied `context` is yaml-dumped as part of the
LLM prompt. Non-serializable values raise inside `yaml.safe_dump` with
an opaque message.
**Fix direction:** Validate top-level value types upfront and raise
`ValueError` with the offending key, OR document the
serializability contract in the docstring.

---

## 2. Control plane (FastAPI routes + ORM + executor)

This subsystem went from 13 raw findings to 11 after dedup. Two
agent reasoning errors were corrected (see Methodology). The
findings here are higher-stakes than the composer's because errors
here corrupt the durable state machine, not just one composition.

### 2.1 [CRITICAL] Session held across `await composer.compose(...)` ✅

**Where:** `control_plane/routes/workflow.py:67-118`, especially line 110
**Pattern:** `start_workflow` is `async def`, takes a SQLAlchemy
Session via `Depends(get_session)`, commits one row at line 108,
then `await`s the composer at line 110 while still holding the
session, then re-uses it at line 117.
**Why brittle:** SQLAlchemy's documented model is "do not use a
Session across an `await` that yields control." The pre-commit
limits the blast radius (no uncommitted transaction is held), but
the connection from the pool is held across the await; under load,
two requests can starve the pool. Confirmed by reading the code.
**Fix direction:** Release the session before `await composer.compose`
(use `session.close()` then re-acquire via a dependency-injected
factory), or restructure so the composer call happens outside the
route handler and the route just consumes the result.

### 2.2 [CRITICAL] Silent skip in `/workflows/diff` categorization parser ✅

**Where:** `control_plane/routes/workflow.py:302-306`
**Pattern:** `try: category = StepCategory(row["category"]); except
(KeyError, ValueError): continue` — malformed rows are silently
dropped.
**Why brittle:** The diff response returns fewer steps than the YAML
contains, the client has no signal that data was lost, and the only
way the bug is detected is a scientist asking "why are there 4 steps
in the diff but 6 in the YAML?" Categorizations were persisted at
compose time and SHOULD be valid, so a parse failure is real
corruption — not a benign tolerance case.
**Fix direction:** Replace `continue` with `raise HTTPException(422,
detail=f"step_categorizations row {idx} malformed: {row!r}")`. Or
raise 500 — this is a server-side data corruption, not a 4xx.

### 2.3 [HIGH] Race in `/hpc/ingest` between terminal-status check and write

**Where:** `control_plane/routes/hpc.py:414-447`
**Pattern:** Read `run.status`, branch on terminal vs non-terminal,
then unconditionally write `run.status = FAILED`. Two concurrent
ingest requests on the same run can both pass the terminal check.
**Fix direction:** Replace with a conditional UPDATE
(`UPDATE runs SET status=FAILED WHERE id=:id AND status NOT IN
(COMPLETED, FAILED, CANCELLED)`) and check the affected row count.
The UPDATE-with-WHERE is atomic in SQLite and Postgres.

### 2.4 [HIGH] `assert run is not None` after composer.compose

**Where:** `control_plane/routes/workflow.py:118`
**Pattern:** Production code uses `assert` to check the post-composer
state. Python's `-O` flag strips asserts.
**Fix direction:** Replace with `if run is None: raise
HTTPException(500, "run row vanished — composer did not commit?")`.

### 2.5 [MEDIUM] Provenance recorder uses `threading.Lock` in async paths

**Where:** `control_plane/provenance/recorder.py:97-135`
**Pattern:** `record()` is called from async route handlers; it uses
`threading.Lock`. The lock acquisition is synchronous and DOES block
the calling coroutine — but it cannot be released by another
coroutine on the same event loop, so a long-running record() under
contention can deadlock.
**Why brittle:** Single-worker uvicorn with concurrent requests is
the common case; two requests hitting `/approvals/correct` at the
same time can hang the event loop until one of them finishes the
file write inside the lock. (Note: the agent that flagged this
inverted the rationale; the correct framing is "lock blocks the
event loop, not coroutines pass through it.")
**Fix direction:** Use `anyio.Lock` (async-aware), or push the
provenance write to a background task / worker queue and return
from the route immediately.

### 2.6 [MEDIUM] `/approvals/correct` records provenance after commit

**Where:** `control_plane/routes/approval.py:140-172`
**Pattern:** DB commit happens at line N, then provenance event is
recorded at line N+M. If the recorder fails (disk full, lock
contention), the approval is committed but the provenance chain has
no event for the transition.
**Fix direction:** Record the provenance event before the commit
(inside the same transaction if the recorder uses the same session),
or wrap both in an outer try/except that rolls back the DB if the
recorder fails.

### 2.7 [MEDIUM] `/hpc/estimate_cost` hardcodes `endpoint="local"` while the response carries an `endpoint` field

**Where:** `control_plane/routes/hpc.py:77` and the
`EstimateCostResponse` schema
**Pattern:** Today there's only one endpoint, but the response shape
implies multi-endpoint. Callers will start depending on the field;
when T04/T05 land and endpoints are real, callers built against
"always local" will silently break.
**Fix direction:** Either remove the field from the response (single
endpoint world) or accept it in the request and validate it.

### 2.8 [MEDIUM] `executor.local._mark_failed` silently no-ops on missing run

**Where:** `control_plane/executors/local.py:263-289`
**Pattern:** `if run is not None: run.status = FAILED; ...`. If the
run row doesn't exist, the function returns success and a provenance
event is still emitted.
**Fix direction:** Raise inside the helper — by the time
`_mark_failed` is called, the run MUST exist. If it doesn't, that's
a bug worth surfacing.

### 2.9 [MEDIUM] `Artifact.location` stored as raw string with no validation

**Where:** `control_plane/models/entities.py:120`,
`control_plane/routes/hpc.py:487`
**Pattern:** Path stored as `Text` column. No `is_file()` check at
write time. Path can be relative, symlinked, or stale.
**Fix direction:** Validate `Path(location).is_file()` at write
time; on read, surface 404 with the path so the operator can audit.

### 2.10 [MEDIUM] `GeneratePlanRequest` lacks `user_id`; route hardcodes `_preview`

**Where:** `control_plane/routes/workflow.py:141-200`,
`control_plane/schemas/api.py:53-54`
**Pattern:** The route always sets `user_id="_preview"` regardless of
who's calling. Schema doesn't expose the field.
**Fix direction:** Either accept `user_id` in the request and trust
it, or document in the schema (`extra="forbid"` already prevents
silent drift on the input side; needs a docstring on the route).

### 2.11 [LOW] `/hpc/submit` 501 message lacks a reference link

**Where:** `control_plane/routes/hpc.py:255-256`
**Pattern:** `_not_implemented("T04 (Globus Compute) or T05 (PBS
bundle)")`. Fine for engineers; opaque for operators.
**Fix direction:** Include a URL to `implementation_plan.md` in the
detail message.

---

## 3. MCP surface + HPC bundle

10 raw findings; 7 retained after dedup. The MCP surface is largely a
thin pass-through layer over the Control Plane HTTP client, which
means the brittleness is mostly about input validation and operator
UX, not internal logic.

### 3.1 [CRITICAL] MCP tools accept any string as `run_id`; UUID parsing throws unhelpfully

**Where:** `mcp_surface/tools/hpc.py:27`,
`mcp_surface/tools/approvals.py:37`,
`mcp_surface/tools/workflows.py:51`
**Pattern:** `UUID(run_id)` with no try/except. A scientist
mistyping the UUID gets a stack trace.
**Fix direction:** Wrap in try/except; return a structured error
dict with the malformed input echoed back (so Claude can correct
itself).

### 3.2 [HIGH] No startup health check on Control Plane URL

**Where:** `mcp_surface/tools/_shared.py:30-38`
**Pattern:** `get_client()` lazily builds the `ControlPlaneClient`
on first tool invocation. A misconfigured `APECX_CONTROL_PLANE_URL`
is silent until a scientist actually calls a tool, then the operator
sees the failure under "the user ran into this in a session."
**Fix direction:** In `server.main()`, call `get_client().healthz()`
synchronously before binding the stdio transport; fail fast with
an exit code.

### 3.3 [HIGH] 503 from CP is unhandled; falls through to `raise_for_status`

**Where:** `mcp_surface/control_plane_client.py:102-106`
**Pattern:** Client catches 501 explicitly (mapping to
`NotImplementedError`), but 503 (service / dependency missing) goes
through `raise_for_status()` and surfaces as a raw httpx exception.
**Fix direction:** Catch 503 explicitly, return a structured
"composer not configured" / "approval-policy unavailable" message.

### 3.4 [HIGH] PBS bundle export doesn't pre-check writable target

**Where:** `execution/pbs_bundle.py:97-98`
**Pattern:** `bundle_dir.mkdir(parents=True, exist_ok=True)` followed
by file writes; if the disk is full or the path is unwritable, the
mkdir succeeds (or fails partially) and the write phase fails midway
leaving a partial bundle.
**Fix direction:** Wrap the entire write phase in try/except `OSError`
and clean up partial output on failure. Do NOT pre-check via
`os.access()` — it's TOCTOU-prone (one of the subagents suggested
that; it's a worse fix than try/except).

### 3.5 [HIGH] `pbs_bundle.py::_render_run_sh` is a stub that always reports success

**Where:** `execution/pbs_bundle.py:186-218`
**Pattern:** Generated `run.sh` writes `stub_completed` and exits 0.
Operator workflow: scientist `qsub`s the bundle, the job "succeeds",
the round-trip ingest reports COMPLETED, and the actual workflow
never ran.
**Why brittle but accepted:** This is documented as T05 follow-up
and is correct as a Phase-2 scaffold. Flagging it because the
stub-success failure mode is invisible — the scientist sees a
green run that did nothing.
**Fix direction:** Add a banner to `submit.pbs` and `run.sh` that
says "STUB BUNDLE — DOES NOT EXECUTE THE WORKFLOW" until T05 lands.
Have the ingest path detect the `stub_completed` marker and
warn-or-fail rather than treating it as a normal terminal state.

### 3.6 [MEDIUM] `bundle_path` in submit_command is absolute

**Where:** `execution/pbs_bundle.py:149`,
`control_plane/routes/hpc.py:346-348`
**Pattern:** `submit_command` is `cd /tmp/<absolute>/... && qsub
submit.pbs`. Worthless after Globus transfer — scientist must edit
the path.
**Fix direction:** Use a relative path or a `<TARGET_DIR>` placeholder
plus a usage note in the bundle README.

### 3.7 [MEDIUM] PBS queue names are hardcoded

**Where:** `execution/pbs_bundle.py:165-166`
**Pattern:** `queue: prod` (Polaris) / `queue: EarlyAppAccess` (Aurora)
baked into the renderer. No validation against a registry of valid
queues.
**Fix direction:** Extract into `configs/hpc/queues.yml` and validate
the requested queue at export time.

### 3.8 [MEDIUM] No test for malformed UUID input on MCP tools

**Where:** `tests/integration/test_mcp_server.py`
**Pattern:** Every test passes a valid `str(uuid4())`; no negative test.
**Fix direction:** Add a parameterized negative-input test that
asserts each tool returns a structured error (not a raised exception)
when given malformed UUIDs.

### 3.9 [MEDIUM] `scripts/build_rag_index.py` doesn't verify the index files actually wrote

**Where:** `scripts/build_rag_index.py:93-103`
**Pattern:** `idx.save(target)` followed by a "wrote N entries" print.
If save fails partway, the print may not fire, but the operator
can't tell whether files were left behind.
**Fix direction:** After save, assert `(target / "faiss.bin").is_file()
and (target / "metadata.json").is_file()` and report sizes; on
failure, clean up partials.

### 3.10 [LOW] `start_workflow` enum validation deferred to Control Plane

**Where:** `mcp_surface/tools/workflows.py:26-37`
**Pattern:** `ExecutorKind(preferred_executor)` raises only when the
HTTP request is built; an invalid string travels further than it
should.
**Fix direction:** Validate at the tool boundary with a clear error
message listing valid values.

---

## 4. Test infrastructure

The test suite has solid bones — separate `unit/`, `integration/`,
`reproducibility/` directories; per-area conftest fixtures; the
academy_real_integration tests are the gold-standard pattern (real
backend, fixture-managed singleton teardown). The findings here are
about brittle test patterns that don't catch behavioral drift.

### 4.1 [HIGH] Tests assert on log output without verifying behavior

**Where:** `tests/integration/test_nanobrain_mocks_policy.py:171-196`
**Pattern:** `test_use_mock_clients_true_emits_warning` asserts that
a warning containing "dev-mode" and "mock" appears in caplog. No
assertion that the actual mock-client switching happened.
**Why brittle:** A refactor that removes the mock-client logic but
leaves the warning in place will pass the test. The test is verifying
that we log, not that we behave correctly.
**Fix direction:** Pair every log-assertion test with a
behavioral assertion (call the system under test, observe the
visible side effect).

### 4.2 [HIGH] `test_academy_demo_mode_env_var_gate_documented` checks source-code strings

**Where:** `tests/integration/test_nanobrain_mocks_policy.py:127-148`
**Pattern:** Test reads `academy_integration.py` as text and greps
for `ACADEMY_DEMO_MODE`. Tests the documentation, not the runtime
gate.
**Fix direction:** Add a behavioral test that sets the env var,
imports the module, calls a dispatched action, and asserts the
mock path was taken (e.g., checks for the warning log line that
Academy emits in demo mode).

### 4.3 [MEDIUM] `_PlaceholderLLM` duplicated across two test files

**Where:** `tests/reproducibility/test_baselines.py:60-77`,
`tests/integration/test_composer_phase2.py:55-84`
**Pattern:** ~20 lines of identical placeholder-LLM machinery in
two places.
**Fix direction:** Extract into `tests/conftest.py` or a shared
`tests/_helpers/` module.

### 4.4 [MEDIUM] `cp_engine` fixture redefined in `test_provenance_chain.py`

**Where:** `tests/integration/test_provenance_chain.py:31-53` vs
`tests/integration/conftest.py:22-30`
**Pattern:** Same fixture body in two files. Schema migration changes
will need both updated.
**Fix direction:** Delete the local copy; if the test needs a pre-
seeded Run row, add a separate fixture that depends on `cp_engine`.

### 4.5 [MEDIUM] Composer fixture freshness invariant is asserted by docstring, not by fixture teardown

**Where:** `tests/reproducibility/test_baselines.py:91-98`
**Pattern:** The `_generate_bytes` helper builds a fresh Composer
per fixture and the docstring explains why a singleton would
"bleed canned responses across fixtures." A future refactor that
caches the composer would pass code review and silently re-use
canned responses across tests.
**Fix direction:** Add a `pytest.fixture(scope="function")` that
asserts each test gets a new Composer instance (e.g., capture the
id and assert it's distinct from the previous test's id).

### 4.6 [MEDIUM] Hardcoded `datetime.now(UTC)` timestamps in fixtures

**Where:** `tests/integration/test_api_hpc_estimate.py:28-42`
**Pattern:** Fixture stores the current wall-clock time. Tests that
assert on timestamp ordering can flake if CI clock drifts or the
test takes long.
**Fix direction:** Use a frozen time (`pytest-freezegun` or a
manually-injected `now` callable).

### 4.7 [MEDIUM] `test_apptainer_runtime.py` skips on port collision instead of finding a free port

**Where:** `tests/integration/test_apptainer_runtime.py:251`
**Pattern:** Hardcodes 5433; skips when busy. Parallel test runs
can self-collide.
**Fix direction:** Bind to port 0 to let the OS pick a free port,
then read the bound port back; pass that to the subprocess.

### 4.8 [MEDIUM] Per-test skipif on `_docker_daemon_is_up()` is module-scoped only

**Where:** `tests/integration/test_infra_lifecycle.py:63-77`
**Pattern:** Module-level `pytestmark` works for tests in this file,
but the pattern is easy to forget when copying tests to a new file.
**Fix direction:** Move the skip predicate into a shared decorator
in `tests/conftest.py` so individual tests can use
`@requires_docker` rather than re-declaring the pytestmark.

### 4.9 [LOW] xfail on Apptainer Postgres test references stale future-work

**Where:** `tests/integration/test_apptainer_runtime.py`
(strict-xfail block)
**Pattern:** Marker may have outlived its reason if the referenced
future-work shipped.
**Fix direction:** Re-evaluate; remove if obsolete, otherwise add a
ticket reference and expected-unblock condition.

---

## 5. Configs, scripts, docs

### 5.1 [HIGH] Doc-code drift on Academy status (G5)

**Where:** `docs/current_gaps_2026_04_23.md` (G5 row) vs `CLAUDE.md`
"Academy integration (real, as of G5 — 2026-04-24)"
**Pattern:** Gaps doc says G5 is "domain-expert" and unimplemented;
CLAUDE.md and the integration test prove it's done.
**Fix direction:** Either delete `current_gaps_2026_04_23.md` (it's
explicitly snapshot-style and the snapshot has rolled forward) or
add a "STATUS CHANGES" appendix listing rows that have been
overturned with the date and commit.

### 5.2 [HIGH] `max_retries: 0` default lacks production safety docs

**Where:** `composer_config.yml:19`,
`composer_schemas.py:58`
**Pattern:** Default 0 is right for development (fast failures during
prompt iteration) but unsafe for production (one transient LLM 5xx
fails the whole run). Neither file warns operators.
**Fix direction:** Add a comment in the YAML and a docstring on the
schema field calling out the dev-vs-prod tradeoff. Consider raising
the default to 1 or 2 if production is the more common deployment.

### 5.3 [MEDIUM] `APECX_LLM_API_KEY` env-var contract is delegated but undocumented

**Where:** `CLAUDE.md` "Live-LLM test recipe" sets it;
`composer.py::_apply_llm_env_overrides` doesn't read it.
**Pattern:** The composer's env-var override list is 4 vars
(`MODEL`, `BASE_URL`, `TEMPERATURE`, `MAX_TOKENS`) — `API_KEY` is
handled downstream by the LLM factory in `apecx_db_integration`.
This split is correct but undocumented.
**Fix direction:** Add a section to `composer_config.yml` header
listing which env vars the composer owns vs which the LLM factory
owns.

### 5.4 [MEDIUM] cspell.json shipped without a pre-commit hook

**Where:** `cspell.json` (added 2026-04-24); no entry in
`.pre-commit-config.yaml` (if one exists; if not, nowhere)
**Pattern:** The wordlist exists, the enforcement doesn't.
**Fix direction:** Either add a pre-commit hook entry or accept that
cspell is an editor-only dev tool and remove the file.

### 5.5 [MEDIUM] `composer_config.yml` uses fragile `../../../` relative paths

**Where:** `src/apecx_integration/composition/composer_config.yml:34`
points at `../../../configs/sandbox/import_whitelist.txt`
**Pattern:** Triple-`..` is brittle to filesystem reorganization.
The header docstring promises "all paths resolved relative to this
config file's parent" which is correct, but `../../../` is the
worst case of that contract.
**Fix direction:** Allow an env var
`APECX_SANDBOX_WHITELIST_PATH` to override; document the relative-
path fallback as a development convenience.

### 5.6 [MEDIUM] `scripts/run_tests.sh` error message is overly prescriptive

**Where:** `scripts/run_tests.sh:33-36`
**Pattern:** When `.venv/bin/python` is missing, the script says
"Create the venv with: ...". Doesn't distinguish "no venv" from
"venv exists but editable install missing."
**Fix direction:** Add a second check (`apecx_integration` is
importable from the venv) and emit a different error message for
each failure mode.

### 5.7 [MEDIUM] No coverage gate in `pyproject.toml` pytest config

**Where:** `pyproject.toml:91-99`
**Pattern:** `[tool.pytest.ini_options]` defines markers and async
mode but no `--cov-fail-under`. Test removal or guard-deletion goes
unnoticed.
**Fix direction:** Add `[tool.coverage.run]` + `[tool.coverage.report]`
sections with `fail_under` set to the current measured coverage
(don't go higher than reality; aspirational thresholds make CI
flaky).

---

## 6. Nanobrain core (advisory — upstream code)

These findings are observations for the nanobrain maintainers, not
direct apecx action items. The framework is generally solid and
in-prod-use; brittleness is concentrated in the diagnostic-message
quality and a few singleton-lifecycle gaps.

### 6.1 [HIGH] `_ensure_manager` poisoned-singleton on launch failure

**Where:** `nanobrain/core/academy_integration.py:295-309`
**Pattern:** `_ensure_manager()` enters the Manager `async with`
context, then calls `await self._manager.launch(agent_class)`. If
launch raises, `self._manager` is set but the launch failed; later
calls see "manager is not None" and skip re-entry, but the manager
state is suspect.
**Fix direction:** Wrap launch in try/except; on failure, exit the
context cleanly and set `self._manager = None` and
`self._manager_cm = None` so the next call retries.

### 6.2 [HIGH] Silent exception swallowing in `from_config` config-attr extraction

**Where:** `nanobrain/core/component_base.py:617-622`
**Pattern:** `dir(config)` walked with bare `getattr(config, key)`
to convert Pydantic models to dicts. A property that raises is
silently skipped, producing an incomplete dict that later fails
with a vague "missing field."
**Fix direction:** Catch the exception, log at DEBUG with the field
name and original error, propagate as a clear "config field
{name} raised {exc}" error.

### 6.3 [HIGH] `validate_config_usage` warns instead of failing

**Where:** `nanobrain/core/component_base.py:28-83`
**Pattern:** Function name suggests strict validation; behavior is
advisory-only. Several "violation" branches log and return.
**Fix direction:** Decide which branches are real bugs (raise) vs
deprecation warnings (log). Rename the function to match its actual
contract.

### 6.4 [MEDIUM] `_allow_direct_instantiation` thread-unsafe class attribute toggle

**Where:** `nanobrain/core/data_unit.py:1325-1328`,
`link.py:1071-1074`, `trigger.py:866-869`, plus 25+ other locations
**Pattern:** Pattern: `Cls._allow_direct_instantiation = True;
instance = Cls(...); Cls._allow_direct_instantiation = False`. Two
threads racing on this hit a window where both can pass the gate
but the flag-restore is interleaved.
**Fix direction:** Replace with a context manager that uses a
thread-local stack: `with allow_direct_instantiation(Cls): instance
= Cls(...)`. The class attribute becomes a
`threading.local`-backed property.

### 6.5 [MEDIUM] `execute()`-override validation message is friendly but the failure point is at init time, not at definition time

**Where:** `nanobrain/core/step.py:660-667` (validation only)
**Pattern:** A subclass that overrides `execute()` doesn't fail
until `Step.from_config(...)` runs. Until then, the override sits
silently in the class body and IDE warnings don't fire.
(Note: a subagent claimed the framework "doesn't prevent the
override"; that's wrong — init-time validation is real prevention.
The actual issue is timing, not absence of enforcement.)
**Fix direction:** Move the check into `__init_subclass__` so it
fires at class-definition time, not at component-init time.

### 6.6 [MEDIUM] `import_class_from_path` collapses N strategy failures into "all failed"

**Where:** `nanobrain/core/component_base.py:118-129`
**Pattern:** Loop catches `(ImportError, AttributeError)` per-attempt
silently; raises a generic message at the end.
**Fix direction:** Accumulate per-attempt error tuples and include
them in the final error with the search-namespace and the original
exception so an operator can see which import path was closest.

### 6.7 [MEDIUM] `_resolve_config_file_path` swallows `(OSError, TypeError)` from `inspect.getfile`

**Where:** `nanobrain/core/component_base.py:743-777`
**Pattern:** Same family as 6.6. ZIP imports / frozen binaries lose
their diagnostic.
**Fix direction:** Log at DEBUG with the original exception before
moving to the next strategy.

### 6.8 [MEDIUM] Academy manager singleton has no atexit guard

**Where:** `nanobrain/core/academy_integration.py:56-57, 509-518`
**Pattern:** Process-level singleton with manual teardown
(`shutdown_academy_manager()`). Tests use a fixture (good); ad-hoc
scripts don't.
**Fix direction:** Add `atexit.register(shutdown_academy_manager_sync)`
where the sync wrapper handles the "no event loop" case at
interpreter shutdown. (Don't naively `atexit.register(asyncio.run(...))` —
asyncio.run inside atexit is fragile because the loop may be gone
already; build a sync teardown that closes file handles directly.)

### 6.9 [MEDIUM] A2A initialization swallows broad Exception

**Where:** `nanobrain/core/a2a_support.py:1105-1116, 1168-1170`
**Pattern:** `try: init...; except Exception: log.warning(...)`. An
agent whose A2A init fails silently continues with no A2A.
**Fix direction:** Distinguish recoverable errors (network, retryable)
from fatal ones (missing imports, bad config). Raise the fatal
ones; log+continue on the recoverable ones.

### 6.10 [LOW] AsyncTriggerExecutor cycle detection covers only immediate self-recursion

**Where:** `nanobrain/core/trigger.py:51-82`
**Pattern:** `execution_stack` checks if a trigger is already running;
catches A→A but not A→B→A.
**Fix direction:** Track the full path, not just current; or a depth
counter to bail out at N levels. (LOW because deeper-cycle
deadlocks haven't been reported and the architecture discourages
them.)

### 6.11 [LOW] Link transform/condition functions resolved without signature validation

**Where:** `nanobrain/core/link.py:50-103, 106-141`
**Pattern:** Dotted-path callable resolution with no
`inspect.signature` check. Wrong signature fails at link-transfer
time with a TypeError.
**Fix direction:** At link init, validate the resolved callable
accepts at least one positional arg.

---

## 7. Nanobrain library + tools (advisory — upstream code)

### 7.1 [CRITICAL] Hardcoded demo path that won't exist on fresh checkout

**Where:** `nanobrain/library/workflows/viral_protein_analysis/steps/bv_brc_data_acquisition_step.py:216`
**Pattern:** Default config points at
`demos/viral_pssm_workflow/config/bvbrc_tool_config.yml`.
**Fix direction:** Either ship the demo config in a documented
location with a relative-resolution rule, or make the field required
and surface the missing-config error at step init.

### 7.2 [HIGH] `extract_component_config` boilerplate duplicated across viral_protein_analysis steps

**Where:** `library/workflows/viral_protein_analysis/steps/`:
`data_aggregation_step.py:45`, `clustering_step.py:112`,
`result_collection_step.py:46`, `viral_pssm_generation_step.py:45`,
`annotation_mapping_step.py` (similar)
**Pattern:** Same `super().extract_component_config(config)` +
field-extraction shape repeated in N files.
**Fix direction:** Extract to a mixin or use Pydantic's auto-mapping
where possible.

### 7.3 [HIGH] `print(...)`-and-return-dict-with-error pattern

**Where:** `library/workflows/viral_protein_analysis/steps/enhanced_bv_brc_data_acquisition_step.py:366-368, 843-845`
**Pattern:** `except Exception as e: print(f"Error: {e}");
acquisition_results[family] = {'error': str(e)}`. Caller sees a
result dict, must remember to inspect each entry's 'error' field.
**Fix direction:** Either raise (let the executor handle retry/skip)
or log via `self.nb_logger.error(...)` and let the result schema
distinguish success from partial failure.

### 7.4 [MEDIUM] Hardcoded relative cache paths across viral_protein_analysis steps

**Where:** Multiple steps use `Path("data/...")` directly
**Pattern:** `Path("data/clustering_cache")` etc. — no per-run
isolation, no env override, no temp-dir fallback.
**Fix direction:** Resolve via the workflow's run directory (passed
via context) or use `tempfile.mkdtemp(prefix=...)` per run.

### 7.5 [MEDIUM] `ResultCollectionStep` and `DataAggregationStep` both define `__init__` AND `_init_from_config`

**Where:** `result_collection_step.py:71-82`,
`data_aggregation_step.py:70-81`
**Pattern:** `from_config` is the contract; `__init__` should be
left to the FromConfigBase hierarchy. (Note: subagent claimed this
"creates two execution paths." That's not quite right —
`FromConfigBase.__init__` raises on direct instantiation, so the
custom `__init__` cannot actually be called by users. Still smell-y
because the code suggests a contract that the framework forbids.)
**Fix direction:** Remove the custom `__init__` methods.

### 7.6 [MEDIUM] BVBRCDataAcquisitionStep returns `[]` on parse failures with no log line

**Where:** `bv_brc_data_acquisition_step.py:944, 951, 1134, 1173,
1203, 1220, 1223, 1229, 1234, 1447, 1515, 1552, 1558, 1563`
**Pattern:** Multiple early-return-`[]` paths inside parsers; caller
can't tell "no data" from "parse failed."
**Fix direction:** Log a warning with the offending input snippet on
parse-failure paths; reserve `return []` for the legitimate-empty
case.

### 7.7 [MEDIUM] AnnotationMappingStep cache miss returns `{}` (same as empty cache hit)

**Where:** `library/workflows/viral_protein_analysis/steps/annotation_mapping_step.py:178`
**Pattern:** `cache.get(key)` collapses miss and hit-with-empty into
the same return.
**Fix direction:** Use a sentinel (`_MISSING = object()`) or raise on
miss.

### 7.8 [LOW] `component_index.py` import-order constraint is comment-enforced only

**Where:** `nanobrain/lightweight/component_index.py` (ruff noqa +
explanatory comment at top)
**Pattern:** The comment is correct and the noqa keeps formatters
out — but a future refactor could accidentally remove both.
**Fix direction:** Add a runtime assertion at module-load:
`assert SentenceTransformer is not None and faiss is not None,
"FAISS/sentence-transformers import order has been broken — see
top-of-file comment"`. The assertion runs before any code that
relies on the order, so a regression fails loud at import.

---

## 8. Cross-repo coupling

### 8.1 [CRITICAL] Python version mismatch ✅

**Where:** `apecx-mcp-integration/pyproject.toml:9`
(`requires-python = ">=3.11"`) vs
`nanobrain/pyproject.toml:10` (`requires-python = ">=3.12"`)
**Pattern:** apecx claims to support Python 3.11. Its mandatory
sibling nanobrain does not.
**Why brittle:** A 3.11 install is straightforwardly broken — the
`pip install -e ../nanobrain` step refuses with "requires Python
>=3.12." The contract on apecx's side is wrong.
**Fix direction:** Bump apecx to `requires-python = ">=3.12"` in
the next commit. Verified during synthesis (see §10).

### 8.2 [HIGH] Manifest class paths not validated at load time

**Where:** `src/apecx_integration/composition/workflows/violin_bvbrc/manifest.yml`
(class paths at lines 29, 52, 98, 123, 152, 173, 192)
**Pattern:** Class paths are strings inside the catalog YAML. A
typo or rename in nanobrain doesn't fail at apecx test time unless a
test exercises the specific component.
**Fix direction:** Add a `ComponentCatalog.validate()` method that
imports every class path; call it from a unit test that runs on
every commit. Or wire it into `pre-commit` so YAML changes are
verified before push.

### 8.3 [HIGH] sentence-transformers version pin diverges across repos ✅

**Where:** `apecx-mcp-integration/pyproject.toml:52`
(`sentence-transformers>=2.7`) vs
`nanobrain/pyproject.toml:47`
(`sentence-transformers>=2.2.2,<3.0.0`)
**Pattern:** apecx has no upper bound; nanobrain caps at <3.0. A
future 3.x release satisfies apecx but not nanobrain — pip resolves
to the intersection (<3.0), but the pyprojects disagree on intent.
**Fix direction:** Align both to `>=2.7,<3.0.0` (same lower bound as
apecx, same upper bound as nanobrain). Verified during synthesis.

### 8.4 [MEDIUM] FAISS/sentence-transformers import order enforced in two places, brittle in both

**Where:** `apecx-mcp-integration/scripts/build_rag_index.py:29-34`,
`nanobrain/nanobrain/lightweight/component_index.py:1-4` (and the
load-bearing comment at the top)
**Pattern:** Comment-and-noqa enforcement on both sides; segfault on
macOS ARM if reversed. See §7.8 for the fix.

### 8.5 [MEDIUM] Stale Academy comment in apecx test file

**Where:** `apecx-mcp-integration/tests/integration/test_academy_real_integration.py:21-25`
**Pattern:** Pre-G5 historical narrative ("Does NOT enter the Manager
context") survived into the post-G5 file. New maintainer reading
the comment will think apecx is still in the broken state.
**Fix direction:** Replace with a one-paragraph "as of G5
(2026-04-24)" status note that points at CLAUDE.md for the API
contract.

### 8.6 [LOW] No CI check guarding the apecx → nanobrain dependency direction

**Where:** No file
**Pattern:** Reverse imports (`from apecx_integration import ...` in
nanobrain) would silently survive code review.
**Fix direction:** Add a CI check (`grep -r "from apecx_integration"
nanobrain/`) or a pre-commit hook on the nanobrain side.

---

## 9. Cross-cutting patterns

These are the recurring shapes the per-area findings collapse into.
Fixing the pattern once fixes ~5 findings.

**P1 — Silent exception swallowing.** Findings 1.1, 1.8, 2.2, 6.2,
6.7, 6.9, 7.3, 7.6, 7.7. Fix-once direction: a single
`@logging_swallow(reraise: bool = True, log_level: int = WARNING)`
decorator applied at every site, with `reraise=False` only where
the swallow is intentional (e.g., kill-on-timeout fallback).
Centralizes both the policy and the audit trail.

**P2 — Diagnostic poverty in import + config resolution.** Findings
6.6, 6.7, 8.2. Fix-once: every "tried N strategies, all failed"
error message should include the per-strategy exception. The
import_class_from_path family is the single richest target for
this — it's called for every component every workflow load.

**P3 — Singleton lifecycle relies on convention.** Findings 6.1,
6.8, 4.5. Fix-once: every process-level singleton in nanobrain
should expose an async context manager and an atexit-friendly
sync teardown. Test fixtures consume the context manager;
ad-hoc scripts can rely on the atexit hook.

**P4 — Async / threading mixing.** Findings 2.1, 2.5, 2.3.
Fix-once: an audit pass for `await` inside a `Depends(get_session)`
scope, and replace `threading.Lock` with `anyio.Lock` where the
caller is async. None of these will surface under unit tests; all
of them surface under load.

---

## 10. Verification status

Findings personally verified by reading code during synthesis (✅ in
the sections above):

- §1.1 Silent exception in `_suggest_for_violation` — confirmed,
  code has `except Exception: return ()` with no log.
- §2.1 Session held across `await composer.compose()` — confirmed
  by reading `routes/workflow.py:108-117`. Pre-await commit limits
  the blast radius (no transaction held), but the connection is.
- §2.2 Silent skip in `/workflows/diff` categorization — confirmed,
  code is `try: ... except: continue` at lines 302-306.
- §8.1 Python version mismatch — confirmed via `grep
  requires-python` in both pyproject.toml files.
- §8.3 sentence-transformers pin divergence — confirmed via grep.

The remaining findings carry the subagent's evidence as
`file:line` citations. Spot-check before acting on them.

## 11. Recommended next-actions

Ranked by ratio of risk-reduction to effort. The first three are
roughly 30 minutes of work and cover the highest-impact gaps.

1. **§8.1 Python version pin.** One-character fix in apecx's
   `pyproject.toml`. Verify nothing in apecx code uses 3.12-only
   syntax (it shouldn't — nanobrain already requires 3.12 and apecx
   imports nanobrain unconditionally).
2. **§2.2 Silent categorization skip.** Convert to a 422 / 500.
   Trivially testable with a fixture that injects malformed
   step_categorizations.
3. **§3.1 + §3.8 MCP tool UUID validation.** Wrap UUID parsing in
   try/except across the 4 tool sites; add a parameterized
   negative-input test.
4. **§5.1 Stale gaps doc.** Either delete or annotate. Cheap, but
   actively misleading anyone who reads it post-G5.
5. **§1.4 Whitelist caching.** Move the `load_whitelist` call from
   `compose()` into `__init__`. Reduces I/O and removes a transient
   failure mode.

Pattern fixes (P1–P4) are the right next move after the surface-
level fixes — they catch the long tail without playing whack-a-mole.

## 12. What is solid (don't change)

Aggregated "solid sections" from the eight subagent reports,
deduplicated. The pattern these all share: clear contracts, explicit
failure modes, no silent fallback paths.

- `apecx-mcp-integration/control_plane/db.py:1-83` — SQLite
  pragmas + connection pooling are honest about their concurrency
  limits; comments document the gotchas.
- `apecx-mcp-integration/control_plane/provenance/recorder.py:97-179` —
  hash-chain validation is thorough; the threading.Lock issue is
  about WHO calls record, not how it's implemented internally.
- `apecx-mcp-integration/control_plane/routes/approval.py:78-95` —
  state-transition guards are clean and consistent.
- `apecx-mcp-integration/control_plane/schemas/api.py` — Pydantic
  envelopes use `extra="forbid"`; silent field drops are blocked at
  the schema layer.
- `apecx-mcp-integration/composition/sandbox.py:155-227` — AST
  visitor for import scanning. Each violation is explicit. Solid
  per-Phase-1 contract.
- `apecx-mcp-integration/composition/differ.py:227-250` — recursive
  config walk handles all container shapes, no missed edges.
- `apecx-mcp-integration/composition/docker_sandbox.py:99-165` —
  pure argv construction, every flag justified, every flag pinned
  by tests.
- `apecx-mcp-integration/tests/integration/test_academy_real_integration.py`
  — fixture-based singleton teardown is the canonical pattern;
  CLAUDE.md documents the contract.
- `apecx-mcp-integration/tests/integration/test_a2a_happy_path.py`
  — real aiohttp JSON-RPC server in-process; no mocks. Paired with
  error-branch coverage in `test_nanobrain_mocks_policy.py`.
- `apecx-mcp-integration/tests/integration/conftest.py:22-35` —
  `cp_engine` and `cp_client` fixtures: fresh migrations per test,
  zero state leakage.
- `apecx-mcp-integration/scripts/run_tests.sh` — venv detection +
  PYTHONPATH setup is correct. The error-message critique (§5.6)
  is for the failure-mode side; the happy-path is solid.
- `apecx-mcp-integration/composition/composer_prompts/system.md` —
  hard constraints (DirectLink-only, path-reference `config:`)
  came from real bugs and the prompt is the load-bearing artifact
  for T01 AC1.
- `nanobrain/core/config/config_base.py` — `_resolve_nested_objects`
  handles class+config recursion with explicit cycle detection.
- `nanobrain/core/trigger.py:87-96` — AsyncTriggerExecutor's
  background-task management with completion callback is textbook.
- `nanobrain/library/steps/approval_step.py:125-462` — error
  handling is exemplary; HTTP client is dependency-injected;
  every decision outcome is enumerated and raises or returns
  with logging.
- `nanobrain/lightweight/component_index.py:148-243` — the index
  hash is computed from source text, not FAISS bytes — sidesteps
  BLAS nondeterminism and gets reproducible hashes.

## 13. Process note (audit on the audit)

The eight-subagent approach worked: total wallclock ~5 minutes,
total finding count 86 raw → 51 after dedup. Three subagents made
small reasoning errors that I caught only because each finding had
a `file:line` citation I could re-read. Without that requirement
I'd have shipped the threading.Lock-doesn't-block claim, the
"execute() override has no enforcement" claim, and the TOCTOU-
prone writability check, all of which would have wasted reviewer
time and damaged the doc's credibility.

**Lesson worth distilling:** when delegating analysis to subagents,
require `file:line` citations and verify the highest-severity
findings personally before promoting them. Subagents are good at
breadth, not at reasoning about subtle semantics.
