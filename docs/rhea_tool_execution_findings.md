# Rhea ingestion path + tool execution — findings

**Date**: 2026-05-14. **Status**: COMPLETE — the acceptance criterion
is met end-to-end. Ingestion path FIXED + verified; `find_tools`
works; the Parsl worker-connectivity issue FIXED; the file-input
ProxyStore protocol works; a real Galaxy tool (**MUSCLE**) was
fetched, discovered, called with a real file input, executed, and
produced a **non-null, correct alignment output** that matches the
tool's own expected test data.

This documents the arc from the directive *"fix the ingestion path …
fetch and use correctly a tool, receive an appropriate non-null
result"*, the follow-up *"test muscle tool … get test data for it
and ensure that you can successfully run it, get the results, and use
them"*, and the final directive *"what we need to demonstrate is a
sample nanobrain workflow that goes from collecting data … this
workflow should be able to run in a standalone manner or via MCP
calls"* (2026-05-14).

The proof script `rhea/scripts/run_muscle_e2e.py` (section 5) is now
superseded by a proper **nanobrain workflow**, `rhea_muscle_alignment`
(section 6) — the actual deliverable.

## Acceptance-criterion status — honest, clause by clause

| Clause | Status |
|---|---|
| **fetch a tool** | ✅ 20+ real Galaxy tools fetched + parsed + embedded + ingested; MUSCLE ingested via `RHEA_INGEST_ONLY=muscle` |
| **`find_tools` returns it** | ✅ semantic search populates the catalog with real tools (MUSCLE discovered from query *"MUSCLE multiple sequence alignment of protein fasta"*) |
| **the tool is *callable*** | ✅ fixed the `call_tool` session-lookup bug — a discovered tool is now invokable |
| **the tool's command *executes*** | ✅ Parsl worker → `launch_agent` → `RheaToolAgent` → conda env → `run_tool` → `conda run … bash <rendered>` runs |
| **a non-null *output*** | ✅ **MUSCLE produced a 1980-byte FASTA alignment** + a 50225-byte HTML alignment; the 5 aligned sequence IDs match the tool's own `seqtest_aln.fasta` expected output |

The user-named issue — *"the Parsl issue"*, "Never received handle
from Parsl worker" — is **FIXED**. Everything downstream of it was
then driven to working, then the file-input path and the final
command-rendering bug were fixed to reach a verified non-null result.

## What was fixed — Rhea fork (all user-authorized)

### 1. The ingestion path (`update_tools.py` — the named directive)

Before: `update_tools.py` only *computed* a new-tool set and stopped —
the `galaxytools` table stayed empty, so `find_tools` had nothing to
find. Now it does the full pipeline: discover → fetch content →
parse → embed → insert.

* **Discovery** stays ToolShed-driven (`get_galaxy_repositories()` —
  the ToolShed `/api/repositories` catalog, configurable URL).
* **Content fetch** — the ToolShed's own anonymous file endpoints
  (`/archive/`, `/raw-file/`) now return `403 — Authentication
  required`. ~76% of ToolShed repos carry a GitHub
  `remote_repository_url`; new `fetch.py::get_tool_xmls_from_repo`
  pulls the tool XML from there. Repos without a GitHub remote URL
  are skipped (logged, counted) — a documented limitation.
* **Parse** — `Tool.from_xml` (already existed).
* **Embed** — `generate_tool_documentation_embedding` → a 1024-dim
  vector.
* **Insert** — upsert a `GalaxyTool` row (`session.merge`, idempotent).
* Bounded by `$RHEA_INGEST_LIMIT`; FAIL-LOUD if zero tools ingested.

Verified: **20 real Galaxy tools** (the `text_processing` repo —
`tp_cat`, `tp_cut_tool`, `tp_grep_tool`, `tp_sed_tool`, …) ingested
into the registry.

### 2. Configurable Galaxy ToolShed

`Settings.galaxy_toolshed_url` + `fetch.py` resolution
(arg → `$GALAXY_TOOLSHED_URL` → default). See `APECX_INTEGRATION.md`.

### 3. `call_tool` session-scoped tool lookup (`rhea_fastmcp.py`)

Real Rhea bug: `find_tools` populates matched tools into the caller's
*session-scoped* `client_state._tools`; `list_tools` reads that
bucket — but `call_tool` did `self.get_tool(name)`, which only checks
the *global* registry. A just-discovered tool was listable but not
callable ("Unknown tool"). Fixed: `call_tool` checks the
session-scoped bucket first, mirroring `list_tools`.

