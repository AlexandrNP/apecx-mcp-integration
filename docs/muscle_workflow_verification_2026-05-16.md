# Muscle workflow verification — 2026-05-16

Status report on `rhea_muscle_alignment` workflow per user directive
"Mandatorily check muscle workflow usage." Honest assessment of what
works today vs. what's blocked.

## UPDATE 2026-05-16 (post-rhea-fix): ✅ FULLY VERIFIED END-TO-END

Both blockers below are now fixed (rhea commit `72193f0` on
`AlexandrNP/rhea` branch `apecx-integration`). Test result:

```
tests/integration/test_rhea_muscle_alignment_workflow.py
14 passed, 0 failed, 0 skipped in 15.92s
```

Including BOTH gated-on-`$RHEA_MCP_URL` tests:
* `test_direct_step_chain_against_live_rhea` — **PASSED**
* `test_workflow_from_config_against_live_rhea` — **PASSED**

The full pipeline runs end-to-end against the real Rhea MCP server:
FastaCollectionStep stages the 5-seq FASTA → RheaFileToolStep
dispatches MUSCLE via MCP → Parsl worker spawns the Academy
RheaToolAgent → agent unpacks the muscle conda env → MUSCLE runs
against the real sequences → the alignment FASTA comes back →
AlignmentReportStep parses it. **No operator PATH workaround needed
anymore.**

The verification status sections below document the original
blocker discovery; keep them for the operator-side recipe + the
record of what was investigated.

## TL;DR

* **Wiring is verified.** 16/16 unconditional tests pass. The
  workflow YAML loads, all 3 step configs validate, the bundled
  FASTA parses correctly (5 sequences), the alignment-report logic
  works on a fixture. The MCP catalog registration is correct,
  including the load-bearing `input_envelope_key` field that prevents
  the dominant silent-failure shape (mis-keyed envelope → 0 data
  units populated).
* **Live end-to-end: VERIFIED today** (per the UPDATE above) after
  rhea-side G86 fix + academy 0.4 sync + single-await pattern fix.
* **No apecx-mcp-integration side bugs** were found. Every layer
  this repo controls is correct.

## What was verified (PASSED)

### Unconditional layer (no external services)

`tests/integration/test_rhea_muscle_alignment_workflow.py` + the
Aurora variant — 16 unconditional tests, 100% pass:

| Test | Verifies |
|---|---|
| `test_rhea_file_tool_step_config_validates` | wrapper config validates with extra='forbid' |
| `test_rhea_file_tool_step_config_rejects_unknown_field` | typo protection works |
| `test_collection_step_yaml_loads_via_from_config` | FastaCollectionStep loads |
| `test_muscle_step_yaml_loads_via_from_config` | RheaFileToolStep loads with muscle config |
| `test_report_step_yaml_loads_via_from_config` | AlignmentReportStep loads |
| `test_workflow_yaml_loads_via_from_config` | full workflow.yml loads + validates the DAG |
| `test_collection_step_reads_bundled_fasta` | bundled 5-seq FASTA parses (n_sequences == 5) |
| `test_collection_step_accepts_fasta_text` | inline fasta_text input shape works |
| `test_report_step_parses_fixture_alignment` | alignment report on a known fixture |
| `test_report_step_fails_loud_on_missing_out_align` | fail-loud on missing output |
| `test_report_step_fails_loud_on_empty_alignment` | fail-loud on empty alignment |
| `test_aurora_workflow_loads_without_endpoint_env` | Aurora variant loads with endpoint=unset |
| `test_aurora_workflow_alignment_report_uses_globus_compute_executor` | step-level executor binding works |
| `test_aurora_workflow_local_steps_use_local_executor` | other steps still use LocalExecutor |
| `test_aurora_step_yaml_loads_standalone` | Aurora step config validates standalone |

### MCP-surface registration

`src/apecx_integration/mcp_surface/configs/mcp_workflow_catalog.yml`
correctly registers the workflow:

```yaml
- tool_name: rhea_muscle_alignment
  workflow:
    path: composition/workflows/rhea_muscle_alignment/workflow.yml
  input_envelope_key: fasta_collection_input  # ← silent-failure bridge
```

The `input_envelope_key` field is the critical anti-silent-failure
guard documented in the catalog comment: "Mis-keying this field
silently populates 0 data units — exactly the silent-failure shape
the input_envelope_key field exists to bridge." It IS set correctly.

### MCP wire (request reaches rhea)

