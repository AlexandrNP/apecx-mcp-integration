---
name: nanobrain-executors
description: Choosing and configuring an Executor (Local, Thread, Process, Parsl). Covers the async contract (`process()` must be async), executor-specific deadlock risks, the Parsl/HPC code path (Aurora PBS, ProxyStore, dynamic scaling), and verbatim error messages. Read whenever you set or change a step's `executor:` field, especially before declaring distributed execution "working".
---

# nanobrain-executors

> **Distributed silent-failure on missing `auto_transfer`:** When a Parsl
> worker step writes its output and the downstream `DirectLink` lacks
> `auto_transfer: true`, the worker completes successfully, the executor
> reports "step done", and **no data ever transfers across the process
> boundary**. The trigger cascade silently no-ops. Cross-reference
> `nanobrain-data-units-triggers-links` warning. Check every link that
> crosses an executor boundary.

> **AcademyManagerWrapper singleton:** Per workspace `CLAUDE.md`, the
> `AcademyManagerWrapper` enters a process-wide context on first use. Tests
> that touch Academy MUST call `shutdown_academy_manager()` in teardown,
> otherwise singleton state bleeds across tests.

> **Parsl `worker_init` MUST point at `.venv/bin/python`,** not system
> Python. Workers that load with the wrong interpreter fail with the
> "missing nanobrain" symptom (see `nanobrain-testing-debugging` § Wrong
> Python interpreter).

## Why this skill exists

The wrong executor for a workload is one of the most common nanobrain
performance bugs:

- `LocalExecutor` for a CPU-bound step → starves the event loop.
- `ThreadExecutor` for an `async def process` → returns a coroutine the thread
  can't await; deadlock or `RuntimeError`.
- `ProcessExecutor` for a step holding non-pickleable state → silent submission failure.
- `ParslExecutor` outside an HPC environment → silent local fallback (or hang
  on Parsl initialization).

Beyond that, the framework's distributed code path is partially **stubbed**.
Several "distributed" features are mocks. Verify before claiming they work.

## File:line ground truth

| Concern | File | Approx. line |
|---|---|---|
| `ExecutorConfig` | `nanobrain/nanobrain/core/executor.py` | 30 |
| `LocalExecutor` | `nanobrain/nanobrain/core/executor.py` | 512–615 |
| `ThreadExecutor` | `nanobrain/nanobrain/core/executor.py` | 624–711 |
| `ProcessExecutor` | `nanobrain/nanobrain/core/executor.py` | 722–810 |
| `ParslExecutor` | `nanobrain/nanobrain/core/executor.py` | 936–1459 |
| Parsl execution context detection | `nanobrain/nanobrain/core/executor.py` | 1003–1127 |
| Parsl app for step execution | `nanobrain/nanobrain/core/executor.py` | 822–905 |
| `DynamicExecutorConfigGenerator` | `nanobrain/nanobrain/core/dynamic_executor_config.py` | 30–248 |
| `PBSResourceManager` | `nanobrain/nanobrain/core/pbs_resource_manager.py` | 66–340 |
| `DistributedResourceRegistry` | `nanobrain/nanobrain/core/distributed_resource_registry.py` | full file |
| `WorkerStepPool` | `nanobrain/nanobrain/core/worker_step_pool.py` | 34–250 |
| `ResourceMonitor` | `nanobrain/nanobrain/core/resource_monitor.py` | 78–348 |
| Distributed workflow stub | `nanobrain/nanobrain/core/distributed/workflow_execution.py` | 1–103 |

## Decision matrix

| You have... | Use |
|---|---|
| Single-threaded async work, dev/testing, integration tests | `LocalExecutor` |
| I/O-bound work (API calls, disk, network), &lt; 10 workers | `ThreadExecutor` |
| CPU-bound, **pickleable** state, no shared memory | `ProcessExecutor` |
| HPC cluster (Aurora, PBS) with parallel nodes | `ParslExecutor` (with caveats below) |
| Anything that includes `mock://` URLs or claims to mock distributed exec | **Stop and verify** |

## ExecutorConfig common fields

```yaml
executor_type: local                          # local | thread | process | parsl
name: my_executor
max_workers: 4
timeout: 60                                   # seconds
parsl_config: { ... }                         # Parsl only
default_resource_specification: { ... }       # Parsl only
```

## LocalExecutor

```yaml
class: "nanobrain.core.executor.LocalExecutor"
config:
  executor_type: local
  name: local_dev
  max_workers: 1
  timeout: 30
```

Behavior: runs in the current event loop. Async functions are wrapped in
`asyncio.create_task()`. Sync functions are called directly. If a sync
function returns a coroutine, the executor `await`s it.

Best for: tests, smoke verification, single-machine work.

