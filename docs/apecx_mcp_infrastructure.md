# `apecx-mcp` infrastructure orchestrator

This document is the operator's reference for the startup-time
infrastructure orchestrator: what it brings up, what it merely probes,
which env vars steer its behavior, what operator prerequisites the
orchestrator can NOT install, and how the `infrastructure_status` MCP
tool reports state at runtime.

The orchestrator is launched as a background daemon thread from
`build_server()` in `src/apecx_integration/mcp_surface/server.py`.
Source code lives at `src/apecx_integration/infrastructure/`.

## 1. The 5-backend roster

| Backend     | Kind              | Required | Default endpoint              | Probe                                                        |
|-------------|-------------------|----------|-------------------------------|--------------------------------------------------------------|
| `postgres`  | `docker_container`| yes      | `localhost:5435`              | `psycopg.connect()` + `SELECT 1`                             |
| `redis`     | `docker_container`| yes      | `localhost:6379`              | `redis.Redis(...).ping()`                                    |
| `minio`     | `docker_container`| yes      | `localhost:9000`              | `httpx.get("/minio/health/live")`                            |
| `ollama`    | `external`        | yes      | `http://localhost:11434`      | `httpx.get("/api/tags")` + model count                       |
| `rhea_mcp`  | `host_process`    | yes      | `http://localhost:3001/mcp/`  | `MCPTransport.call("tools/list", {})` + tool count           |

What "kind" means:

- **`docker_container`** — the orchestrator will `docker run` it when
  the probe is down AND `APECX_MCP_AUTOSTART_INFRA` is enabled.
  A stopped-but-existing container is `docker start`-ed (preserves
  volume state); a missing container is freshly spawned from the
  pinned image.
- **`host_process`** — the orchestrator can `Popen` the process IF the
  prereq env vars (`RHEA_REPO_PATH`, `RHEA_PYTHON_PATH`) are set. Without
  them, the backend reports `external_unconfigured` with an actionable
  remedy. The orchestrator does NOT install Rhea or its miniconda env.
- **`external`** — operator-managed entirely. The orchestrator
  probes only; reports `external_missing` if down. Installing Ollama
  is the operator's job.

## 2. Per-backend state machine

```
missing ──start_all──► starting ──probe-ok──► ready
                              │
                              └─probe-fail──► error_starting (autostart attempted, failed)
                              │
reused: backend was already up at start_all() time.
external_skipped: APECX_MCP_AUTOSTART_INFRA=0.
external_missing: probe down + cannot autostart (Docker missing, Ollama missing).
external_unconfigured: host_process prereq env-vars unset (Rhea MCP).
degraded: was ready; latest re-probe came back unhealthy. The next status call re-probes; recovery flips back to ready/reused.
```

The `infrastructure_status` MCP tool always re-probes ready backends
on every call (with a short per-probe timeout). The tool will NEVER
return stale green from N minutes ago.

### 2.1 Data persistence and the fresh-create warning

The orchestrator's container-spawn path discriminates between two
cases:

* **Operator's container exists but is stopped** → `docker start
  <name>`. Volume state is preserved. No warning.
* **No container by that name exists** → `docker run -d --name <name>
  -v <named_volume>:<path> <image>`. The named volume in
  `ContainerSpec.volumes` survives `docker rm`, but if the operator
  ALSO removed the named volume (`docker volume rm`) the new container
  starts empty.

That second case is a real silent-failure shape: the probe goes green
on the empty fresh container, every test passes, and the operator
discovers their pgvector rows are gone only when a workflow returns
zero results. To surface this honestly the orchestrator sets a
`fresh_create_warning` on the `BackendRuntime` whenever it takes the
`docker run` path; `infrastructure_status` lifts the warning into the
`actionable` list. The warning fires unconditionally on fresh creation
— "the named volume MAY survive prior data" is not the same as "it
DOES" — and the operator's response is to verify (volume list, row
counts) before trusting the backend.

Backends with declared named volumes:

| Backend                | Volume                          | Mounted at                    |
|------------------------|---------------------------------|-------------------------------|
| `apecx-rhea-postgres`  | `apecx-rhea-postgres-data`      | `/var/lib/postgresql/data`    |
| `apecx-rhea-minio`     | `apecx-rhea-minio-data`         | `/data`                       |
| `apecx-redis`          | *(intentionally none)*          | — (ephemeral cache only)      |

If you've been running with the pre-volume orchestrator (before this
commit), your existing `apecx-rhea-postgres` / `apecx-rhea-minio`
containers do NOT have these named volumes attached. They keep
working — the orchestrator reuses them on probe-green and never
touches them. To migrate: stop + remove the container, let the
orchestrator (or `apecx-setup`) recreate it from the new spec. You
WILL lose the data inside the old container; export it first if it
matters (`pg_dump` for Postgres, `mc mirror` for MinIO).

### 2.2 Tool conda env pre-warm phase (2026-05-15)

The orchestrator runs a **pre-warm phase** AFTER `start_all()` returns
and BEFORE `infrastructure_status` first reports `overall=ready` from a
cold start. Goal: every Rhea-side conda env a packaged workflow depends
on is built + conda-pack-cached in Redis at startup — never on the
first user invocation.

#### Why this exists

Rhea's `agent_on_startup` lazily calls `rhea.agent.utils.install_conda_env`
the first time a tool is invoked. Two real reliability problems flow
from that lazy design:

1. The first user invocation pays a 30–90 s install cost. Claude
   Desktop's MCP timeouts (or the user's patience) often end first,
   they retry, the install kicks off again, latency compounds.
2. If `install_conda_env` raises, the Academy actor enters a wedged
   state and every subsequent `run_tool` returns
   `"Action 'run_tool' was cancelled by the agent."` for the rest of
   the rhea-server's lifetime. Operator has no recovery short of
   restarting Rhea.

Pre-warm sidesteps both: it calls `install_conda_env` **directly** (not
through the Academy actor) at orchestrator startup. The conda env is
built, packed, cached BEFORE any MCP tool can be invoked. The first
real user call hits the Redis cache (`unpack_conda_env` is ~1 s, no
install) and there is no wedge risk because the slow + fragile install
already ran where errors propagate cleanly into the status tool.

#### How tools declare a pre-warm dependency

Each `WorkflowCatalogEntry` (in `mcp_workflow_catalog.yml`) carries an
optional `prewarm_rhea_tools: list[str]` field. The orchestrator unions
those lists across the catalog (deduped) and pre-installs each tool
serially (conda's caches don't tolerate concurrent installs in the
same prefix).

```yaml
# mcp_workflow_catalog.yml — rhea_muscle_alignment entry
prewarm_rhea_tools:
  - muscle