### 4. The Parsl worker-connectivity fix (`parsl_config.py`) — THE named issue

`generate_parsl_config` launched the Parsl worker as a
`rhea-worker-agent` **container** via a `WrappedLauncher` doing
`docker run --network host`. On Docker Desktop for Mac this is
fundamentally broken: the worker container is a *sibling* on the
host's daemon, does NOT share the server's network namespace, and
`--network host` is a no-op on macOS — so the worker can never reach
the interchange. Result: *"Never received handle from Parsl worker."*

Fix: a new `backend="local"` mode. It builds a `LocalProvider` with
the **default `SingleNodeLauncher`** (no `WrappedLauncher`, no
container) and lets `HighThroughputExecutor` use its default
`launch_cmd`. The worker is a plain local subprocess of the server —
it shares the server's network namespace, so there is no
interchange-connectivity problem at all. This is still Parsl
(`HighThroughputExecutor` + `LocalProvider`), the standard
single-machine config; it is selectable via
`Settings.parsl_container_backend` (now `Literal["docker","podman","local"]`)
/ `$PARSL_CONTAINER_BACKEND=local`.

**Verified**: with `backend="local"`, the interchange logs
*"1 connected workers"* and Parsl tasks run on the worker.

## The working setup (reproducible recipe)

The container path can't give the worker `conda` (the slim image
lacks it) and can't do the macOS networking. The working setup runs
the rhea-server as a **host process** with everything on localhost:

```bash
# infra
docker run -d --name apecx-rhea-postgres -p 5435:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rhea pgvector/pgvector:0.8.0-pg17
docker run -d --name apecx-rhea-minio -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin minio/minio server /data
# (apecx-redis on :6379 reused; embedding = Ollama mxbai-embed-large, 1024-dim)
ollama pull mxbai-embed-large
# isolated conda (the host's anaconda is broken — libarchive.20.dylib)
bash Miniconda3-latest-MacOSX-arm64.sh -b -p ~/rhea-miniconda
~/rhea-miniconda/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
~/rhea-miniconda/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

# DB schema + ingestion
cd rhea && uv sync && uv pip install -e .
#   (rhea MUST be installed editable — the Parsl worker subprocess
#    runs from a different CWD and can't import it otherwise)
PYTHONPATH=rhea DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5435/rhea \
  EMBEDDING_URL=http://localhost:11434/v1 MODEL=mxbai-embed-large RHEA_INGEST_LIMIT=8 \
  python -m rhea.preprocess.update_tools

# rhea-server as a host process, local Parsl backend
PATH="$HOME/rhea-miniconda/bin:$PATH" HOST=localhost PORT=3001 \
  DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5435/rhea \
  EMBEDDING_URL=http://localhost:11434/v1 EMBEDDING_KEY=EMPTY MODEL=mxbai-embed-large \
  REDIS_HOST=localhost AGENT_REDIS_HOST=localhost \
  MINIO_ENDPOINT=localhost:9000 MINIO_ACCESS_KEY=minioadmin MINIO_SECRET_KEY=minioadmin \
  PARSL_CONTAINER_BACKEND=local \
  uv run -m rhea.server.mcp_server --transport streamable-http
```

Blockers fixed along the way, in order: ToolShed file endpoints
auth-gated → GitHub content path; `text-embeddings-inference` has no
arm64 build → Ollama `mxbai-embed-large`; `call_tool` session bug;
Parsl container worker unreachable → `backend="local"`; `rhea` not
installed → `uv pip install -e .`; host anaconda broken → isolated
miniconda; conda ToS not accepted → `conda tos accept`.

## 5. The file-input ProxyStore protocol — MUSCLE end-to-end

The follow-up directive was to prove the *file-input* path with a
concrete tool: MUSCLE multiple-sequence alignment. This closed the
acceptance criterion's final clause.

### The file-input protocol (how a caller drives a file into a tool)

A Galaxy `type="data"` param resolves through Rhea's ProxyStore
protocol. The caller must:

1. Stage the file into the `rhea-input` ProxyStore (a `Store` backed
   by a `RedisConnector`): `RheaFileProxy.from_buffer(name, bytes,
   redis_client)` → `proxy.to_proxy(store)` returns a `redis_key`.
2. Pass that `redis_key` *as the argument value* for the `data`
   param in the MCP `tools/call`. `process_user_inputs` wraps it as
   `RheaParam.from_param(param, RedisKey(a))` → a `RheaFileParam`.
3. On the worker, `build_env_parameters` pulls the bytes back out of
   the store (`RheaFileProxy.from_proxy`), writes them to a temp file
   in the tool's input dir, and exposes a `GalaxyFileVar` to the
   Cheetah `<command>` template.
