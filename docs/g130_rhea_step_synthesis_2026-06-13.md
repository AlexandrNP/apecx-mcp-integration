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

## Known gap (tracked separately)
The synthesized `RheaFileToolStep` currently supplies only the file input, not a
tool's other required Galaxy params (e.g. muscle rejects with `muscleArguments:
diags Field required`), so end-to-end tool EXECUTION is not yet proven — only
synthesis + determinism pins are. Follow-up: map all required non-file params from
the tool `inputSchema`. See task E3-R-followup.