```

A tool that's already cached in Redis (`HEXISTS conda_envs <tool>` →
1) returns `state="reused"` in ~150 ms; a cold cache miss runs the
real install (~55 s wall time for MUSCLE on a clean Mac).

#### Recoverable conda-failure self-heal

`install_conda_env` knows about two families of recoverable failure.
Each has a distinct recovery action and is gated to run at most once
per call (so a deeper broken-conda doesn't loop):

| Signature in stderr | Recovery action | Source |
|---|---|---|
| `Prefix record`, `already exists`, `Multiple packages found` | `conda clean --all -y` + `conda env remove` + retry strict | metadata corruption between local cache and env metadata |
| `libmamba`, `libarchive`, `solver backend`, `libmambapy` | Retry with `CONDA_SOLVER=classic` in subprocess env | operator's `~/.condarc` says `solver: libmamba` but the conda install's libarchive/libmamba dyld chain is broken — typical on `/opt/anaconda3` after a homebrew upgrade |

Recovery is per-call only: `CONDA_SOLVER=classic` is set in the
subprocess env that `_try_create` spawns, never in `os.environ`. The
operator's intended conda config remains untouched for every other
process the orchestrator owns.

Additionally, `install_conda_env` now ALWAYS passes
`-c bioconda -c conda-forge` to `conda create`. Rhea exists to install
Galaxy tools; Galaxy's `<requirement type="package">` wrappers assume
bioconda (primary) + conda-forge (deps). Without these channels on the
command line, a fresh-conda operator with an empty `~/.condarc` gets a
`PackagesNotFoundError` on the very first tool install and no signpost
telling them why. Operators with site-specific mirrors prepend extra
channels via `RHEA_CONDA_EXTRA_CHANNELS` (comma-separated); the
bioconda+conda-forge canonical pair always lands after them so the
operator's channels take priority.

#### Failure surfacing

`infrastructure_status` includes the pre-warm report under the
`rhea_tool_prewarm` key. Each tool's outcome is one of:

| `state` | Meaning | Latency |
|---|---|---|
| `ready` | Env built, packed, cached fresh this startup | ~30-90 s |
| `reused` | Env already in Redis from a prior process; cache hit | ~150 ms |
| `failed` | Install raised. See `detail` + `error` for actionable text | varies |
| `skipped` | Reserved (e.g. catalog declares no tools) | — |

Any `state="failed"` tool also lifts a `[prewarm:<tool>] <detail>`
entry into the top-level `actionable` list, so an operator polling
`infrastructure_status` from Claude Desktop sees the remediation step
at startup — not at first user call.

#### Conda-pack on the critical path (2026-05-15, follow-up fix)

Rhea's `install_conda_env` was fire-and-forgetting `pack_conda_env`
via `loop.run_in_executor(...)` + `asyncio.ensure_future(...)` —
returning to the caller while the conda-pack thread was still writing
the tar.zst archive into Redis. Two real failure shapes flowed from
that race:

1. **Short-lived caller race.** Pre-warm runs inside an asyncio-driven
   subprocess. When `install_conda_env` returns, `asyncio.run()`
   closes the loop and Python exits — the pack thread may be mid-
   `pipe.execute()` and the Redis cache lands either empty or with a
   truncated blob. Subsequent consumers see `HEXISTS=1` and unpack a
   broken archive, restoring no env and failing at first tool call.
2. **Long-lived actor first-call race.** Even inside the Academy
   actor's long-lived process, `agent_on_startup` signals
   ``_startup_done`` as soon as ``install_conda_env`` returns. A
   second consumer (parallel actor, sibling orchestrator) hitting
   Redis between actor-ready and pack-complete misses the cache and
   redundantly re-runs `conda create`, defeating the cache's purpose
   for that window.

The fix is one line: `await loop.run_in_executor(None, pack_conda_env,
...)` instead of `ensure_future`. Conda-pack is now ON the critical
path, the wall-time cost (~1-10 s per env depending on size) is
moved from "background, race-prone" to "foreground, observable in
status_report.tools[].latency_seconds." When `install_conda_env`
returns, the cache is fact, not promise.

#### Validation outcome — single tool (2026-05-15)

End-to-end on a Mac with a freshly-corrupted `/opt/anaconda3`
(libmamba broken, no `~/.condarc`, classic solver only knows
`pkgs/main`+`pkgs/r`):

| Step | Result |
|---|---|
| `_fetch_tool_requirements` — psycopg JSONB unwrap | extracted 1 `{type, value, version}` dict |
| Rhea-venv subprocess spawn | succeeded |
| First `conda create` (libmamba) | failed at solver-init (`libarchive.20.dylib` not found) |
| Self-heal: retry with `CONDA_SOLVER=classic` | classic solver loaded, ran resolve |
| Resolve against default channels only | `PackagesNotFoundError: muscle=3.8.1551` |
| With `-c bioconda -c conda-forge` baked in | resolved + installed |
| `install_conda_env` post-install verification chain | passed (`conda list`, `bin/` non-empty, major-version-match) |
| `conda_pack` archive → Redis cache (awaited) | persisted (`HEXISTS conda_envs muscle` = 1, 8.72 MB archive) |
| Cold install wall time | **55.4 s** |
| Cache-hit reuse wall time | **0.13 s** (440× faster) |
| Orchestrator `status()` surfacing | `rhea_tool_prewarm.all_ready = true` |
| Forced-failure path (unknown tool name) | `state="failed"`, actionable surfaces |

#### Empty-requirements tools (2026-05-15, latent-bug fix)

`install_conda_env`'s "bin/ contains no package binaries → metadata-only
silent failure" check used to fire unconditionally — including for
Galaxy tools whose `<requirement type="package">` list is empty (the
`tp_cat`/`tp_head_tool`/`nl` family that wraps a system utility and
needs only a callable conda env, not packages inside it). The check
now gates on `[r for r in requirements if r.type == "package"]` being
non-empty, aligning with the per-requirement version check that was
already correctly type-gated. Without this fix, declaring any
empty-requirements tool in `prewarm_rhea_tools` (or hitting one via
Academy actor lazy install) raised RuntimeError with a misleading
libmambapy/libarchive remediation message; the env was torn down and
the actor wedged. See rhea fork commit `1e15d97` for the one-line gate.

Validation: `prewarm_tool('tp_cat')` now returns
`state="ready", 14.4 s, 5.5 KB archive cached` (vs. the old
`state="failed", 19.8 s, error="bin/ contains no package binaries..."`).

#### macOS unpack-target fix (2026-05-15, wedge-actor follow-up)

Rhea's `agent/tool.py:agent_on_startup` passed
`target_path="/home/rhea/conda/envs/<tool.id>"` unconditionally to
`install_conda_env`. That's the canonical path inside Rhea's Linux
container deployment (where `/home/rhea` is the service user's home).
On macOS, `/home` is an autofs read-only mount (`mkdir /home/rhea`
returns `Operation not supported`) — `tar.extractall` raises
`PermissionError`, `agent_on_startup` raises, the Academy actor enters
a wedged state, and every subsequent `run_tool` returns
``"Action 'run_tool' was cancelled by the agent"`` for the rest of the
rhea-server's lifetime.

Pre-warm DID cache the env in Redis (validated separately), but the
actor's unpack target was a DIFFERENT (and hostile) filesystem path,
so the cache hit on `HEXISTS conda_envs muscle = 1` then failed at
`tar.extractall("/home/rhea/conda/envs/muscle")`. Two-side fix:

| Side | Change | Default |
|---|---|---|
| `rhea/agent/tool.py` | read `$RHEA_CONDA_ENVS_DIR` env var; default `/home/rhea/conda/envs` (Linux compat) | unchanged for production |
| `orchestrator.py::_compose_rhea_env` | set `RHEA_CONDA_ENVS_DIR=~/.cache/apecx-rhea/conda/envs` on spawn | macOS dev now works without override |

`~/.cache/apecx-rhea/conda/envs` is chosen because it survives reboots
(unlike `$TMPDIR` which macOS purges aggressively), is XDG-compliant,
and is operator-owned so no sudo needed.

#### Validation outcome — MCP-based muscle workflow execution (2026-05-15)

End-to-end test of the user-facing path (Claude Desktop's MCP tool call
→ apecx-mcp FastMCP server → workflow_registry's synthesized tool →
nanobrain Workflow.from_config → trigger cascade → RheaFileToolStep →
Rhea MCP `tools/call` → Academy muscle actor → conda-pack unpack from
Redis to `~/.cache/apecx-rhea/conda/envs/muscle` → MUSCLE binary → result
fetched back through ProxyStore):

| Test | What it pins | Result |
|---|---|---|
| `test_direct_step_chain_against_live_rhea` | Direct three-step chain (collect → muscle → report), no cascade | PASS 42.8 s |
| `test_workflow_from_config_against_live_rhea` | Full Workflow.from_config + trigger cascade with auto_transfer=true DirectLinks | PASS 13.1 s |
| `test_rhea_tool_call_against_live_rhea` | apecx-mcp FastMCP server's `call_tool("rhea_muscle_alignment", {...})` end-to-end | PASS (in 4-test suite, 7.4 s) |

All three exercise different layers of the user-facing path; they all
pass on real data (5-sequence MUSCLE alignment) against a live Rhea
MCP server spawned by the orchestrator.

#### Validation outcome — synonym dictionary harmonization (2026-05-15)

Both the backend (build) and user-end (lookup) paths exercise the
nanobrain framework natively. The build is a three-step workflow
(taxdump_fetch → dictionary_build → optional resolve); the lookup is
the two-step IRIResolutionWorkflow (normalize → resolve).

| Surface | What it pins | Result |
|---|---|---|
| `bootstrap.ensure_dictionary()` idempotent cache-hit | Detects existing `~/.apecx/dictionary/dictionary.sqlite`, skips build | 1 ms; "synonym dictionary already present — skipping build" |
| Live 5-row build via `BaseStep.from_config` on real VIOLIN + taxdump | Steps actually execute against real data | 22.9 s; 75 MB SQLite; 15 entries + 61 synonyms + 2.8M taxon_hierarchy + 99 K merged_taxons; 0 ambiguous_surface_forms |
| `tests/integration/test_iri_resolution_workflow.py` (8 tests, IRIResolutionWorkflow.from_config + process cascade) | Two-step nanobrain DAG (normalize → resolve) on live dict | 8/8 pass in 6.81 s |
| Live `resolve_canonical_entity("SARS-CoV-2", "pathogen")` | MCP-tool layer dictionary lookup | `NCBITaxon_2697049 / "Severe acute respiratory syndrome coronavirus 2"`, confidence 1.0, fast path |
| Live `resolve_canonical_entity("Chikungunya virus", "pathogen")` | Second positive case | `NCBITaxon_37124`, confidence 1.0, fast path |
| Live `resolve_canonical_entity("completely-bogus-name-12345", "pathogen")` | Negative case — `miss` is reported honestly, not faked | `resolution_path: miss`, confidence 0.0, "no match in dictionary or database" |

The dictionary lookup tools are nanobrain-native: the workflow is
authored as a `BaseStep`+YAML graph (visible via `Workflow.from_config`);
the MCP tool surface (`resolve_canonical_entity`) is a thin async wrapper
that delegates to the same dictionary loader the workflow uses, so the
"fast" + "slow" + "miss" semantics are identical across the two entry
points.

#### Validation outcome — multi-tool (2026-05-15)

Same Mac, after clearing the muscle cache, ran the pre-warm against
two tools serially to validate (a) the per-tool walk works, (b) the
await fix is honored across non-default-channel tools, (c) different
archive sizes both land complete:

| Tool | Channel | Wall time | Archive size in Redis |
|---|---|---|---|
| `tp_awk_tool` (gawk 5.3.1) | conda-forge | 85.0 s | 814 KB |
| `muscle` (3.8.1551) | bioconda | 67.7 s | 8.72 MB |

Both archives are non-empty and within ±5 % of the env's on-disk
`du -sh /opt/anaconda3/envs/<name>` size, confirming no truncation.
Logs preserved at `/tmp/apecx-prewarm-validation/multi_tool_await.log`.

### 2.3 Pre-warm phase as a nanobrain workflow (2026-05-15, G56-G58)

The pre-warm pipeline was refactored from an imperative Python driver
(``infrastructure/rhea_prewarm.py::prewarm_workflow_catalog``) into a
real nanobrain :class:`Workflow` at
``src/apecx_integration/infrastructure/prewarm_workflow/configs/prewarm_workflow.yml``.
The underlying helpers (cache probe, Postgres query, subprocess
install) still live in ``rhea_prewarm.py`` — the workflow steps are
thin nanobrain wrappers around them, so all behavior is preserved
and the helpers' unit tests remain authoritative.

#### Why a workflow instead of a function?

* **Visibility.** Operators reading the workflow YAML see the three
  stages (collect_tools → install_tools → aggregate_report) by name;
  the topology + the per-step trigger + data unit ownership is in one
  reviewable artifact.
* **Extensibility.** Future improvements (parallel install via
  ``ParallelStep``, retries via ``LoopController``, per-tool gating
  via ``ConditionalLink``) are now expressible with first-class
  nanobrain primitives. The old imperative driver would need ad-hoc
  Python branches for each.
* **One pattern, one mental model.** The orchestrator drives pre-warm
  the same way it drives every other apecx workflow —
  ``Workflow.from_config(...)`` + ``process()`` +
  ``wait_for_cascade()``. No special-case Python in
  ``InfraOrchestrator``.

#### Topology

```
prewarm_request (workflow input)
        │ DirectLink (auto_transfer=true)
        ▼
