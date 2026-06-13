# G130 — Rhea tool-name → deterministic nanobrain Step synthesis

Status: SHIPPED (nanobrain branch `e2r-rhea-dynamic-deterministic`; apecx-setup
Docker bring-up on `epitope-evidence-workflow`). Full design + determinism mapping:
`nanobrain/docs/rhea_step_synthesis_determinism.md`.

## What it adds
A builder can request a Galaxy-Toolshed tool by name and get a runnable nanobrain
Step with honest determinism pins, instead of hand-authoring a UTD + step YAML.

- `nanobrain.library.tools.rhea_step_synthesizer.synthesize_rhea_step(tool_name,
  mcp_url=, find_tools_query=)` → a from_config-ready Step config (a
  `ToolExecutionStep` for JSON tools, or a `RheaFileToolStep` for file-input tools).
- `WorkflowBuilder.add_rhea_step(name, spec)` — the lightweight DAG seam.
- Determinism: the worker's REAL `version` + `containers` are surfaced over the MCP
  wire (Rhea fork `apecx_provenance` annotation) into the UTD —
  `rhea:<tool>@<real-version>`, R2 when versioned+containerized, R3@unpinned
  otherwise; NEVER a false R1 or a tag-masquerading-as-digest. File-vs-JSON is
  resolved from the worker's `file_input_args`; synthesis FAILS LOUD on ambiguity
  rather than guessing.

## How to use (for future authors / agents)
`apecx-setup` brings up the Rhea worker automatically (Docker, zero user vars;
`RHEA_MCP_URL=http://localhost:3001/mcp/`). Then `synthesize_rhea_step("muscle")`
resolves the live tool. Verified live 2026-06-13: `rhea:muscle@3.8.1551+galaxy0`,
pins real.

## End-to-end tool EXECUTION — CLOSED (E3-R-followup, 2026-06-13)
The earlier gap (synthesized `RheaFileToolStep` supplied only the file input, so muscle
rejected the call with `muscleArguments: diags Field required`) is RESOLVED. The
synthesizer now derives the non-file params **generically from the tool `inputSchema`**:
schema defaults are auto-mapped into `static_tool_args`; a required param with no schema
default that the caller did not supply FAILS LOUD (no silent omission — that was the bug);
caller `static_tool_args={…}` overrides win. The discovered UTD input carries a new
`has_default` flag (`UTDInputSpec.has_default`) so a required-no-default param is
distinguishable from one whose declared default is `null`.

Real end-to-end RUN verified live 2026-06-13 against the `apecx-rhea-server` worker
(`RHEA_MCP_URL=http://localhost:3001/mcp/`, `rhea` repo on client PYTHONPATH):
`synthesize_rhea_step("muscle", static_tool_args={"diags": false})` →
`static_tool_args={cluster: upgmb, outputFormat: fasta, run: "16", iterations: 16,
diags: false}` → `Workflow.run` on a 3-sequence FASTA → a NON-EMPTY aligned FASTA
(`out_align`, 3 records seqA/seqB/seqC, equal-length aligned columns) + `out_align_html`.
MUSCLE ran to completion (`return_code=0`).

- nanobrain files: `library/tools/rhea_step_synthesizer.py` (`_map_value_args`),
  `library/tools/rhea_discovery.py` (`has_default`),
  `core/unified_tool_descriptor.py` (`UTDInputSpec.has_default`).
- Tests: unit `tests/unit/test_rhea_step_synthesizer.py` (defaults-mapped /
  required-no-default FAIL LOUD / override-wins) + `tests/unit/test_rhea_discovery.py`
  (`has_default` pin); live `tests/integration/test_rhea_synthesize_muscle_run_live.py`
  (gated on `$RHEA_MCP_URL` + importable `rhea`).

Note: the client process running `RheaFileToolStep` needs the `rhea` package on
PYTHONPATH (it stages the input via `rhea.utils.proxy.RheaFileProxy`, pickled by module
reference) plus proxystore/redis/cloudpickle — this is a client-side dep, not worker infra.