## ThreadExecutor

```yaml
class: "nanobrain.core.executor.ThreadExecutor"
config:
  executor_type: thread
  name: io_pool
  max_workers: 8
  timeout: 60
```

Behavior: submits to `ThreadPoolExecutor` via `loop.run_in_executor`. Threads
**cannot await coroutines**. If your `process` is `async def`, the thread
runs a coroutine object — undefined behavior, almost certainly a bug.

Best for: I/O-bound sync code (legacy libraries, blocking SDKs).

**If your step is `async def process`, do NOT use ThreadExecutor for the
step itself.** Use `LocalExecutor` and `await asyncio.to_thread(...)` inside
`process` for the blocking parts.

## ProcessExecutor

```yaml
class: "nanobrain.core.executor.ProcessExecutor"
config:
  executor_type: process
  name: cpu_pool
  max_workers: 4
  timeout: 300
```

Behavior: submits to `ProcessPoolExecutor`. **Tasks must be pickleable** —
no closures, no lambdas, no references to non-pickleable objects. Failures
at submission can be silent.

Best for: CPU-bound pure functions with serializable state.

## ParslExecutor

```yaml
class: "nanobrain.core.executor.ParslExecutor"
config:
  executor_type: parsl
  name: aurora_pssm
  max_workers: 12
  timeout: 3600
  parsl_config:
    strategy: null
    app_cache: true
    checkpoint_mode: task_exit
    retries: 1
    executors:
      - label: aurora_pssm_htex
        class: parsl.executors.HighThroughputExecutor
        max_workers_per_node: 4
        cores_per_worker: 1
        worker_debug: true
        heartbeat_period: 30
        provider_config:
          class: parsl.providers.PBSProProvider
          queue: debug
          account: FoundEpidem
          nodes_per_block: 1
          cpus_per_node: 4
          walltime: "00:25:00"
          worker_init: |
            #!/bin/bash
            module load frameworks
            source /home/onarykov/miniconda3/etc/profile.d/conda.sh
            conda activate nanobrain-upd
            export PYTHONPATH="/home/onarykov/nanobrain:$PYTHONPATH"
          parallelism: 1.0
  fallback:
    enable_fallback: false
    executor_type: local
    max_workers: 4
```

For local development, use a `LocalProvider` instead of `PBSProProvider`:

```yaml
parsl_config:
  executors:
    - label: htex_local_fast
      class: parsl.executors.HighThroughputExecutor
      max_workers_per_node: 2
      provider_config:
        class: parsl.providers.LocalProvider
        min_blocks: 1
        init_blocks: 1
        max_blocks: 1
```

### Parsl execution context detection

`ParslExecutor` detects whether it's running inside an existing PBS allocation
(via `PBS_JOBID` and `PARSL_WORKER_RANK` env vars). Three modes:

- **Top-level** (no PBS env): uses your specified executor (HTEX, WorkQueue).
- **Subworkflow** (inside Parsl worker): forces `WorkQueueExecutor` +
  `LocalProvider` to reuse the existing allocation. You don't get to override.
- **Local** (no PBS): uses `LocalProvider`.

### Async contract on Parsl

Parsl detects `asyncio.iscoroutinefunction(step.process)`; if true, the worker
runs an event loop and uses `asyncio.run_until_complete()`. So
`async def process` works on Parsl.

### Required environment for Aurora HPC

- `PBS_JOBID`, `PBS_NCPUS`, `PBS_NODEFILE` — set by PBS.
- `PYTHONPATH` — must include `nanobrain` source root.
- `conda activate nanobrain-upd` (or your env) in `worker_init`.
- `proxystore` installed (for cross-node resource sharing).
- Shared filesystem (e.g., `/lus/flare`) for ProxyStore file connector.

## Resource management subsystems

| Component | Role |
|---|---|
| `PBSResourceManager` | Discovers PBS nodes via `pbsnodes -a -F json`; allocates cores per workflow |
| `DynamicExecutorConfigGenerator` | Generates per-workflow Parsl config so concurrent workflows don't oversubscribe |
| `DistributedResourceRegistry` | ProxyStore-backed registry for cross-node resource discovery |
| `WorkerStepPool` | Per-worker step instances for parallel processing |
| `ResourceMonitor` | Watches disk space; pauses workflow when critical |
| `ProgressiveScalingMixin` | Optional mixin for steps to start small and scale up |

## Stubbed / mock paths to be aware of

The framework's own CLAUDE.md states "Mock implementations for many
distributed features." Concrete mock sites:

1. **`distributed/workflow_execution.py:1–103`** — `execute_workflow_distributed`
   Parsl app is stubbed; not integrated end-to-end. Distributed workflow
   execution may silently fall back to local.