collect_tools          (CollectToolsStep)        — walks catalog, dedupes tool_names
        │ collect_tools_output: {tool_names, install_config}
        ▼
install_tools          (InstallToolsStep)        — serial walk; calls prewarm_tool() per tool
        │ install_tools_output: {results: list[ToolPrewarmResult]}
        ▼
aggregate_report       (AggregateReportStep)     — builds PrewarmReport (started_at, completed_at, all_ready)
        │ prewarm_report: PrewarmReport
        ▼
prewarm_report (workflow output)
```

Three steps, four DirectLinks (all ``auto_transfer: true`` against
the dominant silent-failure shape). Each step owns its own
input/output DUs + DataUnitChangeTrigger; the workflow owns only the
entry/exit DUs and the four links. Per-step ``execution_timeout`` on
``install_tools`` is bumped to 1800 s to accommodate catalogs with
multiple tools at the 60-90 s install cost each.

#### Two authoring paths, one workflow

* **YAML** — ``Workflow.from_config(prewarm_workflow.yml)`` —
  canonical, reviewable, version-controlled.
* **WorkflowBuilder** — ``build_prewarm_workflow_via_builder()`` in
  ``infrastructure/prewarm_workflow/builder.py`` — programmatic
  assembly via :class:`nanobrain.lightweight.WorkflowBuilder`. Same
  step classes + step config YAMLs; just rewired in Python.

The builder path applies an inline ``_rewrap_link_entries_nested``
workaround for the known WorkflowBuilder issue (friction-log #26)
where the lightweight builder emits flat-shape link entries that
the framework's link loader silently drops. A unit test
(``test_builder_variant_produces_equivalent_workflow``) cross-checks
that the two paths produce semantically equivalent workflows
(same step set, same link count, same IO DU names) — so they can't
drift undetected.

#### Tests

| Layer | File | Count | Validates |
|---|---|---|---|
| Unit (loadability + step contracts) | ``tests/unit/test_prewarm_workflow.py`` | 10 | YAML loads through ``Workflow.from_config``; per-step ``process()`` FAIL-FASTs at shape boundaries with actionable errors; AggregateReportStep correctly aggregates ``all_ready``; builder + YAML produce equivalent workflows; ``auto_transfer: true`` on every parsed link |
| Integration (live cascade) | ``tests/integration/test_prewarm_workflow_live.py`` | 3 | YAML workflow drains end-to-end against live Postgres+Redis; builder workflow drains the same way (proves the rewrap workaround works); orchestrator's ``prewarm_workflow_tools`` stashes the report under ``self._prewarm_report`` and ``status()`` surfaces it correctly |
| Unit (helper module, unchanged) | ``tests/unit/test_rhea_prewarm.py`` | 9 | The underlying helpers (``_fetch_tool_requirements`` JSONB unwrap, ``_collect_tools_from_catalog`` dedupe, ``PrewarmReport.all_ready`` predicate, ``Pydantic extra='forbid'`` on the catalog field) still pass — confirms the refactor preserved their contract |

#### Validation outcome (2026-05-15)

* All 10 prewarm-workflow unit tests pass in 0.70 s.
* All 3 prewarm-workflow integration tests pass in 2.52 s (cache-hit
  path; each test is sub-second since the muscle env was already in
  the Redis cache from prior turns).
* Broader regression check: 68 / 69 related tests pass (the 1
  failure is the pre-existing
  ``test_rhea_mcp_probe_against_live_localhost``, an env-state test
  that requires Rhea MCP on :3001 to be running; unrelated to this
  refactor).
* Smoke probe of the orchestrator's ``prewarm_workflow_tools()``
  against live infra:
  ``drained=True, wall_seconds=1.74, all_ready=true``; muscle hit
  the Redis cache in 6.6 ms.

## 3. Environment variables

| Variable                    | Default                          | Effect                                                                                                                                                                  |
|-----------------------------|----------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `APECX_MCP_AUTOSTART_INFRA` | `1` (on)                         | When `0`, the orchestrator runs in probe-only mode: it never spawns containers/processes, but still reports current state through the status tool.                       |
| `APECX_LLM_BASE_URL`        | `http://localhost:11434/v1`      | Where the Ollama probe looks. A trailing `/v1` is stripped (Ollama's REST API is rooted at the host, not under `/v1`).                                                  |
| `RHEA_MCP_URL`              | `http://localhost:3001/mcp/`     | Where the Rhea MCP probe connects.                                                                                                                                       |
| `RHEA_REPO_PATH`            | unset                            | Path to the Rhea checkout. Required for the orchestrator to attempt autostart of the Rhea MCP host process. Unset → state is `external_unconfigured`.                  |
| `RHEA_PYTHON_PATH`          | unset                            | Path to **Rhea's uv venv `bin/`** (`$RHEA_REPO_PATH/.venv/bin`) whose Python carries Rhea + its deps. **NOT a bare miniconda bin** — that lacks `rhea`, `debugpy`, etc. The orchestrator runs an `import rhea` pre-spawn check and FAIL-LOUDs with this guidance in 2-3 s if the Python is wrong. |
| `RHEA_CONDA_BIN`            | unset                            | Optional. Path to the miniconda `bin/` carrying `conda`. Prepended to the spawned Rhea's PATH so Rhea's downstream tool agents (which run `conda run -n <env>`) find the right `conda`. Without it, the system `conda` is used — if that's a broken Anaconda install, conda subprocesses fail loudly inside Rhea. |
| `RHEA_CONDA_EXTRA_CHANNELS` | unset                            | Optional. Comma-separated list of extra conda channels prepended on every `conda create` the pre-warm + lazy install paths emit. Bioconda + conda-forge are always passed AFTER the operator's extras so site mirrors / private indexes take priority. Empty/unset → just bioconda + conda-forge. |
| `RHEA_CONDA_ENVS_DIR`       | `~/.cache/apecx-rhea/conda/envs` when orchestrator-spawned; `/home/rhea/conda/envs` (Rhea's bare default) otherwise | Where Rhea's Academy tool actor unpacks the conda-pack archive from Redis. The Linux default is structurally inaccessible on macOS (`/home` is autofs read-only) — without an override the actor wedges with `"Action 'run_tool' was cancelled by the agent"`. The orchestrator sets a writable default at spawn time; operators can override (e.g. point at faster SSD scratch) via this env var. |
| `RHEA_EMBEDDING_MODEL`      | `mxbai-embed-large`              | The embedding model Rhea uses for its tool-catalog vector store. 1024-dim default matches the model `apecx-setup` pulls into Ollama. Rhea's bare-install default (`Qwen/Qwen3-Embedding-0.6B`) requires a different embedding server entirely. |
| `APECX_MCP_SKIP_HEALTHCHECK`| unset (off)                      | Skips the **control-plane** healthcheck (the legacy `_verify_control_plane_reachable` path). Does NOT affect the infrastructure orchestrator; use `APECX_MCP_AUTOSTART_INFRA=0` for that. |

## 4. Operator prerequisites we can't install

The orchestrator will tell you what's missing with an actionable
message. The three things it cannot install for you:

### Docker Desktop

- The orchestrator can't `brew install --cask docker` for you (it
  requires sudo and a UI permission dance). Get it from
  <https://www.docker.com/products/docker-desktop/>. Start it before
  launching `apecx-mcp`.
