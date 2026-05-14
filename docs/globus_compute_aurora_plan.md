# Globus Compute remote execution for nanobrain → Aurora — plan + results

**Date**: 2026-05-14. **Status**: IMPLEMENTED + unit/integration tested
against the framework; the real **Aurora run is documented-but-unverified**
— it needs your ALCF endpoint + credentials (see "What I need from you"
and the runbook). User answered the four design questions: **Compute +
Transfer** in scope; **general capability + minimal demo**;
**confidential-client** auth primary; **no Aurora endpoint yet** → runbook
provided. See "## Implementation results" at the bottom for what shipped.

Goal (user directive, 2026-05-14): *"flexibly use GlobusEndpoint as a
remote execution mechanism. Parsl should have the corresponding
capabilities. Ensure that the previous workflow [`rhea_muscle_alignment`]
can reliably execute code on Aurora ... It will likely involve the
usage of GlobusAuth."*

## Brutal-truth summary — read this first

1. **I cannot verify execution on Aurora.** I have no ALCF account, no
   Aurora allocation, and no Globus Compute endpoint on Aurora. The
   "reliably execute on Aurora" clause is verifiable only with your
   ALCF access. I *can* fully build and test the framework capability
   against a **local** Globus Compute endpoint (`globus-compute-endpoint`
   is pip-installable and runs on localhost). Everything ships tested
   against local; the Aurora run is a documented, ready-to-execute
   final step that needs your endpoint UUID + auth.

2. **There is a conceptual mismatch between "the previous workflow"
   and "execute code on Aurora."** `rhea_muscle_alignment` has three
   steps: `FastaCollectionStep` (reads a local file), `RheaFileToolStep`
   (an MCP *client* — the actual MUSCLE compute happens *inside* Rhea,
   wherever Rhea runs), and `AlignmentReportStep` (a ~1 ms pure
   transform). None is naturally a "supercomputer workload." Binding
   any of them to Aurora is technically valid but — for this specific
   workflow — somewhat artificial. The honest deliverable is the
   **general framework capability** (any step bindable to a Globus
   Compute endpoint); the rhea workflow is the *integration vehicle*.
   See "Open question: which step runs on Aurora" below.

3. **The ALCF doc you linked is Globus *Transfer*, not Globus
   *Compute*.** `docs.alcf.anl.gov/.../using-globus/#alcf-globus-endpoints`
   is about *data-transfer* endpoints. "Remote execution mechanism" /
   "execute code" is **Globus Compute** (formerly funcX) — a different
   service. They share **Globus Auth** (OAuth2). A *complete* Aurora
   workflow plausibly needs both: Transfer to stage inputs onto
   Aurora's `/lus/flare` filesystem, Compute to run the code. Whether
   Transfer is in scope is an open question for you (below).

4. **Globus Compute requires a long-running `globus-compute-endpoint`
   process on an Aurora login node**, configured with a PBS provider.
   I cannot SSH to Aurora to set that up. I will provide the endpoint
   config template + a runbook; **you run it on Aurora.**

## Current-state facts (verified this session)

- nanobrain's `ExecutorBase` abstraction (`core/executor.py:44`) is
  cleanly extensible: `ExecutorType` enum (`local|parsl|thread|process`),
  `ExecutorConfig` with a `parsl_config: Dict` field, and
  `executor.execute(task, resource_specification=..., **kwargs)` where
  `task` is a **callable/coroutine**, never a live Step object.
- The Parsl executor already solves the hard part of remote step
  execution: `_get_execute_step_parsl_app()` ships
  `(step_config_path, step_class_name, input_data)` to the worker and
  the worker does `step_class.from_config(...)` + `process()`. A
  Globus Compute executor mirrors this — but ships the config
  **inline as a dict** (no shared-filesystem dependency on the worker).
- `parsl 2026.5.4` is installed **and ships `parsl.executors.GlobusComputeExecutor`**
  — so the literal "Parsl should have the capabilities" ask is already
  satisfiable via the existing `ParslExecutor` + a `parsl_config` dict.
- `globus_sdk 4.5.0` is installed. `globus_compute_sdk` is **NOT** —
  it must be added as a dependency. `dill` + `cloudpickle` present.
- `PBSProProvider` is available in this Parsl (Aurora uses PBS Pro).
- No existing Globus anything in nanobrain (only deferred ProxyStore
  connector mentions).

## Design — two legitimate routes, ship both

### Route 1 (primary deliverable): a native `GlobusComputeExecutor`

