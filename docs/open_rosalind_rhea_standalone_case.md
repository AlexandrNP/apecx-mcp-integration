# Open-Rosalind → Rhea standalone case

**Date**: 2026-05-14.
**Status**: framework components built + tested + **landed in nanobrain
proper**; end-to-end RUN gated on a Rhea worker hosting Open-Rosalind's
tools.

> **2026-05-14 update — nanobrain promotion.** The user authorized
> nanobrain-repo modifications ("Use nanobrain's components whenever
> possible. You are free to make modifications to the nanobrain
> repository too"). The three Rhea components that were first built
> apecx-side as workarounds now live in nanobrain proper:
> `RheaAdapter` + `RheaMCPDiscovery` → `nanobrain/library/tools/`,
> sharing a new `MCPTransport` wire-protocol helper with the
> pre-existing `RheaMCPDispatcher`. The `RheaToolStep` subclass is
> **retired** — `nanobrain`'s `ToolExecutionStep` now self-unwraps the
> trigger envelope (UTD-aware discriminator), so the framework step is
> used directly. See `nanobrain/CLAUDE.md` for the framework-side log.

This document covers the standalone Open-Rosalind case: a nanobrain
workflow-generation surface that consumes **Rhea as an MCP server**.
It supersedes the codegen-adapted Open-Rosalind subset (which remains
in the catalog as a secondary path — see "Two Open-Rosalind paths"
below).

## Why a standalone case (the honest framing)

Open-Rosalind (github.com/maris205/open-rosalind) is natively a
**tool-first bio-agent benchmark** — its problems are answered by
invoking registered bioinformatics tools (`uniprot.search`,
`pubmed`, `alphafold`, `sequence.analyze`) and producing
evidence-grounded traces. It is NOT a code-generation benchmark.

The earlier integration (`tests/benchmarks/datasets/open_rosalind.py`)
forced the `sequence_basic` subset into the codegen surface — a
documented stretch (only 8 of 49 problems are pure-computation; the
F37-F39 findings showed the "smart" scaffolds collapsing on it).

The user's directive — *"Open-Rosalind should be a standalone case
for workflow generation with the use of Rhea ... Rhea should be
utilized by the code generator as an MCP server"* — is the HONEST
shape. Open-Rosalind problems become **workflow-generation tasks**:
given a bio query, generate a nanobrain workflow that dispatches the
right tool(s) via Rhea.

## Architecture

```
                       generation time
  Open-Rosalind  ──►  rhea_workflow codegen
     problem            │
                        ├─► RheaMCPDiscovery.discover()  ──MCP tools/list──►  Rhea worker
                        │      (codegen IS an MCP client of Rhea)
                        │      returns: UTD dicts for Rhea's tool catalog
                        │
                        └─► GENERATE a workflow:
                               workflow_input
                                 └─► ToolExecutionStep (UTD = discovered tool)
                                       └─► workflow_output

                       run time
  generated workflow ──► ToolExecutionStep.process()
                            └─► RheaAdapter.invoke()  ──MCP tools/call──►  Rhea worker
                                  (BACKEND_NAME="rhea")              returns: tool result
```

Components, all framework-native — the Rhea plumbing now lives in
nanobrain proper:

| Component | File | Tests |
|---|---|---|
| `MCPTransport` — shared MCP streamable-HTTP wire helper | `nanobrain/library/tools/_mcp_transport.py` | (exercised via the 3 components below) |
| `RheaAdapter` — `ToolBackendAdapter(BACKEND_NAME="rhea")` | `nanobrain/library/tools/rhea_adapter.py` | `nanobrain/tests/unit/test_rhea_adapter.py` (13) |
| `RheaMCPDiscovery` — codegen-as-MCP-client | `nanobrain/library/tools/rhea_discovery.py` | `nanobrain/tests/unit/test_rhea_discovery.py` (10) |
| `ToolExecutionStep` — now self-unwraps the trigger envelope | `nanobrain/library/steps/tool_execution_step.py` | `nanobrain/tests/unit/test_tool_execution_step.py` (25, of which 5 cover the unwrap) |
| `rhea_workflow` codegen | `tests/benchmarks/codegen/rhea_workflow.py` | factory-gated test |
| Standalone workflow (YAML + lightweight builder) | `composition/workflows/open_rosalind_rhea/` + `open_rosalind_rhea_lightweight_builder.py` | `tests/integration/test_open_rosalind_rhea_workflow.py` (4 + 1 gated) |

Test totals: **59 nanobrain-side** (`test_rhea_adapter` 13 +
`test_rhea_discovery` 10 + `test_tool_execution_step` 25 +
`test_rhea_mcp_dispatcher` 11, 1 gated) + **4 apecx-side** integration
tests pass; 1 gated test skips without a live Rhea worker. Full
nanobrain unit suite: 993 passed, 0 regressions.

## Framework-capacity expansions (landed in nanobrain proper)

The user said "expand framework capacity if required" and later
authorized nanobrain-repo modifications directly. Three real gaps
were filled, all in `nanobrain/`:

### 1. `RheaAdapter` — the missing `ToolBackendAdapter`

An earlier `CLAUDE.md` claimed the Rhea adapter "ships from the Rhea
fork (Track C T-RH-04)" — **it never existed**. `nanobrain` shipped
`HTTPBackendAdapter` and `LocalParslAdapter` but no `rhea` backend.
Without it, `ToolExecutionStep` cannot dispatch to Rhea at all.
`RheaAdapter` now lives at `nanobrain/library/tools/rhea_adapter.py`
alongside the other `ToolBackendAdapter`s — its canonical home. It
speaks the MCP streamable-HTTP wire protocol via the shared
`MCPTransport`, shaped as a `ToolBackendAdapter` for the
`ToolExecutionStep` (G11) path (vs. `RheaMCPDispatcher`, the `ToolBase`
for the Agent path).

### 2. `MCPTransport` — shared MCP wire-protocol helper

The MCP streamable-HTTP wire logic (initialize handshake →
`tools/call` / `tools/list` → SSE parse → `mcp-session-id` lifecycle
with one-shot re-init) was ~90 duplicated lines across
`RheaMCPDispatcher`, `RheaAdapter`, and `RheaMCPDiscovery`. It is now
a single `MCPTransport` class at
`nanobrain/library/tools/_mcp_transport.py`; all three components
delegate to it. A protocol bug fix lands once.

### 3. `ToolExecutionStep` self-unwraps the trigger envelope

`ToolExecutionStep` was designed for DIRECT `process(utd_inputs)`
calls — `nanobrain`'s own tests only ever drove it that way. Inside a
workflow cascade, the trigger system delivers `{<input_du>: payload}`;
the step did NOT unwrap that envelope, so the adapter received
`{sequence_tool_input: {sequence: ...}}` instead of `{sequence: ...}`.
The fix: `ToolExecutionStep.process()` now self-unwraps, using a
**UTD-aware discriminator** — a single-key dict whose key is NOT a
declared UTD input name (and whose value is a dict) is the trigger
envelope; the key is the input data unit's name. A single-key dict
whose key DOES match a declared UTD input is a genuine 1-input call
and passes through untouched (this resolves the one ambiguous case
the earlier apecx-side `RheaToolStep` heuristic could not). The
`RheaToolStep` subclass is **retired** — the framework step is now
used directly everywhere.

## RUN prerequisites + the honest blocker

The components are built + tested. The end-to-end RUN is **gated**
on infrastructure that is NOT in this workspace:

1. **A Rhea worker running** at `$RHEA_MCP_URL`. None is up
   (localhost:3001 refused at integration time). Bringing one up is
   a Docker + Redis stack (see `nanobrain/CLAUDE.md`'s Rhea notes).

2. **The Rhea worker must host Open-Rosalind's bio tools.**
   Open-Rosalind ships 30 tool modules as Python (`uniprot.py`,
   `sequence.py`, `alphafold.py`, ...). They are NOT Rhea tools.
   Registering them WITH a Rhea worker is a **`rhea/`-side task** —
   `rhea/` is read-mostly scope for this workspace, so this step is
   explicitly out of scope here. It needs either: the rhea team to
   register OR's tools, OR the user to authorize `rhea/` edits, OR a
   pre-built Rhea image with the bio tools.

3. `RheaAdapter.from_env()` registered before the workflow runs
   (reads `$RHEA_MCP_URL`).

**What works WITHOUT a Rhea worker** (verified by the test suites):
- The workflow YAML + lightweight builder LOAD + validate.
- The fake-adapter cascade test drives the workflow end-to-end with a
  stubbed `rhea` backend — proving the topology + `ToolExecutionStep`
  envelope unwrap + adapter dispatch path.
- `RheaAdapter` + `RheaMCPDiscovery` are unit-tested against a fake
  MCP server (httpx `MockTransport`), nanobrain-side.
- All gates FAIL LOUD: `$RHEA_MCP_URL` unset → loud error; empty Rhea
  tool catalog → loud error; tool-match miss → loud error. No silent
  no-ops.

**What is gated** (1 skipped test, `test_or_rhea_workflow_against_live_rhea`):
- The end-to-end run against a real Rhea worker. Skips with a loud
  reason when `$RHEA_MCP_URL` is unset.

## Two Open-Rosalind paths in the repo

| Path | Surface | Status |
|---|---|---|
| **Standalone Rhea case** (THIS doc) | workflow generation with Rhea as MCP server | components built + tested; run gated on a Rhea worker |
| Codegen-adapted subset (`datasets/open_rosalind.py`) | the `sequence_basic` 8-problem pure-computation subset on the codegen sweep surface | RAN — see `findings_biology_benchmarks.md` F37-F39 |

The codegen-adapted subset is KEPT (it produced real findings,
notably F39 — the closed memory loop amplifying errors) but is the
SECONDARY Open-Rosalind path. The standalone Rhea case is the
PRIMARY, honest framing.

## How to run once Rhea is up

```bash
# 1. Bring up a Rhea worker hosting Open-Rosalind's bio tools
#    (rhea/-side task — out of this workspace's writable scope).
# 2. Point at it:
export RHEA_MCP_URL="http://localhost:3001/mcp/"
# 3. Run the standalone workflow directly:
PYTHONPATH=src .venv/bin/python -m pytest \
    tests/integration/test_open_rosalind_rhea_workflow.py -v
#    (the gated test now runs instead of skipping)
# 4. OR sweep the rhea_workflow codegen on Open-Rosalind:
PYTHONPATH=src .venv/bin/python -m tests.benchmarks.cli open_rosalind \
    --codegen rhea_workflow --split v0 \
    --output _benchmark_runs/open_rosalind_rhea/run1.json
```

## Brutal-truth assessment

**What's genuinely done**: the framework-native plumbing for
"nanobrain workflow generation that uses Rhea as an MCP server" — the
adapter, the discovery client, the shared `MCPTransport`, the
cascade-safe `ToolExecutionStep` (self-unwraps; no subclass needed),
the workflow (3 construction paths), the codegen, the test suites
(59 nanobrain-side + 4 apecx-side), the LLM-guidance file. All Rhea
plumbing landed in nanobrain proper.

**What's honestly blocked**: the end-to-end run. It needs a Rhea
worker hosting OR's tools. Registering OR's 30 Python tool modules as
Rhea tools is a `rhea/`-side integration that this workspace's scope
rules put out of bounds. I did NOT fake it — there is no mock Rhea
worker pretending to be real, no fabricated tool results. The
fake-adapter test is clearly labelled as a topology test, not an
end-to-end measurement.

**The adoption-reliability win**: every code path fails loud. A
Rhea-backed workflow run with `$RHEA_MCP_URL` unset, or against a
Rhea worker missing the expected tools, produces a clear error — not
a green run with empty answers. That is the silent-failure shape the
workspace policy exists to prevent, and it is closed here at the
adapter, the discovery client, the codegen factory, and the step.