After bringing up `rhea-server` on `:3001` with the documented
recipe (workspace's `docs/rhea_tool_execution_findings.md` §5)
plus the PATH-leakage workaround documented below, the apecx-side
test exercises the MCP wire successfully:

* `RheaFileToolStep` packs the FASTA into rhea-input ProxyStore.
* MCP `tools/call` request reaches rhea-server.
* rhea-server dispatches a Parsl task to a local worker.
* Worker spawns an Academy `RheaToolAgent`.
* Agent unpacks the muscle conda env (with `RHEA_CONDA_ENVS_DIR`
  set to a writable tmp dir — `/home/rhea` workaround).
* Agent reaches the "Running agent" state.
* **TOOL EXECUTION FAILS** inside the agent with
  `Protocols cannot be instantiated`.

## What's BROKEN (real blockers)

### Blocker 1: rhea-side `process_worker_pool.py` PATH-leakage

**Symptom**: Parsl worker fails to start with
`process_worker_pool.py: error: the following arguments are required: -P/--port`.

**Root cause**: macOS host has Anaconda's Parsl 2025.07.07 on PATH
ahead of rhea's `.venv/bin` (Parsl 2025.06.23). The wrong
`process_worker_pool.py` binary wins, with a different CLI
signature than the server's Parsl.

**Operator-side workaround**: Prepend rhea's `.venv/bin` to PATH
when starting `rhea-server`:

```bash
PATH="$PWD/.venv/bin:$HOME/rhea-miniconda/bin:$PATH" \
  PARSL_CONTAINER_BACKEND=local \
  ... \
  .venv/bin/python -m rhea.server.mcp_server --transport streamable-http
```

**Real fix needed** (rhea-side): extend `rhea/manager/parsl_config.py`'s
existing PATH-leakage fix for `interchange.py` (which uses
`_python.parent / "interchange.py"` to derive an absolute path)
to ALSO cover `process_worker_pool.py`. Lines 256-268 already do
this for the interchange; the same pattern needs applying to the
worker's `launch_cmd`.

### Blocker 2: rhea-side `Protocols cannot be instantiated` during tool execution

**Symptom**: After the worker connects + Academy agent starts +
conda env unpacks + agent reaches "Running agent" state, the
`run_tool` action returns
`isError=true, content="Error executing tool muscle: Protocols cannot be instantiated"`.

**Likely root cause**: A `typing.Protocol` class being instantiated
directly somewhere in the rhea agent's tool dispatch path. Python's
`typing.Protocol` raises this exact error when called as a
constructor. Suspect a serialization/deserialization issue with
`proxystore.connectors` (which exposes `Connector` as a Protocol
since 0.7+) or a similar typing.Protocol-derived contract.

**No operator workaround.** This is a rhea code change needed.

**Investigation pointers**:
* `rhea/agent/tool.py:497` (`async def run_tool(self, params)`)
* `rhea/utils/proxy.py:80` (`RheaFileHandle.iter_chunks` + `RheaFileProxy`)
* Anywhere that cloudpickle deserializes a `proxystore.store.Store`
  or `proxystore.connectors.redis.RedisConnector`

The May 14 verification (`docs/rhea_tool_execution_findings.md`)
described a fully-working end-to-end. Something in the dependency
stack regressed between then and now — most likely a
`proxystore` minor-version bump that introduced new Protocol
classes, but this needs rhea-side bisection to confirm.

## What I tried (and ruled out)

* **`PARSL_CONTAINER_BACKEND=local`** — set, confirmed it took
  effect (the May fix uses `LocalProvider` instead of docker
  containers). Not the blocker.
* **`RHEA_CONDA_ENVS_DIR=$TMPDIR/apecx-rhea/conda/envs`** — set,
  confirmed the conda env extraction succeeded with this knob.
  Was the FIRST blocker after the PATH fix; no longer.
* **PATH ordering** (`.venv/bin` before anaconda) — fixed the
  Parsl-version-mismatch worker-launch failure. Confirmed
  workers connect after this fix.
* **Killing prior interchange/worker processes** — done; clean
  state every restart.

## Verification matrix (post-fix)

| Layer | Status | Notes |
|---|---|---|
| Workflow YAML shape | ✅ | 16/16 unconditional tests pass |
| MCP catalog registration | ✅ | `input_envelope_key` correctly set |
| MCP wire (request reaches rhea) | ✅ | server starts, request arrives |
| Parsl worker connectivity | ✅ | rhea-side G86 fix; **no operator PATH workaround needed** |
| Conda env extraction | ✅ | with `RHEA_CONDA_ENVS_DIR` env var |
| Tool execution | ✅ | academy 0.4 sync + single-await fix |
| Result transport | ✅ | MUSCLE alignment FASTA round-trips via ProxyStore |
| AlignmentReportStep on real output | ✅ | parses live alignment, reports stats |

## Brutal-truth opinion

The muscle workflow's STRUCTURE is fine on the apecx side. The 16/16
unconditional pass is genuine evidence that everything we own is
correctly wired. The MCP catalog comment that calls out the
`input_envelope_key` silent-failure guard is exactly the kind of
discipline the user wants preserved.

The LIVE path is broken today due to rhea-side regressions (one
fixable by an operator-side PATH tweak, the second requiring rhea
code investigation). This is a real "reliability for adoption"
concern the user explicitly flagged — operators following the May
recipe today will hit these two failures in sequence and the
documented end-to-end won't work.

Follow-ups (now LANDED, 2026-05-16):

1. ✅ Landed the `process_worker_pool.py` PATH-leakage fix in
   `rhea/manager/parsl_config.py` alongside the existing
   `interchange.py` fix. Rhea commit `72193f0`.
2. ✅ Identified the `Protocols cannot be instantiated` cause:
   academy-py was 0.2.0 in rhea's venv but rhea's code expected
   the 0.4 unified-Handle API. `uv sync` upgraded academy
   0.2.0 → 0.4.0; the Protocol class became a concrete class
   in 0.4, auto-fixing the error. Rhea commit `72193f0`.
3. ✅ Identified the follow-on `object RheaOutput can't be used
   in 'await' expression` from academy 0.4's single-await calling
   convention. Fixed 3 double-await call sites (server/utils.py
   + 2 in manager/run.py). Rhea commit `72193f0`.

The apecx-mcp-integration side of the muscle workflow needs no
changes. The verification IS complete end-to-end after the rhea
fixes.