2. **`mcp_support.py:343–451`** — when `aiohttp` missing, MCP client is mocked.
   `mock://` URLs trigger mock mode.
3. **`distributed_resource_registry.py:68`** — ProxyStore optional; without
   it, cross-node registry doesn't work but no error is raised.
4. **`pbs_resource_manager.py:135, 153`** — defaults to "8 cores per node" if
   pbsnodes data is absent. May undersubscribe non-standard configurations.
5. **Parsl resource_specification (executor.py:1478–1491)** — collected from
   YAML and passed through, but the Parsl worker app does nothing with it.
6. **WorkerStepPool shutdown** — incomplete in the version analyzed; check
   for resource leaks in long-running pools.

**Per workspace policy: do not call distributed execution "tested" until you
have run it against real Parsl on real nodes with real (small) data and
recorded the outcome.**

## Verbatim error messages

```
ImportError: Parsl not available. Install with: pip install parsl
```
> Install Parsl, or change the executor to non-Parsl.

```
RuntimeError: ParslExecutor not initialized
```
> Awaited an execute call before `await executor.initialize()`.

```
ValueError: Step instance missing _config_path for Parsl execution
```
> The Parsl app needs to recreate the step on the worker via
> `step_class.from_config(step_config_path)`. Step instances created from
> inline dicts lack `_config_path`. Use a YAML file.

```
RuntimeError: Parsl execution failed: {error}\n{traceback}
```
> Read the worker-side traceback. Common: missing module on worker
> (PYTHONPATH), import error, missing data file (shared FS path mismatch).

```
RuntimeError: Both PARSL and fallback execution failed.
PARSL error: {original}, Fallback error: {fallback}
```
> Both paths broken. Inspect both errors; usually the original Parsl error
> is the one to fix.

```
ValueError: Invalid config type: {type}. Expected str or ExecutorConfig
```
> You passed something other than a path or ExecutorConfig to from_config.

```
ImportError: ProxyStore not installed.
Install with: pip install 'nanobrain[academy]' or pip install proxystore
```
> Install ProxyStore for distributed resource registry.

## Pitfalls

1. **`ThreadExecutor` for an async-def process.** Subtle and often hangs.
2. **`ProcessExecutor` for a step that captures unpickleable state.** Silent
   submission failure or pickle error.
3. **Parsl with no `PBS_JOBID` claiming "distributed".** It's actually local.
4. **Parsl `worker_init` script that doesn't activate the right conda env.**
   Worker can't import nanobrain; cryptic ImportError.
5. **Step uses inline dict config, then runs on Parsl.** Worker can't
   recreate the step (no `_config_path`).
6. **Two workflows sharing the same Parsl config.** Oversubscription on PBS;
   use `DynamicExecutorConfigGenerator`.
7. **Disk fills during a long Parsl run; `ResourceMonitor` not enabled.**
   Workflow crashes mid-execution.
8. **Trusting `mock://` URLs as real MCP.** Always verify with `aiohttp`
   import and a real server URL.

## Checklist

- [ ] Executor matches the workload class (Local for async dev, Thread for
      blocking I/O, Process for CPU-bound pickleable, Parsl for HPC).
- [ ] If `process` is `async def`, executor is Local or Parsl (not Thread/Process directly).
- [ ] Parsl config explicitly sets `worker_init`, `walltime`, `cpus_per_node`.
- [ ] On Parsl, every step is loaded from a YAML file (not inline dict).
- [ ] `PYTHONPATH` includes the project root in `worker_init`.
- [ ] `aiohttp` and `proxystore` installed if you depend on MCP / distributed registry.
- [ ] If using `enable_fallback: true`, you've tested both the Parsl path
      AND the fallback path.
- [ ] Distributed execution tested against real cluster on small real data, recorded.

## Tool-backend adapters (2026-05-09 -> 2026-05-11)

Distinct from step ``executor:`` (which controls how the Step itself
runs — local / thread / process / Parsl), tool-backend adapters
control how individual TOOL CALLS dispatch. Three concrete adapters
ship in-tree:

  * ``rhea`` — Rhea fork's RheaMCPDispatcher (remote MCP)
  * ``local_parsl`` — G11-completion LocalParslAdapter (local Python
    callables via Parsl). Default preset is ThreadPoolExecutor (P0++b).
  * ``http`` — G38 HTTPBackendAdapter (generic HTTP)

Adapters live at ``nanobrain.library.tools.*`` and register via
``ToolBackendRegistry.register(adapter)``. ``ToolExecutionStep``
consults the registry by ``BACKEND_NAME`` derived from the UTD's
descriptor_id prefix. See ``nanobrain-agents-tools`` skill for picker.