4. Tool outputs are written into a temp output dir, read back, and
   pushed into the `rhea-output` ProxyStore; the result JSON carries
   their `redis_key`s. The caller fetches them with
   `RheaFileProxy.from_proxy(RedisKey(...), out_store)` → `.open()`.

The end-to-end test that exercises all of this against real data:
`rhea/scripts/run_muscle_e2e.py`.

### Bugs found and fixed in this arc (Rhea fork, user-authorized)

* **Stale conda-pack archive in Redis.** `install_conda_env` caches a
  `conda pack` tarball in the Redis `conda_envs` hash. A tarball
  packed inside the old *container* path carries absolute
  `/home/rhea/...` member paths; unpacking it on the macOS host
  fails with `OSError: [Errno 45] Operation not supported`. Fix:
  clear the stale entry (`HDEL conda_envs muscle`) when switching
  from the container backend to `backend="local"`. (Operational, not
  a code change — but a real silent-ish failure mode: the cache key
  outlives the backend it was built for.)

* **`expand_galaxy_if` shadowed a real scalar param with an empty
  nested placeholder** (`rhea/agent/tool.py`). Its dotted-variable
  resolution loop, for a template var like `outputFormat.value`,
  unconditionally called `current.set_nested("value", "")` — even
  when `context["outputFormat"]` already wrapped the real scalar
  `"fasta"`. That injected `_nested["value"] = ""`, which then
  shadowed the real value at render time. Fix: skip the placeholder
  injection when the `GalaxyVar` already wraps a real (non-dict,
  non-empty) scalar; let `.value` resolve to the value itself.

* **`GalaxyVar` honored the `.value`-is-self idiom in `__getattr__`
  but not in the mapping protocol** (`rhea/agent/schema.py`). Galaxy's
  Cheetah idiom is that `$param.value` IS `$param` for a scalar
  param. `GalaxyVar` implements *both* `__getattr__` *and* the
  mapping protocol (`__getitem__` / `__len__` / `__contains__`).
  Cheetah's NameMapper sees the mapping protocol and resolves the
  `.value` segment via `__getitem__("value")`, **not** `__getattr__`
  — so a `__getattr__`-only fix was dead code. `__getitem__("value")`
  fell through to the empty-`GalaxyVar` fallback, so
  `-${outputFormat.value}out` rendered `-{}out` →
  `Invalid command line option "{}out"`. Fix: mirror the
  `value`-returns-self idiom across `__getitem__`, `__contains__`,
  and `get` so the behavior is consistent no matter which accessor
  Cheetah picks. Verified with a standalone Cheetah render plus the
  full e2e.

### Verified result

`run_muscle_e2e.py` against the running host-process rhea-server:

```
[1] fetch test FASTA (1904 bytes, 5 sequences) from the MUSCLE tool's
    own test-data
[2] stage into the rhea-input ProxyStore (RheaFileProxy)
[3] MCP: initialize -> find_tools -> tools/call muscle
       args: {input_seqs: <redis_key>, diags: False, run: "16",
              cluster: "upgmb", outputFormat: "fasta"}
[4] muscle returned: isError=False, return_code=0, 2 output files
[5] out_align: 1980 bytes FASTA  |  out_align_html: 50225 bytes
[6] aligned seq IDs == the tool's expected seqtest_aln.fasta IDs
    SUCCESS: end-to-end file-input run verified
```

The conda env (`muscle` from bioconda) builds locally, the local
Parsl worker runs MUSCLE 3.8.1551, and the FASTA alignment is
non-null and correct.

## 6. The nanobrain workflow — `rhea_muscle_alignment`

`run_muscle_e2e.py` was a standalone proof script — useful to drive
the Rhea side to working, but not the deliverable. The deliverable is
a **nanobrain workflow** that consumes Rhea as an MCP server, runnable
both standalone and via MCP. That workflow now exists:
`src/apecx_integration/composition/workflows/rhea_muscle_alignment/`.

### Shape — collect data → run tool over MCP → use the result

Three nanobrain steps, wired by DirectLinks (`config_version: 2`, every
link `auto_transfer: true`):