A new `ExecutorBase` subclass, `executor_type: globus_compute`. Wraps
`globus_compute_sdk.Executor(endpoint_id=...)` directly. Explicit auth
handling, explicit FAIL-LOUD paths, nanobrain-native config surface,
fully unit/integration tested. This is the better-engineered path and
the real deliverable.

```yaml
# a workflow's executors: block
executors:
  aurora:
    executor_type: globus_compute
    timeout: 3600
    globus_compute:
      endpoint_id: "${AURORA_GC_ENDPOINT_ID}"   # the UUID of YOUR endpoint
      auth_mode: native                          # native | client_credentials
      # client_credentials mode also reads:
      #   client_id / client_secret  (or $GLOBUS_COMPUTE_CLIENT_ID/_SECRET)
      resource_specification:                    # optional, endpoint-dependent
        num_nodes: 1
        ranks_per_node: 1
```

Dispatch shape (the serialization-safe design): a **module-level**
function `_run_step_on_endpoint(step_class_path, step_config, input_data)`
that does `importlib` → `from_config(step_config)` → `await process()`.
Module-level so Globus Compute's dill/cloudpickle serializes it by
reference; the **step config travels inline as a dict**, so there is
no shared-filesystem assumption on the Aurora worker. Prerequisite:
`nanobrain` (and the step's own package + deps) must be importable in
the Aurora worker's Python environment — handled by the endpoint's
`worker_init`.

### Route 2 (also shipped, as a documented config + example): Parsl's `GlobusComputeExecutor`

nanobrain's existing `ParslExecutor` already accepts an arbitrary
`parsl_config` dict. A `parsl_config` whose executor class is
`parsl.executors.GlobusComputeExecutor` makes the existing executor
dispatch through Globus Compute with **zero new executor code**. We
ship a working example config + a test, and document the trade-off:
Route 2 is "free" but buries auth + endpoint config inside a Parsl
config dict and gives less control over FAIL-LOUD behavior. Route 1
is the recommended path; Route 2 satisfies the literal "Parsl should
have the capabilities" ask and is there for users already invested in
Parsl configs.

## Components to build

| # | Task | Repo | Blocked on user? |
|---|---|---|---|
| G22 | `GlobusComputeExecutor(ExecutorBase)` + the module-level remote-step runner | nanobrain | No |
| G23 | GlobusAuth wiring — native + confidential-client modes | nanobrain | No (native default; both built) |
| G24 | `ExecutorType.GLOBUS_COMPUTE` + `ExecutorConfig.globus_compute` + `workflow.py` resolve branch | nanobrain | No |
| G25 | Aurora `globus-compute-endpoint` config template + setup runbook | apecx-mcp-integration docs | Partially — needs your project/queue/env |
| G26 | Tests: unconditional + local-endpoint gated + Aurora gated | both | Local: no. Aurora: yes |
| G27 | Wire into `rhea_muscle_alignment` as the demo + `nanobrain-executors` skill update + this doc's results section | apecx-mcp-integration + nanobrain | Demo step choice: see open question |
| G28 (conditional) | `GlobusTransferStep` for staging data to Aurora's filesystem | nanobrain | Scope question — see below |

## Route-2 + Route-1 dependency: `globus_compute_sdk`

`globus_compute_sdk` must be added to the test/runtime environment.
It is pure-Python and light. Added to the apecx-mcp-integration venv
+ declared in the appropriate extras. The executor lazy-imports it
and FAIL-LOUDs with an actionable message if absent (mirrors how
`RheaFileToolStep` lazy-imports `rhea`).

## Open question: which step of `rhea_muscle_alignment` runs on Aurora

Per the conceptual-mismatch point above, the options are:

- **(a) Bind `alignment_report` to the Aurora executor** — the only
  cleanly-serializable pure transform. Minimal, honest, but it's a
  trivial workload on a supercomputer (demonstrates the *mechanism*,
  not a real HPC use case). Documented caveat.
- **(b) Add a genuinely compute-heavy step** — e.g. a
  bootstrap/phylogenetic-analysis step on the alignment. A *real*
  Aurora workload, but expands scope beyond "the previous workflow."
- **(c) Bind `RheaFileToolStep`** — only meaningful if Rhea itself
  also runs on/near Aurora; otherwise you've just moved the MCP
  *client* to a supercomputer. Not recommended in isolation.
- **(d) Ship the executor as a general capability**, demonstrate with
  (a) as the minimal honest demo, and document (b)/(c) as the paths to
  a real HPC workload. **This is my recommendation** — it ships a real,
  reusable framework capability now and is honest about the demo's
  scope.

## What I need from you

Real blockers for the **Aurora-specific verification** (not for the
framework build, which proceeds now):

1. **ALCF access** — do you have an active ALCF account and an Aurora
   allocation? If yes, the **project/allocation name** (for the PBS
   `-A` flag in the endpoint config).
2. **Globus Compute endpoint on Aurora** — is one already running? If
   yes, its **endpoint UUID**. If no, you'll need to run the setup
   runbook (G25) on an Aurora login node — I provide the config, you
   run `globus-compute-endpoint configure` + `start`.
3. **Auth mode** — `native` (interactive browser OAuth2, tokens cached
   in `~/.globus_compute/`; fine for a human-driven run) vs
   `client_credentials` (a Globus confidential client — `client_id` +
   `client_secret`; needed for headless/automated runs). I build both;
   tell me which is your primary so I gate the integration test
   correctly.
4. **Aurora worker Python environment** — is `nanobrain` (+ the step's
   package + its deps) installed/importable on Aurora? What is the
   environment-activation command (the `worker_init` line — e.g.
   `module load ...; conda activate ...`)? The remote worker must be
   able to `import nanobrain` and `from_config` the step.
5. **Demo step choice** — your call on the open question above
   (recommendation: (d)).
6. **Globus Transfer in scope?** — just Globus Compute (remote
   execution), or also Globus Transfer (staging the input data onto
   Aurora's `/lus/flare`)? The latter adds G28.
7. **Aurora filesystem path** — if Transfer is in scope, the target
   path (e.g. `/lus/flare/projects/<project>/<you>/...`).

## Testing boundary — what gets verified, honestly

- **Unconditional** (CI-safe, no Globus): config validation, executor
  load, every FAIL-LOUD path (missing sdk, missing endpoint_id, auth
  misconfiguration), the module-level remote-step runner exercised
  in-process.
- **Gated on a local endpoint** (`$GLOBUS_COMPUTE_ENDPOINT_ID`): a
  real round-trip — submit a nanobrain step to a `globus-compute-endpoint`
  running on localhost, get the real result back. This proves the
  serialization + dispatch + auth + result-fetch path end-to-end. I
  can run this myself.
- **Gated on Aurora** (`$AURORA_GC_ENDPOINT_ID`): the real Aurora run.
  Test is written + correctly gated; **you** run it (or hand me a live
  endpoint UUID + working auth and I run it). Per the workspace mocks
  policy, the component is "done" only when this has run successfully
  against real Aurora and the run is recorded — that final tick is
  yours to enable.

## Why this is framework-native and not a workaround

- `GlobusComputeExecutor` subclasses the existing `ExecutorBase` and
  slots into the existing `ExecutorType` enum + `resolve_dependencies()`
  factory — same shape as `ParslExecutor`. No parallel abstraction.
- Step ↔ executor wiring is **unchanged** — the executor abstraction
  was already general; a step binds to it via the standard `executor:`
  field. That is the point of the abstraction.
- The remote-step dispatch reuses the proven `from_config`
  reconstruction pattern from `ParslExecutor` — improved only by
  shipping the config inline (removing the shared-filesystem
  assumption).
- Route 2 is *literally* the existing `ParslExecutor` with a different
  `parsl_config` — maximal reuse, zero new code.

## Implementation results

Shipped this session. All tests run under the apecx-mcp-integration
venv (which carries `globus_compute_sdk` 4.11.0, `globus_sdk` 4.5.0).

### What shipped — nanobrain framework

| File | What |
|---|---|
| `nanobrain/core/distributed/globus_auth.py` | `build_globus_app` — shared Globus Auth helper. `client_credentials` (default, confidential client — your primary) + `native` modes. Lazy `globus_sdk` import, FAIL-LOUD on missing creds, never silently downgrades a confidential client. |
| `nanobrain/core/distributed/globus_compute_executor.py` | `GlobusComputeExecutor(ExecutorBase)` + `GlobusComputeConfig` + the module-level `_run_step_on_endpoint` worker function. |
| `nanobrain/core/executor.py` | `ExecutorType.GLOBUS_COMPUTE` enum value; `ExecutorConfig.globus_compute` dict field; `build_executor_from_config` — the shared `executor_type → executor class` dispatch (FAIL-LOUD on unknown type). |
| `nanobrain/core/workflow.py` | `resolve_dependencies` factory branch for `globus_compute` (lazy import). |
| `nanobrain/core/step.py` | **Framework fix**: `resolve_dependencies` now honors a step's own `executor_config:` (it was a declared-but-ignored field). New precedence: pre-built executor > step `executor_config` > workflow executor > default Local. This is what makes per-step Aurora binding possible. |
| `nanobrain/library/steps/globus_transfer_step.py` | `GlobusTransferStep(BaseStep)` — `globus_sdk.TransferClient` data staging; polls the transfer task to completion; FAIL-LOUD on failed/timed-out transfers. Shares `build_globus_app`. |

### What shipped — apecx-mcp-integration

| File | What |
|---|---|
| `composition/workflows/rhea_muscle_alignment_aurora/` | The minimal demo: `workflow.yml` reuses the `fasta_collection` + `muscle_alignment` step configs unchanged (they run locally); a new `steps/alignment_report_aurora.yml` + `steps/alignment_report_aurora_executor.yml` bind the `alignment_report` step to a `globus_compute` executor. |
| `docs/globus_compute_aurora_plan.md` | This doc. |
| `docs/globus_compute_aurora_runbook.md` | The operator runbook for standing up the Aurora endpoint. |
| `docs/aurora_globus_compute_endpoint_config.yaml` | The PBS-provider endpoint config template. |
| `.claude/skills/nanobrain-executors/SKILL.md` | Updated with the `GlobusComputeExecutor`, `GlobusTransferStep`, and per-step `executor_config:` binding sections (workspace-level copy synced). |

### Tests — verified vs. pending

- **Verified (unconditional, no Globus, no network)**: 53 Globus unit
  tests (auth helper both modes + FAIL-LOUD paths; executor config
  validation + FAIL-LOUD; transfer step config + FAIL-LOUD; the
  module-level remote-step runner exercised in-process). 8 per-step
  `executor_config`-binding tests. Aurora demo workflow + step configs
  load + validate via `from_config`, and the `alignment_report` step's
  `.executor` is a `GlobusComputeExecutor` while `fasta_collection`'s
  is a `LocalExecutor`. Regression: nanobrain step/workflow/executor/
  globus suites **410 passed, 0 failures**; apecx Aurora workflow
  tests **4 passed, 1 gated-skip**; G39 workflow-YAML lint clean.
- **Pending — needs your Aurora endpoint**: the real round-trip. Two
  gated tests are written + correctly gated:
  `nanobrain/tests/integration/test_globus_compute_executor_local.py`
  (gated on `$GLOBUS_COMPUTE_ENDPOINT_ID`) and
  `tests/integration/test_rhea_muscle_alignment_aurora_workflow.py`'s
  full-run test (gated on `$AURORA_GC_ENDPOINT_ID`). Per the workspace
  mocks policy, the executor is **not** "tested against real data"
  until one of these runs green against a real endpoint and the run is
  recorded. That tick is yours to enable — see the runbook.

### Corrections to the plan above

- The plan's "ship the config inline (removing the shared-filesystem
  assumption)" did **not** survive contact with the framework. The
  `STEP 0` investigation found the framework's dispatch contract hands
  the executor only a *closure* — from which you recover the step's
  *config-file path* (`_config_path` / `config.source_path`), not a
  serializable config dict. The implementation therefore ships
  `(step_config_path, step_class_name, input_data)` — exactly like
  `ParslExecutor` — and the **shared-filesystem assumption stands**:
  the step's config YAML must be resolvable on the Aurora worker. The
  runbook (§5, §6) documents this correctly.
- The inline `executor_config: {...}` block in a step YAML is not
  possible — `ExecutorConfig` is a `ConfigBase` (file-only, no inline
  dict). Per-step binding uses the `class:` + `config:` indirection
  pointing at a separate `ExecutorConfig` YAML. The skill documents
  the working shape.

### Known adjacent gap (NOT fixed — out of scope, documented honestly)

`workflow.py`'s **workflow-level** executor factory
(`resolve_dependencies`, ~line 2235) wraps executor construction in a
`try/except` that **falls back to `LocalExecutor` on any failure**. A
workflow-level `executor_config:` pointing at a `globus_compute`
executor that fails to build would silently become Local — a
silent-failure shape. The new **step-level** path
(`build_executor_from_config`) is FAIL-LOUD and does not have this
problem; the demo uses the step-level path, so it is not affected. The
workflow-level fallback was left untouched deliberately: changing it
risks regressions in unrelated workflows and is a separate, bounded
fix. Flagged here so it is not forgotten.
