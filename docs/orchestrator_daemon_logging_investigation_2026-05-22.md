# Orchestrator daemon-thread + closed-stream logging — investigation & fixes (2026-05-22)

## Symptom

A clean-install unit run on macOS emitted **138 `--- Logging error ---`**
stderr dumps during teardown, each:

```
--- Logging error ---
Traceback (most recent call last):
  File ".../logging/__init__.py", line 1163, in emit
    stream.write(msg + self.terminator)
ValueError: I/O operation on closed file.
Call stack:
  File ".../threading.py", line 1012, in run
    self._target(...)
  File ".../apecx_integration/infrastructure/orchestrator.py", line 1291, in _runner
    loop.run_until_complete(orch.prewarm_workflow_tools())
  ...
  File ".../nanobrain/core/academy_integration.py", line 475, in requires_academy_integration
    logger.info("ℹ️ No Academy links found in workflow configuration")
```

Zero test failures, exit 0. CI (Ubuntu) did not show them — the count is
environment-dependent (worse when local docker/postgres/redis are up).

## Root cause

`build_server()` (the MCP-server build path) calls
`start_orchestrator_in_background_thread()`, which spawns the
`apecx-infra-orchestrator` **daemon thread** running `start_all()` +
`prewarm_workflow_tools()`. In a unit test:

1. The test calls `build_server()` → the daemon thread starts.
2. The test returns. Nothing stops the thread (it's fire-and-forget; the
   join handle was never surfaced through `build_server`).
3. pytest tears down its per-test capture and **closes the capture stream**.
4. The daemon thread is *still* running `prewarm_workflow_tools()` — loading
   each catalog workflow via `Workflow.from_config`, which logs
   "No Academy links found in workflow configuration".
5. nanobrain's logging `StreamHandler` writes to the now-closed stream →
   `ValueError: I/O operation on closed file` → Python's logging machinery
   prints `--- Logging error ---` per record.

So two independent defects compound: a **thread that outlives its owner**,
and a **logging handler that raises when its stream is closed**.

## Brutal-truth severity

This is mostly cosmetic *in tests* (0 failures). But the underlying defects
are real in production:

- **MCP server runs over stdio.** When Claude Desktop disconnects/kills the
  server, stdout/stderr close. If the orchestrator daemon is still mid
  `start_all`/prewarm, every log line it emits hits a closed pipe → the same
  traceback spew in `~/Library/Logs/Claude/mcp-server-apecx.log`. Noise that
  buries real diagnostics.
- **Abrupt daemon kill.** `daemon=True` threads are killed at interpreter
  exit *without* running `finally` blocks. If the kill lands mid
  conda-env-build or mid Redis-write inside pre-warm, that work is severed
  uncleanly.

I also found two adjacent issues while tracing the path (below). None are
high-severity, but "reliability across diverse settings" is the bar.

## Fixes

### A — nanobrain: `ResilientStreamHandler` (framework capacity)

`nanobrain/core/logging_system.py` gains `ResilientStreamHandler`
(`logging.StreamHandler` subclass) used for all four console-handler sites.
It drops records when its stream is `None`/closed instead of raising, and
suppresses the closed-stream race in `handleError` while still delegating
*other* handler errors to the default machinery (a formatter bug still
surfaces). Rationale: a logging write failing because the consumer went away
must never raise or spew a traceback, and must never mask the component's
real work — observability never breaks correctness. This is the root-cause
fix for "log to a closed stream" anywhere (MCP stdio close, a CLI that closed
stdout, pytest capture). Regression: `tests/unit/test_resilient_stream_handler.py`
(5 tests). nanobrain commit: see below.

### B — apecx: cancellable background drive + clean stop

`start_orchestrator_in_background_thread` now runs the drive (`start_all` +
optional pre-warm) as a single **cancellable asyncio task**, stores the
thread/loop/task handles module-side, and registers a **process-exit
`atexit` hook**. New `stop_orchestrator_in_background_thread(timeout)`
cancels the task (so in-flight `await` points unwind through their `finally`
blocks — no abrupt kill) and joins the thread. Idempotent and a no-op when
nothing is running.