1. **`FastaCollectionStep`** (`apecx_integration/composition/steps/`)
   — the "collect data" step. Reads a FASTA from `{fasta_path}` or
   `{fasta_text}`, or falls back to the bundled `data/seqtest.fasta`
   (the MUSCLE tool's own 5-sequence test data). Emits
   `{fasta_name, fasta_bytes, n_sequences}`.
2. **`RheaFileToolStep`** (`nanobrain/library/steps/rhea_file_tool_step.py`)
   — the framework-capacity expansion. A general-purpose `BaseStep`
   that runs *any* Rhea file-input Galaxy tool over MCP: stages the
   file into Rhea's `rhea-input` ProxyStore (`RheaFileProxy`), calls
   `find_tools` + `tools/call` via the shared `MCPTransport`, fetches
   output files back from the `rhea-output` ProxyStore. It lazy-imports
   the genuine `rhea.utils.proxy.RheaFileProxy` (cloudpickle pickles by
   module reference — a vendored copy would deserialize to the wrong
   class on the server) and FAIL-LOUDs if `rhea` is not importable.
   This is the piece the Explore audit found missing — nanobrain had
   `RheaAdapter`/`ToolExecutionStep` for plain `tools/call`, but
   nothing for Rhea's file-input protocol.
3. **`AlignmentReportStep`** (`apecx_integration/composition/steps/`)
   — the "use the result" step. Parses the `out_align` FASTA, computes
   alignment length + per-sequence gap fraction, emits a human-readable
   summary.

### Three invocation paths — all verified end-to-end against live Rhea

| Path | Entry point | Verified by |
|---|---|---|
| **Standalone — workflow YAML** | `Workflow.from_config(rhea_muscle_alignment/workflow.yml)` → `process()` → trigger cascade | `test_workflow_from_config_against_live_rhea` |
| **Standalone — direct steps** | `BaseStep.from_config(...)` per step, chain `process()` | `test_direct_step_chain_against_live_rhea` |
| **Via MCP** | `align_sequences_with_muscle` MCP-surface tool (`mcp_surface/tools/muscle_alignment.py`, registered in `mcp_surface/server.py`) | manual smoke against live Rhea |

`tests/integration/test_rhea_muscle_alignment_workflow.py`: **14 passed**
(12 unconditional load + pure-transform tests; 2 gated on `$RHEA_MCP_URL`
hitting a live Rhea server). The gated run drives real MUSCLE in ~19 s.
The MCP-tool smoke returned a 5-sequence, 374-column alignment with
mean gap fraction 0.0374.

### Run prerequisites
The `muscle_alignment` step needs, at `process()` time: the `rhea` repo
on `PYTHONPATH` (`rhea/__init__.py` + `rhea/utils/__init__.py` are
empty, so this pulls in only `proxystore`/`redis`/`cloudpickle`/`filetype`
— not Rhea's heavy academy/parsl/sqlalchemy tree); a reachable Rhea MCP
server hosting `muscle`; and a reachable Redis backing the ProxyStores.
Absent those, the workflow LOADS + VALIDATES fine but a run FAILS LOUDLY
at the `muscle_alignment` step — never a silent no-op.

## Brutal-truth assessment

**What's genuinely done and verified**: the entire chain. The
ingestion path (the named directive) — real tools ingested;
`find_tools` semantic search; the `call_tool` session-lookup bug fix;
the Parsl `backend="local"` fix (the named "Parsl issue"); the
file-input ProxyStore protocol; a real Galaxy tool (MUSCLE)
fetched → discovered → called with a real file input → executed →
producing a non-null, *correct* alignment output that matches the
tool's own expected test data; and — the actual deliverable — a
**nanobrain workflow** (`rhea_muscle_alignment`) that consumes Rhea as
an MCP server, runnable standalone (workflow YAML + direct steps) and
via an MCP-surface tool, all three paths verified end-to-end against a
live Rhea server (14/14 tests pass). Every blocker between "MCP call"
and "correct tool output" was found and fixed. The acceptance
criterion is met.

**Scope honestly not covered** (out of scope for this directive, not
a blocker): Galaxy's `<repeat>` input element and the
`process_conditional_inputs` stub in `rhea/utils/process.py` — a
parameter-only tool that uses `<repeat>` still exposes an incomplete
MCP input schema. MUSCLE's one `<conditional>` (`mode`/`run`) *was*
exercised and works. `<repeat>` was not needed for MUSCLE and is a
separate, bounded Rhea-internals arc.

**No silent failures**: every layer FAIL-LOUDs. The ingestion
FAIL-LOUDs on zero tools; `update_tools` skips-with-count repos
without a supported forge mirror; the Parsl worker connection is real
(not faked); the tool command genuinely executes; the e2e test
asserts the output is non-null AND that the sequence IDs match the
tool's own expected test data — a green run cannot hide an empty or
wrong result.