- Detection: when `docker info` returns non-zero, every `docker_container`
  backend transitions to `external_missing` with the install link in
  the actionable message.

### Ollama

- Install: macOS `brew install ollama`, Linux
  `curl -fsSL https://ollama.ai/install.sh | sh`, or download from
  <https://ollama.com/download>.
- Start: `ollama serve` (or `brew services start ollama` on macOS).
- Detection: probe at `$APECX_LLM_BASE_URL` (default
  `http://localhost:11434`) fails → `external_missing` with the
  install link.

### miniconda / Rhea checkout

- Required only if you want the orchestrator to autostart the Rhea
  MCP host process. Otherwise: start `python -m
  rhea.server.mcp_server --transport streamable-http` yourself from
  inside Rhea's checkout.
- Detection: `RHEA_REPO_PATH` and/or `RHEA_PYTHON_PATH` unset →
  `external_unconfigured`.

#### Verified spawn recipe (this configuration is real-tested, not fake-tested)

```bash
# in claude_desktop_config.json's "env" block, or your shell:
export RHEA_REPO_PATH=/path/to/your/rhea-checkout
export RHEA_PYTHON_PATH=$RHEA_REPO_PATH/.venv/bin     # uv-managed venv with rhea installed
export RHEA_CONDA_BIN=/path/to/your/miniconda/bin     # optional — for conda subprocesses
```