### C — apecx tests: autouse teardown

`tests/conftest.py` gains an autouse fixture that calls
`stop_orchestrator_in_background_thread()` after every test, so a
`build_server()`-spawned drive never outlives the test that started it. No-op
(one `is_alive()` check) for the vast majority of tests that never spawn it.

### E — apecx: probe-only mode skips pre-warm

Pre-warm builds conda envs on disk — a system-touching action. In probe-only
mode (`APECX_MCP_AUTOSTART_INFRA=0`, whose contract is "no docker run /
Popen, hands off") it was **still running**. The drive now gates pre-warm on
`orch._autostart`: probe-only logs a skip line and does not build envs.
Tests: `test_background_drive_skips_prewarm_in_probe_only_mode` +
`test_background_drive_runs_prewarm_in_full_mode`.

### D — apecx: pre-warm uses canonical `Workflow.run`

`prewarm_workflow_tools` drove the cascade with the deprecated
`process()` + `wait_for_cascade(settle_ms=300)` pair — the exact shape
G124/G125 replaced (it also tripped the <500ms settle warning). Migrated to
`Workflow.run(..., settle_ms=500)`, which deposits input, drains the cascade,
and collects workflow-level outputs in one call. `run` does NOT call
`initialize()`, so the explicit `initialize()` stays. The stale docstring
that claimed `process()`+`wait_for_cascade()` was "one pattern, one mental
model" was corrected. Functional impact is low post-G125 (the manual read of
the output DU already worked), but this removes a documented silent-failure-
prone pattern from a live code path.

## Why not "just silence the logger"

Setting `logging.raiseExceptions = False` globally would hide the symptom but
also every genuine handler error, and wouldn't address the leaked thread or
the abrupt-kill risk. Fixes B/C/E remove the *cause* (the thread running
after teardown / in the wrong mode); Fix A is the framework-native
defense-in-depth for the cases where a stream legitimately closes under a
still-active component (production stdio shutdown).

## Verification

- nanobrain unit suite: **1206 passed, 9 skipped, 0 failed** (+5 = the new
  `test_resilient_stream_handler.py`).
- apecx unit suite: **1214 passed, 4 skipped, 0 failed** (+3 = the new
  background-drive lifecycle/gate tests).
- closed-stream errors in the apecx run: **0** (`I/O operation on closed
  file` and `--- Logging error ---` both 0; was 138).

**Honest caveat on the 138→0 full-suite count.** The original 138-error run
had local docker backends (postgres/redis) *up*, so the daemon reached
`prewarm → Workflow.from_config` and logged after teardown. The post-fix run
had docker *down*, so `start_all` found no backends and pre-warm returned
early — the heavy logging path didn't execute. So the full-suite count alone
is not a clean A/B. The fix is instead verified deterministically at the unit
level, under both backend conditions:

- **Fix A** — `tests/unit/test_resilient_stream_handler.py::test_full_logger_path_with_closed_stream_is_silent`
  attaches the handler to a real stream, **closes it**, logs, and asserts no
  `--- Logging error ---` reaches stderr. This is the exact closed-stream
  condition, proven independent of the orchestrator.
- **Fix B/E** — `test_background_drive_runs_prewarm_in_full_mode` /
  `…_skips_prewarm_in_probe_only_mode` / `test_stop_background_thread_is_noop_when_none_running`
  prove the drive is cancellable, the thread joins, and pre-warm is gated.
- **Smoke** — `build_server()` then `stop_orchestrator_in_background_thread()`
  logged `InfraOrchestrator: background drive cancelled (shutdown).` and the
  thread joined — confirming the in-flight drive unwinds cleanly rather than
  leaking.
- **Fix C** — the autouse conftest fixture stops + joins the thread before
  pytest closes the capture stream, so the closed-stream write cannot happen
  in tests regardless of backend state.

Net: with backends up OR down, the thread no longer outlives its owner (B/C),
pre-warm no longer runs in probe-only mode (E), and any stream that does close
under an active handler is tolerated (A).