The orchestrator then:

1. Probes `$RHEA_MCP_URL`. If reachable → `reused`, no spawn.
2. Runs `$RHEA_PYTHON_PATH/python -c "import rhea"` as a pre-spawn
   sanity check. A miniconda Python that doesn't have `rhea` installed
   fails this in ~2 s with an actionable message naming
   `$RHEA_REPO_PATH/.venv/bin` as the right value.
3. Composes Rhea's runtime env from the orchestrator's other backend
   specs (single source of truth — Rhea's `DATABASE_URL` matches the
   actual Postgres host:port the orchestrator manages; Rhea's
   `EMBEDDING_URL` matches Ollama; etc.). Without this composition,
   Rhea's defaults point at the wrong ports and silently-broken-but-
   probe-green is the result.
4. `Popen`s `$RHEA_PYTHON_PATH/python -m rhea.server.mcp_server
   --transport streamable-http` with `cwd=$RHEA_REPO_PATH` (required —
   Rhea reads its version from the repo's `pyproject.toml`) and
   `start_new_session=True` (so the SIGTERM teardown can group-kill
   uvicorn workers, not just the leader pid).
5. Polls `tools/list` until ready or the 60 s timeout fires.

Verified on 2026-05-15: the orchestrator brings Rhea up from a clean
state, the MUSCLE workflow runs end-to-end against the
orchestrator-spawned Rhea (5-sequence FASTA → MUSCLE alignment), and
`atexit` cleanly tears the process group down.

#### Bioconda / MUSCLE version pin (operator-side caveat)

Rhea's Galaxy MUSCLE tool is authored against MUSCLE **v3.8.1551**'s
command-line (`-fastaout`, `-cluster1`, `-maxiters`). Bioconda's
`muscle` package now ships v5 by default, which has a completely
different CLI; the tool fails with `Invalid command line / Unknown
option in`. If you build the muscle conda env fresh today, you'll
get v5 and the workflow will fail loudly via the `RheaFileToolStep`
FAIL-LOUD path (not silently). Mitigation on the conda env:

```bash
$RHEA_CONDA_BIN/conda install -n muscle -c bioconda --yes 'muscle=3.8.1551'
```

This is a Rhea-side / Galaxy-tool-definition concern; the
orchestrator faithfully drives whatever Rhea runs.

**Update (2026-05-15)** — the Rhea fork now refuses to keep a
major-version-mismatched env. `rhea/agent/utils.py::install_conda_env`
verifies after every conda create:

1. The requested package is actually present in the env's `bin/`
   (catches the `conda create -y` no-op + the conda-libmamba-solver
   metadata-only-install silent failure).
2. The installed MAJOR version matches the requested major version
   (catches the bioconda `>=` fallback silently installing v5 when
   the Galaxy XML asked for v3).

When either check fails, the env is torn down and `install_conda_env`
raises with a clear actionable message — the broken state never
reaches the Redis conda-pack cache, where it would have poisoned
every subsequent dispatch. The operator's response to the
major-version-skew error is the `conda install` recipe above.

**Update (2026-05-15, pre-warm)** — first user invocation no longer
triggers this code path on a healthy install. The orchestrator's
pre-warm phase (§2.2) calls `install_conda_env` at startup, so the
verification + self-heal chain runs in a context where errors land in
the `infrastructure_status` `actionable` array — not on a user
waiting on a `tools/call`. The MUSCLE 3.8.1551 strict pin is still
enforced inside Rhea; the pre-warm just front-loads it.

The orchestrator now also hands Rhea an explicit `CONDA_EXE`
(`$RHEA_CONDA_BIN/conda`) so its subprocess conda invocations don't
PATH-resolve through a broken system conda — the apecx-side
counterpart of the PATH-leakage guard in Rhea's parsl_config.

## 5. The `infrastructure_status` MCP tool

Returns a JSON dict the model renders back to the operator. Shape:

```jsonc
{
  "overall": "ready",
  "autostart_enabled": true,
  "orchestrator_uptime_seconds": 23.4,
  "start_all_completed": true,
  "backends": [
    {
      "name": "postgres",
      "display_name": "Postgres (apecx-rhea-postgres / pgvector)",
      "kind": "docker_container",
      "required": true,
      "state": "reused",
      "detail": "postgres OK on localhost:5435 (db=rhea, user=postgres)",
      "last_probe_at": 1778824361.46,
      "latency_ms": 27.4,
      "spawned_by_us": false,
      "tags": ["vector-store", "rhea-deps"]
    },
    // … 4 more entries: redis, minio, ollama, rhea_mcp
  ],
  "actionable": []
}
```

### Field meaning

- **`overall`** — one of `ready`, `starting`, `degraded`, `down`,
  `disabled`. `down` means at least one required backend is in a
  terminal-failure state (`error_starting` / `down`).
- **`autostart_enabled`** — the singleton's at-construction-time
  reading of `APECX_MCP_AUTOSTART_INFRA`. Once the singleton is
  constructed, this is fixed for the process lifetime.
- **`orchestrator_uptime_seconds`** — seconds since `start_all()` was
  first invoked.
- **`start_all_completed`** — `false` while bring-up is in flight;
  `true` after every backend has finished its initial probe → spawn
  → poll cycle.
- **`backends`** — per-backend state. `latency_ms` is the last probe's
  RTT. `last_probe_at` is the Unix timestamp of the last probe.
  `spawned_by_us` is `true` only when the orchestrator brought this
  backend up (in which case `atexit` will tear it down on MCP-server
  exit).
- **`actionable`** — a list of one-line strings per non-ready backend.
  These are the strings you should follow to fix things.

### Diagnosing a stuck startup from Claude Desktop

Ask Claude to call `infrastructure_status`. The tool's return tells
you which backends are down and what to do. Typical patterns:

- **"overall: starting"** for >30 seconds → a docker container is
  taking forever to come up. Check its state via
  `docker logs apecx-rhea-postgres` (or the other container name).
- **"overall: degraded"** → one or more backends were ready earlier
  but died. The `detail` field for each backend tells you why.
- **"overall: down"** → a required backend is in `error_starting`
  (autostart was attempted but failed). The actionable message tells
  you the remedy.
- **"overall: disabled"** → `APECX_MCP_AUTOSTART_INFRA=0` and
  `start_all` has not run yet. Status tool still reports what's
  reachable.

## 6. Connecting to Claude Desktop

Add or merge into `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS path; analogous on other OSes):

```json
{
  "mcpServers": {
    "apecx": {
      "command": "/path/to/apecx-mcp-integration/.venv/bin/apecx-mcp",
      "env": {
        "APECX_DATA_ROOT": "/path/to/apecx-data",
        "APECX_MCP_AUTOSTART_INFRA": "1",
        "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
        "APECX_LLM_MODEL": "mistral-nemo:latest",
        "RHEA_MCP_URL": "http://localhost:3001/mcp/",
        "RHEA_REPO_PATH": "/path/to/rhea",
        "RHEA_PYTHON_PATH": "/opt/miniconda3/envs/rhea/bin"
      }
    }
  }
}
```

`RHEA_REPO_PATH` + `RHEA_PYTHON_PATH` are optional — without them
the orchestrator marks `rhea_mcp` as `external_unconfigured` and the
operator runs the Rhea MCP server by hand.

After editing `claude_desktop_config.json`, fully quit and relaunch
Claude Desktop.

## 7. Degraded-mode story

When the orchestrator is in a non-ready state, Rhea-backed tools
(catalog tool, structural search, etc.) return `UNAVAILABLE` with a
reason rather than failing silently. The model can show the operator
the actionable remedy.

Recovery path:

1. Operator reads the actionable message in `infrastructure_status` —
   e.g. `"Rhea MCP is unreachable at http://localhost:3001/mcp/. To
   enable autostart, set $RHEA_REPO_PATH ..."`.
2. Operator sets the env var (or manually starts Rhea MCP).
3. Operator either restarts `apecx-mcp` (which re-runs `start_all`)
   or calls `infrastructure_status` again — the per-call re-probe
   will flip the backend back to `ready` or `reused` on the next
   call.

Note: a backend that has been brought down and back up by the operator
is not automatically re-spawned by the orchestrator's `start_all` —
that path runs once per `apecx-mcp` start. The per-call re-probe in
`infrastructure_status` does, however, catch the recovery.

## 8. Reference

- Source: `src/apecx_integration/infrastructure/`
  - `backends.py` — dataclasses + state enum
  - `containers.py` — shared Docker container specs (also used by
    `apecx-setup`)
  - `probes.py` — per-backend health probes
  - `orchestrator.py` — `InfraOrchestrator` + singleton accessor +
    background-thread launcher
- MCP tool: `src/apecx_integration/mcp_surface/tools/infrastructure_status.py`
- Tests: `tests/unit/test_infrastructure_orchestrator.py`,
  `tests/integration/test_infrastructure_orchestrator_live.py`

## 9. Why this isn't a nanobrain workflow component

The orchestrator is operational plumbing — startup-time bring-up plus
runtime status reporting. Forcing it through `from_config` +
`Workflow.from_config(...)` + DataUnit/Trigger/Link wiring would add
ceremony without buying anything: the orchestrator has no event-driven
data flow, no per-step business logic, no LLM dispatch. It is one
async function (`start_all`) and one snapshot function (`status`),
guarded by a `threading.Lock` so cross-loop status reads are safe.

This is consistent with how `_verify_control_plane_reachable` and
`_ensure_synonym_dict_or_warn` are written today (also in `server.py`)
— they're operational plumbing, not nanobrain components.
